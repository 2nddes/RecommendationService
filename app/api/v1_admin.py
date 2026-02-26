from __future__ import annotations

from flask import Blueprint, request

from app.common.responses import ok, fail
from app.common.settings import Settings
from app.common.validation import as_int, as_str
from app.ops.admin_service import get_admin_status, get_task, get_tasks, start_train_task
from app.ops.model_ops import refresh_current_models

admin_bp = Blueprint("admin", __name__)


@admin_bp.post("/admin/train")
def admin_train():
    """触发模型重训练

    文档: POST /api/v1/admin/train
    """

    body = request.get_json(silent=True) or {}

    component = body.get("component")
    model = body.get("model")
    if component is None or model is None:
        print("未指定 component 或 model")
        return fail(message="Please specify 'component' and 'model' in the request body")

    component = as_str(component, name="component")
    model = as_str(model, name="model")

    settings = Settings.from_config()
    try:
        data = start_train_task(
            settings,
            component=component,
            model=model,
        )
    except Exception as e:  # noqa: BLE001
        return fail(message=f"Failed to start training task: {type(e).__name__}: {e}")
    data["estimated_time"] = "unknown"
    return ok(data, message="Training task started")


@admin_bp.post("/admin/refresh")
def admin_refresh():
    """重新加载权重

    文档: POST /api/v1/admin/refresh
    """

    settings = Settings.from_config()
    data = refresh_current_models(settings)
    if str(data.get("status")) == "completed":
        return ok(data, message="Refresh completed")
    return fail(message=str(data.get("reason") or "refresh_failed"), data=data)


@admin_bp.get("/admin/tasks/<task_id>")
def admin_task(task_id: str):
    """查询后台任务状态。

    文档: GET /api/v1/admin/tasks/<task_id>
    """

    settings = Settings.from_config()
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

    settings = Settings.from_config()
    data = get_tasks(settings, source=source, status=status, limit=limit, offset=offset)
    return ok(data)


@admin_bp.get("/admin/status")
def admin_status():
    """查看当前配置与最近训练产物信息。

    文档: GET /api/v1/admin/status
    """

    settings = Settings.from_config()
    return ok(get_admin_status(settings))
