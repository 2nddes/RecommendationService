from __future__ import annotations

from dataclasses import dataclass
import logging
from math import ceil

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

from app.common.settings import TwoTowerSettings

from .config_model import TwoTowerModel
from .encoder import FeatureTwoTowerEncoder
from .features import (
    age_bucket_index,
    fetch_all_movie_ids,
    fetch_item_stats,
    fetch_item_tags,
    fetch_user_profiles,
    fetch_user_recent_sequences,
    gender_index,
    parse_datetime_like,
    profession_bucket_index,
    register_bucket_index,
)


logger = logging.getLogger(__name__)


def _train_fetch_interactions(cfg: TwoTowerSettings, *, mysql_dsn: str | None) -> list[tuple[int, int, float, float]]:
    from .db import execute

    sql = """
    SELECT t.user_id, t.movie_id, t.action_type, t.rating, t.ts
    FROM (
            SELECT * FROM (
                SELECT uc.user_id, uc.movie_id, 'view' AS action_type, NULL AS rating, uc.created_at AS ts
                FROM user_click uc
                WHERE uc.movie_id IS NOT NULL
                ORDER BY uc.created_at DESC
                LIMIT :limit
            ) action_recent

      UNION ALL

            SELECT * FROM (
                SELECT r.user_id, r.movie_id, 'rating' AS action_type, r.rating AS rating, r.updated_at AS ts
                FROM rating r
                WHERE r.movie_id IS NOT NULL
                ORDER BY r.updated_at DESC
                LIMIT :limit
            ) rating_recent

      UNION ALL

            SELECT * FROM (
                SELECT mc.user_id, mc.movie_id, 'comment' AS action_type, NULL AS rating, mc.created_at AS ts
                FROM movie_comment mc
                WHERE mc.movie_id IS NOT NULL
                AND mc.deleted_at IS NULL
                ORDER BY mc.created_at DESC
                LIMIT :limit
            ) comment_recent

        UNION ALL

            SELECT * FROM (
                SELECT c.user_id, c.movie_id, 'collect' AS action_type, NULL AS rating, c.created_at AS ts
                FROM user_collect_movie c
                WHERE c.movie_id IS NOT NULL
                ORDER BY c.created_at DESC
                LIMIT :limit
            ) collect_recent
    ) t
    ORDER BY t.ts DESC
    LIMIT :limit
    """

    rows = execute(mysql_dsn, sql, {"limit": int(cfg.train_limit)})

    action_weight = {
        "view": 0.2,
        "like": 1.0,
        "collect": 1.2,
        "share": 0.8,
        "comment": 0.7,
        "rating": 0.9,
        "dislike": 0.0,
    }

    out: list[tuple[int, int, float, float]] = []
    for row in rows:
        uid = int(row["user_id"])
        iid = int(row["movie_id"])
        ts_dt = parse_datetime_like(row.get("ts"))
        ts = float(ts_dt.timestamp()) if ts_dt is not None else 0.0
        action_type_raw = row.get("action_type")
        if action_type_raw is None:
            raise ValueError("action_type_missing")
        action_type = str(action_type_raw).strip().lower()
        rating_raw = row.get("rating")
        if action_type == "rating":
            if rating_raw is None:
                raise ValueError("rating_missing_for_rating_action")
            rating = float(rating_raw)
            if rating < 1.0 or rating > 10.0:
                raise ValueError("rating_out_of_range")
        else:
            rating = 0.0

        if action_type == "rating":
            w = max((rating - 5.0) / 5.0, 0.0)
        else:
            if action_type not in action_weight:
                raise RuntimeError(f"two_tower_unknown_action_type: {action_type}")
            w = float(action_weight[action_type])

        if w > 0:
            out.append((uid, iid, w, ts))

    return out


def _filter_tail_interactions(
    interactions: list[tuple[int, int, float, float]],
    *,
    min_user_interactions: int,
    min_item_interactions: int,
) -> tuple[list[tuple[int, int, float, float]], dict[str, int]]:
    if not interactions:
        return interactions, {"raw_interactions": 0, "filtered_interactions": 0}

    user_min = max(int(min_user_interactions), 1)
    item_min = max(int(min_item_interactions), 1)
    kept = list(interactions)

    while True:
        user_count: dict[int, int] = {}
        item_count: dict[int, int] = {}
        for uid, iid, _w, _ts in kept:
            user_count[uid] = user_count.get(uid, 0) + 1
            item_count[iid] = item_count.get(iid, 0) + 1

        valid_users = {uid for uid, c in user_count.items() if c >= user_min}
        valid_items = {iid for iid, c in item_count.items() if c >= item_min}
        nxt = [(uid, iid, w, ts) for uid, iid, w, ts in kept if uid in valid_users and iid in valid_items]
        if len(nxt) == len(kept):
            break
        kept = nxt
        if not kept:
            break

    raw_users = len({uid for uid, _iid, _w, _ts in interactions})
    raw_items = len({iid for _uid, iid, _w, _ts in interactions})
    kept_users = len({uid for uid, _iid, _w, _ts in kept})
    kept_items = len({iid for _uid, iid, _w, _ts in kept})
    return kept, {
        "raw_interactions": int(len(interactions)),
        "filtered_interactions": int(len(kept)),
        "raw_users": int(raw_users),
        "raw_items": int(raw_items),
        "filtered_users": int(kept_users),
        "filtered_items": int(kept_items),
    }


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


def _in_batch_contrastive_loss(
    *,
    user_vec: torch.Tensor,
    item_vec: torch.Tensor,
    item_idx: torch.Tensor,
    temperature: float,
) -> torch.Tensor:
    logits = torch.matmul(user_vec, item_vec.transpose(0, 1)) / max(float(temperature), 1e-6)
    bsz = int(logits.shape[0])
    if bsz <= 1:
        return torch.zeros((), dtype=logits.dtype, device=logits.device)

    labels = torch.arange(bsz, dtype=torch.long, device=logits.device)

    duplicate_mask = item_idx.unsqueeze(0).eq(item_idx.unsqueeze(1))
    duplicate_mask.fill_diagonal_(False)
    logits = logits.masked_fill(duplicate_mask, -1e9)
    logits_t = logits.transpose(0, 1)

    return 0.5 * (F.cross_entropy(logits, labels) + F.cross_entropy(logits_t, labels))


def train_two_tower_model(cfg: TwoTowerSettings, *, mysql_dsn: str | None) -> tuple[TwoTowerModel, dict[str, int | float]]:
    interactions_raw = _train_fetch_interactions(cfg, mysql_dsn=mysql_dsn)
    if not interactions_raw:
        raise RuntimeError("no_training_interactions")

    interactions, filter_stats = _filter_tail_interactions(
        interactions_raw,
        min_user_interactions=int(cfg.train_min_user_interactions),
        min_item_interactions=int(cfg.train_min_item_interactions),
    )
    if not interactions:
        raise RuntimeError("no_interactions_after_tail_filter")

    user_ids = sorted({u for u, _i, _w, _ts in interactions})
    item_ids_recent = sorted({i for _u, i, _w, _ts in interactions})
    all_item_ids = sorted(set(fetch_all_movie_ids(mysql_dsn)) | set(item_ids_recent))
    if not user_ids or not all_item_ids:
        raise RuntimeError("empty_user_or_item_set")

    user_id_to_index = {uid: (i + 1) for i, uid in enumerate(user_ids)}
    item_id_to_index = {iid: (i + 1) for i, iid in enumerate(all_item_ids)}
    user_count = len(user_ids) + 1
    item_count = len(all_item_ids) + 1

    pair_stats: dict[tuple[int, int], tuple[float, float]] = {}
    user_pos_items: dict[int, set[int]] = {}
    for uid, iid, w, ts in interactions:
        ui = user_id_to_index[uid]
        ii = item_id_to_index[iid]
        prev_w, prev_ts = pair_stats.get((ui, ii), (0.0, 0.0))
        pair_stats[(ui, ii)] = (float(prev_w + float(w)), max(float(prev_ts), float(ts)))
        user_pos_items.setdefault(ui, set()).add(ii)

    user_train_pos_items: dict[int, set[int]] = {}
    test_pairs: list[tuple[int, int]] = []
    for ui, items in user_pos_items.items():
        pos_items = sorted(items)
        if len(pos_items) >= 2:
            holdout_ii = max(
                pos_items,
                key=lambda ii: (
                    float(pair_stats.get((ui, ii), (0.0, 0.0))[1]),
                    float(pair_stats.get((ui, ii), (0.0, 0.0))[0]),
                    int(ii),
                ),
            )
            train_set = {ii for ii in pos_items if ii != holdout_ii}
            if train_set:
                user_train_pos_items[ui] = train_set
                test_pairs.append((ui, holdout_ii))
            else:
                user_train_pos_items[ui] = set(pos_items)
        else:
            user_train_pos_items[ui] = set(pos_items)

    train_pairs = [
        ((ui, ii), pair_stats[(ui, ii)][0])
        for (ui, ii) in pair_stats
        if ii in user_train_pos_items.get(ui, set())
    ]
    if not train_pairs:
        raise RuntimeError("no_positive_pairs")

    user_profiles = fetch_user_profiles(mysql_dsn, user_ids)
    user_sequences = fetch_user_recent_sequences(mysql_dsn, user_ids, recent_limit=int(cfg.recent_item_limit))

    raw_item_tags = fetch_item_tags(mysql_dsn, all_item_ids)
    unique_tag_ids = sorted({tid for tags in raw_item_tags.values() for tid in tags})
    tag_id_to_index = {tid: (i + 1) for i, tid in enumerate(unique_tag_ids)}
    tag_count = len(unique_tag_ids) + 1

    item_stats_by_id = fetch_item_stats(mysql_dsn, all_item_ids)
    stats_dim = 17

    seq_len = int(cfg.recent_item_limit)
    max_tags = 12
    profession_bucket_size = 64

    user_gender_idx = torch.zeros((user_count,), dtype=torch.long)
    user_age_idx = torch.zeros((user_count,), dtype=torch.long)
    user_register_idx = torch.zeros((user_count,), dtype=torch.long)
    user_profession_idx = torch.zeros((user_count,), dtype=torch.long)
    user_seq_items = torch.zeros((user_count, seq_len), dtype=torch.long)
    user_seq_mask = torch.zeros((user_count, seq_len), dtype=torch.bool)

    for uid in user_ids:
        ui = user_id_to_index[uid]
        profile = user_profiles.get(uid, {})
        user_gender_idx[ui] = gender_index(profile.get("gender"))
        user_age_idx[ui] = age_bucket_index(profile.get("birth"))
        user_register_idx[ui] = register_bucket_index(profile.get("created_at"))
        user_profession_idx[ui] = profession_bucket_index(profile.get("profession"), bucket_size=profession_bucket_size)

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
        profession_bucket_size=profession_bucket_size,
        dim=cfg.dim,
        stats_dim=stats_dim,
        seed=int(cfg.seed),
        enable_deep_encoder=bool(cfg.train_enable_deep_encoder),
        deep_hidden_mult=int(cfg.train_deep_hidden_mult),
        deep_dropout=float(cfg.train_deep_dropout),
    )
    optimizer = torch.optim.Adam(net.parameters(), lr=float(cfg.train_lr))

    pair_users = torch.as_tensor([p[0][0] for p in train_pairs], dtype=torch.long)
    pair_items = torch.as_tensor([p[0][1] for p in train_pairs], dtype=torch.long)
    pair_weights = torch.as_tensor([p[1] for p in train_pairs], dtype=torch.float32)
    prob = pair_weights / torch.clamp(pair_weights.sum(), min=1e-12)

    dataset = _PositivePairDataset(pair_users=pair_users, pair_items=pair_items)
    batch_size = max(int(cfg.train_batch_size), 2)
    default_steps = int(ceil(len(train_pairs) / batch_size))
    sampled_steps = max(int(cfg.train_steps_per_epoch), default_steps, 1)
    sampled_size = int(sampled_steps * batch_size)
    sampler = torch.utils.data.WeightedRandomSampler(
        weights=prob,
        num_samples=sampled_size,
        replacement=True,
        generator=generator,
    )
    loader = DataLoader(dataset, batch_size=batch_size, sampler=sampler, num_workers=0, drop_last=True)

    negatives = int(cfg.train_negatives)
    use_in_batch = bool(cfg.train_use_in_batch_negatives)
    in_batch_temp = float(cfg.train_in_batch_temperature)
    id_dropout = float(cfg.train_id_dropout)
    last_loss = 0.0

    for _epoch in range(int(cfg.train_epochs)):
        for batch_users, batch_items in loader:
            batch_users_raw = batch_users
            batch_items_raw = batch_items

            batch_users_input = batch_users_raw
            batch_items_input = batch_items_raw
            if id_dropout > 0:
                user_drop_mask = torch.rand((int(batch_users_raw.shape[0]),), generator=generator) < id_dropout
                item_drop_mask = torch.rand((int(batch_items_raw.shape[0]),), generator=generator) < id_dropout
                batch_users_input = batch_users_raw.clone()
                batch_items_input = batch_items_raw.clone()
                batch_users_input[user_drop_mask] = 0
                batch_items_input[item_drop_mask] = 0

            if use_in_batch:
                user_vec = net.encode_user_inputs(
                    user_id_idx=batch_users_input,
                    gender_idx=user_gender_idx[batch_users_raw],
                    age_bucket_idx=user_age_idx[batch_users_raw],
                    register_bucket_idx=user_register_idx[batch_users_raw],
                    profession_idx=user_profession_idx[batch_users_raw],
                    seq_item_idx=user_seq_items[batch_users_raw],
                    seq_mask=user_seq_mask[batch_users_raw],
                )
                item_vec = net.encode_item_inputs(
                    item_id_idx=batch_items_input,
                    tag_idx=item_tag_ids[batch_items_raw],
                    tag_mask=item_tag_mask[batch_items_raw],
                    stats=item_stats[batch_items_raw],
                )
                rank_loss = _in_batch_contrastive_loss(
                    user_vec=user_vec,
                    item_vec=item_vec,
                    item_idx=batch_items_raw,
                    temperature=in_batch_temp,
                )
                l2 = (user_vec.pow(2).sum(dim=1) + item_vec.pow(2).sum(dim=1)).mean()
                reg_loss = float(cfg.train_reg) * l2
                loss = rank_loss + reg_loss
            else:
                expanded_users = batch_users_raw
                expanded_items = batch_items_raw
                if negatives > 1:
                    expanded_users = batch_users_raw.repeat_interleave(negatives)
                    expanded_items = batch_items_raw.repeat_interleave(negatives)
                    batch_users_input = batch_users_input.repeat_interleave(negatives)
                    batch_items_input = batch_items_input.repeat_interleave(negatives)

                batch_neg_items = _train_sample_negative_items(
                    users=batch_users_raw,
                    user_pos_items=user_train_pos_items,
                    item_count=item_count,
                    generator=generator,
                )
                if negatives > 1:
                    batch_neg_items = batch_neg_items.repeat_interleave(negatives)

                logits, l2 = net(
                    user_id_idx=batch_users_input,
                    user_gender_idx=user_gender_idx[expanded_users],
                    user_age_idx=user_age_idx[expanded_users],
                    user_register_idx=user_register_idx[expanded_users],
                    user_profession_idx=user_profession_idx[expanded_users],
                    user_seq_item_idx=user_seq_items[expanded_users],
                    user_seq_mask=user_seq_mask[expanded_users],
                    pos_item_id_idx=batch_items_input,
                    pos_tag_idx=item_tag_ids[expanded_items],
                    pos_tag_mask=item_tag_mask[expanded_items],
                    pos_stats=item_stats[expanded_items],
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
            profession_idx=user_profession_idx,
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
                "profession_bucket_size": int(profession_bucket_size),
                "enable_deep_encoder": bool(cfg.train_enable_deep_encoder),
                "deep_hidden_mult": int(cfg.train_deep_hidden_mult),
                "deep_dropout": float(cfg.train_deep_dropout),
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

    eval_k = int(cfg.hr_eval_k)
    hr_at_k: float | None = None
    if test_pairs:
        hits = 0
        for ui, test_ii in test_pairs:
            user_vec = full_user_emb[ui]
            scores = np.matmul(full_item_emb, user_vec)

            valid_mask = np.ones((scores.shape[0],), dtype=bool)
            valid_mask[0] = False
            exclude = user_train_pos_items.get(ui, set())
            if exclude:
                valid_mask[np.asarray(list(exclude), dtype=np.int64)] = False

            candidate_idx = np.flatnonzero(valid_mask)
            if candidate_idx.size == 0:
                continue

            k = min(eval_k, int(candidate_idx.size))
            candidate_scores = scores[candidate_idx]
            if k == int(candidate_idx.size):
                topk_idx = candidate_idx
            else:
                topk_local = np.argpartition(candidate_scores, -k)[-k:]
                topk_idx = candidate_idx[topk_local]
            if int(test_ii) in set(int(x) for x in topk_idx.tolist()):
                hits += 1
        hr_at_k = float(hits / len(test_pairs))

    metrics = {
        "users": len(user_ids),
        "items": len(all_item_ids),
        "recent_items": len(item_ids_recent),
        "pairs": len(train_pairs),
        "epochs": int(cfg.train_epochs),
        "batch_size": int(batch_size),
        "steps_per_epoch": int(sampled_steps),
        "use_in_batch_negatives": 1 if use_in_batch else 0,
        "last_loss": float(last_loss),
        "hr_k": int(eval_k),
        "hr_test_size": int(len(test_pairs)),
        "hr_at_k": hr_at_k if hr_at_k is not None else 0.0,
        "tail_filter_raw_users": int(filter_stats["raw_users"]),
        "tail_filter_raw_items": int(filter_stats["raw_items"]),
        "tail_filter_users": int(filter_stats["filtered_users"]),
        "tail_filter_items": int(filter_stats["filtered_items"]),
        "tail_filter_raw_interactions": int(filter_stats["raw_interactions"]),
        "tail_filter_interactions": int(filter_stats["filtered_interactions"]),
    }
    return model, metrics
