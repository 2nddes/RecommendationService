from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Sequence

import torch
from sqlalchemy import bindparam, create_engine, text
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
    safe_float,
)
from .model import MMoENet


logger = logging.getLogger(__name__)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _calc_age_from_birth(birth: Any) -> int | None:
    if birth is None:
        return None
    try:
        today = datetime.now(timezone.utc).date()
        return max(today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day)), 0)
    except Exception:
        return None


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

        if not self.model_path:
            raise RuntimeError("mmoe_model_path_is_empty")

        bundle = torch.load(self.model_path, map_location="cpu")
        model = self._ranker_build_model_from_bundle(bundle)
        model.eval()

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

        p_click = pred["click"]
        p_collect = pred["collect"]
        p_comment = pred["comment"]
        p_rating = pred["rating"]
        score = (p_click + p_collect + p_comment + p_rating) / 4.0

        ranked = [
            RankedItem(item_id=int(c.item_id), score=float(s), reason="mmoe")
            for c, s in zip(candidates, score.tolist())
        ]
        ranked.sort(key=lambda x: x.score, reverse=True)
        return ranked

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
                "MMoE inference uses fallback/default features. reasons=%s user_id=%s candidate_count=%s",
                "; ".join(fallback_reasons),
                uid,
                len(candidates),
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

            ctr_vals = [float(user_clicked_static_tag_count.get(int(tag_id), 0)) / float(user_total_click) for tag_id in item_tags]
            user_static_tag_ctr = sum(ctr_vals) / float(len(ctr_vals)) if ctr_vals else 0.0

            movie_f = movie_stats_by_id.get(mid) or {}
            if not movie_f:
                missing_movie_stats_cnt += 1
            one_hot = [1.0 if str(c.source) == s.replace("src_", "") else 0.0 for s in src_names]
            raw = {
                "recall_score": safe_float(c.score),
                "movie_rating_avg": safe_float(movie_f.get("rating_avg"), 0.0),
                "movie_rating_count": safe_float(movie_f.get("rating_count"), 0.0),
                "movie_comment_count": safe_float(movie_f.get("comment_count"), 0.0),
                "movie_click_count": safe_float(movie_f.get("click_count"), 0.0),
                "movie_click_1h": safe_float(movie_f.get("click_1h"), 0.0),
                "movie_click_24h": safe_float(movie_f.get("click_24h"), 0.0),
                "movie_year": safe_float(movie_f.get("year"), 0.0),
                "movie_duration_min": safe_float(movie_f.get("duration_min"), 0.0),
                "user_static_tag_ctr": safe_float(user_static_tag_ctr, 0.0),
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
                "MMoE inference uses PAD+mask for missing long-interest sequence. padded=%s total=%s user_id=%s",
                padded_long_interest_cnt,
                len(candidates),
                uid,
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

        try:
            engine = create_engine(dsn, pool_pre_ping=True)
        except Exception:
            out["_fallback_reasons"].append("failed to create MySQL engine")
            return out

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
                GROUP BY movie_id
            ) mc ON mc.movie_id = m.movie_id
            LEFT JOIN (
                SELECT movie_id, COUNT(*) AS click_count
                FROM user_action
                WHERE action_type = 'view'
                GROUP BY movie_id
            ) ua_all ON ua_all.movie_id = m.movie_id
            LEFT JOIN (
                SELECT movie_id, COUNT(*) AS click_1h
                FROM user_action
                WHERE action_type = 'view' AND created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 HOUR)
                GROUP BY movie_id
            ) ua_1h ON ua_1h.movie_id = m.movie_id
            LEFT JOIN (
                SELECT movie_id, COUNT(*) AS click_24h
                FROM user_action
                WHERE action_type = 'view' AND created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 24 HOUR)
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
            WHERE mt.movie_id IN :ids AND td.type = 'static'
            """
        ).bindparams(bindparam("ids", expanding=True))

        user_profile_sql = text(
            """
            SELECT u.gender, u.birth
            FROM user u
            WHERE u.user_id = :uid AND u.deleted_at IS NULL
            LIMIT 1
            """
        )

        short_hist_sql = text(
            """
            SELECT ua.movie_id
            FROM user_action ua
            WHERE ua.user_id = :uid AND ua.action_type = 'view'
            ORDER BY ua.created_at DESC
            LIMIT :lim
            """
        )

        long_interest_sql = text(
            """
            SELECT mt.tag_id
            FROM rating r
            JOIN movie_tag mt ON mt.movie_id = r.movie_id
            JOIN tag_dict td ON td.tag_id = mt.tag_id
            WHERE r.user_id = :uid AND r.rating >= 8 AND td.type = 'static'
            ORDER BY r.updated_at DESC
            LIMIT :lim
            """
        )

        user_click_tag_sql = text(
            """
            SELECT mt.tag_id, COUNT(*) AS click_cnt
            FROM user_action ua
            JOIN movie_tag mt ON mt.movie_id = ua.movie_id
            JOIN tag_dict td ON td.tag_id = mt.tag_id
            WHERE ua.user_id = :uid AND ua.action_type = 'view' AND td.type = 'static'
            GROUP BY mt.tag_id
            """
        )

        user_click_total_sql = text(
            """
            SELECT COUNT(*) AS total_click
            FROM user_action ua
            WHERE ua.user_id = :uid AND ua.action_type = 'view'
            """
        )

        try:
            with engine.connect() as conn:
                rs = conn.execute(movie_stat_sql, {"ids": mids})
                movie_stats_by_id: Dict[int, Dict[str, Any]] = {}
                for row in rs:
                    d = dict(row._mapping)
                    mid = _safe_int(d.get("movie_id"), 0)
                    if mid > 0:
                        movie_stats_by_id[mid] = d
                out["movie_stats_by_id"] = movie_stats_by_id

                rs = conn.execute(item_static_tags_sql, {"ids": mids})
                item_static_tags_by_movie: Dict[int, List[int]] = {}
                for row in rs:
                    d = dict(row._mapping)
                    mid = _safe_int(d.get("movie_id"), 0)
                    tag_id = _safe_int(d.get("tag_id"), 0)
                    if mid <= 0 or tag_id <= 0:
                        continue
                    item_static_tags_by_movie.setdefault(mid, []).append(tag_id)
                out["item_static_tags_by_movie"] = item_static_tags_by_movie

                if uid > 0:
                    one = conn.execute(user_profile_sql, {"uid": uid}).first()
                    if one is not None:
                        out["user_profile"] = dict(one._mapping)

                    rs = conn.execute(short_hist_sql, {"uid": uid, "lim": int(SHORT_INTEREST_SEQ_LEN)})
                    out["short_hist_movie_ids"] = [
                        _safe_int(dict(r._mapping).get("movie_id"), 0) for r in rs if _safe_int(dict(r._mapping).get("movie_id"), 0) > 0
                    ]

                    rs = conn.execute(long_interest_sql, {"uid": uid, "lim": int(LONG_INTEREST_TAG_SEQ_LEN)})
                    out["long_interest_tag_ids"] = [
                        _safe_int(dict(r._mapping).get("tag_id"), 0) for r in rs if _safe_int(dict(r._mapping).get("tag_id"), 0) > 0
                    ]

                    rs = conn.execute(user_click_tag_sql, {"uid": uid})
                    out["user_clicked_static_tag_count"] = {
                        _safe_int(dict(r._mapping).get("tag_id"), 0): _safe_int(dict(r._mapping).get("click_cnt"), 0)
                        for r in rs
                        if _safe_int(dict(r._mapping).get("tag_id"), 0) > 0
                    }

                    one = conn.execute(user_click_total_sql, {"uid": uid}).first()
                    if one is not None:
                        out["user_total_click"] = _safe_int(dict(one._mapping).get("total_click"), 0)
        except SQLAlchemyError:
            out["_fallback_reasons"].append("failed to query MySQL auxiliary features")
            return out

        if not out.get("movie_stats_by_id"):
            out["_fallback_reasons"].append("movie stats query returned empty")
        if uid > 0 and not out.get("user_profile"):
            out["_fallback_reasons"].append("user profile missing")
        if uid > 0 and not out.get("short_hist_movie_ids"):
            out["_fallback_reasons"].append("short-term behavior sequence missing")
        if uid > 0 and not out.get("long_interest_tag_ids"):
            out["_fallback_reasons"].append("long-term high-rating tag sequence missing")

        return out
