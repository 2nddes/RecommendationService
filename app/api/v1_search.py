from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from functools import lru_cache
from time import perf_counter
from typing import Any

from flask import Blueprint, request
from sqlalchemy import bindparam, create_engine, text

from app.common.responses import ok
from app.common.validation import ParamError, as_date, as_int
from app.reco.online.runtime import get_settings

search_bp = Blueprint("search", __name__)
logger = logging.getLogger(__name__)

_SORT_BY_ALIASES = {
    "default": "relevance",
    "relevance": "relevance",
    "score": "relevance",
    "rating": "rating",
    "bayesian_rating": "rating",
    "collect": "collect",
    "collect_count": "collect",
    "favorite": "collect",
    "favourite": "collect",
    "duration": "duration",
    "duration_min": "duration",
    "time": "time",
    "release_date": "time",
}

_TIME_WINDOW_ALIASES = {
    "week": "weekly",
    "weekly": "weekly",
    "month": "monthly",
    "monthly": "monthly",
    "half_year": "half_year",
    "halfyear": "half_year",
    "half-year": "half_year",
}

@lru_cache(maxsize=2)
def _get_engine(mysql_dsn: str):
    return create_engine(mysql_dsn, pool_pre_ping=True)


_GLOBAL_RATING_AVG_SQL = text(
    """
    SELECT AVG(COALESCE(m.rating_sum, 0) / NULLIF(m.rating_count, 0)) AS global_avg
    FROM movie m
    WHERE m.status = 'published'
      AND m.deleted_at IS NULL
      AND m.rating_count > 0
    """
)


@lru_cache(maxsize=2)
def _get_global_rating_avg(mysql_dsn: str) -> float:
    engine = _get_engine(mysql_dsn)
    with engine.connect() as conn:
        value = conn.execute(_GLOBAL_RATING_AVG_SQL).scalar_one_or_none()
    return float(value or 0.0)


def _bayesian_weighted_rating(
    *,
    rating_sum: float,
    rating_count: int,
    global_avg: float,
    min_votes: int,
) -> float:
    count = max(int(rating_count), 0)
    if count <= 0:
        return 0.0

    votes_floor = max(int(min_votes), 0)
    avg_rating = float(rating_sum) / float(count)
    total_weight = count + votes_floor
    if total_weight <= 0:
        return avg_rating

    return (
        (float(count) / float(total_weight)) * avg_rating
        + (float(votes_floor) / float(total_weight)) * float(global_avg)
    )


def _as_passthrough_params() -> dict[str, Any]:
    passthrough: dict[str, Any] = {}
    reserved = {
        "query",
        "n",
        "offset",
        "tag_id",
        "sort_by",
        "sort_order",
        "time_window",
        "start_date",
        "end_date",
        "duration_min",
        "duration_max",
    }
    for key, values in request.args.lists():
        if key in reserved:
            continue
        cleaned = [str(v) for v in values]
        if not cleaned:
            continue
        passthrough[key] = cleaned[0] if len(cleaned) == 1 else cleaned
    return passthrough


def _normalize_sort_by(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return "relevance"
    normalized = _SORT_BY_ALIASES.get(value)
    if normalized is None:
        raise ParamError("invalid 'sort_by'")
    return normalized


def _normalize_sort_order(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return "desc"
    if value not in {"asc", "desc"}:
        raise ParamError("invalid 'sort_order'")
    return value


def _normalize_window(raw: str | None, *, name: str, mapping: dict[str, str]) -> str | None:
    value = str(raw or "").strip().lower()
    if not value:
        return None
    normalized = mapping.get(value)
    if normalized is None:
        raise ParamError(f"invalid '{name}'")
    return normalized


def _optional_date(raw: str | None, *, name: str) -> date | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    return as_date(value, name=name)


def _optional_int(raw: str | None, *, name: str) -> int | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    return as_int(value, name=name)


def _resolve_release_date_range(
    *,
    time_window: str | None,
    start_date: date | None,
    end_date: date | None,
) -> tuple[date | None, date | None]:
    if time_window is not None and (start_date is not None or end_date is not None):
        raise ParamError("invalid time filters, 'time_window' cannot be combined with 'start_date'/'end_date'")

    applied_start = start_date
    applied_end = end_date
    if time_window is not None:
        today = date.today()
        if time_window == "weekly":
            applied_start = today - timedelta(days=7)
        elif time_window == "monthly":
            applied_start = today - timedelta(days=30)
        elif time_window == "half_year":
            applied_start = today - timedelta(days=180)
        applied_end = today

    if applied_start is not None and applied_end is not None and applied_start > applied_end:
        raise ParamError("invalid date range, expected 'start_date' <= 'end_date'")
    return applied_start, applied_end

def _has_active_filters(filters: dict[str, Any]) -> bool:
    for value in filters.values():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False


def _resolve_search_mode(*, query: str, tag_ids: list[int], has_filters: bool) -> str:
    has_query = bool(str(query).strip())
    has_tags = bool(tag_ids)
    if has_query and has_tags:
        return "mixed"
    if has_query:
        return "query_only"
    if has_tags:
        return "tag_only"
    if has_filters:
        return "filter_only"
    return "browse"


def _build_search_base_sql(
    *,
    query: str,
    tag_ids: list[int],
    release_start_date: date | None,
    release_end_date: date | None,
    duration_min: int | None,
    duration_max: int | None,
) -> tuple[dict[str, Any], str, str]:
    query = query.strip()
    has_query = bool(query)

    params: dict[str, Any] = {}
    if has_query:
        params["query_like"] = f"%{query}%"

    tokens = [tok.strip() for tok in query.split() if tok.strip()]
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

    if tag_ids:
        params["tag_ids"] = tag_ids
        where_clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM movie_tag mt
                WHERE mt.movie_id = m.movie_id
                  AND mt.tag_id IN :tag_ids
            )
            """
        )

    if release_start_date is not None:
        params["release_start_date"] = release_start_date
        where_clauses.append("m.release_date >= :release_start_date")
    if release_end_date is not None:
        params["release_end_date"] = release_end_date
        where_clauses.append("m.release_date <= :release_end_date")
    if duration_min is not None:
        params["duration_min"] = int(duration_min)
        where_clauses.append("m.duration_min >= :duration_min")
    if duration_max is not None:
        params["duration_max"] = int(duration_max)
        where_clauses.append("m.duration_min <= :duration_max")

    where_sql = " AND ".join(where_clauses)

    relevance_score_parts = [
        "COALESCE((COALESCE(m.rating_sum, 0) / NULLIF(m.rating_count, 0)), 0) / 10.0",
        "LEAST(COALESCE(m.rating_count, 0), 10000) / 10000.0",
    ]
    if has_query:
        relevance_score_parts.insert(0, "CASE WHEN m.title LIKE :query_like THEN 6 ELSE 0 END")
        relevance_score_parts.insert(1, "CASE WHEN m.summary LIKE :query_like THEN 3 ELSE 0 END")
        relevance_score_parts.extend(token_score_terms)
    relevance_score_expr = " + ".join(relevance_score_parts)

    return params, where_sql, relevance_score_expr


def _build_ranked_movies_cte(
    *,
    where_sql: str,
    relevance_score_expr: str,
    include_collect_metrics: bool,
    include_bayesian_rating: bool,
    params: dict[str, Any],
) -> str:
    cte_parts = [
        f"""
        filtered_movies AS (
            SELECT
              m.movie_id,
              COALESCE(m.rating_sum, 0) AS rating_sum,
              COALESCE(m.rating_count, 0) AS rating_count,
              m.release_date,
              m.duration_min,
              ({relevance_score_expr}) AS relevance_score
            FROM movie m
            WHERE {where_sql}
        )
        """
    ]

    if include_bayesian_rating:
        cte_parts.append(
            """
        global_stats AS (
            SELECT AVG(COALESCE(mm.rating_sum, 0) / NULLIF(mm.rating_count, 0)) AS global_avg
            FROM movie mm
            WHERE mm.status = 'published'
              AND mm.deleted_at IS NULL
              AND mm.rating_count > 0
        )
            """
        )

    if include_collect_metrics:
        cte_parts.append(
            """
        collect_stats AS (
            SELECT ucm.movie_id, COUNT(*) AS collect_count
            FROM user_collect_movie ucm
            JOIN filtered_movies fm ON fm.movie_id = ucm.movie_id
            GROUP BY ucm.movie_id
        )
            """
        )

    bayesian_rating_expr = "0"
    if include_bayesian_rating:
        bayesian_rating_expr = (
            "CASE "
            "WHEN fm.rating_count <= 0 THEN 0 "
            "ELSE "
            "((fm.rating_count / NULLIF(fm.rating_count + :bayesian_min_votes, 0)) "
            " * (fm.rating_sum / NULLIF(fm.rating_count, 0))) "
            "+ ((:bayesian_min_votes / NULLIF(fm.rating_count + :bayesian_min_votes, 0)) * COALESCE(gs.global_avg, 0)) "
            "END"
        )

    collect_count_expr = "0"
    if include_collect_metrics:
        collect_count_expr = "COALESCE(cs.collect_count, 0)"

    ranked_from_lines = ["FROM filtered_movies fm"]
    if include_bayesian_rating:
        ranked_from_lines.append("CROSS JOIN global_stats gs")
    if include_collect_metrics:
        ranked_from_lines.append("LEFT JOIN collect_stats cs ON cs.movie_id = fm.movie_id")

    cte_parts.append(
        f"""
        ranked_movies AS (
            SELECT
              fm.movie_id,
              fm.rating_count,
              fm.release_date,
              fm.duration_min,
              fm.relevance_score,
              {bayesian_rating_expr} AS bayesian_rating,
              {collect_count_expr} AS collect_count,
            {' '.join(ranked_from_lines)}
        )
        """
    )

    return "WITH\n" + ",\n".join(part.strip() for part in cte_parts)


def _build_order_sql(*, sort_by: str, sort_order: str) -> str:
    if sort_by == "rating":
        return (
            f"ranked_movies.bayesian_rating {sort_order}, ranked_movies.rating_count DESC, "
            "ranked_movies.relevance_score DESC, ranked_movies.movie_id DESC"
        )
    if sort_by == "collect":
        return (
            f"ranked_movies.collect_count {sort_order}, ranked_movies.rating_count DESC, "
            "ranked_movies.relevance_score DESC, ranked_movies.movie_id DESC"
        )
    if sort_by == "duration":
        return (
            f"COALESCE(ranked_movies.duration_min, 0) {sort_order}, ranked_movies.relevance_score DESC, "
            "ranked_movies.rating_count DESC, ranked_movies.movie_id DESC"
        )
    if sort_by == "time":
        return (
            f"(ranked_movies.release_date IS NULL) ASC, ranked_movies.release_date {sort_order}, "
            "ranked_movies.relevance_score DESC, ranked_movies.rating_count DESC, ranked_movies.movie_id DESC"
        )

    return f"ranked_movies.relevance_score {sort_order}, ranked_movies.rating_count DESC, ranked_movies.movie_id DESC"


def _build_search_sql(
    *,
    query: str,
    n: int,
    offset: int,
    tag_ids: list[int],
    sort_by: str,
    sort_order: str,
    release_start_date: date | None,
    release_end_date: date | None,
    duration_min: int | None,
    duration_max: int | None,
    bayesian_min_votes: int,
):
    base_params, where_sql, relevance_score_expr = _build_search_base_sql(
        query=query,
        tag_ids=tag_ids,
        release_start_date=release_start_date,
        release_end_date=release_end_date,
        duration_min=duration_min,
        duration_max=duration_max,
    )

    candidate_params = dict(base_params)
    candidate_params["n"] = int(n)
    candidate_params["offset"] = int(offset)
    if sort_by == "rating":
        candidate_params["bayesian_min_votes"] = max(int(bayesian_min_votes), 0)

    candidate_with_sql = _build_ranked_movies_cte(
        where_sql=where_sql,
        relevance_score_expr=relevance_score_expr,
        include_collect_metrics=(sort_by == "collect"),
        include_bayesian_rating=(sort_by == "rating"),
        params=candidate_params,
    )
    candidate_sql = text(
        f"""
        {candidate_with_sql}
        SELECT ranked_movies.movie_id
        FROM ranked_movies
        ORDER BY {_build_order_sql(sort_by=sort_by, sort_order=sort_order)}
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

    detail_sql = text(
        f"""
        SELECT
          m.movie_id,
          m.title,
          m.year,
          m.release_date,
          m.duration_min,
          m.poster,
          COALESCE(m.summary, '') AS summary,
          COALESCE(m.rating_sum, 0) AS rating_sum,
          COALESCE(m.rating_count, 0) AS rating_count,
          ({relevance_score_expr}) AS score
        FROM movie m
        WHERE m.movie_id IN :movie_ids
          AND m.status = 'published'
          AND m.deleted_at IS NULL
        """
    ).bindparams(bindparam("movie_ids", expanding=True))

    collect_sql = text(
        """
        SELECT ucm.movie_id, COUNT(*) AS collect_count
        FROM user_collect_movie ucm
        WHERE ucm.movie_id IN :movie_ids
        GROUP BY ucm.movie_id
        """
    ).bindparams(bindparam("movie_ids", expanding=True))

    if tag_ids:
        candidate_sql = candidate_sql.bindparams(bindparam("tag_ids", expanding=True))
        count_sql = count_sql.bindparams(bindparam("tag_ids", expanding=True))

    return candidate_sql, count_sql, detail_sql, collect_sql, candidate_params, base_params


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
    query = str(request.args.get("query") or "").strip()
    sort_by = _normalize_sort_by(request.args.get("sort_by"))
    sort_order = _normalize_sort_order(request.args.get("sort_order"))
    time_window = _normalize_window(request.args.get("time_window"), name="time_window", mapping=_TIME_WINDOW_ALIASES)

    start_date = _optional_date(request.args.get("start_date"), name="start_date")
    end_date = _optional_date(request.args.get("end_date"), name="end_date")
    release_start_date, release_end_date = _resolve_release_date_range(
        time_window=time_window,
        start_date=start_date,
        end_date=end_date,
    )

    duration_min = _optional_int(request.args.get("duration_min"), name="duration_min")
    duration_max = _optional_int(request.args.get("duration_max"), name="duration_max")

    if duration_min is not None and duration_min < 0:
        raise ParamError("invalid 'duration_min', expected non-negative integer")
    if duration_max is not None and duration_max < 0:
        raise ParamError("invalid 'duration_max', expected non-negative integer")
    if duration_min is not None and duration_max is not None and duration_min > duration_max:
        raise ParamError("invalid duration range, expected 'duration_min' <= 'duration_max'")

    tag_ids: list[int] = []
    for raw in request.args.getlist("tag_id"):
        raw_text = str(raw).strip()
        if not raw_text:
            continue
        tag_ids.append(as_int(raw_text, name="tag_id"))
    tag_ids = list(dict.fromkeys(tag_ids))

    n = as_int(request.args.get("n", 20), name="n")
    offset = as_int(request.args.get("offset", 0), name="offset")
    if n <= 0:
        raise ParamError("invalid 'n', expected positive integer")
    if offset < 0:
        raise ParamError("invalid 'offset', expected non-negative integer")

    active_filters = {
        "time_window": time_window,
        "release_start_date": str(release_start_date) if release_start_date is not None else None,
        "release_end_date": str(release_end_date) if release_end_date is not None else None,
        "duration_min": duration_min,
        "duration_max": duration_max,
    }
    has_filters = _has_active_filters(active_filters)
    if not query and not tag_ids and not has_filters and sort_by == "relevance":
        raise ParamError("at least one search keyword, tag, filter, or non-default sort is required")

    passthrough = _as_passthrough_params()

    settings = get_settings()
    mysql_dsn = str(settings.core.mysql_dsn or "").strip()
    if not mysql_dsn:
        raise RuntimeError("MYSQL_DSN is required for search")

    search_mode = _resolve_search_mode(query=query, tag_ids=tag_ids, has_filters=has_filters)
    logger.info(
        "search started, mode=%s, query_len=%s, tag_count=%s, sort_by=%s, sort_order=%s, n=%s, offset=%s, passthrough_keys=%s",
        search_mode,
        len(query),
        len(tag_ids),
        sort_by,
        sort_order,
        n,
        offset,
        sorted(list(passthrough.keys())),
    )

    bayesian_min_votes = max(int(getattr(settings.tag_recall, "min_rating_count_m", 100) or 100), 0)
    candidate_sql, count_sql, detail_sql, collect_sql, candidate_params, count_params = _build_search_sql(
        query=query,
        n=n,
        offset=offset,
        tag_ids=tag_ids,
        sort_by=sort_by,
        sort_order=sort_order,
        release_start_date=release_start_date,
        release_end_date=release_end_date,
        duration_min=duration_min,
        duration_max=duration_max,
        bayesian_min_votes=bayesian_min_votes,
    )

    engine = _get_engine(mysql_dsn)
    started = perf_counter()
    count_ms = 0.0
    candidate_ms = 0.0
    detail_ms = 0.0
    global_rating_avg = 0.0
    movie_ids: list[int] = []
    detail_rows: list[Any] = []
    collect_rows: list[Any] = []
    with engine.connect() as conn:
        count_started = perf_counter()
        total = int(conn.execute(count_sql, count_params).scalar_one())
        count_ms = (perf_counter() - count_started) * 1000.0

        candidate_started = perf_counter()
        candidate_rows = conn.execute(candidate_sql, candidate_params).mappings().all()
        candidate_ms = (perf_counter() - candidate_started) * 1000.0

        movie_ids = [int(row["movie_id"]) for row in candidate_rows if row.get("movie_id") is not None]
        if movie_ids:
            detail_started = perf_counter()
            global_rating_avg = _get_global_rating_avg(mysql_dsn)
            page_detail_params = dict(count_params)
            page_detail_params["movie_ids"] = movie_ids
            detail_rows = conn.execute(detail_sql, page_detail_params).mappings().all()
            collect_rows = conn.execute(collect_sql, {"movie_ids": movie_ids}).mappings().all()
            detail_ms = (perf_counter() - detail_started) * 1000.0

    results: list[dict[str, Any]] = []
    detail_by_movie_id = {
        int(row["movie_id"]): row
        for row in detail_rows
        if row.get("movie_id") is not None
    }
    collect_count_by_movie_id = {
        int(row["movie_id"]): max(int(row.get("collect_count") or 0), 0)
        for row in collect_rows
        if row.get("movie_id") is not None
    }
    for movie_id in movie_ids:
        m = detail_by_movie_id.get(int(movie_id))
        if m is None:
            continue
        summary = str(m["summary"] or "").strip()
        if len(summary) > 300:
            summary = summary[:300] + "..."

        rating_sum = float(m["rating_sum"] or 0.0)
        rating_count = max(int(m["rating_count"] or 0), 0)
        rating_avg = None
        if rating_count > 0:
            rating_avg = rating_sum / float(rating_count)
        results.append(
            {
                "movie_id": int(m["movie_id"]),
                "title": str(m["title"] or ""),
                "year": int(m["year"]) if m["year"] is not None else None,
                "release_date": str(m["release_date"]) if m["release_date"] is not None else None,
                "duration_min": int(m["duration_min"]) if m["duration_min"] is not None else None,
                "poster": str(m["poster"] or ""),
                "summary": summary,
                "rating_avg": rating_avg,
                "rating_count": rating_count,
                "bayesian_rating": _bayesian_weighted_rating(
                    rating_sum=rating_sum,
                    rating_count=rating_count,
                    global_avg=global_rating_avg,
                    min_votes=bayesian_min_votes,
                ),
                "collect_count": collect_count_by_movie_id.get(int(movie_id), 0),
                "score": float(m["score"] or 0.0),
            }
        )

    logger.info(
        "search completed, mode=%s, query_len=%s, tag_count=%s, sort_by=%s, sort_order=%s, total=%s, returned=%s, count_ms=%.2f, candidate_ms=%.2f, detail_ms=%.2f, elapsed_ms=%.2f",
        search_mode,
        len(query),
        len(tag_ids),
        sort_by,
        sort_order,
        total,
        len(results),
        count_ms,
        candidate_ms,
        detail_ms,
        (perf_counter() - started) * 1000.0,
    )

    data = {
        "query": query,
        "tag_ids": tag_ids,
        "n": n,
        "offset": offset,
        "sort": {
            "by": sort_by,
            "order": sort_order,
        },
        "filters": {
            "release_date": {
                "time_window": time_window,
                "start_date": str(release_start_date) if release_start_date is not None else None,
                "end_date": str(release_end_date) if release_end_date is not None else None,
            },
            "duration_min": duration_min,
            "duration_max": duration_max,
        },
        "passthrough": passthrough,
        "total": total,
        "results": results,
    }
    return ok(data)
