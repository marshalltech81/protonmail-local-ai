"""Tests for the per-attachment indexing pipeline."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from src import attachment_indexing
from src.attachment_indexing import (
    apply_attachment_writes,
    prepare_attachment_writes,
)
from src.database import EMBEDDING_DIM, Database
from src.extractors import (
    NO_EXTRACTOR_ERROR,
    SCANNED_PDF_OCR_DISABLED_ERROR,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_TOO_LARGE,
    STATUS_UNSUPPORTED,
    ExtractionResult,
)
from src.parser import Attachment

from tests.conftest import make_message, make_mock_embedder, make_thread


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


def _embed_new_chunks(plan: Any, *, db: Database, claimant_id: str, embedder: Any) -> None:
    """Fill ``plan.embeddings_by_chunk_id`` the way ``main.py``'s batched
    pipeline does: diff the plan's chunks against the stored chunk IDs
    and embed only the new ones, in one ``embed_batch`` call."""
    stored = db.get_chunk_ids_for_message(claimant_id, attachment_id=plan.attachment.content_hash)
    new_chunks = [c for c in plan.chunks if c.chunk_id not in stored]
    if new_chunks:
        vectors = embedder.embed_batch([c.text for c in new_chunks])
        plan.embeddings_by_chunk_id = {c.chunk_id: v for c, v in zip(new_chunks, vectors)}


def _prepare_and_apply(
    *, db: Database, thread_id: str, embedder: Any = None, **prepare_kwargs: Any
) -> None:
    """Run one attachment through the indexer's two phases the way
    ``main.py`` does: ``prepare_attachment_writes`` and the embed step
    outside the write transaction, then ``apply_attachment_writes``
    inside ``db.transaction()``."""
    plan = prepare_attachment_writes(db=db, **prepare_kwargs)
    if embedder is not None:
        _embed_new_chunks(plan, db=db, claimant_id=prepare_kwargs["claimant_id"], embedder=embedder)
    with db.transaction():
        apply_attachment_writes(
            plan=plan,
            claimant_id=prepare_kwargs["claimant_id"],
            thread_id=thread_id,
            db=db,
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
        extractor="text@2",
        extracted_text="cached text",
        extraction_error=None,
    )
    extractor = MagicMock()
    monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)

    embedder = make_mock_embedder()
    embedder.embed.return_value = [0.1] * EMBEDDING_DIM

    _prepare_and_apply(
        attachment=attachment,
        claimant_id="message@example.com",
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

    extractor.assert_not_called()
    # The chunks come from the cached text, and the row is left as it was.
    chunk_texts = [
        row["text"]
        for row in db._conn.execute(
            "SELECT text FROM message_chunks WHERE attachment_id = ?", (attachment.content_hash,)
        )
    ]
    assert chunk_texts == ["cached text"]
    row = db.get_attachment_extraction(attachment.content_hash)
    assert (row["extractor"], row["extracted_text"]) == ("text@2", "cached text")
    assert db.get_chunk_ids_for_message(
        "message@example.com", attachment_id=attachment.content_hash
    )


def _process_with_cached_extractor(
    db,
    extractor_name,
    status,
    text,
    monkeypatch,
    *,
    filename="c.docx",
    content_type="application/msword",
):
    attachment = _attachment(b"docx bytes", filename=filename, content_type=content_type)
    db.store_attachment_extraction(
        attachment_id=attachment.content_hash,
        extraction_status=status,
        extractor=extractor_name,
        extracted_text=text,
        extraction_error=None,
    )
    extractor = MagicMock(
        return_value=ExtractionResult(
            status=STATUS_SUCCESS, extractor="docx@3", text="fresh text", error=None
        )
    )
    monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
    embedder = make_mock_embedder()
    embedder.embed.return_value = [0.1] * EMBEDDING_DIM
    _prepare_and_apply(
        attachment=attachment,
        claimant_id="message@example.com",
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
    return extractor, db.get_attachment_extraction(attachment.content_hash)


def test_cache_row_from_an_older_extractor_version_is_re_extracted(tmp_path, monkeypatch):
    """Rows the old DOCX walker wrote (unversioned ``docx``) missed nested
    and header tables and could be ``empty`` (#226). They must not be
    served forever: an older version is a cache miss."""
    for status, text in ((STATUS_SUCCESS, "old text"), (STATUS_EMPTY, None)):
        db = _seed_thread_for_cache_test(tmp_path / status)
        extractor, row = _process_with_cached_extractor(db, "docx", status, text, monkeypatch)
        extractor.assert_called_once()
        assert row["extractor"] == "docx@3"
        assert row["extracted_text"] == "fresh text"


def test_cache_row_from_docx_version_2_is_re_extracted(tmp_path, monkeypatch):
    """Rows the docx@2 walker wrote miss first-page and even-page headers
    and footers (#299), so they are stale once docx is at version 3."""
    for status, text in ((STATUS_SUCCESS, "old text"), (STATUS_EMPTY, None)):
        db = _seed_thread_for_cache_test(tmp_path / status)
        extractor, row = _process_with_cached_extractor(db, "docx@2", status, text, monkeypatch)
        extractor.assert_called_once()
        assert row["extractor"] == "docx@3"
        assert row["extracted_text"] == "fresh text"


def test_cache_row_from_the_unversioned_xlsx_extractor_is_re_extracted(tmp_path, monkeypatch):
    """Rows written before ``xlsx`` was versioned hold the unbounded
    shared-string expansion (#294); the bump makes them a cache miss
    that re-runs the XLSX extractor."""
    db = _seed_thread_for_cache_test(tmp_path)
    extractor, row = _process_with_cached_extractor(
        db,
        "xlsx",
        STATUS_SUCCESS,
        "old text",
        monkeypatch,
        filename="book.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    extractor.assert_called_once()
    assert extractor.call_args.kwargs["module_override"] == "xlsx"


def test_pre_bump_pdf_row_is_re_extracted_by_the_pdf_extractor(tmp_path, monkeypatch):
    """#292: rows the PDF extractor wrote before page-level OCR selection
    (unversioned ``pdf-digital``) skipped a mixed PDF's scanned pages, so
    they are a cache miss that re-runs the PDF extractor."""
    db = _seed_thread_for_cache_test(tmp_path)
    extractor, row = _process_with_cached_extractor(
        db,
        "pdf-digital",
        STATUS_SUCCESS,
        "digital page only",
        monkeypatch,
        filename="statement.pdf",
        content_type="application/pdf",
    )
    extractor.assert_called_once()
    assert extractor.call_args.kwargs["module_override"] == "pdf"
    assert row["extracted_text"] == "fresh text"


def test_stale_ocr_row_is_served_while_ocr_is_off(tmp_path, monkeypatch):
    """Review round 1 on #262: a refresh with OCR off would replace the
    row's OCR text with "OCR disabled" and clear the indexed chunks."""
    db = _seed_thread_for_cache_test(tmp_path)
    attachment = _attachment(b"png bytes", filename="scan.png", content_type="image/png")
    db.store_attachment_extraction(
        attachment_id=attachment.content_hash,
        extraction_status=STATUS_SUCCESS,
        extractor="image-ocr",
        extracted_text="old ocr text",
        extraction_error=None,
    )
    extractor = MagicMock()
    monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
    plan = prepare_attachment_writes(
        db=db,
        **_kwargs(attachment, claimant_id="message@example.com", ocr_enabled=False),
    )
    extractor.assert_not_called()
    assert plan.status == STATUS_SUCCESS and plan.chunks
    assert db.get_attachment_extraction(attachment.content_hash)["extractor"] == "image-ocr"


def test_cache_row_from_the_current_extractor_version_is_reused(tmp_path, monkeypatch):
    db = _seed_thread_for_cache_test(tmp_path)
    extractor, _ = _process_with_cached_extractor(
        db, "docx@3", STATUS_SUCCESS, "cached text", monkeypatch
    )
    extractor.assert_not_called()


def test_cache_row_from_a_newer_extractor_version_is_reused(tmp_path, monkeypatch):
    # After a rollback, rows the newer release wrote must not be
    # downgraded by the older walker.
    db = _seed_thread_for_cache_test(tmp_path)
    extractor, row = _process_with_cached_extractor(
        db, "docx@4", STATUS_SUCCESS, "newer text", monkeypatch
    )
    extractor.assert_not_called()
    assert row["extractor"] == "docx@4"


def test_stale_row_is_refreshed_by_an_occurrence_of_another_type(tmp_path, monkeypatch):
    """The same bytes attached as ``.bin`` resolve to no extractor. They
    share the DOCX cache row, so they re-run the extractor that produced
    it: re-running by their own metadata would overwrite the row with
    ``unsupported``, and skipping would leave their chunks stale."""
    db = _seed_thread_for_cache_test(tmp_path)
    extractor, row = _process_with_cached_extractor(
        db,
        "docx",
        STATUS_SUCCESS,
        "old text",
        monkeypatch,
        filename="blob.bin",
        content_type="application/octet-stream",
    )
    extractor.assert_called_once()
    assert extractor.call_args.kwargs["module_override"] == "docx"
    assert row["extractor"] == "docx@3"
    assert row["extracted_text"] == "fresh text"
    assert db.get_chunk_ids_for_message(
        "message@example.com", attachment_id=hashlib.sha256(b"docx bytes").hexdigest()
    )


def test_reused_terminal_row_clears_the_stale_chunks(tmp_path, monkeypatch):
    """Another message's re-extraction of the same bytes ended ``empty`` and
    stamped the row current. This message then gets a plain cache hit on
    it, so it must still drop the chunks its own stale extraction left."""
    db = _seed_thread_for_cache_test(tmp_path)
    attachment_id = hashlib.sha256(b"docx bytes").hexdigest()
    _process_with_cached_extractor(db, "docx@3", STATUS_SUCCESS, "old text", monkeypatch)
    assert db.get_chunk_ids_for_message("message@example.com", attachment_id=attachment_id)

    db.store_attachment_extraction(
        attachment_id=attachment_id,
        extraction_status=STATUS_EMPTY,
        extractor="docx@3",
        extracted_text=None,
        extraction_error=None,
    )
    extractor = MagicMock()
    monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
    _prepare_and_apply(
        attachment=_attachment(b"docx bytes", filename="c.docx", content_type="application/msword"),
        claimant_id="message@example.com",
        thread_id="thread-1",
        db=db,
        embedder=make_mock_embedder([0.1] * EMBEDDING_DIM),
        chunk_target_tokens=350,
        chunk_max_tokens=500,
        chunk_overlap_tokens=60,
        ocr_enabled=True,
        max_bytes=10_000_000,
        max_ocr_pages=20,
    )
    extractor.assert_not_called()
    assert not db.get_chunk_ids_for_message("message@example.com", attachment_id=attachment_id)


def test_re_extraction_without_text_clears_the_stale_chunks(tmp_path, monkeypatch):
    """A stale row re-extracted to ``empty`` (or ``failed`` / ``too_large``)
    must not leave the old text searchable: the new row is current, so no
    later sweep would repair it."""
    db = _seed_thread_for_cache_test(tmp_path)
    attachment_id = hashlib.sha256(b"docx bytes").hexdigest()
    _process_with_cached_extractor(db, "docx@3", STATUS_SUCCESS, "old text", monkeypatch)
    assert db.get_chunk_ids_for_message("message@example.com", attachment_id=attachment_id)

    with db.transaction():
        db._conn.execute("UPDATE attachment_extractions SET extractor = 'docx'")
    extractor = MagicMock(
        return_value=ExtractionResult(
            status=STATUS_EMPTY, extractor="docx@3", text=None, error=None
        )
    )
    monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
    _prepare_and_apply(
        attachment=_attachment(b"docx bytes", filename="c.docx", content_type="application/msword"),
        claimant_id="message@example.com",
        thread_id="thread-1",
        db=db,
        embedder=make_mock_embedder([0.1] * EMBEDDING_DIM),
        chunk_target_tokens=350,
        chunk_max_tokens=500,
        chunk_overlap_tokens=60,
        ocr_enabled=True,
        max_bytes=10_000_000,
        max_ocr_pages=20,
    )
    extractor.assert_called_once()
    assert not db.get_chunk_ids_for_message("message@example.com", attachment_id=attachment_id)


def _seed_thread_for_cache_test(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = Database(tmp_path / "mail.db")
    db.upsert_thread(
        make_thread(
            messages=[make_message(message_id="message@example.com")], thread_id="thread-1"
        ),
        [0.0] * EMBEDDING_DIM,
    )
    return db


def _run_process_with_cached_status(
    db, attachment, status, monkeypatch, *, error=None, ocr_enabled=True, max_bytes=10_000_000
):
    db.store_attachment_extraction(
        attachment_id=attachment.content_hash,
        extraction_status=status,
        extractor="text@2",
        extracted_text=None,
        extraction_error=error,
    )
    extractor = MagicMock()
    monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
    embedder = make_mock_embedder()
    embedder.embed.return_value = [0.2] * EMBEDDING_DIM
    _prepare_and_apply(
        attachment=attachment,
        claimant_id="message@example.com",
        thread_id="thread-1",
        db=db,
        embedder=embedder,
        chunk_target_tokens=350,
        chunk_max_tokens=500,
        chunk_overlap_tokens=60,
        ocr_enabled=ocr_enabled,
        max_bytes=max_bytes,
        max_ocr_pages=20,
    )
    # A cache hit leaves the cached row as it was and writes no chunks.
    row = db.get_attachment_extraction(attachment.content_hash)
    assert (row["extraction_status"], row["extraction_error"]) == (status, error)
    assert not db.get_chunk_ids_for_message(
        "message@example.com", attachment_id=attachment.content_hash
    )
    return extractor


def test_cached_empty_extraction_is_honored(tmp_path, monkeypatch):
    """An ``empty`` cache row means the payload genuinely had no text;
    re-running the extractor would produce the same result.
    """
    db = _seed_thread_for_cache_test(tmp_path)
    extractor = _run_process_with_cached_status(db, _attachment(), STATUS_EMPTY, monkeypatch)
    extractor.assert_not_called()


def test_cached_too_large_extraction_is_honored_while_still_over_the_cap(tmp_path, monkeypatch):
    """A ``too_large`` cache row whose payload still exceeds the current
    cap is honored: re-running would only record ``too_large`` again."""
    db = _seed_thread_for_cache_test(tmp_path)
    attachment = _attachment()
    extractor = _run_process_with_cached_status(
        db, attachment, STATUS_TOO_LARGE, monkeypatch, max_bytes=attachment.size - 1
    )
    extractor.assert_not_called()


def test_cached_too_large_extraction_is_re_run_once_the_payload_fits(tmp_path, monkeypatch):
    """#693: after the operator raises ``INDEXER_ATTACHMENT_MAX_BYTES``, a
    ``too_large`` row for a payload at or below the new cap is stale. The
    attachment is extracted and the row rewritten, so the next occurrence
    is served from the cache."""
    for label, slack in (("at the cap", 0), ("under the cap", 1)):
        db = _seed_thread_for_cache_test(tmp_path / label)
        attachment = _attachment()
        db.store_attachment_extraction(
            attachment_id=attachment.content_hash,
            extraction_status=STATUS_TOO_LARGE,
            extractor=None,
            extracted_text=None,
            extraction_error=f"payload {attachment.size} bytes exceeds cap 10",
        )
        extractor = MagicMock(
            return_value=ExtractionResult(
                status=STATUS_SUCCESS, extractor="text@2", text="now extracted", error=None
            )
        )
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        kwargs: dict[str, Any] = {
            "attachment": attachment,
            "claimant_id": "message@example.com",
            "thread_id": "thread-1",
            "db": db,
            "embedder": make_mock_embedder([0.1] * EMBEDDING_DIM),
            "chunk_target_tokens": 350,
            "chunk_max_tokens": 500,
            "chunk_overlap_tokens": 60,
            "ocr_enabled": True,
            "max_bytes": attachment.size + slack,
            "max_ocr_pages": 20,
        }
        _prepare_and_apply(**kwargs)

        assert extractor.call_count == 1, label
        assert extractor.call_args.kwargs["max_bytes"] == attachment.size + slack
        row = db.get_attachment_extraction(attachment.content_hash)
        assert row["extraction_status"] == STATUS_SUCCESS, label
        assert db.get_chunk_ids_for_message(
            "message@example.com", attachment_id=attachment.content_hash
        ), label

        # The rewritten row is a plain cache hit from now on.
        _prepare_and_apply(**kwargs)
        assert extractor.call_count == 1, label


def test_cached_unsupported_for_unknown_mime_is_honored(tmp_path, monkeypatch):
    """``unsupported`` for a no-extractor reason stays cached. The
    OCR-disabled subcase is covered separately by
    ``test_ocr_disabled_unsupported_is_re_run_when_ocr_re_enabled``.
    """
    db = _seed_thread_for_cache_test(tmp_path)
    extractor = _run_process_with_cached_status(
        db,
        _attachment(filename="data.foo", content_type="application/x-foo"),
        STATUS_UNSUPPORTED,
        monkeypatch,
        error=NO_EXTRACTOR_ERROR,
    )
    extractor.assert_not_called()


def test_cached_unsupported_is_re_run_for_an_occurrence_with_an_extractor(tmp_path, monkeypatch):
    """#210: the cache is keyed by bytes, but dispatch is by MIME type and
    filename. Bytes first seen as ``.bin`` cached ``unsupported``; the
    same bytes later attached as ``text/plain`` must be extracted."""
    db = _seed_thread_for_cache_test(tmp_path)
    attachment = _attachment(filename="document.txt", content_type="text/plain")
    db.store_attachment_extraction(
        attachment_id=attachment.content_hash,
        extraction_status=STATUS_UNSUPPORTED,
        extractor=None,
        extracted_text=None,
        extraction_error=NO_EXTRACTOR_ERROR,
    )
    extractor = MagicMock(
        return_value=ExtractionResult(
            status=STATUS_SUCCESS, extractor="text", text="now extracted", error=None
        )
    )
    monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
    _prepare_and_apply(
        attachment=attachment,
        claimant_id="message@example.com",
        thread_id="thread-1",
        db=db,
        embedder=make_mock_embedder([0.2] * EMBEDDING_DIM),
        chunk_target_tokens=350,
        chunk_max_tokens=500,
        chunk_overlap_tokens=60,
        ocr_enabled=True,
        max_bytes=10_000_000,
        max_ocr_pages=20,
    )
    extractor.assert_called_once()
    assert db.get_attachment_extraction(attachment.content_hash)["extraction_status"] == (
        STATUS_SUCCESS
    )


def test_ocr_disabled_row_stays_cached_while_ocr_is_off(tmp_path, monkeypatch):
    """A scanned PDF resolves to the PDF extractor either way; the row the
    PDF extractor wrote is re-run only once OCR is on."""
    from src.extractors import SCANNED_PDF_OCR_DISABLED_ERROR

    db = _seed_thread_for_cache_test(tmp_path)
    extractor = _run_process_with_cached_status(
        db,
        _attachment(filename="scan.pdf", content_type="application/pdf"),
        STATUS_UNSUPPORTED,
        monkeypatch,
        error=SCANNED_PDF_OCR_DISABLED_ERROR,
        ocr_enabled=False,
    )
    extractor.assert_not_called()


def test_image_ocr_disabled_row_does_not_block_a_pdf_occurrence(tmp_path, monkeypatch):
    """Review round 2: the PDF extractor reads a digital text layer without
    OCR, so an "OCR disabled" row written for the bytes as an image must
    not keep a PDF occurrence of them unextracted while OCR is off."""
    from src.extractors import OCR_DISABLED_ERROR

    db = _seed_thread_for_cache_test(tmp_path)
    attachment = _attachment(filename="report.pdf", content_type="application/pdf")
    db.store_attachment_extraction(
        attachment_id=attachment.content_hash,
        extraction_status=STATUS_UNSUPPORTED,
        extractor=None,
        extracted_text=None,
        extraction_error=OCR_DISABLED_ERROR,
    )
    extractor = MagicMock(
        return_value=ExtractionResult(
            status=STATUS_SUCCESS, extractor="pdf-digital", text="digital text", error=None
        )
    )
    monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
    _prepare_and_apply(
        attachment=attachment,
        claimant_id="message@example.com",
        thread_id="thread-1",
        db=db,
        embedder=make_mock_embedder([0.2] * EMBEDDING_DIM),
        chunk_target_tokens=350,
        chunk_max_tokens=500,
        chunk_overlap_tokens=60,
        ocr_enabled=False,
        max_bytes=10_000_000,
        max_ocr_pages=20,
    )
    extractor.assert_called_once()


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
        extractor="text@2",
        extracted_text=None,
        extraction_error="recent failure",
    )
    extractor = MagicMock()
    monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)

    embedder = make_mock_embedder()
    embedder.embed.return_value = [0.2] * EMBEDDING_DIM

    _prepare_and_apply(
        attachment=attachment,
        claimant_id="message@example.com",
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

    embedder = make_mock_embedder()
    embedder.embed.return_value = [0.2] * EMBEDDING_DIM

    _prepare_and_apply(
        attachment=attachment,
        claimant_id="message@example.com",
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

    embedder = make_mock_embedder()
    embedder.embed.return_value = [0.2] * EMBEDDING_DIM

    _prepare_and_apply(
        attachment=attachment,
        claimant_id="message@example.com",
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

    extractor.assert_called_once()


def test_ocr_disabled_pdf_cache_is_re_run_when_ocr_re_enabled(tmp_path, monkeypatch):
    """Scanned PDFs seen while OCR is disabled cache the same marker as
    images, so enabling OCR later refreshes them instead of preserving an
    empty/tiny digital-text result forever.
    """
    db = Database(tmp_path / "mail.db")
    db.upsert_thread(
        make_thread(
            messages=[make_message(message_id="message@example.com")], thread_id="thread-1"
        ),
        [0.0] * EMBEDDING_DIM,
    )
    attachment = _attachment(filename="scan.pdf", content_type="application/pdf")
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
            extractor="pdf-ocr",
            text="ocr text from scanned pdf",
            error=None,
        )
    )
    monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)

    embedder = make_mock_embedder()
    embedder.embed.return_value = [0.2] * EMBEDDING_DIM

    _prepare_and_apply(
        attachment=attachment,
        claimant_id="message@example.com",
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

    extractor.assert_called_once()
    cached = db.get_attachment_extraction(attachment.content_hash)
    assert cached is not None
    assert cached["extraction_status"] == STATUS_SUCCESS
    assert cached["extractor"] == "pdf-ocr"


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
        claimant_id="msg@x",
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
    """``prepare_attachment_writes`` must do all extraction + chunking
    before any DB write happens, leaving embedding to the caller's
    batched step, and ``apply_attachment_writes`` must do only DB
    writes — no extractor, no embedding service. This boundary is what
    keeps the SQLite write transaction off the critical path of slow
    embedding service HTTP roundtrips."""

    def test_prepare_does_not_call_extract_when_cache_hits(self, tmp_path, monkeypatch):
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment()
        db.store_attachment_extraction(
            attachment_id=attachment.content_hash,
            extraction_status=STATUS_SUCCESS,
            extractor="text@2",
            extracted_text="cached body",
            extraction_error=None,
        )

        extractor = MagicMock()
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)

        plan = prepare_attachment_writes(db=db, **_kwargs(attachment))

        extractor.assert_not_called()
        # The chunks come from the cached text; embedding them is left
        # to the caller's batched step (#845).
        assert [c.text for c in plan.chunks] == ["cached body"]
        assert plan.embeddings_by_chunk_id == {}
        assert plan.extraction_to_persist is None

    def test_apply_does_no_extraction_or_embedding(self, tmp_path, monkeypatch):
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment()
        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM
        plan = prepare_attachment_writes(db=db, **_kwargs(attachment))
        _embed_new_chunks(plan, db=db, claimant_id="msg@x", embedder=embedder)
        assert plan.embeddings_by_chunk_id

        # Ensure the apply phase does not touch the extractor or embedder.
        extractor = MagicMock()
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        embedder.reset_mock()

        apply_attachment_writes(
            plan=plan,
            claimant_id="msg@x",
            thread_id="thread-x",
            db=db,
        )

        extractor.assert_not_called()
        embedder.embed.assert_not_called()


class TestMultiOccurrenceDeterminism:
    def test_distinct_occurrence_indices_yield_distinct_occurrence_ids(self, tmp_path):
        """Same payload, same filename, two occurrence indices → two
        distinct occurrence IDs. The ID is the diff key for
        ``attachments`` rows so every forwarded copy can coexist."""
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment()
        plan_a = prepare_attachment_writes(db=db, **_kwargs(attachment, occurrence_index=0))
        plan_b = prepare_attachment_writes(db=db, **_kwargs(attachment, occurrence_index=1))
        assert plan_a.occurrence_id != plan_b.occurrence_id

    def test_same_inputs_yield_same_occurrence_id(self, tmp_path):
        """Re-running prepare with identical inputs must yield the same
        occurrence ID so the apply phase's upsert is idempotent."""
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment()
        plan_a = prepare_attachment_writes(db=db, **_kwargs(attachment, occurrence_index=0))
        plan_b = prepare_attachment_writes(db=db, **_kwargs(attachment, occurrence_index=0))
        assert plan_a.occurrence_id == plan_b.occurrence_id

    def test_replay_skips_re_embedding_existing_chunks(self, tmp_path):
        """Running ``_prepare_and_apply`` twice on the same input should
        embed each chunk exactly once. Deterministic chunk IDs +
        diff-write let the second run skip every existing chunk."""
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment(payload=b"first paragraph.\n\nsecond paragraph.")
        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM

        _prepare_and_apply(
            db=db,
            embedder=embedder,
            thread_id="thread-x",
            **_kwargs(attachment),
        )
        embed_calls_first_run = embedder.embed.call_count

        _prepare_and_apply(
            db=db,
            embedder=embedder,
            thread_id="thread-x",
            **_kwargs(attachment),
        )
        # Second run must not embed anything because the chunk IDs are
        # deterministic and already-stored chunks short-circuit.
        assert embedder.embed.call_count == embed_calls_first_run


def _occurrence_count(db: Database, attachment: Attachment) -> int:
    return db._conn.execute(
        "SELECT COUNT(*) FROM attachments WHERE attachment_id = ?", (attachment.content_hash,)
    ).fetchone()[0]


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
        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM

        _prepare_and_apply(
            db=db,
            embedder=embedder,
            thread_id="thread-x",
            **_kwargs(attachment),
        )

        assert not db.get_chunk_ids_for_message("msg@x", attachment_id=attachment.content_hash)
        assert _occurrence_count(db, attachment) == 1
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
        embedder = make_mock_embedder()

        _prepare_and_apply(
            db=db,
            embedder=embedder,
            thread_id="thread-x",
            **_kwargs(attachment, max_bytes=100),
        )

        assert not db.get_chunk_ids_for_message("msg@x", attachment_id=attachment.content_hash)
        assert _occurrence_count(db, attachment) == 1
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
        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM

        _prepare_and_apply(
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
        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM

        _prepare_and_apply(
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


def test_batch_results_are_shared_but_do_not_block_a_supported_occurrence(tmp_path, monkeypatch):
    """#237 + #210 within one batch: a pending result is reused by later
    occurrences of the same bytes, except an ``unsupported`` one when
    the occurrence's metadata now selects an extractor."""
    from src.extractors import extract

    db = _setup_db_for_attachment(tmp_path)
    calls: list[str] = []

    def counting_extract(**kwargs):
        calls.append(kwargs["filename"])
        return extract(**kwargs)

    monkeypatch.setattr(attachment_indexing, "extract_attachment", counting_extract)
    batch: dict[str, ExtractionResult] = {}
    payload = b"plain words in a file"

    def prepare(filename, content_type):
        return prepare_attachment_writes(
            db=db,
            batch_extractions=batch,
            **_kwargs(_attachment(payload, filename=filename, content_type=content_type)),
        )

    blob = prepare("blob.bin", "application/octet-stream")
    text = prepare("doc.txt", "text/plain")
    again = prepare("copy.bin", "application/octet-stream")

    assert calls == ["blob.bin", "doc.txt"]
    assert blob.status == STATUS_UNSUPPORTED
    assert text.status == STATUS_SUCCESS and text.chunks
    assert again.status == STATUS_SUCCESS
    # Reused but uncommitted: the reusing message still persists the row.
    assert again.extraction_to_persist is text.extraction_to_persist


def test_ocr_disabled_row_does_not_block_a_non_ocr_occurrence(tmp_path, monkeypatch):
    """Review round 1: bytes cached "OCR disabled" from an image must not
    block the same bytes attached as text while OCR is off — the text
    extractor does not need OCR."""
    db = _seed_thread_for_cache_test(tmp_path)
    attachment = _attachment(filename="document.txt", content_type="text/plain")
    db.store_attachment_extraction(
        attachment_id=attachment.content_hash,
        extraction_status=STATUS_UNSUPPORTED,
        extractor=None,
        extracted_text=None,
        extraction_error="OCR disabled (INDEXER_OCR_ENABLED=false)",
    )
    extractor = MagicMock(
        return_value=ExtractionResult(
            status=STATUS_SUCCESS, extractor="text", text="now extracted", error=None
        )
    )
    monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
    _prepare_and_apply(
        attachment=attachment,
        claimant_id="message@example.com",
        thread_id="thread-1",
        db=db,
        embedder=make_mock_embedder([0.2] * EMBEDDING_DIM),
        chunk_target_tokens=350,
        chunk_max_tokens=500,
        chunk_overlap_tokens=60,
        ocr_enabled=False,
        max_bytes=10_000_000,
        max_ocr_pages=20,
    )
    extractor.assert_called_once()


def test_batch_ocr_disabled_result_does_not_block_a_non_ocr_occurrence(tmp_path, monkeypatch):
    from src.extractors import extract

    db = _setup_db_for_attachment(tmp_path)
    calls: list[str] = []

    def counting_extract(**kwargs):
        calls.append(kwargs["filename"])
        return extract(**kwargs)

    monkeypatch.setattr(attachment_indexing, "extract_attachment", counting_extract)
    batch: dict[str, ExtractionResult] = {}
    payload = b"plain words in a file"
    for filename, content_type in (("scan.png", "image/png"), ("doc.txt", "text/plain")):
        plan = prepare_attachment_writes(
            db=db,
            batch_extractions=batch,
            **_kwargs(
                _attachment(payload, filename=filename, content_type=content_type),
                ocr_enabled=False,
            ),
        )
    assert calls == ["scan.png", "doc.txt"]
    assert plan.status == STATUS_SUCCESS


def test_unsupported_attachment_log_omits_filename_and_mime(tmp_path, caplog):
    """#257: the sender-supplied filename and MIME type stay out of the
    logs and the persisted ``extraction_error``."""
    caplog.set_level("DEBUG")
    db = _setup_db_for_attachment(tmp_path)
    attachment = _attachment(
        filename="SYNTHETIC_FILENAME_MARKER.bin",
        content_type="application/x-SYNTHETIC_MIME_MARKER",
    )

    plan = prepare_attachment_writes(db=db, **_kwargs(attachment))
    with db.transaction():
        apply_attachment_writes(
            plan=plan,
            claimant_id="msg@x",
            thread_id="thread-x",
            db=db,
        )

    assert plan.status == STATUS_UNSUPPORTED
    cached = db.get_attachment_extraction(attachment.content_hash)
    assert cached is not None
    for marker in ("SYNTHETIC_FILENAME_MARKER", "SYNTHETIC_MIME_MARKER"):
        assert marker not in caplog.text
        assert marker not in (cached["extraction_error"] or "")


def test_failed_extraction_persists_no_filename_or_parser_text(tmp_path, monkeypatch, caplog):
    """#257: a raising extractor records only the exception type."""
    from src import extractors

    caplog.set_level("DEBUG")

    def boom(payload, **opts):
        raise ValueError("SYNTHETIC_EXC_MARKER")

    monkeypatch.setattr(extractors, "_safe_import", lambda module_name: boom)
    db = _setup_db_for_attachment(tmp_path)
    attachment = _attachment(filename="SYNTHETIC_FILENAME_MARKER.pdf", content_type="")

    plan = prepare_attachment_writes(db=db, **_kwargs(attachment))
    with db.transaction():
        apply_attachment_writes(
            plan=plan,
            claimant_id="msg@x",
            thread_id="thread-x",
            db=db,
        )

    assert plan.status == STATUS_FAILED
    cached = db.get_attachment_extraction(attachment.content_hash)
    assert cached is not None
    assert cached["extraction_error"] == "ValueError"
    for marker in ("SYNTHETIC_FILENAME_MARKER", "SYNTHETIC_EXC_MARKER"):
        assert marker not in caplog.text


def test_payload_re_arriving_after_its_last_carrier_was_reaped_is_re_extracted(
    tmp_path, monkeypatch
):
    """Reaping the last message carrying a payload purges its cached
    extraction (#562), so the same bytes arriving again later are
    extracted afresh rather than served from the cache."""
    db = Database(tmp_path / "mail.db")
    first = make_message(message_id="first@x", filepath="/m/first")
    keep = make_message(message_id="keep@x", filepath="/m/keep")
    db.upsert_thread(
        make_thread(messages=[first, keep], thread_id="t-rearrive"), [0.0] * EMBEDDING_DIM
    )
    attachment = _attachment(b"payload that arrives twice")
    embedder = make_mock_embedder()
    embedder.embed.return_value = [0.1] * EMBEDDING_DIM
    kwargs: dict[str, Any] = dict(
        attachment=attachment,
        thread_id="t-rearrive",
        db=db,
        embedder=embedder,
        chunk_target_tokens=350,
        chunk_max_tokens=500,
        chunk_overlap_tokens=60,
        ocr_enabled=True,
        max_bytes=10_000_000,
        max_ocr_pages=20,
    )
    _prepare_and_apply(claimant_id="first@x", **kwargs)
    assert db.get_attachment_extraction(attachment.content_hash) is not None

    db.add_pending_deletion("/m/first", "first@x", "t-rearrive")
    db.reap_thread_messages(
        make_thread(messages=[keep], thread_id="t-rearrive"), [0.0] * EMBEDDING_DIM, ["first@x"]
    )
    assert db.get_attachment_extraction(attachment.content_hash) is None

    extract = MagicMock(wraps=attachment_indexing.extract_attachment)
    monkeypatch.setattr(attachment_indexing, "extract_attachment", extract)
    _prepare_and_apply(claimant_id="keep@x", **kwargs)

    extract.assert_called_once()
    assert db.get_attachment_extraction(attachment.content_hash) is not None


# ---------------------------------------------------------------------------
# Outcome aggregate (#871)
# ---------------------------------------------------------------------------


def _failing_extractor(monkeypatch) -> None:
    from src import extractors

    def boom(payload, **opts):
        raise ValueError("SYNTHETIC_EXC_MARKER")

    monkeypatch.setattr(extractors, "_safe_import", lambda module_name: boom)


# One attachment per outcome, with the plan status each produced before
# the aggregate existed (pinned: counting must not change it).
_OUTCOME_SHAPES: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {
    "success": (dict(payload=b"SYNTHETIC_TEXT_MARKER words"), {}, STATUS_SUCCESS),
    "empty": (dict(payload=b"   \n "), {}, STATUS_EMPTY),
    "unsupported": (
        dict(content_type="application/x-unknown", filename="SYNTHETIC_FILENAME_MARKER.bin"),
        {},
        STATUS_UNSUPPORTED,
    ),
    "too_large": (dict(payload=b"SYNTHETIC_TEXT_MARKER"), {"max_bytes": 4}, STATUS_TOO_LARGE),
    "ocr_disabled": (
        dict(content_type="image/png", filename="SYNTHETIC_FILENAME_MARKER.png"),
        {"ocr_enabled": False},
        STATUS_UNSUPPORTED,
    ),
    "failed": (dict(filename="SYNTHETIC_FILENAME_MARKER.txt"), {}, STATUS_FAILED),
}


class TestAttachmentOutcomeCounts:
    """#871: the periodic attachments aggregate counts every attachment
    occurrence by outcome, so a broken OCR toolchain or parser shows up
    as a rising ``failed`` / ``ocr_disabled`` count rather than not at
    all. Occurrences are counted when their message commits (review
    round 1), so a message re-prepared after an embedder outage is not
    counted twice."""

    @staticmethod
    def _run(tmp_path, monkeypatch, outcome):
        attachment_kwargs, overrides, _ = _OUTCOME_SHAPES[outcome]
        if outcome == "failed":
            _failing_extractor(monkeypatch)
        tmp_path.mkdir(parents=True, exist_ok=True)
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment(**attachment_kwargs)
        return prepare_attachment_writes(db=db, **_kwargs(attachment, **overrides))

    @staticmethod
    def _zero() -> dict[str, int]:
        return dict.fromkeys(attachment_indexing.ATTACHMENT_OUTCOMES, 0) | {
            "cached": 0,
            "pdf_pages_failed": 0,
            "ocr_capped_pdfs": 0,
            "ocr_pages_skipped": 0,
            "warnings_suppressed": 0,
        }

    @staticmethod
    def _drain() -> dict[str, int]:
        return attachment_indexing.attachment_outcomes.drain()

    @staticmethod
    def _commit(*plans) -> None:
        attachment_indexing.record_committed_outcomes(list(plans))

    def test_outcome_names_are_fixed(self):
        assert attachment_indexing.ATTACHMENT_OUTCOMES == (
            "success",
            "failed",
            "unsupported",
            "too_large",
            "ocr_disabled",
            "empty",
        )

    def test_each_outcome_is_counted_once_its_message_commits(self, tmp_path, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        for outcome, (_, _, status) in _OUTCOME_SHAPES.items():
            self._drain()
            plan = self._run(tmp_path / outcome, monkeypatch, outcome)
            assert plan.status == status
            # Preparing alone counts nothing: the message may not commit.
            assert self._drain() == self._zero()
            self._commit(plan)
            assert self._drain() == self._zero() | {outcome: 1}
        for marker in (
            "SYNTHETIC_FILENAME_MARKER",
            "SYNTHETIC_TEXT_MARKER",
            "SYNTHETIC_EXC_MARKER",
        ):
            assert marker not in caplog.text

    def test_a_message_prepared_twice_is_counted_once(self, tmp_path, monkeypatch):
        """Review round 1: after an embedder outage the message is
        prepared again; only the attempt that commits is counted."""
        self._drain()
        self._run(tmp_path / "first", monkeypatch, "failed")
        plan = self._run(tmp_path / "retry", monkeypatch, "failed")
        self._commit(plan)
        assert self._drain()["failed"] == 1

    def test_drain_resets_the_counts(self, tmp_path, monkeypatch):
        self._drain()
        self._commit(self._run(tmp_path, monkeypatch, "success"))
        assert self._drain()["success"] == 1
        assert self._drain() == self._zero()

    def test_cache_hits_and_batch_reuse_count_as_cached(self, tmp_path):
        self._drain()
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment(b"cached words")
        batch: dict[str, ExtractionResult] = {}
        # Extracted, then reused from the batch before it commits.
        plans = [
            prepare_attachment_writes(db=db, batch_extractions=batch, **_kwargs(attachment))
            for _ in range(2)
        ]
        _embed_new_chunks(
            plans[1],
            db=db,
            claimant_id="msg@x",
            embedder=make_mock_embedder([0.1] * EMBEDDING_DIM),
        )
        with db.transaction():
            apply_attachment_writes(plan=plans[1], claimant_id="msg@x", thread_id="thread-x", db=db)
        # Served from the committed cache.
        plans.append(prepare_attachment_writes(db=db, **_kwargs(attachment)))
        self._commit(*plans)
        assert self._drain() == self._zero() | {"success": 3, "cached": 2}

    def test_cached_ocr_disabled_row_counts_as_ocr_disabled(self, tmp_path):
        self._drain()
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment(b"png bytes", content_type="image/png", filename="a.png")
        for _ in range(2):
            plan = prepare_attachment_writes(db=db, **_kwargs(attachment, ocr_enabled=False))
            with db.transaction():
                apply_attachment_writes(plan=plan, claimant_id="msg@x", thread_id="thread-x", db=db)
            self._commit(plan)
        assert self._drain() == self._zero() | {"ocr_disabled": 2, "cached": 1}

    def test_summary_line(self):
        counts = self._zero() | {
            "success": 3,
            "failed": 1,
            "ocr_disabled": 2,
            "cached": 4,
            "pdf_pages_failed": 5,
            "ocr_capped_pdfs": 7,
            "ocr_pages_skipped": 8,
            "warnings_suppressed": 6,
        }
        assert attachment_indexing.format_attachment_outcomes(counts) == (
            "attachments n=6 success=3 failed=1 unsupported=0 too_large=0 "
            "ocr_disabled=2 empty=0 cached=4 pdf_pages_failed=5 ocr_capped_pdfs=7 "
            "ocr_pages_skipped=8 warnings_suppressed=6"
        )

    @pytest.mark.parametrize(
        "field, degraded",
        [
            ("success", False),
            ("empty", False),
            ("cached", False),
            ("failed", True),
            ("unsupported", True),
            ("too_large", True),
            ("ocr_disabled", True),
            ("pdf_pages_failed", True),
            ("ocr_capped_pdfs", True),
            ("ocr_pages_skipped", True),
            ("warnings_suppressed", True),
        ],
    )
    def test_degraded_counts(self, field, degraded):
        """Review round 1: the line is a WARNING when any count means
        attachment text is missing from search."""
        counts = self._zero() | {"success": 1, field: 1}
        assert attachment_indexing.attachment_outcomes_degraded(counts) is degraded

    def test_pdf_pages_pypdf_could_not_read_are_counted(self, tmp_path, monkeypatch, caplog):
        """A PDF page whose text layer pypdf cannot read is skipped
        (DEBUG per page); the aggregate counts those pages. The
        extraction result is unchanged: these pages give no digital text,
        so with OCR off the PDF is recorded as needing OCR. Pages count
        per extraction attempt, not per commit."""
        from src.extractors import pdf

        caplog.set_level("DEBUG")

        class BadPage:
            def extract_text(self):
                raise ValueError("SYNTHETIC_PYPDF_MARKER")

        class FakeReader:
            def __init__(self, stream):
                self.pages = [BadPage(), BadPage()]

        monkeypatch.setattr(pdf.pypdf, "PdfReader", FakeReader)
        self._drain()
        db = _setup_db_for_attachment(tmp_path)
        attachment = _attachment(
            b"%PDF-1.7", content_type="application/pdf", filename="SYNTHETIC_FILENAME_MARKER.pdf"
        )
        plan = prepare_attachment_writes(db=db, **_kwargs(attachment, ocr_enabled=False))
        assert plan.status == STATUS_UNSUPPORTED
        assert plan.extraction_to_persist is not None
        assert plan.extraction_to_persist.error == SCANNED_PDF_OCR_DISABLED_ERROR
        self._commit(plan)
        assert self._drain() == self._zero() | {"ocr_disabled": 1, "pdf_pages_failed": 2}
        for marker in ("SYNTHETIC_PYPDF_MARKER", "SYNTHETIC_FILENAME_MARKER"):
            assert marker not in caplog.text

    def test_no_summary_line_without_attachments(self):
        assert attachment_indexing.format_attachment_outcomes(self._zero()) == ""
