from __future__ import annotations

from flask import Flask

from app.api.v1 import v1_bp
from app.common.errors import register_error_handlers
from app.common.health import health_bp
from app.common.settings import Settings


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings.from_config()

    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False

    app.register_blueprint(health_bp)
    app.register_blueprint(v1_bp, url_prefix="/api/v1")

    register_error_handlers(app)

    @app.before_request
    def _internal_auth_guard():
        # 内部网络可关闭；如需启用，设置 INTERNAL_SECRET。
        # 文档提到: X-Internal-Secret
        from flask import request

        if not settings.internal_secret:
            return None

        if request.headers.get("X-Internal-Secret") != settings.internal_secret:
            from app.common.responses import fail

            return fail(code=401, message="unauthorized", data=None), 401

        return None

    return app
