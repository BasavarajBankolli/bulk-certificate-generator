"""The single, predefined certificate template.

``render_certificate`` is a pure function: data in, PDF bytes out. It knows nothing
about the database or storage, which keeps it trivial to unit-test.
"""

import io
import uuid
from dataclasses import dataclass
from datetime import date

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)
CENTER_X = PAGE_WIDTH / 2
TEXT_MAX_WIDTH = PAGE_WIDTH - 160

NAVY = HexColor("#1F3A5F")
GOLD = HexColor("#B8860B")
GREY = HexColor("#555555")


@dataclass(frozen=True)
class CertificateData:
    certificate_id: uuid.UUID
    recipient_name: str
    course: str
    event_name: str
    certificate_date: date


def render_certificate(data: CertificateData) -> bytes:
    buffer = io.BytesIO()
    pdf = Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT))
    pdf.setTitle(f"Certificate of Completion - {data.recipient_name}")
    pdf.setSubject(data.course)

    _draw_border(pdf)
    _draw_heading(pdf)
    _draw_body(pdf, data)
    _draw_footer(pdf, data)

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _draw_border(pdf: Canvas) -> None:
    pdf.setStrokeColor(NAVY)
    pdf.setLineWidth(6)
    pdf.rect(20, 20, PAGE_WIDTH - 40, PAGE_HEIGHT - 40)
    pdf.setStrokeColor(GOLD)
    pdf.setLineWidth(1.5)
    pdf.rect(34, 34, PAGE_WIDTH - 68, PAGE_HEIGHT - 68)


def _draw_heading(pdf: Canvas) -> None:
    pdf.setFillColor(NAVY)
    pdf.setFont("Times-Bold", 44)
    pdf.drawCentredString(CENTER_X, PAGE_HEIGHT - 120, "CERTIFICATE")
    pdf.setFillColor(GOLD)
    pdf.setFont("Helvetica", 16)
    pdf.drawCentredString(CENTER_X, PAGE_HEIGHT - 148, "OF COMPLETION")
    pdf.setStrokeColor(GOLD)
    pdf.setLineWidth(1)
    pdf.line(CENTER_X - 120, PAGE_HEIGHT - 162, CENTER_X + 120, PAGE_HEIGHT - 162)


def _draw_body(pdf: Canvas, data: CertificateData) -> None:
    # Text flows downwards from `y`; each block returns where the next one starts,
    # so long names or courses push the following lines down instead of overlapping.
    y = PAGE_HEIGHT - 205
    pdf.setFillColor(GREY)
    pdf.setFont("Times-Italic", 16)
    pdf.drawCentredString(CENTER_X, y, "This is to certify that")

    pdf.setFillColor(NAVY)
    y = _draw_fitted_text(pdf, data.recipient_name, "Times-BoldItalic", 40, 20, y - 52)
    pdf.setStrokeColor(GOLD)
    pdf.setLineWidth(1)
    pdf.line(CENTER_X - 220, y - 12, CENTER_X + 220, y - 12)

    pdf.setFillColor(GREY)
    pdf.setFont("Times-Italic", 16)
    pdf.drawCentredString(CENTER_X, y - 42, "has successfully completed")

    pdf.setFillColor(NAVY)
    y = _draw_fitted_text(pdf, data.course, "Helvetica-Bold", 22, 14, y - 80)

    pdf.setFillColor(GREY)
    _draw_fitted_text(pdf, f"at {data.event_name}", "Helvetica", 14, 11, y - 28)


def _draw_footer(pdf: Canvas, data: CertificateData) -> None:
    line_y = 110
    pdf.setStrokeColor(NAVY)
    pdf.setLineWidth(1)
    for x in (CENTER_X - 230, CENTER_X + 70):
        pdf.line(x, line_y, x + 160, line_y)

    pdf.setFillColor(NAVY)
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawCentredString(CENTER_X - 150, line_y + 8, data.certificate_date.strftime("%d %B %Y"))
    pdf.setFillColor(GREY)
    pdf.setFont("Helvetica", 10)
    pdf.drawCentredString(CENTER_X - 150, line_y - 14, "Date")
    pdf.drawCentredString(CENTER_X + 150, line_y - 14, "Authorized Signature")

    pdf.setFont("Helvetica", 8)
    pdf.drawCentredString(CENTER_X, 48, f"Certificate ID: {data.certificate_id}")


def _draw_fitted_text(
    pdf: Canvas, text: str, font: str, max_size: int, min_size: int, y: float
) -> float:
    """Draw centred text, shrinking the font to fit one line, wrapping only if needed.

    Returns the baseline y of the last line drawn.
    """
    size = max_size
    while size > min_size and stringWidth(text, font, size) > TEXT_MAX_WIDTH:
        size -= 1

    lines = simpleSplit(text, font, size, TEXT_MAX_WIDTH)
    pdf.setFont(font, size)
    for index, line in enumerate(lines):
        if index:
            y -= size * 1.2
        pdf.drawCentredString(CENTER_X, y, line)
    return y
