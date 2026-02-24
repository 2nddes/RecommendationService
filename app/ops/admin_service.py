from __future__ import annotations

from datetime import datetime
import secrets
from typing import Any, Dict

from app.common.settings import Settings
from app.ops.artifact_store import get_artifact_store
from app.ops.model_ops import apply_current_models, refresh_current_models, train_current_models
from app.ops.tasks import get_task_manager


def new_task_id(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(3)
    return f"{prefix}_{ts}_{rand}"


def start_train_task(settings: Settings, *, mode: str) -> Dict[str, Any]:
    tm = get_task_manager()
    task_id = new_task_id("train")

    def _fn() -> Dict[str, Any]:
        return train_current_models(settings, mode=mode)

    task = tm.start(task_id=task_id, name=f"train:{mode}", fn=_fn)
    return {"task_id": task.id}


def start_refresh_task(settings: Settings) -> Dict[str, Any]:
    tm = get_task_manager()
    task_id = new_task_id("refresh")

    def _fn() -> Dict[str, Any]:
        return refresh_current_models(settings)

    task = tm.start(task_id=task_id, name="refresh", fn=_fn)
    return {"task_id": task.id}


def start_apply_task(settings: Settings) -> Dict[str, Any]:
    tm = get_task_manager()
    task_id = new_task_id("apply")

    def _fn() -> Dict[str, Any]:
        return apply_current_models(settings)

    task = tm.start(task_id=task_id, name="apply", fn=_fn)
    return {"task_id": task.id}


def get_task(task_id: str) -> Dict[str, Any] | None:
    t = get_task_manager().get(task_id)
    if t is None:
        return None
    return t.to_dict()


def get_admin_status(settings: Settings) -> Dict[str, Any]:
    store = get_artifact_store()
    return {
        "config": {
            "recall_channels": list(settings.recall_channels or []),
            "ranking_method": settings.ranking_method,
            "reranking_method": settings.reranking_method,
            "xgb_model_path": settings.xgb_model_path,
        },
        "artifacts": store.get_all(),
    }
