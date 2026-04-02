from __future__ import annotations

from dataclasses import dataclass

from app.common import config


@dataclass(frozen=True)
class Settings:
    # Frequently changed deployment settings.
    internal_secret: str | None = None
    mysql_dsn: str | None = None
    mmoe_model_path: str | None = None
    reranking_seed: int | None = None
    two_tower_dim: int = 64
    recall_topk_two_tower: int = 300
    two_tower_space: str = "cosine"
    two_tower_index_path: str = "data/two_tower_items.hnsw"
    two_tower_vector_db_path: str = "data/two_tower_vectors.db"
    two_tower_model_path: str = "data/models/two_tower_latest.pt"
    two_tower_startup_build: bool = True
    two_tower_daily_update_interval_hours: float = 24.0
    rag_embedding_model_name: str = "BAAI/bge-large-zh-v1.5"
    rag_faiss_dir: str = "data/faiss/movie_rag"
    rag_faiss_index_name: str = "movie_index"
    rag_build_limit: int = 50000

    # Code defaults for the active pipeline and optional offline training.
    xgb_model_path: str | None = None
    xgb_use_mysql_features: bool = True
    xgb_train_limit: int = 5000
    xgb_train_max_depth: int = 5
    xgb_train_eta: float = 0.1
    xgb_train_subsample: float = 0.9
    xgb_train_colsample: float = 0.9
    xgb_train_rounds: int = 50
    mmoe_train_limit: int = 50000
    mmoe_train_epochs: int = 4
    mmoe_train_batch_size: int = 1024
    mmoe_train_lr: float = 0.001
    mmoe_emb_dim: int = 32
    mmoe_num_experts: int = 4
    mmoe_expert_hidden_dim: int = 64
    mmoe_tower_hidden_dim: int = 32
    mmoe_in_batch_neg_ratio: int = 1
    mmoe_click_neg_user_pool: int = 2000
    mmoe_click_neg_movie_pool: int = 5000
    mmoe_click_parquet_path: str = "data/offline/clicks.parquet"
    mmoe_collect_parquet_path: str = "data/offline/collects.parquet"
    mmoe_rate_parquet_path: str = "data/offline/rates.parquet"
    mmoe_comment_parquet_path: str = "data/offline/comments.parquet"
    mmoe_global_neg_ratio: int = 2
    mmoe_loss_weight_click: float = 1.0
    mmoe_loss_weight_collect: float = 5.0
    mmoe_loss_weight_rate: float = 10.0
    mmoe_loss_weight_comment: float = 20.0
    two_tower_seed: int = 20260105
    two_tower_alpha: float = 0.7
    two_tower_recent_item_limit: int = 50
    two_tower_hr_eval_k: int = 20
    two_tower_reload_interval_s: float = 2.0
    two_tower_train_epochs: int = 6
    two_tower_train_batch_size: int = 2048
    two_tower_train_lr: float = 0.03
    two_tower_train_reg: float = 0.0001
    two_tower_train_negatives: int = 2
    two_tower_train_limit: int = 300000

    @staticmethod
    def from_config() -> "Settings":
        reranking_seed = config.get("RERANKING_SEED", None)
        try:
            reranking_seed_int = int(reranking_seed) if reranking_seed is not None else None
        except Exception:
            reranking_seed_int = None

        return Settings(
            internal_secret=config.get_str("INTERNAL_SECRET", None),
            mysql_dsn=config.get_str("MYSQL_DSN", None),
            mmoe_model_path=config.get_str("MMOE_MODEL_PATH", None),
            reranking_seed=reranking_seed_int,
            two_tower_dim=config.get_int("TWO_TOWER_DIM", 64),
            recall_topk_two_tower=config.get_int("RECALL_TOPK_TWO_TOWER", 300),
            two_tower_space=config.get_str("TWO_TOWER_SPACE", "cosine") or "cosine",
            two_tower_index_path=config.get_str("TWO_TOWER_INDEX_PATH", "data/two_tower_items.hnsw")
            or "data/two_tower_items.hnsw",
            two_tower_vector_db_path=config.get_str("TWO_TOWER_VECTOR_DB_PATH", "data/two_tower_vectors.db")
            or "data/two_tower_vectors.db",
            two_tower_model_path=config.get_str("TWO_TOWER_MODEL_PATH", "data/models/two_tower_latest.pt")
            or "data/models/two_tower_latest.pt",
            two_tower_startup_build=config.get_bool("TWO_TOWER_STARTUP_BUILD", True),
            two_tower_daily_update_interval_hours=config.get_float("TWO_TOWER_DAILY_UPDATE_INTERVAL_HOURS", 24.0),
            rag_embedding_model_name=config.get_str("RAG_EMBEDDING_MODEL_NAME", "BAAI/bge-large-zh-v1.5")
            or "BAAI/bge-large-zh-v1.5",
            rag_faiss_dir=config.get_str("RAG_FAISS_DIR", "data/faiss/movie_rag")
            or "data/faiss/movie_rag",
            rag_faiss_index_name=config.get_str("RAG_FAISS_INDEX_NAME", "movie_index") or "movie_index",
            rag_build_limit=config.get_int("RAG_BUILD_LIMIT", 50000),
        )
