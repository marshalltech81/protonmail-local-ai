"""
Tests for src/database.py.

Covers: schema creation, forward migrations through the current
``SCHEMA_VERSION``, ``upsert_thread`` (insert and body-accumulation update),
threading lookups, file tracking, and stats.
"""

import json
import sqlite3
from datetime import UTC, datetime

from src.database import SCHEMA_VERSION, Database

from tests.conftest import make_message, make_thread

FAKE_EMBEDDING = [0.1] * 768


# ---------------------------------------------------------------------------
# Schema and migrations
# ---------------------------------------------------------------------------


class TestSchema:
    def test_database_created_at_given_path(self, tmp_path):
        db_path = tmp_path / "mail.db"
        Database(db_path)
        assert db_path.exists()

    def test_schema_version_set_correctly(self, db):
        row = db._conn.execute("SELECT version FROM schema_version").fetchone()
        assert row["version"] == SCHEMA_VERSION

    def test_required_tables_exist(self, db):
        tables = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "threads" in tables
        assert "message_thread_map" in tables
        assert "indexed_files" in tables
        assert "schema_version" in tables

    def test_body_text_column_exists(self, db):
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(threads)").fetchall()}
        assert "body_text" in cols

    def test_v1_to_v2_migration_adds_body_text(self, tmp_path):
        """A database created at schema v1 migrates forward to SCHEMA_VERSION."""
        db_path = tmp_path / "v1.db"

        # Build a v1 database manually — no body_text column
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version VALUES (1)")
        conn.execute("""
            CREATE TABLE threads (
                thread_id    TEXT PRIMARY KEY,
                subject      TEXT NOT NULL,
                participants TEXT NOT NULL,
                folder       TEXT NOT NULL,
                date_first   TEXT NOT NULL,
                date_last    TEXT NOT NULL,
                message_ids  TEXT NOT NULL,
                snippet      TEXT,
                has_attachments INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()

        # Open with Database — migrations should run forward to SCHEMA_VERSION
        db = Database(db_path)
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(threads)").fetchall()}
        assert "body_text" in cols
        row = db._conn.execute("SELECT version FROM schema_version").fetchone()
        assert row["version"] == SCHEMA_VERSION

    def test_v2_to_v3_migration_adds_fts_rowid_and_rebuilds_fts(self, tmp_path):
        """A v2 database migrates forward, gaining the fts_rowid column and
        a threads_fts table rebuilt with contentless_delete=1."""
        db_path = tmp_path / "v2.db"

        # Build a v2 database manually — has body_text, old-style threads_fts
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version VALUES (2)")
        conn.execute("""
            CREATE TABLE threads (
                thread_id    TEXT PRIMARY KEY,
                subject      TEXT NOT NULL,
                participants TEXT NOT NULL,
                folder       TEXT NOT NULL,
                date_first   TEXT NOT NULL,
                date_last    TEXT NOT NULL,
                message_ids  TEXT NOT NULL,
                snippet      TEXT,
                has_attachments INTEGER DEFAULT 0,
                body_text    TEXT
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE threads_fts
            USING fts5(
                thread_id UNINDEXED, subject, participants, body,
                content='', tokenize='porter unicode61'
            )
        """)
        conn.execute(
            "INSERT INTO threads (thread_id, subject, participants, folder, "
            "date_first, date_last, message_ids, snippet, body_text) "
            "VALUES ('t1', 'old subject', '[]', 'INBOX', "
            "'2024-01-01', '2024-01-01', '[]', 's', 'carryovertoken body')"
        )
        conn.commit()
        conn.close()

        db = Database(db_path)

        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(threads)").fetchall()}
        assert "fts_rowid" in cols

        version_row = db._conn.execute("SELECT version FROM schema_version").fetchone()
        assert version_row["version"] == SCHEMA_VERSION

        # Existing thread's body is backfilled into the rebuilt FTS and its
        # rowid recorded so future keyword searches can join via fts_rowid.
        hit = db._conn.execute(
            """
            SELECT t.thread_id
            FROM threads_fts
            JOIN threads t ON threads_fts.rowid = t.fts_rowid
            WHERE threads_fts MATCH 'carryovertoken'
            """
        ).fetchone()
        assert hit is not None
        assert hit["thread_id"] == "t1"

    def test_migration_is_idempotent(self, tmp_path):
        """Opening an already-migrated database does not error."""
        db_path = tmp_path / "idem.db"
        Database(db_path)
        Database(db_path)  # second open must not raise


# ---------------------------------------------------------------------------
# upsert_thread — insert
# ---------------------------------------------------------------------------


class TestUpsertThreadInsert:
    def test_inserts_thread_record(self, db):
        thread = make_thread()
        db.upsert_thread(thread, FAKE_EMBEDDING)
        row = db._conn.execute(
            "SELECT * FROM threads WHERE thread_id = ?", (thread.thread_id,)
        ).fetchone()
        assert row is not None
        assert row["subject"] == thread.subject
        assert row["folder"] == thread.folder

    def test_inserts_message_thread_map(self, db):
        msg = make_message()
        thread = make_thread(messages=[msg])
        db.upsert_thread(thread, FAKE_EMBEDDING)
        row = db._conn.execute(
            "SELECT thread_id FROM message_thread_map WHERE message_id = ?",
            (msg.message_id,),
        ).fetchone()
        assert row is not None
        assert row["thread_id"] == thread.thread_id

    def test_marks_filepath_as_indexed(self, db):
        msg = make_message(filepath="/maildir/INBOX/cur/msg1")
        thread = make_thread(messages=[msg])
        db.upsert_thread(thread, FAKE_EMBEDDING)
        assert db.is_indexed("/maildir/INBOX/cur/msg1")

    def test_stores_body_text_on_insert(self, db):
        msg = make_message(body_text="Important content here.")
        thread = make_thread(messages=[msg])
        db.upsert_thread(thread, FAKE_EMBEDDING)
        row = db._conn.execute(
            "SELECT body_text FROM threads WHERE thread_id = ?", (thread.thread_id,)
        ).fetchone()
        assert row["body_text"] is not None
        assert "Important content here." in row["body_text"]

    def test_has_attachments_flag_set(self, db):
        msg = make_message(has_attachments=True)
        thread = make_thread(messages=[msg])
        db.upsert_thread(thread, FAKE_EMBEDDING)
        row = db._conn.execute(
            "SELECT has_attachments FROM threads WHERE thread_id = ?",
            (thread.thread_id,),
        ).fetchone()
        assert row["has_attachments"] == 1

    def test_participants_stored_as_json_array(self, db):
        thread = make_thread()
        db.upsert_thread(thread, FAKE_EMBEDDING)
        row = db._conn.execute(
            "SELECT participants FROM threads WHERE thread_id = ?",
            (thread.thread_id,),
        ).fetchone()
        participants = json.loads(row["participants"])
        assert isinstance(participants, list)
        assert len(participants) > 0


# ---------------------------------------------------------------------------
# upsert_thread — update with body accumulation
# ---------------------------------------------------------------------------


class TestUpsertThreadUpdate:
    def test_body_text_accumulates_on_second_message(self, db, threader):
        """
        When a new message joins an existing thread, its content must be
        appended to the stored body_text — not overwrite it. This ensures the
        embedding represents the full conversation, not just the latest message.
        """
        original = make_message(
            message_id="orig@example.com",
            body_text="Original message content.",
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, FAKE_EMBEDDING)

        reply = make_message(
            message_id="reply@example.com",
            body_text="Reply message content.",
            in_reply_to="orig@example.com",
            filepath="/maildir/INBOX/cur/reply",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t2 = threader.assign_thread(reply)
        db.upsert_thread(t2, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT body_text FROM threads WHERE thread_id = 'orig@example.com'"
        ).fetchone()
        assert "Original message content." in row["body_text"]
        assert "Reply message content." in row["body_text"]

    def test_update_advances_date_last(self, db, threader):
        original = make_message(
            message_id="dates_orig@example.com",
            date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, FAKE_EMBEDDING)

        reply = make_message(
            message_id="dates_reply@example.com",
            subject="Re: Hello world",
            in_reply_to="dates_orig@example.com",
            filepath="/maildir/INBOX/cur/reply",
            date=datetime(2024, 6, 1, tzinfo=UTC),
        )
        t2 = threader.assign_thread(reply)
        db.upsert_thread(t2, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT date_last FROM threads WHERE thread_id = 'dates_orig@example.com'"
        ).fetchone()
        assert "2024-06-01" in row["date_last"]

    def test_accumulated_body_capped_at_8000_chars(self, db, threader):
        original = make_message(
            message_id="long_orig@example.com",
            body_text="A" * 500,
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, FAKE_EMBEDDING)

        for i in range(20):
            reply = make_message(
                message_id=f"long_reply_{i}@example.com",
                body_text="B" * 500,
                in_reply_to="long_orig@example.com",
                filepath=f"/maildir/INBOX/cur/reply_{i}",
                date=datetime(2024, 1, i + 2, tzinfo=UTC),
            )
            t = threader.assign_thread(reply)
            db.upsert_thread(t, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT body_text FROM threads WHERE thread_id = 'long_orig@example.com'"
        ).fetchone()
        assert len(row["body_text"]) <= 8000


# ---------------------------------------------------------------------------
# Lookup methods
# ---------------------------------------------------------------------------


class TestLookups:
    def test_find_thread_by_message_id_hit(self, db):
        msg = make_message(message_id="findme@example.com")
        thread = make_thread(messages=[msg])
        db.upsert_thread(thread, FAKE_EMBEDDING)
        result = db.find_thread_by_message_id("findme@example.com")
        assert result == thread.thread_id

    def test_find_thread_by_message_id_miss(self, db):
        assert db.find_thread_by_message_id("ghost@example.com") is None

    def test_find_thread_by_subject_hit(self, db):
        thread = make_thread(subject="budget discussion")
        db.upsert_thread(thread, FAKE_EMBEDDING)
        result = db.find_thread_by_subject("budget discussion", "INBOX")
        assert result == thread.thread_id

    def test_find_thread_by_subject_miss_wrong_folder(self, db):
        thread = make_thread(subject="budget discussion", folder="INBOX")
        db.upsert_thread(thread, FAKE_EMBEDDING)
        result = db.find_thread_by_subject("budget discussion", "Sent")
        assert result is None

    def test_find_thread_by_subject_miss_unknown_subject(self, db):
        assert db.find_thread_by_subject("unknown subject", "INBOX") is None

    def test_get_thread_returns_thread(self, db):
        thread = make_thread()
        db.upsert_thread(thread, FAKE_EMBEDDING)
        loaded = db.get_thread(thread.thread_id)
        assert loaded is not None
        assert loaded.thread_id == thread.thread_id
        assert loaded.subject == thread.subject

    def test_get_thread_returns_none_for_missing(self, db):
        assert db.get_thread("nonexistent") is None


# ---------------------------------------------------------------------------
# is_indexed
# ---------------------------------------------------------------------------


class TestIsIndexed:
    def test_returns_false_before_indexing(self, db):
        assert db.is_indexed("/maildir/INBOX/cur/unindexed") is False

    def test_returns_true_after_indexing(self, db):
        msg = make_message(filepath="/maildir/INBOX/cur/tracked")
        thread = make_thread(messages=[msg])
        db.upsert_thread(thread, FAKE_EMBEDDING)
        assert db.is_indexed("/maildir/INBOX/cur/tracked") is True


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------


class TestGetStats:
    def test_empty_database_stats(self, db):
        stats = db.get_stats()
        assert stats["total_threads"] == 0
        assert stats["total_messages"] == 0
        assert stats["oldest_message"] is None
        assert stats["newest_message"] is None

    def test_stats_reflect_indexed_data(self, db):
        msg1 = make_message(
            message_id="stats1@example.com",
            filepath="/maildir/INBOX/cur/stats1",
            date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        msg2 = make_message(
            message_id="stats2@example.com",
            filepath="/maildir/INBOX/cur/stats2",
            date=datetime(2024, 6, 1, tzinfo=UTC),
        )
        db.upsert_thread(make_thread(messages=[msg1], thread_id="t1"), FAKE_EMBEDDING)
        db.upsert_thread(
            make_thread(messages=[msg2], thread_id="t2", subject="other"), FAKE_EMBEDDING
        )

        stats = db.get_stats()
        assert stats["total_threads"] == 2
        assert stats["total_messages"] == 2


# ---------------------------------------------------------------------------
# FTS behavior — contentless_delete + fts_rowid (schema v3)
# ---------------------------------------------------------------------------


class TestFtsRowidAndReplacement:
    def test_upsert_update_replaces_fts_row_instead_of_accumulating(self, db, threader):
        """Body-text updates must DELETE the prior FTS row so stale tokens do
        not linger in the search index. Regression test for the pre-v3 bug
        where DELETE silently no-op'd on contentless tables without
        ``contentless_delete=1``.
        """
        msg = make_message(
            message_id="upd@x",
            body_text="oldcontentmarker1 oldcontentmarker2",
            filepath="/u/1",
        )
        t = threader.assign_thread(msg)
        db.upsert_thread(t, FAKE_EMBEDDING)

        reply = make_message(
            message_id="upd_reply@x",
            body_text="newcontentmarker1 newcontentmarker2",
            in_reply_to="upd@x",
            filepath="/u/2",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        t2 = threader.assign_thread(reply)
        db.upsert_thread(t2, FAKE_EMBEDDING)

        # Only one FTS row per thread even after update
        total_fts = db._conn.execute("SELECT COUNT(*) FROM threads_fts").fetchone()[0]
        assert total_fts == 1

    def test_keyword_join_via_fts_rowid(self, db, threader):
        """FTS rowid → thread row join (the pattern MCP keyword search uses)."""
        msg = make_message(message_id="join@x", body_text="uniquejointoken here")
        t = threader.assign_thread(msg)
        db.upsert_thread(t, FAKE_EMBEDDING)

        row = db._conn.execute(
            """
            SELECT t.thread_id
            FROM threads_fts
            JOIN threads t ON threads_fts.rowid = t.fts_rowid
            WHERE threads_fts MATCH 'uniquejointoken'
            """
        ).fetchone()
        assert row is not None
        assert row["thread_id"] == t.thread_id
