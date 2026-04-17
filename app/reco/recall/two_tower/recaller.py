from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import List

from app.common.settings import TwoTowerSettings
from app.reco.recall.base import Recaller
from app.reco.types import Candidate, RequestContext

from .features import fetch_user_excluded_items
from .indexing import ann_search
from .online import build_user_vector


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TwoTowerRecall(Recaller):
    """Two-Tower 召回（基于真实数据库在线用户向量 + 离线物品向量库）。"""

    cfg: TwoTowerSettings
    mysql_dsn: str | None = None

    @property
    def name(self) -> str:
        return "two_tower"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        query_vec = None
        excluded: set[int] = set()

        if ctx.user_id is not None:
            user_id = int(ctx.user_id)
            query_vec = build_user_vector(user_id, mysql_dsn=self.mysql_dsn)
            excluded = fetch_user_excluded_items(
                user_id,
                mysql_dsn=self.mysql_dsn,
                recent_limit=self.cfg.exclude_recent_n,
            )
        else:
            logger.warning(
                "用户ID为空，双塔无法构建向量，user_id=%s, movie_id=%s, n=%s",
                ctx.user_id,
                ctx.movie_id,
                ctx.n,
            )

        if query_vec is None:
            logger.warning(
                "用户向量不可用，无法进行 Two-Tower recall，user_id=%s, movie_id=%s, n=%s, recall_topk=%s",
                ctx.user_id,
                ctx.movie_id,
                ctx.n,
                self.cfg.recall_topk,
            )
            return []

        pairs = ann_search(query_vec, k=max(int(self.cfg.recall_topk), int(ctx.n)))
        out: List[Candidate] = []
        for item_id, score in pairs:
            if int(item_id) in excluded:
                continue
            out.append(Candidate(item_id=int(item_id), score=float(score), source=self.name))

        return out