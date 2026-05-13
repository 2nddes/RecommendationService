from __future__ import annotations

from typing import Sequence

from sqlalchemy import Engine, bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from app.common.mysql_engine import get_shared_mysql_engine



def get_engine(mysql_dsn: str | None) -> Engine | None:
    return get_shared_mysql_engine(mysql_dsn)


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
