from __future__ import annotations

from datetime import datetime
import logging
import os
import time
from typing import Any, Dict, List, Sequence, Tuple

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from app.common.settings import Settings
from app.ops.artifact_store import get_artifact_store
from app.reco.training.common import (
    binary_auc,
    catch_and_reraise,
    get_mysql_engine,
    group_train_test_split_indices,
    log_event,
    log_exception,
    simple_train_test_split_indices,
)


logger = logging.getLogger(__name__)


def _safe_binary_auc(*, task_name: str, y_true: Sequence[float], y_score: Sequence[float]) -> float | None:
    pos_count = sum(1 for y in y_true if y > 0.5)
    neg_count = len(y_true) - pos_count
    if pos_count == 0 or neg_count == 0:
        log_event(
            logger,
            "warning",
            "train.mmoe.eval_auc_skipped",
            negative=neg_count,
            positive=pos_count,
            reason="single_class_test_labels",
            stage="evaluate",
            task=task_name,
        )
        return None
    return binary_auc(y_true, y_score)


def _chunk_values(values: Sequence[int], *, chunk_size: int) -> List[List[int]]:
    uniq = [v for v in (int(x) for x in values) if v > 0]
    return [uniq[i : i + chunk_size] for i in range(0, len(uniq), chunk_size)]


def _fetch_mmoe_training_rows(*, settings: Settings) -> List[Dict[str, Any]]:
    try:
        import numpy as np
    except Exception as e:
        log_exception(logger, "train.mmoe.deps_failed", e, stage="dataset")
        raise RuntimeError(f"mmoe_dataset_dependency_failed: {type(e).__name__}: {e}") from e

    engine = get_mysql_engine(settings.core.mysql_dsn, logger=logger, event_prefix="train.mmoe.mysql_engine")
    limit_n = settings.mmoe.train_limit
    neg_ratio = settings.mmoe.global_neg_ratio
    dataset_seed = 20260402

    sql_click_recent = text(
        """
        SELECT ua.user_id, ua.movie_id, ua.created_at AS event_time
        FROM user_action ua
        WHERE ua.movie_id IS NOT NULL
          AND ua.action_type = 'view'
        ORDER BY ua.created_at DESC
        LIMIT :limit
        """
    )
    sql_collect_recent = text(
        """
        SELECT c.user_id, c.movie_id, c.created_at AS event_time
        FROM user_collect_movie c
        WHERE c.movie_id IS NOT NULL
        ORDER BY c.created_at DESC
        LIMIT :limit
        """
    )
    sql_comment_recent = text(
        """
        SELECT mc.user_id, mc.movie_id, mc.created_at AS event_time
        FROM movie_comment mc
        WHERE mc.movie_id IS NOT NULL
          AND mc.deleted_at IS NULL
        ORDER BY mc.created_at DESC
        LIMIT :limit
        """
    )
    sql_rating_recent = text(
        """
        SELECT r.user_id, r.movie_id, r.rating, r.updated_at AS event_time
        FROM rating r
        WHERE r.movie_id IS NOT NULL
          AND r.rating IS NOT NULL
        ORDER BY r.updated_at DESC
        LIMIT :limit
        """
    )

    def _new_sample(*, uid: int, mid: int) -> Dict[str, Any]:
        return {
            "user_id": uid,
            "movie_id": mid,
            "click": 0.0,
            "collect": 0.0,
            "comment": 0.0,
            "rating": 0.0,
            "source": "recent_interaction",
            "recall_score": 0.0,
            "_event_time": None,
            "_source_priority": 0,
        }

    def _upgrade_source(sample: Dict[str, Any], *, source: str, recall_score: float, priority: int, event_time: Any) -> None:
        prev_time = sample.get("_event_time")
        prev_priority = sample.get("_source_priority", 0)
        should_refresh = priority > prev_priority or (
            priority == prev_priority
            and event_time is not None
            and (prev_time is None or event_time >= prev_time)
        )
        if should_refresh:
            sample["source"] = source
            sample["recall_score"] = recall_score
            sample["_source_priority"] = priority
            sample["_event_time"] = event_time

    def _parse_uid_mid_event(row: Any) -> tuple[int, int, Any]:
        m = row._mapping
        with catch_and_reraise(
            logger,
            "train.mmoe.row_parse_failed",
            "mmoe_training_row_parse_failed",
            stage="dataset",
        ):
            return int(m.get("user_id") or 0), int(m.get("movie_id") or 0), m.get("event_time")

    def _parse_uid_mid_rating_event(row: Any) -> tuple[int, int, int, Any]:
        m = row._mapping
        with catch_and_reraise(
            logger,
            "train.mmoe.row_parse_failed",
            "mmoe_training_row_parse_failed",
            stage="dataset",
        ):
            return int(m.get("user_id") or 0), int(m.get("movie_id") or 0), int(m.get("rating") or 0), m.get("event_time")

    by_pair: Dict[Tuple[int, int], Dict[str, Any]] = {}
    target_fetch_counts = {"click": 0, "collect": 0, "comment": 0, "rating": 0}
    try:
        with engine.connect() as conn:
            rs = conn.execute(sql_click_recent, {"limit": limit_n})
            for row in rs:
                uid, mid, event_time = _parse_uid_mid_event(row)
                if uid <= 0 or mid <= 0:
                    continue
                target_fetch_counts["click"] += 1
                sample = by_pair.setdefault((uid, mid), _new_sample(uid=uid, mid=mid))
                sample["click"] = 1.0
                _upgrade_source(sample, source="recent_interaction", recall_score=0.6, priority=1, event_time=event_time)

            rs = conn.execute(sql_collect_recent, {"limit": limit_n})
            for row in rs:
                uid, mid, event_time = _parse_uid_mid_event(row)
                if uid <= 0 or mid <= 0:
                    continue
                target_fetch_counts["collect"] += 1
                sample = by_pair.setdefault((uid, mid), _new_sample(uid=uid, mid=mid))
                sample["click"] = 1.0
                sample["collect"] = 1.0
                _upgrade_source(sample, source="user_collection", recall_score=1.0, priority=4, event_time=event_time)

            rs = conn.execute(sql_comment_recent, {"limit": limit_n})
            for row in rs:
                uid, mid, event_time = _parse_uid_mid_event(row)
                if uid <= 0 or mid <= 0:
                    continue
                target_fetch_counts["comment"] += 1
                sample = by_pair.setdefault((uid, mid), _new_sample(uid=uid, mid=mid))
                sample["click"] = 1.0
                sample["comment"] = 1.0
                _upgrade_source(sample, source="recent_interaction", recall_score=0.9, priority=3, event_time=event_time)

            rs = conn.execute(sql_rating_recent, {"limit": limit_n})
            for row in rs:
                uid, mid, rating_val, event_time = _parse_uid_mid_rating_event(row)
                if uid <= 0 or mid <= 0:
                    continue
                target_fetch_counts["rating"] += 1
                sample = by_pair.setdefault((uid, mid), _new_sample(uid=uid, mid=mid))
                sample["click"] = 1.0
                if rating_val >= 8:
                    sample["rating"] = 1.0
                    _upgrade_source(sample, source="user_high_rating_similar", recall_score=1.0, priority=5, event_time=event_time)
                else:
                    _upgrade_source(sample, source="recent_interaction", recall_score=0.7, priority=2, event_time=event_time)
    except SQLAlchemyError as e:
        log_exception(logger, "train.mmoe.fetch_rows_failed", e, stage="dataset", train_limit=limit_n)
        raise RuntimeError(f"mmoe_fetch_training_rows_failed: {type(e).__name__}: {e}") from e

    log_event(
        logger,
        "info",
        "train.mmoe.target_rows_fetched",
        stage="dataset",
        click_rows=target_fetch_counts["click"],
        collect_rows=target_fetch_counts["collect"],
        comment_rows=target_fetch_counts["comment"],
        rating_rows=target_fetch_counts["rating"],
        train_limit=limit_n,
    )

    positives = list(by_pair.values())
    if not positives:
        err = RuntimeError("mmoe_training_rows_empty")
        log_exception(logger, "train.mmoe.empty_rows", err, stage="dataset")
        raise err

    movie_pool = sorted({r["movie_id"] for r in positives if r["movie_id"] > 0})
    seen_by_user: Dict[int, set[int]] = {}
    user_pos: Dict[int, int] = {}
    for row in positives:
        uid = row["user_id"]
        user_pos[uid] = user_pos.get(uid, 0) + 1
        seen_by_user.setdefault(uid, set()).add(row["movie_id"])

    negatives: List[Dict[str, Any]] = []
    if neg_ratio > 0 and movie_pool:
        rng = np.random.default_rng(dataset_seed)
        movie_pool_np = np.asarray(movie_pool, dtype=np.int64)
        for uid, pos_cnt in user_pos.items():
            need = pos_cnt * neg_ratio
            if need <= 0:
                continue

            seen = seen_by_user.get(uid, set())
            picked: List[int] = []
            picked_set: set[int] = set()
            if movie_pool_np.size > 0:
                sample_batch = min(need * 6, movie_pool_np.size)
                attempts = 0
                max_attempts = need * 24
                while len(picked) < need and attempts < max_attempts:
                    replace = sample_batch > movie_pool_np.size
                    raw_batch = rng.choice(movie_pool_np, size=sample_batch, replace=replace)
                    for raw_mid in raw_batch.tolist():
                        mid = raw_mid
                        if mid <= 0 or mid in seen or mid in picked_set:
                            continue
                        picked_set.add(mid)
                        picked.append(mid)
                        if len(picked) >= need:
                            break
                    attempts += sample_batch

            if len(picked) < need:
                for mid in movie_pool:
                    if mid <= 0 or mid in seen or mid in picked_set:
                        continue
                    picked_set.add(mid)
                    picked.append(mid)
                    if len(picked) >= need:
                        break

            for mid in picked[:need]:
                negatives.append(
                    {
                        "user_id": uid,
                        "movie_id": mid,
                        "click": 0.0,
                        "collect": 0.0,
                        "comment": 0.0,
                        "rating": 0.0,
                        "source": "two_tower" if (uid + mid) % 2 == 0 else "item_similar_by_tags",
                        "recall_score": 0.0,
                        "_event_time": None,
                    }
                )

    out = positives + negatives
    rng = np.random.default_rng(dataset_seed)
    if len(out) > 1:
        rng.shuffle(out)
    for row in out:
        row.pop("_event_time", None)
        row.pop("_source_priority", None)

    log_event(
        logger,
        "info",
        "train.mmoe.dataset_built",
        stage="dataset",
        click_positive=sum(1 for r in out if r["click"] > 0.5),
        click_negative=sum(1 for r in out if r["click"] <= 0.5),
        collect_positive=sum(1 for r in out if r["collect"] > 0.5),
        collect_negative=sum(1 for r in out if r["collect"] <= 0.5),
        comment_positive=sum(1 for r in out if r["comment"] > 0.5),
        comment_negative=sum(1 for r in out if r["comment"] <= 0.5),
        rate_positive=sum(1 for r in out if r["rating"] > 0.5),
        rate_negative=sum(1 for r in out if r["rating"] <= 0.5),
        global_movie_pool=len(movie_pool),
        global_neg_ratio=neg_ratio,
        global_negative=sum(1 for r in out if r["click"] <= 0.5),
        rows=len(out),
    )
    return out


def _fetch_mmoe_aux_training_features(*, settings: Settings, user_ids: Sequence[int], movie_ids: Sequence[int]) -> Dict[str, Any]:
    engine = get_mysql_engine(settings.core.mysql_dsn, logger=logger, event_prefix="train.mmoe.mysql_engine")

    uid_list = sorted({uid for uid in map(int, user_ids) if uid > 0})
    mid_list = sorted({mid for mid in map(int, movie_ids) if mid > 0})
    if not uid_list or not mid_list:
        err = ValueError("empty_user_or_movie_ids")
        log_exception(logger, "train.mmoe.aux_invalid_ids", err, users=len(uid_list), movies=len(mid_list), stage="aux_features")
        raise err

    mid_chunks = _chunk_values(mid_list, chunk_size=1000)
    uid_chunks = _chunk_values(uid_list, chunk_size=1000)

    movie_base_sql = text(
        """
        SELECT m.movie_id AS movie_id,
               CASE WHEN m.rating_count > 0 THEN (m.rating_sum * 1.0 / m.rating_count) ELSE 0 END AS rating_avg,
               m.rating_count,
               m.year,
               m.duration_min
        FROM movie m
        WHERE m.movie_id IN :mids
        """
    ).bindparams(bindparam("mids", expanding=True))

    item_static_tags_sql = text(
        """
        SELECT mt.movie_id, mt.tag_id
        FROM movie_tag mt
        JOIN tag_dict td ON td.tag_id = mt.tag_id
        WHERE mt.movie_id IN :mids
          AND td.type = 'static'
          AND td.status = 'show'
        ORDER BY mt.movie_id ASC, mt.weight DESC, mt.hot_score DESC, mt.tag_id DESC
        """
    ).bindparams(bindparam("mids", expanding=True))

    user_profile_sql = text(
        """
        SELECT u.user_id, u.gender, u.birth
        FROM user u
        WHERE u.user_id IN :uids
        """
    ).bindparams(bindparam("uids", expanding=True))

    out: Dict[str, Any] = {
        "movie_stats_by_id": {},
        "item_static_tags_by_movie": {},
        "user_profile_by_id": {},
    }

    movie_stats_by_id: Dict[int, Dict[str, Any]] = {}
    item_static_tags_by_movie: Dict[int, List[int]] = {}
    user_profile_by_id: Dict[int, Dict[str, Any]] = {}
    movie_base_rows = 0
    movie_tag_rows = 0
    user_profile_rows = 0

    def _movie_stats_entry(mid: int) -> Dict[str, Any]:
        return movie_stats_by_id.setdefault(
            mid,
            {
                "movie_id": mid,
                "rating_avg": 0.0,
                "rating_count": 0,
                "year": 0,
                "duration_min": 0,
                "comment_count": 0,
                "click_count": 0,
                "click_1h": 0,
                "click_24h": 0,
            },
        )

    log_event(
        logger,
        "info",
        "train.mmoe.aux_query_plan",
        movie_chunks=len(mid_chunks),
        movie_ids=len(mid_list),
        stage="aux_features",
        user_chunks=len(uid_chunks),
        user_ids=len(uid_list),
    )

    try:
        query_started = time.perf_counter()
        with engine.connect() as conn:
            for mids in mid_chunks:
                rs = conn.execute(movie_base_sql, {"mids": mids})
                for row in rs:
                    movie_base_rows += 1
                    m = row._mapping
                    mid = int(m.get("movie_id") or 0)
                    if mid <= 0:
                        continue
                    entry = _movie_stats_entry(mid)
                    entry["rating_avg"] = m.get("rating_avg")
                    entry["rating_count"] = m.get("rating_count")
                    entry["year"] = m.get("year")
                    entry["duration_min"] = m.get("duration_min")

                rs = conn.execute(item_static_tags_sql, {"mids": mids})
                for row in rs:
                    movie_tag_rows += 1
                    m = row._mapping
                    mid = int(m.get("movie_id") or 0)
                    tag_id = int(m.get("tag_id") or 0)
                    if mid > 0 and tag_id > 0:
                        item_static_tags_by_movie.setdefault(mid, []).append(tag_id)

            for uids in uid_chunks:
                rs = conn.execute(user_profile_sql, {"uids": uids})
                for row in rs:
                    user_profile_rows += 1
                    m = row._mapping
                    uid = int(m.get("user_id") or 0)
                    if uid > 0:
                        user_profile_by_id[uid] = {
                            "user_id": uid,
                            "gender": m.get("gender"),
                            "birth": m.get("birth"),
                        }
    except SQLAlchemyError as e:
        log_exception(logger, "train.mmoe.aux_query_failed", e, stage="aux_features")
        raise RuntimeError(f"mmoe_aux_feature_query_failed: {type(e).__name__}: {e}") from e

    log_event(
        logger,
        "info",
        "train.mmoe.aux_query_summary",
        elapsed_ms=round((time.perf_counter() - query_started) * 1000.0, 2),
        item_static_tag_rows=movie_tag_rows,
        movie_base_rows=movie_base_rows,
        stage="aux_features",
        user_profile_rows=user_profile_rows,
    )

    out["movie_stats_by_id"] = movie_stats_by_id
    out["item_static_tags_by_movie"] = item_static_tags_by_movie
    out["user_profile_by_id"] = user_profile_by_id
    return out


def train_mmoe_model(settings: Settings) -> Dict[str, Any]:
    store = get_artifact_store()
    started_at = time.time()

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join("data", "artifacts", "mmoe")
    os.makedirs(out_dir, exist_ok=True)
    artifact_path = os.path.join(out_dir, f"mmoe_{ts}.pt")
    log_event(
        logger,
        "info",
        "train.mmoe.start",
        artifact_path=artifact_path,
        batch_size=settings.mmoe.train_batch_size,
        epochs=settings.mmoe.train_epochs,
        lr=settings.mmoe.train_lr,
        stage="prepare",
        train_limit=settings.mmoe.train_limit,
    )

    try:
        import torch
        from torch import nn

        from app.reco.ranking.mmoe import MMoENet, bundle_feature_order
        from app.reco.ranking.mmoe.features import (
            ITEM_TAG_SEQ_LEN,
            LONG_INTEREST_TAG_SEQ_LEN,
            SHORT_INTEREST_SEQ_LEN,
            bucketize_age,
            default_age_bucket_index,
            default_gender_index,
            normalize_gender,
            pad_or_truncate,
            safe_float,
        )
    except Exception as e:  # noqa: BLE001
        log_exception(logger, "train.mmoe.deps_failed", e, stage="prepare", status="failed")
        raise RuntimeError(f"mmoe_dependency_failed: {type(e).__name__}: {e}") from e

    rows = _fetch_mmoe_training_rows(settings=settings)
    if not rows:
        log_event(logger, "warning", "train.mmoe.empty_data", reason="no_training_data_or_mysql_not_configured", status="skipped")
        return {
            "component": "ranking",
            "name": "mmoe",
            "artifact_path": None,
            "trained": False,
            "details": {"skipped": True, "reason": "no_training_data_or_mysql_not_configured"},
        }

    user_ids_all = [r["user_id"] for r in rows]
    movie_ids_all = [r["movie_id"] for r in rows]
    log_event(
        logger,
        "info",
        "train.mmoe.sample_overview",
        rows=len(rows),
        stage="dataset",
        unique_items=len(set(movie_ids_all)),
        unique_users=len(set(user_ids_all)),
    )
    aux = _fetch_mmoe_aux_training_features(settings=settings, user_ids=user_ids_all, movie_ids=movie_ids_all)
    movie_stats_by_id = aux["movie_stats_by_id"]
    item_static_tags_by_movie = aux["item_static_tags_by_movie"]
    user_profile_by_id = aux["user_profile_by_id"]
    log_event(
        logger,
        "info",
        "train.mmoe.aux_loaded",
        movie_stats=len(movie_stats_by_id),
        rows=len(rows),
        stage="aux_features",
        user_profiles=len(user_profile_by_id),
    )

    feature_names = bundle_feature_order()
    src_features = {
        "src_user_collection",
        "src_user_high_rating_similar",
        "src_user_interest_tag",
        "src_item_similar_by_tags",
        "src_two_tower",
    }

    numeric_raw: List[List[float]] = []
    labels: List[List[float]] = []
    user_values: List[int] = []
    item_values: List[int] = []
    gender_values: List[str] = []
    age_bucket_values: List[str] = []
    all_tag_ids: set[int] = set()
    item_tag_raw_rows: List[List[int]] = []

    missing_movie_stats_cnt = 0
    missing_user_profile_cnt = 0
    missing_item_static_tags_cnt = 0
    for row in rows:
        uid = row["user_id"]
        mid = row["movie_id"]
        if mid not in movie_stats_by_id:
            missing_movie_stats_cnt += 1
        mf = movie_stats_by_id.get(mid, {})
        if mid not in item_static_tags_by_movie:
            missing_item_static_tags_cnt += 1
        item_tags = [tag_id for tag_id in item_static_tags_by_movie.get(mid, []) if tag_id > 0]
        all_tag_ids.update(item_tags)

        if uid not in user_profile_by_id:
            missing_user_profile_cnt += 1
        user_profile = user_profile_by_id.get(uid, {})
        user_gender = normalize_gender(user_profile.get("gender") or "unknown")
        birth = user_profile.get("birth")
        user_age = None
        if birth is not None:
            try:
                now = datetime.utcnow().date()
                user_age = now.year - birth.year - ((now.month, now.day) < (birth.month, birth.day))
            except Exception as e:
                log_event(
                    logger,
                    "warning",
                    "train.mmoe.birth_parse_failed",
                    error=f"{type(e).__name__}: {e}",
                    stage="feature_build",
                    user_id=uid,
                )
                user_age = None
        user_age_bucket = bucketize_age(user_age)

        raw = {
            "recall_score": 0.0,
            "movie_rating_avg": safe_float(mf.get("rating_avg")),
            "movie_rating_count": safe_float(mf.get("rating_count")),
            "movie_comment_count": safe_float(mf.get("comment_count")),
            "movie_click_count": safe_float(mf.get("click_count")),
            "movie_click_1h": safe_float(mf.get("click_1h")),
            "movie_click_24h": safe_float(mf.get("click_24h")),
            "movie_year": safe_float(mf.get("year")),
            "movie_duration_min": safe_float(mf.get("duration_min")),
            "user_static_tag_ctr": 0.0,
            "src_user_collection": 0.0,
            "src_user_high_rating_similar": 0.0,
            "src_user_interest_tag": 0.0,
            "src_item_similar_by_tags": 0.0,
            "src_two_tower": 0.0,
        }

        numeric_raw.append([raw[name] for name in feature_names])
        labels.append([row["click"], row["collect"], row["comment"], row["rating"]])
        user_values.append(uid)
        item_values.append(mid)
        gender_values.append(user_gender)
        age_bucket_values.append(user_age_bucket)
        item_tag_raw_rows.append(pad_or_truncate(item_tags, size=ITEM_TAG_SEQ_LEN))

    log_event(
        logger,
        "info",
        "train.mmoe.aux_defaults_applied",
        long_interest_defaulted=len(rows),
        short_hist_defaulted=len(rows),
        stage="feature_build",
        user_static_tag_ctr_defaulted=len(rows),
    )
    if missing_movie_stats_cnt > 0:
        log_event(
            logger,
            "warning",
            "train.mmoe.movie_stats_missing_summary",
            missing_items=missing_movie_stats_cnt,
            stage="feature_build",
            total=len(rows),
        )
    if missing_user_profile_cnt > 0:
        log_event(
            logger,
            "warning",
            "train.mmoe.user_profile_missing_summary",
            missing_users=missing_user_profile_cnt,
            stage="feature_build",
            total=len(rows),
        )
    if missing_item_static_tags_cnt > 0:
        log_event(
            logger,
            "warning",
            "train.mmoe.item_static_tags_missing_summary",
            missing_items=missing_item_static_tags_cnt,
            stage="feature_build",
            total=len(rows),
        )

    log_event(
        logger,
        "info",
        "train.mmoe.sample_features_built",
        leakage_guard={"recall_score": "disabled", "source_one_hot": "disabled"},
        rows=len(rows),
        stage="feature_build",
        tags_vocab=len(all_tag_ids),
        users_with_profile=len(user_profile_by_id),
    )

    total_rows = len(labels)
    if total_rows <= 1:
        return {
            "component": "ranking",
            "name": "mmoe",
            "artifact_path": None,
            "trained": False,
            "details": {"skipped": True, "reason": "insufficient_training_rows", "rows": total_rows},
        }

    pos_click = sum(1 for v in labels if v[0] > 0.5)
    pos_collect = sum(1 for v in labels if v[1] > 0.5)
    pos_comment = sum(1 for v in labels if v[2] > 0.5)
    pos_rating = sum(1 for v in labels if v[3] > 0.5)
    neg_click = total_rows - pos_click
    neg_collect = total_rows - pos_collect
    neg_comment = total_rows - pos_comment
    neg_rating = total_rows - pos_rating

    log_event(
        logger,
        "info",
        "train.mmoe.label_distribution",
        click_negative=neg_click,
        click_positive=pos_click,
        collect_negative=neg_collect,
        collect_positive=pos_collect,
        comment_negative=neg_comment,
        comment_positive=pos_comment,
        rating_negative=neg_rating,
        rating_positive=pos_rating,
        rows=total_rows,
        stage="dataset",
    )

    if pos_click == 0:
        return {
            "component": "ranking",
            "name": "mmoe",
            "artifact_path": None,
            "trained": False,
            "details": {
                "skipped": True,
                "reason": "click_task_positive_samples_empty",
                "click_positive": pos_click,
                "click_negative": neg_click,
                "collect_positive": pos_collect,
                "collect_negative": neg_collect,
                "comment_positive": pos_comment,
                "comment_negative": neg_comment,
                "rating_positive": pos_rating,
                "rating_negative": neg_rating,
            },
        }

    user_vocab = sorted(set(user_values))
    item_vocab = sorted(set(item_values))
    user_index = {uid: i + 1 for i, uid in enumerate(user_vocab)}
    item_index = {mid: i + 1 for i, mid in enumerate(item_vocab)}
    tag_vocab = sorted(all_tag_ids)
    tag_index = {tag_id: i + 2 for i, tag_id in enumerate(tag_vocab)}
    gender_index = default_gender_index()
    age_bucket_index = default_age_bucket_index()

    item_tag_rows = [[tag_index.get(tag_id, 0) for tag_id in row] for row in item_tag_raw_rows]
    short_hist_rows = [[0] * SHORT_INTEREST_SEQ_LEN for _ in rows]
    long_interest_tag_rows = [[0] * LONG_INTEREST_TAG_SEQ_LEN for _ in rows]

    feature_stats: Dict[str, Dict[str, float]] = {}
    for col_i, name in enumerate(feature_names):
        col = [r[col_i] for r in numeric_raw]
        mean = sum(col) / len(col)
        var = sum((x - mean) ** 2 for x in col) / len(col)
        std = var ** 0.5
        if name in src_features:
            mean = 0.0
            std = 1.0
        if std <= 1e-8:
            std = 1.0
        feature_stats[name] = {"mean": mean, "std": std}

    x_numeric = [
        [
            (row[col_i] - feature_stats[name]["mean"]) / feature_stats[name]["std"]
            for col_i, name in enumerate(feature_names)
        ]
        for row in numeric_raw
    ]

    user_idx: List[int] = []
    item_idx: List[int] = []
    gender_idx: List[int] = []
    age_bucket_idx: List[int] = []
    for uid, mid, g, a in zip(user_values, item_values, gender_values, age_bucket_values):
        if uid not in user_index:
            err = RuntimeError("user_index_missing")
            log_exception(logger, "train.mmoe.user_index_missing", err, user_id=uid, stage="feature_build")
            raise err
        if mid not in item_index:
            err = RuntimeError("item_index_missing")
            log_exception(logger, "train.mmoe.item_index_missing", err, item_id=mid, stage="feature_build")
            raise err
        if g not in gender_index:
            err = RuntimeError("gender_index_missing")
            log_exception(logger, "train.mmoe.gender_index_missing", err, gender=g, stage="feature_build")
            raise err
        if a not in age_bucket_index:
            err = RuntimeError("age_bucket_index_missing")
            log_exception(logger, "train.mmoe.age_bucket_index_missing", err, age_bucket=a, stage="feature_build")
            raise err
        user_idx.append(int(user_index[uid]))
        item_idx.append(int(item_index[mid]))
        gender_idx.append(int(gender_index[g]))
        age_bucket_idx.append(int(age_bucket_index[a]))

    try:
        split_idx = group_train_test_split_indices(user_values, train_ratio=0.8)
        split_strategy = "group_by_user"
    except Exception:
        split_idx = simple_train_test_split_indices(len(labels), train_ratio=0.8)
        split_strategy = "random_row"
    train_idx, test_idx = split_idx
    log_event(
        logger,
        "info",
        "train.mmoe.split_done",
        feature_count=len(feature_names),
        split_strategy=split_strategy,
        stage="split",
        test_rows=len(test_idx),
        train_rows=len(train_idx),
    )

    user_tensor = torch.tensor(user_idx, dtype=torch.long)
    item_tensor = torch.tensor(item_idx, dtype=torch.long)
    numeric_tensor = torch.tensor(x_numeric, dtype=torch.float32)
    gender_tensor = torch.tensor(gender_idx, dtype=torch.long)
    age_bucket_tensor = torch.tensor(age_bucket_idx, dtype=torch.long)
    item_tag_tensor = torch.tensor(item_tag_rows, dtype=torch.long)
    short_hist_tensor = torch.tensor(short_hist_rows, dtype=torch.long)
    long_interest_tag_tensor = torch.tensor(long_interest_tag_rows, dtype=torch.long)
    label_tensor = torch.tensor(labels, dtype=torch.float32)

    model = MMoENet(
        user_vocab_size=len(user_index) + 1,
        item_vocab_size=len(item_index) + 1,
        num_numeric_features=len(feature_names),
        emb_dim=settings.mmoe.emb_dim,
        num_experts=settings.mmoe.num_experts,
        expert_hidden_dim=settings.mmoe.expert_hidden_dim,
        tower_hidden_dim=settings.mmoe.tower_hidden_dim,
        gender_vocab_size=max(gender_index.values()) + 1,
        age_bucket_vocab_size=max(age_bucket_index.values()) + 1,
        tag_vocab_size=max(tag_index.values()) + 1,
        use_item_tag_pooling=True,
        use_target_attention=True,
        use_long_interest_pooling=True,
    )
    optimizer = torch.optim.Adam(model.parameters(), lr=settings.mmoe.train_lr)
    bce = nn.BCELoss()

    def _derive_pos_weight(*, pos: int, neg: int) -> float:
        if not settings.mmoe.enable_dynamic_pos_weight:
            return 1.0
        if pos <= 0:
            return 1.0
        ratio = float(neg) / float(pos)
        return max(1.0, min(ratio, float(settings.mmoe.dynamic_pos_weight_cap)))

    def _weighted_bce(prob: torch.Tensor, target: torch.Tensor, *, pos_weight: float) -> torch.Tensor:
        if pos_weight <= 1.0:
            return bce(prob, target)
        eps = 1e-7
        p = torch.clamp(prob, eps, 1.0 - eps)
        loss = -(target * torch.log(p) * float(pos_weight) + (1.0 - target) * torch.log(1.0 - p))
        return loss.mean()

    epochs = settings.mmoe.train_epochs
    batch_size = settings.mmoe.train_batch_size
    in_batch_neg_ratio = settings.mmoe.in_batch_neg_ratio
    w_click = settings.mmoe.loss_weight_click if pos_click > 0 else 0.0
    w_collect = settings.mmoe.loss_weight_collect if pos_collect > 0 else 0.0
    w_rate = settings.mmoe.loss_weight_rate if pos_rating > 0 else 0.0
    w_comment = settings.mmoe.loss_weight_comment if pos_comment > 0 else 0.0
    click_pos_weight = _derive_pos_weight(pos=pos_click, neg=neg_click)
    collect_pos_weight = _derive_pos_weight(pos=pos_collect, neg=neg_collect)
    comment_pos_weight = _derive_pos_weight(pos=pos_comment, neg=neg_comment)
    rating_pos_weight = _derive_pos_weight(pos=pos_rating, neg=neg_rating)
    n = len(train_idx)
    train_idx_tensor = torch.tensor(train_idx, dtype=torch.long)
    test_idx_tensor = torch.tensor(test_idx, dtype=torch.long)

    log_event(
        logger,
        "info",
        "train.mmoe.task_weight_adjusted",
        click_enabled=w_click > 0.0,
        collect_enabled=w_collect > 0.0,
        comment_enabled=w_comment > 0.0,
        rating_enabled=w_rate > 0.0,
        stage="fit",
    )
    log_event(
        logger,
        "info",
        "train.mmoe.class_balance_weights",
        click_pos_weight=click_pos_weight,
        collect_pos_weight=collect_pos_weight,
        comment_pos_weight=comment_pos_weight,
        dynamic_cap=settings.mmoe.dynamic_pos_weight_cap,
        enabled=settings.mmoe.enable_dynamic_pos_weight,
        rating_pos_weight=rating_pos_weight,
        stage="fit",
    )
    log_event(
        logger,
        "info",
        "train.mmoe.neg_sampling_config",
        in_batch_neg_ratio=in_batch_neg_ratio,
        loss_weight_click=w_click,
        loss_weight_collect=w_collect,
        loss_weight_comment=w_comment,
        loss_weight_rate=w_rate,
        stage="fit",
    )

    model.train()
    for epoch_idx in range(epochs):
        epoch_loss_sum = 0.0
        epoch_steps = 0
        epoch_click_neg_samples = 0
        epoch_collect_neg_samples = 0
        epoch_comment_neg_samples = 0
        epoch_rating_neg_samples = 0
        order = train_idx_tensor[torch.randperm(n)]
        for start in range(0, n, batch_size):
            idx = order[start : start + batch_size]
            pred = model(
                user_tensor[idx],
                item_tensor[idx],
                numeric_tensor[idx],
                gender_idx=gender_tensor[idx],
                age_bucket_idx=age_bucket_tensor[idx],
                item_tag_ids=item_tag_tensor[idx],
                short_hist_item_ids=short_hist_tensor[idx],
                long_interest_tag_ids=long_interest_tag_tensor[idx],
            )

            click_pos_loss = (
                _weighted_bce(pred["click"], label_tensor[idx, 0], pos_weight=click_pos_weight)
                if w_click > 0.0
                else pred["click"].sum() * 0.0
            )
            click_neg_loss = pred["click"].new_tensor(0.0)
            collect_neg_loss = pred["click"].new_tensor(0.0)
            comment_neg_loss = pred["click"].new_tensor(0.0)
            rating_neg_loss = pred["click"].new_tensor(0.0)

            if idx.shape[0] > 1 and in_batch_neg_ratio > 0:
                for neg_round in range(in_batch_neg_ratio):
                    neg_src = idx.roll(shifts=neg_round + 1)
                    neg_pred = model(
                        user_tensor[idx],
                        item_tensor[neg_src],
                        numeric_tensor[neg_src],
                        gender_idx=gender_tensor[idx],
                        age_bucket_idx=age_bucket_tensor[idx],
                        item_tag_ids=item_tag_tensor[neg_src],
                        short_hist_item_ids=short_hist_tensor[idx],
                        long_interest_tag_ids=long_interest_tag_tensor[idx],
                    )
                    if w_click > 0.0:
                        click_neg_target = torch.zeros_like(neg_pred["click"])
                        click_neg_loss = click_neg_loss + bce(neg_pred["click"], click_neg_target)
                        epoch_click_neg_samples += int(click_neg_target.shape[0])
                    if w_collect > 0.0:
                        collect_neg_target = torch.zeros_like(neg_pred["collect"])
                        collect_neg_loss = collect_neg_loss + bce(neg_pred["collect"], collect_neg_target)
                        epoch_collect_neg_samples += int(collect_neg_target.shape[0])
                    if w_comment > 0.0:
                        comment_neg_target = torch.zeros_like(neg_pred["comment"])
                        comment_neg_loss = comment_neg_loss + bce(neg_pred["comment"], comment_neg_target)
                        epoch_comment_neg_samples += int(comment_neg_target.shape[0])
                    if w_rate > 0.0:
                        rating_neg_target = torch.zeros_like(neg_pred["rating"])
                        rating_neg_loss = rating_neg_loss + bce(neg_pred["rating"], rating_neg_target)
                        epoch_rating_neg_samples += int(rating_neg_target.shape[0])

                ratio_den = float(in_batch_neg_ratio)
                if w_click > 0.0:
                    click_neg_loss = click_neg_loss / ratio_den
                if w_collect > 0.0:
                    collect_neg_loss = collect_neg_loss / ratio_den
                if w_comment > 0.0:
                    comment_neg_loss = comment_neg_loss / ratio_den
                if w_rate > 0.0:
                    rating_neg_loss = rating_neg_loss / ratio_den

            collect_pos_loss = (
                _weighted_bce(pred["collect"], label_tensor[idx, 1], pos_weight=collect_pos_weight)
                if w_collect > 0.0
                else pred["collect"].sum() * 0.0
            )
            comment_pos_loss = (
                _weighted_bce(pred["comment"], label_tensor[idx, 2], pos_weight=comment_pos_weight)
                if w_comment > 0.0
                else pred["comment"].sum() * 0.0
            )
            rating_pos_loss = (
                _weighted_bce(pred["rating"], label_tensor[idx, 3], pos_weight=rating_pos_weight)
                if w_rate > 0.0
                else pred["rating"].sum() * 0.0
            )

            loss = (
                w_click * (click_pos_loss + click_neg_loss)
                + w_collect * (collect_pos_loss + collect_neg_loss)
                + w_comment * (comment_pos_loss + comment_neg_loss)
                + w_rate * (rating_pos_loss + rating_neg_loss)
            )

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            epoch_loss_sum += float(loss.item())
            epoch_steps += 1

        avg_loss = epoch_loss_sum / epoch_steps
        log_event(
            logger,
            "info",
            "train.mmoe.epoch_done",
            avg_loss=f"{avg_loss:.6f}",
            epoch=epoch_idx + 1,
            epochs=epochs,
            in_batch_click_negatives=epoch_click_neg_samples,
            in_batch_collect_negatives=epoch_collect_neg_samples,
            in_batch_comment_negatives=epoch_comment_neg_samples,
            in_batch_rating_negatives=epoch_rating_neg_samples,
            stage="fit",
            step_count=epoch_steps,
        )

    model.eval()
    with torch.no_grad():
        test_pred = model(
            user_tensor[test_idx_tensor],
            item_tensor[test_idx_tensor],
            numeric_tensor[test_idx_tensor],
            gender_idx=gender_tensor[test_idx_tensor],
            age_bucket_idx=age_bucket_tensor[test_idx_tensor],
            item_tag_ids=item_tag_tensor[test_idx_tensor],
            short_hist_item_ids=short_hist_tensor[test_idx_tensor],
            long_interest_tag_ids=long_interest_tag_tensor[test_idx_tensor],
        )

    y_test = label_tensor[test_idx_tensor].cpu().numpy()
    auc_click = _safe_binary_auc(task_name="click", y_true=y_test[:, 0].tolist(), y_score=test_pred["click"].cpu().numpy().tolist())
    auc_collect = _safe_binary_auc(task_name="collect", y_true=y_test[:, 1].tolist(), y_score=test_pred["collect"].cpu().numpy().tolist())
    auc_comment = _safe_binary_auc(task_name="comment", y_true=y_test[:, 2].tolist(), y_score=test_pred["comment"].cpu().numpy().tolist())
    auc_rating = _safe_binary_auc(task_name="rating", y_true=y_test[:, 3].tolist(), y_score=test_pred["rating"].cpu().numpy().tolist())
    auc_values = [x for x in [auc_click, auc_collect, auc_comment, auc_rating] if x is not None]
    auc_mean = float(sum(auc_values) / len(auc_values)) if auc_values else None
    log_event(
        logger,
        "info",
        "train.mmoe.eval_done",
        auc_click=auc_click,
        auc_collect=auc_collect,
        auc_comment=auc_comment,
        auc_mean=auc_mean,
        auc_rating=auc_rating,
        stage="evaluate",
    )

    bundle = {
        "state_dict": model.state_dict(),
        "model_meta": {
            "user_vocab_size": len(user_index) + 1,
            "item_vocab_size": len(item_index) + 1,
            "num_numeric_features": len(feature_names),
            "emb_dim": settings.mmoe.emb_dim,
            "num_experts": settings.mmoe.num_experts,
            "expert_hidden_dim": settings.mmoe.expert_hidden_dim,
            "tower_hidden_dim": settings.mmoe.tower_hidden_dim,
            "gender_vocab_size": max(gender_index.values()) + 1,
            "age_bucket_vocab_size": max(age_bucket_index.values()) + 1,
            "tag_vocab_size": max(tag_index.values()) + 1,
            "use_item_tag_pooling": True,
            "use_target_attention": True,
            "use_long_interest_pooling": True,
        },
        "tasks": ["click", "collect", "comment", "rating"],
        "feature_order": feature_names,
        "feature_stats": feature_stats,
        "user_index": user_index,
        "item_index": item_index,
        "gender_index": gender_index,
        "age_bucket_index": age_bucket_index,
        "tag_index": tag_index,
    }
    torch.save(bundle, artifact_path)
    log_event(logger, "info", "train.mmoe.model_saved", artifact_path=artifact_path, stage="finalize")

    store.set("ranking.mmoe.latest_artifact_path", artifact_path)
    store.set("ranking.mmoe.latest_trained_at", ts)
    elapsed_ms = int((time.time() - started_at) * 1000)
    log_event(logger, "info", "train.mmoe.done", elapsed_ms=elapsed_ms, stage="finalize", status="completed")

    return {
        "component": "ranking",
        "name": "mmoe",
        "artifact_path": artifact_path,
        "trained": True,
        "details": {
            "rows": total_rows,
            "click_positive": pos_click,
            "click_negative": neg_click,
            "collect_positive": pos_collect,
            "collect_negative": neg_collect,
            "comment_positive": pos_comment,
            "comment_negative": neg_comment,
            "rating_positive": pos_rating,
            "rating_negative": neg_rating,
            "feature_count": len(feature_names),
            "epochs": epochs,
            "batch_size": batch_size,
            "train_rows": len(train_idx),
            "test_rows": len(test_idx),
            "test_auc": auc_mean,
            "test_auc_click": auc_click,
            "test_auc_collect": auc_collect,
            "test_auc_comment": auc_comment,
            "test_auc_rating": auc_rating,
        },
    }
