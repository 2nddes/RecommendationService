from __future__ import annotations

import logging

from flask import Blueprint, request

from app.common.responses import ok
from app.common.validation import ParamError, as_int
from app.reco.online.runtime import get_settings
from app.services.recommendation_service import build_recommendation_service

recommend_bp = Blueprint("recommend", __name__)
logger = logging.getLogger(__name__)


def _resolve_user_reco_delivery_mode(settings) -> str:
    raw_mode = str(settings.cache.user_reco_delivery_mode or "paged").strip().lower()
    if raw_mode in {"paged", "pop"}:
        return raw_mode
    raise RuntimeError(f"invalid_user_reco_delivery_mode: {raw_mode}")


@recommend_bp.get("/recommend/user")
def recommend_user():
    """个性化推荐（猜你喜欢）

    文档: GET /api/v1/recommend/user
    query params:
      - user_id: int (required)
            - page/page_size: only for paged mode
            - n: only for pop mode
    """

    user_id_raw = request.args.get("user_id")
    if user_id_raw is None:
        raise ParamError("missing required query parameter: user_id")

    user_id = as_int(user_id_raw, name="user_id")
    settings = get_settings()
    mode = _resolve_user_reco_delivery_mode(settings)

    if mode == "pop":
        if request.args.get("page") is not None or request.args.get("page_size") is not None:
            raise ParamError("invalid request for pop mode, use 'n' only")

        n = as_int(request.args.get("n", 20), name="n")
        if n < 1 or n > 100:
            raise ParamError("invalid 'n', expected integer in [1, 100]")

        page = 1
        page_size = n
        logger.debug("收到个性化推荐请求，user_id=%s, mode=%s, n=%s", user_id, mode, n)
    else:
        if request.args.get("n") is not None:
            raise ParamError("invalid request for paged mode, use 'page' and 'page_size' only")

        page = as_int(request.args.get("page", 1), name="page")
        page_size = as_int(request.args.get("page_size", 20), name="page_size")

        if page < 1:
            raise ParamError("invalid 'page', expected integer >= 1")
        if page_size < 1 or page_size > 100:
            raise ParamError("invalid 'page_size', expected integer in [1, 100]")

        logger.debug("收到个性化推荐请求，user_id=%s, mode=%s, page=%s, page_size=%s", user_id, mode, page, page_size)

    service = build_recommendation_service(settings)
    data = service.recommend_user(user_id=user_id, page=page, page_size=page_size)
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
    logger.debug("收到相似影片推荐请求，movie_id=%s, n=%s", movie_id, n)

    settings = get_settings()
    service = build_recommendation_service(settings)

    data = service.recommend_item(movie_id=movie_id, n=n)
    logger.debug("相似影片推荐完成，movie_id=%s, 返回条数=%s", movie_id, len(data.get("items") or []))
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
    logger.debug("趋势推荐完成，window=%s, n=%s, 返回条数=%s", window, n, len(data.get("items") or []))
    return ok(data)

