from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import threading
from typing import Any

import numpy as np
import torch

from app.common.settings import Settings


@dataclass(frozen=True)
class TwoTowerConfig:
    dim: int = 64
    seed: int = 20260105
    alpha: float = 0.7
    recent_item_limit: int = 50
    recall_topk: int = 300
    hr_eval_k: int = 20
    space: str = "cosine"
    reload_interval_s: float = 2.0
    index_path: str = os.path.join("data", "two_tower_items.hnsw")
    vector_db_path: str = os.path.join("data", "two_tower_vectors.db")
    model_path: str = os.path.join("data", "models", "two_tower_latest.pt")

    train_epochs: int = 6
    train_batch_size: int = 2048
    train_lr: float = 0.03
    train_reg: float = 1e-4
    train_negatives: int = 2
    train_limit: int = 300000


@dataclass
class TwoTowerModel:
    dim: int
    user_ids: np.ndarray
    item_ids: np.ndarray
    user_emb: np.ndarray
    item_emb: np.ndarray
    user_id_to_index: dict[int, int]
    item_id_to_index: dict[int, int]
    metadata: dict[str, Any] | None = None


def load_config_from_settings(settings: Settings) -> TwoTowerConfig:
    cfg = TwoTowerConfig(
        dim=int(settings.two_tower_dim),
        seed=int(settings.two_tower_seed),
        alpha=float(settings.two_tower_alpha),
        recent_item_limit=int(settings.two_tower_recent_item_limit),
        recall_topk=int(settings.recall_topk_two_tower),
        hr_eval_k=int(settings.two_tower_hr_eval_k),
        space=str(settings.two_tower_space or "cosine"),
        reload_interval_s=float(settings.two_tower_reload_interval_s),
        index_path=str(settings.two_tower_index_path or os.path.join("data", "two_tower_items.hnsw")),
        vector_db_path=str(settings.two_tower_vector_db_path or os.path.join("data", "two_tower_vectors.db")),
        model_path=str(settings.two_tower_model_path or os.path.join("data", "models", "two_tower_latest.pt")),
        train_epochs=int(settings.two_tower_train_epochs),
        train_batch_size=int(settings.two_tower_train_batch_size),
        train_lr=float(settings.two_tower_train_lr),
        train_reg=float(settings.two_tower_train_reg),
        train_negatives=int(settings.two_tower_train_negatives),
        train_limit=int(settings.two_tower_train_limit),
    )
    return cfg


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
