from __future__ import annotations

import logging

from flask import Blueprint, request

from app.common.responses import ok, fail
from app.common.validation import as_int, as_str
from app.ops.admin_service import get_admin_status, get_task, get_tasks, start_train_task
from app.ops.model_ops import refresh_current_models
from app.reco.runtime import get_settings

admin_bp = Blueprint("admin", __name__)
logger = logging.getLogger(__name__)


@admin_bp.post("/admin/train")
def admin_train():
    """触发模型重训练

    文档: POST /api/v1/admin/train
    """

    body = request.get_json(silent=True) or {}

    component = body.get("component")
    model = body.get("model")
    if component is None or model is None:
        logger.warning("训练任务缺少参数，component=%s, model=%s", component, model)
        return fail(message="Please specify 'component' and 'model' in the request body")

    component = as_str(component, name="component")
    model = as_str(model, name="model")
    logger.info("收到训练任务请求，component=%s, model=%s", component, model)

    settings = get_settings()
    try:
        data = start_train_task(
            settings,
            component=component,
            model=model,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("创建训练任务失败，component=%s, model=%s", component, model)
        return fail(message=f"Failed to start training task: {type(e).__name__}: {e}")
    data["estimated_time"] = "unknown"
    logger.info("训练任务已提交，task_id=%s", data.get("task_id"))
    return ok(data, message="Training task started")


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
    logger.warning("模型刷新失败，reason=%s", data.get("reason"))
    return fail(message=str(data.get("reason") or "refresh_failed"), data=data)


@admin_bp.get("/admin/tasks/<task_id>")
def admin_task(task_id: str):
    """查询后台任务状态。

    文档: GET /api/v1/admin/tasks/<task_id>
    """

    settings = get_settings()
    t = get_task(settings, task_id)
    if t is None:
        return fail(message=f"Task not found: {task_id}")
    return ok(t)


@admin_bp.get("/admin/tasks")
def admin_tasks():
    """查询后台任务列表。

    文档: GET /api/v1/admin/tasks
    query params:
      - source: all|memory|db (optional, default all)
            - status: pending|processing|completed|failed (optional)
      - limit: int (optional, default 20)
      - offset: int (optional, default 0)
    """

    source = (request.args.get("source", "all") or "all").strip().lower()
    if source not in {"all", "memory", "db"}:
        return fail(message="invalid 'source', expected one of: all, memory, db")

    status = request.args.get("status")
    if status is not None:
        status = status.strip().lower()
        if status not in {"pending", "processing", "completed", "failed"}:
            return fail(message="invalid 'status', expected one of: pending, processing, completed, failed")

    limit = as_int(request.args.get("limit", 20), name="limit")
    offset = as_int(request.args.get("offset", 0), name="offset")
    if limit <= 0:
        return fail(message="invalid 'limit', expected positive integer")
    if offset < 0:
        return fail(message="invalid 'offset', expected non-negative integer")

    settings = get_settings()
    data = get_tasks(settings, source=source, status=status, limit=limit, offset=offset)
    return ok(data)


@admin_bp.get("/admin/status")
def admin_status():
    """查看当前配置与最近训练产物信息。

    文档: GET /api/v1/admin/status
    """

    settings = get_settings()
    return ok(get_admin_status(settings))
