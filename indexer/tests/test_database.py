"""
Tests for src/database.py.

Covers: schema creation, ``upsert_thread`` (insert and body-accumulation
update), threading lookups, file tracking, chunk + attachment writes,
and stats.
"""

import json
import sqlite3
import threading
from datetime import UTC, datetime

import pytest
from src.attachment_indexing import attachment_occurrence_id
from src.database import SCHEMA_VERSION, Database

from tests.conftest import make_message, make_thread

FAKE_EMBEDDING = [0.1] * 768


# ---------------------------------------------------------------------------
# Schema setup
# ---------------------------------------------------------------------------


class TestSchema:
    def test_database_created_at_given_path(self, tmp_path):
        db_path = tmp_path / "mail.db"
        database = Database(db_path)
        database.close()
        assert db_path.exists()

    def test_raises_clear_error_when_sqlite_too_old(self, tmp_path, monkeypatch):
        """The schema uses FTS5 ``contentless_delete=1`` (SQLite >= 3.43).
        If the runtime is older we fail loudly at Database init with an
        actionable message, instead of silently degrading."""
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

    def test_foreign_key_enforcement_enabled(self, db):
        assert db._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_pending_deletions_table_columns(self, db):
        cols = {
            row[1] for row in db._conn.execute("PRAGMA table_info(pending_deletions)").fetchall()
        }
        assert cols == {"filepath", "message_id", "thread_id", "marked_at"}

    def test_reopening_initialized_database_does_not_error(self, tmp_path):
        """A fresh database created on first open is reopened cleanly on
        the second call: ``_migrate`` finds the matching SCHEMA_VERSION
        row and exits without touching the schema."""
        db_path = tmp_path / "idem.db"
        first = Database(db_path)
        first.close()
        second = Database(db_path)  # second open must not raise
        second.close()

    def test_opening_with_stale_version_runs_migration_to_current(self, tmp_path):
        """An existing volume one version behind ``SCHEMA_VERSION``
        triggers the migration runner, which applies the matching
        migration file and stamps the current version. The schema
        change must take effect (here: ``threads.display_subject``
        column exists after upgrade)."""
        db_path = tmp_path / "stale.db"
        database = Database(db_path)
        database.close()
        import sqlite3

        # Drop the v13 column and stamp v12 to simulate an existing
        # install that pre-dates the v13 migration. ``ALTER TABLE
        # DROP COLUMN`` exists in SQLite >= 3.35; the indexer's runtime
        # already requires SQLite >= 3.43.
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("ALTER TABLE threads DROP COLUMN display_subject")
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION - 1,))
            conn.commit()
        finally:
            conn.close()

        # Reopening should run the v13 migration silently.
        database = Database(db_path)
        try:
            cur = database._conn.execute("PRAGMA table_info(threads)")
            columns = {row["name"] for row in cur.fetchall()}
            assert "display_subject" in columns
            stored = database._conn.execute("SELECT version FROM schema_version").fetchone()[
                "version"
            ]
            assert stored == SCHEMA_VERSION
        finally:
            database.close()

    def test_opening_with_unreachable_lower_version_raises(self, tmp_path):
        """If the stored version is older than the oldest forward
        migration shipped, the runner raises rather than silently
        skipping unmodelled steps."""
        db_path = tmp_path / "ancient.db"
        database = Database(db_path)
        database.close()
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        try:
            # v1 is older than any migration file we ship — we currently
            # ship v13 only; v2..v12 have no migration files because
            # ``_apply_initial_schema`` covers them on fresh installs.
            conn.execute("UPDATE schema_version SET version = ?", (1,))
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(RuntimeError, match="migration sequence"):
            Database(db_path)

    def test_opening_with_higher_stored_version_raises_downgrade_error(self, tmp_path):
        """A stored version above ``SCHEMA_VERSION`` is a downgrade
        attempt — the runner does not support reverse migrations. The
        error must point at the two viable paths (image upgrade or
        volume wipe)."""
        db_path = tmp_path / "future.db"
        database = Database(db_path)
        database.close()
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 1,))
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(RuntimeError, match="Downgrade migrations are not supported"):
            Database(db_path)


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

    def test_writes_display_subject_from_oldest_message(self, db):
        """``display_subject`` is the oldest incoming message's
        original (case-preserving) subject so the human-facing label
        is the cleanest available — not whatever ``Re:`` chain arrives
        last."""
        old = make_message(
            message_id="msg-old@example.com",
            subject="Today's Meeting",
            date=datetime(2024, 1, 1, 9, 0, tzinfo=UTC),
        )
        reply = make_message(
            message_id="msg-new@example.com",
            subject="Re: Today's Meeting",
            date=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        )
        # Pass them in reverse-chronological order to confirm that the
        # MIN(date) selection — not list order — drives the choice.
        thread = make_thread(messages=[reply, old], subject="today's meeting")
        db.upsert_thread(thread, FAKE_EMBEDDING)
        row = db._conn.execute(
            "SELECT subject, display_subject FROM threads WHERE thread_id = ?",
            (thread.thread_id,),
        ).fetchone()
        assert row["subject"] == "today's meeting"  # normalized matching key untouched
        assert row["display_subject"] == "Today's Meeting"

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
    def test_display_subject_preserves_first_writer_through_replies(self, db, threader):
        """COALESCE on update means the original cleaner subject sticks
        even after a ``Re: …`` reply is upserted into the same thread.
        Without this, every reply would clobber the display label and
        the user would see the most recent ``Re:`` chain in the UI."""
        original = make_message(
            message_id="display@example.com",
            subject="Today's Meeting",
            date=datetime(2024, 1, 1, 9, 0, tzinfo=UTC),
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, FAKE_EMBEDDING)

        reply = make_message(
            message_id="display-reply@example.com",
            subject="Re: Today's Meeting",
            in_reply_to="display@example.com",
            filepath="/maildir/INBOX/cur/display-reply",
            date=datetime(2024, 1, 1, 14, 0, tzinfo=UTC),
        )
        t2 = threader.assign_thread(reply)
        db.upsert_thread(t2, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT display_subject FROM threads WHERE thread_id = 'display@example.com'"
        ).fetchone()
        assert row["display_subject"] == "Today's Meeting"

    def test_display_subject_backfills_when_legacy_row_was_null(self, db):
        """A pre-v13 row has ``display_subject = NULL``. The next upsert
        that supplies a non-NULL value backfills it (COALESCE picks the
        non-NULL side). After that, normal first-writer-wins applies."""
        thread = make_thread()
        db.upsert_thread(thread, FAKE_EMBEDDING)
        # Simulate the legacy state by clearing display_subject.
        db._conn.execute(
            "UPDATE threads SET display_subject = NULL WHERE thread_id = ?",
            (thread.thread_id,),
        )
        db._conn.commit()

        # Re-upsert the same thread; COALESCE should fill the column.
        db.upsert_thread(thread, FAKE_EMBEDDING)
        row = db._conn.execute(
            "SELECT display_subject FROM threads WHERE thread_id = ?",
            (thread.thread_id,),
        ).fetchone()
        assert row["display_subject"] == thread.messages[0].subject

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

    def test_update_preserves_snippet_when_older_message_arrives_late(self, db, threader):
        """Regression: snippet used to be derived only from ``thread.messages``,
        which for an update only holds the newly-arrived message. An older
        out-of-order message would therefore replace a snippet that still
        represented the actual newest message in the thread — while
        ``date_last`` was correctly preserved via the max() merge. The
        snippet should follow the same rule and track the latest message."""
        newer = make_message(
            message_id="snip_newer@example.com",
            body_text="Newer message preview text.",
            filepath="/snip/1",
            date=datetime(2024, 6, 1, tzinfo=UTC),
        )
        t1 = threader.assign_thread(newer)
        db.upsert_thread(t1, FAKE_EMBEDDING)

        older = make_message(
            message_id="snip_older@example.com",
            in_reply_to="snip_newer@example.com",
            body_text="Older message preview text.",
            filepath="/snip/2",
            date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        t2 = threader.assign_thread(older)
        db.upsert_thread(t2, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT snippet, date_last FROM threads WHERE thread_id = 'snip_newer@example.com'"
        ).fetchone()
        assert "Newer message" in row["snippet"]
        assert "Older message" not in row["snippet"]
        assert row["date_last"].startswith("2024-06-01")

    def test_update_refreshes_snippet_when_newer_message_arrives(self, db, threader):
        """Companion to the out-of-order test: when the incoming message
        extends ``date_last``, the snippet must follow — the stored preview
        should reflect the most recent message, not freeze at the first one."""
        first = make_message(
            message_id="snip_first@example.com",
            body_text="First message preview text.",
            filepath="/snip_fwd/1",
            date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        t1 = threader.assign_thread(first)
        db.upsert_thread(t1, FAKE_EMBEDDING)

        second = make_message(
            message_id="snip_second@example.com",
            in_reply_to="snip_first@example.com",
            body_text="Second message preview text.",
            filepath="/snip_fwd/2",
            date=datetime(2024, 6, 1, tzinfo=UTC),
        )
        t2 = threader.assign_thread(second)
        db.upsert_thread(t2, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT snippet FROM threads WHERE thread_id = 'snip_first@example.com'"
        ).fetchone()
        assert "Second message" in row["snippet"]

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

    def test_find_threads_by_subject_hit(self, db):
        thread = make_thread(subject="budget discussion")
        db.upsert_thread(thread, FAKE_EMBEDDING)
        result = db.find_threads_by_subject("budget discussion", "INBOX")
        assert result == [thread.thread_id]

    def test_find_threads_by_subject_miss_wrong_folder(self, db):
        thread = make_thread(subject="budget discussion", folder="INBOX")
        db.upsert_thread(thread, FAKE_EMBEDDING)
        result = db.find_threads_by_subject("budget discussion", "Sent")
        assert result == []

    def test_find_threads_by_subject_miss_unknown_subject(self, db):
        assert db.find_threads_by_subject("unknown subject", "INBOX") == []

    def test_find_threads_by_subject_returns_multiple_newest_first(self, db):
        """Regression: threader now iterates candidates until one passes the
        participant/date gate, so multiple same-subject threads in the same
        folder must all surface (newest first)."""
        from datetime import UTC, datetime

        older_msg = make_message(
            message_id="old@example.com",
            subject="invoice",
            date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        newer_msg = make_message(
            message_id="new@example.com",
            subject="invoice",
            date=datetime(2024, 6, 1, tzinfo=UTC),
        )
        older = make_thread(messages=[older_msg], subject="invoice")
        newer = make_thread(messages=[newer_msg], subject="invoice")
        db.upsert_thread(older, FAKE_EMBEDDING)
        db.upsert_thread(newer, FAKE_EMBEDDING)
        result = db.find_threads_by_subject("invoice", "INBOX")
        assert result == [newer.thread_id, older.thread_id]

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
# File identity on indexed_files (schema v7)
# ---------------------------------------------------------------------------


class TestIndexedFileIdentity:
    def test_upsert_writes_size_mtime_content_hash(self, db):
        """A fully-populated Message (as ``parse_email`` produces) lands
        its file-identity fields in ``indexed_files`` alongside the
        indexed_at timestamp."""
        msg = make_message(filepath="/maildir/INBOX/cur/ident")
        msg.size = 4096
        msg.mtime_ns = 1_700_000_000_000_000_000
        msg.content_hash = "a" * 64
        db.upsert_thread(make_thread(messages=[msg]), FAKE_EMBEDDING)
        row = db._conn.execute(
            "SELECT size, mtime_ns, content_hash FROM indexed_files WHERE filepath = ?",
            ("/maildir/INBOX/cur/ident",),
        ).fetchone()
        assert row["size"] == 4096
        assert row["mtime_ns"] == 1_700_000_000_000_000_000
        assert row["content_hash"] == "a" * 64

    def test_upsert_accepts_null_identity_for_test_fixtures(self, db):
        """Messages built by test fixtures that do not go through
        ``parse_email`` have ``size`` / ``mtime_ns`` / ``content_hash``
        as ``None``. ``upsert_thread`` writes them as SQL NULL without
        raising so test code can keep using the lightweight
        ``make_message`` factory without populating those fields."""
        msg = make_message(filepath="/maildir/INBOX/cur/no_ident")
        # Defaults: size=None, mtime_ns=None, content_hash=None
        db.upsert_thread(make_thread(messages=[msg]), FAKE_EMBEDDING)
        row = db._conn.execute(
            "SELECT size, mtime_ns, content_hash FROM indexed_files WHERE filepath = ?",
            ("/maildir/INBOX/cur/no_ident",),
        ).fetchone()
        assert row["size"] is None
        assert row["mtime_ns"] is None
        assert row["content_hash"] is None

    def test_update_filepath_preserves_identity_on_rename(self, db):
        """mbsync renames files in place for flag changes (S → SR etc.)
        without touching content. ``update_filepath`` must carry the
        captured identity forward so the index doesn't regress to
        ``NULL`` columns just because a flag bit flipped."""
        msg = make_message(filepath="/maildir/INBOX/cur/renameme:2,S")
        msg.size = 8192
        msg.mtime_ns = 1_800_000_000_000_000_000
        msg.content_hash = "b" * 64
        db.upsert_thread(make_thread(messages=[msg]), FAKE_EMBEDDING)

        db.update_filepath(
            "/maildir/INBOX/cur/renameme:2,S",
            "/maildir/INBOX/cur/renameme:2,SR",
        )

        row = db._conn.execute(
            "SELECT size, mtime_ns, content_hash FROM indexed_files WHERE filepath = ?",
            ("/maildir/INBOX/cur/renameme:2,SR",),
        ).fetchone()
        assert row["size"] == 8192
        assert row["mtime_ns"] == 1_800_000_000_000_000_000
        assert row["content_hash"] == "b" * 64

    def test_find_indexed_paths_by_content_hash_hit(self, db):
        msg = make_message(filepath="/maildir/INBOX/cur/hashhit")
        msg.size = 1024
        msg.content_hash = "c" * 64
        db.upsert_thread(make_thread(messages=[msg]), FAKE_EMBEDDING)
        paths = db.find_indexed_paths_by_content_hash("c" * 64)
        assert paths == ["/maildir/INBOX/cur/hashhit"]

    def test_find_indexed_paths_by_content_hash_returns_multiple(self, db):
        """Same content at two filepaths (duplicate delivery / same
        message in two folders) returns both entries."""
        msg_a = make_message(
            message_id="dup-a@example.com",
            filepath="/maildir/INBOX/cur/a",
        )
        msg_a.content_hash = "d" * 64
        msg_b = make_message(
            message_id="dup-b@example.com",
            filepath="/maildir/Archive/cur/b",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        msg_b.content_hash = "d" * 64
        db.upsert_thread(make_thread(messages=[msg_a], thread_id="ta"), FAKE_EMBEDDING)
        db.upsert_thread(make_thread(messages=[msg_b], thread_id="tb"), FAKE_EMBEDDING)
        paths = db.find_indexed_paths_by_content_hash("d" * 64)
        assert set(paths) == {
            "/maildir/INBOX/cur/a",
            "/maildir/Archive/cur/b",
        }

    def test_find_indexed_paths_by_content_hash_miss(self, db):
        assert db.find_indexed_paths_by_content_hash("e" * 64) == []

    def test_find_indexed_paths_by_content_hash_ignores_null_rows(self, db):
        """Rows indexed before schema v7 (or when identity capture
        failed) carry ``content_hash IS NULL``. A lookup must never
        treat those as matches for any hash string."""
        msg = make_message(filepath="/maildir/INBOX/cur/nullhash")
        # size=None, content_hash=None by default
        db.upsert_thread(make_thread(messages=[msg]), FAKE_EMBEDDING)
        assert db.find_indexed_paths_by_content_hash("") == []
        assert db.find_indexed_paths_by_content_hash("f" * 64) == []


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


class TestSendersColumn:
    def test_senders_column_exists(self, db):
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(threads)").fetchall()}
        assert "senders" in cols

    def test_upsert_stores_only_from_addresses_in_senders(self, db):
        """Regression: the from_addr filter used to match participants
        (From + To + Cc), so 'from alice' matched threads where alice was
        a recipient. senders now holds only From addresses."""
        msg = make_message(
            message_id="s1@x",
            from_addr="alice@example.com",
            to_addrs=["bob@example.com", "carol@example.com"],
        )
        thread = make_thread(messages=[msg])
        db.upsert_thread(thread, FAKE_EMBEDDING)
        row = db._conn.execute(
            "SELECT senders FROM threads WHERE thread_id = ?", (thread.thread_id,)
        ).fetchone()
        senders = json.loads(row["senders"])
        assert senders == ["alice@example.com"]

    def test_upsert_dedupes_senders_by_canonical_address(self, db, threader):
        """Regression: merge used to key de-dup on the raw display string via
        ``dict.fromkeys``, so ``Bob Smith <bob@x>`` and a later ``bob@x``
        accumulated as two sender entries for the same correspondent. Keying
        on the canonical bare address collapses them — first-seen display
        wins."""
        first = make_message(
            message_id="dm1@x",
            from_addr="Bob Smith <bob@example.com>",
            to_addrs=["alice@example.com"],
        )
        second = make_message(
            message_id="dm2@x",
            from_addr="bob@example.com",
            to_addrs=["alice@example.com"],
            in_reply_to="dm1@x",
            filepath="/dm/2",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t1 = threader.assign_thread(first)
        db.upsert_thread(t1, FAKE_EMBEDDING)
        t2 = threader.assign_thread(second)
        db.upsert_thread(t2, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT senders, participants FROM threads WHERE thread_id = ?",
            (t1.thread_id,),
        ).fetchone()
        senders = json.loads(row["senders"])
        participants = json.loads(row["participants"])
        assert senders == ["Bob Smith <bob@example.com>"]
        assert "Bob Smith <bob@example.com>" in participants
        assert "bob@example.com" not in participants

    def test_upsert_dedupes_senders_case_insensitively(self, db):
        """Case differences in the local or domain part should not create
        duplicate sender entries in an insert-only path either."""
        msg_a = make_message(
            message_id="ci1@x",
            from_addr="Carol@Example.COM",
            to_addrs=["dave@example.com"],
        )
        msg_b = make_message(
            message_id="ci2@x",
            from_addr="carol@example.com",
            to_addrs=["dave@example.com"],
            date=datetime(2024, 1, 2, tzinfo=UTC),
            filepath="/ci/2",
        )
        thread = make_thread(
            messages=[msg_a, msg_b],
            thread_id="ci-thread",
        )
        db.upsert_thread(thread, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT senders FROM threads WHERE thread_id = 'ci-thread'"
        ).fetchone()
        senders = json.loads(row["senders"])
        assert senders == ["Carol@Example.COM"]

    def test_upsert_merges_senders_across_messages(self, db, threader):
        original = make_message(
            message_id="ms1@x",
            from_addr="alice@example.com",
            to_addrs=["bob@example.com"],
        )
        reply = make_message(
            message_id="ms2@x",
            from_addr="bob@example.com",
            to_addrs=["alice@example.com"],
            in_reply_to="ms1@x",
            filepath="/ms/2",
            date=datetime(2024, 1, 2, tzinfo=UTC),
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, FAKE_EMBEDDING)
        t2 = threader.assign_thread(reply)
        db.upsert_thread(t2, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT senders FROM threads WHERE thread_id = ?", (t1.thread_id,)
        ).fetchone()
        senders = json.loads(row["senders"])
        assert set(senders) == {"alice@example.com", "bob@example.com"}


class TestPendingDeletions:
    def test_add_pending_deletion_returns_true_on_first_insert(self, db):
        inserted = db.add_pending_deletion("/p", "msg@x", "t1")
        assert inserted is True

    def test_add_pending_deletion_is_idempotent(self, db):
        db.add_pending_deletion("/p", "msg@x", "t1")
        # Second call must not update marked_at nor report an insert
        assert db.add_pending_deletion("/p", "msg@x", "t1") is False
        assert db.count_pending_deletions() == 1

    def test_add_pending_deletion_writes_iso8601_utc_timestamp(self, db):
        """Regression: ``datetime('now')`` produced a space-separated,
        TZ-less timestamp that sorted lexicographically before the
        reaper's ISO 8601 cutoff, reaping tombstones up to a day early."""
        db.add_pending_deletion("/iso", "iso@x", "t1")
        row = db._conn.execute(
            "SELECT marked_at FROM pending_deletions WHERE filepath = '/iso'"
        ).fetchone()
        marked_at = row["marked_at"]
        assert "T" in marked_at, f"expected 'T' separator, got {marked_at!r}"
        assert marked_at.endswith("+00:00"), f"expected '+00:00' TZ, got {marked_at!r}"

    def test_tombstone_comparison_includes_cutoff_day(self, db):
        """End-to-end: a tombstone marked just now compared against a cutoff
        one second ago must NOT be reaped. Previously the space vs T
        mismatch made it look older than the cutoff."""
        from datetime import UTC, datetime, timedelta

        db.add_pending_deletion("/today", "today@x", "t1")
        cutoff = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        result = db.list_pending_deletions_older_than(cutoff)
        assert not any(r["filepath"] == "/today" for r in result)

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


class TestReapThreadMessages:
    """``reap_thread_messages`` fuses the thread rewrite and per-message
    teardown into a single transaction so a crash mid-reap cannot leave
    ``threads`` and ``message_thread_map`` disagreeing about which
    messages belong to the thread. The prior code used two separate
    transactions (``rebuild_thread`` then N × ``remove_message``)."""

    def _seed_two_message_thread(self, db, threader):
        original = make_message(message_id="r1@x", filepath="/r/1")
        reply = make_message(
            message_id="r2@x",
            in_reply_to="r1@x",
            filepath="/r/2",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, FAKE_EMBEDDING)
        t2 = threader.assign_thread(reply)
        db.upsert_thread(t2, FAKE_EMBEDDING)
        db.add_pending_deletion("/r/2", "r2@x", t1.thread_id)
        return t1, original, reply

    def test_atomic_reap_rewrites_thread_and_removes_reaped_rows(self, db, threader):
        from src.threader import Thread

        t1, original, _ = self._seed_two_message_thread(db, threader)
        rebuilt = Thread(
            thread_id=t1.thread_id,
            subject="hello world",
            participants=[original.from_addr, *original.to_addrs],
            messages=[original],
            folder="INBOX",
            date_first=original.date,
            date_last=original.date,
        )

        removed = db.reap_thread_messages(rebuilt, FAKE_EMBEDDING, ["r2@x"])

        assert removed == ["/r/2"]
        # message_thread_map only has the survivor now
        map_ids = {
            r["message_id"]
            for r in db._conn.execute(
                "SELECT message_id FROM message_thread_map WHERE thread_id = ?",
                (t1.thread_id,),
            ).fetchall()
        }
        assert map_ids == {"r1@x"}
        # indexed_files and pending_deletions for the reaped file are gone
        assert not db.is_indexed("/r/2")
        assert not db.has_pending_deletion("/r/2")

    def test_atomic_reap_rolls_back_when_thread_rewrite_fails(self, db, threader):
        """If the thread rewrite step raises, the per-message removals must
        not have taken effect — the transaction rolls back cleanly."""
        t1, _, _ = self._seed_two_message_thread(db, threader)

        with pytest.raises(ValueError):
            # Wrong-dimension embedding trips the validator in upsert_thread /
            # _rewrite_thread_row, before the remove loop runs.
            db.reap_thread_messages(
                make_thread(thread_id=t1.thread_id),
                [0.0] * 10,  # wrong dim, but rewrite path doesn't check dim
                ["r2@x"],
            )
        # Nothing was removed — tombstone and map entry still intact
        assert db.has_pending_deletion("/r/2")
        assert any(
            r["message_id"] == "r2@x"
            for r in db._conn.execute("SELECT message_id FROM message_thread_map").fetchall()
        )


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


# ---------------------------------------------------------------------------
# Schema v9 — message_chunks tables and the diff-based chunk write
# ---------------------------------------------------------------------------


def _make_chunk(chunk_id: str, index: int = 0, text: str = "chunk text"):
    """Lightweight ``MessageChunk`` factory for chunk-write tests."""
    from src.chunker import MessageChunk

    return MessageChunk(
        chunk_id=chunk_id,
        chunk_index=index,
        text=text,
        char_start=0,
        char_end=len(text),
        token_est=max(1, len(text) // 4),
    )


def _seed_thread_for_message(db, message_id: str, thread_id: str, filepath: str | None = None):
    msg = make_message(
        message_id=message_id,
        filepath=filepath or f"/maildir/INBOX/cur/{message_id}",
    )
    db.upsert_thread(make_thread(messages=[msg], thread_id=thread_id), FAKE_EMBEDDING)


class TestChunkTables:
    def test_chunk_tables_exist(self, db):
        tables = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual')"
            ).fetchall()
        }
        assert "message_chunks" in tables

    def test_chunk_indexes_exist(self, db):
        indexes = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_message_chunks_message" in indexes
        assert "idx_message_chunks_thread" in indexes

    def test_chunk_columns_complete(self, db):
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(message_chunks)").fetchall()}
        for required in (
            "chunk_id",
            "message_id",
            "thread_id",
            "chunk_index",
            "text",
            "char_start",
            "char_end",
            "token_est",
            "chunked_at",
            "fts_rowid",
        ):
            assert required in cols

    def test_chunk_fts_and_vec_virtual_tables_present(self, db):
        # Virtual tables register backing shadow tables; matching by
        # name confirms the CREATE VIRTUAL TABLE ran.
        names = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'message_chunks%'"
            ).fetchall()
        }
        assert "message_chunks_fts" in names
        assert "message_chunks_vec" in names


class TestReplaceMessageChunks:
    def test_chunk_write_requires_parent_message_mapping(self, db):
        chunk = _make_chunk("parent-required".ljust(64, "0"), 0, "orphan")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.replace_message_chunks(
                message_id="missing@x",
                thread_id="missing-thread",
                chunks=[chunk],
                embeddings_by_chunk_id={chunk.chunk_id: [0.1] * 768},
            )

    def test_first_write_inserts_all_chunks_and_indexes(self, db):
        _seed_thread_for_message(db, "m1@x", "t1")
        chunks = [_make_chunk("a" * 64, 0, "first"), _make_chunk("b" * 64, 1, "second")]
        embeds = {chunks[0].chunk_id: [0.1] * 768, chunks[1].chunk_id: [0.2] * 768}

        result = db.replace_message_chunks(
            message_id="m1@x",
            thread_id="t1",
            chunks=chunks,
            embeddings_by_chunk_id=embeds,
        )

        assert result == {"inserted": 2, "deleted": 0, "kept": 0}
        # Each chunk landed in all three indexes.
        assert (
            db._conn.execute(
                "SELECT COUNT(*) FROM message_chunks WHERE message_id = ?", ("m1@x",)
            ).fetchone()[0]
            == 2
        )
        assert (
            db._conn.execute(
                "SELECT COUNT(*) FROM message_chunks_vec WHERE chunk_id IN (?, ?)",
                (chunks[0].chunk_id, chunks[1].chunk_id),
            ).fetchone()[0]
            == 2
        )
        # FTS row was created and recorded back into chunk row.
        rows = db._conn.execute(
            "SELECT fts_rowid FROM message_chunks WHERE message_id = ?", ("m1@x",)
        ).fetchall()
        assert all(r["fts_rowid"] is not None for r in rows)

    def test_replay_with_identical_input_is_idempotent(self, db):
        """Re-running the chunker with the same body must produce no work
        — the diff path must skip already-stored chunk_ids and require no
        embeddings for them.
        """
        _seed_thread_for_message(db, "m2@x", "t2")
        chunks = [_make_chunk("c" * 64, 0, "stable")]
        embeds = {chunks[0].chunk_id: [0.3] * 768}

        first = db.replace_message_chunks(
            message_id="m2@x",
            thread_id="t2",
            chunks=chunks,
            embeddings_by_chunk_id=embeds,
        )
        assert first == {"inserted": 1, "deleted": 0, "kept": 0}

        # Replay with no embeddings — would raise if the diff path tried
        # to insert anything.
        second = db.replace_message_chunks(
            message_id="m2@x",
            thread_id="t2",
            chunks=chunks,
            embeddings_by_chunk_id={},
        )
        assert second == {"inserted": 0, "deleted": 0, "kept": 1}

    def test_diff_write_inserts_new_keeps_existing_drops_gone(self, db):
        _seed_thread_for_message(db, "m3@x", "t3")
        keep = _make_chunk("k" * 64, 0, "keep this")
        drop = _make_chunk("d" * 64, 1, "drop this")
        new = _make_chunk("n" * 64, 1, "new chunk")

        # Round 1: keep + drop.
        db.replace_message_chunks(
            message_id="m3@x",
            thread_id="t3",
            chunks=[keep, drop],
            embeddings_by_chunk_id={
                keep.chunk_id: [0.1] * 768,
                drop.chunk_id: [0.2] * 768,
            },
        )

        # Round 2: keep + new (drop should be deleted; keep should be kept).
        result = db.replace_message_chunks(
            message_id="m3@x",
            thread_id="t3",
            chunks=[keep, new],
            embeddings_by_chunk_id={new.chunk_id: [0.3] * 768},
        )
        assert result == {"inserted": 1, "deleted": 1, "kept": 1}

        stored = {
            row[0]
            for row in db._conn.execute(
                "SELECT chunk_id FROM message_chunks WHERE message_id = ?", ("m3@x",)
            ).fetchall()
        }
        assert stored == {keep.chunk_id, new.chunk_id}
        # vec table tracks the same set.
        vec_count = db._conn.execute(
            "SELECT COUNT(*) FROM message_chunks_vec WHERE chunk_id = ?", (drop.chunk_id,)
        ).fetchone()[0]
        assert vec_count == 0

    def test_missing_embedding_for_new_chunk_raises(self, db):
        chunk = _make_chunk("e" * 64, 0, "needs embed")
        with pytest.raises(ValueError, match="missing embedding"):
            db.replace_message_chunks(
                message_id="m4@x",
                thread_id="t4",
                chunks=[chunk],
                embeddings_by_chunk_id={},
            )

    def test_wrong_dim_embedding_raises(self, db):
        chunk = _make_chunk("f" * 64, 0, "bad dim")
        with pytest.raises(ValueError, match="EMBEDDING_DIM|reserves 768"):
            db.replace_message_chunks(
                message_id="m5@x",
                thread_id="t5",
                chunks=[chunk],
                embeddings_by_chunk_id={chunk.chunk_id: [0.1] * 100},
            )


class TestThreadChunkAggregation:
    def test_get_thread_chunk_embeddings_returns_per_message_vectors(self, db):
        # Two messages in the same thread, each with one chunk.
        _seed_thread_for_message(db, "m6a@x", "t6")
        msg = make_message(message_id="m6b@x", filepath="/maildir/INBOX/cur/m6b@x")
        db.upsert_thread(make_thread(messages=[msg], thread_id="t6"), FAKE_EMBEDDING)
        for mid, vec in [("m6a@x", [0.5] * 768), ("m6b@x", [0.7] * 768)]:
            chunk = _make_chunk(f"x{mid}".ljust(64, "0"), 0, f"body of {mid}")
            db.replace_message_chunks(
                message_id=mid,
                thread_id="t6",
                chunks=[chunk],
                embeddings_by_chunk_id={chunk.chunk_id: vec},
            )

        results = db.get_thread_chunk_embeddings("t6")
        assert len(results) == 2
        # Both vectors round-trip with their original values.
        sums = sorted(round(sum(v) / len(v), 3) for v in results)
        assert sums == [0.5, 0.7]

    def test_get_chunk_embeddings_for_messages_filters_correctly(self, db):
        _seed_thread_for_message(db, "m7a@x", "t7")
        msg = make_message(message_id="m7b@x", filepath="/maildir/INBOX/cur/m7b@x")
        db.upsert_thread(make_thread(messages=[msg], thread_id="t7"), FAKE_EMBEDDING)
        for mid, vec in [("m7a@x", [0.1] * 768), ("m7b@x", [0.9] * 768)]:
            chunk = _make_chunk(f"y{mid}".ljust(64, "0"), 0, f"body of {mid}")
            db.replace_message_chunks(
                message_id=mid,
                thread_id="t7",
                chunks=[chunk],
                embeddings_by_chunk_id={chunk.chunk_id: vec},
            )

        survivors = db.get_chunk_embeddings_for_messages(["m7a@x"])
        assert len(survivors) == 1
        assert round(sum(survivors[0]) / len(survivors[0]), 3) == 0.1

    def test_get_chunk_embeddings_for_messages_empty_input_returns_empty(self, db):
        assert db.get_chunk_embeddings_for_messages([]) == []


class TestAtomicIndexTransaction:
    def test_thread_and_chunk_writes_roll_back_together(self, db):
        msg = make_message(message_id="atomic@x", filepath="/maildir/INBOX/cur/atomic")
        thread = make_thread(messages=[msg], thread_id="atomic-thread")
        chunk = _make_chunk("atomic-chunk".ljust(64, "0"), 0, "atomic body")

        with pytest.raises(RuntimeError, match="force rollback"):
            with db.transaction():
                db.upsert_thread(thread, FAKE_EMBEDDING)
                db.replace_message_chunks(
                    message_id=msg.message_id,
                    thread_id=thread.thread_id,
                    chunks=[chunk],
                    embeddings_by_chunk_id={chunk.chunk_id: [0.2] * 768},
                )
                raise RuntimeError("force rollback")

        assert db.get_thread(thread.thread_id) is None
        assert db.get_chunk_ids_for_message(msg.message_id) == set()
        assert db.find_thread_by_message_id(msg.message_id) is None


class TestChunkCascadeOnMessageRemoval:
    def test_remove_message_drops_its_chunks(self, db, threader):
        message = make_message(message_id="m8@x", filepath="/m/8")
        thread = threader.assign_thread(message)
        db.upsert_thread(thread, FAKE_EMBEDDING)

        chunk = _make_chunk("z" * 64, 0, "to be removed")
        db.replace_message_chunks(
            message_id="m8@x",
            thread_id=thread.thread_id,
            chunks=[chunk],
            embeddings_by_chunk_id={chunk.chunk_id: [0.4] * 768},
        )

        db.remove_message("m8@x")

        assert db.get_chunk_ids_for_message("m8@x") == set()
        vec_count = db._conn.execute(
            "SELECT COUNT(*) FROM message_chunks_vec WHERE chunk_id = ?", (chunk.chunk_id,)
        ).fetchone()[0]
        assert vec_count == 0

    def test_delete_thread_completely_drops_all_thread_chunks(self, db, threader):
        m1 = make_message(message_id="m9a@x", filepath="/m/9a")
        m2 = make_message(message_id="m9b@x", filepath="/m/9b")
        t = threader.assign_thread(m1)
        t.messages.append(m2)
        db.upsert_thread(t, FAKE_EMBEDDING)

        for mid in ("m9a@x", "m9b@x"):
            chunk = _make_chunk(f"q{mid}".ljust(64, "0"), 0, "doomed")
            db.replace_message_chunks(
                message_id=mid,
                thread_id=t.thread_id,
                chunks=[chunk],
                embeddings_by_chunk_id={chunk.chunk_id: [0.5] * 768},
            )

        db.delete_thread_completely(t.thread_id)

        assert db.get_thread_chunk_embeddings(t.thread_id) == []
        assert (
            db._conn.execute(
                "SELECT COUNT(*) FROM message_chunks WHERE thread_id = ?", (t.thread_id,)
            ).fetchone()[0]
            == 0
        )


# ---------------------------------------------------------------------------
# Schema v12 — attachments + attachment_extractions + enforced sidecar parents
# ---------------------------------------------------------------------------


class TestAttachmentTables:
    def test_attachment_tables_exist(self, db):
        names = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'attachment%'"
            ).fetchall()
        }
        assert "attachments" in names
        assert "attachments_fts" in names
        assert "attachment_extractions" in names

    def test_message_chunks_has_attachment_id_column(self, db):
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(message_chunks)").fetchall()}
        assert "attachment_id" in cols

    def test_attachments_has_occurrence_primary_key(self, db):
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(attachments)").fetchall()}
        assert "attachment_occurrence_id" in cols


class TestUpsertAttachment:
    def test_first_upsert_inserts_and_returns_true(self, db, threader):
        msg = make_message(message_id="att1@x", filepath="/m/att1")
        thread = threader.assign_thread(msg)
        db.upsert_thread(thread, FAKE_EMBEDDING)

        inserted = db.upsert_attachment(
            message_id="att1@x",
            thread_id=thread.thread_id,
            attachment_id="hash-a" * 8,
            filename="invoice.pdf",
            content_type="application/pdf",
            size_bytes=1234,
            occurrence_id=attachment_occurrence_id(
                message_id="att1@x",
                content_hash="hash-a" * 8,
                filename="invoice.pdf",
                occurrence_index=0,
            ),
        )
        assert inserted is True

        row = db._conn.execute(
            "SELECT filename, content_type, size_bytes, fts_rowid "
            "FROM attachments WHERE message_id = ? AND attachment_id = ?",
            ("att1@x", "hash-a" * 8),
        ).fetchone()
        assert row["filename"] == "invoice.pdf"
        assert row["content_type"] == "application/pdf"
        assert row["size_bytes"] == 1234
        assert row["fts_rowid"] is not None

    def test_repeat_upsert_returns_false_and_no_extra_fts(self, db, threader):
        msg = make_message(message_id="att2@x", filepath="/m/att2")
        thread = threader.assign_thread(msg)
        db.upsert_thread(thread, FAKE_EMBEDDING)

        kwargs = dict(
            message_id="att2@x",
            thread_id=thread.thread_id,
            attachment_id="hash-b" * 8,
            filename="contract.docx",
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            size_bytes=5678,
            occurrence_id=attachment_occurrence_id(
                message_id="att2@x",
                content_hash="hash-b" * 8,
                filename="contract.docx",
                occurrence_index=0,
            ),
        )
        assert db.upsert_attachment(**kwargs) is True
        assert db.upsert_attachment(**kwargs) is False

        # Exactly one attachments row + one FTS row.
        cnt = db._conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE message_id = ?", ("att2@x",)
        ).fetchone()[0]
        assert cnt == 1

    def test_same_payload_can_have_multiple_filename_occurrences(self, db, threader):
        msg = make_message(message_id="att-dupe@x", filepath="/m/att-dupe")
        thread = threader.assign_thread(msg)
        db.upsert_thread(thread, FAKE_EMBEDDING)

        shared_hash = "hash-dupe" * 8
        assert db.upsert_attachment(
            message_id="att-dupe@x",
            thread_id=thread.thread_id,
            attachment_id=shared_hash,
            filename="invoice-a.pdf",
            content_type="application/pdf",
            size_bytes=10,
            occurrence_id="occ-a",
        )
        assert db.upsert_attachment(
            message_id="att-dupe@x",
            thread_id=thread.thread_id,
            attachment_id=shared_hash,
            filename="invoice-b.pdf",
            content_type="application/pdf",
            size_bytes=10,
            occurrence_id="occ-b",
        )

        rows = db._conn.execute(
            "SELECT filename FROM attachments WHERE message_id = ? ORDER BY filename",
            ("att-dupe@x",),
        ).fetchall()
        assert [r["filename"] for r in rows] == ["invoice-a.pdf", "invoice-b.pdf"]

    def test_attachments_fts_searchable_by_filename(self, db, threader):
        msg = make_message(message_id="att3@x", filepath="/m/att3")
        thread = threader.assign_thread(msg)
        db.upsert_thread(thread, FAKE_EMBEDDING)
        db.upsert_attachment(
            message_id="att3@x",
            thread_id=thread.thread_id,
            attachment_id="hash-c" * 8,
            filename="march-statement.pdf",
            content_type="application/pdf",
            size_bytes=10,
            occurrence_id=attachment_occurrence_id(
                message_id="att3@x",
                content_hash="hash-c" * 8,
                filename="march-statement.pdf",
                occurrence_index=0,
            ),
        )
        hits = db._conn.execute(
            "SELECT rowid FROM attachments_fts WHERE attachments_fts MATCH 'statement'"
        ).fetchall()
        assert len(hits) == 1


class TestAttachmentExtractionCache:
    def test_get_returns_none_when_not_stored(self, db):
        assert db.get_attachment_extraction("nonexistent" * 4) is None

    def test_store_then_get_roundtrips(self, db):
        attachment_id = "hash-d" * 8
        db.store_attachment_extraction(
            attachment_id=attachment_id,
            extraction_status="success",
            extractor="pdf-digital",
            extracted_text="hello world",
            extraction_error=None,
        )
        row = db.get_attachment_extraction(attachment_id)
        assert row is not None
        assert row["extraction_status"] == "success"
        assert row["extractor"] == "pdf-digital"
        assert row["extracted_text"] == "hello world"

    def test_store_replaces_existing_row(self, db):
        attachment_id = "hash-e" * 8
        db.store_attachment_extraction(
            attachment_id=attachment_id,
            extraction_status="empty",
            extractor="pdf-digital",
            extracted_text=None,
            extraction_error=None,
        )
        # Operator enabled OCR → re-extract upgraded the row.
        db.store_attachment_extraction(
            attachment_id=attachment_id,
            extraction_status="success",
            extractor="pdf-ocr",
            extracted_text="now we have text",
            extraction_error=None,
        )
        row = db.get_attachment_extraction(attachment_id)
        assert row["extraction_status"] == "success"
        assert row["extracted_text"] == "now we have text"


class TestAttachmentChunkSlicing:
    """Body chunks and attachment chunks must be diffed independently
    so a write of attachment chunks doesn't delete body chunks for the
    same message — and vice versa.
    """

    def test_body_and_attachment_chunks_coexist(self, db, threader):
        msg = make_message(message_id="slice1@x", filepath="/m/s1")
        thread = threader.assign_thread(msg)
        db.upsert_thread(thread, FAKE_EMBEDDING)

        body_chunk = _make_chunk("body-chunk".ljust(64, "0"), 0, "body")
        att_chunk = _make_chunk("att-chunk".ljust(64, "0"), 0, "attachment")
        attachment_id = "att-hash" * 8

        db.replace_message_chunks(
            message_id="slice1@x",
            thread_id=thread.thread_id,
            chunks=[body_chunk],
            embeddings_by_chunk_id={body_chunk.chunk_id: [0.1] * 768},
        )
        db.replace_message_chunks(
            message_id="slice1@x",
            thread_id=thread.thread_id,
            chunks=[att_chunk],
            embeddings_by_chunk_id={att_chunk.chunk_id: [0.2] * 768},
            attachment_id=attachment_id,
        )

        # Both chunks present.
        all_ids = {
            row[0]
            for row in db._conn.execute(
                "SELECT chunk_id FROM message_chunks WHERE message_id = ?",
                ("slice1@x",),
            ).fetchall()
        }
        assert all_ids == {body_chunk.chunk_id, att_chunk.chunk_id}

        # The slice-aware getter returns only the requested slice.
        assert db.get_chunk_ids_for_message("slice1@x") == {body_chunk.chunk_id}
        assert db.get_chunk_ids_for_message("slice1@x", attachment_id=attachment_id) == {
            att_chunk.chunk_id
        }

    def test_re_writing_body_chunks_does_not_drop_attachment_chunks(self, db, threader):
        msg = make_message(message_id="slice2@x", filepath="/m/s2")
        thread = threader.assign_thread(msg)
        db.upsert_thread(thread, FAKE_EMBEDDING)

        body = _make_chunk("body2".ljust(64, "0"), 0, "body")
        att = _make_chunk("att2".ljust(64, "0"), 0, "attachment")
        att_id = "attID" * 13

        db.replace_message_chunks(
            message_id="slice2@x",
            thread_id=thread.thread_id,
            chunks=[body],
            embeddings_by_chunk_id={body.chunk_id: [0.1] * 768},
        )
        db.replace_message_chunks(
            message_id="slice2@x",
            thread_id=thread.thread_id,
            chunks=[att],
            embeddings_by_chunk_id={att.chunk_id: [0.2] * 768},
            attachment_id=att_id,
        )

        # Re-write body slice with a different chunk — attachment chunk stays.
        body2 = _make_chunk("body2-new".ljust(64, "0"), 0, "new body")
        db.replace_message_chunks(
            message_id="slice2@x",
            thread_id=thread.thread_id,
            chunks=[body2],
            embeddings_by_chunk_id={body2.chunk_id: [0.3] * 768},
        )

        assert db.get_chunk_ids_for_message("slice2@x") == {body2.chunk_id}
        assert db.get_chunk_ids_for_message("slice2@x", attachment_id=att_id) == {att.chunk_id}


class TestAttachmentCascadeOnMessageRemoval:
    def test_remove_message_drops_its_attachment_rows(self, db, threader):
        msg = make_message(message_id="cas1@x", filepath="/m/cas1")
        thread = threader.assign_thread(msg)
        db.upsert_thread(thread, FAKE_EMBEDDING)

        db.upsert_attachment(
            message_id="cas1@x",
            thread_id=thread.thread_id,
            attachment_id="cascade-hash" * 4,
            filename="doomed.pdf",
            content_type="application/pdf",
            size_bytes=1,
            occurrence_id=attachment_occurrence_id(
                message_id="cas1@x",
                content_hash="cascade-hash" * 4,
                filename="doomed.pdf",
                occurrence_index=0,
            ),
        )
        # Make sure it landed.
        before = db._conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE message_id = ?", ("cas1@x",)
        ).fetchone()[0]
        assert before == 1

        db.remove_message("cas1@x")

        after = db._conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE message_id = ?", ("cas1@x",)
        ).fetchone()[0]
        assert after == 0

    def test_extraction_cache_is_preserved_on_message_removal(self, db, threader):
        """Cached extractions outlive their messages so a future
        re-arrival of the same content (forwarded again) skips the
        extract cost."""
        msg = make_message(message_id="cas2@x", filepath="/m/cas2")
        thread = threader.assign_thread(msg)
        db.upsert_thread(thread, FAKE_EMBEDDING)

        attachment_id = "preserve-hash" * 4
        db.upsert_attachment(
            message_id="cas2@x",
            thread_id=thread.thread_id,
            attachment_id=attachment_id,
            filename="preserved.pdf",
            content_type="application/pdf",
            size_bytes=1,
            occurrence_id=attachment_occurrence_id(
                message_id="cas2@x",
                content_hash=attachment_id,
                filename="preserved.pdf",
                occurrence_index=0,
            ),
        )
        db.store_attachment_extraction(
            attachment_id=attachment_id,
            extraction_status="success",
            extractor="pdf-digital",
            extracted_text="cached content",
            extraction_error=None,
        )

        db.remove_message("cas2@x")

        cached = db.get_attachment_extraction(attachment_id)
        assert cached is not None
        assert cached["extracted_text"] == "cached content"

    def test_delete_thread_completely_drops_attachments_for_all_messages(self, db, threader):
        m1 = make_message(message_id="cas3a@x", filepath="/m/cas3a")
        m2 = make_message(message_id="cas3b@x", filepath="/m/cas3b")
        t = threader.assign_thread(m1)
        t.messages.append(m2)
        db.upsert_thread(t, FAKE_EMBEDDING)

        for mid in ("cas3a@x", "cas3b@x"):
            attachment_id = f"thread-cascade-{mid}".ljust(64, "0")
            db.upsert_attachment(
                message_id=mid,
                thread_id=t.thread_id,
                attachment_id=attachment_id,
                filename=f"{mid}.pdf",
                content_type="application/pdf",
                size_bytes=1,
                occurrence_id=attachment_occurrence_id(
                    message_id=mid,
                    content_hash=attachment_id,
                    filename=f"{mid}.pdf",
                    occurrence_index=0,
                ),
            )

        db.delete_thread_completely(t.thread_id)

        cnt = db._conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE thread_id = ?", (t.thread_id,)
        ).fetchone()[0]
        assert cnt == 0
