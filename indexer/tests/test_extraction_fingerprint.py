"""Stored-text identity is atomic with extraction and backfills without extraction (#1361)."""

import hashlib

import pytest
from src.database import Database

from tests.conftest import TEST_ACCOUNT


def _store(db, key, text, module="text"):
    db.store_attachment_extraction(
        attachment_id=key,
        extractor_module=module,
        extraction_status="success" if text else "empty",
        extractor=module + "@1",
        extracted_text=text,
        extraction_error=None,
    )


@pytest.mark.parametrize(
    "text", [None, "", "plain", "é\x00😀" * 20_000], ids=["none", "empty", "ascii", "unicode"]
)
def test_new_and_replaced_text_has_its_own_hash(tmp_path, text):
    db = Database(tmp_path / "mail.db", account=TEST_ACCOUNT)
    try:
        _store(db, "synthetic", "before")
        _store(db, "synthetic", text)
        row = db.get_attachment_extraction("synthetic", "text")
        assert row["text_sha256"] == (
            None if text is None else hashlib.sha256(text.encode()).hexdigest()
        )
    finally:
        db.close()


def test_backfill_resumes_and_never_rehashes_finished_rows(tmp_path):
    path = tmp_path / "mail.db"
    db = Database(path, account=TEST_ACCOUNT)
    for i, text in enumerate(["é\x00😀" * 40_000, "", "last"]):
        _store(db, str(i), text)
    db._conn.execute("UPDATE attachment_extractions SET text_sha256=NULL")
    db._conn.commit()
    assert db.backfill_extraction_fingerprints(max_rows=1, max_bytes=1) == (1, 280_000, True)
    db.close()
    db = Database(path, account=TEST_ACCOUNT)
    try:
        assert db.backfill_extraction_fingerprints(max_rows=1) == (1, 0, True)
        assert db.backfill_extraction_fingerprints(max_rows=1) == (1, 4, False)
        assert db.backfill_extraction_fingerprints() == (0, 0, False)
        assert (
            db.get_attachment_extraction("0", "text")["text_sha256"]
            == hashlib.sha256(("é\x00😀" * 40_000).encode()).hexdigest()
        )
    finally:
        db.close()


def test_v26_migration_leaves_identity_unknown_and_queues_nothing(tmp_path):
    path = tmp_path / "mail.db"
    db = Database(path, account=TEST_ACCOUNT)
    _store(db, "synthetic", "keep me")
    db._conn.execute("DROP INDEX idx_extractions_missing_text_sha256")
    db._conn.execute("ALTER TABLE attachment_extractions DROP COLUMN text_sha256")
    db._conn.execute("UPDATE schema_version SET version=26")
    db._conn.commit()
    db.close()
    db = Database(path, account=TEST_ACCOUNT)
    try:
        row = db.get_attachment_extraction("synthetic", "text")
        assert row["extracted_text"] == "keep me" and row["text_sha256"] is None
        assert db.queue_stats() == {"queued": 0, "dead": 0}
        assert db.backfill_extraction_fingerprints() == (1, 7, False)
    finally:
        db.close()


def test_backfill_reads_exact_bytes_in_fixed_blocks_and_uses_pending_index(tmp_path):
    from contextlib import contextmanager

    db = Database(tmp_path / "mail.db", account=TEST_ACCOUNT)
    text = "word é\x00😀 " * 50_000
    _store(db, "first", text)
    _store(db, "second", "untouched")
    db._conn.execute("UPDATE attachment_extractions SET text_sha256=NULL")
    db._conn.commit()
    conn = db._conn
    read_sizes = []
    returned = []

    class CountingConnection:
        def __getattr__(self, name):
            return getattr(conn, name)

        @contextmanager
        def blobopen(self, *args, **kwargs):
            with conn.blobopen(*args, **kwargs) as blob:

                class CountingBlob:
                    def read(self, n):
                        read_sizes.append(n)
                        block = blob.read(n)
                        returned.append(len(block))
                        return block

                yield CountingBlob()

    db._conn = CountingConnection()
    try:
        assert db.backfill_extraction_fingerprints(max_bytes=1) == (1, len(text.encode()), True)
        assert sum(returned) == len(text.encode())
        assert set(read_sizes) == {64 * 1024}
        assert db.get_attachment_extraction("second", "text")["text_sha256"] is None
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT rowid FROM attachment_extractions "
            "WHERE text_sha256 IS NULL AND extracted_text IS NOT NULL "
            "ORDER BY attachment_id, extractor_module LIMIT 1"
        ).fetchall()
        assert any("idx_extractions_missing_text_sha256" in str(tuple(r)) for r in plan)
    finally:
        db.close()


def test_failed_backfill_rolls_back_the_batch_and_logs_no_content(tmp_path, monkeypatch, caplog):
    import logging

    from src import main

    db = Database(tmp_path / "mail.db", account=TEST_ACCOUNT)
    marker = "SYNTHETIC_PRIVATE_FINGERPRINT_MARKER"
    for key in ["a", "b"]:
        _store(db, key, marker)
    db._conn.execute("UPDATE attachment_extractions SET text_sha256=NULL")
    db._conn.execute(
        "CREATE TRIGGER fail_hash BEFORE UPDATE OF text_sha256 ON attachment_extractions "
        "WHEN OLD.attachment_id='b' BEGIN SELECT RAISE(ABORT, 'SYNTHETIC_PRIVATE_FINGERPRINT_MARKER'); END"
    )
    db._conn.commit()
    monkeypatch.setattr(main, "_extraction_fingerprint_backfill_done", False)
    monkeypatch.setattr(
        main,
        "_streaks",
        {
            main.EXTRACTION_FINGERPRINT_BACKFILL: main._FailureStreak(
                main.EXTRACTION_FINGERPRINT_BACKFILL
            )
        },
    )
    try:
        with caplog.at_level(logging.INFO):
            main._run_extraction_fingerprint_backfill(db)
            assert db.get_attachment_extraction("a", "text")["text_sha256"] is None
            db._conn.execute("DROP TRIGGER fail_hash")
            db._conn.commit()
            main._run_extraction_fingerprint_backfill(db)
            main._run_extraction_fingerprint_backfill(db)
        assert "failed: IntegrityError" in caplog.text
        assert "filled=2" in caplog.text and f"bytes_read={2 * len(marker.encode())}" in caplog.text
        assert "every stored text has an identity" in caplog.text
        assert "recovered" in caplog.text
        assert marker not in caplog.text
        assert main._extraction_fingerprint_backfill_done
    finally:
        db.close()


@pytest.mark.parametrize("kwargs", [{"max_rows": 0}, {"max_bytes": 0}])
def test_backfill_requires_positive_budgets(tmp_path, kwargs):
    db = Database(tmp_path / "mail.db", account=TEST_ACCOUNT)
    try:
        with pytest.raises(ValueError, match="budgets must be positive"):
            db.backfill_extraction_fingerprints(**kwargs)
    finally:
        db.close()
