"""Celery task: a thin wrapper around GenerationService.

All business logic lives in the service; this module only handles sessions,
retries and the "give up" path.
"""

import logging
import uuid

from celery import Task
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.services.generation_service import GenerationService
from app.services.storage import create_storage
from app.workers.celery_app import PROCESS_JOB_TASK, celery_app

logger = logging.getLogger(__name__)
configure_logging(get_settings().log_level)

MAX_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 5


@celery_app.task(bind=True, name=PROCESS_JOB_TASK, max_retries=MAX_RETRIES)
def process_job(self: Task, job_id: str) -> None:
    try:
        _run(job_id)
    except OperationalError as exc:
        # Database unreachable: retry with exponential backoff (5s, 10s, 20s).
        # Retrying is safe because process_job skips certificates that are already done.
        if self.request.retries < MAX_RETRIES:
            logger.warning("job_id=%s database error, retrying: %s", job_id, exc)
            countdown = RETRY_BASE_DELAY_SECONDS * 2**self.request.retries
            raise self.retry(exc=exc, countdown=countdown) from exc
        _mark_failed(job_id, "Database unavailable while processing job")
        raise
    except Exception:
        logger.exception("job_id=%s unexpected error", job_id)
        _mark_failed(job_id, "Unexpected error while processing job")
        raise


def _run(job_id: str) -> None:
    with SessionLocal() as db:
        service = GenerationService(db, create_storage(get_settings()))
        service.process_job(uuid.UUID(job_id))


def _mark_failed(job_id: str, reason: str) -> None:
    # Best effort with a fresh session: if the database is still down this fails too,
    # and the job stays PROCESSING (see README "Known limitations").
    try:
        with SessionLocal() as db:
            GenerationService(db, create_storage(get_settings())).mark_job_failed(
                uuid.UUID(job_id), reason
            )
    except Exception:
        logger.exception("job_id=%s could not be marked FAILED", job_id)
