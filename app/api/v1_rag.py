from __future__ import annotations

import json
import time
from typing import Iterator

from flask import Blueprint, Response, request, stream_with_context

from app.common.validation import as_int, as_str
from app.common.settings import Settings
from app.reco.rag_service import get_movie_rag_service


rag_bp = Blueprint("rag", __name__)


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\\ndata: {json.dumps(payload, ensure_ascii=False)}\\n\\n"


def _as_bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
    return default


@rag_bp.post("/recommend/rag/stream")
def recommend_rag_stream():
    """RAG 流式推荐接口。

    文档: POST /api/v1/recommend/rag/stream
    body json:
      - query: str (required)
      - n: int (optional, default 8)
      - rebuild_index: bool (optional, default false)
    """

    payload = request.get_json(silent=True) or {}
    query = as_str(payload.get("query"), name="query").strip()
    n = as_int(payload.get("n", 8), name="n")
    rebuild_index = _as_bool(payload.get("rebuild_index", False), default=False)

    if not query:
        raise ValueError("query cannot be empty")
    if n <= 0:
        raise ValueError("n must be positive")

    settings = Settings.from_config()
    rag_service = get_movie_rag_service(settings)

    @stream_with_context
    def _generate() -> Iterator[str]:
        started_at = time.time()
        yield _sse("start", {"query": query, "n": n})

        count = 0
        for item in rag_service.stream_recommendations(
            query=query,
            n=n,
            force_rebuild=rebuild_index,
        ):
            count += 1
            yield _sse("movie", {"index": count, "item": item.to_dict()})

        elapsed_ms = int((time.time() - started_at) * 1000)
        yield _sse("done", {"count": count, "elapsed_ms": elapsed_ms})

    response = Response(_generate(), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response
