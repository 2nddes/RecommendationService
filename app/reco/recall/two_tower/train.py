from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

from .config_model import TwoTowerConfig, TwoTowerModel
from .encoder import FeatureTwoTowerEncoder
from .features import (
    age_bucket_index,
    fetch_all_movie_ids,
    fetch_item_stats,
    fetch_item_tags,
    fetch_user_profiles,
    fetch_user_recent_sequences,
    gender_index,
    register_bucket_index,
)


def _train_fetch_interactions(mysql_dsn: str | None, *, limit: int) -> list[tuple[int, int, float]]:
    from .db import execute

    sql = """
    SELECT t.user_id, t.movie_id, t.action_type, t.rating
    FROM (
      SELECT ua.user_id, ua.movie_id, ua.action_type, NULL AS rating, ua.created_at AS ts
      FROM user_action ua
      WHERE ua.movie_id IS NOT NULL

      UNION ALL

      SELECT r.user_id, r.movie_id, 'rate' AS action_type, r.rating AS rating, r.updated_at AS ts
      FROM rating r
      WHERE r.movie_id IS NOT NULL

      UNION ALL

      SELECT c.user_id, c.movie_id, 'collect' AS action_type, NULL AS rating, c.created_at AS ts
      FROM user_collect_movie c
      WHERE c.movie_id IS NOT NULL
    ) t
    ORDER BY t.ts DESC
    LIMIT :limit
    """

    rows = execute(mysql_dsn, sql, {"limit": int(limit)})

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
            action_type = str(row.get("action_type") or "view")
            rating = float(row.get("rating") or 0.0)
        except Exception:
            continue

        if action_type == "rate":
            w = max((rating - 5.0) / 5.0, 0.0)
        else:
            w = float(action_weight.get(action_type, 0.1))

        if w > 0:
            out.append((uid, iid, w))

    return out


@dataclass
class _PositivePairDataset(Dataset[tuple[torch.Tensor, torch.Tensor]]):
    pair_users: torch.Tensor
    pair_items: torch.Tensor

    def __len__(self) -> int:
        return int(self.pair_users.shape[0])

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor]:
        return self.pair_users[idx], self.pair_items[idx]


def _train_sample_negative_items(
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
    interactions = _train_fetch_interactions(mysql_dsn, limit=cfg.train_limit)
    if not interactions:
        raise RuntimeError("no_training_interactions")

    user_ids = sorted({u for u, _i, _w in interactions})
    item_ids_recent = sorted({i for _u, i, _w in interactions})
    all_item_ids = sorted(set(fetch_all_movie_ids(mysql_dsn)) | set(item_ids_recent))
    if not user_ids or not all_item_ids:
        raise RuntimeError("empty_user_or_item_set")

    user_id_to_index = {uid: (i + 1) for i, uid in enumerate(user_ids)}
    item_id_to_index = {iid: (i + 1) for i, iid in enumerate(all_item_ids)}
    user_count = len(user_ids) + 1
    item_count = len(all_item_ids) + 1

    pos_weight: dict[tuple[int, int], float] = {}
    user_pos_items: dict[int, set[int]] = {}
    for uid, iid, w in interactions:
        ui = user_id_to_index[uid]
        ii = item_id_to_index[iid]
        pos_weight[(ui, ii)] = pos_weight.get((ui, ii), 0.0) + float(w)
        user_pos_items.setdefault(ui, set()).add(ii)

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

    user_profiles = fetch_user_profiles(mysql_dsn, user_ids)
    user_sequences = fetch_user_recent_sequences(mysql_dsn, user_ids, recent_limit=max(int(cfg.recent_item_limit), 1))

    raw_item_tags = fetch_item_tags(mysql_dsn, all_item_ids)
    unique_tag_ids = sorted({tid for tags in raw_item_tags.values() for tid in tags})
    tag_id_to_index = {tid: (i + 1) for i, tid in enumerate(unique_tag_ids)}
    tag_count = len(unique_tag_ids) + 1

    item_stats_by_id = fetch_item_stats(mysql_dsn, all_item_ids)
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
        user_gender_idx[ui] = gender_index(profile.get("gender"))
        user_age_idx[ui] = age_bucket_index(profile.get("birth"))
        user_register_idx[ui] = register_bucket_index(profile.get("created_at"))

        seq_raw = user_sequences.get(uid, [])
        seq_idx = [item_id_to_index.get(mid, 0) for mid in seq_raw if item_id_to_index.get(mid, 0) > 0][:seq_len]
        if seq_idx:
            user_seq_items[ui, : len(seq_idx)] = torch.as_tensor(seq_idx, dtype=torch.long)
            user_seq_mask[ui, : len(seq_idx)] = True

    item_tag_ids = torch.zeros((item_count, max_tags), dtype=torch.long)
    item_tag_mask = torch.zeros((item_count, max_tags), dtype=torch.bool)
    item_stats = torch.zeros((item_count, stats_dim), dtype=torch.float32)
    for iid in all_item_ids:
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

    net = FeatureTwoTowerEncoder(
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
    loader = DataLoader(dataset, batch_size=int(cfg.train_batch_size), sampler=sampler, num_workers=0, drop_last=False)

    negatives = int(cfg.train_negatives)
    last_loss = 0.0

    for _epoch in range(int(cfg.train_epochs)):
        for batch_users, batch_items in loader:
            if negatives > 1:
                batch_users = batch_users.repeat_interleave(negatives)
                batch_items = batch_items.repeat_interleave(negatives)

            batch_neg_items = _train_sample_negative_items(
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
    public_item_id_to_index = {iid: i for i, iid in enumerate(all_item_ids)}

    model = TwoTowerModel(
        dim=cfg.dim,
        user_ids=np.asarray(user_ids, dtype=np.int64),
        item_ids=np.asarray(all_item_ids, dtype=np.int64),
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

    eval_k = max(1, min(int(cfg.hr_eval_k), len(all_item_ids)))
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

            if eval_k >= len(all_item_ids):
                topk_idx = np.arange(1, len(all_item_ids) + 1, dtype=np.int64)
            else:
                topk_idx = np.argpartition(scores, -eval_k)[-eval_k:]
            if int(test_ii) in set(int(x) for x in topk_idx.tolist()):
                hits += 1
        hr_at_k = float(hits / max(len(test_pairs), 1))

    metrics = {
        "users": len(user_ids),
        "items": len(all_item_ids),
        "recent_items": len(item_ids_recent),
        "pairs": len(train_pairs),
        "epochs": int(cfg.train_epochs),
        "last_loss": float(last_loss),
        "hr_k": int(eval_k),
        "hr_test_size": int(len(test_pairs)),
        "hr_at_k": hr_at_k if hr_at_k is not None else 0.0,
    }
    return model, metrics
