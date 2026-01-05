from __future__ import annotations

from typing import List

from app.reco.ranking.base import Ranker
from app.reco.types import Candidate, RankedItem, RequestContext


class CollaborativeFilteringRanker(Ranker):
    @property
    def name(self) -> str:
        return "cf"

    def rank(self, ctx: RequestContext, candidates: List[Candidate]) -> List[RankedItem]:
        # TODO: 这里接入 CF 打分/相似度或预测评分
        ranked = [RankedItem(item_id=c.item_id, score=c.score, reason="cf") for c in candidates]
        return sorted(ranked, key=lambda x: x.score, reverse=True)


class TagRanker(Ranker):
    @property
    def name(self) -> str:
        return "tag"

    def rank(self, ctx: RequestContext, candidates: List[Candidate]) -> List[RankedItem]:
        # TODO: 这里接入 内容/标签匹配打分
        ranked = [RankedItem(item_id=c.item_id, score=c.score, reason="tag") for c in candidates]
        return sorted(ranked, key=lambda x: x.score, reverse=True)


class NeuralNetRanker(Ranker):
    @property
    def name(self) -> str:
        return "nn"

    def rank(self, ctx: RequestContext, candidates: List[Candidate]) -> List[RankedItem]:
        # TODO: 这里接入 DNN/DeepFM/Transformer 等排序模型
        ranked = [RankedItem(item_id=c.item_id, score=c.score, reason="nn") for c in candidates]
        return sorted(ranked, key=lambda x: x.score, reverse=True)
