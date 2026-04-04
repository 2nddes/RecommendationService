from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from app.common.redis_cache import (
    get_redis_client,
    store_item_similar,
    store_movie_features,
    store_trending_items,
    store_user_features,
    store_user_interest,
)
from app.common.settings import Settings
from app.reco.recall.two_tower.db import get_engine


logger = logging.getLogger(__name__)


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


def _query_rows(settings: Settings, sql: str, params: dict) -> list[dict]:
    engine = get_engine(settings.core.mysql_dsn)
    if engine is None:
        return []
    try:
        with engine.connect() as conn:
            rs = conn.execute(text(sql), params)
            return [dict(row._mapping) for row in rs]
    except Exception:
        logger.exception("cache precompute SQL failed")
        return []


def refresh_feature_cache(settings: Settings) -> dict[str, int]:
    if get_redis_client(settings) is None:
        return {"movie_features": 0, "user_features": 0}

    movie_sql = """
    SELECT
      m.movie_id,
      (COALESCE(m.rating_sum, 0) / NULLIF(m.rating_count, 0)) AS rating_avg,
      COALESCE(m.year, 0) AS year,
    COALESCE(m.duration_min, 0) AS duration_min,
      GROUP_CONCAT(mt.tag_id ORDER BY mt.weight DESC, mt.hot_score DESC SEPARATOR ',') AS tag_ids
    FROM movie m
    LEFT JOIN movie_tag mt ON mt.movie_id = m.movie_id
    WHERE m.movie_id IS NOT NULL
    GROUP BY m.movie_id
    """
    movie_rows = _query_rows(settings, movie_sql, {})

    movie_payload = []
    for row in movie_rows:
        try:
            movie_id = int(row.get("movie_id") or 0)
        except Exception:
            continue
        if movie_id <= 0:
            continue
        tags_raw = str(row.get("tag_ids") or "").strip()
        tags = []
        if tags_raw:
            for x in tags_raw.split(","):
                try:
                    tags.append(int(x))
                except Exception:
                    continue
        movie_payload.append(
            {
                "movie_id": movie_id,
                "rating_avg": float(row.get("rating_avg") or 0.0),
                "year": int(row.get("year") or 0),
                "duration_min": int(row.get("duration_min") or 0),
                "tags": tags,
            }
        )

    user_sql = """
    SELECT
      u.user_id,
      u.gender,
      u.birth,
      u.created_at,
      GROUP_CONCAT(uct.tag_id ORDER BY uct.tag_id SEPARATOR ',') AS interest_tag_ids
    FROM user u
    LEFT JOIN user_collect_tag uct ON uct.user_id = u.user_id
    WHERE u.user_id IS NOT NULL
    GROUP BY u.user_id
    """
    user_rows = _query_rows(settings, user_sql, {})

    user_payload = []
    for row in user_rows:
        try:
            user_id = int(row.get("user_id") or 0)
        except Exception:
            continue
        if user_id <= 0:
            continue
        tags_raw = str(row.get("interest_tag_ids") or "").strip()
        tags = []
        if tags_raw:
            for x in tags_raw.split(","):
                try:
                    tags.append(int(x))
                except Exception:
                    continue
        user_payload.append(
            {
                "user_id": user_id,
                "gender": row.get("gender"),
                "birth": row.get("birth"),
                "created_at": row.get("created_at"),
                "interest_tags": tags,
            }
        )

    movie_cnt = store_movie_features(settings, movie_payload)
    user_cnt = store_user_features(settings, user_payload)
    return {"movie_features": int(movie_cnt), "user_features": int(user_cnt)}


def refresh_trending_cache(settings: Settings) -> dict[str, int]:
    windows = ("daily", "weekly", "monthly", "all_time")
    out: dict[str, int] = {}

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

    limit = max(int(settings.cache.trending_topk), 10)
    for window in windows:
        rows = _query_rows(settings, sql, {"window_start": _window_start(window), "limit": limit})
        pairs: list[tuple[int, float]] = []
        for row in rows:
            try:
                pairs.append((int(row.get("item_id") or 0), float(row.get("score") or 0.0)))
            except Exception:
                continue
        out[window] = int(store_trending_items(settings, window=window, pairs=pairs))

    return out


def refresh_static_recall_cache(settings: Settings) -> dict[str, int]:
    if get_redis_client(settings) is None:
        return {"item_similar_by_tags": 0, "user_interest_tag": 0}

    limit = max(int(settings.cache.static_recall_topk), 20)

    item_sql = """
    WITH ranked_item_sim AS (
      SELECT
        mt1.movie_id AS src_movie_id,
        mt2.movie_id AS item_id,
        SUM(COALESCE(mt1.weight, 1.0) * COALESCE(mt2.weight, 1.0)) AS score,
        ROW_NUMBER() OVER (
          PARTITION BY mt1.movie_id
          ORDER BY SUM(COALESCE(mt1.weight, 1.0) * COALESCE(mt2.weight, 1.0)) DESC, mt2.movie_id DESC
        ) AS rn
      FROM movie_tag mt1
      JOIN movie_tag mt2 ON mt2.tag_id = mt1.tag_id
      JOIN movie m ON m.movie_id = mt2.movie_id
      WHERE mt1.movie_id <> mt2.movie_id
        AND m.status = 'published'
      GROUP BY mt1.movie_id, mt2.movie_id
    )
    SELECT src_movie_id, item_id, score
    FROM ranked_item_sim
    WHERE rn <= :limit
    ORDER BY src_movie_id ASC, score DESC, item_id DESC
    """
    item_rows = _query_rows(settings, item_sql, {"limit": limit})

    by_src_item: dict[int, list[tuple[int, float]]] = {}
    for row in item_rows:
        try:
            src = int(row.get("src_movie_id") or 0)
            item_id = int(row.get("item_id") or 0)
            score = float(row.get("score") or 0.0)
        except Exception:
            continue
        if src <= 0 or item_id <= 0:
            continue
        by_src_item.setdefault(src, []).append((item_id, score))

    item_cnt = 0
    for src, pairs in by_src_item.items():
        item_cnt += int(store_item_similar(settings, movie_id=src, pairs=pairs))

    user_sql = """
    WITH ranked_user_interest AS (
      SELECT
        uct.user_id,
        mt.movie_id AS item_id,
        SUM((1.0 + COALESCE(mt.weight, 1.0)) * (1.0 + 0.01 * COALESCE(mt.hot_score, 0))) AS score,
        ROW_NUMBER() OVER (
          PARTITION BY uct.user_id
          ORDER BY SUM((1.0 + COALESCE(mt.weight, 1.0)) * (1.0 + 0.01 * COALESCE(mt.hot_score, 0))) DESC, mt.movie_id DESC
        ) AS rn
      FROM user_collect_tag uct
      JOIN movie_tag mt ON mt.tag_id = uct.tag_id
      JOIN movie m ON m.movie_id = mt.movie_id
      WHERE m.status = 'published'
      GROUP BY uct.user_id, mt.movie_id
    )
    SELECT user_id, item_id, score
    FROM ranked_user_interest
    WHERE rn <= :limit
    ORDER BY user_id ASC, score DESC, item_id DESC
    """
    user_rows = _query_rows(settings, user_sql, {"limit": limit})

    by_user: dict[int, list[tuple[int, float]]] = {}
    for row in user_rows:
        try:
            user_id = int(row.get("user_id") or 0)
            item_id = int(row.get("item_id") or 0)
            score = float(row.get("score") or 0.0)
        except Exception:
            continue
        if user_id <= 0 or item_id <= 0:
            continue
        by_user.setdefault(user_id, []).append((item_id, score))

    user_cnt = 0
    for user_id, pairs in by_user.items():
        user_cnt += int(store_user_interest(settings, user_id=user_id, pairs=pairs))

    return {
        "item_similar_by_tags": int(item_cnt),
        "user_interest_tag": int(user_cnt),
    }


def run_all_cache_precompute(settings: Settings) -> dict[str, dict[str, int]]:
    return {
        "features": refresh_feature_cache(settings),
        "trending": refresh_trending_cache(settings),
        "static_recall": refresh_static_recall_cache(settings),
    }
