from __future__ import annotations

import argparse
import logging
import time
from time import perf_counter
from typing import Any

from app.common.logging_setup import configure_logging
from app.common.settings import Settings
from app.ops.rag_embedding_ops import (
    DEFAULT_FAILURE_SAMPLE_LIMIT,
    RAG_REBUILD_SCOPE_FULL,
    RAG_REBUILD_SCOPE_SINGLE,
    claim_next_rag_rebuild_job,
    complete_rag_rebuild_job,
    fail_processing_rag_rebuild_jobs,
    fail_rag_rebuild_job,
    update_rag_rebuild_job_snapshot,
)
from app.reco.rag_service import get_movie_rag_service


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


def _resolve_scope(job: dict[str, Any]) -> str:
    payload = job.get("payload") or {}
    scope = str(payload.get("scope") or RAG_REBUILD_SCOPE_FULL).strip().lower()
    if scope not in {RAG_REBUILD_SCOPE_FULL, RAG_REBUILD_SCOPE_SINGLE}:
        raise RuntimeError(f"invalid_rag_rebuild_scope: {scope}")
    return scope


def _resolve_target_movie_ids(settings: Settings, job: dict[str, Any]) -> tuple[str, list[int], int]:
    scope = _resolve_scope(job)
    payload = job.get("payload") or {}
    rag_service = get_movie_rag_service(settings)

    if scope == RAG_REBUILD_SCOPE_SINGLE:
        movie_id = int(payload.get("movie_id") or 0)
        if movie_id <= 0:
            raise RuntimeError("invalid_rag_rebuild_payload: movie_id")
        return scope, [movie_id], 0

    pruned_embeddings = int(rag_service.prune_stale_embeddings())
    return scope, rag_service.list_active_movie_ids(), pruned_embeddings


def _build_result_snapshot(
    *,
    payload: dict[str, Any],
    progress: dict[str, Any],
    failure_samples: list[dict[str, Any]],
    elapsed_ms: float,
) -> dict[str, Any]:
    result = {
        "scope": str(progress.get("scope") or payload.get("scope") or RAG_REBUILD_SCOPE_FULL),
        "total_movies": int(progress.get("total_movies") or 0),
        "processed_movies": int(progress.get("processed_movies") or 0),
        "completed_jobs": int(progress.get("completed_jobs") or 0),
        "failed_jobs": int(progress.get("failed_jobs") or 0),
        "pruned_embeddings": int(progress.get("pruned_embeddings") or 0),
        "elapsed_ms": max(0, int(round(float(elapsed_ms)))),
        "failure_samples": list(failure_samples[:DEFAULT_FAILURE_SAMPLE_LIMIT]),
    }
    if progress.get("max_retry") is not None:
        result["max_retry"] = max(1, int(progress.get("max_retry") or 1))

    movie_id = payload.get("movie_id")
    if movie_id is not None:
        result["movie_id"] = int(movie_id)
    return result


def _flush_progress(
    *,
    settings: Settings,
    job_id: int,
    payload: dict[str, Any],
    progress: dict[str, Any],
    failure_samples: list[dict[str, Any]],
    started_at: float,
) -> None:
    progress["flush_count"] = int(progress.get("flush_count") or 0) + 1
    elapsed_ms = (perf_counter() - started_at) * 1000.0
    update_rag_rebuild_job_snapshot(
        mysql_dsn=settings.core.mysql_dsn,
        job_id=int(job_id),
        progress=progress,
        result=_build_result_snapshot(
            payload=payload,
            progress=progress,
            failure_samples=failure_samples,
            elapsed_ms=elapsed_ms,
        ),
        status="processing",
        error=None,
        set_finished_at=False,
        clear_finished_at=True,
    )
    logger.info(
        "RAG rebuild progress, task_id=%s, scope=%s, processed_movies=%s/%s, completed_jobs=%s, failed_jobs=%s, elapsed_ms=%.2f",
        job_id,
        progress.get("scope"),
        progress.get("processed_movies"),
        progress.get("total_movies"),
        progress.get("completed_jobs"),
        progress.get("failed_jobs"),
        elapsed_ms,
    )


def _log_first_failure(*, job_id: int, movie_id: int, attempt: int, error: str) -> None:
    if int(attempt) != 1:
        return
    logger.warning(
        "RAG rebuild first attempt failed, task_id=%s, movie_id=%s, error=%s",
        job_id,
        movie_id,
        error,
    )


def _run_job(settings: Settings, job: dict[str, Any]) -> None:
    job_id = int(job["id"])
    payload = dict(job.get("payload") or {})
    max_retry = max(1, int(settings.rag.embedding_job_max_retry))
    log_every_movies = max(1, int(settings.rag.rebuild_log_every_movies))
    started_at = perf_counter()
    failure_samples: list[dict[str, Any]] = []
    index_reload_details: dict[str, Any] = {}
    progress = {
        "scope": str(payload.get("scope") or RAG_REBUILD_SCOPE_FULL),
        "total_movies": 0,
        "processed_movies": 0,
        "completed_jobs": 0,
        "failed_jobs": 0,
        "pruned_embeddings": 0,
        "flush_count": 0,
        "max_retry": max_retry,
    }

    try:
        scope, movie_ids, pruned_embeddings = _resolve_target_movie_ids(settings, job)
        progress["scope"] = scope
        progress["total_movies"] = len(movie_ids)
        progress["pruned_embeddings"] = int(pruned_embeddings)

        update_rag_rebuild_job_snapshot(
            mysql_dsn=settings.core.mysql_dsn,
            job_id=job_id,
            progress=progress,
            result=_build_result_snapshot(
                payload=payload,
                progress=progress,
                failure_samples=failure_samples,
                elapsed_ms=0.0,
            ),
            status="processing",
            error=None,
            set_finished_at=False,
            clear_finished_at=True,
        )
        logger.info(
            "RAG rebuild task started, task_id=%s, scope=%s, total_movies=%s, movie_id=%s, pruned_embeddings=%s",
            job_id,
            scope,
            progress["total_movies"],
            payload.get("movie_id"),
            progress["pruned_embeddings"],
        )

        rag_service = get_movie_rag_service(settings)
        for movie_id in movie_ids:
            last_error: str | None = None
            for attempt in range(1, max_retry + 1):
                try:
                    rag_service.upsert_one(movie_id=int(movie_id), refresh_index=False)
                    progress["completed_jobs"] = int(progress.get("completed_jobs") or 0) + 1
                    break
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    _log_first_failure(
                        job_id=job_id,
                        movie_id=int(movie_id),
                        attempt=attempt,
                        error=last_error,
                    )
                    if attempt >= max_retry:
                        progress["failed_jobs"] = int(progress.get("failed_jobs") or 0) + 1
                        if len(failure_samples) < DEFAULT_FAILURE_SAMPLE_LIMIT:
                            failure_samples.append(
                                {
                                    "movie_id": int(movie_id),
                                    "error": last_error,
                                    "attempts": int(attempt),
                                }
                            )

            progress["processed_movies"] = int(progress.get("processed_movies") or 0) + 1
            if (
                int(progress["processed_movies"]) % log_every_movies == 0
                and int(progress["processed_movies"]) < int(progress["total_movies"])
            ):
                _flush_progress(
                    settings=settings,
                    job_id=job_id,
                    payload=payload,
                    progress=progress,
                    failure_samples=failure_samples,
                    started_at=started_at,
                )

        if int(progress.get("completed_jobs") or 0) > 0 or int(progress.get("pruned_embeddings") or 0) > 0:
            index_reload_details = dict(rag_service.load_from_mysql() or {})
            logger.info(
                "RAG rebuild index reloaded, task_id=%s, state=%s, source_rows=%s, indexed_rows=%s",
                job_id,
                index_reload_details.get("state"),
                index_reload_details.get("source_rows"),
                index_reload_details.get("indexed_rows"),
            )

        elapsed_ms = (perf_counter() - started_at) * 1000.0
        result = _build_result_snapshot(
            payload=payload,
            progress=progress,
            failure_samples=failure_samples,
            elapsed_ms=elapsed_ms,
        )
        if index_reload_details:
            result["index_state"] = str(index_reload_details.get("state") or "unknown")
            result["source_rows"] = max(0, int(index_reload_details.get("source_rows") or 0))
            result["indexed_rows"] = max(0, int(index_reload_details.get("indexed_rows") or 0))

        if int(progress.get("failed_jobs") or 0) > 0:
            fail_rag_rebuild_job(
                mysql_dsn=settings.core.mysql_dsn,
                job_id=job_id,
                error="one_or_more_embeddings_failed",
                progress=progress,
                result=result,
            )
            logger.warning(
                "RAG rebuild task finished with failures, task_id=%s, scope=%s, completed_jobs=%s, failed_jobs=%s, elapsed_ms=%.2f",
                job_id,
                progress.get("scope"),
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
            "RAG rebuild task completed, task_id=%s, scope=%s, total_movies=%s, elapsed_ms=%.2f",
            job_id,
            progress.get("scope"),
            progress.get("total_movies"),
            elapsed_ms,
        )
    except Exception as exc:
        elapsed_ms = (perf_counter() - started_at) * 1000.0
        result = _build_result_snapshot(
            payload=payload,
            progress=progress,
            failure_samples=failure_samples,
            elapsed_ms=elapsed_ms,
        )
        fail_rag_rebuild_job(
            mysql_dsn=settings.core.mysql_dsn,
            job_id=job_id,
            error=f"{type(exc).__name__}: {exc}",
            progress=progress,
            result=result,
        )
        logger.exception(
            "RAG rebuild task crashed, task_id=%s, scope=%s, processed_movies=%s, completed_jobs=%s, failed_jobs=%s",
            job_id,
            progress.get("scope"),
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
    parser = argparse.ArgumentParser(description="Single-row RAG rebuild worker for ops_task queue")
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