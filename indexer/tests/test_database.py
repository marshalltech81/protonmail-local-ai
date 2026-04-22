"""
Tests for src/database.py.

Covers: schema creation, forward migrations through the current
``SCHEMA_VERSION``, ``upsert_thread`` (insert and body-accumulation update),
threading lookups, file tracking, and stats.
"""

import json
import sqlite3
import threading
from datetime import UTC, datetime

import pytest
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

    def test_raises_clear_error_when_sqlite_too_old(self, tmp_path, monkeypatch):
        """Schema v3 uses ``contentless_delete=1`` (SQLite >= 3.43). If the
        runtime is older we fail loudly at Database init with an actionable
        message, instead of silently dying inside a broad migration except."""
        from src import database

        monkeypatch.setattr(database.sqlite3, "sqlite_version", "3.40.1")
        monkeypatch.setattr(database.sqlite3, "sqlite_version_info", (3, 40, 1))
        with pytest.raises(database.SQLiteTooOldError, match="contentless_delete"):
            Database(tmp_path / "too_old.db")

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

    def test_v3_to_v4_migration_adds_pending_deletions(self, tmp_path):
        """A v3 database migrates forward to include pending_deletions."""
        db_path = tmp_path / "v3.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version VALUES (3)")
        # Minimal tables so the open path doesn't blow up on something unrelated
        conn.execute("""
            CREATE TABLE threads (
                thread_id TEXT PRIMARY KEY, subject TEXT, participants TEXT,
                folder TEXT, date_first TEXT, date_last TEXT, message_ids TEXT,
                snippet TEXT, has_attachments INTEGER, body_text TEXT,
                fts_rowid INTEGER
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE threads_fts
            USING fts5(
                subject, participants, body,
                content='', contentless_delete=1, tokenize='porter unicode61'
            )
        """)
        conn.commit()
        conn.close()

        db = Database(db_path)
        tables = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "pending_deletions" in tables
        row = db._conn.execute("SELECT version FROM schema_version").fetchone()
        assert row["version"] == SCHEMA_VERSION

    def test_pending_deletions_table_columns(self, db):
        cols = {
            row[1] for row in db._conn.execute("PRAGMA table_info(pending_deletions)").fetchall()
        }
        assert cols == {"filepath", "message_id", "thread_id", "marked_at"}

    def test_migration_is_idempotent(self, tmp_path):
        """Opening an already-migrated database does not error."""
        db_path = tmp_path / "idem.db"
        Database(db_path)
        Database(db_path)  # second open must not raise


# ---------------------------------------------------------------------------
# upsert_thread — insert
# ---------------------------------------------------------------------------


class TestBuildMergedBody:
    def test_insert_returns_text_for_embedding(self, db):
        """For a thread not yet in the DB, build_merged_body reflects the
        thread's own text_for_embedding() — i.e. the full message set the
        caller assembled."""
        thread = make_thread()
        body = db.build_merged_body(thread)
        assert "Subject:" in body
        assert thread.subject in body

    def test_update_appends_new_message_to_stored_body(self, db, threader):
        """On update, build_merged_body returns the stored body with the
        new message appended. This is what the embedder should see so the
        vector represents the whole thread, not just the new message."""
        import src.database as db_mod

        original = make_message(
            message_id="bm_orig@example.com",
            body_text="Original message content marker.",
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, [0.0] * db_mod.EMBEDDING_DIM)

        reply = make_message(
            message_id="bm_reply@example.com",
            body_text="Reply content marker.",
            in_reply_to="bm_orig@example.com",
            filepath="/bm/reply",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t2 = threader.assign_thread(reply)
        body = db.build_merged_body(t2)

        assert "Original message content marker." in body
        assert "Reply content marker." in body

    def test_upsert_with_explicit_body_stores_that_body(self, db):
        """Passing ``body=`` overrides the recomputed merge — critical so
        the stored body matches what the caller embedded."""
        thread = make_thread()
        db.upsert_thread(thread, FAKE_EMBEDDING, body="CUSTOM BODY MARKER")

        row = db._conn.execute(
            "SELECT body_text FROM threads WHERE thread_id = ?", (thread.thread_id,)
        ).fetchone()
        assert row["body_text"] == "CUSTOM BODY MARKER"


class TestEmbeddingDimGuard:
    def test_upsert_rejects_wrong_dimension(self, db):
        """Passing an embedding whose length does not match EMBEDDING_DIM
        fails fast — switching OLLAMA_EMBED_MODEL to a non-768 model would
        otherwise surface as a cryptic sqlite-vec error on insert."""
        thread = make_thread()
        with pytest.raises(ValueError, match="dims"):
            db.upsert_thread(thread, [0.1] * 512)  # wrong dim
        with pytest.raises(ValueError, match="dims"):
            db.upsert_thread(thread, [0.1] * 1024)  # wrong dim


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

    def test_update_preserves_prior_message_ids(self, db, threader):
        """Regression: the second message arriving in an existing thread must
        not drop the first message's ID from threads.message_ids. Prior bug
        serialized only ``thread.messages`` (which held the newest message
        alone) into the UPSERT, clobbering the accumulated list."""
        original = make_message(message_id="mid_orig@example.com", filepath="/m/1")
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, FAKE_EMBEDDING)

        reply = make_message(
            message_id="mid_reply@example.com",
            in_reply_to="mid_orig@example.com",
            filepath="/m/2",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t2 = threader.assign_thread(reply)
        db.upsert_thread(t2, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT message_ids FROM threads WHERE thread_id = 'mid_orig@example.com'"
        ).fetchone()
        stored_ids = json.loads(row["message_ids"])
        assert stored_ids == ["mid_orig@example.com", "mid_reply@example.com"]

    def test_update_preserves_prior_participants(self, db, threader):
        """Regression: a reply from a new participant must not clobber
        previously-recorded participants."""
        original = make_message(
            message_id="pp_orig@example.com",
            from_addr="alice@example.com",
            to_addrs=["bob@example.com"],
            filepath="/pp/1",
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, FAKE_EMBEDDING)

        reply = make_message(
            message_id="pp_reply@example.com",
            from_addr="carol@example.com",
            to_addrs=["alice@example.com"],
            in_reply_to="pp_orig@example.com",
            filepath="/pp/2",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t2 = threader.assign_thread(reply)
        db.upsert_thread(t2, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT participants FROM threads WHERE thread_id = 'pp_orig@example.com'"
        ).fetchone()
        participants = json.loads(row["participants"])
        assert "alice@example.com" in participants
        assert "bob@example.com" in participants
        assert "carol@example.com" in participants

    def test_update_preserves_has_attachments_flag(self, db, threader):
        """Regression: a plain reply following an original message with
        attachments must not reset has_attachments to 0."""
        original = make_message(
            message_id="ha_orig@example.com",
            filepath="/ha/1",
            has_attachments=True,
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, FAKE_EMBEDDING)

        reply = make_message(
            message_id="ha_reply@example.com",
            in_reply_to="ha_orig@example.com",
            filepath="/ha/2",
            date=datetime(2024, 1, 2, tzinfo=UTC),
            has_attachments=False,
        )
        t2 = threader.assign_thread(reply)
        db.upsert_thread(t2, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT has_attachments FROM threads WHERE thread_id = 'ha_orig@example.com'"
        ).fetchone()
        assert row["has_attachments"] == 1

    def test_update_lowers_date_first_for_out_of_order_older_message(self, db, threader):
        """Regression: a late-arriving older message must lower date_first
        rather than leaving it at the originally-indexed newer date."""
        newer = make_message(
            message_id="ooo_db_newer@example.com",
            filepath="/ooo/1",
            date=datetime(2024, 6, 1, tzinfo=UTC),
        )
        t1 = threader.assign_thread(newer)
        db.upsert_thread(t1, FAKE_EMBEDDING)

        older = make_message(
            message_id="ooo_db_older@example.com",
            in_reply_to="ooo_db_newer@example.com",
            filepath="/ooo/2",
            date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        t2 = threader.assign_thread(older)
        db.upsert_thread(t2, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT date_first, date_last FROM threads WHERE thread_id = 'ooo_db_newer@example.com'"
        ).fetchone()
        assert row["date_first"].startswith("2024-01-01")
        assert row["date_last"].startswith("2024-06-01")

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


# ---------------------------------------------------------------------------
# Pending deletions — tombstone CRUD
# ---------------------------------------------------------------------------


class TestPendingDeletions:
    def test_add_pending_deletion_returns_true_on_first_insert(self, db):
        inserted = db.add_pending_deletion("/p", "msg@x", "t1")
        assert inserted is True

    def test_add_pending_deletion_is_idempotent(self, db):
        db.add_pending_deletion("/p", "msg@x", "t1")
        # Second call must not update marked_at nor report an insert
        assert db.add_pending_deletion("/p", "msg@x", "t1") is False
        assert db.count_pending_deletions() == 1

    def test_clear_pending_deletion(self, db):
        db.add_pending_deletion("/p", "msg@x", "t1")
        db.clear_pending_deletion("/p")
        assert db.has_pending_deletion("/p") is False

    def test_has_pending_deletion(self, db):
        assert db.has_pending_deletion("/p") is False
        db.add_pending_deletion("/p", "msg@x", "t1")
        assert db.has_pending_deletion("/p") is True

    def test_list_pending_deletions_older_than_filters(self, db):
        db.add_pending_deletion("/old", "msg1", "t1")
        # Walk the marked_at back manually to simulate an aged tombstone
        db._conn.execute(
            "UPDATE pending_deletions SET marked_at = '2000-01-01T00:00:00+00:00' "
            "WHERE filepath = '/old'"
        )
        db._conn.commit()
        db.add_pending_deletion("/new", "msg2", "t1")

        old = db.list_pending_deletions_older_than("2024-01-01T00:00:00+00:00")
        assert len(old) == 1
        assert old[0]["filepath"] == "/old"


# ---------------------------------------------------------------------------
# Reconciliation support — lookups, filepath updates, message/thread removal
# ---------------------------------------------------------------------------


class TestReconciliationSupport:
    def test_find_message_entry_by_filepath(self, db):
        msg = make_message(filepath="/maildir/INBOX/cur/find_me")
        db.upsert_thread(make_thread(messages=[msg]), FAKE_EMBEDDING)
        row = db.find_message_entry_by_filepath("/maildir/INBOX/cur/find_me")
        assert row is not None
        assert row["message_id"] == msg.message_id

    def test_find_message_entry_by_filepath_miss(self, db):
        assert db.find_message_entry_by_filepath("/nope") is None

    def test_count_total_messages(self, db):
        assert db.count_total_messages() == 0
        m1 = make_message(message_id="c1@x", filepath="/m/1")
        m2 = make_message(message_id="c2@x", filepath="/m/2")
        db.upsert_thread(make_thread(messages=[m1], thread_id="t1"), FAKE_EMBEDDING)
        db.upsert_thread(
            make_thread(messages=[m2], thread_id="t2", subject="other"), FAKE_EMBEDDING
        )
        assert db.count_total_messages() == 2

    def test_update_filepath_moves_map_indexed_and_tombstone_rows(self, db):
        msg = make_message(filepath="/old/path")
        db.upsert_thread(make_thread(messages=[msg]), FAKE_EMBEDDING)
        db.add_pending_deletion("/old/path", msg.message_id, make_thread().thread_id)

        db.update_filepath("/old/path", "/new/path")

        assert db.find_message_entry_by_filepath("/new/path") is not None
        assert db.find_message_entry_by_filepath("/old/path") is None
        assert db.is_indexed("/new/path") is True
        assert db.is_indexed("/old/path") is False
        assert db.has_pending_deletion("/new/path") is True
        assert db.has_pending_deletion("/old/path") is False

    def test_update_filepath_noop_when_paths_equal(self, db):
        msg = make_message(filepath="/same")
        db.upsert_thread(make_thread(messages=[msg]), FAKE_EMBEDDING)
        db.update_filepath("/same", "/same")  # must not raise
        assert db.find_message_entry_by_filepath("/same") is not None

    def test_remove_message_removes_map_indexed_and_tombstone(self, db):
        msg1 = make_message(message_id="keep@x", filepath="/keep")
        msg2 = make_message(message_id="drop@x", filepath="/drop")
        thread = make_thread(messages=[msg1, msg2])
        db.upsert_thread(thread, FAKE_EMBEDDING)
        db.add_pending_deletion("/drop", "drop@x", thread.thread_id)

        db.remove_message("drop@x")

        assert db.find_message_entry_by_filepath("/drop") is None
        assert db.is_indexed("/drop") is False
        assert db.has_pending_deletion("/drop") is False
        # Other message and parent thread row stay intact
        assert db.find_message_entry_by_filepath("/keep") is not None
        assert db.get_thread(thread.thread_id) is not None

    def test_remove_message_silently_returns_for_unknown_id(self, db):
        db.remove_message("ghost@x")  # must not raise

    def test_delete_thread_completely_removes_all_dependent_rows(self, db):
        msg1 = make_message(message_id="d1@x", filepath="/d/1")
        msg2 = make_message(message_id="d2@x", filepath="/d/2")
        thread = make_thread(messages=[msg1, msg2], thread_id="doomed", subject="doomedsubject")
        db.upsert_thread(thread, FAKE_EMBEDDING)
        db.add_pending_deletion("/d/1", "d1@x", "doomed")

        db.delete_thread_completely("doomed")

        assert db.get_thread("doomed") is None
        assert db.find_message_entry_by_filepath("/d/1") is None
        assert db.find_message_entry_by_filepath("/d/2") is None
        assert db.is_indexed("/d/1") is False
        assert db.is_indexed("/d/2") is False
        assert db.has_pending_deletion("/d/1") is False
        # threads_fts uses contentless_delete=1 with rowid-keyed deletes; a
        # MATCH against the old subject must return zero hits after the
        # thread is reaped.
        fts_hits = db._conn.execute(
            "SELECT COUNT(*) FROM threads_fts WHERE threads_fts MATCH 'doomedsubject'"
        ).fetchone()[0]
        vec = db._conn.execute(
            "SELECT COUNT(*) FROM threads_vec WHERE thread_id = 'doomed'"
        ).fetchone()[0]
        assert fts_hits == 0
        assert vec == 0


# ---------------------------------------------------------------------------
# rebuild_thread — full rewrite without body accumulation
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_writes_do_not_corrupt_or_error(self, db):
        """Two threads hammering ``upsert_thread`` must not interleave
        ``BEGIN IMMEDIATE``/``COMMIT`` pairs on the shared connection.
        Without the per-instance lock, cross-thread interleaving can
        raise ``sqlite3.OperationalError`` ("cannot start a transaction
        within a transaction") or silently commit partial state.
        """
        errors: list[Exception] = []
        FAKE_EMB = [0.0] * 768

        def writer(prefix: str):
            try:
                for i in range(50):
                    msg = make_message(
                        message_id=f"{prefix}_{i}@x",
                        filepath=f"/c/{prefix}/{i}",
                        date=datetime(2024, 1, 1, tzinfo=UTC),
                    )
                    thread = make_thread(messages=[msg], thread_id=f"t_{prefix}_{i}")
                    db.upsert_thread(thread, FAKE_EMB)
            except Exception as exc:
                errors.append(exc)

        t_a = threading.Thread(target=writer, args=("a",))
        t_b = threading.Thread(target=writer, args=("b",))
        t_a.start()
        t_b.start()
        t_a.join()
        t_b.join()

        assert errors == []
        # Both threads' threads all landed in the DB.
        assert db.count_total_messages() == 100


class TestRebuildThread:
    def test_rebuild_replaces_body_text_instead_of_appending(self, db, threader):
        original = make_message(message_id="r1@x", body_text="First message body.", filepath="/r/1")
        reply = make_message(
            message_id="r2@x",
            body_text="Second message body.",
            in_reply_to="r1@x",
            filepath="/r/2",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, FAKE_EMBEDDING)
        t2 = threader.assign_thread(reply)
        db.upsert_thread(t2, FAKE_EMBEDDING)

        # Simulate reaping the original — rebuild from reply only
        from src.threader import Thread

        rebuilt = Thread(
            thread_id=t1.thread_id,
            subject="hello world",
            participants=[reply.from_addr] + reply.to_addrs,
            messages=[reply],
            folder="INBOX",
            date_first=reply.date,
            date_last=reply.date,
        )
        db.rebuild_thread(rebuilt, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT body_text FROM threads WHERE thread_id = ?", (t1.thread_id,)
        ).fetchone()
        # The original's body must no longer be present — rebuild is a
        # full replacement, not an append.
        assert "First message body." not in row["body_text"]
        assert "Second message body." in row["body_text"]

    def test_rebuild_updates_fts_and_vec_rows(self, db, threader):
        original = make_message(message_id="r3@x", filepath="/r/3")
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, FAKE_EMBEDDING)

        # Rebuild with a different subject
        from src.threader import Thread

        rebuilt = Thread(
            thread_id=t1.thread_id,
            subject="brand new subject",
            participants=["only@x"],
            messages=[original],
            folder="INBOX",
            date_first=original.date,
            date_last=original.date,
        )
        db.rebuild_thread(rebuilt, FAKE_EMBEDDING)

        # Primary thread row reflects the new subject
        thread_row = db._conn.execute(
            "SELECT subject FROM threads WHERE thread_id = ?", (t1.thread_id,)
        ).fetchone()
        assert thread_row["subject"] == "brand new subject"

        # FTS index is searchable for the new subject and not the old one
        new_hits = db._conn.execute(
            "SELECT rowid FROM threads_fts WHERE threads_fts MATCH 'brand'"
        ).fetchall()
        old_hits = db._conn.execute(
            "SELECT rowid FROM threads_fts WHERE threads_fts MATCH 'hello'"
        ).fetchall()
        assert len(new_hits) == 1
        assert len(old_hits) == 0

        vec_count = db._conn.execute(
            "SELECT COUNT(*) FROM threads_vec WHERE thread_id = ?", (t1.thread_id,)
        ).fetchone()[0]
        assert vec_count == 1
