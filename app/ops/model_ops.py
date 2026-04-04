from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import logging
import os
import time
from typing import Any, Dict, List, Sequence, Tuple

from sqlalchemy import Engine, bindparam, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.common.settings import Settings
from app.ops.artifact_store import get_artifact_store


logger = logging.getLogger(__name__)


def _fmt_log_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)


def _log_event(level: str, event: str, **fields: Any) -> None:
    payload: Dict[str, Any] = {"event": event}
    for key, value in fields.items():
        payload[key] = value
    message = " | ".join(f"{k}={_fmt_log_value(v)}" for k, v in payload.items())
    getattr(logger, level, logger.info)(message)


def _log_exception(event: str, error: Exception, **fields: Any) -> None:
    payload: Dict[str, Any] = {"event": event, "error": f"{type(error).__name__}: {error}"}
    for key, value in fields.items():
        payload[key] = value
    message = " | ".join(f"{k}={_fmt_log_value(v)}" for k, v in payload.items())
    logger.exception(message)


@contextmanager
def _catch_and_reraise(event: str, error_prefix: str, **fields: Any):
    try:
        yield
    except Exception as e:  # noqa: BLE001
        _log_exception(event, e, **fields)
        raise RuntimeError(f"{error_prefix}: {type(e).__name__}: {e}") from e



@dataclass(frozen=True)
class TrainOutcome:
    component: str  # ranking|recall
    name: str
    artifact_path: str | None
    trained: bool
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def train_current_models(
    settings: Settings,
    *,
    component: str | None = None,
    model: str | None = None,
    train_job_id: int | None = None,
) -> Dict[str, Any]:
    """Train (or rebuild) artifacts for models that are enabled by config.

    This does NOT change which model is selected; config remains the source of truth.
    It only produces artifacts and records their paths.
    """

    _log_event(
        "info",
        "train.task.start",
        component=component,
        model=model,
        stage="dispatch",
        status="processing",
        train_job_id=train_job_id,
    )

    if train_job_id is not None:
        update_model_train_job(
            mysql_dsn=settings.mysql_dsn,
            job_id=train_job_id,
            status="processing",
        )

    try:
        if component == "ranking" and model == "xgb":
            outcome = _train_xgb(settings)
            result = {"train_outcome": outcome.to_dict()}
        elif component == "ranking" and model == "mmoe":
            outcome = _train_mmoe(settings)
            result = {"train_outcome": outcome.to_dict()}
        elif component == "recall" and model == "two_tower":
            outcome = _train_two_tower_index(settings)
            result = {"train_outcome": outcome.to_dict()}
        else:
            raise ValueError(f"Unknown component/model combination: {component}/{model}")

        if not outcome.trained:
            details = outcome.details if isinstance(outcome.details, dict) else {}
            reason = details.get("reason") or details.get("error") or "train_outcome_not_trained"
            _log_event(
                "warning",
                "train.task.untrained",
                component=component,
                model=model,
                reason=reason,
                status="failed",
                train_job_id=train_job_id,
            )
            raise RuntimeError(str(reason))

        if train_job_id is not None:
            update_model_train_job(
                mysql_dsn=settings.mysql_dsn,
                job_id=train_job_id,
                status="completed",
                metrics=result,
                set_finished_at=True,
            )
        _log_event(
            "info",
            "train.task.done",
            artifact_path=outcome.artifact_path,
            component=component,
            model=model,
            status="completed",
            train_job_id=train_job_id,
        )
        return result
    except Exception as e:  # noqa: BLE001
        if train_job_id is not None:
            update_model_train_job(
                mysql_dsn=settings.mysql_dsn,
                job_id=train_job_id,
                status="failed",
                metrics={
                    "error": f"{type(e).__name__}: {e}",
                    "component": component,
                    "model": model,
                },
                set_finished_at=True,
            )
        _log_exception(
            "train.task.failed",
            e,
            component=component,
            model=model,
            status="failed",
            train_job_id=train_job_id,
        )
        raise


def refresh_current_models(settings: Settings) -> Dict[str, Any]:
    try:
        from app.reco.ranking.mmoe_ranker import load_latest_local_model as load_latest_mmoe_local_model
        from app.reco.recall.two_tower import load_latest_local_model as load_latest_two_tower_local_model

        if not load_latest_mmoe_local_model(settings):
            return {"status": "failed", "reason": "mmoe_model_not_found"}
        if not load_latest_two_tower_local_model(settings):
            return {"status": "failed", "reason": "two_tower_model_not_found"}

        return {"status": "completed", "reason": None}
    except Exception as e:  # noqa: BLE001
        _log_exception("refresh.current_models.failed", e)
        raise RuntimeError(f"refresh_current_models_failed: {type(e).__name__}: {e}") from e



# ----------------------------
# XGBoost
# ----------------------------


def _get_mysql_engine(mysql_dsn: str | None) -> Engine | None:
    dsn = mysql_dsn
    if not dsn:
        err = RuntimeError("mysql_dsn_missing")
        _log_exception("mysql.engine.dsn_missing", err)
        raise err
    try:
        return create_engine(dsn, pool_pre_ping=True)
    except Exception as e:
        _log_exception("mysql.engine.create_failed", e, mysql_dsn_set=bool(dsn.strip()))
        raise RuntimeError(f"mysql_engine_create_failed: {type(e).__name__}: {e}") from e


def _binary_auc(y_true: Sequence[float], y_score: Sequence[float]) -> float:
    if len(y_true) != len(y_score) or len(y_true) == 0:
        err = ValueError("invalid_auc_input")
        _log_exception("train.metric.auc_invalid_input", err, y_true_len=len(y_true), y_score_len=len(y_score))
        raise err

    pairs = [(float(y), float(s)) for y, s in zip(y_true, y_score)]
    pos_count = sum(1 for y, _ in pairs if y > 0.5)
    neg_count = len(pairs) - pos_count
    if pos_count == 0 or neg_count == 0:
        err = ValueError("auc_requires_both_classes")
        _log_exception("train.metric.auc_class_undefined", err, pos_count=pos_count, neg_count=neg_count)
        raise err

    pairs.sort(key=lambda x: x[1])

    rank_sum_pos = 0.0
    i = 0
    n = len(pairs)
    while i < n:
        j = i + 1
        while j < n and pairs[j][1] == pairs[i][1]:
            j += 1

        avg_rank = ((i + 1) + j) / 2.0
        pos_in_group = sum(1 for k in range(i, j) if pairs[k][0] > 0.5)
        rank_sum_pos += avg_rank * pos_in_group
        i = j

    auc = (rank_sum_pos - (pos_count * (pos_count + 1) / 2.0)) / (pos_count * neg_count)
    return auc


def _binary_train_test_split_indices(y: Sequence[float], train_ratio: float = 0.8) -> tuple[List[int], List[int]]:
    pos = [i for i, v in enumerate(y) if v > 0.5]
    neg = [i for i, v in enumerate(y) if v <= 0.5]
    if not pos or not neg:
        err = ValueError("split_requires_both_classes")
        _log_exception("train.split.binary_invalid", err, pos_count=len(pos), neg_count=len(neg))
        raise err

    def _split(group: List[int]) -> tuple[List[int], List[int]]:
        if len(group) <= 1:
            return group[:], []
        cut = int(len(group) * train_ratio)
        return group[:cut], group[cut:]

    pos_train, pos_test = _split(pos)
    neg_train, neg_test = _split(neg)

    train_idx = pos_train + neg_train
    test_idx = pos_test + neg_test
    if not train_idx or not test_idx:
        err = ValueError("split_result_empty")
        _log_exception("train.split.binary_empty", err, train_rows=len(train_idx), test_rows=len(test_idx))
        raise err
    return train_idx, test_idx


def _simple_train_test_split_indices(total: int, train_ratio: float = 0.8) -> tuple[List[int], List[int]]:
    n = total
    if n <= 1:
        err = ValueError("split_requires_more_rows")
        _log_exception("train.split.simple_invalid", err, rows=n)
        raise err
    cut = int(n * train_ratio)
    train_idx = list(range(0, cut))
    test_idx = list(range(cut, n))
    if not train_idx or not test_idx:
        err = ValueError("split_result_empty")
        _log_exception("train.split.simple_empty", err, train_rows=len(train_idx), test_rows=len(test_idx))
        raise err
    return train_idx, test_idx


def _safe_binary_auc(*, task_name: str, y_true: Sequence[float], y_score: Sequence[float]) -> float | None:
    pos_count = sum(1 for y in y_true if y > 0.5)
    neg_count = len(y_true) - pos_count
    if pos_count == 0 or neg_count == 0:
        _log_event(
            "warning",
            "train.mmoe.eval_auc_skipped",
            negative=neg_count,
            positive=pos_count,
            reason="single_class_test_labels",
            stage="evaluate",
            task=task_name,
        )
        return None
    return _binary_auc(y_true, y_score)


def _chunk_values(values: Sequence[int], *, chunk_size: int) -> List[List[int]]:
    size = chunk_size
    uniq = [v for v in (int(x) for x in values) if v > 0]
    return [uniq[i : i + size] for i in range(0, len(uniq), size)]


def _interaction_strength(*, action_type: str, rating: int | None, source_kind: str) -> float:
    if source_kind == "rating" and rating is not None:
        # map 1~10 to [-0.8, 1.0], keep low-score ratings as hard negatives
        return (rating - 5.0) / 5.0

    if source_kind == "action" and action_type == "rate":
        # explicit score should come from rating table only
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
        _log_exception("train.interaction.unknown_action", err, action_type=action_type)
        raise err
    return action_weight[action_type]


def _fetch_xgb_training_rows(*, settings: Settings) -> List[Tuple[int, int, str, int | None, str, float]]:
    """Return tuples: (user_id, movie_id, action_type, rating, source_kind, strength)."""

    engine = _get_mysql_engine(settings.mysql_dsn)
    if engine is None:
        err = RuntimeError("mysql_not_configured")
        _log_exception("train.xgb.mysql_unavailable", err, stage="dataset")
        raise err

    limit_n = settings.xgb_train_limit

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
                with _catch_and_reraise("train.xgb.row_parse_failed", "xgb_training_row_parse_failed", stage="dataset"):
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

                    # deduplicate (user,item): keep the strongest interaction; tie-break by recency
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
        _log_exception("train.xgb.fetch_rows_failed", e, stage="dataset", train_limit=limit_n)
        raise RuntimeError(f"xgb_fetch_training_rows_failed: {type(e).__name__}: {e}") from e


def _train_xgb(settings: Settings) -> TrainOutcome:
    store = get_artifact_store()
    started_at = time.time()

    # Decide output location (staging)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join("data", "artifacts", "xgb")
    os.makedirs(out_dir, exist_ok=True)
    artifact_path = os.path.join(out_dir, f"xgb_{ts}.json")
    _log_event(
        "info",
        "train.xgb.start",
        artifact_path=artifact_path,
        eta=settings.xgb_train_eta,
        max_depth=settings.xgb_train_max_depth,
        rounds=settings.xgb_train_rounds,
        stage="prepare",
        train_limit=settings.xgb_train_limit,
    )

    try:
        import numpy as np
        import xgboost as xgb

        from app.reco.ranking.xgb_features import ManualFeatureBuilder, ManualFeatureConfig, fetch_movie_features
        from app.reco.types import Candidate, RequestContext
    except Exception as e:  # noqa: BLE001
        _log_exception("train.xgb.deps_failed", e, stage="prepare", status="failed")
        raise RuntimeError(f"xgb_dependency_failed: {type(e).__name__}: {e}") from e

    rows = _fetch_xgb_training_rows(settings=settings)
    if not rows:
        _log_event("warning", "train.xgb.empty_data", reason="no_training_data_or_mysql_not_configured", status="skipped")
        return TrainOutcome(
            component="ranking",
            name="xgb",
            artifact_path=None,
            trained=False,
            details={"skipped": True, "reason": "no_training_data_or_mysql_not_configured"},
        )

    # Build candidates and labels
    candidates: List[Candidate] = []
    labels: List[float] = []

    # rotate sources to make src_* features non-trivial
    sources = ["user_collection", "user_high_rating_similar", "user_interest_tag", "item_similar_by_tags"]

    for i, (_user_id, movie_id, action_type, rating, source_kind, strength) in enumerate(rows):
        src = sources[i % len(sources)]
        # treat action intensity as recall score proxy
        candidates.append(Candidate(item_id=movie_id, score=strength, source=src))

        # explicit negative feedback and low ratings become hard negatives
        y = 1.0 if strength >= 0.8 else 0.0
        if source_kind == "rating" and rating is not None:
            y = 1.0 if rating >= 8 else 0.0
        if action_type == "dislike":
            y = 0.0
        labels.append(y)

    pos_cnt = sum(1 for x in labels if x > 0.5)
    neg_cnt = len(labels) - pos_cnt
    _log_event(
        "info",
        "train.xgb.samples_ready",
        negative=neg_cnt,
        positive=pos_cnt,
        rows=len(labels),
        stage="dataset",
    )
    if pos_cnt == 0 or neg_cnt == 0:
        _log_event(
            "warning",
            "train.xgb.label_insufficient",
            negative=neg_cnt,
            positive=pos_cnt,
            reason="insufficient_label_diversity",
            status="skipped",
        )
        return TrainOutcome(
            component="ranking",
            name="xgb",
            artifact_path=None,
            trained=False,
            details={
                "skipped": True,
                "reason": "insufficient_label_diversity",
                "positive": pos_cnt,
                "negative": neg_cnt,
            },
        )

    movie_ids = [c.item_id for c in candidates]
    movie_features = (
        fetch_movie_features(movie_ids, mysql_dsn=settings.mysql_dsn) if settings.xgb_use_mysql_features else {}
    )

    builder = ManualFeatureBuilder(config=ManualFeatureConfig(include_mysql_movie_features=settings.xgb_use_mysql_features))

    # We build per-user contexts. For simplicity, use a single ctx with has_user=1.
    ctx = RequestContext(user_id=rows[0][0], n=10)
    feat_rows = builder.build_rows(ctx, candidates, movie_features)
    X = np.asarray(builder.to_matrix(feat_rows), dtype=float)
    y = np.asarray(labels, dtype=float)

    try:
        split_idx = _binary_train_test_split_indices(y.tolist(), train_ratio=0.8)
    except Exception:
        split_idx = _simple_train_test_split_indices(len(y), train_ratio=0.8)

    train_idx, test_idx = split_idx
    _log_event(
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
        "max_depth": settings.xgb_train_max_depth,
        "eta": settings.xgb_train_eta,
        "subsample": settings.xgb_train_subsample,
        "colsample_bytree": settings.xgb_train_colsample,
        "seed": 20260106,
    }

    num_boost_round = settings.xgb_train_rounds
    _log_event("info", "train.xgb.fit_start", boost_rounds=num_boost_round, stage="fit")
    booster = xgb.train(params, dtrain, num_boost_round=num_boost_round)
    test_pred = booster.predict(dtest)
    test_auc = _binary_auc(y_test.tolist(), test_pred.tolist())
    booster.save_model(artifact_path)
    _log_event(
        "info",
        "train.xgb.eval_done",
        artifact_path=artifact_path,
        stage="evaluate",
        test_auc=test_auc,
    )

    store.set("ranking.xgb.latest_artifact_path", artifact_path)
    store.set("ranking.xgb.latest_trained_at", ts)
    elapsed_ms = int((time.time() - started_at) * 1000)
    _log_event("info", "train.xgb.done", elapsed_ms=elapsed_ms, stage="finalize", status="completed")

    return TrainOutcome(
        component="ranking",
        name="xgb",
        artifact_path=artifact_path,
        trained=True,
        details={
            "rows": len(rows),
            "positive": pos_cnt,
            "negative": neg_cnt,
            "feature_count": X.shape[1],
            "boost_rounds": num_boost_round,
            "train_rows": len(train_idx),
            "test_rows": len(test_idx),
            "test_auc": test_auc,
        },
    )


# ----------------------------
# Two-tower ANN index
# ----------------------------


def _fetch_mmoe_training_rows(
    *,
    settings: Settings,
) -> List[Dict[str, Any]]:
    """Fetch recent per-target rows and aggregate them into multi-task samples.

    To reduce database pressure on large tables, source queries are kept index-friendly:
    each target reads only the latest ``mmoe_train_limit`` rows from its canonical fact
    table, without joining user/movie dimensions in the hot path.
    """

    try:
        import numpy as np
    except Exception as e:
        _log_exception("train.mmoe.deps_failed", e, stage="dataset")
        raise RuntimeError(f"mmoe_dataset_dependency_failed: {type(e).__name__}: {e}") from e

    engine = _get_mysql_engine(settings.mysql_dsn)
    if engine is None:
        err = RuntimeError("mysql_not_configured")
        _log_exception("train.mmoe.mysql_unavailable", err, stage="dataset")
        raise err

    limit_n = settings.mmoe_train_limit
    neg_ratio = settings.mmoe_global_neg_ratio
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
        with _catch_and_reraise("train.mmoe.row_parse_failed", "mmoe_training_row_parse_failed", stage="dataset"):
            return int(m.get("user_id") or 0), int(m.get("movie_id") or 0), m.get("event_time")

    def _parse_uid_mid_rating_event(row: Any) -> tuple[int, int, int, Any]:
        m = row._mapping
        with _catch_and_reraise("train.mmoe.row_parse_failed", "mmoe_training_row_parse_failed", stage="dataset"):
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
        _log_exception("train.mmoe.fetch_rows_failed", e, stage="dataset", train_limit=limit_n)
        raise RuntimeError(f"mmoe_fetch_training_rows_failed: {type(e).__name__}: {e}") from e

    _log_event(
        "info",
        "train.mmoe.target_rows_fetched",
        click_rows=target_fetch_counts["click"],
        collect_rows=target_fetch_counts["collect"],
        comment_rows=target_fetch_counts["comment"],
        rating_rows=target_fetch_counts["rating"],
        stage="dataset",
        train_limit=limit_n,
    )

    positives = list(by_pair.values())
    if not positives:
        err = RuntimeError("mmoe_training_rows_empty")
        _log_exception("train.mmoe.empty_rows", err, stage="dataset")
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

    _log_event(
        "info",
        "train.mmoe.dataset_built",
        click_positive=sum(1 for r in out if r["click"] > 0.5),
        collect_positive=sum(1 for r in out if r["collect"] > 0.5),
        comment_positive=sum(1 for r in out if r["comment"] > 0.5),
        global_movie_pool=len(movie_pool),
        global_neg_ratio=neg_ratio,
        global_negative=sum(1 for r in out if r["click"] <= 0.5),
        rate_positive=sum(1 for r in out if r["rating"] > 0.5),
        rows=len(out),
        stage="dataset",
    )
    return out


def _fetch_mmoe_aux_training_features(
    *,
    settings: Settings,
    user_ids: Sequence[int],
    movie_ids: Sequence[int],
) -> Dict[str, Any]:
    engine = _get_mysql_engine(settings.mysql_dsn)
    if engine is None:
        err = RuntimeError("mysql_engine_unavailable")
        _log_exception("train.mmoe.aux_mysql_unavailable", err, stage="aux_features")
        raise err

    uid_list = sorted({uid for uid in map(int, user_ids) if uid > 0})
    mid_list = sorted({mid for mid in map(int, movie_ids) if mid > 0})
    if not uid_list or not mid_list:
        err = ValueError("empty_user_or_movie_ids")
        _log_exception("train.mmoe.aux_invalid_ids", err, users=len(uid_list), movies=len(mid_list), stage="aux_features")
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

    _log_event(
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
        _log_exception("train.mmoe.aux_query_failed", e, stage="aux_features")
        raise RuntimeError(f"mmoe_aux_feature_query_failed: {type(e).__name__}: {e}") from e

    _log_event(
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


def _train_mmoe(settings: Settings) -> TrainOutcome:
    store = get_artifact_store()
    started_at = time.time()

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join("data", "artifacts", "mmoe")
    os.makedirs(out_dir, exist_ok=True)
    artifact_path = os.path.join(out_dir, f"mmoe_{ts}.pt")
    _log_event(
        "info",
        "train.mmoe.start",
        artifact_path=artifact_path,
        batch_size=settings.mmoe_train_batch_size,
        epochs=settings.mmoe_train_epochs,
        lr=settings.mmoe_train_lr,
        stage="prepare",
        train_limit=settings.mmoe_train_limit,
    )

    try:
        import torch
        from torch import nn

        from app.reco.ranking.mmoe_ranker import MMoENet, bundle_feature_order
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
        _log_exception("train.mmoe.deps_failed", e, stage="prepare", status="failed")
        raise RuntimeError(f"mmoe_dependency_failed: {type(e).__name__}: {e}") from e

    rows = _fetch_mmoe_training_rows(settings=settings)
    if not rows:
        _log_event("warning", "train.mmoe.empty_data", reason="no_training_data_or_mysql_not_configured", status="skipped")
        return TrainOutcome(
            component="ranking",
            name="mmoe",
            artifact_path=None,
            trained=False,
            details={"skipped": True, "reason": "no_training_data_or_mysql_not_configured"},
        )

    user_ids_all = [r["user_id"] for r in rows]
    movie_ids_all = [r["movie_id"] for r in rows]
    _log_event(
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
    _log_event(
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
                _log_event(
                    "warning",
                    "train.mmoe.birth_parse_failed",
                    error=f"{type(e).__name__}: {e}",
                    stage="feature_build",
                    user_id=uid,
                )
                user_age = None
        user_age_bucket = bucketize_age(user_age)

        source = row["source"]
        raw = {
            "recall_score": row["recall_score"],
            "movie_rating_avg": safe_float(mf.get("rating_avg")),
            "movie_rating_count": safe_float(mf.get("rating_count")),
            "movie_comment_count": safe_float(mf.get("comment_count")),
            "movie_click_count": safe_float(mf.get("click_count")),
            "movie_click_1h": safe_float(mf.get("click_1h")),
            "movie_click_24h": safe_float(mf.get("click_24h")),
            "movie_year": safe_float(mf.get("year")),
            "movie_duration_min": safe_float(mf.get("duration_min")),
            "user_static_tag_ctr": 0.0,
            "src_user_collection": 1.0 if source == "user_collection" else 0.0,
            "src_user_high_rating_similar": 1.0 if source == "user_high_rating_similar" else 0.0,
            "src_user_interest_tag": 1.0 if source in {"user_interest_tag", "recent_interaction"} else 0.0,
            "src_item_similar_by_tags": 1.0 if source == "item_similar_by_tags" else 0.0,
            "src_two_tower": 1.0 if source == "two_tower" else 0.0,
        }

        numeric_raw.append([raw[name] for name in feature_names])
        labels.append(
            [
                row["click"],
                row["collect"],
                row["comment"],
                row["rating"],
            ]
        )
        user_values.append(uid)
        item_values.append(mid)
        gender_values.append(user_gender)
        age_bucket_values.append(user_age_bucket)
        item_tag_raw_rows.append(pad_or_truncate(item_tags, size=ITEM_TAG_SEQ_LEN))

    _log_event(
        "info",
        "train.mmoe.aux_defaults_applied",
        long_interest_defaulted=len(rows),
        short_hist_defaulted=len(rows),
        stage="feature_build",
        user_static_tag_ctr_defaulted=len(rows),
    )
    if missing_movie_stats_cnt > 0:
        _log_event(
            "warning",
            "train.mmoe.movie_stats_missing_summary",
            missing_items=missing_movie_stats_cnt,
            stage="feature_build",
            total=len(rows),
        )
    if missing_user_profile_cnt > 0:
        _log_event(
            "warning",
            "train.mmoe.user_profile_missing_summary",
            missing_users=missing_user_profile_cnt,
            stage="feature_build",
            total=len(rows),
        )
    if missing_item_static_tags_cnt > 0:
        _log_event(
            "warning",
            "train.mmoe.item_static_tags_missing_summary",
            missing_items=missing_item_static_tags_cnt,
            stage="feature_build",
            total=len(rows),
        )

    _log_event(
        "info",
        "train.mmoe.sample_features_built",
        rows=len(rows),
        stage="feature_build",
        tags_vocab=len(all_tag_ids),
        users_with_profile=len(user_profile_by_id),
    )

    total_rows = len(labels)
    if total_rows <= 1:
        return TrainOutcome(
            component="ranking",
            name="mmoe",
            artifact_path=None,
            trained=False,
            details={"skipped": True, "reason": "insufficient_training_rows", "rows": total_rows},
        )

    pos_click = sum(1 for v in labels if v[0] > 0.5)
    pos_collect = sum(1 for v in labels if v[1] > 0.5)
    pos_comment = sum(1 for v in labels if v[2] > 0.5)
    pos_rating = sum(1 for v in labels if v[3] > 0.5)
    neg_click = total_rows - pos_click
    neg_collect = total_rows - pos_collect
    neg_comment = total_rows - pos_comment
    neg_rating = total_rows - pos_rating

    _log_event(
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
        return TrainOutcome(
            component="ranking",
            name="mmoe",
            artifact_path=None,
            trained=False,
            details={
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
        )

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
            _log_exception("train.mmoe.user_index_missing", err, user_id=uid, stage="feature_build")
            raise err
        if mid not in item_index:
            err = RuntimeError("item_index_missing")
            _log_exception("train.mmoe.item_index_missing", err, item_id=mid, stage="feature_build")
            raise err
        if g not in gender_index:
            err = RuntimeError("gender_index_missing")
            _log_exception("train.mmoe.gender_index_missing", err, gender=g, stage="feature_build")
            raise err
        if a not in age_bucket_index:
            err = RuntimeError("age_bucket_index_missing")
            _log_exception("train.mmoe.age_bucket_index_missing", err, age_bucket=a, stage="feature_build")
            raise err
        user_idx.append(int(user_index[uid]))
        item_idx.append(int(item_index[mid]))
        gender_idx.append(int(gender_index[g]))
        age_bucket_idx.append(int(age_bucket_index[a]))

    split_idx = _simple_train_test_split_indices(len(labels), train_ratio=0.8)
    train_idx, test_idx = split_idx
    _log_event(
        "info",
        "train.mmoe.split_done",
        feature_count=len(feature_names),
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
        emb_dim=settings.mmoe_emb_dim,
        num_experts=settings.mmoe_num_experts,
        expert_hidden_dim=settings.mmoe_expert_hidden_dim,
        tower_hidden_dim=settings.mmoe_tower_hidden_dim,
        gender_vocab_size=max(gender_index.values()) + 1,
        age_bucket_vocab_size=max(age_bucket_index.values()) + 1,
        tag_vocab_size=max(tag_index.values()) + 1,
        use_item_tag_pooling=True,
        use_target_attention=True,
        use_long_interest_pooling=True,
    )
    optimizer = torch.optim.Adam(model.parameters(), lr=settings.mmoe_train_lr)
    bce = nn.BCELoss()

    epochs = settings.mmoe_train_epochs
    batch_size = settings.mmoe_train_batch_size
    in_batch_neg_ratio = settings.mmoe_in_batch_neg_ratio
    w_click = settings.mmoe_loss_weight_click if pos_click > 0 else 0.0
    w_collect = settings.mmoe_loss_weight_collect if pos_collect > 0 else 0.0
    w_rate = settings.mmoe_loss_weight_rate if pos_rating > 0 else 0.0
    w_comment = settings.mmoe_loss_weight_comment if pos_comment > 0 else 0.0
    n = len(train_idx)
    train_idx_tensor = torch.tensor(train_idx, dtype=torch.long)
    test_idx_tensor = torch.tensor(test_idx, dtype=torch.long)
    _log_event(
        "info",
        "train.mmoe.task_weight_adjusted",
        click_enabled=w_click > 0.0,
        collect_enabled=w_collect > 0.0,
        comment_enabled=w_comment > 0.0,
        rating_enabled=w_rate > 0.0,
        stage="fit",
    )
    _log_event(
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

            click_pos_loss = bce(pred["click"], label_tensor[idx, 0]) if w_click > 0.0 else pred["click"].sum() * 0.0
            click_neg_loss = pred["click"].new_tensor(0.0)
            collect_neg_loss = pred["click"].new_tensor(0.0)
            comment_neg_loss = pred["click"].new_tensor(0.0)
            rating_neg_loss = pred["click"].new_tensor(0.0)

            # In-batch negative sampling for all tasks:
            # keep user-side features, but roll item-side features within the same batch.
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
                bce(pred["collect"], label_tensor[idx, 1]) if w_collect > 0.0 else pred["collect"].sum() * 0.0
            )
            comment_pos_loss = (
                bce(pred["comment"], label_tensor[idx, 2]) if w_comment > 0.0 else pred["comment"].sum() * 0.0
            )
            rating_pos_loss = bce(pred["rating"], label_tensor[idx, 3]) if w_rate > 0.0 else pred["rating"].sum() * 0.0

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

        avg_loss = (epoch_loss_sum / epoch_steps)
        _log_event(
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
    auc_click = _safe_binary_auc(
        task_name="click",
        y_true=y_test[:, 0].tolist(),
        y_score=test_pred["click"].cpu().numpy().tolist(),
    )
    auc_collect = _safe_binary_auc(
        task_name="collect",
        y_true=y_test[:, 1].tolist(),
        y_score=test_pred["collect"].cpu().numpy().tolist(),
    )
    auc_comment = _safe_binary_auc(
        task_name="comment",
        y_true=y_test[:, 2].tolist(),
        y_score=test_pred["comment"].cpu().numpy().tolist(),
    )
    auc_rating = _safe_binary_auc(
        task_name="rating",
        y_true=y_test[:, 3].tolist(),
        y_score=test_pred["rating"].cpu().numpy().tolist(),
    )
    auc_values = [x for x in [auc_click, auc_collect, auc_comment, auc_rating] if x is not None]
    auc_mean = float(sum(auc_values) / len(auc_values)) if auc_values else None
    _log_event(
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
            "emb_dim": settings.mmoe_emb_dim,
            "num_experts": settings.mmoe_num_experts,
            "expert_hidden_dim": settings.mmoe_expert_hidden_dim,
            "tower_hidden_dim": settings.mmoe_tower_hidden_dim,
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
    _log_event("info", "train.mmoe.model_saved", artifact_path=artifact_path, stage="finalize")

    store.set("ranking.mmoe.latest_artifact_path", artifact_path)
    store.set("ranking.mmoe.latest_trained_at", ts)
    elapsed_ms = int((time.time() - started_at) * 1000)
    _log_event("info", "train.mmoe.done", elapsed_ms=elapsed_ms, stage="finalize", status="completed")

    return TrainOutcome(
        component="ranking",
        name="mmoe",
        artifact_path=artifact_path,
        trained=True,
        details={
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
    )


def _two_tower_active_index_path(settings: Settings) -> str:
    return settings.two_tower_index_path or os.path.join("data", "two_tower_items.hnsw")


def _train_two_tower_index(settings: Settings) -> TrainOutcome:
    store = get_artifact_store()
    started_at = time.time()

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join("data", "artifacts", "two_tower")
    os.makedirs(out_dir, exist_ok=True)
    artifact_model_path = os.path.join(out_dir, f"two_tower_{ts}.pt")
    artifact_index_path = os.path.join(out_dir, f"two_tower_{ts}.hnsw")
    artifact_vector_db_path = os.path.join(out_dir, f"two_tower_{ts}.db")
    _log_event(
        "info",
        "train.two_tower.start",
        index_path=artifact_index_path,
        model_path=artifact_model_path,
        stage="prepare",
        vector_db_path=artifact_vector_db_path,
    )

    try:
        from app.reco.recall.two_tower import (
            load_config_from_settings,
            materialize_item_vectors_from_model,
            save_model_weights,
            train_two_tower_model,
        )
    except Exception as e:  # noqa: BLE001
        _log_exception("train.two_tower.deps_failed", e, stage="prepare", status="failed")
        raise RuntimeError(f"two_tower_dependency_failed: {type(e).__name__}: {e}") from e

    cfg = load_config_from_settings(settings)
    _log_event(
        "info",
        "train.two_tower.config_loaded",
        dim=cfg.dim,
        recall_topk=cfg.recall_topk,
        stage="prepare",
        train_batch_size=cfg.train_batch_size,
        train_epochs=cfg.train_epochs,
    )

    try:
        model, train_metrics = train_two_tower_model(cfg, mysql_dsn=settings.mysql_dsn)
        _log_event("info", "train.two_tower.fit_done", metrics=train_metrics, stage="fit")
        save_model_weights(model, artifact_model_path)
        _log_event("info", "train.two_tower.model_saved", model_path=artifact_model_path, stage="finalize")
        count = materialize_item_vectors_from_model(
            cfg=cfg,
            model_path=artifact_model_path,
            vector_db_path=artifact_vector_db_path,
            index_path=artifact_index_path,
        )
        _log_event("info", "train.two_tower.index_done", items_indexed=int(count), stage="finalize")
    except Exception as e:  # noqa: BLE001
        _log_exception("train.two_tower.failed", e, stage="fit", status="failed")
        raise RuntimeError(f"two_tower_train_failed: {type(e).__name__}: {e}") from e

    store.set("recall.two_tower.latest_model_artifact_path", artifact_model_path)
    store.set("recall.two_tower.latest_index_artifact_path", artifact_index_path)
    store.set("recall.two_tower.latest_vector_db_artifact_path", artifact_vector_db_path)
    store.set("recall.two_tower.latest_trained_at", ts)
    elapsed_ms = int((time.time() - started_at) * 1000)
    _log_event("info", "train.two_tower.done", elapsed_ms=elapsed_ms, stage="finalize", status="completed")

    return TrainOutcome(
        component="recall",
        name="two_tower",
        artifact_path=artifact_model_path,
        trained=True,
        details={
            "items_indexed": int(count),
            "model_path": artifact_model_path,
            "index_path": artifact_index_path,
            "vector_db_path": artifact_vector_db_path,
            **train_metrics,
        },
    )


def create_model_train_job(*, mysql_dsn: str | None, mode: str = "full") -> int:
    engine = _get_mysql_engine(mysql_dsn)
    if engine is None:
        raise RuntimeError("mysql_not_configured_for_model_train_job")

    sql = """
    INSERT INTO model_train_job(mode, status)
    VALUES (:mode, 'pending')
    """
    try:
        with engine.begin() as conn:
            rs = conn.execute(text(sql), {"mode": str(mode)})
            new_id = rs.lastrowid
    except SQLAlchemyError as e:
        raise RuntimeError(f"create_model_train_job_failed: {e}") from e

    if new_id is None:
        raise RuntimeError("create_model_train_job_failed: empty_insert_id")
    return int(new_id)


def update_model_train_job(
    *,
    mysql_dsn: str | None,
    job_id: int,
    status: str,
    metrics: Dict[str, Any] | None = None,
    set_finished_at: bool = False,
) -> None:
    engine = _get_mysql_engine(mysql_dsn)
    if engine is None:
        raise RuntimeError("mysql_not_configured_for_model_train_job")

    updates = ["status = :status"]
    params: Dict[str, Any] = {"status": str(status), "job_id": int(job_id)}

    if metrics is not None:
        updates.append("metrics = CAST(:metrics AS JSON)")
        params["metrics"] = json.dumps(metrics, ensure_ascii=False)
    if set_finished_at:
        updates.append("finished_at = CURRENT_TIMESTAMP")

    sql = f"UPDATE model_train_job SET {', '.join(updates)} WHERE id = :job_id"

    try:
        with engine.begin() as conn:
            conn.execute(text(sql), params)
    except SQLAlchemyError as e:
        raise RuntimeError(f"update_model_train_job_failed: {e}") from e


def get_model_train_job(*, mysql_dsn: str | None, job_id: int) -> Dict[str, Any] | None:
    engine = _get_mysql_engine(mysql_dsn)
    if engine is None:
        err = RuntimeError("mysql_not_configured_for_model_train_job")
        _log_exception("train.job.mysql_unavailable", err, job_id=job_id)
        raise err

    sql = """
    SELECT id, mode, status, metrics, created_at, finished_at
    FROM model_train_job
    WHERE id = :job_id
    LIMIT 1
    """

    try:
        with engine.connect() as conn:
            row = conn.execute(text(sql), {"job_id": int(job_id)}).mappings().first()
    except SQLAlchemyError as e:
        _log_exception("train.job.get_failed", e, job_id=job_id)
        raise RuntimeError(f"get_model_train_job_failed: {type(e).__name__}: {e}") from e

    if row is None:
        err = RuntimeError("model_train_job_not_found")
        _log_exception("train.job.not_found", err, job_id=job_id)
        raise err

    created_at = row.get("created_at")
    finished_at = row.get("finished_at")
    metrics = row.get("metrics")
    if metrics is None:
        err = RuntimeError("model_train_job_metrics_missing")
        _log_exception("train.job.metrics_missing", err, job_id=job_id)
        raise err
    if isinstance(metrics, str):
        try:
            metrics = json.loads(metrics)
        except Exception as e:
            _log_exception("train.job.metrics_parse_failed", e, job_id=job_id)
            raise RuntimeError(f"model_train_job_metrics_parse_failed: {type(e).__name__}: {e}") from e

    return {
        "id": int(row.get("id")),
        "mode": row.get("mode"),
        "status": row.get("status"),
        "metrics": metrics,
        "created_at": created_at.isoformat() if created_at is not None else None,
        "finished_at": finished_at.isoformat() if finished_at is not None else None,
    }


def list_model_train_jobs(
    *,
    mysql_dsn: str | None,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> List[Dict[str, Any]]:
    engine = _get_mysql_engine(mysql_dsn)
    if engine is None:
        err = RuntimeError("mysql_not_configured_for_model_train_job")
        _log_exception("train.job.list_mysql_unavailable", err, limit=limit, offset=offset, status=status)
        raise err

    clauses = []
    params: Dict[str, Any] = {
        "limit": int(limit),
        "offset": int(offset),
    }

    if status:
        clauses.append("status = :status")
        params["status"] = str(status)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
    SELECT id, mode, status, metrics, created_at, finished_at
    FROM model_train_job
    {where_sql}
    ORDER BY id DESC
    LIMIT :limit OFFSET :offset
    """

    try:
        with engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
    except SQLAlchemyError as e:
        _log_exception("train.job.list_failed", e, limit=limit, offset=offset, status=status)
        raise RuntimeError(f"list_model_train_jobs_failed: {type(e).__name__}: {e}") from e

    out: List[Dict[str, Any]] = []
    for row in rows:
        created_at = row.get("created_at")
        finished_at = row.get("finished_at")
        metrics = row.get("metrics")
        if metrics is None:
            err = RuntimeError("model_train_job_metrics_missing")
            _log_exception("train.job.list_metrics_missing", err, row_id=row.get("id"))
            raise err
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except Exception as e:
                _log_exception("train.job.list_metrics_parse_failed", e, row_id=row.get("id"))
                raise RuntimeError(f"model_train_job_list_metrics_parse_failed: {type(e).__name__}: {e}") from e

        out.append(
            {
                "id": int(row.get("id")),
                "mode": row.get("mode"),
                "status": row.get("status"),
                "metrics": metrics,
                "created_at": created_at.isoformat() if created_at is not None else None,
                "finished_at": finished_at.isoformat() if finished_at is not None else None,
            }
        )

    return out
