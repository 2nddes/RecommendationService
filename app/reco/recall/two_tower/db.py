from __future__ import annotations

from typing import Sequence

from sqlalchemy import Engine, bindparam, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

_engine_by_dsn: dict[str, Engine] = {}


def get_engine(mysql_dsn: str | None) -> Engine | None:
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


def execute(mysql_dsn: str | None, sql: str, params: dict, *, expanding: Sequence[str] = ()) -> list[dict]:
    engine = get_engine(mysql_dsn)
    if engine is None:
        return []

    with engine.connect() as conn:
        stmt = text(sql)
        for key in expanding:
            stmt = stmt.bindparams(bindparam(key, expanding=True))
        rs = conn.execute(stmt, params)
        return [dict(row._mapping) for row in rs]
