from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Iterable, List

from app.reco.types import Candidate, RankedItem, RequestContext
from app.reco.recall.base import Recaller
from app.reco.ranking.base import Ranker
from app.reco.reranking.base import Reranker


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecommendationPipeline:
    recallers: List[Recaller]
    ranker: Ranker
    reranker: Reranker

    def recommend(self, ctx: RequestContext) -> List[int]:
        logger.info("推荐流水线开始，user_id=%s, movie_id=%s, n=%s", ctx.user_id, ctx.movie_id, ctx.n)
        candidates = self._recall(ctx)
        logger.info("召回阶段完成，候选数=%s", len(candidates))
        ranked = self.ranker.rank(ctx, candidates)
        logger.info("排序阶段完成，结果数=%s, 排序器=%s", len(ranked), self.ranker.name)
        reranked = self.reranker.rerank(ctx, ranked)
        final_items = [x.item_id for x in reranked[: max(ctx.n, 0)]]
        logger.info("重排阶段完成，返回条数=%s, 重排器=%s", len(final_items), self.reranker.name)
        return final_items

    def _recall(self, ctx: RequestContext) -> List[Candidate]:
        # 由movie_id映射到Candidate的字典，方便去重和合并
        merged: dict[int, Candidate] = {}

        for recaller in self.recallers:
            before_count = len(merged)
            for c in recaller.recall(ctx):
                if c.item_id not in merged:
                    merged[c.item_id] = c
                else:
                    # 合并策略：保留更高分的候选
                    prev = merged[c.item_id]
                    if c.score > prev.score:
                        merged[c.item_id] = c
            logger.info("召回器执行完成，recaller=%s, 新增候选=%s, 累计候选=%s", recaller.name, len(merged) - before_count, len(merged))

        return list(merged.values())


def take(items: Iterable[RankedItem], n: int) -> List[RankedItem]:
    return list(items)[: max(n, 0)]
