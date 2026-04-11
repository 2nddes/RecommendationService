from __future__ import annotations

from dataclasses import dataclass, field
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.json"


@lru_cache(maxsize=1)
def _load_raw_config() -> dict[str, Any]:
    with _CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError("config_root_invalid")
    return data


@dataclass(frozen=True)
class CoreSettings:
    internal_secret: str | None = None
    mysql_dsn: str | None = None
    reranking_seed: int | None = None


@dataclass(frozen=True)
class RedisSettings:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    username: str | None = None
    password: str | None = None
    ssl: bool = False
    socket_timeout_s: float = 0.5
    connect_timeout_s: float = 0.5


@dataclass(frozen=True)
class CacheSettings:
    key_prefix: str = "reco"
    feature_ttl_seconds: int = 3600
    recall_ttl_seconds: int = 1800
    trending_refresh_interval_seconds: int = 600
    static_recall_refresh_interval_seconds: int = 600
    trending_topk: int = 200
    static_recall_topk: int = 200
    user_reco_cache_size: int = 500
    user_reco_ttl_seconds: int = 86400
    user_reco_build_lock_seconds: int = 10
    user_reco_delivery_mode: str = "paged"


@dataclass(frozen=True)
class LogSettings:
    level: str = "INFO"
    file_path: str = "logs/app.log"


@dataclass(frozen=True)
class TwoTowerSettings:
    # Online recall / index settings.
    dim: int = 64
    seed: int = 20260105
    alpha: float = 0.7
    recent_item_limit: int = 50
    exclude_recent_n: int = 300
    recall_topk: int = 300
    hr_eval_k: int = 20
    space: str = "cosine"
    reload_interval_s: float = 2.0
    index_path: str = "data/two_tower_items.hnsw"
    vector_db_path: str = "data/two_tower_vectors.db"
    model_path: str = "data/models/two_tower_latest.pt"

    # Startup and periodic refresh settings.
    startup_build: bool = True
    daily_update_interval_hours: float = 24.0

    # Training settings.
    train_epochs: int = 6
    train_batch_size: int = 4096
    train_lr: float = 0.03
    train_reg: float = 0.0001
    train_negatives: int = 2
    train_limit: int = 300000
    train_steps_per_epoch: int = 200
    train_min_user_interactions: int = 10
    train_min_item_interactions: int = 5
    train_use_in_batch_negatives: bool = True
    train_in_batch_temperature: float = 0.07
    train_id_dropout: float = 0.15
    train_enable_deep_encoder: bool = True
    train_deep_hidden_mult: int = 2
    train_deep_dropout: float = 0.10


@dataclass(frozen=True)
class XGBSettings:
    model_path: str = "data/models/xgb_latest.json"
    use_mysql_features: bool = True
    train_limit: int = 5000
    train_max_depth: int = 5
    train_eta: float = 0.1
    train_subsample: float = 0.9
    train_colsample: float = 0.9
    train_rounds: int = 50


@dataclass(frozen=True)
class MMOESettings:
    model_path: str = "data/models/mmoe_latest.pt"
    train_limit: int = 50000
    train_epochs: int = 4
    train_batch_size: int = 1024
    train_lr: float = 0.001
    emb_dim: int = 32
    num_experts: int = 4
    expert_hidden_dim: int = 64
    tower_hidden_dim: int = 32
    in_batch_neg_ratio: int = 1
    click_neg_user_pool: int = 2000
    click_neg_movie_pool: int = 5000
    click_parquet_path: str = "data/offline/clicks.parquet"
    collect_parquet_path: str = "data/offline/collects.parquet"
    rate_parquet_path: str = "data/offline/rates.parquet"
    comment_parquet_path: str = "data/offline/comments.parquet"
    global_neg_ratio: int = 2
    loss_weight_click: float = 1.0
    loss_weight_collect: float = 5.0
    loss_weight_rate: float = 10.0
    loss_weight_comment: float = 20.0
    enable_dynamic_pos_weight: bool = True
    dynamic_pos_weight_cap: float = 20.0


@dataclass(frozen=True)
class RagSettings:
    embedding_model_name: str = "BAAI/bge-large-zh-v1.5"
    faiss_dir: str = "data/faiss/movie_rag"
    faiss_index_name: str = "movie_index"
    build_limit: int = 50000


@dataclass(frozen=True)
class TagRecallSettings:
    enabled: bool = False
    min_rating_count_m: int = 100
    retain_topn_per_tag: int = 1000
    user_topk_tags: int = 20
    per_tag_fetch_m: int = 200
    online_candidate_multiplier: int = 4
    high_rating_threshold: int = 8
    recent_interaction_limit: int = 300
    director_endorsement_source: str = "rating_count"


@dataclass(frozen=True)
class Settings:
    core: CoreSettings = field(default_factory=CoreSettings)
    redis: RedisSettings = field(default_factory=RedisSettings)
    cache: CacheSettings = field(default_factory=CacheSettings)
    log: LogSettings = field(default_factory=LogSettings)
    two_tower: TwoTowerSettings = field(default_factory=TwoTowerSettings)
    xgb: XGBSettings = field(default_factory=XGBSettings)
    mmoe: MMOESettings = field(default_factory=MMOESettings)
    rag: RagSettings = field(default_factory=RagSettings)
    tag_recall: TagRecallSettings = field(default_factory=TagRecallSettings)

    @staticmethod
    def _section(cfg: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        raw = cfg.get(key, {})
        if not isinstance(raw, Mapping):
            raise RuntimeError(f"config_section_invalid: {key}")
        return raw

    @staticmethod
    def from_config() -> "Settings":
        raw_cfg = _load_raw_config()

        return Settings(
            core=CoreSettings(**Settings._section(raw_cfg, "core")),
            redis=RedisSettings(**Settings._section(raw_cfg, "redis")),
            cache=CacheSettings(**Settings._section(raw_cfg, "cache")),
            log=LogSettings(**Settings._section(raw_cfg, "log")),
            two_tower=TwoTowerSettings(**Settings._section(raw_cfg, "two_tower")),
            xgb=XGBSettings(**Settings._section(raw_cfg, "xgb")),
            mmoe=MMOESettings(**Settings._section(raw_cfg, "mmoe")),
            rag=RagSettings(**Settings._section(raw_cfg, "rag")),
            tag_recall=TagRecallSettings(**Settings._section(raw_cfg, "tag_recall")),
        )
