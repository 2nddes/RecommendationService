from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Registry:
    _items: Dict[str, Callable[[], T]]

    def get(self, name: str) -> Callable[[], T]:
        if name not in self._items:
            raise KeyError(f"unknown component: {name}")
        return self._items[name]

    def names(self) -> Iterable[str]:
        return self._items.keys()


recall_registry: Registry = Registry(_items={})
ranking_registry: Registry = Registry(_items={})
reranking_registry: Registry = Registry(_items={})


def register_recaller(name: str, factory: Callable[[], T]) -> None:
    recall_registry._items[name] = factory


def register_ranker(name: str, factory: Callable[[], T]) -> None:
    ranking_registry._items[name] = factory


def register_reranker(name: str, factory: Callable[[], T]) -> None:
    reranking_registry._items[name] = factory
