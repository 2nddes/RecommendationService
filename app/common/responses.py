from __future__ import annotations

from typing import Any

from flask import jsonify


def ok(data: Any, message: str = "success", code: int = 200):
    return jsonify({"code": code, "message": message, "data": data})


def fail(code: int, message: str, data: Any = None):
    return jsonify({"code": code, "message": message, "data": data})
