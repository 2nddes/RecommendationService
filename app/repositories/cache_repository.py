from __future__ import annotations

from app.common.redis_cache import load_trending_items, store_trending_items
from app.common.settings import Settings


class CacheRepository:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def load_trending(self, *, window: str, n: int) -> list[int]:
        return load_trending_items(self._settings, window=window, n=n)

    def store_trending(self, *, window: str, pairs: list[tuple[int, float]]) -> int:
        return int(store_trending_items(self._settings, window=window, pairs=pairs))
