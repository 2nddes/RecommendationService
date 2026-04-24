from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import text

from app.common.redis_cache import (
    get_redis_client,
    store_tag_recall_items,
    store_movie_features,
    store_trending_items,
    store_user_features,
)
from app.common.settings import Settings
from app.reco.recall.two_tower.db import get_engine


logger = logging.getLogger(__name__)


def _execute_statement(settings: Settings, sql: str, params: dict | None = None) -> None:
    engine = get_engine(settings.core.mysql_dsn)
    if engine is None:
        return
    with engine.begin() as conn:
        conn.execute(text(sql), params or {})


def _index_exists(settings: Settings, *, table_name: str, index_name: str) -> bool:
    rows = _query_rows(
        settings,
        """
        SELECT COUNT(1) AS cnt
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = :table_name
          AND index_name = :index_name
        """,
        {
            "table_name": str(table_name),
            "index_name": str(index_name),
        },
    )
    return bool(rows and int(rows[0].get("cnt") or 0) > 0)


def _column_exists(settings: Settings, *, table_name: str, column_name: str) -> bool:
    rows = _query_rows(
        settings,
        """
        SELECT COUNT(1) AS cnt
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = :table_name
          AND column_name = :column_name
        """,
        {
            "table_name": str(table_name),
            "column_name": str(column_name),
        },
    )
    return bool(rows and int(rows[0].get("cnt") or 0) > 0)


def _ensure_column(settings: Settings, *, table_name: str, column_name: str, ddl: str) -> bool:
    if _column_exists(settings, table_name=table_name, column_name=column_name):
        return False
    _execute_statement(settings, ddl)
    logger.info("Search column created, table=%s, column=%s", table_name, column_name)
    return True


def _ensure_index(settings: Settings, *, table_name: str, index_name: str, columns_sql: str) -> bool:
    if _index_exists(settings, table_name=table_name, index_name=index_name):
        return False
    _execute_statement(
        settings,
        f"CREATE INDEX {index_name} ON {table_name} ({columns_sql})",
    )
    logger.info("Search index created, table=%s, index=%s, columns=%s", table_name, index_name, columns_sql)
    return True


def ensure_search_schema(settings: Settings) -> dict[str, int]:
    engine = get_engine(settings.core.mysql_dsn)
    if engine is None:
        return {"columns_created": 0, "indexes_created": 0, "skipped": 1}

    columns_created = 0
    if _ensure_column(
        settings,
        table_name="movie",
        column_name="collect_count",
        ddl="ALTER TABLE movie ADD COLUMN collect_count int NOT NULL DEFAULT 0 COMMENT '收藏数统计' AFTER rating_count",
    ):
        columns_created += 1
    if _ensure_column(
        settings,
        table_name="movie",
        column_name="bayesian_rating",
        ddl="ALTER TABLE movie ADD COLUMN bayesian_rating double NOT NULL DEFAULT 0 COMMENT '贝叶斯评分' AFTER collect_count",
    ):
        columns_created += 1

    indexes_created = 0
    if _ensure_index(
        settings,
        table_name="movie",
        index_name="idx_search_release_page",
        columns_sql="status, deleted_at, release_date, movie_id",
    ):
        indexes_created += 1
    if _ensure_index(
        settings,
        table_name="movie",
        index_name="idx_search_duration_page",
        columns_sql="status, deleted_at, duration_min, movie_id",
    ):
        indexes_created += 1
    if _ensure_index(
        settings,
        table_name="movie_tag",
        index_name="idx_search_tag_movie",
        columns_sql="tag_id, movie_id",
    ):
        indexes_created += 1
    if _ensure_index(
        settings,
        table_name="movie",
        index_name="idx_search_collect_page",
        columns_sql="status, deleted_at, collect_count, movie_id",
    ):
        indexes_created += 1
    if _ensure_index(
        settings,
        table_name="movie",
        index_name="idx_search_bayesian_page",
        columns_sql="status, deleted_at, bayesian_rating, movie_id",
    ):
        indexes_created += 1

    return {"columns_created": int(columns_created), "indexes_created": int(indexes_created), "skipped": 0}


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


def _bayesian_weighted_rating(*, rating_sum: float, rating_count: int, global_mean: float, min_votes: int) -> float:
    v = max(int(rating_count), 0)
    if v <= 0:
        return 0.0

    m = max(int(min_votes), 0)
    r = float(rating_sum) / float(v)
    if v + m <= 0:
        return r
    return (float(v) / float(v + m)) * r + (float(m) / float(v + m)) * float(global_mean)


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


def refresh_search_stats(settings: Settings) -> dict[str, int]:
    schema_summary = ensure_search_schema(settings)
    engine = get_engine(settings.core.mysql_dsn)
    if engine is None:
        return {
            "skipped": 1,
            "rows_written": 0,
            "columns_created": int(schema_summary.get("columns_created") or 0),
            "indexes_created": int(schema_summary.get("indexes_created") or 0),
        }

    c_rows = _query_rows(
        settings,
        """
        SELECT AVG(COALESCE(m.rating_sum, 0) / NULLIF(m.rating_count, 0)) AS global_avg
        FROM movie m
        WHERE m.status = 'published'
          AND m.deleted_at IS NULL
          AND m.rating_count > 0
        """,
        {},
    )
    global_avg = float(c_rows[0].get("global_avg") or 0.0) if c_rows else 0.0
    min_votes = max(int(settings.tag_recall.min_rating_count_m), 0)

    update_sql = text(
        """
        UPDATE movie m
        LEFT JOIN (
            SELECT ucm.movie_id, COUNT(*) AS collect_count
            FROM user_collect_movie ucm
            GROUP BY ucm.movie_id
        ) cs ON cs.movie_id = m.movie_id
        SET
            m.collect_count = COALESCE(cs.collect_count, 0),
            m.bayesian_rating = CASE
                WHEN COALESCE(m.rating_count, 0) <= 0 THEN 0
                ELSE (
                    ((m.rating_count / NULLIF(m.rating_count + :min_votes, 0)) * (m.rating_sum / NULLIF(m.rating_count, 0)))
                    + ((:min_votes / NULLIF(m.rating_count + :min_votes, 0)) * :global_avg)
                )
            END
        WHERE m.deleted_at IS NULL
        """
    )

    started = datetime.utcnow()
    with engine.begin() as conn:
        result = conn.execute(
            update_sql,
            {
                "min_votes": int(min_votes),
                "global_avg": float(global_avg),
            },
        )

    elapsed_ms = int((datetime.utcnow() - started).total_seconds() * 1000.0)
    rows_written = max(int(result.rowcount or 0), 0)
    logger.info(
        "Search stats refresh completed, rows_written=%s, global_avg=%.4f, min_votes=%s, elapsed_ms=%s",
        rows_written,
        global_avg,
        min_votes,
        elapsed_ms,
    )
    return {
        "skipped": 0,
        "rows_written": rows_written,
        "columns_created": int(schema_summary.get("columns_created") or 0),
        "indexes_created": int(schema_summary.get("indexes_created") or 0),
    }


def refresh_tag_inverted_recall_cache(settings: Settings) -> dict[str, int]:
    if not settings.tag_recall.enabled:
        return {
            "enabled": 0,
            "skipped": 1,
            "genre_keys": 0,
            "director_keys": 0,
            "keys_written": 0,
            "members_written": 0,
        }

    if get_redis_client(settings) is None:
        return {
            "enabled": 1,
            "skipped": 1,
            "genre_keys": 0,
            "director_keys": 0,
            "keys_written": 0,
            "members_written": 0,
        }

    endorsement_source = str(settings.tag_recall.director_endorsement_source or "").strip().lower()
    if endorsement_source != "rating_count":
        raise RuntimeError(f"director_endorsement_source_not_supported: {endorsement_source}")

    min_votes = max(int(settings.tag_recall.min_rating_count_m), 0)

    c_sql = """
    SELECT AVG(COALESCE(m.rating_sum, 0) / NULLIF(m.rating_count, 0)) AS global_avg
    FROM movie m
    WHERE m.status = 'published' AND m.rating_count > 0
    """
    c_rows = _query_rows(settings, c_sql, {})
    global_avg = float(c_rows[0].get("global_avg") or 0.0) if c_rows else 0.0

    genre_sql = """
    SELECT
      mt.tag_id AS tag_id,
      mt.movie_id AS movie_id,
      COALESCE(mt.vote_up, 0) AS endorsement_cnt,
      COALESCE(m.rating_sum, 0) AS rating_sum,
      COALESCE(m.rating_count, 0) AS rating_count
    FROM movie_tag mt
    JOIN movie m ON m.movie_id = mt.movie_id
    JOIN tag_dict td ON td.tag_id = mt.tag_id
    WHERE m.status = 'published'
    """

    director_sql = """
    SELECT
      mp.person_id AS tag_id,
      mp.movie_id AS movie_id,
      COALESCE(m.rating_sum, 0) AS rating_sum,
      COALESCE(m.rating_count, 0) AS rating_count
    FROM movie_person mp
    JOIN movie m ON m.movie_id = mp.movie_id
    WHERE m.status = 'published' AND mp.person_role = 'director'
    """

    genre_rows = _query_rows(settings, genre_sql, {})
    director_rows = _query_rows(settings, director_sql, {})

    payload: dict[tuple[str, int], dict[int, float]] = {}
    for row in genre_rows:
        tag_id = int(row["tag_id"])
        movie_id = int(row["movie_id"])
        rating_count = int(row.get("rating_count") or 0)
        rating_sum = float(row.get("rating_sum") or 0.0)
        endorsement_cnt = max(int(row.get("endorsement_cnt") or 0), 0)

        wr = _bayesian_weighted_rating(
            rating_sum=rating_sum,
            rating_count=rating_count,
            global_mean=global_avg,
            min_votes=min_votes,
        )
        final_score = float(wr) * float(math.log(float(endorsement_cnt) + 1.0))
        if final_score <= 0.0:
            continue

        bucket = payload.setdefault(("genre", tag_id), {})
        prev = bucket.get(movie_id)
        if prev is None or final_score > prev:
            bucket[movie_id] = final_score

    for row in director_rows:
        tag_id = int(row["tag_id"])
        movie_id = int(row["movie_id"])
        rating_count = int(row.get("rating_count") or 0)
        rating_sum = float(row.get("rating_sum") or 0.0)
        endorsement_cnt = max(rating_count, 0)

        wr = _bayesian_weighted_rating(
            rating_sum=rating_sum,
            rating_count=rating_count,
            global_mean=global_avg,
            min_votes=min_votes,
        )
        final_score = float(wr) * float(math.log(float(endorsement_cnt) + 1.0))
        if final_score <= 0.0:
            continue

        bucket = payload.setdefault(("director", tag_id), {})
        prev = bucket.get(movie_id)
        if prev is None or final_score > prev:
            bucket[movie_id] = final_score

    tag_items: dict[tuple[str, int], list[tuple[int, float]]] = {}
    genre_keys = 0
    director_keys = 0
    for spec, item_map in payload.items():
        pairs = [(int(movie_id), float(score)) for movie_id, score in item_map.items() if float(score) > 0.0]
        if not pairs:
            continue
        tag_items[spec] = pairs
        if spec[0] == "genre":
            genre_keys += 1
        elif spec[0] == "director":
            director_keys += 1

    written = store_tag_recall_items(
        settings,
        tag_items=tag_items,
        retain_topn=max(int(settings.tag_recall.retain_topn_per_tag), 1),
    )
    return {
        "enabled": 1,
        "skipped": 0,
        "genre_keys": int(genre_keys),
        "director_keys": int(director_keys),
        "keys_written": int(len(written)),
        "members_written": int(sum(written.values())),
    }


def run_all_cache_precompute(settings: Settings) -> dict[str, dict[str, int]]:
    return {
        "search_stats": refresh_search_stats(settings),
        "features": refresh_feature_cache(settings),
        "trending": refresh_trending_cache(settings),
        "tag_recall": refresh_tag_inverted_recall_cache(settings),
    }
