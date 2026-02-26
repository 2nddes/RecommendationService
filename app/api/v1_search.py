from __future__ import annotations

from flask import Blueprint, request

from app.common.responses import ok, fail
from app.common.validation import as_int, as_str

search_bp = Blueprint("search", __name__)


@search_bp.post("/search")
def search():
    # TODO
    """
    文档: POST /api/v1/search
    json body:
      - query: str (required)
      - n: int (optional, default 20)
      - filters: object (optional)
    """

    payload = request.get_json(silent=True) or {}

    if "query" not in payload:
        return fail(message="Missing required parameter: query")

    query = as_str(payload.get("query"), name="query")
    n = as_int(payload.get("n", 20), name="n")
    filters = payload.get("filters") or {}

    # 占位返回：不实现语义检索/向量召回
    data = {
        "query": query,
        "n": n,
        "filters": filters,
        "total": 0,
        "results": [],
    }
    return ok(data)
