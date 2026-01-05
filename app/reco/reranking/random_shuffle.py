from __future__ import annotations

import random
from typing import List

from app.reco.reranking.base import Reranker
from app.reco.types import RankedItem, RequestContext


class RandomShuffleReranker(Reranker):
    """随机打乱重排。

    说明：当前满足“随机打乱顺序”的需求；未来可替换为多样性、去重、曝光控制等策略。
    """

    def __init__(self, seed: int | None = None):
        self._seed = seed

    @property
    def name(self) -> str:
        return "random_shuffle"

    def rerank(self, ctx: RequestContext, ranked: List[RankedItem]) -> List[RankedItem]:
        rng = random.Random(self._seed)
        items = list(ranked)
        rng.shuffle(items)
        return items
