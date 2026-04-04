from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import threading
from typing import Any

import numpy as np
import torch

from app.common.settings import TwoTowerSettings


@dataclass
class TwoTowerModel:
    """In-memory Two-Tower model bundle used by recall runtime.

    Arrays are expected to be aligned by index:
    - user_ids[i] <-> user_emb[i]
    - item_ids[i] <-> item_emb[i]
    This alignment is required for fast id-to-vector lookup.
    """

    # Embedding dimension; must match user_emb/item_emb second axis.
    dim: int
    # User ids aligned with user_emb rows, shape: [num_users].
    user_ids: np.ndarray
    # Item ids aligned with item_emb rows, shape: [num_items].
    item_ids: np.ndarray
    # User embedding matrix, shape: [num_users, dim].
    user_emb: np.ndarray
    # Item embedding matrix, shape: [num_items, dim].
    item_emb: np.ndarray
    # Fast lookup map: user_id -> row index in user_ids/user_emb.
    user_id_to_index: dict[int, int]
    # Fast lookup map: item_id -> row index in item_ids/item_emb.
    item_id_to_index: dict[int, int]
    # Optional training/runtime metadata (encoder state, schema, etc.).
    metadata: dict[str, Any] | None = None

def l2_normalize(v: np.ndarray) -> np.ndarray:
    denom = float(np.linalg.norm(v) + 1e-12)
    return (v / denom).astype(np.float32, copy=False)


_model_lock = threading.RLock()
_model_cache: dict[str, tuple[float, TwoTowerModel]] = {}


def _model_build_indices(user_ids: np.ndarray, item_ids: np.ndarray) -> tuple[dict[int, int], dict[int, int]]:
    return (
        {int(uid): i for i, uid in enumerate(user_ids.tolist())},
        {int(iid): i for i, iid in enumerate(item_ids.tolist())},
    )


def save_model_weights(model: TwoTowerModel, model_path: str) -> None:
    os.makedirs(os.path.dirname(model_path) or ".", exist_ok=True)
    tmp = model_path + ".tmp"
    payload = {
        "version": 2,
        "dim": int(model.dim),
        "user_ids": torch.as_tensor(model.user_ids.astype(np.int64, copy=False)),
        "item_ids": torch.as_tensor(model.item_ids.astype(np.int64, copy=False)),
        "user_emb": torch.as_tensor(model.user_emb.astype(np.float32, copy=False)),
        "item_emb": torch.as_tensor(model.item_emb.astype(np.float32, copy=False)),
        "metadata": model.metadata or {},
        "trained_at": float(datetime.utcnow().timestamp()),
    }
    torch.save(payload, tmp)
    os.replace(tmp, model_path)


def load_model_weights(model_path: str) -> TwoTowerModel | None:
    path = str(model_path).strip()
    if not path or not os.path.exists(path):
        return None

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None

    with _model_lock:
        cached = _model_cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]

        try:
            data = torch.load(path, map_location="cpu", weights_only=False)
            if not isinstance(data, dict):
                return None

            dim = int(data["dim"])
            user_ids = torch.as_tensor(data["user_ids"], dtype=torch.int64).cpu().numpy()
            item_ids = torch.as_tensor(data["item_ids"], dtype=torch.int64).cpu().numpy()
            user_emb = torch.as_tensor(data["user_emb"], dtype=torch.float32).cpu().numpy()
            item_emb = torch.as_tensor(data["item_emb"], dtype=torch.float32).cpu().numpy()
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else None
        except Exception:
            return None

        if user_emb.ndim != 2 or item_emb.ndim != 2 or user_emb.shape[1] != dim or item_emb.shape[1] != dim:
            return None

        user_idx, item_idx = _model_build_indices(user_ids, item_ids)
        model = TwoTowerModel(
            dim=dim,
            user_ids=user_ids,
            item_ids=item_ids,
            user_emb=user_emb,
            item_emb=item_emb,
            user_id_to_index=user_idx,
            item_id_to_index=item_idx,
            metadata=metadata,
        )
        _model_cache[path] = (mtime, model)
        return model


def invalidate_model_cache() -> None:
    with _model_lock:
        _model_cache.clear()
