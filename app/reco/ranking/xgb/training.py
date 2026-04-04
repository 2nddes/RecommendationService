from __future__ import annotations

from datetime import datetime
import logging
import os
import time
from typing import Any, Dict, List, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.common.settings import Settings
from app.ops.artifact_store import get_artifact_store
from app.reco.training.common import (
    binary_auc,
    binary_train_test_split_indices,
    catch_and_reraise,
    get_mysql_engine,
    log_event,
    log_exception,
    simple_train_test_split_indices,
)


logger = logging.getLogger(__name__)


def _interaction_strength(*, action_type: str, rating: int | None, source_kind: str) -> float:
    if source_kind == "rating" and rating is not None:
        return (rating - 5.0) / 5.0

    if source_kind == "action" and action_type == "rate":
        return 0.0

    action_weight = {
        "view": 0.2,
        "like": 1.0,
        "collect": 1.2,
        "share": 0.8,
        "comment": 0.7,
        "rate": 0.9,
        "dislike": -0.8,
    }
    if action_type not in action_weight:
        err = ValueError("unknown_action_type")
        log_exception(logger, "train.xgb.interaction.unknown_action", err, action_type=action_type)
        raise err
    return action_weight[action_type]


def _fetch_xgb_training_rows(*, settings: Settings) -> List[Tuple[int, int, str, int | None, str, float]]:
    engine = get_mysql_engine(settings.core.mysql_dsn, logger=logger, event_prefix="train.xgb.mysql_engine")
    limit_n = settings.xgb.train_limit

    sql = """
    SELECT t.user_id,
           t.movie_id,
           t.action_type,
           t.rating,
           t.source_kind,
           t.event_time
    FROM (
         SELECT * FROM (
             SELECT ua.user_id AS user_id,
                 ua.movie_id AS movie_id,
                 ua.action_type AS action_type,
                 NULL AS rating,
                 'action' AS source_kind,
                 ua.created_at AS event_time
             FROM user_action ua
             WHERE ua.movie_id IS NOT NULL
                   AND ua.action_type <> 'rate'
             ORDER BY ua.created_at DESC
             LIMIT :limit
         ) ua_recent

        UNION ALL

         SELECT * FROM (
             SELECT r.user_id AS user_id,
                 r.movie_id AS movie_id,
                 'rate' AS action_type,
                 r.rating AS rating,
                 'rating' AS source_kind,
                 r.updated_at AS event_time
             FROM rating r
             WHERE r.movie_id IS NOT NULL
             ORDER BY r.updated_at DESC
             LIMIT :limit
         ) rating_recent

        UNION ALL

         SELECT * FROM (
             SELECT ucm.user_id AS user_id,
                 ucm.movie_id AS movie_id,
                 'collect' AS action_type,
                 NULL AS rating,
                 'collect' AS source_kind,
                 ucm.created_at AS event_time
             FROM user_collect_movie ucm
             WHERE ucm.movie_id IS NOT NULL
             ORDER BY ucm.created_at DESC
             LIMIT :limit
         ) collect_recent
    ) t
    ORDER BY t.event_time DESC
    LIMIT :limit
    """

    try:
        with engine.connect() as conn:
            rs = conn.execute(text(sql), {"limit": limit_n})
            best_by_pair: Dict[Tuple[int, int], Dict[str, Any]] = {}
            for row in rs:
                m = row._mapping
                with catch_and_reraise(
                    logger,
                    "train.xgb.row_parse_failed",
                    "xgb_training_row_parse_failed",
                    stage="dataset",
                ):
                    user_id = int(m["user_id"])
                    movie_id = int(m["movie_id"])
                    action_type = m.get("action_type") or "view"
                    source_kind = m.get("source_kind") or "action"
                    rating = int(m["rating"]) if m.get("rating") is not None else None
                    event_time = m.get("event_time")

                    strength = _interaction_strength(action_type=action_type, rating=rating, source_kind=source_kind)
                    source_priority = 3 if source_kind == "rating" else (2 if source_kind == "collect" else 1)
                    key = (user_id, movie_id)
                    prev = best_by_pair.get(key)

                    if prev is None:
                        best_by_pair[key] = {
                            "user_id": user_id,
                            "movie_id": movie_id,
                            "action_type": action_type,
                            "rating": rating,
                            "source_kind": source_kind,
                            "strength": strength,
                            "source_priority": source_priority,
                            "event_time": event_time,
                        }
                    else:
                        prev_priority = prev.get("source_priority", 0)
                        prev_strength = prev.get("strength", 0.0)
                        prev_time = prev.get("event_time")
                        replace = source_priority > prev_priority or (
                            source_priority == prev_priority and abs(strength) > abs(prev_strength)
                        ) or (
                            source_priority == prev_priority
                            and abs(strength) == abs(prev_strength)
                            and event_time is not None
                            and prev_time is not None
                            and event_time > prev_time
                        )
                        if replace:
                            prev.update(
                                {
                                    "action_type": action_type,
                                    "rating": rating,
                                    "source_kind": source_kind,
                                    "strength": strength,
                                    "source_priority": source_priority,
                                    "event_time": event_time,
                                }
                            )

            dedup_rows = list(best_by_pair.values())
            dedup_rows.sort(key=lambda x: x["event_time"], reverse=True)
            dedup_rows = dedup_rows[: limit_n]

            out: List[Tuple[int, int, str, int | None, str, float]] = []
            for row in dedup_rows:
                out.append(
                    (
                        row["user_id"],
                        row["movie_id"],
                        row["action_type"],
                        row["rating"],
                        row["source_kind"],
                        row["strength"],
                    )
                )
            return out
    except SQLAlchemyError as e:
        log_exception(logger, "train.xgb.fetch_rows_failed", e, stage="dataset", train_limit=limit_n)
        raise RuntimeError(f"xgb_fetch_training_rows_failed: {type(e).__name__}: {e}") from e


def train_xgb_model(settings: Settings) -> Dict[str, Any]:
    store = get_artifact_store()
    started_at = time.time()

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join("data", "artifacts", "xgb")
    os.makedirs(out_dir, exist_ok=True)
    artifact_path = os.path.join(out_dir, f"xgb_{ts}.json")
    log_event(
        logger,
        "info",
        "train.xgb.start",
        artifact_path=artifact_path,
        eta=settings.xgb.train_eta,
        max_depth=settings.xgb.train_max_depth,
        rounds=settings.xgb.train_rounds,
        stage="prepare",
        train_limit=settings.xgb.train_limit,
    )

    try:
        import numpy as np
        import xgboost as xgb

        from app.reco.ranking.xgb.features import ManualFeatureBuilder, ManualFeatureConfig, fetch_movie_features
        from app.reco.types import Candidate, RequestContext
    except Exception as e:  # noqa: BLE001
        log_exception(logger, "train.xgb.deps_failed", e, stage="prepare", status="failed")
        raise RuntimeError(f"xgb_dependency_failed: {type(e).__name__}: {e}") from e

    rows = _fetch_xgb_training_rows(settings=settings)
    if not rows:
        log_event(logger, "warning", "train.xgb.empty_data", reason="no_training_data_or_mysql_not_configured", status="skipped")
        return {
            "component": "ranking",
            "name": "xgb",
            "artifact_path": None,
            "trained": False,
            "details": {"skipped": True, "reason": "no_training_data_or_mysql_not_configured"},
        }

    candidates: List[Candidate] = []
    labels: List[float] = []
    sources = ["user_collection", "user_high_rating_similar", "user_interest_tag", "item_similar_by_tags"]

    for i, (_user_id, movie_id, action_type, rating, source_kind, strength) in enumerate(rows):
        src = sources[i % len(sources)]
        candidates.append(Candidate(item_id=movie_id, score=0.0, source=src))

        y = 1.0 if strength >= 0.8 else 0.0
        if source_kind == "rating" and rating is not None:
            y = 1.0 if rating >= 8 else 0.0
        if action_type == "dislike":
            y = 0.0
        labels.append(y)

    pos_cnt = sum(1 for x in labels if x > 0.5)
    neg_cnt = len(labels) - pos_cnt
    log_event(
        logger,
        "info",
        "train.xgb.samples_ready",
        negative=neg_cnt,
        positive=pos_cnt,
        rows=len(labels),
        stage="dataset",
    )
    log_event(logger, "info", "train.xgb.leakage_guard_enabled", disabled_features=["recall_score"], stage="dataset")
    if pos_cnt == 0 or neg_cnt == 0:
        log_event(
            logger,
            "warning",
            "train.xgb.label_insufficient",
            negative=neg_cnt,
            positive=pos_cnt,
            reason="insufficient_label_diversity",
            status="skipped",
        )
        return {
            "component": "ranking",
            "name": "xgb",
            "artifact_path": None,
            "trained": False,
            "details": {
                "skipped": True,
                "reason": "insufficient_label_diversity",
                "positive": pos_cnt,
                "negative": neg_cnt,
            },
        }

    movie_ids = [c.item_id for c in candidates]
    movie_features = (
        fetch_movie_features(movie_ids, mysql_dsn=settings.core.mysql_dsn) if settings.xgb.use_mysql_features else {}
    )

    builder = ManualFeatureBuilder(config=ManualFeatureConfig(include_mysql_movie_features=settings.xgb.use_mysql_features))
    ctx = RequestContext(user_id=rows[0][0], n=10)
    feat_rows = builder.build_rows(ctx, candidates, movie_features)
    X = np.asarray(builder.to_matrix(feat_rows), dtype=float)
    y = np.asarray(labels, dtype=float)

    try:
        split_idx = binary_train_test_split_indices(y.tolist(), train_ratio=0.8)
    except Exception:
        split_idx = simple_train_test_split_indices(len(y), train_ratio=0.8)

    train_idx, test_idx = split_idx
    log_event(
        logger,
        "info",
        "train.xgb.split_done",
        feature_count=X.shape[1],
        stage="split",
        test_rows=len(test_idx),
        train_rows=len(train_idx),
    )
    X_train = X[train_idx]
    y_train = y[train_idx]
    X_test = X[test_idx]
    y_test = y[test_idx]

    dtrain = xgb.DMatrix(X_train, label=y_train, feature_names=builder.feature_names())
    dtest = xgb.DMatrix(X_test, label=y_test, feature_names=builder.feature_names())

    params = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "max_depth": settings.xgb.train_max_depth,
        "eta": settings.xgb.train_eta,
        "subsample": settings.xgb.train_subsample,
        "colsample_bytree": settings.xgb.train_colsample,
        "seed": 20260106,
    }

    num_boost_round = settings.xgb.train_rounds
    log_event(logger, "info", "train.xgb.fit_start", boost_rounds=num_boost_round, stage="fit")
    booster = xgb.train(params, dtrain, num_boost_round=num_boost_round)
    test_pred = booster.predict(dtest)
    test_auc = binary_auc(y_test.tolist(), test_pred.tolist())
    booster.save_model(artifact_path)
    log_event(
        logger,
        "info",
        "train.xgb.eval_done",
        artifact_path=artifact_path,
        stage="evaluate",
        test_auc=test_auc,
    )

    store.set("ranking.xgb.latest_artifact_path", artifact_path)
    store.set("ranking.xgb.latest_trained_at", ts)
    elapsed_ms = int((time.time() - started_at) * 1000)
    log_event(logger, "info", "train.xgb.done", elapsed_ms=elapsed_ms, stage="finalize", status="completed")

    return {
        "component": "ranking",
        "name": "xgb",
        "artifact_path": artifact_path,
        "trained": True,
        "details": {
            "rows": len(rows),
            "positive": pos_cnt,
            "negative": neg_cnt,
            "feature_count": X.shape[1],
            "boost_rounds": num_boost_round,
            "train_rows": len(train_idx),
            "test_rows": len(test_idx),
            "test_auc": test_auc,
        },
    }
