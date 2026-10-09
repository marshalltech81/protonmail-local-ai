"""Bounded interleaving of reparse jobs with foreground jobs (#1142).

A reparse queues every indexed file at once. ``claim_batch`` hands
foreground rows (fresh mail, recovery, rescans, re-extraction: every
reason but ``reparse``) out first, keeps one slot of each batch for the
oldest due reparse row while both classes are due, and alternates the
two at a batch size of 1, so fresh mail is not held behind the backlog
and the reparse still advances under a sustained foreground backlog.
"""

import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from src import main
from src.database import EMBEDDING_DIM, Database
from src.queue import (
    REASON_INITIAL_SCAN,
    REASON_ON_CREATED,
    REASON_RECOVERY,
    REASON_REEXTRACT,
    REASON_REPARSE,
    REASON_RESCAN,
    STATUS_QUEUED,
    IndexingQueue,
)
from src.threader import Threader
from src.timings import TimingAggregator

from tests.conftest import make_mock_embedder

MARKER = "SYNTHETIC_INTERLEAVE_MARKER"
_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
_T0 = datetime(2026, 1, 1, tzinfo=UTC)
FOREGROUND_REASONS = (
    REASON_ON_CREATED,
    REASON_INITIAL_SCAN,
    REASON_RECOVERY,
    REASON_RESCAN,
    REASON_REEXTRACT,
)


def _add(queue: IndexingQueue, filepath: str, reason: str, minutes: int) -> None:
    """Queue ``filepath`` due ``minutes`` after ``_T0`` (in the past)."""
    queue.enqueue(filepath, reason, due_at=_T0 + timedelta(minutes=minutes))


def _reasons(rows) -> list[str]:
    return [row["reason"] for row in rows]


def _succeed(queue: IndexingQueue, rows) -> None:
    for row in rows:
        queue.mark_succeeded(row["filepath"])


@pytest.fixture
def queue(tmp_path):
    db = Database(tmp_path / "q.db")
    yield IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
    db.close()


class TestBatchComposition:
    def test_foreground_due_after_the_backlog_goes_first(self, queue):
        for i in range(20):
            _add(queue, f"/r/{i:02d}", REASON_REPARSE, 0)
        _add(queue, "/f/new", REASON_ON_CREATED, 10)
        rows = queue.claim_batch(8)
        assert [r["filepath"] for r in rows] == ["/f/new"] + [f"/r/{i:02d}" for i in range(7)]

    @pytest.mark.parametrize("reason", FOREGROUND_REASONS)
    def test_every_non_reparse_reason_is_foreground(self, queue, reason):
        _add(queue, "/r/a", REASON_REPARSE, 0)
        _add(queue, "/r/b", REASON_REPARSE, 1)
        _add(queue, "/f/a", reason, 5)
        assert _reasons(queue.claim_batch(2)) == [reason, REASON_REPARSE]

    @pytest.mark.parametrize("limit", [1, 2, 3, 8, 50])
    @pytest.mark.parametrize("n_fg", [0, 1, 2, 7, 60])
    @pytest.mark.parametrize("n_rp", [0, 1, 2, 7, 60])
    def test_ordering_capacity_and_no_duplicates(self, queue, limit, n_fg, n_rp):
        # Interleave due times across the classes so the classes' own
        # orders differ from insertion order and from each other.
        for i in range(n_fg):
            _add(queue, f"/f/{i:03d}", REASON_ON_CREATED, (n_fg - i) * 2)
        for i in range(n_rp):
            _add(queue, f"/r/{i:03d}", REASON_REPARSE, (n_rp - i) * 2 + 1)
        rows = queue.claim_batch(limit)
        paths = [r["filepath"] for r in rows]

        assert len(paths) == len(set(paths))
        # Never an empty slot while rows are due.
        assert len(rows) == min(limit, n_fg + n_rp)
        reasons = _reasons(rows)
        n_rp_taken = reasons.count(REASON_REPARSE)
        # Foreground first.
        assert (
            reasons
            == [REASON_ON_CREATED] * (len(rows) - n_rp_taken) + [REASON_REPARSE] * n_rp_taken
        )
        # One reserved reparse slot when both are due and limit > 1;
        # unused capacity from either class goes to the other.
        if limit > 1 and n_fg and n_rp:
            assert n_rp_taken == max(1, min(n_rp, limit - n_fg))
        else:
            assert n_rp_taken == min(n_rp, limit - min(n_fg, limit))
        # Due order within each class.
        for prefix in ("/f/", "/r/"):
            dues = [r["next_attempt_at"] for r in rows if r["filepath"].startswith(prefix)]
            assert dues == sorted(dues)
        expected_fg = sorted(range(n_fg), key=lambda i: -i)[: len(rows) - n_rp_taken]
        expected_rp = sorted(range(n_rp), key=lambda i: -i)[:n_rp_taken]
        assert paths == [f"/f/{i:03d}" for i in expected_fg] + [f"/r/{i:03d}" for i in expected_rp]

    def test_future_due_and_dead_rows_are_excluded_from_both_classes(self, queue):
        future = datetime.now(UTC) + timedelta(hours=1)
        queue.enqueue("/r/future", REASON_REPARSE, due_at=future)
        queue.enqueue("/f/future", REASON_ON_CREATED, due_at=future)
        _add(queue, "/r/dead", REASON_REPARSE, 0)
        queue.mark_dead_terminal("/r/dead", stage="parse", error="oversized: too large")
        _add(queue, "/f/dead", REASON_ON_CREATED, 0)
        queue.mark_dead_terminal("/f/dead", stage="parse", error="oversized: too large")
        assert queue.claim_batch(8) == []
        assert queue.claim_batch(1) == []

        _add(queue, "/r/due", REASON_REPARSE, 1)
        _add(queue, "/f/due", REASON_ON_CREATED, 2)
        assert [r["filepath"] for r in queue.claim_batch(8)] == ["/f/due", "/r/due"]

    def test_first_index_order_is_unchanged(self, queue):
        """No reparse rows: the batch is the oldest due rows, as before
        (#699), whatever the reason."""
        dues = [7, 3, 9, 1, 5, 2]
        for i, minutes in enumerate(dues):
            reason = REASON_RECOVERY if i == 2 else REASON_INITIAL_SCAN
            _add(queue, f"/f/{i}", reason, minutes)
        rows = queue.claim_batch(50)
        assert [r["filepath"] for r in rows] == [
            f"/f/{i}" for i in sorted(range(len(dues)), key=lambda i: dues[i])
        ]


class TestProgress:
    def test_reparse_advances_under_a_sustained_foreground_backlog(self, queue):
        """New foreground rows keep arriving faster than a batch drains
        them; the reparse still gets one row per batch."""
        for i in range(30):
            _add(queue, f"/r/{i:02d}", REASON_REPARSE, 0)
        fresh = 0
        for batch in range(10):
            while fresh < (batch + 2) * 8:
                _add(queue, f"/f/{fresh:03d}", REASON_ON_CREATED, 100 + fresh)
                fresh += 1
            rows = queue.claim_batch(8)
            assert _reasons(rows) == [REASON_ON_CREATED] * 7 + [REASON_REPARSE]
            assert rows[-1]["filepath"] == f"/r/{batch:02d}"
            _succeed(queue, rows)

    def test_arrivals_mid_drain_are_claimed_next(self, queue):
        for i in range(20):
            _add(queue, f"/r/{i:02d}", REASON_REPARSE, 0)
        rows = queue.claim_batch(4)
        assert _reasons(rows) == [REASON_REPARSE] * 4
        _succeed(queue, rows)
        queue.enqueue("/f/arrived", REASON_ON_CREATED)  # due now, after the backlog
        rows = queue.claim_batch(4)
        assert [r["filepath"] for r in rows] == ["/f/arrived", "/r/04", "/r/05", "/r/06"]


class TestSingletonAlternation:
    def test_batch_size_one_alternates_while_both_are_due(self, queue):
        for i in range(3):
            _add(queue, f"/r/{i}", REASON_REPARSE, i)
            _add(queue, f"/f/{i}", REASON_ON_CREATED, 10 + i)
        claimed = []
        for _ in range(6):
            (row,) = queue.claim_batch(1)
            claimed.append(row["filepath"])
            queue.mark_succeeded(row["filepath"])
        assert claimed == ["/f/0", "/r/0", "/f/1", "/r/1", "/f/2", "/r/2"]
        assert queue.claim_batch(1) == []

    @pytest.mark.parametrize("reason", [REASON_ON_CREATED, REASON_REPARSE])
    def test_batch_size_one_with_one_class_drains_it(self, queue, reason):
        for i in range(3):
            _add(queue, f"/x/{i}", reason, i)
        claimed = []
        for _ in range(3):
            (row,) = queue.claim_batch(1)
            claimed.append(row["filepath"])
            queue.mark_succeeded(row["filepath"])
        assert claimed == ["/x/0", "/x/1", "/x/2"]

    def test_a_reclaimed_row_does_not_stall_the_other_class(self, queue):
        """The caller may leave a claimed row due (a deferral that is
        already due again): alternation still hands the other class its
        turn."""
        _add(queue, "/f/a", REASON_ON_CREATED, 5)
        _add(queue, "/r/a", REASON_REPARSE, 0)
        assert [r["filepath"] for r in queue.claim_batch(1)] == ["/f/a"]
        assert [r["filepath"] for r in queue.claim_batch(1)] == ["/r/a"]
        assert [r["filepath"] for r in queue.claim_batch(1)] == ["/f/a"]

    def test_a_larger_batch_does_not_need_the_flag(self, queue):
        _add(queue, "/f/a", REASON_ON_CREATED, 5)
        _add(queue, "/r/a", REASON_REPARSE, 0)
        for _ in range(3):
            assert _reasons(queue.claim_batch(2)) == [REASON_ON_CREATED, REASON_REPARSE]


# ----- through the real pipeline ------------------------------------------


def _write_eml(path: Path, message_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "From: alice@example.com\r\n"
        "To: bob@example.com\r\n"
        f"Subject: {MARKER} {message_id}\r\n"
        f"Message-ID: <{message_id}>\r\n"
        "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\nbody of {message_id} {MARKER}\r\n",
        encoding="utf-8",
    )


def _drain(db, queue, embedder, *, batch_size=4, max_passes=None, breaker=None) -> int:
    return main._drain_queue_batched(
        db,
        embedder,
        Threader(db),
        queue,
        batch_size=batch_size,
        timing_aggregator=TimingAggregator(window=4),
        max_passes=max_passes,
        breaker=breaker,
    )


def _indexed(tmp_path: Path, n: int) -> tuple[Database, IndexingQueue, list[str]]:
    """``n`` messages indexed through the pipeline, then all queued for a
    reparse."""
    db = Database(tmp_path / "mail.db")
    queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
    paths = []
    for i in range(n):
        path = tmp_path / "INBOX" / "cur" / f"m{i:02d}:2,S"
        _write_eml(path, f"m{i:02d}@example.com")
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        paths.append(str(path))
    _drain(db, queue, make_mock_embedder(_VECTOR))
    assert queue.enqueue_reparse() == n
    return db, queue, paths


def _fresh(tmp_path: Path, queue: IndexingQueue, name: str) -> str:
    path = tmp_path / "INBOX" / "new" / name
    _write_eml(path, f"{name}@example.com")
    queue.enqueue(str(path), REASON_ON_CREATED)
    return str(path)


@pytest.fixture
def phase1_order(monkeypatch):
    """Every row Phase 1 runs, in order."""
    seen: list[str] = []
    real = main._phase1_commit_thread

    def record(row, *args, **kwargs):
        seen.append(row["filepath"])
        return real(row, *args, **kwargs)

    monkeypatch.setattr(main, "_phase1_commit_thread", record)
    return seen


class TestPipeline:
    def test_fresh_mail_is_indexed_in_the_first_batch(self, tmp_path, phase1_order):
        db, queue, paths = _indexed(tmp_path, 10)
        fresh = _fresh(tmp_path, queue, "fresh")
        phase1_order.clear()
        embedder = make_mock_embedder(_VECTOR)
        assert _drain(db, queue, embedder, max_passes=1) == 4
        assert phase1_order == [fresh, *paths[:3]]
        assert db.find_message_entry_by_filepath(fresh) is not None
        db.close()

    def test_mail_arriving_mid_drain_goes_ahead_of_the_rest(
        self, tmp_path, monkeypatch, phase1_order
    ):
        db, queue, paths = _indexed(tmp_path, 10)
        arrived: list[str] = []
        real_succeeded = queue.mark_succeeded

        def succeeded(filepath):
            real_succeeded(filepath)
            if not arrived:
                arrived.append(_fresh(tmp_path, queue, "arrived"))

        monkeypatch.setattr(queue, "mark_succeeded", succeeded)
        phase1_order.clear()
        _drain(db, queue, make_mock_embedder(_VECTOR))
        # Batch 1 is all reparse; the arrival leads batch 2.
        assert phase1_order[:5] == [*paths[:4], arrived[0]]
        assert sorted(phase1_order) == sorted([*paths, arrived[0]])
        assert main._reparse_progress.reparsed == 10
        assert db._conn.execute("SELECT COUNT(*) FROM indexing_jobs").fetchone()[0] == 0
        db.close()

    def test_the_rename_wait_holds_in_a_mixed_batch(self, tmp_path):
        db, queue, paths = _indexed(tmp_path, 2)
        fresh = _fresh(tmp_path, queue, "fresh")
        old = Path(paths[0])
        old.rename(old.with_name(old.name + "R"))
        _drain(db, queue, make_mock_embedder(_VECTOR), max_passes=1)
        row = db._conn.execute(
            "SELECT reason, attempts, last_error, next_attempt_at FROM indexing_jobs "
            "WHERE filepath = ?",
            (paths[0],),
        ).fetchone()
        assert (row["reason"], row["attempts"]) == (REASON_REPARSE, 0)
        assert row["last_error"] == main.RENAME_DEFERRED_ERROR
        wait = datetime.fromisoformat(row["next_attempt_at"]) - datetime.now(UTC)
        assert wait > timedelta(seconds=main.RENAME_DEFER_SECS - 10)
        assert db.find_message_entry_by_filepath(fresh) is not None
        # Not claimed again before the wait ends.
        assert paths[0] not in [r["filepath"] for r in queue.claim_batch(8)]
        db.close()

    def test_an_interrupted_reparse_row_still_runs_alone(self, tmp_path, phase1_order):
        db, queue, paths = _indexed(tmp_path, 3)
        _fresh(tmp_path, queue, "fresh")
        queue.mark_interrupted([paths[0]])
        phase1_order.clear()
        assert _drain(db, queue, make_mock_embedder(_VECTOR), max_passes=1) == 1
        assert phase1_order == [paths[0]]
        db.close()

    def test_an_embedding_outage_in_a_mixed_batch_only_defers(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        db, queue, paths = _indexed(tmp_path, 3)
        fresh = [_fresh(tmp_path, queue, f"fresh{i}") for i in range(2)]
        embedder = make_mock_embedder()
        embedder.embed.side_effect = ConnectionError("embedder down")
        breaker = main._EmbedOutageBreaker()

        _drain(db, queue, embedder, batch_size=4, breaker=breaker)

        rows = {
            row["filepath"]: row
            for row in db._conn.execute(
                "SELECT filepath, reason, status, attempts, last_error_class FROM indexing_jobs"
            )
        }
        # Every row is still queued with no attempt spent, nothing dead.
        assert sorted(rows) == sorted([*paths, *fresh])
        for row in rows.values():
            assert (row["status"], row["attempts"]) == (STATUS_QUEUED, 0)
        # The batch held both fresh rows and one reparse row; the two
        # reparse rows left out of it were never touched.
        claimed = [fp for fp, row in rows.items() if row["last_error_class"] is not None]
        assert sorted(claimed) == sorted([*fresh, *paths[:2]])
        assert {rows[fp]["last_error_class"] for fp in claimed} == {"retryable"}
        assert not breaker.allow(time.monotonic())
        assert MARKER not in caplog.text
        db.close()

    def test_heartbeat_and_reparse_progress_are_unchanged(self, tmp_path, caplog, monkeypatch):
        caplog.set_level(logging.INFO)
        now = {"t": 1000.0}
        monkeypatch.setattr(main, "_monotonic", lambda: now["t"])
        db, queue, paths = _indexed(tmp_path, 6)
        _fresh(tmp_path, queue, "fresh")
        _drain(db, queue, make_mock_embedder(_VECTOR), max_passes=1)

        counts = queue.heartbeat_counts()
        assert (counts["pending"], counts["reparse"], counts["reparse_dead"]) == (3, 3, 0)
        caplog.clear()
        now["t"] += main.QUEUE_HEARTBEAT_INTERVAL_SECS
        main._maybe_log_queue_heartbeat(queue)
        [line] = [r for r in caplog.records if r.getMessage().startswith("reparse")]
        assert line.getMessage() == (
            "reparse: remaining=3 parked_trashed=0 reparsed_since_last_heartbeat=3 dead=0"
        )
        assert MARKER not in caplog.text
        db.close()


# ----- claim cost -----------------------------------------------------------


class TestClaimCost:
    """A claim is two SELECTs on the ``(status, next_attempt_at)`` index;
    each may walk past every due row of the other class before it finds
    its own (#1142). Plain timing over a 33,000-row queue, the live
    mailbox's size, put a claim at about 1.4 ms (2.6 ms at worst) on an
    Apple-silicon laptop, against 0.04 ms for the single SELECT before,
    with about 8 VM instructions per row walked. The bounds here are
    generous: 1 s, and three times the measured instruction count."""

    ROWS = 33_000
    # Instructions between progress-handler calls.
    TICK = 1000

    @staticmethod
    def _fill(db: Database, reason: str, n: int, due: str) -> None:
        db._conn.executemany(
            "INSERT INTO indexing_jobs (filepath, reason, status, attempts, created_at, "
            "updated_at, next_attempt_at) VALUES (?, ?, 'queued', 0, ?, ?, ?)",
            [(f"/{reason}/{i:06d}", reason, due, due, due) for i in range(n)],
        )
        db._conn.commit()

    @pytest.mark.parametrize("backlog", [REASON_REPARSE, REASON_INITIAL_SCAN])
    def test_a_claim_over_a_33000_row_backlog_is_bounded(self, tmp_path, backlog):
        db = Database(tmp_path / "q.db")
        queue = IndexingQueue(db)
        self._fill(db, backlog, self.ROWS, "2026-01-01T00:00:00+00:00")
        other = REASON_ON_CREATED if backlog == REASON_REPARSE else REASON_REPARSE
        # The other class is due after the whole backlog: the worst case
        # for finding it.
        self._fill(db, other, 1, "2026-01-02T00:00:00+00:00")
        statements: list[str] = []
        ticks = [0]

        def tick() -> int:
            ticks[0] += 1
            return 0

        db._conn.set_trace_callback(statements.append)
        db._conn.set_progress_handler(tick, self.TICK)
        start = time.perf_counter()
        rows = queue.claim_batch(50)
        elapsed = time.perf_counter() - start
        db._conn.set_progress_handler(None, 0)
        db._conn.set_trace_callback(None)

        assert len(rows) == 50
        assert _reasons(rows).count(REASON_REPARSE) == (49 if backlog == REASON_REPARSE else 1)
        selects = [s for s in statements if s.lstrip().startswith("SELECT")]
        assert len(selects) == 2
        # Each SELECT walks the index in due order: no table scan, no sort.
        for sql in selects:
            details = [row["detail"] for row in db._conn.execute(f"EXPLAIN QUERY PLAN {sql}")]
            assert any("USING INDEX idx_indexing_jobs_status_next" in d for d in details)
            assert not any("TEMP B-TREE" in d for d in details)
        # The work is one walk of the backlog (about 8 instructions a row),
        # not one per claimed row or a quadratic one; and the walk did
        # happen, so this is the worst case.
        assert self.ROWS < ticks[0] * self.TICK < 3 * 8 * self.ROWS
        assert elapsed < 1.0
        db.close()
