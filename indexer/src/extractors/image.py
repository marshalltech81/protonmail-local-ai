"""Image OCR extractor (PNG / JPG / TIFF / etc).

Tesseract via ``pytesseract``. The dispatcher only routes here when
OCR is enabled, so this module assumes Tesseract is available — a
missing binary surfaces as a runtime ``TesseractNotFoundError`` and
the dispatcher converts it to a ``failed`` extraction row.

Auto-rotates EXIF-oriented JPEGs (smartphone photos default to
landscape EXIF metadata even when shot portrait, and unrotated input
hurts OCR accuracy materially). Anything else — language hints,
preprocessing — is left as a future tuning concern.
"""

from __future__ import annotations

import io

import pytesseract
from PIL import Image, ImageOps


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001 — dispatcher already gated on this
    max_ocr_pages: int = 20,  # noqa: ARG001 — single-page format
) -> tuple[str, str]:
    """OCR an image attachment. Returns (text, "image-ocr")."""
    image: Image.Image = Image.open(io.BytesIO(payload))
    # ``exif_transpose`` reads the EXIF Orientation tag and rotates the
    # pixels accordingly. No-op for images without EXIF.
    image = ImageOps.exif_transpose(image)
    text = pytesseract.image_to_string(image)
    return text, "image-ocr"
