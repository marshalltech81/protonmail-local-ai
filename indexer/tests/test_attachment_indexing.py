"""Tests for the per-attachment indexing pipeline."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from src import attachment_indexing
from src.attachment_indexing import (
    apply_attachment_writes,
    prepare_attachment_writes,
    process_attachment,
)
from src.database import EMBEDDING_DIM, Database
from src.extractors import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_TOO_LARGE,
    STATUS_UNSUPPORTED,
    ExtractionResult,
)
from src.parser import Attachment

from tests.conftest import make_message, make_thread


def _attachment(
    payload: bytes = b"hello from an attachment",
    *,
    filename: str = "note.txt",
    content_type: str = "text/plain",
) -> Attachment:
    return Attachment(
        filename=filename,
        content_type=content_type,
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


def _seed_thread_for_cache_test(tmp_path):
    db = Database(tmp_path / "mail.db")
    db.upsert_thread(
        make_thread(
            messages=[make_message(message_id="message@example.com")], thread_id="thread-1"
        ),
        [0.0] * EMBEDDING_DIM,
    )
    return db


def _run_process_with_cached_status(
    db, attachment, status, monkeypatch, *, error=None, ocr_enabled=True
):
    db.store_attachment_extraction(
        attachment_id=attachment.content_hash,
        extraction_status=status,
        extractor="text",
        extracted_text=None,
        extraction_error=error,
    )
    extractor = MagicMock()
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
        ocr_enabled=ocr_enabled,
        max_bytes=10_000_000,
        max_ocr_pages=20,
    )
    return summary, extractor


def test_cached_empty_extraction_is_honored(tmp_path, monkeypatch):
    """An ``empty`` cache row means the payload genuinely had no text;
    re-running the extractor would produce the same result.
    """
    db = _seed_thread_for_cache_test(tmp_path)
    summary, extractor = _run_process_with_cached_status(
        db, _attachment(), STATUS_EMPTY, monkeypatch
    )
    assert summary["extractions_reused"] == 1
    assert summary["extractions_run"] == 0
    extractor.assert_not_called()


def test_cached_too_large_extraction_is_honored(tmp_path, monkeypatch):
    """A ``too_large`` cache row implies the payload exceeds the
    operator's size cap. Re-running on every reappearance is wasted
    cycles unless the cap is widened (which requires a redeploy and
    can be paired with a cache clear).
    """
    db = _seed_thread_for_cache_test(tmp_path)
    summary, extractor = _run_process_with_cached_status(
        db, _attachment(), STATUS_TOO_LARGE, monkeypatch
    )
    assert summary["extractions_reused"] == 1
    extractor.assert_not_called()


def test_cached_unsupported_for_unknown_mime_is_honored(tmp_path, monkeypatch):
    """``unsupported`` for a no-extractor reason stays cached. The
    OCR-disabled subcase is covered separately by
    ``test_ocr_disabled_unsupported_is_re_run_when_ocr_re_enabled``.
    """
    db = _seed_thread_for_cache_test(tmp_path)
    summary, extractor = _run_process_with_cached_status(
        db,
        _attachment(),
        STATUS_UNSUPPORTED,
        monkeypatch,
        error="no extractor for content_type='application/x-foo'",
    )
    assert summary["extractions_reused"] == 1
    extractor.assert_not_called()


def test_recent_failed_cached_extraction_is_honored(tmp_path, monkeypatch):
    """A STATUS_FAILED row cached within the retry window short-circuits.

    Re-running the extractor on every reappearance of the same payload
    would burn OCR / parse cycles on a chronic failure. The retry
    window (``_FAILED_CACHE_MAX_AGE``) lets a real fix land later
    without permanently caching broken extractions.
    """
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
        extraction_error="recent failure",
    )
    extractor = MagicMock()
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

    assert summary["extractions_reused"] == 1
    assert summary["extractions_run"] == 0
    extractor.assert_not_called()


def test_stale_failed_cached_extraction_is_retried(tmp_path, monkeypatch):
    """A STATUS_FAILED row beyond the retry window is re-extracted.

    The window exists so a chronic failure stops burning OCR cycles,
    but a real library / dep upgrade should eventually pick the
    payload up again rather than caching the failure forever.
    """
    db = Database(tmp_path / "mail.db")
    db.upsert_thread(
        make_thread(
            messages=[make_message(message_id="message@example.com")], thread_id="thread-1"
        ),
        [0.0] * EMBEDDING_DIM,
    )
    attachment = _attachment()
    # Stamp the cache row 30 days in the past — well beyond the 7-day
    # retry window — so the resolver classifies it as stale.
    stale_iso = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    db._conn.execute(
        "INSERT INTO attachment_extractions "
        "(attachment_id, extraction_status, extractor, extracted_text, "
        "extraction_error, extracted_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            attachment.content_hash,
            STATUS_FAILED,
            "text",
            None,
            "old failure",
            stale_iso,
        ),
    )
    db._conn.commit()
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


def test_ocr_disabled_unsupported_is_re_run_when_ocr_re_enabled(tmp_path, monkeypatch):
    """An image cached as ``unsupported`` because OCR was off should re-run
    when the operator re-enables OCR. Other ``unsupported`` reasons (no
    extractor for this MIME type) stay cached.
    """
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
        extraction_status="unsupported",
        extractor=None,
        extracted_text=None,
        extraction_error="OCR disabled (INDEXER_OCR_ENABLED=false)",
    )
    extractor = MagicMock(
        return_value=ExtractionResult(
            status=STATUS_SUCCESS,
            extractor="image-ocr",
            text="now extracted",
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
        ocr_enabled=True,  # operator turned it on after the cached row was written
        max_bytes=10_000_000,
        max_ocr_pages=20,
    )

    assert summary["extractions_reused"] == 0
    assert summary["extractions_run"] == 1
    extractor.assert_called_once()


def _setup_db_for_attachment(tmp_path, message_id="msg@x", thread_id="thread-x"):
    """Create a DB with one thread + message ready to receive attachments."""
    db = Database(tmp_path / "mail.db")
    db.upsert_thread(
        make_thread(messages=[make_message(message_id=message_id)], thread_id=thread_id),
        [0.0] * EMBEDDING_DIM,
    )
    return db


def _kwargs(attachment, **overrides):
    base = dict(
        attachment=attachment,
        message_id="msg@x",
        chunk_target_tokens=350,
        chunk_max_tokens=500,
        chunk_overlap_tokens=60,
        ocr_enabled=True,
        max_bytes=10_000_000,
        max_ocr_pages=20,
    )
    base.update(overrides)
    return base


class TestPrepareApplyBoundary:
    """``prepare_attachment_writes`` must do all extraction + embedding
    before any DB write happens, and ``apply_attachment_writes`` must
    do only DB writes — no extractor, no Ollama. This boundary is what
    keeps the SQLite write transaction off the critical path of slow
    Ollama HTTP roundtrips."""

    def test_prepare_does_not_call_extract_or_embed_when_cache_hits(self, tmp_path, monkeypatch):
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment()
        db.store_attachment_extraction(
            attachment_id=attachment.content_hash,
            extraction_status=STATUS_SUCCESS,
            extractor="text",
            extracted_text="cached body",
            extraction_error=None,
        )

        extractor = MagicMock()
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        embedder = MagicMock()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM

        plan = prepare_attachment_writes(db=db, embedder=embedder, **_kwargs(attachment))

        extractor.assert_not_called()
        # Embed still runs for new chunks even on a cache hit (the chunks
        # are derived from the cached text and may be new).
        assert embedder.embed.called
        assert plan.extraction_reused is True
        assert plan.extraction_to_persist is None

    def test_apply_does_no_extraction_or_embedding(self, tmp_path, monkeypatch):
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment()
        embedder = MagicMock()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM
        plan = prepare_attachment_writes(db=db, embedder=embedder, **_kwargs(attachment))

        # Ensure the apply phase does not touch the extractor or embedder.
        extractor = MagicMock()
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        embedder.reset_mock()

        apply_attachment_writes(plan=plan, message_id="msg@x", thread_id="thread-x", db=db)

        extractor.assert_not_called()
        embedder.embed.assert_not_called()


class TestMultiOccurrenceDeterminism:
    def test_distinct_occurrence_indices_yield_distinct_occurrence_ids(self, tmp_path):
        """Same payload, same filename, two occurrence indices → two
        distinct occurrence IDs. The ID is the diff key for
        ``attachments`` rows so every forwarded copy can coexist."""
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment()
        embedder = MagicMock()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM

        plan_a = prepare_attachment_writes(
            db=db, embedder=embedder, **_kwargs(attachment, occurrence_index=0)
        )
        plan_b = prepare_attachment_writes(
            db=db, embedder=embedder, **_kwargs(attachment, occurrence_index=1)
        )
        assert plan_a.occurrence_id != plan_b.occurrence_id

    def test_same_inputs_yield_same_occurrence_id(self, tmp_path):
        """Re-running prepare with identical inputs must yield the same
        occurrence ID so the apply phase's upsert is idempotent."""
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment()
        embedder = MagicMock()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM

        plan_a = prepare_attachment_writes(
            db=db, embedder=embedder, **_kwargs(attachment, occurrence_index=0)
        )
        plan_b = prepare_attachment_writes(
            db=db, embedder=embedder, **_kwargs(attachment, occurrence_index=0)
        )
        assert plan_a.occurrence_id == plan_b.occurrence_id

    def test_replay_skips_re_embedding_existing_chunks(self, tmp_path):
        """Running ``process_attachment`` twice on the same input should
        embed each chunk exactly once. Deterministic chunk IDs +
        diff-write let the second run skip every existing chunk."""
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment(payload=b"first paragraph.\n\nsecond paragraph.")
        embedder = MagicMock()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM

        process_attachment(
            db=db,
            embedder=embedder,
            thread_id="thread-x",
            **_kwargs(attachment),
        )
        embed_calls_first_run = embedder.embed.call_count

        process_attachment(
            db=db,
            embedder=embedder,
            thread_id="thread-x",
            **_kwargs(attachment),
        )
        # Second run must not embed anything because the chunk IDs are
        # deterministic and already-stored chunks short-circuit.
        assert embedder.embed.call_count == embed_calls_first_run


class TestNonSuccessPlanPaths:
    def test_unsupported_status_persists_status_only_no_chunks(self, tmp_path, monkeypatch):
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment(
            payload=b"\x00\x01\x02", filename="x.bin", content_type="application/x-foo"
        )
        # Real dispatcher returns ``unsupported`` for unknown MIME +
        # extension, so we don't need to mock — but mocking makes the
        # contract explicit and decouples this test from the dispatcher.
        monkeypatch.setattr(
            attachment_indexing,
            "extract_attachment",
            MagicMock(
                return_value=ExtractionResult(
                    status=STATUS_UNSUPPORTED,
                    extractor=None,
                    text=None,
                    error="no extractor",
                )
            ),
        )
        embedder = MagicMock()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM

        summary = process_attachment(
            db=db, embedder=embedder, thread_id="thread-x", **_kwargs(attachment)
        )

        assert summary["chunks_inserted"] == 0
        assert summary["occurrences_inserted"] == 1
        # No embedding work should happen for an unsupported attachment.
        embedder.embed.assert_not_called()
        cached = db.get_attachment_extraction(attachment.content_hash)
        assert cached is not None
        assert cached["extraction_status"] == STATUS_UNSUPPORTED

    def test_too_large_status_persists_status_only_no_chunks(self, tmp_path, monkeypatch):
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment(payload=b"x" * 200)
        monkeypatch.setattr(
            attachment_indexing,
            "extract_attachment",
            MagicMock(
                return_value=ExtractionResult(
                    status=STATUS_TOO_LARGE,
                    extractor=None,
                    text=None,
                    error="payload exceeds cap",
                )
            ),
        )
        embedder = MagicMock()

        summary = process_attachment(
            db=db,
            embedder=embedder,
            thread_id="thread-x",
            **_kwargs(attachment, max_bytes=100),
        )

        assert summary["chunks_inserted"] == 0
        assert summary["occurrences_inserted"] == 1
        embedder.embed.assert_not_called()
        cached = db.get_attachment_extraction(attachment.content_hash)
        assert cached is not None
        assert cached["extraction_status"] == STATUS_TOO_LARGE


class TestExtractedTextCap:
    def test_cap_truncates_text_in_persisted_cache_row(self, tmp_path):
        """End-to-end through the real dispatcher: a 5,000-char payload
        with a 128-char cap must result in 128 chars of cached text and
        only the chunks derivable from that prefix."""
        db = _setup_db_for_attachment(tmp_path)
        long_payload = ("paragraph one. " * 1000).encode()
        attachment = _attachment(payload=long_payload)
        embedder = MagicMock()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM

        process_attachment(
            db=db,
            embedder=embedder,
            thread_id="thread-x",
            **_kwargs(attachment, max_extracted_chars=128),
        )

        cached = db.get_attachment_extraction(attachment.content_hash)
        assert cached is not None
        assert cached["extraction_status"] == STATUS_SUCCESS
        assert len(cached["extracted_text"]) <= 128

    def test_cap_disabled_when_none(self, tmp_path):
        """Passing ``max_extracted_chars=None`` must not truncate."""
        db = _setup_db_for_attachment(tmp_path)
        long_payload = ("paragraph one. " * 200).encode()
        attachment = _attachment(payload=long_payload)
        embedder = MagicMock()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM

        process_attachment(
            db=db,
            embedder=embedder,
            thread_id="thread-x",
            **_kwargs(attachment, max_extracted_chars=None),
        )

        cached = db.get_attachment_extraction(attachment.content_hash)
        assert cached is not None
        # Stored text is the stripped extraction; allow for trailing
        # whitespace stripped by the dispatcher but assert it covers the
        # full payload size (within stripping tolerance).
        assert len(cached["extracted_text"]) >= len(long_payload) - 5
