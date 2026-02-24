from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import os
import shutil
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


def train_current_models(settings: Settings, *, mode: str) -> Dict[str, Any]:
    """Train (or rebuild) artifacts for models that are enabled by config.

    This does NOT change which model is selected; config remains the source of truth.
    It only produces artifacts and records their paths.
    """

    outcomes: List[TrainOutcome] = []

    # TODO: add more models here
    # Ranking
    if settings.ranking_method == "xgb":
        outcomes.append(_train_xgb(settings, mode=mode))

    # Recall
    if "two_tower" in (settings.recall_channels or []):
        outcomes.append(_train_two_tower_index(settings, mode=mode))

    return {
        "mode": mode,
        "trained": [o.to_dict() for o in outcomes],
    }


def apply_current_models(settings: Settings) -> Dict[str, Any]:
    """Apply latest trained artifacts to active paths defined by config/env."""

    store = get_artifact_store()
    applied: List[Dict[str, Any]] = []

    # Ranking
    if settings.ranking_method == "xgb":
        applied.append(_apply_xgb(settings, store))

    # Recall
    if "two_tower" in (settings.recall_channels or []):
        applied.append(_apply_two_tower(settings, store))

    return {"applied": applied}


def refresh_current_models(settings: Settings) -> Dict[str, Any]:
    """Incremental refresh hook.

    For now, this maps to nearline rebuild for components that support it.
    """

    return train_current_models(settings, mode="incremental")


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


def _train_xgb(settings: Settings, *, mode: str) -> TrainOutcome:
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
    ctx = RequestContext(user_id=int(rows[0][0]), n=10, strategy="admin_train")
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
        details={"rows": len(rows), "mode": mode, "feature_count": int(X.shape[1])},
    )


def _apply_xgb(settings: Settings, store) -> Dict[str, Any]:
    active_path = settings.xgb_model_path
    artifact_path = store.get("ranking.xgb.latest_artifact_path")

    if not active_path:
        return {
            "component": "ranking",
            "name": "xgb",
            "applied": False,
            "reason": "XGB_MODEL_PATH is not set in config",
        }

    if not artifact_path or not os.path.exists(str(artifact_path)):
        return {
            "component": "ranking",
            "name": "xgb",
            "applied": False,
            "reason": "no_latest_artifact_to_apply",
        }

    os.makedirs(os.path.dirname(active_path) or ".", exist_ok=True)
    tmp = active_path + ".tmp"
    shutil.copyfile(str(artifact_path), tmp)
    os.replace(tmp, active_path)

    return {
        "component": "ranking",
        "name": "xgb",
        "applied": True,
        "active_path": active_path,
        "artifact_path": artifact_path,
    }


# ----------------------------
# Two-tower ANN index
# ----------------------------


def _two_tower_active_index_path(settings: Settings) -> str:
    return settings.two_tower_index_path or os.path.join("data", "two_tower_items.hnsw")


def _train_two_tower_index(settings: Settings, *, mode: str) -> TrainOutcome:
    store = get_artifact_store()

    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join("data", "artifacts", "two_tower")
    os.makedirs(out_dir, exist_ok=True)
    artifact_path = os.path.join(out_dir, f"two_tower_{ts}.hnsw")

    try:
        from app.reco.recall.two_tower import build_hnsw_index, load_config_from_settings
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
        count = build_hnsw_index(index_path=artifact_path, cfg=cfg, mysql_dsn=settings.mysql_dsn)
    except Exception as e:  # noqa: BLE001
        return TrainOutcome(
            component="recall",
            name="two_tower",
            artifact_path=None,
            trained=False,
            details={"failed": True, "reason": f"{type(e).__name__}: {e}"},
        )

    store.set("recall.two_tower.latest_artifact_path", artifact_path)
    store.set("recall.two_tower.latest_trained_at", ts)

    return TrainOutcome(
        component="recall",
        name="two_tower",
        artifact_path=artifact_path,
        trained=True,
        details={"items_indexed": int(count), "mode": mode},
    )


def _apply_two_tower(settings: Settings, store) -> Dict[str, Any]:
    active_path = _two_tower_active_index_path(settings)
    artifact_path = store.get("recall.two_tower.latest_artifact_path")

    if not artifact_path or not os.path.exists(str(artifact_path)):
        return {
            "component": "recall",
            "name": "two_tower",
            "applied": False,
            "reason": "no_latest_artifact_to_apply",
        }

    os.makedirs(os.path.dirname(active_path) or ".", exist_ok=True)
    tmp = active_path + ".tmp"
    shutil.copyfile(str(artifact_path), tmp)
    os.replace(tmp, active_path)

    # Invalidate in-memory cache so it picks up the new file ASAP.
    try:
        from app.reco.recall.two_tower import invalidate_index_cache

        invalidate_index_cache()
    except Exception:
        pass

    return {
        "component": "recall",
        "name": "two_tower",
        "applied": True,
        "active_path": active_path,
        "artifact_path": artifact_path,
    }
