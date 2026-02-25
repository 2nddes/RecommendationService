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
                metrics={"error": f"{type(e).__name__}: {e}"},
                set_finished_at=True,
            )
        raise


def refresh_current_models(settings: Settings) -> Dict[str, Any]:
    # 重新加载权重
    # TODO
    return {"refreshed": True}



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


def _fetch_xgb_training_rows(*, mysql_dsn: str | None, limit: int = 5000) -> List[Tuple[int, int, str, int | None]]:
    """Return tuples: (user_id, movie_id, action_type, rating)."""

    engine = _get_mysql_engine(mysql_dsn)
    if engine is None:
        return []

    sql = """
    SELECT ua.user_id AS user_id,
           ua.movie_id AS movie_id,
           ua.action_type AS action_type,
           ua.rating AS rating
    FROM user_action ua
    WHERE ua.movie_id IS NOT NULL
    ORDER BY ua.id DESC
    LIMIT :limit
    """

    try:
        with engine.connect() as conn:
            rs = conn.execute(text(sql), {"limit": int(limit)})
            out: List[Tuple[int, int, str, int | None]] = []
            for row in rs:
                d = dict(row._mapping)
                try:
                    out.append(
                        (
                            int(d.get("user_id")),
                            int(d.get("movie_id")),
                            str(d.get("action_type")),
                            int(d["rating"]) if d.get("rating") is not None else None,
                        )
                    )
                except Exception:
                    continue
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

    for i, (user_id, movie_id, action_type, rating) in enumerate(rows):
        src = sources[i % len(sources)]
        # treat action intensity as recall score proxy
        base = 1.0 if action_type in {"like", "collect", "rate", "comment", "share"} else 0.3
        candidates.append(Candidate(item_id=int(movie_id), score=float(base), source=src))

        y = 1.0 if action_type in {"like", "collect"} else 0.0
        if action_type == "rate" and rating is not None:
            y = 1.0 if int(rating) >= 8 else 0.0
        labels.append(float(y))

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

    num_boost_round = int(settings.xgb_train_rounds)
    booster = xgb.train(params, dtrain, num_boost_round=num_boost_round)

    booster.save_model(artifact_path)

    store.set("ranking.xgb.latest_artifact_path", artifact_path)
    store.set("ranking.xgb.latest_trained_at", ts)

    return TrainOutcome(
        component="ranking",
        name="xgb",
        artifact_path=artifact_path,
        trained=True,
        details={"rows": len(rows), "feature_count": int(X.shape[1])},
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
