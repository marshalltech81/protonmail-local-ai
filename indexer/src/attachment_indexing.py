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
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Lock

from .chunker import MessageChunk, chunk_message
from .database import Database
from .extractors import (
    LEGACY_OLE2_ERROR,
    OCR_DISABLED_ERROR,
    PERMANENT_FAILURE_ERRORS,
    SCANNED_PDF_OCR_DISABLED_ERROR,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_TOO_LARGE,
    STATUS_UNSUPPORTED,
    ExtractionResult,
    drain_extractor_counts,
    resolved_extractor_module,
    stale_extractor_module,
)
from .extractors import (
    extract as extract_attachment,
)
from .parser import Attachment

log = logging.getLogger("indexer.attachments")

# Outcomes the periodic attachments aggregate counts (#871): the
# extraction statuses, with ``unsupported`` for want of OCR split out as
# ``ocr_disabled``.
ATTACHMENT_OUTCOMES: tuple[str, ...] = (
    STATUS_SUCCESS,
    STATUS_FAILED,
    STATUS_UNSUPPORTED,
    STATUS_TOO_LARGE,
    "ocr_disabled",
    STATUS_EMPTY,
)


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
    ``parser_caps_messages`` and ``warnings_suppressed``). Counts
    only: no filename, type or text.
    """

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = Lock()

    def record(self, status: str, error: str | None, *, cached: bool) -> None:
        outcome = status
        if status == STATUS_UNSUPPORTED and (error or "").startswith(OCR_DISABLED_ERROR):
            outcome = "ocr_disabled"
        with self._lock:
            self._counts[outcome] += 1
            if cached:
                self._counts["cached"] += 1

    def drain(self) -> dict[str, int]:
        """Return every outcome's count plus ``cached`` and the extractor
        counts, and reset them."""
        with self._lock:
            counts, self._counts = self._counts, Counter()
        drained = {name: counts[name] for name in (*ATTACHMENT_OUTCOMES, "cached")}
        drained.update(drain_extractor_counts())
        return drained


attachment_outcomes = AttachmentOutcomeCounts()

# Fields of the summary line after ``n``, in order.
_SUMMARY_FIELDS = (
    *ATTACHMENT_OUTCOMES,
    "cached",
    "pdf_pages_failed",
    "pdf_pages_unrecovered",
    "ocr_capped_pdfs",
    "ocr_pages_skipped",
    "ocr_capped_images",
    "extractor_caps",
    "parser_caps_messages",
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
    "pdf_pages_unrecovered",
    "ocr_capped_pdfs",
    "ocr_pages_skipped",
    "ocr_capped_images",
    "extractor_caps",
    "parser_caps_messages",
    "warnings_suppressed",
)


def record_committed_outcomes(plans: list[AttachmentWritePlan]) -> None:
    """Count the outcome of each plan of a message whose writes committed."""
    for plan in plans:
        attachment_outcomes.record(plan.status, plan.extraction_error, cached=plan.cached)


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


def _unsupported_still_holds(error: str | None, attachment: Attachment, ocr_enabled: bool) -> bool:
    """Whether an ``unsupported`` result also applies to ``attachment``.

    Results are shared by content hash, but dispatch reads each
    occurrence's MIME type and filename, so the same bytes can arrive as
    ``.bin`` first and ``.txt`` later (#210). An "OCR disabled" result
    holds until OCR is turned on for an occurrence that needs OCR: an
    image, or a PDF when the PDF extractor wrote the result (it found no
    digital text layer). An OLE2 result also holds for an occurrence
    that selects the DOCX or XLSX extractor, which the dispatcher would
    reject the same way (#694); an occurrence labelled ``.doc`` / ``.xls``
    selects the legacy extractor instead and re-runs it (#935). An
    encrypted PDF, a PDF over a pypdf limit or a workbook over the
    eager-part budget holds for every occurrence: the extractor read the
    bytes as its format before declining, so they decide the outcome,
    not the label (#931). Any other result holds only while this
    occurrence selects no extractor.
    """
    module = resolved_extractor_module(attachment.content_type, attachment.filename)
    error = error or ""
    needs_ocr = module == "image" or (module == "pdf" and error == SCANNED_PDF_OCR_DISABLED_ERROR)
    if "OCR disabled" in error and needs_ocr:
        return not ocr_enabled
    if error == LEGACY_OLE2_ERROR and module in {"docx", "xlsx"}:
        return True
    if error in PERMANENT_FAILURE_ERRORS:
        return True
    return module is None


def reruns_once_ocr_is_on(error: str | None, content_type: str, filename: str) -> bool:
    """Whether reprocessing an occurrence (by its MIME type and filename)
    of bytes cached as an ``unsupported`` result with ``error`` re-runs
    extraction once OCR is on. The startup sweep re-queues by this, so
    it shares ``_unsupported_still_holds`` with the cache check."""
    occurrence = Attachment(filename=filename, content_type=content_type, size=0)
    return not _unsupported_still_holds(error, occurrence, ocr_enabled=True)


def too_large_fits(size: int, max_bytes: int) -> bool:
    """Whether bytes of ``size`` cached as ``too_large`` now fit under
    ``max_bytes``, the same comparison ``extractors.extract`` makes, so
    re-extracting them can no longer record ``too_large``."""
    return size <= max_bytes


def _cache_hit_short_circuits(
    cached: dict, attachment: Attachment, ocr_enabled: bool, max_bytes: int
) -> bool:
    """Return True when ``cached`` should short-circuit re-extraction.

    ``STATUS_SUCCESS`` rows with non-empty text are the obvious hit. The
    other statuses are also honored to spare the worker from redoing
    work whose result will not change between attempts:

    * ``STATUS_EMPTY`` — the payload genuinely had no text. Re-running
      will produce the same empty result.
    * ``STATUS_TOO_LARGE`` — while the payload still exceeds
      ``max_bytes``. Once the operator raises the cap far enough for it
      to fit, the row is stale and the payload is extracted (#693);
      ``too_large_fits`` is the same predicate for the startup sweep.
    * ``STATUS_UNSUPPORTED`` — while ``_unsupported_still_holds`` for
      this occurrence: re-run once OCR is re-enabled, or when this
      occurrence's metadata selects an extractor.
    * ``STATUS_FAILED`` — re-run if the cached row is older than
      ``_FAILED_CACHE_MAX_AGE`` (defense against a chronic failure
      burning OCR cycles on every reappearance), otherwise honor the
      cache.
    """
    status = cached["extraction_status"]
    if status == STATUS_SUCCESS:
        return bool(cached["extracted_text"])
    if status == STATUS_EMPTY:
        return True
    if status == STATUS_TOO_LARGE:
        return not too_large_fits(len(attachment.payload), max_bytes)
    if status == STATUS_UNSUPPORTED:
        return _unsupported_still_holds(cached["extraction_error"], attachment, ocr_enabled)
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
    batch_extractions: dict[str, ExtractionResult] | None = None,
    on_progress: Callable[[], None] | None = None,
) -> tuple[str | None, str, ExtractionResult | None, str | None, bool]:
    """Return ``(text, status, extraction_to_persist, error, cached)``:
    ``error`` is the extraction error behind ``status`` and ``cached``
    whether the result was served without extracting, for the outcome
    counts.

    A successful cache hit short-circuits and returns the stored text
    with ``extraction_to_persist=None`` so the apply phase does not
    re-write a row that already represents this content. Anything else
    — cache miss, cached non-success, cached row with empty text —
    re-runs the extractor and asks the apply phase to persist the
    fresh result.

    ``batch_extractions`` holds the results extracted earlier in the
    same batch, by content hash: those are not committed yet, so the
    cache cannot serve them (#237). A reused one is still returned for
    persisting, since the message that extracted it may fail to commit.
    """
    pending = (
        batch_extractions.get(attachment.content_hash) if batch_extractions is not None else None
    )
    if pending is not None and (
        pending.status != STATUS_UNSUPPORTED
        or _unsupported_still_holds(pending.error, attachment, ocr_enabled)
    ):
        text = pending.text if pending.status == STATUS_SUCCESS else None
        return text, pending.status, pending, pending.error, True

    cached = db.get_attachment_extraction(attachment.content_hash)
    # A row written by an older version of a since-fixed extractor would
    # otherwise be served forever. Re-run the module that wrote it, from
    # whichever occurrence of the bytes arrives: the row is shared by
    # content hash, so an occurrence whose own metadata resolves to
    # another extractor must still refresh it with the same one.
    refresh_module = (
        stale_extractor_module(cached["extractor"], ocr_enabled=ocr_enabled)
        if cached is not None
        else None
    )
    if (
        cached is not None
        and refresh_module is None
        and _cache_hit_short_circuits(cached, attachment, ocr_enabled, max_bytes)
    ):
        # Successful hits return the stored text; non-success hits
        # (empty / unsupported / too_large / failed-within-window)
        # return ``None`` text so the caller skips chunking but the
        # apply phase also skips re-persisting an unchanged row.
        text = cached["extracted_text"] if cached["extraction_status"] == STATUS_SUCCESS else None
        return text, cached["extraction_status"], None, cached["extraction_error"], True

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
        module_override=refresh_module,
        on_progress=on_progress,
    )
    if batch_extractions is not None:
        batch_extractions[attachment.content_hash] = result
    text = result.text if result.status == STATUS_SUCCESS else None
    return text, result.status, result, result.error, False


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
    batch_extractions: dict[str, ExtractionResult] | None = None,
    on_progress: Callable[[], None] | None = None,
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
    each page it reads (#485).
    """
    occurrence_id = attachment_occurrence_id(
        claimant_id=claimant_id,
        content_hash=attachment.content_hash,
        filename=attachment.filename,
        occurrence_index=occurrence_index,
    )

    text, status, extraction_to_persist, extraction_error, cached = _resolve_extracted_text(
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
    )

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
    )


def apply_attachment_writes(
    *,
    plan: AttachmentWritePlan,
    claimant_id: str,
    thread_id: str,
    db: Database,
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
    * ``attachment_extractions`` is keyed by content hash, so a single
      ``store_attachment_extraction`` covers any future occurrences of
      the same payload — and is skipped entirely on a cache hit.
    * ``message_chunks`` carries per-occurrence chunks of the extracted
      text so any chunk hit lifts the parent thread of the email that
      carried it.
    """
    db.upsert_attachment(
        claimant_id=claimant_id,
        thread_id=thread_id,
        attachment_id=plan.attachment.content_hash,
        filename=plan.attachment.filename,
        content_type=plan.attachment.content_type,
        size_bytes=plan.attachment.size,
        occurrence_id=plan.occurrence_id,
    )

    if plan.extraction_to_persist is not None:
        result = plan.extraction_to_persist
        db.store_attachment_extraction(
            attachment_id=plan.attachment.content_hash,
            extraction_status=result.status,
            extractor=result.extractor,
            extracted_text=result.text,
            extraction_error=result.error,
        )

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
    )
