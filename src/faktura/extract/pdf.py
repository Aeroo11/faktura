"""PDF page rendering for vision models."""

from __future__ import annotations

import io
from pathlib import Path

import pypdfium2 as pdfium

# ~170 DPI with the longest side capped: enough for small print, cheap in tokens.
DEFAULT_DPI = 170
MAX_SIDE_PX = 2200


def render_first_page_png(path: Path, dpi: int = DEFAULT_DPI) -> bytes:
    pdf = pdfium.PdfDocument(path)
    try:
        image = pdf[0].render(scale=dpi / 72).to_pil()
    finally:
        pdf.close()
    if max(image.size) > MAX_SIDE_PX:
        image.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX))
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
