from __future__ import annotations

import logging
import threading
import time
from time import perf_counter
from typing import Any
from typing import Callable

from app.ops.cache_ops import run_all_cache_precompute
from app.common.runtime_health import mark_component_error, mark_component_state, mark_component_success
from app.common.settings import Settings
from app.reco.online.runtime import initialize_pipeline, set_settings
from app.reco.rag_service import initialize_movie_rag_service
from app.reco.ranking.mmoe import load_latest_local_model as load_latest_mmoe_local_model
from app.reco.recall.two_tower import build_hnsw_index, load_latest_local_model as load_latest_two_tower_local_model


logger = logging.getLogger(__name__)

_worker_threads: dict[str, threading.Thread] = {}


def _load_active_recommendation_models(settings: Settings) -> dict[str, str]:
    mmoe_model_path = load_latest_mmoe_local_model(settings)
    if not mmoe_model_path:
        raise RuntimeError("mmoe_model_not_found")

    two_tower_model_path = load_latest_two_tower_local_model(settings)
    if not two_tower_model_path:
        raise RuntimeError("two_tower_model_not_found")

    return {
        "mmoe_model_path": mmoe_model_path,
        "two_tower_model_path": two_tower_model_path,
    }


def _run_two_tower_full_build(settings: Settings) -> dict[str, Any]:
    cfg = settings.two_tower
    logger.info("Starting two-tower full rebuild")
    count = build_hnsw_index(
        index_path=cfg.index_path,
        cfg=cfg,
        mysql_dsn=settings.core.mysql_dsn,
    )
    logger.info(
        "Two-tower rebuild finished, items=%s, index_path=%s",
        int(count),
        cfg.index_path,
    )
    return {
        "items_indexed": int(count),
        "vector_db": cfg.vector_db_path,
        "index_path": cfg.index_path,
        "model_path": cfg.model_path,
    }


def refresh_recommendation_runtime(
    settings: Settings,
    *,
    rebuild_two_tower_index: bool,
) -> dict[str, Any]:
    model_paths = _load_active_recommendation_models(settings)
    two_tower_status: dict[str, Any] | None = None
    if rebuild_two_tower_index:
        two_tower_status = _run_two_tower_full_build(settings)

    initialize_pipeline(settings)
    return {
        **model_paths,
        "two_tower": two_tower_status,
    }


def run_startup_warmup(settings: Settings) -> None:
    logger.info("Startup warmup begins")
    mark_component_state("warmup", ready=False, status="running")

    try:
        refresh_recommendation_runtime(settings, reason="startup", rebuild_two_tower_index=True)
        logger.info("Global recommendation pipeline initialized")
        initialize_movie_rag_service(settings)
        logger.info("Global RAG service initialized")

        summary = run_all_cache_precompute(settings)
        logger.info(
            "Startup cache precompute finished, summary=%s",
            summary,
        )

        logger.info("Startup warmup completed")
        mark_component_success("warmup", details={"reason": "startup"})
    except Exception as exc:
        logger.exception("Startup warmup failed")
        mark_component_error("warmup", exc, details={"reason": "startup"})
        raise


def _mark_worker_running(component: str, *, details: dict[str, Any] | None = None) -> None:
    mark_component_state(component, ready=True, status="running", details=details)


def _two_tower_refresh_worker(settings: Settings) -> None:
    component = "two_tower_refresh_worker"
    interval_seconds = settings.two_tower.daily_update_interval_hours * 3600.0
    _mark_worker_running(component, details={"interval_seconds": int(interval_seconds)})
    logger.info("Two-tower refresh worker begins, interval_seconds=%s", int(interval_seconds))
    while True:
        time.sleep(interval_seconds)
        try:
            refresh_recommendation_runtime(
                settings,
                reason="two_tower_refresh_worker",
                rebuild_two_tower_index=True,
            )
            mark_component_success(component, details={"interval_seconds": int(interval_seconds)})
            _mark_worker_running(component, details={"interval_seconds": int(interval_seconds)})
        except Exception as exc:
            logger.exception("Two-tower refresh iteration failed")
            mark_component_error(component, exc, details={"interval_seconds": int(interval_seconds)})


def _cache_precompute_worker(settings: Settings) -> None:
    component = "cache_precompute_worker"
    interval_seconds = max(float(settings.cache.trending_refresh_interval_seconds), 60.0)
    _mark_worker_running(component, details={"interval_seconds": int(interval_seconds)})
    logger.info("Cache precompute worker begins, interval_seconds=%s", int(interval_seconds))
    while True:
        time.sleep(interval_seconds)
        try:
            started = perf_counter()
            summary = run_all_cache_precompute(settings)
            logger.info(
                "Scheduled cache precompute finished, elapsed_ms=%.2f, summary=%s",
                (perf_counter() - started) * 1000.0,
                summary,
            )
            mark_component_success(component, details={"interval_seconds": int(interval_seconds)})
            _mark_worker_running(component, details={"interval_seconds": int(interval_seconds)})
        except Exception as exc:
            logger.exception("Cache precompute iteration failed")
            mark_component_error(component, exc, details={"interval_seconds": int(interval_seconds)})


def _train_queue_worker(settings: Settings) -> None:
    component = "train_queue_worker"
    if not settings.core.mysql_dsn:
        logger.warning("Train queue worker skipped: mysql_dsn is not configured")
        mark_component_state(component, ready=False, status="skipped")
        return

    from app.ops.train_worker import run_loop

    _mark_worker_running(component)
    logger.info("Train queue worker begins")
    try:
        run_loop(interval_seconds=3.0)
    except Exception as exc:
        logger.exception("Train queue worker crashed")
        mark_component_error(component, exc)


def _rag_rebuild_queue_worker(settings: Settings) -> None:
    component = "rag_rebuild_worker"
    if not settings.core.mysql_dsn:
        logger.warning("RAG rebuild worker skipped: mysql_dsn is not configured")
        mark_component_state(component, ready=False, status="skipped")
        return

    from app.ops.rag_embedding_worker import run_loop

    _mark_worker_running(component)
    logger.info("RAG rebuild worker begins")
    try:
        run_loop(interval_seconds=3.0)
    except Exception as exc:
        logger.exception("RAG rebuild worker crashed")
        mark_component_error(component, exc)


def _start_worker(name: str, target: Callable[[Settings], None], settings: Settings) -> None:
    thread = threading.Thread(target=target, args=(settings,), name=name, daemon=True)
    thread.start()
    _worker_threads[name] = thread


def start_startup_jobs(settings: Settings) -> None:

    try:
        set_settings(settings)
        run_startup_warmup(settings)
        _start_worker("reco-two-tower-refresh-worker", _two_tower_refresh_worker, settings)
        _start_worker("reco-cache-precompute-worker", _cache_precompute_worker, settings)
        _start_worker("reco-train-queue-worker", _train_queue_worker, settings)
        _start_worker("reco-rag-rebuild-worker", _rag_rebuild_queue_worker, settings)
    except Exception:
        raise