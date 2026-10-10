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

import functools
import importlib
import logging
import os
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from threading import Lock, local

import defusedxml
from PIL import Image

from ..rate_limited_log import LineBudget
from ._runner import ChildError

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

# Cap the *uncompressed* size of any zip-based attachment (DOCX / XLSX / PPTX).
# The dispatcher's ``max_bytes`` already bounds the on-disk payload, but
# a 1 MB workbook can decompress to multi-GB of XML (zip bomb). Reject
# anything whose declared uncompressed size exceeds this cap before
# python-docx / openpyxl / python-pptx get a chance to expand it. 200 MB
# covers any realistic spreadsheet while keeping memory bounded.
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
#   ``attachment_indexing.record_committed_outcomes`` adds the capped
#   PDFs served from the cache or the batch, per committed occurrence
#   (#891).
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
#   ingestion-state failures, #873, and the parser's repeated-header
#   lines: merged To/Cc and ambiguous From, #1144, and the queue's
#   per-message terminal, retry and dead-letter lines, #1320), withheld. Counted
#   apart from the attachment WARNINGs, and reported on the queue
#   heartbeat as ``suppressed_lines``, because a suppressed embed or
#   repeated-header line says nothing about attachment text (Codex
#   round 2 on #904, round 3 on #1158).
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
_parser_recipients_merged_messages = 0
_parser_sender_ambiguous_messages = 0
# Decoding fallbacks in an attached email's text (#922): a header's
# (an unknown charset, raw 8-bit bytes that are not UTF-8, encoded-words
# kept as sent), a part filename's, and a body text part's charset (an
# unknown label, or bytes it replaced). They replace characters rather
# than drop text, so they do not mark the text incomplete (#1315 decides
# that for every surface).
_eml_headers_degraded = 0
_eml_filenames_degraded = 0
_eml_charsets_degraded = 0

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
    if pages:
        note_text_lost()


def note_ocr_capped(pages_skipped: int) -> None:
    """Count one PDF whose OCR stopped at the page cap, and its unread
    scanned pages."""
    global _ocr_capped_pdfs, _ocr_pages_skipped
    with _counts_lock:
        _ocr_capped_pdfs += 1
        _ocr_pages_skipped += pages_skipped
    note_text_lost()


# The pages the PDF OCR cap skipped in the extraction running on this
# thread (#891), and whether any text was lost in it (#1242): ``extract``
# sets them before an extractor runs and reads them after, so they land
# on the result without changing every extractor's return shape.
_attempt = local()


def note_text_lost() -> None:
    """Record that the extraction running on this thread lost text: a
    cap, a page no reader recovered, or a scanned page left unread
    (#1242). Its result then never certifies that the attachment's text
    is complete (``ExtractionResult.text_complete``). Every count and cap
    warning above calls it; an extractor calls it directly for a loss
    with no count of its own."""
    _attempt.text_lost = True


def record_ocr_pages_skipped(pages_skipped: int) -> None:
    """Record on the running extraction's result the scanned pages the
    PDF OCR cap left unread."""
    _attempt.ocr_pages_skipped = pages_skipped


# The configured limits that cut the running extraction (#1418), one per
# setting: ``INDEXER_OCR_MAX_PAGES`` (PDF pages OCR'd, TIFF frames),
# ``INDEXER_PDF_MAX_DIGITAL_PAGES`` and
# ``INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS``. ``extract`` sets each to 0
# (the limit in effect did not cut) for the modules it applies to, or
# ``None`` (not applicable), before the extractor runs; the extractor
# records the limit's value when it cuts. A hardcoded limit (an image's
# text budget, a raw tool's output ceiling, a page-pixel budget) records
# nothing: raising a setting cannot lift it, and a fix to it is an
# ``EXTRACTOR_VERSIONS`` bump. Kept on the cached row, so a result cut
# by a limit the operator has since raised is extracted again
# (``attachment_indexing.cap_raised``).
CAP_OCR_PAGES = "ocr_pages_cap"
CAP_DIGITAL_PAGES = "digital_pages_cap"
CAP_EXTRACTED_CHARS = "extracted_chars_cap"
CAP_COLUMNS = (CAP_OCR_PAGES, CAP_DIGITAL_PAGES, CAP_EXTRACTED_CHARS)


def record_cap_cut(cap: str, limit: int) -> None:
    """Record on the running extraction's result that the configured
    ``limit`` of ``cap`` (one of ``CAP_COLUMNS``) cut it."""
    setattr(_attempt, cap, limit)


def note_ocr_capped_image() -> None:
    """Count one multipage image whose OCR stopped at the page cap."""
    global _ocr_capped_images
    with _counts_lock:
        _ocr_capped_images += 1
    note_text_lost()


def warn_extractor_cap(logger: logging.Logger, cap: str, msg: str, *args: object) -> None:
    """Count one extraction attempt that ``cap`` (a fixed name) cut, and
    log ``msg`` after the cap name at WARNING, rate limited. ``args`` must
    be counts or fixed text, as for ``warn_rate_limited``."""
    global _extractor_caps
    with _counts_lock:
        _extractor_caps += 1
    note_text_lost()
    warn_rate_limited(logger, "extractor cap %s: " + msg, cap, *args)


def note_parser_caps_message() -> None:
    """Count one message a parser work cap cut."""
    global _parser_caps_messages
    with _counts_lock:
        _parser_caps_messages += 1


def note_parser_address_repeats(*, merged: bool, ambiguous: bool) -> None:
    """Count one message whose repeated To / Cc headers were merged
    (``merged``), and one whose sender is ambiguous (``ambiguous``),
    for the aggregate line (#1144)."""
    global _parser_recipients_merged_messages, _parser_sender_ambiguous_messages
    with _counts_lock:
        _parser_recipients_merged_messages += int(merged)
        _parser_sender_ambiguous_messages += int(ambiguous)


def note_eml_degraded(*, headers: int, filenames: int, charsets: int) -> None:
    """Count the decoding fallbacks in an attached email's text (#922):
    in its headers, its part filenames and its body charsets. Not a text
    loss (#1315)."""
    global _eml_headers_degraded, _eml_filenames_degraded, _eml_charsets_degraded
    with _counts_lock:
        _eml_headers_degraded += headers
        _eml_filenames_degraded += filenames
        _eml_charsets_degraded += charsets


def drain_extractor_counts() -> dict[str, int]:
    """Return the counts above since the last call, and reset them."""
    with _counts_lock:
        counts = _drain_counters()
        counts["warnings_suppressed"] = _LINE_BUDGET.drain(_ATTACHMENT_LINES)
    return counts


def _drain_counters() -> dict[str, int]:
    """The counters above (not the suppressed lines), reset. The caller
    holds ``_counts_lock``."""
    global _pdf_pages_failed, _pdf_pages_unrecovered, _ocr_capped_pdfs
    global _ocr_pages_skipped, _ocr_capped_images, _extractor_caps, _parser_caps_messages
    global _parser_recipients_merged_messages, _parser_sender_ambiguous_messages
    global _eml_headers_degraded, _eml_filenames_degraded, _eml_charsets_degraded
    counts = {
        "pdf_pages_failed": _pdf_pages_failed,
        "pdf_pages_unrecovered": _pdf_pages_unrecovered,
        "ocr_capped_pdfs": _ocr_capped_pdfs,
        "ocr_pages_skipped": _ocr_pages_skipped,
        "ocr_capped_images": _ocr_capped_images,
        "extractor_caps": _extractor_caps,
        "parser_caps_messages": _parser_caps_messages,
        "parser_recipients_merged_messages": _parser_recipients_merged_messages,
        "parser_sender_ambiguous_messages": _parser_sender_ambiguous_messages,
        "eml_headers_degraded": _eml_headers_degraded,
        "eml_filenames_degraded": _eml_filenames_degraded,
        "eml_charsets_degraded": _eml_charsets_degraded,
    }
    _pdf_pages_failed = _pdf_pages_unrecovered = _ocr_capped_pdfs = 0
    _ocr_pages_skipped = _ocr_capped_images = _extractor_caps = 0
    _parser_caps_messages = 0
    _parser_recipients_merged_messages = _parser_sender_ambiguous_messages = 0
    _eml_headers_degraded = _eml_filenames_degraded = _eml_charsets_degraded = 0
    return counts


def add_counters(counts: Mapping[str, int]) -> None:
    """Add ``counts`` (keys of ``_drain_counters``; others ignored) to
    the counters above."""
    global _pdf_pages_failed, _pdf_pages_unrecovered, _ocr_capped_pdfs
    global _ocr_pages_skipped, _ocr_capped_images, _extractor_caps, _parser_caps_messages
    global _parser_recipients_merged_messages, _parser_sender_ambiguous_messages
    global _eml_headers_degraded, _eml_filenames_degraded, _eml_charsets_degraded
    with _counts_lock:
        _pdf_pages_failed += counts.get("pdf_pages_failed", 0)
        _pdf_pages_unrecovered += counts.get("pdf_pages_unrecovered", 0)
        _ocr_capped_pdfs += counts.get("ocr_capped_pdfs", 0)
        _ocr_pages_skipped += counts.get("ocr_pages_skipped", 0)
        _ocr_capped_images += counts.get("ocr_capped_images", 0)
        _extractor_caps += counts.get("extractor_caps", 0)
        _parser_caps_messages += counts.get("parser_caps_messages", 0)
        _parser_recipients_merged_messages += counts.get("parser_recipients_merged_messages", 0)
        _parser_sender_ambiguous_messages += counts.get("parser_sender_ambiguous_messages", 0)
        _eml_headers_degraded += counts.get("eml_headers_degraded", 0)
        _eml_filenames_degraded += counts.get("eml_filenames_degraded", 0)
        _eml_charsets_degraded += counts.get("eml_charsets_degraded", 0)


def drain_counters() -> dict[str, int]:
    """The counters above since the last drain, reset; the suppressed
    lines are left in place."""
    with _counts_lock:
        return _drain_counters()


# The degradation an extraction in the extractor child records (#1314),
# which lives in the child's memory: the counters above, and the
# attempt's text loss (``note_text_lost``) and OCR pages skipped
# (``record_ocr_pages_skipped``) under the keys below. The child sends
# each that is not zero as an ``N <key> <count>`` frame
# (``child_degradation``) and the parent re-applies them
# (``apply_child_degradation``). Only these fixed keys and counts
# cross; never a log line, a format string or its arguments. The
# child's suppressed-line count is not sent: none of the child's lines
# reach the log (the runner discards its stderr), so the parent's own
# line below stands for them.
CHILD_TEXT_LOST = "text_lost"
CHILD_OCR_PAGES_SKIPPED = "result_ocr_pages_skipped"
# The factor (2, 4 or 8) by which the image child scaled down a JPEG over
# its pixel ceiling before OCR (``record_image_scale_factor``, #1401).
CHILD_IMAGE_SCALE_FACTOR = "image_scale_factor"
# The cap name the parent reports a scale-down under.
IMAGE_PIXEL_CEILING_CAP = "image_pixel_ceiling"
# The DPI the PDF child rendered pages for OCR at when the page-pixel
# budget lowered it (``record_pdf_ocr_dpi``, #1293); the parent reports
# it as an extractor cap.
CHILD_PDF_OCR_DPI = "pdf_ocr_dpi"
CHILD_DEGRADATION_KEYS = frozenset(
    {
        "pdf_pages_failed",
        "pdf_pages_unrecovered",
        "ocr_capped_pdfs",
        "ocr_pages_skipped",
        "ocr_capped_images",
        "extractor_caps",
        "parser_caps_messages",
        "parser_recipients_merged_messages",
        "parser_sender_ambiguous_messages",
        "eml_headers_degraded",
        "eml_filenames_degraded",
        "eml_charsets_degraded",
        CHILD_TEXT_LOST,
        CHILD_OCR_PAGES_SKIPPED,
        CHILD_IMAGE_SCALE_FACTOR,
        CHILD_PDF_OCR_DPI,
    }
)


def reset_attempt() -> None:
    """Clear the running extraction's text loss and OCR pages skipped,
    as the dispatcher does before an extractor runs, and what a child
    reports besides its counts; the child calls it before its
    extraction. The cap record (``CAP_COLUMNS``) is the parent's alone:
    the child reports a cap that cut by name and the parent records it."""
    _attempt.text_lost = False
    _attempt.ocr_pages_skipped = None
    _attempt.image_scale_factor = None
    _attempt.pdf_ocr_dpi = None
    _attempt.child_caps = []
    _attempt.recovered_errors = []


def report_cap(name: str) -> None:
    """In the extractor child: report that the cap ``name`` (one of the
    parent extractor's fixed cap names) cut the extraction. Sent as a
    ``C`` frame whether the extraction then returns or raises (#1293),
    so the parent logs and records it as the in-process extractor did
    where it happened."""
    caps: list[str] | None = getattr(_attempt, "child_caps", None)
    if caps is None:
        caps = _attempt.child_caps = []
    if name not in caps:
        caps.append(name)


def record_recovered_error(type_name: str) -> None:
    """In the extractor child: record that the extraction caught an
    exception of ``type_name`` and degraded instead of failing (the PDF
    OCR fallback, #1293). Sent as an ``R`` frame; the parent logs it.
    The type name only: the exception's message can quote the
    document."""
    errors: list[str] | None = getattr(_attempt, "recovered_errors", None)
    if errors is None:
        errors = _attempt.recovered_errors = []
    if type_name not in errors:
        errors.append(type_name)


def child_reports() -> tuple[list[str], list[str]]:
    """In the child, after the extraction (returned or raised): the caps
    it reported (``report_cap``) and the errors it recovered from
    (``record_recovered_error``)."""
    return (
        list(getattr(_attempt, "child_caps", [])),
        list(getattr(_attempt, "recovered_errors", [])),
    )


def record_pdf_ocr_dpi(dpi: int) -> None:
    """In the PDF child: record that pages were rendered for OCR at
    ``dpi``, lowered from the default to fit the page-pixel budget. The
    parent reports it as an extractor cap (``pdf._report``)."""
    _attempt.pdf_ocr_dpi = dpi


def record_image_scale_factor(factor: int) -> None:
    """In the image child: record that the image was decoded at
    1/``factor`` scale to fit the child's pixel ceiling (#1401). The
    parent reports it as an extractor cap (``apply_child_degradation``)."""
    _attempt.image_scale_factor = factor


def child_degradation() -> dict[str, int]:
    """In the child, after the extraction: the degradation it recorded,
    by ``CHILD_DEGRADATION_KEYS`` key, zero counts left out."""
    state = {key: n for key, n in drain_counters().items() if n}
    if getattr(_attempt, "text_lost", False):
        state[CHILD_TEXT_LOST] = 1
    skipped = getattr(_attempt, "ocr_pages_skipped", None)
    if skipped is not None:
        state[CHILD_OCR_PAGES_SKIPPED] = skipped
    factor = getattr(_attempt, "image_scale_factor", None)
    if factor is not None:
        state[CHILD_IMAGE_SCALE_FACTOR] = factor
    dpi = getattr(_attempt, "pdf_ocr_dpi", None)
    if dpi is not None:
        state[CHILD_PDF_OCR_DPI] = dpi
    return state


def apply_child_degradation(logger: logging.Logger, module: str, counts: Mapping[str, int]) -> None:
    """In the parent: re-apply the degradation the child reported
    (``child_degradation``) to the counters and the running extraction,
    then log it in one rate-limited WARNING naming ``module`` and each
    key with its count. The counts and the text loss are applied
    whether or not the line is logged. An image scale-down
    (``CHILD_IMAGE_SCALE_FACTOR``) is an extractor cap instead, logged
    on its own line with the factor (#1401)."""
    factor = counts.get(CHILD_IMAGE_SCALE_FACTOR)
    if factor:
        warn_extractor_cap(
            logger,
            IMAGE_PIXEL_CEILING_CAP,
            "%s decoded at 1/%d scale (lossy) to fit the pixel ceiling",
            module,
            factor,
        )
        counts = {key: n for key, n in counts.items() if key != CHILD_IMAGE_SCALE_FACTOR}
    if not counts:
        return
    add_counters(counts)
    if counts.get(CHILD_TEXT_LOST):
        note_text_lost()
    if CHILD_OCR_PAGES_SKIPPED in counts:
        record_ocr_pages_skipped(counts[CHILD_OCR_PAGES_SKIPPED])
    reported = " ".join(f"{key}={counts[key]}" for key in sorted(counts))
    warn_rate_limited(logger, "extractor %s degraded in the child: %s", module, reported)


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
      *or* the format's optional dependency is missing in this image,
      or the extractor declined in a way the same bytes always repeat
      (``PERMANENT_FAILURE_ERRORS``, #931).
    * ``"too_large"`` — payload exceeded ``max_bytes``.
    * ``"failed"`` — extractor raised; ``error`` records the exception
      type only, since its message can quote the document. Indexer
      treats this as terminal for the attachment (won't keep
      retrying), but a future re-extraction sweep can re-run after a
      library upgrade.

    ``ocr_pages_skipped`` is the scanned pages the PDF OCR page cap left
    unread (#891): set on a ``success`` or ``empty`` PDF result (0 when
    none), ``None`` (unknown) otherwise. Kept with the cached row so an
    occurrence served from the cache still counts as capped.

    ``ocr_pages_cap``, ``digital_pages_cap`` and ``extracted_chars_cap``
    (#1418) record, on a ``success`` or ``empty`` result, the configured
    limit that cut it (``INDEXER_OCR_MAX_PAGES``,
    ``INDEXER_PDF_MAX_DIGITAL_PAGES``,
    ``INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS``): its value when it cut,
    0 when it did not, ``None`` when it does not apply to the module or
    the result has another status. See ``CAP_COLUMNS``.

    ``text_complete`` (#1242) is whether ``text`` holds all the text the
    extractor could read: ``True`` only for a ``success`` or ``empty``
    result whose extraction lost nothing (``note_text_lost``: an
    extractor cap, the dispatcher's ``max_extracted_chars`` cut, the PDF
    digital-page or OCR page cap, a PDF page no reader recovered, a
    scanned page left unread, an image's OCR frame cap). ``False`` for
    every other status, which never certifies absence. ``None`` when not
    known (a result built without it). Kept with the cached row.
    """

    status: str
    extractor: str | None
    text: str | None
    error: str | None
    ocr_pages_skipped: int | None = None
    text_complete: bool | None = None
    ocr_pages_cap: int | None = None
    digital_pages_cap: int | None = None
    extracted_chars_cap: int | None = None


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
# docx 5 still: a long part-relationship chain is now ``failed``
# (``DocxRelationshipChainError``) instead of escaping as
# ``RecursionError`` (#945); that escape cached no row, so none is stale.
# docx 5 still: a package over a pre-open budget is now ``failed``
# (``DocxPackageBudgetError``, #967, #946), with no bump: the walk after
# the open has no budget yet (#1031), so the few over-budget ``success``
# rows, whose text is still right, were left alone.
# text 3: a payload starting with a fixed binary signature is recorded
# ``unsupported`` instead of decoded as replacement characters, so the
# ``success`` rows the previous version wrote for one are refreshed (#932).
# doc 1, xls 1: legacy binary ``.doc`` (catdoc) and ``.xls`` (xlrd in a
# child process), recorded ``unsupported`` before (#935).
# pdf 5, xlsx 6: a PDF that needs an open password or exceeds a pypdf
# limit, and a workbook over the eager-part budget, are recorded
# ``unsupported`` instead of ``failed``, so the ``failed`` rows the
# previous versions wrote for them are refreshed (#931).
# ppt 1: legacy binary ``.ppt`` (Apache POI in a Java process), recorded
# ``unsupported`` before (#957).
# ppt 1 still: a password-protected deck is recorded ``unsupported``
# instead of ``failed`` (#983), with no bump: the text does not change
# (none either way), and a bump would make the startup sweep re-run
# every cached deck to reclassify the few encrypted ones. A ``failed``
# row written for one before stays ``failed`` until the same bytes are
# processed again more than 7 days on (a reparse, or another message
# carrying them), when it converts.
# pptx 1: the first ``.pptx`` extractor (#936). Rows cached ``unsupported``
# for a ``.pptx`` before it carry no extractor, so no version marks them
# stale; the "no extractor" sweep re-queues them instead.
# pptx 2: reads slideshows (``.ppsx``) and templates (``.potx``), which
# ``pptx.Presentation`` refused, so one labelled ``.pptx`` failed (#947).
# ``.pptm`` / ``.ppsx`` / ``.potx`` occurrences cached "no extractor" are
# re-queued by that sweep.
# pptx 3: a deck over a pre-open budget is recorded ``unsupported``
# instead of ``failed`` (#1032), so the ``failed`` rows version 2 wrote
# for one are refreshed once; the startup sweep re-runs stale rows only,
# never aged ``failed`` ones. The PPTX walk is budgeted (#936), so the
# re-run is bounded.
# docx 5 still: the same mapping for a document over a pre-open budget
# (#1032) came with no bump, for the reason above (#1031): the few
# ``failed`` rows version 5 wrote for one stay ``failed`` until the same
# bytes are processed again more than 7 days on, or until a deliberate
# bump follows #1031. PR #1068 briefly shipped ``docx`` 6 for this
# mapping; PR #1075 reverted it. A row a build between the two stamped
# ``docx@6`` is kept (a newer row is never downgraded, see
# ``stale_extractor_module``); rolling back to such a build treats the
# ``docx@5`` rows written since as stale and re-runs them through the
# unbudgeted walk at its next start, which is that build's behaviour.
# Version 6 is therefore taken: the next ``docx`` bump goes to 7, or
# the ``docx@6`` rows such a build wrote would never be re-extracted.
# pptx 3 still: reads macro-enabled slideshows (``.ppsm``) and templates
# (``.potm``), whose main parts python-pptx loaded as generic parts, so
# one labelled ``.pptx`` failed by type (#1042); the bump above refreshes
# those rows, and ``.ppsm`` / ``.potm`` occurrences cached "no extractor"
# carry no version and are re-queued by that sweep.
# docx 7: the walk after the open reads the XML one element at a time
# under walk budgets (#1031), so a document over a budget now returns
# partial text where it returned all of it, after minutes, before. The
# text of a document inside the budgets is unchanged. The bump re-runs
# every cached DOCX row once through the budgeted walk: the ``docx@5``
# rows, including the ``failed`` rows version 5 wrote for a document
# over a pre-open budget, which are now recorded ``unsupported``
# (#1032), and any ``docx@6`` row the build described above wrote.
# docx 7, pptx 3, xlsx 6 still: the extraction runs in a child process
# under an address-space and a CPU limit (#1040), with no bump. The text
# and status of a file inside the limits are unchanged, failures keep
# their type names, and the files the limits now fail are crafted
# (measured in each module), so a bump would only re-run every cached
# OOXML row through a child to change none of them.
# image 3 still: moved to the limited child (#1292) with a
# 10,000,000-char budget applied after stripping; output identical to
# the in-process path whenever stripped OCR text is <= 10M chars; above
# that only with a raised OCR page limit or crafted input — owner
# exception, 2026-10-08, no bump (bumping would reset completeness and
# re-OCR with new failure modes).
# image 4: converts frames in modes Pillow cannot write as PNG (CMYK,
# ...) to RGB (#1400). Those images were recorded ``failed``, and a
# failed row is re-run only when its bytes are extracted again, so the
# bump is what makes the startup sweep re-queue them; it re-OCRs every
# cached image payload once (about 3,460 on the live index) and clears
# ``text_complete`` on them until re-indexed (owner approved). A later
# image change (#1413 routing) takes 5 or higher.
# image 5: the "OCR disabled" result of an image is stamped ``image@5``
# (#1415), so a row recorded with no stamp, which may be a PDF sent
# under an image label from before the routing to ``pdf``, is re-run
# once whatever the OCR setting (``attachment_indexing``
# ``_unsupported_still_holds``), and the ``failed`` image rows are re-run
# too (owner approved, 2026-10-10). Like 4, it re-OCRs every cached image
# payload once while OCR is on.
# image 6: the image child has its own pixel ceiling, 48,000,000 pixels,
# under a 1,605 MiB address-space limit (#1401, owner decision
# 2026-10-10). Images from 30,000,000 to 48,000,000 pixels, recorded
# ``failed`` under ``DecompressionBombWarning`` before, are read at full
# resolution. A JPEG or MPO over the ceiling is read at 1/2 scale
# (``draft``) as a lossy fallback (``image.MAX_DRAFT_FACTOR``: on the
# synthetic catalogue, low-contrast lines of 50 and 60 px were lost at
# half scale), reported as the ``image_pixel_ceiling`` cap; an image
# that still does not fit is recorded ``unsupported``
# (``IMAGE_PIXEL_CEILING_ERROR``), and an MPO reports that only its
# primary picture was read. The bump re-OCRs every cached image payload
# once, as image 4 did, and with OCR off re-runs the ``image@5`` "OCR
# disabled" rows once (no OCR runs; they are re-stamped).
# doc 2, ppt 2: the raw tool's output byte cap follows the configured
# ``max_extracted_chars`` (four bytes a character, up to a 40 MiB
# ceiling) instead of a fixed 8 MiB (#1308), so the same bytes can
# yield more text (a raised or disabled character cap) or less (a
# lowered one). The bump re-runs every cached ``.doc`` and ``.ppt`` row
# once: catdoc in milliseconds, the ``.ppt`` reader at one JVM start
# (0.15 to 0.35 s) per deck. ``ppt`` rows recorded ``failed`` for an
# encrypted deck before #983 convert to ``unsupported`` on that re-run.
# eml 1: attached emails (``message/rfc822``, ``application/eml``,
# ``.eml``), the first ``eml`` extractor (#922): a stamp only, as for
# ``pptx`` 1. Their occurrences were cached ``unsupported`` ("no
# extractor") with no stamp, so the "no extractor" sweep re-queues them
# once.
# eml 2: a quoted-printable or uuencode part or nested email that decoded
# cleanly is complete (#1288); version 1 counted every quoted-printable
# one as lossy. The text is unchanged; the bump re-runs the cached ``eml``
# rows once (attached emails only) so their ``text_complete`` is
# re-assessed.
# eml 3: a uuencode body part of an attached email with no ``end`` line
# last (cut in transit) is incomplete (#1402); text unchanged, same
# re-run of the cached ``eml`` rows.
EXTRACTOR_VERSIONS: dict[str, int] = {
    "doc": 2,
    "docx": 7,
    "eml": 3,
    "image": 6,
    "pdf": 5,
    "ppt": 2,
    "pptx": 3,
    "text": 3,
    "xls": 1,
    "xlsx": 6,
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
    of it, else ``None``. The cache is keyed by content hash and the
    extractor module the occurrence selects (#928), so a stale row is
    refreshed by a fresh extraction of an occurrence that selects its
    module.

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

# ``unsupported`` error for a payload bound for an OOXML extractor (DOCX,
# XLSX, PPTX) that is an OLE2 compound file, which none of them can read
# (#694, #936), when the occurrence's label selects no legacy extractor
# (#935): a password-protected OOXML package, or a legacy file labelled
# as OOXML. An occurrence labelled ``.doc`` / ``.xls`` selects the legacy
# extractor and has its own cache row (#928).
LEGACY_OLE2_ERROR = "OLE2 compound file (legacy .doc / .xls or encrypted Office file)"

# ``unsupported`` error for a payload labelled ``.ppt`` /
# ``application/vnd.ms-powerpoint`` that is not an OLE2 compound file
# (#957): the ``ppt`` extractor reads only OLE2. Kept apart from "no
# extractor" so the row holds for later ``.ppt`` occurrences instead of
# re-running on each (``attachment_indexing``).
NON_OLE2_PPT_ERROR = "not an OLE2 compound file (labelled legacy .ppt)"

# ``unsupported`` error for a payload under a legacy ``.doc`` / ``.xls`` label
# that is neither OLE2 nor a ZIP (for example an RTF file labelled ``.doc``):
# no reader takes it, and the OOXML route would only fail and be retried (#1227).
NOT_OLE2_OR_OOXML_ERROR = "not an OLE2 or OOXML container (labelled as a legacy Office type)"

# The fixed 8-byte signature every OLE2 compound file starts with.
_OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# ``unsupported`` errors for an extractor exception the same bytes always
# repeat in that extractor (#931), so a ``failed`` row would only re-run
# it every ``_FAILED_CACHE_MAX_AGE``. Fixed text: the exceptions' own
# messages can quote the document. The PPTX and DOCX pre-open package
# budgets are decided from the ZIP central directory alone (#1032).
ENCRYPTED_PDF_ERROR = "encrypted PDF (open password required)"
PDF_LIMIT_ERROR = "PDF structure exceeds pypdf limits"
XLSX_EAGER_BUDGET_ERROR = "workbook exceeds the eager-part budget"
PPTX_PACKAGE_BUDGET_ERROR = "presentation exceeds a pre-open package budget"
DOCX_PACKAGE_BUDGET_ERROR = "document exceeds a pre-open package budget"
# A password-protected legacy ``.ppt`` (#983), from the reader's reserved
# exit status.
ENCRYPTED_PPT_ERROR = "encrypted legacy .ppt (open password required)"
# An image over the image child's pixel ceiling that a JPEG scale-down
# within the validated factor cannot fit (#1401).
IMAGE_PIXEL_CEILING_ERROR = "image exceeds the pixel ceiling"
PERMANENT_FAILURE_ERRORS = frozenset(
    {
        ENCRYPTED_PDF_ERROR,
        PDF_LIMIT_ERROR,
        XLSX_EAGER_BUDGET_ERROR,
        PPTX_PACKAGE_BUDGET_ERROR,
        DOCX_PACKAGE_BUDGET_ERROR,
        ENCRYPTED_PPT_ERROR,
        IMAGE_PIXEL_CEILING_ERROR,
    }
)
# Extractors that read an OOXML package (a ZIP): each gets the OLE2 check
# and the ZIP guard before its library opens the payload.
OOXML_MODULES = frozenset({"docx", "pptx", "xlsx"})

# ``unsupported`` error for a payload bound for the text extractor that
# starts with one of ``_BINARY_SIGNATURES`` (#932): decoding it would only
# index replacement characters. Decided by the bytes alone, like the OLE2
# check.
BINARY_AS_TEXT_ERROR = "binary payload labelled as text"

# Fixed prefixes of binary formats senders mislabel as text: PDF, ZIP
# (including OOXML; an empty archive starts with its end-of-central-
# directory record), OLE2, PNG, JPEG and GIF. A prefix list only; no
# sniffing beyond it.
_BINARY_SIGNATURES = (
    b"%PDF-",
    b"PK\x03\x04",
    b"PK\x05\x06",
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
    # Legacy PowerPoint: OLE2 only; anything else is ``unsupported``
    # (``NON_OLE2_PPT_ERROR``, #957).
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    # Macro-enabled decks (``.pptm``, text only; macros are never read),
    # slideshows (``.ppsx``) and templates (``.potx``): the PPTX extractor
    # loads their main parts (#947), as it does the macro-enabled
    # slideshows (``.ppsm``) and templates (``.potm``) it registers (#1042).
    # Keys are lowercase: the label is lowercased before the lookup.
    "application/vnd.ms-powerpoint.presentation.macroenabled.12": "pptx",
    "application/vnd.openxmlformats-officedocument.presentationml.slideshow": "pptx",
    "application/vnd.openxmlformats-officedocument.presentationml.template": "pptx",
    "application/vnd.ms-powerpoint.slideshow.macroenabled.12": "pptx",
    "application/vnd.ms-powerpoint.template.macroenabled.12": "pptx",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/plain": "text",
    "text/csv": "text",
    "text/markdown": "text",
    # Attached emails (#922). ``message/delivery-status`` stays unsupported.
    "message/rfc822": "eml",
    "application/eml": "eml",
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
    ".ppt": "ppt",
    ".pptx": "pptx",
    ".pptm": "pptx",
    ".ppsx": "pptx",
    ".potx": "pptx",
    ".ppsm": "pptx",
    ".potm": "pptx",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".txt": "text",
    ".csv": "text",
    ".md": "text",
    ".markdown": "text",
    ".eml": "eml",
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


def _recording_text_completeness[**P](
    fn: Callable[P, ExtractionResult],
) -> Callable[P, ExtractionResult]:
    """Set ``text_complete`` on every result ``fn`` returns (#1242): the
    attempt's loss flag is cleared first, any loss recorded during it
    (``note_text_lost``) makes the result incomplete, and a status other
    than ``success`` or ``empty`` is never complete."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> ExtractionResult:
        _attempt.text_lost = False
        result = fn(*args, **kwargs)
        complete = result.status in (STATUS_SUCCESS, STATUS_EMPTY) and not _attempt.text_lost
        return replace(result, text_complete=complete)

    return wrapper


@_recording_text_completeness
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

    module_name, dispatch_via = _resolve_extractor(content_type, filename)

    # A PDF under an image label runs the PDF extractor (#1415), decided
    # before the OCR gate below, since that extractor reads a digital text
    # layer without OCR: the same constant-size prefix check.
    if module_name == "image":
        module_name = _route_container(module_name, payload)

    # Image types are gated by ``ocr_enabled`` because the only sensible
    # extractor is Tesseract. Disabling OCR globally should cleanly
    # downgrade them to ``unsupported`` rather than failing per-call. The
    # result is stamped (#1415), so a row recorded before the routing above
    # (no stamp) is told apart and re-run once.
    if module_name == "image" and not ocr_enabled:
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=_stamp_extractor(module_name, module_name),
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

    # A ``.ppt`` that is not OLE2 (#957): the same constant-size prefix
    # check; the aggregate counts the unsupported result.
    if module_name == "ppt" and not payload.startswith(_OLE2_SIGNATURE):
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=None,
            text=None,
            error=NON_OLE2_PPT_ERROR,
        )

    # A legacy label whose bytes are neither OLE2 nor ZIP (#1227): the same
    # constant-size prefix check; no reader takes it, so it is not retried.
    if module_name in _LEGACY_TO_OOXML and not payload.startswith(
        (*_ZIP_SIGNATURES, _OLE2_SIGNATURE)
    ):
        # Logged like the other permanent declines: fixed text, no payload (#1227).
        warn_rate_limited(
            log,
            "%s payload is not an OLE2 or OOXML container; recorded unsupported, not retried",
            module_name,
        )
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=None,
            text=None,
            error=NOT_OLE2_OR_OOXML_ERROR,
        )

    # The container decides between a legacy and an OOXML extractor
    # (#694, #935): a constant-size prefix check; the aggregate counts an
    # unsupported result, so no per-item line.
    module_name = _route_container(module_name, payload)
    if module_name is None:
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=None,
            text=None,
            error=LEGACY_OLE2_ERROR,
        )

    # A binary payload labelled as text (#932): the same constant-size
    # prefix check, also decided by the bytes alone and counted by the
    # aggregate.
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

    # Zip-based formats (DOCX, XLSX, PPTX) need a zip-bomb pre-check: the
    # ``max_bytes`` cap above only bounds the compressed payload; a
    # malicious workbook can declare 200× expansion in its central
    # directory. Reject before handing to lxml.
    if module_name in OOXML_MODULES:
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

    # Known zero for a PDF unless its OCR cap records a count; unknown for
    # every other module (#891).
    _attempt.ocr_pages_skipped = 0 if module_name == "pdf" else None
    # The configured limits each module is subject to start at 0 (did not
    # cut); the extractor records the limit when it cuts (#1418). The
    # character cap is the dispatcher's own, below, for every module.
    _attempt.ocr_pages_cap = 0 if module_name in _OCR_PAGE_CAP_MODULES else None
    _attempt.digital_pages_cap = 0 if module_name == "pdf" else None
    _attempt.extracted_chars_cap = 0
    # The raw-tool extractors size their output byte cap from the
    # character cap (#1308); no other extractor takes it.
    raw_tool_options = (
        {"max_extracted_chars": max_extracted_chars} if module_name in ("doc", "ppt") else {}
    )
    try:
        text, extractor_name = extractor_fn(
            payload,
            ocr_enabled=ocr_enabled,
            max_ocr_pages=max_ocr_pages,
            ocr_timeout_seconds=ocr_timeout_seconds,
            max_pdf_pages=max_pdf_pages,
            on_progress=on_progress,
            **raw_tool_options,
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
        permanent_error = _permanent_failure_error(module_name, exc)
        if permanent_error is not None:
            # The same bytes would fail the same way on every retry, so
            # the result is ``unsupported``, served to later occurrences
            # (#931). Stamped with the extractor so a version bump still
            # refreshes it. It drops the attachment out of search, so it
            # is a WARNING like a failure, rate limited; fixed text only.
            warn_rate_limited(
                log,
                "extractor %s declined (dispatch_via=%s): %s; recorded unsupported, not retried",
                module_name,
                dispatch_via,
                permanent_error,
            )
            return ExtractionResult(
                status=STATUS_UNSUPPORTED,
                extractor=_stamp_extractor(module_name, module_name),
                text=None,
                error=permanent_error,
            )
        # Per-payload extractor errors (broken PDFs, malformed DOCX,
        # missing optional deps that slipped past _safe_import) become
        # ``failed`` rows so a single bad attachment cannot dead-letter
        # the parent message. ``MemoryError`` / ``RecursionError`` are
        # excluded above precisely because they are not per-payload.
        # Parser exceptions quote the document (text, member names), so
        # only the type is logged and persisted (#257). WARNING, since
        # the attachment drops out of search (#871), rate limited. An
        # extraction in the extractor child (#1040, #1291) reports the
        # type name of what it raised.
        error_type = exc.type_name if isinstance(exc, ChildError) else type(exc).__name__
        _warn_failed(module_name, dispatch_via, error_type)
        return ExtractionResult(
            status=STATUS_FAILED,
            extractor=_stamp_extractor(module_name, module_name),
            text=None,
            error=error_type,
        )

    if extractor_name == "pdf-ocr-disabled":
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=None,
            text=None,
            error=SCANNED_PDF_OCR_DISABLED_ERROR,
        )

    extractor_name = _stamp_extractor(module_name, extractor_name)
    ocr_pages_skipped = _attempt.ocr_pages_skipped
    cleaned = (text or "").strip()
    if not cleaned:
        return ExtractionResult(
            status=STATUS_EMPTY,
            extractor=extractor_name,
            text=None,
            error=None,
            ocr_pages_skipped=ocr_pages_skipped,
            ocr_pages_cap=_attempt.ocr_pages_cap,
            digital_pages_cap=_attempt.digital_pages_cap,
            extracted_chars_cap=_attempt.extracted_chars_cap,
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
        record_cap_cut(CAP_EXTRACTED_CHARS, max_extracted_chars)
    return ExtractionResult(
        status=STATUS_SUCCESS,
        extractor=extractor_name,
        text=cleaned,
        error=None,
        ocr_pages_skipped=ocr_pages_skipped,
        ocr_pages_cap=_attempt.ocr_pages_cap,
        digital_pages_cap=_attempt.digital_pages_cap,
        extracted_chars_cap=_attempt.extracted_chars_cap,
    )


# Modules ``INDEXER_OCR_MAX_PAGES`` applies to: scanned PDF pages and
# multipage image frames (#1418).
_OCR_PAGE_CAP_MODULES = frozenset({"pdf", "image"})


def _permanent_failure_error(module_name: str, exc: Exception) -> str | None:
    """The fixed ``unsupported`` error for an exception from ``module_name``
    that the same bytes always repeat, else ``None`` (#931). Matched by
    exact class, never by message text: a subclass (pypdf's
    ``WrongPasswordError``, raised only for a password we never supply)
    or any other exception stays ``failed``. The extractor already ran,
    so its module imports."""
    if module_name == "pdf":
        from pypdf.errors import FileNotDecryptedError, LimitReachedError

        if type(exc) is FileNotDecryptedError:
            return ENCRYPTED_PDF_ERROR
        if type(exc) is LimitReachedError:
            return PDF_LIMIT_ERROR
    elif module_name == "xlsx":
        from .xlsx import XlsxEagerPartBudgetError

        if type(exc) is XlsxEagerPartBudgetError:
            return XLSX_EAGER_BUDGET_ERROR
    elif module_name == "pptx":
        from .pptx import PptxPackageBudgetError

        if type(exc) is PptxPackageBudgetError:
            return PPTX_PACKAGE_BUDGET_ERROR
    elif module_name == "docx":
        from .docx import DocxPackageBudgetError

        if type(exc) is DocxPackageBudgetError:
            return DOCX_PACKAGE_BUDGET_ERROR
    elif module_name == "ppt":
        from .ppt import PptEncryptedError

        if type(exc) is PptEncryptedError:
            return ENCRYPTED_PPT_ERROR
    elif module_name == "image":
        from .image import ImagePixelCeilingError

        if type(exc) is ImagePixelCeilingError:
            return IMAGE_PIXEL_CEILING_ERROR
    return None


# The legacy (OLE2) extractor for a legacy label, and the OOXML one the
# same label selects for a payload that is not OLE2.
_LEGACY_TO_OOXML = {"doc": "docx", "xls": "xlsx"}
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06")
_PDF_SIGNATURE = b"%PDF-"


def _route_container(module_name: str, payload: bytes) -> str | None:
    """The extractor for ``payload`` once its container is known, or
    ``None`` when it is an OLE2 file no extractor reads.

    * A legacy label (``doc``, ``xls``) keeps its legacy extractor for an
      OLE2 payload; any other payload goes to the OOXML extractor, a
      best effort for an OOXML file mislabelled as a legacy type.
    * An OLE2 payload under an OOXML label (an encrypted OOXML file is
      OLE2 too, or a legacy file labelled ``.docx``) is ``None``: no
      OOXML extractor (DOCX, XLSX, PPTX) can read OLE2, and an attempt
      would only record ``failed`` and re-run every
      ``_FAILED_CACHE_MAX_AGE`` (#694).
    * A PDF (``%PDF-``) under an image label goes to ``pdf`` (#1415): the
      image extractor cannot read it, and the PDF extractor reads its
      digital text layer whatever the OCR setting.
    * Anything else is unchanged.
    """
    if module_name == "image" and payload.startswith(_PDF_SIGNATURE):
        return "pdf"
    ole2 = payload.startswith(_OLE2_SIGNATURE)
    if module_name in _LEGACY_TO_OOXML:
        return module_name if ole2 else _LEGACY_TO_OOXML[module_name]
    if ole2 and module_name in OOXML_MODULES:
        return None
    return module_name


def extraction_module(content_type: str, filename: str, payload: bytes) -> str | None:
    """The extractor module whose result an extraction of ``payload``
    under this label is, or ``None`` when the label selects none: the
    module the label selects after the container check (``.doc`` with
    OOXML bytes runs ``docx``, an image label with PDF bytes ``pdf``). An
    OLE2 payload under an OOXML label, which no extractor reads, stays
    under that label's module. A prefix
    check only. With the content hash, the extraction cache key (#928):
    labels that run the same extractor on the same bytes share its row."""
    selected = _resolve_extractor(content_type, filename)[0]
    if selected is None:
        return None
    # A legacy label with bytes no reader takes (#1227) stays under its own
    # module: under the OOXML module it would share a row with an OOXML label.
    if selected in _LEGACY_TO_OOXML and not payload.startswith((*_ZIP_SIGNATURES, _OLE2_SIGNATURE)):
        return selected
    return _route_container(selected, payload) or selected


def ole2_extraction_module(content_type: str, filename: str) -> str | None:
    """``extraction_module`` for OLE2 bytes under this label, for a caller
    that knows the bytes are OLE2 (an "OLE2 compound file" row) but does
    not hold them."""
    return extraction_module(content_type, filename, _OLE2_SIGNATURE)


def label_extraction_modules(content_type: str, filename: str) -> frozenset[str]:
    """Every module ``extraction_module`` can return for this label,
    whatever the bytes: a legacy label also runs its OOXML extractor on
    bytes that are not OLE2, and an image label the PDF extractor on PDF
    bytes (#1415). Empty when the label selects none."""
    selected = _resolve_extractor(content_type, filename)[0]
    if selected is None:
        return frozenset()
    if selected in _LEGACY_TO_OOXML:
        return frozenset({selected, _LEGACY_TO_OOXML[selected]})
    if selected == "image":
        return frozenset({selected, "pdf"})
    return frozenset({selected})


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


def over_package_budget(
    payload: bytes,
    *,
    max_members: int,
    max_expansion_bytes: int,
    max_rels_bytes: int,
    max_declared_bytes: int,
) -> bool:
    """True when an OOXML package is over one of its extractor's pre-open
    budgets (#936, #967, #1033): more than ``max_members`` members,
    members expanding by more than ``max_expansion_bytes`` past their
    compressed size, more than ``max_rels_bytes`` declared in relationship
    (``.rels``) members, or more than ``max_declared_bytes`` declared in
    all members together, stored or compressed. Reads only the central
    directory, as ``_validate_zip_payload`` does. zipfile returns no more
    of a member than its declared size, but decompresses the member's
    whole stream first, so a member that understates its size is
    expanded anyway; the extractors call this in their child process,
    whose address-space limit bounds that (#1040). A payload that is not
    a ZIP is left to the library to reject."""
    import io

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
    except zipfile.BadZipFile:
        return False
    expansion = sum(max(info.file_size - info.compress_size, 0) for info in members)
    rels_bytes = sum(info.file_size for info in members if info.filename.endswith(".rels"))
    declared = sum(info.file_size for info in members)
    return (
        len(members) > max_members
        or expansion > max_expansion_bytes
        or rels_bytes > max_rels_bytes
        or declared > max_declared_bytes
    )


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
