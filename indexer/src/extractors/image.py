"""Image OCR extractor (PNG / JPG / TIFF / etc).

Tesseract via ``pytesseract``. The dispatcher only routes here when
OCR is enabled, so this module assumes Tesseract is available — a
missing binary surfaces as a runtime ``TesseractNotFoundError`` and
the dispatcher converts it to a ``failed`` extraction row.

Auto-rotates EXIF-oriented JPEGs (smartphone photos default to
landscape EXIF metadata even when shot portrait, and unrotated input
hurts OCR accuracy materially). A multipage TIFF (a scanned invoice or
fax) is OCR'd page by page, up to ``max_ocr_pages``, like a scanned PDF;
other formats' extra frames are animation, not pages, and only the
first is read. When OCR stops at the cap, one probe seek finds whether
a frame was left unread; if so, a rate-limited WARNING says so and the
image is counted for the attachments aggregate (#885). Anything else — language hints, preprocessing — is left
as a future tuning concern.

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

HEIC / HEIF (iPhone photos) open through pillow-heif's Pillow plugin
(#691), registered below at import, so they pass the same byte cap,
pixel cap and bomb handling as every other format: Pillow checks the
size from the header before libheif decodes anything. Only the primary
image is read; thumbnails, depth maps and auxiliary images are not
decoded. pillow-heif bundles its own libheif and libde265, which Debian
security updates do not cover (owner accepted, 2026-10-04).
"""

from __future__ import annotations

import io
import logging
import warnings
from collections.abc import Callable

import pillow_heif
import pytesseract
from PIL import Image, ImageOps

from . import note_ocr_capped_image, warn_rate_limited

log = logging.getLogger("indexer.extractor.image")

# HEIF opener only (pillow-heif 1.x has no AVIF plugin); skip decoding a
# photo's thumbnails, depth maps and auxiliary images, which OCR never
# reads.
pillow_heif.register_heif_opener(thumbnails=False, depth_images=False, aux_images=False)


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001 — dispatcher already gated on this
    max_ocr_pages: int = 20,
    ocr_timeout_seconds: float | None = None,
    max_pdf_pages: int | None = None,  # noqa: ARG001 — single-page format
    on_progress: Callable[[], None] | None = None,
) -> tuple[str, str]:
    """OCR an image attachment. Returns (text, "image-ocr").

    ``ocr_timeout_seconds`` (when set) bounds Tesseract per call, so a
    multipage TIFF costs at most ``max_ocr_pages`` of them.
    Tesseract is single-threaded and a crafted high-noise image can
    keep it busy for minutes; the indexer queue is single-worker, so
    one bad image stalls every subsequent attachment until the OS
    reaps Tesseract. ``pytesseract`` raises ``RuntimeError`` when the
    timeout fires; the dispatcher converts that to a ``failed``
    extraction row.

    ``on_progress`` (when set) is called after each page is OCR'd.
    """
    with warnings.catch_warnings():
        # Promote the bomb warning to an error so anything between the
        # global pixel cap and PIL's hard 2x ceiling becomes a clean
        # ``failed`` extraction. Scoped via ``catch_warnings`` so the
        # filter doesn't leak across unrelated callers in the same
        # process.
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        image: Image.Image = Image.open(io.BytesIO(payload))
        tesseract_kwargs: dict[str, float] = {}
        if ocr_timeout_seconds is not None and ocr_timeout_seconds > 0:
            tesseract_kwargs["timeout"] = float(ocr_timeout_seconds)
        texts: list[str] = []
        page = 0
        while True:
            # ``exif_transpose`` reads the EXIF Orientation tag and rotates
            # the pixels accordingly. No-op for images without EXIF.
            texts.append(
                pytesseract.image_to_string(ImageOps.exif_transpose(image), **tesseract_kwargs)
            )
            if on_progress is not None:
                on_progress()
            page += 1
            if image.format != "TIFF":
                break
            if max_ocr_pages > 0 and page >= max_ocr_pages:
                _note_if_capped(image, page)
                break
            # Seek page by page rather than read ``n_frames``: that walks
            # every image directory in the file before any cap applies.
            try:
                image.seek(page)
            except EOFError:
                break
    return "\n\n".join(texts), "image-ocr"


def _note_if_capped(image: Image.Image, pages_read: int) -> None:
    """Log and count a TIFF whose OCR stopped at the cap with a frame
    left unread (#885).

    One seek past the cap is the bound: counting every frame would walk
    the whole frame chain, which the loop above avoids. ``EOFError``
    means the cap read every frame. Any other error (a corrupt frame
    directory) cannot change the result, which is the frames already
    read, so it is logged by type and the image counted as capped.
    ``MemoryError`` and ``RecursionError`` propagate: the dispatcher
    treats them as host pressure."""
    try:
        image.seek(pages_read)
    except EOFError:
        return
    except MemoryError, RecursionError:
        raise
    except Exception as e:
        note_ocr_capped_image()
        warn_rate_limited(
            log,
            "image OCR capped at %d frames; the next frame could not be read (%s)",
            pages_read,
            type(e).__name__,
        )
        return
    note_ocr_capped_image()
    warn_rate_limited(
        log, "image OCR capped at %d of at least %d frames", pages_read, pages_read + 1
    )
