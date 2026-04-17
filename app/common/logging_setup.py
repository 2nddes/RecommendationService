from __future__ import annotations

from contextvars import ContextVar
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.common.settings import Settings


_configured = False
_ansi_escape_re = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class _SanitizingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return _ansi_escape_re.sub("", rendered)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _build_formatter() -> logging.Formatter:
    return _SanitizingFormatter(
        "%(asctime)s | %(levelname)s | pid=%(process)d | thread=%(threadName)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def configure_logging() -> Path:
    """Initialize project-wide logging to rotating file handlers only."""

    global _configured

    root_logger = logging.getLogger()
    if _configured:
        _remove_console_handlers(root_logger)
        handler = _find_file_handler(root_logger)
        if handler is not None:
            handler.setFormatter(_build_formatter())
            return Path(handler.baseFilename)

    settings = Settings.from_config()

    log_file = Path(settings.log.file_path)
    if not log_file.is_absolute():
        log_file = _repo_root() / log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)

    level_name = settings.log.level.upper()
    level = getattr(logging, level_name, logging.INFO)

    root_logger.setLevel(level)
    _remove_console_handlers(root_logger)

    file_handler = _find_file_handler(root_logger)
    if file_handler is None:
        file_handler = RotatingFileHandler(
            filename=str(log_file),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        root_logger.addHandler(file_handler)

    file_handler.setLevel(level)
    file_handler.setFormatter(_build_formatter())

    _configured = True
    logging.getLogger(__name__).info("Logging initialized. log_file=%s", str(log_file))
    return log_file


def _find_file_handler(logger: logging.Logger) -> RotatingFileHandler | None:
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            return handler
    return None


def _remove_console_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
