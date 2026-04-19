from __future__ import annotations

import json
import logging
from time import perf_counter
from typing import Iterator

from flask import Blueprint, Response, request, stream_with_context

from app.common.validation import as_str, as_bool
from app.reco.online.runtime import get_settings
from app.reco.rag_clients import OpenAICompatError
from app.reco.rag_service import get_movie_rag_service


rag_bp = Blueprint("rag", __name__)
logger = logging.getLogger(__name__)


def _preview_text(text: str, *, limit: int = 64) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(int(limit) - 3, 0)] + "..."


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@rag_bp.post("/recommend/rag/stream")
def recommend_rag_stream():
    payload = request.get_json(silent=True) or {}
    query = as_str(payload.get("query"), name="query").strip()
    thinking = as_bool(payload.get("thinking", False), name="thinking")

    if not query:
        raise ValueError("query cannot be empty")

    query_preview = _preview_text(query)
    settings = get_settings()
    rag_service = get_movie_rag_service(settings)

    @stream_with_context
    def _generate() -> Iterator[str]:
        started_at = perf_counter()
        chunk_count = 0
        sent_chars = 0
        first_chunk_ms: float | None = None
        try:
            logger.info(
                "RAG stream request started, query_len=%s, query_preview=%s, thinking=%s",
                len(query),
                query_preview,
                bool(thinking),
            )
            logger.info(
                "RAG stream start event emitted, query_len=%s, query_preview=%s, thinking=%s",
                len(query),
                query_preview,
                bool(thinking),
            )
            yield _sse("start", {"query": query, "thinking": bool(thinking)})
            cited_movie_ids, chunks = rag_service.stream_answer(query=query, thinking=thinking)
            logger.info(
                "RAG stream evidence ready, query_len=%s, query_preview=%s, thinking=%s, cited_count=%s, cited_preview=%s",
                len(query),
                query_preview,
                bool(thinking),
                len(cited_movie_ids),
                cited_movie_ids[:5],
            )
            for piece in chunks:
                if not piece:
                    continue
                if first_chunk_ms is None:
                    first_chunk_ms = (perf_counter() - started_at) * 1000.0
                    logger.info(
                        "RAG stream first answer chunk, query_len=%s, query_preview=%s, thinking=%s, first_chunk_ms=%.2f, cited_count=%s",
                        len(query),
                        query_preview,
                        bool(thinking),
                        first_chunk_ms,
                        len(cited_movie_ids),
                    )
                sent_chars += len(piece)
                chunk_count += 1
                yield _sse("answer_delta", {"text": piece})

            elapsed_ms = (perf_counter() - started_at) * 1000.0
            logger.info(
                "RAG stream completed, query_len=%s, query_preview=%s, thinking=%s, cited_count=%s, chunk_count=%s, chars=%s, first_chunk_ms=%s, elapsed_ms=%.2f",
                len(query),
                query_preview,
                bool(thinking),
                len(cited_movie_ids),
                chunk_count,
                sent_chars,
                f"{first_chunk_ms:.2f}" if first_chunk_ms is not None else "n/a",
                elapsed_ms,
            )
            yield _sse(
                "answer_done",
                {
                    "elapsed_ms": int(elapsed_ms),
                    "cited_movie_ids": cited_movie_ids,
                    "chars": sent_chars,
                },
            )
        except OpenAICompatError as exc:
            logger.exception(
                "RAG LLM request failed, query_len=%s, query_preview=%s, thinking=%s, chunk_count=%s, chars=%s, elapsed_ms=%.2f",
                len(query),
                query_preview,
                bool(thinking),
                chunk_count,
                sent_chars,
                (perf_counter() - started_at) * 1000.0,
            )
            yield _sse("error", {"message": str(exc), "type": "llm_error"})
        except Exception as exc:
            logger.exception(
                "RAG stream failed, query_len=%s, query_preview=%s, thinking=%s, chunk_count=%s, chars=%s, elapsed_ms=%.2f",
                len(query),
                query_preview,
                bool(thinking),
                chunk_count,
                sent_chars,
                (perf_counter() - started_at) * 1000.0,
            )
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}", "type": "server_error"})

    response = Response(_generate(), content_type="text/event-stream; charset=utf-8")
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["Connection"] = "keep-alive"
    response.headers["X-Accel-Buffering"] = "no"
    return response
