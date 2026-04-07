from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence

from sqlalchemy import Engine, bindparam, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.reco.types import Candidate, RequestContext


FeatureRow = Dict[str, float]
FeatureFn = Callable[[RequestContext, Candidate, Mapping[str, Any] | None], float]


def _log1p(x: float) -> float:
    if x <= 0:
        return 0.0
    return float(math.log1p(x))


_engine_by_dsn: dict[str, Engine] = {}


def _get_engine(mysql_dsn: str | None) -> Engine | None:
    if not mysql_dsn:
        return None
    dsn = str(mysql_dsn).strip()
    if not dsn:
        return None
    cached = _engine_by_dsn.get(dsn)
    if cached is not None:
        return cached
    _engine_by_dsn[dsn] = create_engine(dsn, pool_pre_ping=True)
    return _engine_by_dsn[dsn]


def fetch_movie_features(movie_ids: Sequence[int], *, mysql_dsn: str | None = None) -> Dict[int, Dict[str, Any]]:
    """Fetch per-movie features from MySQL in batch.

    Returns a dict keyed by movie.id. If MySQL is not configured or query fails, returns {}.
    """

    movie_ids = [int(x) for x in movie_ids if int(x) > 0]
    if not movie_ids:
        return {}

    engine = _get_engine(mysql_dsn)
    if engine is None:
        return {}

    sql = """
        SELECT m.movie_id AS id,
            CASE WHEN m.rating_count > 0 THEN (m.rating_sum * 1.0 / m.rating_count) ELSE 0 END AS rating_avg,
           m.rating_count,
           m.year,
           m.duration_min
    FROM movie m
        WHERE m.movie_id IN :ids
    """

    with engine.connect() as conn:
        stmt = text(sql).bindparams(bindparam("ids", expanding=True))
        rs = conn.execute(stmt, {"ids": list(movie_ids)})
        out: Dict[int, Dict[str, Any]] = {}
        for row in rs:
            d = dict(row._mapping)
            mid = int(d["id"])
            if mid > 0:
                out[mid] = d
        return out


@dataclass(frozen=True)
class ManualFeatureConfig:
    """Configuration for manual feature engineering.

    Keep this small and easy to edit; future algorithm swaps can keep the same feature builder.
    """

    include_mysql_movie_features: bool = True


@dataclass(frozen=True)
class ManualFeatureBuilder:
    config: ManualFeatureConfig

    def feature_names(self) -> List[str]:
        return [name for name, _fn in self._feature_fns()]

    def build_rows(
        self,
        ctx: RequestContext,
        candidates: Sequence[Candidate],
        movie_features_by_id: Mapping[int, Mapping[str, Any]] | None,
    ) -> List[FeatureRow]:
        fns = self._feature_fns()
        rows: List[FeatureRow] = []

        for c in candidates:
            movie_f = movie_features_by_id.get(c.item_id) if movie_features_by_id else None
            row: FeatureRow = {}
            for name, fn in fns:
                row[name] = float(fn(ctx, c, movie_f))
            rows.append(row)

        return rows

    def to_matrix(self, rows: Sequence[FeatureRow]) -> List[List[float]]:
        names = self.feature_names()
        return [[float(r.get(n, 0.0)) for n in names] for r in rows]

    def _feature_fns(self) -> List[tuple[str, FeatureFn]]:
        # Core, always-available features
        fns: List[tuple[str, FeatureFn]] = [
            ("recall_score", lambda _ctx, c, _m: float(c.score)),
            ("has_user", lambda ctx, _c, _m: 1.0 if ctx.user_id is not None else 0.0),
            ("has_seed_movie", lambda ctx, _c, _m: 1.0 if ctx.movie_id is not None else 0.0),
            ("req_n", lambda ctx, _c, _m: float(max(ctx.n, 0))),
            ("src_user_collection", lambda _ctx, c, _m: 1.0 if c.source == "user_collection" else 0.0),
            (
                "src_user_high_rating_similar",
                lambda _ctx, c, _m: 1.0 if c.source == "user_high_rating_similar" else 0.0,
            ),
            ("src_user_interest_tag", lambda _ctx, c, _m: 1.0 if c.source == "user_interest_tag" else 0.0),
            ("src_item_similar_by_tags", lambda _ctx, c, _m: 1.0 if c.source == "item_similar_by_tags" else 0.0),
        ]

        if self.config.include_mysql_movie_features:
            # These features are strong but optional; if MySQL is missing they become 0.
            fns.extend(
                [
                    ("movie_rating_avg", lambda _ctx, _c, m: float(m["rating_avg"] if m else 0.0)),
                    (
                        "movie_log_rating_cnt",
                        lambda _ctx, _c, m: _log1p(float(m["rating_count"] if m else 0.0)),
                    ),
                    ("movie_year", lambda _ctx, _c, m: float(m["year"] if m else 0)),
                    (
                        "movie_duration_min",
                        lambda _ctx, _c, m: float(m["duration_min"] if m else 0),
                    ),
                ]
            )

        return fns
