from __future__ import annotations

import logging
from uuid import uuid4

from flask import Flask, request

from app.api.v1 import v1_bp
from app.common.errors import register_error_handlers
from app.common.logging_setup import configure_logging, get_request_id, set_request_id
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

    @app.before_request
    def _request_context_setup():
        request_id = str(request.headers.get("X-Request-ID") or "").strip() or uuid4().hex
        set_request_id(request_id)

    @app.after_request
    def _attach_request_id(response):
        response.headers["X-Request-ID"] = get_request_id()
        return response

    @app.before_request
    def _internal_auth_guard():
        # 内部网络可关闭；如需启用，设置 INTERNAL_SECRET。
        # 文档提到: X-Internal-Secret
        if not settings.core.internal_secret:
            return None

        if request.headers.get("X-Internal-Secret") != settings.core.internal_secret:
            from app.common.responses import fail

            logger.warning("内部鉴权失败，path=%s", request.path)
            return fail(code=401, message="unauthorized", data=None), 401

        return None

    return app

