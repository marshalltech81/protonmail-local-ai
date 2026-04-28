"""
Per-attachment indexing pipeline.

Lives in its own module so ``main.py``'s file-pipeline orchestration
stays small enough to read.

The function is intentionally synchronous and connection-bound: every
write goes through ``Database`` (which serializes via the per-instance
RLock), and the embedder is a simple httpx client. There is no batching
across attachments — each one is its own logical unit so a malformed
PDF cannot dead-letter the parent message's indexing job.
"""

from __future__ import annotations

import hashlib
import logging

from .chunker import chunk_message
from .database import Database
from .embedder import Embedder
from .extractors import (
    STATUS_SUCCESS,
    STATUS_TOO_LARGE,
    STATUS_UNSUPPORTED,
)
from .extractors import (
    extract as extract_attachment,
)
from .parser import Attachment

log = logging.getLogger("indexer.attachments")


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
) -> dict[str, int]:
    """Index one attachment occurrence.

    Three layers cooperate:

    * ``attachments`` row records this specific occurrence (a forwarded
      PDF gets one row per email it appeared in) so filename / MIME
      filters work uniformly.
    * ``attachment_extractions`` is keyed by content hash, so the
      expensive extract (Tesseract / pypdf / openpyxl) runs at most
      once per unique payload regardless of forwarding count.
    * ``message_chunks`` carries per-occurrence chunks of the extracted
      text — one set per (message_id, attachment_id) so any chunk hit
      lifts the parent thread of the email that carried it.

    Returns a small summary so the caller can roll up per-attachment
    counts for telemetry. The
    function does not raise for benign extraction outcomes
    (``unsupported``, ``empty``, ``too_large``) — those are reflected
    in the returned counts and the cached status row. Hard failures
    (DB errors, embedder I/O) propagate so the caller can decide
    whether to retry.
    """
    summary = {
        "occurrences_inserted": 0,
        "extractions_reused": 0,
        "extractions_run": 0,
        "chunks_inserted": 0,
        "chunks_kept": 0,
    }

    occurrence_id = hashlib.sha256(
        (
            f"{message_id}\0{attachment.content_hash}\0{attachment.filename}\0{occurrence_index}"
        ).encode()
    ).hexdigest()

    if db.upsert_attachment(
        message_id=message_id,
        thread_id=thread_id,
        attachment_id=attachment.content_hash,
        filename=attachment.filename,
        content_type=attachment.content_type,
        size_bytes=attachment.size,
        occurrence_id=occurrence_id,
    ):
        summary["occurrences_inserted"] = 1

    cached = db.get_attachment_extraction(attachment.content_hash)
    if (
        cached is not None
        and cached["extraction_status"] == STATUS_SUCCESS
        and cached["extracted_text"]
    ):
        text = cached["extracted_text"]
        status = cached["extraction_status"]
        summary["extractions_reused"] = 1
    else:
        result = extract_attachment(
            content_type=attachment.content_type,
            filename=attachment.filename,
            payload=attachment.payload,
            ocr_enabled=ocr_enabled,
            max_bytes=max_bytes,
            max_ocr_pages=max_ocr_pages,
        )
        db.store_attachment_extraction(
            attachment_id=attachment.content_hash,
            extraction_status=result.status,
            extractor=result.extractor,
            extracted_text=result.text,
            extraction_error=result.error,
        )
        text = result.text if result.status == STATUS_SUCCESS else None
        status = result.status
        summary["extractions_run"] = 1

    if status != STATUS_SUCCESS or not text:
        # No usable text for chunking. Still searchable by filename / MIME
        # via the FTS row written above. ``unsupported`` and ``too_large``
        # log at debug because they are common (zip files, huge backups);
        # ``failed`` already logged at warning by the dispatcher.
        if status in {STATUS_UNSUPPORTED, STATUS_TOO_LARGE}:
            log.debug(
                "attachment %s status=%s (%s) — no chunks",
                attachment.filename,
                status,
                attachment.content_type,
            )
        return summary

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
    embeddings_by_chunk_id = {chunk.chunk_id: embedder.embed(chunk.text) for chunk in new_chunks}
    write_summary = db.replace_message_chunks(
        message_id=message_id,
        thread_id=thread_id,
        chunks=chunks,
        embeddings_by_chunk_id=embeddings_by_chunk_id,
        attachment_id=attachment.content_hash,
    )
    summary["chunks_inserted"] = write_summary["inserted"]
    summary["chunks_kept"] = write_summary["kept"]
    return summary
