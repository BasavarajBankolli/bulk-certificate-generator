import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Certificate, CertificateStatus, GenerationJob
from app.services.generation_service import GenerationService
from app.services.storage import LocalStorage
from app.templates.certificate import CertificateData, render_certificate
from tests.conftest import create_job

FAILING_RECIPIENT = "Recipient 1"


def _fail_one(data: CertificateData) -> bytes:
    if data.recipient_name == FAILING_RECIPIENT:
        raise RuntimeError("boom")
    return render_certificate(data)


@pytest.fixture
def processed_job(db_session: Session, storage: LocalStorage) -> GenerationJob:
    """A finished job with 5 certificates: Recipient 1 failed, the other 4 succeeded."""
    job = create_job(db_session, recipient_count=5)
    GenerationService(db_session, storage, renderer=_fail_one).process_job(job.id)
    return job


def _certificate(db: Session, job_id: uuid.UUID, name: str) -> Certificate:
    return db.scalars(
        select(Certificate).where(Certificate.job_id == job_id, Certificate.recipient_name == name)
    ).one()


def test_download_successful_certificate(
    client: TestClient, db_session: Session, processed_job: GenerationJob
) -> None:
    certificate = _certificate(db_session, processed_job.id, "Recipient 0")

    response = client.get(f"/api/v1/certificates/{certificate.id}/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert f"certificate-{certificate.id}.pdf" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-")


def test_download_failed_certificate_returns_409_with_reason(
    client: TestClient, db_session: Session, processed_job: GenerationJob
) -> None:
    certificate = _certificate(db_session, processed_job.id, FAILING_RECIPIENT)

    response = client.get(f"/api/v1/certificates/{certificate.id}/download")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Certificate generation failed: Unable to generate certificate"
    }


def test_download_pending_certificate_returns_409(client: TestClient, db_session: Session) -> None:
    job = create_job(db_session, recipient_count=1)
    certificate = job.certificates[0]

    response = client.get(f"/api/v1/certificates/{certificate.id}/download")

    assert response.status_code == 409
    assert response.json() == {"detail": "Certificate is not ready yet (status: PENDING)"}


def test_download_missing_certificate_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/certificates/{uuid.uuid4()}/download")

    assert response.status_code == 404
    assert response.json() == {"detail": "Certificate not found"}


def test_download_when_file_is_missing_returns_500_without_internals(
    client: TestClient, db_session: Session, processed_job: GenerationJob, tmp_path: Path
) -> None:
    certificate = _certificate(db_session, processed_job.id, "Recipient 0")
    for pdf in tmp_path.rglob(f"{certificate.id}.pdf"):
        pdf.unlink()

    response = client.get(f"/api/v1/certificates/{certificate.id}/download")

    assert response.status_code == 500
    assert response.json() == {"detail": "Certificate file is unavailable"}


def test_get_certificate_metadata(
    client: TestClient, db_session: Session, processed_job: GenerationJob
) -> None:
    succeeded = _certificate(db_session, processed_job.id, "Recipient 0")
    failed = _certificate(db_session, processed_job.id, FAILING_RECIPIENT)

    ok_body = client.get(f"/api/v1/certificates/{succeeded.id}").json()
    failed_body = client.get(f"/api/v1/certificates/{failed.id}").json()

    assert ok_body["status"] == "SUCCESS"
    assert ok_body["download_url"] == f"/api/v1/certificates/{succeeded.id}/download"
    assert ok_body["recipient_email"] == "recipient0@example.com"
    assert failed_body["status"] == "FAILED"
    assert failed_body["error_message"] == "Unable to generate certificate"
    assert failed_body["download_url"] is None


def test_get_unknown_certificate_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/certificates/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Certificate not found"}


def test_list_job_certificates(client: TestClient, processed_job: GenerationJob) -> None:
    body = client.get(f"/api/v1/jobs/{processed_job.id}/certificates").json()

    assert body["total"] == 5
    assert (body["limit"], body["offset"]) == (50, 0)
    assert len(body["items"]) == 5
    assert {item["job_id"] for item in body["items"]} == {str(processed_job.id)}


def test_list_filters_by_status(client: TestClient, processed_job: GenerationJob) -> None:
    url = f"/api/v1/jobs/{processed_job.id}/certificates"

    failed = client.get(url, params={"status": "FAILED"}).json()
    succeeded = client.get(url, params={"status": "SUCCESS"}).json()

    assert failed["total"] == 1
    assert failed["items"][0]["recipient_name"] == FAILING_RECIPIENT
    assert succeeded["total"] == 4
    assert {item["status"] for item in succeeded["items"]} == {CertificateStatus.SUCCESS}


def test_list_paginates_without_overlap(client: TestClient, processed_job: GenerationJob) -> None:
    url = f"/api/v1/jobs/{processed_job.id}/certificates"

    pages = [client.get(url, params={"limit": 2, "offset": offset}).json() for offset in (0, 2, 4)]

    ids = [item["certificate_id"] for page in pages for item in page["items"]]
    assert [len(page["items"]) for page in pages] == [2, 2, 1]
    assert all(page["total"] == 5 for page in pages)
    assert len(set(ids)) == 5


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "DONE"}]
)
def test_list_rejects_invalid_query_parameters(
    client: TestClient, processed_job: GenerationJob, params: dict
) -> None:
    response = client.get(f"/api/v1/jobs/{processed_job.id}/certificates", params=params)

    assert response.status_code == 422


def test_list_for_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/jobs/{uuid.uuid4()}/certificates")

    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found"}
