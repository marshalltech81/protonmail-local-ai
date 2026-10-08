"""A rename before parse keeps the job of an indexed file (#1145).

mbsync renames a Maildir file on every flag change. When the worker
reaches a job whose file was renamed but the watcher has not yet
recorded the rename, parse raises ``FileNotFoundError``. For a file
whose path is still indexed, ``on_moved`` will only move the file's
records and its job (``update_filepath``); it enqueues nothing for the
new name. So the job waits once, without spending an attempt, whatever
its reason, instead of being dropped with the work it carries.
"""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import httpx2
import pytest
from openai import APIConnectionError
from src import attachment_indexing, extractors, main
from src.database import EMBEDDING_DIM, Database
from src.queue import (
    ERROR_CLASS_RETRYABLE,
    REASON_INITIAL_SCAN,
    REASON_ON_CREATED,
    REASON_RECOVERY,
    REASON_REEXTRACT,
    REASON_REPARSE,
    IndexingQueue,
)
from src.threader import Threader
from src.timings import TimingAggregator

from tests.conftest import make_mock_embedder

MARKER = "SYNTHETIC_RENAME_WAIT_MARKER"
_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
# The value already persisted in ``last_error`` on rows waiting when a
# new image starts. ``mcp-server`` matches it too, so it never changes.
_PERSISTED_MARKER = "FileNotFoundError: deferred until the rename is recorded"


class _FakeEvent:
    def __init__(self, src_path: str, dest_path: str):
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = False


def _write_eml(path: Path, message_id: str, *, body: str | None, attachment: bool = False) -> None:
    """A message whose subject, body and attachment carry the marker, so a
    test can show no mail text reaches the logs or ``last_error``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = (
        "From: alice@example.com\r\n"
        "To: bob@example.com\r\n"
        f"Subject: {MARKER} subject of {message_id}\r\n"
        f"Message-ID: <{message_id}>\r\n"
        "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        "MIME-Version: 1.0\r\n"
    )
    if not attachment:
        text = (
            headers + "Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n{body if body is not None else ''}\r\n"
        )
    else:
        text = (
            headers + 'Content-Type: multipart/mixed; boundary="b"\r\n'
            "\r\n--b\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n{body}\r\n"
            "--b\r\n"
            'Content-Type: text/plain; charset=utf-8; name="notes.txt"\r\n'
            'Content-Disposition: attachment; filename="notes.txt"\r\n'
            f"\r\nattached {MARKER} text\r\n"
            "--b--\r\n"
        )
    path.write_text(text, encoding="utf-8")


def _drain(db: Database, queue: IndexingQueue, embedder, *, max_passes=None) -> int:
    return main._drain_queue_batched(
        db,
        embedder,
        Threader(db),
        queue,
        batch_size=4,
        timing_aggregator=TimingAggregator(window=4),
        max_passes=max_passes,
    )


def _make_due(db: Database) -> None:
    db._conn.execute("UPDATE indexing_jobs SET next_attempt_at = '2000-01-01T00:00:00+00:00'")
    db._conn.commit()


def _jobs(db: Database) -> dict[str, sqlite3.Row]:
    return {row["filepath"]: row for row in db._conn.execute("SELECT * FROM indexing_jobs")}


def _index_one(tmp_path: Path, *, body: str | None, attachment: bool = False, max_attempts=3):
    db = Database(tmp_path / "mail.db")
    queue = IndexingQueue(db, max_attempts=max_attempts, base_backoff_seconds=0)
    path = tmp_path / "INBOX" / "cur" / "m0:2,S"
    _write_eml(
        path,
        "m0@example.com",
        body=None if body is None else f"{body} {MARKER}",
        attachment=attachment,
    )
    queue.enqueue(str(path), REASON_INITIAL_SCAN)
    _drain(db, queue, make_mock_embedder(_VECTOR))
    assert db.is_indexed(str(path))
    assert _jobs(db) == {}
    return db, queue, str(path)


# Each shape builds a job for an indexed file through the code that
# queues it in production, and returns a check that its work was done
# once the job runs on the new path.


def _shape_reparse(tmp_path, monkeypatch):
    db, queue, path = _index_one(tmp_path, body="body")
    assert queue.enqueue_reparse() == 1

    def repaired(_embedder):
        assert main._reparse_progress.reparsed == 1

    return db, queue, path, repaired


def _shape_reextract(tmp_path, monkeypatch):
    db, queue, path = _index_one(tmp_path, body="body", attachment=True)
    monkeypatch.setitem(
        extractors.EXTRACTOR_VERSIONS, "text", extractors.EXTRACTOR_VERSIONS["text"] + 1
    )
    assert main._requeue_stale_extractions(db, queue) == 1
    calls = MagicMock(wraps=attachment_indexing.extract_attachment)
    monkeypatch.setattr(attachment_indexing, "extract_attachment", calls)

    def repaired(_embedder):
        assert calls.call_count == 1

    return db, queue, path, repaired


def _shape_recovery(tmp_path, monkeypatch):
    db, queue, path = _index_one(tmp_path, body=None)  # a chunkless thread
    [thread_id] = db._conn.execute("SELECT thread_id FROM threads").fetchone()
    db.replace_thread_vector(thread_id, [0.0] * EMBEDDING_DIM)
    db._conn.commit()
    assert main._recover_zero_vector_threads(db, queue) == 1

    def repaired(embedder):
        assert embedder.embed.call_count == 1
        [vector] = db._conn.execute("SELECT embedding FROM threads_vec").fetchone()
        assert vector != bytes(4 * EMBEDDING_DIM)

    return db, queue, path, repaired


def _shape_phase2_pending(tmp_path, monkeypatch):
    """Phase 1 committed (the path is indexed) but the embedder was down,
    so the chunks were never stored and the job is retried; one attempt
    was spent by an earlier failure."""
    db = Database(tmp_path / "mail.db")
    queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
    path = str(tmp_path / "INBOX" / "cur" / "m0:2,S")
    _write_eml(Path(path), "m0@example.com", body=f"body {MARKER}")
    queue.enqueue(path, REASON_ON_CREATED)
    down = make_mock_embedder()
    down.embed.side_effect = APIConnectionError(request=httpx2.Request("POST", "http://x"))
    _drain(db, queue, down)
    assert db.is_indexed(path)
    assert db._conn.execute("SELECT COUNT(*) FROM message_chunks").fetchone()[0] == 0
    queue.mark_failed(path, stage="embed", error="EmbedResponseError")
    assert _jobs(db)[path]["attempts"] == 1

    def repaired(_embedder):
        assert db._conn.execute("SELECT COUNT(*) FROM message_chunks").fetchone()[0] > 0

    return db, queue, path, repaired


_SHAPES = {
    REASON_REPARSE: _shape_reparse,
    REASON_REEXTRACT: _shape_reextract,
    REASON_RECOVERY: _shape_recovery,
    REASON_ON_CREATED: _shape_phase2_pending,
}


class TestARenameBeforeParseKeepsTheJob:
    @pytest.mark.parametrize("reason", sorted(_SHAPES))
    def test_the_job_waits_for_the_rename_then_does_its_work(
        self, tmp_path, monkeypatch, caplog, reason
    ):
        caplog.set_level(logging.INFO)
        db, queue, path, repaired = _SHAPES[reason](tmp_path, monkeypatch)
        _make_due(db)
        before = _jobs(db)[path]
        assert before["reason"] == reason
        queue.drain_deferrals()
        old = Path(path)
        new = old.with_name(old.name + "R")
        old.rename(new)  # mbsync's rename; the watcher has not seen it yet

        caplog.clear()
        embedder = make_mock_embedder(_VECTOR)
        _drain(db, queue, embedder)
        row = _jobs(db)[path]
        assert (row["reason"], row["status"], row["last_stage"]) == (reason, "queued", "parse")
        assert row["attempts"] == before["attempts"]
        assert row["last_error"] == _PERSISTED_MARKER == main.RENAME_DEFERRED_ERROR
        wait = datetime.fromisoformat(row["next_attempt_at"]) - datetime.now(UTC)
        assert timedelta(seconds=main.RENAME_DEFER_SECS - 10) < wait
        assert wait <= timedelta(seconds=main.RENAME_DEFER_SECS)
        assert "file_missing" not in caplog.text
        # Counted on the heartbeat's ``deferrals since last heartbeat: parse=``.
        assert queue.drain_deferrals()["parse"] == 1

        main.MaildirHandler(db, queue).on_moved(_FakeEvent(str(old), str(new)))
        assert set(_jobs(db)) == {str(new)}
        _make_due(db)
        _drain(db, queue, embedder)
        assert _jobs(db) == {}
        assert db.is_indexed(str(new))
        repaired(embedder)
        assert MARKER not in caplog.text
        db.close()

    @pytest.mark.parametrize("reason", sorted(_SHAPES))
    def test_a_file_still_missing_after_the_wait_is_dropped(
        self, tmp_path, monkeypatch, caplog, reason
    ):
        caplog.set_level(logging.INFO)
        db, queue, path, _repaired = _SHAPES[reason](tmp_path, monkeypatch)
        _make_due(db)
        Path(path).unlink()
        _drain(db, queue, make_mock_embedder(_VECTOR))
        assert _jobs(db)[path]["last_error"] == _PERSISTED_MARKER
        assert "file_missing" not in caplog.text
        _make_due(db)
        _drain(db, queue, make_mock_embedder(_VECTOR))
        assert _jobs(db) == {}
        assert f"skipped: {path} reason=file_missing" in caplog.text
        assert MARKER not in caplog.text
        db.close()

    def test_a_row_waiting_before_an_upgrade_gets_no_second_wait(self, tmp_path, caplog):
        """A row deferred by the previous image carries the persisted
        marker; the new image reads it as the wait already spent."""
        caplog.set_level(logging.INFO)
        db, queue, path = _index_one(tmp_path, body="body")
        queue.enqueue(path, REASON_REPARSE)
        queue.defer(
            path,
            stage="parse",
            error=_PERSISTED_MARKER,
            error_class=ERROR_CLASS_RETRYABLE,
            delay_seconds=0,
        )
        _make_due(db)
        Path(path).unlink()
        _drain(db, queue, make_mock_embedder(_VECTOR))
        assert _jobs(db) == {}
        assert "reason=file_missing" in caplog.text
        db.close()

    def test_an_unindexed_missing_file_is_dropped_at_once(self, tmp_path, caplog):
        """Its rename reaches the queue as a fresh ``on_moved`` job, so the
        old one has nothing to wait for."""
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
        gone = str(tmp_path / "INBOX" / "cur" / "never:2,S")
        queue.enqueue(gone, REASON_ON_CREATED)
        _drain(db, queue, make_mock_embedder(_VECTOR))
        assert _jobs(db) == {}
        assert queue.drain_deferrals()["parse"] == 0
        assert f"skipped: {gone} reason=file_missing" in caplog.text
        db.close()


class TestARenameInsideTheChargedStep:
    """Pins today's outcome of a rename the watcher records while the
    message's charged parse step runs: settlement is keyed by the old
    path, so the moved row keeps the charge (#1178). Not changed by
    #1145; this test flips when #1178 is fixed."""

    def _rename_during_parse(self, monkeypatch, db, queue, path):
        real_parse = main.parse_email
        old = Path(path)
        new = old.with_name(old.name + "R")

        def parse(file, **kwargs):
            if Path(file) == old:
                old.rename(new)
                main.MaildirHandler(db, queue).on_moved(_FakeEvent(str(old), str(new)))
                raise FileNotFoundError(str(old))
            return real_parse(file, **kwargs)

        monkeypatch.setattr(main, "parse_email", parse)
        return str(new)

    def test_the_moved_row_keeps_a_phantom_attempt(self, tmp_path, monkeypatch):
        db, queue, path = _index_one(tmp_path, body="body")
        queue.enqueue(path, REASON_REEXTRACT)
        new = self._rename_during_parse(monkeypatch, db, queue, path)
        _drain(db, queue, make_mock_embedder(_VECTOR), max_passes=1)
        row = _jobs(db)[new]
        assert (row["status"], row["attempts"], row["last_stage"]) == ("queued", 1, "interrupted")
        db.close()

    def test_at_one_attempt_the_moved_row_is_dead_lettered_unrun(self, tmp_path, monkeypatch):
        db, queue, path = _index_one(tmp_path, body="body", max_attempts=1)
        queue.enqueue(path, REASON_REEXTRACT)
        new = self._rename_during_parse(monkeypatch, db, queue, path)
        _drain(db, queue, make_mock_embedder(_VECTOR))
        row = _jobs(db)[new]
        assert (row["status"], row["attempts"], row["last_stage"]) == ("dead", 1, "interrupted")
        db.close()
