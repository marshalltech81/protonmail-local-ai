"""#1289: the startup extraction sweep (``main._requeue_stale_extractions``)
reads its three per-occurrence queries in batches of ``SWEEP_FETCH_ROWS``
under the database lock and keeps only qualifying filepaths, so one
message carrying thousands of attachment occurrences with sender-chosen
labels and sizes costs the sweep one filepath, not thousands of rows.
Enqueue counts, the pending and dead-letter exclusions and the log line
are unchanged."""

import logging
import sqlite3
import threading

import pytest
from src import database, main
from src.database import EMBEDDING_DIM, Database
from src.extractors import NO_EXTRACTOR_ERROR, OCR_DISABLED_ERROR
from src.queue import REASON_INITIAL_SCAN, REASON_REEXTRACT, IndexingQueue

from tests.conftest import make_message, make_thread

# Over two fetch batches per heavy message at the default batch size.
OCCURRENCES = 2500
MAX_BYTES = 25 * 1024 * 1024
MARKER = "SYNTHETIC_1289_MARKER"


class _CursorSpy:
    """Wraps a cursor to record each ``fetchmany`` batch's row count,
    whether ``fetchall`` ran and whether the cursor was closed."""

    def __init__(self, cursor: sqlite3.Cursor, sql: str):
        self._cursor = cursor
        self.sql = sql
        self.batches: list[int] = []
        self.fetchall_called = False
        self.closed = False

    def fetchmany(self, size):
        rows = self._cursor.fetchmany(size)
        self.batches.append(len(rows))
        return rows

    def fetchall(self):
        self.fetchall_called = True
        return self._cursor.fetchall()

    def close(self):
        self.closed = True
        self._cursor.close()

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _ConnSpy:
    """Delegates to the real connection and wraps every ``execute``
    cursor."""

    def __init__(self, conn: sqlite3.Connection):
        self.real = conn
        self.cursors: list[_CursorSpy] = []

    def execute(self, sql, params=()):
        spy = _CursorSpy(self.real.execute(sql, params), sql)
        self.cursors.append(spy)
        return spy

    def __getattr__(self, name):
        return getattr(self.real, name)

    def sweep_cursors(self) -> list[_CursorSpy]:
        """The cursors of the three per-occurrence sweep queries, in the
        order they ran."""
        return [
            c
            for c in self.cursors
            if "JOIN message_thread_map m ON m.claimant_id = a.claimant_id" in c.sql
            and ("'unsupported'" in c.sql or "'too_large'" in c.sql)
        ]


def _message(db: Database, name: str) -> str:
    filepath = f"/maildir/INBOX/cur/{name}"
    msg = make_message(message_id=f"{name}@example.com", filepath=filepath)
    db.upsert_thread(make_thread(messages=[msg], thread_id=f"t-{name}"), [0.1] * EMBEDDING_DIM)
    return filepath


def _occurrences(db: Database, filepath: str, rows: list[tuple]) -> None:
    """``rows``: (attachment_id, filename, MIME type, size, module,
    status, error) per occurrence of the message at ``filepath``."""
    claimant = db._conn.execute(
        "SELECT claimant_id FROM message_thread_map WHERE filepath = ?", (filepath,)
    ).fetchone()["claimant_id"]
    db._conn.executemany(
        "INSERT INTO attachments (attachment_occurrence_id, claimant_id, attachment_id, "
        "thread_id, filename, content_type, size_bytes, seen_at, extractor_module) "
        "SELECT ?, claimant_id, ?, thread_id, ?, ?, ?, '2026-01-01', ? "
        "FROM message_thread_map WHERE claimant_id = ?",
        [
            (f"{filepath}#{i}", aid, name, ctype, size, module, claimant)
            for i, (aid, name, ctype, size, module, _s, _e) in enumerate(rows)
        ],
    )
    db._conn.executemany(
        "INSERT OR IGNORE INTO attachment_extractions (attachment_id, extractor_module, "
        "extraction_status, extractor, extracted_text, extraction_error, extracted_at) "
        "VALUES (?, ?, ?, NULL, NULL, ?, '2026-01-01')",
        [(aid, module, status, error) for aid, _n, _c, _z, module, status, error in rows],
    )
    db._conn.commit()


def _no_extractor(attachment_id: str, filename: str) -> tuple:
    return (
        attachment_id,
        filename,
        "application/octet-stream",
        40_000,
        "",
        "unsupported",
        NO_EXTRACTOR_ERROR,
    )


def _too_large(attachment_id: str, size: int) -> tuple:
    return (attachment_id, "archive.zip", "application/zip", size, "", "too_large", None)


def _ocr_disabled(attachment_id: str, filename: str) -> tuple:
    return (
        attachment_id,
        filename,
        "image/png",
        90_000,
        "image",
        "unsupported",
        OCR_DISABLED_ERROR,
    )


# An occurrence whose extension now routes to the image module.
_HEIC = _no_extractor("heic-bytes", "IMG_0001.HEIC")

_SHAPES = {
    # Distinct sender labels, none routed, plus one that now is: the
    # message qualifies through one occurrence among thousands.
    "noext_mixed": [
        _no_extractor(f"nx-{i}", f"{MARKER}-{i:05d}-statement.bin") for i in range(OCCURRENCES)
    ]
    + [_HEIC],
    # One label and payload repeated: no occurrence qualifies.
    "noext_dup": [_no_extractor("dup-bytes", "invoice.bin")] * OCCURRENCES,
    # Every occurrence qualifies once OCR is on.
    "ocr": [_ocr_disabled(f"img-{i}", f"scan-{i:05d}.png") for i in range(OCCURRENCES)],
    # Distinct sizes, all still over the cap.
    "big_over": [_too_large(f"big-{i}", MAX_BYTES + 1 + i) for i in range(OCCURRENCES)],
    # One size repeated over the cap.
    "big_dup": [_too_large("big-dup", MAX_BYTES + 4096)] * OCCURRENCES,
    # Over the cap, plus one occurrence exactly at it.
    "big_mixed": [_too_large(f"bm-{i}", MAX_BYTES + 1 + i) for i in range(OCCURRENCES)]
    + [_too_large("bm-fits", MAX_BYTES)],
    # Qualifying, but dead-lettered or already pending.
    "dead": [_HEIC],
    "pending": [_HEIC],
}


@pytest.fixture
def mailbox(tmp_path):
    """One message per shape; yields the db, queue and filepaths."""
    db = Database(tmp_path / "mail.db")
    queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
    paths = {}
    for name, rows in _SHAPES.items():
        paths[name] = _message(db, name)
        _occurrences(db, paths[name], rows)
    queue.enqueue(paths["dead"], REASON_INITIAL_SCAN)
    queue.mark_dead_terminal(paths["dead"], stage="parse", error="x")
    queue.enqueue(paths["pending"], REASON_INITIAL_SCAN)
    yield db, queue, paths
    db.close()


def _configure(monkeypatch, *, ocr: bool) -> None:
    monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
    monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", ocr)
    monkeypatch.setattr(main, "INDEXER_ATTACHMENT_MAX_BYTES", MAX_BYTES)


def _spy(db: Database, monkeypatch) -> _ConnSpy:
    spy = _ConnSpy(db._conn)
    monkeypatch.setattr(db, "_conn", spy)
    return spy


def test_each_query_keeps_one_filepath_per_qualifying_message(mailbox):
    """Retained memory follows the qualifying messages: each method
    returns their filepaths, not one row per occurrence."""
    db, _queue, paths = mailbox
    rerun = main._occurrence_reruns_extraction
    assert db.find_no_extractor_attachment_filepaths(rerun) == {
        paths["noext_mixed"],
        paths["dead"],
        paths["pending"],
    }
    assert db.find_ocr_disabled_attachment_filepaths(rerun) == {paths["ocr"]}
    assert db.find_fitting_too_large_attachment_filepaths(MAX_BYTES) == {paths["big_mixed"]}
    # The size test runs in SQL, at too_large_fits' boundary (<=).
    assert db.find_fitting_too_large_attachment_filepaths(MAX_BYTES - 1) == set()


def test_sweep_reads_bounded_batches_and_closes_its_cursors(mailbox, monkeypatch):
    db, queue, _paths = mailbox
    _configure(monkeypatch, ocr=True)
    spy = _spy(db, monkeypatch)

    main._requeue_stale_extractions(db, queue)

    no_extractor, too_large, ocr = spy.sweep_cursors()
    assert "'too_large'" in too_large.sql
    # Every occurrence each query matches is inspected; the too-large
    # size test runs in SQL, so that query returns only the fitting one.
    assert sum(no_extractor.batches) == (OCCURRENCES + 1) + OCCURRENCES + 2
    assert sum(ocr.batches) == OCCURRENCES
    assert sum(too_large.batches) == 1
    for cursor in (no_extractor, too_large, ocr):
        # Never more than SWEEP_FETCH_ROWS rows held at once, never
        # fetchall, and the cursor closed.
        assert max(cursor.batches) <= database.SWEEP_FETCH_ROWS
        assert not cursor.fetchall_called
        assert cursor.closed
        assert "ORDER BY" not in cursor.sql
    assert len(no_extractor.batches) > 2


def test_sweep_enqueue_counts_exclusions_and_log_line(mailbox, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    db, queue, paths = mailbox
    _configure(monkeypatch, ocr=True)
    caplog.clear()

    assert main._requeue_stale_extractions(db, queue) == 3
    queued = {
        r["filepath"]: r["reason"]
        for r in db._conn.execute(
            "SELECT filepath, reason FROM indexing_jobs WHERE status = 'queued'"
        )
    }
    assert queued == {
        paths["noext_mixed"]: REASON_REEXTRACT,
        paths["ocr"]: REASON_REEXTRACT,
        paths["big_mixed"]: REASON_REEXTRACT,
        # Left alone: already pending.
        paths["pending"]: REASON_INITIAL_SCAN,
    }
    assert queue.is_dead(paths["dead"])
    lines = [r for r in caplog.records if r.getMessage().startswith("re-queued ")]
    assert len(lines) == 1
    assert lines[0].levelno == logging.WARNING
    assert lines[0].getMessage() == (
        "re-queued 3 of 5 message(s) (0 for a missing text-completeness record) "
        "whose attachments were extracted by an older extractor version (none), skipped "
        "while OCR was off, had no extractor, now fit under "
        "INDEXER_ATTACHMENT_MAX_BYTES, or were deferred by the per-message extraction "
        "budget; 1 already pending, skipped 1 dead-lettered "
        "(run make requeue-dead to refresh them)."
    )
    assert MARKER not in caplog.text


@pytest.mark.parametrize(("stamp", "queued"), [(None, 3), ("image@5", 2)])
def test_ocr_off_queues_only_unstamped_ocr_disabled_rows(mailbox, monkeypatch, stamp, queued):
    """#1415: with OCR off the "OCR disabled" query still runs, in the same
    bounded batches, and qualifies only an unstamped image row; a stamped
    one waits for OCR."""
    db, queue, paths = mailbox
    db._conn.execute(
        "UPDATE attachment_extractions SET extractor = ? WHERE extraction_error = ?",
        (stamp, OCR_DISABLED_ERROR),
    )
    db._conn.commit()
    _configure(monkeypatch, ocr=False)
    spy = _spy(db, monkeypatch)
    assert main._requeue_stale_extractions(db, queue) == queued
    cursors = spy.sweep_cursors()
    assert len(cursors) == 3
    ocr = cursors[2]
    assert sum(ocr.batches) == OCCURRENCES
    assert max(ocr.batches) <= database.SWEEP_FETCH_ROWS
    assert ocr.closed and not ocr.fetchall_called
    found = {
        r["filepath"]
        for r in db._conn.execute("SELECT filepath FROM indexing_jobs WHERE status = 'queued'")
    }
    assert (paths["ocr"] in found) is (stamp is None)


def test_cursor_closed_and_lock_released_when_the_predicate_raises(mailbox, monkeypatch):
    db, _queue, _paths = mailbox
    spy = _spy(db, monkeypatch)

    def fails(_row):
        raise RuntimeError("predicate failed")

    with pytest.raises(RuntimeError):
        db.find_no_extractor_attachment_filepaths(fails)
    (cursor,) = spy.sweep_cursors()
    assert cursor.closed
    assert len(cursor.batches) == 1

    acquired: list[bool] = []

    def take_lock():
        got = db._lock.acquire(timeout=5)
        acquired.append(got)
        if got:
            db._lock.release()

    worker = threading.Thread(target=take_lock)
    worker.start()
    worker.join()
    assert acquired == [True]
