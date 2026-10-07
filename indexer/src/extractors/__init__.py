"""
Attachment text extraction with per-MIME dispatch.

Public API: ``extract(content_type, filename, payload, ...)`` returns an
``ExtractionResult`` with one of the documented statuses. The dispatch
table maps a normalized content type (or, for ambiguous / missing
``Content-Type`` headers, a filename extension) to a per-format
extractor function. Per-format modules each expose a single
``extract(payload, **opts)`` callable that returns ``(text, extractor_name)``
on success or raises on failure.

The dispatcher itself is the only place that:

* enforces ``INDEXER_ATTACHMENT_MAX_BYTES`` (skip very large attachments
  to bound CPU and memory under a single email with a huge zip);
* honors ``INDEXER_OCR_ENABLED`` (a single switch turns off all OCR
  paths — image extraction and the scanned-PDF fallback — if the
  operator wants to disable Tesseract entirely);
* turns extractor exceptions into ``failed`` ``ExtractionResult`` rows
  rather than letting them bubble into the indexer queue and dead-
  letter the parent message.

Per-format extractors live in sibling modules and are intentionally
tiny — they exist so each MIME type's library can be lazy-imported
(see ``_safe_import``). A missing optional dependency for a rare
format does not break the indexer; it surfaces as
``status="unsupported"`` with an extractor-specific error message
operators can act on.
"""

from __future__ import annotations

import importlib
import logging
import os
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

import defusedxml
from PIL import Image

from ..rate_limited_log import LineBudget

# ``defuse_stdlib`` swaps the standard-library XML parsers (``xml.etree``,
# ``xml.sax``, ``xml.dom.*``, ``xml.parsers.expat``, ``xmlrpc.client``)
# for hardened equivalents that reject billion-laughs / external-entity /
# DTD-of-doom payloads. python-docx and openpyxl ultimately decode their
# zip members through libraries that may reach into stdlib XML; calling
# this once at module import time covers that path. The lxml-direct
# paths inside python-docx already disable entity resolution at parser
# construction; openpyxl uses ``defusedxml.ElementTree`` when available
# (which adding the dep here also enables).
defusedxml.defuse_stdlib()


# Process-wide cap for ALL PIL consumers — the standalone image
# extractor here AND any transitive PIL user (pypdf rendering embedded
# document images, openpyxl chart graphics, etc.). Set at module import
# time so the cap applies uniformly the first time any indexer code
# path reaches PIL.
#
# 30 Mpx is comfortably above any legitimate attachment image (a
# 300 DPI letter page is ~8 Mpx; a 600 DPI A3 is ~24 Mpx; a modern
# phone shot is ~12 Mpx) and well below the host-pressure threshold
# (decoding a 30 Mpx RGB image is ~90 MB of pixel buffer). PIL's
# default limit (~89 Mpx) is a HINT — it emits a
# ``DecompressionBombWarning`` and otherwise lets processing proceed —
# and was observed during a real indexer backfill to admit a 94 Mpx
# image embedded in a marketing PDF, contributing to OOM kills.
# Lowering the cap converts those into a hard rejection.
GLOBAL_MAX_IMAGE_PIXELS = 30_000_000
Image.MAX_IMAGE_PIXELS = GLOBAL_MAX_IMAGE_PIXELS

log = logging.getLogger("indexer.extractor")

# Cap the *uncompressed* size of any zip-based attachment (DOCX / XLSX).
# The dispatcher's ``max_bytes`` already bounds the on-disk payload, but
# a 1 MB workbook can decompress to multi-GB of XML (zip bomb). Reject
# anything whose declared uncompressed size exceeds this cap before
# python-docx / openpyxl get a chance to expand it. 200 MB covers any
# realistic spreadsheet while keeping memory bounded.
ZIP_MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024

# Counts the indexer reports in its periodic attachments aggregate
# (#871), per extraction attempt, since the last drain:
#
# * ``pdf_pages_failed``: PDF pages whose text layer pypdf could not read.
#   The PDF extractor skips such a page (one DEBUG line each) and counts
#   it here, so a parser regression is visible without a line per page.
# * ``pdf_pages_unrecovered``: those of them whose text was never
#   recovered (OCR off, a digital return before OCR, OCR reading nothing,
#   or the OCR cap leaving the page unread).
# * ``ocr_capped_pdfs`` / ``ocr_pages_skipped``: scanned PDFs whose OCR
#   stopped at ``max_ocr_pages``, and the scanned pages left unread.
# * ``ocr_capped_images``: multipage images (TIFF) whose OCR stopped at
#   ``max_ocr_pages`` with a frame left unread (#885). Their unread
#   frames are not counted: the image extractor seeks one frame past
#   the cap rather than walk the whole frame chain.
# * ``extractor_caps``: caps inside an extractor that cut what it
#   returned (#903), one per cap per extraction attempt: the dispatcher's
#   ``max_extracted_chars``, the PDF digital-page cap, the OCR DPI
#   lowered to fit the page-pixel budget, the XLSX node, cell and
#   text budgets, the catdoc output byte cap, and the XLS sheet, cell
#   and text budgets (#935). Each also logs a rate-limited WARNING
#   naming the cap.
# * ``parser_caps_messages``: messages a parser work cap cut (#872),
#   counted here so the parser's per-message line can be rate limited
#   without losing a message (review round 5 on #884).
# * ``warnings_suppressed``: per-item WARNINGs (failed extraction, OCR
#   cap, OCR fallback failure, extractor cap, parser cap) that the rate
#   limit below withheld (the budget's ``attachment`` bucket).
# * the budget's ``line`` bucket: the repeated indexer lines that share
#   the rate limit (embed retries and recoveries, health-file and
#   ingestion-state failures, #873), withheld. Counted apart from the
#   attachment WARNINGs, and reported on the queue heartbeat as
#   ``suppressed_lines``, because a suppressed embed line says nothing
#   about attachment text (Codex round 2 on #904).
#
# Kept in this always-imported module because ``pdf`` is imported lazily.
# A few integers and a window start: the state stays bounded.
_counts_lock = Lock()
_pdf_pages_failed = 0
_pdf_pages_unrecovered = 0
_ocr_capped_pdfs = 0
_ocr_pages_skipped = 0
_ocr_capped_images = 0
_extractor_caps = 0
_parser_caps_messages = 0

# At most this many per-attachment WARNINGs per window, shared by every
# kind (review rounds 1 and 2 on #884): a sender can attach many distinct
# malformed or over-long files, and one line each could flood the
# retained log. The rest are counted.
_WARNINGS_PER_WINDOW = 20
_WARNING_WINDOW_SECS = 300.0
_ATTACHMENT_LINES = "attachment"
_OTHER_LINES = "line"
_LINE_BUDGET = LineBudget(
    limit=_WARNINGS_PER_WINDOW,
    window_secs=_WARNING_WINDOW_SECS,
    buckets=(_ATTACHMENT_LINES, _OTHER_LINES),
)


def note_pdf_page_failed() -> None:
    """Count one PDF page whose text layer could not be read."""
    global _pdf_pages_failed
    with _counts_lock:
        _pdf_pages_failed += 1


def note_pdf_pages_unrecovered(pages: int) -> None:
    """Count PDF pages pypdf could not read whose text OCR never recovered."""
    global _pdf_pages_unrecovered
    with _counts_lock:
        _pdf_pages_unrecovered += pages


def note_ocr_capped(pages_skipped: int) -> None:
    """Count one PDF whose OCR stopped at the page cap, and its unread
    scanned pages."""
    global _ocr_capped_pdfs, _ocr_pages_skipped
    with _counts_lock:
        _ocr_capped_pdfs += 1
        _ocr_pages_skipped += pages_skipped


def note_ocr_capped_image() -> None:
    """Count one multipage image whose OCR stopped at the page cap."""
    global _ocr_capped_images
    with _counts_lock:
        _ocr_capped_images += 1


def warn_extractor_cap(logger: logging.Logger, cap: str, msg: str, *args: object) -> None:
    """Count one extraction attempt that ``cap`` (a fixed name) cut, and
    log ``msg`` after the cap name at WARNING, rate limited. ``args`` must
    be counts or fixed text, as for ``warn_rate_limited``."""
    global _extractor_caps
    with _counts_lock:
        _extractor_caps += 1
    warn_rate_limited(logger, "extractor cap %s: " + msg, cap, *args)


def note_parser_caps_message() -> None:
    """Count one message a parser work cap cut."""
    global _parser_caps_messages
    with _counts_lock:
        _parser_caps_messages += 1


def drain_extractor_counts() -> dict[str, int]:
    """Return the counts above since the last call, and reset them."""
    global _pdf_pages_failed, _pdf_pages_unrecovered, _ocr_capped_pdfs
    global _ocr_pages_skipped, _ocr_capped_images, _extractor_caps, _parser_caps_messages
    with _counts_lock:
        counts = {
            "pdf_pages_failed": _pdf_pages_failed,
            "pdf_pages_unrecovered": _pdf_pages_unrecovered,
            "ocr_capped_pdfs": _ocr_capped_pdfs,
            "ocr_pages_skipped": _ocr_pages_skipped,
            "ocr_capped_images": _ocr_capped_images,
            "extractor_caps": _extractor_caps,
            "parser_caps_messages": _parser_caps_messages,
            "warnings_suppressed": _LINE_BUDGET.drain(_ATTACHMENT_LINES),
        }
        _pdf_pages_failed = _pdf_pages_unrecovered = _ocr_capped_pdfs = 0
        _ocr_pages_skipped = _ocr_capped_images = _extractor_caps = 0
        _parser_caps_messages = 0
    return counts


def drain_suppressed_lines() -> int:
    """Return the non-attachment lines withheld since the last call, and
    reset the count (reported on the queue heartbeat)."""
    return _LINE_BUDGET.drain(_OTHER_LINES)


def warn_rate_limited(
    logger: logging.Logger,
    msg: str,
    *args: object,
    level: int = logging.WARNING,
    attachment: bool = True,
) -> bool:
    """Log one repeated line (a WARNING unless ``level`` says otherwise)
    unless this window's budget is spent; then count it as suppressed:
    in ``warnings_suppressed`` for an attachment line (the default), or
    in the heartbeat's ``suppressed_lines`` for any other indexer line
    (``attachment=False``). ``args`` must be counts, module names, type
    names or fixed text. Returns whether the line was logged."""
    bucket = _ATTACHMENT_LINES if attachment else _OTHER_LINES
    return _LINE_BUDGET.log(logger, bucket, msg, *args, level=level)


def _warn_failed(module_name: str, dispatch_via: str, reason: str) -> None:
    """Log a failed extraction at WARNING (it drops the attachment out of
    search), rate limited. ``reason`` is an exception type name or fixed
    text."""
    warn_rate_limited(
        log, "extractor %s failed (dispatch_via=%s): %s", module_name, dispatch_via, reason
    )


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome of one extraction attempt against an attachment payload.

    ``status`` is one of:

    * ``"success"`` — non-empty text extracted; ``text`` populated.
    * ``"empty"`` — extractor ran cleanly but the document had no text
      to extract (truly empty page, image of a blank surface, etc.).
    * ``"unsupported"`` — no extractor registered for this MIME type
      *or* the format's optional dependency is missing in this image.
    * ``"too_large"`` — payload exceeded ``max_bytes``.
    * ``"failed"`` — extractor raised; ``error`` records the exception
      type only, since its message can quote the document. Indexer
      treats this as terminal for the attachment (won't keep
      retrying), but a future re-extraction sweep can re-run after a
      library upgrade.
    """

    status: str
    extractor: str | None
    text: str | None
    error: str | None


# Version of each extractor module whose output changed for the same
# bytes, keyed by dispatch module (``_MIME_DISPATCH`` values). The
# dispatcher stamps the recorded extractor name (``docx@2``; the image
# module's ``image-ocr@2``) into the cache, and a row stamped with an
# older version is re-extracted instead of being served forever;
# ``main._requeue_stale_extractions`` also re-queues the messages that
# carry it at startup. Bump a module's version whenever a fix changes
# what it returns. Names a module records are the module name, or the
# module name plus a ``-suffix`` (``pdf-ocr``), so a name maps back to
# its module.
#
# docx 2: reads each cell once, nested tables, and header/footer tables
# (#226, #228).
# docx 3: first-page and even-page headers and footers (#299).
# image 2: OCRs every page of a multipage TIFF (#231).
# text 2: decodes UTF-16 / UTF-32 by BOM and BOM-less UTF-16 (#234).
# xlsx 2: stops at a text budget instead of expanding every shared-string
# reference (#294); keeps empty cells' column positions (#296); reads
# cells outside a stale worksheet dimension (#305).
# xlsx 3: cuts worksheets at an XML node budget before openpyxl parses
# them (#432).
# xlsx 4: fails a workbook whose parts openpyxl loads whole are over
# their caps (#428).
# pdf 3: opens AES-encrypted PDFs that need no open password, which
# failed with ``DependencyError`` before ``cryptography`` was added (#691).
# image 3: opens HEIC/HEIF photos, which failed with
# ``UnidentifiedImageError`` before pillow-heif was added (#691).
# pdf 4: lets ``MemoryError`` / ``RecursionError`` on a page escape as
# host pressure; before, the page was skipped and the PDF could be
# cached as a success with that page's text missing (#707).
# docx 4, xlsx 5: an OLE2 payload (a real ``.doc`` / ``.xls``, or an
# encrypted OOXML file) is recorded ``unsupported`` instead of ``failed``, so the
# ``failed`` rows the previous versions wrote for one are refreshed (#694).
# docx 5: reads Word templates (``.dotx``), which ``docx.Document``
# refused, so a template labelled ``.docx`` failed (#937).
# text 3: a payload starting with a fixed binary signature is recorded
# ``unsupported`` instead of decoded as replacement characters, so the
# ``success`` rows the previous version wrote for one are refreshed (#932).
# doc 1, xls 1: legacy binary ``.doc`` (catdoc) and ``.xls`` (xlrd in a
# child process), recorded ``unsupported`` before (#935).
EXTRACTOR_VERSIONS: dict[str, int] = {
    "doc": 1,
    "docx": 5,
    "image": 3,
    "pdf": 4,
    "text": 3,
    "xls": 1,
    "xlsx": 5,
}


def _stamp_extractor(module_name: str, extractor_name: str) -> str:
    """Append the module's version to the name recorded in the cache."""
    version = EXTRACTOR_VERSIONS.get(module_name)
    return f"{extractor_name}@{version}" if version is not None else extractor_name


def _extractor_module(name: str) -> str:
    """``pdf-ocr@3`` -> ``pdf``."""
    return name.partition("@")[0].partition("-")[0]


def _extractor_version(name: str) -> int:
    """``docx@2`` -> 2. Names written before versioning count as 1."""
    _, sep, version = name.partition("@")
    return int(version) if sep and version.isdigit() else 1


def is_stale_extractor(name: str | None, *, ocr_enabled: bool = True) -> bool:
    """True when ``name`` was recorded by an older version of its module.

    Only older: after a rollback, rows a newer release wrote are kept
    rather than downgraded by the older code.
    """
    return stale_extractor_module(name, ocr_enabled=ocr_enabled) is not None


def stale_extractor_module(name: str | None, *, ocr_enabled: bool = True) -> str | None:
    """The module that recorded ``name`` when that was an older version
    of it, else ``None``. A stale row is refreshed by re-running this
    module from any occurrence of the same bytes: the cache is shared by
    content hash, so the same file attached as ``.bin`` must re-run the
    DOCX extractor rather than its own (none) and overwrite the row.

    A row an OCR extractor wrote (``image-ocr``, ``pdf-ocr``) is not
    stale while OCR is off: the refresh could only record "OCR disabled"
    over its text, so it is kept until OCR is turned back on.
    """
    if not name:
        return None
    if not ocr_enabled and name.partition("@")[0].endswith("-ocr"):
        return None
    module = _extractor_module(name)
    current = EXTRACTOR_VERSIONS.get(module)
    if current is None or _extractor_version(name) >= current:
        return None
    return module


# Public statuses are exposed as constants so callers can compare without
# typo-prone string literals scattered across the codebase.
STATUS_SUCCESS = "success"
STATUS_EMPTY = "empty"
STATUS_UNSUPPORTED = "unsupported"
STATUS_TOO_LARGE = "too_large"
STATUS_FAILED = "failed"

# ``unsupported`` errors for input that needs OCR while it is off. A scanned
# PDF's row is marked apart from an image's: the PDF extractor also reads a
# digital text layer without OCR, so only its own row says the bytes have
# none.
OCR_DISABLED_ERROR = "OCR disabled (INDEXER_OCR_ENABLED=false)"
SCANNED_PDF_OCR_DISABLED_ERROR = f"{OCR_DISABLED_ERROR}; scanned PDF"

# ``unsupported`` error when neither the MIME type nor the filename
# extension selects an extractor. Both are sender-supplied, so the
# persisted text names neither (#257).
NO_EXTRACTOR_ERROR = "no extractor for this content type or filename extension"

# ``unsupported`` error for a payload bound for the DOCX or XLSX extractor
# that is an OLE2 compound file, which neither OOXML extractor can read
# (#694), when the occurrence's label selects no legacy extractor
# (#935): a password-protected OOXML package, or a legacy file labelled
# as OOXML. The row is re-run for an occurrence whose label selects the
# ``doc`` or ``xls`` extractor (``attachment_indexing``).
LEGACY_OLE2_ERROR = "OLE2 compound file (legacy .doc / .xls or encrypted Office file)"

# The fixed 8-byte signature every OLE2 compound file starts with.
_OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# ``unsupported`` error for a payload bound for the text extractor that
# starts with one of ``_BINARY_SIGNATURES`` (#932): decoding it would only
# index replacement characters. Decided by the bytes alone, like the OLE2
# check.
BINARY_AS_TEXT_ERROR = "binary payload labelled as text"

# Fixed prefixes of binary formats senders mislabel as text: PDF, ZIP
# (including OOXML), OLE2, PNG, JPEG and GIF. A prefix list only; no
# sniffing beyond it.
_BINARY_SIGNATURES = (
    b"%PDF-",
    b"PK\x03\x04",
    _OLE2_SIGNATURE,
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
)


# Maps normalized MIME -> per-format extractor module name (under
# ``indexer.extractors``). The module is imported lazily so a missing
# optional dependency doesn't break the indexer at startup.
_MIME_DISPATCH: dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    # Word templates (``.dotx``): the DOCX extractor loads the template
    # main part (#937).
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template": "docx",
    # Legacy ``.doc`` / ``.xls`` labels select the legacy extractors for
    # an OLE2 payload, and the OOXML ones otherwise, a best effort for
    # OOXML files mislabelled as a legacy type (``_route_container``,
    # #935).
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/plain": "text",
    "text/csv": "text",
    "text/markdown": "text",
}

# Filename-extension fallback for cases where Content-Type is missing,
# is ``application/octet-stream``, or otherwise unhelpful — common for
# attachments forwarded from clients that strip MIME hints.
_EXT_DISPATCH: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".dotx": "docx",
    ".doc": "doc",
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".txt": "text",
    ".csv": "text",
    ".md": "text",
    ".markdown": "text",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".tif": "image",
    ".tiff": "image",
    ".bmp": "image",
    ".webp": "image",
    ".gif": "image",
    # Still HEIF images; pillow-heif also registers the sequence
    # extensions ``.heics`` / ``.heifs``, which are not routed by name.
    ".heic": "image",
    ".heif": "image",
    ".hif": "image",
}

# Image MIME types are routed to the image extractor unless OCR is
# disabled (in which case we report ``unsupported`` so the cached row
# can be upgraded later if the operator flips the switch).
_IMAGE_MIME_PREFIX = "image/"

# Default payload cap, shared with the indexer's
# ``INDEXER_ATTACHMENT_MAX_BYTES`` default (``src/main.py``).
DEFAULT_MAX_BYTES = 32 * 1024 * 1024


def extract(
    *,
    content_type: str,
    filename: str,
    payload: bytes,
    ocr_enabled: bool = True,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_ocr_pages: int = 20,
    max_extracted_chars: int | None = None,
    ocr_timeout_seconds: float | None = None,
    max_pdf_pages: int | None = None,
    module_override: str | None = None,
    on_progress: Callable[[], None] | None = None,
) -> ExtractionResult:
    """Run text extraction for one attachment payload.

    Returns an ``ExtractionResult`` regardless of outcome — the
    dispatcher converts every exception inside a per-format extractor
    into a ``failed`` result so a single malformed attachment cannot
    dead-letter the parent message. The caller is expected to persist
    the result via ``Database.store_attachment_extraction``.

    ``max_extracted_chars`` (when supplied) caps the length of
    extracted text that is returned and persisted in the cache. A
    multi-hundred-page OCR'd PDF can otherwise produce megabytes of
    text and bloat the ``attachment_extractions`` table well past the
    payload's on-disk size. ``None`` means no cap.

    ``module_override`` runs that extractor module instead of the one the
    metadata resolves to; used to refresh a stale cache row (see
    ``stale_extractor_module``).

    ``on_progress`` (when supplied) is called after each page an
    extractor reads (the PDF and image extractors), so the indexer can
    refresh its heartbeat through a long OCR (#485).
    """
    if len(payload) > max_bytes:
        return ExtractionResult(
            status=STATUS_TOO_LARGE,
            extractor=None,
            text=None,
            error=f"payload {len(payload)} bytes exceeds cap {max_bytes}",
        )

    module_name: str | None
    if module_override is not None:
        module_name, dispatch_via = module_override, "cache-refresh"
    else:
        module_name, dispatch_via = _resolve_extractor(content_type, filename)

    # Image types are gated by ``ocr_enabled`` because the only sensible
    # extractor is Tesseract. Disabling OCR globally should cleanly
    # downgrade them to ``unsupported`` rather than failing per-call.
    if module_name == "image" and not ocr_enabled:
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=None,
            text=None,
            error=OCR_DISABLED_ERROR,
        )

    if module_name is None:
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=None,
            text=None,
            error=NO_EXTRACTOR_ERROR,
        )

    # The container decides between a legacy and an OOXML extractor
    # (#694, #935): a constant-size prefix check; the aggregate counts an
    # unsupported result, so no per-item line.
    module_name = _route_container(module_name, payload, content_type, filename)
    if module_name is None:
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=None,
            text=None,
            error=LEGACY_OLE2_ERROR,
        )

    # A binary payload labelled as text (#932): the same constant-size
    # prefix check, also decided by the bytes alone (including a
    # ``module_override`` refresh) and counted by the aggregate.
    if module_name == "text" and payload.startswith(_BINARY_SIGNATURES):
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=None,
            text=None,
            error=BINARY_AS_TEXT_ERROR,
        )

    extractor_fn = _safe_import(module_name)
    if extractor_fn is None:
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=None,
            text=None,
            error=f"extractor module {module_name!r} not importable in this image",
        )

    # Zip-based formats (DOCX, XLSX) need a zip-bomb pre-check: the
    # ``max_bytes`` cap above only bounds the compressed payload; a
    # malicious workbook can declare 200× expansion in its central
    # directory. Reject before handing to lxml.
    if module_name in {"docx", "xlsx"}:
        zip_error = _validate_zip_payload(payload)
        if zip_error is not None:
            # A ``failed`` row drops the attachment out of search, so it
            # is visible at WARNING (#871); fixed text, as the error
            # names only sizes.
            _warn_failed(module_name, dispatch_via, "zip uncompressed-size cap exceeded")
            return ExtractionResult(
                status=STATUS_FAILED,
                extractor=_stamp_extractor(module_name, module_name),
                text=None,
                error=zip_error,
            )

    try:
        text, extractor_name = extractor_fn(
            payload,
            ocr_enabled=ocr_enabled,
            max_ocr_pages=max_ocr_pages,
            ocr_timeout_seconds=ocr_timeout_seconds,
            max_pdf_pages=max_pdf_pages,
            on_progress=on_progress,
        )
    except MemoryError, RecursionError:
        # Resource-exhaustion errors are not "the extractor failed on
        # this payload" — they are "the runtime is in trouble". Letting
        # them bubble surfaces the host-level pressure (a zip-bomb
        # attachment that decompressed past the indexer container's
        # memory ceiling) instead of caching a misleading ``failed``
        # row that retries on every reappearance of the same payload.
        raise
    except Exception as exc:  # noqa: BLE001 — see comment below
        # Per-payload extractor errors (broken PDFs, malformed DOCX,
        # missing optional deps that slipped past _safe_import) become
        # ``failed`` rows so a single bad attachment cannot dead-letter
        # the parent message. ``MemoryError`` / ``RecursionError`` are
        # excluded above precisely because they are not per-payload.
        # Parser exceptions quote the document (text, member names), so
        # only the type is logged and persisted (#257). WARNING, since
        # the attachment drops out of search (#871), rate limited.
        _warn_failed(module_name, dispatch_via, type(exc).__name__)
        return ExtractionResult(
            status=STATUS_FAILED,
            extractor=_stamp_extractor(module_name, module_name),
            text=None,
            error=type(exc).__name__,
        )

    if extractor_name == "pdf-ocr-disabled":
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=None,
            text=None,
            error=SCANNED_PDF_OCR_DISABLED_ERROR,
        )

    extractor_name = _stamp_extractor(module_name, extractor_name)
    cleaned = (text or "").strip()
    if not cleaned:
        return ExtractionResult(
            status=STATUS_EMPTY,
            extractor=extractor_name,
            text=None,
            error=None,
        )
    if max_extracted_chars is not None and len(cleaned) > max_extracted_chars:
        # Text past the cap is not indexed: WARNING, rate limited (#903).
        warn_extractor_cap(
            log,
            "extracted_chars",
            "%s output truncated from %d to %d chars",
            extractor_name,
            len(cleaned),
            max_extracted_chars,
        )
        cleaned = cleaned[:max_extracted_chars]
    return ExtractionResult(
        status=STATUS_SUCCESS,
        extractor=extractor_name,
        text=cleaned,
        error=None,
    )


# The legacy (OLE2) extractor for a legacy label, and the OOXML one the
# same label selects for a payload that is not OLE2.
_LEGACY_TO_OOXML = {"doc": "docx", "xls": "xlsx"}


def _route_container(
    module_name: str, payload: bytes, content_type: str, filename: str
) -> str | None:
    """The extractor for ``payload`` once its container is known, or
    ``None`` when it is an OLE2 file no extractor reads.

    * A legacy label (``doc``, ``xls``) keeps its legacy extractor for an
      OLE2 payload; any other payload goes to the OOXML extractor, a
      best effort for an OOXML file mislabelled as a legacy type.
    * An OLE2 payload bound for an OOXML extractor goes to the legacy
      extractor this occurrence's own label selects. This covers a
      ``module_override`` refresh of a stale DOCX / XLSX row from a
      ``.doc`` / ``.xls`` occurrence. With no legacy label (an encrypted
      OOXML file is OLE2 too, or the label says ``.docx``), it is
      ``None``: neither OOXML extractor can read OLE2, and an attempt
      would only record ``failed`` and re-run every
      ``_FAILED_CACHE_MAX_AGE`` (#694).
    * Anything else is unchanged.
    """
    ole2 = payload.startswith(_OLE2_SIGNATURE)
    if module_name in _LEGACY_TO_OOXML:
        return module_name if ole2 else _LEGACY_TO_OOXML[module_name]
    if ole2 and module_name in _LEGACY_TO_OOXML.values():
        labelled = _resolve_extractor(content_type, filename)[0]
        return labelled if labelled in _LEGACY_TO_OOXML else None
    return module_name


def resolved_extractor_module(content_type: str, filename: str) -> str | None:
    """The extractor module this metadata selects, or ``None``. Dispatch
    reads the MIME type and filename, not the bytes, so the same bytes
    can be unsupported under one occurrence and extractable under
    another."""
    return _resolve_extractor(content_type, filename)[0]


def _resolve_extractor(content_type: str, filename: str) -> tuple[str | None, str]:
    """Return (module_name, dispatch_via) for an attachment.

    ``dispatch_via`` is just for diagnostics so a confusing dispatch
    can be traced back to "MIME header said X" vs. "filename extension
    was Y". Order:

    1. Direct MIME match against ``_MIME_DISPATCH``.
    2. ``image/*`` routes to the image extractor when OCR is enabled
       (gating happens upstream so the resolver itself can stay pure).
    3. Filename extension fallback against ``_EXT_DISPATCH``.
    """
    normalized_mime = (content_type or "").lower().split(";", 1)[0].strip()
    if normalized_mime in _MIME_DISPATCH:
        return _MIME_DISPATCH[normalized_mime], "mime"
    if normalized_mime.startswith(_IMAGE_MIME_PREFIX):
        return "image", "mime-image"
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _EXT_DISPATCH:
        return _EXT_DISPATCH[ext], "extension"
    return None, "none"


def _validate_zip_payload(payload: bytes) -> str | None:
    """Return an error string when ``payload`` looks like a zip bomb, else ``None``.

    Walks the central directory and rejects the archive when:

    * the file is not a valid zip (let the per-format extractor surface
      that as ``failed`` for a clearer error string),
    * any single member declares an uncompressed size above the cap, or
    * the sum of declared uncompressed sizes exceeds the cap.

    Reading ``ZipInfo.file_size`` does not decompress anything — it just
    parses the central directory header — so this check is cheap and
    runs before lxml gets involved.
    """
    try:
        import io

        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            total = 0
            for info in zf.infolist():
                if info.file_size > ZIP_MAX_UNCOMPRESSED_BYTES:
                    # The member name is attacker-chosen; report sizes only.
                    return (
                        f"zip member declares {info.file_size} uncompressed "
                        f"bytes (cap {ZIP_MAX_UNCOMPRESSED_BYTES})"
                    )
                total += info.file_size
                if total > ZIP_MAX_UNCOMPRESSED_BYTES:
                    return f"zip total uncompressed size exceeds cap {ZIP_MAX_UNCOMPRESSED_BYTES}"
    except zipfile.BadZipFile:
        # Not a zip — let the format-specific extractor produce a more
        # informative failure (e.g. python-docx's ``BadZipFile``).
        return None
    return None


_IMPORT_CACHE: dict[str, Callable[..., tuple[str, str]] | None] = {}


def _safe_import(module_name: str) -> Callable[..., tuple[str, str]] | None:
    """Lazy-import a per-format extractor, caching the result.

    Each extractor module exposes ``extract(payload, **opts) -> (text, name)``.
    A missing optional dependency (the module's ``import`` raising
    ``ImportError`` for one of *its* imports) is downgraded to ``None``
    here so callers see it as ``unsupported`` rather than a hard failure.
    Cached so repeated attachments of the same MIME type do not re-pay
    the import cost.

    Importing relative to the current package keeps this resilient to
    any future package rename. The actual ``ImportError`` is logged so
    a missing optional dependency surfaces with the offending module
    name rather than just "extractor X not importable".
    """
    if module_name in _IMPORT_CACHE:
        return _IMPORT_CACHE[module_name]
    try:
        module = importlib.import_module(f".{module_name}", package=__package__)
    except ImportError as exc:
        log.warning(
            "extractor %s unavailable (missing dependency %r): %s",
            module_name,
            exc.name or "<unknown>",
            exc,
        )
        _IMPORT_CACHE[module_name] = None
        return None
    fn = getattr(module, "extract", None)
    _IMPORT_CACHE[module_name] = fn
    return fn
