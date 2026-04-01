from __future__ import annotations

import os

from app.common.settings import Settings


def load_latest_local_model(settings: Settings) -> str | None:
    configured = str(settings.mmoe_model_path or "").strip()
    active_path = configured or os.path.join("data", "models", "mmoe_latest.pt")

    if os.path.exists(active_path):
        return active_path

    artifact_dir = os.path.join("data", "artifacts", "mmoe")
    if not os.path.isdir(artifact_dir):
        return None

    candidates = [
        os.path.join(artifact_dir, name)
        for name in os.listdir(artifact_dir)
        if name.endswith(".pt")
    ]
    if not candidates:
        return None

    latest = max(candidates, key=lambda p: os.path.getmtime(p))
    os.makedirs(os.path.dirname(active_path) or ".", exist_ok=True)
    tmp = active_path + ".tmp"
    with open(latest, "rb") as src, open(tmp, "wb") as dst:
        dst.write(src.read())
    os.replace(tmp, active_path)
    return active_path
