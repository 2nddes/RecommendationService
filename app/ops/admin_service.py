from __future__ import annotations

from datetime import datetime
import logging
import secrets
from typing import Any, Dict, List

from app.common.settings import Settings
from app.ops.artifact_store import get_artifact_store
from app.ops.model_ops import (
    create_model_train_job,
    get_model_train_job,
    list_model_train_jobs,
)


logger = logging.getLogger(__name__)


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
    task_id = new_task_id("train")
    logger.info("开始创建训练任务，task_id=%s, component=%s, model=%s", task_id, component, model)

    payload = {
        "component": component,
        "model": model,
        "task_id": task_id,
        "queue": "db_worker",
    }
    train_job_id = create_model_train_job(mysql_dsn=settings.core.mysql_dsn, mode="full", metrics=payload)
    logger.info("训练任务已入队，task_id=%s, train_job_id=%s", task_id, train_job_id)
    return {"task_id": str(train_job_id), "train_job_id": int(train_job_id)}


def _map_model_train_job_status(status: str | None) -> str:
    mapping = {
        "pending": "pending",
        "processing": "processing",
        "completed": "completed",
        "failed": "failed",
    }
    return mapping.get(str(status), "pending")


def get_task(settings: Settings, task_id: str) -> Dict[str, Any] | None:
    if not str(task_id).isdigit():
        return None

    job = get_model_train_job(mysql_dsn=settings.core.mysql_dsn, job_id=int(task_id))
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

    include_db = source_value in {"all", "db"}

    if include_db:
        db_status = None
        if status_value in {"pending", "processing", "completed", "failed"}:
            reverse = {
                "pending": "pending",
                "processing": "processing",
                "completed": "completed",
                "failed": "failed",
            }
            db_status = reverse.get(status_value)

        db_jobs = list_model_train_jobs(mysql_dsn=settings.core.mysql_dsn, limit=int(limit), offset=int(offset), status=db_status)
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

    if source_value == "memory":
        return {
            "items": [],
            "total": 0,
            "limit": int(limit),
            "offset": int(offset),
            "source": source_value,
            "status": status_value,
            "note": "in-memory task runner has been removed; use source=db",
        }

    items.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)

    total = len(items)
    start = int(offset)
    end = start + int(limit)
    paged = items[start:end]

    return {
        "items": paged,
        "total": total,
        "limit": int(limit),
        "offset": start,
        "source": source_value,
        "status": status_value,
    }


def get_admin_status(settings: Settings) -> Dict[str, Any]:
    store = get_artifact_store()
    recallers = ["two_tower"]
    if settings.tag_recall.enabled:
        recallers.append("tag_inverted")

    return {
        "config": {
            "pipeline": {
                "recall": recallers,
                "ranking": "mmoe",
                "reranking": "random_shuffle",
            },
            "mmoe_model_path": settings.mmoe.model_path,
            "two_tower_model_path": settings.two_tower.model_path,
            "two_tower_index_path": settings.two_tower.index_path,
            "two_tower_vector_db_path": settings.two_tower.vector_db_path,
            "two_tower_startup_build": settings.two_tower.startup_build,
            "two_tower_daily_update_interval_hours": settings.two_tower.daily_update_interval_hours,
        },
        "artifacts": store.get_all(),
    }

