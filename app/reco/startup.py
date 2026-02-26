from __future__ import annotations

import threading
import time
from typing import Any

from app.common.settings import Settings
from app.reco.recall.two_tower import (
    build_hnsw_index,
    load_config_from_settings,
    load_latest_local_model,
)
from app.reco.ranking.xgb_ranker import load_latest_local_model as load_latest_xgb_local_model
from app.reco.ranking.stub_rankers import warmup_collaborative_filtering_model


_started = False
_start_lock = threading.RLock()
_worker_thread: threading.Thread | None = None


def _safe_sleep(seconds: float) -> None:
    if seconds <= 0:
        return
    time.sleep(float(seconds))


def _run_two_tower_full_build(settings: Settings) -> dict[str, Any]:
    cfg = load_config_from_settings(settings)

    # 启动时先加载本地最新权重（active path 或 artifacts 目录最新版本）
    loaded = load_latest_local_model(settings)

    count = build_hnsw_index(
        index_path=cfg.index_path,
        cfg=cfg,
        mysql_dsn=settings.mysql_dsn,
    )
    return {
        "items_indexed": int(count),
        "vector_db": cfg.vector_db_path,
        "index_path": cfg.index_path,
        "model_path": loaded,
    }


def _startup_worker(settings: Settings) -> None:
    # 1) 启动阶段重操作：双塔全量物品向量推理 + 向量库更新 + 索引构建
    if bool(settings.two_tower_startup_build):
        try:
            _run_two_tower_full_build(settings)
        except Exception:
            pass

    # 2) 启动阶段重操作：CF 模型构建
    if bool(settings.startup_prewarm_cf):
        try:
            warmup_collaborative_filtering_model(settings.mysql_dsn)
        except Exception:
            pass

    # 3) 常驻日更：离线每日更新物品向量库
    interval = max(float(settings.two_tower_daily_update_interval_hours), 1.0) * 3600.0
    while True:
        _safe_sleep(interval)
        try:
            _run_two_tower_full_build(settings)
        except Exception:
            continue


def start_startup_jobs(settings: Settings) -> None:
    """启动推荐系统后台任务（仅启动一次）。"""

    global _started, _worker_thread
    with _start_lock:
        if _started:
            return

        if str(settings.ranking_method or "").lower() == "xgb":
            try:
                load_latest_xgb_local_model(settings)
            except Exception:
                pass

        _started = True
        _worker_thread = threading.Thread(
            target=_startup_worker,
            args=(settings,),
            name="reco-startup-worker",
            daemon=True,
        )
        _worker_thread.start()
