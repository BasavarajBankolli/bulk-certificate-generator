import os
from collections.abc import Iterator
from pathlib import Path

# Must be set before any `app` module is imported: settings require DATABASE_URL.
# Tests run on in-memory SQLite by default; set TEST_DATABASE_URL to use PostgreSQL.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "sqlite://")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_enqueuer, get_storage
from app.db import models  # noqa: F401  (registers tables on Base.metadata)
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.services.storage import LocalStorage


class FakeQueue:
    """Stands in for Celery/Redis: records job IDs instead of publishing them."""

    def __init__(self) -> None:
        self.job_ids: list[str] = []

    def __call__(self, job_id: str) -> None:
        self.job_ids.append(job_id)


@pytest.fixture
def engine() -> Iterator[Engine]:
    if TEST_DATABASE_URL.startswith("sqlite"):
        # StaticPool shares one in-memory database across threads (TestClient uses a thread).
        test_engine = create_engine(
            TEST_DATABASE_URL,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        test_engine = create_engine(TEST_DATABASE_URL)

    Base.metadata.create_all(test_engine)
    yield test_engine
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def client(
    session_factory: sessionmaker[Session], storage: LocalStorage, queue: FakeQueue
) -> Iterator[TestClient]:
    def override_get_db() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_enqueuer] = lambda: queue
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def job_payload(recipient_count: int = 2, **overrides: object) -> dict:
    """A valid POST /jobs body with `recipient_count` distinct recipients."""
    payload = {
        "event_name": "Python Workshop 2026",
        "certificate_date": "2026-10-07",
        "recipients": [
            {
                "name": f"Recipient {index}",
                "email": f"recipient{index}@example.com",
                "course": "Python Workshop",
            }
            for index in range(recipient_count)
        ],
    }
    payload.update(overrides)
    return payload
