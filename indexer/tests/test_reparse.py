"""In-place reparse through the job queue (#1078).

A parser change that keeps chunk IDs (new per-message columns, no new
body text) is applied to mail already indexed by queueing every indexed
file with reason ``reparse``: the migration that needs it ends with
``REPARSE_ENQUEUE_SQL``, and ``make reparse`` runs the same statement.
The worker re-parses, rewrites the per-message rows and, because the
deterministic chunk IDs already exist, makes no embedding call.
"""

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from src import main, reparse
from src.database import EMBEDDING_DIM, SCHEMA_VERSION, Database
from src.migrations import runner
from src.queue import (
    REASON_INITIAL_SCAN,
    REASON_ON_CREATED,
    REASON_REPARSE,
    REPARSE_ENQUEUE_SQL,
    IndexingQueue,
)
from src.threader import Threader
from src.timings import TimingAggregator

from tests.conftest import make_mock_embedder

MARKER = "SYNTHETIC_REPARSE_MARKER"
_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
_MIGRATIONS_DIR = Path(main.__file__).parent / "migrations"


def _write_eml(path: Path, message_id: str, body: str | None) -> None:
    """A message whose subject and body carry the marker, so a test can
    show no mail text reaches the logs or ``last_error``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "From: alice@example.com\r\n"
        "To: bob@example.com\r\n"
        f"Subject: {MARKER} subject of {message_id}\r\n"
        f"Message-ID: <{message_id}>\r\n"
        "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n{body if body is not None else ''}\r\n",
        encoding="utf-8",
    )


def _drain(db: Database, queue: IndexingQueue, embedder) -> int:
    return main._drain_queue_batched(
        db,
        embedder,
        Threader(db),
        queue,
        batch_size=4,
        timing_aggregator=TimingAggregator(window=4),
    )


def _index(tmp_path: Path, bodies: list[str | None]) -> tuple[Database, IndexingQueue, list[str]]:
    """Index one message per body through the real pipeline; ``None``
    writes an empty body (a chunkless message)."""
    db = Database(tmp_path / "mail.db")
    queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
    paths = []
    for i, body in enumerate(bodies):
        path = tmp_path / "INBOX" / "cur" / f"m{i}:2,S"
        _write_eml(path, f"m{i}@example.com", None if body is None else f"{body} {MARKER}")
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        paths.append(str(path))
    _drain(db, queue, make_mock_embedder(_VECTOR))
    assert db._conn.execute("SELECT COUNT(*) FROM indexing_jobs").fetchone()[0] == 0
    return db, queue, paths


def _jobs(db: Database) -> dict[str, sqlite3.Row]:
    return {
        row["filepath"]: row
        for row in db._conn.execute("SELECT * FROM indexing_jobs ORDER BY filepath")
    }


class TestReparseEnqueueStatement:
    def test_queues_every_indexed_file_with_the_reparse_reason(self, tmp_path):
        db, queue, paths = _index(tmp_path, ["one", "two", "three"])
        assert queue.enqueue_reparse() == 3
        jobs = _jobs(db)
        assert sorted(jobs) == sorted(paths)
        for row in jobs.values():
            assert (row["reason"], row["status"], row["attempts"]) == (REASON_REPARSE, "queued", 0)
            assert row["last_error"] is None
            # Same shape as the rows Python writes, so due-time ordering
            # and the heartbeat's age parse both work.
            due = datetime.fromisoformat(row["next_attempt_at"])
            assert due.tzinfo is not None
            assert abs((datetime.now(UTC) - due).total_seconds()) < 60
        assert len(queue.claim_batch(10)) == 3
        db.close()

    def test_existing_jobs_keep_their_state(self, tmp_path):
        """Pending, retrying and dead rows are not clobbered: pending and
        retrying ones are reparsed by their own run, and a dead row stays
        dead until ``make requeue-dead``."""
        db, queue, paths = _index(tmp_path, ["pending", "retrying", "dead", "fresh"])
        pending, retrying, dead, fresh = paths
        queue.enqueue(pending, REASON_ON_CREATED)
        queue.enqueue(retrying, REASON_INITIAL_SCAN)
        queue.mark_failed(retrying, stage="db_write", error="OperationalError")
        queue.enqueue(dead, REASON_INITIAL_SCAN)
        queue.mark_dead_terminal(dead, stage="parse", error="oversized: too large")
        before = {fp: dict(row) for fp, row in _jobs(db).items()}

        assert queue.enqueue_reparse() == 1

        after = {fp: dict(row) for fp, row in _jobs(db).items()}
        for fp in (pending, retrying, dead):
            assert after[fp] == before[fp]
        assert after[dead]["status"] == "dead"
        assert after[fresh]["reason"] == REASON_REPARSE
        # Running it again queues nothing new.
        assert queue.enqueue_reparse() == 0
        db.close()

    def test_files_never_indexed_are_not_queued(self, tmp_path):
        db = Database(tmp_path / "mail.db")
        assert IndexingQueue(db).enqueue_reparse() == 0
        assert _jobs(db) == {}
        db.close()

    def test_migrations_that_queue_a_reparse_use_the_shared_statement(self):
        """A migration copies the statement verbatim, so the conflict
        rule tested above is the one it runs."""
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            text = path.read_text(encoding="utf-8")
            if f"'{REASON_REPARSE}'" in text:
                assert REPARSE_ENQUEUE_SQL in text, path.name

    def test_the_documented_statement_is_the_shared_one(self):
        """docs/architecture.md shows the statement later migrations copy."""
        doc = (Path(__file__).resolve().parents[2] / "docs" / "architecture.md").read_text(
            encoding="utf-8"
        )
        assert REPARSE_ENQUEUE_SQL in doc


class TestReparseAfterAParserChange:
    """Acceptance test of #1078: a parser change that adds one column,
    shipped as a migration ending with the shared statement, fills the
    column on every indexed message without an embedding call."""

    def test_every_message_gains_the_column_with_zero_embedding_calls(self, tmp_path, monkeypatch):
        bodies: list[str | None] = [f"body {i}" for i in range(5)] + [None]
        db, queue, _paths = _index(tmp_path, bodies)
        # One thread each, so the chunkless message's thread has no chunks.
        assert db._conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == len(bodies)
        chunks_before = db._conn.execute(
            "SELECT chunk_id FROM message_chunks ORDER BY chunk_id"
        ).fetchall()
        vectors_before = db._conn.execute(
            "SELECT thread_id, embedding FROM threads_vec ORDER BY thread_id"
        ).fetchall()

        # The "parser change": a migration adds a test-only column and
        # queues the reparse; the new upsert writes the column.
        migrations = tmp_path / "migrations"
        migrations.mkdir()
        (migrations / f"{SCHEMA_VERSION + 1:04d}_test_reparse_column.sql").write_text(
            "ALTER TABLE messages ADD COLUMN test_parsed TEXT;\n\n" + REPARSE_ENQUEUE_SQL + "\n",
            encoding="utf-8",
        )
        runner.apply_pending(
            db._conn,
            current_version=SCHEMA_VERSION,
            target_version=SCHEMA_VERSION + 1,
            migration_dir=migrations,
        )
        real_upsert = db.upsert_thread

        def upsert_with_column(thread, embedding):
            real_upsert(thread, embedding)
            for msg in thread.messages:
                db._conn.execute(
                    "UPDATE messages SET test_parsed = 'yes' WHERE claimant_id = ?",
                    (msg.claimant_id,),
                )
            db._conn.commit()

        monkeypatch.setattr(db, "upsert_thread", upsert_with_column)
        embedder = make_mock_embedder(_VECTOR)

        assert _drain(db, queue, embedder) == len(bodies)

        assert embedder.embed_batch.call_count == 0
        assert embedder.embed.call_count == 0
        counts = db._conn.execute("SELECT COUNT(*), COUNT(test_parsed) FROM messages").fetchone()
        assert tuple(counts) == (len(bodies), len(bodies))
        assert _jobs(db) == {}
        assert (
            db._conn.execute("SELECT chunk_id FROM message_chunks ORDER BY chunk_id").fetchall()
            == chunks_before
        )
        # The chunkless message's subject-fallback vector is kept, not
        # embedded again.
        assert (
            db._conn.execute(
                "SELECT thread_id, embedding FROM threads_vec ORDER BY thread_id"
            ).fetchall()
            == vectors_before
        )
        db.close()

    def test_a_chunkless_thread_left_at_zero_is_still_repaired(self, tmp_path):
        """The fallback is skipped only when the thread kept a real vector:
        one stuck at the zero placeholder gets its subject embedded."""
        db, queue, _paths = _index(tmp_path, [None])
        [thread_id] = db._conn.execute("SELECT thread_id FROM threads").fetchone()
        db.replace_thread_vector(thread_id, [0.0] * EMBEDDING_DIM)
        db._conn.commit()
        queue.enqueue_reparse()
        embedder = make_mock_embedder(_VECTOR)
        _drain(db, queue, embedder)
        assert embedder.embed.call_count == 1
        db.close()

    def test_a_failed_reparse_keeps_mail_out_of_last_error_and_logs(
        self, tmp_path, monkeypatch, caplog
    ):
        caplog.set_level(logging.DEBUG)
        db, queue, paths = _index(tmp_path, ["body"])
        queue.enqueue_reparse()

        def boom(*_a, **_kw):
            raise ValueError(f"{MARKER} quoted by a parser")

        monkeypatch.setattr(main, "parse_email", boom)
        caplog.clear()
        _drain(db, queue, make_mock_embedder(_VECTOR))
        row = _jobs(db)[paths[0]]
        assert row["reason"] == REASON_REPARSE
        assert row["last_error"] == "ValueError"
        assert MARKER not in caplog.text
        db.close()


class TestSenderAmbiguousMigration:
    """#1144: the real v1 -> v2 migration leaves every message NULL (not
    assessed), queues the reparse, and the reparse fills 0 / 1 with no
    embedding call; a dead-lettered job keeps its message NULL."""

    def test_backfill_through_the_reparse(self, tmp_path, caplog):
        caplog.set_level(logging.DEBUG)
        db, queue, paths = _index(tmp_path, ["one", "three"])
        plain, dead = paths
        # A message with a repeated From, indexed as a v1 indexer left it.
        repeated = str(tmp_path / "INBOX" / "cur" / "rep:2,S")
        _write_eml(Path(repeated), "rep@example.com", f"two {MARKER}")
        text = Path(repeated).read_text(encoding="utf-8")
        Path(repeated).write_text(
            text.replace("To: ", "From: SYNTHETIC_REPARSE_MARKER@example.com\r\nTo: ", 1),
            encoding="utf-8",
        )
        queue.enqueue(repeated, REASON_ON_CREATED)
        _drain(db, queue, make_mock_embedder(_VECTOR))
        queue.enqueue(dead, REASON_INITIAL_SCAN)
        queue.mark_dead_terminal(dead, stage="parse", error="oversized: too large")
        chunks_before = db._conn.execute(
            "SELECT chunk_id FROM message_chunks ORDER BY chunk_id"
        ).fetchall()
        db._conn.execute("ALTER TABLE attachment_extractions DROP COLUMN ocr_pages_skipped")
        db._conn.execute("DROP TABLE message_participant_names")
        db._conn.execute(
            "CREATE INDEX idx_message_participants_address_name "
            "ON message_participants(address, name)"
        )
        _drop_v5_columns(db)
        db._conn.execute("ALTER TABLE messages DROP COLUMN participant_names_complete")
        db._conn.execute("ALTER TABLE messages DROP COLUMN sender_ambiguous")
        db._conn.execute("UPDATE schema_version SET version = 1")
        db._conn.commit()
        db.close()

        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
        flags = db._conn.execute("SELECT sender_ambiguous FROM messages").fetchall()
        assert [r[0] for r in flags] == [None, None, None]
        jobs = _jobs(db)
        assert {fp: (r["reason"], r["status"]) for fp, r in jobs.items()} == {
            plain: (REASON_REPARSE, "queued"),
            repeated: (REASON_REPARSE, "queued"),
            dead: (REASON_INITIAL_SCAN, "dead"),
        }

        embedder = make_mock_embedder(_VECTOR)
        _drain(db, queue, embedder)
        assert embedder.embed_batch.call_count == 0
        assert embedder.embed.call_count == 0
        by_path = {
            r["filepath"]: r["sender_ambiguous"]
            for r in db._conn.execute("SELECT filepath, sender_ambiguous FROM messages")
        }
        assert by_path == {plain: 0, repeated: 1, dead: None}
        assert (
            db._conn.execute("SELECT chunk_id FROM message_chunks ORDER BY chunk_id").fetchall()
            == chunks_before
        )
        assert MARKER not in caplog.text
        db.close()


class TestParticipantNamesMigration:
    """#1140: the real v3 -> v4 migration seeds
    ``message_participant_names`` with each participant's stored first
    name, leaves ``messages.participant_names_complete`` NULL (unknown)
    on every message, dead letters included, and queues the reparse,
    which adds the other names and sets the flag with no embedding call,
    chunkless threads included."""

    def test_backfill_through_the_reparse(self, tmp_path, caplog, monkeypatch):
        caplog.set_level(logging.DEBUG)
        import tests.test_reparse as this

        original = this._write_eml

        def two_names(path: Path, message_id: str, body: str | None) -> None:
            # Every message writes bob under two names.
            original(path, message_id, body)
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text.replace(
                    "To: bob@example.com", f"To: Bob <bob@example.com>, {MARKER} <bob@example.com>"
                ),
                encoding="utf-8",
            )

        monkeypatch.setattr(this, "_write_eml", two_names)
        db, queue, paths = _index(tmp_path, ["one", None, "three"])
        all_names = {
            (r["claimant_id"], r["name"])
            for r in db._conn.execute("SELECT claimant_id, name FROM message_participant_names")
        }
        assert {name for _, name in all_names} == {"Bob", MARKER}
        dead = paths[2]
        [dead_claimant] = db._conn.execute(
            "SELECT claimant_id FROM messages WHERE filepath = ?", (dead,)
        ).fetchone()
        queue.enqueue(dead, REASON_INITIAL_SCAN)
        queue.mark_dead_terminal(dead, stage="parse", error="oversized: too large")
        chunks_before = db._conn.execute(
            "SELECT chunk_id FROM message_chunks ORDER BY chunk_id"
        ).fetchall()
        vectors_before = db._conn.execute(
            "SELECT thread_id, embedding FROM threads_vec ORDER BY thread_id"
        ).fetchall()
        # The v3 shape: no names table, the old (address, name) index.
        db._conn.execute("DROP TABLE message_participant_names")
        db._conn.execute(
            "CREATE INDEX idx_message_participants_address_name "
            "ON message_participants(address, name)"
        )
        _drop_v5_columns(db)
        db._conn.execute("ALTER TABLE messages DROP COLUMN participant_names_complete")
        db._conn.execute("UPDATE schema_version SET version = 3")
        db._conn.commit()
        db.close()

        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
        flags = db._conn.execute("SELECT participant_names_complete FROM messages").fetchall()
        assert [r[0] for r in flags] == [None, None, None]
        # Seeded with the first names only, and every live file queued;
        # the dead-lettered job stays dead.
        seeded = {
            (r["claimant_id"], r["name"])
            for r in db._conn.execute("SELECT claimant_id, name FROM message_participant_names")
        }
        assert {name for _, name in seeded} == {"Bob"}
        assert {fp: (r["reason"], r["status"]) for fp, r in _jobs(db).items()} == {
            paths[0]: (REASON_REPARSE, "queued"),
            paths[1]: (REASON_REPARSE, "queued"),
            dead: (REASON_INITIAL_SCAN, "dead"),
        }

        embedder = make_mock_embedder(_VECTOR)
        assert _drain(db, queue, embedder) == 2
        assert embedder.embed_batch.call_count == 0
        assert embedder.embed.call_count == 0
        after = {
            (r["claimant_id"], r["name"])
            for r in db._conn.execute("SELECT claimant_id, name FROM message_participant_names")
        }
        # The reparsed messages gain every name and a known flag; the
        # dead-lettered one keeps its first name and stays unknown.
        assert after == {(c, n) for c, n in all_names if c != dead_claimant or n == "Bob"}
        by_path = {
            r["filepath"]: r["participant_names_complete"]
            for r in db._conn.execute("SELECT filepath, participant_names_complete FROM messages")
        }
        assert by_path == {paths[0]: 1, paths[1]: 1, dead: None}
        assert (
            db._conn.execute("SELECT chunk_id FROM message_chunks ORDER BY chunk_id").fetchall()
            == chunks_before
        )
        assert (
            db._conn.execute(
                "SELECT thread_id, embedding FROM threads_vec ORDER BY thread_id"
            ).fetchall()
            == vectors_before
        )
        assert MARKER not in caplog.text
        db.close()


# The columns v5 adds to ``messages`` (#1086).
_V5_COLUMNS = (
    "subject_complete",
    "from_addresses_complete",
    "to_addresses_complete",
    "cc_addresses_complete",
    "attachments_manifest_complete",
    "body_complete",
    "caps_json",
)


def _drop_v5_columns(db: Database) -> None:
    """The v4 shape's missing columns: v5's and v6's (#1242)."""
    for column in _V5_COLUMNS:
        db._conn.execute(f"ALTER TABLE messages DROP COLUMN {column}")
    for table, column in (
        ("attachments", "text_complete"),
        ("attachments", "text_extractor"),
        ("attachment_extractions", "text_complete"),
    ):
        db._conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def _completeness(db: Database) -> dict[str, tuple]:
    return {
        row["filepath"]: tuple(row)[1:]
        for row in db._conn.execute(f"SELECT filepath, {', '.join(_V5_COLUMNS)} FROM messages")
    }


_COMPLETE = (1, 1, 1, 1, 1, 1, "{}")


class TestCompletenessMigration:
    """#1086: the real v4 -> v5 migration leaves every message's
    completeness NULL (not assessed), dead letters included, and queues
    the reparse, which fills it with no embedding call."""

    def test_backfill_through_the_reparse(self, tmp_path, caplog):
        caplog.set_level(logging.DEBUG)
        db, queue, paths = _index(tmp_path, ["one", None, "three"])
        live, chunkless, dead = paths
        assert set(_completeness(db).values()) == {_COMPLETE}
        queue.enqueue(dead, REASON_INITIAL_SCAN)
        queue.mark_dead_terminal(dead, stage="parse", error="oversized: too large")
        chunks_before = db._conn.execute(
            "SELECT chunk_id FROM message_chunks ORDER BY chunk_id"
        ).fetchall()
        _drop_v5_columns(db)
        db._conn.execute("UPDATE schema_version SET version = 4")
        db._conn.commit()
        db.close()

        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
        unknown = (None,) * len(_V5_COLUMNS)
        assert _completeness(db) == {live: unknown, chunkless: unknown, dead: unknown}
        assert {fp: (r["reason"], r["status"]) for fp, r in _jobs(db).items()} == {
            live: (REASON_REPARSE, "queued"),
            chunkless: (REASON_REPARSE, "queued"),
            dead: (REASON_INITIAL_SCAN, "dead"),
        }

        embedder = make_mock_embedder(_VECTOR)
        assert _drain(db, queue, embedder) == 2
        assert embedder.embed_batch.call_count == 0
        assert embedder.embed.call_count == 0
        # A chunkless body is complete: there was nothing to lose.
        assert _completeness(db) == {live: _COMPLETE, chunkless: _COMPLETE, dead: unknown}
        assert (
            db._conn.execute("SELECT chunk_id FROM message_chunks ORDER BY chunk_id").fetchall()
            == chunks_before
        )
        assert MARKER not in caplog.text
        db.close()


class TestBodyCompletePublication:
    """#1086: ``body_complete`` describes the committed body chunks, so
    phase 2c stores it in their transaction and phase 1 resets it."""

    def _body_complete(self, db, path):
        return db._conn.execute(
            "SELECT body_complete FROM messages WHERE filepath = ?", (path,)
        ).fetchone()[0]

    def test_a_capped_body_is_stored_incomplete(self, tmp_path, monkeypatch):
        from src import parser

        monkeypatch.setattr(parser, "MAX_BODY_TEXT_PARTS", 1)
        path = tmp_path / "INBOX" / "cur" / "parts:2,S"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            b"From: a@example.com\r\nMessage-ID: <parts@example.com>\r\n"
            b"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nfirst\r\n"
            b"--b\r\nContent-Type: text/plain\r\n\r\nSYNTHETIC_REPARSE_MARKER\r\n--b--\r\n"
        )
        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        _drain(db, queue, make_mock_embedder(_VECTOR))
        assert self._body_complete(db, str(path)) == 0
        caps = db._conn.execute("SELECT caps_json FROM messages").fetchone()[0]
        assert caps == '{"body_parts": 1}'
        db.close()

    def test_a_failed_phase_2c_leaves_it_unknown(self, tmp_path, monkeypatch, caplog):
        """Phase 1 commits the message row and resets the flag; a 2c
        failure after the flag was written rolls it back with the
        chunks, so a message once complete is not left claiming chunks
        this pass did not commit."""
        caplog.set_level(logging.DEBUG)
        db, queue, paths = _index(tmp_path, ["one"])
        assert self._body_complete(db, paths[0]) == 1
        calls = []
        real = db.set_body_complete

        def then_fail(claimant_id, complete):
            calls.append(complete)
            real(claimant_id, complete)

        def boom(*_a, **_kw):
            raise sqlite3.OperationalError("injected")

        monkeypatch.setattr(db, "set_body_complete", then_fail)
        monkeypatch.setattr(db, "replace_thread_vector", boom)
        queue.enqueue_reparse()
        _drain(db, queue, make_mock_embedder(_VECTOR))
        # Every attempt wrote the flag and rolled it back; the job ends
        # dead-lettered with the flag unknown.
        assert calls == [True] * 3
        assert self._body_complete(db, paths[0]) is None
        job = _jobs(db)[paths[0]]
        assert (job["status"], job["last_error"]) == ("dead", "OperationalError")
        # The phase 1 flags are committed: they describe the message row.
        assert _completeness(db)[paths[0]][:5] == (1, 1, 1, 1, 1)

        monkeypatch.undo()
        assert queue.requeue_dead() == 1
        _drain(db, queue, make_mock_embedder(_VECTOR))
        assert self._body_complete(db, paths[0]) == 1
        assert MARKER not in caplog.text
        db.close()


def _lines(caplog, prefix: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith(prefix)]


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(main, "_monotonic", lambda: now["t"])
    return now


class TestReparseVisibility:
    def test_heartbeat_counts_the_reparse_backlog(self, tmp_path):
        db, queue, paths = _index(tmp_path, ["a", "b", "c"])
        assert queue.heartbeat_counts()["reparse"] == 0
        queue.enqueue_reparse()
        queue.mark_dead_terminal(paths[0], stage="parse", error="oversized: too large")
        counts = queue.heartbeat_counts()
        assert (counts["pending"], counts["reparse"], counts["reparse_dead"]) == (2, 2, 1)
        db.close()

    def test_progress_per_interval_then_one_completion_line(self, tmp_path, caplog, clock):
        caplog.set_level(logging.INFO)
        db, queue, paths = _index(tmp_path, ["a", "b", "c"])
        caplog.clear()
        main._maybe_log_queue_heartbeat(queue)
        assert _lines(caplog, "reparse") == []

        queue.enqueue_reparse()
        clock["t"] += main.QUEUE_HEARTBEAT_INTERVAL_SECS
        main._maybe_log_queue_heartbeat(queue)
        [line] = _lines(caplog, "reparse")
        assert line.levelno == logging.INFO
        assert line.getMessage() == ("reparse: remaining=3 reparsed_since_last_heartbeat=0 dead=0")

        # Two reparse, one dead-letters.
        queue.mark_dead_terminal(paths[2], stage="parse", error="oversized: too large")
        _drain(db, queue, make_mock_embedder(_VECTOR))
        caplog.clear()
        clock["t"] += main.QUEUE_HEARTBEAT_INTERVAL_SECS
        main._maybe_log_queue_heartbeat(queue)
        [line] = _lines(caplog, "reparse")
        assert line.levelno == logging.WARNING
        assert line.getMessage() == (
            "reparse complete: 2 message(s) reparsed since the indexer started, "
            "1 dead-lettered (make requeue-dead retries them)"
        )

        caplog.clear()
        clock["t"] += main.QUEUE_HEARTBEAT_INTERVAL_SECS
        main._maybe_log_queue_heartbeat(queue)
        assert _lines(caplog, "reparse") == []
        assert MARKER not in caplog.text
        db.close()

    def test_clean_completion_logs_at_info(self, tmp_path, caplog, clock):
        caplog.set_level(logging.INFO)
        db, queue, _paths = _index(tmp_path, ["a"])
        queue.enqueue_reparse()
        clock["t"] += main.QUEUE_HEARTBEAT_INTERVAL_SECS
        main._maybe_log_queue_heartbeat(queue)
        assert len(_lines(caplog, "reparse: remaining=1 ")) == 1
        _drain(db, queue, make_mock_embedder(_VECTOR))
        caplog.clear()
        clock["t"] += main.QUEUE_HEARTBEAT_INTERVAL_SECS
        main._maybe_log_queue_heartbeat(queue)
        [line] = _lines(caplog, "reparse")
        assert line.levelno == logging.INFO
        assert line.getMessage() == (
            "reparse complete: 1 message(s) reparsed since the indexer started, 0 dead-lettered"
        )
        db.close()


class TestReparseCommand:
    def test_queues_and_reports_counts(self, tmp_path, monkeypatch, capsys):
        db, queue, paths = _index(tmp_path, ["a", "b"])
        queue.enqueue(paths[0], REASON_ON_CREATED)
        db.close()
        monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "mail.db"))

        assert reparse.main([]) == 0

        out = capsys.readouterr().out
        assert out == (
            "Queued 1 indexed message(s) for reparse. Messages that already "
            "had a job keep it; dead-lettered ones stay dead until make requeue-dead.\n"
        )
        assert MARKER not in out
        db = Database(tmp_path / "mail.db")
        assert {row["reason"] for row in _jobs(db).values()} == {REASON_ON_CREATED, REASON_REPARSE}
        db.close()

    def test_missing_database_fails_without_creating_one(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "absent.db"
        monkeypatch.setenv("SQLITE_PATH", str(db_path))
        assert reparse.main([]) == 1
        assert not db_path.exists()
        assert "nothing to reparse" in capsys.readouterr().err


class TestReviewRound1:
    """Codex review round 1 on #1143."""

    def test_a_reparse_drained_between_heartbeats_still_logs_completion(
        self, tmp_path, caplog, clock
    ):
        """No heartbeat saw the backlog: the worker's own count still
        leads to one completion line."""
        caplog.set_level(logging.INFO)
        db, queue, _paths = _index(tmp_path, ["a", "b"])
        queue.enqueue_reparse()
        _drain(db, queue, make_mock_embedder(_VECTOR))
        caplog.clear()
        clock["t"] += main.QUEUE_HEARTBEAT_INTERVAL_SECS
        main._maybe_log_queue_heartbeat(queue)
        [line] = _lines(caplog, "reparse")
        assert line.getMessage() == (
            "reparse complete: 2 message(s) reparsed since the indexer started, 0 dead-lettered"
        )
        db.close()

    def test_an_all_dead_reparse_between_heartbeats_still_logs_completion(
        self, tmp_path, monkeypatch, caplog, clock
    ):
        caplog.set_level(logging.INFO)
        db, _queue, _paths = _index(tmp_path, ["a"])
        queue = IndexingQueue(db, max_attempts=1, base_backoff_seconds=0)
        queue.enqueue_reparse()

        def boom(*_a, **_kw):
            raise ValueError(MARKER)

        monkeypatch.setattr(main, "parse_email", boom)
        _drain(db, queue, make_mock_embedder(_VECTOR))
        caplog.clear()
        clock["t"] += main.QUEUE_HEARTBEAT_INTERVAL_SECS
        main._maybe_log_queue_heartbeat(queue)
        [line] = _lines(caplog, "reparse")
        assert line.levelno == logging.WARNING
        assert line.getMessage().startswith("reparse complete: 0 message(s) reparsed")
        assert "1 dead-lettered" in line.getMessage()
        assert MARKER not in caplog.text
        db.close()

    def test_a_rename_not_yet_recorded_keeps_the_reparse_job(self, tmp_path):
        """The file was renamed on disk but ``on_moved`` has not moved its
        records yet: the job waits for the rename instead of being
        dropped, then reparses the new path."""
        db, queue, paths = _index(tmp_path, ["a"])
        queue.enqueue_reparse()
        old = Path(paths[0])
        new = old.with_name(old.name + "R")
        old.rename(new)

        _drain(db, queue, make_mock_embedder(_VECTOR))
        row = _jobs(db)[str(old)]
        assert (row["reason"], row["attempts"], row["last_stage"]) == (REASON_REPARSE, 0, "parse")
        assert row["last_error"] == main.RENAME_DEFERRED_ERROR

        db.update_filepath(str(old), str(new))  # what on_moved does
        db._conn.execute("UPDATE indexing_jobs SET next_attempt_at = '2000-01-01T00:00:00+00:00'")
        db._conn.commit()
        embedder = make_mock_embedder(_VECTOR)
        _drain(db, queue, embedder)
        assert _jobs(db) == {}
        assert main._reparse_progress.reparsed == 1
        assert embedder.embed_batch.call_count == 0
        db.close()

    def test_a_file_still_missing_after_the_wait_is_dropped(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        db, queue, paths = _index(tmp_path, ["a"])
        queue.enqueue_reparse()
        Path(paths[0]).unlink()
        _drain(db, queue, make_mock_embedder(_VECTOR))
        assert _jobs(db)[paths[0]]["last_error"] == main.RENAME_DEFERRED_ERROR
        assert "file_missing" not in caplog.text
        # Still missing once the wait is over: gone, so dropped.
        db._conn.execute("UPDATE indexing_jobs SET next_attempt_at = '2000-01-01T00:00:00+00:00'")
        db._conn.commit()
        _drain(db, queue, make_mock_embedder(_VECTOR))
        assert _jobs(db) == {}
        assert "reason=file_missing" in caplog.text
        db.close()
