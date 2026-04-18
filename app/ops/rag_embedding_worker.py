from __future__ import annotations

import argparse
import logging
import time
from time import perf_counter
from typing import Any

from app.common.logging_setup import configure_logging
from app.common.settings import Settings
from app.ops.rag_embedding_ops import (
    RAG_REBUILD_SCOPE_FULL,
    claim_next_rag_rebuild_job,
    complete_rag_rebuild_job,
    fail_processing_rag_rebuild_jobs,
    fail_rag_rebuild_job,
    update_rag_rebuild_job_snapshot,
)
from app.reco.rag_service import get_movie_rag_service, initialize_movie_rag_service


logger = logging.getLogger(__name__)

_STALE_TASK_ERROR = "worker_restarted_before_completion"
_bootstrapped = False


def _bootstrap_worker(settings: Settings) -> None:
    global _bootstrapped
    if _bootstrapped:
        return
    failed = fail_processing_rag_rebuild_jobs(
        mysql_dsn=settings.core.mysql_dsn,
        error=_STALE_TASK_ERROR,
    )
    if failed > 0:
        logger.warning("RAG rebuild worker marked stale tasks as failed, count=%s", failed)
    _bootstrapped = True


def _new_progress(*, total_movies: int = 0, pruned_embeddings: int = 0) -> dict[str, int]:
    return {
        "total_movies": max(0, int(total_movies or 0)),
        "processed_movies": 0,
        "completed_jobs": 0,
        "failed_jobs": 0,
        "pruned_embeddings": max(0, int(pruned_embeddings or 0)),
    }


def _build_result(
    *,
    progress: dict[str, int],
    elapsed_ms: float,
    index_reload_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "scope": RAG_REBUILD_SCOPE_FULL,
        "total_movies": int(progress.get("total_movies") or 0),
        "processed_movies": int(progress.get("processed_movies") or 0),
        "completed_jobs": int(progress.get("completed_jobs") or 0),
        "failed_jobs": int(progress.get("failed_jobs") or 0),
        "pruned_embeddings": int(progress.get("pruned_embeddings") or 0),
        "elapsed_ms": max(0, int(round(float(elapsed_ms)))),
    }
    if index_reload_details:
        result["index_state"] = str(index_reload_details.get("state") or "unknown")
        result["source_rows"] = max(0, int(index_reload_details.get("source_rows") or 0))
        result["indexed_rows"] = max(0, int(index_reload_details.get("indexed_rows") or 0))
    return result


def _mark_job_processing(settings: Settings, *, job_id: int, progress: dict[str, int]) -> None:
    update_rag_rebuild_job_snapshot(
        mysql_dsn=settings.core.mysql_dsn,
        job_id=job_id,
        progress=progress,
        result={},
        status="processing",
        error=None,
        set_finished_at=False,
        clear_finished_at=True,
    )


def _get_or_initialize_rag_service(settings: Settings):
    try:
        return get_movie_rag_service(settings)
    except RuntimeError as exc:
        if str(exc) != "rag_service_not_initialized":
            raise
    return initialize_movie_rag_service(settings)


def _run_job(settings: Settings, job: dict[str, Any]) -> None:
    job_id = int(job["id"])
    started_at = perf_counter()
    progress = _new_progress()

    try:
        rag_service = _get_or_initialize_rag_service(settings)
        pruned_embeddings = int(rag_service.prune_stale_embeddings())
        movie_ids = rag_service.list_active_movie_ids()
        progress = _new_progress(total_movies=len(movie_ids), pruned_embeddings=pruned_embeddings)
        _mark_job_processing(settings, job_id=job_id, progress=progress)

        logger.info(
            "RAG full rebuild task started, task_id=%s, total_movies=%s, pruned_embeddings=%s",
            job_id,
            progress["total_movies"],
            progress["pruned_embeddings"],
        )

        has_index_changes = pruned_embeddings > 0
        for movie_id in movie_ids:
            try:
                rag_service.upsert_one(movie_id=int(movie_id), refresh_index=False)
                progress["completed_jobs"] += 1
                has_index_changes = True
            except Exception as exc:
                progress["failed_jobs"] += 1
                logger.warning(
                    "RAG rebuild embedding failed, task_id=%s, movie_id=%s, error=%s",
                    job_id,
                    movie_id,
                    exc,
                )
            finally:
                progress["processed_movies"] += 1

        index_reload_details: dict[str, Any] = {}
        if has_index_changes:
            index_reload_details = dict(rag_service.load_from_mysql() or {})
            logger.info(
                "RAG rebuild index reloaded, task_id=%s, state=%s, source_rows=%s, indexed_rows=%s",
                job_id,
                index_reload_details.get("state"),
                index_reload_details.get("source_rows"),
                index_reload_details.get("indexed_rows"),
            )

        elapsed_ms = (perf_counter() - started_at) * 1000.0
        result = _build_result(
            progress=progress,
            elapsed_ms=elapsed_ms,
            index_reload_details=index_reload_details,
        )

        if int(progress.get("failed_jobs") or 0) > 0:
            fail_rag_rebuild_job(
                mysql_dsn=settings.core.mysql_dsn,
                job_id=job_id,
                error="one_or_more_embeddings_failed",
                progress=progress,
                result=result,
            )
            logger.warning(
                "RAG full rebuild task finished with failures, task_id=%s, processed_movies=%s, completed_jobs=%s, failed_jobs=%s, elapsed_ms=%.2f",
                job_id,
                progress.get("processed_movies"),
                progress.get("completed_jobs"),
                progress.get("failed_jobs"),
                elapsed_ms,
            )
            return

        complete_rag_rebuild_job(
            mysql_dsn=settings.core.mysql_dsn,
            job_id=job_id,
            progress=progress,
            result=result,
        )
        logger.info(
            "RAG full rebuild task completed, task_id=%s, total_movies=%s, elapsed_ms=%.2f",
            job_id,
            progress.get("total_movies"),
            elapsed_ms,
        )
    except Exception as exc:
        elapsed_ms = (perf_counter() - started_at) * 1000.0
        result = _build_result(progress=progress, elapsed_ms=elapsed_ms)
        fail_rag_rebuild_job(
            mysql_dsn=settings.core.mysql_dsn,
            job_id=job_id,
            error=f"{type(exc).__name__}: {exc}",
            progress=progress,
            result=result,
        )
        logger.exception(
            "RAG full rebuild task crashed, task_id=%s, processed_movies=%s, completed_jobs=%s, failed_jobs=%s",
            job_id,
            progress.get("processed_movies"),
            progress.get("completed_jobs"),
            progress.get("failed_jobs"),
        )


def run_once() -> bool:
    settings = Settings.from_config()
    _bootstrap_worker(settings)

    job = claim_next_rag_rebuild_job(mysql_dsn=settings.core.mysql_dsn)
    if job is None:
        return False

    _run_job(settings, job)
    return True


def run_loop(*, interval_seconds: float = 3.0) -> None:
    logger.info("RAG rebuild worker started, polling_interval_seconds=%s", interval_seconds)
    while True:
        handled = run_once()
        if handled:
            continue
        time.sleep(float(interval_seconds))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Full-rebuild RAG worker for unified ops_task queue")
    parser.add_argument("--once", action="store_true", help="Process at most one pending rebuild job and exit")
    parser.add_argument("--interval", type=float, default=3.0, help="Polling interval in seconds")
    return parser.parse_args()


def main() -> None:
    log_file = configure_logging()
    logger.info("RAG rebuild worker logging initialized, log_file=%s", str(log_file))
    args = _parse_args()
    if args.once:
        run_once()
        return
    run_loop(interval_seconds=float(args.interval))


if __name__ == "__main__":
    main()