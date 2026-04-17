from __future__ import annotations

import numpy as np
import torch

from .config_model import l2_normalize
from .features import (
    age_bucket_index,
    fetch_item_stats,
    fetch_item_tags,
    fetch_user_profiles,
    fetch_user_recent_sequences,
    gender_index,
    profession_bucket_index,
    register_bucket_index,
)
from .runtime import get_two_tower_runtime


def build_item_vector(movie_id: int, *, mysql_dsn: str | None) -> np.ndarray | None:
    runtime = get_two_tower_runtime()
    model = runtime.model

    idx = model.item_id_to_index.get(int(movie_id))
    if idx is None:
        raw_tags = fetch_item_tags(mysql_dsn, [int(movie_id)]).get(int(movie_id), [])
        tag_idx = [
            runtime.tag_map.get(int(tag_id), 0)
            for tag_id in raw_tags
            if runtime.tag_map.get(int(tag_id), 0) > 0
        ][: runtime.max_tags]

        tag_tensor = torch.zeros((1, runtime.max_tags), dtype=torch.long)
        tag_mask = torch.zeros((1, runtime.max_tags), dtype=torch.bool)
        if tag_idx:
            tag_tensor[0, : len(tag_idx)] = torch.as_tensor(tag_idx, dtype=torch.long)
            tag_mask[0, : len(tag_idx)] = True

        stats_vec_raw = fetch_item_stats(mysql_dsn, [int(movie_id)]).get(
            int(movie_id),
            np.zeros((runtime.stats_dim,), dtype=np.float32),
        )
        stats_vec = np.zeros((runtime.stats_dim,), dtype=np.float32)
        copy_n = min(int(runtime.stats_dim), int(stats_vec_raw.shape[0]))
        if copy_n > 0:
            stats_vec[:copy_n] = stats_vec_raw[:copy_n]
        stats_tensor = torch.as_tensor(stats_vec.reshape(1, -1), dtype=torch.float32)
        item_train_idx = int(runtime.item_map.get(int(movie_id), 0))

        with torch.no_grad():
            vec = runtime.encoder.encode_item_inputs(
                item_id_idx=torch.as_tensor([item_train_idx], dtype=torch.long),
                tag_idx=tag_tensor,
                tag_mask=tag_mask,
                stats=stats_tensor,
            )[0].cpu().numpy()
        return l2_normalize(vec)

    return model.item_emb[idx]


def build_user_vector(user_id: int, *, mysql_dsn: str | None) -> np.ndarray | None:
    runtime = get_two_tower_runtime()
    user_profile = fetch_user_profiles(mysql_dsn, [user_id]).get(user_id, {})
    seq = fetch_user_recent_sequences(mysql_dsn, [user_id], recent_limit=runtime.seq_len).get(user_id, [])

    user_train_idx = int(runtime.user_map.get(user_id, 0))
    gender_idx = gender_index(user_profile.get("gender"))
    age_idx = age_bucket_index(user_profile.get("birth"))
    reg_idx = register_bucket_index(user_profile.get("created_at"))
    profession_idx = 0
    if runtime.profession_bucket_size > 1:
        profession_idx = profession_bucket_index(
            user_profile.get("profession"),
            bucket_size=runtime.profession_bucket_size,
        )

    seq_item_idx = [
        runtime.item_map.get(int(movie_id), 0)
        for movie_id in seq
        if runtime.item_map.get(int(movie_id), 0) > 0
    ][: runtime.seq_len]
    seq_tensor = torch.zeros((1, runtime.seq_len), dtype=torch.long)
    seq_mask = torch.zeros((1, runtime.seq_len), dtype=torch.bool)
    if seq_item_idx:
        seq_tensor[0, : len(seq_item_idx)] = torch.as_tensor(seq_item_idx, dtype=torch.long)
        seq_mask[0, : len(seq_item_idx)] = True

    with torch.no_grad():
        vec = runtime.encoder.encode_user_inputs(
            user_id_idx=torch.as_tensor([user_train_idx], dtype=torch.long),
            gender_idx=torch.as_tensor([gender_idx], dtype=torch.long),
            age_bucket_idx=torch.as_tensor([age_idx], dtype=torch.long),
            register_bucket_idx=torch.as_tensor([reg_idx], dtype=torch.long),
            profession_idx=torch.as_tensor([profession_idx], dtype=torch.long),
            seq_item_idx=seq_tensor,
            seq_mask=seq_mask,
        )[0].cpu().numpy()
    return l2_normalize(vec)