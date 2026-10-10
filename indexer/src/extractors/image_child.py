"""Image decode and OCR, run in the extractor child (#1292).

``image.extract`` runs it as
``python -I extractor_child.py image <max pages> <OCR timeout> <payload file>``,
through the runner's launcher (``_launcher.py``), which has already
lowered the address-space and CPU limits ``image.py`` passes. Tesseract,
which pytesseract starts here, inherits both limits and is killed with
the child's process group when the run ends; its temporary files go to
the run's scratch directory (``TMPDIR``), which the runner removes.

Tesseract via ``pytesseract``. The dispatcher only routes here when
OCR is enabled, so this module assumes Tesseract is available — a
missing binary surfaces as ``TesseractNotFoundError``, which the
parent records as a ``failed`` extraction row.

A frame in a mode Pillow cannot write as PNG (CMYK, YCbCr, ...), which
``pytesseract`` needs, is converted to RGB per frame (#1400).

Auto-rotates EXIF-oriented JPEGs (smartphone photos default to
landscape EXIF metadata even when shot portrait, and unrotated input
hurts OCR accuracy materially). A multipage TIFF (a scanned invoice or
fax) is OCR'd page by page, up to ``max_ocr_pages``, like a scanned PDF;
other formats' extra frames are animation, not pages, and only the
first is read. When OCR stops at the cap, one probe seek finds whether
a frame was left unread; if so, the cap is reported by name and the
parent logs and counts it (#885). Anything else — language hints,
preprocessing — is left as a future tuning concern.

Decompression-bomb defense: ``INDEXER_ATTACHMENT_MAX_BYTES`` caps the
payload on disk, but PNG / WebP / TIFF can deflate ~1000× into a
multi-gigapixel canvas. The pixel-count cap lives at process scope in
``indexer.extractors.__init__`` (``GLOBAL_MAX_IMAGE_PIXELS``), which the
child imports, so it applies here as in the indexer. This module
promotes the milder ``DecompressionBombWarning`` to an error inside
``extract_text()`` so a between-cap-and-2x-cap image surfaces as a
``failed`` extraction row rather than passing through.

HEIC / HEIF (iPhone photos) open through pillow-heif's Pillow plugin
(#691), registered below at import, so they pass the same byte cap,
pixel cap and bomb handling as every other format: Pillow checks the
size from the header before libheif decodes anything. Only the primary
image is read; thumbnails, depth maps and auxiliary images are not
decoded. pillow-heif bundles its own libheif and libde265, which Debian
security updates do not cover (owner accepted, 2026-10-04).

An error is reported by type name only (``extractor_child``), since its
message can quote the image's metadata.
"""

from __future__ import annotations

import io
import warnings
from collections.abc import Callable

import pillow_heif
import pytesseract
from PIL import Image, ImageOps

from .image import _MAX_TEXT_CHARS, CAP_FRAMES, CAP_FRAMES_UNREADABLE, CAP_TEXT

# HEIF opener only (pillow-heif 1.x has no AVIF plugin); skip decoding a
# photo's thumbnails, depth maps and auxiliary images, which OCR never
# reads.
pillow_heif.register_heif_opener(thumbnails=False, depth_images=False, aux_images=False)

_SEPARATOR = "\n\n"

# The modes ``pytesseract`` can pass on: it saves each frame as PNG
# before running Tesseract, after pasting any alpha channel onto white
# (``RGBA``, ``LA``, ``PA``). Any other mode Pillow cannot write as PNG
# (CMYK, YCbCr, HSV, F, the premultiplied and padded RGB/L modes) raises
# ``OSError`` on that save (#1400). ``LAB`` is left out: Pillow cannot
# write it either, and pytesseract would take its "A" band for alpha and
# paste through it. ``tests/test_image_child.py`` checks this set against
# Pillow's own save for each mode.
_PNG_MODES = frozenset({"1", "L", "P", "PA", "LA", "RGB", "RGBA", "I", "I;16", "I;16B"})


def extract_text(
    payload: bytes,
    max_ocr_pages: str,
    ocr_timeout_seconds: str,
    tesseract_cmd: str | None = None,
    *,
    on_progress: Callable[[], None] | None = None,
) -> tuple[str, list[str]]:
    """OCR an image payload. Returns the text and the names of the caps
    that cut it.

    ``max_ocr_pages``, ``ocr_timeout_seconds`` and ``tesseract_cmd``
    arrive as the child's arguments: the TIFF page cap (``0``: no cap),
    the per-page Tesseract timeout in seconds (``0``: none), and the
    Tesseract the indexer found on its own ``PATH``, since the child runs
    with none. Tesseract is single-process and a crafted high-noise
    image can keep it busy for minutes; ``pytesseract`` raises
    ``RuntimeError`` when the timeout fires, recorded ``failed`` by the
    parent.

    ``on_progress`` (when set) is called after each page is OCR'd.
    """
    page_cap = int(max_ocr_pages)
    timeout = float(ocr_timeout_seconds)
    if tesseract_cmd is not None:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    with warnings.catch_warnings():
        # Promote the bomb warning to an error so anything between the
        # global pixel cap and PIL's hard 2x ceiling becomes a clean
        # ``failed`` extraction. Scoped via ``catch_warnings`` so the
        # filter doesn't leak across unrelated callers in the same
        # process.
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        image: Image.Image = Image.open(io.BytesIO(payload))
        tesseract_kwargs: dict[str, float] = {}
        if timeout > 0:
            tesseract_kwargs["timeout"] = timeout
        texts: list[str] = []
        stripped = _StrippedLength()
        page = 0
        while True:
            # ``exif_transpose`` reads the EXIF Orientation tag and rotates
            # the pixels accordingly. No-op for images without EXIF.
            text = pytesseract.image_to_string(
                _png_ready(ImageOps.exif_transpose(image)), **tesseract_kwargs
            )
            if on_progress is not None:
                on_progress()
            if texts:
                stripped.add(_SEPARATOR)
            stripped.add(text)
            texts.append(text)
            page += 1
            if stripped.length > _MAX_TEXT_CHARS:
                return _joined(texts)[:_MAX_TEXT_CHARS], [CAP_TEXT]
            if image.format != "TIFF":
                break
            if page_cap > 0 and page >= page_cap:
                cap = _probe_past_cap(image, page)
                return _joined(texts), [] if cap is None else [cap]
            # Seek page by page rather than read ``n_frames``: that walks
            # every image directory in the file before any cap applies.
            try:
                image.seek(page)
            except EOFError:
                break
    return _joined(texts), []


def _png_ready(frame: Image.Image) -> Image.Image:
    """The frame as ``pytesseract`` can save it: unchanged when Pillow
    writes its mode as PNG, else converted to RGB (#1400). The frame is
    already decoded under the pixel cap and the child's memory limit."""
    return frame if frame.mode in _PNG_MODES else frame.convert("RGB")


def _joined(texts: list[str]) -> str:
    """The pages' text as the indexer stores it: joined, then stripped,
    as the dispatcher strips every result. Stripping before the budget
    cuts keeps the result equal to the in-process extraction's whenever
    the stripped text fits the budget (owner decision 2026-10-08, #1325)."""
    return _SEPARATOR.join(texts).strip()


class _StrippedLength:
    """The length the joined text so far has once stripped, kept as text
    arrives at the cost of one scan of each piece: from the first
    non-whitespace character to the end of the last. Text past the
    budget in it is past the budget in the final stripped text too,
    whatever later pages add."""

    def __init__(self) -> None:
        self._offset = 0
        self._first: int | None = None
        self._end = 0

    def add(self, text: str) -> None:
        body = text.strip()
        if body:
            if self._first is None:
                self._first = self._offset + len(text) - len(text.lstrip())
            self._end = self._offset + len(text.rstrip())
        self._offset += len(text)

    @property
    def length(self) -> int:
        return 0 if self._first is None else self._end - self._first


def _probe_past_cap(image: Image.Image, pages_read: int) -> str | None:
    """The cap to report when OCR stopped at the cap with a frame left
    unread (#885), else ``None``.

    One seek past the cap is the bound: counting every frame would walk
    the whole frame chain, which the loop above avoids. ``EOFError``
    means the cap read every frame. Any other error (a corrupt frame
    directory) cannot change the result, which is the frames already
    read, so the image is reported capped with its next frame
    unreadable. ``MemoryError`` and ``RecursionError`` propagate: the
    child reports them by type, as the child's own limit."""
    try:
        image.seek(pages_read)
    except EOFError:
        return None
    except MemoryError, RecursionError:
        raise
    except Exception:  # noqa: BLE001 — reported as a fixed cap name
        return CAP_FRAMES_UNREADABLE
    return CAP_FRAMES
