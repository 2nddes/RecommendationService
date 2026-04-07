from __future__ import annotations

import logging

from flask import Blueprint, request

from app.common.responses import ok
from app.common.validation import ParamError, as_int
from app.reco.online.runtime import get_settings
from app.services.recommendation_service import build_recommendation_service

recommend_bp = Blueprint("recommend", __name__)
logger = logging.getLogger(__name__)


@recommend_bp.get("/recommend/user")
def recommend_user():
    """个性化推荐（猜你喜欢）

    文档: GET /api/v1/recommend/user
    query params:
      - user_id: int (required)
      - n: int (optional, default 10)
    """

    user_id_raw = request.args.get("user_id")
    if user_id_raw is None:
        raise ParamError("missing required query parameter: user_id")

    user_id = as_int(user_id_raw, name="user_id")
    n = as_int(request.args.get("n", 10), name="n")
    logger.info("收到个性化推荐请求，user_id=%s, n=%s", user_id, n)
    settings = get_settings()
    service = build_recommendation_service(settings)
    data = service.recommend_user(user_id=user_id, n=n)
    return ok(data)


@recommend_bp.get("/recommend/item")
def recommend_item():
    """相似影片推荐（看了又看）

    文档: GET /api/v1/recommend/item
    query params:
      - movie_id: int (required)
      - n: int (optional, default 8)
    """

    movie_id_raw = request.args.get("movie_id")
    if movie_id_raw is None:
        raise ParamError("missing required query parameter: movie_id")

    movie_id = as_int(movie_id_raw, name="movie_id")
    n = as_int(request.args.get("n", 8), name="n")
    logger.info("收到相似影片推荐请求，movie_id=%s, n=%s", movie_id, n)

    settings = get_settings()
    service = build_recommendation_service(settings)

    data = service.recommend_item(movie_id=movie_id, n=n)
    logger.info("相似影片推荐完成，movie_id=%s, 返回条数=%s", movie_id, len(data.get("items") or []))
    return ok(data)


@recommend_bp.get("/recommend/trending")
def recommend_trending():
    """趋势推荐（热门榜单）

    文档: GET /api/v1/recommend/trending
    query params:
      - window: str (optional, default weekly)
      - n: int (optional, default 10)
    """

    window = request.args.get("window", "weekly")
    n = as_int(request.args.get("n", 10), name="n")

    if window not in {"daily", "weekly", "monthly", "half_year", "one_year", "all_time"}:
        raise ParamError("invalid window")

    settings = get_settings()
    service = build_recommendation_service(settings)
    data = service.recommend_trending(window=window, n=n)
    logger.info("趋势推荐完成，window=%s, n=%s, 返回条数=%s", window, n, len(data.get("items") or []))
    return ok(data)

