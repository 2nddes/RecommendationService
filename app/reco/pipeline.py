from __future__ import annotations

from dataclasses import dataclass
import logging
from time import perf_counter
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
        pipeline_start = perf_counter()
        logger.debug("推荐流水线开始，user_id=%s, movie_id=%s, n=%s", ctx.user_id, ctx.movie_id, ctx.n)
        recall_start = perf_counter()
        candidates = self._recall(ctx)
        recall_ms = (perf_counter() - recall_start) * 1000.0
        logger.debug("召回阶段完成，候选数=%s, elapsed_ms=%.2f", len(candidates), recall_ms)

        rank_start = perf_counter()
        ranked = self.ranker.rank(ctx, candidates)
        rank_ms = (perf_counter() - rank_start) * 1000.0
        logger.debug("排序阶段完成，结果数=%s, 排序器=%s, elapsed_ms=%.2f", len(ranked), self.ranker.name, rank_ms)

        rerank_start = perf_counter()
        reranked = self.reranker.rerank(ctx, ranked)
        final_items = [x.item_id for x in reranked[: max(ctx.n, 0)]]
        rerank_ms = (perf_counter() - rerank_start) * 1000.0
        total_ms = (perf_counter() - pipeline_start) * 1000.0
        logger.info(
            "推荐流水线完成，user_id=%s, movie_id=%s, recaller_count=%s, ranker=%s, reranker=%s, candidate_count=%s, ranked_count=%s, returned_count=%s, recall_ms=%.2f, rank_ms=%.2f, rerank_ms=%.2f, elapsed_ms=%.2f",
            ctx.user_id,
            ctx.movie_id,
            len(self.recallers),
            self.ranker.name,
            self.reranker.name,
            len(candidates),
            len(ranked),
            len(final_items),
            recall_ms,
            rank_ms,
            rerank_ms,
            total_ms,
        )
        return final_items

    def _recall(self, ctx: RequestContext) -> List[Candidate]:
        # 由movie_id映射到Candidate的字典，方便去重和合并
        merged: dict[int, Candidate] = {}

        for recaller in self.recallers:
            before_count = len(merged)
            recaller_start = perf_counter()
            try:
                recalled = recaller.recall(ctx)
            except Exception:
                logger.exception(
                    "召回器执行失败，recaller=%s, user_id=%s, movie_id=%s, n=%s",
                    recaller.name,
                    ctx.user_id,
                    ctx.movie_id,
                    ctx.n,
                )
                raise

            for c in recalled:
                if c.item_id not in merged:
                    merged[c.item_id] = c
                else:
                    # 合并策略：保留更高分的候选
                    prev = merged[c.item_id]
                    if c.score > prev.score:
                        merged[c.item_id] = c
            recaller_ms = (perf_counter() - recaller_start) * 1000.0
            logger.info(
                "召回器执行完成，recaller=%s, 返回候选=%s, 新增候选=%s, 累计候选=%s, elapsed_ms=%.2f",
                recaller.name,
                len(recalled),
                len(merged) - before_count,
                len(merged),
                recaller_ms,
            )

        return list(merged.values())


def take(items: Iterable[RankedItem], n: int) -> List[RankedItem]:
    return list(items)[: max(n, 0)]
