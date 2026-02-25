from __future__ import annotations

from dataclasses import dataclass
from typing import List

from app.reco.recall.base import Recaller
from app.reco.recall.two_tower import (
    TwoTowerConfig,
    ann_search,
    build_item_vector,
    build_user_vector,
    fetch_user_excluded_items,
)
from app.reco.types import Candidate, RequestContext


@dataclass(frozen=True)
class TwoTowerRecall(Recaller):
    """Two-Tower 召回（基于真实数据库在线用户向量 + 离线物品向量库）。"""

    cfg: TwoTowerConfig
    mysql_dsn: str | None = None

    @property
    def name(self) -> str:
        return "two_tower"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        query_vec = None
        excluded: set[int] = set()

        if ctx.user_id is not None:
            query_vec = build_user_vector(int(ctx.user_id), self.cfg, None, mysql_dsn=self.mysql_dsn)
            excluded = fetch_user_excluded_items(int(ctx.user_id), mysql_dsn=self.mysql_dsn)
        else:
            print("用户ID为空，双塔无法构建向量")
        # if query_vec is None and ctx.movie_id is not None:
        #     query_vec = build_item_vector(int(ctx.movie_id), self.cfg, None, mysql_dsn=self.mysql_dsn)
        #     excluded = {int(ctx.movie_id)}

        if query_vec is None:
            print("用户向量不可用，无法进行 Two-Tower recall")
            return []

        pairs = ann_search(query_vec, k=self.cfg.recall_topk, cfg=self.cfg)
        out: List[Candidate] = []
        for item_id, score in pairs:
            if int(item_id) in excluded:
                continue
            out.append(Candidate(item_id=int(item_id), score=float(score), source=self.name))

        return out
