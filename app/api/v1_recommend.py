from __future__ import annotations

from datetime import datetime, timedelta
import logging

from flask import Blueprint, request
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.common.responses import ok, fail
from app.common.validation import as_int
from app.common.settings import Settings
from app.reco.factory import build_pipeline
from app.reco.recall.two_tower import ann_search, build_item_vector, load_config_from_settings
from app.reco.types import RequestContext

recommend_bp = Blueprint("recommend", __name__)
logger = logging.getLogger(__name__)

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
    logger.warning("趋势推荐查询失败：MySQL 引擎不可用")
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
    logger.exception("趋势推荐查询异常，window=%s, n=%s", window, n)
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
    logger.info("收到个性化推荐请求，user_id=%s, n=%s", user_id, n)

    # 召回/排序/重排：由 pipeline 统一编排。
    settings = Settings.from_config()
    pipeline = build_pipeline(settings)
    ctx = RequestContext(user_id=user_id, n=n)
    items = pipeline.recommend(ctx)
    logger.info("个性化推荐完成，user_id=%s, 返回条数=%s", user_id, len(items))

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
    logger.info("收到相似影片推荐请求，movie_id=%s, n=%s", movie_id, n)

    settings = Settings.from_config()

    cfg = load_config_from_settings(settings)
    item_vec = build_item_vector(movie_id, cfg, None, mysql_dsn=settings.mysql_dsn)
    if item_vec is None:
      logger.warning("相似影片推荐失败：未找到物品向量，movie_id=%s", movie_id)
      return fail(message="Item vector not found for movie_id: {}".format(movie_id))

    pairs = ann_search(item_vec, k=max(n + 1, n), cfg=cfg)
    items: list[int] = []
    for item_id, _score in pairs:
      iid = int(item_id)
      if iid == movie_id:
        continue
      items.append(iid)
      if len(items) >= n:
        break

    data = {"source_id": movie_id, "items": items, "n": n}
    logger.info("相似影片推荐完成，movie_id=%s, 返回条数=%s", movie_id, len(items))
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
      logger.warning("趋势推荐请求参数非法，window=%s", window)
      return fail(message="invalid 'window'")

    settings = Settings.from_config()
    items = _fetch_trending_items(settings.mysql_dsn, window=window, n=n)
    logger.info("趋势推荐完成，window=%s, n=%s, 返回条数=%s", window, n, len(items))

    data = {
        "window": window,
        "items": items,
        "n": n,
    }
    return ok(data)
