import uuid
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"


class CertificateStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.PARTIALLY_COMPLETED, JobStatus.FAILED}
)

# Certificates the worker still has to (re)process. PROCESSING is included so a job
# interrupted by a worker crash resumes from where it stopped.
UNFINISHED_CERTIFICATE_STATUSES = (CertificateStatus.PENDING, CertificateStatus.PROCESSING)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _status_check(statuses: type[StrEnum]) -> str:
    # Statuses are stored as VARCHAR + CHECK rather than a PostgreSQL ENUM type,
    # so adding a status later is a simple constraint change (no ALTER TYPE).
    allowed = ", ".join(f"'{status.value}'" for status in statuses)
    return f"status IN ({allowed})"


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_name: Mapped[str] = mapped_column(String(200))
    certificate_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default=JobStatus.QUEUED)

    total_count: Mapped[int]
    success_count: Mapped[int] = mapped_column(default=0, server_default="0")
    failure_count: Mapped[int] = mapped_column(default=0, server_default="0")

    # Idempotency: the client-supplied key plus a SHA-256 of the request body,
    # so a reused key with a *different* body can be rejected with 409.
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))

    # Job-level failure reason (e.g. worker gave up after retries).
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    certificates: Mapped[list["Certificate"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(_status_check(JobStatus), name="valid_status"),
        CheckConstraint("total_count > 0", name="positive_total"),
        CheckConstraint("success_count >= 0 AND failure_count >= 0", name="non_negative_counts"),
        # Supports operational queries such as "jobs stuck in PROCESSING".
        Index("ix_generation_jobs_status", "status"),
    )


class Certificate(Base):
    __tablename__ = "certificates"

    # Also printed on the PDF as a verification reference.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_jobs.id", ondelete="CASCADE"))

    recipient_name: Mapped[str] = mapped_column(String(200))
    recipient_email: Mapped[str] = mapped_column(String(320))  # RFC 5321 maximum
    course: Mapped[str] = mapped_column(String(200))

    status: Mapped[str] = mapped_column(String(20), default=CertificateStatus.PENDING)
    # A storage *key* (e.g. "certificates/<job>/<id>.pdf"), not a filesystem path,
    # so the storage backend can be swapped for S3 without a schema change.
    storage_key: Mapped[str | None] = mapped_column(String(500))
    # Client-safe message only; full tracebacks go to the logs.
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[GenerationJob] = relationship(back_populates="certificates")

    __table_args__ = (
        CheckConstraint(_status_check(CertificateStatus), name="valid_status"),
        CheckConstraint(
            "status <> 'SUCCESS' OR storage_key IS NOT NULL", name="success_has_storage_key"
        ),
        # One composite index serves: list a job's certificates, filter them by status,
        # and let the worker fetch the unfinished ones.
        Index("ix_certificates_job_id_status", "job_id", "status"),
    )
