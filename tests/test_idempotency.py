import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import GenerationJob
from app.schemas.job import JobCreate
from app.services.job_service import JobService
from tests.conftest import FakeQueue, job_payload

JOBS_URL = "/api/v1/jobs"


def _job_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(GenerationJob))


def test_same_key_and_same_body_returns_existing_job(
    client: TestClient, db_session: Session, queue: FakeQueue
) -> None:
    headers = {"Idempotency-Key": "order-123"}

    first = client.post(JOBS_URL, json=job_payload(), headers=headers)
    second = client.post(JOBS_URL, json=job_payload(), headers=headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]
    assert _job_count(db_session) == 1
    # The replay must not enqueue the job a second time.
    assert queue.job_ids == [first.json()["job_id"]]


def test_same_key_with_equivalent_body_after_normalisation_is_a_replay(
    client: TestClient,
) -> None:
    headers = {"Idempotency-Key": "order-456"}
    payload = job_payload(
        recipients=[{"name": "Alice", "email": "alice@example.com", "course": "Python"}]
    )
    equivalent = job_payload(
        recipients=[{"name": " Alice ", "email": "ALICE@example.com", "course": "Python"}]
    )

    first = client.post(JOBS_URL, json=payload, headers=headers)
    second = client.post(JOBS_URL, json=equivalent, headers=headers)

    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]


def test_same_key_with_different_body_returns_409(client: TestClient, db_session: Session) -> None:
    headers = {"Idempotency-Key": "order-789"}

    client.post(JOBS_URL, json=job_payload(recipient_count=1), headers=headers)
    response = client.post(JOBS_URL, json=job_payload(recipient_count=2), headers=headers)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Idempotency-Key has already been used with a different request"
    }
    assert _job_count(db_session) == 1


def test_requests_without_key_always_create_new_jobs(
    client: TestClient, db_session: Session
) -> None:
    first = client.post(JOBS_URL, json=job_payload())
    second = client.post(JOBS_URL, json=job_payload())

    assert first.status_code == second.status_code == 201
    assert first.json()["job_id"] != second.json()["job_id"]
    assert _job_count(db_session) == 2


def test_different_keys_create_different_jobs(client: TestClient, db_session: Session) -> None:
    client.post(JOBS_URL, json=job_payload(), headers={"Idempotency-Key": "a"})
    client.post(JOBS_URL, json=job_payload(), headers={"Idempotency-Key": "b"})

    assert _job_count(db_session) == 2


def test_concurrent_duplicate_is_resolved_by_unique_constraint(
    db_session: Session, queue: FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = JobService(db_session, queue)
    request = JobCreate.model_validate(job_payload())
    first, _ = service.create_job(request, "race-key")

    # Simulate a race: the pre-insert lookup misses the row another request just committed,
    # so the INSERT hits the UNIQUE constraint and the service must recover.
    real_lookup = service._find_by_idempotency_key
    lookups: list[str] = []

    def racing_lookup(key: str) -> GenerationJob | None:
        lookups.append(key)
        return None if len(lookups) == 1 else real_lookup(key)

    monkeypatch.setattr(service, "_find_by_idempotency_key", racing_lookup)

    second, created = service.create_job(request, "race-key")

    assert created is False
    assert second.id == first.id
    assert _job_count(db_session) == 1
