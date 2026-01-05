from __future__ import annotations

from flask import Flask

from app.common.responses import fail
from app.common.validation import ParamError


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ParamError)
    def _handle_param_error(err: ParamError):
        return fail(400, err.message, None), 400

    @app.errorhandler(Exception)
    def _handle_unexpected(err: Exception):
        # 这里不打印/不吞日志策略留给运维配置；先返回统一结构
        return fail(500, "internal error", None), 500
