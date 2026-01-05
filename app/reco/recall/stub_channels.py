from __future__ import annotations

from typing import List

from app.reco.recall.base import Recaller
from app.reco.types import Candidate, RequestContext


class CollaborativeFilteringRecall(Recaller):
    @property
    def name(self) -> str:
        return "cf"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        # TODO: 这里接入 User-Based/Item-Based CF 或矩阵分解等
        return []


class TagRecall(Recaller):
    @property
    def name(self) -> str:
        return "tag"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        # TODO: 这里接入 标签/类型/主题 等内容召回
        return []


class TwoTowerRecall(Recaller):
    @property
    def name(self) -> str:
        return "two_tower"

    def recall(self, ctx: RequestContext) -> List[Candidate]:
        # TODO: 这里接入 双塔/向量检索（ANN）召回
        return []
