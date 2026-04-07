from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from flask import Blueprint, request
from sqlalchemy import bindparam, create_engine, text

from app.common.responses import ok
from app.common.validation import ParamError, as_int
from app.reco.online.runtime import get_settings

search_bp = Blueprint("search", __name__)
logger = logging.getLogger(__name__)


@lru_cache(maxsize=2)
def _get_engine(mysql_dsn: str):
    return create_engine(mysql_dsn, pool_pre_ping=True)


def _as_passthrough_params() -> dict[str, Any]:
    passthrough: dict[str, Any] = {}
    reserved = {"query", "n", "offset"}
    for key, values in request.args.lists():
        if key in reserved:
            continue
        cleaned = [str(v) for v in values]
        if not cleaned:
            continue
        passthrough[key] = cleaned[0] if len(cleaned) == 1 else cleaned
    return passthrough


def _build_search_sql(*, query: str, n: int, offset: int, tags: list[str]):
    query = query.strip()
    has_query = bool(query)
    params: dict[str, Any] = {
        "n": int(n),
        "offset": int(offset),
    }
    if has_query:
        params["query_like"] = f"%{query}%"

    tokens = [tok.strip() for tok in query.split() if tok.strip()]
    # 避免重复 token 导致 SQL 条件膨胀。
    tokens = list(dict.fromkeys(tokens))

    token_all_terms: list[str] = []
    token_score_terms: list[str] = []
    for idx, token in enumerate(tokens):
        pname = f"kw_{idx}"
        params[pname] = f"%{token}%"
        token_all_terms.append(f"(m.title LIKE :{pname} OR m.summary LIKE :{pname})")
        token_score_terms.append(
            f"CASE WHEN m.title LIKE :{pname} THEN 2 "
            f"WHEN m.summary LIKE :{pname} THEN 1 ELSE 0 END"
        )

    where_clauses = [
        "m.status = 'published'",
        "m.deleted_at IS NULL",
    ]

    if has_query:
        query_match = "(m.title LIKE :query_like OR m.summary LIKE :query_like)"
        if token_all_terms:
            query_match = f"({query_match} OR ({' AND '.join(token_all_terms)}))"
        where_clauses.append(query_match)

    if tags:
        params["tags"] = tags
        where_clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM movie_tag mt
                JOIN tag_dict td ON td.tag_id = mt.tag_id
                WHERE mt.movie_id = m.movie_id
                  AND td.status = 'show'
                  AND td.tag_name IN :tags
            )
            """
        )

    where_sql = " AND ".join(where_clauses)
    score_parts = [
        "COALESCE((COALESCE(m.rating_sum, 0) / NULLIF(m.rating_count, 0)), 0) / 10.0",
        "LEAST(COALESCE(m.rating_count, 0), 10000) / 10000.0",
    ]
    if has_query:
        score_parts.insert(0, "CASE WHEN m.title LIKE :query_like THEN 6 ELSE 0 END")
        score_parts.insert(1, "CASE WHEN m.summary LIKE :query_like THEN 3 ELSE 0 END")
        score_parts.extend(token_score_terms)
    score_expr = " + ".join(score_parts)

    select_sql = text(
        f"""
        SELECT
          m.movie_id,
          m.title,
          m.year,
          COALESCE(m.summary, '') AS summary,
          COALESCE((COALESCE(m.rating_sum, 0) / NULLIF(m.rating_count, 0)), 0) AS rating_avg,
          COALESCE(m.rating_count, 0) AS rating_count,
          ({score_expr}) AS score
        FROM movie m
        WHERE {where_sql}
        ORDER BY score DESC, m.rating_count DESC, m.movie_id DESC
        LIMIT :n OFFSET :offset
        """
    )

    count_sql = text(
        f"""
        SELECT COUNT(1) AS total
        FROM movie m
        WHERE {where_sql}
        """
    )

    if tags:
        select_sql = select_sql.bindparams(bindparam("tags", expanding=True))
        count_sql = count_sql.bindparams(bindparam("tags", expanding=True))

    return select_sql, count_sql, params


@search_bp.get("/search")
def search():
    """
    文档: GET /api/v1/search
    query params:
      - query: str (optional)
      - n: int (optional, default 20)
      - offset: int (optional, default 0)
      - 其他参数: 透传，支持多值（例如 tag=a&tag=b）
    """
    query_raw = request.args.get("query")
    query = (query_raw or "").strip()
    tags = [t.strip() for t in request.args.getlist("tag") if str(t).strip()]
    if not query and not tags:
        raise ParamError("at least one of 'query' or 'tag' is required")

    n_raw = request.args.get("n")
    offset_raw = request.args.get("offset")

    n = 20 if n_raw is None else as_int(n_raw, name="n")
    offset = 0 if offset_raw is None else as_int(offset_raw, name="offset")

    if n <= 0:
        raise ParamError("invalid 'n', expected positive integer")
    if offset < 0:
        raise ParamError("invalid 'offset', expected non-negative integer")

    passthrough = _as_passthrough_params()

    settings = get_settings()
    mysql_dsn = str(settings.core.mysql_dsn or "").strip()
    if not mysql_dsn:
        raise RuntimeError("MYSQL_DSN is required for search")

    logger.info(
        "search started, query_len=%s, n=%s, offset=%s, passthrough_keys=%s",
        len(query),
        n,
        offset,
        sorted(list(passthrough.keys())),
    )

    select_sql, count_sql, params = _build_search_sql(
        query=query,
        n=n,
        offset=offset,
        tags=tags,
    )

    engine = _get_engine(mysql_dsn)
    with engine.connect() as conn:
        total = int(conn.execute(count_sql, params).scalar_one())
        rows = conn.execute(select_sql, params).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        m = row._mapping
        summary = str(m["summary"] or "").strip()
        if len(summary) > 300:
            summary = summary[:300] + "..."

        results.append(
            {
                "movie_id": int(m["movie_id"]),
                "title": str(m["title"] or ""),
                "year": int(m["year"]) if m["year"] is not None else None,
                "summary": summary,
                "rating_avg": float(m["rating_avg"]) if m["rating_avg"] is not None else None,
                "rating_count": int(m["rating_count"] or 0),
                "score": float(m["score"] or 0.0),
            }
        )

    logger.info(
        "search completed, query_len=%s, total=%s, returned=%s",
        len(query),
        total,
        len(results),
    )

    data = {
        "query": query,
        "n": n,
        "offset": offset,
        "passthrough": passthrough,
        "total": total,
        "results": results,
    }
    return ok(data)
