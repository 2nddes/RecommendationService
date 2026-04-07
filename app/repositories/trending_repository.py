from __future__ import annotations

from datetime import datetime, timedelta
import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.reco.recall.two_tower.db import get_engine


logger = logging.getLogger(__name__)


class TrendingRepository:
    def __init__(self, mysql_dsn: str | None) -> None:
        self._mysql_dsn = mysql_dsn

    def _window_start(self, window: str) -> datetime | None:
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

    def fetch_item_scores(self, *, window: str, n: int) -> list[tuple[int, float]]:
        engine = get_engine(self._mysql_dsn)
        if engine is None:
            logger.warning("TrendingRepository skipped: mysql engine unavailable, window=%s, n=%s", window, n)
            return []

        logger.info("TrendingRepository query started, window=%s, n=%s", window, n)

        if window == "all_time":
            sql = """
            SELECT
              m.movie_id AS item_id,
              COALESCE(m.rating_count, 0) AS score
            FROM movie m
            WHERE m.status = 'published'
            ORDER BY m.rating_count DESC, m.movie_id DESC
            LIMIT :limit
            """
            try:
                with engine.connect() as conn:
                    rows = conn.execute(text(sql), {"limit": int(n)})
                    out: list[tuple[int, float]] = []
                    for row in rows:
                        out.append((int(row._mapping["item_id"]), float(row._mapping["score"] or 0.0)))
                    logger.info(
                        "TrendingRepository query completed, window=%s, requested=%s, returned=%s",
                        window,
                        n,
                        len(out),
                    )
                    return out
            except SQLAlchemyError:
                logger.exception("TrendingRepository query failed, window=%s, n=%s", window, n)
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

        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(sql),
                    {
                        "window_start": self._window_start(window),
                        "limit": int(n),
                    },
                )
                out: list[tuple[int, float]] = []
                for row in rows:
                    out.append((int(row._mapping["item_id"]), float(row._mapping["score"] or 0.0)))
                logger.info("TrendingRepository query completed, window=%s, requested=%s, returned=%s", window, n, len(out))
                return out
        except SQLAlchemyError:
            logger.exception("TrendingRepository query failed, window=%s, n=%s", window, n)
            return []
