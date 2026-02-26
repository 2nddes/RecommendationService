from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from typing import Any, Dict, List, Tuple

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

    dtrain = xgb.DMatrix(X, label=y, feature_names=builder.feature_names())

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
        },
    )


# ----------------------------
# Two-tower ANN index
# ----------------------------


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
