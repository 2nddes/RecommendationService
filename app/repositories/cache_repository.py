from __future__ import annotations

from app.common.redis_cache import (
    load_trending_items,
    pop_user_recommendation_items,
    load_user_recommendation_page,
    load_user_recommendation_total,
    release_user_recommendation_lock,
    store_trending_items,
    store_user_recommendation_items,
    try_acquire_user_recommendation_lock,
)
from app.common.settings import Settings


class CacheRepository:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def load_trending(self, *, window: str, n: int) -> list[int]:
        return load_trending_items(self._settings, window=window, n=n)

    def store_trending(self, *, window: str, pairs: list[tuple[int, float]]) -> int:
        return int(store_trending_items(self._settings, window=window, pairs=pairs))

    def load_user_recommendation_page(self, *, user_id: int, page: int, page_size: int) -> tuple[list[int], int]:
        normalized_page = max(int(page), 1)
        normalized_page_size = max(int(page_size), 1)
        start = (normalized_page - 1) * normalized_page_size
        end = start + normalized_page_size - 1
        items = load_user_recommendation_page(
            self._settings,
            user_id=user_id,
            start=start,
            end=end,
        )
        total = load_user_recommendation_total(self._settings, user_id=user_id)
        return items, int(total)

    def pop_user_recommendation_items(self, *, user_id: int, count: int) -> tuple[list[int], int]:
        return pop_user_recommendation_items(self._settings, user_id=user_id, count=count)

    def store_user_recommendation(self, *, user_id: int, items: list[int]) -> int:
        return int(store_user_recommendation_items(self._settings, user_id=user_id, items=items))

    def try_acquire_user_recommendation_lock(self, *, user_id: int, token: str) -> bool:
        return bool(
            try_acquire_user_recommendation_lock(
                self._settings,
                user_id=user_id,
                token=token,
            )
        )

    def release_user_recommendation_lock(self, *, user_id: int, token: str) -> None:
        release_user_recommendation_lock(
            self._settings,
            user_id=user_id,
            token=token,
        )
