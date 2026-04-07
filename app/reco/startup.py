from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.common.runtime_health import mark_component_error, mark_component_state, mark_component_success
from app.common.settings import Settings
from app.ops.cache_ops import run_all_cache_precompute
from app.reco.online.runtime import get_pipeline, get_settings
from app.reco.recall.two_tower import (
    build_hnsw_index,
    load_latest_local_model,
)


logger = logging.getLogger(__name__)
_started = False
_start_lock = threading.RLock()
_worker_thread: threading.Thread | None = None
_cache_worker_thread: threading.Thread | None = None
_train_worker_thread: threading.Thread | None = None

def _run_two_tower_full_build(settings: Settings) -> dict[str, Any]:
    logger.info("Starting two-tower full rebuild")
    cfg = settings.two_tower
    loaded = load_latest_local_model(settings)

    count = build_hnsw_index(
        index_path=cfg.index_path,
        cfg=cfg,
        mysql_dsn=settings.core.mysql_dsn,
    )
    logger.info("Two-tower rebuild finished, items=%s, index_path=%s", int(count), cfg.index_path)
    return {
        "items_indexed": int(count),
        "vector_db": cfg.vector_db_path,
        "index_path": cfg.index_path,
        "model_path": loaded,
    }


def _run_startup_once(settings: Settings) -> None:
    logger.info("Startup init tasks begin")
    mark_component_state("warmup", ready=False, status="running")
    try:
        if settings.two_tower.startup_build:
            _run_two_tower_full_build(settings)

        get_pipeline()
        logger.info("Global recommendation pipeline preloaded")

        summary = run_all_cache_precompute(settings)
        logger.info("Startup cache precompute finished, summary=%s", summary)

        mark_component_success("warmup", details={"mode": "async"})
    except Exception as exc:
        logger.exception("Startup init tasks failed")
        mark_component_error("warmup", exc, details={"mode": "async"})


def _startup_worker(settings: Settings) -> None:
    logger.info("Startup refresh worker begins")
    interval = settings.two_tower.daily_update_interval_hours * 3600.0
    logger.info("Entering two-tower refresh loop, interval_seconds=%s", int(interval))
    while True:
        time.sleep(interval)
        try:
            _run_two_tower_full_build(settings)
            mark_component_success("pipeline", details={"worker": "refresh"})
        except Exception as exc:
            logger.exception("Two-tower refresh iteration failed")
            mark_component_error("pipeline", exc, details={"worker": "refresh"})


def _cache_precompute_worker(settings: Settings) -> None:
    interval = max(float(settings.cache.trending_refresh_interval_seconds), 60.0)
    logger.info("Entering cache precompute loop, interval_seconds=%s", int(interval))
    while True:
        time.sleep(interval)
        try:
            summary = run_all_cache_precompute(settings)
            logger.info("Scheduled cache precompute finished, summary=%s", summary)
            mark_component_success("cache_precompute", details={"worker": "scheduled"})
        except Exception as exc:
            logger.exception("Cache precompute iteration failed")
            mark_component_error("cache_precompute", exc, details={"worker": "scheduled"})


def _train_queue_worker(settings: Settings) -> None:
    # Keep this in-process for dev convenience so DB pending jobs are consumed automatically.
    if not settings.core.mysql_dsn:
        logger.warning("Train queue worker skipped: mysql_dsn is not configured")
        return

    from app.ops.train_worker import run_loop

    logger.info("Train queue worker begins")
    try:
        run_loop(interval_seconds=3.0)
    except Exception:
        logger.exception("Train queue worker crashed")


def start_startup_jobs(settings: Settings) -> None:
    global _started, _worker_thread, _cache_worker_thread, _train_worker_thread
    with _start_lock:
        if _started:
            logger.info("Startup jobs already initialized, skipping duplicate launch")
            return

        settings = get_settings()
        _started = True
        mark_component_state("warmup", ready=False, status="pending", details={"mode": "async"})

        # Startup initialization is asynchronous: service can start and expose readiness state.
        init_thread = threading.Thread(
            target=_run_startup_once,
            args=(settings,),
            name="reco-startup-init",
            daemon=True,
        )
        init_thread.start()
        logger.info("Startup init async warmup started, thread_name=%s", init_thread.name)

        _worker_thread = threading.Thread(
            target=_startup_worker,
            args=(settings,),
            name="reco-startup-refresh-worker",
            daemon=True,
        )
        _worker_thread.start()
        logger.info("Startup worker launched, thread_name=%s", _worker_thread.name)

        _cache_worker_thread = threading.Thread(
            target=_cache_precompute_worker,
            args=(settings,),
            name="reco-cache-precompute-worker",
            daemon=True,
        )
        _cache_worker_thread.start()
        logger.info("Cache precompute worker launched, thread_name=%s", _cache_worker_thread.name)

        _train_worker_thread = threading.Thread(
            target=_train_queue_worker,
            args=(settings,),
            name="reco-train-queue-worker",
            daemon=True,
        )
        _train_worker_thread.start()
        logger.info("Train queue worker launched, thread_name=%s", _train_worker_thread.name)

