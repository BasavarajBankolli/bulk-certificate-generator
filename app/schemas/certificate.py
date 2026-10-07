import uuid
from datetime import datetime

from pydantic import BaseModel

from app.db.models import Certificate, CertificateStatus

DOWNLOAD_URL_TEMPLATE = "/api/v1/certificates/{certificate_id}/download"


class CertificateResponse(BaseModel):
    certificate_id: uuid.UUID
    job_id: uuid.UUID
    recipient_name: str
    recipient_email: str
    course: str
    status: CertificateStatus
    error_message: str | None
    download_url: str | None
    created_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_certificate(cls, certificate: Certificate) -> "CertificateResponse":
        is_downloadable = certificate.status == CertificateStatus.SUCCESS
        return cls(
            certificate_id=certificate.id,
            job_id=certificate.job_id,
            recipient_name=certificate.recipient_name,
            recipient_email=certificate.recipient_email,
            course=certificate.course,
            status=CertificateStatus(certificate.status),
            error_message=certificate.error_message,
            download_url=(
                DOWNLOAD_URL_TEMPLATE.format(certificate_id=certificate.id)
                if is_downloadable
                else None
            ),
            created_at=certificate.created_at,
            completed_at=certificate.completed_at,
        )


class CertificateListResponse(BaseModel):
    items: list[CertificateResponse]
    total: int
    limit: int
    offset: int
