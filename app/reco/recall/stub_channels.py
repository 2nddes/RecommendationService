from __future__ import annotations

from dataclasses import dataclass
from typing import List

from app.reco.recall.base import Recaller
from app.reco.types import Candidate, RequestContext
from app.reco.recall.two_tower import (
    DeterministicEmbedder,
    TwoTowerConfig,
    ann_search,
    build_item_vector,
    build_user_vector,
    fetch_user_excluded_items,
)


class CollaborativeFilteringRecall(Recaller):
    @property
    def name(self) -> str:
        return "cf"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        # TODO: 这里接入 User-Based/Item-Based CF 或矩阵分解等
        return []


@dataclass(frozen=True)
class TwoTowerRecall(Recaller):
    cfg: TwoTowerConfig
    mysql_dsn: str | None = None

    @property
    def name(self) -> str:
        return "two_tower"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        cfg = self.cfg
        tag_embedder = DeterministicEmbedder(dim=cfg.dim, seed=cfg.seed)

        # 1) 用户召回：实时算用户向量
        query_vec = None
        excluded: set[int] = set()

        if ctx.user_id is not None:
            query_vec = build_user_vector(int(ctx.user_id), cfg, tag_embedder, mysql_dsn=self.mysql_dsn)
            # 排除用户收藏、互动过的物品
            excluded = fetch_user_excluded_items(int(ctx.user_id), mysql_dsn=self.mysql_dsn)

        # 2) 相似物品召回：用 movie_id 当查询向量（同一套 item tower）
        if query_vec is None and ctx.movie_id is not None:
            query_vec = build_item_vector(int(ctx.movie_id), cfg, tag_embedder, mysql_dsn=self.mysql_dsn)
            excluded = {int(ctx.movie_id)}

        if query_vec is None:
            return []

        k = cfg.recall_topk

        pairs = ann_search(query_vec, k=k, cfg=cfg)
        out: List[Candidate] = []
        for item_id, score in pairs:
            if item_id in excluded:
                continue
            out.append(Candidate(item_id=item_id, score=score, source=self.name))

        return out
