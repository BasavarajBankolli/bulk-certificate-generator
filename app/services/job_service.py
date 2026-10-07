import hashlib
import logging
import uuid
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import IdempotencyConflictError, JobNotFoundError, QueueUnavailableError
from app.db.models import Certificate, CertificateStatus, GenerationJob
from app.schemas.job import JobCreate

logger = logging.getLogger(__name__)

# Publishes a job ID to the background queue. Injected so tests can use a fake.
Enqueuer = Callable[[str], None]


def hash_request(request: JobCreate) -> str:
    """SHA-256 of the *validated* request.

    Hashing after validation means whitespace/email-case differences that normalise to
    the same data are treated as the same request.
    """
    return hashlib.sha256(request.model_dump_json().encode()).hexdigest()


class JobService:
    def __init__(self, db: Session, enqueue: Enqueuer) -> None:
        self._db = db
        self._enqueue = enqueue

    def create_job(
        self, request: JobCreate, idempotency_key: str | None
    ) -> tuple[GenerationJob, bool]:
        """Create and enqueue a job. Returns ``(job, created)``.

        ``created`` is False when an earlier job with the same Idempotency-Key is returned.
        """
        request_hash = hash_request(request)

        if idempotency_key:
            existing = self._find_by_idempotency_key(idempotency_key)
            if existing:
                return self._replay(existing, request_hash), False

        job = self._build_job(request, idempotency_key, request_hash)
        self._db.add(job)
        try:
            # Job and all certificate rows are committed in one transaction.
            self._db.commit()
        except IntegrityError:
            # Two identical requests raced: the UNIQUE constraint on idempotency_key
            # let only one INSERT win. Return the winner instead of failing.
            self._db.rollback()
            existing = self._find_by_idempotency_key(idempotency_key) if idempotency_key else None
            if existing is None:
                raise
            return self._replay(existing, request_hash), False

        self._enqueue_or_discard(job)
        logger.info("Created job_id=%s with %d recipients", job.id, job.total_count)
        return job, True

    def get_job(self, job_id: uuid.UUID) -> GenerationJob:
        job = self._db.get(GenerationJob, job_id)
        if job is None:
            raise JobNotFoundError()
        return job

    def list_certificates(
        self,
        job_id: uuid.UUID,
        status: CertificateStatus | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Certificate], int]:
        """One page of a job's certificates plus the total matching count."""
        self.get_job(job_id)  # 404 for unknown jobs instead of an empty list

        query = select(Certificate).where(Certificate.job_id == job_id)
        if status is not None:
            query = query.where(Certificate.status == status)

        total = self._db.scalar(select(func.count()).select_from(query.subquery()))
        # Ordering by (created_at, id) keeps pagination stable between requests.
        page = self._db.scalars(
            query.order_by(Certificate.created_at, Certificate.id).limit(limit).offset(offset)
        )
        return list(page), total

    def _find_by_idempotency_key(self, key: str) -> GenerationJob | None:
        return self._db.scalar(select(GenerationJob).where(GenerationJob.idempotency_key == key))

    @staticmethod
    def _replay(existing: GenerationJob, request_hash: str) -> GenerationJob:
        if existing.request_hash != request_hash:
            raise IdempotencyConflictError()
        logger.info("Idempotent replay for job_id=%s", existing.id)
        return existing

    @staticmethod
    def _build_job(
        request: JobCreate, idempotency_key: str | None, request_hash: str
    ) -> GenerationJob:
        job = GenerationJob(
            id=uuid.uuid4(),
            event_name=request.event_name,
            certificate_date=request.certificate_date,
            total_count=len(request.recipients),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        job.certificates = [
            Certificate(
                id=uuid.uuid4(),
                recipient_name=recipient.name,
                recipient_email=recipient.email,
                course=recipient.course,
            )
            for recipient in request.recipients
        ]
        return job

    def _enqueue_or_discard(self, job: GenerationJob) -> None:
        """Enqueue after commit, so the worker can never see a job that isn't saved yet.

        If the broker is down, delete the job (a compensating action) and return 503.
        The client can then safely retry, even with the same Idempotency-Key.
        """
        try:
            self._enqueue(str(job.id))
        except Exception as exc:  # any broker/network error means the job would never run
            logger.exception("Failed to enqueue job_id=%s; discarding it", job.id)
            self._db.delete(job)
            self._db.commit()
            raise QueueUnavailableError() from exc
