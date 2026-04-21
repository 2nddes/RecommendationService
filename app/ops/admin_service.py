from __future__ import annotations

from datetime import datetime
import logging
import os
import secrets
from typing import Any, Dict

from app.common.runtime_health import snapshot_runtime_health
from app.common.settings import Settings
from app.ops.artifact_store import get_artifact_store
from app.ops.model_ops import create_model_train_job
from app.ops.rag_embedding_ops import (
    create_rag_rebuild_job,
    get_active_rag_rebuild_job,
)
from app.ops.task_ops import get_task as get_task_record
from app.ops.task_ops import list_tasks as list_task_records
from app.ops.task_ops import resolve_task_row_id
from app.reco.rag_service import get_movie_rag_service, initialize_movie_rag_service


logger = logging.getLogger(__name__)

_TASK_KIND_ALIASES = {
    "all": "all",
    "train": "train_job",
    "train_job": "train_job",
    "rag_rebuild": "rag_rebuild_job",
    "rag_rebuild_job": "rag_rebuild_job",
}

_TASK_DISPLAY_TYPES = {
    "train_job": "train",
    "rag_rebuild_job": "rag_rebuild",
}


def new_task_id(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(3)
    return f"{prefix}_{ts}_{rand}"


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


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


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1]
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _path_info(path: Any) -> Dict[str, Any]:
    raw = str(path or "").strip()
    if not raw:
        return {
            "path": None,
            "absolute_path": None,
            "basename": None,
            "exists": False,
        }
    absolute_path = os.path.abspath(raw)
    return {
        "path": raw,
        "absolute_path": absolute_path,
        "basename": os.path.basename(raw),
        "exists": bool(os.path.exists(absolute_path)),
    }


def _same_path(left: Any, right: Any) -> bool:
    left_raw = str(left or "").strip()
    right_raw = str(right or "").strip()
    if not left_raw or not right_raw:
        return False
    return os.path.normcase(os.path.abspath(left_raw)) == os.path.normcase(os.path.abspath(right_raw))


def _task_elapsed_ms(task: Dict[str, Any], result: Dict[str, Any]) -> int | None:
    elapsed_ms = _safe_int(result.get("elapsed_ms"))
    if elapsed_ms is not None:
        return max(0, elapsed_ms)
    started_at = _parse_iso_datetime(task.get("started_at"))
    finished_at = _parse_iso_datetime(task.get("finished_at")) or _parse_iso_datetime(task.get("updated_at"))
    if started_at is None or finished_at is None:
        return None
    return max(0, int((finished_at - started_at).total_seconds() * 1000.0))


def _build_task_subject(task_type: str, payload: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    subject: Dict[str, Any] = {}
    if task_type == "train_job":
        component = payload.get("component")
        model = payload.get("model")
        mode = payload.get("mode")
        request_id = payload.get("request_id")
        if component is not None:
            subject["component"] = str(component)
        if model is not None:
            subject["model"] = str(model)
        if mode is not None:
            subject["mode"] = str(mode)
        if request_id is not None:
            subject["request_id"] = str(request_id)
    elif task_type == "rag_rebuild_job":
        scope = payload.get("scope") or result.get("scope")
        movie_id = payload.get("movie_id")
        request_id = payload.get("request_id")
        if scope is not None:
            subject["scope"] = str(scope)
        if movie_id is not None:
            movie_id_value = _safe_int(movie_id)
            if movie_id_value is not None:
                subject["movie_id"] = movie_id_value
        if request_id is not None:
            subject["request_id"] = str(request_id)
    return subject


def _build_task_counters(task_type: str, progress: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any] | None:
    if task_type != "rag_rebuild_job":
        return None
    return {
        "total_movies": int(progress.get("total_movies") or result.get("total_movies") or 0),
        "processed_movies": int(progress.get("processed_movies") or result.get("processed_movies") or 0),
        "completed_jobs": int(progress.get("completed_jobs") or result.get("completed_jobs") or 0),
        "failed_jobs": int(progress.get("failed_jobs") or result.get("failed_jobs") or 0),
        "pruned_embeddings": int(progress.get("pruned_embeddings") or result.get("pruned_embeddings") or 0),
    }


def _task_progress_percent(
    task_type: str,
    status: str,
    progress: Dict[str, Any],
    result: Dict[str, Any],
) -> float | None:
    if task_type == "rag_rebuild_job":
        total_movies = int(progress.get("total_movies") or result.get("total_movies") or 0)
        processed_movies = int(progress.get("processed_movies") or result.get("processed_movies") or 0)
        if total_movies > 0:
            return round(min(max(processed_movies, 0) / float(total_movies), 1.0) * 100.0, 2)
        if status == "pending":
            return 0.0
        if status in {"completed", "failed"}:
            return 100.0
        return None

    if status == "pending":
        return 0.0
    if status in {"completed", "failed"}:
        return 100.0
    return None


def _build_task_summary(
    task_type: str,
    status: str,
    subject: Dict[str, Any],
    counters: Dict[str, Any] | None,
    result: Dict[str, Any],
    error: Any,
) -> str:
    if task_type == "train_job":
        component = str(subject.get("component") or "unknown_component")
        model = str(subject.get("model") or "unknown_model")
        train_outcome = result.get("train_outcome") if isinstance(result.get("train_outcome"), dict) else {}
        artifact_path = train_outcome.get("artifact_path")
        if status == "completed":
            if artifact_path:
                return f"{component}/{model} completed, artifact={artifact_path}"
            return f"{component}/{model} completed"
        if error:
            return f"{component}/{model} {status}, error={error}"
        return f"{component}/{model} {status}"

    if task_type == "rag_rebuild_job":
        scope = str(subject.get("scope") or "full_rebuild")
        if counters is None:
            return f"{scope} {status}"
        total_movies = int(counters.get("total_movies") or 0)
        processed_movies = int(counters.get("processed_movies") or 0)
        failed_jobs = int(counters.get("failed_jobs") or 0)
        if total_movies > 0:
            summary = f"{scope} {processed_movies}/{total_movies}"
        else:
            summary = f"{scope} {status}"
        if failed_jobs > 0:
            summary += f", failed={failed_jobs}"
        return summary

    if error:
        return f"{task_type} {status}, error={error}"
    return f"{task_type} {status}"


def _serialize_task(task: Dict[str, Any]) -> Dict[str, Any]:
    row_id = int(task["id"])
    task_type = str(task["task_type"])
    status = str(task.get("status") or "unknown")
    payload = task.get("payload") or {}
    progress = task.get("progress") or {}
    result = task.get("result") or {}
    error = task.get("error")
    subject = _build_task_subject(task_type, payload, result)
    counters = _build_task_counters(task_type, progress, result)
    elapsed_ms = _task_elapsed_ms(task, result)
    progress_percent = _task_progress_percent(task_type, status, progress, result)
    summary = _build_task_summary(task_type, status, subject, counters, result, error)
    task_ref = task.get("task_ref")

    data = {
        "task_id": task_ref,
        "row_id": row_id,
        "task_type": task_type,
        "status": status,
        "parent_task_id": task.get("parent_task_ref"),
        "parent_row_id": task.get("parent_task_id"),
        "retry_count": int(task.get("retry_count") or 0),
        "error": error,
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
        "display_type": _TASK_DISPLAY_TYPES.get(task_type, task_type),
        "subject": subject,
        "summary": summary,
        "progress_percent": progress_percent,
        "elapsed_ms": elapsed_ms,
        "is_active": status in {"pending", "processing"},
        "is_terminal": status in {"completed", "failed"},
        "has_error": bool(error),
        "links": {
            "self": f"/api/v1/admin/tasks/{task_ref}" if task_ref else None,
        },
    }

    if task_type == "train_job":
        component = subject.get("component")
        model = subject.get("model")
        train_outcome = result.get("train_outcome") if isinstance(result.get("train_outcome"), dict) else {}
        if component is not None:
            data["component"] = str(component)
        if model is not None:
            data["model"] = str(model)
        if train_outcome.get("artifact_path") is not None:
            data["artifact_path"] = str(train_outcome.get("artifact_path"))
        if train_outcome.get("trained") is not None:
            data["trained"] = bool(train_outcome.get("trained"))

    if task_type == "rag_rebuild_job":
        scope = subject.get("scope")
        movie_id = subject.get("movie_id")
        if scope is not None:
            data["scope"] = str(scope)
        if movie_id is not None:
            data["movie_id"] = int(movie_id)
        if counters is not None:
            data["counters"] = counters

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


def _get_or_initialize_rag_service(settings: Settings):
    try:
        return get_movie_rag_service(settings)
    except RuntimeError as exc:
        if str(exc) != "rag_service_not_initialized":
            raise
    return initialize_movie_rag_service(settings)


def refresh_rag_movie_embedding(
    settings: Settings,
    *,
    movie_id: int,
) -> Dict[str, Any]:
    _validate_rag_rebuild_settings(settings)
    logger.info("开始同步刷新单电影RAG embedding，movie_id=%s", movie_id)

    rag_service = _get_or_initialize_rag_service(settings)
    embedding_id = rag_service.upsert_one(movie_id=int(movie_id), refresh_index=True)
    return {
        "movie_id": int(movie_id),
        "embedding_id": int(embedding_id),
        "status": "completed",
    }


def start_rag_rebuild_task(settings: Settings) -> Dict[str, Any]:
    _validate_rag_rebuild_settings(settings)

    active_job = get_active_rag_rebuild_job(mysql_dsn=settings.core.mysql_dsn)
    if active_job is not None:
        raise ValueError(f"rag_rebuild_job_already_running: {active_job['id']}")

    request_id = new_task_id("rag_rebuild")
    job_id = create_rag_rebuild_job(
        mysql_dsn=settings.core.mysql_dsn,
        request_id=request_id,
    )
    logger.info("RAG full rebuild task created, request_id=%s, job_id=%s", request_id, job_id)

    task = _load_task_view(settings, str(job_id), kind="rag_rebuild_job")
    if task is None:
        raise RuntimeError(f"rag_rebuild_task_not_found_after_create: {job_id}")
    return task


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
            "returned": 0,
            "has_more": False,
            "limit": int(limit),
            "offset": int(offset),
            "source": source_value,
            "status": status_value,
            "task_type": kind_value or "all",
            "kind": kind_value or "all",
            "parent_task_id": parent_task_ref,
            "note": "in-memory task runner has been removed; use source=db",
        }

    returned = len(items)
    return {
        "items": items,
        "total": total,
        "returned": returned,
        "has_more": int(offset) + returned < total,
        "limit": int(limit),
        "offset": int(offset),
        "source": source_value,
        "status": status_value,
        "task_type": kind_value or "all",
        "kind": kind_value or "all",
        "parent_task_id": parent_task_ref,
    }


def _build_runtime_summary(runtime_health: Dict[str, Any]) -> Dict[str, Any]:
    overall = runtime_health.get("overall") or {}
    components = runtime_health.get("components") or {}
    return {
        "generated_at": runtime_health.get("generated_at"),
        "ready": bool(overall.get("ready")),
        "status": str(overall.get("status") or ("ok" if overall.get("ready") else "degraded")),
        "component_count": int(overall.get("component_count") or len(components)),
        "ready_component_count": int(overall.get("ready_component_count") or 0),
        "error_component_count": int(overall.get("error_component_count") or 0),
        "running_component_count": int(overall.get("running_component_count") or 0),
        "pending_component_count": int(overall.get("pending_component_count") or 0),
        "skipped_component_count": int(overall.get("skipped_component_count") or 0),
        "warmup_ready": bool(overall.get("warmup_ready")),
        "pipeline_ready": bool(overall.get("pipeline_ready")),
        "rag_ready": bool(overall.get("rag_ready")),
        "not_ready_components": list(overall.get("not_ready_components") or []),
    }


def _build_models_summary(settings: Settings, artifacts: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "mmoe": {
            "category": "ranking",
            "configured": {
                "model": _path_info(settings.mmoe.model_path),
            },
            "latest": {
                "artifact": _path_info(artifacts.get("ranking.mmoe.latest_artifact_path")),
                "trained_at": artifacts.get("ranking.mmoe.latest_trained_at"),
            },
            "is_configured_latest": _same_path(settings.mmoe.model_path, artifacts.get("ranking.mmoe.latest_artifact_path")),
        },
        "xgb": {
            "category": "ranking",
            "configured": {
                "model": _path_info(settings.xgb.model_path),
            },
            "latest": {
                "artifact": _path_info(artifacts.get("ranking.xgb.latest_artifact_path")),
                "trained_at": artifacts.get("ranking.xgb.latest_trained_at"),
            },
            "is_configured_latest": _same_path(settings.xgb.model_path, artifacts.get("ranking.xgb.latest_artifact_path")),
        },
        "two_tower": {
            "category": "recall",
            "configured": {
                "model": _path_info(settings.two_tower.model_path),
                "index": _path_info(settings.two_tower.index_path),
                "vector_db": _path_info(settings.two_tower.vector_db_path),
            },
            "latest": {
                "model_artifact": _path_info(artifacts.get("recall.two_tower.latest_model_artifact_path")),
                "index_artifact": _path_info(artifacts.get("recall.two_tower.latest_index_artifact_path")),
                "vector_db_artifact": _path_info(artifacts.get("recall.two_tower.latest_vector_db_artifact_path")),
                "trained_at": artifacts.get("recall.two_tower.latest_trained_at"),
            },
            "is_configured_latest": {
                "model": _same_path(settings.two_tower.model_path, artifacts.get("recall.two_tower.latest_model_artifact_path")),
                "index": _same_path(settings.two_tower.index_path, artifacts.get("recall.two_tower.latest_index_artifact_path")),
                "vector_db": _same_path(settings.two_tower.vector_db_path, artifacts.get("recall.two_tower.latest_vector_db_artifact_path")),
            },
        },
    }


def _build_artifact_summary(artifacts: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "artifact_key_count": len(artifacts),
        "artifact_keys": sorted(list(artifacts.keys())),
        "latest_trained_at": {
            "mmoe": artifacts.get("ranking.mmoe.latest_trained_at"),
            "xgb": artifacts.get("ranking.xgb.latest_trained_at"),
            "two_tower": artifacts.get("recall.two_tower.latest_trained_at"),
        },
    }


def _task_capabilities() -> Dict[str, Any]:
    return {
        "sources": ["all", "db", "memory"],
        "status_values": ["pending", "processing", "completed", "failed"],
        "task_types": [
            {
                "value": "train_job",
                "aliases": ["train"],
                "display_type": _TASK_DISPLAY_TYPES["train_job"],
            },
            {
                "value": "rag_rebuild_job",
                "aliases": ["rag_rebuild"],
                "display_type": _TASK_DISPLAY_TYPES["rag_rebuild_job"],
            },
        ],
        "supports_parent_task_filter": True,
        "memory_source_note": "in-memory task runner has been removed; use source=db",
    }


def get_admin_status(settings: Settings) -> Dict[str, Any]:
    store = get_artifact_store()
    artifacts = store.get_all()
    runtime_health = snapshot_runtime_health()
    recallers = ["two_tower"]
    if settings.tag_recall.enabled:
        recallers.append("tag_inverted")

    return {
        "generated_at": _now_iso(),
        "config": {
            "pipeline": {
                "recall": recallers,
                "ranking": "mmoe",
                "reranking": "random_shuffle",
                "tag_recall_enabled": bool(settings.tag_recall.enabled),
            },
            "mmoe_model_path": settings.mmoe.model_path,
            "xgb_model_path": settings.xgb.model_path,
            "two_tower_model_path": settings.two_tower.model_path,
            "two_tower_index_path": settings.two_tower.index_path,
            "two_tower_vector_db_path": settings.two_tower.vector_db_path,
        },
        "models": _build_models_summary(settings, artifacts),
        "artifacts": artifacts,
        "artifact_summary": _build_artifact_summary(artifacts),
        "runtime_health": runtime_health,
        "runtime_summary": _build_runtime_summary(runtime_health),
        "task_capabilities": _task_capabilities(),
    }

