from __future__ import annotations

# 组件注册入口：未来扩展新召回通道/排序/重排时，在这里注册

from app.reco.registry import register_ranker, register_recaller, register_reranker
from app.reco.recall.two_tower_recall import TwoTowerRecall
from app.reco.recall.two_tower import load_config_from_settings
from app.reco.recall.mysql_channels import (
    UserCollectionRecall,
    UserHighRatingSimilarRecall,
    UserInterestTagRecall,
    ItemSimilarByTagsRecall,
)
from app.reco.ranking.stub_rankers import (
    CollaborativeFilteringRanker,
)
from app.reco.ranking.xgb_ranker import XGBoostRanker
from app.reco.reranking.random_shuffle import RandomShuffleReranker

_bootstrapped = False


def bootstrap_components() -> None:
    global _bootstrapped
    if _bootstrapped:
        return

    # Recall channels
    register_recaller(
        "two_tower",
        lambda s: TwoTowerRecall(cfg=load_config_from_settings(s), mysql_dsn=s.mysql_dsn),
    )

    # # MySQL-backed recall channels
    # register_recaller(
    #     "user_collection",
    #     lambda s: UserCollectionRecall(
    #         mysql_dsn=s.mysql_dsn,
    #         topk=s.recall_topk_user_collection,
    #         per_seed_topk=s.recall_per_seed_topk_user_collection,
    #     ),
    # )
    # register_recaller(
    #     "user_high_rating_similar",
    #     lambda s: UserHighRatingSimilarRecall(
    #         mysql_dsn=s.mysql_dsn,
    #         topk=s.recall_topk_user_high_rating,
    #         rating_threshold=s.recall_rating_threshold,
    #     ),
    # )
    # register_recaller(
    #     "user_interest_tag",
    #     lambda s: UserInterestTagRecall(
    #         mysql_dsn=s.mysql_dsn,
    #         topk=s.recall_topk_user_interest_tag,
    #     ),
    # )
    # # for /recommend/item fallback when you want content-based similar
    # register_recaller(
    #     "item_similar_by_tags",
    #     lambda s: ItemSimilarByTagsRecall(
    #         mysql_dsn=s.mysql_dsn,
    #         topk=s.recall_topk_item_similar_tag,
    #     ),
    # )

    # Ranking methods
    register_ranker("cf", lambda s: CollaborativeFilteringRanker(mysql_dsn=s.mysql_dsn))
    register_ranker(
        "xgb",
        lambda s: XGBoostRanker(
            model_path=s.xgb_model_path,
            use_mysql_features=s.xgb_use_mysql_features,
            allow_fallback=s.xgb_allow_fallback,
            mysql_dsn=s.mysql_dsn,
        ),
    )

    # Reranking methods
    register_reranker("random_shuffle", lambda s: RandomShuffleReranker(seed=s.reranking_seed))

    _bootstrapped = True
