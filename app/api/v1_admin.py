from __future__ import annotations

import logging

from flask import Blueprint, abort, request

from app.common.responses import ok
from app.common.validation import ParamError, as_int, as_str
from app.ops.admin_service import (
    get_admin_status,
    get_task,
    get_tasks,
    start_rag_rebuild_movie_task,
    start_rag_rebuild_task,
    start_train_task,
)
from app.ops.model_ops import refresh_current_models
from app.reco.online.runtime import get_settings

admin_bp = Blueprint("admin", __name__)
logger = logging.getLogger(__name__)


def _parse_task_kind(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    mapping = {
        "all": "all",
        "train": "train_job",
        "train_job": "train_job",
        "rag_rebuild": "rag_rebuild_job",
        "rag_rebuild_job": "rag_rebuild_job",
    }
    normalized = mapping.get(value)
    if normalized is None:
        raise ParamError("invalid 'kind'")
    return normalized


def _task_type_query_value() -> str | None:
    task_type = _parse_task_kind(request.args.get("task_type"))
    kind = _parse_task_kind(request.args.get("kind"))
    if task_type is not None and kind is not None and task_type != kind:
        raise ParamError("task_type/kind conflict")
    return task_type or kind


def _parent_task_query_value() -> str | None:
    parent_task_id = request.args.get("parent_task_id")
    rebuild_job_id = request.args.get("rebuild_job_id")
    values = [str(value).strip() for value in (parent_task_id, rebuild_job_id) if value is not None and str(value).strip()]
    if len(values) > 1 and values[0] != values[1]:
        raise ParamError("parent_task_id/rebuild_job_id conflict")
    return values[0] if values else None


@admin_bp.post("/admin/train")
def admin_train():
    """触发模型重训练

    文档: POST /api/v1/admin/train
    """

    body = request.get_json(silent=True) or {}

    component = body.get("component")
    model = body.get("model")
    if component is None or model is None:
        raise ParamError("missing required request body fields: component/model")

    component = as_str(component, name="component")
    model = as_str(model, name="model")
    logger.info("收到训练任务请求，component=%s, model=%s", component, model)

    settings = get_settings()
    data = start_train_task(
        settings,
        component=component,
        model=model,
    )
    data["estimated_time"] = "unknown"
    logger.info("训练任务已提交，task_id=%s", data.get("task_id"))
    return ok(data, message="Training task started")


@admin_bp.post("/admin/rag/enqueue")
def admin_rag_enqueue():
    body = request.get_json(silent=True) or {}
    movie_id = as_int(body.get("movie_id"), name="movie_id")
    if movie_id <= 0:
        raise ParamError("invalid 'movie_id', expected positive integer")

    settings = get_settings()
    data = start_rag_rebuild_movie_task(settings, movie_id=int(movie_id))
    return ok(data, message="RAG single-movie rebuild task queued")


@admin_bp.post("/admin/rag/rebuild")
def admin_rag_rebuild():
    settings = get_settings()
    data = start_rag_rebuild_task(settings)
    return ok(data, message="RAG full rebuild task started")


@admin_bp.post("/admin/refresh")
def admin_refresh():
    """重新加载权重

    文档: POST /api/v1/admin/refresh
    """

    settings = get_settings()
    logger.info("收到模型刷新请求")
    data = refresh_current_models(settings)
    if str(data.get("status")) == "completed":
        logger.info("模型刷新完成")
        return ok(data, message="Refresh completed")
    raise RuntimeError(str(data.get("reason") or "refresh_failed"))


@admin_bp.get("/admin/tasks/<task_id>")
def admin_task(task_id: str):
    """查询后台任务状态。

    文档: GET /api/v1/admin/tasks/<task_id>
        query params:
            - task_type|kind: optional task type filter
    """

    settings = get_settings()
    task_type = _task_type_query_value()
    t = get_task(settings, task_id, kind=task_type)
    if t is None:
        abort(404)
    return ok(t)


@admin_bp.get("/admin/tasks")
def admin_tasks():
    """查询后台任务列表。

    文档: GET /api/v1/admin/tasks
    query params:
      - source: all|memory|db (optional, default all)
    - status: pending|processing|completed|failed (optional)
        - task_type|kind: all|train|rag_rebuild (optional, default all)
    - parent_task_id|rebuild_job_id: optional parent task filter
      - limit: int (optional, default 20)
      - offset: int (optional, default 0)
    """

    source = (request.args.get("source", "all") or "all").strip().lower()
    if source not in {"all", "memory", "db"}:
        raise ParamError("invalid source")

    status = request.args.get("status")
    if status is not None:
        status = status.strip().lower()
        if status not in {"pending", "processing", "completed", "failed"}:
            raise ParamError("invalid status")

    task_type = _task_type_query_value()
    parent_task_id = _parent_task_query_value()

    limit = as_int(request.args.get("limit", 20), name="limit")
    offset = as_int(request.args.get("offset", 0), name="offset")

    settings = get_settings()
    data = get_tasks(
        settings,
        source=source,
        status=status,
        limit=limit,
        offset=offset,
        kind=task_type,
        parent_task_id=parent_task_id,
    )
    return ok(data)


@admin_bp.get("/admin/status")
def admin_status():
    """查看当前配置与最近训练产物信息。

    文档: GET /api/v1/admin/status
    """

    settings = get_settings()
    return ok(get_admin_status(settings))
