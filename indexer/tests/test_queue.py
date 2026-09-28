"""Tests for src/queue.py — durable indexing queue (schema v8).

Covers the retry / backoff / dead-letter state machine, re-enqueue
semantics for previously-failed rows, and the ``claim_next`` ordering
against the ``next_attempt_at`` backoff column.
"""

from datetime import UTC, datetime, timedelta

from src.database import Database
from src.queue import (
    DEFAULT_BASE_BACKOFF_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    ERROR_CLASS_OPERATOR,
    ERROR_CLASS_PERMANENT,
    ERROR_CLASS_RETRYABLE,
    REASON_INITIAL_SCAN,
    REASON_ON_CREATED,
    STATUS_DEAD,
    STATUS_QUEUED,
    IndexingQueue,
    load_config_from_env,
)


def _queue(db: Database, max_attempts: int = 3, base_backoff_seconds: int = 0) -> IndexingQueue:
    """Queue with fast backoff so retry tests run in-process without
    needing to manipulate the clock."""
    return IndexingQueue(
        db,
        max_attempts=max_attempts,
        base_backoff_seconds=base_backoff_seconds,
    )


class TestEnqueueClaim:
    def test_enqueue_creates_queued_row(self, db: Database):
        q = _queue(db)
        q.enqueue("/maildir/INBOX/cur/a", REASON_ON_CREATED)
        row = db._conn.execute(
            "SELECT status, reason, attempts FROM indexing_jobs WHERE filepath = ?",
            ("/maildir/INBOX/cur/a",),
        ).fetchone()
        assert row["status"] == STATUS_QUEUED
        assert row["reason"] == REASON_ON_CREATED
        assert row["attempts"] == 0

    def test_claim_next_returns_the_due_row(self, db: Database):
        q = _queue(db)
        q.enqueue("/maildir/INBOX/cur/a", REASON_ON_CREATED)
        row = q.claim_next()
        assert row is not None
        assert row["filepath"] == "/maildir/INBOX/cur/a"

    def test_claim_next_returns_none_on_empty_queue(self, db: Database):
        q = _queue(db)
        assert q.claim_next() is None

    def test_claim_next_returns_none_when_only_dead_rows_remain(self, db: Database):
        """``dead`` is a visible-but-ignored state. The worker must not
        re-attempt dead rows; ``claim_next`` filters on
        ``status = 'queued'``."""
        q = _queue(db, max_attempts=1)
        q.enqueue("/m/dead", REASON_ON_CREATED)
        q.mark_failed("/m/dead", stage="parse", error="bad")  # one attempt → dead
        row = db._conn.execute(
            "SELECT status FROM indexing_jobs WHERE filepath = '/m/dead'"
        ).fetchone()
        assert row["status"] == STATUS_DEAD
        assert q.claim_next() is None

    def test_claim_next_skips_rows_with_future_next_attempt(self, db: Database):
        """A ``queued`` row whose ``next_attempt_at`` is in the future
        is in backoff and must not be claimed yet, even though it is
        the only row in the table."""
        q = _queue(db, max_attempts=5, base_backoff_seconds=3600)
        q.enqueue("/m/backoff", REASON_ON_CREATED)
        # First failure schedules a 1-hour backoff (base_backoff × 2^0).
        q.mark_failed("/m/backoff", stage="embed", error="embedding service down")
        row = db._conn.execute(
            "SELECT next_attempt_at FROM indexing_jobs WHERE filepath = '/m/backoff'"
        ).fetchone()
        # next_attempt_at is in the future — claim_next must return None
        # even though the row is still status='queued'.
        assert datetime.fromisoformat(row["next_attempt_at"]) > datetime.now(UTC)
        assert q.claim_next() is None

    def test_claim_next_returns_oldest_due_row_first(self, db: Database):
        q = _queue(db)
        # Insert two rows with an explicit next_attempt_at delta so we
        # can verify ordering regardless of the exact timestamp enqueue
        # wrote.
        q.enqueue("/m/later", REASON_INITIAL_SCAN)
        older_ts = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        db._conn.execute(
            "UPDATE indexing_jobs SET next_attempt_at = ? WHERE filepath = '/m/later'",
            (older_ts,),
        )
        db._conn.commit()
        q.enqueue("/m/newer", REASON_INITIAL_SCAN)

        first = q.claim_next()
        assert first["filepath"] == "/m/later"


class TestMarkSucceededAndFailed:
    def test_mark_succeeded_deletes_the_row(self, db: Database):
        q = _queue(db)
        q.enqueue("/m/ok", REASON_ON_CREATED)
        q.mark_succeeded("/m/ok")
        assert q.claim_next() is None
        row = db._conn.execute("SELECT 1 FROM indexing_jobs WHERE filepath = '/m/ok'").fetchone()
        assert row is None

    def test_mark_succeeded_is_noop_for_missing_row(self, db: Database):
        """The worker may call mark_succeeded after a retry cycle that
        was resolved through another code path; missing-row should not
        raise."""
        q = _queue(db)
        q.mark_succeeded("/m/never_enqueued")  # no raise
        assert q.claim_next() is None

    def test_mark_failed_increments_attempts_and_schedules_backoff(self, db: Database):
        q = _queue(db, max_attempts=5, base_backoff_seconds=60)
        q.enqueue("/m/fail", REASON_ON_CREATED)
        q.mark_failed("/m/fail", stage="embed", error="embedding service down")
        row = db._conn.execute(
            "SELECT attempts, last_stage, last_error, status, next_attempt_at "
            "FROM indexing_jobs WHERE filepath = '/m/fail'"
        ).fetchone()
        assert row["attempts"] == 1
        assert row["last_stage"] == "embed"
        assert row["last_error"] == "embedding service down"
        assert row["status"] == STATUS_QUEUED
        # base_backoff_seconds × 2^0 = 60s
        expected = datetime.now(UTC) + timedelta(seconds=60)
        actual = datetime.fromisoformat(row["next_attempt_at"])
        # Allow 5s slack for test execution time.
        assert abs((actual - expected).total_seconds()) < 5

    def test_mark_failed_transitions_to_dead_after_max_attempts(self, db: Database):
        q = _queue(db, max_attempts=3, base_backoff_seconds=0)
        q.enqueue("/m/threestrike", REASON_ON_CREATED)
        q.mark_failed("/m/threestrike", stage="parse", error="bad1")
        q.mark_failed("/m/threestrike", stage="parse", error="bad2")
        q.mark_failed("/m/threestrike", stage="parse", error="bad3")
        row = db._conn.execute(
            "SELECT status, attempts, last_error FROM indexing_jobs "
            "WHERE filepath = '/m/threestrike'"
        ).fetchone()
        assert row["status"] == STATUS_DEAD
        assert row["attempts"] == 3
        assert row["last_error"] == "bad3"

    def test_mark_failed_is_noop_for_missing_row(self, db: Database):
        """Worker crashed after claim but before durable failure write;
        somebody else cleaned the row. Must not raise."""
        q = _queue(db)
        q.mark_failed("/m/never_enqueued", stage="parse", error="x")
        assert q.claim_next() is None


class TestIsDead:
    """``is_dead(filepath)`` tells the initial scan whether to skip a row.

    Without this gate, every restart re-enqueues dead-lettered files
    via ``INSERT OR REPLACE``, resetting attempts to 0 and burning
    another full retry cascade on the same upstream condition that
    caused the original dead-letter (e.g. embedding service 500s on a
    poison-pill payload). The reset semantics on ``enqueue`` are
    intentional for genuine watchdog rename / create events — those
    signal actual file change — but routine startup re-discovery
    should NOT trigger them.
    """

    def test_returns_true_for_dead_row(self, db: Database):
        q = _queue(db, max_attempts=1)
        q.enqueue("/m/dead", REASON_ON_CREATED)
        q.mark_failed("/m/dead", stage="parse", error="bad")  # 1 attempt -> dead
        assert q.is_dead("/m/dead") is True

    def test_returns_false_for_queued_row(self, db: Database):
        q = _queue(db)
        q.enqueue("/m/active", REASON_ON_CREATED)
        assert q.is_dead("/m/active") is False

    def test_returns_false_for_unknown_filepath(self, db: Database):
        # Path was never enqueued OR has succeeded and been deleted —
        # both surface as "no row" and must not be reported as dead.
        q = _queue(db)
        assert q.is_dead("/m/never") is False


class TestMarkSkipped:
    """``mark_skipped`` drops a row without consuming retry budget.

    Distinct from mark_failed (no attempts increment, no dead-letter)
    and from mark_succeeded (the file was NOT indexed). Used for
    terminal non-error conditions like FileNotFoundError at parse.
    """

    def test_mark_skipped_deletes_the_row(self, db: Database):
        q = _queue(db)
        q.enqueue("/m/gone", REASON_ON_CREATED)
        q.mark_skipped("/m/gone", reason="file_missing")
        assert q.claim_next() is None
        row = db._conn.execute("SELECT 1 FROM indexing_jobs WHERE filepath = '/m/gone'").fetchone()
        assert row is None

    def test_mark_skipped_does_not_increment_attempts(self, db: Database):
        # If a file was previously failed once and now goes missing,
        # mark_skipped should drop the row outright. The attempts
        # counter is irrelevant — there's no retry budget to spend.
        q = _queue(db, max_attempts=5, base_backoff_seconds=0)
        q.enqueue("/m/once", REASON_ON_CREATED)
        q.mark_failed("/m/once", stage="embed", error="embedding service")
        row = db._conn.execute(
            "SELECT attempts FROM indexing_jobs WHERE filepath = '/m/once'"
        ).fetchone()
        assert row["attempts"] == 1
        q.mark_skipped("/m/once", reason="file_missing")
        # Row is gone; attempts on the (now-deleted) row are not
        # what the queue cares about — visibility is via the log line.
        assert q.claim_next() is None

    def test_mark_skipped_is_noop_for_missing_row(self, db: Database):
        # Mirror the mark_succeeded / mark_failed contract: silent
        # no-op on a row that's already gone (race with a different
        # cleanup path). Must not raise.
        q = _queue(db)
        q.mark_skipped("/m/never_enqueued", reason="file_missing")
        assert q.claim_next() is None


class TestMarkDeadTerminal:
    """``mark_dead_terminal`` writes a dead row directly for a
    non-retryable failure that must survive restarts. Distinct from
    ``mark_skipped`` (drops the row — used when the file is gone) and
    from ``mark_failed`` (schedules a retry until max_attempts). Used
    for terminal-but-present failures like oversized files, where
    deleting the row would let ``initial_index`` re-enqueue the same
    file on every container start.
    """

    def test_writes_dead_row_with_stage_and_error(self, db: Database):
        q = _queue(db)
        q.enqueue("/m/huge", REASON_ON_CREATED)
        q.mark_dead_terminal("/m/huge", stage="parse", error="oversized: 60MB > 50MB cap")
        row = db._conn.execute(
            "SELECT status, attempts, last_stage, last_error "
            "FROM indexing_jobs WHERE filepath = '/m/huge'"
        ).fetchone()
        assert row["status"] == STATUS_DEAD
        assert row["attempts"] == 1
        assert row["last_stage"] == "parse"
        assert "oversized" in row["last_error"]

    def test_is_dead_returns_true_so_initial_scan_skips_on_restart(self, db: Database):
        q = _queue(db)
        q.enqueue("/m/huge", REASON_ON_CREATED)
        q.mark_dead_terminal("/m/huge", stage="parse", error="oversized")
        # ``initial_index`` consults ``is_dead`` before re-enqueueing
        # files surfaced by the Maildir walk; this gate is what
        # prevents the retry storm the fix targets.
        assert q.is_dead("/m/huge") is True

    def test_claim_next_skips_dead_terminal_rows(self, db: Database):
        # Dead rows are visible-but-ignored: the worker must not
        # re-attempt them, otherwise the terminal designation is
        # meaningless.
        q = _queue(db)
        q.enqueue("/m/huge", REASON_ON_CREATED)
        q.mark_dead_terminal("/m/huge", stage="parse", error="oversized")
        assert q.claim_next() is None

    def test_dead_terminal_row_appears_in_stats(self, db: Database):
        q = _queue(db)
        q.enqueue("/m/huge", REASON_ON_CREATED)
        q.mark_dead_terminal("/m/huge", stage="parse", error="oversized")
        assert q.stats() == {"queued": 0, "dead": 1}


class TestReEnqueueResetsState:
    def test_reenqueue_resets_failed_row_to_fresh_attempt(self, db: Database):
        """A newly-observed watchdog event is fresh intent. A previous
        failed row should be reset — attempts back to 0, next_attempt_at
        now — so the worker picks it up immediately."""
        q = _queue(db, max_attempts=5, base_backoff_seconds=3600)
        q.enqueue("/m/reset", REASON_ON_CREATED)
        q.mark_failed("/m/reset", stage="embed", error="embedding service")
        # After failure the row is backoff'd for an hour.
        assert q.claim_next() is None

        q.enqueue("/m/reset", REASON_ON_CREATED)
        # Re-enqueue resets attempts and schedules immediately.
        row = q.claim_next()
        assert row is not None
        assert row["filepath"] == "/m/reset"
        assert row["attempts"] == 0

    def test_reenqueue_resurrects_dead_row(self, db: Database):
        """``dead`` is "give up for now", not "never try again". A new
        enqueue for the same path — typically an mbsync re-delivery or
        the user retouching a file — should re-run the pipeline."""
        q = _queue(db, max_attempts=1, base_backoff_seconds=0)
        q.enqueue("/m/zombie", REASON_ON_CREATED)
        q.mark_failed("/m/zombie", stage="parse", error="bad")
        # Row is dead.
        assert q.claim_next() is None

        q.enqueue("/m/zombie", REASON_ON_CREATED)
        row = q.claim_next()
        assert row is not None
        assert row["filepath"] == "/m/zombie"
        assert row["attempts"] == 0


class TestStats:
    def test_stats_reports_queued_and_dead_counts(self, db: Database):
        q = _queue(db, max_attempts=1, base_backoff_seconds=0)
        q.enqueue("/m/a", REASON_ON_CREATED)
        q.enqueue("/m/b", REASON_ON_CREATED)
        q.enqueue("/m/c", REASON_ON_CREATED)
        q.mark_failed("/m/c", stage="parse", error="x")  # → dead
        stats = q.stats()
        assert stats == {"queued": 2, "dead": 1}

    def test_stats_returns_zeros_for_empty_queue(self, db: Database):
        q = _queue(db)
        assert q.stats() == {"queued": 0, "dead": 0}


class TestLoadConfigFromEnv:
    def test_defaults_when_env_missing(self):
        cfg = load_config_from_env({})
        assert cfg["max_attempts"] == DEFAULT_MAX_ATTEMPTS
        assert cfg["base_backoff_seconds"] == DEFAULT_BASE_BACKOFF_SECONDS

    def test_overrides_from_env(self):
        cfg = load_config_from_env(
            {
                "INDEXER_MAX_ATTEMPTS": "10",
                "INDEXER_RETRY_BASE_SECONDS": "90",
            }
        )
        assert cfg["max_attempts"] == 10
        assert cfg["base_backoff_seconds"] == 90

    def test_zero_max_attempts_falls_back_to_default(self):
        # max_attempts <= 0 would dead-letter every row on the first
        # failure (``new_attempts >= self.max_attempts`` matches at
        # 1 >= 0). Clamp to the documented default so a typo doesn't
        # silently neutralize the retry contract.
        cfg = load_config_from_env({"INDEXER_MAX_ATTEMPTS": "0"})
        assert cfg["max_attempts"] == DEFAULT_MAX_ATTEMPTS

    def test_negative_max_attempts_falls_back_to_default(self):
        cfg = load_config_from_env({"INDEXER_MAX_ATTEMPTS": "-1"})
        assert cfg["max_attempts"] == DEFAULT_MAX_ATTEMPTS

    def test_zero_base_backoff_falls_back_to_default(self):
        # base_backoff_seconds <= 0 schedules next_attempt_at at "now"
        # (zero seconds added) or in the past (negative), so claim_next
        # immediately re-claims the failing row and the retry budget
        # burns in a tight loop. Clamp to the documented default.
        cfg = load_config_from_env({"INDEXER_RETRY_BASE_SECONDS": "0"})
        assert cfg["base_backoff_seconds"] == DEFAULT_BASE_BACKOFF_SECONDS

    def test_negative_base_backoff_falls_back_to_default(self):
        cfg = load_config_from_env({"INDEXER_RETRY_BASE_SECONDS": "-30"})
        assert cfg["base_backoff_seconds"] == DEFAULT_BASE_BACKOFF_SECONDS

    def test_malformed_int_falls_back_to_default(self):
        cfg = load_config_from_env(
            {
                "INDEXER_MAX_ATTEMPTS": "five",
                "INDEXER_RETRY_BASE_SECONDS": "thirty",
            }
        )
        assert cfg["max_attempts"] == DEFAULT_MAX_ATTEMPTS
        assert cfg["base_backoff_seconds"] == DEFAULT_BASE_BACKOFF_SECONDS


class TestBackoffCap:
    def test_backoff_caps_at_six_hours(self, db: Database):
        """A long streak of failures otherwise computes an impractical
        next_attempt_at (days or weeks out). The cap keeps it at six
        hours so a transient outage doesn't hide work for longer than
        an ops shift."""
        q = _queue(db, max_attempts=100, base_backoff_seconds=3600)
        q.enqueue("/m/sustained", REASON_ON_CREATED)
        # Hammer mark_failed so 2^attempts × base exceeds the cap.
        for _ in range(10):
            q.mark_failed("/m/sustained", stage="embed", error="embedding service")
        row = db._conn.execute(
            "SELECT next_attempt_at FROM indexing_jobs WHERE filepath = '/m/sustained'"
        ).fetchone()
        scheduled = datetime.fromisoformat(row["next_attempt_at"])
        gap = (scheduled - datetime.now(UTC)).total_seconds()
        # Six hours plus a small slack for test execution.
        assert gap <= 6 * 3600 + 5


def _row(db: Database, filepath: str):
    return db._conn.execute(
        "SELECT status, attempts, last_stage, last_error, last_error_class, next_attempt_at "
        "FROM indexing_jobs WHERE filepath = ?",
        (filepath,),
    ).fetchone()


class TestFailureClasses:
    """Every failure outcome records WHY it failed, so an operator (or
    ``requeue_dead``) can tell an exhausted retry from a permanently
    unindexable source from a configuration problem."""

    def test_mark_failed_records_retryable_on_retry_and_on_dead(self, tmp_path):
        db = Database(tmp_path / "q.db")
        q = _queue(db, max_attempts=2)
        q.enqueue("/m/a", REASON_INITIAL_SCAN)

        q.mark_failed("/m/a", stage="parse", error="boom")
        assert _row(db, "/m/a")["last_error_class"] == ERROR_CLASS_RETRYABLE
        q.mark_failed("/m/a", stage="parse", error="boom")
        row = _row(db, "/m/a")
        assert row["status"] == STATUS_DEAD
        assert row["last_error_class"] == ERROR_CLASS_RETRYABLE

    def test_mark_dead_terminal_records_permanent_source_failure(self, tmp_path):
        db = Database(tmp_path / "q.db")
        q = _queue(db)
        q.enqueue("/m/a", REASON_INITIAL_SCAN)

        q.mark_dead_terminal("/m/a", stage="parse", error="oversized")

        assert _row(db, "/m/a")["last_error_class"] == ERROR_CLASS_PERMANENT

    def test_enqueue_clears_previous_class(self, tmp_path):
        db = Database(tmp_path / "q.db")
        q = _queue(db)
        q.enqueue("/m/a", REASON_INITIAL_SCAN)
        q.mark_dead_terminal("/m/a", stage="parse", error="oversized")

        q.enqueue("/m/a", REASON_ON_CREATED)

        assert _row(db, "/m/a")["last_error_class"] is None


class TestDefer:
    """Infrastructure failures (embedder outage, auth misconfiguration)
    say nothing about the message. They must postpone it without
    spending its attempt budget, so no outage — however long — can
    dead-letter mail."""

    def test_defer_keeps_attempts_and_schedules_future_retry(self, tmp_path):
        db = Database(tmp_path / "q.db")
        q = _queue(db, max_attempts=2)
        q.enqueue("/m/a", REASON_INITIAL_SCAN)
        before = datetime.now(UTC)

        for _ in range(10):
            q.defer(
                "/m/a",
                stage="embed",
                error="APIConnectionError",
                error_class=ERROR_CLASS_RETRYABLE,
                delay_seconds=60,
            )

        row = _row(db, "/m/a")
        assert row["status"] == STATUS_QUEUED
        assert row["attempts"] == 0
        assert row["last_stage"] == "embed"
        assert row["last_error_class"] == ERROR_CLASS_RETRYABLE
        assert datetime.fromisoformat(row["next_attempt_at"]) >= before + timedelta(seconds=60)
        assert q.claim_next() is None

    def test_defer_records_operator_action_class(self, tmp_path):
        db = Database(tmp_path / "q.db")
        q = _queue(db)
        q.enqueue("/m/a", REASON_INITIAL_SCAN)

        q.defer(
            "/m/a",
            stage="embed",
            error="AuthenticationError: status=401",
            error_class=ERROR_CLASS_OPERATOR,
            delay_seconds=30,
        )

        assert _row(db, "/m/a")["last_error_class"] == ERROR_CLASS_OPERATOR


class TestRequeueDead:
    def _dead(self, db, q, path, *, terminal: bool):
        q.enqueue(path, REASON_INITIAL_SCAN)
        if terminal:
            q.mark_dead_terminal(path, stage="parse", error="oversized")
        else:
            for _ in range(q.max_attempts):
                q.mark_failed(path, stage="embed", error="boom")
        assert q.is_dead(path)

    def test_requeues_every_dead_row_with_fresh_budget(self, tmp_path):
        db = Database(tmp_path / "q.db")
        q = _queue(db)
        self._dead(db, q, "/m/a", terminal=False)
        self._dead(db, q, "/m/b", terminal=True)
        q.enqueue("/m/live", REASON_INITIAL_SCAN)
        q.mark_failed("/m/live", stage="parse", error="x")

        assert q.requeue_dead() == 2

        for path in ("/m/a", "/m/b"):
            row = _row(db, path)
            assert row["status"] == STATUS_QUEUED
            assert row["attempts"] == 0
            assert row["last_error_class"] is None
        assert _row(db, "/m/live")["attempts"] == 1
        assert q.stats() == {"queued": 3, "dead": 0}

    def test_class_filter_limits_requeue(self, tmp_path):
        db = Database(tmp_path / "q.db")
        q = _queue(db)
        self._dead(db, q, "/m/a", terminal=False)
        self._dead(db, q, "/m/b", terminal=True)

        assert q.requeue_dead(error_class=ERROR_CLASS_RETRYABLE) == 1

        assert q.has_pending_row("/m/a")
        assert q.is_dead("/m/b")


class TestInFlightAttempts:
    """#235: a message that kills or hangs the worker never reached
    ``mark_failed``, so it was re-claimed forever at ``attempts=0``. The
    message actually running carries one attempt while it runs, refunded
    when its step returns; a process that dies mid-step leaves it charged.
    A new ``IndexingQueue`` over the same database stands in for the
    restarted indexer."""

    def test_step_that_returns_leaves_attempts_unchanged(self, tmp_path):
        db = Database(tmp_path / "q.db")
        q = _queue(db)
        q.enqueue("/m/a", REASON_INITIAL_SCAN)

        assert q.begin_attempt("/m/a")
        assert _row(db, "/m/a")["attempts"] == 1
        q.end_attempt("/m/a")

        assert _row(db, "/m/a")["attempts"] == 0
        assert q.in_flight() is None

    def test_worker_death_mid_step_counts_one_attempt(self, tmp_path):
        db = Database(tmp_path / "q.db")
        _queue(db).enqueue("/m/a", REASON_INITIAL_SCAN)
        _queue(db).begin_attempt("/m/a")  # process dies here

        row = _row(db, "/m/a")
        assert row["attempts"] == 1
        assert row["status"] == STATUS_QUEUED

    def test_repeated_worker_death_dead_letters_the_message(self, tmp_path):
        db = Database(tmp_path / "q.db")
        _queue(db, max_attempts=3).enqueue("/m/a", REASON_INITIAL_SCAN)
        for _ in range(3):
            assert _queue(db, max_attempts=3).begin_attempt("/m/a")

        restarted = _queue(db, max_attempts=3)
        assert not restarted.begin_attempt("/m/a")

        row = _row(db, "/m/a")
        assert row["status"] == STATUS_DEAD
        assert row["attempts"] == 3
        assert row["last_stage"] == "interrupted"
        assert row["last_error_class"] == ERROR_CLASS_RETRYABLE
        assert "stopped while processing" in row["last_error"]
        assert restarted.in_flight() is None

    def test_only_the_running_message_is_charged(self, tmp_path):
        """Charging the whole claimed batch would let an ordinary restart
        dead-letter healthy batchmates."""
        db = Database(tmp_path / "q.db")
        q = _queue(db)
        for path in ("/m/a", "/m/b", "/m/c"):
            q.enqueue(path, REASON_INITIAL_SCAN)
        q.claim_batch(3)

        q.begin_attempt("/m/b")  # process dies here

        assert [_row(db, p)["attempts"] for p in ("/m/a", "/m/b", "/m/c")] == [0, 1, 0]

    def test_mark_failed_during_a_step_counts_one_attempt(self, tmp_path):
        db = Database(tmp_path / "q.db")
        q = _queue(db)
        q.enqueue("/m/a", REASON_INITIAL_SCAN)

        q.begin_attempt("/m/a")
        q.mark_failed("/m/a", stage="parse", error="boom")
        q.end_attempt("/m/a")

        assert _row(db, "/m/a")["attempts"] == 1
        assert q.in_flight() is None

    def test_defer_during_a_step_spends_no_attempt(self, tmp_path):
        db = Database(tmp_path / "q.db")
        q = _queue(db)
        q.enqueue("/m/a", REASON_INITIAL_SCAN)

        q.begin_attempt("/m/a")
        q.defer(
            "/m/a",
            stage="embed",
            error="APIConnectionError",
            error_class=ERROR_CLASS_RETRYABLE,
            delay_seconds=60,
        )

        assert _row(db, "/m/a")["attempts"] == 0

    def test_terminal_outcomes_during_a_step_clear_the_charge(self, tmp_path):
        db = Database(tmp_path / "q.db")
        q = _queue(db)
        for path in ("/m/ok", "/m/gone", "/m/huge"):
            q.enqueue(path, REASON_INITIAL_SCAN)

        q.begin_attempt("/m/ok")
        q.mark_succeeded("/m/ok")
        q.begin_attempt("/m/gone")
        q.mark_skipped("/m/gone", reason="moved")
        q.begin_attempt("/m/huge")
        q.mark_dead_terminal("/m/huge", stage="parse", error="oversized")

        assert _row(db, "/m/huge")["attempts"] == 1
        assert q.in_flight() is None

    def test_in_flight_reports_the_running_message_and_its_start(self, tmp_path):
        import time

        db = Database(tmp_path / "q.db")
        q = _queue(db)
        q.enqueue("/m/a", REASON_INITIAL_SCAN)
        before = time.monotonic()

        q.begin_attempt("/m/a")

        in_flight = q.in_flight()
        assert in_flight is not None
        assert in_flight[0] == "/m/a"
        assert before <= in_flight[1] <= time.monotonic()
