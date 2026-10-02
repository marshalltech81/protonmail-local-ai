"""
Tests for src/database.py.

Covers: schema creation, ``upsert_thread`` (insert and body-accumulation
update), threading lookups, file tracking, chunk + attachment writes,
and stats.
"""

import inspect
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest
from src.attachment_indexing import attachment_occurrence_id
from src.database import (
    EMBEDDING_DIM,  # noqa: F401  -- via reuse
    SCHEMA_APPLICATION_ID,
    SCHEMA_VERSION,
    Database,
)

from tests.conftest import count_pending_deletions, make_message, make_thread

FAKE_EMBEDDING = [0.1] * EMBEDDING_DIM


# ---------------------------------------------------------------------------
# Schema setup
# ---------------------------------------------------------------------------


def _tombstone_thread(db, thread_id: str) -> None:
    """Tombstone every message in ``thread_id``, as the reconciler does
    before ``delete_thread_completely``, which refuses otherwise."""
    for row in db.get_thread_messages(thread_id):
        db.add_pending_deletion(row["filepath"], row["claimant_id"], thread_id)


def _reap_message(db, thread, message_id: str) -> list[str] | None:
    """Remove ``message_id`` from ``thread`` the way the reconciler does:
    tombstone it, then ``reap_thread_messages`` with a rewrite from the
    surviving messages. ``thread`` must keep at least one survivor."""
    reaped = next(m for m in thread.messages if m.message_id == message_id)
    survivors = [m for m in thread.messages if m.message_id != message_id]
    db.add_pending_deletion(reaped.filepath, message_id, thread.thread_id)
    rebuilt = make_thread(
        messages=survivors,
        thread_id=thread.thread_id,
        subject=thread.subject,
        folder=thread.folder,
    )
    return db.reap_thread_messages(rebuilt, FAKE_EMBEDDING, [message_id])


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
        assert cols == {"filepath", "claimant_id", "thread_id", "marked_at"}

    def test_reopening_initialized_database_does_not_error(self, tmp_path):
        """A fresh database created on first open is reopened cleanly on
        the second call: ``_migrate`` finds the matching SCHEMA_VERSION
        row and exits without touching the schema."""
        db_path = tmp_path / "idem.db"
        first = Database(db_path)
        first.close()
        second = Database(db_path)  # second open must not raise
        second.close()

    def test_fresh_install_is_stamped_version_zero(self, db):
        """The initial schema is version 0; the first migration will be
        ``0001``."""
        assert SCHEMA_VERSION == 0
        assert db._conn.execute("SELECT version FROM schema_version").fetchone()[0] == 0

    def test_database_from_the_old_numbering_fails_with_rebuild_instructions(self, tmp_path):
        """Before the renumber the baseline was v21 and the latest v22.
        Such a database now reads as newer than the code and must fail
        closed with the volume-wipe step."""
        db_path = tmp_path / "v22.db"
        Database(db_path).close()
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("UPDATE schema_version SET version = 22")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(RuntimeError, match="wipe the sqlite-volume"):
            Database(db_path)

    def test_fresh_install_carries_the_application_id(self, db):
        assert db._conn.execute("PRAGMA application_id").fetchone()[0] == SCHEMA_APPLICATION_ID

    def test_old_numbering_database_at_a_reused_version_fails_closed(self, tmp_path):
        """Once the new sequence reaches a number the old one used, the
        version alone cannot tell the two apart. An old database has no
        ``application_id`` stamp and must fail closed, not read as ready."""
        db_path = tmp_path / "old-v0.db"
        Database(db_path).close()
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA application_id = 0")
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(RuntimeError, match="wipe the sqlite-volume"):
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


class TestIngestionState:
    def test_no_row_until_the_indexer_records_one(self, db):
        assert db._conn.execute("SELECT COUNT(*) FROM ingestion_state").fetchone()[0] == 0

    def test_record_keeps_a_single_row_with_the_latest_values(self, db):
        db.record_ingestion_state(
            sync_completed_at="2026-09-28T12:00:00+00:00",
            sync_interval_secs=60,
            seen_at="2026-09-28T12:00:05+00:00",
        )
        db.record_ingestion_state(
            sync_completed_at=None,
            sync_interval_secs=None,
            seen_at="2026-09-28T12:00:35+00:00",
        )
        rows = db._conn.execute(
            "SELECT sync_completed_at, sync_interval_secs, indexer_seen_at FROM ingestion_state"
        ).fetchall()
        assert [tuple(r) for r in rows] == [(None, None, "2026-09-28T12:00:35+00:00")]


class TestEmbeddingDimGuard:
    def test_upsert_rejects_wrong_dimension(self, db):
        """Passing an embedding whose length does not match EMBEDDING_DIM
        fails fast — switching to an embed model with a different output
        dimension would otherwise surface as a cryptic sqlite-vec error
        on insert."""
        thread = make_thread()
        with pytest.raises(ValueError, match="dims"):
            db.upsert_thread(thread, [0.1] * 512)  # wrong dim
        with pytest.raises(ValueError, match="dims"):
            db.upsert_thread(thread, [0.1] * 1024)  # wrong dim


class TestThreadVectorUnitNormInvariant:
    """Every vector written to ``threads_vec`` must be L2-unit-norm so
    the cosine-equals-dot-product assumption holds for downstream
    retrieval. The three thread-vector write boundaries
    (``upsert_thread``, ``replace_thread_vector``, and
    ``_rewrite_thread_row`` via ``reap_thread_messages``) all normalize at the boundary so callers
    that pass a non-unit ``mean_vector(...)`` cannot bypass the
    invariant. Zero placeholders survive normalization because the
    Phase 1 seed logic depends on them as a sentinel."""

    @staticmethod
    def _read_thread_vec(db, thread_id: str) -> list[float]:
        import struct

        from src.database import EMBEDDING_DIM

        row = db._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        assert row is not None, f"no threads_vec row for {thread_id}"
        return list(struct.unpack(f"{EMBEDDING_DIM}f", row["embedding"]))

    @staticmethod
    def _norm(vec: list[float]) -> float:
        return sum(x * x for x in vec) ** 0.5

    def test_upsert_thread_normalizes_non_unit_input(self, db):
        # Magnitude-2 input: every component is 2.0/sqrt(EMBEDDING_DIM),
        # so the vector's L2 norm is 2.0. Without normalization, vec0
        # stores it raw and ``vec_distance_cosine`` no longer collapses
        # to a dot product. After this PR the boundary normalizes.
        from src.database import EMBEDDING_DIM

        thread = make_thread()
        scaled = [2.0 / (EMBEDDING_DIM**0.5)] * EMBEDDING_DIM
        db.upsert_thread(thread, scaled)
        stored = self._read_thread_vec(db, thread.thread_id)
        assert self._norm(stored) == pytest.approx(1.0, abs=1e-6)

    def test_replace_thread_vector_normalizes_non_unit_input(self, db):
        # Phase 2c writes ``mean_vector(chunk_embs)`` here; mean of unit
        # vectors generally has norm < 1. Use a 3-4-5 style scaled
        # vector to make the normalization observable.
        from src.database import EMBEDDING_DIM

        thread = make_thread()
        db.upsert_thread(thread, [0.0] * EMBEDDING_DIM)  # placeholder seed
        scaled = [0.5] * EMBEDDING_DIM  # norm = 0.5 * sqrt(EMBEDDING_DIM) ≠ 1
        db.replace_thread_vector(thread.thread_id, scaled)
        stored = self._read_thread_vec(db, thread.thread_id)
        assert self._norm(stored) == pytest.approx(1.0, abs=1e-6)

    def test_reap_thread_messages_normalizes_non_unit_input(self, db):
        # The reconciler reap path passes ``mean_vector(survivor_chunks)``
        # through ``_rewrite_thread_row``. Verify the same normalize
        # boundary fires on that code path so reap-then-search returns
        # comparable cosine scores against still-live threads.
        from src.database import EMBEDDING_DIM

        msg = make_message(message_id="m1@x")
        thread = make_thread(messages=[msg], thread_id="t-reap")
        db.upsert_thread(thread, [0.0] * EMBEDDING_DIM)
        scaled = [3.0 / (EMBEDDING_DIM**0.5)] * EMBEDDING_DIM  # norm 3.0
        db.reap_thread_messages(thread, scaled, reaped_claimant_ids=[])
        stored = self._read_thread_vec(db, "t-reap")
        assert self._norm(stored) == pytest.approx(1.0, abs=1e-6)

    def test_zero_placeholder_seed_is_preserved(self, db):
        # Phase 1 writes an all-zero seed for genuinely-new threads;
        # ``l2_normalize`` deliberately preserves zero vectors so the
        # three-case priority chain in ``main._batched_phase1`` keeps
        # working. Normalizing to NaN here would corrupt every brand-
        # new thread.
        from src.database import EMBEDDING_DIM

        thread = make_thread()
        db.upsert_thread(thread, [0.0] * EMBEDDING_DIM)
        stored = self._read_thread_vec(db, thread.thread_id)
        assert all(v == 0.0 for v in stored)


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
        """The cleaner original subject sticks even after a ``Re: …``
        reply is upserted into the same thread, AS LONG AS the reply
        arrives second. Without this, every reply would clobber the
        display label and the user would see the most recent ``Re:``
        chain in the UI."""
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

    def test_display_subject_replaced_when_older_root_arrives_after_reply(self, db, threader):
        """Out-of-order indexing: the reply arrives first and stamps
        ``display_subject = "Re: Today's Meeting"``. When the older
        root message is later indexed, its earlier date pushes
        ``date_first`` back and its cleaner subject must replace the
        ``Re:``-prefixed label. A naive COALESCE-on-upsert would trap
        the reply subject as the display label permanently and the UI
        would show the wrong thread title forever."""
        reply = make_message(
            message_id="reply-first@example.com",
            subject="Re: Today's Meeting",
            date=datetime(2024, 1, 1, 14, 0, tzinfo=UTC),
        )
        # Reply arrives first — at this point the indexer has no idea
        # there is an older root.
        t1 = threader.assign_thread(reply)
        db.upsert_thread(t1, FAKE_EMBEDDING)
        row = db._conn.execute(
            "SELECT display_subject FROM threads WHERE thread_id = ?",
            (t1.thread_id,),
        ).fetchone()
        assert row["display_subject"] == "Re: Today's Meeting"

        # Now the original root message is discovered (older date,
        # cleaner subject). It should join the same thread and replace
        # the display_subject.
        root = make_message(
            message_id="root-second@example.com",
            subject="Today's Meeting",
            date=datetime(2024, 1, 1, 9, 0, tzinfo=UTC),
        )
        # Stitch the root into the same thread by sharing a Message-ID
        # the reply references. We can simulate this by upserting a
        # Thread that carries the older message as a "new arrival" but
        # shares the existing thread_id (the threader produces this
        # shape when it discovers a thread root post-hoc).
        from src.threader import Thread

        t2 = Thread(
            thread_id=t1.thread_id,
            subject=t1.subject,
            participants=[root.from_addr] + root.to_addrs,
            messages=[root],
            folder=root.folder,
            date_first=root.date,
            date_last=root.date,
        )
        db.upsert_thread(t2, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT display_subject, date_first FROM threads WHERE thread_id = ?",
            (t1.thread_id,),
        ).fetchone()
        # date_first must reflect the older root (sanity-check the merge).
        assert row["date_first"] == "2024-01-01T09:00:00+00:00"
        # display_subject must now reflect the older root, not the reply.
        assert row["display_subject"] == "Today's Meeting"

    def test_display_subject_backfills_when_legacy_row_was_null(self, db):
        """A pre-v13 row has ``display_subject = NULL``. The next upsert
        that supplies a non-NULL value backfills it. After that, normal
        date-ordered precedence applies."""
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

    def test_per_message_body_cap_matches_across_insert_and_update_paths(self, db, threader):
        # Both the fresh-insert path (``Thread.text_for_embedding``) and
        # the accumulation path (``Database._compute_body``) must apply
        # the SAME per-message char cap. The previous shape used 500 chars
        # on insert and 2000 chars on update — meaning a thread that
        # arrived as a single 3000-char message had only the first 500
        # chars indexed for FTS forever, while an identical thread that
        # arrived as a sequence of replies got 2000 chars per message.
        # The asymmetry was permanent and tied to arrival ordering.
        from src.threader import PER_MESSAGE_BODY_CAP_CHARS

        long_body = "X" * (PER_MESSAGE_BODY_CAP_CHARS + 1000)

        # Distinct subjects keep the two seed messages from subject-merging
        # into a single thread via the threader's subject-fallback path.
        fresh_msg = make_message(
            message_id="cap_fresh@example.com",
            subject="cap fresh subject",
            body_text=long_body,
        )
        t_fresh = threader.assign_thread(fresh_msg)
        db.upsert_thread(t_fresh, FAKE_EMBEDDING)
        fresh_row = db._conn.execute(
            "SELECT body_text FROM threads WHERE thread_id = 'cap_fresh@example.com'"
        ).fetchone()

        seed_msg = make_message(
            message_id="cap_seed@example.com",
            subject="cap seed subject",
            body_text="short seed",
        )
        t_seed = threader.assign_thread(seed_msg)
        db.upsert_thread(t_seed, FAKE_EMBEDDING)
        update_msg = make_message(
            message_id="cap_update@example.com",
            subject="Re: cap seed subject",
            in_reply_to="cap_seed@example.com",
            body_text=long_body,
            filepath="/maildir/INBOX/cur/cap_update",
            date=datetime(2024, 6, 1, tzinfo=UTC),
        )
        t_update = threader.assign_thread(update_msg)
        db.upsert_thread(t_update, FAKE_EMBEDDING)
        update_row = db._conn.execute(
            "SELECT body_text FROM threads WHERE thread_id = 'cap_seed@example.com'"
        ).fetchone()

        # Each path keeps exactly ``PER_MESSAGE_BODY_CAP_CHARS`` of the
        # long body. The cap fires once per message even though the two
        # paths assemble the surrounding metadata differently.
        assert fresh_row["body_text"].count("X") == PER_MESSAGE_BODY_CAP_CHARS
        assert update_row["body_text"].count("X") == PER_MESSAGE_BODY_CAP_CHARS

    def test_accumulated_body_capped_at_token_budget(self, db, threader):
        # The accumulated thread body must respect the same token-based
        # cap (``THREAD_BODY_TEXT_MAX_TOKENS``) on update that the
        # fresh-insert ``Thread.text_for_embedding`` applies. Without
        # the shared cap, replies arriving after the initial insert
        # could expand the stored body well past what the insert path
        # would have kept, drifting FTS / embedding inputs across the
        # two code paths.
        from src.chunker import estimate_tokens
        from src.threader import THREAD_BODY_TEXT_MAX_TOKENS

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
        assert estimate_tokens(row["body_text"]) <= THREAD_BODY_TEXT_MAX_TOKENS


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
        assert count_pending_deletions(db) == 1

    def test_add_pending_deletion_refuses_a_path_the_message_no_longer_maps_to(self, db):
        """#301: a sweep holding a stale path must not tombstone a message
        the watcher has since moved; the reaper matches tombstones by
        message ID, so the stale row would delete the live message."""
        msg = make_message(message_id="moved@x", filepath="/cur/moved:2,ST")
        thread = make_thread(messages=[msg])
        db.upsert_thread(thread, FAKE_EMBEDDING)
        db.update_filepath("/cur/moved:2,ST", "/cur/moved:2,S", clear_tombstone=True)

        assert db.add_pending_deletion("/cur/moved:2,ST", "moved@x", thread.thread_id) is False
        assert count_pending_deletions(db) == 0
        assert db.add_pending_deletion("/cur/moved:2,S", "moved@x", thread.thread_id) is True

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

    def test_reap_removes_map_indexed_and_tombstone(self, db):
        msg1 = make_message(message_id="keep@x", filepath="/keep")
        msg2 = make_message(message_id="drop@x", filepath="/drop")
        thread = make_thread(messages=[msg1, msg2])
        db.upsert_thread(thread, FAKE_EMBEDDING)

        assert _reap_message(db, thread, "drop@x") == ["/drop"]

        assert db.find_message_entry_by_filepath("/drop") is None
        assert db.is_indexed("/drop") is False
        assert db.has_pending_deletion("/drop") is False
        # Other message and parent thread row stay intact
        assert db.find_message_entry_by_filepath("/keep") is not None
        assert db.get_thread(thread.thread_id) is not None

    def test_reap_skips_unknown_message_id(self, db):
        """A reaped ID with no ``message_thread_map`` row removes nothing
        and does not block the rewrite."""
        thread = make_thread(messages=[make_message(message_id="keep@x", filepath="/keep")])
        db.upsert_thread(thread, FAKE_EMBEDDING)

        assert db.reap_thread_messages(thread, FAKE_EMBEDDING, ["ghost@x"]) == []

        assert db.find_message_entry_by_filepath("/keep") is not None
        assert db.get_thread(thread.thread_id) is not None

    def test_delete_thread_completely_removes_all_dependent_rows(self, db):
        msg1 = make_message(message_id="d1@x", filepath="/d/1")
        msg2 = make_message(message_id="d2@x", filepath="/d/2")
        thread = make_thread(messages=[msg1, msg2], thread_id="doomed", subject="doomedsubject")
        db.upsert_thread(thread, FAKE_EMBEDDING)
        db.add_pending_deletion("/d/1", "d1@x", "doomed")

        _tombstone_thread(db, "doomed")
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


class TestConcurrency:
    def test_concurrent_writes_do_not_corrupt_or_error(self, db):
        """Two threads hammering ``upsert_thread`` must not interleave
        ``BEGIN IMMEDIATE``/``COMMIT`` pairs on the shared connection.
        Without the per-instance lock, cross-thread interleaving can
        raise ``sqlite3.OperationalError`` ("cannot start a transaction
        within a transaction") or silently commit partial state.
        """
        errors: list[Exception] = []
        FAKE_EMB = [0.0] * EMBEDDING_DIM

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
    messages belong to the thread."""

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


class TestReapRewritesThreadRow:
    """The reap's thread rewrite regenerates the row from the survivors
    rather than merging into the stored row as ``upsert_thread`` does."""

    def test_reap_rewrites_reply_subjects_in_fts(self, db, threader):
        """#303: a changed reply subject is keyword-searchable through
        the ``threads_fts`` subject column; the reap rewrite keeps the
        survivors' and drops the reaped message's."""
        from src.threader import Thread

        original = make_message(message_id="s1@x", subject="Hello world", filepath="/s/1")
        kept = make_message(
            message_id="s2@x",
            subject="Re: Hello world KEPTZX1",
            in_reply_to="s1@x",
            filepath="/s/2",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        reaped = make_message(
            message_id="s3@x",
            subject="Re: Hello world GONEZX2",
            in_reply_to="s1@x",
            filepath="/s/3",
            date=datetime(2024, 3, 1, tzinfo=UTC),
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, FAKE_EMBEDDING)
        for msg in (kept, reaped):
            db.upsert_thread(threader.assign_thread(msg), FAKE_EMBEDDING)

        def hits(term):
            return db._conn.execute(
                "SELECT COUNT(*) FROM threads_fts WHERE threads_fts MATCH ?", (f"subject:{term}",)
            ).fetchone()[0]

        assert (hits("keptzx1"), hits("gonezx2")) == (1, 1)

        rebuilt = Thread(
            thread_id=t1.thread_id,
            subject=t1.subject,
            participants=["only@x"],
            messages=[original, kept],
            folder="INBOX",
            date_first=original.date,
            date_last=kept.date,
        )
        db.add_pending_deletion("/s/3", "s3@x", t1.thread_id)
        assert db.reap_thread_messages(rebuilt, FAKE_EMBEDDING, ["s3@x"]) == ["/s/3"]
        assert (hits("keptzx1"), hits("gonezx2")) == (1, 0)
        assert db.get_thread(t1.thread_id).subject == "hello world"

    def test_reap_replaces_body_text_instead_of_appending(self, db, threader):
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

        # Reap the original — rebuild from reply only
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
        db.add_pending_deletion("/r/1", "r1@x", t1.thread_id)
        assert db.reap_thread_messages(rebuilt, FAKE_EMBEDDING, ["r1@x"]) == ["/r/1"]

        row = db._conn.execute(
            "SELECT body_text FROM threads WHERE thread_id = ?", (t1.thread_id,)
        ).fetchone()
        # The original's body must no longer be present — the rewrite is
        # a full replacement, not an append.
        assert "First message body." not in row["body_text"]
        assert "Second message body." in row["body_text"]

    def test_reap_refreshes_display_subject_when_root_message_reaped(self, db, threader):
        """Codex review of main caught that ``_rewrite_thread_row`` did
        not refresh ``display_subject`` — reaping the original root of a
        thread (which contributed the user-facing label via
        ``upsert_thread``) would leave search results rendering with the
        deleted message's subject. The rewrite now derives
        ``display_subject`` from the surviving messages the same way
        ``upsert_thread`` does (oldest message's original subject)."""
        from src.threader import Thread

        original = make_message(
            message_id="ds-original@x",
            subject="Original Display Subject",
            filepath="/ds/1",
            date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        reply = make_message(
            message_id="ds-reply@x",
            subject="Re: Original Display Subject",
            in_reply_to="ds-original@x",
            filepath="/ds/2",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, FAKE_EMBEDDING)
        t2 = threader.assign_thread(reply)
        db.upsert_thread(t2, FAKE_EMBEDDING)

        # Sanity check: the original's subject is the display label.
        row = db._conn.execute(
            "SELECT display_subject FROM threads WHERE thread_id = ?",
            (t1.thread_id,),
        ).fetchone()
        assert row["display_subject"] == "Original Display Subject"

        # Reap the original — rebuild from the reply only.
        rebuilt = Thread(
            thread_id=t1.thread_id,
            subject=t1.subject,
            participants=[reply.from_addr] + reply.to_addrs,
            messages=[reply],
            folder="INBOX",
            date_first=reply.date,
            date_last=reply.date,
        )
        db.add_pending_deletion("/ds/1", "ds-original@x", t1.thread_id)
        assert db.reap_thread_messages(rebuilt, FAKE_EMBEDDING, ["ds-original@x"]) == ["/ds/1"]

        # The display label must now reflect the surviving message,
        # not the reaped root.
        row = db._conn.execute(
            "SELECT display_subject FROM threads WHERE thread_id = ?",
            (t1.thread_id,),
        ).fetchone()
        assert row["display_subject"] == "Re: Original Display Subject"

    @pytest.mark.parametrize(
        ("survivor_subjects", "expected"),
        [
            (["", "Re: Live Label", "Re: Later Label"], "Re: Live Label"),
            (["", ""], None),
        ],
    )
    def test_reap_display_subject_skips_blank_oldest_survivor(
        self, db, threader, survivor_subjects, expected
    ):
        """The label after a reap is the first survivor subject, in date
        order, that is non-empty; NULL only when no survivor has one."""
        from src.threader import Thread

        original = make_message(
            message_id="bl-0@x",
            subject="Deleted Label",
            filepath="/bl/0",
            date=datetime(2024, 1, 1, tzinfo=UTC),
        )
        db.upsert_thread(threader.assign_thread(original), FAKE_EMBEDDING)
        survivors = []
        for i, subject in enumerate(survivor_subjects, start=1):
            msg = make_message(
                message_id=f"bl-{i}@x",
                subject=subject,
                in_reply_to=f"bl-{i - 1}@x",
                filepath=f"/bl/{i}",
                date=datetime(2024, 2, i, tzinfo=UTC),
            )
            thread = threader.assign_thread(msg)
            db.upsert_thread(thread, FAKE_EMBEDDING)
            survivors.append(msg)

        rebuilt = Thread(
            thread_id=thread.thread_id,
            subject=thread.subject,
            participants=[survivors[0].from_addr],
            messages=survivors,
            folder="INBOX",
            date_first=survivors[0].date,
            date_last=survivors[-1].date,
        )
        db.add_pending_deletion("/bl/0", "bl-0@x", thread.thread_id)
        assert db.reap_thread_messages(rebuilt, FAKE_EMBEDDING, ["bl-0@x"]) == ["/bl/0"]

        assert db.get_thread_display_subject(thread.thread_id) == expected

    def test_reap_updates_fts_and_vec_rows(self, db, threader):
        original = make_message(message_id="r3@x", filepath="/r/3")
        reply = make_message(
            message_id="r4@x",
            in_reply_to="r3@x",
            filepath="/r/4",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        t1 = threader.assign_thread(original)
        db.upsert_thread(t1, FAKE_EMBEDDING)
        db.upsert_thread(threader.assign_thread(reply), FAKE_EMBEDDING)

        # Reap the reply and rewrite with a different subject. The
        # survivor carries that subject too: a survivor whose own subject
        # differs from the thread's is indexed in the FTS subject column
        # (#303), which would keep "hello" searchable for a real reason.
        from dataclasses import replace

        from src.threader import Thread

        rebuilt = Thread(
            thread_id=t1.thread_id,
            subject="brand new subject",
            participants=["only@x"],
            messages=[replace(original, subject="Brand new subject")],
            folder="INBOX",
            date_first=original.date,
            date_last=original.date,
        )
        db.add_pending_deletion("/r/4", "r4@x", t1.thread_id)
        assert db.reap_thread_messages(rebuilt, FAKE_EMBEDDING, ["r4@x"]) == ["/r/4"]

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
        assert "idx_message_chunks_claimant" in indexes
        assert "idx_message_chunks_thread" in indexes

    def test_chunk_columns_complete(self, db):
        cols = {row[1] for row in db._conn.execute("PRAGMA table_info(message_chunks)").fetchall()}
        for required in (
            "chunk_id",
            "claimant_id",
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
                claimant_id="missing@x",
                thread_id="missing-thread",
                chunks=[chunk],
                embeddings_by_chunk_id={chunk.chunk_id: [0.1] * EMBEDDING_DIM},
            )

    def test_first_write_inserts_all_chunks_and_indexes(self, db):
        _seed_thread_for_message(db, "m1@x", "t1")
        chunks = [_make_chunk("a" * 64, 0, "first"), _make_chunk("b" * 64, 1, "second")]
        embeds = {
            chunks[0].chunk_id: [0.1] * EMBEDDING_DIM,
            chunks[1].chunk_id: [0.2] * EMBEDDING_DIM,
        }

        result = db.replace_message_chunks(
            claimant_id="m1@x",
            thread_id="t1",
            chunks=chunks,
            embeddings_by_chunk_id=embeds,
        )

        assert result == {"inserted": 2, "deleted": 0, "kept": 0}
        # Each chunk landed in all three indexes.
        assert (
            db._conn.execute(
                "SELECT COUNT(*) FROM message_chunks WHERE claimant_id = ?", ("m1@x",)
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
            "SELECT fts_rowid FROM message_chunks WHERE claimant_id = ?", ("m1@x",)
        ).fetchall()
        assert all(r["fts_rowid"] is not None for r in rows)

    def test_replay_with_identical_input_is_idempotent(self, db):
        """Re-running the chunker with the same body must produce no work
        — the diff path must skip already-stored chunk_ids and require no
        embeddings for them.
        """
        _seed_thread_for_message(db, "m2@x", "t2")
        chunks = [_make_chunk("c" * 64, 0, "stable")]
        embeds = {chunks[0].chunk_id: [0.3] * EMBEDDING_DIM}

        first = db.replace_message_chunks(
            claimant_id="m2@x",
            thread_id="t2",
            chunks=chunks,
            embeddings_by_chunk_id=embeds,
        )
        assert first == {"inserted": 1, "deleted": 0, "kept": 0}

        # Replay with no embeddings — would raise if the diff path tried
        # to insert anything.
        second = db.replace_message_chunks(
            claimant_id="m2@x",
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
            claimant_id="m3@x",
            thread_id="t3",
            chunks=[keep, drop],
            embeddings_by_chunk_id={
                keep.chunk_id: [0.1] * EMBEDDING_DIM,
                drop.chunk_id: [0.2] * EMBEDDING_DIM,
            },
        )

        # Round 2: keep + new (drop should be deleted; keep should be kept).
        result = db.replace_message_chunks(
            claimant_id="m3@x",
            thread_id="t3",
            chunks=[keep, new],
            embeddings_by_chunk_id={new.chunk_id: [0.3] * EMBEDDING_DIM},
        )
        assert result == {"inserted": 1, "deleted": 1, "kept": 1}

        stored = {
            row[0]
            for row in db._conn.execute(
                "SELECT chunk_id FROM message_chunks WHERE claimant_id = ?", ("m3@x",)
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
                claimant_id="m4@x",
                thread_id="t4",
                chunks=[chunk],
                embeddings_by_chunk_id={},
            )

    def test_normalizes_non_unit_chunk_embedding_at_db_boundary(self, db):
        """Storage invariant: every vector written to
        ``message_chunks_vec`` must be L2-unit-norm so cosine ranking
        does not depend on per-row magnitude. Production writes flow
        through ``OpenAIEmbedder._extract_embeddings`` which already
        normalizes provider output, but the ``EmbeddingBackend``
        contract is generic — a fake backend in a test or a future
        non-OpenAI caller could pass non-unit vectors. Enforce the
        invariant at the DB write boundary so it cannot be silently
        bypassed."""
        import struct

        _seed_thread_for_message(db, "m-norm@x", "t-norm")
        chunk = _make_chunk("n" * 64, 0, "non-unit chunk")
        # Magnitude-2 vector: each component is 2.0/sqrt(EMBEDDING_DIM)
        # so the L2 norm is 2.0. Without normalization at the DB
        # boundary the row lands raw and downstream cosine ranking
        # weights this chunk twice as heavily as a unit-norm peer.
        scaled = [2.0 / (EMBEDDING_DIM**0.5)] * EMBEDDING_DIM

        db.replace_message_chunks(
            claimant_id="m-norm@x",
            thread_id="t-norm",
            chunks=[chunk],
            embeddings_by_chunk_id={chunk.chunk_id: scaled},
        )

        row = db._conn.execute(
            "SELECT embedding FROM message_chunks_vec WHERE chunk_id = ?",
            (chunk.chunk_id,),
        ).fetchone()
        stored = list(struct.unpack(f"{EMBEDDING_DIM}f", row["embedding"]))
        norm = sum(x * x for x in stored) ** 0.5
        assert norm == pytest.approx(1.0, abs=1e-6), (
            f"replace_message_chunks must L2-normalize at the DB write "
            f"boundary; stored norm was {norm} (expected ~1.0). The "
            f"OpenAIEmbedder happens to pre-normalize today, but the "
            f"persistence boundary itself is what protects the invariant "
            f"from arbitrary EmbeddingBackend callers."
        )

    def test_wrong_dim_embedding_raises(self, db):
        chunk = _make_chunk("f" * 64, 0, "bad dim")
        with pytest.raises(ValueError, match="EMBEDDING_DIM|reserves 4096"):
            db.replace_message_chunks(
                claimant_id="m5@x",
                thread_id="t5",
                chunks=[chunk],
                embeddings_by_chunk_id={chunk.chunk_id: [0.1] * 100},
            )


def _one_hot(slot: int) -> list[float]:
    """Build a unit-norm vector with a single 1.0 at ``slot``.

    Used by aggregation tests that need vectors that survive the
    DB-write boundary's L2 normalization with distinguishable shapes.
    Magnitude-only inputs like ``[0.5] * EMBEDDING_DIM`` and
    ``[0.7] * EMBEDDING_DIM`` collapse to the same normalized shape
    (``[1/sqrt(N)] * N``) once the storage invariant fires, so a
    test that distinguished them by per-element value would no
    longer be valid. One-hot vectors are already unit-norm and
    survive normalization byte-identical.
    """
    vec = [0.0] * EMBEDDING_DIM
    vec[slot] = 1.0
    return vec


class TestMessageDateOnChunks:
    """A chunk stores no copy of its message's date (#575): readers take
    a passage's date from its ``messages`` row, so a re-dated message
    whose chunks were not rewritten cannot disagree with them.
    """

    def test_chunks_store_no_message_date(self, db):
        columns = {r["name"] for r in db._conn.execute("PRAGMA table_info(message_chunks)")}
        assert "message_date" not in columns
        assert "message_date" not in inspect.signature(db.replace_message_chunks).parameters

    def test_redated_message_moves_the_thread_range(self, db):
        """Review round 2: reprocessing a message with a corrected date
        sets the thread's range from its messages' ``sent_at``; the old
        date does not linger as an endpoint (the threader hands over the
        stored range widened by the new date)."""
        from src.threader import Thread

        jan = datetime(2024, 1, 10, 9, 0, tzinfo=UTC)
        jun = datetime(2024, 6, 10, 9, 0, tzinfo=UTC)
        msg = make_message(message_id="redate@x", filepath="/maildir/INBOX/cur/redate", date=jan)
        db.upsert_thread(make_thread(messages=[msg], thread_id="t-redate"), FAKE_EMBEDDING)
        msg.date = jun
        widened = Thread(
            thread_id="t-redate",
            subject="hello world",
            participants=[msg.from_addr, *msg.to_addrs],
            messages=[msg],
            folder="INBOX",
            date_first=jan,
            date_last=jun,
        )
        db.upsert_thread(widened, FAKE_EMBEDDING)

        row = db._conn.execute(
            "SELECT date_first, date_last FROM threads WHERE thread_id = 't-redate'"
        ).fetchone()
        sent_at = db.get_message_sent_at(msg.claimant_id)
        assert sent_at == jun
        assert (row["date_first"], row["date_last"]) == (jun.isoformat(), jun.isoformat())


class TestThreadChunkAggregation:
    def test_get_thread_chunk_embeddings_returns_per_message_vectors(self, db):
        # Two messages in the same thread, each with one chunk. Use
        # one-hot vectors with distinct active slots so the round-trip
        # is checkable through the normalize-at-boundary path: the
        # active slot identifies which input the vector came from.
        _seed_thread_for_message(db, "m6a@x", "t6")
        msg = make_message(message_id="m6b@x", filepath="/maildir/INBOX/cur/m6b@x")
        db.upsert_thread(make_thread(messages=[msg], thread_id="t6"), FAKE_EMBEDDING)
        for mid, slot in [("m6a@x", 0), ("m6b@x", 1)]:
            chunk = _make_chunk(f"x{mid}".ljust(64, "0"), 0, f"body of {mid}")
            db.replace_message_chunks(
                claimant_id=mid,
                thread_id="t6",
                chunks=[chunk],
                embeddings_by_chunk_id={chunk.chunk_id: _one_hot(slot)},
            )

        results = db.get_thread_chunk_embeddings("t6")
        assert len(results) == 2
        active_slots = sorted(v.index(1.0) for v in results)
        assert active_slots == [0, 1]

    def test_get_chunk_embeddings_for_messages_filters_correctly(self, db):
        _seed_thread_for_message(db, "m7a@x", "t7")
        msg = make_message(message_id="m7b@x", filepath="/maildir/INBOX/cur/m7b@x")
        db.upsert_thread(make_thread(messages=[msg], thread_id="t7"), FAKE_EMBEDDING)
        for mid, slot in [("m7a@x", 2), ("m7b@x", 3)]:
            chunk = _make_chunk(f"y{mid}".ljust(64, "0"), 0, f"body of {mid}")
            db.replace_message_chunks(
                claimant_id=mid,
                thread_id="t7",
                chunks=[chunk],
                embeddings_by_chunk_id={chunk.chunk_id: _one_hot(slot)},
            )

        survivors = db.get_chunk_embeddings_for_messages(["m7a@x"])
        assert len(survivors) == 1
        assert survivors[0].index(1.0) == 2

    def test_get_chunk_embeddings_for_messages_empty_input_returns_empty(self, db):
        assert db.get_chunk_embeddings_for_messages([]) == []

    def test_thread_has_chunks_returns_true_only_when_rows_exist(self, db):
        # ``thread_has_chunks`` exists so the batched indexer's hot
        # subject-fallback gate can check "any chunks committed?"
        # without unpacking every chunk vector via
        # ``get_thread_chunk_embeddings``. The contract: True when at
        # least one ``message_chunks`` row references the thread,
        # False otherwise (no rows, or thread does not exist at all).
        assert db.thread_has_chunks("never-existed") is False

        _seed_thread_for_message(db, "th@x", "t_has")
        # Thread row exists but no chunks yet — gate must say False.
        assert db.thread_has_chunks("t_has") is False

        chunk = _make_chunk("zhas".ljust(64, "0"), 0, "some body")
        db.replace_message_chunks(
            claimant_id="th@x",
            thread_id="t_has",
            chunks=[chunk],
            embeddings_by_chunk_id={chunk.chunk_id: _one_hot(0)},
        )
        assert db.thread_has_chunks("t_has") is True


class TestAtomicIndexTransaction:
    def test_thread_and_chunk_writes_roll_back_together(self, db):
        msg = make_message(message_id="atomic@x", filepath="/maildir/INBOX/cur/atomic")
        thread = make_thread(messages=[msg], thread_id="atomic-thread")
        chunk = _make_chunk("atomic-chunk".ljust(64, "0"), 0, "atomic body")

        with pytest.raises(RuntimeError, match="force rollback"):
            with db.transaction():
                db.upsert_thread(thread, FAKE_EMBEDDING)
                db.replace_message_chunks(
                    claimant_id=msg.message_id,
                    thread_id=thread.thread_id,
                    chunks=[chunk],
                    embeddings_by_chunk_id={chunk.chunk_id: [0.2] * EMBEDDING_DIM},
                )
                raise RuntimeError("force rollback")

        assert db.get_thread(thread.thread_id) is None
        assert db.get_chunk_ids_for_message(msg.message_id) == set()
        assert db.find_thread_by_message_id(msg.message_id) is None


class TestChunkCascadeOnMessageRemoval:
    def test_reap_drops_its_chunks(self, db, threader):
        message = make_message(message_id="m8@x", filepath="/m/8")
        thread = threader.assign_thread(message)
        thread.messages.append(make_message(message_id="m8-keep@x", filepath="/m/8-keep"))
        db.upsert_thread(thread, FAKE_EMBEDDING)

        chunk = _make_chunk("z" * 64, 0, "to be removed")
        db.replace_message_chunks(
            claimant_id="m8@x",
            thread_id=thread.thread_id,
            chunks=[chunk],
            embeddings_by_chunk_id={chunk.chunk_id: [0.4] * EMBEDDING_DIM},
        )

        _reap_message(db, thread, "m8@x")

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
                claimant_id=mid,
                thread_id=t.thread_id,
                chunks=[chunk],
                embeddings_by_chunk_id={chunk.chunk_id: [0.5] * EMBEDDING_DIM},
            )

        _tombstone_thread(db, t.thread_id)
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
            claimant_id="att1@x",
            thread_id=thread.thread_id,
            attachment_id="hash-a" * 8,
            filename="invoice.pdf",
            content_type="application/pdf",
            size_bytes=1234,
            occurrence_id=attachment_occurrence_id(
                claimant_id="att1@x",
                content_hash="hash-a" * 8,
                filename="invoice.pdf",
                occurrence_index=0,
            ),
        )
        assert inserted is True

        row = db._conn.execute(
            "SELECT filename, content_type, size_bytes, fts_rowid "
            "FROM attachments WHERE claimant_id = ? AND attachment_id = ?",
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
            claimant_id="att2@x",
            thread_id=thread.thread_id,
            attachment_id="hash-b" * 8,
            filename="contract.docx",
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            size_bytes=5678,
            occurrence_id=attachment_occurrence_id(
                claimant_id="att2@x",
                content_hash="hash-b" * 8,
                filename="contract.docx",
                occurrence_index=0,
            ),
        )
        assert db.upsert_attachment(**kwargs) is True
        assert db.upsert_attachment(**kwargs) is False

        # Exactly one attachments row + one FTS row.
        cnt = db._conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE claimant_id = ?", ("att2@x",)
        ).fetchone()[0]
        assert cnt == 1

    def test_same_payload_can_have_multiple_filename_occurrences(self, db, threader):
        msg = make_message(message_id="att-dupe@x", filepath="/m/att-dupe")
        thread = threader.assign_thread(msg)
        db.upsert_thread(thread, FAKE_EMBEDDING)

        shared_hash = "hash-dupe" * 8
        assert db.upsert_attachment(
            claimant_id="att-dupe@x",
            thread_id=thread.thread_id,
            attachment_id=shared_hash,
            filename="invoice-a.pdf",
            content_type="application/pdf",
            size_bytes=10,
            occurrence_id="occ-a",
        )
        assert db.upsert_attachment(
            claimant_id="att-dupe@x",
            thread_id=thread.thread_id,
            attachment_id=shared_hash,
            filename="invoice-b.pdf",
            content_type="application/pdf",
            size_bytes=10,
            occurrence_id="occ-b",
        )

        rows = db._conn.execute(
            "SELECT filename FROM attachments WHERE claimant_id = ? ORDER BY filename",
            ("att-dupe@x",),
        ).fetchall()
        assert [r["filename"] for r in rows] == ["invoice-a.pdf", "invoice-b.pdf"]

    def test_attachments_fts_searchable_by_filename(self, db, threader):
        msg = make_message(message_id="att3@x", filepath="/m/att3")
        thread = threader.assign_thread(msg)
        db.upsert_thread(thread, FAKE_EMBEDDING)
        db.upsert_attachment(
            claimant_id="att3@x",
            thread_id=thread.thread_id,
            attachment_id="hash-c" * 8,
            filename="march-statement.pdf",
            content_type="application/pdf",
            size_bytes=10,
            occurrence_id=attachment_occurrence_id(
                claimant_id="att3@x",
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
            claimant_id="slice1@x",
            thread_id=thread.thread_id,
            chunks=[body_chunk],
            embeddings_by_chunk_id={body_chunk.chunk_id: [0.1] * EMBEDDING_DIM},
        )
        db.replace_message_chunks(
            claimant_id="slice1@x",
            thread_id=thread.thread_id,
            chunks=[att_chunk],
            embeddings_by_chunk_id={att_chunk.chunk_id: [0.2] * EMBEDDING_DIM},
            attachment_id=attachment_id,
        )

        # Both chunks present.
        all_ids = {
            row[0]
            for row in db._conn.execute(
                "SELECT chunk_id FROM message_chunks WHERE claimant_id = ?",
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
            claimant_id="slice2@x",
            thread_id=thread.thread_id,
            chunks=[body],
            embeddings_by_chunk_id={body.chunk_id: [0.1] * EMBEDDING_DIM},
        )
        db.replace_message_chunks(
            claimant_id="slice2@x",
            thread_id=thread.thread_id,
            chunks=[att],
            embeddings_by_chunk_id={att.chunk_id: [0.2] * EMBEDDING_DIM},
            attachment_id=att_id,
        )

        # Re-write body slice with a different chunk — attachment chunk stays.
        body2 = _make_chunk("body2-new".ljust(64, "0"), 0, "new body")
        db.replace_message_chunks(
            claimant_id="slice2@x",
            thread_id=thread.thread_id,
            chunks=[body2],
            embeddings_by_chunk_id={body2.chunk_id: [0.3] * EMBEDDING_DIM},
        )

        assert db.get_chunk_ids_for_message("slice2@x") == {body2.chunk_id}
        assert db.get_chunk_ids_for_message("slice2@x", attachment_id=att_id) == {att.chunk_id}


class TestAttachmentCascadeOnMessageRemoval:
    def test_reap_drops_its_attachment_rows(self, db, threader):
        msg = make_message(message_id="cas1@x", filepath="/m/cas1")
        thread = threader.assign_thread(msg)
        thread.messages.append(make_message(message_id="cas1-keep@x", filepath="/m/cas1-keep"))
        db.upsert_thread(thread, FAKE_EMBEDDING)

        db.upsert_attachment(
            claimant_id="cas1@x",
            thread_id=thread.thread_id,
            attachment_id="cascade-hash" * 4,
            filename="doomed.pdf",
            content_type="application/pdf",
            size_bytes=1,
            occurrence_id=attachment_occurrence_id(
                claimant_id="cas1@x",
                content_hash="cascade-hash" * 4,
                filename="doomed.pdf",
                occurrence_index=0,
            ),
        )
        # Make sure it landed.
        before = db._conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE claimant_id = ?", ("cas1@x",)
        ).fetchone()[0]
        assert before == 1

        _reap_message(db, thread, "cas1@x")

        after = db._conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE claimant_id = ?", ("cas1@x",)
        ).fetchone()[0]
        assert after == 0

    def test_extraction_cache_is_preserved_on_message_removal(self, db, threader):
        """Cached extractions outlive their messages so a future
        re-arrival of the same content (forwarded again) skips the
        extract cost."""
        msg = make_message(message_id="cas2@x", filepath="/m/cas2")
        thread = threader.assign_thread(msg)
        thread.messages.append(make_message(message_id="cas2-keep@x", filepath="/m/cas2-keep"))
        db.upsert_thread(thread, FAKE_EMBEDDING)

        attachment_id = "preserve-hash" * 4
        db.upsert_attachment(
            claimant_id="cas2@x",
            thread_id=thread.thread_id,
            attachment_id=attachment_id,
            filename="preserved.pdf",
            content_type="application/pdf",
            size_bytes=1,
            occurrence_id=attachment_occurrence_id(
                claimant_id="cas2@x",
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

        _reap_message(db, thread, "cas2@x")

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
                claimant_id=mid,
                thread_id=t.thread_id,
                attachment_id=attachment_id,
                filename=f"{mid}.pdf",
                content_type="application/pdf",
                size_bytes=1,
                occurrence_id=attachment_occurrence_id(
                    claimant_id=mid,
                    content_hash=attachment_id,
                    filename=f"{mid}.pdf",
                    occurrence_index=0,
                ),
            )

        _tombstone_thread(db, t.thread_id)
        db.delete_thread_completely(t.thread_id)

        cnt = db._conn.execute(
            "SELECT COUNT(*) FROM attachments WHERE thread_id = ?", (t.thread_id,)
        ).fetchone()[0]
        assert cnt == 0


class TestWalCheckpoint:
    """``Database.wal_checkpoint_truncate`` shrinks the WAL file.

    SQLite's automatic checkpoint at the page-count threshold copies
    frames back and lets later writes reuse the WAL, but it never
    shrinks the file, and an open read transaction on any connection
    (an open connection alone does not pin a snapshot) lets it grow
    further. The main-loop periodic truncate-checkpoint is what
    reclaims that space.
    """

    def test_returns_three_int_tuple(self, tmp_path):
        db = Database(tmp_path / "mail.db")
        try:
            result = db.wal_checkpoint_truncate()
            assert isinstance(result, tuple) and len(result) == 3
            busy, log_pages, ckpt_pages = result
            assert isinstance(busy, int)
            assert isinstance(log_pages, int)
            assert isinstance(ckpt_pages, int)
        finally:
            db.close()

    def test_checkpoint_after_writes_truncates_wal(self, tmp_path):
        """Sanity check: write some data, run the checkpoint, and the
        WAL file is either gone or zero-length. Without the explicit
        truncate the WAL persists across writes for the lifetime of
        the connection."""
        db_path = tmp_path / "mail.db"
        db = Database(db_path)
        try:
            db.upsert_thread(make_thread([make_message(message_id="m1@x")]), FAKE_EMBEDDING)
            wal_path = tmp_path / "mail.db-wal"
            assert wal_path.exists() and wal_path.stat().st_size > 0
            busy, _log, _ckpt = db.wal_checkpoint_truncate()
            # busy=0 expected since the writer's own connection is the
            # only reader and the cursor has been released by now.
            assert busy == 0
            # After TRUNCATE the WAL is either deleted or zero-length
            # (SQLite 3.43+ leaves the file at 0 bytes).
            if wal_path.exists():
                assert wal_path.stat().st_size == 0
        finally:
            db.close()


class TestMessagesTable:
    """Per-message records: one ``messages`` row per indexed message plus
    normalized ``message_participants`` rows, written atomically with the
    thread. They back exact enumeration (every message from/to X), the
    authoritative per-message view, and source provenance."""

    def _participants(self, db, claimant_id):
        return {
            (r["role"], r["address"], r["name"])
            for r in db._conn.execute(
                "SELECT role, address, name FROM message_participants WHERE claimant_id = ?",
                (claimant_id,),
            )
        }

    def test_schema_contract(self, db):
        cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(messages)")}
        assert cols == {
            "claimant_id",
            "message_id",
            "thread_id",
            "filepath",
            "folder",
            "subject",
            "sent_at",
            "in_reply_to",
            "references_json",
            "has_attachments",
            "size_bytes",
            "content_hash",
            "indexed_at",
        }
        cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(message_participants)")}
        assert cols == {"claimant_id", "role", "address", "name"}
        index_cols = [
            r["name"]
            for r in db._conn.execute("PRAGMA index_info(idx_message_participants_address)")
        ]
        assert index_cols == ["address", "role"]

    def test_upsert_thread_writes_message_and_participants(self, db):
        msg = make_message(
            message_id="m1@example.com",
            subject="Re: Budget",
            from_addr='"Alice Example" <Alice@Example.com>',
            to_addrs=["Bob <bob@example.com>", "carol@example.com"],
            cc_addrs=["Dan <DAN@example.com>"],
            filepath="/maildir/INBOX/cur/m1",
            in_reply_to="m0@example.com",
            references=["m0@example.com"],
            has_attachments=True,
        )
        msg.size = 1234
        msg.content_hash = "a" * 64
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _one_hot(0))

        row = db._conn.execute(
            "SELECT * FROM messages WHERE message_id = 'm1@example.com'"
        ).fetchone()
        assert row["claimant_id"] == "m1@example.com#aaaaaaaa"
        assert row["thread_id"] == "t1"
        assert row["filepath"] == "/maildir/INBOX/cur/m1"
        assert row["folder"] == "INBOX"
        assert row["subject"] == "Re: Budget"
        assert row["sent_at"] == "2024-01-01T12:00:00+00:00"
        assert row["in_reply_to"] == "m0@example.com"
        assert json.loads(row["references_json"]) == ["m0@example.com"]
        assert row["has_attachments"] == 1
        assert row["size_bytes"] == 1234
        assert row["content_hash"] == "a" * 64
        assert row["indexed_at"]

        assert self._participants(db, msg.claimant_id) == {
            ("from", "alice@example.com", "Alice Example"),
            ("to", "bob@example.com", "Bob"),
            ("to", "carol@example.com", None),
            ("cc", "dan@example.com", "Dan"),
        }

    def test_reindexing_updates_in_place_without_duplicates(self, db):
        msg = make_message(message_id="m1@example.com", filepath="/maildir/INBOX/cur/m1")
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _one_hot(0))
        moved = make_message(
            message_id="m1@example.com", folder="Archive", filepath="/maildir/Archive/cur/m1"
        )
        db.upsert_thread(make_thread(messages=[moved], thread_id="t1"), _one_hot(0))

        rows = db._conn.execute("SELECT folder, filepath FROM messages").fetchall()
        assert [(r["folder"], r["filepath"]) for r in rows] == [
            ("Archive", "/maildir/Archive/cur/m1")
        ]
        assert self._participants(db, "m1@example.com") == {
            ("from", "alice@example.com", None),
            ("to", "bob@example.com", None),
        }

    def test_malformed_and_duplicate_addresses(self, db):
        msg = make_message(
            message_id="m1@example.com",
            to_addrs=["bob@example.com", "Bobby <BOB@example.com>", "undisclosed-recipients"],
        )
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _one_hot(0))

        assert {p for p in self._participants(db, "m1@example.com") if p[0] == "to"} == {
            ("to", "bob@example.com", None)
        }

    def test_update_filepath_moves_message_locator(self, db):
        msg = make_message(message_id="m1@example.com", filepath="/maildir/INBOX/cur/m1:2,S")
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _one_hot(0))

        db.update_filepath("/maildir/INBOX/cur/m1:2,S", "/maildir/INBOX/cur/m1:2,RS")

        row = db._conn.execute("SELECT filepath FROM messages").fetchone()
        assert row["filepath"] == "/maildir/INBOX/cur/m1:2,RS"

    def test_removing_message_or_thread_cascades(self, db):
        m1 = make_message(message_id="m1@example.com", filepath="/m/1")
        m2 = make_message(message_id="m2@example.com", filepath="/m/2")
        thread = make_thread(messages=[m1, m2], thread_id="t1")
        db.upsert_thread(thread, _one_hot(0))

        _reap_message(db, thread, "m1@example.com")
        assert [r["message_id"] for r in db._conn.execute("SELECT message_id FROM messages")] == [
            "m2@example.com"
        ]
        assert not self._participants(db, "m1@example.com")

        _tombstone_thread(db, "t1")
        db.delete_thread_completely("t1")
        assert db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert db._conn.execute("SELECT COUNT(*) FROM message_participants").fetchone()[0] == 0


class TestUpdateFilepathWithFolder:
    """A cross-folder rename records the new locator and folder in one
    transaction — both commit, or neither does."""

    def _seed(self, db):
        msg = make_message(message_id="m1@example.com", filepath="/md/INBOX/cur/m1")
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _one_hot(0))

    def _state(self, db):
        row = db._conn.execute("SELECT folder, filepath FROM messages").fetchone()
        mapped = db._conn.execute("SELECT filepath FROM message_thread_map").fetchone()
        return row["folder"], row["filepath"], mapped["filepath"]

    def test_updates_locator_and_folder_together(self, db):
        self._seed(db)
        db.update_filepath("/md/INBOX/cur/m1", "/md/Archive/cur/m1", folder="Archive")
        assert self._state(db) == ("Archive", "/md/Archive/cur/m1", "/md/Archive/cur/m1")

    def test_rolls_back_with_an_enclosing_transaction(self, db):
        self._seed(db)
        with pytest.raises(RuntimeError), db.transaction():
            db.update_filepath("/md/INBOX/cur/m1", "/md/Archive/cur/m1", folder="Archive")
            raise RuntimeError("caller failed after the rename")
        assert self._state(db) == ("INBOX", "/md/INBOX/cur/m1", "/md/INBOX/cur/m1")

    def test_folder_failure_rolls_back_the_locator(self, db):
        self._seed(db)
        db._conn.execute(
            "CREATE TRIGGER fail_folder BEFORE UPDATE OF folder ON messages "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            db.update_filepath("/md/INBOX/cur/m1", "/md/Archive/cur/m1", folder="Archive")
        assert self._state(db) == ("INBOX", "/md/INBOX/cur/m1", "/md/INBOX/cur/m1")
        assert db.is_indexed("/md/INBOX/cur/m1")
        assert not db.is_indexed("/md/Archive/cur/m1")


def test_rename_lookups_use_the_filepath_index(db):
    """Every flag rename updates ``messages`` by filepath; without an index
    that is a full-table scan under the shared write lock."""
    for sql in (
        "UPDATE messages SET filepath = ? WHERE filepath = ?",
        "UPDATE messages SET folder = ? WHERE filepath = ?",
    ):
        plan = " ".join(
            r["detail"] for r in db._conn.execute("EXPLAIN QUERY PLAN " + sql, ("a", "b"))
        )
        assert "idx_messages_filepath" in plan, plan


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (
            "SELECT claimant_id FROM messages m WHERE m.message_id = ? "
            "ORDER BY m.sent_at, m.claimant_id LIMIT 21",
            ("a",),
        ),
        (
            "SELECT claimant_id FROM messages WHERE message_id = ? AND claimant_id != ? "
            "ORDER BY claimant_id LIMIT 21",
            ("a", "b"),
        ),
    ],
)
def test_claimant_listings_walk_a_message_id_index_in_order(db, sql, params):
    """#538: the MCP server's ``get_message`` lists one Message-ID's
    claimants oldest first, or in claimant-ID order; an index in each
    order lets ``LIMIT`` stop the walk instead of a sort reading them all."""
    plan = " ".join(r["detail"] for r in db._conn.execute("EXPLAIN QUERY PLAN " + sql, params))
    assert "INDEX idx_messages_message" in plan, plan
    assert "TEMP B-TREE" not in plan, plan


MESSAGE_MAP_INDEXES = {
    "idx_message_thread_map_filepath",
    "idx_message_thread_map_thread",
}


def _message_map_indexes(conn) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'message_thread_map'"
        )
    }


class TestMessageMapLookupIndexes:
    """#302: flag renames and thread rebuilds look ``message_thread_map``
    up by filepath or thread_id; without indexes each lookup scans the
    table, so N renames over N messages is quadratic."""

    def test_fresh_install_creates_the_indexes(self, db):
        assert _message_map_indexes(db._conn) >= MESSAGE_MAP_INDEXES

    @pytest.mark.parametrize(
        ("sql", "params", "index"),
        [
            (
                "SELECT claimant_id, thread_id, filepath FROM message_thread_map WHERE filepath = ?",
                ("/md/cur/7:2,S",),
                "idx_message_thread_map_filepath",
            ),
            (
                "UPDATE message_thread_map SET filepath = ? WHERE filepath = ?",
                ("/md/cur/7:2,RS", "/md/cur/7:2,S"),
                "idx_message_thread_map_filepath",
            ),
            (
                "SELECT claimant_id, filepath FROM message_thread_map WHERE thread_id = ?",
                ("t1",),
                "idx_message_thread_map_thread",
            ),
            (
                "DELETE FROM message_thread_map WHERE thread_id = ?",
                ("t1",),
                "idx_message_thread_map_thread",
            ),
            (
                "SELECT filepath FROM message_thread_map WHERE thread_id IN (?, ?)",
                ("t1", "t2"),
                "idx_message_thread_map_thread",
            ),
        ],
    )
    def test_lookups_search_the_index(self, db, sql, params, index):
        # Representative population so the planner's choice is not an
        # empty-table artefact.
        db._conn.execute("PRAGMA foreign_keys = OFF")
        db._conn.executemany(
            "INSERT INTO message_thread_map VALUES (?, ?, ?, ?)",
            ((f"<m{i}@x>#0", f"<m{i}@x>", f"t{i // 4}", f"/md/cur/{i}:2,S") for i in range(2000)),
        )
        db._conn.commit()
        db._conn.execute("ANALYZE")

        plan = " ".join(r["detail"] for r in db._conn.execute("EXPLAIN QUERY PLAN " + sql, params))

        assert f"SEARCH message_thread_map USING INDEX {index}" in plan, plan
        assert "SCAN message_thread_map" not in plan, plan


class TestUpdateFilepathMovesQueueRow:
    """#203: a flag rename must carry the file's queue row, with its
    retry or dead state, to the new path."""

    def test_queued_row_keeps_its_retry_state(self, tmp_path):
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db, max_attempts=5, base_backoff_seconds=0)
        queue.enqueue("/m/cur/b:2,S", REASON_INITIAL_SCAN)
        queue.mark_failed("/m/cur/b:2,S", stage="embed", error="x")

        db.update_filepath("/m/cur/b:2,S", "/m/cur/b:2,RS")

        assert db.queue_get_attempts("/m/cur/b:2,S") is None
        assert db.queue_get_attempts("/m/cur/b:2,RS") == 1
        assert db.queue_get_status("/m/cur/b:2,RS") == "queued"

    def test_dead_row_moves_so_requeue_targets_the_live_path(self, tmp_path):
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db, max_attempts=1, base_backoff_seconds=0)
        queue.enqueue("/m/cur/b:2,S", REASON_INITIAL_SCAN)
        queue.mark_failed("/m/cur/b:2,S", stage="embed", error="x")

        db.update_filepath("/m/cur/b:2,S", "/m/cur/b:2,RS")

        assert db.queue_get_status("/m/cur/b:2,RS") == "dead"
        assert queue.requeue_dead() == 1
        assert db.queue_get_status("/m/cur/b:2,RS") == "queued"


class TestInterruptedInitialSchema:
    def test_restart_after_a_failure_mid_schema_initializes_cleanly(self, tmp_path, monkeypatch):
        """Regression (#207): the initial DDL committed statement by
        statement and the version stamp separately, so a failure part-way
        (disk full, a virtual-table error, a kill) left half the tables
        with no version row, and every later start failed with "table
        threads already exists" until the volume was wiped."""
        from src.database import SCHEMA_VERSION

        real_apply = Database._apply_initial_schema

        def failing_apply(self, cur):
            def deny_one_index(action, arg1, *_):
                if action == sqlite3.SQLITE_CREATE_INDEX and arg1 == "idx_threads_fts_rowid":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            self._conn.set_authorizer(deny_one_index)
            try:
                real_apply(self, cur)
            finally:
                self._conn.set_authorizer(None)

        monkeypatch.setattr(Database, "_apply_initial_schema", failing_apply)
        with pytest.raises(sqlite3.DatabaseError):
            Database(tmp_path / "mail.db")
        monkeypatch.setattr(Database, "_apply_initial_schema", real_apply)

        db = Database(tmp_path / "mail.db")

        assert db._conn.execute("SELECT version FROM schema_version").fetchone()[0] == (
            SCHEMA_VERSION
        )
        assert db.count_total_messages() == 0


class TestZeroVectorRecoveryBatching:
    """#306: the recovery lookup must bind its thread IDs in bounded
    batches, so a chunkless-thread count above the connection's
    ``SQLITE_LIMIT_VARIABLE_NUMBER`` cannot abort startup."""

    def test_recovery_batches_under_the_variable_limit(self, db, monkeypatch):
        from src import database

        monkeypatch.setattr(database, "_IN_CLAUSE_BATCH_SIZE", 10)
        stuck_paths = []
        for i in range(40):
            stuck = i % 3 == 0  # 14 stuck, 26 healthy, interleaved
            path = f"/maildir/INBOX/cur/m{i}"
            msg = make_message(message_id=f"m{i}@example.com", filepath=path)
            embedding = [0.0] * EMBEDDING_DIM if stuck else FAKE_EMBEDDING
            db.upsert_thread(make_thread(messages=[msg], thread_id=f"t{i}"), embedding)
            if stuck:
                stuck_paths.append(path)

        # Lower the limit below the 40 chunkless IDs before any recovery
        # statement is prepared (the limit is checked at prepare time).
        db._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 16)
        statements: list[str] = []
        db._conn.set_trace_callback(statements.append)
        try:
            found = db.find_zero_vector_chunkless_thread_filepaths()
        finally:
            db._conn.set_trace_callback(None)

        assert sorted(found) == sorted(stuck_paths)
        vec_lookups = [s for s in statements if "FROM threads_vec WHERE thread_id IN" in s]
        map_lookups = [s for s in statements if "FROM message_thread_map WHERE thread_id IN" in s]
        assert len(vec_lookups) == 4  # ceil(40 / 10)
        assert len(map_lookups) == 2  # ceil(14 / 10)


class TestFtsSubjectScanBound:
    """#439 review round 2: rebuilding the ``threads_fts`` subject read
    and normalized every stored subject of the thread on each upsert,
    quadratic in a long thread of long subjects. Rows and characters
    examined per rewrite are now capped."""

    def test_long_thread_of_long_subjects_is_bounded(self, db, monkeypatch):
        import time

        from src import threader as threader_mod

        subject = "Re: " + "x" * 10_000
        calls: list[int] = []
        real = threader_mod._normalize_subject

        def counting(s):
            calls.append(len(s))
            return real(s)

        monkeypatch.setattr(threader_mod, "_normalize_subject", counting)

        # ``fts_subject_text`` caps its own input too, so the counts above
        # would still pass if the upsert dropped the SQL ``substr``/``LIMIT``
        # and fetched every full subject (#478). Record the rows the upsert
        # actually fetched from the database before that cap applies.
        from src import database as database_mod

        fetched: list[list[str]] = []
        real_fts_subject_text = database_mod.fts_subject_text

        def recording(thread_subject, subjects):
            subjects = list(subjects)
            fetched.append(subjects)
            return real_fts_subject_text(thread_subject, subjects)

        monkeypatch.setattr(database_mod, "fts_subject_text", recording)
        start = time.perf_counter()
        per_upsert: list[int] = []
        messages = []
        for i in range(300):
            msg = make_message(
                message_id=f"long{i}@x",
                subject=subject,
                filepath=f"/long/{i}",
                date=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
            )
            messages.append(msg)
            before = len(calls)
            db.upsert_thread(
                make_thread(messages=[msg], thread_id="long0@x", subject="x" * 10_000),
                FAKE_EMBEDDING,
            )
            per_upsert.append(len(calls) - before)
        elapsed = time.perf_counter() - start

        assert max(per_upsert) <= 1 + threader_mod.FTS_SUBJECT_SCAN_ROWS
        assert max(calls) <= threader_mod.FTS_SUBJECT_SCAN_CHARS
        # One fetch per upsert, each capped in SQL: the last upserts see a
        # thread longer than the row cap, every subject longer than the
        # character cap.
        assert len(fetched) == 300
        assert max(len(rows) for rows in fetched) == threader_mod.FTS_SUBJECT_SCAN_ROWS
        assert max(len(s) for rows in fetched for s in rows) == (
            threader_mod.FTS_SUBJECT_SCAN_CHARS
        )
        # The work bound above is the real check. The wall-clock bound only
        # catches a gross regression: ~8 s locally, but one CI runner took
        # 69 s on unchanged code (#474), so it is set well clear of that.
        assert elapsed < 300, f"300 upserts took {elapsed:.1f}s"
        row = db._conn.execute("SELECT message_ids FROM threads").fetchone()
        assert len(json.loads(row["message_ids"])) == 300

        # The reap rewrite builds the same column from its survivors.
        from src.threader import Thread

        survivors = messages[1:]
        rebuilt = Thread(
            thread_id="long0@x",
            subject="x" * 10_000,
            participants=["alice@example.com"],
            messages=survivors,
            folder="INBOX",
            date_first=survivors[0].date,
            date_last=survivors[-1].date,
        )
        db.add_pending_deletion("/long/0", "long0@x", "long0@x")
        before = len(calls)
        assert db.reap_thread_messages(rebuilt, FAKE_EMBEDDING, ["long0@x"]) == ["/long/0"]
        assert len(calls) - before <= 1 + threader_mod.FTS_SUBJECT_SCAN_ROWS
        assert max(calls[before:]) <= threader_mod.FTS_SUBJECT_SCAN_CHARS
