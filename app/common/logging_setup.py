from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.common import config


_configured = False


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def configure_logging() -> Path:
    """配置全局日志，输出到专用文件（按大小轮转）。"""

    global _configured

    if _configured:
        handler = _find_file_handler(logging.getLogger())
        if handler is not None:
            return Path(handler.baseFilename)

    log_file_raw = config.get_str("LOG_FILE_PATH", "data/logs/recommendation_service.log")
    log_file = Path(log_file_raw or "data/logs/recommendation_service.log")
    if not log_file.is_absolute():
        log_file = _repo_root() / log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)

    level_name = (config.get_str("LOG_LEVEL", "INFO") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    file_handler = _find_file_handler(root_logger)
    if file_handler is None:
        file_handler = RotatingFileHandler(
            filename=str(log_file),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        root_logger.addHandler(file_handler)

    _configured = True
    logging.getLogger(__name__).info("日志系统初始化完成，日志文件: %s", str(log_file))
    return log_file


def _find_file_handler(logger: logging.Logger) -> RotatingFileHandler | None:
    for h in logger.handlers:
        if isinstance(h, RotatingFileHandler):
            return h
    return None
