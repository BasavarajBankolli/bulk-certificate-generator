import uuid

from fastapi import APIRouter, Depends, Response

from app.api.deps import get_certificate_service
from app.schemas.certificate import CertificateResponse
from app.schemas.common import ErrorResponse
from app.services.certificate_service import CertificateService

router = APIRouter(prefix="/certificates", tags=["certificates"])


@router.get(
    "/{certificate_id}",
    response_model=CertificateResponse,
    summary="Get certificate metadata",
    responses={404: {"model": ErrorResponse, "description": "Certificate not found"}},
)
def get_certificate(
    certificate_id: uuid.UUID, service: CertificateService = Depends(get_certificate_service)
) -> CertificateResponse:
    return CertificateResponse.from_certificate(service.get_certificate(certificate_id))


@router.get(
    "/{certificate_id}/download",
    summary="Download the certificate PDF",
    response_class=Response,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "The certificate PDF"},
        404: {"model": ErrorResponse, "description": "Certificate not found"},
        409: {"model": ErrorResponse, "description": "Certificate pending or failed"},
    },
)
def download_certificate(
    certificate_id: uuid.UUID, service: CertificateService = Depends(get_certificate_service)
) -> Response:
    certificate, pdf_bytes = service.read_pdf(certificate_id)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="certificate-{certificate.id}.pdf"'},
    )
