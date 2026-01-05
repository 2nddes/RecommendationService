from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    internal_secret: str | None = None
    recall_channels: list[str] = None  # type: ignore[assignment]
    ranking_method: str = "cf"
    reranking_method: str = "random_shuffle"
    reranking_seed: int | None = None
    mysql_dsn: str | None = None

    @staticmethod
    def from_env() -> "Settings":
        recall_raw = os.getenv("RECALL_CHANNELS")
        recall_channels = (
            [x.strip() for x in recall_raw.split(",") if x.strip()]
            if recall_raw
            else ["user_collection", "user_high_rating_similar", "user_interest_tag"]
        )

        reranking_seed_raw = os.getenv("RERANKING_SEED")
        reranking_seed = int(reranking_seed_raw) if reranking_seed_raw else None

        return Settings(
            internal_secret=os.getenv("INTERNAL_SECRET") or None,
            recall_channels=recall_channels,
            ranking_method=os.getenv("RANKING_METHOD") or "cf",
            reranking_method=os.getenv("RERANKING_METHOD") or "random_shuffle",
            reranking_seed=reranking_seed,
            mysql_dsn=os.getenv("MYSQL_DSN") or None,
        )
