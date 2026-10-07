"""The Celery task wrapper, tested without Redis by calling the task function directly."""

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import GenerationJob, JobStatus
from app.workers import tasks
from tests.conftest import create_job


class RetryRequested(Exception):
    pass


@pytest.fixture
def task_db(monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session]) -> None:
    monkeypatch.setattr(tasks, "SessionLocal", session_factory)


def _raise(exc: BaseException):
    def raiser(job_id: str) -> None:
        raise exc

    return raiser


def test_database_error_triggers_retry_with_backoff(
    task_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    retries: list[dict] = []

    def fake_retry(**kwargs: object) -> RetryRequested:
        retries.append(kwargs)
        return RetryRequested()

    monkeypatch.setattr(tasks, "_run", _raise(OperationalError("SELECT 1", {}, Exception())))
    monkeypatch.setattr(tasks.process_job, "retry", fake_retry)

    with pytest.raises(RetryRequested):
        tasks.process_job.run("00000000-0000-0000-0000-000000000000")

    assert retries[0]["countdown"] == tasks.RETRY_BASE_DELAY_SECONDS


def test_unexpected_error_marks_job_failed(
    task_db: None, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = create_job(db_session, recipient_count=2)
    monkeypatch.setattr(tasks, "_run", _raise(RuntimeError("bug")))

    with pytest.raises(RuntimeError):
        tasks.process_job.run(str(job.id))

    db_session.expire_all()
    failed_job = db_session.get(GenerationJob, job.id)
    assert failed_job.status == JobStatus.FAILED
    assert failed_job.error_message == "Unexpected error while processing job"
    assert failed_job.failure_count == 2
