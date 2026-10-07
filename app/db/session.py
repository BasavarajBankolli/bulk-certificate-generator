from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# pool_pre_ping transparently replaces connections dropped by the database.
engine = create_engine(get_settings().database_url, pool_pre_ping=True)

# expire_on_commit=False lets us keep reading attributes (e.g. job.id) after commit
# without an extra SELECT; we commit often in the worker.
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed afterwards."""
    with SessionLocal() as session:
        yield session
