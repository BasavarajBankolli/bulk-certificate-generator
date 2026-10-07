import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services.generation_service import GenerationService
from app.services.storage import LocalStorage
from app.templates.certificate import CertificateData, render_certificate
from tests.conftest import create_job, job_payload


def _status(client: TestClient, job_id: uuid.UUID | str) -> dict:
    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    return response.json()


def _fail_all(data: CertificateData) -> bytes:
    raise RuntimeError("renderer exploded")


def test_new_job_is_queued(client: TestClient) -> None:
    job_id = client.post("/api/v1/jobs", json=job_payload(recipient_count=3)).json()["job_id"]

    body = _status(client, job_id)

    assert body["status"] == "QUEUED"
    assert (body["total"], body["successful"], body["failed"], body["pending"]) == (3, 0, 0, 3)
    assert body["progress"] == 0
    assert body["started_at"] is None
    assert body["event_name"] == "Python Workshop 2026"


def test_job_is_processing_while_certificates_are_generated(
    client: TestClient, db_session: Session, storage: LocalStorage
) -> None:
    job = create_job(db_session, recipient_count=2)
    snapshots: list[dict] = []

    def observing_renderer(data: CertificateData) -> bytes:
        # Poll the API from inside the worker, exactly like a client would mid-job.
        snapshots.append(_status(client, job.id))
        return render_certificate(data)

    GenerationService(db_session, storage, renderer=observing_renderer).process_job(job.id)

    first, second = snapshots
    assert first["status"] == "PROCESSING"
    assert first["started_at"] is not None
    assert (first["successful"], first["pending"], first["progress"]) == (0, 2, 0)
    assert (second["successful"], second["pending"], second["progress"]) == (1, 1, 50)


def test_job_is_completed_when_all_succeed(
    client: TestClient, db_session: Session, storage: LocalStorage
) -> None:
    job = create_job(db_session, recipient_count=3)

    GenerationService(db_session, storage).process_job(job.id)

    body = _status(client, job.id)
    assert body["status"] == "COMPLETED"
    assert (body["successful"], body["failed"], body["pending"], body["progress"]) == (3, 0, 0, 100)
    assert body["completed_at"] is not None


def test_job_is_partially_completed_when_some_fail(
    client: TestClient, db_session: Session, storage: LocalStorage
) -> None:
    job = create_job(db_session, recipient_count=4)

    def fail_first(data: CertificateData) -> bytes:
        if data.recipient_name == "Recipient 0":
            raise RuntimeError("boom")
        return render_certificate(data)

    GenerationService(db_session, storage, renderer=fail_first).process_job(job.id)

    body = _status(client, job.id)
    assert body["status"] == "PARTIALLY_COMPLETED"
    assert (body["successful"], body["failed"], body["pending"], body["progress"]) == (3, 1, 0, 100)


def test_job_is_failed_when_all_fail(
    client: TestClient, db_session: Session, storage: LocalStorage
) -> None:
    job = create_job(db_session, recipient_count=2)

    GenerationService(db_session, storage, renderer=_fail_all).process_job(job.id)

    body = _status(client, job.id)
    assert body["status"] == "FAILED"
    assert (body["successful"], body["failed"], body["pending"], body["progress"]) == (0, 2, 0, 100)


def test_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/jobs/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found"}


def test_malformed_job_id_returns_422(client: TestClient) -> None:
    assert client.get("/api/v1/jobs/not-a-uuid").status_code == 422
