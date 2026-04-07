from __future__ import annotations

from typing import Any

from app.common.settings import Settings
from app.ops.model_ops import train_current_models


def train_models(
    settings: Settings,
    *,
    component: str | None,
    model: str | None,
    train_job_id: int | None,
) -> dict[str, Any]:
    return train_current_models(
        settings,
        component=component,
        model=model,
        train_job_id=train_job_id,
    )
