"""Certificate file storage.

Business logic only sees ``StorageBackend`` and string *keys*. Today the keys map to
files under a local directory; an ``S3Storage`` implementing the same three methods
could replace ``LocalStorage`` by changing only ``create_storage``.
"""

import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO, Protocol

from app.core.config import Settings
from app.core.exceptions import StorageError

logger = logging.getLogger(__name__)


def certificate_key(job_id: uuid.UUID, certificate_id: uuid.UUID) -> str:
    """Deterministic key built only from UUIDs, never from user input.

    Re-generating a certificate overwrites the same key instead of creating a duplicate.
    """
    return f"certificates/{job_id}/{certificate_id}.pdf"


class StorageBackend(Protocol):
    def save(self, key: str, data: bytes) -> None: ...

    def open(self, key: str) -> BinaryIO: ...

    def exists(self, key: str) -> bool: ...


class LocalStorage:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir.resolve()

    def save(self, key: str, data: bytes) -> None:
        path = self._path_for(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write: write to a temp file in the same directory, then rename.
            # Readers never see a half-written PDF, even if the worker crashes mid-write.
            fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as tmp_file:
                    tmp_file.write(data)
                os.replace(tmp_name, path)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            logger.exception("Failed to write storage key=%s", key)
            raise StorageError("Unable to store certificate") from exc

    def open(self, key: str) -> BinaryIO:
        try:
            return self._path_for(key).open("rb")
        except OSError as exc:
            logger.exception("Failed to open storage key=%s", key)
            raise StorageError("Certificate file is unavailable") from exc

    def exists(self, key: str) -> bool:
        return self._path_for(key).is_file()

    def _path_for(self, key: str) -> Path:
        # Defence in depth: keys are built from UUIDs, but refuse anything that
        # would resolve outside the storage directory (e.g. "../../etc/passwd").
        path = (self._base_dir / key).resolve()
        if not path.is_relative_to(self._base_dir):
            raise StorageError(f"Invalid storage key: {key!r}")
        return path


def create_storage(settings: Settings) -> StorageBackend:
    return LocalStorage(settings.storage_dir)
