from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Iterable, List, Sequence

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.reco.recall.base import Recaller
from app.reco.types import Candidate, RequestContext


def _get_mysql_dsn() -> str | None:
    # Example: mysql+pymysql://user:pass@127.0.0.1:3306/movie_recommend?charset=utf8mb4
    return os.getenv("MYSQL_DSN") or None


_engine: Engine | None = None


def _get_engine() -> Engine | None:
    global _engine
    dsn = _get_mysql_dsn()
    if not dsn:
        return None

    if _engine is None:
        # pool_pre_ping: avoid stale connections
        _engine = create_engine(dsn, pool_pre_ping=True)
    return _engine


def _rows_to_candidates(rows: Iterable[dict], source: str, id_key: str, score_key: str) -> List[Candidate]:
    out: List[Candidate] = []
    for r in rows:
        try:
            item_id = int(r[id_key])
            score = float(r.get(score_key) or 0.0)
        except Exception:
            continue
        out.append(Candidate(item_id=item_id, score=score, source=source))
    return out


def _execute(sql: str, params: dict) -> List[dict]:
    engine = _get_engine()
    if engine is None:
        return []

    try:
        with engine.connect() as conn:
            rs = conn.execute(text(sql), params)
            return [dict(row._mapping) for row in rs]
    except SQLAlchemyError:
        # 召回阶段不应把服务打挂：失败则本通道返回空
        return []


def _clamp_positive(value: int, default: int) -> int:
    return value if value > 0 else default


@dataclass(frozen=True)
class UserCollectionRecall(Recaller):
    """从用户收藏的影片出发，召回相似影片。

    依赖表：user_collection, rec_similarity
    """

    topk: int = 200
    per_seed_topk: int = 50

    @property
    def name(self) -> str:
        return "user_collection"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        if ctx.user_id is None:
            return []

        topk = _clamp_positive(int(os.getenv("RECALL_TOPK_USER_COLLECTION") or self.topk), self.topk)
        per_seed_topk = _clamp_positive(
            int(os.getenv("RECALL_PER_SEED_TOPK_USER_COLLECTION") or self.per_seed_topk),
            self.per_seed_topk,
        )

        # 取最近收藏的若干部作为种子
        seed_sql = """
        SELECT uc.movie_id
        FROM user_collection uc
        WHERE uc.user_id = :user_id
        ORDER BY uc.id DESC
        LIMIT 50
        """
        seeds = _execute(seed_sql, {"user_id": ctx.user_id})
        seed_ids = [int(x["movie_id"]) for x in seeds if x.get("movie_id") is not None]
        if not seed_ids:
            return []

        # 用 rec_similarity 做 item-based 召回（包含正反向）
        # 注意：这里假设 rec_similarity 存的是 movie 表的主键 id。
        sim_sql = """
        (
          SELECT rs.movie_b_id AS item_id, rs.similarity AS score
          FROM rec_similarity rs
          WHERE rs.movie_a_id IN :seed_ids
          ORDER BY rs.similarity DESC
          LIMIT :limit
        )
        UNION ALL
        (
          SELECT rs.movie_a_id AS item_id, rs.similarity AS score
          FROM rec_similarity rs
          WHERE rs.movie_b_id IN :seed_ids
          ORDER BY rs.similarity DESC
          LIMIT :limit
        )
        """

        rows = _execute(
            sim_sql,
            {
                "seed_ids": tuple(seed_ids),
                "limit": per_seed_topk,
            },
        )

        # 排除用户自己已收藏/已评分过的电影
        exclude_sql = """
        SELECT DISTINCT x.movie_id
        FROM (
          SELECT movie_id FROM user_collection WHERE user_id = :user_id
          UNION ALL
          SELECT movie_id FROM user_action WHERE user_id = :user_id
        ) x
        """
        exclude_rows = _execute(exclude_sql, {"user_id": ctx.user_id})
        excluded = {int(r["movie_id"]) for r in exclude_rows if r.get("movie_id") is not None}

        # 合并同一 item 的最高相似度
        best: dict[int, float] = {}
        for r in rows:
            try:
                mid = int(r["item_id"])
                if mid in excluded:
                    continue
                sc = float(r.get("score") or 0.0)
            except Exception:
                continue
            prev = best.get(mid)
            if prev is None or sc > prev:
                best[mid] = sc

        candidates = [Candidate(item_id=k, score=v, source=self.name) for k, v in best.items()]
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:topk]


@dataclass(frozen=True)
class UserHighRatingSimilarRecall(Recaller):
    """从用户高评分影片出发，召回相似影片。

    依赖表：user_action, rec_similarity
    """

    rating_threshold: int = 8
    topk: int = 300

    @property
    def name(self) -> str:
        return "user_high_rating_similar"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        if ctx.user_id is None:
            return []

        topk = _clamp_positive(int(os.getenv("RECALL_TOPK_USER_HIGH_RATING") or self.topk), self.topk)
        thr = int(os.getenv("RECALL_RATING_THRESHOLD") or self.rating_threshold)

        seed_sql = """
        SELECT ua.movie_id
        FROM user_action ua
        WHERE ua.user_id = :user_id
          AND ua.action_type = 'rate'
          AND ua.rating IS NOT NULL
          AND ua.rating >= :thr
        ORDER BY ua.id DESC
        LIMIT 100
        """
        seeds = _execute(seed_sql, {"user_id": ctx.user_id, "thr": thr})
        seed_ids = [int(x["movie_id"]) for x in seeds if x.get("movie_id") is not None]
        if not seed_ids:
            return []

        sim_sql = """
        (
          SELECT rs.movie_b_id AS item_id, rs.similarity AS score
          FROM rec_similarity rs
          WHERE rs.movie_a_id IN :seed_ids
          ORDER BY rs.similarity DESC
          LIMIT :limit
        )
        UNION ALL
        (
          SELECT rs.movie_a_id AS item_id, rs.similarity AS score
          FROM rec_similarity rs
          WHERE rs.movie_b_id IN :seed_ids
          ORDER BY rs.similarity DESC
          LIMIT :limit
        )
        """
        rows = _execute(sim_sql, {"seed_ids": tuple(seed_ids), "limit": 1000})

        exclude_sql = """
        SELECT DISTINCT x.movie_id
        FROM (
          SELECT movie_id FROM user_collection WHERE user_id = :user_id
          UNION ALL
          SELECT movie_id FROM user_action WHERE user_id = :user_id
        ) x
        """
        exclude_rows = _execute(exclude_sql, {"user_id": ctx.user_id})
        excluded = {int(r["movie_id"]) for r in exclude_rows if r.get("movie_id") is not None}

        best: dict[int, float] = {}
        for r in rows:
            try:
                mid = int(r["item_id"])
                if mid in excluded:
                    continue
                sc = float(r.get("score") or 0.0)
            except Exception:
                continue
            prev = best.get(mid)
            if prev is None or sc > prev:
                best[mid] = sc

        candidates = [Candidate(item_id=k, score=v, source=self.name) for k, v in best.items()]
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:topk]


@dataclass(frozen=True)
class UserInterestTagRecall(Recaller):
    """基于用户兴趣标签召回（静态/动态标签）。

    依赖表：user_interest_tag, movie_tag_static, movie_tag_dynamic

    备注：
    - user_interest_tag.tag_id 的含义由 is_static 决定：
      - is_static=1 -> tag_static_dict.id，关联 movie_tag_static.tag_id
      - is_static=0 -> tag_dynamic_dict.id，关联 movie_tag_dynamic.tag_id（通常要求 tag_dynamic_dict.status='approved'）
    """

    topk: int = 300

    @property
    def name(self) -> str:
        return "user_interest_tag"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        if ctx.user_id is None:
            return []

        topk = _clamp_positive(int(os.getenv("RECALL_TOPK_USER_INTEREST_TAG") or self.topk), self.topk)

        # 静态标签召回：按兴趣权重累加
        static_sql = """
        SELECT mts.movie_id AS item_id, SUM(uit.weight) AS score
        FROM user_interest_tag uit
        JOIN movie_tag_static mts ON mts.tag_id = uit.tag_id
        WHERE uit.user_id = :user_id AND uit.is_static = 1
        GROUP BY mts.movie_id
        ORDER BY score DESC
        LIMIT :limit
        """

        # 动态标签召回：兴趣权重 * 影片动态标签权重
        dynamic_sql = """
        SELECT mtd.movie_id AS item_id, SUM(uit.weight * COALESCE(mtd.weight, 1.0)) AS score
        FROM user_interest_tag uit
        JOIN tag_dynamic_dict tdd ON tdd.id = uit.tag_id
        JOIN movie_tag_dynamic mtd ON mtd.tag_id = uit.tag_id
        WHERE uit.user_id = :user_id AND uit.is_static = 0
          AND tdd.status = 'approved'
        GROUP BY mtd.movie_id
        ORDER BY score DESC
        LIMIT :limit
        """

        static_rows = _execute(static_sql, {"user_id": ctx.user_id, "limit": topk})
        dynamic_rows = _execute(dynamic_sql, {"user_id": ctx.user_id, "limit": topk})

        # 排除已交互内容
        exclude_sql = """
        SELECT DISTINCT x.movie_id
        FROM (
          SELECT movie_id FROM user_collection WHERE user_id = :user_id
          UNION ALL
          SELECT movie_id FROM user_action WHERE user_id = :user_id
        ) x
        """
        exclude_rows = _execute(exclude_sql, {"user_id": ctx.user_id})
        excluded = {int(r["movie_id"]) for r in exclude_rows if r.get("movie_id") is not None}

        merged: dict[int, float] = {}
        for r in list(static_rows) + list(dynamic_rows):
            try:
                mid = int(r["item_id"])
                if mid in excluded:
                    continue
                sc = float(r.get("score") or 0.0)
            except Exception:
                continue
            merged[mid] = merged.get(mid, 0.0) + sc

        candidates = [Candidate(item_id=k, score=v, source=self.name) for k, v in merged.items()]
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:topk]


@dataclass(frozen=True)
class ItemSimilarByTagsRecall(Recaller):
    """给 /recommend/item 用的内容相似召回：按标签交集数量召回。

    依赖表：movie_tag_static, movie_tag_dynamic
    """

    topk: int = 200

    @property
    def name(self) -> str:
        return "item_similar_by_tags"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        if ctx.movie_id is None:
            return []

        topk = _clamp_positive(int(os.getenv("RECALL_TOPK_ITEM_SIMILAR_TAG") or self.topk), self.topk)

        # 静态标签交集
        sql_static = """
        SELECT mts2.movie_id AS item_id, COUNT(*) AS score
        FROM movie_tag_static mts1
        JOIN movie_tag_static mts2 ON mts2.tag_id = mts1.tag_id
        WHERE mts1.movie_id = :movie_id AND mts2.movie_id <> :movie_id
        GROUP BY mts2.movie_id
        ORDER BY score DESC
        LIMIT :limit
        """

        # 动态标签交集（只按 tag_id 交集计数；也可改为加权）
        sql_dynamic = """
        SELECT mtd2.movie_id AS item_id, SUM(LEAST(COALESCE(mtd1.weight, 1.0), COALESCE(mtd2.weight, 1.0))) AS score
        FROM movie_tag_dynamic mtd1
        JOIN movie_tag_dynamic mtd2 ON mtd2.tag_id = mtd1.tag_id
        WHERE mtd1.movie_id = :movie_id AND mtd2.movie_id <> :movie_id
        GROUP BY mtd2.movie_id
        ORDER BY score DESC
        LIMIT :limit
        """

        rows = _execute(sql_static, {"movie_id": ctx.movie_id, "limit": topk})
        rows2 = _execute(sql_dynamic, {"movie_id": ctx.movie_id, "limit": topk})

        merged: dict[int, float] = {}
        for r in list(rows) + list(rows2):
            try:
                mid = int(r["item_id"])
                sc = float(r.get("score") or 0.0)
            except Exception:
                continue
            merged[mid] = merged.get(mid, 0.0) + sc

        candidates = [Candidate(item_id=k, score=v, source=self.name) for k, v in merged.items()]
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:topk]
