from __future__ import annotations

from flask import Blueprint, request

from app.common.responses import ok
from app.common.settings import Settings
from app.common.validation import ParamError, as_str
from app.ops.admin_service import get_admin_status, get_task, start_apply_task, start_refresh_task, start_train_task

admin_bp = Blueprint("admin", __name__)


@admin_bp.post("/admin/train")
def admin_train():
    """触发模型重训练（全量）

    文档: POST /api/v1/admin/train
    """

    body = request.get_json(silent=True) or {}
    mode = body.get("mode") or "full"
    if mode not in {"full", "incremental"}:
        raise ParamError("invalid 'mode', expected 'full' or 'incremental'")

    settings = Settings.from_config()
    data = start_train_task(settings, mode=as_str(mode, name="mode"))
    data["estimated_time"] = "unknown"
    return ok(data, message="Training task started")


@admin_bp.post("/admin/refresh")
def admin_refresh():
    """刷新缓存/增量更新

    文档: POST /api/v1/admin/refresh
    """

    settings = Settings.from_config()
    data = start_refresh_task(settings)
    data["estimated_time"] = "unknown"
    return ok(data, message="Refresh task started")


@admin_bp.post("/admin/apply")
def admin_apply():
    """应用最新训练产物到线上配置指定的路径。

    说明：模型选择依旧由配置(env)决定，这里只负责“把最新产物发布到配置指定位置”。

    文档: POST /api/v1/admin/apply
    """

    settings = Settings.from_config()
    data = start_apply_task(settings)
    data["estimated_time"] = "unknown"
    return ok(data, message="Apply task started")


@admin_bp.get("/admin/tasks/<task_id>")
def admin_task(task_id: str):
    """查询后台任务状态。

    文档: GET /api/v1/admin/tasks/<task_id>
    """

    t = get_task(task_id)
    if t is None:
        raise ParamError("unknown task_id")
    return ok(t)


@admin_bp.get("/admin/status")
def admin_status():
    """查看当前配置与最近训练产物信息。

    文档: GET /api/v1/admin/status
    """

    settings = Settings.from_config()
    return ok(get_admin_status(settings))
