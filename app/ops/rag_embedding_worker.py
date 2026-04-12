from __future__ import annotations

import argparse
import logging
import time

from app.common.logging_setup import configure_logging
from app.common.settings import Settings
from app.ops.rag_embedding_ops import (
    claim_next_rag_embedding_job,
    complete_rag_embedding_job,
    fail_rag_embedding_job,
)
from app.reco.rag_service import get_movie_rag_service


logger = logging.getLogger(__name__)


def run_once() -> bool:
    settings = Settings.from_config()
    max_retry = int(settings.rag.embedding_job_max_retry)
    job = claim_next_rag_embedding_job(mysql_dsn=settings.core.mysql_dsn, max_retry=max_retry)
    if job is None:
        return False

    job_id = int(job["id"])
    movie_id = int(job["movie_id"])
    logger.info("RAG embedding worker claimed job, job_id=%s, movie_id=%s", job_id, movie_id)
    try:
        rag_service = get_movie_rag_service(settings)
        emb_id = rag_service.upsert_one(movie_id=int(movie_id))
        complete_rag_embedding_job(mysql_dsn=settings.core.mysql_dsn, job_id=job_id)
        logger.info("RAG embedding worker completed job, job_id=%s, movie_id=%s, emb_id=%s", job_id, movie_id, emb_id)
    except Exception as exc:
        logger.exception("RAG embedding worker failed, job_id=%s, movie_id=%s", job_id, movie_id)
        fail_rag_embedding_job(
            mysql_dsn=settings.core.mysql_dsn,
            job_id=job_id,
            error=f"{type(exc).__name__}: {exc}",
            max_retry=max_retry,
        )
    return True


def run_loop(*, interval_seconds: float = 3.0) -> None:
    logger.info("RAG embedding worker started, polling_interval_seconds=%s", interval_seconds)
    while True:
        handled = run_once()
        if handled:
            continue
        time.sleep(float(interval_seconds))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Independent worker for rag_embedding_job queue")
    parser.add_argument("--once", action="store_true", help="Process at most one pending job and exit")
    parser.add_argument("--interval", type=float, default=3.0, help="Polling interval in seconds")
    return parser.parse_args()


def main() -> None:
    log_file = configure_logging()
    logger.info("RAG embedding worker logging initialized, log_file=%s", str(log_file))
    args = _parse_args()
    if args.once:
        run_once()
        return
    run_loop(interval_seconds=float(args.interval))


if __name__ == "__main__":
    main()
