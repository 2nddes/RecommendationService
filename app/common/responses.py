from __future__ import annotations

from typing import Any

from flask import jsonify


def ok(data: Any, message: str = "success", code: int = 200):
    return jsonify({"code": code, "message": message, "data": data})


def fail(
    code: int = 400,
    message: str = "bad request",
    data: Any = None,
    *,
    http_status: int | None = None,
):
    response = jsonify({"code": code, "message": message, "data": data})
    response.status_code = int(http_status if http_status is not None else code)
    return response
