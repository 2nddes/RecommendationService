from __future__ import annotations

import re

from flask import Flask
from werkzeug.exceptions import HTTPException

from app.common.responses import fail
from app.common.validation import ParamError


def _humanize_message(message: str | None, *, fallback: str) -> str:
    raw = (message or "").strip()
    if not raw:
        return fallback
    if re.fullmatch(r"[a-z0-9_]+", raw):
        return raw.replace("_", " ")
    return raw


def _extract_exception_message(err: Exception, *, fallback: str) -> str:
    text = ""
    if isinstance(err, KeyError) and err.args:
        text = str(err.args[0])
    else:
        text = str(err)
    return _humanize_message(text, fallback=fallback)


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ParamError)
    def _handle_param_error(err: ParamError):
        return fail(
            code=400,
            message=_humanize_message(err.message, fallback="invalid request parameters"),
            data=None,
            http_status=400,
        )

    @app.errorhandler(HTTPException)
    def _handle_http_exception(err: HTTPException):
        message = _humanize_message(err.description, fallback=err.name or "http error")
        return fail(code=err.code or 500, message=message, data=None, http_status=err.code or 500)

    @app.errorhandler(ValueError)
    @app.errorhandler(KeyError)
    def _handle_bad_request_family(err: Exception):
        return fail(
            code=400,
            message=_extract_exception_message(err, fallback="invalid request"),
            data=None,
            http_status=400,
        )

    @app.errorhandler(RuntimeError)
    def _handle_runtime_error(err: RuntimeError):
        return fail(
            code=500,
            message=_extract_exception_message(err, fallback="service execution failed"),
            data=None,
            http_status=500,
        )

    @app.errorhandler(Exception)
    def _handle_unexpected(err: Exception):
        app.logger.exception("unhandled exception: %s", err)
        return fail(code=500, message="internal server error", data=None, http_status=500)
