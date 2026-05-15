from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from app.common.settings import Settings
from app.ops.task_ops import TASK_TYPE_TRAIN, UNSET, claim_next_task, create_task, get_task_by_id, list_tasks, update_task
from app.reco.training.common import TrainOutcome, get_mysql_engine, log_event, log_exception, to_train_outcome


logger = logging.getLogger(__name__)


def _merge_metrics(payload: Dict[str, Any], result: Dict[str, Any], error: str | None) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    if isinstance(payload, dict):
        merged.update(payload)
    if isinstance(result, dict):
        merged.update(result)
    if error and "error" not in merged:
        merged["error"] = error
    return merged


def _map_train_job(task: Dict[str, Any]) -> Dict[str, Any]:
    payload = task.get("payload") or {}
    result = task.get("result") or {}
    error = task.get("error")
    mode = str(payload.get("mode") or "full")
    return {
        "id": int(task["id"]),
        "task_ref": task.get("task_ref"),
        "mode": mode,
        "status": task.get("status"),
        "metrics": _merge_metrics(payload, result, error),
        "payload": payload,
        "progress": task.get("progress") or {},
        "result": result,
        "error": error,
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
    }


def _rebuild_global_pipeline_singleton(*, settings: Settings, rebuild_two_tower_index: bool) -> None:
    from app.reco.startup import refresh_recommendation_runtime

    refresh_recommendation_runtime(
        settings,
        rebuild_two_tower_index=rebuild_two_tower_index,
    )
    log_event(
        logger,
        "info",
        "train.runtime.pipeline_rebuilt",
        rebuild_two_tower_index=rebuild_two_tower_index,
    )


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
    _rebuild_global_pipeline_singleton(
        settings=settings,
        rebuild_two_tower_index=component == "recall" and model == "two_tower",
    )
    return result


def refresh_current_models(settings: Settings) -> Dict[str, Any]:
    try:
        _rebuild_global_pipeline_singleton(
            settings=settings,
            rebuild_two_tower_index=True,
        )
    except Exception as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}

    return {"status": "completed", "reason": None}


def create_model_train_job(*, mysql_dsn: str | None, mode: str = "full", metrics: Dict[str, Any] | None = None) -> int:
    payload = {"queued": True}
    if isinstance(metrics, dict):
        payload.update(metrics)
    payload["mode"] = str(mode)
    return create_task(
        mysql_dsn=mysql_dsn,
        task_type=TASK_TYPE_TRAIN,
        status="pending",
        payload=payload,
    )


def claim_next_model_train_job(*, mysql_dsn: str | None) -> Dict[str, Any] | None:
    task = claim_next_task(mysql_dsn=mysql_dsn, task_type=TASK_TYPE_TRAIN)
    if task is None:
        return None
    return _map_train_job(task)


def update_model_train_job(
    *,
    mysql_dsn: str | None,
    job_id: int,
    status: str,
    metrics: Dict[str, Any] | None = None,
    set_finished_at: bool = False,
) -> None:
    error = metrics.get("error") if isinstance(metrics, dict) else None
    update_task(
        mysql_dsn=mysql_dsn,
        task_id=job_id,
        status=str(status),
        result=metrics if isinstance(metrics, dict) else UNSET,
        error=error if metrics is not None or str(status) == "completed" else UNSET,
        set_finished_at=bool(set_finished_at),
        set_started_at_if_null=str(status) == "processing",
    )


def get_model_train_job(*, mysql_dsn: str | None, job_id: int) -> Dict[str, Any] | None:
    try:
        task = get_task_by_id(mysql_dsn=mysql_dsn, task_id=job_id, task_type=TASK_TYPE_TRAIN)
    except RuntimeError as exc:
        log_exception(logger, "train.job.get_failed", exc, job_id=job_id)
        raise
    if task is None:
        return None
    return _map_train_job(task)


def list_model_train_jobs(
    *,
    mysql_dsn: str | None,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> List[Dict[str, Any]]:
    try:
        result = list_tasks(
            mysql_dsn=mysql_dsn,
            limit=int(limit),
            offset=int(offset),
            status=str(status) if status else None,
            task_type=TASK_TYPE_TRAIN,
        )
    except RuntimeError as exc:
        log_exception(logger, "train.job.list_failed", exc, limit=limit, offset=offset, status=status)
        raise
    return [_map_train_job(task) for task in result["items"]]

