from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from app.reco.types import Candidate, RequestContext


class Recaller(ABC):
    """召回阶段：从一个通道召回候选集。"""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def recall(self, ctx: RequestContext) -> List[Candidate]:
        raise NotImplementedError
