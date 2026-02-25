from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.common import config


@dataclass(frozen=True)
class Settings:
    internal_secret: str | None = None
    recall_channels: list[str] = None  # type: ignore[assignment]
    ranking_method: str = "cf"
    xgb_model_path: str | None = None
    xgb_use_mysql_features: bool = True
    xgb_allow_fallback: bool = True
    reranking_method: str = "random_shuffle"
    reranking_seed: int | None = None
    mysql_dsn: str | None = None

    # Recall tuning
    recall_topk_user_collection: int = 200
    recall_per_seed_topk_user_collection: int = 50
    recall_topk_user_high_rating: int = 300
    recall_rating_threshold: int = 8
    recall_topk_user_interest_tag: int = 300
    recall_topk_item_similar_tag: int = 200

    # XGB training tuning
    xgb_train_limit: int = 5000
    xgb_train_max_depth: int = 5
    xgb_train_eta: float = 0.1
    xgb_train_subsample: float = 0.9
    xgb_train_colsample: float = 0.9
    xgb_train_rounds: int = 50

    # Two-tower
    two_tower_index_path: str = "data/two_tower_items.hnsw"
    two_tower_dim: int = 64
    two_tower_seed: int = 20260105
    two_tower_alpha: float = 0.7
    two_tower_recent_item_limit: int = 50
    recall_topk_two_tower: int = 300
    two_tower_space: str = "cosine"
    two_tower_reload_interval_s: float = 2.0
    two_tower_vector_db_path: str = "data/two_tower_vectors.db"
    two_tower_model_path: str = "data/models/two_tower_latest.pt"
    two_tower_train_epochs: int = 6
    two_tower_train_batch_size: int = 2048
    two_tower_train_lr: float = 0.03
    two_tower_train_reg: float = 0.0001
    two_tower_train_negatives: int = 2
    two_tower_train_limit: int = 300000
    two_tower_startup_build: bool = True
    two_tower_daily_update_interval_hours: float = 24.0
    startup_prewarm_cf: bool = True

    @staticmethod
    def from_config() -> "Settings":
        recall_channels = config.get_str_list(
            "RECALL_CHANNELS",
            ["user_collection", "user_high_rating_similar", "user_interest_tag"],
        )

        reranking_seed = config.get("RERANKING_SEED", None)
        try:
            reranking_seed_int = int(reranking_seed) if reranking_seed is not None else None
        except Exception:
            reranking_seed_int = None

        return Settings(
            internal_secret=config.get_str("INTERNAL_SECRET", None),
            recall_channels=recall_channels,
            ranking_method=config.get_str("RANKING_METHOD", "cf") or "cf",
            xgb_model_path=config.get_str("XGB_MODEL_PATH", None),
            xgb_use_mysql_features=config.get_bool("XGB_USE_MYSQL_FEATURES", True),
            xgb_allow_fallback=config.get_bool("XGB_ALLOW_FALLBACK", True),
            reranking_method=config.get_str("RERANKING_METHOD", "random_shuffle") or "random_shuffle",
            reranking_seed=reranking_seed_int,
            mysql_dsn=config.get_str("MYSQL_DSN", None),

            recall_topk_user_collection=config.get_int("RECALL_TOPK_USER_COLLECTION", 200),
            recall_per_seed_topk_user_collection=config.get_int("RECALL_PER_SEED_TOPK_USER_COLLECTION", 50),
            recall_topk_user_high_rating=config.get_int("RECALL_TOPK_USER_HIGH_RATING", 300),
            recall_rating_threshold=config.get_int("RECALL_RATING_THRESHOLD", 8),
            recall_topk_user_interest_tag=config.get_int("RECALL_TOPK_USER_INTEREST_TAG", 300),
            recall_topk_item_similar_tag=config.get_int("RECALL_TOPK_ITEM_SIMILAR_TAG", 200),

            xgb_train_limit=config.get_int("XGB_TRAIN_LIMIT", 5000),
            xgb_train_max_depth=config.get_int("XGB_TRAIN_MAX_DEPTH", 5),
            xgb_train_eta=config.get_float("XGB_TRAIN_ETA", 0.1),
            xgb_train_subsample=config.get_float("XGB_TRAIN_SUBSAMPLE", 0.9),
            xgb_train_colsample=config.get_float("XGB_TRAIN_COLSAMPLE", 0.9),
            xgb_train_rounds=config.get_int("XGB_TRAIN_ROUNDS", 50),

            two_tower_index_path=config.get_str("TWO_TOWER_INDEX_PATH", "data/two_tower_items.hnsw")
            or "data/two_tower_items.hnsw",
            two_tower_dim=config.get_int("TWO_TOWER_DIM", 64),
            two_tower_seed=config.get_int("TWO_TOWER_SEED", 20260105),
            two_tower_alpha=config.get_float("TWO_TOWER_ALPHA", 0.7),
            two_tower_recent_item_limit=config.get_int("TWO_TOWER_RECENT_ITEM_LIMIT", 50),
            recall_topk_two_tower=config.get_int("RECALL_TOPK_TWO_TOWER", 300),
            two_tower_space=config.get_str("TWO_TOWER_SPACE", "cosine") or "cosine",
            two_tower_reload_interval_s=config.get_float("TWO_TOWER_RELOAD_INTERVAL_S", 2.0),
            two_tower_vector_db_path=config.get_str("TWO_TOWER_VECTOR_DB_PATH", "data/two_tower_vectors.db")
            or "data/two_tower_vectors.db",
            two_tower_model_path=config.get_str("TWO_TOWER_MODEL_PATH", "data/models/two_tower_latest.pt")
            or "data/models/two_tower_latest.pt",
            two_tower_train_epochs=config.get_int("TWO_TOWER_TRAIN_EPOCHS", 6),
            two_tower_train_batch_size=config.get_int("TWO_TOWER_TRAIN_BATCH_SIZE", 2048),
            two_tower_train_lr=config.get_float("TWO_TOWER_TRAIN_LR", 0.03),
            two_tower_train_reg=config.get_float("TWO_TOWER_TRAIN_REG", 0.0001),
            two_tower_train_negatives=config.get_int("TWO_TOWER_TRAIN_NEGATIVES", 2),
            two_tower_train_limit=config.get_int("TWO_TOWER_TRAIN_LIMIT", 300000),
            two_tower_startup_build=config.get_bool("TWO_TOWER_STARTUP_BUILD", True),
            two_tower_daily_update_interval_hours=config.get_float("TWO_TOWER_DAILY_UPDATE_INTERVAL_HOURS", 24.0),
            startup_prewarm_cf=config.get_bool("STARTUP_PREWARM_CF", True),
        )

    @staticmethod
    def from_env() -> "Settings":
        # Backward-compatible name; config.json is now the single source of truth.
        return Settings.from_config()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_config()
