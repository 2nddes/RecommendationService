from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
import logging
from time import perf_counter
from typing import Any

from app.common.settings import Settings
from app.repositories.cache_repository import CacheRepository
from app.repositories.search_repository import SearchPage, SearchRepository


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchRequest:
    query: str
    n: int
    offset: int
    tag_ids: tuple[int, ...]
    sort_by: str
    sort_order: str
    release_start_date: str | None
    release_end_date: str | None
    duration_min: int | None
    duration_max: int | None


@dataclass(frozen=True)
class SearchExecution:
    total: int
    results: list[dict[str, Any]]
    strategy: str
    cache_hit: bool


class SearchService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache_repo = CacheRepository(settings)
        self._repo = SearchRepository(settings.core.mysql_dsn)

    def search(
        self,
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
    ) -> SearchExecution:
        request = SearchRequest(
            query=str(query or "").strip(),
            n=int(n),
            offset=int(offset),
            tag_ids=tuple(int(tag_id) for tag_id in tag_ids),
            sort_by=str(sort_by),
            sort_order=str(sort_order),
            release_start_date=str(release_start_date) if release_start_date is not None else None,
            release_end_date=str(release_end_date) if release_end_date is not None else None,
            duration_min=int(duration_min) if duration_min is not None else None,
            duration_max=int(duration_max) if duration_max is not None else None,
        )
        cache_signature = self._build_cache_signature(request)
        started = perf_counter()

        if self._is_cacheable(request):
            cached = self._cache_repo.load_search_result(signature=cache_signature)
            if isinstance(cached, dict):
                logger.info(
                    "Search cache hit, signature=%s, total=%s, returned=%s, elapsed_ms=%.2f",
                    cache_signature[:12],
                    int(cached.get("total") or 0),
                    len(cached.get("results") or []),
                    (perf_counter() - started) * 1000.0,
                )
                return SearchExecution(
                    total=int(cached.get("total") or 0),
                    results=list(cached.get("results") or []),
                    strategy=str(cached.get("strategy") or "cache"),
                    cache_hit=True,
                )

        page = self._repo.search(
            query=request.query,
            n=request.n,
            offset=request.offset,
            tag_ids=list(request.tag_ids),
            sort_by=request.sort_by,
            sort_order=request.sort_order,
            release_start_date=release_start_date,
            release_end_date=release_end_date,
            duration_min=duration_min,
            duration_max=duration_max,
        )

        if self._is_cacheable(request):
            self._cache_repo.store_search_result(
                signature=cache_signature,
                payload={
                    "total": int(page.total),
                    "results": page.results,
                    "strategy": page.strategy,
                },
            )

        logger.info(
            "Search service completed, strategy=%s, cache_hit=%s, total=%s, returned=%s, elapsed_ms=%.2f",
            page.strategy,
            False,
            page.total,
            len(page.results),
            (perf_counter() - started) * 1000.0,
        )
        return SearchExecution(total=page.total, results=page.results, strategy=page.strategy, cache_hit=False)

    def _is_cacheable(self, request: SearchRequest) -> bool:
        return (
            int(request.offset) <= int(self._settings.cache.search_cache_max_offset)
            and int(request.n) <= int(self._settings.cache.search_cache_max_n)
        )

    def _build_cache_signature(self, request: SearchRequest) -> str:
        payload = json.dumps(asdict(request), ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()