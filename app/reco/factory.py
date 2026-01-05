from __future__ import annotations

from typing import List

from app.common.settings import Settings
from app.reco.pipeline import RecommendationPipeline
from app.reco.registry import ranking_registry, recall_registry, reranking_registry
from app.reco.bootstrap import bootstrap_components
from app.reco.recall.base import Recaller
from app.reco.ranking.base import Ranker
from app.reco.reranking.base import Reranker


def build_pipeline(settings: Settings) -> RecommendationPipeline:
    # 确保所有组件都已注册
    bootstrap_components()

    recallers: List[Recaller] = [recall_registry.build(name, settings) for name in settings.recall_channels]
    ranker: Ranker = ranking_registry.build(settings.ranking_method, settings)
    reranker: Reranker = reranking_registry.build(settings.reranking_method, settings)

    return RecommendationPipeline(recallers=recallers, ranker=ranker, reranker=reranker)
