from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
import threading
from time import perf_counter
from typing import Any, Dict, List, Sequence

import torch
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from torch import Tensor

from app.reco.ranking.base import Ranker
from app.reco.types import Candidate, RankedItem, RequestContext

from .features import (
    ITEM_TAG_SEQ_LEN,
    LONG_INTEREST_TAG_SEQ_LEN,
    SHORT_INTEREST_SEQ_LEN,
    bucketize_age,
    bundle_feature_order,
    default_age_bucket_index,
    default_gender_index,
    normalize_gender,
    pad_or_truncate,
)
from .model import MMoENet


logger = logging.getLogger(__name__)
_model_cache_lock = threading.RLock()
_model_cache: dict[str, tuple[float, Dict[str, Any], MMoENet]] = {}
_engine_cache_lock = threading.RLock()
_engine_cache: dict[str, Engine] = {}


def _get_mysql_engine(dsn: str) -> Engine:
    with _engine_cache_lock:
        cached = _engine_cache.get(dsn)
        if cached is not None:
            return cached
        engine = create_engine(dsn, pool_pre_ping=True)
        _engine_cache[dsn] = engine
        return engine


def _calc_age_from_birth(birth: Any) -> int | None:
    if birth is None:
        return None
    today = datetime.now(timezone.utc).date()
    return max(today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day)), 0)


@dataclass(frozen=True)
class MMoERanker(Ranker):
    model_path: str | None = None
    use_mysql_features: bool = True
    mysql_dsn: str | None = None

    @property
    def name(self) -> str:
        return "mmoe"

    def rank(self, ctx: RequestContext, candidates: List[Candidate]) -> List[RankedItem]:
        if not candidates:
            return []

        rank_start = perf_counter()

        if not self.model_path:
            raise RuntimeError("mmoe_model_path_is_empty")

        bundle, model = self._ranker_load_cached_bundle_and_model(self.model_path)

        feature_order = [str(x) for x in bundle.get("feature_order") or bundle_feature_order()]
        user_ids, item_ids, numeric_rows, gender_ids, age_bucket_ids, item_tag_ids, short_hist_ids, long_tag_ids = (
            self._ranker_build_infer_tensors(
                ctx=ctx,
                candidates=candidates,
                bundle=bundle,
                feature_order=feature_order,
                feature_stats=bundle.get("feature_stats") or {},
            )
        )
        tensor_ready_ms = (perf_counter() - rank_start) * 1000.0

        infer_start = perf_counter()
        with torch.no_grad():
            pred = model(
                user_ids,
                item_ids,
                numeric_rows,
                gender_idx=gender_ids,
                age_bucket_idx=age_bucket_ids,
                item_tag_ids=item_tag_ids,
                short_hist_item_ids=short_hist_ids,
                long_interest_tag_ids=long_tag_ids,
            )
        infer_ms = (perf_counter() - infer_start) * 1000.0

        p_click = pred["click"]
        p_collect = pred["collect"]
        p_comment = pred["comment"]
        p_rating = pred["rating"]
        score = (p_click + p_collect + p_comment + p_rating) / 4.0

        ranked = [
            RankedItem(item_id=int(c.item_id), score=float(s), reason="mmoe")
            for c, s in zip(candidates, score.tolist())
        ]
        sort_start = perf_counter()
        ranked.sort(key=lambda x: x.score, reverse=True)
        sort_ms = (perf_counter() - sort_start) * 1000.0
        total_ms = (perf_counter() - rank_start) * 1000.0
        model_version = os.path.basename(self.model_path or "")
        logger.info(
            "event=reco.rank.mmoe.done | user_id=%s | candidate_count=%s | model_version=%s | tensor_build_ms=%.2f | infer_ms=%.2f | sort_ms=%.2f | elapsed_ms=%.2f",
            ctx.user_id,
            len(candidates),
            model_version,
            tensor_ready_ms,
            infer_ms,
            sort_ms,
            total_ms,
        )
        return ranked

    def _ranker_load_cached_bundle_and_model(self, model_path: str) -> tuple[Dict[str, Any], MMoENet]:
        try:
            mtime = os.path.getmtime(model_path)
        except OSError as e:
            raise RuntimeError(f"mmoe_model_not_found: {model_path}") from e

        with _model_cache_lock:
            cached = _model_cache.get(model_path)
            if cached is not None and cached[0] == mtime:
                return cached[1], cached[2]

            bundle = torch.load(model_path, map_location="cpu")
            if not isinstance(bundle, dict):
                raise RuntimeError("mmoe_bundle_invalid")

            model = self._ranker_build_model_from_bundle(bundle)
            model.eval()
            _model_cache[model_path] = (mtime, bundle, model)
            return bundle, model

    def _ranker_build_model_from_bundle(self, bundle: Dict[str, Any]) -> MMoENet:
        model_meta = bundle["model_meta"]
        model = MMoENet(
            user_vocab_size=int(model_meta["user_vocab_size"]),
            item_vocab_size=int(model_meta["item_vocab_size"]),
            num_numeric_features=int(model_meta["num_numeric_features"]),
            emb_dim=int(model_meta["emb_dim"]),
            num_experts=int(model_meta["num_experts"]),
            expert_hidden_dim=int(model_meta["expert_hidden_dim"]),
            tower_hidden_dim=int(model_meta["tower_hidden_dim"]),
            gender_vocab_size=int(model_meta.get("gender_vocab_size", 0)),
            age_bucket_vocab_size=int(model_meta.get("age_bucket_vocab_size", 0)),
            tag_vocab_size=int(model_meta.get("tag_vocab_size", 0)),
            use_item_tag_pooling=bool(model_meta.get("use_item_tag_pooling", False)),
            use_target_attention=bool(model_meta.get("use_target_attention", False)),
            use_long_interest_pooling=bool(model_meta.get("use_long_interest_pooling", False)),
        )
        model.load_state_dict(bundle["state_dict"])
        return model

    def _ranker_build_infer_tensors(
        self,
        *,
        ctx: RequestContext,
        candidates: Sequence[Candidate],
        bundle: Dict[str, Any],
        feature_order: Sequence[str],
        feature_stats: Dict[str, Dict[str, float]],
    ) -> tuple[Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor, Tensor]:
        rows: List[List[float]] = []
        user_idx: List[int] = []
        item_idx: List[int] = []
        gender_idx_rows: List[int] = []
        age_bucket_idx_rows: List[int] = []
        item_tag_rows: List[List[int]] = []
        short_hist_rows: List[List[int]] = []
        long_tag_rows: List[List[int]] = []

        uid = int(ctx.user_id) if ctx.user_id is not None else 0
        user_index = bundle.get("user_index") or {}
        item_index = bundle.get("item_index") or {}
        gender_index = bundle.get("gender_index") or default_gender_index()
        age_bucket_index = bundle.get("age_bucket_index") or default_age_bucket_index()
        tag_index = bundle.get("tag_index") or {}

        movie_ids = [int(c.item_id) for c in candidates]
        aux = self._fetch_infer_feature_aux(uid=uid, movie_ids=movie_ids)
        fallback_reasons = [str(x) for x in (aux.get("_fallback_reasons") or [])]
        if fallback_reasons:
            logger.warning(
                "event=reco.rank.mmoe.fallback_features | user_id=%s | candidate_count=%s | reason_count=%s | reasons=%s",
                uid,
                len(candidates),
                len(fallback_reasons),
                "; ".join(fallback_reasons),
            )

        user_profile = aux.get("user_profile") or {}
        user_gender = normalize_gender(str(user_profile.get("gender") or "unknown"))
        user_age = _calc_age_from_birth(user_profile.get("birth"))
        user_age_bucket = bucketize_age(user_age)

        short_hist_movie_ids = [
            int(item_index.get(mid, 0))
            for mid in (aux.get("short_hist_movie_ids") or [])
            if int(item_index.get(mid, 0)) > 0
        ]
        short_hist_seq = pad_or_truncate(short_hist_movie_ids, size=SHORT_INTEREST_SEQ_LEN)

        long_interest_tag_ids = [
            int(tag_index.get(int(tag_id), 0))
            for tag_id in (aux.get("long_interest_tag_ids") or [])
            if int(tag_index.get(int(tag_id), 0)) > 0
        ]
        long_interest_tag_seq_default = pad_or_truncate(long_interest_tag_ids, size=LONG_INTEREST_TAG_SEQ_LEN)

        src_names = [
            "src_user_collection",
            "src_user_high_rating_similar",
            "src_user_interest_tag",
            "src_item_similar_by_tags",
            "src_two_tower",
        ]

        uid_idx = int(user_index.get(uid, 0))

        movie_stats_by_id = aux.get("movie_stats_by_id") or {}
        item_static_tags_by_movie = aux.get("item_static_tags_by_movie") or {}
        user_clicked_static_tag_count = aux.get("user_clicked_static_tag_count") or {}
        user_total_click = int(aux.get("user_total_click") or 0)
        has_click_history = user_total_click > 0
        padded_long_interest_cnt = 0
        missing_movie_stats_cnt = 0

        for c in candidates:
            mid = int(c.item_id)
            item_idx.append(int(item_index.get(mid, 0)))
            user_idx.append(uid_idx)

            item_tags = item_static_tags_by_movie.get(mid) or []
            item_tag_indices = [
                int(tag_index.get(int(tag_id), 0))
                for tag_id in item_tags
                if int(tag_index.get(int(tag_id), 0)) > 0
            ]
            item_tag_seq = pad_or_truncate(item_tag_indices, size=ITEM_TAG_SEQ_LEN)
            long_interest_tag_seq = list(long_interest_tag_seq_default)
            if not any(long_interest_tag_seq):
                # Missing sequence is represented by PAD and masked in model pooling.
                padded_long_interest_cnt += 1

            if has_click_history and item_tags:
                ctr_vals = [
                    float(user_clicked_static_tag_count.get(int(tag_id), 0)) / float(user_total_click)
                    for tag_id in item_tags
                ]
                user_static_tag_ctr = sum(ctr_vals) / float(len(ctr_vals))
            else:
                user_static_tag_ctr = 0.0

            movie_f = movie_stats_by_id.get(mid) or {}
            if not movie_f:
                missing_movie_stats_cnt += 1
            one_hot = [1.0 if str(c.source) == s.replace("src_", "") else 0.0 for s in src_names]
            raw = {
                "recall_score": float(c.score),
                "movie_rating_avg": float(movie_f.get("rating_avg", 0.0)),
                "movie_rating_count": float(movie_f.get("rating_count", 0.0)),
                "movie_comment_count": float(movie_f.get("comment_count", 0.0)),
                "movie_click_count": float(movie_f.get("click_count", 0.0)),
                "movie_click_1h": float(movie_f.get("click_1h", 0.0)),
                "movie_click_24h": float(movie_f.get("click_24h", 0.0)),
                "movie_year": float(movie_f.get("year", 0.0)),
                "movie_duration_min": float(movie_f.get("duration_min", 0.0)),
                "user_static_tag_ctr": float(user_static_tag_ctr),
                **{k: v for k, v in zip(src_names, one_hot)},
            }

            row = []
            for name in feature_order:
                stats = feature_stats.get(name, {"mean": 0.0, "std": 1.0})
                mean = float(stats.get("mean", 0.0))
                std = float(stats.get("std", 1.0))
                std = std if abs(std) > 1e-8 else 1.0
                val = float(raw.get(name, mean))
                row.append((val - mean) / std)
            rows.append(row)

            gender_idx_rows.append(int(gender_index.get(user_gender, gender_index.get("unknown", 1))))
            age_bucket_idx_rows.append(int(age_bucket_index.get(user_age_bucket, age_bucket_index.get("unknown", 1))))
            item_tag_rows.append(item_tag_seq)
            short_hist_rows.append(short_hist_seq)
            long_tag_rows.append(long_interest_tag_seq)

        if missing_movie_stats_cnt > 0:
            logger.warning(
                "MMoE inference filled missing movie stats with default values. missing=%s total=%s user_id=%s",
                missing_movie_stats_cnt,
                len(candidates),
                uid,
            )
        if padded_long_interest_cnt > 0:
            logger.warning(
                "event=reco.rank.mmoe.long_interest_missing | user_id=%s | padded=%s | total=%s | padded_ratio=%.4f",
                uid,
                padded_long_interest_cnt,
                len(candidates),
                float(padded_long_interest_cnt) / max(float(len(candidates)), 1.0),
            )

        return (
            torch.tensor(user_idx, dtype=torch.long),
            torch.tensor(item_idx, dtype=torch.long),
            torch.tensor(rows, dtype=torch.float32),
            torch.tensor(gender_idx_rows, dtype=torch.long),
            torch.tensor(age_bucket_idx_rows, dtype=torch.long),
            torch.tensor(item_tag_rows, dtype=torch.long),
            torch.tensor(short_hist_rows, dtype=torch.long),
            torch.tensor(long_tag_rows, dtype=torch.long),
        )

    def _fetch_infer_feature_aux(self, *, uid: int, movie_ids: Sequence[int]) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "user_profile": {},
            "movie_stats_by_id": {},
            "item_static_tags_by_movie": {},
            "short_hist_movie_ids": [],
            "long_interest_tag_ids": [],
            "user_clicked_static_tag_count": {},
            "user_total_click": 0,
            "_fallback_reasons": [],
        }

        if not self.use_mysql_features:
            out["_fallback_reasons"].append("XGB/MMoE MySQL features disabled by config")
            return out
        dsn = str(self.mysql_dsn or "").strip()
        if not dsn:
            out["_fallback_reasons"].append("MYSQL_DSN is empty")
            return out

        mids = [int(x) for x in movie_ids if int(x) > 0]
        if not mids:
            out["_fallback_reasons"].append("candidate movie_ids is empty")
            return out

        mids = list(dict.fromkeys(mids))

        engine = _get_mysql_engine(dsn)

        movie_stat_sql = text(
            """
            SELECT m.movie_id AS movie_id,
                   CASE WHEN m.rating_count > 0 THEN (m.rating_sum * 1.0 / m.rating_count) ELSE 0 END AS rating_avg,
                   m.rating_count,
                   m.year,
                   m.duration_min,
                   COALESCE(mc.comment_count, 0) AS comment_count,
                   COALESCE(ua_all.click_count, 0) AS click_count,
                   COALESCE(ua_1h.click_1h, 0) AS click_1h,
                   COALESCE(ua_24h.click_24h, 0) AS click_24h
            FROM movie m
            LEFT JOIN (
                SELECT movie_id, COUNT(*) AS comment_count
                FROM movie_comment
                WHERE deleted_at IS NULL
                  AND movie_id IN :ids
                GROUP BY movie_id
            ) mc ON mc.movie_id = m.movie_id
            LEFT JOIN (
                SELECT movie_id, COUNT(*) AS click_count
                FROM user_click
                WHERE movie_id IN :ids
                GROUP BY movie_id
            ) ua_all ON ua_all.movie_id = m.movie_id
            LEFT JOIN (
                SELECT movie_id, COUNT(*) AS click_1h
                FROM user_click
                WHERE created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 HOUR)
                  AND movie_id IN :ids
                GROUP BY movie_id
            ) ua_1h ON ua_1h.movie_id = m.movie_id
            LEFT JOIN (
                SELECT movie_id, COUNT(*) AS click_24h
                FROM user_click
                WHERE created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 24 HOUR)
                  AND movie_id IN :ids
                GROUP BY movie_id
            ) ua_24h ON ua_24h.movie_id = m.movie_id
            WHERE m.movie_id IN :ids
            """
        ).bindparams(bindparam("ids", expanding=True))

        item_static_tags_sql = text(
            """
            SELECT mt.movie_id, mt.tag_id
            FROM movie_tag mt
            JOIN tag_dict td ON td.tag_id = mt.tag_id
            WHERE mt.movie_id IN :ids
              AND td.type = 'static'
              AND td.status = 'show'
            ORDER BY mt.movie_id ASC, mt.weight DESC, mt.hot_score DESC, mt.tag_id DESC
            """
        ).bindparams(bindparam("ids", expanding=True))

        user_profile_sql = text(
            """
            SELECT u.gender, u.birth
            FROM user u
            WHERE u.user_id = :uid
              AND u.deleted_at IS NULL
              AND u.status = 'active'
            LIMIT 1
            """
        )

        short_hist_sql = text(
            """
            SELECT t.movie_id
            FROM (
                SELECT uc.movie_id,
                       ROW_NUMBER() OVER (
                           ORDER BY uc.created_at DESC, uc.movie_id DESC
                       ) AS rn
                FROM user_click uc
                WHERE uc.user_id = :uid
            ) t
            WHERE t.rn <= :lim
            ORDER BY t.rn ASC
            """
        )

        long_interest_sql = text(
            """
            SELECT y.tag_id
            FROM (
                SELECT x.tag_id,
                       MAX(x.priority) AS max_priority,
                       MAX(x.event_time) AS last_event_time
                FROM (
                    SELECT uct.tag_id,
                           3 AS priority,
                           uct.created_at AS event_time
                    FROM user_collect_tag uct
                    JOIN tag_dict td
                      ON td.tag_id = uct.tag_id
                    WHERE uct.user_id = :uid
                      AND uct.is_static = 1
                      AND td.type = 'static'
                      AND td.status = 'show'

                    UNION ALL

                    SELECT mt.tag_id,
                           2 AS priority,
                           r.updated_at AS event_time
                    FROM rating r
                    JOIN movie_tag mt ON mt.movie_id = r.movie_id
                    JOIN tag_dict td ON td.tag_id = mt.tag_id
                    WHERE r.user_id = :uid
                      AND r.rating >= 8
                      AND td.type = 'static'
                      AND td.status = 'show'
                ) x
                GROUP BY x.tag_id
            ) y
            ORDER BY y.max_priority DESC, y.last_event_time DESC, y.tag_id DESC
            LIMIT :lim
            """
        )

        user_click_tag_sql = text(
            """
            SELECT x.tag_id, COUNT(*) AS click_cnt
            FROM (
                SELECT mt.tag_id
                                FROM user_click uc
                                JOIN movie_tag mt ON mt.movie_id = uc.movie_id
                JOIN tag_dict td ON td.tag_id = mt.tag_id
                                WHERE uc.user_id = :uid
                  AND td.type = 'static'
                  AND td.status = 'show'
            ) x
            GROUP BY x.tag_id
            """
        )

        user_click_total_sql = text(
            """
            SELECT COUNT(*) AS total_click
                        FROM user_click uc
                        WHERE uc.user_id = :uid
            """
        )

        aux_start = perf_counter()
        query_ms: Dict[str, float] = {}

        with engine.connect() as conn:
            t0 = perf_counter()
            rs = conn.execute(movie_stat_sql, {"ids": mids})
            movie_stats_by_id: Dict[int, Dict[str, Any]] = {}
            for row in rs:
                d = dict(row._mapping)
                mid = int(d["movie_id"])
                if mid > 0:
                    movie_stats_by_id[mid] = d
            out["movie_stats_by_id"] = movie_stats_by_id
            query_ms["movie_stats"] = (perf_counter() - t0) * 1000.0

            t0 = perf_counter()
            rs = conn.execute(item_static_tags_sql, {"ids": mids})
            item_static_tags_by_movie: Dict[int, List[int]] = {}
            for row in rs:
                d = dict(row._mapping)
                mid = int(d["movie_id"])
                tag_id = int(d["tag_id"])
                item_static_tags_by_movie.setdefault(mid, []).append(tag_id)
            out["item_static_tags_by_movie"] = item_static_tags_by_movie
            query_ms["item_static_tags"] = (perf_counter() - t0) * 1000.0

            if uid > 0:
                t0 = perf_counter()
                one = conn.execute(user_profile_sql, {"uid": uid}).first()
                if one is not None:
                    out["user_profile"] = dict(one._mapping)
                query_ms["user_profile"] = (perf_counter() - t0) * 1000.0

                t0 = perf_counter()
                rs = conn.execute(short_hist_sql, {"uid": uid, "lim": int(SHORT_INTEREST_SEQ_LEN)})
                out["short_hist_movie_ids"] = [int(dict(r._mapping)["movie_id"]) for r in rs if int(dict(r._mapping)["movie_id"]) > 0]
                query_ms["short_hist"] = (perf_counter() - t0) * 1000.0

                t0 = perf_counter()
                rs = conn.execute(long_interest_sql, {"uid": uid, "lim": int(LONG_INTEREST_TAG_SEQ_LEN)})
                out["long_interest_tag_ids"] = [int(dict(r._mapping)["tag_id"]) for r in rs if int(dict(r._mapping)["tag_id"]) > 0]
                query_ms["long_interest"] = (perf_counter() - t0) * 1000.0

                t0 = perf_counter()
                rs = conn.execute(user_click_tag_sql, {"uid": uid})
                out["user_clicked_static_tag_count"] = {
                    int(dict(r._mapping)["tag_id"]): int(dict(r._mapping)["click_cnt"])
                    for r in rs
                    if int(dict(r._mapping)["tag_id"]) > 0
                }
                query_ms["user_click_tag"] = (perf_counter() - t0) * 1000.0

                t0 = perf_counter()
                one = conn.execute(user_click_total_sql, {"uid": uid}).first()
                if one is not None:
                    out["user_total_click"] = int(dict(one._mapping)["total_click"])
                query_ms["user_click_total"] = (perf_counter() - t0) * 1000.0

        logger.info(
            "event=reco.rank.mmoe.aux_query_summary | user_id=%s | candidate_movie_count=%s | movie_stats_rows=%s | item_static_tag_rows=%s | user_profile_found=%s | short_hist_len=%s | long_interest_len=%s | movie_stats_ms=%.2f | item_static_tags_ms=%.2f | user_profile_ms=%.2f | short_hist_ms=%.2f | long_interest_ms=%.2f | user_click_tag_ms=%.2f | user_click_total_ms=%.2f | elapsed_ms=%.2f",
            uid,
            len(mids),
            len(out.get("movie_stats_by_id") or {}),
            sum(len(v) for v in (out.get("item_static_tags_by_movie") or {}).values()),
            bool(out.get("user_profile")),
            len(out.get("short_hist_movie_ids") or []),
            len(out.get("long_interest_tag_ids") or []),
            query_ms.get("movie_stats", 0.0),
            query_ms.get("item_static_tags", 0.0),
            query_ms.get("user_profile", 0.0),
            query_ms.get("short_hist", 0.0),
            query_ms.get("long_interest", 0.0),
            query_ms.get("user_click_tag", 0.0),
            query_ms.get("user_click_total", 0.0),
            (perf_counter() - aux_start) * 1000.0,
        )

        if not out.get("movie_stats_by_id"):
            out["_fallback_reasons"].append("movie stats query returned empty")
        if uid > 0 and not out.get("user_profile"):
            out["_fallback_reasons"].append("user profile missing")
        if uid > 0 and not out.get("short_hist_movie_ids"):
            out["_fallback_reasons"].append("short-term behavior sequence missing")
        if uid > 0 and not out.get("long_interest_tag_ids"):
            out["_fallback_reasons"].append("long-term high-rating tag sequence missing")

        return out
