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
    "two_tower_refresh_worker": ComponentHealth(name="two_tower_refresh_worker", ready=False, status="pending"),
    "cache_precompute_worker": ComponentHealth(name="cache_precompute_worker", ready=False, status="pending"),
    "train_queue_worker": ComponentHealth(name="train_queue_worker", ready=False, status="pending"),
    "rag_rebuild_worker": ComponentHealth(name="rag_rebuild_worker", ready=False, status="pending"),
}


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _serialize_exception(_exc: Exception) -> dict[str, Any]:
    return {
        "type": type(_exc).__name__,
        "message": str(_exc),
    }


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
        c.last_error = _serialize_exception(_exc)
        if details:
            c.details = dict(details)


def snapshot_runtime_health() -> dict[str, Any]:
    with _lock:
        components = {name: asdict(value) for name, value in _components.items()}

    warmup_ready = bool(components.get("warmup", {}).get("ready"))
    pipeline_ready = bool(components.get("pipeline", {}).get("ready"))
    rag_ready = bool(components.get("rag", {}).get("ready"))
    overall_ready = warmup_ready and pipeline_ready and rag_ready
    ready_component_count = sum(1 for component in components.values() if bool(component.get("ready")))
    error_component_count = sum(1 for component in components.values() if str(component.get("status") or "") == "error")
    running_component_count = sum(1 for component in components.values() if str(component.get("status") or "") == "running")
    pending_component_count = sum(1 for component in components.values() if str(component.get("status") or "") == "pending")
    skipped_component_count = sum(1 for component in components.values() if str(component.get("status") or "") == "skipped")

    return {
        "generated_at": _now_iso(),
        "overall": {
            "ready": overall_ready,
            "status": "ok" if overall_ready else "degraded",
            "warmup_ready": warmup_ready,
            "pipeline_ready": pipeline_ready,
            "rag_ready": rag_ready,
            "component_count": len(components),
            "ready_component_count": ready_component_count,
            "error_component_count": error_component_count,
            "running_component_count": running_component_count,
            "pending_component_count": pending_component_count,
            "skipped_component_count": skipped_component_count,
            "not_ready_components": [
                name
                for name, component in sorted(components.items())
                if not bool(component.get("ready"))
            ],
        },
        "components": components,
    }
