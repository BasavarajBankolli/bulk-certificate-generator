import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Certificate, CertificateStatus, GenerationJob, JobStatus
from app.schemas.job import JobCreate, JobStatusResponse


def _payload(**overrides: object) -> dict:
    payload = {
        "event_name": "Python Workshop 2026",
        "certificate_date": "2026-10-07",
        "recipients": [{"name": "Alice", "email": "alice@example.com", "course": "Python"}],
    }
    payload.update(overrides)
    return payload


def test_job_with_certificates_is_persisted(db_session: Session) -> None:
    job = GenerationJob(event_name="Event", certificate_date=date(2026, 10, 7), total_count=1)
    job.certificates.append(
        Certificate(recipient_name="Alice", recipient_email="alice@example.com", course="Python")
    )
    db_session.add(job)
    db_session.commit()

    assert job.status == JobStatus.QUEUED
    assert job.success_count == 0
    assert job.certificates[0].status == CertificateStatus.PENDING


def test_idempotency_key_is_unique(db_session: Session) -> None:
    for _ in range(2):
        db_session.add(
            GenerationJob(
                event_name="Event",
                certificate_date=date(2026, 10, 7),
                total_count=1,
                idempotency_key="same-key",
            )
        )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_successful_certificate_requires_storage_key(db_session: Session) -> None:
    job = GenerationJob(event_name="Event", certificate_date=date(2026, 10, 7), total_count=1)
    job.certificates.append(
        Certificate(
            recipient_name="Alice",
            recipient_email="alice@example.com",
            course="Python",
            status=CertificateStatus.SUCCESS,
        )
    )
    db_session.add(job)
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_schema_strips_whitespace_and_lowercases_email() -> None:
    request = JobCreate.model_validate(
        _payload(
            recipients=[{"name": "  Alice  ", "email": "Alice@Example.COM", "course": " Python "}]
        )
    )

    recipient = request.recipients[0]
    assert (recipient.name, recipient.email, recipient.course) == (
        "Alice",
        "alice@example.com",
        "Python",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"recipients": []},
        {"event_name": "   "},
        {"certificate_date": "2026-02-30"},
        {"certificate_date": (date.today() + timedelta(days=400)).isoformat()},
        {"recipients": [{"name": "", "email": "a@example.com", "course": "Python"}]},
        {"recipients": [{"name": "Alice", "email": "not-an-email", "course": "Python"}]},
        {"recipients": [{"name": "Alice", "email": "a@example.com"}]},
    ],
)
def test_schema_rejects_invalid_input(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        JobCreate.model_validate(_payload(**overrides))


def test_schema_rejects_duplicate_recipient_for_same_course() -> None:
    duplicate = {"name": "Alice", "email": "alice@example.com", "course": "Python"}
    with pytest.raises(ValidationError, match="Duplicate recipient"):
        JobCreate.model_validate(_payload(recipients=[duplicate, {**duplicate, "name": "A."}]))


def test_status_response_computes_pending_and_progress() -> None:
    job = GenerationJob(
        id=uuid.uuid4(),
        event_name="Event",
        certificate_date=date(2026, 10, 7),
        status=JobStatus.PROCESSING,
        total_count=3,
        success_count=1,
        failure_count=1,
        created_at=datetime.now(UTC),
    )

    response = JobStatusResponse.from_job(job)

    assert (response.successful, response.failed, response.pending) == (1, 1, 1)
    assert response.progress == 66
