from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.common.settings import Settings
from app.reco.recall.two_tower import (
    build_hnsw_index,
    load_config_from_settings,
    load_latest_local_model,
)


logger = logging.getLogger(__name__)
_started = False
_start_lock = threading.RLock()
_worker_thread: threading.Thread | None = None


def _safe_sleep(seconds: float) -> None:
    if seconds <= 0:
        return
    time.sleep(float(seconds))


def _run_two_tower_full_build(settings: Settings) -> dict[str, Any]:
    logger.info("Starting two-tower full rebuild")
    cfg = load_config_from_settings(settings)
    loaded = load_latest_local_model(settings)

    count = build_hnsw_index(
        index_path=cfg.index_path,
        cfg=cfg,
        mysql_dsn=settings.mysql_dsn,
    )
    logger.info("Two-tower rebuild finished, items=%s, index_path=%s", int(count), cfg.index_path)
    return {
        "items_indexed": int(count),
        "vector_db": cfg.vector_db_path,
        "index_path": cfg.index_path,
        "model_path": loaded,
    }


def _startup_worker(settings: Settings) -> None:
    logger.info("Startup worker begins")
    if bool(settings.two_tower_startup_build):
        try:
            _run_two_tower_full_build(settings)
        except Exception:
            logger.exception("Initial two-tower rebuild failed")

    interval = float(settings.two_tower_daily_update_interval_hours) * 3600.0
    logger.info("Entering two-tower refresh loop, interval_seconds=%s", int(interval))
    while True:
        _safe_sleep(interval)
        try:
            _run_two_tower_full_build(settings)
        except Exception:
            logger.exception("Scheduled two-tower rebuild failed")


def start_startup_jobs(settings: Settings) -> None:
    global _started, _worker_thread
    with _start_lock:
        if _started:
            logger.info("Startup jobs already initialized, skipping duplicate launch")
            return

        _started = True
        _worker_thread = threading.Thread(
            target=_startup_worker,
            args=(settings,),
            name="reco-startup-worker",
            daemon=True,
        )
        _worker_thread.start()
        logger.info("Startup worker launched, thread_name=%s", _worker_thread.name)
