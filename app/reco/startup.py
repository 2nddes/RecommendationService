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
from app.reco.ranking.xgb_ranker import load_latest_local_model as load_latest_xgb_local_model
from app.reco.ranking.stub_rankers import warmup_collaborative_filtering_model


logger = logging.getLogger(__name__)
_started = False
_start_lock = threading.RLock()
_worker_thread: threading.Thread | None = None


def _safe_sleep(seconds: float) -> None:
    if seconds <= 0:
        return
    time.sleep(float(seconds))


def _run_two_tower_full_build(settings: Settings) -> dict[str, Any]:
    logger.info("开始执行双塔全量构建任务")
    cfg = load_config_from_settings(settings)

    # 启动时先加载本地最新权重（active path 或 artifacts 目录最新版本）
    loaded = load_latest_local_model(settings)

    count = build_hnsw_index(
        index_path=cfg.index_path,
        cfg=cfg,
        mysql_dsn=settings.mysql_dsn,
    )
    logger.info("双塔全量构建完成，索引条数=%s, index_path=%s", int(count), cfg.index_path)
    return {
        "items_indexed": int(count),
        "vector_db": cfg.vector_db_path,
        "index_path": cfg.index_path,
        "model_path": loaded,
    }


def _startup_worker(settings: Settings) -> None:
    logger.info("启动后台工作线程")
    # 1) 启动阶段重操作：双塔全量物品向量推理 + 向量库更新 + 索引构建
    if bool(settings.two_tower_startup_build):
        try:
            _run_two_tower_full_build(settings)
        except Exception:
            logger.exception("启动阶段双塔全量构建失败")

    # 2) 启动阶段重操作：CF 模型构建
    if bool(settings.startup_prewarm_cf):
        try:
            warmup_collaborative_filtering_model(settings.mysql_dsn)
            logger.info("启动阶段 CF 模型预热完成")
        except Exception:
            logger.exception("启动阶段 CF 模型预热失败")

    # 3) 常驻日更：离线每日更新物品向量库
    interval = max(float(settings.two_tower_daily_update_interval_hours), 1.0) * 3600.0
    logger.info("进入双塔日更循环，间隔秒数=%s", int(interval))
    while True:
        _safe_sleep(interval)
        try:
            _run_two_tower_full_build(settings)
        except Exception:
            logger.exception("双塔日更任务执行失败，等待下一个周期重试")
            continue


def start_startup_jobs(settings: Settings) -> None:
    """启动推荐系统后台任务（仅启动一次）。"""

    global _started, _worker_thread
    with _start_lock:
        if _started:
            logger.info("后台启动任务已初始化，跳过重复启动")
            return

        if str(settings.ranking_method or "").lower() == "xgb":
            try:
                load_latest_xgb_local_model(settings)
                logger.info("启动阶段已尝试加载 XGB 最新模型")
            except Exception:
                logger.exception("启动阶段加载 XGB 最新模型失败")

        _started = True
        _worker_thread = threading.Thread(
            target=_startup_worker,
            args=(settings,),
            name="reco-startup-worker",
            daemon=True,
        )
        _worker_thread.start()
        logger.info("后台工作线程已启动，thread_name=%s", _worker_thread.name)
