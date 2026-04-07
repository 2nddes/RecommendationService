from __future__ import annotations

from flask import Flask
from werkzeug.exceptions import HTTPException

from app.common.responses import fail
from app.common.validation import ParamError


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ParamError)
    def _handle_param_error(err: ParamError):
        app.logger.warning("param error: %s", err.message)
        return fail(
            code=400,
            message="invalid request parameters",
            data=None,
            http_status=400,
        )

    @app.errorhandler(HTTPException)
    def _handle_http_exception(err: HTTPException):
        status = int(err.code or 500)
        app.logger.warning(
            "http exception: code=%s, name=%s, description=%s",
            status,
            err.name,
            err.description,
        )
        message = "invalid request" if status < 500 else "internal server error"
        return fail(code=status, message=message, data=None, http_status=status)

    @app.errorhandler(ValueError)
    @app.errorhandler(KeyError)
    def _handle_bad_request_family(err: Exception):
        app.logger.warning("bad request exception: %s", err, exc_info=True)
        return fail(
            code=400,
            message="invalid request",
            data=None,
            http_status=400,
        )

    @app.errorhandler(RuntimeError)
    def _handle_runtime_error(err: RuntimeError):
        app.logger.exception("runtime exception: %s", err)
        return fail(
            code=500,
            message="service execution failed",
            data=None,
            http_status=500,
        )

    @app.errorhandler(Exception)
    def _handle_unexpected(err: Exception):
        app.logger.exception("unhandled exception: %s", err)
        return fail(code=500, message="internal server error", data=None, http_status=500)
