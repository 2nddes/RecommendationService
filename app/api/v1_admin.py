from __future__ import annotations

from datetime import datetime

from flask import Blueprint

from app.common.responses import ok

admin_bp = Blueprint("admin", __name__)


@admin_bp.post("/admin/train")
def admin_train():
    """触发模型重训练（全量）

    文档: POST /api/v1/admin/train
    """

    # 占位：返回一个模拟 task_id
    task_id = f"task_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    return ok(
        {
            "task_id": task_id,
            "estimated_time": "unknown",
        },
        message="Training task started",
    )


@admin_bp.post("/admin/refresh")
def admin_refresh():
    """刷新缓存/增量更新

    文档: POST /api/v1/admin/refresh
    """

    return ok({"status": "accepted"}, message="Refresh task started")
