import io
import uuid
from pathlib import Path

import pytest
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import StorageError
from app.db.models import Certificate, CertificateStatus, GenerationJob, JobStatus
from app.services.generation_service import (
    INVALID_RECIPIENT_MESSAGE,
    JOB_ABORTED_MESSAGE,
    RENDER_FAILED_MESSAGE,
    STORAGE_FAILED_MESSAGE,
    GenerationService,
    derive_final_status,
)
from app.services.storage import LocalStorage
from app.templates.certificate import CertificateData, render_certificate
from tests.conftest import create_job


def _certificates(db: Session, job_id: uuid.UUID) -> list[Certificate]:
    return list(db.scalars(select(Certificate).where(Certificate.job_id == job_id)))


def _always_fail(data: CertificateData) -> bytes:
    raise RuntimeError("renderer exploded")


@pytest.mark.parametrize(
    ("success", "failed", "expected"),
    [
        (10, 0, JobStatus.COMPLETED),
        (9, 1, JobStatus.PARTIALLY_COMPLETED),
        (1, 9, JobStatus.PARTIALLY_COMPLETED),
        (0, 10, JobStatus.FAILED),
    ],
)
def test_derive_final_status(success: int, failed: int, expected: JobStatus) -> None:
    assert derive_final_status(success, failed) == expected


def test_all_certificates_succeed(db_session: Session, storage: LocalStorage) -> None:
    job = create_job(db_session, recipient_count=3)

    GenerationService(db_session, storage).process_job(job.id)

    db_session.refresh(job)
    assert job.status == JobStatus.COMPLETED
    assert (job.success_count, job.failure_count) == (3, 0)
    assert job.started_at is not None and job.completed_at is not None
    for certificate in _certificates(db_session, job.id):
        assert certificate.status == CertificateStatus.SUCCESS
        assert certificate.completed_at is not None
        assert storage.exists(certificate.storage_key)


def test_generated_pdf_contains_recipient_information(
    db_session: Session, storage: LocalStorage
) -> None:
    job = create_job(db_session, recipient_count=1)

    GenerationService(db_session, storage).process_job(job.id)

    certificate = _certificates(db_session, job.id)[0]
    with storage.open(certificate.storage_key) as file:
        text = PdfReader(io.BytesIO(file.read())).pages[0].extract_text()
    assert certificate.recipient_name in text
    assert certificate.course in text
    assert job.event_name in text


def test_all_certificates_fail(db_session: Session, storage: LocalStorage) -> None:
    job = create_job(db_session, recipient_count=3)

    GenerationService(db_session, storage, renderer=_always_fail).process_job(job.id)

    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert (job.success_count, job.failure_count) == (0, 3)
    assert {c.error_message for c in _certificates(db_session, job.id)} == {RENDER_FAILED_MESSAGE}


def test_storage_failure_only_fails_that_certificate(db_session: Session, tmp_path: Path) -> None:
    job = create_job(db_session, recipient_count=3)
    broken_id = _certificates(db_session, job.id)[0].id

    class FlakyStorage(LocalStorage):
        def save(self, key: str, data: bytes) -> None:
            if str(broken_id) in key:
                raise StorageError("disk full")
            super().save(key, data)

    GenerationService(db_session, FlakyStorage(tmp_path)).process_job(job.id)

    db_session.refresh(job)
    assert job.status == JobStatus.PARTIALLY_COMPLETED
    broken = db_session.get(Certificate, broken_id)
    assert broken.status == CertificateStatus.FAILED
    assert broken.error_message == STORAGE_FAILED_MESSAGE
    assert broken.storage_key is None


@pytest.mark.parametrize("bad_name", ["   ", "张伟"])
def test_invalid_stored_recipient_is_failed_not_crashing(
    db_session: Session, storage: LocalStorage, bad_name: str
) -> None:
    job = create_job(db_session, recipient_count=2)
    bad = _certificates(db_session, job.id)[0]
    bad.recipient_name = bad_name  # e.g. a row inserted without going through the API
    db_session.commit()

    GenerationService(db_session, storage).process_job(job.id)

    db_session.refresh(job)
    assert job.status == JobStatus.PARTIALLY_COMPLETED
    assert db_session.get(Certificate, bad.id).error_message == INVALID_RECIPIENT_MESSAGE


def test_rerunning_a_finished_job_is_a_no_op(db_session: Session, storage: LocalStorage) -> None:
    job = create_job(db_session, recipient_count=2)
    GenerationService(db_session, storage).process_job(job.id)
    calls: list[CertificateData] = []

    def counting_renderer(data: CertificateData) -> bytes:
        calls.append(data)
        return render_certificate(data)

    GenerationService(db_session, storage, renderer=counting_renderer).process_job(job.id)

    db_session.refresh(job)
    assert calls == []
    assert (job.status, job.success_count) == (JobStatus.COMPLETED, 2)


def test_job_resumes_after_worker_crash(
    session_factory: sessionmaker[Session], storage: LocalStorage
) -> None:
    class WorkerCrash(BaseException):
        """Simulates the worker process dying (not a normal per-certificate error)."""

    rendered: list[CertificateData] = []

    def crash_on_fourth(data: CertificateData) -> bytes:
        if len(rendered) == 3:
            raise WorkerCrash()
        rendered.append(data)
        return render_certificate(data)

    with session_factory() as db:
        job_id = create_job(db, recipient_count=10).id
        with pytest.raises(WorkerCrash):
            GenerationService(db, storage, renderer=crash_on_fourth).process_job(job_id)

    # A new worker picks up the redelivered message with a fresh session.
    with session_factory() as db:
        job = db.get(GenerationJob, job_id)
        assert job.status == JobStatus.PROCESSING
        assert job.success_count == 3

        resumed: list[CertificateData] = []

        def counting_renderer(data: CertificateData) -> bytes:
            resumed.append(data)
            return render_certificate(data)

        GenerationService(db, storage, renderer=counting_renderer).process_job(job_id)

        db.refresh(job)
        assert len(resumed) == 7  # only the unfinished certificates were regenerated
        assert (job.status, job.success_count, job.failure_count) == (JobStatus.COMPLETED, 10, 0)


def test_mark_job_failed_fails_unfinished_certificates(
    db_session: Session, storage: LocalStorage
) -> None:
    job = create_job(db_session, recipient_count=3)

    GenerationService(db_session, storage).mark_job_failed(job.id, "Database unavailable")

    db_session.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.error_message == "Database unavailable"
    assert job.failure_count == 3
    assert {c.error_message for c in _certificates(db_session, job.id)} == {JOB_ABORTED_MESSAGE}


def test_unknown_job_is_ignored(db_session: Session, storage: LocalStorage, tmp_path: Path) -> None:
    GenerationService(db_session, storage).process_job(uuid.uuid4())

    assert not list(tmp_path.rglob("*.pdf"))
