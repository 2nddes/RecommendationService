from __future__ import annotations

from app.common.settings import Settings
from app.reco.pipeline import RecommendationPipeline
from app.reco.recall.two_tower import load_config_from_settings
from app.reco.recall.two_tower_recall import TwoTowerRecall
from app.reco.ranking.mmoe_ranker import MMoERanker, load_latest_local_model as load_latest_mmoe_model
from app.reco.reranking.random_shuffle import RandomShuffleReranker


def build_pipeline(settings: Settings) -> RecommendationPipeline:
    return RecommendationPipeline(
        recallers=[
            TwoTowerRecall(
                cfg=load_config_from_settings(settings),
                mysql_dsn=settings.mysql_dsn,
            )
        ],
        ranker=MMoERanker(
            model_path=load_latest_mmoe_model(settings),
            use_mysql_features=True,
            mysql_dsn=settings.mysql_dsn,
        ),
        reranker=RandomShuffleReranker(seed=settings.reranking_seed),
    )
