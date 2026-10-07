import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_enqueuer
from app.db.models import Certificate, CertificateStatus, GenerationJob, JobStatus
from app.main import app
from tests.conftest import FakeQueue, job_payload

JOBS_URL = "/api/v1/jobs"


def _count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model))


def test_create_job_returns_201_and_queues_job(
    client: TestClient, db_session: Session, queue: FakeQueue
) -> None:
    response = client.post(JOBS_URL, json=job_payload(recipient_count=2))

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == JobStatus.QUEUED
    assert body["total"] == 2
    assert response.headers["location"] == f"/api/v1/jobs/{body['job_id']}"
    # The job was handed to the queue; no PDF was generated during the request.
    assert queue.job_ids == [body["job_id"]]

    job = db_session.get(GenerationJob, uuid.UUID(body["job_id"]))
    assert job is not None
    assert {cert.status for cert in job.certificates} == {CertificateStatus.PENDING}
    assert len(job.certificates) == 2


def test_create_job_stores_normalised_recipient_data(
    client: TestClient, db_session: Session
) -> None:
    payload = job_payload(
        recipients=[{"name": "  Alice  ", "email": "Alice@Example.com", "course": "Python"}]
    )

    response = client.post(JOBS_URL, json=payload)

    certificate = db_session.scalars(select(Certificate)).one()
    assert response.status_code == 201
    assert certificate.recipient_name == "Alice"
    assert certificate.recipient_email == "alice@example.com"


@pytest.mark.parametrize(
    ("payload", "expected_message"),
    [
        (job_payload(recipients=[]), "at least 1 item"),
        (
            job_payload(recipients=[{"name": "A", "email": "not-an-email", "course": "Py"}]),
            "valid email",
        ),
        (job_payload(recipients=[{"email": "a@example.com", "course": "Py"}]), "Field required"),
        (job_payload(recipients=[{"name": "  ", "email": "a@example.com", "course": "Py"}]), ""),
        (job_payload(recipient_count=1001), "at most 1000 recipients"),
        (
            job_payload(
                recipients=[
                    {"name": "Alice", "email": "alice@example.com", "course": "Python"},
                    {"name": "Alice B", "email": "ALICE@example.com", "course": "python"},
                ]
            ),
            "Duplicate recipient",
        ),
        (job_payload(certificate_date="07-10-2026"), ""),
        (job_payload(event_name=""), ""),
    ],
    ids=[
        "empty-recipients",
        "invalid-email",
        "missing-name",
        "blank-name",
        "oversized-batch",
        "duplicate-recipient",
        "invalid-date",
        "empty-event-name",
    ],
)
def test_invalid_requests_return_422_and_create_nothing(
    client: TestClient,
    db_session: Session,
    queue: FakeQueue,
    payload: dict,
    expected_message: str,
) -> None:
    response = client.post(JOBS_URL, json=payload)

    assert response.status_code == 422
    assert expected_message in str(response.json()["detail"])
    assert _count(db_session, GenerationJob) == 0
    assert queue.job_ids == []


def test_malformed_json_returns_422(client: TestClient) -> None:
    response = client.post(
        JOBS_URL, content="{not json", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422


def test_queue_failure_returns_503_and_discards_job(
    client: TestClient, db_session: Session
) -> None:
    def broken_queue(job_id: str) -> None:
        raise ConnectionError("Redis is down")

    app.dependency_overrides[get_enqueuer] = lambda: broken_queue

    response = client.post(JOBS_URL, json=job_payload())

    assert response.status_code == 503
    assert response.json() == {"detail": "Job queue is unavailable, please retry later"}
    assert _count(db_session, GenerationJob) == 0
    assert _count(db_session, Certificate) == 0
