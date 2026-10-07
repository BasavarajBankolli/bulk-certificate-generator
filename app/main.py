from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging

API_PREFIX = "/api/v1"

DESCRIPTION = """
Generate PDF certificates for many recipients in the background.

1. `POST /api/v1/jobs` with a list of recipients - returns immediately with a `job_id`.
2. Poll `GET /api/v1/jobs/{job_id}` until the job reaches a terminal status.
3. List certificates with `GET /api/v1/jobs/{job_id}/certificates` and download each PDF.
"""


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name, version="1.0.0", description=DESCRIPTION)
    register_exception_handlers(app)
    app.include_router(api_router, prefix=API_PREFIX)
    return app


app = create_app()
