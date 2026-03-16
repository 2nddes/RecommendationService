from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

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
    hr_eval_k: int = 20
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
    metadata: dict[str, Any] | None = None


def load_config_from_settings(settings: Settings) -> TwoTowerConfig:
    cfg = TwoTowerConfig(
        dim=max(int(settings.two_tower_dim), 1),
        seed=int(settings.two_tower_seed),
        alpha=min(max(float(settings.two_tower_alpha), 0.0), 1.0),
        recent_item_limit=max(int(settings.two_tower_recent_item_limit), 0),
        recall_topk=max(int(settings.recall_topk_two_tower), 0),
        hr_eval_k=max(int(settings.two_tower_hr_eval_k), 1),
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
        hr_eval_k=cfg.hr_eval_k,
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

        user_idx, item_idx = _build_model_indices(user_ids, item_ids)
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


def _parse_datetime_like(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if raw is None:
        return None
    text_val = str(raw).strip()
    if not text_val:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text_val, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text_val)
    except Exception:
        return None


def _age_bucket_index(raw_birth: object) -> int:
    dt = _parse_datetime_like(raw_birth)
    if dt is None:
        return 0
    age = max(int((datetime.utcnow().date() - dt.date()).days // 365), 0)
    if age <= 17:
        return 1
    if age <= 24:
        return 2
    if age <= 34:
        return 3
    if age <= 44:
        return 4
    if age <= 54:
        return 5
    return 6


def _register_bucket_index(raw_created_at: object) -> int:
    dt = _parse_datetime_like(raw_created_at)
    if dt is None:
        return 0
    days = max(int((datetime.utcnow() - dt).days), 0)
    if days < 30:
        return 1
    if days < 180:
        return 2
    if days < 365:
        return 3
    if days < 365 * 3:
        return 4
    return 5


def _gender_index(raw_gender: object) -> int:
    g = str(raw_gender or "unknown").strip().lower()
    if g == "male":
        return 1
    if g == "female":
        return 2
    return 0


def _fetch_user_profiles(mysql_dsn: str | None, user_ids: Sequence[int]) -> dict[int, dict[str, object]]:
    if not user_ids:
        return {}

    sql = """
    SELECT u.user_id, u.gender, u.birth, u.created_at
    FROM user u
    WHERE u.user_id IN :user_ids
    """
    rows = _execute(
        mysql_dsn,
        sql,
        {"user_ids": [int(x) for x in user_ids]},
        expanding=("user_ids",),
    )
    out: dict[int, dict[str, object]] = {}
    for row in rows:
        try:
            uid = int(row["user_id"])
        except Exception:
            continue
        out[uid] = {
            "gender": row.get("gender"),
            "birth": row.get("birth"),
            "created_at": row.get("created_at"),
        }
    return out


def _fetch_user_recent_sequences(
    mysql_dsn: str | None,
    user_ids: Sequence[int],
    *,
    recent_limit: int,
) -> dict[int, list[int]]:
    if not user_ids or int(recent_limit) <= 0:
        return {}

    sql = """
    SELECT x.user_id, x.movie_id, x.ts
    FROM (
      SELECT ua.user_id, ua.movie_id, ua.created_at AS ts
      FROM user_action ua
      WHERE ua.user_id IN :user_ids AND ua.movie_id IS NOT NULL

      UNION ALL

      SELECT r.user_id, r.movie_id, r.updated_at AS ts
      FROM rating r
      WHERE r.user_id IN :user_ids AND r.movie_id IS NOT NULL

      UNION ALL

      SELECT c.user_id, c.movie_id, c.created_at AS ts
      FROM user_collect_movie c
      WHERE c.user_id IN :user_ids AND c.movie_id IS NOT NULL
    ) x
    ORDER BY x.user_id ASC, x.ts DESC
    """
    rows = _execute(
        mysql_dsn,
        sql,
        {"user_ids": [int(x) for x in user_ids]},
        expanding=("user_ids",),
    )

    out: dict[int, list[int]] = {}
    limit = int(recent_limit)
    for row in rows:
        try:
            uid = int(row["user_id"])
            iid = int(row["movie_id"])
        except Exception:
            continue
        seq = out.setdefault(uid, [])
        if len(seq) < limit:
            seq.append(iid)
    return out


def _fetch_item_tags(mysql_dsn: str | None, item_ids: Sequence[int]) -> dict[int, list[int]]:
    if not item_ids:
        return {}
    sql = """
    SELECT mt.movie_id, mt.tag_id
    FROM movie_tag mt
    WHERE mt.movie_id IN :movie_ids
    ORDER BY mt.movie_id ASC, mt.weight DESC, mt.hot_score DESC
    """
    rows = _execute(
        mysql_dsn,
        sql,
        {"movie_ids": [int(x) for x in item_ids]},
        expanding=("movie_ids",),
    )
    out: dict[int, list[int]] = {}
    for row in rows:
        try:
            mid = int(row["movie_id"])
            tid = int(row["tag_id"])
        except Exception:
            continue
        out.setdefault(mid, []).append(tid)
    return out


def _build_item_stat_vector(row: Mapping[str, object]) -> np.ndarray:
    rating_count = float(row.get("rating_count") or 0.0)
    rating_sum = float(row.get("rating_sum") or 0.0)
    avg_rating = rating_sum / rating_count if rating_count > 0 else 0.0

    hist = [float(row.get(f"rating_{i}_count") or 0.0) for i in range(1, 11)]
    hist_total = max(sum(hist), 1.0)
    hist_ratio = [v / hist_total for v in hist]

    collect_cnt = float(row.get("collect_cnt") or 0.0)
    hot_cnt = float(row.get("hot_cnt_30d") or 0.0)
    return np.asarray(
        [avg_rating / 10.0, np.log1p(rating_count), *hist_ratio, np.log1p(collect_cnt), np.log1p(hot_cnt)],
        dtype=np.float32,
    )


def _fetch_item_stats(mysql_dsn: str | None, item_ids: Sequence[int]) -> dict[int, np.ndarray]:
    if not item_ids:
        return {}

    sql = """
    SELECT m.movie_id,
           m.rating_sum,
           m.rating_count,
           m.rating_1_count,
           m.rating_2_count,
           m.rating_3_count,
           m.rating_4_count,
           m.rating_5_count,
           m.rating_6_count,
           m.rating_7_count,
           m.rating_8_count,
           m.rating_9_count,
           m.rating_10_count,
           COALESCE(c.collect_cnt, 0) AS collect_cnt,
           COALESCE(h.hot_cnt_30d, 0) AS hot_cnt_30d
    FROM movie m
    LEFT JOIN (
        SELECT movie_id, COUNT(*) AS collect_cnt
        FROM user_collect_movie
        GROUP BY movie_id
    ) c ON c.movie_id = m.movie_id
    LEFT JOIN (
        SELECT movie_id, COUNT(*) AS hot_cnt_30d
        FROM user_action
        WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
        GROUP BY movie_id
    ) h ON h.movie_id = m.movie_id
    WHERE m.movie_id IN :movie_ids
    """
    rows = _execute(
        mysql_dsn,
        sql,
        {"movie_ids": [int(x) for x in item_ids]},
        expanding=("movie_ids",),
    )

    out: dict[int, np.ndarray] = {}
    for row in rows:
        try:
            mid = int(row["movie_id"])
        except Exception:
            continue
        out[mid] = _build_item_stat_vector(row)
    return out


class _FeatureTwoTowerEncoder(nn.Module):
    def __init__(
        self,
        *,
        user_count: int,
        item_count: int,
        tag_count: int,
        dim: int,
        stats_dim: int,
        seed: int,
    ) -> None:
        super().__init__()
        self.user_id_table = nn.Embedding(user_count, dim)
        self.item_id_table = nn.Embedding(item_count, dim)
        self.gender_table = nn.Embedding(3, dim)
        self.age_bucket_table = nn.Embedding(7, dim)
        self.register_bucket_table = nn.Embedding(6, dim)
        self.tag_table = nn.Embedding(tag_count, dim)
        self.item_stats_proj = nn.Linear(stats_dim, dim)
        self.user_proj = nn.Linear(dim * 5, dim)
        self.item_proj = nn.Linear(dim * 3, dim)

        gen = torch.Generator(device="cpu")
        gen.manual_seed(int(seed))
        with torch.no_grad():
            self.user_id_table.weight.normal_(mean=0.0, std=0.05, generator=gen)
            self.item_id_table.weight.normal_(mean=0.0, std=0.05, generator=gen)
            self.gender_table.weight.normal_(mean=0.0, std=0.05, generator=gen)
            self.age_bucket_table.weight.normal_(mean=0.0, std=0.05, generator=gen)
            self.register_bucket_table.weight.normal_(mean=0.0, std=0.05, generator=gen)
            self.tag_table.weight.normal_(mean=0.0, std=0.05, generator=gen)

    @staticmethod
    def _masked_mean(x: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
        m = mask.unsqueeze(-1).to(dtype=x.dtype)
        denom = torch.clamp(m.sum(dim=1), min=1e-6)
        return (x * m).sum(dim=1) / denom

    def encode_user_inputs(
        self,
        *,
        user_id_idx: torch.Tensor,
        gender_idx: torch.Tensor,
        age_bucket_idx: torch.Tensor,
        register_bucket_idx: torch.Tensor,
        seq_item_idx: torch.Tensor,
        seq_mask: torch.Tensor,
    ) -> torch.Tensor:
        uid_vec = self.user_id_table(user_id_idx)
        gender_vec = self.gender_table(gender_idx)
        age_vec = self.age_bucket_table(age_bucket_idx)
        reg_vec = self.register_bucket_table(register_bucket_idx)

        seq_emb = self.item_id_table(seq_item_idx)
        seq_vec = self._masked_mean(seq_emb, seq_mask)

        user_input = torch.cat([uid_vec, gender_vec, age_vec, reg_vec, seq_vec], dim=1)
        return F.normalize(self.user_proj(user_input), p=2, dim=1, eps=1e-12)

    def encode_item_inputs(
        self,
        *,
        item_id_idx: torch.Tensor,
        tag_idx: torch.Tensor,
        tag_mask: torch.Tensor,
        stats: torch.Tensor,
    ) -> torch.Tensor:
        iid_vec = self.item_id_table(item_id_idx)
        tag_emb = self.tag_table(tag_idx)
        tag_vec = self._masked_mean(tag_emb, tag_mask)
        stats_vec = self.item_stats_proj(stats)
        item_input = torch.cat([iid_vec, tag_vec, stats_vec], dim=1)
        return F.normalize(self.item_proj(item_input), p=2, dim=1, eps=1e-12)

    def forward(
        self,
        *,
        user_id_idx: torch.Tensor,
        user_gender_idx: torch.Tensor,
        user_age_idx: torch.Tensor,
        user_register_idx: torch.Tensor,
        user_seq_item_idx: torch.Tensor,
        user_seq_mask: torch.Tensor,
        pos_item_id_idx: torch.Tensor,
        pos_tag_idx: torch.Tensor,
        pos_tag_mask: torch.Tensor,
        pos_stats: torch.Tensor,
        neg_item_id_idx: torch.Tensor,
        neg_tag_idx: torch.Tensor,
        neg_tag_mask: torch.Tensor,
        neg_stats: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        pu = self.encode_user_inputs(
            user_id_idx=user_id_idx,
            gender_idx=user_gender_idx,
            age_bucket_idx=user_age_idx,
            register_bucket_idx=user_register_idx,
            seq_item_idx=user_seq_item_idx,
            seq_mask=user_seq_mask,
        )
        pi = self.encode_item_inputs(
            item_id_idx=pos_item_id_idx,
            tag_idx=pos_tag_idx,
            tag_mask=pos_tag_mask,
            stats=pos_stats,
        )
        pj = self.encode_item_inputs(
            item_id_idx=neg_item_id_idx,
            tag_idx=neg_tag_idx,
            tag_mask=neg_tag_mask,
            stats=neg_stats,
        )
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
    if int(item_count) <= 1:
        return torch.zeros((int(users.shape[0]),), dtype=torch.long)

    neg = torch.randint(1, item_count, size=(int(users.shape[0]),), generator=generator, dtype=torch.long)
    for idx, u_idx in enumerate(users.tolist()):
        positives = user_pos_items.get(int(u_idx), set())
        if not positives:
            continue
        tries = 0
        n = int(neg[idx].item())
        while n in positives and tries < 20:
            n = int(torch.randint(1, item_count, size=(1,), generator=generator).item())
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

    # Reserve index 0 for Unknown user/item/tag.
    user_id_to_index = {uid: (i + 1) for i, uid in enumerate(user_ids)}
    item_id_to_index = {iid: (i + 1) for i, iid in enumerate(item_ids)}
    user_count = len(user_ids) + 1
    item_count = len(item_ids) + 1

    # aggregate positives
    pos_weight: dict[tuple[int, int], float] = {}
    user_pos_items: dict[int, set[int]] = {}
    for uid, iid, w in interactions:
        ui = user_id_to_index[uid]
        ii = item_id_to_index[iid]
        pos_weight[(ui, ii)] = pos_weight.get((ui, ii), 0.0) + float(w)
        user_pos_items.setdefault(ui, set()).add(ii)

    # user-level holdout for HR@K: keep one positive as test when user has >=2 positives
    user_train_pos_items: dict[int, set[int]] = {}
    test_pairs: list[tuple[int, int]] = []
    for ui, items in user_pos_items.items():
        pos_items = sorted(items)
        if len(pos_items) >= 2:
            holdout_ii = max(pos_items, key=lambda ii: (float(pos_weight.get((ui, ii), 0.0)), int(ii)))
            train_set = {ii for ii in pos_items if ii != holdout_ii}
            if train_set:
                user_train_pos_items[ui] = train_set
                test_pairs.append((ui, holdout_ii))
            else:
                user_train_pos_items[ui] = set(pos_items)
        else:
            user_train_pos_items[ui] = set(pos_items)

    train_pairs = [
        ((ui, ii), w)
        for (ui, ii), w in pos_weight.items()
        if ii in user_train_pos_items.get(ui, set())
    ]
    if not train_pairs:
        raise RuntimeError("no_positive_pairs")

    user_profiles = _fetch_user_profiles(mysql_dsn, user_ids)
    user_sequences = _fetch_user_recent_sequences(
        mysql_dsn,
        user_ids,
        recent_limit=max(int(cfg.recent_item_limit), 1),
    )

    raw_item_tags = _fetch_item_tags(mysql_dsn, item_ids)
    unique_tag_ids = sorted({tid for tags in raw_item_tags.values() for tid in tags})
    tag_id_to_index = {tid: (i + 1) for i, tid in enumerate(unique_tag_ids)}
    tag_count = len(unique_tag_ids) + 1

    item_stats_by_id = _fetch_item_stats(mysql_dsn, item_ids)
    stats_dim = 14

    seq_len = max(int(cfg.recent_item_limit), 1)
    max_tags = 12

    user_gender_idx = torch.zeros((user_count,), dtype=torch.long)
    user_age_idx = torch.zeros((user_count,), dtype=torch.long)
    user_register_idx = torch.zeros((user_count,), dtype=torch.long)
    user_seq_items = torch.zeros((user_count, seq_len), dtype=torch.long)
    user_seq_mask = torch.zeros((user_count, seq_len), dtype=torch.bool)

    for uid in user_ids:
        ui = user_id_to_index[uid]
        profile = user_profiles.get(uid, {})
        user_gender_idx[ui] = _gender_index(profile.get("gender"))
        user_age_idx[ui] = _age_bucket_index(profile.get("birth"))
        user_register_idx[ui] = _register_bucket_index(profile.get("created_at"))

        seq_raw = user_sequences.get(uid, [])
        seq_idx = [item_id_to_index.get(mid, 0) for mid in seq_raw if item_id_to_index.get(mid, 0) > 0][:seq_len]
        if seq_idx:
            user_seq_items[ui, : len(seq_idx)] = torch.as_tensor(seq_idx, dtype=torch.long)
            user_seq_mask[ui, : len(seq_idx)] = True

    item_tag_ids = torch.zeros((item_count, max_tags), dtype=torch.long)
    item_tag_mask = torch.zeros((item_count, max_tags), dtype=torch.bool)
    item_stats = torch.zeros((item_count, stats_dim), dtype=torch.float32)
    for iid in item_ids:
        ii = item_id_to_index[iid]
        tag_idx = [tag_id_to_index.get(t, 0) for t in raw_item_tags.get(iid, []) if tag_id_to_index.get(t, 0) > 0][:max_tags]
        if tag_idx:
            item_tag_ids[ii, : len(tag_idx)] = torch.as_tensor(tag_idx, dtype=torch.long)
            item_tag_mask[ii, : len(tag_idx)] = True
        vec = item_stats_by_id.get(iid)
        if vec is not None and vec.shape[0] == stats_dim:
            item_stats[ii] = torch.as_tensor(vec, dtype=torch.float32)

    torch.manual_seed(int(cfg.seed))
    generator = torch.Generator(device="cpu")
    generator.manual_seed(int(cfg.seed))

    net = _FeatureTwoTowerEncoder(
        user_count=user_count,
        item_count=item_count,
        tag_count=tag_count,
        dim=cfg.dim,
        stats_dim=stats_dim,
        seed=int(cfg.seed),
    )
    optimizer = torch.optim.Adam(net.parameters(), lr=float(cfg.train_lr))

    pair_users = torch.as_tensor([p[0][0] for p in train_pairs], dtype=torch.long)
    pair_items = torch.as_tensor([p[0][1] for p in train_pairs], dtype=torch.long)
    pair_weights = torch.as_tensor([p[1] for p in train_pairs], dtype=torch.float32)
    prob = pair_weights / torch.clamp(pair_weights.sum(), min=1e-12)

    dataset = _PositivePairDataset(pair_users=pair_users, pair_items=pair_items)
    sampled_size = max(len(train_pairs), int(cfg.train_batch_size))
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
    last_loss = 0.0

    for _epoch in range(int(cfg.train_epochs)):
        for batch_users, batch_items in loader:
            if negatives > 1:
                batch_users = batch_users.repeat_interleave(negatives)
                batch_items = batch_items.repeat_interleave(negatives)

            batch_neg_items = _sample_negative_items(
                users=batch_users,
                user_pos_items=user_train_pos_items,
                item_count=item_count,
                generator=generator,
            )

            logits, l2 = net(
                user_id_idx=batch_users,
                user_gender_idx=user_gender_idx[batch_users],
                user_age_idx=user_age_idx[batch_users],
                user_register_idx=user_register_idx[batch_users],
                user_seq_item_idx=user_seq_items[batch_users],
                user_seq_mask=user_seq_mask[batch_users],
                pos_item_id_idx=batch_items,
                pos_tag_idx=item_tag_ids[batch_items],
                pos_tag_mask=item_tag_mask[batch_items],
                pos_stats=item_stats[batch_items],
                neg_item_id_idx=batch_neg_items,
                neg_tag_idx=item_tag_ids[batch_neg_items],
                neg_tag_mask=item_tag_mask[batch_neg_items],
                neg_stats=item_stats[batch_neg_items],
            )
            bpr_loss = -F.logsigmoid(logits).mean()
            reg_loss = float(cfg.train_reg) * l2
            loss = bpr_loss + reg_loss

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            last_loss = float(loss.item())

    with torch.no_grad():
        all_user_idx = torch.arange(user_count, dtype=torch.long)
        all_item_idx = torch.arange(item_count, dtype=torch.long)
        full_user_emb = net.encode_user_inputs(
            user_id_idx=all_user_idx,
            gender_idx=user_gender_idx,
            age_bucket_idx=user_age_idx,
            register_bucket_idx=user_register_idx,
            seq_item_idx=user_seq_items,
            seq_mask=user_seq_mask,
        ).cpu().numpy().astype(np.float32, copy=False)
        full_item_emb = net.encode_item_inputs(
            item_id_idx=all_item_idx,
            tag_idx=item_tag_ids,
            tag_mask=item_tag_mask,
            stats=item_stats,
        ).cpu().numpy().astype(np.float32, copy=False)

    user_emb = full_user_emb[1:]
    item_emb = full_item_emb[1:]

    public_user_id_to_index = {uid: i for i, uid in enumerate(user_ids)}
    public_item_id_to_index = {iid: i for i, iid in enumerate(item_ids)}

    model = TwoTowerModel(
        dim=cfg.dim,
        user_ids=np.asarray(user_ids, dtype=np.int64),
        item_ids=np.asarray(item_ids, dtype=np.int64),
        user_emb=user_emb.astype(np.float32, copy=False),
        item_emb=item_emb.astype(np.float32, copy=False),
        user_id_to_index=public_user_id_to_index,
        item_id_to_index=public_item_id_to_index,
        metadata={
            "encoder": {
                "dim": int(cfg.dim),
                "stats_dim": int(stats_dim),
                "seq_len": int(seq_len),
                "max_tags": int(max_tags),
                "user_count": int(user_count),
                "item_count": int(item_count),
                "tag_count": int(tag_count),
                "state_dict": {k: v.detach().cpu() for k, v in net.state_dict().items()},
                "user_id_to_train_index": {int(k): int(v) for k, v in user_id_to_index.items()},
                "item_id_to_train_index": {int(k): int(v) for k, v in item_id_to_index.items()},
                "tag_id_to_index": {int(k): int(v) for k, v in tag_id_to_index.items()},
            }
        },
    )

    eval_k = max(1, min(int(cfg.hr_eval_k), len(item_ids)))
    hr_at_k: float | None = None
    if test_pairs:
        hits = 0
        for ui, test_ii in test_pairs:
            user_vec = full_user_emb[ui]
            scores = np.matmul(full_item_emb, user_vec)
            scores[0] = -1e12

            exclude = user_train_pos_items.get(ui, set())
            if exclude:
                scores[np.asarray(list(exclude), dtype=np.int64)] = -1e12

            if eval_k >= len(item_ids):
                topk_idx = np.arange(1, len(item_ids) + 1, dtype=np.int64)
            else:
                topk_idx = np.argpartition(scores, -eval_k)[-eval_k:]
            if int(test_ii) in set(int(x) for x in topk_idx.tolist()):
                hits += 1
        hr_at_k = float(hits / max(len(test_pairs), 1))

    metrics = {
        "users": len(user_ids),
        "items": len(item_ids),
        "pairs": len(train_pairs),
        "epochs": int(cfg.train_epochs),
        "last_loss": float(last_loss),
        "hr_k": int(eval_k),
        "hr_test_size": int(len(test_pairs)),
        "hr_at_k": hr_at_k if hr_at_k is not None else 0.0,
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


def _load_feature_encoder(model: TwoTowerModel) -> tuple[
    _FeatureTwoTowerEncoder,
    dict[int, int],
    dict[int, int],
    dict[int, int],
    int,
    int,
] | None:
    metadata = model.metadata if isinstance(model.metadata, dict) else None
    encoder_meta = metadata.get("encoder") if isinstance(metadata, dict) else None
    if not isinstance(encoder_meta, dict):
        return None

    try:
        dim = int(encoder_meta["dim"])
        stats_dim = int(encoder_meta["stats_dim"])
        seq_len = int(encoder_meta["seq_len"])
        max_tags = int(encoder_meta["max_tags"])
        user_count = int(encoder_meta["user_count"])
        item_count = int(encoder_meta["item_count"])
        tag_count = int(encoder_meta["tag_count"])
        state_dict = encoder_meta["state_dict"]
        if not isinstance(state_dict, dict):
            return None
    except Exception:
        return None

    user_map_raw = encoder_meta.get("user_id_to_train_index")
    item_map_raw = encoder_meta.get("item_id_to_train_index")
    tag_map_raw = encoder_meta.get("tag_id_to_index")
    if not isinstance(user_map_raw, dict) or not isinstance(item_map_raw, dict) or not isinstance(tag_map_raw, dict):
        return None

    user_map = {int(k): int(v) for k, v in user_map_raw.items()}
    item_map = {int(k): int(v) for k, v in item_map_raw.items()}
    tag_map = {int(k): int(v) for k, v in tag_map_raw.items()}

    encoder = _FeatureTwoTowerEncoder(
        user_count=user_count,
        item_count=item_count,
        tag_count=tag_count,
        dim=dim,
        stats_dim=stats_dim,
        seed=0,
    )
    try:
        encoder.load_state_dict(state_dict, strict=True)
    except Exception:
        return None

    encoder.eval()
    return encoder, user_map, item_map, tag_map, seq_len, max_tags


def build_item_vector(movie_id: int, cfg: TwoTowerConfig, _unused: object | None = None, *, mysql_dsn: str | None) -> np.ndarray | None:
    model = load_model_weights(cfg.model_path)
    if model is None:
        return None

    idx = model.item_id_to_index.get(int(movie_id))
    if idx is None:
        bundle = _load_feature_encoder(model)
        if bundle is None:
            return None

        encoder, _user_map, item_map, tag_map, _seq_len, max_tags = bundle
        raw_tags = _fetch_item_tags(mysql_dsn, [int(movie_id)]).get(int(movie_id), [])
        tag_idx = [tag_map.get(int(tid), 0) for tid in raw_tags if tag_map.get(int(tid), 0) > 0][:max_tags]

        tag_tensor = torch.zeros((1, max_tags), dtype=torch.long)
        tag_mask = torch.zeros((1, max_tags), dtype=torch.bool)
        if tag_idx:
            tag_tensor[0, : len(tag_idx)] = torch.as_tensor(tag_idx, dtype=torch.long)
            tag_mask[0, : len(tag_idx)] = True

        stats_vec = _fetch_item_stats(mysql_dsn, [int(movie_id)]).get(int(movie_id), np.zeros((14,), dtype=np.float32))
        stats_tensor = torch.as_tensor(stats_vec.reshape(1, -1), dtype=torch.float32)
        item_train_idx = int(item_map.get(int(movie_id), 0))

        with torch.no_grad():
            vec = encoder.encode_item_inputs(
                item_id_idx=torch.as_tensor([item_train_idx], dtype=torch.long),
                tag_idx=tag_tensor,
                tag_mask=tag_mask,
                stats=stats_tensor,
            )[0].cpu().numpy()
        return _l2_normalize(vec)

    return model.item_emb[idx]


def build_user_vector(user_id: int, cfg: TwoTowerConfig, _unused: object | None = None, *, mysql_dsn: str | None) -> np.ndarray | None:
    model = load_model_weights(cfg.model_path)
    if model is None:
        return None

    bundle = _load_feature_encoder(model)
    if bundle is None:
        # Backward compatibility for old checkpoints without feature metadata.
        u_idx = model.user_id_to_index.get(int(user_id))
        return model.user_emb[u_idx] if u_idx is not None else None

    encoder, user_map, item_map, _tag_map, seq_len, _max_tags = bundle
    user_profile = _fetch_user_profiles(mysql_dsn, [int(user_id)]).get(int(user_id), {})
    seq = _fetch_user_recent_sequences(mysql_dsn, [int(user_id)], recent_limit=seq_len).get(int(user_id), [])

    user_train_idx = int(user_map.get(int(user_id), 0))
    gender_idx = _gender_index(user_profile.get("gender"))
    age_idx = _age_bucket_index(user_profile.get("birth"))
    reg_idx = _register_bucket_index(user_profile.get("created_at"))

    seq_item_idx = [item_map.get(int(mid), 0) for mid in seq if item_map.get(int(mid), 0) > 0][:seq_len]
    seq_tensor = torch.zeros((1, seq_len), dtype=torch.long)
    seq_mask = torch.zeros((1, seq_len), dtype=torch.bool)
    if seq_item_idx:
        seq_tensor[0, : len(seq_item_idx)] = torch.as_tensor(seq_item_idx, dtype=torch.long)
        seq_mask[0, : len(seq_item_idx)] = True

    with torch.no_grad():
        vec = encoder.encode_user_inputs(
            user_id_idx=torch.as_tensor([user_train_idx], dtype=torch.long),
            gender_idx=torch.as_tensor([gender_idx], dtype=torch.long),
            age_bucket_idx=torch.as_tensor([age_idx], dtype=torch.long),
            register_bucket_idx=torch.as_tensor([reg_idx], dtype=torch.long),
            seq_item_idx=seq_tensor,
            seq_mask=seq_mask,
        )[0].cpu().numpy()
    return _l2_normalize(vec)


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
