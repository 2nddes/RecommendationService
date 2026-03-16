from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from typing import Any, Dict, List, Sequence, Tuple

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.common.settings import Settings
from app.ops.artifact_store import get_artifact_store



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

    if train_job_id is not None:
        update_model_train_job(
            mysql_dsn=settings.mysql_dsn,
            job_id=int(train_job_id),
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

        if not bool(outcome.trained):
            details = outcome.details if isinstance(outcome.details, dict) else {}
            reason = details.get("reason") or details.get("error") or "train_outcome_not_trained"
            raise RuntimeError(str(reason))

        if train_job_id is not None:
            update_model_train_job(
                mysql_dsn=settings.mysql_dsn,
                job_id=int(train_job_id),
                status="completed",
                metrics=result,
                set_finished_at=True,
            )
        return result
    except Exception as e:  # noqa: BLE001
        if train_job_id is not None:
            update_model_train_job(
                mysql_dsn=settings.mysql_dsn,
                job_id=int(train_job_id),
                status="failed",
                metrics={
                    "error": f"{type(e).__name__}: {e}",
                    "component": component,
                    "model": model,
                },
                set_finished_at=True,
            )
        raise


def refresh_current_models(settings: Settings) -> Dict[str, Any]:
    ranking_method = str(settings.ranking_method or "").strip().lower()
    recall_channels = [str(ch).strip().lower() for ch in (settings.recall_channels or [])]

    try:
        if ranking_method == "xgb":
            from app.reco.ranking.xgb_ranker import load_latest_local_model as load_latest_xgb_local_model

            model_path = load_latest_xgb_local_model(settings)
            if not model_path:
                return {"status": "failed", "reason": "xgb_model_not_found"}

        if ranking_method == "mmoe":
            from app.reco.ranking.mmoe_ranker import load_latest_local_model as load_latest_mmoe_local_model

            model_path = load_latest_mmoe_local_model(settings)
            if not model_path:
                return {"status": "failed", "reason": "mmoe_model_not_found"}

        if "two_tower" in recall_channels:
            from app.reco.recall.two_tower import load_latest_local_model as load_latest_two_tower_local_model

            model_path = load_latest_two_tower_local_model(settings)
            if not model_path:
                return {"status": "failed", "reason": "two_tower_model_not_found"}

        return {"status": "completed", "reason": None}
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "reason": f"{type(e).__name__}: {e}"}



# ----------------------------
# XGBoost
# ----------------------------


def _get_mysql_engine(mysql_dsn: str | None) -> Engine | None:
    dsn = mysql_dsn
    if not dsn:
        return None
    try:
        return create_engine(str(dsn), pool_pre_ping=True)
    except Exception:
        return None


def _binary_auc(y_true: Sequence[float], y_score: Sequence[float]) -> float | None:
    if len(y_true) != len(y_score) or len(y_true) == 0:
        return None

    pairs = [(float(y), float(s)) for y, s in zip(y_true, y_score)]
    pos_count = sum(1 for y, _ in pairs if y > 0.5)
    neg_count = len(pairs) - pos_count
    if pos_count == 0 or neg_count == 0:
        return None

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
    return float(max(min(auc, 1.0), 0.0))


def _binary_train_test_split_indices(y: Sequence[float], train_ratio: float = 0.8) -> tuple[List[int], List[int]] | None:
    pos = [i for i, v in enumerate(y) if float(v) > 0.5]
    neg = [i for i, v in enumerate(y) if float(v) <= 0.5]
    if not pos or not neg:
        return None

    def _split(group: List[int]) -> tuple[List[int], List[int]]:
        if len(group) <= 1:
            return group[:], []
        cut = int(len(group) * float(train_ratio))
        cut = min(max(cut, 1), len(group) - 1)
        return group[:cut], group[cut:]

    pos_train, pos_test = _split(pos)
    neg_train, neg_test = _split(neg)

    train_idx = pos_train + neg_train
    test_idx = pos_test + neg_test
    if not train_idx or not test_idx:
        return None
    return train_idx, test_idx


def _simple_train_test_split_indices(total: int, train_ratio: float = 0.8) -> tuple[List[int], List[int]] | None:
    n = int(total)
    if n <= 1:
        return None
    cut = int(n * float(train_ratio))
    cut = min(max(cut, 1), n - 1)
    train_idx = list(range(0, cut))
    test_idx = list(range(cut, n))
    if not train_idx or not test_idx:
        return None
    return train_idx, test_idx


def _interaction_strength(*, action_type: str, rating: int | None, source_kind: str) -> float:
    if source_kind == "rating" and rating is not None:
        # map 1~10 to [-0.8, 1.0], keep low-score ratings as hard negatives
        return max(min((float(rating) - 5.0) / 5.0, 1.0), -0.8)

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
    return float(action_weight.get(str(action_type), 0.1))


def _fetch_xgb_training_rows(*, mysql_dsn: str | None, limit: int = 5000) -> List[Tuple[int, int, str, int | None, str, float]]:
    """Return tuples: (user_id, movie_id, action_type, rating, source_kind, strength)."""

    engine = _get_mysql_engine(mysql_dsn)
    if engine is None:
        return []

    sql = """
    SELECT t.user_id,
           t.movie_id,
           t.action_type,
           t.rating,
           t.source_kind,
           t.event_time
    FROM (
        SELECT ua.user_id AS user_id,
               ua.movie_id AS movie_id,
               ua.action_type AS action_type,
               NULL AS rating,
               'action' AS source_kind,
               ua.created_at AS event_time
        FROM user_action ua
        WHERE ua.movie_id IS NOT NULL
                    AND ua.action_type <> 'rate'

        UNION ALL

        SELECT r.user_id AS user_id,
               r.movie_id AS movie_id,
               'rate' AS action_type,
               r.rating AS rating,
               'rating' AS source_kind,
               r.updated_at AS event_time
        FROM rating r
        WHERE r.movie_id IS NOT NULL

        UNION ALL

        SELECT ucm.user_id AS user_id,
               ucm.movie_id AS movie_id,
               'collect' AS action_type,
               NULL AS rating,
               'collect' AS source_kind,
               ucm.created_at AS event_time
        FROM user_collect_movie ucm
        WHERE ucm.movie_id IS NOT NULL
    ) t
    ORDER BY t.event_time DESC
    LIMIT :limit
    """

    try:
        with engine.connect() as conn:
            rs = conn.execute(text(sql), {"limit": int(limit)})
            best_by_pair: Dict[Tuple[int, int], Dict[str, Any]] = {}
            for row in rs:
                d = dict(row._mapping)
                try:
                    user_id = int(d.get("user_id"))
                    movie_id = int(d.get("movie_id"))
                    action_type = str(d.get("action_type") or "view")
                    source_kind = str(d.get("source_kind") or "action")
                    rating = int(d["rating"]) if d.get("rating") is not None else None
                    event_time = d.get("event_time")

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
                        prev_priority = int(prev.get("source_priority") or 0)
                        prev_strength = float(prev.get("strength") or 0.0)
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
                except Exception:
                    continue

            dedup_rows = list(best_by_pair.values())
            dedup_rows.sort(key=lambda x: x.get("event_time") or datetime.min, reverse=True)
            dedup_rows = dedup_rows[: max(int(limit), 1)]

            out: List[Tuple[int, int, str, int | None, str, float]] = []
            for row in dedup_rows:
                out.append(
                    (
                        int(row["user_id"]),
                        int(row["movie_id"]),
                        str(row["action_type"]),
                        int(row["rating"]) if row.get("rating") is not None else None,
                        str(row["source_kind"]),
                        float(row["strength"]),
                    )
                )
            return out
    except SQLAlchemyError:
        return []


def _train_xgb(settings: Settings) -> TrainOutcome:
    store = get_artifact_store()

    # Decide output location (staging)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join("data", "artifacts", "xgb")
    os.makedirs(out_dir, exist_ok=True)
    artifact_path = os.path.join(out_dir, f"xgb_{ts}.json")

    try:
        import numpy as np
        import xgboost as xgb

        from app.reco.ranking.xgb_features import ManualFeatureBuilder, ManualFeatureConfig, fetch_movie_features
        from app.reco.types import Candidate, RequestContext
    except Exception as e:  # noqa: BLE001
        return TrainOutcome(
            component="ranking",
            name="xgb",
            artifact_path=None,
            trained=False,
            details={"skipped": True, "reason": f"deps_not_available: {type(e).__name__}: {e}"},
        )

    rows = _fetch_xgb_training_rows(mysql_dsn=settings.mysql_dsn, limit=int(settings.xgb_train_limit))
    if not rows:
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
        base = max(float(strength), 0.05)
        candidates.append(Candidate(item_id=int(movie_id), score=float(base), source=src))

        # explicit negative feedback and low ratings become hard negatives
        y = 1.0 if float(strength) >= 0.8 else 0.0
        if source_kind == "rating" and rating is not None:
            y = 1.0 if int(rating) >= 8 else 0.0
        if action_type == "dislike":
            y = 0.0
        labels.append(float(y))

    pos_cnt = sum(1 for x in labels if x > 0.5)
    neg_cnt = len(labels) - pos_cnt
    if pos_cnt == 0 or neg_cnt == 0:
        return TrainOutcome(
            component="ranking",
            name="xgb",
            artifact_path=None,
            trained=False,
            details={
                "skipped": True,
                "reason": "insufficient_label_diversity",
                "positive": int(pos_cnt),
                "negative": int(neg_cnt),
            },
        )

    movie_ids = [c.item_id for c in candidates]
    movie_features = (
        fetch_movie_features(movie_ids, mysql_dsn=settings.mysql_dsn) if settings.xgb_use_mysql_features else {}
    )

    builder = ManualFeatureBuilder(config=ManualFeatureConfig(include_mysql_movie_features=settings.xgb_use_mysql_features))

    # We build per-user contexts. For simplicity, use a single ctx with has_user=1.
    ctx = RequestContext(user_id=int(rows[0][0]), n=10)
    feat_rows = builder.build_rows(ctx, candidates, movie_features)
    X = np.asarray(builder.to_matrix(feat_rows), dtype=float)
    y = np.asarray(labels, dtype=float)

    split_idx = _binary_train_test_split_indices(y.tolist(), train_ratio=0.8)
    if split_idx is None:
        split_idx = _simple_train_test_split_indices(len(y), train_ratio=0.8)
    if split_idx is None:
        return TrainOutcome(
            component="ranking",
            name="xgb",
            artifact_path=None,
            trained=False,
            details={"skipped": True, "reason": "insufficient_data_for_train_test_split"},
        )

    train_idx, test_idx = split_idx
    X_train = X[train_idx]
    y_train = y[train_idx]
    X_test = X[test_idx]
    y_test = y[test_idx]

    dtrain = xgb.DMatrix(X_train, label=y_train, feature_names=builder.feature_names())
    dtest = xgb.DMatrix(X_test, label=y_test, feature_names=builder.feature_names())

    params = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "max_depth": int(settings.xgb_train_max_depth),
        "eta": float(settings.xgb_train_eta),
        "subsample": float(settings.xgb_train_subsample),
        "colsample_bytree": float(settings.xgb_train_colsample),
        "seed": 20260106,
    }

    num_boost_round = max(int(settings.xgb_train_rounds), 1)
    booster = xgb.train(params, dtrain, num_boost_round=num_boost_round)
    test_pred = booster.predict(dtest)
    test_auc = _binary_auc(y_test.tolist(), test_pred.tolist())
    booster.save_model(artifact_path)

    store.set("ranking.xgb.latest_artifact_path", artifact_path)
    store.set("ranking.xgb.latest_trained_at", ts)

    return TrainOutcome(
        component="ranking",
        name="xgb",
        artifact_path=artifact_path,
        trained=True,
        details={
            "rows": len(rows),
            "positive": int(pos_cnt),
            "negative": int(neg_cnt),
            "feature_count": int(X.shape[1]),
            "boost_rounds": int(num_boost_round),
            "train_rows": int(len(train_idx)),
            "test_rows": int(len(test_idx)),
            "test_auc": float(test_auc) if test_auc is not None else None,
        },
    )


# ----------------------------
# Two-tower ANN index
# ----------------------------


def _fetch_mmoe_training_rows(*, mysql_dsn: str | None, limit: int = 100000) -> List[Dict[str, Any]]:
    """Build multi-task labels from MySQL only.

    Labels:
    - click: action_type='view' -> 1
    - collect: collect action / user_collect_movie -> 1
    - comment: user_action.comment / movie_comment -> 1
    - rating: rating > 5 -> 1
    """

    engine = _get_mysql_engine(mysql_dsn)
    if engine is None:
        return []

    sql = """
    SELECT t.user_id,
           t.movie_id,
           t.event_type,
           t.rating,
           t.event_time
    FROM (
        SELECT ua.user_id AS user_id,
               ua.movie_id AS movie_id,
               'click' AS event_type,
               NULL AS rating,
               ua.created_at AS event_time
        FROM user_action ua
        WHERE ua.movie_id IS NOT NULL AND ua.action_type = 'view'

        UNION ALL

        SELECT ua.user_id AS user_id,
               ua.movie_id AS movie_id,
               'collect' AS event_type,
               NULL AS rating,
               ua.created_at AS event_time
        FROM user_action ua
        WHERE ua.movie_id IS NOT NULL AND ua.action_type = 'collect'

        UNION ALL

        SELECT ucm.user_id AS user_id,
               ucm.movie_id AS movie_id,
               'collect' AS event_type,
               NULL AS rating,
               ucm.created_at AS event_time
        FROM user_collect_movie ucm
        WHERE ucm.movie_id IS NOT NULL

        UNION ALL

        SELECT ua.user_id AS user_id,
               ua.movie_id AS movie_id,
               'comment' AS event_type,
               NULL AS rating,
               ua.created_at AS event_time
        FROM user_action ua
        WHERE ua.movie_id IS NOT NULL AND ua.action_type = 'comment'

        UNION ALL

        SELECT mc.user_id AS user_id,
               mc.movie_id AS movie_id,
               'comment' AS event_type,
               NULL AS rating,
               mc.created_at AS event_time
        FROM movie_comment mc
        WHERE mc.movie_id IS NOT NULL AND mc.deleted_at IS NULL

        UNION ALL

        SELECT r.user_id AS user_id,
               r.movie_id AS movie_id,
               'rating' AS event_type,
               r.rating AS rating,
               r.updated_at AS event_time
        FROM rating r
        WHERE r.movie_id IS NOT NULL
    ) t
    ORDER BY t.event_time DESC
    LIMIT :limit
    """

    try:
        with engine.connect() as conn:
            rs = conn.execute(text(sql), {"limit": max(int(limit), 1)})

            by_pair: Dict[Tuple[int, int], Dict[str, Any]] = {}
            for row in rs:
                d = dict(row._mapping)
                try:
                    user_id = int(d.get("user_id"))
                    movie_id = int(d.get("movie_id"))
                except Exception:
                    continue

                if user_id <= 0 or movie_id <= 0:
                    continue

                key = (user_id, movie_id)
                sample = by_pair.get(key)
                if sample is None:
                    sample = {
                        "user_id": user_id,
                        "movie_id": movie_id,
                        "click": 0.0,
                        "collect": 0.0,
                        "comment": 0.0,
                        "rating": 0.0,
                        "source": "user_collection",
                        "recall_score": 0.0,
                        "event_time": d.get("event_time"),
                    }
                    by_pair[key] = sample

                event_type = str(d.get("event_type") or "").strip().lower()
                if event_type == "click":
                    sample["click"] = 1.0
                    sample["source"] = "user_interest_tag"
                    sample["recall_score"] = max(float(sample["recall_score"]), 0.5)
                elif event_type == "collect":
                    sample["collect"] = 1.0
                    sample["source"] = "user_collection"
                    sample["recall_score"] = max(float(sample["recall_score"]), 0.9)
                elif event_type == "comment":
                    sample["comment"] = 1.0
                    sample["source"] = "user_interest_tag"
                    sample["recall_score"] = max(float(sample["recall_score"]), 0.8)
                elif event_type == "rating":
                    rating_raw = d.get("rating")
                    rating_val = int(rating_raw) if rating_raw is not None else 0
                    sample["rating"] = 1.0 if rating_val > 5 else 0.0
                    sample["source"] = "user_high_rating_similar"
                    sample["recall_score"] = max(float(sample["recall_score"]), min(max(rating_val, 0), 10) / 10.0)

            return list(by_pair.values())
    except SQLAlchemyError:
        return []


def _train_mmoe(settings: Settings) -> TrainOutcome:
    store = get_artifact_store()

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join("data", "artifacts", "mmoe")
    os.makedirs(out_dir, exist_ok=True)
    artifact_path = os.path.join(out_dir, f"mmoe_{ts}.pt")

    try:
        import torch
        from torch import nn

        from app.reco.ranking.mmoe_ranker import MMoENet, bundle_feature_order
        from app.reco.ranking.xgb_features import fetch_movie_features
    except Exception as e:  # noqa: BLE001
        return TrainOutcome(
            component="ranking",
            name="mmoe",
            artifact_path=None,
            trained=False,
            details={"skipped": True, "reason": f"deps_not_available: {type(e).__name__}: {e}"},
        )

    rows = _fetch_mmoe_training_rows(mysql_dsn=settings.mysql_dsn, limit=int(settings.mmoe_train_limit))
    if not rows:
        return TrainOutcome(
            component="ranking",
            name="mmoe",
            artifact_path=None,
            trained=False,
            details={"skipped": True, "reason": "no_training_data_or_mysql_not_configured"},
        )

    movie_ids = [int(r["movie_id"]) for r in rows]
    movie_features = fetch_movie_features(movie_ids, mysql_dsn=settings.mysql_dsn)

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

    for row in rows:
        uid = int(row["user_id"])
        mid = int(row["movie_id"])
        mf = movie_features.get(mid) or {}

        source = str(row.get("source") or "")
        raw = {
            "recall_score": float(row.get("recall_score") or 0.0),
            "movie_rating_avg": float(mf.get("rating_avg") or 0.0),
            "movie_rating_count": float(mf.get("rating_count") or 0.0),
            "movie_year": float(mf.get("year") or 0.0),
            "movie_duration_min": float(mf.get("duration_min") or 0.0),
            "src_user_collection": 1.0 if source == "user_collection" else 0.0,
            "src_user_high_rating_similar": 1.0 if source == "user_high_rating_similar" else 0.0,
            "src_user_interest_tag": 1.0 if source == "user_interest_tag" else 0.0,
            "src_item_similar_by_tags": 1.0 if source == "item_similar_by_tags" else 0.0,
            "src_two_tower": 1.0 if source == "two_tower" else 0.0,
        }

        numeric_raw.append([float(raw.get(name, 0.0)) for name in feature_names])
        labels.append(
            [
                float(row.get("click") or 0.0),
                float(row.get("collect") or 0.0),
                float(row.get("comment") or 0.0),
                float(row.get("rating") or 0.0),
            ]
        )
        user_values.append(uid)
        item_values.append(mid)

    total_rows = len(labels)
    if total_rows <= 1:
        return TrainOutcome(
            component="ranking",
            name="mmoe",
            artifact_path=None,
            trained=False,
            details={"skipped": True, "reason": "insufficient_training_rows", "rows": total_rows},
        )

    pos_click = sum(int(v[0] > 0.5) for v in labels)
    pos_collect = sum(int(v[1] > 0.5) for v in labels)
    pos_comment = sum(int(v[2] > 0.5) for v in labels)
    pos_rating = sum(int(v[3] > 0.5) for v in labels)
    if min(pos_click, pos_collect, pos_comment, pos_rating) == 0:
        return TrainOutcome(
            component="ranking",
            name="mmoe",
            artifact_path=None,
            trained=False,
            details={
                "skipped": True,
                "reason": "insufficient_task_positive_samples",
                "click_positive": int(pos_click),
                "collect_positive": int(pos_collect),
                "comment_positive": int(pos_comment),
                "rating_positive": int(pos_rating),
            },
        )

    user_vocab = sorted(set(user_values))
    item_vocab = sorted(set(item_values))
    user_index = {uid: i + 1 for i, uid in enumerate(user_vocab)}
    item_index = {mid: i + 1 for i, mid in enumerate(item_vocab)}

    feature_stats: Dict[str, Dict[str, float]] = {}
    for col_i, name in enumerate(feature_names):
        col = [float(r[col_i]) for r in numeric_raw]
        mean = sum(col) / max(len(col), 1)
        var = sum((x - mean) ** 2 for x in col) / max(len(col), 1)
        std = var ** 0.5
        if name in src_features:
            mean = 0.0
            std = 1.0
        if std <= 1e-8:
            std = 1.0
        feature_stats[name] = {"mean": float(mean), "std": float(std)}

    x_numeric = [
        [
            (float(row[col_i]) - feature_stats[name]["mean"]) / feature_stats[name]["std"]
            for col_i, name in enumerate(feature_names)
        ]
        for row in numeric_raw
    ]

    user_idx = [int(user_index.get(uid, 0)) for uid in user_values]
    item_idx = [int(item_index.get(mid, 0)) for mid in item_values]

    split_idx = _simple_train_test_split_indices(len(labels), train_ratio=0.8)
    if split_idx is None:
        return TrainOutcome(
            component="ranking",
            name="mmoe",
            artifact_path=None,
            trained=False,
            details={"skipped": True, "reason": "insufficient_data_for_train_test_split"},
        )
    train_idx, test_idx = split_idx

    user_tensor = torch.tensor(user_idx, dtype=torch.long)
    item_tensor = torch.tensor(item_idx, dtype=torch.long)
    numeric_tensor = torch.tensor(x_numeric, dtype=torch.float32)
    label_tensor = torch.tensor(labels, dtype=torch.float32)

    model = MMoENet(
        user_vocab_size=len(user_index) + 1,
        item_vocab_size=len(item_index) + 1,
        num_numeric_features=len(feature_names),
        emb_dim=int(settings.mmoe_emb_dim),
        num_experts=int(settings.mmoe_num_experts),
        expert_hidden_dim=int(settings.mmoe_expert_hidden_dim),
        tower_hidden_dim=int(settings.mmoe_tower_hidden_dim),
    )
    optimizer = torch.optim.Adam(model.parameters(), lr=float(settings.mmoe_train_lr))
    bce = nn.BCELoss()

    epochs = max(int(settings.mmoe_train_epochs), 1)
    batch_size = max(int(settings.mmoe_train_batch_size), 16)
    n = len(train_idx)
    train_idx_tensor = torch.tensor(train_idx, dtype=torch.long)
    test_idx_tensor = torch.tensor(test_idx, dtype=torch.long)

    model.train()
    for _ in range(epochs):
        order = train_idx_tensor[torch.randperm(n)]
        for start in range(0, n, batch_size):
            idx = order[start : start + batch_size]
            pred = model(user_tensor[idx], item_tensor[idx], numeric_tensor[idx])

            loss = (
                bce(pred["click"], label_tensor[idx, 0])
                + bce(pred["collect"], label_tensor[idx, 1])
                + bce(pred["comment"], label_tensor[idx, 2])
                + bce(pred["rating"], label_tensor[idx, 3])
            )

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

    model.eval()
    with torch.no_grad():
        test_pred = model(user_tensor[test_idx_tensor], item_tensor[test_idx_tensor], numeric_tensor[test_idx_tensor])

    y_test = label_tensor[test_idx_tensor].cpu().numpy()
    auc_click = _binary_auc(y_test[:, 0].tolist(), test_pred["click"].cpu().numpy().tolist())
    auc_collect = _binary_auc(y_test[:, 1].tolist(), test_pred["collect"].cpu().numpy().tolist())
    auc_comment = _binary_auc(y_test[:, 2].tolist(), test_pred["comment"].cpu().numpy().tolist())
    auc_rating = _binary_auc(y_test[:, 3].tolist(), test_pred["rating"].cpu().numpy().tolist())
    auc_values = [x for x in [auc_click, auc_collect, auc_comment, auc_rating] if x is not None]
    auc_mean = float(sum(auc_values) / len(auc_values)) if auc_values else None

    bundle = {
        "state_dict": model.state_dict(),
        "model_meta": {
            "user_vocab_size": len(user_index) + 1,
            "item_vocab_size": len(item_index) + 1,
            "num_numeric_features": len(feature_names),
            "emb_dim": int(settings.mmoe_emb_dim),
            "num_experts": int(settings.mmoe_num_experts),
            "expert_hidden_dim": int(settings.mmoe_expert_hidden_dim),
            "tower_hidden_dim": int(settings.mmoe_tower_hidden_dim),
        },
        "tasks": ["click", "collect", "comment", "rating"],
        "feature_order": feature_names,
        "feature_stats": feature_stats,
        "user_index": user_index,
        "item_index": item_index,
    }
    torch.save(bundle, artifact_path)

    store.set("ranking.mmoe.latest_artifact_path", artifact_path)
    store.set("ranking.mmoe.latest_trained_at", ts)

    return TrainOutcome(
        component="ranking",
        name="mmoe",
        artifact_path=artifact_path,
        trained=True,
        details={
            "rows": int(total_rows),
            "click_positive": int(pos_click),
            "collect_positive": int(pos_collect),
            "comment_positive": int(pos_comment),
            "rating_positive": int(pos_rating),
            "feature_count": int(len(feature_names)),
            "epochs": int(epochs),
            "batch_size": int(batch_size),
            "train_rows": int(len(train_idx)),
            "test_rows": int(len(test_idx)),
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

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join("data", "artifacts", "two_tower")
    os.makedirs(out_dir, exist_ok=True)
    artifact_model_path = os.path.join(out_dir, f"two_tower_{ts}.pt")
    artifact_index_path = os.path.join(out_dir, f"two_tower_{ts}.hnsw")
    artifact_vector_db_path = os.path.join(out_dir, f"two_tower_{ts}.db")

    try:
        from app.reco.recall.two_tower import (
            load_config_from_settings,
            materialize_item_vectors_from_model,
            save_model_weights,
            train_two_tower_model,
        )
    except Exception as e:  # noqa: BLE001
        return TrainOutcome(
            component="recall",
            name="two_tower",
            artifact_path=None,
            trained=False,
            details={"skipped": True, "reason": f"deps_not_available: {type(e).__name__}: {e}"},
        )

    cfg = load_config_from_settings(settings)

    try:
        model, train_metrics = train_two_tower_model(cfg, mysql_dsn=settings.mysql_dsn)
        save_model_weights(model, artifact_model_path)
        count = materialize_item_vectors_from_model(
            cfg=cfg,
            model_path=artifact_model_path,
            vector_db_path=artifact_vector_db_path,
            index_path=artifact_index_path,
        )
    except Exception as e:  # noqa: BLE001
        return TrainOutcome(
            component="recall",
            name="two_tower",
            artifact_path=None,
            trained=False,
            details={"failed": True, "reason": f"{type(e).__name__}: {e}"},
        )

    store.set("recall.two_tower.latest_model_artifact_path", artifact_model_path)
    store.set("recall.two_tower.latest_index_artifact_path", artifact_index_path)
    store.set("recall.two_tower.latest_vector_db_artifact_path", artifact_vector_db_path)
    store.set("recall.two_tower.latest_trained_at", ts)

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
        return None

    sql = """
    SELECT id, mode, status, metrics, created_at, finished_at
    FROM model_train_job
    WHERE id = :job_id
    LIMIT 1
    """

    try:
        with engine.connect() as conn:
            row = conn.execute(text(sql), {"job_id": int(job_id)}).mappings().first()
    except SQLAlchemyError:
        return None

    if row is None:
        return None

    created_at = row.get("created_at")
    finished_at = row.get("finished_at")
    metrics = row.get("metrics") or {}
    if isinstance(metrics, str):
        try:
            metrics = json.loads(metrics)
        except Exception:
            metrics = {"raw": metrics}

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
        return []

    clauses = []
    params: Dict[str, Any] = {
        "limit": max(int(limit), 0),
        "offset": max(int(offset), 0),
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
    except SQLAlchemyError:
        return []

    out: List[Dict[str, Any]] = []
    for row in rows:
        created_at = row.get("created_at")
        finished_at = row.get("finished_at")
        metrics = row.get("metrics") or {}
        if isinstance(metrics, str):
            try:
                metrics = json.loads(metrics)
            except Exception:
                metrics = {"raw": metrics}

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
