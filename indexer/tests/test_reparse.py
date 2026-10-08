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
