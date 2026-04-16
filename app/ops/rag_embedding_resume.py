from __future__ import annotations

import argparse
import json
import logging
from time import perf_counter

import numpy as np
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.common.logging_setup import configure_logging
from app.common.settings import Settings
from app.reco.rag_clients import create_embedding
from app.reco.rag_service import get_movie_rag_service


logger = logging.getLogger(__name__)
_LOG_EVERY = 100


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build embeddings only for movies missing rows in movie_embeddings"
    )
    parser.add_argument("--limit", type=int, default=0, help="Maximum missing movies to process; 0 means all")
    return parser.parse_args()


def _list_missing_movie_ids(*, rag_service, limit: int | None) -> list[int]:
    sql_text = """
        SELECT m.movie_id
        FROM movie m
        WHERE m.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM movie_embeddings me
              WHERE me.movie_id = m.movie_id
                AND COALESCE(OCTET_LENGTH(me.embedding_vector), 0) > 0
          )
        ORDER BY m.movie_id ASC
    """
    params: dict[str, int] = {}
    if limit is not None and int(limit) > 0:
        sql_text += "\nLIMIT :limit"
        params["limit"] = int(limit)

    with rag_service._ensure_engine().connect() as conn:
        rows = conn.execute(text(sql_text), params).mappings().all()
    return [int(row["movie_id"]) for row in rows if row.get("movie_id") is not None]


def _embedding_exists(*, rag_service, movie_id: int) -> bool:
    sql = text(
        """
        SELECT 1
        FROM movie_embeddings
        WHERE movie_id = :movie_id
          AND COALESCE(OCTET_LENGTH(embedding_vector), 0) > 0
        LIMIT 1
        """
    )
    with rag_service._ensure_engine().connect() as conn:
        row = conn.execute(sql, {"movie_id": int(movie_id)}).first()
    return row is not None


def _insert_embedding_row(*, rag_service, movie_id: int, chunk_text: str, vector: np.ndarray) -> int:
    sql = text(
        """
        INSERT INTO movie_embeddings(movie_id, chunk_text, embedding_vector)
        VALUES (:movie_id, :chunk_text, :embedding_vector)
        """
    )
    params = {
        "movie_id": int(movie_id),
        "chunk_text": chunk_text,
        "embedding_vector": np.asarray(vector, dtype=np.float32).reshape(-1).tobytes(),
    }
    with rag_service._ensure_engine().begin() as conn:
        rs = conn.execute(sql, params)
    if rs.lastrowid is None:
        raise RuntimeError("movie_embeddings_insert_failed: empty_insert_id")
    return int(rs.lastrowid)


def run_resume(*, limit: int | None = None) -> dict[str, int]:
    settings = Settings.from_config()
    rag_service = get_movie_rag_service(settings)
    started_at = perf_counter()
    embedding_cfg = rag_service._embedding_cfg()

    limit_value = None if limit is None else max(0, int(limit))

    movie_ids = _list_missing_movie_ids(rag_service=rag_service, limit=limit_value)
    total_movies = len(movie_ids)
    logger.info("RAG resume prepared, missing_movies=%s, limit=%s", total_movies, limit_value)

    summary = {
        "missing_movies": total_movies,
        "processed_movies": 0,
        "requested_movies": 0,
        "inserted_movies": 0,
        "skipped_existing": 0,
        "failed_movies": 0,
    }

    for index, movie_id in enumerate(movie_ids, start=1):
        summary["processed_movies"] = int(index)
        if _embedding_exists(rag_service=rag_service, movie_id=int(movie_id)):
            summary["skipped_existing"] = int(summary["skipped_existing"]) + 1
        else:
            try:
                movie = rag_service._fetch_movie(int(movie_id))
                if movie is None:
                    raise RuntimeError(f"movie_not_found: {movie_id}")

                chunk_text = rag_service._build_chunk_text(movie)
                vector = np.asarray(create_embedding(cfg=embedding_cfg, text=chunk_text), dtype=np.float32).reshape(-1)
                if vector.size <= 0:
                    raise RuntimeError("embedding_vector_empty")

                summary["requested_movies"] = int(summary["requested_movies"]) + 1
                _insert_embedding_row(
                    rag_service=rag_service,
                    movie_id=int(movie_id),
                    chunk_text=chunk_text,
                    vector=vector,
                )
                summary["inserted_movies"] = int(summary["inserted_movies"]) + 1
            except IntegrityError:
                summary["skipped_existing"] = int(summary["skipped_existing"]) + 1
                logger.info("RAG resume skipped existing row, movie_id=%s", int(movie_id))
            except Exception as exc:
                summary["failed_movies"] = int(summary["failed_movies"]) + 1
                logger.warning("RAG resume failed, movie_id=%s, error=%s: %s", int(movie_id), type(exc).__name__, exc)

        if int(index) % _LOG_EVERY == 0 or int(index) == total_movies:
            logger.info(
                "RAG resume progress, processed_movies=%s/%s, inserted_movies=%s, skipped_existing=%s, failed_movies=%s",
                summary["processed_movies"],
                total_movies,
                summary["inserted_movies"],
                summary["skipped_existing"],
                summary["failed_movies"],
            )

    summary["elapsed_ms"] = max(0, int(round((perf_counter() - started_at) * 1000.0)))
    return summary


def main() -> None:
    log_file = configure_logging()
    logger.info("RAG resume script logging initialized, log_file=%s", str(log_file))
    args = _parse_args()
    summary = run_resume(limit=args.limit)
    print(json.dumps(summary, ensure_ascii=False))
    if int(summary.get("failed_movies") or 0) > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()