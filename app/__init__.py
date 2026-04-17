from __future__ import annotations

import logging
from uuid import uuid4

from flask import Flask, request

from app.api.v1 import v1_bp
from app.common.errors import register_error_handlers
from app.common.logging_setup import configure_logging
from app.common.settings import Settings
from app.reco.startup import start_startup_jobs


logger = logging.getLogger(__name__)


def create_app(settings: Settings) -> Flask:
    log_file = configure_logging()

    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    logger.info("应用初始化开始，日志文件=%s", str(log_file))

    app.register_blueprint(v1_bp, url_prefix="/api/v1")

    register_error_handlers(app)
    start_startup_jobs(settings)
    logger.info("应用初始化完成，接口前缀=/api/v1")
    
    return app

