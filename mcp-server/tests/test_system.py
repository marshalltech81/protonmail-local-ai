"""Tests for src/tools/system.py standalone helpers.

The MCP-registered ``get_index_status`` tool is covered by framework
wiring; this file targets the module-level ``get_index_status`` helper
used by the ``make status`` Makefile target, which previously returned
a hardcoded ``{"status": "ok"}`` regardless of actual index state.
"""

import sqlite3
from pathlib import Path

import pytest
import sqlite_vec
from src.tools.system import get_index_status


def _build_minimal_schema(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(
        """
        CREATE TABLE threads (
            thread_id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            participants TEXT NOT NULL,
            folder TEXT NOT NULL,
            date_first TEXT NOT NULL,
            date_last TEXT NOT NULL,
            message_ids TEXT NOT NULL,
            snippet TEXT,
            has_attachments INTEGER DEFAULT 0,
            body_text TEXT,
            fts_rowid INTEGER
        );
        CREATE TABLE message_thread_map (
            message_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            filepath TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE threads_fts USING fts5(
            subject, participants, body, content='',
            contentless_delete=1, tokenize='porter unicode61'
        );
        CREATE VIRTUAL TABLE threads_vec USING vec0(
            thread_id TEXT PRIMARY KEY, embedding FLOAT[4]
        );
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def populated_db(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / "index.db"
    _build_minimal_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        INSERT INTO threads VALUES
            ('t1', 's1', '[]', 'INBOX', '2024-01-01T00:00:00+00:00',
             '2024-06-01T00:00:00+00:00', '["t1"]', 's', 0, '', 1);
        INSERT INTO message_thread_map VALUES ('t1', 't1', '/m/1');
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("SQLITE_PATH", str(db_path))
    return db_path


class TestGetIndexStatusStandalone:
    def test_returns_real_stats_from_populated_index(self, populated_db):
        status = get_index_status()
        assert status["status"] == "ok"
        assert status["total_threads"] == 1
        assert status["total_messages"] == 1
        assert "2024-01-01" in (status["oldest_message"] or "")
        assert "2024-06-01" in (status["newest_message"] or "")
        assert "checked_at" in status

    def test_empty_index_reports_zero_counts(self, tmp_path, monkeypatch):
        db_path = tmp_path / "empty.db"
        _build_minimal_schema(db_path)
        monkeypatch.setenv("SQLITE_PATH", str(db_path))
        status = get_index_status()
        assert status["status"] == "ok"
        assert status["total_threads"] == 0
        assert status["total_messages"] == 0

    def test_returns_error_when_db_cannot_be_opened(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "does-not-exist.db"))
        status = get_index_status()
        assert status["status"] == "error"
        assert "error" in status
