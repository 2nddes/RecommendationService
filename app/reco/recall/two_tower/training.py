from __future__ import annotations

from datetime import datetime
import logging
import os
import time
from typing import Any, Dict

from app.common.settings import Settings
from app.ops.artifact_store import get_artifact_store
from app.reco.contracts.artifacts import write_manifest
from app.reco.training.common import log_event, log_exception


logger = logging.getLogger(__name__)


def train_two_tower_index(settings: Settings) -> Dict[str, Any]:
    store = get_artifact_store()
    started_at = time.time()

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join("data", "artifacts", "two_tower")
    os.makedirs(out_dir, exist_ok=True)
    artifact_model_path = os.path.join(out_dir, f"two_tower_{ts}.pt")
    artifact_index_path = os.path.join(out_dir, f"two_tower_{ts}.hnsw")
    artifact_vector_db_path = os.path.join(out_dir, f"two_tower_{ts}.db")
    log_event(
        logger,
        "info",
        "train.two_tower.start",
        index_path=artifact_index_path,
        model_path=artifact_model_path,
        stage="prepare",
        vector_db_path=artifact_vector_db_path,
    )

    from app.reco.recall.two_tower import (
        materialize_item_vectors_from_model,
        save_model_weights,
        train_two_tower_model,
    )

    cfg = settings.two_tower
    log_event(
        logger,
        "info",
        "train.two_tower.config_loaded",
        dim=cfg.dim,
        recall_topk=cfg.recall_topk,
        stage="prepare",
        train_batch_size=cfg.train_batch_size,
        train_epochs=cfg.train_epochs,
    )

    model, train_metrics = train_two_tower_model(cfg, mysql_dsn=settings.core.mysql_dsn)
    log_event(logger, "info", "train.two_tower.fit_done", metrics=train_metrics, stage="fit")
    save_model_weights(model, artifact_model_path)
    log_event(logger, "info", "train.two_tower.model_saved", model_path=artifact_model_path, stage="finalize")
    count = materialize_item_vectors_from_model(
        cfg=cfg,
        model_path=artifact_model_path,
        vector_db_path=artifact_vector_db_path,
        index_path=artifact_index_path,
    )
    log_event(logger, "info", "train.two_tower.index_done", items_indexed=int(count), stage="finalize")

    store.set("recall.two_tower.latest_model_artifact_path", artifact_model_path)
    store.set("recall.two_tower.latest_index_artifact_path", artifact_index_path)
    store.set("recall.two_tower.latest_vector_db_artifact_path", artifact_vector_db_path)
    store.set("recall.two_tower.latest_trained_at", ts)
    manifest_path = write_manifest(
        component="recall",
        model_name="two_tower",
        artifact_path=artifact_model_path,
        details={
            "index_path": artifact_index_path,
            "vector_db_path": artifact_vector_db_path,
            "items_indexed": int(count),
        },
    )
    elapsed_ms = int((time.time() - started_at) * 1000)
    log_event(logger, "info", "train.two_tower.done", elapsed_ms=elapsed_ms, stage="finalize", status="completed")

    return {
        "component": "recall",
        "name": "two_tower",
        "artifact_path": artifact_model_path,
        "trained": True,
        "details": {
            "items_indexed": int(count),
            "model_path": artifact_model_path,
            "index_path": artifact_index_path,
            "vector_db_path": artifact_vector_db_path,
            "manifest_path": manifest_path,
            **train_metrics,
        },
    }
