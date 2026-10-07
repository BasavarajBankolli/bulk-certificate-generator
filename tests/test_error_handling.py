"""Internal errors must be logged but never leak stack traces to API clients."""

import uuid
from collections.abc import Iterator

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.deps import get_job_service
from app.db.session import get_db
from app.main import app


def test_database_unavailable_returns_503(client: TestClient) -> None:
    def broken_db() -> Iterator[Session]:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        yield  # pragma: no cover - makes this a generator dependency

    app.dependency_overrides[get_db] = broken_db

    response = client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "Service temporarily unavailable"}


def test_unexpected_error_returns_generic_500() -> None:
    class ExplodingService:
        def get_job(self, job_id: uuid.UUID) -> None:
            raise RuntimeError("secret internal detail")

    app.dependency_overrides[get_job_service] = ExplodingService
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(f"/api/v1/jobs/{uuid.uuid4()}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "secret" not in response.text
