from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

from app.reco.types import Candidate, RankedItem, RequestContext
from app.reco.recall.base import Recaller
from app.reco.ranking.base import Ranker
from app.reco.reranking.base import Reranker


@dataclass(frozen=True)
class RecommendationPipeline:
    recallers: List[Recaller]
    ranker: Ranker
    reranker: Reranker

    def recommend(self, ctx: RequestContext) -> List[int]:
        candidates = self._recall(ctx)
        ranked = self.ranker.rank(ctx, candidates)
        reranked = self.reranker.rerank(ctx, ranked)
        return [x.item_id for x in reranked[: max(ctx.n, 0)]]

    def _recall(self, ctx: RequestContext) -> List[Candidate]:
        merged: dict[int, Candidate] = {}

        for recaller in self.recallers:
            for c in recaller.recall(ctx):
                if c.item_id not in merged:
                    merged[c.item_id] = c
                else:
                    # 合并策略：保留更高分的候选
                    prev = merged[c.item_id]
                    if c.score > prev.score:
                        merged[c.item_id] = c

        return list(merged.values())


def take(items: Iterable[RankedItem], n: int) -> List[RankedItem]:
    return list(items)[: max(n, 0)]
