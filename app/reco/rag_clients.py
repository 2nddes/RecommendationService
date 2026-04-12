from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)


class OpenAICompatError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenAICompatConfig:
    base_url: str
    api_key: str | None
    model: str
    timeout_seconds: float = 30.0


def _build_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _build_headers(api_key: str | None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _post_json(*, url: str, payload: dict, api_key: str | None, timeout_seconds: float):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url=url, data=body, method="POST", headers=_build_headers(api_key))
    try:
        with urlopen(req, timeout=timeout_seconds) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
        logger.exception("OpenAI-compatible HTTP error, url=%s, status=%s", url, getattr(exc, "code", None))
        raise OpenAICompatError(f"http_error: status={getattr(exc, 'code', 'unknown')}, detail={detail[:300]}") from exc
    except URLError as exc:
        logger.exception("OpenAI-compatible URL error, url=%s", url)
        raise OpenAICompatError(f"network_error: {exc}") from exc
    except Exception as exc:
        logger.exception("OpenAI-compatible request failed, url=%s", url)
        raise OpenAICompatError(f"request_failed: {type(exc).__name__}: {exc}") from exc


def create_embedding(*, cfg: OpenAICompatConfig, text: str) -> list[float]:
    if not cfg.base_url:
        raise OpenAICompatError("embedding_api_base_url_missing")

    payload = {
        "model": cfg.model,
        "input": text,
    }
    data = _post_json(
        url=_build_url(cfg.base_url, "/embeddings"),
        payload=payload,
        api_key=cfg.api_key,
        timeout_seconds=float(cfg.timeout_seconds),
    )
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise OpenAICompatError("embedding_response_invalid")

    emb = rows[0].get("embedding") if isinstance(rows[0], dict) else None
    if not isinstance(emb, list) or not emb:
        raise OpenAICompatError("embedding_vector_missing")

    out: list[float] = []
    for item in emb:
        out.append(float(item))
    return out


def stream_chat_completion(*, cfg: OpenAICompatConfig, system_prompt: str, user_prompt: str) -> Iterator[str]:
    if not cfg.base_url:
        raise OpenAICompatError("llm_api_base_url_missing")

    payload = {
        "model": cfg.model,
        "stream": True,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        url=_build_url(cfg.base_url, "/chat/completions"),
        data=body,
        method="POST",
        headers=_build_headers(cfg.api_key),
    )

    emitted = False
    try:
        with urlopen(req, timeout=float(cfg.timeout_seconds)) as resp:
            while True:
                raw = resp.readline()
                if not raw:
                    break

                line = raw.decode("utf-8", errors="ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[len("data:") :].strip()
                if chunk == "[DONE]":
                    break

                try:
                    packet = json.loads(chunk)
                except json.JSONDecodeError:
                    continue

                choices = packet.get("choices") if isinstance(packet, dict) else None
                if not isinstance(choices, list) or not choices:
                    continue
                first = choices[0] if isinstance(choices[0], dict) else {}
                delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    emitted = True
                    yield content

        if emitted:
            return

        # Fallback for providers that ignore stream=true
        full = complete_chat(
            cfg=cfg,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        if full:
            step = 24
            for i in range(0, len(full), step):
                yield full[i : i + step]
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
        raise OpenAICompatError(f"http_error: status={getattr(exc, 'code', 'unknown')}, detail={detail[:300]}") from exc
    except URLError as exc:
        raise OpenAICompatError(f"network_error: {exc}") from exc
    except Exception as exc:
        raise OpenAICompatError(f"request_failed: {type(exc).__name__}: {exc}") from exc


def complete_chat(*, cfg: OpenAICompatConfig, system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": cfg.model,
        "stream": False,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    data = _post_json(
        url=_build_url(cfg.base_url, "/chat/completions"),
        payload=payload,
        api_key=cfg.api_key,
        timeout_seconds=float(cfg.timeout_seconds),
    )
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise OpenAICompatError("chat_response_invalid")

    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    raise OpenAICompatError("chat_content_missing")
