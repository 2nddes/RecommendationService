from __future__ import annotations

import os

from app.common.settings import Settings
from app.reco.pipeline import RecommendationPipeline
from app.reco.ranking.mmoe import MMoERanker, load_mmoe_bundle
from app.reco.recall.tag_inverted import TagInvertedRecall
from app.reco.recall.two_tower import (
    TwoTowerRecall,
    initialize_two_tower_runtime,
)
from app.reco.reranking.random_shuffle import RandomShuffleReranker


def build_pipeline(settings: Settings) -> RecommendationPipeline:
    initialize_two_tower_runtime(settings.two_tower)
    mmoe_bundle, mmoe_model = load_mmoe_bundle(settings.mmoe.model_path)

    recallers = [
        TwoTowerRecall(
            cfg=settings.two_tower,
            mysql_dsn=settings.core.mysql_dsn,
        )
    ]
    if settings.tag_recall.enabled:
        recallers.append(
            TagInvertedRecall(
                cfg=settings.tag_recall,
                settings=settings,
                mysql_dsn=settings.core.mysql_dsn,
            )
        )

    return RecommendationPipeline(
        recallers=recallers,
        ranker=MMoERanker(
            bundle=mmoe_bundle,
            model=mmoe_model,
            model_version=os.path.basename(settings.mmoe.model_path),
            use_mysql_features=True,
            mysql_dsn=settings.core.mysql_dsn,
        ),
        reranker=RandomShuffleReranker(seed=settings.core.reranking_seed),
    )

