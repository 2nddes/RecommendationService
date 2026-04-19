from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)


class OpenAICompatError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenAICompatConfig:
    base_url: str
    path: str
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


def _preview_text(text: str, *, limit: int = 120) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(int(limit) - 3, 0)] + "..."


def _extract_chat_content(data: object) -> str:
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
        url=_build_url(cfg.base_url, cfg.path),
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


def stream_chat_completion(*, cfg: OpenAICompatConfig, system_prompt: str, user_prompt: str, thinking: bool = False) -> Iterator[str]:
    if not cfg.base_url:
        raise OpenAICompatError("llm_api_base_url_missing")

    payload = {
        "model": cfg.model,
        "enable_thinking": thinking,
        "stream": True,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = _build_url(cfg.base_url, cfg.path)
    req = Request(
        url=url,
        data=body,
        method="POST",
        headers=_build_headers(cfg.api_key),
    )

    started = perf_counter()
    emitted = False
    emitted_chars = 0
    chunk_count = 0
    first_chunk_ms: float | None = None
    response_content_type: str | None = None
    non_sse_parts: list[str] = []
    try:
        with urlopen(req, timeout=float(cfg.timeout_seconds)) as resp:
            response_content_type = resp.headers.get("Content-Type")
            logger.info(
                "RAG provider request opened, model=%s, url=%s, content_type=%s",
                cfg.model,
                url,
                response_content_type,
            )
            while True:
                raw = resp.readline()
                if not raw:
                    break

                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                if not line.startswith("data:"):
                    if not line.startswith("event:") and not line.startswith(":"):
                        non_sse_parts.append(line)
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
                    chunk_count += 1
                    emitted_chars += len(content)
                    if first_chunk_ms is None:
                        first_chunk_ms = (perf_counter() - started) * 1000.0
                        logger.info(
                            "RAG provider first stream chunk, model=%s, content_type=%s, first_chunk_ms=%.2f",
                            cfg.model,
                            response_content_type,
                            first_chunk_ms,
                        )
                    yield content

        if emitted:
            logger.info(
                "RAG provider stream completed, model=%s, chunk_count=%s, chars=%s, first_chunk_ms=%s, elapsed_ms=%.2f, fallback=%s",
                cfg.model,
                chunk_count,
                emitted_chars,
                f"{first_chunk_ms:.2f}" if first_chunk_ms is not None else "n/a",
                (perf_counter() - started) * 1000.0,
                False,
            )
            return

        full = ""
        fallback_source = "fallback_request"
        raw_body = "\n".join(non_sse_parts).strip()
        if raw_body:
            try:
                full = _extract_chat_content(json.loads(raw_body))
                fallback_source = "initial_response_json"
                logger.warning(
                    "RAG provider ignored stream=true and returned non-SSE JSON, model=%s, content_type=%s, chars=%s, elapsed_ms=%.2f",
                    cfg.model,
                    response_content_type,
                    len(full),
                    (perf_counter() - started) * 1000.0,
                )
            except (json.JSONDecodeError, OpenAICompatError):
                logger.warning(
                    "RAG provider returned non-SSE payload that could not be reused, model=%s, content_type=%s, body_preview=%s, elapsed_ms=%.2f",
                    cfg.model,
                    response_content_type,
                    _preview_text(raw_body),
                    (perf_counter() - started) * 1000.0,
                )

        if not full:
            logger.warning(
                "RAG provider returned no SSE chunks, switching to fallback request, model=%s, content_type=%s, elapsed_ms=%.2f",
                cfg.model,
                response_content_type,
                (perf_counter() - started) * 1000.0,
            )
            full = complete_chat(
                cfg=cfg,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        if full:
            step = 24
            slice_count = 0
            for i in range(0, len(full), step):
                slice_count += 1
                yield full[i : i + step]
            logger.info(
                "RAG provider fallback completed, model=%s, chars=%s, slice_count=%s, source=%s, elapsed_ms=%.2f",
                cfg.model,
                len(full),
                slice_count,
                fallback_source,
                (perf_counter() - started) * 1000.0,
            )
        else:
            logger.info(
                "RAG provider fallback completed, model=%s, chars=%s, slice_count=%s, source=%s, elapsed_ms=%.2f",
                cfg.model,
                0,
                0,
                fallback_source,
                (perf_counter() - started) * 1000.0,
            )
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
        logger.exception(
            "RAG provider stream HTTP error, model=%s, url=%s, status=%s",
            cfg.model,
            url,
            getattr(exc, "code", None),
        )
        raise OpenAICompatError(f"http_error: status={getattr(exc, 'code', 'unknown')}, detail={detail[:300]}") from exc
    except URLError as exc:
        logger.exception("RAG provider stream URL error, model=%s, url=%s", cfg.model, url)
        raise OpenAICompatError(f"network_error: {exc}") from exc
    except Exception as exc:
        logger.exception("RAG provider stream request failed, model=%s, url=%s", cfg.model, url)
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
        url=_build_url(cfg.base_url, cfg.path),
        payload=payload,
        api_key=cfg.api_key,
        timeout_seconds=float(cfg.timeout_seconds),
    )
    return _extract_chat_content(data)
