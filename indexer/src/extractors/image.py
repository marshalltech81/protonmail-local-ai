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

Both safeguards are scoped to ``extract()`` rather than module-import
time so other PIL consumers in the same process keep PIL's defaults
and the warnings filter doesn't leak across unrelated callers.
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
# filter inside ``extract()`` promotes the milder
# ``DecompressionBombWarning`` (between 1× and 2×) to the same error so
# the dispatcher records both as ``failed`` rather than letting them
# through with a log line.
_MAX_IMAGE_PIXELS = 50_000_000


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001 — dispatcher already gated on this
    max_ocr_pages: int = 20,  # noqa: ARG001 — single-page format
    ocr_timeout_seconds: float | None = None,
    max_pdf_pages: int | None = None,  # noqa: ARG001 — single-page format
) -> tuple[str, str]:
    """OCR an image attachment. Returns (text, "image-ocr").

    ``ocr_timeout_seconds`` (when set) bounds Tesseract per call.
    Tesseract is single-threaded and a crafted high-noise image can
    keep it busy for minutes; the indexer queue is single-worker, so
    one bad image stalls every subsequent attachment until the OS
    reaps Tesseract. ``pytesseract`` raises ``RuntimeError`` when the
    timeout fires; the dispatcher converts that to a ``failed``
    extraction row.
    """
    prior_max_pixels = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS
    try:
        with warnings.catch_warnings():
            # Scope the bomb-warning promotion to this call so other
            # callers in the same process aren't forced into the same
            # filter state.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image: Image.Image = Image.open(io.BytesIO(payload))
            # ``exif_transpose`` reads the EXIF Orientation tag and rotates the
            # pixels accordingly. No-op for images without EXIF.
            image = ImageOps.exif_transpose(image)
            tesseract_kwargs: dict[str, float] = {}
            if ocr_timeout_seconds is not None and ocr_timeout_seconds > 0:
                tesseract_kwargs["timeout"] = float(ocr_timeout_seconds)
            text = pytesseract.image_to_string(image, **tesseract_kwargs)
    finally:
        Image.MAX_IMAGE_PIXELS = prior_max_pixels
    return text, "image-ocr"
