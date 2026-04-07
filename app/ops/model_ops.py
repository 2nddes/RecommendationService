from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.common.settings import Settings
from app.reco.training.common import TrainOutcome, get_mysql_engine, log_event, log_exception, to_train_outcome


logger = logging.getLogger(__name__)


def _rebuild_global_pipeline_singleton(*, reason: str) -> None:
    """Best-effort refresh of global recommendation runtime after model changes."""
    from app.reco.runtime import rebuild_pipeline

    rebuild_pipeline(reason=reason)
    log_event(logger, "info", "train.runtime.pipeline_rebuilt", reason=reason)


def train_current_models(
    settings: Settings,
    *,
    component: str | None = None,
    model: str | None = None,
    train_job_id: int | None = None,
) -> Dict[str, Any]:
    """Train (or rebuild) artifacts for models that are enabled by config.

    This does NOT change which model is selected; config remains the source of truth.
    It only produces artifacts and records their paths.
    """

    log_event(
        logger,
        "info",
        "train.task.start",
        component=component,
        model=model,
        stage="dispatch",
        status="processing",
        train_job_id=train_job_id,
    )

    if train_job_id is not None:
        update_model_train_job(
            mysql_dsn=settings.core.mysql_dsn,
            job_id=train_job_id,
            status="processing",
        )

    if component == "ranking" and model == "xgb":
        from app.reco.ranking.xgb.training import train_xgb_model

        payload = train_xgb_model(settings)
    elif component == "ranking" and model == "mmoe":
        from app.reco.ranking.mmoe.training import train_mmoe_model

        payload = train_mmoe_model(settings)
    elif component == "recall" and model == "two_tower":
        from app.reco.recall.two_tower.training import train_two_tower_index

        payload = train_two_tower_index(settings)
    else:
        raise ValueError(f"Unknown component/model combination: {component}/{model}")

    outcome = to_train_outcome(payload)
    result = {"train_outcome": outcome.to_dict()}

    if not outcome.trained:
        details = outcome.details if isinstance(outcome.details, dict) else {}
        reason = details.get("reason") or details.get("error") or "train_outcome_not_trained"
        log_event(
            logger,
            "warning",
            "train.task.untrained",
            component=component,
            model=model,
            reason=reason,
            status="failed",
            train_job_id=train_job_id,
        )
        raise RuntimeError(str(reason))

    if train_job_id is not None:
        update_model_train_job(
            mysql_dsn=settings.core.mysql_dsn,
            job_id=train_job_id,
            status="completed",
            metrics=result,
            set_finished_at=True,
        )
    log_event(
        logger,
        "info",
        "train.task.done",
        artifact_path=outcome.artifact_path,
        component=component,
        model=model,
        status="completed",
        train_job_id=train_job_id,
    )
    _rebuild_global_pipeline_singleton(reason=f"train_current_models:{component}.{model}")
    return result


def refresh_current_models(settings: Settings) -> Dict[str, Any]:
    from app.reco.ranking.mmoe import load_latest_local_model as load_latest_mmoe_local_model
    from app.reco.recall.two_tower import load_latest_local_model as load_latest_two_tower_local_model

    if not load_latest_mmoe_local_model(settings):
        return {"status": "failed", "reason": "mmoe_model_not_found"}
    if not load_latest_two_tower_local_model(settings):
        return {"status": "failed", "reason": "two_tower_model_not_found"}

    _rebuild_global_pipeline_singleton(reason="refresh_current_models")
    return {"status": "completed", "reason": None}


def create_model_train_job(*, mysql_dsn: str | None, mode: str = "full", metrics: Dict[str, Any] | None = None) -> int:
    engine = get_mysql_engine(mysql_dsn, logger=logger)

    sql = """
    INSERT INTO model_train_job(mode, status, metrics)
    VALUES (:mode, 'pending', CAST(:metrics AS JSON))
    """
    payload = {"queued": True}
    if isinstance(metrics, dict):
        payload.update(metrics)
    try:
        with engine.begin() as conn:
            rs = conn.execute(text(sql), {"mode": str(mode), "metrics": json.dumps(payload, ensure_ascii=False)})
            new_id = rs.lastrowid
    except SQLAlchemyError as e:
        raise RuntimeError(f"create_model_train_job_failed: {e}") from e

    if new_id is None:
        raise RuntimeError("create_model_train_job_failed: empty_insert_id")
    return int(new_id)


def claim_next_model_train_job(*, mysql_dsn: str | None) -> Dict[str, Any] | None:
    engine = get_mysql_engine(mysql_dsn, logger=logger)

    select_sql = text(
        """
        SELECT id, mode, status, metrics, created_at, finished_at
        FROM model_train_job
        WHERE status = 'pending'
        ORDER BY id ASC
        LIMIT 1
        FOR UPDATE
        """
    )
    update_sql = text(
        """
        UPDATE model_train_job
        SET status = 'processing'
        WHERE id = :job_id
        """
    )

    try:
        with engine.begin() as conn:
            row = conn.execute(select_sql).mappings().first()
            if row is None:
                return None
            job_id = int(row.get("id"))
            conn.execute(update_sql, {"job_id": job_id})
    except SQLAlchemyError as e:
        raise RuntimeError(f"claim_model_train_job_failed: {e}") from e

    return get_model_train_job(mysql_dsn=mysql_dsn, job_id=job_id)


def update_model_train_job(
    *,
    mysql_dsn: str | None,
    job_id: int,
    status: str,
    metrics: Dict[str, Any] | None = None,
    set_finished_at: bool = False,
) -> None:
    engine = get_mysql_engine(mysql_dsn, logger=logger)

    updates = ["status = :status"]
    params: Dict[str, Any] = {"status": str(status), "job_id": int(job_id)}

    if metrics is not None:
        updates.append("metrics = CAST(:metrics AS JSON)")
        params["metrics"] = json.dumps(metrics, ensure_ascii=False)
    if set_finished_at:
        updates.append("finished_at = CURRENT_TIMESTAMP")

    sql = f"UPDATE model_train_job SET {', '.join(updates)} WHERE id = :job_id"

    try:
        with engine.begin() as conn:
            conn.execute(text(sql), params)
    except SQLAlchemyError as e:
        raise RuntimeError(f"update_model_train_job_failed: {e}") from e


def get_model_train_job(*, mysql_dsn: str | None, job_id: int) -> Dict[str, Any] | None:
    engine = get_mysql_engine(mysql_dsn, logger=logger)

    sql = """
    SELECT id, mode, status, metrics, created_at, finished_at
    FROM model_train_job
    WHERE id = :job_id
    LIMIT 1
    """

    try:
        with engine.connect() as conn:
            row = conn.execute(text(sql), {"job_id": int(job_id)}).mappings().first()
    except SQLAlchemyError as e:
        log_exception(logger, "train.job.get_failed", e, job_id=job_id)
        raise RuntimeError(f"get_model_train_job_failed: {type(e).__name__}: {e}") from e

    if row is None:
        return None

    created_at = row.get("created_at")
    finished_at = row.get("finished_at")
    metrics = row.get("metrics")
    if metrics is None:
        metrics = {}
    if isinstance(metrics, str):
        metrics = json.loads(metrics)

    return {
        "id": int(row.get("id")),
        "mode": row.get("mode"),
        "status": row.get("status"),
        "metrics": metrics,
        "created_at": created_at.isoformat() if created_at is not None else None,
        "finished_at": finished_at.isoformat() if finished_at is not None else None,
    }


def list_model_train_jobs(
    *,
    mysql_dsn: str | None,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> List[Dict[str, Any]]:
    engine = get_mysql_engine(mysql_dsn, logger=logger)

    clauses = []
    params: Dict[str, Any] = {
        "limit": int(limit),
        "offset": int(offset),
    }

    if status:
        clauses.append("status = :status")
        params["status"] = str(status)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
    SELECT id, mode, status, metrics, created_at, finished_at
    FROM model_train_job
    {where_sql}
    ORDER BY id DESC
    LIMIT :limit OFFSET :offset
    """

    try:
        with engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
    except SQLAlchemyError as e:
        log_exception(logger, "train.job.list_failed", e, limit=limit, offset=offset, status=status)
        raise RuntimeError(f"list_model_train_jobs_failed: {type(e).__name__}: {e}") from e

    out: List[Dict[str, Any]] = []
    for row in rows:
        created_at = row.get("created_at")
        finished_at = row.get("finished_at")
        metrics = row.get("metrics")
        if metrics is None:
            metrics = {}
        if isinstance(metrics, str):
            metrics = json.loads(metrics)

        out.append(
            {
                "id": int(row.get("id")),
                "mode": row.get("mode"),
                "status": row.get("status"),
                "metrics": metrics,
                "created_at": created_at.isoformat() if created_at is not None else None,
                "finished_at": finished_at.isoformat() if finished_at is not None else None,
            }
        )

    return out

