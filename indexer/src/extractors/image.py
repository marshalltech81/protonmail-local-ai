"""Image OCR extractor (PNG / JPG / TIFF / etc).

Tesseract via ``pytesseract``. The dispatcher only routes here when
OCR is enabled, so this module assumes Tesseract is available — a
missing binary surfaces as a runtime ``TesseractNotFoundError`` and
the dispatcher converts it to a ``failed`` extraction row.

Auto-rotates EXIF-oriented JPEGs (smartphone photos default to
landscape EXIF metadata even when shot portrait, and unrotated input
hurts OCR accuracy materially). Anything else — language hints,
preprocessing — is left as a future tuning concern.

Decompression-bomb defense: ``INDEXER_ATTACHMENT_MAX_BYTES`` only caps
the payload on disk. PNG / WebP / TIFF can deflate ~1000× into a
multi-gigapixel canvas that would OOM the indexer container during
``Image.open`` or OCR. PIL's ``MAX_IMAGE_PIXELS`` is the documented
defense — we lower it from PIL's default (~89 Mpx) to 50 Mpx and
promote the warning to an error so an oversize image surfaces as a
``failed`` extraction row rather than wedging the worker.
"""

from __future__ import annotations

import io
import warnings

import pytesseract
from PIL import Image, ImageOps

# 50 Mpx covers any realistic scanned-page or smartphone photo (a 12 Mpx
# phone shot is ~12,000,000 pixels) while keeping memory bounded —
# decoding a 50 Mpx RGB image is ~150 MB of pixel buffer at most. PIL
# raises ``DecompressionBombError`` past 2× this limit; the warning
# filter below promotes the milder ``DecompressionBombWarning`` (between
# 1× and 2×) to the same error so the dispatcher records both as
# ``failed`` rather than letting them through with a log line.
Image.MAX_IMAGE_PIXELS = 50_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)


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
