from __future__ import annotations

from datetime import datetime
import secrets
from typing import Any, Dict, List

from app.common.settings import Settings
from app.ops.artifact_store import get_artifact_store
from app.ops.model_ops import (
    create_model_train_job,
    get_model_train_job,
    list_model_train_jobs,
    refresh_current_models,
    train_current_models,
)
from app.ops.tasks import get_task_manager


def new_task_id(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(3)
    return f"{prefix}_{ts}_{rand}"


def start_train_task(
    settings: Settings,
    *,
    component: str | None = None,
    model: str | None = None,
) -> Dict[str, Any]:
    tm = get_task_manager()
    task_id = new_task_id("train")
    train_job_id = create_model_train_job(mysql_dsn=settings.mysql_dsn, mode="full")

    def _fn() -> Dict[str, Any]:
        return train_current_models(settings, component=component, model=model, train_job_id=train_job_id)

    task = tm.start(task_id=task_id, name=f"train:{component}.{model}", fn=_fn)
    return {"task_id": task.id, "train_job_id": int(train_job_id)}


def start_refresh_task(settings: Settings) -> Dict[str, Any]:
    tm = get_task_manager()
    task_id = new_task_id("refresh")

    def _fn() -> Dict[str, Any]:
        return refresh_current_models(settings)

    task = tm.start(task_id=task_id, name="refresh", fn=_fn)
    return {"task_id": task.id}


def _map_model_train_job_status(status: str | None) -> str:
    mapping = {
        "pending": "pending",
        "processing": "running",
        "completed": "succeeded",
        "failed": "failed",
    }
    return mapping.get(str(status), "pending")


def get_task(settings: Settings, task_id: str) -> Dict[str, Any] | None:
    t = get_task_manager().get(task_id)
    if t is not None:
        return t.to_dict()

    if not str(task_id).isdigit():
        return None

    job = get_model_train_job(mysql_dsn=settings.mysql_dsn, job_id=int(task_id))
    if job is None:
        return None

    metrics = job.get("metrics") or {}
    error = None
    if isinstance(metrics, dict):
        error = metrics.get("error")

    return {
        "id": str(job["id"]),
        "name": "train_job",
        "status": _map_model_train_job_status(job.get("status")),
        "created_at": job.get("created_at"),
        "started_at": None,
        "finished_at": job.get("finished_at"),
        "error": error,
        "result": {
            "train_job_id": int(job["id"]),
            "mode": job.get("mode"),
            "status": job.get("status"),
            "metrics": metrics,
        },
    }


def get_tasks(
    settings: Settings,
    *,
    source: str = "all",
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> Dict[str, Any]:
    source_value = (source or "all").strip().lower()
    status_value = (status or "").strip().lower() or None

    items: List[Dict[str, Any]] = []

    include_memory = source_value in {"all", "memory"}
    include_db = source_value in {"all", "db"}

    if include_memory:
        memory_tasks = [t.to_dict() for t in get_task_manager().list()]
        for item in memory_tasks:
            item["source"] = "memory"
        if status_value:
            memory_tasks = [t for t in memory_tasks if str(t.get("status", "")).lower() == status_value]
        items.extend(memory_tasks)

    if include_db:
        db_status = None
        if status_value in {"pending", "running", "succeeded", "failed"}:
            reverse = {
                "pending": "pending",
                "running": "processing",
                "succeeded": "completed",
                "failed": "failed",
            }
            db_status = reverse.get(status_value)

        db_jobs = list_model_train_jobs(mysql_dsn=settings.mysql_dsn, limit=max(int(limit), 1), offset=max(int(offset), 0), status=db_status)
        for job in db_jobs:
            metrics = job.get("metrics") or {}
            error = metrics.get("error") if isinstance(metrics, dict) else None
            items.append(
                {
                    "id": str(job["id"]),
                    "name": "train_job",
                    "status": _map_model_train_job_status(job.get("status")),
                    "created_at": job.get("created_at"),
                    "started_at": None,
                    "finished_at": job.get("finished_at"),
                    "error": error,
                    "source": "db",
                    "result": {
                        "train_job_id": int(job["id"]),
                        "mode": job.get("mode"),
                        "status": job.get("status"),
                        "metrics": metrics,
                    },
                }
            )

    items.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)

    total = len(items)
    start = max(int(offset), 0)
    end = start + max(int(limit), 1)
    paged = items[start:end]

    return {
        "items": paged,
        "total": total,
        "limit": max(int(limit), 1),
        "offset": start,
        "source": source_value,
        "status": status_value,
    }


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
