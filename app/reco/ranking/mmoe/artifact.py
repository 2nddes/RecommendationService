from __future__ import annotations

import os

from app.common.settings import Settings


def load_latest_local_model(settings: Settings) -> str | None:
    active_path = settings.mmoe.model_path

    active_exists = os.path.exists(active_path)

    artifact_dir = os.path.join("data", "artifacts", "mmoe")

    latest: str | None = None
    if os.path.isdir(artifact_dir):
        candidates = [
            os.path.join(artifact_dir, name)
            for name in os.listdir(artifact_dir)
            if name.endswith(".pt")
        ]
        if candidates:
            latest = max(candidates, key=lambda p: os.path.getmtime(p))

    if latest is not None:
        latest_mtime = os.path.getmtime(latest)
        active_mtime = os.path.getmtime(active_path) if active_exists else -1.0
        if (not active_exists) or latest_mtime > active_mtime:
            os.makedirs(os.path.dirname(active_path) or ".", exist_ok=True)
            tmp = active_path + ".tmp"
            with open(latest, "rb") as src, open(tmp, "wb") as dst:
                dst.write(src.read())
            os.replace(tmp, active_path)
            active_exists = True

    return active_path if active_exists else None
