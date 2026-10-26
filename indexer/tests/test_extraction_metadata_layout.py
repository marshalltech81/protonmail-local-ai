"""Cache metadata precedes large text without changing cached evidence (#1381)."""

import sqlite3

import pytest
from src.database import SCHEMA_VERSION, Database

from tests.conftest import TEST_ACCOUNT
from tests.test_database import _schema_shape

# The deployed v22 layout, before the metadata-only physical reorder.
_V22_CACHE = """
CREATE TABLE attachment_extractions (
    attachment_id TEXT NOT NULL,
    extractor_module TEXT NOT NULL,
    extraction_status TEXT NOT NULL,
    extractor TEXT,
    extracted_text TEXT,
    extraction_error TEXT,
    extracted_at TEXT NOT NULL,
    ocr_pages_skipped INTEGER CHECK (ocr_pages_skipped >= 0),
    text_complete INTEGER CHECK (text_complete IN (0, 1)),
    ocr_pages_cap INTEGER CHECK (ocr_pages_cap >= 0),
    digital_pages_cap INTEGER CHECK (digital_pages_cap >= 0),
    extracted_chars_cap INTEGER CHECK (extracted_chars_cap >= 0),
    identifier TEXT,
    PRIMARY KEY (attachment_id, extractor_module)
)
"""


def _v22(db):
    from tests.conftest import drop_literal_schema

    drop_literal_schema(db._conn)
    db._conn.execute("DROP TABLE IF EXISTS authority_rule_state")
    db._conn.execute("DROP TABLE attachment_extractions")
    db._conn.execute(_V22_CACHE)
    db._conn.execute("UPDATE schema_version SET version = 22")
    for number, status in enumerate(("success", "empty", "failed", "unsupported", "too_large")):
        db._conn.execute(
            "INSERT INTO attachment_extractions (rowid, attachment_id, extractor_module, "
            "extraction_status, extractor, extracted_text, extraction_error, extracted_at, "
            "ocr_pages_skipped, text_complete, ocr_pages_cap, digital_pages_cap, "
            "extracted_chars_cap, identifier) VALUES (?, ?, 'text', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                100 + number,
                f"synthetic-{number}",
                status,
                "text@3",
                "synthetic " * 200000 if status == "success" else None,
                "Fixed synthetic error" if status == "failed" else None,
                "2026-01-01T00:00:00+00:00",
                number if number % 2 else None,
                number % 2,
                None,
                0,
                20,
                "none@1",
            ),
        )
    db._conn.commit()


def _rows(conn):
    return [
        dict(row)
        for row in conn.execute("SELECT rowid, * FROM attachment_extractions ORDER BY rowid")
    ]


def test_fresh_metadata_precedes_extracted_text(db):
    columns = [row[1] for row in db._conn.execute("PRAGMA table_info(attachment_extractions)")]
    assert columns[-1] == "extracted_text"


def test_v22_cache_migration_preserves_every_value_and_rowid(tmp_path):
    path = tmp_path / "old.db"
    old = Database(path, account=TEST_ACCOUNT)
    _v22(old)
    before = [dict(row, text_sha256=None) for row in _rows(old._conn)]
    old.close()
    migrated = Database(path, account=TEST_ACCOUNT)
    fresh = Database(tmp_path / "fresh.db", account=TEST_ACCOUNT)
    try:
        assert (
            migrated._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == SCHEMA_VERSION
        )
        assert _rows(migrated._conn) == before
        assert _schema_shape(migrated._conn) == _schema_shape(fresh._conn)
        columns = [
            row[1] for row in migrated._conn.execute("PRAGMA table_info(attachment_extractions)")
        ]
        assert columns[-1] == "extracted_text"
        for expected in before:
            actual = migrated.get_attachment_extraction(expected["attachment_id"], "text")
            assert actual is not None
            assert dict(actual) == {key: value for key, value in expected.items() if key != "rowid"}
        assert (
            migrated._conn.execute("SELECT COUNT(*) FROM indexing_jobs").fetchone()[0] == 0
        )  # This fixture has cache rows but no indexed files to backfill.
    finally:
        migrated.close()
        fresh.close()


def test_failed_rebuild_restores_original_cache_and_version(tmp_path):
    db = Database(tmp_path / "failure.db", account=TEST_ACCOUNT)
    _v22(db)
    before = _rows(db._conn)
    db._conn.set_authorizer(
        lambda action, *args: (
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_DROP_TABLE else sqlite3.SQLITE_OK
        )
    )
    try:
        with pytest.raises(sqlite3.DatabaseError):
            db._migrate()
    finally:
        db._conn.set_authorizer(None)
    try:
        assert _rows(db._conn) == before
        assert db._conn.execute("SELECT version FROM schema_version").fetchone()[0] == 22
        assert (
            db._conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='attachment_extractions_v23'"
            ).fetchone()[0]
            == 0
        )
    finally:
        db.close()
