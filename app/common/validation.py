from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ParamError(Exception):
    message: str


def as_int(value: Any, *, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ParamError(f"invalid '{name}', expected integer")


def as_str(value: Any, *, name: str) -> str:
    if value is None:
        raise ParamError(f"missing '{name}'")
    if not isinstance(value, str):
        raise ParamError(f"invalid '{name}', expected string")
    return value


def optional(value: Any, caster: Callable[[Any], Any], default: Any):
    if value is None:
        return default
    return caster(value)
