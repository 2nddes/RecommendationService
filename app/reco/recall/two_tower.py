from __future__ import annotations

from dataclasses import dataclass
import os
import threading
import time
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np

try:
    import hnswlib  # type: ignore
except Exception:  # pragma: no cover
    hnswlib = None

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.common.settings import Settings


# ----------------------------
# MySQL helpers
# ----------------------------


_engine_by_dsn: dict[str, Engine] = {}


def _get_engine(mysql_dsn: str | None) -> Engine | None:
    if not mysql_dsn:
        return None
    dsn = str(mysql_dsn).strip()
    if not dsn:
        return None
    cached = _engine_by_dsn.get(dsn)
    if cached is not None:
        return cached
    try:
        _engine_by_dsn[dsn] = create_engine(dsn, pool_pre_ping=True)
        return _engine_by_dsn[dsn]
    except Exception:
        return None


def _execute(mysql_dsn: str | None, sql: str, params: dict) -> List[dict]:
    engine = _get_engine(mysql_dsn)
    if engine is None:
        return []

    try:
        with engine.connect() as conn:
            rs = conn.execute(text(sql), params)
            return [dict(row._mapping) for row in rs]
    except SQLAlchemyError:
        return []


# ----------------------------
# Two-tower embedding utilities
# ----------------------------


@dataclass(frozen=True)
class TwoTowerConfig:
    dim: int = 64
    seed: int = 20260105
    # 用户向量 = alpha * tag_pref + (1-alpha) * recent_items
    alpha: float = 0.7
    # 取最近交互的 item 数
    recent_item_limit: int = 50
    # 召回 TopK（通常大于 ctx.n）
    recall_topk: int = 300
    # hnswlib space: cosine/ip/l2
    space: str = "cosine"
    # index reload interval (seconds)
    reload_interval_s: float = 2.0
    # active index path
    index_path: str = os.path.join("data", "two_tower_items.hnsw")


def load_config_from_settings(settings: Settings) -> TwoTowerConfig:
    cfg = TwoTowerConfig(
        dim=int(settings.two_tower_dim),
        seed=int(settings.two_tower_seed),
        alpha=float(settings.two_tower_alpha),
        recent_item_limit=int(settings.two_tower_recent_item_limit),
        recall_topk=int(settings.recall_topk_two_tower),
        space=str(settings.two_tower_space or "cosine"),
        reload_interval_s=float(settings.two_tower_reload_interval_s),
        index_path=str(settings.two_tower_index_path or os.path.join("data", "two_tower_items.hnsw")),
    )
    # basic clamp
    dim = max(int(cfg.dim), 1)
    alpha = min(max(float(cfg.alpha), 0.0), 1.0)
    recent = max(int(cfg.recent_item_limit), 0)
    topk = max(int(cfg.recall_topk), 0)
    reload = max(float(cfg.reload_interval_s), 0.1)
    space = cfg.space if cfg.space in {"cosine", "ip", "l2"} else "cosine"
    index_path = str(cfg.index_path).strip() or os.path.join("data", "two_tower_items.hnsw")
    return TwoTowerConfig(
        dim=dim,
        seed=int(cfg.seed),
        alpha=alpha,
        recent_item_limit=recent,
        recall_topk=topk,
        space=space,
        reload_interval_s=reload,
        index_path=index_path,
    )


class DeterministicEmbedder:
    """用可复现的方式把离散 id 映射到向量。

    说明：这里用 hash(seed, id) 来初始化 RNG，生成标准正态向量，再做 L2 normalize。
    这不是训练得到的模型，但具备稳定性 + 可用的向量检索闭环；后续可替换为真实双塔权重。
    """

    def __init__(self, dim: int, seed: int) -> None:
        self.dim = dim
        self.seed = seed
        self._cache: Dict[int, np.ndarray] = {}
        self._lock = threading.Lock()

    def vec(self, key: int) -> np.ndarray:
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

            # 64-bit mix to make seed stable
            mixed = (int(key) * 1000003) ^ int(self.seed)
            rng = np.random.default_rng(np.uint64(mixed) & np.uint64(0xFFFFFFFFFFFFFFFF))
            v = rng.standard_normal(self.dim, dtype=np.float32)
            v = _l2_normalize(v)
            self._cache[key] = v
            return v


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    denom = float(np.linalg.norm(v) + 1e-12)
    return (v / denom).astype(np.float32, copy=False)


def _weighted_sum(vectors: Iterable[Tuple[np.ndarray, float]], dim: int) -> np.ndarray:
    out = np.zeros((dim,), dtype=np.float32)
    for v, w in vectors:
        if w == 0.0:
            continue
        out += (v * np.float32(w)).astype(np.float32, copy=False)
    return _l2_normalize(out)


# ----------------------------
# Vector index (HNSW) loader
# ----------------------------


@dataclass
class _IndexState:
    index_path: str
    dim: int
    space: str
    index: object | None = None
    mtime: float = 0.0
    last_check: float = 0.0


_index_lock = threading.RLock()
_index_state: _IndexState | None = None


def invalidate_index_cache() -> None:
    """Force in-memory ANN index cache to reload on next query."""

    global _index_state
    with _index_lock:
        _index_state = None


def _default_index_path() -> str:
    # legacy helper: kept for internal callers; prefer cfg.index_path.
    return os.path.join("data", "two_tower_items.hnsw")


def get_or_load_index(cfg: TwoTowerConfig) -> object | None:
    if hnswlib is None:
        return None

    index_path = str(cfg.index_path).strip() or _default_index_path()

    with _index_lock:
        global _index_state
        if _index_state is None or _index_state.index_path != index_path or _index_state.dim != cfg.dim or _index_state.space != cfg.space:
            _index_state = _IndexState(index_path=index_path, dim=cfg.dim, space=cfg.space)

        st = _index_state

        now = time.time()
        if st.index is not None and (now - st.last_check) < cfg.reload_interval_s:
            return st.index

        st.last_check = now

        try:
            mtime = os.path.getmtime(index_path)
        except OSError:
            st.index = None
            st.mtime = 0.0
            return None

        if st.index is not None and st.mtime == mtime:
            return st.index

        # load / reload
        idx = hnswlib.Index(space=cfg.space, dim=cfg.dim)
        idx.load_index(index_path)
        st.index = idx
        st.mtime = mtime
        return st.index


# ----------------------------
# Feature queries and embedding builders
# ----------------------------


def _fetch_movie_tags(movie_ids: Sequence[int], *, mysql_dsn: str | None) -> Mapping[int, List[Tuple[int, float]]]:
    if not movie_ids:
        return {}

    ids_tuple = tuple(int(x) for x in movie_ids)

    static_sql = """
    SELECT mts.movie_id AS movie_id, mts.tag_id AS tag_id, 1.0 AS weight
    FROM movie_tag_static mts
    WHERE mts.movie_id IN :movie_ids
    """

    dynamic_sql = """
    SELECT mtd.movie_id AS movie_id, mtd.tag_id AS tag_id, COALESCE(mtd.weight, 1.0) AS weight
    FROM movie_tag_dynamic mtd
    JOIN tag_dynamic_dict tdd ON tdd.id = mtd.tag_id
    WHERE mtd.movie_id IN :movie_ids
      AND tdd.status = 'approved'
    """

    rows = _execute(mysql_dsn, static_sql, {"movie_ids": ids_tuple})
    rows2 = _execute(mysql_dsn, dynamic_sql, {"movie_ids": ids_tuple})

    out: Dict[int, List[Tuple[int, float]]] = {int(mid): [] for mid in movie_ids}
    for r in list(rows) + list(rows2):
        try:
            mid = int(r["movie_id"])
            tid = int(r["tag_id"])
            w = float(r.get("weight") or 1.0)
        except Exception:
            continue
        out.setdefault(mid, []).append((tid, w))

    return out


def build_item_vector(
    movie_id: int,
    cfg: TwoTowerConfig,
    tag_embedder: DeterministicEmbedder,
    *,
    mysql_dsn: str | None,
) -> np.ndarray | None:
    tags_map = _fetch_movie_tags([movie_id], mysql_dsn=mysql_dsn)
    tags = tags_map.get(int(movie_id)) or []
    if not tags:
        return None

    vectors = [(tag_embedder.vec(tid), float(w)) for tid, w in tags]
    return _weighted_sum(vectors, cfg.dim)


def _fetch_user_interest_tags(user_id: int, *, mysql_dsn: str | None) -> List[Tuple[int, float]]:
    # 动态标签只保留 approved
    sql = """
    (
      SELECT uit.tag_id AS tag_id, uit.weight AS weight
      FROM user_interest_tag uit
      WHERE uit.user_id = :user_id AND uit.is_static = 1
    )
    UNION ALL
    (
      SELECT uit.tag_id AS tag_id, uit.weight AS weight
      FROM user_interest_tag uit
      JOIN tag_dynamic_dict tdd ON tdd.id = uit.tag_id
      WHERE uit.user_id = :user_id AND uit.is_static = 0
        AND tdd.status = 'approved'
    )
    """
    rows = _execute(mysql_dsn, sql, {"user_id": int(user_id)})
    out: List[Tuple[int, float]] = []
    for r in rows:
        try:
            tid = int(r["tag_id"])
            w = float(r.get("weight") or 0.0)
        except Exception:
            continue
        if w != 0.0:
            out.append((tid, w))
    return out


def _fetch_user_recent_actions(user_id: int, limit: int, *, mysql_dsn: str | None) -> List[int]:
    if limit <= 0:
        return []

    sql = """
    SELECT ua.movie_id AS movie_id
    FROM user_action ua
    WHERE ua.user_id = :user_id
      AND ua.movie_id IS NOT NULL
    ORDER BY ua.id DESC
    LIMIT :limit
    """
    rows = _execute(mysql_dsn, sql, {"user_id": int(user_id), "limit": int(limit)})
    out: List[int] = []
    for r in rows:
        try:
            out.append(int(r["movie_id"]))
        except Exception:
            continue
    return out


def build_user_vector(
    user_id: int,
    cfg: TwoTowerConfig,
    tag_embedder: DeterministicEmbedder,
    *,
    mysql_dsn: str | None,
) -> np.ndarray | None:
    # 1) 静态/动态兴趣标签向量
    tags = _fetch_user_interest_tags(user_id, mysql_dsn=mysql_dsn)
    tag_vec: np.ndarray | None = None
    if tags:
        tag_vec = _weighted_sum([(tag_embedder.vec(tid), float(w)) for tid, w in tags], cfg.dim)

    # 2) 最近交互 item 向量（实时变化）
    recent_ids = _fetch_user_recent_actions(user_id, cfg.recent_item_limit, mysql_dsn=mysql_dsn)
    recent_vec: np.ndarray | None = None
    if recent_ids:
        tags_map = _fetch_movie_tags(recent_ids, mysql_dsn=mysql_dsn)
        item_vecs: List[np.ndarray] = []
        for mid in recent_ids:
            t = tags_map.get(int(mid)) or []
            if not t:
                continue
            v = _weighted_sum([(tag_embedder.vec(tid), float(w)) for tid, w in t], cfg.dim)
            item_vecs.append(v)

        if item_vecs:
            # 简单平均 + 归一化
            recent_vec = _l2_normalize(np.mean(np.stack(item_vecs, axis=0), axis=0))

    if tag_vec is None and recent_vec is None:
        return None

    if tag_vec is None:
        return recent_vec
    if recent_vec is None:
        return tag_vec

    # 融合（更偏向标签画像，仍保留实时交互）
    alpha = float(cfg.alpha)
    return _l2_normalize(alpha * tag_vec + (1.0 - alpha) * recent_vec)


def fetch_user_excluded_items(user_id: int, *, mysql_dsn: str | None) -> set[int]:
    sql = """
    SELECT DISTINCT x.movie_id
    FROM (
      SELECT movie_id FROM user_collection WHERE user_id = :user_id
      UNION ALL
      SELECT movie_id FROM user_action WHERE user_id = :user_id
    ) x
    """
    rows = _execute(mysql_dsn, sql, {"user_id": int(user_id)})
    excluded: set[int] = set()
    for r in rows:
        try:
            excluded.add(int(r["movie_id"]))
        except Exception:
            continue
    return excluded


# ----------------------------
# Online ANN search
# ----------------------------


_search_lock = threading.RLock()


def ann_search(vec: np.ndarray, k: int, cfg: TwoTowerConfig) -> List[Tuple[int, float]]:
    idx = get_or_load_index(cfg)
    if idx is None:
        return []

    k = max(int(k), 0)
    if k <= 0:
        return []

    q = vec.astype(np.float32, copy=False)
    if q.ndim == 1:
        q = q.reshape(1, -1)

    with _search_lock:
        labels, distances = idx.knn_query(q, k=k)

    # hnswlib cosine distance: 1 - cosine_similarity
    out: List[Tuple[int, float]] = []
    for lab, dist in zip(labels[0].tolist(), distances[0].tolist()):
        try:
            item_id = int(lab)
            d = float(dist)
        except Exception:
            continue

        if cfg.space == "cosine":
            score = 1.0 - d
        elif cfg.space == "l2":
            score = -d
        else:  # ip
            # hnswlib ip returns negative inner-product distance in many builds; keep a monotonic transform
            score = -d

        out.append((item_id, score))

    return out


# ----------------------------
# Offline / nearline index build
# ----------------------------


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
    """构建物品塔索引。

    - 物品向量：由 (静态/动态)标签 embedding 加权求和 + L2 normalize
    - 索引：hnswlib HNSW

    返回：成功写入的 item 数
    """

    if hnswlib is None:
        raise RuntimeError("hnswlib is not installed")

    engine = _get_engine(mysql_dsn)
    if engine is None:
        raise RuntimeError("MYSQL_DSN is not set")

    # movie id list
    if movie_ids is None:
        sql = """
        SELECT m.id AS movie_id
        FROM movie m
        WHERE m.deleted_at IS NULL
          AND m.status IN ('published')
        """
        rows = _execute(mysql_dsn, sql, {})
        movie_ids = [int(r["movie_id"]) for r in rows if r.get("movie_id") is not None]

    movie_ids = [int(x) for x in movie_ids]
    if not movie_ids:
        return 0

    # tags for all
    tags_map = _fetch_movie_tags(movie_ids, mysql_dsn=mysql_dsn)

    tag_embedder = DeterministicEmbedder(dim=cfg.dim, seed=cfg.seed)

    vectors: List[np.ndarray] = []
    labels: List[int] = []
    for mid in movie_ids:
        tags = tags_map.get(int(mid)) or []
        if not tags:
            continue
        v = _weighted_sum([(tag_embedder.vec(tid), float(w)) for tid, w in tags], cfg.dim)
        vectors.append(v)
        labels.append(int(mid))

    if not vectors:
        return 0

    data = np.stack(vectors, axis=0).astype(np.float32, copy=False)

    max_el = int(max_elements or max(len(labels), 1))

    idx = hnswlib.Index(space=cfg.space, dim=cfg.dim)
    idx.init_index(max_elements=max_el, ef_construction=int(ef_construction), M=int(M))
    idx.add_items(data, np.asarray(labels, dtype=np.int64))
    idx.set_ef(min(200, max(10, cfg.recall_topk)))

    os.makedirs(os.path.dirname(index_path) or ".", exist_ok=True)

    tmp_path = index_path + ".tmp"
    idx.save_index(tmp_path)
    # atomic-ish swap on Windows: replace if exists
    try:
        os.replace(tmp_path, index_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

    return len(labels)
