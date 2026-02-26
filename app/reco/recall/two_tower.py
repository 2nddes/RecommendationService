from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import sqlite3
import threading
import time
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from torch.utils.data import DataLoader, Dataset

try:
    import hnswlib  # type: ignore
except Exception:  # pragma: no cover
    hnswlib = None

from sqlalchemy import Engine, bindparam, create_engine, text
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


def _execute(mysql_dsn: str | None, sql: str, params: dict, *, expanding: Sequence[str] = ()) -> List[dict]:
    engine = _get_engine(mysql_dsn)
    if engine is None:
        return []

    try:
        with engine.connect() as conn:
            stmt = text(sql)
            for key in expanding:
                stmt = stmt.bindparams(bindparam(key, expanding=True))
            rs = conn.execute(stmt, params)
            return [dict(row._mapping) for row in rs]
    except SQLAlchemyError:
        return []


# ----------------------------
# Config + model
# ----------------------------


@dataclass(frozen=True)
class TwoTowerConfig:
    dim: int = 64
    seed: int = 20260105
    alpha: float = 0.7
    recent_item_limit: int = 50
    recall_topk: int = 300
    space: str = "cosine"
    reload_interval_s: float = 2.0
    index_path: str = os.path.join("data", "two_tower_items.hnsw")
    vector_db_path: str = os.path.join("data", "two_tower_vectors.db")
    model_path: str = os.path.join("data", "models", "two_tower_latest.pt")

    # training
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


def load_config_from_settings(settings: Settings) -> TwoTowerConfig:
    cfg = TwoTowerConfig(
        dim=max(int(settings.two_tower_dim), 1),
        seed=int(settings.two_tower_seed),
        alpha=min(max(float(settings.two_tower_alpha), 0.0), 1.0),
        recent_item_limit=max(int(settings.two_tower_recent_item_limit), 0),
        recall_topk=max(int(settings.recall_topk_two_tower), 0),
        space=str(settings.two_tower_space or "cosine"),
        reload_interval_s=max(float(settings.two_tower_reload_interval_s), 0.1),
        index_path=str(settings.two_tower_index_path or os.path.join("data", "two_tower_items.hnsw")),
        vector_db_path=str(settings.two_tower_vector_db_path or os.path.join("data", "two_tower_vectors.db")),
        model_path=str(settings.two_tower_model_path or os.path.join("data", "models", "two_tower_latest.pt")),
        train_epochs=max(int(settings.two_tower_train_epochs), 1),
        train_batch_size=max(int(settings.two_tower_train_batch_size), 128),
        train_lr=max(float(settings.two_tower_train_lr), 1e-5),
        train_reg=max(float(settings.two_tower_train_reg), 0.0),
        train_negatives=max(int(settings.two_tower_train_negatives), 1),
        train_limit=max(int(settings.two_tower_train_limit), 1000),
    )

    space = cfg.space if cfg.space in {"cosine", "ip", "l2"} else "cosine"
    return TwoTowerConfig(
        dim=cfg.dim,
        seed=cfg.seed,
        alpha=cfg.alpha,
        recent_item_limit=cfg.recent_item_limit,
        recall_topk=cfg.recall_topk,
        space=space,
        reload_interval_s=cfg.reload_interval_s,
        index_path=cfg.index_path,
        vector_db_path=cfg.vector_db_path,
        model_path=cfg.model_path,
        train_epochs=cfg.train_epochs,
        train_batch_size=cfg.train_batch_size,
        train_lr=cfg.train_lr,
        train_reg=cfg.train_reg,
        train_negatives=cfg.train_negatives,
        train_limit=cfg.train_limit,
    )


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    denom = float(np.linalg.norm(v) + 1e-12)
    return (v / denom).astype(np.float32, copy=False)


_model_lock = threading.RLock()
_model_cache: dict[str, tuple[float, TwoTowerModel]] = {}


def _build_model_indices(user_ids: np.ndarray, item_ids: np.ndarray) -> tuple[dict[int, int], dict[int, int]]:
    return (
        {int(uid): i for i, uid in enumerate(user_ids.tolist())},
        {int(iid): i for i, iid in enumerate(item_ids.tolist())},
    )


def save_model_weights(model: TwoTowerModel, model_path: str) -> None:
    os.makedirs(os.path.dirname(model_path) or ".", exist_ok=True)
    tmp = model_path + ".tmp"
    payload = {
        "dim": int(model.dim),
        "user_ids": torch.as_tensor(model.user_ids.astype(np.int64, copy=False)),
        "item_ids": torch.as_tensor(model.item_ids.astype(np.int64, copy=False)),
        "user_emb": torch.as_tensor(model.user_emb.astype(np.float32, copy=False)),
        "item_emb": torch.as_tensor(model.item_emb.astype(np.float32, copy=False)),
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
        except Exception:
            return None

        if user_emb.ndim != 2 or item_emb.ndim != 2 or user_emb.shape[1] != dim or item_emb.shape[1] != dim:
            return None

        user_idx, item_idx = _build_model_indices(user_ids, item_ids)
        model = TwoTowerModel(
            dim=dim,
            user_ids=user_ids,
            item_ids=item_ids,
            user_emb=user_emb,
            item_emb=item_emb,
            user_id_to_index=user_idx,
            item_id_to_index=item_idx,
        )
        _model_cache[path] = (mtime, model)
        return model


def invalidate_model_cache() -> None:
    with _model_lock:
        _model_cache.clear()


def _fetch_training_interactions(mysql_dsn: str | None, *, limit: int) -> List[tuple[int, int, float]]:
    sql = """
    SELECT ua.user_id, ua.movie_id, ua.action_type
    FROM user_action ua
    WHERE ua.movie_id IS NOT NULL
    ORDER BY ua.created_at DESC
    LIMIT :limit
    """
    rows = _execute(mysql_dsn, sql, {"limit": int(limit)})

    action_weight = {
        "view": 0.2,
        "like": 1.0,
        "collect": 1.2,
        "share": 0.8,
        "comment": 0.7,
        "rate": 0.9,
        "dislike": 0.0,
    }

    out: list[tuple[int, int, float]] = []
    for row in rows:
        try:
            uid = int(row["user_id"])
            iid = int(row["movie_id"])
            w = float(action_weight.get(str(row.get("action_type") or "view"), 0.1))
        except Exception:
            continue
        if w > 0:
            out.append((uid, iid, w))

    sql_rating = """
    SELECT r.user_id, r.movie_id, r.rating
    FROM rating r
    WHERE r.movie_id IS NOT NULL
    ORDER BY r.updated_at DESC
    LIMIT :limit
    """
    rows_r = _execute(mysql_dsn, sql_rating, {"limit": int(limit)})
    for row in rows_r:
        try:
            uid = int(row["user_id"])
            iid = int(row["movie_id"])
            rating = float(row.get("rating") or 0.0)
        except Exception:
            continue
        w = max((rating - 5.0) / 5.0, 0.0)
        if w > 0:
            out.append((uid, iid, w))

    sql_collect = """
    SELECT ucm.user_id, ucm.movie_id
    FROM user_collect_movie ucm
    ORDER BY ucm.created_at DESC
    LIMIT :limit
    """
    rows_c = _execute(mysql_dsn, sql_collect, {"limit": int(limit)})
    for row in rows_c:
        try:
            out.append((int(row["user_id"]), int(row["movie_id"]), 1.1))
        except Exception:
            continue

    return out


class _PositivePairDataset(Dataset[tuple[torch.Tensor, torch.Tensor]]):
    def __init__(self, pair_users: torch.Tensor, pair_items: torch.Tensor) -> None:
        self._pair_users = pair_users
        self._pair_items = pair_items

    def __len__(self) -> int:
        return int(self._pair_users.shape[0])

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor]:
        return self._pair_users[idx], self._pair_items[idx]


class _TwoTowerBPR(nn.Module):
    def __init__(self, user_count: int, item_count: int, dim: int, seed: int) -> None:
        super().__init__()
        self.user_table = nn.Embedding(user_count, dim)
        self.item_table = nn.Embedding(item_count, dim)
        gen = torch.Generator(device="cpu")
        gen.manual_seed(int(seed))
        with torch.no_grad():
            self.user_table.weight.normal_(mean=0.0, std=0.05, generator=gen)
            self.item_table.weight.normal_(mean=0.0, std=0.05, generator=gen)

    def forward(
        self,
        user_idx: torch.Tensor,
        pos_item_idx: torch.Tensor,
        neg_item_idx: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        pu = self.user_table(user_idx)
        pi = self.item_table(pos_item_idx)
        pj = self.item_table(neg_item_idx)
        logits = (pu * pi).sum(dim=1) - (pu * pj).sum(dim=1)
        l2 = (pu.pow(2).sum(dim=1) + pi.pow(2).sum(dim=1) + pj.pow(2).sum(dim=1)).mean()
        return logits, l2


def _sample_negative_items(
    *,
    users: torch.Tensor,
    user_pos_items: dict[int, set[int]],
    item_count: int,
    generator: torch.Generator,
) -> torch.Tensor:
    neg = torch.randint(item_count, size=(int(users.shape[0]),), generator=generator, dtype=torch.long)
    for idx, u_idx in enumerate(users.tolist()):
        positives = user_pos_items.get(int(u_idx), set())
        if not positives:
            continue
        tries = 0
        n = int(neg[idx].item())
        while n in positives and tries < 20:
            n = int(torch.randint(item_count, size=(1,), generator=generator).item())
            tries += 1
        neg[idx] = int(n)
    return neg


def train_two_tower_model(cfg: TwoTowerConfig, *, mysql_dsn: str | None) -> tuple[TwoTowerModel, dict[str, int | float]]:
    interactions = _fetch_training_interactions(mysql_dsn, limit=cfg.train_limit)
    if not interactions:
        raise RuntimeError("no_training_interactions")

    user_ids = sorted({u for u, _i, _w in interactions})
    item_ids = sorted({i for _u, i, _w in interactions})
    if not user_ids or not item_ids:
        raise RuntimeError("empty_user_or_item_set")

    user_id_to_index = {uid: i for i, uid in enumerate(user_ids)}
    item_id_to_index = {iid: i for i, iid in enumerate(item_ids)}

    # aggregate positives
    pos_weight: dict[tuple[int, int], float] = {}
    user_pos_items: dict[int, set[int]] = {}
    for uid, iid, w in interactions:
        ui = user_id_to_index[uid]
        ii = item_id_to_index[iid]
        pos_weight[(ui, ii)] = pos_weight.get((ui, ii), 0.0) + float(w)
        user_pos_items.setdefault(ui, set()).add(ii)

    pairs = list(pos_weight.items())
    if not pairs:
        raise RuntimeError("no_positive_pairs")

    torch.manual_seed(int(cfg.seed))
    generator = torch.Generator(device="cpu")
    generator.manual_seed(int(cfg.seed))

    net = _TwoTowerBPR(user_count=len(user_ids), item_count=len(item_ids), dim=cfg.dim, seed=int(cfg.seed))
    optimizer = torch.optim.Adam(net.parameters(), lr=float(cfg.train_lr))

    pair_users = torch.as_tensor([p[0][0] for p in pairs], dtype=torch.long)
    pair_items = torch.as_tensor([p[0][1] for p in pairs], dtype=torch.long)
    pair_weights = torch.as_tensor([p[1] for p in pairs], dtype=torch.float32)
    prob = pair_weights / torch.clamp(pair_weights.sum(), min=1e-12)

    dataset = _PositivePairDataset(pair_users=pair_users, pair_items=pair_items)
    sampled_size = max(len(pairs), int(cfg.train_batch_size))
    sampler = torch.utils.data.WeightedRandomSampler(
        weights=prob,
        num_samples=sampled_size,
        replacement=True,
        generator=generator,
    )
    loader = DataLoader(
        dataset,
        batch_size=int(cfg.train_batch_size),
        sampler=sampler,
        num_workers=0,
        drop_last=False,
    )

    negatives = int(cfg.train_negatives)
    item_count = len(item_ids)
    last_loss = 0.0

    for _epoch in range(int(cfg.train_epochs)):
        for batch_users, batch_items in loader:
            if negatives > 1:
                batch_users = batch_users.repeat_interleave(negatives)
                batch_items = batch_items.repeat_interleave(negatives)

            batch_neg_items = _sample_negative_items(
                users=batch_users,
                user_pos_items=user_pos_items,
                item_count=item_count,
                generator=generator,
            )

            logits, l2 = net(batch_users, batch_items, batch_neg_items)
            bpr_loss = -F.logsigmoid(logits).mean()
            reg_loss = float(cfg.train_reg) * l2
            loss = bpr_loss + reg_loss

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            last_loss = float(loss.item())

    with torch.no_grad():
        user_emb = F.normalize(net.user_table.weight.detach(), p=2, dim=1, eps=1e-12).cpu().numpy().astype(np.float32)
        item_emb = F.normalize(net.item_table.weight.detach(), p=2, dim=1, eps=1e-12).cpu().numpy().astype(np.float32)

    model = TwoTowerModel(
        dim=cfg.dim,
        user_ids=np.asarray(user_ids, dtype=np.int64),
        item_ids=np.asarray(item_ids, dtype=np.int64),
        user_emb=user_emb.astype(np.float32, copy=False),
        item_emb=item_emb.astype(np.float32, copy=False),
        user_id_to_index=user_id_to_index,
        item_id_to_index=item_id_to_index,
    )

    metrics = {
        "users": len(user_ids),
        "items": len(item_ids),
        "pairs": len(pairs),
        "epochs": int(cfg.train_epochs),
        "last_loss": float(last_loss),
    }
    return model, metrics


# ----------------------------
# Vector database
# ----------------------------


class ItemVectorStore:
    def __init__(self, db_path: str, dim: int) -> None:
        self.db_path = db_path
        self.dim = int(dim)
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS item_vector (
                  item_id INTEGER PRIMARY KEY,
                  dim INTEGER NOT NULL,
                  vector BLOB NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                  k TEXT PRIMARY KEY,
                  v TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def replace_all(self, vectors: Mapping[int, np.ndarray]) -> int:
        now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        rows = [
            (int(item_id), int(self.dim), np.asarray(vec, dtype=np.float32).tobytes(), now)
            for item_id, vec in vectors.items()
        ]

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM item_vector")
            if rows:
                conn.executemany(
                    """
                    INSERT INTO item_vector(item_id, dim, vector, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    rows,
                )
            conn.execute(
                """
                INSERT INTO meta(k, v) VALUES ('last_full_build_at', ?)
                ON CONFLICT(k) DO UPDATE SET v=excluded.v
                """,
                (now,),
            )
            conn.commit()
        return len(rows)

    def load_all(self) -> tuple[np.ndarray, np.ndarray]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT item_id, dim, vector FROM item_vector ORDER BY item_id ASC"
            ).fetchall()

        if not rows:
            return np.zeros((0,), dtype=np.int64), np.zeros((0, self.dim), dtype=np.float32)

        ids: List[int] = []
        vecs: List[np.ndarray] = []
        for item_id, dim, blob in rows:
            if int(dim) != self.dim:
                continue
            arr = np.frombuffer(blob, dtype=np.float32)
            if arr.size != self.dim:
                continue
            ids.append(int(item_id))
            vecs.append(arr.copy())

        if not ids:
            return np.zeros((0,), dtype=np.int64), np.zeros((0, self.dim), dtype=np.float32)

        return np.asarray(ids, dtype=np.int64), np.stack(vecs, axis=0).astype(np.float32, copy=False)


# ----------------------------
# Query-time vectors
# ----------------------------


def _fetch_user_recent_actions(user_id: int, limit: int, *, mysql_dsn: str | None) -> List[int]:
    if int(limit) <= 0:
        return []

    sql = """
    SELECT x.movie_id
    FROM (
      SELECT ua.movie_id AS movie_id, ua.created_at AS ts
      FROM user_action ua
      WHERE ua.user_id = :user_id AND ua.movie_id IS NOT NULL

      UNION ALL

      SELECT r.movie_id AS movie_id, r.updated_at AS ts
      FROM rating r
      WHERE r.user_id = :user_id AND r.movie_id IS NOT NULL

      UNION ALL

      SELECT ucm.movie_id AS movie_id, ucm.created_at AS ts
      FROM user_collect_movie ucm
      WHERE ucm.user_id = :user_id AND ucm.movie_id IS NOT NULL
    ) x
    ORDER BY x.ts DESC
    LIMIT :limit
    """
    rows = _execute(mysql_dsn, sql, {"user_id": int(user_id), "limit": int(limit)})
    out: List[int] = []
    for row in rows:
        try:
            out.append(int(row["movie_id"]))
        except Exception:
            continue
    return out


def build_item_vector(movie_id: int, cfg: TwoTowerConfig, _unused: object | None = None, *, mysql_dsn: str | None) -> np.ndarray | None:
    _ = mysql_dsn
    model = load_model_weights(cfg.model_path)
    if model is None:
        return None
    idx = model.item_id_to_index.get(int(movie_id))
    if idx is None:
        return None
    return model.item_emb[idx]


def build_user_vector(user_id: int, cfg: TwoTowerConfig, _unused: object | None = None, *, mysql_dsn: str | None) -> np.ndarray | None:
    model = load_model_weights(cfg.model_path)
    if model is None:
        return None

    vecs: List[np.ndarray] = []

    u_idx = model.user_id_to_index.get(int(user_id))
    if u_idx is not None:
        vecs.append(model.user_emb[u_idx])

    recent_ids = _fetch_user_recent_actions(int(user_id), int(cfg.recent_item_limit), mysql_dsn=mysql_dsn)
    rec_item_vecs = [model.item_emb[model.item_id_to_index[iid]] for iid in recent_ids if iid in model.item_id_to_index]
    if rec_item_vecs:
        vecs.append(_l2_normalize(np.mean(np.stack(rec_item_vecs, axis=0), axis=0)))

    if not vecs:
        return None
    if len(vecs) == 1:
        return _l2_normalize(vecs[0].copy())

    alpha = float(cfg.alpha)
    return _l2_normalize(alpha * vecs[0] + (1.0 - alpha) * vecs[1])


def fetch_user_excluded_items(user_id: int, *, mysql_dsn: str | None) -> set[int]:
    sql = """
    SELECT DISTINCT x.movie_id
    FROM (
      SELECT movie_id FROM user_collect_movie WHERE user_id = :user_id
      UNION ALL
      SELECT movie_id FROM rating WHERE user_id = :user_id
      UNION ALL
      SELECT movie_id FROM user_action WHERE user_id = :user_id
    ) x
    """
    rows = _execute(mysql_dsn, sql, {"user_id": int(user_id)})
    out: set[int] = set()
    for row in rows:
        try:
            out.add(int(row["movie_id"]))
        except Exception:
            continue
    return out


# ----------------------------
# Index + search
# ----------------------------


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


def _load_hnsw_from_disk(cfg: TwoTowerConfig) -> object | None:
    if hnswlib is None:
        return None
    try:
        idx = hnswlib.Index(space=cfg.space, dim=cfg.dim)
        idx.load_index(cfg.index_path)
        idx.set_ef(min(200, max(10, int(cfg.recall_topk))))
        return idx
    except Exception:
        return None


def _refresh_index_state(cfg: TwoTowerConfig) -> _IndexState:
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
        st.index = _load_hnsw_from_disk(cfg)
        st.mtime = mtime

    if st.ids is None or st.vectors is None or st.db_mtime != db_mtime:
        store = ItemVectorStore(cfg.vector_db_path, cfg.dim)
        st.ids, st.vectors = store.load_all()
        st.db_mtime = db_mtime

    return st


def ann_search(vec: np.ndarray, k: int, cfg: TwoTowerConfig) -> List[Tuple[int, float]]:
    # TODO：在向量数据库中查询
    k = max(int(k), 0)
    if k <= 0:
        return []

    query = vec.astype(np.float32, copy=False)
    if query.ndim == 1:
        query = query.reshape(1, -1)

    with _index_lock:
        st = _refresh_index_state(cfg)

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
                elif cfg.space == "l2":
                    score = -d
                else:
                    score = -d
                out.append((item_id, score))
            return out

        if st.ids is None or st.vectors is None or st.ids.size == 0:
            return []

        if cfg.space == "cosine":
            q = _l2_normalize(query[0])
            sims = st.vectors @ q
            top_idx = np.argsort(sims)[::-1][:k]
            return [(int(st.ids[i]), float(sims[i])) for i in top_idx.tolist()]

        dists = np.linalg.norm(st.vectors - query[0], axis=1)
        top_idx = np.argsort(dists)[:k]
        return [(int(st.ids[i]), float(-dists[i])) for i in top_idx.tolist()]


# ----------------------------
# Build vector DB + ANN from model
# ----------------------------


def _persist_hnsw_from_vectors(*, cfg: TwoTowerConfig, vectors: Mapping[int, np.ndarray], index_path: str) -> None:
    if hnswlib is None or not vectors:
        return

    item_ids = sorted(int(x) for x in vectors.keys())
    matrix = np.stack([np.asarray(vectors[mid], dtype=np.float32) for mid in item_ids], axis=0)

    idx = hnswlib.Index(space=cfg.space, dim=cfg.dim)
    idx.init_index(max_elements=max(len(item_ids), 1), ef_construction=200, M=16)
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
    _persist_hnsw_from_vectors(cfg=cfg, vectors=vectors, index_path=idx_path)
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
