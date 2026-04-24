from __future__ import annotations

from datetime import date, timedelta
import logging
from typing import Any

from flask import Blueprint, request

from app.common.responses import ok
from app.common.validation import ParamError, as_date, as_int
from app.reco.online.runtime import get_settings
from app.services.search_service import SearchService


search_bp = Blueprint("search", __name__)
logger = logging.getLogger(__name__)


_SORT_BY_ALIASES = {
    "": "default",
    "default": "default",
    "composite": "default",
    "relevance": "default",
    "score": "default",
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


def _as_passthrough_params() -> dict[str, Any]:
    passthrough: dict[str, Any] = {}
    reserved = {
        "query",
        "n",
        "offset",
        "tag_id",
        "tag_ids",
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


def _parse_tag_ids() -> list[int]:
    out: list[int] = []
    for key in ("tag_id", "tag_ids"):
        for raw in request.args.getlist(key):
            raw_text = str(raw or "").strip()
            if not raw_text:
                continue
            parts = [part.strip() for part in raw_text.split(",") if part.strip()] if key == "tag_ids" else [raw_text]
            for part in parts:
                out.append(as_int(part, name=key))
    return list(dict.fromkeys(out))


@search_bp.get("/search")
def search():
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

    tag_ids = _parse_tag_ids()

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

    service = SearchService(settings)
    execution = service.search(
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
    )

    logger.info(
        "search completed, mode=%s, query_len=%s, tag_count=%s, sort_by=%s, sort_order=%s, total=%s, returned=%s, strategy=%s, cache_hit=%s",
        search_mode,
        len(query),
        len(tag_ids),
        sort_by,
        sort_order,
        execution.total,
        len(execution.results),
        execution.strategy,
        execution.cache_hit,
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
        "total": execution.total,
        "results": execution.results,
    }
    return ok(data)
