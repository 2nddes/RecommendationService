from __future__ import annotations

from dataclasses import dataclass
import logging
from time import perf_counter
from typing import List

from app.common.redis_cache import load_tag_recall_items
from app.common.settings import Settings, TagRecallSettings
from app.reco.recall.base import Recaller
from app.reco.recall.two_tower.db import execute
from app.reco.recall.two_tower.features import fetch_user_excluded_items
from app.reco.types import Candidate, RequestContext


logger = logging.getLogger(__name__)


def _format_tag_preview(preference_tags: list[tuple[str, int, float]], *, limit: int = 5) -> list[str]:
    preview: list[str] = []
    for tag_type, tag_id, pref_weight in preference_tags[: max(int(limit), 0)]:
        preview.append(f"{tag_type}:{int(tag_id)}:{float(pref_weight):.2f}")
    return preview


def _fetch_user_genre_preferences(
    *,
    mysql_dsn: str | None,
    user_id: int,
    high_rating_threshold: int,
    limit: int,
) -> list[tuple[int, float]]:
    sql = """
    SELECT x.tag_id, SUM(x.pref_weight) AS pref_weight
    FROM (
        SELECT uct.tag_id, COUNT(*) AS pref_weight
        FROM user_collect_tag uct
        JOIN tag_dict td ON td.tag_id = uct.tag_id
        WHERE uct.user_id = :user_id AND td.status = 'show'
        GROUP BY uct.tag_id

        UNION ALL

        SELECT mt.tag_id, COUNT(*) AS pref_weight
        FROM rating r
        JOIN movie_tag mt ON mt.movie_id = r.movie_id
        JOIN tag_dict td ON td.tag_id = mt.tag_id
        WHERE r.user_id = :user_id
          AND r.rating >= :high_rating_threshold
          AND td.status = 'show'
        GROUP BY mt.tag_id
    ) x
    GROUP BY x.tag_id
    ORDER BY pref_weight DESC, x.tag_id DESC
    LIMIT :limit
    """
    rows = execute(
        mysql_dsn,
        sql,
        {
            "user_id": int(user_id),
            "high_rating_threshold": int(high_rating_threshold),
            "limit": int(limit),
        },
    )

    out: list[tuple[int, float]] = []
    for row in rows:
        out.append((int(row["tag_id"]), float(row.get("pref_weight") or 0.0)))
    return out


def _fetch_user_director_preferences(
    *,
    mysql_dsn: str | None,
    user_id: int,
    recent_limit: int,
    limit: int,
) -> list[tuple[int, float]]:
    if int(recent_limit) <= 0:
        return []

    sql = """
    SELECT mp.person_id AS director_id, COUNT(*) AS pref_weight
    FROM (
        SELECT z.movie_id
        FROM (
            SELECT uc.movie_id, uc.created_at AS ts
            FROM user_click uc
            WHERE uc.user_id = :user_id

            UNION ALL

            SELECT c.movie_id, c.created_at AS ts
            FROM user_collect_movie c
            WHERE c.user_id = :user_id

            UNION ALL

            SELECT r.movie_id, r.updated_at AS ts
            FROM rating r
            WHERE r.user_id = :user_id

            UNION ALL

            SELECT mc.movie_id, mc.created_at AS ts
            FROM movie_comment mc
            WHERE mc.user_id = :user_id
              AND mc.deleted_at IS NULL
        ) z
        WHERE z.movie_id IS NOT NULL
        ORDER BY z.ts DESC
        LIMIT :recent_limit
    ) recent
    JOIN movie_person mp ON mp.movie_id = recent.movie_id
    WHERE mp.person_role = 'director'
    GROUP BY mp.person_id
    ORDER BY pref_weight DESC, mp.person_id DESC
    LIMIT :limit
    """
    rows = execute(
        mysql_dsn,
        sql,
        {
            "user_id": int(user_id),
            "recent_limit": int(recent_limit),
            "limit": int(limit),
        },
    )

    out: list[tuple[int, float]] = []
    for row in rows:
        out.append((int(row["director_id"]), float(row.get("pref_weight") or 0.0)))
    return out


@dataclass(frozen=True)
class TagInvertedRecall(Recaller):
    cfg: TagRecallSettings
    settings: Settings
    mysql_dsn: str | None = None

    @property
    def name(self) -> str:
        return "tag_inverted"

    def _fetch_user_preference_tags(self, user_id: int) -> list[tuple[str, int, float]]:
        limit = max(int(self.cfg.user_topk_tags), 1)
        high_rating_threshold = max(int(self.cfg.high_rating_threshold), 1)

        genre_rows = _fetch_user_genre_preferences(
            mysql_dsn=self.mysql_dsn,
            user_id=int(user_id),
            high_rating_threshold=high_rating_threshold,
            limit=limit,
        )
        director_rows = _fetch_user_director_preferences(
            mysql_dsn=self.mysql_dsn,
            user_id=int(user_id),
            recent_limit=max(int(self.cfg.recent_interaction_limit), 0),
            limit=limit,
        )

        merged: list[tuple[str, int, float]] = []
        for tag_id, pref_weight in genre_rows:
            merged.append(("genre", int(tag_id), float(pref_weight)))
        for director_id, pref_weight in director_rows:
            merged.append(("director", int(director_id), float(pref_weight)))

        merged.sort(key=lambda x: (x[2], x[1]), reverse=True)
        return merged[:limit]

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        started = perf_counter()
        if ctx.user_id is None:
            logger.warning(
                "用户ID为空，tag倒排召回跳过，user_id=%s, movie_id=%s, n=%s",
                ctx.user_id,
                ctx.movie_id,
                ctx.n,
            )
            return []

        if not self.settings.redis.enabled:
            logger.info("Redis未开启，tag倒排召回跳过，user_id=%s", ctx.user_id)
            return []

        target_n = max(int(ctx.n), 0)
        if target_n <= 0:
            return []

        user_id = int(ctx.user_id)
        preference_started = perf_counter()
        preference_tags = self._fetch_user_preference_tags(user_id)
        preference_fetch_ms = (perf_counter() - preference_started) * 1000.0
        genre_tag_count = sum(1 for tag_type, _tag_id, _pref in preference_tags if tag_type == "genre")
        director_tag_count = sum(1 for tag_type, _tag_id, _pref in preference_tags if tag_type == "director")
        if not preference_tags:
            logger.info(
                "用户无有效标签偏好，tag倒排召回返回空，user_id=%s, requested_n=%s, preference_fetch_ms=%.2f, elapsed_ms=%.2f",
                user_id,
                target_n,
                preference_fetch_ms,
                (perf_counter() - started) * 1000.0,
            )
            return []

        tag_specs = [(tag_type, tag_id) for tag_type, tag_id, _pref in preference_tags]
        redis_started = perf_counter()
        rows_by_spec = load_tag_recall_items(
            self.settings,
            tag_specs=tag_specs,
            per_tag_topn=max(int(self.cfg.per_tag_fetch_m), 1),
        )
        redis_fetch_ms = (perf_counter() - redis_started) * 1000.0
        redis_hit_tags = sum(1 for rows in rows_by_spec.values() if rows)
        redis_rows = sum(len(rows) for rows in rows_by_spec.values())
        if logger.isEnabledFor(logging.DEBUG):
            missing_specs = [
                f"{tag_type}:{tag_id}"
                for tag_type, tag_id in tag_specs
                if not (rows_by_spec.get((tag_type, tag_id)) or [])
            ]
            logger.debug(
                "tag倒排召回偏好详情，user_id=%s, preference_preview=%s, missing_tag_specs=%s",
                user_id,
                _format_tag_preview(preference_tags),
                missing_specs[:10],
            )
        if not rows_by_spec or redis_hit_tags <= 0:
            logger.info(
                "Redis倒排索引未命中，tag倒排召回返回空，user_id=%s, requested_tags=%s, genre_tags=%s, director_tags=%s, redis_hit_tags=%s, redis_rows=%s, preference_fetch_ms=%.2f, redis_fetch_ms=%.2f, elapsed_ms=%.2f",
                user_id,
                len(tag_specs),
                genre_tag_count,
                director_tag_count,
                redis_hit_tags,
                redis_rows,
                preference_fetch_ms,
                redis_fetch_ms,
                (perf_counter() - started) * 1000.0,
            )
            return []

        excluded_started = perf_counter()
        excluded = fetch_user_excluded_items(
            user_id,
            mysql_dsn=self.mysql_dsn,
            recent_limit=max(int(self.cfg.recent_interaction_limit), 0),
        )
        excluded_fetch_ms = (perf_counter() - excluded_started) * 1000.0

        merged_scores: dict[int, float] = {}
        filtered_excluded = 0
        # 用户权重当前按决策固定为 1.0，仅用于保留后续扩展点。
        user_tag_weight = 1.0
        for tag_type, tag_id, _pref in preference_tags:
            spec = (str(tag_type), int(tag_id))
            rows = rows_by_spec.get(spec) or []
            for movie_id, zset_score in rows:
                mid = int(movie_id)
                if mid in excluded:
                    filtered_excluded += 1
                    continue
                merged_scores[mid] = merged_scores.get(mid, 0.0) + (user_tag_weight * float(zset_score))

        if not merged_scores:
            logger.info(
                "tag倒排召回合并后为空，user_id=%s, requested_tags=%s, genre_tags=%s, director_tags=%s, redis_hit_tags=%s, redis_rows=%s, excluded_count=%s, filtered_excluded=%s, preference_fetch_ms=%.2f, redis_fetch_ms=%.2f, excluded_fetch_ms=%.2f, elapsed_ms=%.2f",
                user_id,
                len(tag_specs),
                genre_tag_count,
                director_tag_count,
                redis_hit_tags,
                redis_rows,
                len(excluded),
                filtered_excluded,
                preference_fetch_ms,
                redis_fetch_ms,
                excluded_fetch_ms,
                (perf_counter() - started) * 1000.0,
            )
            return []

        multiplier = max(int(self.cfg.online_candidate_multiplier), 1)
        out_limit = max(target_n, target_n * multiplier)
        sorted_items = sorted(merged_scores.items(), key=lambda x: x[1], reverse=True)[:out_limit]
        logger.info(
            "tag倒排召回完成，user_id=%s, requested_n=%s, requested_tags=%s, genre_tags=%s, director_tags=%s, redis_hit_tags=%s, redis_rows=%s, excluded_count=%s, filtered_excluded=%s, merged_candidates=%s, returned_count=%s, out_limit=%s, preference_fetch_ms=%.2f, redis_fetch_ms=%.2f, excluded_fetch_ms=%.2f, elapsed_ms=%.2f",
            user_id,
            target_n,
            len(tag_specs),
            genre_tag_count,
            director_tag_count,
            redis_hit_tags,
            redis_rows,
            len(excluded),
            filtered_excluded,
            len(merged_scores),
            len(sorted_items),
            out_limit,
            preference_fetch_ms,
            redis_fetch_ms,
            excluded_fetch_ms,
            (perf_counter() - started) * 1000.0,
        )
        return [
            Candidate(item_id=int(movie_id), score=float(score), source=self.name)
            for movie_id, score in sorted_items
        ]
