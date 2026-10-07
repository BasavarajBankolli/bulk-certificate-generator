"""FastAPI dependencies.

Routes receive services through these functions, so tests can swap the database,
storage or queue with ``app.dependency_overrides`` without touching route code.
"""

from fastapi import Depends
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.services.job_service import Enqueuer, JobService
from app.services.storage import StorageBackend, create_storage
from app.workers.celery_app import enqueue_process_job


def get_storage() -> StorageBackend:
    return create_storage(get_settings())


def get_enqueuer() -> Enqueuer:
    return enqueue_process_job


def get_job_service(
    db: Session = Depends(get_db), enqueue: Enqueuer = Depends(get_enqueuer)
) -> JobService:
    return JobService(db, enqueue)
