"""PDF extractor with OCR fallback.

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
the cap are never rendered; the truncation logs a rate-limited
WARNING with the counts and is counted in the attachments aggregate
(#871), but is not recorded. If the pages within the cap yield
text, the dispatcher caches an ordinary ``success`` that cannot be
told apart from a complete extraction; if they yield none, the usual
``empty`` (or short digital-text ``success``) applies. The result is
cached by content hash, so raising the cap later does not re-extract a
payload already cached; it applies only to payloads extracted after
the change.

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
the dispatcher records that as a ``failed`` row by type.
"""

from __future__ import annotations

import io
import logging
import math
import tempfile
import time
from collections.abc import Callable

import pypdf

from . import (
    note_ocr_capped,
    note_pdf_page_failed,
    note_pdf_pages_unrecovered,
    warn_extractor_cap,
    warn_rate_limited,
)

log = logging.getLogger("indexer.extractor.pdf")

# Minimum extracted-character count below which we treat a page's
# digital text as "nothing usable" and OCR the page. A handful of stray
# whitespace/header tokens from a scanned page sometimes do come out of
# pypdf — without this floor we'd accept that as "success" and never
# OCR the actual page contents. The same floor over the whole document
# decides whether a PDF with OCR off is recorded as needing OCR.
_MIN_DIGITAL_CHARS = 40
_OCR_DISABLED_EXTRACTOR = "pdf-ocr-disabled"

# OCR render resolution, and the most pixels any one page may rasterize
# to. A US-letter page at 200 dpi is ~3.7M pixels; the budget leaves room
# for A3 / legal while stopping a tiny PDF that declares a huge page from
# asking Poppler for gigabytes of raster (written to the tmpfs, which
# counts against the container's memory limit, before Pillow's own
# size check can run).
_OCR_DPI = 200
_MAX_OCR_PAGE_PIXELS = 10_000_000


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
            return digital_text, "pdf-digital"
        # The digital text layer is below the useful threshold, so this
        # PDF likely needs OCR. Return a sentinel extractor name so the
        # dispatcher can cache the same OCR-disabled ``unsupported`` shape
        # image attachments use; that cache row is re-run when OCR is
        # enabled later instead of permanently poisoning recall.
        return digital_text, _OCR_DISABLED_EXTRACTOR

    # The pages to OCR: those whose own text layer is under the floor,
    # first ``max_ocr_pages`` of them.
    ocr_pages = [i for i, text in enumerate(digital_pages) if len(text) < _MIN_DIGITAL_CHARS]
    if 0 < max_ocr_pages < len(ocr_pages):
        # The pages past the cap are never read (#871): counted for the
        # attachments aggregate, and the line is rate limited, since one
        # message can carry many capped PDFs (review round 2 on #884).
        note_ocr_capped(len(ocr_pages) - max_ocr_pages)
        warn_rate_limited(
            log, "pdf OCR capped at %d of %d scanned pages", max_ocr_pages, len(ocr_pages)
        )
        ocr_pages = ocr_pages[:max_ocr_pages]
    if not ocr_pages:
        return digital_text, "pdf-digital"

    try:
        ocr_text = _extract_ocr(
            payload,
            pages=ocr_pages,
            ocr_timeout_seconds=ocr_timeout_seconds,
            on_progress=on_progress,
        )
    except MemoryError, RecursionError:
        # Host pressure, not this document: the dispatcher re-raises it.
        raise
    except Exception as exc:  # noqa: BLE001
        # The message can quote the document, so only the type is
        # logged (#257).
        log.warning("PDF OCR fallback failed: %s", type(exc).__name__)
        if len(digital_text) >= _MIN_DIGITAL_CHARS:
            # A mixed PDF keeps its digital text, as before page-level
            # OCR; its unread pages are lost, as past the cap.
            return digital_text, "pdf-digital"
        # The digital text layer was below the usable threshold, so
        # swallowing the failure would cache the attachment as empty /
        # partial and make the job look successful. Let the dispatcher
        # record a failed extraction with the OCR error type so
        # operators can fix Poppler/Tesseract and re-run extraction.
        raise

    recovered.update(index for index, text in ocr_text.items() if text)
    if not any(ocr_text.values()):
        return digital_text, "pdf-digital"
    # Each page in order: its digital text, then its OCR text. A scanned
    # page's few digital characters (a stamped header) are kept beside
    # the OCR; the chunker normalises whitespace.
    merged = (
        "\n\n".join(part for part in (text, ocr_text.get(i, "")) if part)
        for i, text in enumerate(digital_pages)
    )
    return "\n\n".join(page for page in merged if page), "pdf-ocr"


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
            # The pages past the cap are never read: WARNING, rate
            # limited, counted for the attachments aggregate (#903).
            warn_extractor_cap(
                log, "pdf_digital_pages", "pdf-digital stopped at %d pages", max_pdf_pages
            )
            break
        try:
            text = page.extract_text() or ""
        except MemoryError, RecursionError:
            # Host pressure, not this page: the dispatcher re-raises it.
            raise
        except Exception as exc:  # noqa: BLE001
            # Per-page failures (broken cross-ref tables, cipher
            # entries pypdf chokes on) shouldn't abort the whole doc.
            log.debug("pypdf page extract failed: %s", type(exc).__name__)
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
    render takes no timeout; see the comment at the page-count guard. Each run is OCR'd before the next is
    rendered, so at most one run's page images are held at once.

    Uses ``pdf2image`` (Poppler) for rendering and ``pytesseract`` for
    OCR. Both are imported lazily so a missing system dep surfaces here
    rather than at indexer start.

    The ``output_folder`` passed to ``convert_from_bytes`` is a
    per-call ``TemporaryDirectory`` rooted at ``/tmp``. Two reasons:

    1. ``pdf2image`` writes one PPM per rendered page (a US-letter page
       at 200 dpi is ~6 MB). Without an auto-cleaning context manager
       these files leak into ``/tmp`` for the lifetime of the indexer
       container, which runs ``/tmp`` as a tmpfs of bounded size. After
       enough scanned PDFs the tmpfs reaches 100% and every subsequent
       OCR fallback fails with ``ENOSPC`` until the container restarts.
    2. ``/tmp`` is the only writable path on the hardened ``read_only``
       image — ``pdf2image``'s default tempdir falls back to
       ``/var/tmp`` which is not writable here.

    The temp dir is cleaned on both success and exception, so an
    ``ENOSPC`` mid-render or a failing tesseract call does not leak.
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
        # bypassing pdf2image (#868).
        started = time.monotonic()
        pdfinfo_from_bytes(payload, timeout=math.ceil(render_budget))
        page_count_seconds = time.monotonic() - started
        if page_count_seconds * 2 > render_budget:
            raise TimeoutError("PDF OCR render budget exhausted")
        render_budget -= page_count_seconds

    # ``dir="/tmp"`` keeps the temp dir on the writable tmpfs (the
    # hardened image's only writable path). ``TemporaryDirectory``
    # removes the dir and its contents on context exit, including the
    # exception path — so a leaked PPM cannot survive the OCR call.
    with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:  # nosec B108 — tmpfs
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
                # A lower DPI lowers OCR accuracy for every page (#903).
                # Reported once a render at it has run, not when it is
                # chosen, so a Poppler failure before any render is only
                # the failure (review round 3 on #917).
                warn_extractor_cap(
                    log, "pdf_ocr_dpi", "pdf OCR rendered at %d dpi, not %d", dpi, _OCR_DPI
                )
            for index, image in zip(run, images, strict=False):
                text = pytesseract.image_to_string(image, **tesseract_kwargs)
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
