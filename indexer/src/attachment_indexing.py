"""
Per-attachment indexing pipeline.

Lives in its own module so ``main.py``'s file-pipeline orchestration
stays small enough to read.

Two-phase shape:

* ``prepare_attachment_writes`` runs everything that must NOT happen
  inside a SQLite write transaction — extractor (OCR / pypdf / openpyxl)
  CPU work and chunking. It only reads the DB (extraction cache
  lookups). The output is an ``AttachmentWritePlan`` that the caller can
  hold in memory until it's ready to commit. It does not embed: the plan
  comes back with ``embeddings_by_chunk_id`` empty. The batched drain
  pipeline in ``main.py`` diffs each plan's chunks against the stored
  chunk IDs, embeds the new chunks of every message in the batch
  together (Phase 2b) and fills each plan from that result before
  applying it.

* ``apply_attachment_writes`` performs only DB writes and is intended
  to be called inside the indexer's outer ``with db.transaction():``
  block. No network, no extraction, no embedding — every slow operation
  has already happened.

Keep the phases separate: running them back-to-back inside one
transaction reintroduces the original bug where embedding service
latency blocks the SQLite write transaction.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from .chunker import MessageChunk, chunk_message
from .database import Database
from .extractors import (
    BINARY_AS_TEXT_ERROR,
    CAP_COLUMNS,
    CAP_DIGITAL_PAGES,
    CAP_EXTRACTED_CHARS,
    CAP_OCR_PAGES,
    CONTAINER_IDENTIFICATION_VERSION,
    LEGACY_OLE2_ERROR,
    NON_OLE2_PPT_ERROR,
    NOT_OLE2_OR_OOXML_ERROR,
    OCR_DISABLED_ERROR,
    PERMANENT_FAILURE_ERRORS,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_TOO_LARGE,
    STATUS_UNSUPPORTED,
    ExtractionResult,
    drain_extractor_counts,
    extraction_module,
    has_container_prefix,
    is_stale_extractor,
    label_extraction_modules,
    note_ocr_capped,
    note_ocr_capped_image,
    ole2_extraction_module,
    warn_extractor_cap,
    warn_rate_limited,
)
from .extractors import (
    extract as extract_attachment,
)
from .extractors._runner import process_launches
from .parser import Attachment

log = logging.getLogger("indexer.attachments")

# The plan status of an occurrence whose extraction the per-message
# budget deferred to a later pass (#1236). Never an extraction status:
# nothing is written to ``attachment_extractions`` for it.
STATUS_DEFERRED = "deferred"

# Outcomes the periodic attachments aggregate counts (#871): the
# extraction statuses, with ``unsupported`` for want of OCR split out as
# ``ocr_disabled``, and occurrences deferred to a later pass (#1236).
ATTACHMENT_OUTCOMES: tuple[str, ...] = (
    STATUS_SUCCESS,
    STATUS_FAILED,
    STATUS_UNSUPPORTED,
    STATUS_TOO_LARGE,
    "ocr_disabled",
    STATUS_EMPTY,
    STATUS_DEFERRED,
)

# The per-message extraction budget (#1236): the work one pass of one
# message may start on attachments the cache and the batch cannot serve,
# counted in ``_resolve_extracted_text`` around each dispatch: processes
# started (``_runner.process_launches``: extractor children, raw tools,
# and the Poppler and Tesseract processes a scanned PDF starts) and
# monotonic seconds spent extracting. Once either is reached the
# message's remaining uncached attachments are deferred, and the message
# is continued on a later pass behind the rows already due. The first
# dispatch of a pass is always admitted, so every pass resolves at least
# one deferred attachment, and an admitted attachment runs to its own
# bounds, so a pass can end past the budget by one attachment's run.
# Fixed, positive values, with no off switch (owner, 2026-10-09). Sized
# from plain timings in the indexer image (docs/architecture.md,
# "Per-message extraction budget").
EXTRACTION_TURN_LAUNCHES = 64
EXTRACTION_TURN_SECONDS = 5.0


@dataclass
class ExtractionBudget:
    """One message's extraction budget for one pass (#1236)."""

    max_launches: int = EXTRACTION_TURN_LAUNCHES
    max_seconds: float = EXTRACTION_TURN_SECONDS
    launches: int = 0
    seconds: float = 0.0
    dispatched: int = 0
    deferred: int = 0
    clock: Callable[[], float] = time.monotonic

    def exhausted(self) -> bool:
        """Whether the next uncached attachment is deferred: one has
        already been dispatched this pass and the launches or the seconds
        reached their budget."""
        return self.dispatched > 0 and (
            self.launches >= self.max_launches or self.seconds >= self.max_seconds
        )

    def charge(self, launches: int, seconds: float) -> None:
        self.dispatched += 1
        self.launches += launches
        self.seconds += seconds


class AttachmentOutcomeCounts:
    """Attachment occurrences by outcome since the last ``drain``.

    Every occurrence of a message that commits is counted
    (``record_committed_outcomes``), whether extracted now or served
    from the cache or the batch (``cached`` counts the latter too), so
    the line the indexer logs with each summary shows how much of the
    mail's attachments is searchable. Counting at commit keeps a message
    prepared again after an embedder outage from being counted twice.
    Per-attachment lines would flood the log; ``failed`` extractions also
    log their own rate-limited WARNING. ``drain`` also reports the
    extractors' per-attempt counts (``extractors.drain_extractor_counts``:
    ``pdf_pages_failed``, ``pdf_pages_unrecovered``, ``ocr_capped_pdfs``,
    ``ocr_pages_skipped``, ``ocr_capped_images``, ``extractor_caps``,
    ``parser_caps_messages``, ``parser_recipients_merged_messages``,
    ``parser_sender_ambiguous_messages``, the attached-email decoding
    fallbacks ``eml_headers_degraded``, ``eml_filenames_degraded`` and
    ``eml_charsets_degraded``, and ``warnings_suppressed``). Counts
    only: no filename, type or text.
    """

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = Lock()

    def record(
        self, status: str, error: str | None, *, cached: bool, resumed: bool = False
    ) -> None:
        outcome = status
        if status == STATUS_UNSUPPORTED and (error or "").startswith(OCR_DISABLED_ERROR):
            outcome = "ocr_disabled"
        with self._lock:
            self._counts[outcome] += 1
            if cached:
                self._counts["cached"] += 1
            if resumed:
                self._counts["deferred_resumed"] += 1

    def record_deferred_message(self) -> None:
        """A message committed with attachments deferred to a later pass
        (#1236)."""
        with self._lock:
            self._counts["deferred_messages"] += 1

    def record_dropped(self, n: int, slices: int) -> None:
        """Stored occurrences a pass deleted because the message's
        current parse no longer produces them, and how many of them took
        a payload's searchable text with them (#1375)."""
        if n:
            with self._lock:
                self._counts["dropped"] += n
                self._counts["dropped_text"] += slices

    def drain(self) -> dict[str, int]:
        """Return every outcome's count plus ``cached`` and the extractor
        counts, and reset them."""
        with self._lock:
            counts, self._counts = self._counts, Counter()
        drained = {
            name: counts[name]
            for name in (
                *ATTACHMENT_OUTCOMES,
                "cached",
                "deferred_messages",
                "deferred_resumed",
                "dropped",
                "dropped_text",
            )
        }
        drained.update(drain_extractor_counts())
        return drained


attachment_outcomes = AttachmentOutcomeCounts()

# Fields of the summary line after ``n``, in order.
_SUMMARY_FIELDS = (
    *ATTACHMENT_OUTCOMES,
    "cached",
    # Messages continued on a later pass because their extraction reached
    # the per-message budget, and previously deferred occurrences that
    # resolved (#1236).
    "deferred_messages",
    "deferred_resumed",
    # Stored occurrences deleted because the message's current parse no
    # longer produces them (#1375), and how many of them took a payload's
    # searchable text along (the latter is in ``_DEGRADED_FIELDS``).
    "dropped",
    "dropped_text",
    "pdf_pages_failed",
    "pdf_pages_unrecovered",
    "ocr_capped_pdfs",
    "ocr_pages_skipped",
    "ocr_capped_images",
    "extractor_caps",
    "parser_caps_messages",
    "parser_recipients_merged_messages",
    "parser_sender_ambiguous_messages",
    # Decoding fallbacks in attached emails' text (#922): characters
    # replaced, not text lost, so not in ``_DEGRADED_FIELDS`` (#1315).
    "eml_headers_degraded",
    "eml_filenames_degraded",
    "eml_charsets_degraded",
    "warnings_suppressed",
)
# Counts that mean attachment text is missing from search: the line is
# then a WARNING (review round 1 on #884). ``pdf_pages_failed`` is left
# out: a page pypdf cannot read is OCR'd when OCR is on and may be
# recovered, so it is a diagnostic count (review round 3); the pages no
# OCR recovered are ``pdf_pages_unrecovered`` (review round 4).
_DEGRADED_FIELDS = (
    STATUS_FAILED,
    STATUS_UNSUPPORTED,
    STATUS_TOO_LARGE,
    "ocr_disabled",
    # Text not indexed yet: the occurrence waits for a later pass (#1236).
    STATUS_DEFERRED,
    "deferred_messages",
    "pdf_pages_unrecovered",
    "ocr_capped_pdfs",
    "ocr_pages_skipped",
    "ocr_capped_images",
    "extractor_caps",
    "parser_caps_messages",
    "warnings_suppressed",
    # A dropped occurrence took the only searchable copy of its payload's
    # text with it (#1375); a stale row beside a surviving sibling does not.
    "dropped_text",
)


def record_committed_outcomes(plans: list[AttachmentWritePlan]) -> None:
    """Count the outcome of each plan of a message whose writes committed.

    A plan served from the cache or the batch whose result the PDF OCR
    cap cut is counted as capped here, as the PDF extractor counts a
    fresh extraction (#891), and logs the same rate-limited WARNING. An
    unknown count (``None``, a row cached before schema v3) counts as
    nothing. An extraction continuation skips the occurrences resolved in
    earlier passes (#1236), so a message continued over many passes
    counts each occurrence once per pass that resolves or defers it."""
    for plan in plans:
        if plan.resolved_earlier:
            continue
        attachment_outcomes.record(
            plan.status,
            plan.extraction_error,
            cached=plan.cached,
            resumed=plan.was_deferred and not plan.deferred,
        )
        if plan.cached and plan.ocr_pages_skipped:
            note_ocr_capped(plan.ocr_pages_skipped)
            warn_rate_limited(
                log,
                "pdf OCR capped: cached result is missing %d scanned pages",
                plan.ocr_pages_skipped,
            )
        if plan.cached:
            _count_cached_caps(plan)


def _count_cached_caps(plan: AttachmentWritePlan) -> None:
    """Count a cut a configured limit made that remains on a result
    served from the cache or the batch (#1418), as the extractor counts
    a fresh one, with the same rate-limited WARNING: an image's OCR frame
    cap (#1201) in ``ocr_capped_images``, the PDF digital-page and the
    character caps in ``extractor_caps``. A PDF's OCR page cap is counted
    by its pages skipped, above. Counts and limits only."""
    ocr_pages_cap, digital_pages_cap, extracted_chars_cap = plan.caps
    module = (plan.text_extractor or "").partition("@")[0].partition("-")[0]
    if ocr_pages_cap and module == "image":
        note_ocr_capped_image()
        warn_rate_limited(
            log, "image OCR capped: cached result stopped at %d frames", ocr_pages_cap
        )
    if digital_pages_cap:
        warn_extractor_cap(
            log, "pdf_digital_pages", "cached result stopped at %d pages", digital_pages_cap
        )
    if extracted_chars_cap:
        warn_extractor_cap(
            log, "extracted_chars", "cached result was cut at %d chars", extracted_chars_cap
        )


def attachment_outcomes_degraded(counts: dict[str, int]) -> bool:
    """Whether ``counts`` include attachments whose text is not searchable."""
    return any(counts[name] for name in _DEGRADED_FIELDS)


def format_attachment_outcomes(counts: dict[str, int]) -> str:
    """One log line for ``AttachmentOutcomeCounts.drain()``'s result, or
    an empty string when every count is zero."""
    if not any(counts[name] for name in _SUMMARY_FIELDS):
        return ""
    parts = [f"attachments n={sum(counts[name] for name in ATTACHMENT_OUTCOMES)}"]
    parts.extend(f"{name}={counts[name]}" for name in _SUMMARY_FIELDS)
    return " ".join(parts)


def attachment_occurrence_id(
    *,
    claimant_id: str,
    content_hash: str,
    filename: str,
    occurrence_index: int,
) -> str:
    """Deterministic id for one attachment occurrence on one message.

    Keyed by the message's claimant ID (``parser.claimant_id``), so two
    files claiming one Message-ID never share an occurrence row (#217).

    Same payload appearing twice on the same message (e.g. inline + as
    a regular attachment) gets two distinct rows differentiated by
    ``occurrence_index``. The hash inputs and order are part of the
    on-disk identity and must not change without a schema bump. This
    function is the only place the id is derived: callers derive it here
    (``prepare_attachment_writes`` stores it on the plan) and
    ``Database.upsert_attachment`` persists the id it is given without
    computing it.
    """
    return hashlib.sha256(
        f"{claimant_id}\0{content_hash}\0{filename}\0{occurrence_index}".encode()
    ).hexdigest()


# How long to honor a cached ``STATUS_FAILED`` row before re-running the
# extractor. AGENTS.md says ``attachment_extractions`` exists "so OCR /
# parse cost runs at most once per unique payload" — but a ``failed``
# row may be due to a corrupt PDF that *will* keep failing or a
# transient extractor bug that a future dependency bump fixes. Default
# 7 days strikes a balance: most retries within a week of a real
# library upgrade, but a chronic failure no longer burns OCR cycles
# every time the same payload reappears.
_FAILED_CACHE_MAX_AGE = timedelta(days=7)


# The ``extractor_module`` of an occurrence whose MIME type and filename
# select no extractor (#928).
NO_EXTRACTOR_MODULE = ""


def extraction_cache_module(attachment: Attachment) -> str:
    """The extractor module an occurrence's MIME type and filename run on
    its bytes (``extractors.extraction_module``), or
    ``NO_EXTRACTOR_MODULE``. With the content hash, the key of the
    ``attachment_extractions`` row the occurrence uses (#928): dispatch
    from a label and the bytes is deterministic, so every occurrence with
    the same key would extract the same result."""
    return (
        extraction_module(attachment.content_type, attachment.filename, attachment.payload)
        or NO_EXTRACTOR_MODULE
    )


def unstamped_ocr_disabled(error: str | None, extractor: str | None) -> bool:
    """Whether a cached ``unsupported`` row is an image's "OCR disabled"
    result recorded with no stamp, before an image label with PDF bytes
    ran the PDF extractor (#1415). Such a row may hold a PDF the PDF
    extractor reads without OCR, so it never holds: it is re-run once,
    whatever the OCR setting, and the fresh result is stamped (or keyed
    ``pdf``), so it is not matched again. A scanned PDF's own row is not
    one."""
    return error == OCR_DISABLED_ERROR and extractor is None


def _unsupported_still_holds(
    error: str | None, module: str, ocr_enabled: bool, extractor: str | None
) -> bool:
    """Whether an ``unsupported`` result cached under ``module`` and
    recorded by ``extractor`` still holds for the occurrences that select
    that module (#928).

    An unstamped image "OCR disabled" result never holds
    (``unstamped_ocr_disabled``, #1415); any other "OCR disabled" result
    holds until OCR is turned on. An OLE2 result
    under an OOXML label, a "binary payload labelled as text" result, a
    "not an OLE2 compound file" result under the ``ppt`` label, a "not an
    OLE2 or OOXML container" result under a legacy label and the "no
    extractor" result are decided by the label and the bytes alone (#694,
    #932, #957, #1227), so they hold for good. So do an encrypted PDF, a
    PDF over a pypdf limit, a workbook over the eager-part budget, a
    deck or document over a pre-open package budget and an encrypted
    legacy ``.ppt``: the module that raised them would decline the same
    bytes again, and the row is that module's own (#931, #1032, #983).
    So do the container results (#1416: an OLE2 file with no Office
    stream, an encrypted Office file, a ZIP that is not an Office
    package, an ambiguous container), decided by the bytes alone. A row
    whose container certification is due never reaches this check: the
    lookup re-extracts it first (``identification_refreshes``).
    Any other result (an
    extractor not importable in this image) holds only while the
    occurrence selects no extractor.
    """
    if unstamped_ocr_disabled(error, extractor):
        return False
    error = error or ""
    if "OCR disabled" in error:
        return not ocr_enabled
    if error in {
        LEGACY_OLE2_ERROR,
        BINARY_AS_TEXT_ERROR,
        NON_OLE2_PPT_ERROR,
        NOT_OLE2_OR_OOXML_ERROR,
    }:
        return True
    if error in PERMANENT_FAILURE_ERRORS:
        return True
    return module == NO_EXTRACTOR_MODULE


def reprocess_reruns_extraction(
    error: str | None,
    extractor_module: str,
    content_type: str,
    filename: str,
    extractor: str | None,
) -> bool:
    """Whether reprocessing an occurrence (by its MIME type and filename)
    that uses an ``unsupported`` row with ``error`` cached under
    ``extractor_module`` by ``extractor`` would extract again once OCR is
    on: its label now runs another module on the bytes (a release started
    routing it, or a migrated v0 row was keyed by its stamp), or the row
    no longer holds. The startup sweep re-queues by this, so it shares
    ``_unsupported_still_holds`` with the cache check."""
    if error == LEGACY_OLE2_ERROR:
        # The row says the bytes are OLE2, so the label's module is exact.
        modules = {ole2_extraction_module(content_type, filename) or NO_EXTRACTOR_MODULE}
    else:
        modules = set(label_extraction_modules(content_type, filename)) or {NO_EXTRACTOR_MODULE}
    if extractor_module not in modules:
        return True
    return not _unsupported_still_holds(error, extractor_module, True, extractor)


# A result's cap record: ``(ocr_pages_cap, digital_pages_cap,
# extracted_chars_cap)``, as ``CAP_COLUMNS`` orders them (#1418).
CapRecord = tuple[int | None, int | None, int | None]
_NO_CAPS: CapRecord = (None, None, None)


def _result_caps(result: ExtractionResult) -> CapRecord:
    return (result.ocr_pages_cap, result.digital_pages_cap, result.extracted_chars_cap)


def _row_caps(row: Mapping[str, Any]) -> CapRecord:
    return (row[CAP_OCR_PAGES], row[CAP_DIGITAL_PAGES], row[CAP_EXTRACTED_CHARS])


def too_large_fits(size: int, max_bytes: int) -> bool:
    """Whether bytes of ``size`` cached as ``too_large`` now fit under
    ``max_bytes``, the same comparison ``extractors.extract`` makes, so
    re-extracting them can no longer record ``too_large``."""
    return size <= max_bytes


def _ocr_row_kept(row: Mapping[str, Any], ocr_enabled: bool) -> bool:
    """Whether a cached ``success`` or ``empty`` row is kept as it is
    while OCR is off, out of both cap-refresh arms (#1418): an OCR
    extractor wrote it (the stamp before ``@`` ends in ``-ocr``), or OCR
    pages cut or skipped it (``ocr_pages_cap`` or ``ocr_pages_skipped``
    above 0; NULL counts as 0). Re-extracting it with OCR off could only
    record "OCR disabled" over its text or lose the record of its unread
    scanned pages, so it waits until OCR is on. The sweep's SQL applies
    the same test."""
    if ocr_enabled:
        return False
    return (
        (row["extractor"] or "").partition("@")[0].endswith("-ocr")
        or (row[CAP_OCR_PAGES] or 0) > 0
        or (row["ocr_pages_skipped"] or 0) > 0
    )


def _limit_raised(recorded: int | None, current: int) -> bool:
    """Whether a limit that cut at ``recorded`` (> 0) is now higher, or
    off (``current`` 0, for a limit that has a disabled value)."""
    return recorded is not None and recorded > 0 and (current == 0 or current > recorded)


def cap_raised(
    row: Mapping[str, Any],
    *,
    ocr_enabled: bool,
    max_ocr_pages: int,
    max_pdf_pages: int,
    max_extracted_chars: int,
) -> bool:
    """Whether a cached result (``row``: its status, stamp and cap
    record) was cut by a configured limit the operator has since raised
    (#1418), so it is extracted again. Only a ``success`` or ``empty``
    result, never one ``_ocr_row_kept`` keeps while OCR is off. ``max_ocr_pages``
    has no disabled value (``INDEXER_OCR_MAX_PAGES`` is at least 1), so
    only a higher value lifts its cut; ``max_pdf_pages`` and
    ``max_extracted_chars`` are 0 for no limit, which lifts theirs.
    Lowering a limit never qualifies. ``Database.
    find_cap_refresh_attachment_filepaths`` runs the same test in SQL
    for the startup sweep."""
    if row["extraction_status"] not in {STATUS_SUCCESS, STATUS_EMPTY}:
        return False
    if _ocr_row_kept(row, ocr_enabled):
        return False
    ocr_pages_cap = row[CAP_OCR_PAGES]
    return (
        (ocr_pages_cap is not None and ocr_pages_cap > 0 and max_ocr_pages > ocr_pages_cap)
        or _limit_raised(row[CAP_DIGITAL_PAGES], max_pdf_pages)
        or _limit_raised(row[CAP_EXTRACTED_CHARS], max_extracted_chars)
    )


def cap_bootstrap_due(row: Mapping[str, Any], *, ocr_enabled: bool) -> bool:
    """Whether a cached result predates the cap record (all three
    columns NULL, cached before schema v11) and lost text
    (``text_complete`` 0), so it is extracted once to record which
    limits cut it (#1418): NULL alone never proves a limit was raised.
    The re-extraction records the columns, so this holds once per row.
    Same status and OCR rules as ``cap_raised``."""
    if row["extraction_status"] not in {STATUS_SUCCESS, STATUS_EMPTY}:
        return False
    if _ocr_row_kept(row, ocr_enabled):
        return False
    return row["text_complete"] == 0 and all(row[cap] is None for cap in CAP_COLUMNS)


def _identifier_version(identifier: str) -> int:
    """``container@2`` -> 2; 0 for a value with no version, as the
    sweep's SQL reads it."""
    _, _, version = identifier.partition("@")
    return int(version) if version.isdigit() else 0


def identification_due(row: Mapping[str, Any], *, ocr_enabled: bool) -> bool:
    """Whether a cached row's container certification is due (#1416):
    it has no ``identifier`` (cached before schema v12) or one an older
    identification version wrote. '' (a payload with neither the OLE2
    nor a ZIP signature) and a newer version (after a rollback) are not.
    Never a ``too_large`` row, which no dispatch reached, and, while OCR
    is off, never a row an OCR extractor wrote, as
    ``extractors.stale_extractor_module`` keeps one. The startup sweep
    runs the same test in SQL (``Database.
    find_identification_refresh_attachment_filepaths``); the lookup adds
    the payload's prefix (``identification_refreshes``)."""
    if row["extraction_status"] == STATUS_TOO_LARGE:
        return False
    if not ocr_enabled and (row["extractor"] or "").partition("@")[0].endswith("-ocr"):
        return False
    identifier = row["identifier"]
    if identifier is None:
        return True
    return identifier != "" and _identifier_version(identifier) < CONTAINER_IDENTIFICATION_VERSION


def identification_refreshes(row: Mapping[str, Any], payload: bytes, *, ocr_enabled: bool) -> bool:
    """Whether the lookup re-extracts a cached row for its container
    certification (#1416): ``identification_due``, except a row with no
    identifier whose payload starts with neither the OLE2 nor a ZIP
    signature (a constant-size check). That row's result is kept: the
    apply phase records '' on it
    (``Database.certify_extraction_identifier``)."""
    if not identification_due(row, ocr_enabled=ocr_enabled):
        return False
    return row["identifier"] is not None or has_container_prefix(payload)


def _cache_hit_short_circuits(
    cached: Mapping[str, Any],
    attachment: Attachment,
    module: str,
    ocr_enabled: bool,
    max_bytes: int,
    *,
    max_ocr_pages: int,
    max_pdf_pages: int,
    max_extracted_chars: int,
) -> bool:
    """Return True when ``cached`` should short-circuit re-extraction.

    ``STATUS_SUCCESS`` rows with non-empty text are the obvious hit. The
    other statuses are also honored to spare the worker from redoing
    work whose result will not change between attempts:

    * ``STATUS_EMPTY`` — the payload genuinely had no text. Re-running
      will produce the same empty result.

    A ``success`` or ``empty`` row is not a hit when a configured limit
    that cut it has since been raised (``cap_raised``), or when it
    predates the cap record and lost text (``cap_bootstrap_due``, once):
    it is extracted again (#1418). The limits are the settings as
    ``extract`` takes them, ``max_pdf_pages`` and ``max_extracted_chars``
    0 for none.
    * ``STATUS_TOO_LARGE`` — while the payload still exceeds
      ``max_bytes``. Once the operator raises the cap far enough for it
      to fit, the row is stale and the payload is extracted (#693);
      the startup sweep runs ``too_large_fits``' comparison in SQL
      (``Database.find_fitting_too_large_attachment_filepaths``).
    * ``STATUS_UNSUPPORTED`` — while ``_unsupported_still_holds`` for
      the row's module and stamp: re-run once OCR is re-enabled, and an
      unstamped image "OCR disabled" row at once (#1415).
    * ``STATUS_FAILED`` — re-run if the cached row is older than
      ``_FAILED_CACHE_MAX_AGE`` (defense against a chronic failure
      burning OCR cycles on every reappearance), otherwise honor the
      cache.
    """
    status = cached["extraction_status"]
    if status in {STATUS_SUCCESS, STATUS_EMPTY} and (
        cap_raised(
            cached,
            ocr_enabled=ocr_enabled,
            max_ocr_pages=max_ocr_pages,
            max_pdf_pages=max_pdf_pages,
            max_extracted_chars=max_extracted_chars,
        )
        or cap_bootstrap_due(cached, ocr_enabled=ocr_enabled)
    ):
        return False
    if status == STATUS_SUCCESS:
        return bool(cached["extracted_text"])
    if status == STATUS_EMPTY:
        return True
    if status == STATUS_TOO_LARGE:
        return not too_large_fits(len(attachment.payload), max_bytes)
    if status == STATUS_UNSUPPORTED:
        return _unsupported_still_holds(
            cached["extraction_error"], module, ocr_enabled, cached["extractor"]
        )
    if status == STATUS_FAILED:
        cached_at = cached["extracted_at"]
        if not cached_at:
            return False
        try:
            stamp = datetime.fromisoformat(cached_at)
        except Exception:
            # ``fromisoformat`` raises ``ValueError`` on a malformed
            # ``extracted_at`` string and ``TypeError`` if the column
            # wasn't a string. Both indicate a corrupt cache row; fall
            # through and let the caller refresh it.
            return False
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return datetime.now(UTC) - stamp < _FAILED_CACHE_MAX_AGE
    return False


@dataclass
class AttachmentWritePlan:
    """Pre-computed attachment write plan, safe to apply inside a transaction.

    Captures everything the apply phase needs: the attachment metadata,
    the deterministic occurrence id, optional extraction result to
    persist (``None`` when a successful cache hit means nothing new to
    store), and the chunk + embedding payload for ``replace_message_chunks``.

    ``status`` is the final extraction status the apply phase will record
    (or skip recording, if ``extraction_to_persist is None``). It's
    duplicated on the plan so the caller can short-circuit cleanly when
    no chunkable text was produced.
    """

    attachment: Attachment
    occurrence_id: str
    status: str
    extraction_to_persist: ExtractionResult | None
    chunks: list[MessageChunk] = field(default_factory=list)
    embeddings_by_chunk_id: dict[str, list[float]] = field(default_factory=dict)
    # Whether a plan without text clears the attachment's stored chunks.
    # The batched indexer turns it off when another copy of the same bytes
    # in the message fills that slice.
    clears_stale_chunks: bool = True
    # The extraction error behind ``status`` and whether the result came
    # from the cache or the batch: the outcome counted once the message
    # commits (``record_committed_outcomes``).
    extraction_error: str | None = None
    cached: bool = False
    # The scanned PDF pages the OCR cap left unread in that result, or
    # ``None`` when unknown (#891).
    ocr_pages_skipped: int | None = None
    # That result's cap record (``CAP_COLUMNS``, #1418): a cut that
    # remains on a result served from the cache is counted at commit.
    caps: CapRecord = (None, None, None)
    # Whether the occurrence's text is complete (``occurrence_text_complete``)
    # and the extractor stamp of the result that applied, written with
    # its chunks (#1242).
    text_complete: bool | None = None
    text_extractor: str | None = None
    # Whether the occurrence carried a deferral mark before this pass
    # (#1236): one that now resolves is counted as resumed.
    was_deferred: bool = False
    # A deferred occurrence whose stored mark already says so (deferred,
    # ``text_complete`` 0): its apply writes nothing (#1236).
    mark_unchanged: bool = False
    # An occurrence resolved in an earlier pass, served from its cached
    # row as it stands to settle its payload's shared slice (#1236): it
    # writes no occurrence row and is not counted again; it only adds its
    # chunks to the slice.
    resolved_earlier: bool = False
    # Whether the plan holding this payload's chunk slice deletes stored
    # chunks it does not write. Off while another occurrence of the
    # payload in the message is deferred and holds chunks there (#1236).
    deletes_missing_chunks: bool = True

    @property
    def deferred(self) -> bool:
        return self.status == STATUS_DEFERRED


def occurrence_text_complete(
    *,
    status: str,
    extractor: str | None,
    extraction_complete: bool | None,
    payload_complete: bool,
) -> bool | None:
    """Whether an occurrence's indexed text is all of its text (#1242).

    ``None`` (not assessed) when the result came from an older version of
    its extractor (``EXTRACTOR_VERSIONS``; the startup sweep clears such
    occurrences, so publishing one does not restore them), or when the
    cached result carries no record (cached before schema v6). ``False``
    for a status that never certifies absence (failed, unsupported, too
    large, OCR disabled) and for a payload a parse cap emptied. Otherwise
    the result's own ``text_complete``."""
    if is_stale_extractor(extractor):
        return None
    if status not in {STATUS_SUCCESS, STATUS_EMPTY} or not payload_complete:
        return False
    return extraction_complete


def completeness_unrecorded(
    status: str, extractor: str | None, text_complete: object, *, ocr_enabled: bool
) -> bool:
    """Whether a cached result is refreshed for want of a completeness
    record (#1285): a ``success`` or ``empty`` row with none (cached
    before schema v6). A fresh result always records one, so a refreshed
    row is served from then on. An ``-ocr`` row is kept while OCR is off,
    as ``extractors.stale_extractor_module`` keeps a stale one: a refresh
    could only replace its text with "OCR disabled". The startup sweep
    re-queues by the same predicate."""
    if text_complete is not None or status not in {STATUS_SUCCESS, STATUS_EMPTY}:
        return False
    return ocr_enabled or not (extractor or "").partition("@")[0].endswith("-ocr")


def _resolve_extracted_text(
    *,
    attachment: Attachment,
    db: Database,
    ocr_enabled: bool,
    max_bytes: int,
    max_ocr_pages: int,
    max_extracted_chars: int | None,
    ocr_timeout_seconds: float | None = None,
    max_pdf_pages: int | None = None,
    batch_extractions: dict[tuple[str, str], ExtractionResult] | None = None,
    on_progress: Callable[[], None] | None = None,
    budget: ExtractionBudget | None = None,
    serve_cached: bool = False,
) -> tuple[
    str | None,
    str,
    ExtractionResult | None,
    str | None,
    bool,
    int | None,
    str | None,
    bool | None,
    CapRecord,
]:
    """Return ``(text, status, extraction_to_persist, error, cached,
    ocr_pages_skipped, extractor, text_complete, caps)``: ``error`` is the
    extraction error behind ``status``, ``cached`` whether the result was
    served without extracting and ``ocr_pages_skipped`` the result's
    OCR-cap count (``None`` when unknown), for the outcome counts;
    ``extractor`` is the result's stamp and ``text_complete`` whether it
    lost no text (``None`` when unknown, #1242); ``caps`` is its cap
    record (``CAP_COLUMNS``, #1418).

    A successful cache hit short-circuits and returns the stored text
    with ``extraction_to_persist=None`` so the apply phase does not
    re-write a row that already represents this content. Anything else
    — cache miss, cached non-success, cached row with empty text —
    re-runs the extractor and asks the apply phase to persist the
    fresh result.

    Results are keyed by content hash and the extractor module this
    occurrence selects (``extraction_cache_module``, #928), so an
    occurrence is served only what an extraction under its own label
    would produce.

    ``batch_extractions`` holds the results extracted earlier in the
    same batch, by that key: those are not committed yet, so the cache
    cannot serve them (#237). A reused one is still returned for
    persisting, since the message that extracted it may fail to commit.

    ``budget`` is the message's extraction budget: once it is exhausted
    an attachment the cache and the batch cannot serve is not extracted
    and comes back ``STATUS_DEFERRED``, with nothing to persist; each
    dispatch is charged its process launches and seconds.

    ``serve_cached`` (an occurrence whose result already applied, re-read
    so its payload's shared chunk slice can be settled, #1236) serves the
    cached row as it is, whatever its age or the settings, so finished
    work is not reopened; with no row it is resolved as usual.
    """
    module = extraction_cache_module(attachment)
    key = (attachment.content_hash, module)
    pending = batch_extractions.get(key) if batch_extractions is not None else None
    if pending is not None:
        text = pending.text if pending.status == STATUS_SUCCESS else None
        return (
            text,
            pending.status,
            pending,
            pending.error,
            True,
            pending.ocr_pages_skipped,
            pending.extractor,
            pending.text_complete,
            _result_caps(pending),
        )

    cached = db.get_attachment_extraction(attachment.content_hash, module)
    if serve_cached and cached is not None:
        text = cached["extracted_text"] if cached["extraction_status"] == STATUS_SUCCESS else None
        return (
            text,
            cached["extraction_status"],
            None,
            cached["extraction_error"],
            True,
            cached["ocr_pages_skipped"],
            cached["extractor"],
            None if cached["text_complete"] is None else bool(cached["text_complete"]),
            _row_caps(cached),
        )
    # A row written by an older version of a since-fixed extractor would
    # otherwise be served forever: it is re-extracted, and only by an
    # occurrence that selects its module.
    # A row with no completeness record is re-extracted once (#1285).
    # A row whose container certification is due (#1416) is checked here,
    # beside the stale stamp and before any status is honoured: an OLE2 or
    # ZIP payload cached before identification, or under an older
    # identification version, is identified and extracted again.
    if (
        cached is not None
        and not is_stale_extractor(cached["extractor"], ocr_enabled=ocr_enabled)
        and not identification_refreshes(cached, attachment.payload, ocr_enabled=ocr_enabled)
        and not completeness_unrecorded(
            cached["extraction_status"],
            cached["extractor"],
            cached["text_complete"],
            ocr_enabled=ocr_enabled,
        )
        and _cache_hit_short_circuits(
            cached,
            attachment,
            module,
            ocr_enabled,
            max_bytes,
            max_ocr_pages=max_ocr_pages,
            max_pdf_pages=max_pdf_pages or 0,
            max_extracted_chars=max_extracted_chars or 0,
        )
    ):
        # Successful hits return the stored text; non-success hits
        # (empty / unsupported / too_large / failed-within-window)
        # return ``None`` text so the caller skips chunking but the
        # apply phase also skips re-persisting an unchanged row.
        text = cached["extracted_text"] if cached["extraction_status"] == STATUS_SUCCESS else None
        return (
            text,
            cached["extraction_status"],
            None,
            cached["extraction_error"],
            True,
            cached["ocr_pages_skipped"],
            cached["extractor"],
            None if cached["text_complete"] is None else bool(cached["text_complete"]),
            _row_caps(cached),
        )

    if budget is not None and budget.exhausted():
        budget.deferred += 1
        return (None, STATUS_DEFERRED, None, None, False, None, None, None, _NO_CAPS)

    launches_before = process_launches()
    started = budget.clock() if budget is not None else 0.0
    result = extract_attachment(
        content_type=attachment.content_type,
        filename=attachment.filename,
        payload=attachment.payload,
        ocr_enabled=ocr_enabled,
        max_bytes=max_bytes,
        max_ocr_pages=max_ocr_pages,
        max_extracted_chars=max_extracted_chars,
        ocr_timeout_seconds=ocr_timeout_seconds,
        max_pdf_pages=max_pdf_pages,
        on_progress=on_progress,
    )
    if budget is not None:
        budget.charge(process_launches() - launches_before, budget.clock() - started)
    if batch_extractions is not None:
        batch_extractions[key] = result
    text = result.text if result.status == STATUS_SUCCESS else None
    return (
        text,
        result.status,
        result,
        result.error,
        False,
        result.ocr_pages_skipped,
        result.extractor,
        result.text_complete,
        _result_caps(result),
    )


def prepare_attachment_writes(
    *,
    attachment: Attachment,
    claimant_id: str,
    db: Database,
    chunk_target_tokens: int,
    chunk_max_tokens: int,
    chunk_overlap_tokens: int,
    ocr_enabled: bool,
    max_bytes: int,
    max_ocr_pages: int,
    occurrence_index: int = 0,
    max_extracted_chars: int | None = None,
    ocr_timeout_seconds: float | None = None,
    max_pdf_pages: int | None = None,
    batch_extractions: dict[tuple[str, str], ExtractionResult] | None = None,
    on_progress: Callable[[], None] | None = None,
    budget: ExtractionBudget | None = None,
    serve_cached: bool = False,
) -> AttachmentWritePlan:
    """Compute everything needed to write one attachment occurrence.

    Reads the extraction cache, runs the extractor when needed, then
    chunks. Pure read + CPU — no DB writes. Safe to call before opening
    the indexer's outer transaction; the apply phase will commit the DB
    writes inside that transaction.

    The returned plan's ``embeddings_by_chunk_id`` is empty: the caller
    diffs ``chunks`` against the stored chunk IDs and embeds the new
    ones before calling ``apply_attachment_writes``. The cross-message
    batched indexer does this so a single ``embed_batch`` call can
    cover chunks from many messages in one HTTP round-trip.

    The function does not raise for benign extraction outcomes
    (``unsupported``, ``empty``, ``too_large``) — those land on the plan
    as a status-only row to persist, with no chunks. Hard failures
    (``Database`` I/O) still propagate so the caller
    can decide whether to retry the message.

    ``on_progress`` is passed to the extractor, which calls it after
    each page it reads (#485). ``budget`` is passed to
    ``_resolve_extracted_text``: a deferred occurrence's plan has
    ``STATUS_DEFERRED`` and no chunks, and keeps the chunks stored for
    it (#1236). With ``serve_cached`` a plan served from its cached row
    as it stands is ``resolved_earlier``: it only contributes its chunks
    to its payload's slice.
    """
    occurrence_id = attachment_occurrence_id(
        claimant_id=claimant_id,
        content_hash=attachment.content_hash,
        filename=attachment.filename,
        occurrence_index=occurrence_index,
    )

    (
        text,
        status,
        extraction_to_persist,
        extraction_error,
        cached,
        ocr_pages_skipped,
        extractor,
        extraction_complete,
        caps,
    ) = _resolve_extracted_text(
        attachment=attachment,
        db=db,
        ocr_enabled=ocr_enabled,
        max_bytes=max_bytes,
        max_ocr_pages=max_ocr_pages,
        max_extracted_chars=max_extracted_chars,
        ocr_timeout_seconds=ocr_timeout_seconds,
        max_pdf_pages=max_pdf_pages,
        batch_extractions=batch_extractions,
        on_progress=on_progress,
        budget=budget,
        serve_cached=serve_cached,
    )
    if status == STATUS_DEFERRED:
        return AttachmentWritePlan(
            attachment=attachment,
            occurrence_id=occurrence_id,
            status=status,
            extraction_to_persist=None,
            clears_stale_chunks=False,
            text_complete=False,
        )
    text_complete = occurrence_text_complete(
        status=status,
        extractor=extractor,
        extraction_complete=extraction_complete,
        payload_complete=attachment.payload_complete,
    )
    resolved_earlier = serve_cached and cached and extraction_to_persist is None

    if status != STATUS_SUCCESS or not text:
        # No usable text for chunking. Still searchable by filename / MIME
        # via the FTS row written in apply. ``unsupported`` and ``too_large``
        # log at debug because they are common (zip files, huge backups);
        # the periodic aggregate counts them at INFO (#871).
        if status in {STATUS_UNSUPPORTED, STATUS_TOO_LARGE}:
            log.debug("attachment status=%s — no chunks", status)
        return AttachmentWritePlan(
            attachment=attachment,
            occurrence_id=occurrence_id,
            status=status,
            extraction_to_persist=extraction_to_persist,
            extraction_error=extraction_error,
            cached=cached,
            ocr_pages_skipped=ocr_pages_skipped,
            caps=caps,
            text_complete=text_complete,
            text_extractor=extractor,
            resolved_earlier=resolved_earlier,
        )

    # Chunk the extracted text. The chunker takes
    # ``message_pk`` = composite of claimant_id + content_hash so chunk
    # IDs are stable across re-runs of the same attachment in the same
    # email and distinct from body chunks (whose pk = claimant_id alone).
    chunk_pk = f"{claimant_id}::{attachment.content_hash}"
    chunks = chunk_message(
        message_pk=chunk_pk,
        body_text=text,
        kind="attachment",
        target_tokens=chunk_target_tokens,
        max_tokens=chunk_max_tokens,
        overlap_tokens=chunk_overlap_tokens,
    )
    return AttachmentWritePlan(
        attachment=attachment,
        occurrence_id=occurrence_id,
        status=status,
        extraction_to_persist=extraction_to_persist,
        chunks=chunks,
        extraction_error=extraction_error,
        cached=cached,
        ocr_pages_skipped=ocr_pages_skipped,
        caps=caps,
        text_complete=text_complete,
        text_extractor=extractor,
        resolved_earlier=resolved_earlier,
    )


def apply_attachment_writes(
    *,
    plan: AttachmentWritePlan,
    claimant_id: str,
    thread_id: str,
    db: Database,
    purged_extractions: dict[tuple[str, str], sqlite3.Row] | None = None,
) -> None:
    """Persist a prepared attachment plan. DB writes only.

    Designed to be called inside the indexer's outer
    ``with db.transaction():`` block — no network, no extraction, no
    embedding happens here, so the SQLite write transaction stays open
    for only as long as the inserts and FTS / vec sync take.

    Three layers cooperate:

    * ``attachments`` row records this specific occurrence (a forwarded
      PDF gets one row per email it appeared in) so filename / MIME
      filters work uniformly.
    * ``attachment_extractions`` is keyed by content hash and extractor
      module, so a single ``store_attachment_extraction`` covers any
      future occurrences of the same payload that select the same
      extractor — and is skipped entirely on a cache hit. The
      occurrence row records the module, naming the row it uses.
    * ``message_chunks`` carries per-occurrence chunks of the extracted
      text so any chunk hit lifts the parent thread of the email that
      carried it.
    """
    if plan.mark_unchanged:
        # Deferred again; the stored row already records it (#1236).
        return
    if not plan.resolved_earlier:
        _write_occurrence(
            plan,
            claimant_id=claimant_id,
            thread_id=thread_id,
            db=db,
            purged_extractions=purged_extractions,
        )
    if plan.deferred:
        return
    _write_slice(plan, claimant_id=claimant_id, thread_id=thread_id, db=db)


def _write_occurrence(
    plan: AttachmentWritePlan,
    *,
    claimant_id: str,
    thread_id: str,
    db: Database,
    purged_extractions: dict[tuple[str, str], sqlite3.Row] | None,
) -> None:
    """The occurrence's row, its cached result and its completeness (or
    its deferral mark)."""
    module = extraction_cache_module(plan.attachment)
    db.upsert_attachment(
        claimant_id=claimant_id,
        thread_id=thread_id,
        attachment_id=plan.attachment.content_hash,
        filename=plan.attachment.filename,
        content_type=plan.attachment.content_type,
        size_bytes=plan.attachment.size,
        occurrence_id=plan.occurrence_id,
        extractor_module=module,
    )
    if plan.deferred:
        # Nothing was extracted: no cache row is written, and the chunks
        # stored for the occurrence stay (#1236).
        db.mark_attachment_extraction_deferred(plan.occurrence_id)
        return

    if plan.extraction_to_persist is not None:
        result = plan.extraction_to_persist
        db.store_attachment_extraction(
            attachment_id=plan.attachment.content_hash,
            extractor_module=module,
            extraction_status=result.status,
            extractor=result.extractor,
            extracted_text=result.text,
            extraction_error=result.error,
            ocr_pages_skipped=result.ocr_pages_skipped,
            text_complete=result.text_complete,
            ocr_pages_cap=result.ocr_pages_cap,
            digital_pages_cap=result.digital_pages_cap,
            extracted_chars_cap=result.extracted_chars_cap,
            identifier=result.identifier,
        )
    else:
        if purged_extractions:
            # A cache hit whose row an earlier message of the batch purged
            # (it dropped the last occurrence using it, #1375): put it back
            # as it was, so this occurrence has its cached result.
            purged = purged_extractions.get((plan.attachment.content_hash, module))
            if purged is not None:
                db.restore_attachment_extraction(purged)
        # A cache hit on a row with no identifier whose payload has neither
        # the OLE2 nor a ZIP signature: no identification applies, so the
        # row is certified '' and kept (#1416). A no-op on any other row.
        if not has_container_prefix(plan.attachment.payload):
            db.certify_extraction_identifier(plan.attachment.content_hash, module)
    # Whether the chunks written below hold all of this occurrence's
    # text (#1242): in the caller's transaction, so it commits and rolls
    # back with them.
    db.set_attachment_text_complete(plan.occurrence_id, plan.text_complete, plan.text_extractor)


def _write_slice(
    plan: AttachmentWritePlan, *, claimant_id: str, thread_id: str, db: Database
) -> None:
    """The payload's chunk slice: the plan's chunks, or clearing it."""
    if not plan.chunks or plan.status != STATUS_SUCCESS:
        # No usable text now: drop chunks an earlier (since-superseded)
        # extraction of this attachment left behind, or they stay
        # searchable for good. This holds for a reused row too: another
        # message's re-extraction may have stamped it current after this
        # message indexed the stale text. Costs one indexed SELECT when
        # there is nothing to delete.
        if not plan.clears_stale_chunks:
            return
        db.replace_message_chunks(
            claimant_id=claimant_id,
            thread_id=thread_id,
            chunks=[],
            embeddings_by_chunk_id={},
            attachment_id=plan.attachment.content_hash,
        )
        return

    db.replace_message_chunks(
        claimant_id=claimant_id,
        thread_id=thread_id,
        chunks=plan.chunks,
        embeddings_by_chunk_id=plan.embeddings_by_chunk_id,
        attachment_id=plan.attachment.content_hash,
        delete_missing=plan.deletes_missing_chunks,
    )
