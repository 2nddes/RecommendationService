from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Iterable, List, Mapping, Sequence

from sqlalchemy import Engine, bindparam, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.reco.recall.base import Recaller
from app.reco.types import Candidate, RequestContext


_engine_by_dsn: dict[str, Engine] = {}
logger = logging.getLogger(__name__)


def _get_engine(mysql_dsn: str | None) -> Engine | None:
    if not mysql_dsn:
        err = RuntimeError("mysql_dsn_missing")
        logger.exception("mysql recall dsn is missing")
        raise err
    dsn = str(mysql_dsn).strip()
    if not dsn:
        err = RuntimeError("mysql_dsn_empty")
        logger.exception("mysql recall dsn is empty")
        raise err
    cached = _engine_by_dsn.get(dsn)
    if cached is not None:
        return cached
    try:
        _engine_by_dsn[dsn] = create_engine(dsn, pool_pre_ping=True)
        return _engine_by_dsn[dsn]
    except Exception as e:
        logger.exception("mysql recall engine create failed, dsn_set=%s", bool(dsn))
        raise RuntimeError(f"mysql_recall_engine_create_failed: {type(e).__name__}: {e}") from e


def _execute(mysql_dsn: str | None, sql: str, params: Mapping[str, Any], *, expanding: Sequence[str] = ()) -> List[dict]:
    engine = _get_engine(mysql_dsn)
    if engine is None:
        err = RuntimeError("mysql_recall_engine_unavailable")
        logger.exception("mysql recall execute failed: engine unavailable")
        raise err

    try:
        with engine.connect() as conn:
            stmt = text(sql)
            for key in expanding:
                stmt = stmt.bindparams(bindparam(key, expanding=True))
            rs = conn.execute(stmt, dict(params))
            return [dict(row._mapping) for row in rs]
    except SQLAlchemyError as e:
        logger.exception("mysql recall execute query failed")
        raise RuntimeError(f"mysql_recall_query_failed: {type(e).__name__}: {e}") from e


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            raise ValueError("value_is_none")
        return float(value)
    except Exception as e:
        logger.exception("mysql recall float parse failed")
        raise RuntimeError(f"mysql_recall_float_parse_failed: {type(e).__name__}: {e}") from e


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            raise ValueError("value_is_none")
        return int(value)
    except Exception as e:
        logger.exception("mysql recall int parse failed")
        raise RuntimeError(f"mysql_recall_int_parse_failed: {type(e).__name__}: {e}") from e


def _collect_user_exclusions(mysql_dsn: str | None, user_id: int) -> set[int]:
    sql = """
    SELECT DISTINCT x.movie_id
    FROM (
      SELECT movie_id FROM user_collect_movie WHERE user_id = :user_id
      UNION ALL
      SELECT movie_id FROM rating WHERE user_id = :user_id
      UNION ALL
      SELECT movie_id FROM user_action WHERE user_id = :user_id
    ) x
    """
    rows = _execute(mysql_dsn, sql, {"user_id": int(user_id)})
    return {_as_int(r.get("movie_id")) for r in rows if r.get("movie_id") is not None}


def _user_exists(mysql_dsn: str | None, user_id: int) -> bool:
    sql = """
    SELECT 1 AS ok
    FROM user u
    WHERE u.user_id = :user_id
    LIMIT 1
    """
    rows = _execute(mysql_dsn, sql, {"user_id": int(user_id)})
    return bool(rows)


def _trending_candidates(mysql_dsn: str | None, *, topk: int, source: str) -> List[Candidate]:
    sql = """
    SELECT
      m.movie_id AS item_id,
      (
        (COALESCE(m.rating_sum, 0) / NULLIF(m.rating_count, 0)) * LOG10(COALESCE(m.rating_count, 0) + 1)
      ) AS score
    FROM movie m
    WHERE m.status = 'published'
    ORDER BY score DESC, m.rating_count DESC, m.movie_id DESC
    LIMIT :limit
    """
    rows = _execute(mysql_dsn, sql, {"limit": int(topk)})
    out: List[Candidate] = []
    for row in rows:
        mid = _as_int(row.get("item_id"))
        if mid <= 0:
            continue
        out.append(Candidate(item_id=mid, score=_as_float(row.get("score")), source=source))
    return out


def _merge_best(rows: Iterable[Mapping[str, Any]], *, source: str, excluded: set[int]) -> List[Candidate]:
    best: dict[int, float] = {}
    for r in rows:
        mid = _as_int(r.get("item_id"))
        if mid <= 0 or mid in excluded:
            continue
        score = _as_float(r.get("score"))
        prev = best.get(mid)
        if prev is None or score > prev:
            best[mid] = score

    out = [Candidate(item_id=item_id, score=score, source=source) for item_id, score in best.items()]
    out.sort(key=lambda x: x.score, reverse=True)
    return out


@dataclass(frozen=True)
class UserCollectionRecall(Recaller):
    """基于用户历史正反馈种子的内容召回（movie_tag）。

    依赖表：user_collect_movie, rating, movie_tag, movie
    """

    mysql_dsn: str | None = None
    topk: int = 200
    per_seed_topk: int = 50

    @property
    def name(self) -> str:
        return "user_collection"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        if ctx.user_id is None:
            err = ValueError("user_id_undefined")
            logger.exception("user_collection recall input invalid: user_id is None")
            raise err

        if not _user_exists(self.mysql_dsn, int(ctx.user_id)):
            err = RuntimeError("user_not_found")
            logger.exception("user_collection recall user not found, user_id=%s", ctx.user_id)
            raise err

        topk = int(self.topk)

        seed_sql = """
        SELECT movie_id
        FROM (
          SELECT ucm.movie_id AS movie_id, ucm.created_at AS ts
          FROM user_collect_movie ucm
          WHERE ucm.user_id = :user_id
          UNION ALL
          SELECT r.movie_id AS movie_id, r.updated_at AS ts
          FROM rating r
          WHERE r.user_id = :user_id AND r.rating >= 7
        ) s
        ORDER BY ts DESC
        LIMIT 80
        """
        seed_rows = _execute(self.mysql_dsn, seed_sql, {"user_id": int(ctx.user_id)})
        seed_ids = [_as_int(r.get("movie_id")) for r in seed_rows if _as_int(r.get("movie_id")) > 0]
        if not seed_ids:
            err = RuntimeError("seed_ids_empty")
            logger.exception("user_collection recall seed ids empty, user_id=%s", ctx.user_id)
            raise err

        sim_sql = """
        SELECT
          mt2.movie_id AS item_id,
          SUM((COALESCE(mt1.weight, 1.0) * COALESCE(mt2.weight, 1.0)) + 0.05 * COALESCE(mt2.hot_score, 0)) AS score
        FROM movie_tag mt1
        JOIN movie_tag mt2 ON mt2.tag_id = mt1.tag_id
        JOIN movie m ON m.movie_id = mt2.movie_id
        WHERE mt1.movie_id IN :seed_ids
          AND mt2.movie_id NOT IN :seed_ids
          AND m.status = 'published'
        GROUP BY mt2.movie_id
        ORDER BY score DESC
        LIMIT :limit
        """
        rows = _execute(
            self.mysql_dsn,
            sim_sql,
            {"seed_ids": list(seed_ids), "limit": int(max(topk, self.per_seed_topk * len(seed_ids)))},
            expanding=("seed_ids",),
        )

        excluded = _collect_user_exclusions(self.mysql_dsn, int(ctx.user_id))
        candidates = _merge_best(rows, source=self.name, excluded=excluded)
        if candidates:
            return candidates[:topk]
        logger.warning("user_collection recall produced no candidates, user_id=%s", ctx.user_id)
        return []


@dataclass(frozen=True)
class UserHighRatingSimilarRecall(Recaller):
    """基于评分数据的用户协同召回。 

    依赖表：rating, movie
    """

    mysql_dsn: str | None = None
    rating_threshold: int = 8
    topk: int = 300

    @property
    def name(self) -> str:
        return "user_high_rating_similar"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        if ctx.user_id is None:
            err = ValueError("user_id_undefined")
            logger.exception("user_high_rating_similar recall input invalid: user_id is None")
            raise err

        if not _user_exists(self.mysql_dsn, int(ctx.user_id)):
            err = RuntimeError("user_not_found")
            logger.exception("user_high_rating_similar recall user not found, user_id=%s", ctx.user_id)
            raise err

        topk = int(self.topk)
        thr = int(self.rating_threshold)

        seed_sql = """
        SELECT r.movie_id
        FROM rating r
        WHERE r.user_id = :user_id AND r.rating >= :thr
        ORDER BY r.updated_at DESC
        LIMIT 60
        """
        seed_rows = _execute(self.mysql_dsn, seed_sql, {"user_id": int(ctx.user_id), "thr": thr})
        seed_ids = [_as_int(r.get("movie_id")) for r in seed_rows if _as_int(r.get("movie_id")) > 0]
        if not seed_ids:
            err = RuntimeError("seed_ids_empty")
            logger.exception("user_high_rating_similar recall seed ids empty, user_id=%s", ctx.user_id)
            raise err

        cf_sql = """
        SELECT
          r2.movie_id AS item_id,
          AVG(r2.rating) * LOG10(COUNT(*) + 1) AS score
        FROM rating r1
        JOIN rating r2 ON r2.user_id = r1.user_id
        JOIN movie m ON m.movie_id = r2.movie_id
        WHERE r1.movie_id IN :seed_ids
          AND r1.rating >= :thr
          AND r2.rating >= :thr
          AND r2.movie_id NOT IN :seed_ids
          AND m.status = 'published'
        GROUP BY r2.movie_id
        ORDER BY score DESC
        LIMIT :limit
        """
        rows = _execute(
            self.mysql_dsn,
            cf_sql,
            {"seed_ids": list(seed_ids), "thr": thr, "limit": int(topk * 3)},
            expanding=("seed_ids",),
        )

        excluded = _collect_user_exclusions(self.mysql_dsn, int(ctx.user_id))
        candidates = _merge_best(rows, source=self.name, excluded=excluded)
        if candidates:
            return candidates[:topk]
        logger.warning("user_high_rating_similar recall produced no candidates, user_id=%s", ctx.user_id)
        return []


@dataclass(frozen=True)
class UserInterestTagRecall(Recaller):
    """基于用户兴趣标签召回。

    依赖表：user_collect_tag, movie_tag, movie
    """

    mysql_dsn: str | None = None
    topk: int = 300

    @property
    def name(self) -> str:
        return "user_interest_tag"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        if ctx.user_id is None:
            err = ValueError("user_id_undefined")
            logger.exception("user_interest_tag recall input invalid: user_id is None")
            raise err

        if not _user_exists(self.mysql_dsn, int(ctx.user_id)):
            err = RuntimeError("user_not_found")
            logger.exception("user_interest_tag recall user not found, user_id=%s", ctx.user_id)
            raise err

        topk = int(self.topk)

        sql = """
        SELECT
          mt.movie_id AS item_id,
          SUM((1.0 + COALESCE(mt.weight, 1.0)) * (1.0 + 0.01 * COALESCE(mt.hot_score, 0))) AS score
        FROM user_collect_tag uct
        JOIN movie_tag mt ON mt.tag_id = uct.tag_id
        JOIN movie m ON m.movie_id = mt.movie_id
        WHERE uct.user_id = :user_id
          AND m.status = 'published'
        GROUP BY mt.movie_id
        ORDER BY score DESC
        LIMIT :limit
        """

        rows = _execute(self.mysql_dsn, sql, {"user_id": int(ctx.user_id), "limit": int(topk * 2)})
        excluded = _collect_user_exclusions(self.mysql_dsn, int(ctx.user_id))
        candidates = _merge_best(rows, source=self.name, excluded=excluded)

        if candidates:
            return candidates[:topk]
        logger.warning("user_interest_tag recall produced no candidates, user_id=%s", ctx.user_id)
        return []


@dataclass(frozen=True)
class ItemSimilarByTagsRecall(Recaller):
    """给 `/recommend/item` 用的内容相似召回：按标签重叠强度召回。"""

    mysql_dsn: str | None = None
    topk: int = 200

    @property
    def name(self) -> str:
        return "item_similar_by_tags"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        if ctx.movie_id is None:
            err = ValueError("movie_id_undefined")
            logger.exception("item_similar_by_tags recall input invalid: movie_id is None")
            raise err

        topk = int(self.topk)

        sql = """
        SELECT
          mt2.movie_id AS item_id,
          SUM(COALESCE(mt1.weight, 1.0) * COALESCE(mt2.weight, 1.0)) AS score
        FROM movie_tag mt1
        JOIN movie_tag mt2 ON mt2.tag_id = mt1.tag_id
        JOIN movie m ON m.movie_id = mt2.movie_id
        WHERE mt1.movie_id = :movie_id
          AND mt2.movie_id <> :movie_id
          AND m.status = 'published'
        GROUP BY mt2.movie_id
        ORDER BY score DESC
        LIMIT :limit
        """
        rows = _execute(self.mysql_dsn, sql, {"movie_id": int(ctx.movie_id), "limit": int(topk)})

        candidates = _merge_best(rows, source=self.name, excluded=set())
        if candidates:
            return candidates[:topk]
        logger.warning("item_similar_by_tags recall produced no candidates, movie_id=%s", ctx.movie_id)
        return []
