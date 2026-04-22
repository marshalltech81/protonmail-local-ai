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
        q.mark_failed("/m/backoff", stage="embed", error="ollama down")
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
        q.mark_failed("/m/fail", stage="embed", error="ollama down")
        row = db._conn.execute(
            "SELECT attempts, last_stage, last_error, status, next_attempt_at "
            "FROM indexing_jobs WHERE filepath = '/m/fail'"
        ).fetchone()
        assert row["attempts"] == 1
        assert row["last_stage"] == "embed"
        assert row["last_error"] == "ollama down"
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


class TestReEnqueueResetsState:
    def test_reenqueue_resets_failed_row_to_fresh_attempt(self, db: Database):
        """A newly-observed watchdog event is fresh intent. A previous
        failed row should be reset — attempts back to 0, next_attempt_at
        now — so the worker picks it up immediately."""
        q = _queue(db, max_attempts=5, base_backoff_seconds=3600)
        q.enqueue("/m/reset", REASON_ON_CREATED)
        q.mark_failed("/m/reset", stage="embed", error="ollama")
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
            q.mark_failed("/m/sustained", stage="embed", error="ollama")
        row = db._conn.execute(
            "SELECT next_attempt_at FROM indexing_jobs WHERE filepath = '/m/sustained'"
        ).fetchone()
        scheduled = datetime.fromisoformat(row["next_attempt_at"])
        gap = (scheduled - datetime.now(UTC)).total_seconds()
        # Six hours plus a small slack for test execution.
        assert gap <= 6 * 3600 + 5
