from __future__ import annotations

from dataclasses import dataclass
import os
from typing import List, Sequence

from app.common.settings import Settings
from app.reco.ranking.base import Ranker
from app.reco.ranking.xgb_features import ManualFeatureBuilder, ManualFeatureConfig, fetch_movie_features
from app.reco.types import Candidate, RankedItem, RequestContext


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return default


def load_latest_local_model(settings: Settings) -> str | None:
    configured = str(settings.xgb_model_path or "").strip()
    active_path = configured or os.path.join("data", "models", "xgb_latest.json")

    if os.path.exists(active_path):
        return active_path

    artifact_dir = os.path.join("data", "artifacts", "xgb")
    if not os.path.isdir(artifact_dir):
        return None

    candidates = [
        os.path.join(artifact_dir, name)
        for name in os.listdir(artifact_dir)
        if name.endswith(".json")
    ]
    if not candidates:
        return None

    latest = max(candidates, key=lambda p: os.path.getmtime(p))
    os.makedirs(os.path.dirname(active_path) or ".", exist_ok=True)
    tmp = active_path + ".tmp"
    with open(latest, "rb") as src, open(tmp, "wb") as dst:
        dst.write(src.read())
    os.replace(tmp, active_path)
    return active_path


@dataclass(frozen=True)
class XGBoostRanker(Ranker):
    """XGBoost + manual feature engineering ranker.

    Design goals:
    - Easy to modify feature set: edit `xgb_features.py`.
    - Easy to swap algorithm later: keep Ranker interface stable and replace scorer.
    """

    model_path: str | None = None
    use_mysql_features: bool = True
    mysql_dsn: str | None = None

    @property
    def name(self) -> str:
        return "xgb"

    def rank(self, ctx: RequestContext, candidates: List[Candidate]) -> List[RankedItem]:
        if not candidates:
            return []

        # 1) Build manual features (optionally enriched from MySQL)
        movie_features_by_id = (
            fetch_movie_features([c.item_id for c in candidates], mysql_dsn=self.mysql_dsn)
            if self.use_mysql_features
            else {}
        )

        builder = ManualFeatureBuilder(
            config=ManualFeatureConfig(include_mysql_movie_features=self.use_mysql_features)
        )
        rows = builder.build_rows(ctx, candidates, movie_features_by_id)
        matrix = builder.to_matrix(rows)
        feature_names = builder.feature_names()

        # 2) Score with xgboost model.
        scores = self._predict_with_xgboost(matrix, feature_names)
        reason = "xgb"

        ranked = [RankedItem(item_id=c.item_id, score=float(s), reason=reason) for c, s in zip(candidates, scores)]
        return sorted(ranked, key=lambda x: x.score, reverse=True)

    def _predict_with_xgboost(self, matrix: Sequence[Sequence[float]], feature_names: Sequence[str]) -> List[float]:
        model_path = self.model_path
        if not model_path:
            raise RuntimeError("ranking_model_path_is_empty")

        try:
            import numpy as np
            import xgboost as xgb
        except Exception as e:
            raise RuntimeError(f"xgboost_dependency_not_available: {e}") from e

        try:
            booster = xgb.Booster()
            booster.load_model(model_path)

            X = np.asarray(matrix, dtype=float)
            dmat = xgb.DMatrix(X, feature_names=list(feature_names))
            pred = booster.predict(dmat)
            return [float(x) for x in pred.tolist()]
        except Exception as e:
            raise RuntimeError(f"xgboost_rank_inference_failed: {type(e).__name__}: {e}") from e
