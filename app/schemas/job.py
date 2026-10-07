import uuid
from datetime import date, datetime, timedelta

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.core.config import get_settings
from app.db.models import GenerationJob, JobStatus
from app.templates.certificate import is_renderable

# Certificates are normally issued for past or near-future events.
MAX_DAYS_IN_FUTURE = 365


def _ensure_renderable(value: str) -> str:
    if not is_renderable(value):
        raise ValueError("contains characters the certificate font cannot render")
    return value


class RecipientIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200, examples=["Alice Johnson"])
    email: EmailStr = Field(examples=["alice@example.com"])
    course: str = Field(min_length=1, max_length=200, examples=["Python Workshop"])

    @field_validator("email")
    @classmethod
    def normalize_email(cls, email: str) -> str:
        return email.lower()

    @field_validator("name", "course")
    @classmethod
    def printable_on_certificate(cls, value: str) -> str:
        return _ensure_renderable(value)


class JobCreate(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "event_name": "Python Workshop 2026",
                    "certificate_date": "2026-10-07",
                    "recipients": [
                        {
                            "name": "Alice",
                            "email": "alice@example.com",
                            "course": "Python Workshop",
                        },
                        {
                            "name": "Bob",
                            "email": "bob@example.com",
                            "course": "Python Workshop",
                        },
                    ],
                }
            ]
        },
    )

    event_name: str = Field(min_length=1, max_length=200)
    certificate_date: date
    recipients: list[RecipientIn] = Field(min_length=1)

    @field_validator("event_name")
    @classmethod
    def printable_on_certificate(cls, value: str) -> str:
        return _ensure_renderable(value)

    @field_validator("certificate_date")
    @classmethod
    def date_not_far_in_future(cls, value: date) -> date:
        if value > date.today() + timedelta(days=MAX_DAYS_IN_FUTURE):
            raise ValueError(
                f"certificate_date cannot be more than {MAX_DAYS_IN_FUTURE} days in the future"
            )
        return value

    @field_validator("recipients")
    @classmethod
    def within_batch_limit(cls, recipients: list[RecipientIn]) -> list[RecipientIn]:
        # Read at validation time so the limit is configurable via MAX_BATCH_SIZE.
        max_size = get_settings().max_batch_size
        if len(recipients) > max_size:
            raise ValueError(f"A job can contain at most {max_size} recipients")
        return recipients

    @model_validator(mode="after")
    def no_duplicate_recipients(self) -> "JobCreate":
        seen: set[tuple[str, str]] = set()
        for index, recipient in enumerate(self.recipients):
            key = (recipient.email, recipient.course.casefold())
            if key in seen:
                raise ValueError(
                    f"Duplicate recipient at index {index}: "
                    f"{recipient.email} is already listed for '{recipient.course}'"
                )
            seen.add(key)
        return self


class JobCreatedResponse(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    total: int


class JobStatusResponse(BaseModel):
    job_id: uuid.UUID
    event_name: str
    certificate_date: date
    status: JobStatus
    total: int
    successful: int
    failed: int
    pending: int = Field(description="Certificates not finished yet (pending or in progress)")
    progress: int = Field(description="Percentage of certificates processed (success + failed)")
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    @classmethod
    def from_job(cls, job: GenerationJob) -> "JobStatusResponse":
        processed = job.success_count + job.failure_count
        return cls(
            job_id=job.id,
            event_name=job.event_name,
            certificate_date=job.certificate_date,
            status=JobStatus(job.status),
            total=job.total_count,
            successful=job.success_count,
            failed=job.failure_count,
            pending=job.total_count - processed,
            # Floor division: progress only shows 100 when every certificate is done.
            progress=processed * 100 // job.total_count,
            error_message=job.error_message,
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
        )
