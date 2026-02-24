from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

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


@dataclass(frozen=True)
class XGBoostRanker(Ranker):
    """XGBoost + manual feature engineering ranker.

    Design goals:
    - Easy to modify feature set: edit `xgb_features.py`.
    - Easy to swap algorithm later: keep Ranker interface stable and replace scorer.
    """

    model_path: str | None = None
    use_mysql_features: bool = True
    allow_fallback: bool = True
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

        # 2) Score with XGBoost if model is present; otherwise fall back to a simple weighted score.
        scores = self._predict_with_xgboost(matrix, feature_names)
        if scores is None:
            scores = self._fallback_scores(rows)
            reason = "xgb_fallback"
        else:
            reason = "xgb"

        ranked = [RankedItem(item_id=c.item_id, score=float(s), reason=reason) for c, s in zip(candidates, scores)]
        return sorted(ranked, key=lambda x: x.score, reverse=True)

    def _predict_with_xgboost(self, matrix: Sequence[Sequence[float]], feature_names: Sequence[str]) -> List[float] | None:
        model_path = self.model_path
        if not model_path:
            return None

        # Allow graceful degradation in environments without xgboost installed.
        allow_no_xgb = bool(self.allow_fallback)
        try:
            import numpy as np
            import xgboost as xgb
        except Exception:
            if allow_no_xgb:
                return None
            raise

        try:
            booster = xgb.Booster()
            booster.load_model(model_path)

            X = np.asarray(matrix, dtype=float)
            dmat = xgb.DMatrix(X, feature_names=list(feature_names))
            pred = booster.predict(dmat)
            return [float(x) for x in pred.tolist()]
        except Exception:
            if allow_no_xgb:
                return None
            raise

    def _fallback_scores(self, rows: Sequence[dict[str, float]]) -> List[float]:
        """Deterministic fallback scoring.

        This keeps the service working even when model file isn't shipped yet.
        You can tweak weights quickly during early iterations.
        """

        # These weights intentionally map to names from `ManualFeatureBuilder`.
        w = {
            "recall_score": 1.0,
            "movie_rating_avg": 0.15,
            "movie_log_rating_cnt": 0.05,
            "src_user_interest_tag": 0.02,
            "src_user_high_rating_similar": 0.02,
        }

        out: List[float] = []
        for r in rows:
            s = 0.0
            for k, wk in w.items():
                s += float(r.get(k, 0.0)) * float(wk)
            out.append(s)
        return out
