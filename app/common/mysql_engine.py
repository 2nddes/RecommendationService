from __future__ import annotations

import threading

from sqlalchemy import Engine, create_engine


_engine_cache_lock = threading.RLock()
_engine_by_dsn: dict[str, Engine] = {}


def get_shared_mysql_engine(mysql_dsn: str | None) -> Engine | None:
    if not mysql_dsn:
        return None
    dsn = str(mysql_dsn).strip()
    if not dsn:
        return None

    with _engine_cache_lock:
        cached = _engine_by_dsn.get(dsn)
        if cached is not None:
            return cached

        engine = create_engine(dsn, pool_pre_ping=True)
        _engine_by_dsn[dsn] = engine
        return engine