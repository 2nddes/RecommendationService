from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.ops.task_ops import (
    TASK_TABLE,
    TASK_TYPE_RAG_REBUILD,
    claim_next_task,
    create_task,
    get_task_by_id,
    list_tasks,
    update_task,
)
from app.reco.training.common import get_mysql_engine


logger = logging.getLogger(__name__)

RAG_REBUILD_SCOPE_FULL = "full_rebuild"
RAG_REBUILD_SCOPE_SINGLE = "single_movie"
RAG_REBUILD_SCOPES = {RAG_REBUILD_SCOPE_FULL, RAG_REBUILD_SCOPE_SINGLE}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)



def _normalize_movie_ids(movie_ids: Iterable[int] | None) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for raw_movie_id in movie_ids or []:
        movie_id = int(raw_movie_id)
        if movie_id <= 0 or movie_id in seen:
            continue
        seen.add(movie_id)
        normalized.append(movie_id)
    return normalized


def _default_progress(
    *,
    scope: str,
    total_movies: int = 0,
    pruned_embeddings: int = 0,
) -> Dict[str, Any]:
    progress = {
        "scope": scope,
        "total_movies": int(total_movies or 0),
        "processed_movies": 0,
        "completed_jobs": 0,
        "failed_jobs": 0,
        "pruned_embeddings": int(pruned_embeddings or 0),
        "flush_count": 0,
    }
    return progress


def _sanitize_progress(progress: Dict[str, Any] | None) -> Dict[str, Any]:
    current = dict(progress or {})
    current["scope"] = current.get("scope")
    for key in (
        "total_movies",
        "processed_movies",
        "completed_jobs",
        "failed_jobs",
        "pruned_embeddings",
        "flush_count",
    ):
        current[key] = int(current.get(key) or 0)
    return current


def _sanitize_result(result: Dict[str, Any] | None) -> Dict[str, Any]:
    current = dict(result or {})
    if current.get("scope") is not None:
        current["scope"] = current.get("scope")
    for key in (
        "movie_id",
        "total_movies",
        "processed_movies",
        "completed_jobs",
        "failed_jobs",
        "pruned_embeddings",
        "elapsed_ms",
    ):
        if current.get(key) is not None:
            try:
                current[key] = int(current.get(key) or 0)
            except (TypeError, ValueError):
                current[key] = 0

    return current


def _map_rag_rebuild_job_row(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = row.get("payload") or {}
    progress = row.get("progress") or {}
    result = row.get("result") or {}
    scope = payload.get("scope") or progress.get("scope") or result.get("scope") or RAG_REBUILD_SCOPE_FULL
    movie_id = payload.get("movie_id")
    return {
        "id": int(row["id"]),
        "status": str(row["status"]),
        "task_ref": row.get("task_ref"),
        "scope": str(scope),
        "movie_id": int(movie_id) if movie_id is not None else None,
        "total_movies": int(progress.get("total_movies") or result.get("total_movies") or 0),
        "processed_movies": int(progress.get("processed_movies") or result.get("processed_movies") or 0),
        "completed_jobs": int(progress.get("completed_jobs") or result.get("completed_jobs") or 0),
        "failed_jobs": int(progress.get("failed_jobs") or result.get("failed_jobs") or 0),
        "pruned_embeddings": int(progress.get("pruned_embeddings") or result.get("pruned_embeddings") or 0),
        "error": row.get("error"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
        "payload": payload,
        "progress": progress,
        "result": result,
    }


def create_rag_rebuild_job(
    *,
    mysql_dsn: str | None,
    scope: str,
    movie_id: int | None = None,
    request_id: str | None = None,
) -> int:
    payload: Dict[str, Any] = {"scope": scope}
    if request_id:
        payload["request_id"] = str(request_id)

    total_movies = 0
    if scope == RAG_REBUILD_SCOPE_SINGLE:
        payload["movie_id"] = movie_id
        total_movies = 1

    return int(
        create_task(
            mysql_dsn=mysql_dsn,
            task_type=TASK_TYPE_RAG_REBUILD,
            status="pending",
            payload=payload,
            progress=_default_progress(scope=scope, total_movies=total_movies),
            result={},
        )
    )


def get_active_rag_rebuild_job(*, mysql_dsn: str | None) -> Dict[str, Any] | None:
    engine = get_mysql_engine(mysql_dsn, logger=logger, event_prefix="rag.rebuild.mysql_engine")
    sql = text(
        f"""
        SELECT id
        FROM {TASK_TABLE}
        WHERE task_type = :task_type
          AND status IN ('pending', 'processing')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {"task_type": TASK_TYPE_RAG_REBUILD}).mappings().first()
    if row is None:
        return None
    task = get_task_by_id(mysql_dsn=mysql_dsn, task_id=int(row["id"]), task_type=TASK_TYPE_RAG_REBUILD)
    return _map_rag_rebuild_job_row(task) if task is not None else None


def claim_next_rag_rebuild_job(*, mysql_dsn: str | None) -> Dict[str, Any] | None:
    task = claim_next_task(mysql_dsn=mysql_dsn, task_type=TASK_TYPE_RAG_REBUILD)
    if task is None:
        return None
    return _map_rag_rebuild_job_row(task)


def update_rag_rebuild_job_snapshot(
    *,
    mysql_dsn: str | None,
    job_id: int,
    progress: Dict[str, Any],
    result: Dict[str, Any],
    status: str = "processing",
    error: str | None = None,
    set_finished_at: bool = False,
    clear_finished_at: bool = False,
) -> None:
    update_task(
        mysql_dsn=mysql_dsn,
        task_id=int(job_id),
        status=str(status),
        progress=_sanitize_progress(progress),
        result=_sanitize_result(result),
        error=error,
        set_started_at_if_null=True,
        set_finished_at=set_finished_at,
        clear_finished_at=clear_finished_at,
    )


def complete_rag_rebuild_job(
    *,
    mysql_dsn: str | None,
    job_id: int,
    progress: Dict[str, Any],
    result: Dict[str, Any],
) -> None:
    update_rag_rebuild_job_snapshot(
        mysql_dsn=mysql_dsn,
        job_id=int(job_id),
        progress=progress,
        result=result,
        status="completed",
        error=None,
        set_finished_at=True,
        clear_finished_at=False,
    )


def fail_rag_rebuild_job(
    *,
    mysql_dsn: str | None,
    job_id: int,
    error: str,
    progress: Dict[str, Any] | None = None,
    result: Dict[str, Any] | None = None,
) -> None:
    current_progress = _sanitize_progress(progress) if progress is not None else _default_progress(scope=RAG_REBUILD_SCOPE_FULL)
    current_result = _sanitize_result(result)
    update_rag_rebuild_job_snapshot(
        mysql_dsn=mysql_dsn,
        job_id=int(job_id),
        progress=current_progress,
        result=current_result,
        status="failed",
        error=str(error)[:1000],
        set_finished_at=True,
        clear_finished_at=False,
    )


def fail_processing_rag_rebuild_jobs(*, mysql_dsn: str | None, error: str) -> int:
    engine = get_mysql_engine(mysql_dsn, logger=logger, event_prefix="rag.rebuild.mysql_engine")
    sql = text(
        f"""
        UPDATE {TASK_TABLE}
        SET status = 'failed',
            error = :error,
            finished_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE task_type = :task_type
          AND status = 'processing'
        """
    )
    try:
        with engine.begin() as conn:
            rs = conn.execute(
                sql,
                {
                    "error": str(error)[:1000],
                    "task_type": TASK_TYPE_RAG_REBUILD,
                },
            )
    except SQLAlchemyError as exc:
        raise RuntimeError(f"fail_processing_rag_rebuild_jobs_failed: {type(exc).__name__}: {exc}") from exc
    return max(0, int(rs.rowcount or 0))


def get_rag_rebuild_job(*, mysql_dsn: str | None, job_id: int) -> Dict[str, Any] | None:
    task = get_task_by_id(mysql_dsn=mysql_dsn, task_id=job_id, task_type=TASK_TYPE_RAG_REBUILD)
    if task is None:
        return None
    return _map_rag_rebuild_job_row(task)


def list_rag_rebuild_jobs(
    *,
    mysql_dsn: str | None,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> list[Dict[str, Any]]:
    result = list_tasks(
        mysql_dsn=mysql_dsn,
        limit=int(limit),
        offset=int(offset),
        status=str(status) if status else None,
        task_type=TASK_TYPE_RAG_REBUILD,
    )
    return [_map_rag_rebuild_job_row(row) for row in result["items"]]


def normalize_rag_rebuild_movie_ids(movie_ids: Iterable[int] | None) -> list[int]:
    return _normalize_movie_ids(movie_ids)