from __future__ import annotations

from flask import Blueprint, request

from app.common.responses import ok
from app.common.validation import ParamError, as_int
from app.common.settings import Settings
from app.reco.factory import build_pipeline
from app.reco.types import RequestContext

recommend_bp = Blueprint("recommend", __name__)


@recommend_bp.get("/recommend/user")
def recommend_user():
    """个性化推荐（猜你喜欢）

    文档: GET /api/v1/recommend/user
    query params:
      - user_id: int (required)
      - n: int (optional, default 10)
      - strategy: str (optional, default hybrid)
    """

    user_id_raw = request.args.get("user_id")
    if user_id_raw is None:
        raise ParamError("missing 'user_id'")

    user_id = as_int(user_id_raw, name="user_id")
    n = as_int(request.args.get("n", 10), name="n")
    strategy = request.args.get("strategy", "hybrid")

    # 召回/排序/重排：由 pipeline 统一编排。
    settings = Settings.from_config()
    pipeline = build_pipeline(settings)
    ctx = RequestContext(user_id=user_id, n=n, strategy=strategy)
    items = pipeline.recommend(ctx)

    data = {"user_id": user_id, "strategy": strategy, "items": items, "n": n}
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
        raise ParamError("missing 'movie_id'")

    movie_id = as_int(movie_id_raw, name="movie_id")
    n = as_int(request.args.get("n", 8), name="n")

    # 这里复用 pipeline：只要 recall channels 支持 ctx.movie_id 即可。
    # 默认 Settings 走 user_* 通道时可能为空；需要时可配置 RECALL_CHANNELS=item_similar_by_tags,...
    settings = Settings.from_config()
    pipeline = build_pipeline(settings)
    ctx = RequestContext(movie_id=movie_id, n=n, strategy="item")
    items = pipeline.recommend(ctx)

    data = {"source_id": movie_id, "items": items, "n": n}
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

    data = {
        "window": window,
        "items": [],
        "n": n,
    }
    return ok(data)
