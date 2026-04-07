from __future__ import annotations

import logging
import numpy as np
import torch

from app.common.settings import TwoTowerSettings

from .config_model import TwoTowerModel, l2_normalize, load_model_weights
from .encoder import FeatureTwoTowerEncoder
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


logger = logging.getLogger(__name__)

def _online_load_feature_encoder(model: TwoTowerModel) -> tuple[
    FeatureTwoTowerEncoder,
    dict[int, int],
    dict[int, int],
    dict[int, int],
    int,
    int,
    int,
    int,
]:
    metadata = model.metadata
    if not isinstance(metadata, dict):
        raise RuntimeError("two_tower_model_metadata_invalid")

    encoder_meta = metadata.get("encoder")
    if not isinstance(encoder_meta, dict):
        raise RuntimeError("two_tower_encoder_metadata_missing")

    dim = int(encoder_meta["dim"])
    stats_dim = int(encoder_meta["stats_dim"])
    seq_len = int(encoder_meta["seq_len"])
    max_tags = int(encoder_meta["max_tags"])
    has_profession_feature = "profession_bucket_size" in encoder_meta
    profession_bucket_size = int(encoder_meta.get("profession_bucket_size", 64 if has_profession_feature else 1))
    enable_deep_encoder = bool(encoder_meta.get("enable_deep_encoder", False))
    deep_hidden_mult = int(encoder_meta.get("deep_hidden_mult", 2))
    deep_dropout = float(encoder_meta.get("deep_dropout", 0.10))
    user_count = int(encoder_meta["user_count"])
    item_count = int(encoder_meta["item_count"])
    tag_count = int(encoder_meta["tag_count"])
    state_dict = encoder_meta["state_dict"]
    if not isinstance(state_dict, dict):
        raise RuntimeError("two_tower_encoder_state_dict_invalid")

    user_map_raw = encoder_meta.get("user_id_to_train_index")
    item_map_raw = encoder_meta.get("item_id_to_train_index")
    tag_map_raw = encoder_meta.get("tag_id_to_index")
    if not isinstance(user_map_raw, dict) or not isinstance(item_map_raw, dict) or not isinstance(tag_map_raw, dict):
        raise RuntimeError("two_tower_encoder_id_mapping_invalid")

    user_map = {int(k): int(v) for k, v in user_map_raw.items()}
    item_map = {int(k): int(v) for k, v in item_map_raw.items()}
    tag_map = {int(k): int(v) for k, v in tag_map_raw.items()}

    encoder = FeatureTwoTowerEncoder(
        user_count=user_count,
        item_count=item_count,
        tag_count=tag_count,
        profession_bucket_size=profession_bucket_size,
        dim=dim,
        stats_dim=stats_dim,
        seed=0,
        enable_deep_encoder=enable_deep_encoder,
        deep_hidden_mult=deep_hidden_mult,
        deep_dropout=deep_dropout,
    )
    if not has_profession_feature:
        with torch.no_grad():
            encoder.profession_table.weight.zero_()
            encoder.profession_proj.weight.zero_()
            encoder.profession_proj.bias.zero_()

    encoder.load_state_dict(state_dict, strict=False)

    encoder.eval()
    return encoder, user_map, item_map, tag_map, seq_len, max_tags, stats_dim, profession_bucket_size


def build_item_vector(movie_id: int, cfg: TwoTowerSettings, *, mysql_dsn: str | None) -> np.ndarray | None:
    model = load_model_weights(cfg.model_path)
    if model is None:
        raise RuntimeError(f"two_tower_model_unavailable: {cfg.model_path}")

    idx = model.item_id_to_index.get(int(movie_id))
    if idx is None:
        bundle = _online_load_feature_encoder(model)
        encoder, _user_map, item_map, tag_map, _seq_len, max_tags, stats_dim, _profession_bucket_size = bundle
        raw_tags = fetch_item_tags(mysql_dsn, [int(movie_id)]).get(int(movie_id), [])
        tag_idx = [tag_map.get(int(tid), 0) for tid in raw_tags if tag_map.get(int(tid), 0) > 0][:max_tags]

        tag_tensor = torch.zeros((1, max_tags), dtype=torch.long)
        tag_mask = torch.zeros((1, max_tags), dtype=torch.bool)
        if tag_idx:
            tag_tensor[0, : len(tag_idx)] = torch.as_tensor(tag_idx, dtype=torch.long)
            tag_mask[0, : len(tag_idx)] = True

        stats_vec_raw = fetch_item_stats(mysql_dsn, [int(movie_id)]).get(int(movie_id), np.zeros((stats_dim,), dtype=np.float32))
        stats_vec = np.zeros((stats_dim,), dtype=np.float32)
        copy_n = min(int(stats_dim), int(stats_vec_raw.shape[0]))
        if copy_n > 0:
            stats_vec[:copy_n] = stats_vec_raw[:copy_n]
        stats_tensor = torch.as_tensor(stats_vec.reshape(1, -1), dtype=torch.float32)
        item_train_idx = int(item_map.get(int(movie_id), 0))

        with torch.no_grad():
            vec = encoder.encode_item_inputs(
                item_id_idx=torch.as_tensor([item_train_idx], dtype=torch.long),
                tag_idx=tag_tensor,
                tag_mask=tag_mask,
                stats=stats_tensor,
            )[0].cpu().numpy()
        return l2_normalize(vec)

    return model.item_emb[idx]


def build_user_vector(user_id: int, cfg: TwoTowerSettings, *, mysql_dsn: str | None) -> np.ndarray | None:
    model = load_model_weights(cfg.model_path)
    if model is None:
        raise RuntimeError(f"two_tower_model_unavailable: {cfg.model_path}")

    bundle = _online_load_feature_encoder(model)
    encoder, user_map, item_map, _tag_map, seq_len, _max_tags, _stats_dim, profession_bucket_size = bundle
    user_profile = fetch_user_profiles(mysql_dsn, [user_id]).get(user_id, {})
    seq = fetch_user_recent_sequences(mysql_dsn, [user_id], recent_limit=seq_len).get(user_id, [])

    user_train_idx = int(user_map.get(user_id, 0))
    gender_idx = gender_index(user_profile.get("gender"))
    age_idx = age_bucket_index(user_profile.get("birth"))
    reg_idx = register_bucket_index(user_profile.get("created_at"))
    profession_idx = 0
    if profession_bucket_size > 1:
        profession_idx = profession_bucket_index(user_profile.get("profession"), bucket_size=profession_bucket_size)

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
            profession_idx=torch.as_tensor([profession_idx], dtype=torch.long),
            seq_item_idx=seq_tensor,
            seq_mask=seq_mask,
        )[0].cpu().numpy()
    return l2_normalize(vec)
