from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status

from app.api.deps import get_job_service
from app.schemas.common import ErrorResponse
from app.schemas.job import JobCreate, JobCreatedResponse
from app.services.job_service import JobService

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post(
    "",
    response_model=JobCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a certificate generation job",
    responses={
        200: {"model": JobCreatedResponse, "description": "Idempotent replay of an earlier job"},
        409: {"model": ErrorResponse, "description": "Idempotency-Key reused with another body"},
        503: {"model": ErrorResponse, "description": "Job queue unavailable"},
    },
)
def create_job(
    payload: JobCreate,
    response: Response,
    service: JobService = Depends(get_job_service),
    idempotency_key: Annotated[
        str | None,
        Header(
            min_length=1,
            max_length=255,
            description="Optional. Retrying with the same key returns the original job.",
        ),
    ] = None,
) -> JobCreatedResponse:
    """Validate recipients, store the job and queue it. Returns without waiting for PDFs."""
    job, created = service.create_job(payload, idempotency_key)

    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    if not created:
        response.status_code = status.HTTP_200_OK

    return JobCreatedResponse(job_id=job.id, status=job.status, total=job.total_count)
