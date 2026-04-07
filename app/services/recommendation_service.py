from __future__ import annotations

import logging
from time import perf_counter

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

    def recommend_user(self, *, user_id: int, n: int) -> dict:
        logger.info("User recommendation started, user_id=%s, n=%s", user_id, n)
        req_start = perf_counter()
        pipeline = get_pipeline()
        ctx = RequestContext(user_id=user_id, n=n)
        items = pipeline.recommend(ctx)
        logger.info(
            "User recommendation completed, user_id=%s, returned=%s, elapsed_ms=%.2f",
            user_id,
            len(items),
            (perf_counter() - req_start) * 1000.0,
        )
        return {"user_id": user_id, "items": items, "n": n}

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
