from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from app.reco.types import Candidate, RankedItem, RequestContext


class Ranker(ABC):
    """排序阶段：对候选集打分并排序。"""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def rank(self, ctx: RequestContext, candidates: List[Candidate]) -> List[RankedItem]:
        raise NotImplementedError
