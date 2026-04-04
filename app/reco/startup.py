from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.common.settings import Settings
from app.ops.cache_ops import run_all_cache_precompute
from app.reco.runtime import get_pipeline, get_settings
from app.reco.recall.two_tower import (
    build_hnsw_index,
    load_latest_local_model,
)


logger = logging.getLogger(__name__)
_started = False
_start_lock = threading.RLock()
_worker_thread: threading.Thread | None = None
_cache_worker_thread: threading.Thread | None = None


def _safe_sleep(seconds: float) -> None:
    if seconds <= 0:
        return
    time.sleep(float(seconds))


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
    if settings.two_tower.startup_build:
        try:
            _run_two_tower_full_build(settings)
        except Exception:
            logger.exception("Initial two-tower rebuild failed")

    try:
        get_pipeline()
        logger.info("Global recommendation pipeline preloaded")
    except Exception:
        logger.exception("Global recommendation pipeline preload failed")

    try:
        summary = run_all_cache_precompute(settings)
        logger.info("Startup cache precompute finished, summary=%s", summary)
    except Exception:
        logger.exception("Startup cache precompute failed")


def _startup_worker(settings: Settings) -> None:
    logger.info("Startup refresh worker begins")
    interval = settings.two_tower.daily_update_interval_hours * 3600.0
    logger.info("Entering two-tower refresh loop, interval_seconds=%s", int(interval))
    while True:
        _safe_sleep(interval)
        try:
            _run_two_tower_full_build(settings)
        except Exception:
            logger.exception("Scheduled two-tower rebuild failed")


def _cache_precompute_worker(settings: Settings) -> None:
    interval = min(
        max(float(settings.cache.trending_refresh_interval_seconds), 60.0),
        max(float(settings.cache.static_recall_refresh_interval_seconds), 60.0),
    )
    logger.info("Entering cache precompute loop, interval_seconds=%s", int(interval))
    while True:
        _safe_sleep(interval)
        try:
            summary = run_all_cache_precompute(settings)
            logger.info("Scheduled cache precompute finished, summary=%s", summary)
        except Exception:
            logger.exception("Scheduled cache precompute failed")


def start_startup_jobs(settings: Settings) -> None:
    global _started, _worker_thread, _cache_worker_thread
    with _start_lock:
        if _started:
            logger.info("Startup jobs already initialized, skipping duplicate launch")
            return

        settings = get_settings()
        _started = True

        # Startup initialization tasks must complete before service continues.
        init_thread = threading.Thread(
            target=_run_startup_once,
            args=(settings,),
            name="reco-startup-init",
            daemon=False,
        )
        init_thread.start()
        init_thread.join()
        logger.info("Startup init tasks completed, thread_name=%s", init_thread.name)

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

