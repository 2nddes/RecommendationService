from __future__ import annotations

import argparse
import logging
import time

from app.common.logging_setup import configure_logging
from app.common.settings import Settings
from app.ops.model_ops import claim_next_model_train_job, update_model_train_job
from app.reco.offline.training_dispatcher import train_models


logger = logging.getLogger(__name__)


def _job_target(job: dict) -> tuple[str | None, str | None]:
    payload = job.get("payload") or {}
    if not isinstance(payload, dict):
        return None, None
    component = payload.get("component")
    model = payload.get("model")
    return (str(component) if component is not None else None, str(model) if model is not None else None)


def run_once() -> bool:
    settings = Settings.from_config()
    job = claim_next_model_train_job(mysql_dsn=settings.core.mysql_dsn)
    if job is None:
        return False

    job_id = int(job["id"])
    component, model = _job_target(job)
    logger.info("Worker claimed train job, job_id=%s, component=%s, model=%s", job_id, component, model)

    try:
        train_models(
            settings,
            component=component,
            model=model,
            train_job_id=job_id,
        )
        logger.info("Worker finished train job, job_id=%s", job_id)
    except Exception as e:
        logger.exception(
            "Worker failed train job, job_id=%s, component=%s, model=%s",
            job_id,
            component,
            model,
        )
        update_model_train_job(
            mysql_dsn=settings.core.mysql_dsn,
            job_id=job_id,
            status="failed",
            metrics={
                "error": f"{type(e).__name__}: {e}",
                "component": component,
                "model": model,
                "worker": "app.ops.train_worker",
            },
            set_finished_at=True,
        )
    return True


def run_loop(*, interval_seconds: float = 3.0) -> None:
    logger.info("Train worker started, polling_interval_seconds=%s", interval_seconds)
    while True:
        handled = run_once()
        if handled:
            continue
        time.sleep(float(interval_seconds))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Independent train worker for unified ops_task queue")
    parser.add_argument("--once", action="store_true", help="Process at most one pending job and exit")
    parser.add_argument("--interval", type=float, default=3.0, help="Polling interval in seconds")
    return parser.parse_args()


def main() -> None:
    log_file = configure_logging()
    logger.info("Train worker logging initialized, log_file=%s", str(log_file))
    args = _parse_args()
    if args.once:
        run_once()
        return
    run_loop(interval_seconds=float(args.interval))


if __name__ == "__main__":
    main()
