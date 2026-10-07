import io
import uuid
from datetime import date

from pypdf import PdfReader

from app.templates.certificate import CertificateData, render_certificate


def _data(**overrides: object) -> CertificateData:
    values = {
        "certificate_id": uuid.uuid4(),
        "recipient_name": "Alice Johnson",
        "course": "Python Workshop",
        "event_name": "PyCon India 2026",
        "certificate_date": date(2026, 10, 7),
    }
    values.update(overrides)
    return CertificateData(**values)


def _extract_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() for page in reader.pages)


def test_render_returns_single_page_pdf() -> None:
    pdf_bytes = render_certificate(_data())

    assert pdf_bytes.startswith(b"%PDF-")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 1
    # A4 landscape: width > height.
    page = reader.pages[0]
    assert float(page.mediabox.width) > float(page.mediabox.height)


def test_pdf_contains_recipient_details() -> None:
    data = _data()

    text = _extract_text(render_certificate(data))

    assert "Alice Johnson" in text
    assert "Python Workshop" in text
    assert "PyCon India 2026" in text
    assert "07 October 2026" in text
    assert str(data.certificate_id) in text


def test_long_recipient_name_is_rendered_without_losing_text() -> None:
    long_name = " ".join(["Maximilian Alexander Bartholomew"] * 6)  # ~190 characters

    text = _extract_text(render_certificate(_data(recipient_name=long_name)))

    # The name may be wrapped across lines, but every word must still be present.
    assert " ".join(text.split()).count("Bartholomew") == 6


def test_accented_latin_characters_are_supported() -> None:
    text = _extract_text(render_certificate(_data(recipient_name="José Müller")))

    assert "José Müller" in text
