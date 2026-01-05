from __future__ import annotations

# 组件注册入口：未来扩展新召回通道/排序/重排时，在这里注册

from app.reco.registry import register_ranker, register_recaller, register_reranker
from app.reco.recall.stub_channels import (
    CollaborativeFilteringRecall,
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
    register_recaller("cf", lambda _s: CollaborativeFilteringRecall())
    register_recaller("two_tower", lambda _s: TwoTowerRecall())

    # MySQL-backed recall channels
    register_recaller("user_collection", lambda _s: UserCollectionRecall())
    register_recaller("user_high_rating_similar", lambda _s: UserHighRatingSimilarRecall())
    register_recaller("user_interest_tag", lambda _s: UserInterestTagRecall())
    # for /recommend/item fallback when you want content-based similar
    register_recaller("item_similar_by_tags", lambda _s: ItemSimilarByTagsRecall())

    # Ranking methods
    register_ranker("cf", lambda _s: CollaborativeFilteringRanker())
    register_ranker("tag", lambda _s: TagRanker())
    register_ranker("nn", lambda _s: NeuralNetRanker())

    # Reranking methods
    register_reranker("random_shuffle", lambda s: RandomShuffleReranker(seed=s.reranking_seed))

    _bootstrapped = True
