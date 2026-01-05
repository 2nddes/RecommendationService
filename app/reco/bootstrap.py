from __future__ import annotations

# 组件注册入口：未来扩展新召回通道/排序/重排时，在这里注册

from app.reco.registry import register_ranker, register_recaller, register_reranker
from app.reco.recall.stub_channels import (
    CollaborativeFilteringRecall,
    TagRecall,
    TwoTowerRecall,
)
from app.reco.recall.mysql_channels import (
    UserCollectionRecall,
    UserHighRatingSimilarRecall,
    UserInterestTagRecall,
    ItemSimilarByTagsRecall,
)
from app.reco.ranking.stub_rankers import (
    CollaborativeFilteringRanker,
    NeuralNetRanker,
    TagRanker,
)
from app.reco.reranking.random_shuffle import RandomShuffleReranker

_bootstrapped = False


def bootstrap_components() -> None:
    global _bootstrapped
    if _bootstrapped:
        return

    # Recall channels
    register_recaller("cf", lambda: CollaborativeFilteringRecall())
    register_recaller("tag", lambda: TagRecall())
    register_recaller("two_tower", lambda: TwoTowerRecall())

    # MySQL-backed recall channels
    register_recaller("user_collection", lambda: UserCollectionRecall())
    register_recaller("user_high_rating_similar", lambda: UserHighRatingSimilarRecall())
    register_recaller("user_interest_tag", lambda: UserInterestTagRecall())
    # for /recommend/item fallback when you want content-based similar
    register_recaller("item_similar_by_tags", lambda: ItemSimilarByTagsRecall())

    # Ranking methods
    register_ranker("cf", lambda: CollaborativeFilteringRanker())
    register_ranker("tag", lambda: TagRanker())
    register_ranker("nn", lambda: NeuralNetRanker())

    # Reranking methods
    # seed 在 factory 中可被覆盖（通过 Settings.reranking_seed）
    register_reranker("random_shuffle", lambda: RandomShuffleReranker())

    _bootstrapped = True
