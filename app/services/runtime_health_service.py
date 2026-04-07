from __future__ import annotations

from app.common.runtime_health import snapshot_runtime_health


class RuntimeHealthService:
    def get_snapshot(self) -> dict:
        return snapshot_runtime_health()


_runtime_health_service = RuntimeHealthService()


def get_runtime_health_service() -> RuntimeHealthService:
    return _runtime_health_service
