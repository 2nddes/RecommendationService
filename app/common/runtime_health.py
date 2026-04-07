from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import threading
from typing import Any


@dataclass
class ComponentHealth:
    name: str
    ready: bool = False
    status: str = "unknown"
    last_success_at: str | None = None
    last_error_at: str | None = None
    last_error: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)


_lock = threading.RLock()
_components: dict[str, ComponentHealth] = {
    "warmup": ComponentHealth(name="warmup", ready=False, status="pending"),
    "pipeline": ComponentHealth(name="pipeline", ready=False, status="pending"),
    "rag": ComponentHealth(name="rag", ready=False, status="pending"),
    "cache_precompute": ComponentHealth(name="cache_precompute", ready=False, status="pending"),
}


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def mark_component_state(
    name: str,
    *,
    ready: bool,
    status: str,
    details: dict[str, Any] | None = None,
) -> None:
    with _lock:
        c = _components.setdefault(name, ComponentHealth(name=name))
        c.ready = bool(ready)
        c.status = str(status)
        if details:
            c.details = dict(details)


def mark_component_success(name: str, *, details: dict[str, Any] | None = None) -> None:
    with _lock:
        c = _components.setdefault(name, ComponentHealth(name=name))
        c.ready = True
        c.status = "ok"
        c.last_success_at = _now_iso()
        c.last_error = None
        if details:
            c.details = dict(details)


def mark_component_error(name: str, _exc: Exception, *, details: dict[str, Any] | None = None) -> None:
    with _lock:
        c = _components.setdefault(name, ComponentHealth(name=name))
        c.ready = False
        c.status = "error"
        c.last_error_at = _now_iso()
        c.last_error = None
        if details:
            c.details = dict(details)


def snapshot_runtime_health() -> dict[str, Any]:
    with _lock:
        components = {name: asdict(value) for name, value in _components.items()}

    warmup_ready = bool(components.get("warmup", {}).get("ready"))
    pipeline_ready = bool(components.get("pipeline", {}).get("ready"))
    overall_ready = warmup_ready and pipeline_ready

    return {
        "generated_at": _now_iso(),
        "overall": {
            "ready": overall_ready,
            "warmup_ready": warmup_ready,
            "pipeline_ready": pipeline_ready,
        },
        "components": components,
    }
