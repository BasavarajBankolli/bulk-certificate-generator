"""Domain exceptions.

Services raise these; a single exception handler in ``app.api.errors`` turns them
into ``{"detail": ...}`` responses using ``status_code``. Keeping the status code
on the exception avoids a separate mapping table that could drift out of sync.
"""


class AppError(Exception):
    status_code: int = 500
    default_detail: str = "Internal server error"

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail or self.default_detail
        super().__init__(self.detail)


class NotFoundError(AppError):
    status_code = 404
    default_detail = "Resource not found"


class ConflictError(AppError):
    status_code = 409
    default_detail = "Request conflicts with the current state of the resource"


class ServiceUnavailableError(AppError):
    status_code = 503
    default_detail = "Service temporarily unavailable"


class JobNotFoundError(NotFoundError):
    default_detail = "Job not found"


class CertificateNotFoundError(NotFoundError):
    default_detail = "Certificate not found"


class CertificateUnavailableError(ConflictError):
    """The certificate exists but cannot be downloaded (still pending, or it failed)."""

    default_detail = "Certificate is not available for download"


class IdempotencyConflictError(ConflictError):
    default_detail = "Idempotency-Key has already been used with a different request"


class QueueUnavailableError(ServiceUnavailableError):
    default_detail = "Job queue is unavailable, please retry later"


class StorageError(AppError):
    default_detail = "Certificate storage error"
