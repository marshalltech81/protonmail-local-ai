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
text. An error in the child (a decompression bomb, a Tesseract timeout
or failure, the child's own ``MemoryError`` or ``RecursionError`` at
its limit) is reported by type name and recorded ``failed`` by the
dispatcher, as is a limit hit, a timeout or output that breaks the
protocol.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable

from . import note_ocr_capped_image, warn_extractor_cap, warn_rate_limited
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

# Address space each process in the child's tree may map
# (``RLIMIT_AS``): the child, and each Tesseract it starts, one at a
# time, so the tree holds at most two at once. Plainly measured in the
# indexer image (CPython 3.14, Tesseract 5.5.0 with up to four OpenMP
# threads), as the smallest limit under which the extraction still
# succeeds, on synthetic images at the 30,000,000-pixel cap:
#
# * a photo-like JPEG with 3,000 words of text, and the same as HEIC:
#   441 MiB (Tesseract), 8 s;
# * an all-white RGBA PNG (126 KB, a decompression-bomb shape): 441 MiB
#   (the child's decode, alpha removal and rotation copy), 1 s;
# * a JPEG of random noise: 606 MiB (Tesseract), 6 s;
# * a 32-bit integer TIFF: 307 MiB; a small screenshot: 95 MiB.
#
# A 21-frame TIFF of text pages at the cap OCRs its 20 pages in 140 s
# (310 s of CPU in all, each Tesseract under 8 s), the child's peak
# 113 MB; of RGBA pages, 17 s with the child's CPU at 17 s. 1 GiB is
# 1.7 times the largest (noise) and 2.3 times the largest page with
# text; past twice the pixel cap the header is refused before any
# decode.
CHILD_MAX_ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024

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
_CAPS = frozenset({CAP_FRAMES, CAP_FRAMES_UNREADABLE, CAP_TEXT})


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
        on_progress=on_progress,
    )
    for cap in result.caps:
        if cap == CAP_TEXT:
            warn_extractor_cap(log, cap, "image OCR text cut at %d chars", _MAX_TEXT_CHARS)
            continue
        # The frames past the cap are not counted: the child seeks one
        # frame past it rather than walk the whole frame chain (#885).
        note_ocr_capped_image()
        if cap == CAP_FRAMES:
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
