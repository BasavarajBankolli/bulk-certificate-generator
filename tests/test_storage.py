import uuid
from pathlib import Path

import pytest

from app.core.exceptions import StorageError
from app.services.storage import LocalStorage, certificate_key


def test_certificate_key_is_deterministic_and_uuid_based() -> None:
    job_id, certificate_id = uuid.uuid4(), uuid.uuid4()

    key = certificate_key(job_id, certificate_id)

    assert key == f"certificates/{job_id}/{certificate_id}.pdf"
    assert key == certificate_key(job_id, certificate_id)


def test_save_then_open_round_trips_bytes(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = certificate_key(uuid.uuid4(), uuid.uuid4())

    storage.save(key, b"%PDF-1.4 test")

    assert storage.exists(key)
    with storage.open(key) as file:
        assert file.read() == b"%PDF-1.4 test"


def test_save_overwrites_existing_file_and_leaves_no_temp_files(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    key = certificate_key(uuid.uuid4(), uuid.uuid4())

    storage.save(key, b"first")
    storage.save(key, b"second")

    with storage.open(key) as file:
        assert file.read() == b"second"
    assert not list(tmp_path.rglob("*.tmp"))


def test_missing_key_does_not_exist_and_cannot_be_opened(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)

    assert not storage.exists("certificates/missing.pdf")
    with pytest.raises(StorageError):
        storage.open("certificates/missing.pdf")


def test_keys_escaping_the_storage_directory_are_rejected(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "storage")

    with pytest.raises(StorageError):
        storage.save("../outside.pdf", b"data")
    assert not (tmp_path / "outside.pdf").exists()


def test_write_failure_raises_storage_error(tmp_path: Path) -> None:
    # A regular file where a directory is expected makes mkdir fail.
    (tmp_path / "certificates").write_text("not a directory")
    storage = LocalStorage(tmp_path)

    with pytest.raises(StorageError, match="Unable to store certificate"):
        storage.save(certificate_key(uuid.uuid4(), uuid.uuid4()), b"data")
