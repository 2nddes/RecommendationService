from __future__ import annotations

from datetime import datetime
import logging
import secrets
from typing import Any, Dict

from app.common.settings import Settings
from app.ops.artifact_store import get_artifact_store
from app.ops.model_ops import create_model_train_job
from app.ops.rag_embedding_ops import (
    RAG_REBUILD_SCOPE_FULL,
    RAG_REBUILD_SCOPE_SINGLE,
    create_rag_rebuild_job,
    get_active_rag_rebuild_job,
)
from app.ops.task_ops import get_task as get_task_record
from app.ops.task_ops import list_tasks as list_task_records
from app.ops.task_ops import resolve_task_row_id


logger = logging.getLogger(__name__)

_TASK_KIND_ALIASES = {
    "all": "all",
    "train": "train_job",
    "train_job": "train_job",
    "rag_rebuild": "rag_rebuild_job",
    "rag_rebuild_job": "rag_rebuild_job",
}


def new_task_id(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(3)
    return f"{prefix}_{ts}_{rand}"


def _normalize_task_kind(kind: str | None, *, allow_all: bool) -> str | None:
    if kind is None:
        return None
    value = str(kind).strip().lower()
    mapped = _TASK_KIND_ALIASES.get(value)
    if mapped is None:
        raise ValueError(f"invalid task kind: {kind}")
    if mapped == "all":
        return "all" if allow_all else None
    return mapped

def _serialize_task(task: Dict[str, Any]) -> Dict[str, Any]:
    row_id = int(task["id"])
    task_type = str(task["task_type"])
    payload = task.get("payload") or {}
    progress = task.get("progress") or {}
    result = task.get("result") or {}
    data = {
        "task_id": task.get("task_ref"),
        "row_id": row_id,
        "task_type": task_type,
        "status": task.get("status"),
        "parent_task_id": task.get("parent_task_ref"),
        "parent_row_id": task.get("parent_task_id"),
        "retry_count": int(task.get("retry_count") or 0),
        "error": task.get("error"),
        "payload": payload,
        "progress": progress,
        "result": result,
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "source": "db",
        "kind": task_type,
        "name": task_type,
    }

    if task_type == "rag_rebuild_job":
        scope = payload.get("scope") or progress.get("scope")
        if scope is not None:
            data["scope"] = str(scope)
        movie_id = payload.get("movie_id")
        if movie_id is not None:
            data["movie_id"] = int(movie_id)
    return data


def _load_task_view(settings: Settings, task_id: str, *, kind: str | None = None) -> Dict[str, Any] | None:
    normalized_kind = _normalize_task_kind(kind, allow_all=False)
    task = get_task_record(
        mysql_dsn=settings.core.mysql_dsn,
        task_id=task_id,
        task_type=normalized_kind,
    )
    if task is None:
        return None
    return _serialize_task(task)


def start_train_task(
    settings: Settings,
    *,
    component: str | None = None,
    model: str | None = None,
) -> Dict[str, Any]:
    request_id = new_task_id("train")
    logger.info("开始创建训练任务，request_id=%s, component=%s, model=%s", request_id, component, model)

    payload = {
        "component": component,
        "model": model,
        "request_id": request_id,
        "queue": "db_worker",
    }
    train_job_id = create_model_train_job(mysql_dsn=settings.core.mysql_dsn, mode="full", metrics=payload)
    logger.info("训练任务已入队，request_id=%s, train_job_id=%s", request_id, train_job_id)
    task = _load_task_view(settings, str(train_job_id), kind="train_job")
    if task is None:
        raise RuntimeError(f"train_task_not_found_after_create: {train_job_id}")
    return task


def _create_rag_rebuild_task(
    settings: Settings,
    *,
    scope: str,
    movie_id: int | None = None,
) -> Dict[str, Any]:
    _validate_rag_rebuild_settings(settings)

    active_job = get_active_rag_rebuild_job(mysql_dsn=settings.core.mysql_dsn)
    if active_job is not None:
        raise ValueError(f"rag_rebuild_job_already_running: {active_job['id']}")

    request_id = new_task_id("rag_rebuild")
    job_id = create_rag_rebuild_job(
        mysql_dsn=settings.core.mysql_dsn,
        scope=scope,
        movie_id=int(movie_id) if movie_id is not None else None,
        request_id=request_id,
    )

    logger.info(
        "RAG rebuild task created, request_id=%s, job_id=%s, scope=%s, movie_id=%s",
        request_id,
        job_id,
        scope,
        int(movie_id) if movie_id is not None else None,
    )
    task = _load_task_view(settings, str(job_id), kind="rag_rebuild_job")
    if task is None:
        raise RuntimeError(f"rag_rebuild_task_not_found_after_create: {job_id}")
    return task


def start_rag_rebuild_movie_task(
    settings: Settings,
    *,
    movie_id: int,
) -> Dict[str, Any]:
    return _create_rag_rebuild_task(
        settings,
        scope=RAG_REBUILD_SCOPE_SINGLE,
        movie_id=int(movie_id),
    )


def _validate_rag_rebuild_settings(settings: Settings) -> None:
    missing: list[str] = []
    if not settings.core.mysql_dsn:
        missing.append("core.mysql_dsn")
    if not str(settings.rag.embedding_api_base_url or "").strip():
        missing.append("rag.embedding_api_base_url")
    if not str(settings.rag.embedding_model_name or "").strip():
        missing.append("rag.embedding_model_name")
    if missing:
        raise RuntimeError(f"rag_rebuild_config_missing: {', '.join(missing)}")


def start_rag_rebuild_task(settings: Settings) -> Dict[str, Any]:
    return _create_rag_rebuild_task(settings, scope=RAG_REBUILD_SCOPE_FULL)


def get_task(settings: Settings, task_id: str, *, kind: str | None = None) -> Dict[str, Any] | None:
    return _load_task_view(settings, task_id, kind=kind)


def get_tasks(
    settings: Settings,
    *,
    source: str = "all",
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
    kind: str | None = None,
    parent_task_id: str | None = None,
) -> Dict[str, Any]:
    source_value = (source or "all").strip().lower()
    status_value = (status or "").strip().lower() or None
    kind_value = _normalize_task_kind(kind, allow_all=True)

    include_db = source_value in {"all", "db"}
    parent_row_id: int | None = None
    parent_task_ref: str | None = None

    if parent_task_id is not None:
        parent_task_ref = str(parent_task_id).strip()
        if not parent_task_ref:
            raise ValueError("invalid parent_task_id")
        parent_row_id = resolve_task_row_id(
            mysql_dsn=settings.core.mysql_dsn,
            task_id=parent_task_ref,
            task_type=None,
        )
        if parent_row_id is None:
            raise ValueError(f"parent_task_not_found: {parent_task_ref}")

    if include_db:
        db_status = None
        if status_value in {"pending", "processing", "completed", "failed"}:
            db_status = status_value

        query = list_task_records(
            mysql_dsn=settings.core.mysql_dsn,
            limit=int(limit),
            offset=int(offset),
            status=db_status,
            task_type=None if kind_value in {None, "all"} else kind_value,
            parent_task_id=parent_row_id,
            exclude_task_types=["rag_embedding_job"] if kind_value in {None, "all"} else None,
        )
        items = [_serialize_task(task) for task in query["items"]]
        total = int(query["total"])
    else:
        items = []
        total = 0

    if source_value == "memory":
        return {
            "items": [],
            "total": 0,
            "limit": int(limit),
            "offset": int(offset),
            "source": source_value,
            "status": status_value,
            "task_type": kind_value or "all",
            "note": "in-memory task runner has been removed; use source=db",
        }

    return {
        "items": items,
        "total": total,
        "limit": int(limit),
        "offset": int(offset),
        "source": source_value,
        "status": status_value,
        "task_type": kind_value or "all",
        "kind": kind_value or "all",
        "parent_task_id": parent_task_ref,
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

