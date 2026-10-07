import logging
import uuid

from sqlalchemy.orm import Session

from app.core.exceptions import (
    CertificateNotFoundError,
    CertificateUnavailableError,
    StorageError,
)
from app.db.models import Certificate, CertificateStatus
from app.services.storage import StorageBackend

logger = logging.getLogger(__name__)


class CertificateService:
    def __init__(self, db: Session, storage: StorageBackend) -> None:
        self._db = db
        self._storage = storage

    def get_certificate(self, certificate_id: uuid.UUID) -> Certificate:
        certificate = self._db.get(Certificate, certificate_id)
        if certificate is None:
            raise CertificateNotFoundError()
        return certificate

    def read_pdf(self, certificate_id: uuid.UUID) -> tuple[Certificate, bytes]:
        """Return the certificate and its PDF bytes, or explain why it can't be downloaded."""
        certificate = self.get_certificate(certificate_id)

        if certificate.status == CertificateStatus.FAILED:
            raise CertificateUnavailableError(
                f"Certificate generation failed: {certificate.error_message}"
            )
        if certificate.status != CertificateStatus.SUCCESS:
            raise CertificateUnavailableError(
                f"Certificate is not ready yet (status: {certificate.status})"
            )

        if not self._storage.exists(certificate.storage_key):
            # The database says SUCCESS but the file is gone: a server-side problem.
            logger.error(
                "certificate_id=%s file missing at key=%s", certificate.id, certificate.storage_key
            )
            raise StorageError("Certificate file is unavailable")

        # Certificates are a few KB, so reading into memory is simpler than streaming.
        with self._storage.open(certificate.storage_key) as file:
            return certificate, file.read()
