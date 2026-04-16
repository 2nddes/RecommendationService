from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.reco.training.common import get_mysql_engine


logger = logging.getLogger(__name__)

TASK_TABLE = "ops_task"
TASK_TYPE_TRAIN = "train_job"
TASK_TYPE_RAG_REBUILD = "rag_rebuild_job"

_UNSET = object()
UNSET = _UNSET
_TASK_REF_RE = re.compile(r"^(?P<task_type>.+)_(?P<suffix>\d+)$")


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_loads(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        loaded = json.loads(value)
        return dict(loaded) if isinstance(loaded, dict) else {"value": loaded}
    return {"value": value}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _select_columns() -> str:
    return """
        t.id,
        COALESCE(t.task_ref_override, CONCAT(t.task_type, '_', t.id)) AS task_ref,
        t.task_type,
        t.status,
        t.parent_task_id,
        COALESCE(parent.task_ref_override, CONCAT(parent.task_type, '_', parent.id)) AS parent_task_ref,
        t.retry_count,
        t.error,
        t.payload,
        t.progress,
        t.result,
        t.created_at,
        t.updated_at,
        t.started_at,
        t.finished_at
    """


def _map_task_row(row: Dict[str, Any]) -> Dict[str, Any]:
    parent_task_id = row.get("parent_task_id")
    return {
        "id": int(row["id"]),
        "task_ref": str(row["task_ref"]),
        "task_type": str(row["task_type"]),
        "status": str(row["status"]),
        "parent_task_id": int(parent_task_id) if parent_task_id is not None else None,
        "parent_task_ref": row.get("parent_task_ref"),
        "retry_count": int(row.get("retry_count") or 0),
        "error": row.get("error"),
        "payload": _json_loads(row.get("payload")),
        "progress": _json_loads(row.get("progress")),
        "result": _json_loads(row.get("result")),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
    }


def parse_task_ref(task_ref: str | None) -> tuple[str | None, int | None]:
    raw = str(task_ref or "").strip()
    if not raw:
        return None, None
    match = _TASK_REF_RE.match(raw)
    if match is None:
        return None, None
    return match.group("task_type"), int(match.group("suffix"))


def create_task(
    *,
    mysql_dsn: str | None,
    task_type: str,
    status: str = "pending",
    parent_task_id: int | None = None,
    retry_count: int = 0,
    error: str | None = None,
    payload: Dict[str, Any] | None = None,
    progress: Dict[str, Any] | None = None,
    result: Dict[str, Any] | None = None,
    task_ref_override: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> int:
    engine = get_mysql_engine(mysql_dsn, logger=logger, event_prefix="task.mysql_engine")
    sql = text(
        f"""
        INSERT INTO {TASK_TABLE}(
            task_ref_override,
            task_type,
            status,
            parent_task_id,
            retry_count,
            error,
            payload,
            progress,
            result,
            started_at,
            finished_at
        )
        VALUES (
            :task_ref_override,
            :task_type,
            :status,
            :parent_task_id,
            :retry_count,
            :error,
            CAST(:payload AS JSON),
            CAST(:progress AS JSON),
            CAST(:result AS JSON),
            :started_at,
            :finished_at
        )
        """
    )
    params = {
        "task_ref_override": str(task_ref_override).strip() if task_ref_override else None,
        "task_type": str(task_type),
        "status": str(status),
        "parent_task_id": int(parent_task_id) if parent_task_id is not None else None,
        "retry_count": int(retry_count),
        "error": str(error)[:1000] if error else None,
        "payload": _json_dumps(payload),
        "progress": _json_dumps(progress),
        "result": _json_dumps(result),
        "started_at": started_at,
        "finished_at": finished_at,
    }
    try:
        with engine.begin() as conn:
            rs = conn.execute(sql, params)
            new_id = rs.lastrowid
    except SQLAlchemyError as exc:
        raise RuntimeError(f"create_task_failed: {type(exc).__name__}: {exc}") from exc
    if new_id is None:
        raise RuntimeError("create_task_failed: empty_insert_id")
    return int(new_id)


def get_task_by_id(*, mysql_dsn: str | None, task_id: int, task_type: str | None = None) -> Dict[str, Any] | None:
    engine = get_mysql_engine(mysql_dsn, logger=logger, event_prefix="task.mysql_engine")
    clauses = ["t.id = :task_id"]
    params: Dict[str, Any] = {"task_id": int(task_id)}
    if task_type is not None:
        clauses.append("t.task_type = :task_type")
        params["task_type"] = str(task_type)
    sql = text(
        f"""
        SELECT {_select_columns()}
        FROM {TASK_TABLE} t
        LEFT JOIN {TASK_TABLE} parent ON parent.id = t.parent_task_id
        WHERE {' AND '.join(clauses)}
        LIMIT 1
        """
    )
    try:
        with engine.connect() as conn:
            row = conn.execute(sql, params).mappings().first()
    except SQLAlchemyError as exc:
        raise RuntimeError(f"get_task_by_id_failed: {type(exc).__name__}: {exc}") from exc
    if row is None:
        return None
    return _map_task_row(dict(row))


def get_task_by_ref(*, mysql_dsn: str | None, task_ref: str) -> Dict[str, Any] | None:
    raw = str(task_ref or "").strip()
    if not raw:
        return None
    engine = get_mysql_engine(mysql_dsn, logger=logger, event_prefix="task.mysql_engine")
    exact_sql = text(
        f"""
        SELECT {_select_columns()}
        FROM {TASK_TABLE} t
        LEFT JOIN {TASK_TABLE} parent ON parent.id = t.parent_task_id
        WHERE t.task_ref_override = :task_ref
        LIMIT 1
        """
    )
    try:
        with engine.connect() as conn:
            row = conn.execute(exact_sql, {"task_ref": raw}).mappings().first()
            if row is not None:
                return _map_task_row(dict(row))
    except SQLAlchemyError as exc:
        raise RuntimeError(f"get_task_by_ref_failed: {type(exc).__name__}: {exc}") from exc

    parsed_task_type, parsed_id = parse_task_ref(raw)
    if parsed_task_type is None or parsed_id is None:
        return None
    return get_task_by_id(mysql_dsn=mysql_dsn, task_id=parsed_id, task_type=parsed_task_type)


def get_task(*, mysql_dsn: str | None, task_id: str, task_type: str | None = None) -> Dict[str, Any] | None:
    raw = str(task_id or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        numeric_id = int(raw)
        task = get_task_by_id(mysql_dsn=mysql_dsn, task_id=numeric_id, task_type=task_type)
        if task is not None:
            return task
        return None
    task = get_task_by_ref(mysql_dsn=mysql_dsn, task_ref=raw)
    if task is None:
        return None
    if task_type is not None and str(task.get("task_type")) != str(task_type):
        return None
    return task


def resolve_task_row_id(*, mysql_dsn: str | None, task_id: str, task_type: str | None = None) -> int | None:
    task = get_task(mysql_dsn=mysql_dsn, task_id=task_id, task_type=task_type)
    if task is None:
        return None
    return int(task["id"])


def list_tasks(
    *,
    mysql_dsn: str | None,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    task_type: str | None = None,
    parent_task_id: int | None = None,
    exclude_task_types: Iterable[str] | None = None,
) -> Dict[str, Any]:
    engine = get_mysql_engine(mysql_dsn, logger=logger, event_prefix="task.mysql_engine")
    clauses: list[str] = []
    params: Dict[str, Any] = {
        "limit": int(limit),
        "offset": int(offset),
    }
    if status:
        clauses.append("t.status = :status")
        params["status"] = str(status)
    if task_type:
        clauses.append("t.task_type = :task_type")
        params["task_type"] = str(task_type)
    if parent_task_id is not None:
        clauses.append("t.parent_task_id = :parent_task_id")
        params["parent_task_id"] = int(parent_task_id)
    excluded = [str(value).strip() for value in (exclude_task_types or []) if str(value).strip()]
    if excluded:
        placeholders: list[str] = []
        for index, excluded_task_type in enumerate(excluded):
            param_name = f"exclude_task_type_{index}"
            placeholders.append(f":{param_name}")
            params[param_name] = excluded_task_type
        clauses.append(f"t.task_type NOT IN ({', '.join(placeholders)})")

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    select_sql = text(
        f"""
        SELECT {_select_columns()}
        FROM {TASK_TABLE} t
        LEFT JOIN {TASK_TABLE} parent ON parent.id = t.parent_task_id
        {where_sql}
        ORDER BY t.created_at DESC, t.id DESC
        LIMIT :limit OFFSET :offset
        """
    )
    count_sql = text(
        f"""
        SELECT COUNT(1) AS total
        FROM {TASK_TABLE} t
        {where_sql}
        """
    )
    try:
        with engine.connect() as conn:
            total = int(conn.execute(count_sql, params).scalar_one())
            rows = conn.execute(select_sql, params).mappings().all()
    except SQLAlchemyError as exc:
        raise RuntimeError(f"list_tasks_failed: {type(exc).__name__}: {exc}") from exc
    return {
        "items": [_map_task_row(dict(row)) for row in rows],
        "total": total,
    }


def claim_next_task(*, mysql_dsn: str | None, task_type: str, max_retry: int | None = None) -> Dict[str, Any] | None:
    engine = get_mysql_engine(mysql_dsn, logger=logger, event_prefix="task.mysql_engine")
    clauses = [
        "task_type = :task_type",
        "status = 'pending'",
    ]
    params: Dict[str, Any] = {"task_type": str(task_type)}
    if max_retry is not None:
        clauses.append("retry_count < :max_retry")
        params["max_retry"] = int(max_retry)
    select_sql = text(
        f"""
        SELECT id
        FROM {TASK_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        FOR UPDATE
        """
    )
    update_sql = text(
        f"""
        UPDATE {TASK_TABLE}
        SET status = 'processing',
            started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = :task_id
        """
    )
    try:
        with engine.begin() as conn:
            row = conn.execute(select_sql, params).mappings().first()
            if row is None:
                return None
            task_id = int(row["id"])
            conn.execute(update_sql, {"task_id": task_id})
    except SQLAlchemyError as exc:
        raise RuntimeError(f"claim_next_task_failed: {type(exc).__name__}: {exc}") from exc
    return get_task_by_id(mysql_dsn=mysql_dsn, task_id=task_id, task_type=task_type)


def update_task(
    *,
    mysql_dsn: str | None,
    task_id: int,
    status: str | object = _UNSET,
    parent_task_id: int | None | object = _UNSET,
    retry_count: int | object = _UNSET,
    error: str | None | object = _UNSET,
    payload: Dict[str, Any] | None | object = _UNSET,
    progress: Dict[str, Any] | None | object = _UNSET,
    result: Dict[str, Any] | None | object = _UNSET,
    set_started_at_if_null: bool = False,
    set_finished_at: bool = False,
    clear_finished_at: bool = False,
) -> None:
    updates: list[str] = []
    params: Dict[str, Any] = {"task_id": int(task_id)}

    if status is not _UNSET:
        updates.append("status = :status")
        params["status"] = str(status)
    if parent_task_id is not _UNSET:
        updates.append("parent_task_id = :parent_task_id")
        params["parent_task_id"] = int(parent_task_id) if parent_task_id is not None else None
    if retry_count is not _UNSET:
        updates.append("retry_count = :retry_count")
        params["retry_count"] = int(retry_count)
    if error is not _UNSET:
        updates.append("error = :error")
        params["error"] = str(error)[:1000] if error else None
    if payload is not _UNSET:
        updates.append("payload = CAST(:payload AS JSON)")
        params["payload"] = _json_dumps(payload)
    if progress is not _UNSET:
        updates.append("progress = CAST(:progress AS JSON)")
        params["progress"] = _json_dumps(progress)
    if result is not _UNSET:
        updates.append("result = CAST(:result AS JSON)")
        params["result"] = _json_dumps(result)
    if set_started_at_if_null:
        updates.append("started_at = COALESCE(started_at, CURRENT_TIMESTAMP)")
    if clear_finished_at:
        updates.append("finished_at = NULL")
    if set_finished_at:
        updates.append("finished_at = CURRENT_TIMESTAMP")

    updates.append("updated_at = CURRENT_TIMESTAMP")

    engine = get_mysql_engine(mysql_dsn, logger=logger, event_prefix="task.mysql_engine")
    sql = text(f"UPDATE {TASK_TABLE} SET {', '.join(updates)} WHERE id = :task_id")
    try:
        with engine.begin() as conn:
            conn.execute(sql, params)
    except SQLAlchemyError as exc:
        raise RuntimeError(f"update_task_failed: {type(exc).__name__}: {exc}") from exc