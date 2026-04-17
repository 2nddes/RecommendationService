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


def set_settings(settings: Settings) -> Settings:
    global _global_settings
    with _lock:
        _global_settings = settings
        return _global_settings


def get_settings() -> Settings:
    global _global_settings
    with _lock:
        if _global_settings is None:
            _global_settings = Settings.from_config()
            logger.info("Global settings initialized")
        return _global_settings


def initialize_pipeline(settings: Settings) -> RecommendationPipeline:
    global _global_settings, _global_pipeline

    try:
        pipeline = build_pipeline(settings)
    except Exception as exc:
        logger.exception("Global Recommendation Pipeline initialization failed")
        mark_component_error("pipeline", exc, details={"stage": "build_pipeline"})
        raise

    with _lock:
        _global_settings = settings
        _global_pipeline = pipeline

    mark_component_success("pipeline")
    return pipeline


def rebuild_pipeline(*, settings: Settings | None = None) -> RecommendationPipeline:
    return initialize_pipeline(settings or get_settings())


def get_pipeline() -> RecommendationPipeline:
    with _lock:
        if _global_pipeline is None:
            raise RuntimeError("pipeline_not_initialized")
        return _global_pipeline
