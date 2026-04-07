from __future__ import annotations

from flask import Blueprint

from app.common.responses import ok
from app.services.runtime_health_service import get_runtime_health_service

health_bp = Blueprint("health", __name__)


@health_bp.get("/health")
def health():
    snapshot = get_runtime_health_service().get_snapshot()
    status = "ok" if snapshot.get("overall", {}).get("ready") else "degraded"
    return ok({"status": status, "ready": bool(snapshot.get("overall", {}).get("ready"))})


@health_bp.get("/health/runtime")
def health_runtime():
    return ok(get_runtime_health_service().get_snapshot())
