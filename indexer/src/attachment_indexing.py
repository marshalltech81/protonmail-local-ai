"""
Per-attachment indexing pipeline.

Lives in its own module so ``main.py``'s file-pipeline orchestration
stays small enough to read.

Two-phase shape:

* ``prepare_attachment_writes`` runs everything that must NOT happen
  inside a SQLite write transaction — extractor (OCR / pypdf / openpyxl)
  CPU work and the per-chunk ``embedder.embed`` HTTP roundtrips against
  Ollama. It only reads the DB (cache lookups and existing chunk-id
  diffing). The output is a fully-materialised ``AttachmentWritePlan``
  that the caller can hold in memory until it's ready to commit.

* ``apply_attachment_writes`` performs only DB writes and is intended
  to be called inside the indexer's outer ``with db.transaction():``
  block. No network, no extraction, no embedding — every slow operation
  has already happened.

* ``process_attachment`` is a thin convenience wrapper that runs both
  phases back-to-back. Tests and any caller that doesn't need the
  split go through this. The indexer pipeline does NOT use it because
  conflating phases reintroduces the original bug where Ollama latency
  blocks the SQLite write transaction.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .chunker import MessageChunk, chunk_message
from .database import Database
from .embedder import Embedder
from .extractors import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_TOO_LARGE,
    STATUS_UNSUPPORTED,
    ExtractionResult,
)
from .extractors import (
    extract as extract_attachment,
)
from .parser import Attachment

log = logging.getLogger("indexer.attachments")


def attachment_occurrence_id(
    *,
    message_id: str,
    content_hash: str,
    filename: str,
    occurrence_index: int,
) -> str:
    """Deterministic id for one attachment occurrence on one message.

    Same payload appearing twice on the same message (e.g. inline + as
    a regular attachment) gets two distinct rows differentiated by
    ``occurrence_index``. The hash inputs and order are part of the
    on-disk identity and must not change without a schema bump — both
    the indexer write path and ``Database.upsert_attachment`` derive ids
    from this function so they cannot drift.
    """
    return hashlib.sha256(
        f"{message_id}\0{content_hash}\0{filename}\0{occurrence_index}".encode()
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


def _cache_hit_short_circuits(cached: dict, ocr_enabled: bool) -> bool:
    """Return True when ``cached`` should short-circuit re-extraction.

    ``STATUS_SUCCESS`` rows with non-empty text are the obvious hit. The
    other statuses are also honored to spare the worker from redoing
    work whose result will not change between attempts:

    * ``STATUS_EMPTY`` — the payload genuinely had no text. Re-running
      will produce the same empty result.
    * ``STATUS_UNSUPPORTED`` / ``STATUS_TOO_LARGE`` — the dispatch table
      and size cap are runtime config; if either changed, the operator
      restarted the indexer and the cache is the wrong place to resolve
      the version skew (a future schema bump or explicit cache clear
      handles it). One special case: an image cached as
      ``unsupported`` because OCR was disabled at the time should be
      re-run when the operator re-enables it, since the cached row's
      "OCR disabled" reason is no longer current.
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
        return True
    if status == STATUS_UNSUPPORTED:
        # Re-run an image that was cached as unsupported because OCR was
        # off at the time, now that OCR is on. Other unsupported reasons
        # (no extractor for this MIME type) stay cached.
        error = cached["extraction_error"] or ""
        if ocr_enabled and "OCR disabled" in error:
            return False
        return True
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
    extraction_reused: bool
    chunks: list[MessageChunk] = field(default_factory=list)
    embeddings_by_chunk_id: dict[str, list[float]] = field(default_factory=dict)


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
) -> tuple[str | None, str, ExtractionResult | None, bool]:
    """Return ``(text, status, extraction_to_persist, extraction_reused)``.

    A successful cache hit short-circuits and returns the stored text
    with ``extraction_to_persist=None`` so the apply phase does not
    re-write a row that already represents this content. Anything else
    — cache miss, cached non-success, cached row with empty text —
    re-runs the extractor and asks the apply phase to persist the
    fresh result.
    """
    cached = db.get_attachment_extraction(attachment.content_hash)
    if cached is not None and _cache_hit_short_circuits(cached, ocr_enabled):
        # Successful hits return the stored text; non-success hits
        # (empty / unsupported / too_large / failed-within-window)
        # return ``None`` text so the caller skips chunking but the
        # apply phase also skips re-persisting an unchanged row.
        text = cached["extracted_text"] if cached["extraction_status"] == STATUS_SUCCESS else None
        return text, cached["extraction_status"], None, True

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
    )
    text = result.text if result.status == STATUS_SUCCESS else None
    return text, result.status, result, False


def prepare_attachment_writes(
    *,
    attachment: Attachment,
    message_id: str,
    db: Database,
    embedder: Embedder,
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
) -> AttachmentWritePlan:
    """Compute everything needed to write one attachment occurrence.

    Reads the extraction cache, runs the extractor when needed, then
    chunks and embeds. Pure read + CPU + outbound HTTP — no DB writes.
    Safe to call before opening the indexer's outer transaction; the
    apply phase will commit the DB writes inside that transaction.

    The function does not raise for benign extraction outcomes
    (``unsupported``, ``empty``, ``too_large``) — those land on the plan
    as a status-only row to persist, with no chunks. Hard failures
    (``Database`` I/O, ``Embedder`` I/O) still propagate so the caller
    can decide whether to retry the message.
    """
    occurrence_id = attachment_occurrence_id(
        message_id=message_id,
        content_hash=attachment.content_hash,
        filename=attachment.filename,
        occurrence_index=occurrence_index,
    )

    text, status, extraction_to_persist, extraction_reused = _resolve_extracted_text(
        attachment=attachment,
        db=db,
        ocr_enabled=ocr_enabled,
        max_bytes=max_bytes,
        max_ocr_pages=max_ocr_pages,
        max_extracted_chars=max_extracted_chars,
        ocr_timeout_seconds=ocr_timeout_seconds,
        max_pdf_pages=max_pdf_pages,
    )

    if status != STATUS_SUCCESS or not text:
        # No usable text for chunking. Still searchable by filename / MIME
        # via the FTS row written in apply. ``unsupported`` and ``too_large``
        # log at debug because they are common (zip files, huge backups).
        if status in {STATUS_UNSUPPORTED, STATUS_TOO_LARGE}:
            log.debug(
                "attachment %s status=%s (%s) — no chunks",
                attachment.filename,
                status,
                attachment.content_type,
            )
        return AttachmentWritePlan(
            attachment=attachment,
            occurrence_id=occurrence_id,
            status=status,
            extraction_to_persist=extraction_to_persist,
            extraction_reused=extraction_reused,
        )

    # Chunk the extracted text and embed. The chunker takes
    # ``message_pk`` = composite of message_id + content_hash so chunk
    # IDs are stable across re-runs of the same attachment in the same
    # email and distinct from body chunks (whose pk = message_id alone).
    chunk_pk = f"{message_id}::{attachment.content_hash}"
    chunks = chunk_message(
        message_pk=chunk_pk,
        body_text=text,
        target_tokens=chunk_target_tokens,
        max_tokens=chunk_max_tokens,
        overlap_tokens=chunk_overlap_tokens,
    )
    stored_ids = db.get_chunk_ids_for_message(message_id, attachment_id=attachment.content_hash)
    new_chunks = [c for c in chunks if c.chunk_id not in stored_ids]
    # Embedding happens HERE — outside any DB transaction the caller owns.
    # A multi-page PDF with N new chunks runs N synchronous Ollama calls
    # before the apply phase needs to grab the SQLite write lock.
    embeddings_by_chunk_id = {chunk.chunk_id: embedder.embed(chunk.text) for chunk in new_chunks}

    return AttachmentWritePlan(
        attachment=attachment,
        occurrence_id=occurrence_id,
        status=status,
        extraction_to_persist=extraction_to_persist,
        extraction_reused=extraction_reused,
        chunks=chunks,
        embeddings_by_chunk_id=embeddings_by_chunk_id,
    )


def apply_attachment_writes(
    *,
    plan: AttachmentWritePlan,
    message_id: str,
    thread_id: str,
    db: Database,
) -> dict[str, int]:
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
    summary = {
        "occurrences_inserted": 0,
        "extractions_reused": 1 if plan.extraction_reused else 0,
        "extractions_run": 0 if plan.extraction_reused else 1,
        "chunks_inserted": 0,
        "chunks_kept": 0,
    }

    if db.upsert_attachment(
        message_id=message_id,
        thread_id=thread_id,
        attachment_id=plan.attachment.content_hash,
        filename=plan.attachment.filename,
        content_type=plan.attachment.content_type,
        size_bytes=plan.attachment.size,
        occurrence_id=plan.occurrence_id,
    ):
        summary["occurrences_inserted"] = 1

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
        return summary

    write_summary = db.replace_message_chunks(
        message_id=message_id,
        thread_id=thread_id,
        chunks=plan.chunks,
        embeddings_by_chunk_id=plan.embeddings_by_chunk_id,
        attachment_id=plan.attachment.content_hash,
    )
    summary["chunks_inserted"] = write_summary["inserted"]
    summary["chunks_kept"] = write_summary["kept"]
    return summary


def process_attachment(
    *,
    attachment: Attachment,
    message_id: str,
    thread_id: str,
    db: Database,
    embedder: Embedder,
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
) -> dict[str, int]:
    """Single-call wrapper: prepare + apply for one attachment occurrence.

    Convenience for tests and any caller that does not need the
    transaction split. The indexer's main pipeline calls
    ``prepare_attachment_writes`` outside the transaction and
    ``apply_attachment_writes`` inside, so ``embedder.embed`` calls do
    not block the SQLite write lock.
    """
    plan = prepare_attachment_writes(
        attachment=attachment,
        message_id=message_id,
        db=db,
        embedder=embedder,
        chunk_target_tokens=chunk_target_tokens,
        chunk_max_tokens=chunk_max_tokens,
        chunk_overlap_tokens=chunk_overlap_tokens,
        ocr_enabled=ocr_enabled,
        max_bytes=max_bytes,
        max_ocr_pages=max_ocr_pages,
        occurrence_index=occurrence_index,
        max_extracted_chars=max_extracted_chars,
        ocr_timeout_seconds=ocr_timeout_seconds,
        max_pdf_pages=max_pdf_pages,
    )
    return apply_attachment_writes(
        plan=plan,
        message_id=message_id,
        thread_id=thread_id,
        db=db,
    )
