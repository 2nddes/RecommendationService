from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from app.reco.types import RankedItem, RequestContext


class Reranker(ABC):
    """重排阶段：对排序后的结果进行业务规则/多样性/探索等处理。"""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def rerank(self, ctx: RequestContext, ranked: List[RankedItem]) -> List[RankedItem]:
        raise NotImplementedError
