from __future__ import annotations

import logging
import threading

from app.common.runtime_health import mark_component_error, mark_component_state, mark_component_success
from app.common.settings import Settings
from app.reco.factory import build_pipeline
from app.reco.pipeline import RecommendationPipeline


logger = logging.getLogger(__name__)
_lock = threading.RLock()
_global_settings: Settings | None = None
_global_pipeline: RecommendationPipeline | None = None


def get_settings() -> Settings:
    """Return the global Settings singleton."""

    global _global_settings
    with _lock:
        if _global_settings is None:
            _global_settings = Settings.from_config()
            logger.info("Global settings initialized")
        return _global_settings


def reload_settings() -> Settings:
    """Reload settings from config and reset dependent singletons."""

    global _global_settings, _global_pipeline
    with _lock:
        _global_settings = Settings.from_config()
        _global_pipeline = None
        logger.info("Global settings reloaded, pipeline cache cleared")
        return _global_settings


def get_pipeline() -> RecommendationPipeline:
    """Return the global RecommendationPipeline singleton."""

    global _global_pipeline
    with _lock:
        if _global_pipeline is None:
            logger.info("Initializing global Recommendation Pipeline...")
            try:
                _global_pipeline = build_pipeline(get_settings())
                mark_component_success("pipeline")
            except Exception as exc:
                logger.exception("Global Recommendation Pipeline initialization failed")
                mark_component_error("pipeline", exc, details={"stage": "build_pipeline"})
                raise
        return _global_pipeline


def reset_pipeline(reason: str | None = None) -> None:
    """Clear pipeline singleton so next request rebuilds it with latest models."""

    global _global_pipeline
    with _lock:
        _global_pipeline = None
        mark_component_state("pipeline", ready=False, status="pending", details={"reason": reason or "manual_reset"})
        if reason:
            logger.info("Global Recommendation Pipeline reset, reason=%s", reason)
        else:
            logger.info("Global Recommendation Pipeline reset")


def rebuild_pipeline(reason: str | None = None) -> RecommendationPipeline:
    """Force rebuild pipeline singleton immediately."""

    reset_pipeline(reason=reason)
    return get_pipeline()
