from __future__ import annotations

import logging
from time import perf_counter, sleep
from uuid import uuid4

from app.common.settings import Settings
from app.repositories.cache_repository import CacheRepository
from app.repositories.trending_repository import TrendingRepository
from app.reco.online.runtime import get_pipeline
from app.reco.recall.two_tower import ann_search, build_item_vector
from app.reco.types import RequestContext


logger = logging.getLogger(__name__)


class RecommendationService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache_repo = CacheRepository(settings)
        self._trending_repo = TrendingRepository(settings.core.mysql_dsn)

    def recommend_user(self, *, user_id: int, page: int, page_size: int) -> dict:
        if page < 1:
            raise ValueError("'page' must be >= 1")
        if page_size < 1:
            raise ValueError("'page_size' must be >= 1")

        mode = self._resolve_user_reco_delivery_mode()
        req_start = perf_counter()
        if mode == "pop":
            logger.info(
                "User recommendation request, user_id=%s, mode=%s, n=%s",
                user_id,
                mode,
                page_size,
            )
        else:
            logger.info(
                "User recommendation request, user_id=%s, mode=%s, page=%s, page_size=%s",
                user_id,
                mode,
                page,
                page_size,
            )

        if mode == "pop":
            payload, cache_hit = self._recommend_user_pop(user_id=user_id, page=page, page_size=page_size)
        else:
            payload, cache_hit = self._recommend_user_paged(user_id=user_id, page=page, page_size=page_size)

        logger.info(
            "User recommendation request completed, user_id=%s, mode=%s, initial_cache_hit=%s, elapsed_ms=%.2f",
            user_id,
            mode,
            cache_hit,
            (perf_counter() - req_start) * 1000.0,
        )
        return payload

    def _resolve_user_reco_delivery_mode(self) -> str:
        raw_mode = str(self._settings.cache.user_reco_delivery_mode or "paged").strip().lower()
        if raw_mode in {"paged", "pop"}:
            return raw_mode
        logger.warning("Invalid user_reco_delivery_mode=%s, fallback to paged", raw_mode)
        return "paged"

    def _build_user_recommendation_cache(self, *, user_id: int, build_target: int, fallback_log: bool = False) -> int:
        if fallback_log:
            logger.warning(
                "User recommendation cache fallback build started, user_id=%s, build_target=%s",
                user_id,
                build_target,
            )
        else:
            logger.info(
                "User recommendation cache build started, user_id=%s, build_target=%s",
                user_id,
                build_target,
            )

        build_start = perf_counter()
        try:
            pipeline = get_pipeline()
            ctx = RequestContext(user_id=user_id, n=build_target)
            built_items = pipeline.recommend(ctx)
            if fallback_log:
                logger.warning(
                    "User recommendation cache fallback pipeline result, user_id=%s, build_target=%s, built_count=%s, item_preview=%s",
                    user_id,
                    build_target,
                    len(built_items),
                    built_items[:5],
                )
            else:
                logger.info(
                    "User recommendation cache pipeline result, user_id=%s, build_target=%s, built_count=%s, item_preview=%s",
                    user_id,
                    build_target,
                    len(built_items),
                    built_items[:5],
                )
            if not built_items:
                logger.warning(
                    "User recommendation cache build returned empty items, user_id=%s, build_target=%s",
                    user_id,
                    build_target,
                )

            stored = self._cache_repo.store_user_recommendation(user_id=user_id, items=built_items)
            if fallback_log:
                logger.warning(
                    "User recommendation cache fallback stored, user_id=%s, build_target=%s, stored_count=%s",
                    user_id,
                    build_target,
                    stored,
                )
                logger.warning(
                    "User recommendation cache fallback build completed, user_id=%s, build_target=%s, built_count=%s, stored_count=%s, elapsed_ms=%.2f",
                    user_id,
                    build_target,
                    len(built_items),
                    stored,
                    (perf_counter() - build_start) * 1000.0,
                )
            else:
                logger.info(
                    "User recommendation cache stored, user_id=%s, build_target=%s, stored_count=%s",
                    user_id,
                    build_target,
                    stored,
                )
                logger.info(
                    "User recommendation cache build completed, user_id=%s, build_target=%s, built_count=%s, stored_count=%s, elapsed_ms=%.2f",
                    user_id,
                    build_target,
                    len(built_items),
                    stored,
                    (perf_counter() - build_start) * 1000.0,
                )
            return int(stored if stored > 0 else len(built_items))
        except Exception:
            logger.exception(
                "User recommendation cache build failed, user_id=%s, build_target=%s, fallback=%s",
                user_id,
                build_target,
                fallback_log,
            )
            raise

    def _recommend_user_paged(self, *, user_id: int, page: int, page_size: int) -> tuple[dict, bool]:
        build_target = max(int(self._settings.cache.user_reco_cache_size), 1)
        start = (page - 1) * page_size
        end = start + page_size - 1
        cache_items, total = self._cache_repo.load_user_recommendation_page(
            user_id=user_id,
            page=page,
            page_size=page_size,
        )
        logger.info(
            "User recommendation paged initial cache read, user_id=%s, page=%s, page_size=%s, start=%s, end=%s, returned=%s, total=%s",
            user_id,
            page,
            page_size,
            start,
            end,
            len(cache_items),
            total,
        )

        cache_hit = total > 0
        if not cache_hit:
            token = uuid4().hex
            logger.warning(
                "User recommendation paged cache miss, user_id=%s, page=%s, page_size=%s, build_target=%s, action=acquire_build_lock",
                user_id,
                page,
                page_size,
                build_target,
            )
            acquired = self._cache_repo.try_acquire_user_recommendation_lock(user_id=user_id, token=token)
            if acquired:
                logger.info(
                    "User recommendation paged build lock acquired, user_id=%s, page=%s, page_size=%s",
                    user_id,
                    page,
                    page_size,
                )
                try:
                    cache_items, total = self._cache_repo.load_user_recommendation_page(
                        user_id=user_id,
                        page=page,
                        page_size=page_size,
                    )
                    logger.info(
                        "User recommendation paged double-check cache read, user_id=%s, page=%s, page_size=%s, start=%s, end=%s, returned=%s, total=%s",
                        user_id,
                        page,
                        page_size,
                        start,
                        end,
                        len(cache_items),
                        total,
                    )
                    if total <= 0:
                        logger.info(
                            "User recommendation paged cache build required, user_id=%s, page=%s, page_size=%s, build_target=%s, reason=double_check_empty",
                            user_id,
                            page,
                            page_size,
                            build_target,
                        )
                        self._build_user_recommendation_cache(user_id=user_id, build_target=build_target)
                        cache_items, total = self._cache_repo.load_user_recommendation_page(
                            user_id=user_id,
                            page=page,
                            page_size=page_size,
                        )
                        logger.info(
                            "User recommendation paged post-build cache read, user_id=%s, page=%s, page_size=%s, start=%s, end=%s, returned=%s, total=%s",
                            user_id,
                            page,
                            page_size,
                            start,
                            end,
                            len(cache_items),
                            total,
                        )
                finally:
                    self._cache_repo.release_user_recommendation_lock(user_id=user_id, token=token)
            else:
                logger.warning(
                    "User recommendation paged build lock busy, user_id=%s, page=%s, page_size=%s, action=wait_for_peer_build",
                    user_id,
                    page,
                    page_size,
                )
                for attempt in range(1, 5):
                    sleep(0.05)
                    cache_items, total = self._cache_repo.load_user_recommendation_page(
                        user_id=user_id,
                        page=page,
                        page_size=page_size,
                    )
                    logger.warning(
                        "User recommendation paged wait retry, user_id=%s, page=%s, page_size=%s, attempt=%s, returned=%s, total=%s",
                        user_id,
                        page,
                        page_size,
                        attempt,
                        len(cache_items),
                        total,
                    )
                    if total > 0:
                        break

            if total <= 0:
                logger.warning(
                    "User recommendation paged fallback build required, user_id=%s, page=%s, page_size=%s, build_target=%s, reason=cache_still_empty",
                    user_id,
                    page,
                    page_size,
                    build_target,
                )
                self._build_user_recommendation_cache(user_id=user_id, build_target=build_target, fallback_log=True)
                cache_items, total = self._cache_repo.load_user_recommendation_page(
                    user_id=user_id,
                    page=page,
                    page_size=page_size,
                )
                logger.warning(
                    "User recommendation paged post-fallback cache read, user_id=%s, page=%s, page_size=%s, start=%s, end=%s, returned=%s, total=%s",
                    user_id,
                    page,
                    page_size,
                    start,
                    end,
                    len(cache_items),
                    total,
                )

        has_next = (page * page_size) < total
        logger.info(
            "User recommendation paged return, user_id=%s, page=%s, page_size=%s, returned=%s, total=%s, has_next=%s",
            user_id,
            page,
            page_size,
            len(cache_items),
            total,
            has_next,
        )
        return (
            {
                "user_id": user_id,
                "items": cache_items,
                "n": page_size,
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_next": has_next,
            },
            cache_hit,
        )

    def _recommend_user_pop(self, *, user_id: int, page: int, page_size: int) -> tuple[dict, bool]:
        if page != 1:
            logger.info("User recommendation pop mode ignores page parameter, user_id=%s, page=%s", user_id, page)

        build_target = max(int(self._settings.cache.user_reco_cache_size), 1)
        cache_items, remaining = self._cache_repo.pop_user_recommendation_items(user_id=user_id, count=page_size)
        logger.info(
            "User recommendation pop initial cache read, user_id=%s, n=%s, returned=%s, remaining=%s",
            user_id,
            page_size,
            len(cache_items),
            remaining,
        )

        cache_hit = bool(cache_items) or remaining > 0
        if not cache_hit:
            token = uuid4().hex
            logger.warning(
                "User recommendation pop cache miss, user_id=%s, n=%s, build_target=%s, action=acquire_build_lock",
                user_id,
                page_size,
                build_target,
            )
            acquired = self._cache_repo.try_acquire_user_recommendation_lock(user_id=user_id, token=token)
            if acquired:
                logger.info(
                    "User recommendation pop build lock acquired, user_id=%s, n=%s",
                    user_id,
                    page_size,
                )
                try:
                    cache_items, remaining = self._cache_repo.pop_user_recommendation_items(user_id=user_id, count=page_size)
                    logger.info(
                        "User recommendation pop double-check cache read, user_id=%s, n=%s, returned=%s, remaining=%s",
                        user_id,
                        page_size,
                        len(cache_items),
                        remaining,
                    )
                    if not cache_items and remaining <= 0:
                        logger.info(
                            "User recommendation pop cache build required, user_id=%s, n=%s, build_target=%s, reason=double_check_empty",
                            user_id,
                            page_size,
                            build_target,
                        )
                        self._build_user_recommendation_cache(user_id=user_id, build_target=build_target)
                        cache_items, remaining = self._cache_repo.pop_user_recommendation_items(
                            user_id=user_id,
                            count=page_size,
                        )
                        logger.info(
                            "User recommendation pop post-build cache read, user_id=%s, n=%s, returned=%s, remaining=%s",
                            user_id,
                            page_size,
                            len(cache_items),
                            remaining,
                        )
                finally:
                    self._cache_repo.release_user_recommendation_lock(user_id=user_id, token=token)
            else:
                logger.warning(
                    "User recommendation pop build lock busy, user_id=%s, n=%s, action=wait_for_peer_build",
                    user_id,
                    page_size,
                )
                for attempt in range(1, 5):
                    sleep(0.05)
                    cache_items, remaining = self._cache_repo.pop_user_recommendation_items(user_id=user_id, count=page_size)
                    logger.warning(
                        "User recommendation pop wait retry, user_id=%s, n=%s, attempt=%s, returned=%s, remaining=%s",
                        user_id,
                        page_size,
                        attempt,
                        len(cache_items),
                        remaining,
                    )
                    if cache_items or remaining > 0:
                        break

            if not cache_items and remaining <= 0:
                logger.warning(
                    "User recommendation pop fallback build required, user_id=%s, n=%s, build_target=%s, reason=cache_still_empty",
                    user_id,
                    page_size,
                    build_target,
                )
                self._build_user_recommendation_cache(user_id=user_id, build_target=build_target, fallback_log=True)
                cache_items, remaining = self._cache_repo.pop_user_recommendation_items(user_id=user_id, count=page_size)
                logger.warning(
                    "User recommendation pop post-fallback cache read, user_id=%s, n=%s, returned=%s, remaining=%s",
                    user_id,
                    page_size,
                    len(cache_items),
                    remaining,
                )

        total_before_pop = int(remaining + len(cache_items))
        logger.info(
            "User recommendation pop return, user_id=%s, n=%s, returned=%s, remaining=%s, total_before_pop=%s, has_next=%s",
            user_id,
            page_size,
            len(cache_items),
            remaining,
            total_before_pop,
            remaining > 0,
        )
        return (
            {
                "user_id": user_id,
                "items": cache_items,
                "n": page_size,
                "page": page,
                "page_size": page_size,
                "total": total_before_pop,
                "has_next": remaining > 0,
            },
            cache_hit,
        )

    def recommend_item(self, *, movie_id: int, n: int) -> dict:
        logger.info("Item recommendation started, movie_id=%s, n=%s", movie_id, n)
        cfg = self._settings.two_tower
        item_vec = build_item_vector(movie_id, cfg, mysql_dsn=self._settings.core.mysql_dsn)
        if item_vec is None:
            logger.warning("Item recommendation failed: item vector missing, movie_id=%s", movie_id)
            raise ValueError(f"Item vector not found for movie_id: {movie_id}")

        pairs = ann_search(item_vec, k=max(n + 1, n), cfg=cfg)
        logger.info("Item recommendation ANN finished, movie_id=%s, ann_candidates=%s", movie_id, len(pairs))
        items: list[int] = []
        for item_id, _score in pairs:
            iid = int(item_id)
            if iid == movie_id:
                continue
            items.append(iid)
            if len(items) >= n:
                break

        if not items:
            logger.warning("Item recommendation produced empty result, movie_id=%s, n=%s", movie_id, n)
        else:
            logger.info("Item recommendation completed, movie_id=%s, returned=%s", movie_id, len(items))

        return {"source_id": movie_id, "items": items, "n": n}

    def recommend_trending(self, *, window: str, n: int) -> dict:
        logger.info("Trending recommendation started, window=%s, n=%s", window, n)
        items = self._cache_repo.load_trending(window=window, n=n)
        if items:
            logger.info("Trending recommendation cache hit, window=%s, returned=%s", window, len(items))
        else:
            logger.info("Trending recommendation cache miss, window=%s", window)
            pairs = self._trending_repo.fetch_item_scores(window=window, n=max(n, self._settings.cache.trending_topk))
            items = [item_id for item_id, _score in pairs[:n]]
            if pairs:
                stored = self._cache_repo.store_trending(window=window, pairs=pairs)
                logger.info("Trending recommendation cache backfill done, window=%s, stored=%s", window, stored)
            else:
                logger.warning("Trending recommendation fallback returned empty, window=%s", window)
        logger.info("Trending recommendation completed, window=%s, returned=%s", window, len(items))
        return {"window": window, "items": items, "n": n}


def build_recommendation_service(settings: Settings) -> RecommendationService:
    return RecommendationService(settings)
