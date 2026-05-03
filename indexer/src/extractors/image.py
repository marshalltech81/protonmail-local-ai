"""Image OCR extractor (PNG / JPG / TIFF / etc).

Tesseract via ``pytesseract``. The dispatcher only routes here when
OCR is enabled, so this module assumes Tesseract is available — a
missing binary surfaces as a runtime ``TesseractNotFoundError`` and
the dispatcher converts it to a ``failed`` extraction row.

Auto-rotates EXIF-oriented JPEGs (smartphone photos default to
landscape EXIF metadata even when shot portrait, and unrotated input
hurts OCR accuracy materially). Anything else — language hints,
preprocessing — is left as a future tuning concern.

Decompression-bomb defense: ``INDEXER_ATTACHMENT_MAX_BYTES`` caps the
payload on disk, but PNG / WebP / TIFF can deflate ~1000× into a
multi-gigapixel canvas that would OOM the indexer container at
``Image.open`` time. The pixel-count cap lives at process scope in
``indexer.extractors.__init__`` (``GLOBAL_MAX_IMAGE_PIXELS``) so it
applies uniformly to this extractor AND to transitive PIL consumers
(pypdf-rendered embedded images, etc.) without each module needing to
re-do the save/restore dance. This module promotes the milder
``DecompressionBombWarning`` to an error inside ``extract()`` so a
between-cap-and-2x-cap image surfaces as a ``failed`` extraction row
rather than passing through with a log line.
"""

from __future__ import annotations

import io
import warnings

import pytesseract
from PIL import Image, ImageOps


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
    with warnings.catch_warnings():
        # Promote the bomb warning to an error so anything between the
        # global pixel cap and PIL's hard 2x ceiling becomes a clean
        # ``failed`` extraction. Scoped via ``catch_warnings`` so the
        # filter doesn't leak across unrelated callers in the same
        # process.
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        image: Image.Image = Image.open(io.BytesIO(payload))
        # ``exif_transpose`` reads the EXIF Orientation tag and rotates the
        # pixels accordingly. No-op for images without EXIF.
        image = ImageOps.exif_transpose(image)
        tesseract_kwargs: dict[str, float] = {}
        if ocr_timeout_seconds is not None and ocr_timeout_seconds > 0:
            tesseract_kwargs["timeout"] = float(ocr_timeout_seconds)
        text = pytesseract.image_to_string(image, **tesseract_kwargs)
    return text, "image-ocr"
