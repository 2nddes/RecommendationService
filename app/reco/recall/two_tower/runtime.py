from __future__ import annotations

from dataclasses import dataclass
import logging
import threading

import hnswlib  # type: ignore
import torch

from app.common.settings import TwoTowerSettings

from .config_model import TwoTowerModel, load_model_weights
from .encoder import FeatureTwoTowerEncoder


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TwoTowerRuntime:
    cfg: TwoTowerSettings
    model: TwoTowerModel
    encoder: FeatureTwoTowerEncoder
    user_map: dict[int, int]
    item_map: dict[int, int]
    tag_map: dict[int, int]
    seq_len: int
    max_tags: int
    stats_dim: int
    profession_bucket_size: int
    index: object


_runtime_lock = threading.RLock()
_runtime: TwoTowerRuntime | None = None


def _build_feature_runtime(model: TwoTowerModel) -> tuple[
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


def _load_hnsw_index(cfg: TwoTowerSettings) -> object:
    if hnswlib is None:
        raise RuntimeError("two_tower_hnsw_unavailable")

    index = hnswlib.Index(space=cfg.space, dim=cfg.dim)
    index.load_index(cfg.index_path)
    index.set_ef(cfg.recall_topk)
    return index


def build_two_tower_runtime(cfg: TwoTowerSettings) -> TwoTowerRuntime:
    model = load_model_weights(cfg.model_path)
    if model is None:
        raise RuntimeError(f"two_tower_model_unavailable: {cfg.model_path}")

    encoder, user_map, item_map, tag_map, seq_len, max_tags, stats_dim, profession_bucket_size = _build_feature_runtime(model)
    index = _load_hnsw_index(cfg)
    logger.info("Two-tower runtime loaded, model_path=%s, index_path=%s", cfg.model_path, cfg.index_path)
    return TwoTowerRuntime(
        cfg=cfg,
        model=model,
        encoder=encoder,
        user_map=user_map,
        item_map=item_map,
        tag_map=tag_map,
        seq_len=seq_len,
        max_tags=max_tags,
        stats_dim=stats_dim,
        profession_bucket_size=profession_bucket_size,
        index=index,
    )


def initialize_two_tower_runtime(cfg: TwoTowerSettings) -> TwoTowerRuntime:
    runtime = build_two_tower_runtime(cfg)
    global _runtime
    with _runtime_lock:
        _runtime = runtime
        return _runtime


def get_two_tower_runtime() -> TwoTowerRuntime:
    with _runtime_lock:
        if _runtime is None:
            raise RuntimeError("two_tower_runtime_not_initialized")
        return _runtime