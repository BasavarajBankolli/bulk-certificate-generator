import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from app.core.exceptions import AppError

logger = logging.getLogger(__name__)


async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    if exc.status_code >= 500:
        logger.error("%s %s failed: %s", request.method, request.url.path, exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


async def handle_database_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
    logger.exception("Database error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=503, content={"detail": "Service temporarily unavailable"})


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # Full traceback goes to the logs; the client only sees a generic message.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(OperationalError, handle_database_unavailable)
    app.add_exception_handler(Exception, handle_unexpected_error)
