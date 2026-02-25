from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class RequestContext:
    user_id: int | None = None
    movie_id: int | None = None
    n: int = 10
    window: str = "weekly"
    query: str | None = None
    filters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Candidate:
    item_id: int
    score: float = 0.0
    source: str = "unknown"


@dataclass(frozen=True)
class RankedItem:
    item_id: int
    score: float
    reason: str | None = None
