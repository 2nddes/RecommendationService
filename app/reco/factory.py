from __future__ import annotations

from app.common.settings import Settings
from app.reco.pipeline import RecommendationPipeline
from app.reco.recall.two_tower import load_latest_local_model as load_latest_two_tower_model
from app.reco.recall.two_tower_recall import TwoTowerRecall
from app.reco.ranking.mmoe import MMoERanker, load_latest_local_model as load_latest_mmoe_model
from app.reco.reranking.random_shuffle import RandomShuffleReranker


def build_pipeline(settings: Settings) -> RecommendationPipeline:
    # Ensure active two-tower model path is synced with latest artifact before serving.
    load_latest_two_tower_model(settings)
    return RecommendationPipeline(
        recallers=[
            TwoTowerRecall(
                cfg=settings.two_tower,
                mysql_dsn=settings.core.mysql_dsn,
            )
        ],
        ranker=MMoERanker(
            model_path=load_latest_mmoe_model(settings),
            use_mysql_features=True,
            mysql_dsn=settings.core.mysql_dsn,
        ),
        reranker=RandomShuffleReranker(seed=settings.core.reranking_seed),
    )

