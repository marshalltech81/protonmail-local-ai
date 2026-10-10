"""Image OCR extractor (PNG / JPG / TIFF / HEIC / etc).

The decode (PIL, pillow-heif) and the OCR (Tesseract through
pytesseract) run in the extractor child (``extractor_child.py`` running
``image_child.py``), started through ``_runner.run_child`` (PLAN.md
decision 42, #1292). The launcher lowers the child's address space
(``RLIMIT_AS``) and CPU time (``RLIMIT_CPU``) before it starts; each
Tesseract the child starts inherits both limits (each process gets its
own), and the runner kills the child's whole process group when the run
ends and removes its scratch directory, where pytesseract writes its
temporary files. See ``image_child`` for the decode, the pixel cap and
the page loop.

The child's result crosses the pipe in the runner's framed protocol: a
``P`` frame per OCR'd page, passed to the dispatcher's ``on_progress``
so the heartbeat keeps firing through a long multipage TIFF (#485); a
``C`` frame per cap that cut the text, logged and counted here; and the
text. The frame cap that cut records ``INDEXER_OCR_MAX_PAGES`` on the
result (#1418); the text budget, a hardcoded limit, records nothing. An
error in the child (a decompression bomb, a Tesseract timeout
or failure, the child's own ``MemoryError`` or ``RecursionError`` at
its limit) is reported by type name and recorded ``failed`` by the
dispatcher, as is a limit hit, a timeout or output that breaks the
protocol.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable

from . import (
    CAP_OCR_PAGES,
    CHILD_DEGRADATION_KEYS,
    CHILD_IMAGE_SCALE_FACTOR,
    apply_child_degradation,
    note_ocr_capped_image,
    record_cap_cut,
    warn_extractor_cap,
    warn_rate_limited,
)
from ._runner import run_child

log = logging.getLogger("indexer.extractor.image")

# Characters of OCR text the child returns, separators included. A
# working image stays far below it (the indexer keeps at most
# ``INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS``, 2,000,000 by default); it
# bounds the child's output, which the parent reads whole (#1292).
_MAX_TEXT_CHARS = 10_000_000

# Cap names the child reports: OCR stopped at ``max_ocr_pages`` with a
# frame left unread, or with the next frame unreadable; the text budget
# above cut the text.
CAP_FRAMES = "ocr_frames"
CAP_FRAMES_UNREADABLE = "ocr_frames_unreadable"
CAP_TEXT = "image_text_chars"
# A multi-picture (MPO) JPEG whose header lists more than one picture:
# only the primary picture is OCR'd (#1401).
CAP_MPO_FRAMES = "mpo_frames"

# The child's own pixel ceiling (#1401, owner decision 2026-10-10): the
# most pixels a frame may have to be decoded and OCR'd at full
# resolution, 48,000,000 (an 8000 x 6000 phone photo). It is sized with
# ``CHILD_MAX_ADDRESS_SPACE_BYTES`` below. It applies in the child only;
# ``GLOBAL_MAX_IMAGE_PIXELS`` (30,000,000) stays the cap of every other
# PIL consumer.
CHILD_MAX_IMAGE_PIXELS = 48_000_000

# A JPEG or MPO over the ceiling is decoded at 1/2 scale through
# ``Image.draft`` when that fits (up to 192,000,000 source pixels). It is
# a LOSSY fallback, not a validated one. On a synthetic catalogue (one
# font rendered at 0.7 of the line height, one line spacing, lines 40 to
# 200 px high; clean, blurred, perspective, low-contrast and JPEG
# quality 50; on 48, 108 and 192 MP canvases; ``docs/architecture.md``),
# half scale kept 58 of the 62 cases read at full resolution to within
# 2 points, and read nothing of the other four: low-contrast lines of
# 60 px (48 MP) and 120 px (192 MP), and 40 px lines clean and
# compressed (108 MP); a sweep of ink contrast at 48 MP lost 2 of 12
# (low-contrast 50 and 60 px lines). The parent logs each scale-down at
# WARNING with the factor and marks the text incomplete. 1/4 (which lost
# every 40 px case at 48 MP) and 1/8 are excluded; an image a half-scale
# decode cannot fit, and any other format over the ceiling, is
# ``unsupported`` (``IMAGE_PIXEL_CEILING_ERROR``).
MAX_DRAFT_FACTOR = 2

# Address space each process in the child's tree may map
# (``RLIMIT_AS``): the child, and each Tesseract it starts, one at a
# time, so the tree holds at most two at once. Plainly measured in the
# indexer image (CPython 3.14, Tesseract 5.5.0 with up to four OpenMP
# threads, #1401), as the smallest limit under which the extraction
# still succeeds, on synthetic 48,000,000-pixel images: a JPEG of random
# noise needs 944 MiB (Tesseract); text pages need 688 MiB as JPEG
# (baseline, progressive, EXIF-rotated, MPO, CMYK) and HEIC, 672 MiB as
# PNG, LZW TIFF, a three-page TIFF and an all-white RGBA PNG (the
# child's decode), 464 MiB as a 32-bit TIFF and 336 MiB as a palette
# PNG. The half-scale fallback needs at most 864 MiB (a 192,000,000-pixel
# noise JPEG inside the 32 MiB byte cap; 1,008 MiB for one of 87 MiB,
# past the default cap). The limit, 1,605 MiB, is the smallest that
# gives the largest case inside the byte cap (944 MiB) a 1.7 times
# margin, the margin the 30,000,000-pixel limit was sized with.
CHILD_MAX_ADDRESS_SPACE_BYTES = 1605 * 1024 * 1024

# Tesseract runs up to four OpenMP threads, and CPU time counts every
# thread, so a page can use up to four times its wall-clock time in CPU.
# The CPU limit of each process is sized so the per-page OCR timeout
# fires first: four times the timeout, plus a margin. With no OCR
# timeout (``INDEXER_OCR_TIMEOUT_SECONDS=0``) the limit for the 60 s
# default applies.
_TESSERACT_THREADS = 4
_DEFAULT_OCR_TIMEOUT_SECONDS = 60.0
_CPU_MARGIN_SECONDS = 30

# Wall-clock seconds per page past its OCR timeout (the decode, the
# rotation and pytesseract's temporary file), and for the child's
# start-up.
_PAGE_MARGIN_SECONDS = 10.0
_START_MARGIN_SECONDS = 30.0

# Bytes of the child's output read: its text budget at UTF-8's worst
# case of four bytes a character, plus the frames (a progress frame per
# page and the cap names).
_MAX_OUTPUT_BYTES = 4 * _MAX_TEXT_CHARS + 1024 * 1024

# The cap names the child may report.
_CAPS = frozenset({CAP_FRAMES, CAP_FRAMES_UNREADABLE, CAP_TEXT, CAP_MPO_FRAMES})


class ImagePixelCeilingError(Exception):
    """The image is over the child's pixel ceiling and cannot be scaled
    down to fit it (#1401). The same bytes always repeat it, so the
    dispatcher records it ``unsupported``. Fixed text."""

    def __init__(self) -> None:
        super().__init__("image over the child's pixel ceiling")


def child_cpu_seconds(ocr_timeout_seconds: float | None) -> int:
    """CPU seconds each process in the child's tree may use."""
    timeout = ocr_timeout_seconds or _DEFAULT_OCR_TIMEOUT_SECONDS
    return int(_TESSERACT_THREADS * timeout) + _CPU_MARGIN_SECONDS


def child_timeout_seconds(max_ocr_pages: int, ocr_timeout_seconds: float | None) -> float:
    """Wall-clock seconds the child may run: every page it may OCR at
    its longest (the OCR timeout, or with none the CPU limit of a
    single-threaded Tesseract), plus margins."""
    per_page = ocr_timeout_seconds or float(child_cpu_seconds(None))
    return max(max_ocr_pages, 1) * (per_page + _PAGE_MARGIN_SECONDS) + _START_MARGIN_SECONDS


def _tesseract_cmd() -> str:
    """The Tesseract the indexer's ``PATH`` finds (``/usr/bin/tesseract``
    in the image). The child runs with no ``PATH`` (``_runner``), so
    without it pytesseract would search only the default path, which
    misses a Tesseract installed elsewhere, as on a developer's Mac.
    When none is found, the bare name: the child then fails with
    ``TesseractNotFoundError``, as the indexer did."""
    return shutil.which("tesseract") or "tesseract"


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001 — dispatcher already gated on this
    max_ocr_pages: int = 20,
    ocr_timeout_seconds: float | None = None,
    max_pdf_pages: int | None = None,  # noqa: ARG001 — single-page format
    on_progress: Callable[[], None] | None = None,
) -> tuple[str, str]:
    """OCR an image attachment in the extractor child. Returns
    (text, "image-ocr").

    ``ocr_timeout_seconds`` (when set) bounds Tesseract per page, so a
    multipage TIFF costs at most ``max_ocr_pages`` of them.
    ``on_progress`` (when set) is called for each page OCR'd.
    """
    timeout = ocr_timeout_seconds if ocr_timeout_seconds and ocr_timeout_seconds > 0 else None
    result = run_child(
        "image",
        payload,
        options=[str(max_ocr_pages), str(timeout or 0), _tesseract_cmd()],
        max_address_space_bytes=CHILD_MAX_ADDRESS_SPACE_BYTES,
        max_cpu_seconds=child_cpu_seconds(timeout),
        timeout_seconds=child_timeout_seconds(max_ocr_pages, timeout),
        max_output_bytes=_MAX_OUTPUT_BYTES,
        caps=_CAPS,
        counts=CHILD_DEGRADATION_KEYS,
        permanent={"ImagePixelCeilingError": ImagePixelCeilingError},
        on_progress=on_progress,
    )
    # Anything the decode or OCR recorded through the package helpers in
    # the child (#1314), the scale-down factor included; the image caps
    # below cross as C frames.
    apply_child_degradation(log, "image", result.counts)
    factor = result.counts.get(CHILD_IMAGE_SCALE_FACTOR)
    if factor and result.text.strip():
        # The scaled-down image was read: its text is indexed (#1401).
        warn_rate_limited(
            log,
            "image OCR at 1/%d scale read %d chars",
            factor,
            len(result.text),
            level=logging.INFO,
        )
    for cap in result.caps:
        if cap == CAP_TEXT:
            warn_extractor_cap(log, cap, "image OCR text cut at %d chars", _MAX_TEXT_CHARS)
            continue
        if cap == CAP_MPO_FRAMES:
            warn_extractor_cap(
                log, cap, "image OCR read the primary picture of a multi-picture file only"
            )
            continue
        # The frames past the cap are not counted: the child seeks one
        # frame past it rather than walk the whole frame chain (#885).
        note_ocr_capped_image()
        if cap == CAP_FRAMES:
            # The limit that cut, so raising it re-extracts (#1418). Not
            # for an unreadable next frame: a higher limit would only try
            # to read it, and fail the image.
            record_cap_cut(CAP_OCR_PAGES, max_ocr_pages)
            warn_rate_limited(
                log,
                "image OCR capped at %d of at least %d frames",
                max_ocr_pages,
                max_ocr_pages + 1,
            )
        else:
            warn_rate_limited(
                log,
                "image OCR capped at %d frames; the next frame could not be read",
                max_ocr_pages,
            )
    return result.text, "image-ocr"
