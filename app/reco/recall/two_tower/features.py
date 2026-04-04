from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence

import numpy as np

from .db import execute


def _feature_execute_sql(mysql_dsn: str | None, sql: str, params: dict, *, expanding: tuple[str, ...] = ()) -> list[dict]:
    return execute(mysql_dsn, sql, params, expanding=expanding)


def parse_datetime_like(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if raw is None:
        return None
    text_val = str(raw).strip()
    if not text_val:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text_val, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text_val)
    except Exception:
        return None


def age_bucket_index(raw_birth: object) -> int:
    dt = parse_datetime_like(raw_birth)
    if dt is None:
        return 0
    age = int((datetime.utcnow().date() - dt.date()).days // 365)
    if age <= 17:
        return 1
    if age <= 24:
        return 2
    if age <= 34:
        return 3
    if age <= 44:
        return 4
    if age <= 54:
        return 5
    return 6


def register_bucket_index(raw_created_at: object) -> int:
    dt = parse_datetime_like(raw_created_at)
    if dt is None:
        return 0
    days = int((datetime.utcnow() - dt).days)
    if days < 30:
        return 1
    if days < 180:
        return 2
    if days < 365:
        return 3
    if days < 365 * 3:
        return 4
    return 5


def gender_index(raw_gender: object) -> int:
    g = str(raw_gender or "unknown").strip().lower()
    if g == "male":
        return 1
    if g == "female":
        return 2
    return 0


def fetch_user_profiles(mysql_dsn: str | None, user_ids: Sequence[int]) -> dict[int, dict[str, object]]:
    if not user_ids:
        return {}

    sql = """
    SELECT u.user_id, u.gender, u.birth, u.created_at
    FROM user u
    WHERE u.user_id IN :user_ids
    """
    rows = _feature_execute_sql(
        mysql_dsn,
        sql,
        {"user_ids": [int(x) for x in user_ids]},
        expanding=("user_ids",),
    )
    out: dict[int, dict[str, object]] = {}
    for row in rows:
        try:
            uid = int(row["user_id"])
        except Exception:
            continue
        out[uid] = {
            "gender": row.get("gender"),
            "birth": row.get("birth"),
            "created_at": row.get("created_at"),
        }
    return out


def fetch_user_recent_sequences(
    mysql_dsn: str | None,
    user_ids: Sequence[int],
    *,
    recent_limit: int,
) -> dict[int, list[int]]:
    if not user_ids or int(recent_limit) <= 0:
        return {}

    sql = """
    SELECT x.user_id, x.movie_id, x.ts
    FROM (
      SELECT ua.user_id, ua.movie_id, ua.created_at AS ts
      FROM user_action ua
      WHERE ua.user_id IN :user_ids AND ua.movie_id IS NOT NULL

      UNION ALL

      SELECT r.user_id, r.movie_id, r.updated_at AS ts
      FROM rating r
      WHERE r.user_id IN :user_ids AND r.movie_id IS NOT NULL

      UNION ALL

      SELECT c.user_id, c.movie_id, c.created_at AS ts
      FROM user_collect_movie c
      WHERE c.user_id IN :user_ids AND c.movie_id IS NOT NULL
    ) x
    ORDER BY x.user_id ASC, x.ts DESC
    """
    rows = _feature_execute_sql(
        mysql_dsn,
        sql,
        {"user_ids": [int(x) for x in user_ids]},
        expanding=("user_ids",),
    )

    out: dict[int, list[int]] = {}
    limit = int(recent_limit)
    for row in rows:
        try:
            uid = int(row["user_id"])
            iid = int(row["movie_id"])
        except Exception:
            continue
        seq = out.setdefault(uid, [])
        if len(seq) < limit:
            seq.append(iid)
    return out


def fetch_item_tags(mysql_dsn: str | None, item_ids: Sequence[int]) -> dict[int, list[int]]:
    if not item_ids:
        return {}
    sql = """
    SELECT mt.movie_id, mt.tag_id
    FROM movie_tag mt
    WHERE mt.movie_id IN :movie_ids
    ORDER BY mt.movie_id ASC, mt.weight DESC, mt.hot_score DESC
    """
    rows = _feature_execute_sql(
        mysql_dsn,
        sql,
        {"movie_ids": [int(x) for x in item_ids]},
        expanding=("movie_ids",),
    )
    out: dict[int, list[int]] = {}
    for row in rows:
        try:
            mid = int(row["movie_id"])
            tid = int(row["tag_id"])
        except Exception:
            continue
        out.setdefault(mid, []).append(tid)
    return out


def _feature_build_item_stats_vector(row: Mapping[str, object]) -> np.ndarray:
    rating_count = float(row.get("rating_count") or 0.0)
    rating_sum = float(row.get("rating_sum") or 0.0)
    avg_rating = rating_sum / rating_count if rating_count > 0 else 0.0

    hist = [float(row.get(f"rating_{i}_count") or 0.0) for i in range(1, 11)]
    hist_total = max(sum(hist), 1.0)
    hist_ratio = [v / hist_total for v in hist]

    collect_cnt = float(row.get("collect_cnt") or 0.0)
    hot_cnt = float(row.get("hot_cnt_30d") or 0.0)
    return np.asarray(
        [avg_rating / 10.0, np.log1p(rating_count), *hist_ratio, np.log1p(collect_cnt), np.log1p(hot_cnt)],
        dtype=np.float32,
    )


def fetch_item_stats(mysql_dsn: str | None, item_ids: Sequence[int]) -> dict[int, np.ndarray]:
    if not item_ids:
        return {}

    sql = """
    SELECT m.movie_id,
           m.rating_sum,
           m.rating_count,
           m.rating_1_count,
           m.rating_2_count,
           m.rating_3_count,
           m.rating_4_count,
           m.rating_5_count,
           m.rating_6_count,
           m.rating_7_count,
           m.rating_8_count,
           m.rating_9_count,
           m.rating_10_count,
           COALESCE(c.collect_cnt, 0) AS collect_cnt,
           COALESCE(h.hot_cnt_30d, 0) AS hot_cnt_30d
    FROM movie m
    LEFT JOIN (
        SELECT movie_id, COUNT(*) AS collect_cnt
        FROM user_collect_movie
        GROUP BY movie_id
    ) c ON c.movie_id = m.movie_id
    LEFT JOIN (
        SELECT movie_id, COUNT(*) AS hot_cnt_30d
        FROM user_action
        WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
        GROUP BY movie_id
    ) h ON h.movie_id = m.movie_id
    WHERE m.movie_id IN :movie_ids
    """
    rows = _feature_execute_sql(
        mysql_dsn,
        sql,
        {"movie_ids": [int(x) for x in item_ids]},
        expanding=("movie_ids",),
    )

    out: dict[int, np.ndarray] = {}
    for row in rows:
        try:
            mid = int(row["movie_id"])
        except Exception:
            continue
        out[mid] = _feature_build_item_stats_vector(row)
    return out


def fetch_all_movie_ids(mysql_dsn: str | None) -> list[int]:
    sql = """
    SELECT m.movie_id
    FROM movie m
    WHERE m.movie_id IS NOT NULL
    ORDER BY m.movie_id ASC
    """
    rows = _feature_execute_sql(mysql_dsn, sql, {})
    out: list[int] = []
    for row in rows:
        try:
            mid = int(row.get("movie_id") or 0)
        except Exception:
            continue
        if mid > 0:
            out.append(mid)
    return out


def fetch_user_excluded_items(user_id: int, *, mysql_dsn: str | None, recent_limit: int) -> set[int]:
    limit = max(int(recent_limit), 0)
    if limit == 0:
        return set()

    sql = """
    SELECT z.movie_id
    FROM (
      SELECT x.movie_id, MAX(x.ts) AS last_ts
      FROM (
        SELECT movie_id, created_at AS ts FROM user_collect_movie WHERE user_id = :user_id
        UNION ALL
        SELECT movie_id, updated_at AS ts FROM rating WHERE user_id = :user_id
        UNION ALL
        SELECT movie_id, created_at AS ts FROM user_action WHERE user_id = :user_id
      ) x
      WHERE x.movie_id IS NOT NULL
      GROUP BY x.movie_id
      ORDER BY last_ts DESC
      LIMIT :limit
    ) z
    """
    rows = _feature_execute_sql(mysql_dsn, sql, {"user_id": int(user_id), "limit": limit})
    out: set[int] = set()
    for row in rows:
        try:
            out.add(int(row["movie_id"]))
        except Exception:
            continue
    return out
