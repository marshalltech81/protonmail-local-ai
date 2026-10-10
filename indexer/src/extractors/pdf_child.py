"""PDF extraction, run in the extractor child (#1293).

``pdf.extract`` runs it as
``python -I extractor_child.py pdf <options> <payload file>`` (the
options are ``pdf.OPTIONS``), through the runner's launcher
(``_launcher.py``), which has already lowered the address-space and CPU
limits ``pdf.py`` passes. Poppler's ``pdfinfo`` and ``pdftoppm`` (started
by pdf2image) and Tesseract (started by pytesseract) inherit both limits
and are killed with the child's process group when the run ends; their
files go to the run's scratch directory (``TMPDIR``), which the runner
removes.

Two paths share one entry point:

1. **Digital PDFs** (most generated invoices, statements, contracts):
   ``pypdf`` walks the page tree and pulls out the embedded text layer.
   Cheap — typical page is a few ms — and exact (no OCR error rate).

2. **Scanned pages** (photos of paper, faxes, signed PDFs flattened to
   image): the digital path returns empty or near-empty text for the
   page. Each such page falls through to OCR: it is rendered to a PIL
   image via ``pdf2image`` (which calls out to Poppler's ``pdftoppm``)
   and the image is passed to Tesseract. The choice is made per page,
   so a PDF mixing digital and scanned pages OCRs only the scanned
   ones (#292). Only pages the digital walk reached
   (``max_pdf_pages``) are candidates.

The OCR fallback is gated by ``ocr_enabled`` and bounded by
``max_ocr_pages``, counted over the pages selected for OCR, so a
500-page scanned book attachment does not monopolise CPU. Pages beyond
the cap are never rendered; the truncation is counted in the
attachments aggregate (#871) and reported by name, and the parent logs
it at WARNING. The result records the pages skipped (#891), and the
parent records the cap that cut it, as it records the digital-page cap
(#1418), so raising either setting re-extracts the cached result
(``attachment_indexing.cap_raised``).

With OCR off, a PDF whose whole text layer is under the floor records
the OCR-disabled sentinel, which is re-run once OCR is on. A PDF with
usable digital text records ``pdf-digital`` even if some pages are
scanned, and that row is not re-run when OCR is turned on later: its
scanned pages stay unread until the next ``pdf`` version bump.

Encrypted PDFs: ``pypdf`` opens an encrypted file with the empty user
password, so an owner-password-only PDF (print / copy restrictions, no
open password) extracts like any other; AES needs ``cryptography``
(#691). No other password is ever tried. A PDF that needs a real open
password raises ``FileNotDecryptedError`` when its pages are read, and
the dispatcher records that as ``unsupported`` with fixed text, as it
does a pypdf ``LimitReachedError`` that escapes this module (raised
while the file is opened or its pages are listed): the same bytes
always fail the same way (#931). The child reports either by type name
and the parent raises it again. One raised by a single page's text
extraction is a per-page failure like any other, below.

What the in-process extractor logged here crosses to the parent as
fixed tokens instead (``pdf``): the caps that cut (``report_cap``), the
lowered OCR DPI (``record_pdf_ocr_dpi``) and the type name of an OCR
fallback that failed (``record_recovered_error``); the counters and the
text loss cross as the child's degradation counts. Nothing here logs
what the parent needs: the child's stderr is discarded. A
``MemoryError`` or ``RecursionError`` is the child's own limit: it is
reported by type name like any other error and recorded ``failed``.
"""

from __future__ import annotations

import io
import math
import os
import tempfile
import time
from collections.abc import Callable, Sequence

import pypdf

from . import (
    note_ocr_capped,
    note_pdf_page_failed,
    note_pdf_pages_unrecovered,
    note_text_lost,
    record_ocr_pages_skipped,
    record_pdf_ocr_dpi,
    record_recovered_error,
    report_cap,
)
from .pdf import (
    _MAX_TEXT_CHARS,
    CAP_DIGITAL,
    CAP_OCR,
    CAP_TEXT,
    EXTRACTOR_DIGITAL,
    EXTRACTOR_OCR,
    EXTRACTOR_OCR_DISABLED,
    OCR_DPI,
    OPTIONS,
)

# Minimum extracted-character count below which we treat a page's
# digital text as "nothing usable" and OCR the page. A handful of stray
# whitespace/header tokens from a scanned page sometimes do come out of
# pypdf — without this floor we'd accept that as "success" and never
# OCR the actual page contents. The same floor over the whole document
# decides whether a PDF with OCR off is recorded as needing OCR.
_MIN_DIGITAL_CHARS = 40

# OCR render resolution, and the most pixels any one page may rasterize
# to. A US-letter page at 200 dpi is ~3.7M pixels; the budget leaves room
# for A3 / legal while stopping a tiny PDF that declares a huge page from
# asking Poppler for gigabytes of raster (written to the tmpfs, which
# counts against the container's memory limit, before Pillow's own
# size check can run).
_OCR_DPI = OCR_DPI
_MAX_OCR_PAGE_PIXELS = 10_000_000


class PdfChildOptionsError(Exception):
    """An argument of the child is outside ``pdf.OPTIONS``. Fixed text:
    the parent built the arguments, so this is a bug, not the payload."""

    def __init__(self) -> None:
        super().__init__("pdf child argument out of range")


def parse_options(values: Sequence[str]) -> dict[str, object]:
    """Check every argument against ``pdf.OPTIONS`` before any work, and
    return them by name, typed. Raises ``PdfChildOptionsError`` on the
    wrong count, a wrong type or a value out of range."""
    if len(values) != len(OPTIONS):
        raise PdfChildOptionsError
    parsed: dict[str, object] = {}
    for (name, kind, low, high), value in zip(OPTIONS, values, strict=True):
        parsed[name] = _parse_option(kind, low, high, value)
    return parsed


def _parse_option(kind: str, low: float, high: float, value: str) -> object:
    """One argument of ``kind`` within [``low``, ``high``]."""
    if kind == "path":
        if not low <= len(value) <= high or "\0" in value:
            raise PdfChildOptionsError
        return value
    if kind == "int-or-none" and value == "none":
        return None
    if kind in ("bool", "int", "int-or-none"):
        if not value.isascii() or not value.isdecimal():
            raise PdfChildOptionsError
        number: float = int(value)
    else:
        try:
            number = float(value)
        except ValueError:
            raise PdfChildOptionsError from None
        if not math.isfinite(number):
            raise PdfChildOptionsError
    if not low <= number <= high:
        raise PdfChildOptionsError
    return bool(number) if kind == "bool" else number


def extract_text(
    payload: bytes,
    *options: str,
    on_progress: Callable[[], None] | None = None,
) -> tuple[str, list[str], str]:
    """The child's extraction: the text, stripped as the dispatcher
    strips it and cut at ``pdf._MAX_TEXT_CHARS`` (``pdf_text_chars``, as
    for images, #1325), no caps returned (each is reported as it cuts,
    ``report_cap``, so it crosses even when the extraction then raises),
    and the extractor name."""
    parsed = parse_options(options)
    path = parsed["path"]
    assert isinstance(path, str)
    # pdf2image and pytesseract find their tools through ``PATH``; the
    # child starts with none. In a test that runs the child in process the
    # value is the process's own.
    os.environ["PATH"] = path
    timeout = parsed["ocr_timeout_seconds"]
    max_pdf_pages = parsed["max_pdf_pages"]
    max_ocr_pages = parsed["max_ocr_pages"]
    assert isinstance(timeout, float)
    assert isinstance(max_ocr_pages, int)
    assert max_pdf_pages is None or isinstance(max_pdf_pages, int)
    text, name = extract(
        payload,
        ocr_enabled=bool(parsed["ocr_enabled"]),
        max_ocr_pages=max_ocr_pages,
        ocr_timeout_seconds=timeout or None,
        max_pdf_pages=max_pdf_pages,
        on_progress=on_progress,
    )
    stripped = text.strip()
    if len(stripped) > _MAX_TEXT_CHARS:
        report_cap(CAP_TEXT)
        stripped = stripped[:_MAX_TEXT_CHARS]
    return stripped, [], name


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,
    max_ocr_pages: int = 20,
    ocr_timeout_seconds: float | None = None,
    max_pdf_pages: int | None = None,
    on_progress: Callable[[], None] | None = None,
) -> tuple[str, str]:
    """Extract text from a PDF payload, falling back to OCR if needed.

    ``max_pdf_pages`` bounds the digital pypdf walk. The OCR cap above
    only bounds the rendered-image path; a 5 MB text-only PDF can
    legitimately carry thousands of pages, and at ~ms each that adds
    up to a meaningful queue stall.

    ``ocr_timeout_seconds`` is forwarded into the OCR fallback for the
    same reason as ``image.extract`` — see that module's docstring.

    ``on_progress`` (when set) is called after each page the digital
    walk reads and after each page OCR'd, so the indexer's heartbeat
    keeps up with a long scan (#485). A page that hangs reports nothing.
    """
    # Pages pypdf could not read, and the ones OCR then recovered: the
    # difference is counted for the attachments aggregate as
    # ``pdf_pages_unrecovered`` (review round 4 on #884), on every return
    # and raise below.
    failed: set[int] = set()
    digital_pages = _extract_digital_pages(
        payload, max_pdf_pages=max_pdf_pages, on_progress=on_progress, failed=failed
    )
    recovered: set[int] = set()
    try:
        return _text_from_pages(
            payload,
            digital_pages,
            recovered=recovered,
            ocr_enabled=ocr_enabled,
            max_ocr_pages=max_ocr_pages,
            ocr_timeout_seconds=ocr_timeout_seconds,
            on_progress=on_progress,
        )
    finally:
        if failed:
            note_pdf_pages_unrecovered(len(failed - recovered))


def _text_from_pages(
    payload: bytes,
    digital_pages: list[str],
    *,
    recovered: set[int],
    ocr_enabled: bool,
    max_ocr_pages: int,
    ocr_timeout_seconds: float | None,
    on_progress: Callable[[], None] | None,
) -> tuple[str, str]:
    """``extract`` from the digital walk's pages on: the digital text,
    or the OCR fallback for the pages under the floor. Adds to
    ``recovered`` each page OCR read text from."""
    digital_text = "\n\n".join(text for text in digital_pages if text)

    if not ocr_enabled:
        if len(digital_text) >= _MIN_DIGITAL_CHARS:
            # A page under the floor is one OCR would read: with OCR off
            # its text, if it is a scan, is lost (#1242).
            if any(len(text) < _MIN_DIGITAL_CHARS for text in digital_pages):
                note_text_lost()
            return digital_text, EXTRACTOR_DIGITAL
        # The digital text layer is below the useful threshold, so this
        # PDF likely needs OCR. Return a sentinel extractor name so the
        # dispatcher can cache the same OCR-disabled ``unsupported`` shape
        # image attachments use; that cache row is re-run when OCR is
        # enabled later instead of permanently poisoning recall.
        return digital_text, EXTRACTOR_OCR_DISABLED

    # The pages to OCR: those whose own text layer is under the floor,
    # first ``max_ocr_pages`` of them.
    ocr_pages = [i for i, text in enumerate(digital_pages) if len(text) < _MIN_DIGITAL_CHARS]
    if 0 < max_ocr_pages < len(ocr_pages):
        # The pages past the cap are never read (#871): counted for the
        # attachments aggregate. The result carries the count too, so the
        # cached row does (#891). Reported by name: the parent logs it,
        # rate limited, and records the limit that cut, so raising it
        # re-extracts (#1418).
        note_ocr_capped(len(ocr_pages) - max_ocr_pages)
        record_ocr_pages_skipped(len(ocr_pages) - max_ocr_pages)
        report_cap(CAP_OCR)
        ocr_pages = ocr_pages[:max_ocr_pages]
    if not ocr_pages:
        return digital_text, EXTRACTOR_DIGITAL

    try:
        ocr_text = _extract_ocr(
            payload,
            pages=ocr_pages,
            ocr_timeout_seconds=ocr_timeout_seconds,
            on_progress=on_progress,
        )
    except MemoryError, RecursionError:
        # The child's own limit, not an OCR failure: it ends the
        # extraction, which the parent records ``failed``.
        raise
    except Exception as exc:  # noqa: BLE001
        # The message can quote the document, so only the type is
        # reported (#257), and the parent logs it.
        record_recovered_error(type(exc).__name__)
        if len(digital_text) >= _MIN_DIGITAL_CHARS:
            # A mixed PDF keeps its digital text, as before page-level
            # OCR; its unread pages are lost, as past the cap.
            note_text_lost()
            return digital_text, EXTRACTOR_DIGITAL
        # The digital text layer was below the usable threshold, so
        # swallowing the failure would cache the attachment as empty /
        # partial and make the job look successful. Let the dispatcher
        # record a failed extraction with the OCR error type so
        # operators can fix Poppler/Tesseract and re-run extraction.
        raise

    recovered.update(index for index, text in ocr_text.items() if text)
    if not any(ocr_text.values()):
        return digital_text, EXTRACTOR_DIGITAL
    # Each page in order: its digital text, then its OCR text. A scanned
    # page's few digital characters (a stamped header) are kept beside
    # the OCR; the chunker normalises whitespace.
    merged = (
        "\n\n".join(part for part in (text, ocr_text.get(i, "")) if part)
        for i, text in enumerate(digital_pages)
    )
    return "\n\n".join(page for page in merged if page), EXTRACTOR_OCR


def _extract_digital_pages(
    payload: bytes,
    *,
    max_pdf_pages: int | None = None,
    on_progress: Callable[[], None] | None = None,
    failed: set[int] | None = None,
) -> list[str]:
    """Pull the embedded text layer out of a PDF: one stripped string
    per page, empty for a page without text or whose extraction failed.

    ``max_pdf_pages`` (when set) caps page iteration so a pathological
    PDF with thousands of mostly-blank pages cannot stall the worker.
    """
    reader = pypdf.PdfReader(io.BytesIO(payload))
    pages: list[str] = []
    for index, page in enumerate(reader.pages):
        if max_pdf_pages is not None and index >= max_pdf_pages:
            # The pages past the cap are never read: reported by name,
            # and the parent logs it at WARNING, counts it for the
            # attachments aggregate (#903) and records the limit that
            # cut, so raising it re-extracts (#1418).
            report_cap(CAP_DIGITAL)
            break
        try:
            text = page.extract_text() or ""
        except MemoryError, RecursionError:
            # The child's own limit, not this page: it ends the
            # extraction, which the parent records ``failed``.
            raise
        except Exception:  # noqa: BLE001
            # Per-page failures (broken cross-ref tables, cipher
            # entries pypdf chokes on) shouldn't abort the whole doc.
            # Counted for the INFO attachments aggregate (#871).
            note_pdf_page_failed()
            if failed is not None:
                failed.add(index)
            text = ""
        pages.append(text.strip())
        if on_progress is not None:
            on_progress()
    return pages


def _extract_ocr(
    payload: bytes,
    *,
    pages: list[int],
    ocr_timeout_seconds: float | None = None,
    on_progress: Callable[[], None] | None = None,
) -> dict[int, str]:
    """Render ``pages`` (ascending 0-based indexes) to images and OCR
    them via Tesseract; returns each page's stripped text by index.

    Each run of consecutive pages is one Poppler call, so no page outside
    ``pages`` is rendered. The render timeout is one budget shared by
    the runs, counting only time spent in Poppler, so the whole render
    stays bounded as when it was one call; Tesseract time is bounded per
    page by its own timeout. pdf2image's own page count before each
    render takes no timeout; see the comment at the page-count guard.
    Each run is OCR'd before the next is rendered, and each page's image
    is closed once it is OCR'd, so the child holds at most one decoded
    page at a time, whatever ``max_ocr_pages`` is.

    Uses ``pdf2image`` (Poppler) for rendering and ``pytesseract`` for
    OCR. Both are imported lazily so a missing system dep surfaces here
    rather than at indexer start.

    The ``output_folder`` passed to ``convert_from_bytes`` is a
    per-call ``TemporaryDirectory`` in the run's scratch directory
    (``TMPDIR``, on the ``/tmp`` tmpfs), as are pdf2image's copies of the
    payload. ``pdf2image`` writes one PPM per rendered page (a US-letter
    page at 200 dpi is ~6 MB), and the tmpfs counts against the
    container's memory, so the directory is removed as each call ends,
    on success and exception; the runner removes the scratch directory
    whatever is left in it once the child's process group is dead, a
    killed child included.
    """
    import pytesseract
    from pdf2image import convert_from_bytes, pdfinfo_from_bytes

    dpi = _ocr_dpi(payload, pages)
    runs: list[list[int]] = []
    for index in pages:
        if runs and index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])
    render_budget = (
        float(ocr_timeout_seconds)
        if ocr_timeout_seconds is not None and ocr_timeout_seconds > 0
        else None
    )

    tesseract_kwargs: dict[str, float] = {}
    if ocr_timeout_seconds is not None and ocr_timeout_seconds > 0:
        # Apply the timeout per page rather than to the whole document
        # so a slow page does not eat the entire budget for the rest.
        tesseract_kwargs["timeout"] = float(ocr_timeout_seconds)

    page_count_seconds = 0.0
    if render_budget is not None:
        # pdf2image runs Poppler's ``pdfinfo`` for the page count before
        # each render and passes it no timeout (#781). Time one bounded
        # run of it here first (pdf2image types the timeout as whole
        # seconds); a PDF that stalls it raises ``PDFPopplerTimeoutError``
        # here, which degrades like a hung render. ``pdfinfo`` takes about
        # as long each time on the same bytes, so each render's timeout
        # holds back that time for the unbounded call inside it, and a
        # page count over half the budget leaves no room for one render:
        # that is an OCR timeout too. The remaining gap, a ``pdfinfo``
        # much slower on the inner call than on this one, would need
        # bypassing pdf2image (#868); in the child, the child's own
        # wall-clock timeout bounds it.
        started = time.monotonic()
        pdfinfo_from_bytes(payload, timeout=math.ceil(render_budget))
        page_count_seconds = time.monotonic() - started
        if page_count_seconds * 2 > render_budget:
            raise TimeoutError("PDF OCR render budget exhausted")
        render_budget -= page_count_seconds

    # In the scratch directory (``TMPDIR``). ``TemporaryDirectory``
    # removes the dir and its contents on context exit, including the
    # exception path — so a leaked PPM cannot survive the OCR call.
    with tempfile.TemporaryDirectory() as tmpdir:
        texts: dict[int, str] = {}
        for run in runs:
            convert_kwargs: dict[str, object] = {
                "dpi": dpi,
                "first_page": run[0] + 1,
                "last_page": run[-1] + 1,
                "output_folder": tmpdir,
            }
            if render_budget is not None:
                # The same budget bounds the whole Poppler render, so a
                # hung render cannot block the worker. The render's own
                # timeout leaves room for pdf2image's unbounded page
                # count (see above).
                render_timeout = render_budget - page_count_seconds
                if render_timeout <= 0:
                    raise TimeoutError("PDF OCR render budget exhausted")
                convert_kwargs["timeout"] = render_timeout
            started = time.monotonic()
            images = convert_from_bytes(payload, **convert_kwargs)  # type: ignore[arg-type]
            if render_budget is not None:
                render_budget -= time.monotonic() - started
            if dpi < _OCR_DPI and run is runs[0]:
                # A lower DPI lowers OCR accuracy for every page (#903):
                # the parent logs it as an extractor cap. Reported once a
                # render at it has run, not when it is chosen, so a
                # Poppler failure before any render is only the failure
                # (review round 3 on #917).
                record_pdf_ocr_dpi(dpi)
            for index, image in zip(run, images, strict=False):
                try:
                    text = pytesseract.image_to_string(image, **tesseract_kwargs)
                finally:
                    # Its pixels are not needed again (see above).
                    image.close()
                texts[index] = (text or "").strip()
                if on_progress is not None:
                    on_progress()
        return texts


def _ocr_dpi(payload: bytes, pages: list[int]) -> int:
    """Return the highest DPI, up to ``_OCR_DPI``, at which every OCR'd
    page fits ``_MAX_OCR_PAGE_PIXELS``.

    Reads each page's MediaBox (scaled by UserUnit), which is what
    ``pdftoppm`` rasterizes, over ``pages``, the pages the OCR pass will
    render.
    Each side is counted as a whole number of pixels, at least one,
    because that is what gets allocated: a sliver page has a tiny area
    but can still rasterize to one pixel by hundreds of millions. One DPI
    applies to the whole document, so an oversized page lowers it for
    every page. A parse failure propagates: without page sizes the raster
    size is unknown, so the fallback fails closed.
    """
    reader = pypdf.PdfReader(io.BytesIO(payload))
    pages_inches: list[tuple[float, float]] = []
    for index in pages:
        page = reader.pages[index]
        box = page.mediabox
        unit = float(page.user_unit)
        pages_inches.append((abs(float(box.width)) * unit / 72, abs(float(box.height)) * unit / 72))

    def fits(dpi: int) -> bool:
        return all(
            max(1, math.ceil(w * dpi)) * max(1, math.ceil(h * dpi)) <= _MAX_OCR_PAGE_PIXELS
            for w, h in pages_inches
        )

    # Pixel count only grows with DPI, so the first fit going down is the
    # highest; at most ``_OCR_DPI`` cheap checks.
    for dpi in range(_OCR_DPI, 0, -1):
        if fits(dpi):
            return dpi
    raise ValueError("PDF page too large to render for OCR")
