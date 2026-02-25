from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, request
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.common.responses import ok, fail
from app.common.validation import as_int
from app.common.settings import Settings
from app.reco.factory import build_pipeline
from app.reco.types import RequestContext

recommend_bp = Blueprint("recommend", __name__)

_engine_by_dsn: dict[str, Engine] = {}


def _get_engine(mysql_dsn: str | None) -> Engine | None:
  if not mysql_dsn:
    return None
  dsn = str(mysql_dsn).strip()
  if not dsn:
    return None
  cached = _engine_by_dsn.get(dsn)
  if cached is not None:
    return cached
  try:
    _engine_by_dsn[dsn] = create_engine(dsn, pool_pre_ping=True)
    return _engine_by_dsn[dsn]
  except Exception:
    return None


def _window_start(window: str) -> datetime | None:
  now = datetime.utcnow()
  if window == "daily":
    return now - timedelta(days=1)
  if window == "weekly":
    return now - timedelta(days=7)
  if window == "monthly":
    return now - timedelta(days=30)
  if window == "all_time":
    return None
  return now - timedelta(days=7)


def _fetch_trending_items(mysql_dsn: str | None, *, window: str, n: int) -> list[int]:
  engine = _get_engine(mysql_dsn)
  if engine is None:
    return []

  sql = """
  SELECT
    m.movie_id AS item_id,
    (
    0.55 * (COALESCE(m.rating_sum, 0) / NULLIF(m.rating_count, 0))
    + 0.20 * LOG10(COALESCE(m.rating_count, 0) + 1)
    + 0.25 * LOG10(COALESCE(ua.action_cnt, 0) + 1)
    ) AS score
  FROM movie m
  LEFT JOIN (
    SELECT movie_id, COUNT(*) AS action_cnt
    FROM user_action
    WHERE (:window_start IS NULL OR created_at >= :window_start)
    GROUP BY movie_id
  ) ua ON ua.movie_id = m.movie_id
  WHERE m.status = 'published'
  ORDER BY score DESC, COALESCE(ua.action_cnt, 0) DESC, m.rating_count DESC, m.movie_id DESC
  LIMIT :limit
  """

  try:
    with engine.connect() as conn:
      rows = conn.execute(
        text(sql),
        {
          "window_start": _window_start(window),
          "limit": max(int(n), 0),
        },
      )
      out: list[int] = []
      for row in rows:
        try:
          out.append(int(row._mapping["item_id"]))
        except Exception:
          continue
      return out
  except SQLAlchemyError:
    return []


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
        return fail(message="Missing required query parameter: user_id")

    user_id = as_int(user_id_raw, name="user_id")
    n = as_int(request.args.get("n", 10), name="n")

    # 召回/排序/重排：由 pipeline 统一编排。
    settings = Settings.from_config()
    pipeline = build_pipeline(settings)
    ctx = RequestContext(user_id=user_id, n=n)
    items = pipeline.recommend(ctx)

    data = {"user_id": user_id, "items": items, "n": n}
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
        return fail(message="Missing required query parameter: movie_id")

    movie_id = as_int(movie_id_raw, name="movie_id")
    n = as_int(request.args.get("n", 8), name="n")

    # 这里复用 pipeline：只要 recall channels 支持 ctx.movie_id 即可。
    # 默认 Settings 走 user_* 通道时可能为空；需要时可配置 RECALL_CHANNELS=item_similar_by_tags,...
    settings = Settings.from_config()
    pipeline = build_pipeline(settings)
    ctx = RequestContext(movie_id=movie_id, n=n)
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

    window = request.args.get("window")
    n = as_int(request.args.get("n", 10), name="n")

    if window not in {"daily", "weekly", "monthly", "all_time"}:
      return fail(message="invalid 'window'")

    settings = Settings.from_config()
    items = _fetch_trending_items(settings.mysql_dsn, window=window, n=n)

    data = {
        "window": window,
        "items": items,
        "n": n,
    }
    return ok(data)
