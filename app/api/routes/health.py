from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.common import ErrorResponse, HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": ErrorResponse, "description": "Database unavailable"}},
)
def health(db: Session = Depends(get_db)) -> HealthResponse:
    # A failing query raises OperationalError, which the global handler maps to 503.
    db.execute(text("SELECT 1"))
    return HealthResponse(status="ok", database="ok")
