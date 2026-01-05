from __future__ import annotations

from typing import Callable, Dict, Generic, Iterable, TypeVar

from app.common.settings import Settings

T = TypeVar("T")


ComponentFactory = Callable[[Settings], T]


class Registry(Generic[T]):
    def __init__(self) -> None:
        self._items: Dict[str, ComponentFactory[T]] = {}

    def register(self, name: str, factory: ComponentFactory[T]) -> None:
        self._items[name] = factory

    def get(self, name: str) -> ComponentFactory[T]:
        if name not in self._items:
            raise KeyError(f"unknown component: {name}")
        return self._items[name]

    def build(self, name: str, settings: Settings) -> T:
        return self.get(name)(settings)

    def names(self) -> Iterable[str]:
        return self._items.keys()


recall_registry: Registry = Registry()
ranking_registry: Registry = Registry()
reranking_registry: Registry = Registry()


def register_recaller(name: str, factory: ComponentFactory[T]) -> None:
    recall_registry.register(name, factory)


def register_ranker(name: str, factory: ComponentFactory[T]) -> None:
    ranking_registry.register(name, factory)


def register_reranker(name: str, factory: ComponentFactory[T]) -> None:
    reranking_registry.register(name, factory)
