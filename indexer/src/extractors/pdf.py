"""PDF extractor with OCR fallback.

The whole extraction runs in the extractor child (``extractor_child.py``
running ``pdf_child.py``), started through ``_runner.run_child`` (PLAN.md
decision 42, #1293): pypdf's parse and digital text walk, the page sizes
that set the OCR DPI, Poppler's ``pdfinfo`` and ``pdftoppm`` (started by
pdf2image) and Tesseract (started by pytesseract). The launcher lowers
the child's address space (``RLIMIT_AS``) and CPU time (``RLIMIT_CPU``)
before it starts; each Poppler and Tesseract process the child starts
inherits both limits (each process gets its own), and the runner kills
the child's whole process group when the run ends, at its wall-clock
timeout included, and removes its scratch directory, where pdf2image
writes its copies of the payload and the rendered pages and pytesseract
its temporary files (#1021). See ``pdf_child`` for the extraction itself:
the digital walk, the per-page OCR fallback and its caps.

The child's result crosses the pipe in the runner's framed protocol, as
fixed tokens and counts only (#1293):

* a ``P`` frame per page the digital walk read and per page OCR'd,
  passed to the dispatcher's ``on_progress`` (#485);
* a ``C`` frame per cap that cut the text: the digital-page cap
  (``INDEXER_PDF_MAX_DIGITAL_PAGES``) and the OCR page cap
  (``INDEXER_OCR_MAX_PAGES``), whose limits this module records on the
  result (#1418), and the child's text budget;
* ``N`` frames with the degradation the child recorded: pages pypdf
  could not read and those OCR never recovered, the OCR cap's skipped
  pages (also kept on the result, #891), a text loss (#1242) and the
  OCR DPI when the page-pixel budget lowered it;
* an ``R`` frame with the type name of an OCR fallback that failed;
* the text with the extractor name (``pdf-digital``, ``pdf-ocr``, or the
  OCR-disabled sentinel ``pdf-ocr-disabled``), or ``E`` with the type
  name of what the extraction raised.

The caps, counts and the OCR failure are applied and logged here, from
``_report``, whether the child then returned text or raised, as the
in-process extractor applied them before it returned or raised. A
``FileNotDecryptedError`` or ``LimitReachedError`` (pypdf, matched by
exact type name) is raised again here so the dispatcher records it
``unsupported`` (#931); any other error, the child's own
``MemoryError`` or ``RecursionError`` at its limits included, is
recorded ``failed`` by type name, as are a limit hit, a timeout and
output that breaks the protocol.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable

from pypdf.errors import FileNotDecryptedError, LimitReachedError

from . import (
    CAP_DIGITAL_PAGES,
    CAP_OCR_PAGES,
    CHILD_DEGRADATION_KEYS,
    CHILD_OCR_PAGES_SKIPPED,
    CHILD_PDF_OCR_DPI,
    apply_child_degradation,
    record_cap_cut,
    warn_extractor_cap,
    warn_rate_limited,
)
from ._runner import ChildReport, run_child

log = logging.getLogger("indexer.extractor.pdf")

# The extractor names the child may return: the digital text layer, the
# per-page OCR fallback, and the sentinel for a PDF that needs OCR while
# it is off.
EXTRACTOR_DIGITAL = "pdf-digital"
EXTRACTOR_OCR = "pdf-ocr"
EXTRACTOR_OCR_DISABLED = "pdf-ocr-disabled"
_NAMES = frozenset({EXTRACTOR_DIGITAL, EXTRACTOR_OCR, EXTRACTOR_OCR_DISABLED})

# Cap names the child reports: the digital walk stopped at
# ``max_pdf_pages``, OCR stopped at ``max_ocr_pages``, and the text
# budget below cut the text.
CAP_DIGITAL = "pdf_digital_pages"
CAP_OCR = "pdf_ocr_pages"
CAP_TEXT = "pdf_text_chars"
_CAPS = frozenset({CAP_DIGITAL, CAP_OCR, CAP_TEXT})

# OCR render resolution. The child lowers it for the whole document when
# a page would rasterize past its page-pixel budget, and reports the DPI
# it used.
OCR_DPI = 200

# Characters of text the child returns, after stripping it as the
# dispatcher does, as for images (owner decision 2026-10-08 on #1325):
# it bounds the child's output, which the parent reads whole. A PDF
# whose stripped text fits stores what the in-process extractor stored;
# the indexer keeps at most ``INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS``
# (2,000,000 by default) anyway.
_MAX_TEXT_CHARS = 10_000_000

# Bytes of the child's output read: the text budget at UTF-8's worst case
# of four bytes a character, plus the frames (a progress frame per page
# read and OCR'd, the caps and counts).
_MAX_OUTPUT_BYTES = 4 * _MAX_TEXT_CHARS + 1024 * 1024

# Address space each process in the child's tree may map (``RLIMIT_AS``):
# the child (pypdf, and Pillow holding one rendered page), and the one
# Poppler or Tesseract process it starts at a time. Plainly measured in
# the indexer image (pypdf 6.19, Poppler and Tesseract 5.5.0 from Debian
# trixie, #1293; ``docs/architecture.md`` has the table) as the smallest
# limit under which the extraction still succeeds, on synthetic PDFs
# inside the 32 MiB byte cap: a page whose content stream is 71 MiB of
# path operators (pypdf decodes at most 75,000,000 bytes of a page's
# content) needs 1,776 MiB, in pypdf's text walk; 500 pages of kerned
# text 112 MiB; a scanned page at the 10,000,000-pixel render budget
# 240 MiB (Tesseract), and Poppler rendering a page that holds a
# 48-megapixel scan 144 MiB. The limit, 2 GiB, is 1.15 times the
# largest; the OCR path needs an eighth of it.
CHILD_MAX_ADDRESS_SPACE_BYTES = 2048 * 1024 * 1024

# Wall-clock seconds the child may take per page of the digital walk: a
# kerned page took 0.03 s and a dense one 0.003 s in the image, and the
# heaviest page measured (the 71 MiB of path operators above) 14.5 s, so
# 500 pages at 2 s each leave room for many such pages in one PDF.
_DIGITAL_PAGE_SECONDS = 2.0
# With the digital-page cap or the OCR page cap off, the wall clock and
# CPU time are sized for their defaults.
_DEFAULT_DIGITAL_PAGES = 500
_DEFAULT_OCR_PAGES = 20
# Tesseract runs up to four OpenMP threads, and CPU time counts every
# thread, so a page can use up to four times its wall-clock time in CPU.
# With no OCR timeout (``INDEXER_OCR_TIMEOUT_SECONDS=0``) the limit for
# the 60 s default applies, as for images.
_TESSERACT_THREADS = 4
_DEFAULT_OCR_TIMEOUT_SECONDS = 60.0
_CPU_MARGIN_SECONDS = 30
# Wall-clock seconds per OCR'd page past its OCR timeout (loading the
# rendered page and pytesseract's temporary file), and for the child's
# start-up.
_PAGE_MARGIN_SECONDS = 10.0
_START_MARGIN_SECONDS = 30.0

# The child's arguments, in order, and the range each must be in; the
# child checks every one against this table before any work starts
# (``pdf_child.parse_options``). ``max_pdf_pages`` takes ``none`` for no
# cap; 0 caps the walk at no pages, as in process. ``max_ocr_pages`` 0
# and ``ocr_timeout_seconds`` 0 mean no cap and no timeout. ``path`` is
# the indexer's ``PATH``: the child runs with none (``_runner``), and
# pdf2image and pytesseract find Poppler and Tesseract through it, as
# they did in the indexer.
_MAX_OPTION_INT = 2**63 - 1
_MAX_OPTION_SECONDS = 1e9
OPTIONS: tuple[tuple[str, str, float, float], ...] = (
    ("ocr_enabled", "bool", 0, 1),
    ("max_ocr_pages", "int", 0, _MAX_OPTION_INT),
    ("ocr_timeout_seconds", "seconds", 0, _MAX_OPTION_SECONDS),
    ("max_pdf_pages", "int-or-none", 0, _MAX_OPTION_INT),
    ("path", "path", 1, 4096),
)


def child_options(
    *,
    ocr_enabled: bool,
    max_ocr_pages: int,
    ocr_timeout_seconds: float | None,
    max_pdf_pages: int | None,
    path: str,
) -> list[str]:
    """The child's arguments (``OPTIONS``) for the extractor's settings,
    with their in-process meanings kept: a page cap at or below 0 is no
    OCR cap, and a digital cap below 0 stops the walk at once, as 0
    does; a timeout at or below 0 is none. Values past a range's top
    are held at it, which no setting can tell apart."""
    timeout = ocr_timeout_seconds if ocr_timeout_seconds is not None else 0.0
    return [
        "1" if ocr_enabled else "0",
        str(min(max(max_ocr_pages, 0), _MAX_OPTION_INT)),
        repr(min(max(float(timeout), 0.0), _MAX_OPTION_SECONDS)),
        "none" if max_pdf_pages is None else str(min(max(max_pdf_pages, 0), _MAX_OPTION_INT)),
        path,
    ]


def _ocr_timeout(ocr_timeout_seconds: float | None) -> float:
    """The per-page OCR timeout the limits are sized for."""
    if ocr_timeout_seconds is not None and ocr_timeout_seconds > 0:
        return float(ocr_timeout_seconds)
    return _DEFAULT_OCR_TIMEOUT_SECONDS


def _digital_pages(max_pdf_pages: int | None) -> int:
    return _DEFAULT_DIGITAL_PAGES if max_pdf_pages is None else max(max_pdf_pages, 0)


def child_cpu_seconds(max_pdf_pages: int | None, ocr_timeout_seconds: float | None) -> int:
    """CPU seconds each process in the child's tree may use: the child's
    digital walk at its per-page allowance, or a Tesseract at four
    threads for its whole OCR timeout, whichever is longer, plus a
    margin."""
    walk = _DIGITAL_PAGE_SECONDS * _digital_pages(max_pdf_pages)
    ocr = _TESSERACT_THREADS * _ocr_timeout(ocr_timeout_seconds)
    return int(math.ceil(max(walk, ocr))) + _CPU_MARGIN_SECONDS


def child_timeout_seconds(
    *,
    ocr_enabled: bool,
    max_ocr_pages: int,
    ocr_timeout_seconds: float | None,
    max_pdf_pages: int | None,
) -> float:
    """Wall-clock seconds the child may run: the digital walk at its
    per-page allowance, and with OCR on, the render (the OCR timeout for
    the timed page count and every render, and as much again for
    pdf2image's untimed page count before each render, #868) and every
    page it may OCR at its longest (the OCR timeout, or with none the
    CPU limit of a single-threaded Tesseract, as for images), plus
    margins."""
    seconds = _START_MARGIN_SECONDS + _DIGITAL_PAGE_SECONDS * _digital_pages(max_pdf_pages)
    if ocr_enabled:
        if ocr_timeout_seconds is not None and ocr_timeout_seconds > 0:
            per_page = float(ocr_timeout_seconds)
        else:
            per_page = float(
                _TESSERACT_THREADS * _DEFAULT_OCR_TIMEOUT_SECONDS + _CPU_MARGIN_SECONDS
            )
        pages = max_ocr_pages if max_ocr_pages > 0 else _DEFAULT_OCR_PAGES
        seconds += 2 * per_page + pages * (per_page + _PAGE_MARGIN_SECONDS)
    return seconds


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,
    max_ocr_pages: int = 20,
    ocr_timeout_seconds: float | None = None,
    max_pdf_pages: int | None = None,
    on_progress: Callable[[], None] | None = None,
) -> tuple[str, str]:
    """Extract text from a PDF payload in the extractor child, falling
    back to OCR per page. Returns the text and the extractor name.

    ``max_pdf_pages`` bounds the digital pypdf walk; ``max_ocr_pages``
    the pages OCR'd; ``ocr_timeout_seconds`` the render and each page's
    Tesseract run (see ``pdf_child``). ``on_progress`` (when set) is
    called after each page the digital walk reads and after each page
    OCR'd, so the indexer's heartbeat keeps up with a long scan (#485).
    """

    def report(child: ChildReport) -> None:
        _report(child, max_ocr_pages=max_ocr_pages, max_pdf_pages=max_pdf_pages)

    result = run_child(
        "pdf",
        payload,
        options=child_options(
            ocr_enabled=ocr_enabled,
            max_ocr_pages=max_ocr_pages,
            ocr_timeout_seconds=ocr_timeout_seconds,
            max_pdf_pages=max_pdf_pages,
            path=os.pathsep.join(os.get_exec_path()),
        ),
        max_address_space_bytes=CHILD_MAX_ADDRESS_SPACE_BYTES,
        max_cpu_seconds=child_cpu_seconds(max_pdf_pages, ocr_timeout_seconds),
        timeout_seconds=child_timeout_seconds(
            ocr_enabled=ocr_enabled,
            max_ocr_pages=max_ocr_pages,
            ocr_timeout_seconds=ocr_timeout_seconds,
            max_pdf_pages=max_pdf_pages,
        ),
        max_output_bytes=_MAX_OUTPUT_BYTES,
        caps=_CAPS,
        counts=CHILD_DEGRADATION_KEYS,
        names=_NAMES,
        permanent={
            "FileNotDecryptedError": FileNotDecryptedError,
            "LimitReachedError": LimitReachedError,
        },
        on_progress=on_progress,
        on_report=report,
    )
    assert result.name is not None  # ``names`` makes the child send one
    return result.text, result.name


def _report(child: ChildReport, *, max_ocr_pages: int, max_pdf_pages: int | None) -> None:
    """Apply and log what the child reported besides its text, as the
    in-process extractor did where it happened: its degradation counts,
    the OCR DPI it lowered, the caps that cut it and an OCR fallback that
    failed. Only fixed text, the configured limits and the child's counts
    and type names are logged."""
    counts = dict(child.counts)
    dpi = counts.pop(CHILD_PDF_OCR_DPI, None)
    apply_child_degradation(log, "pdf", counts)
    if dpi is not None:
        # A lower DPI lowers OCR accuracy for every page (#903).
        warn_extractor_cap(log, "pdf_ocr_dpi", "pdf OCR rendered at %d dpi, not %d", dpi, OCR_DPI)
    for cap in child.caps:
        if cap == CAP_DIGITAL:
            # The pages past the cap are never read (#903); the limit
            # that cut, so raising it re-extracts (#1418).
            warn_extractor_cap(log, cap, "pdf-digital stopped at %d pages", max_pdf_pages)
            if max_pdf_pages is not None:
                record_cap_cut(CAP_DIGITAL_PAGES, max_pdf_pages)
        elif cap == CAP_OCR:
            # The scanned pages past the cap are never read (#871); the
            # child counted them. The limit that cut (#1418).
            record_cap_cut(CAP_OCR_PAGES, max_ocr_pages)
            skipped = counts.get(CHILD_OCR_PAGES_SKIPPED, 0)
            warn_rate_limited(
                log,
                "pdf OCR capped at %d of %d scanned pages",
                max_ocr_pages,
                max_ocr_pages + skipped,
            )
        else:
            warn_extractor_cap(log, cap, "pdf text cut at %d chars", _MAX_TEXT_CHARS)
    for type_name in child.recovered:
        # Rate limited (#889): Tesseract timing out on every scanned PDF
        # would otherwise log once per attachment.
        warn_rate_limited(log, "PDF OCR fallback failed: %s", type_name)
