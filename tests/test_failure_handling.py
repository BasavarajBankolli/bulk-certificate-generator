"""The most important requirement: one bad certificate must not stop the rest."""

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Certificate, CertificateStatus, JobStatus
from app.services.generation_service import RENDER_FAILED_MESSAGE, GenerationService
from app.services.storage import LocalStorage
from app.templates.certificate import CertificateData, render_certificate
from tests.conftest import create_job

FAILING_RECIPIENT = "Recipient 4"  # in the middle of the batch, not first or last


def _fail_for_one_recipient(data: CertificateData) -> bytes:
    if data.recipient_name == FAILING_RECIPIENT:
        raise RuntimeError("simulated rendering failure")
    return render_certificate(data)


def test_one_failure_out_of_ten_does_not_stop_the_job(
    client: TestClient, db_session: Session, storage: LocalStorage, tmp_path: Path
) -> None:
    job = create_job(db_session, recipient_count=10)

    GenerationService(db_session, storage, renderer=_fail_for_one_recipient).process_job(job.id)

    db_session.refresh(job)
    certificates = list(db_session.scalars(select(Certificate).where(Certificate.job_id == job.id)))
    succeeded = [c for c in certificates if c.status == CertificateStatus.SUCCESS]
    failed = [c for c in certificates if c.status == CertificateStatus.FAILED]

    assert len(succeeded) == 9
    assert len(failed) == 1
    assert failed[0].recipient_name == FAILING_RECIPIENT
    assert failed[0].error_message == RENDER_FAILED_MESSAGE
    assert failed[0].storage_key is None

    # 9 PDFs really exist on disk, one per successful certificate.
    assert len(list(tmp_path.rglob("*.pdf"))) == 9
    assert all(storage.exists(c.storage_key) for c in succeeded)

    assert job.status == JobStatus.PARTIALLY_COMPLETED
    assert (job.success_count, job.failure_count, job.total_count) == (9, 1, 10)

    # And the API reports exactly what the assignment expects.
    body = client.get(f"/api/v1/jobs/{job.id}").json()
    assert body["status"] == "PARTIALLY_COMPLETED"
    assert (body["successful"], body["failed"], body["pending"], body["progress"]) == (9, 1, 0, 100)
