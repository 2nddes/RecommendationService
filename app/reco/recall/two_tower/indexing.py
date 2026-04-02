from __future__ import annotations

from dataclasses import dataclass
import os
import threading
import time
from typing import Dict, List, Mapping, Sequence, Tuple

import numpy as np

try:
    import hnswlib  # type: ignore
except Exception:  # pragma: no cover
    hnswlib = None

from app.common.settings import Settings

from .config_model import TwoTowerConfig, l2_normalize, load_config_from_settings, load_model_weights
from .store import ItemVectorStore


@dataclass
class _IndexState:
    index_path: str
    vector_db_path: str
    dim: int
    space: str
    index: object | None = None
    ids: np.ndarray | None = None
    vectors: np.ndarray | None = None
    mtime: float = 0.0
    db_mtime: float = 0.0
    last_check: float = 0.0


_index_state: _IndexState | None = None
_index_lock = threading.RLock()


def invalidate_index_cache() -> None:
    global _index_state
    with _index_lock:
        _index_state = None


def _index_load_hnsw_from_disk(cfg: TwoTowerConfig) -> object | None:
    if hnswlib is None:
        return None
    try:
        idx = hnswlib.Index(space=cfg.space, dim=cfg.dim)
        idx.load_index(cfg.index_path)
        idx.set_ef(min(200, max(10, int(cfg.recall_topk))))
        return idx
    except Exception:
        return None


def _index_refresh_state(cfg: TwoTowerConfig) -> _IndexState:
    global _index_state

    if (
        _index_state is None
        or _index_state.index_path != cfg.index_path
        or _index_state.vector_db_path != cfg.vector_db_path
        or _index_state.dim != cfg.dim
        or _index_state.space != cfg.space
    ):
        _index_state = _IndexState(
            index_path=cfg.index_path,
            vector_db_path=cfg.vector_db_path,
            dim=cfg.dim,
            space=cfg.space,
        )

    st = _index_state
    now = time.time()
    if now - st.last_check < float(cfg.reload_interval_s):
        return st
    st.last_check = now

    try:
        mtime = os.path.getmtime(cfg.index_path)
    except OSError:
        mtime = 0.0

    try:
        db_mtime = os.path.getmtime(cfg.vector_db_path)
    except OSError:
        db_mtime = 0.0

    if st.index is None or st.mtime != mtime:
        st.index = _index_load_hnsw_from_disk(cfg)
        st.mtime = mtime

    if st.ids is None or st.vectors is None or st.db_mtime != db_mtime:
        store = ItemVectorStore(cfg.vector_db_path, cfg.dim)
        st.ids, st.vectors = store.load_all()
        st.db_mtime = db_mtime

    return st


def ann_search(vec: np.ndarray, k: int, cfg: TwoTowerConfig) -> List[Tuple[int, float]]:
    k = int(k)
    if k <= 0:
        return []

    query = vec.astype(np.float32, copy=False)
    if query.ndim == 1:
        query = query.reshape(1, -1)

    with _index_lock:
        st = _index_refresh_state(cfg)

        if st.index is not None:
            labels, distances = st.index.knn_query(query, k=k)
            out: List[Tuple[int, float]] = []
            for label, dist in zip(labels[0].tolist(), distances[0].tolist()):
                try:
                    item_id = int(label)
                    d = float(dist)
                except Exception:
                    continue
                if cfg.space == "cosine":
                    score = 1.0 - d
                else:
                    score = -d
                out.append((item_id, score))
            return out

        if st.ids is None or st.vectors is None or st.ids.size == 0:
            return []

        if cfg.space == "cosine":
            q = l2_normalize(query[0])
            sims = st.vectors @ q
            top_idx = np.argsort(sims)[::-1][:k]
            return [(int(st.ids[i]), float(sims[i])) for i in top_idx.tolist()]

        dists = np.linalg.norm(st.vectors - query[0], axis=1)
        top_idx = np.argsort(dists)[:k]
        return [(int(st.ids[i]), float(-dists[i])) for i in top_idx.tolist()]


def _index_persist_hnsw_from_vectors(*, cfg: TwoTowerConfig, vectors: Mapping[int, np.ndarray], index_path: str) -> None:
    if hnswlib is None or not vectors:
        return

    item_ids = sorted(int(x) for x in vectors.keys())
    matrix = np.stack([np.asarray(vectors[mid], dtype=np.float32) for mid in item_ids], axis=0)

    idx = hnswlib.Index(space=cfg.space, dim=cfg.dim)
    idx.init_index(max_elements=len(item_ids), ef_construction=200, M=16)
    idx.add_items(matrix, np.asarray(item_ids, dtype=np.int64))
    idx.set_ef(min(200, max(10, int(cfg.recall_topk))))

    os.makedirs(os.path.dirname(index_path) or ".", exist_ok=True)
    tmp = index_path + ".tmp"
    idx.save_index(tmp)
    os.replace(tmp, index_path)


def materialize_item_vectors_from_model(
    *,
    cfg: TwoTowerConfig,
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
    invalidate_index_cache()
    return int(count)


def build_hnsw_index(
    *,
    index_path: str,
    cfg: TwoTowerConfig,
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
    cfg = load_config_from_settings(settings)

    if os.path.exists(cfg.model_path):
        load_model_weights(cfg.model_path)
        return cfg.model_path

    artifact_dir = os.path.join("data", "artifacts", "two_tower")
    if not os.path.isdir(artifact_dir):
        return None

    candidates = [os.path.join(artifact_dir, name) for name in os.listdir(artifact_dir) if name.endswith(".pt")]
    if not candidates:
        return None

    latest = max(candidates, key=lambda p: os.path.getmtime(p))
    os.makedirs(os.path.dirname(cfg.model_path) or ".", exist_ok=True)
    tmp = cfg.model_path + ".tmp"
    with open(latest, "rb") as src, open(tmp, "wb") as dst:
        dst.write(src.read())
    os.replace(tmp, cfg.model_path)
    load_model_weights(cfg.model_path)
    return cfg.model_path
