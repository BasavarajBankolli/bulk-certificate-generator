"""Worker-side business logic: turn a queued job into PDF certificates.

This is plain Python (no Celery imports), so tests call it directly with a test
database session, temporary storage and, where needed, a failing renderer.
"""

import logging
import uuid
from collections.abc import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.exceptions import StorageError
from app.db.models import (
    TERMINAL_JOB_STATUSES,
    UNFINISHED_CERTIFICATE_STATUSES,
    Certificate,
    CertificateStatus,
    GenerationJob,
    JobStatus,
    utcnow,
)
from app.services.storage import StorageBackend, certificate_key
from app.templates.certificate import CertificateData, render_certificate

logger = logging.getLogger(__name__)

Renderer = Callable[[CertificateData], bytes]

# Client-safe messages stored on failed certificates. Details go to the logs.
RENDER_FAILED_MESSAGE = "Unable to generate certificate"
STORAGE_FAILED_MESSAGE = "Unable to store certificate"
INVALID_RECIPIENT_MESSAGE = "Invalid recipient data"
JOB_ABORTED_MESSAGE = "Job processing was aborted"


class InvalidRecipientError(ValueError):
    pass


def derive_final_status(success_count: int, failure_count: int) -> JobStatus:
    """Final job status once every certificate has been processed."""
    if failure_count == 0:
        return JobStatus.COMPLETED
    if success_count == 0:
        return JobStatus.FAILED
    return JobStatus.PARTIALLY_COMPLETED


class GenerationService:
    def __init__(
        self, db: Session, storage: StorageBackend, renderer: Renderer = render_certificate
    ) -> None:
        self._db = db
        self._storage = storage
        self._renderer = renderer

    def process_job(self, job_id: uuid.UUID) -> None:
        """Generate every unfinished certificate of a job, then set its final status.

        Safe to run more than once (e.g. after a worker crash): finished jobs are skipped
        and only PENDING/PROCESSING certificates are (re)generated.
        """
        job = self._db.get(GenerationJob, job_id)
        if job is None:
            logger.warning("job_id=%s not found; nothing to process", job_id)
            return
        if job.status in TERMINAL_JOB_STATUSES:
            logger.info("job_id=%s already %s; skipping", job_id, job.status)
            return

        self._mark_job_processing(job)
        for certificate in self._unfinished_certificates(job_id):
            self._process_certificate(job, certificate)
        self._finalize_job(job)

    def mark_job_failed(self, job_id: uuid.UUID, reason: str) -> None:
        """Job-level failure (e.g. retries exhausted): fail whatever is left unfinished."""
        job = self._db.get(GenerationJob, job_id)
        if job is None or job.status in TERMINAL_JOB_STATUSES:
            return

        result = self._db.execute(
            update(Certificate)
            .where(
                Certificate.job_id == job_id,
                Certificate.status.in_(UNFINISHED_CERTIFICATE_STATUSES),
            )
            .values(
                status=CertificateStatus.FAILED,
                error_message=JOB_ABORTED_MESSAGE,
                completed_at=utcnow(),
            )
        )
        job.failure_count += result.rowcount
        job.status = JobStatus.FAILED
        job.error_message = reason
        job.completed_at = utcnow()
        self._db.commit()
        logger.error("job_id=%s marked FAILED: %s", job_id, reason)

    def _mark_job_processing(self, job: GenerationJob) -> None:
        job.status = JobStatus.PROCESSING
        job.started_at = job.started_at or utcnow()
        self._db.commit()
        logger.info("job_id=%s processing started", job.id)

    def _unfinished_certificates(self, job_id: uuid.UUID) -> list[Certificate]:
        return list(
            self._db.scalars(
                select(Certificate)
                .where(
                    Certificate.job_id == job_id,
                    Certificate.status.in_(UNFINISHED_CERTIFICATE_STATUSES),
                )
                .order_by(Certificate.created_at, Certificate.id)
            )
        )

    def _process_certificate(self, job: GenerationJob, certificate: Certificate) -> None:
        certificate.status = CertificateStatus.PROCESSING
        self._db.commit()

        # Failure isolation: anything that goes wrong while building or storing *this*
        # PDF marks only this certificate FAILED, and the loop moves on to the next one.
        # Database errors are deliberately outside this try: those are job-level problems
        # handled by the Celery task's retry logic.
        try:
            _validate_recipient(certificate)
            pdf_bytes = self._renderer(_certificate_data(job, certificate))
            key = certificate_key(job.id, certificate.id)
            self._storage.save(key, pdf_bytes)
        except InvalidRecipientError as exc:
            logger.warning("certificate_id=%s invalid recipient: %s", certificate.id, exc)
            self._record_failure(job, certificate, INVALID_RECIPIENT_MESSAGE)
        except StorageError:
            logger.exception("certificate_id=%s job_id=%s storage failed", certificate.id, job.id)
            self._record_failure(job, certificate, STORAGE_FAILED_MESSAGE)
        except Exception:
            logger.exception("certificate_id=%s job_id=%s render failed", certificate.id, job.id)
            self._record_failure(job, certificate, RENDER_FAILED_MESSAGE)
        else:
            self._record_success(job, certificate, key)

    def _record_success(self, job: GenerationJob, certificate: Certificate, key: str) -> None:
        certificate.status = CertificateStatus.SUCCESS
        certificate.storage_key = key
        certificate.error_message = None
        certificate.completed_at = utcnow()
        # Counter is incremented in SQL, in the same transaction as the certificate
        # update, so counters can never disagree with certificate statuses.
        self._db.execute(
            update(GenerationJob)
            .where(GenerationJob.id == job.id)
            .values(success_count=GenerationJob.success_count + 1)
        )
        self._db.commit()

    def _record_failure(self, job: GenerationJob, certificate: Certificate, message: str) -> None:
        certificate.status = CertificateStatus.FAILED
        certificate.error_message = message
        certificate.completed_at = utcnow()
        self._db.execute(
            update(GenerationJob)
            .where(GenerationJob.id == job.id)
            .values(failure_count=GenerationJob.failure_count + 1)
        )
        self._db.commit()

    def _finalize_job(self, job: GenerationJob) -> None:
        self._db.refresh(job)  # pick up counters incremented in SQL
        job.status = derive_final_status(job.success_count, job.failure_count)
        job.completed_at = utcnow()
        self._db.commit()
        logger.info(
            "job_id=%s finished status=%s successful=%d failed=%d",
            job.id,
            job.status,
            job.success_count,
            job.failure_count,
        )


def _validate_recipient(certificate: Certificate) -> None:
    """Defensive check: the API already validated input, but never trust stored rows blindly."""
    if not certificate.recipient_name.strip():
        raise InvalidRecipientError("recipient name is empty")
    if not certificate.course.strip():
        raise InvalidRecipientError("course is empty")


def _certificate_data(job: GenerationJob, certificate: Certificate) -> CertificateData:
    return CertificateData(
        certificate_id=certificate.id,
        recipient_name=certificate.recipient_name,
        course=certificate.course,
        event_name=job.event_name,
        certificate_date=job.certificate_date,
    )
