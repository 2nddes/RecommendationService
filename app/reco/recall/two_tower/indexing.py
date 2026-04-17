from __future__ import annotations

import logging
import os
from typing import Dict, List, Mapping, Sequence, Tuple

import hnswlib  # type: ignore
import numpy as np

from app.common.settings import Settings, TwoTowerSettings

from .config_model import load_model_weights
from .runtime import get_two_tower_runtime
from .store import ItemVectorStore


logger = logging.getLogger(__name__)


def ann_search(vec: np.ndarray, k: int) -> List[Tuple[int, float]]:
    query = vec.astype(np.float32, copy=False)
    if query.ndim == 1:
        query = query.reshape(1, -1)

    runtime = get_two_tower_runtime()
    labels, distances = runtime.index.knn_query(query, k=k)
    out: List[Tuple[int, float]] = []
    for label, dist in zip(labels[0].tolist(), distances[0].tolist()):
        item_id = int(label)
        distance = float(dist)
        if runtime.cfg.space == "cosine":
            score = 1.0 - distance
        else:
            score = -distance
        out.append((item_id, score))
    return out


def _index_persist_hnsw_from_vectors(*, cfg: TwoTowerSettings, vectors: Mapping[int, np.ndarray], index_path: str) -> None:
    if hnswlib is None:
        raise RuntimeError("two_tower_hnsw_unavailable")
    if not vectors:
        return

    item_ids = sorted(int(x) for x in vectors.keys())
    matrix = np.stack([np.asarray(vectors[mid], dtype=np.float32) for mid in item_ids], axis=0)

    index = hnswlib.Index(space=cfg.space, dim=cfg.dim)
    index.init_index(max_elements=len(item_ids), ef_construction=200, M=16)
    index.add_items(matrix, np.asarray(item_ids, dtype=np.int64))
    index.set_ef(min(200, max(10, int(cfg.recall_topk))))

    os.makedirs(os.path.dirname(index_path) or ".", exist_ok=True)
    tmp = index_path + ".tmp"
    index.save_index(tmp)
    os.replace(tmp, index_path)


def materialize_item_vectors_from_model(
    *,
    cfg: TwoTowerSettings,
    model_path: str,
    vector_db_path: str | None = None,
    index_path: str | None = None,
) -> int:
    model = load_model_weights(model_path)
    if model is None:
        return 0

    vectors: Dict[int, np.ndarray] = {
        int(item_id): model.item_emb[idx] for idx, item_id in enumerate(model.item_ids.tolist())
    }

    vdb = vector_db_path or cfg.vector_db_path
    idx_path = index_path or cfg.index_path

    store = ItemVectorStore(vdb, cfg.dim)
    count = store.replace_all(vectors)
    _index_persist_hnsw_from_vectors(cfg=cfg, vectors=vectors, index_path=idx_path)
    return int(count)


def build_hnsw_index(
    *,
    index_path: str,
    cfg: TwoTowerSettings,
    mysql_dsn: str | None,
    movie_ids: Sequence[int] | None = None,
    max_elements: int | None = None,
    ef_construction: int = 200,
    M: int = 16,
) -> int:
    _ = (mysql_dsn, movie_ids, max_elements, ef_construction, M)
    return materialize_item_vectors_from_model(
        cfg=cfg,
        model_path=cfg.model_path,
        vector_db_path=cfg.vector_db_path,
        index_path=index_path,
    )


def load_latest_local_model(settings: Settings) -> str | None:
    cfg = settings.two_tower
    artifact_dir = os.path.join("data", "artifacts", "two_tower")
    active_exists = os.path.exists(cfg.model_path)

    latest: str | None = None
    if os.path.isdir(artifact_dir):
        candidates = [os.path.join(artifact_dir, name) for name in os.listdir(artifact_dir) if name.endswith(".pt")]
        if candidates:
            latest = max(candidates, key=lambda path: os.path.getmtime(path))

    if latest is not None:
        latest_mtime = os.path.getmtime(latest)
        active_mtime = os.path.getmtime(cfg.model_path) if active_exists else -1.0
        if (not active_exists) or latest_mtime > active_mtime:
            os.makedirs(os.path.dirname(cfg.model_path) or ".", exist_ok=True)
            tmp = cfg.model_path + ".tmp"
            with open(latest, "rb") as src, open(tmp, "wb") as dst:
                dst.write(src.read())
            os.replace(tmp, cfg.model_path)
            active_exists = True
            logger.info("Two-tower latest artifact promoted to active model, source=%s, target=%s", latest, cfg.model_path)

    if not active_exists:
        return None
    return cfg.model_path