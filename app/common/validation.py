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


def as_bool(value: Any, *, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        raise ParamError(f"missing '{name}'")
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "t", "yes", "y", "on"):
            return True
        if v in ("0", "false", "f", "no", "n", "off"):
            return False
        raise ParamError(f"invalid '{name}', expected boolean")
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        raise ParamError(f"invalid '{name}', expected boolean")
