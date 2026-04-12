from __future__ import annotations

import json
import logging
import time
from typing import Iterator

from flask import Blueprint, Response, request, stream_with_context

from app.common.validation import as_int, as_str
from app.reco.online.runtime import get_settings
from app.reco.rag_clients import OpenAICompatError
from app.reco.rag_service import get_movie_rag_service


rag_bp = Blueprint("rag", __name__)
logger = logging.getLogger(__name__)


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@rag_bp.post("/recommend/rag/stream")
def recommend_rag_stream():
    payload = request.get_json(silent=True) or {}
    query = as_str(payload.get("query"), name="query").strip()
    n = as_int(payload.get("n", 8), name="n")

    if not query:
        raise ValueError("query cannot be empty")
    if n <= 0:
        raise ValueError("n must be positive")

    settings = get_settings()
    rag_service = get_movie_rag_service(settings)

    @stream_with_context
    def _generate() -> Iterator[str]:
        started_at = time.time()
        yield _sse("start", {"query": query, "n": int(n)})
        try:
            cited_movie_ids, chunks = rag_service.stream_answer(query=query, n=n)
            sent_chars = 0
            for piece in chunks:
                if not piece:
                    continue
                sent_chars += len(piece)
                yield _sse("answer_delta", {"text": piece})

            elapsed_ms = int((time.time() - started_at) * 1000)
            yield _sse(
                "answer_done",
                {
                    "elapsed_ms": elapsed_ms,
                    "cited_movie_ids": cited_movie_ids,
                    "chars": sent_chars,
                },
            )
        except OpenAICompatError as exc:
            logger.exception("RAG LLM request failed")
            yield _sse("error", {"message": str(exc), "type": "llm_error"})
        except Exception as exc:
            logger.exception("RAG stream failed")
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}", "type": "server_error"})

    response = Response(_generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response
