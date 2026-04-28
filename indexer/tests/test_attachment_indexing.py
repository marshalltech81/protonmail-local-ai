"""Tests for the per-attachment indexing pipeline."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

from src import attachment_indexing
from src.attachment_indexing import process_attachment
from src.database import EMBEDDING_DIM, Database
from src.extractors import STATUS_FAILED, STATUS_SUCCESS, ExtractionResult
from src.parser import Attachment

from tests.conftest import make_message, make_thread


def _attachment(payload: bytes = b"hello from an attachment") -> Attachment:
    return Attachment(
        filename="note.txt",
        content_type="text/plain",
        size=len(payload),
        payload=payload,
        content_hash=hashlib.sha256(payload).hexdigest(),
    )


def test_successful_cached_extraction_is_reused(tmp_path, monkeypatch):
    db = Database(tmp_path / "mail.db")
    db.upsert_thread(
        make_thread(
            messages=[make_message(message_id="message@example.com")], thread_id="thread-1"
        ),
        [0.0] * EMBEDDING_DIM,
    )
    attachment = _attachment()
    db.store_attachment_extraction(
        attachment_id=attachment.content_hash,
        extraction_status=STATUS_SUCCESS,
        extractor="text",
        extracted_text="cached text",
        extraction_error=None,
    )
    extractor = MagicMock()
    monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)

    embedder = MagicMock()
    embedder.embed.return_value = [0.1] * EMBEDDING_DIM

    summary = process_attachment(
        attachment=attachment,
        message_id="message@example.com",
        thread_id="thread-1",
        db=db,
        embedder=embedder,
        chunk_target_tokens=350,
        chunk_max_tokens=500,
        chunk_overlap_tokens=60,
        ocr_enabled=True,
        max_bytes=10_000_000,
        max_ocr_pages=20,
    )

    assert summary["extractions_reused"] == 1
    assert summary["extractions_run"] == 0
    extractor.assert_not_called()
    assert db.get_chunk_ids_for_message(
        "message@example.com", attachment_id=attachment.content_hash
    )


def test_non_success_cached_extraction_is_retried(tmp_path, monkeypatch):
    db = Database(tmp_path / "mail.db")
    db.upsert_thread(
        make_thread(
            messages=[make_message(message_id="message@example.com")], thread_id="thread-1"
        ),
        [0.0] * EMBEDDING_DIM,
    )
    attachment = _attachment()
    db.store_attachment_extraction(
        attachment_id=attachment.content_hash,
        extraction_status=STATUS_FAILED,
        extractor="text",
        extracted_text=None,
        extraction_error="old failure",
    )
    extractor = MagicMock(
        return_value=ExtractionResult(
            status=STATUS_SUCCESS,
            extractor="text",
            text="fresh extracted text",
            error=None,
        )
    )
    monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)

    embedder = MagicMock()
    embedder.embed.return_value = [0.2] * EMBEDDING_DIM

    summary = process_attachment(
        attachment=attachment,
        message_id="message@example.com",
        thread_id="thread-1",
        db=db,
        embedder=embedder,
        chunk_target_tokens=350,
        chunk_max_tokens=500,
        chunk_overlap_tokens=60,
        ocr_enabled=True,
        max_bytes=10_000_000,
        max_ocr_pages=20,
    )

    assert summary["extractions_reused"] == 0
    assert summary["extractions_run"] == 1
    extractor.assert_called_once()
    cached = db.get_attachment_extraction(attachment.content_hash)
    assert cached is not None
    assert cached["extraction_status"] == STATUS_SUCCESS
    assert cached["extracted_text"] == "fresh extracted text"
