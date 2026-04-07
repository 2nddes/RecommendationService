from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from app.common.runtime_health import mark_component_success
from app.common.redis_cache import (
    get_redis_client,
    store_movie_features,
    store_trending_items,
    store_user_features,
)
from app.common.settings import Settings
from app.reco.recall.two_tower.db import get_engine


logger = logging.getLogger(__name__)


def _window_start(window: str) -> datetime | None:
    now = datetime.utcnow()
    if window == "all_time":
        return None
    if window == "daily":
        return now - timedelta(days=1)
    if window == "weekly":
        return now - timedelta(days=7)
    if window == "monthly":
        return now - timedelta(days=30)
    if window == "half_year":
        return now - timedelta(days=180)
    if window == "one_year":
        return now - timedelta(days=365)
    return now - timedelta(days=7)


def _query_rows(settings: Settings, sql: str, params: dict) -> list[dict]:
    engine = get_engine(settings.core.mysql_dsn)
    if engine is None:
        return []
    with engine.connect() as conn:
        rs = conn.execute(text(sql), params)
        return [dict(row._mapping) for row in rs]


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
        movie_id = int(row["movie_id"])
        tags_raw = str(row.get("tag_ids") or "").strip()
        tags = []
        if tags_raw:
            for x in tags_raw.split(","):
                tags.append(int(x))
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
        user_id = int(row["user_id"])
        tags_raw = str(row.get("interest_tag_ids") or "").strip()
        tags = []
        if tags_raw:
            for x in tags_raw.split(","):
                tags.append(int(x))
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
    windows = ("daily", "weekly", "monthly", "half_year", "one_year", "all_time")
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
            SELECT x.movie_id, SUM(x.cnt) AS action_cnt
            FROM (
                SELECT movie_id, COUNT(*) AS cnt
                FROM user_click
                WHERE (:window_start IS NULL OR created_at >= :window_start)
                GROUP BY movie_id

                UNION ALL

                SELECT movie_id, COUNT(*) AS cnt
                FROM movie_comment
                WHERE (:window_start IS NULL OR created_at >= :window_start)
                    AND deleted_at IS NULL
                GROUP BY movie_id
            ) x
            GROUP BY x.movie_id
    ) ua ON ua.movie_id = m.movie_id
    WHERE m.status = 'published'
    ORDER BY score DESC, COALESCE(ua.action_cnt, 0) DESC, m.rating_count DESC, m.movie_id DESC
    LIMIT :limit
    """

    sql_all_time = """
        SELECT
            m.movie_id AS item_id,
            COALESCE(m.rating_count, 0) AS score
        FROM movie m
        WHERE m.status = 'published'
        ORDER BY m.rating_count DESC, m.movie_id DESC
        LIMIT :limit
        """

    limit = max(int(settings.cache.trending_topk), 10)
    for window in windows:
        if window == "all_time":
            rows = _query_rows(settings, sql_all_time, {"limit": limit})
        else:
            rows = _query_rows(settings, sql, {"window_start": _window_start(window), "limit": limit})
        pairs: list[tuple[int, float]] = []
        for row in rows:
            item_id = int(row["item_id"])
            pairs.append((item_id, float(row.get("score") or 0.0)))
        out[window] = int(store_trending_items(settings, window=window, pairs=pairs))

    return out


def run_all_cache_precompute(settings: Settings) -> dict[str, dict[str, int]]:
    summary = {
        "features": refresh_feature_cache(settings),
        "trending": refresh_trending_cache(settings),
    }
    mark_component_success("cache_precompute", details=summary)
    return summary
