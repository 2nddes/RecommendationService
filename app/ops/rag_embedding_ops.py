from __future__ import annotations

import logging
from typing import Any, Dict

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.reco.training.common import get_mysql_engine


logger = logging.getLogger(__name__)


def enqueue_rag_embedding_job(*, mysql_dsn: str | None, movie_id: int) -> int:
    engine = get_mysql_engine(mysql_dsn, logger=logger, event_prefix="rag.embedding.mysql_engine")

    sql = text(
        """
        INSERT INTO rag_embedding_job(movie_id, status, retry_count, error)
        VALUES (:movie_id, 'pending', 0, NULL)
        """
    )
    with engine.begin() as conn:
        rs = conn.execute(sql, {"movie_id": int(movie_id)})
        new_id = rs.lastrowid
    if new_id is None:
        raise RuntimeError("enqueue_rag_embedding_job_failed: empty_insert_id")
    return int(new_id)


def claim_next_rag_embedding_job(*, mysql_dsn: str | None, max_retry: int) -> Dict[str, Any] | None:
    engine = get_mysql_engine(mysql_dsn, logger=logger, event_prefix="rag.embedding.mysql_engine")
    select_sql = text(
        """
        SELECT id, movie_id, status, retry_count, error, created_at, updated_at
        FROM rag_embedding_job
        WHERE status = 'pending'
          AND retry_count < :max_retry
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        FOR UPDATE
        """
    )
    update_sql = text(
        """
        UPDATE rag_embedding_job
        SET status = 'processing', updated_at = CURRENT_TIMESTAMP
        WHERE id = :job_id
        """
    )
    try:
        with engine.begin() as conn:
            row = conn.execute(select_sql, {"max_retry": int(max_retry)}).mappings().first()
            if row is None:
                return None
            job_id = int(row["id"])
            conn.execute(update_sql, {"job_id": job_id})
    except SQLAlchemyError as exc:
        raise RuntimeError(f"claim_rag_embedding_job_failed: {type(exc).__name__}: {exc}") from exc

    return get_rag_embedding_job(mysql_dsn=mysql_dsn, job_id=job_id)


def complete_rag_embedding_job(*, mysql_dsn: str | None, job_id: int) -> None:
    engine = get_mysql_engine(mysql_dsn, logger=logger, event_prefix="rag.embedding.mysql_engine")
    sql = text(
        """
        UPDATE rag_embedding_job
        SET status = 'completed', error = NULL, updated_at = CURRENT_TIMESTAMP
        WHERE id = :job_id
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, {"job_id": int(job_id)})


def fail_rag_embedding_job(*, mysql_dsn: str | None, job_id: int, error: str, max_retry: int) -> None:
    engine = get_mysql_engine(mysql_dsn, logger=logger, event_prefix="rag.embedding.mysql_engine")
    sql = text(
        """
        UPDATE rag_embedding_job
        SET retry_count = retry_count + 1,
            status = CASE WHEN retry_count + 1 >= :max_retry THEN 'failed' ELSE 'pending' END,
            error = :error,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = :job_id
        """
    )
    with engine.begin() as conn:
        conn.execute(sql, {"job_id": int(job_id), "error": str(error)[:1000], "max_retry": int(max_retry)})


def get_rag_embedding_job(*, mysql_dsn: str | None, job_id: int) -> Dict[str, Any] | None:
    engine = get_mysql_engine(mysql_dsn, logger=logger, event_prefix="rag.embedding.mysql_engine")
    sql = text(
        """
        SELECT id, movie_id, status, retry_count, error, created_at, updated_at
        FROM rag_embedding_job
        WHERE id = :job_id
        LIMIT 1
        """
    )
    with engine.connect() as conn:
        row = conn.execute(sql, {"job_id": int(job_id)}).mappings().first()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "movie_id": int(row["movie_id"]),
        "status": str(row["status"]),
        "retry_count": int(row["retry_count"] or 0),
        "error": row.get("error"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") is not None else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") is not None else None,
    }
