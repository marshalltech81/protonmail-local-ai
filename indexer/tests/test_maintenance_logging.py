"""
Log lines for the indexer's recurring work: failure recoveries (#873),
the queue heartbeat and maintenance summaries (#874), and WAL and
storage (#875).

Each line carries counts, durations, fixed text and type names only, so
every test also checks that a synthetic marker placed in mail, a path or
a provider error stays out of the log.
"""

import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from src import main
from src.database import Database
from src.folder_watch import FolderWatchRefresher
from src.queue import (
    ERROR_CLASS_RETRYABLE,
    EXTRACTION_DEFERRED_ERROR,
    PERMISSION_DEFERRED_ERROR,
    REASON_INITIAL_SCAN,
    STAGE_EMBED,
    STAGE_EXTRACT,
    STAGE_PARSE,
    STAGE_TRASHED,
    IndexingQueue,
)
from src.threader import Threader
from src.timings import TimingAggregator

from tests.conftest import make_mock_embedder

MARKER = "SYNTHETIC_OBS_MARKER"


def _messages(caplog, prefix: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith(prefix)]


@pytest.fixture
def clock(monkeypatch):
    """A settable ``main._monotonic``."""
    now = {"t": 1000.0}
    monkeypatch.setattr(main, "_monotonic", lambda: now["t"])
    return now


# --- #873: embedder circuit breaker -------------------------------------


class TestBreakerRecovery:
    def test_closing_after_failures_logs_one_recovery_line(self, caplog):
        caplog.set_level(logging.INFO)
        b = main._EmbedOutageBreaker(base_seconds=30, cap_seconds=600)
        b.record_failure(100.0)
        b.record_failure(130.0)

        b.record_success(now=225.0)
        b.record_success(now=300.0)

        lines = _messages(caplog, "embedder recovered")
        assert [(r.levelno, r.getMessage()) for r in lines] == [
            (
                logging.INFO,
                "embedder recovered after 2 failure(s), paused 125s; indexing resumed",
            )
        ]

    def test_success_without_a_failure_logs_nothing(self, caplog):
        caplog.set_level(logging.DEBUG)
        main._EmbedOutageBreaker().record_success(now=5.0)
        assert not _messages(caplog, "embedder recovered")

    def test_a_second_outage_counts_from_its_own_start(self, caplog):
        caplog.set_level(logging.INFO)
        b = main._EmbedOutageBreaker()
        b.record_failure(0.0)
        b.record_success(now=10.0)
        b.record_failure(50.0)
        b.record_success(now=80.0)
        assert [r.getMessage() for r in _messages(caplog, "embedder recovered")] == [
            "embedder recovered after 1 failure(s), paused 10s; indexing resumed",
            "embedder recovered after 1 failure(s), paused 30s; indexing resumed",
        ]


# --- #873: recovery of the other recurring steps -----------------------


def _recoveries(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if " recovered after " in r.getMessage()]


class TestFailureStreak:
    def test_component_names_are_a_fixed_set(self):
        with pytest.raises(ValueError):
            main._FailureStreak(MARKER)

    def test_fail_fail_succeed_logs_one_line_with_count_and_duration(self, caplog, clock):
        caplog.set_level(logging.INFO)
        streak = main._FailureStreak("wal checkpoint")
        streak.failed()
        clock["t"] += 40
        streak.failed()
        clock["t"] += 50
        streak.succeeded()
        streak.succeeded()
        records = [r for r in caplog.records if " recovered after " in r.getMessage()]
        assert [(r.levelno, r.getMessage()) for r in records] == [
            (logging.INFO, "wal checkpoint recovered after 2 failure(s) over 90s")
        ]


class TestHealthFileRecovery:
    def test_fail_then_succeed_logs_one_recovery(self, tmp_path, monkeypatch, caplog, clock):
        caplog.set_level(logging.INFO)
        monkeypatch.setattr(main, "_ingestion_state", None)
        missing = tmp_path / MARKER / "health"
        monkeypatch.setattr(main, "INDEXER_HEALTH_FILE", missing)
        main._extraction_heartbeat()
        main._extraction_heartbeat()
        assert "health file refresh failed: FileNotFoundError" in caplog.text

        clock["t"] += 7
        monkeypatch.setattr(main, "INDEXER_HEALTH_FILE", tmp_path / "health")
        main.touch_health_file()
        main.touch_health_file()

        assert _recoveries(caplog) == ["health file refresh recovered after 2 failure(s) over 7s"]
        assert MARKER not in caplog.text

    def test_failure_warnings_are_rate_limited(self, tmp_path, monkeypatch, caplog):
        """A heartbeat runs per attachment page, so a lost health file
        would log once per page without the shared limiter."""
        caplog.set_level(logging.INFO)
        monkeypatch.setattr(main, "_ingestion_state", None)
        monkeypatch.setattr(main, "INDEXER_HEALTH_FILE", tmp_path / "gone" / "health")
        from src import extractors

        for _ in range(extractors._WARNINGS_PER_WINDOW + 5):
            main._extraction_heartbeat()
        assert len(_messages(caplog, "health file refresh failed")) == (
            extractors._WARNINGS_PER_WINDOW
        )
        # Codex round 2 on #904: not an attachment line, so it must not
        # turn the attachments aggregate into a WARNING.
        assert extractors.drain_extractor_counts()["warnings_suppressed"] == 0
        assert extractors.drain_suppressed_lines() == 5


class TestIngestionStateRecovery:
    def test_fail_then_succeed_logs_one_recovery(self, tmp_path, caplog, clock):
        caplog.set_level(logging.INFO)
        calls = {"n": 0}

        def record(**_kw):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise sqlite3.OperationalError(MARKER)

        db = SimpleNamespace(record_ingestion_state=record)
        recorder = main._IngestionStateRecorder(db, tmp_path, interval_secs=30)  # type: ignore[arg-type]
        for i in range(3):
            recorder.maybe_record(now=float(i))
        clock["t"] += 12
        recorder.maybe_record(now=3.0)
        recorder.maybe_record(now=100.0)

        assert len(_messages(caplog, "recording ingestion state failed")) == 3
        assert _recoveries(caplog) == [
            "ingestion state recording recovered after 3 failure(s) over 12s"
        ]
        assert "recording ingestion state failed: OperationalError" in caplog.text
        assert MARKER not in caplog.text


class TestWatchRefreshRecovery:
    def test_fail_then_succeed_logs_one_recovery(self, monkeypatch, caplog, clock):
        caplog.set_level(logging.INFO)
        outcomes = [PermissionError(13, "denied", MARKER), None]

        def refresh(*_a, **_kw):
            err = outcomes.pop(0)
            if err is not None:
                raise err
            return False

        monkeypatch.setattr(main, "_refresh_folder_watches", refresh)
        watches = SimpleNamespace(watched_dirs=4)
        main._run_watch_refresh(watches, None, None, skip_trashed=False)  # type: ignore[arg-type]
        clock["t"] += 3
        main._run_watch_refresh(watches, None, None, skip_trashed=False)  # type: ignore[arg-type]

        assert "Maildir watch refresh failed: PermissionError" in caplog.text
        assert _recoveries(caplog) == ["Maildir watch refresh recovered after 1 failure(s) over 3s"]
        assert MARKER not in caplog.text


class _FakeReconciler:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.config = SimpleNamespace(force=False)

    def sweep(self):
        err = self.outcomes.pop(0)
        if err is not None:
            raise err
        return {"tombstoned": 2, "cleared": 1, "renamed": 3, "missing": 0}

    def reap(self):
        return {"threads_reaped": 1, "threads_rebuilt": 0, "aborted": False, "blocked_threads": 0}


class TestReconcileRecovery:
    def test_fail_then_succeed_logs_one_recovery(self, tmp_path, caplog, clock):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "mail.db")
        reconciler = _FakeReconciler([OSError(5, "io", MARKER), None])
        main._run_periodic_reconcile(reconciler, db)  # type: ignore[arg-type]
        clock["t"] += 900
        main._run_periodic_reconcile(reconciler, db)  # type: ignore[arg-type]

        assert len(_messages(caplog, "periodic reconciliation failed")) == 1
        assert _recoveries(caplog) == [
            "periodic reconciliation recovered after 1 failure(s) over 900s"
        ]
        assert "periodic reconciliation failed: OSError" in caplog.text
        assert MARKER not in caplog.text
        db.close()


class TestRescanRecovery:
    def test_fail_then_succeed_logs_one_recovery(self, tmp_path, monkeypatch, caplog, clock):
        caplog.set_level(logging.INFO)
        outcomes = [OSError(13, "denied", MARKER), None]

        def walk(*_a, **_kw):
            err = outcomes.pop(0)
            if err is not None:
                raise err
            return 0

        monkeypatch.setattr(main, "_enqueue_unindexed_messages", walk)
        monkeypatch.setattr(main, "sweep_paths", lambda *_a, **_kw: {})
        state = main._IngestionStateRecorder(SimpleNamespace(), tmp_path)  # type: ignore[arg-type]
        main._run_periodic_rescan(None, None, state, skip_trashed=False)  # type: ignore[arg-type]
        clock["t"] += 1800
        main._run_periodic_rescan(None, None, state, skip_trashed=False)  # type: ignore[arg-type]

        assert len(_messages(caplog, "periodic Maildir rescan failed")) == 1
        assert _recoveries(caplog) == [
            "periodic Maildir rescan recovered after 1 failure(s) over 1800s"
        ]
        assert "periodic Maildir rescan failed: PermissionError" in caplog.text
        assert MARKER not in caplog.text


class TestRescanRenameSweepFailure:
    def test_a_sweep_failure_does_not_stop_the_walk(self, tmp_path, monkeypatch, caplog):
        """Codex round 4 on #1134: the rename sweep shared the walk's
        ``try``, so a sweep failure (an unreadable directory) skipped the
        walk that finds missed new mail in every other folder."""
        caplog.set_level(logging.INFO)
        walked: list[bool] = []

        def sweep(*_a, **_kw):
            raise OSError(13, "denied", MARKER)

        monkeypatch.setattr(main, "sweep_paths", sweep)
        monkeypatch.setattr(
            main, "_enqueue_unindexed_messages", lambda *_a, **_kw: walked.append(True) or 0
        )
        state = main._IngestionStateRecorder(SimpleNamespace(), tmp_path)  # type: ignore[arg-type]
        main._run_periodic_rescan(None, None, state, skip_trashed=False)  # type: ignore[arg-type]

        assert walked == [True]
        lines = [r for r in caplog.records if "rename sweep failed" in r.getMessage()]
        assert [(r.levelno, r.getMessage()) for r in lines] == [
            (logging.WARNING, "periodic rename sweep failed: PermissionError")
        ]
        assert not _messages(caplog, "periodic Maildir rescan failed")
        assert MARKER not in caplog.text

    def test_fail_then_succeed_logs_one_recovery(self, tmp_path, monkeypatch, caplog, clock):
        """Codex round 5 on #1134: the sweep's failure had no recovery
        line once it ran on its own."""
        caplog.set_level(logging.INFO)
        outcomes = [OSError(13, "denied", MARKER), None]

        def sweep(*_a, **_kw):
            err = outcomes.pop(0)
            if err is not None:
                raise err
            return {}

        monkeypatch.setattr(main, "sweep_paths", sweep)
        monkeypatch.setattr(main, "_enqueue_unindexed_messages", lambda *_a, **_kw: 0)
        state = main._IngestionStateRecorder(SimpleNamespace(), tmp_path)  # type: ignore[arg-type]
        main._run_periodic_rescan(None, None, state, skip_trashed=False)  # type: ignore[arg-type]
        clock["t"] += 1800
        main._run_periodic_rescan(None, None, state, skip_trashed=False)  # type: ignore[arg-type]

        assert _recoveries(caplog) == [
            "periodic rename sweep recovered after 1 failure(s) over 1800s"
        ]
        assert MARKER not in caplog.text


class TestWalCheckpointRecovery:
    def test_fail_then_succeed_logs_one_recovery(self, tmp_path, monkeypatch, caplog, clock):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "mail.db")
        real = db.wal_checkpoint_truncate
        outcomes = [sqlite3.OperationalError(MARKER), None]

        def checkpoint():
            err = outcomes.pop(0)
            if err is not None:
                raise err
            return real()

        monkeypatch.setattr(db, "wal_checkpoint_truncate", checkpoint)
        main._run_wal_maintenance(db)
        clock["t"] += 600
        main._run_wal_maintenance(db)

        assert _recoveries(caplog) == ["wal checkpoint recovered after 1 failure(s) over 600s"]
        assert "wal checkpoint failed: OperationalError" in caplog.text
        assert MARKER not in caplog.text
        db.close()


class TestPruneRecovery:
    def test_fail_then_succeed_logs_one_recovery(self, tmp_path, monkeypatch, caplog, clock):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "mail.db")
        outcomes = [sqlite3.OperationalError(MARKER), None]

        def prune(**_kw):
            err = outcomes.pop(0)
            if err is not None:
                raise err
            return 0

        monkeypatch.setattr(db, "prune_reaped_messages", prune)
        main._prune_reaped_records(db)
        clock["t"] += 5
        main._prune_reaped_records(db)

        assert _recoveries(caplog) == ["reaped-record prune recovered after 1 failure(s) over 5s"]
        assert MARKER not in caplog.text
        db.close()


# --- #874: queue heartbeat -----------------------------------------------


def _seed_queue(tmp_path) -> tuple[Database, IndexingQueue, datetime]:
    """One row in each heartbeat bucket, paths carrying the marker:
    two never tried (one due two minutes ago), one retrying, one
    retrying a permission error past its deferral window, one deferred
    for permissions, one parked trashed file, one continued for deferred
    attachment extraction (#1236), one dead."""
    db = Database(tmp_path / "mail.db")
    queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
    now = datetime.now(UTC)
    path = f"/maildir/{MARKER}/cur/%s"
    queue.enqueue(path % "old", REASON_INITIAL_SCAN, due_at=now - timedelta(seconds=120))
    queue.enqueue(path % "new", REASON_INITIAL_SCAN)
    queue.enqueue(path % "retry", REASON_INITIAL_SCAN)
    queue.mark_failed(path % "retry", stage="db_write", error="OperationalError")
    queue.enqueue(path % "perm_retry", REASON_INITIAL_SCAN)
    queue.mark_failed(
        path % "perm_retry",
        stage=STAGE_PARSE,
        error="PermissionError: [Errno 13] Permission denied",
    )
    queue.enqueue(path % "perm", REASON_INITIAL_SCAN)
    queue.defer(
        path % "perm",
        stage=STAGE_PARSE,
        error=PERMISSION_DEFERRED_ERROR,
        error_class=ERROR_CLASS_RETRYABLE,
        delay_seconds=60,
    )
    queue.enqueue(path % "trash", REASON_INITIAL_SCAN)
    queue.defer(
        path % "trash",
        stage=STAGE_TRASHED,
        error="file is T-flagged; parked until reaped or restored",
        error_class=ERROR_CLASS_RETRYABLE,
        delay_seconds=3600,
    )
    queue.enqueue(path % "extract", REASON_INITIAL_SCAN)
    queue.defer(
        path % "extract",
        stage=STAGE_EXTRACT,
        error=EXTRACTION_DEFERRED_ERROR,
        error_class=ERROR_CLASS_RETRYABLE,
        delay_seconds=3600,
    )
    queue.enqueue(path % "dead", REASON_INITIAL_SCAN)
    queue.mark_dead_terminal(path % "dead", stage="parse", error="too large")
    return db, queue, now


class TestQueueHeartbeatCounts:
    def test_one_row_per_bucket(self, tmp_path):
        db, queue, now = _seed_queue(tmp_path)
        counts = queue.heartbeat_counts(now=now + timedelta(seconds=5))
        assert counts == {
            "pending": 2,
            "retrying": 2,
            "deferred_permission": 1,
            "parked_trashed": 1,
            "extraction_deferred": 1,
            "dead": 1,
            "oldest_due_age": 125,
            "reparse": 0,
            "reparse_parked_trashed": 0,
            "reparse_dead": 0,
        }
        db.close()

    def test_empty_queue(self, tmp_path):
        db = Database(tmp_path / "mail.db")
        counts = IndexingQueue(db).heartbeat_counts()
        assert counts == {
            "pending": 0,
            "retrying": 0,
            "deferred_permission": 0,
            "parked_trashed": 0,
            "extraction_deferred": 0,
            "dead": 0,
            "oldest_due_age": 0,
            "reparse": 0,
            "reparse_parked_trashed": 0,
            "reparse_dead": 0,
        }
        db.close()

    def test_defer_counts_by_stage_and_resets(self, tmp_path):
        db, queue, _now = _seed_queue(tmp_path)
        queue.defer(
            f"/maildir/{MARKER}/cur/new",
            stage=STAGE_EMBED,
            error="APIConnectionError",
            error_class=ERROR_CLASS_RETRYABLE,
            delay_seconds=30,
        )
        assert queue.drain_deferrals() == {"parse": 1, "embed": 1, "trashed": 1}
        assert queue.drain_deferrals() == {"parse": 0, "embed": 0, "trashed": 0}
        db.close()


class TestQueueHeartbeatLine:
    def test_logs_counts_at_the_interval(self, tmp_path, caplog, clock):
        caplog.set_level(logging.INFO)
        db, queue, _now = _seed_queue(tmp_path)
        # The queue's own retry and dead-letter lines carry file paths.
        caplog.clear()

        main._maybe_log_queue_heartbeat(queue)
        clock["t"] += main.QUEUE_HEARTBEAT_INTERVAL_SECS - 1
        main._maybe_log_queue_heartbeat(queue)
        lines = _messages(caplog, "queue: ")
        assert len(lines) == 1
        assert lines[0].levelno == logging.INFO
        assert re.fullmatch(
            r"queue: pending=2 retrying=2 deferred_permission=1 parked_trashed=1 "
            r"extraction_deferred=1 dead=1 "
            r"oldest_due_age=1\d\ds; deferrals since last heartbeat: parse=1 embed=0 trashed=1; "
            r"suppressed_lines=0",
            lines[0].getMessage(),
        )

        clock["t"] += 1
        main._maybe_log_queue_heartbeat(queue)
        lines = _messages(caplog, "queue: ")
        assert len(lines) == 2
        assert lines[1].getMessage().endswith("parse=0 embed=0 trashed=0; suppressed_lines=0")
        assert MARKER not in caplog.text
        db.close()

    def test_reports_suppressed_indexer_lines_since_the_last_heartbeat(
        self, tmp_path, caplog, clock, monkeypatch
    ):
        caplog.set_level(logging.INFO)
        from src import extractors
        from src.rate_limited_log import LineBudget

        db, queue, _now = _seed_queue(tmp_path)
        # The seeding's own retry and dead-letter lines spend the shared
        # budget (#1320); start this test's burst on a fresh one.
        monkeypatch.setattr(
            extractors,
            "_LINE_BUDGET",
            LineBudget(
                limit=extractors._WARNINGS_PER_WINDOW,
                window_secs=extractors._WARNING_WINDOW_SECS,
                buckets=(extractors._ATTACHMENT_LINES, extractors._OTHER_LINES),
            ),
        )
        for _ in range(extractors._WARNINGS_PER_WINDOW + 3):
            extractors.warn_rate_limited(main.log, "synthetic repeated line", attachment=False)
        caplog.clear()

        main._maybe_log_queue_heartbeat(queue)
        clock["t"] += main.QUEUE_HEARTBEAT_INTERVAL_SECS
        main._maybe_log_queue_heartbeat(queue)
        lines = [r.getMessage() for r in _messages(caplog, "queue: ")]
        assert lines[0].endswith("; suppressed_lines=3")
        assert lines[1].endswith("; suppressed_lines=0")
        assert extractors.drain_extractor_counts()["warnings_suppressed"] == 0
        db.close()

    def test_a_failed_count_query_logs_its_type(self, tmp_path, monkeypatch, caplog):
        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db)

        def boom(**_kw):
            raise sqlite3.OperationalError(MARKER)

        monkeypatch.setattr(queue, "heartbeat_counts", boom)
        main._maybe_log_queue_heartbeat(queue)
        assert "queue heartbeat failed: OperationalError" in caplog.text
        assert MARKER not in caplog.text
        db.close()

    def test_the_initial_drain_logs_the_heartbeat(self, tmp_path, caplog):
        """The initial index can run for hours inside one drain call."""
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db)
        main._drain_queue_batched(
            db,
            make_mock_embedder(),
            Threader(db),
            queue,
            batch_size=10,
            timing_aggregator=TimingAggregator(window=10),
        )
        assert len(_messages(caplog, "queue: pending=0 ")) == 1
        db.close()


# --- #874: re-extract sweep ---------------------------------------------


class TestReextractSweepDeadSkips:
    def _stale(self, db, monkeypatch, paths):
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", False)
        monkeypatch.setattr(main, "is_stale_extractor", lambda *_a, **_kw: True)
        monkeypatch.setattr(db, "get_extractor_names", lambda: ["pdf@1"])
        monkeypatch.setattr(db, "find_filepaths_with_extractors", lambda _names: paths)
        monkeypatch.setattr(db, "find_no_extractor_attachment_filepaths", lambda _q: set())
        monkeypatch.setattr(db, "find_fitting_too_large_attachment_filepaths", lambda _m: set())

    def test_dead_lettered_files_are_counted(self, tmp_path, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db)
        paths = [f"/maildir/{MARKER}/cur/{n}" for n in ("a", "b", "c")]
        for p in paths[1:]:
            queue.enqueue(p, REASON_INITIAL_SCAN)
            queue.mark_dead_terminal(p, stage="parse", error="x")
        self._stale(db, monkeypatch, paths)
        caplog.clear()

        assert main._requeue_stale_extractions(db, queue) == 1
        lines = _messages(caplog, "re-queued ")
        assert len(lines) == 1
        assert lines[0].levelno == logging.WARNING
        assert (
            lines[0]
            .getMessage()
            .endswith("; skipped 2 dead-lettered (run make requeue-dead to refresh them).")
        )
        assert MARKER not in caplog.text
        db.close()

    def test_only_dead_lettered_files_still_log(self, tmp_path, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db)
        path = f"/maildir/{MARKER}/cur/a"
        queue.enqueue(path, REASON_INITIAL_SCAN)
        queue.mark_dead_terminal(path, stage="parse", error="x")
        self._stale(db, monkeypatch, [path])

        assert main._requeue_stale_extractions(db, queue) == 0
        lines = _messages(caplog, "re-queued 0 message(s)")
        assert len(lines) == 1
        assert "skipped 1 dead-lettered" in lines[0].getMessage()
        db.close()

    def test_no_dead_skips_keeps_info(self, tmp_path, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db)
        self._stale(db, monkeypatch, ["/maildir/x/cur/a"])

        assert main._requeue_stale_extractions(db, queue) == 1
        (line,) = _messages(caplog, "re-queued ")
        assert line.levelno == logging.INFO
        assert line.getMessage().endswith(
            "; skipped 0 dead-lettered (run make requeue-dead to refresh them)."
        )
        db.close()


# --- #874: maintenance pass summaries ------------------------------------

_MS = r"ms=\d+"


class TestRescanSummary:
    def test_each_pass_logs_seen_and_queued(self, tmp_path, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        maildir = tmp_path / "maildir"
        cur = maildir / MARKER / "cur"
        cur.mkdir(parents=True)
        for name in ("a", "b", "c"):
            (cur / name).write_bytes(b"Subject: x\r\n\r\nbody\r\n")
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db)
        queue.enqueue(str(cur / "c"), REASON_INITIAL_SCAN)
        queue.mark_dead_terminal(str(cur / "c"), stage="parse", error="x")
        state = main._IngestionStateRecorder(db, maildir)
        caplog.clear()

        main._run_periodic_rescan(db, queue, state, skip_trashed=False)
        # A pass that finds nothing new still logs.
        main._run_periodic_rescan(db, queue, state, skip_trashed=False)

        lines = [r.getMessage() for r in _messages(caplog, "maintenance pass=rescan")]
        assert len(lines) == 2
        assert re.fullmatch(
            rf"maintenance pass=rescan {_MS} seen=3 queued=2 skipped_dead=1", lines[0]
        )
        assert re.fullmatch(
            rf"maintenance pass=rescan {_MS} seen=3 queued=0 skipped_dead=1", lines[1]
        )
        assert MARKER not in caplog.text
        db.close()

    def test_a_failed_pass_logs_no_summary(self, monkeypatch, caplog, tmp_path):
        caplog.set_level(logging.INFO)

        def walk(*_a, **_kw):
            raise OSError(5, "io", MARKER)

        monkeypatch.setattr(main, "_iter_maildir_messages", walk)
        db = Database(tmp_path / "mail.db")
        state = main._IngestionStateRecorder(db, tmp_path)
        main._run_periodic_rescan(db, IndexingQueue(db), state, skip_trashed=False)
        assert not _messages(caplog, "maintenance pass=rescan")
        assert MARKER not in caplog.text
        db.close()


class TestReconcileSummary:
    def test_logs_sweep_and_reap_counts(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "mail.db")
        main._run_periodic_reconcile(_FakeReconciler([None]), db)  # type: ignore[arg-type]
        (line,) = _messages(caplog, "maintenance pass=reconcile")
        assert line.levelno == logging.INFO
        assert re.fullmatch(
            rf"maintenance pass=reconcile {_MS} tombstoned=2 cleared=1 renamed=3 missing=0 "
            r"threads_reaped=1 threads_rebuilt=0 blocked_threads=0 brake=ok",
            line.getMessage(),
        )
        db.close()

    @pytest.mark.parametrize(
        ("reap", "force", "brake"),
        [
            ({"threads_reaped": 0, "threads_rebuilt": 0, "aborted": False}, False, "ok"),
            (
                {
                    "threads_reaped": 0,
                    "threads_rebuilt": 0,
                    "aborted": True,
                    "tombstones_pending": 9,
                },
                False,
                "tripped",
            ),
            ({"threads_reaped": 0, "threads_rebuilt": 0, "aborted": False}, True, "forced"),
        ],
    )
    def test_brake_state(self, tmp_path, caplog, reap, force, brake):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "mail.db")
        reconciler = _FakeReconciler([None])
        reconciler.config.force = force
        reconciler.reap = lambda: reap  # type: ignore[method-assign]
        main._run_periodic_reconcile(reconciler, db)  # type: ignore[arg-type]
        (line,) = _messages(caplog, "maintenance pass=reconcile")
        assert line.getMessage().endswith(f"blocked_threads=0 brake={brake}")
        db.close()

    def test_archive_mode_logs_no_reconcile_summary(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "mail.db")
        main._run_periodic_reconcile(None, db)
        assert not _messages(caplog, "maintenance pass=reconcile")
        db.close()


class TestWatchRefreshSummary:
    def test_periodic_refresh_logs_the_watch_count(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        monkeypatch.setattr(main, "_refresh_folder_watches", lambda *_a, **_kw: False)
        watches = SimpleNamespace(watched_dirs=7)
        main._run_watch_refresh(watches, None, None, skip_trashed=False, summary=True)  # type: ignore[arg-type]
        main._run_watch_refresh(watches, None, None, skip_trashed=False)  # type: ignore[arg-type]
        lines = [r.getMessage() for r in _messages(caplog, "maintenance pass=watch_refresh")]
        assert len(lines) == 1
        assert re.fullmatch(rf"maintenance pass=watch_refresh {_MS} watches=7", lines[0])

    def test_watched_dirs_counts_the_scheduled_directories(self, tmp_path):
        (tmp_path / "INBOX" / "cur").mkdir(parents=True)
        (tmp_path / "INBOX" / "new").mkdir()
        observer = SimpleNamespace(schedule=lambda *_a, **_kw: object(), unschedule=lambda _w: None)
        refresher = FolderWatchRefresher(tmp_path, observer, None)  # type: ignore[arg-type]
        assert refresher.watched_dirs == 0
        refresher.start()
        assert refresher.watched_dirs == 3


# --- #875: WAL checkpoint and storage ------------------------------------


def _busy_db(tmp_path, monkeypatch, results):
    """A real database whose checkpoint returns ``results`` in turn."""
    db = Database(tmp_path / f"{MARKER}.db")
    outcomes = list(results)
    monkeypatch.setattr(db, "wal_checkpoint_truncate", lambda: outcomes.pop(0))
    return db


class TestWalBusyStreak:
    def test_warns_from_the_kth_busy_pass_then_logs_the_recovery(
        self, tmp_path, monkeypatch, caplog
    ):
        caplog.set_level(logging.INFO)
        k = main.WAL_BUSY_WARN_AFTER
        db = _busy_db(tmp_path, monkeypatch, [(1, 40 + i, 0) for i in range(k + 1)] + [(0, 0, 0)])
        caplog.clear()  # the database's own startup line names its path
        for _ in range(k + 2):
            main._run_wal_maintenance(db)

        warnings = [
            (r.levelno, r.getMessage()) for r in _messages(caplog, "wal checkpoint blocked")
        ]
        assert warnings == [
            (logging.WARNING, f"wal checkpoint blocked {k} times in a row; WAL={40 + k - 1} pages"),
            (logging.WARNING, f"wal checkpoint blocked {k + 1} times in a row; WAL={40 + k} pages"),
        ]
        assert [r.getMessage() for r in _messages(caplog, "wal checkpoint unblocked")] == [
            f"wal checkpoint unblocked after {k + 1} blocked pass(es)"
        ]
        assert MARKER not in caplog.text
        db.close()

    def test_a_short_busy_run_neither_warns_nor_logs_recovery(self, tmp_path, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        k = main.WAL_BUSY_WARN_AFTER
        db = _busy_db(tmp_path, monkeypatch, [(1, 5, 0)] * (k - 1) + [(0, 0, 0)])
        for _ in range(k):
            main._run_wal_maintenance(db)
        assert not _messages(caplog, "wal checkpoint blocked")
        assert not _messages(caplog, "wal checkpoint unblocked")
        db.close()


class TestStorageLine:
    def test_logs_sizes_once_per_pass(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / f"{MARKER}.db")
        caplog.clear()  # the database's own startup line names its path
        main._run_wal_maintenance(db)
        lines = _messages(caplog, "storage: ")
        assert len(lines) == 1
        assert lines[0].levelno == logging.INFO
        assert re.fullmatch(r"storage: db=\d+MB wal=\d+MB free_disk=\d+MB", lines[0].getMessage())
        assert MARKER not in caplog.text
        db.close()

    def test_sizes_are_in_mebibytes(self, tmp_path, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "mail.db")
        sizes = {str(db.path): 5 * 1024 * 1024 + 1, f"{db.path}-wal": 3 * 1024 * 1024}
        monkeypatch.setattr(main.os, "stat", lambda p: SimpleNamespace(st_size=sizes[str(p)]))
        monkeypatch.setattr(
            main.shutil, "disk_usage", lambda _p: SimpleNamespace(free=7 * 1024 * 1024)
        )
        main._log_storage(db)
        assert [r.getMessage() for r in _messages(caplog, "storage: ")] == [
            "storage: db=5MB wal=3MB free_disk=7MB"
        ]
        db.close()

    def test_a_missing_wal_is_zero(self, tmp_path, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        db = Database(tmp_path / "mail.db")
        real_stat = main.os.stat

        def stat(p):
            if str(p).endswith("-wal"):
                raise FileNotFoundError(2, "missing", str(p))
            return real_stat(p)

        monkeypatch.setattr(main.os, "stat", stat)
        main._log_storage(db)
        (line,) = _messages(caplog, "storage: ")
        assert " wal=0MB " in line.getMessage()
        db.close()

    def test_a_stat_failure_logs_its_type(self, tmp_path, monkeypatch, caplog):
        db = Database(tmp_path / "mail.db")

        def stat(p):
            raise PermissionError(13, "denied", MARKER)

        monkeypatch.setattr(main.os, "stat", stat)
        main._log_storage(db)
        assert "storage: size check failed (PermissionError)" in caplog.text
        assert MARKER not in caplog.text
        db.close()


# --- #934: watcher rename failures log the type only --------------------


class TestOnMovedFailureLogsTypeOnly:
    """A failed rename update in the watcher logs the exception type,
    never its text."""

    def _moved(self, tmp_path):
        src = tmp_path / "INBOX" / "cur" / "1700000000.M1.host:2,S"
        return SimpleNamespace(
            src_path=str(src),
            dest_path=str(src.with_name("1700000000.M1.host:2,RS")),
            is_directory=False,
        )

    def test_reconciler_handle_moved_failure(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(main, "MAILDIR_PATH", tmp_path)
        calls = []

        def handle_moved(*a, **_kw):
            calls.append(a)
            raise sqlite3.OperationalError(MARKER)

        db = SimpleNamespace(is_indexed=lambda _p: True)
        reconciler = SimpleNamespace(handle_moved=handle_moved)
        handler = main.MaildirHandler(db, None, reconciler=reconciler)  # type: ignore[arg-type]

        handler.on_moved(self._moved(tmp_path))

        assert len(calls) == 1
        assert [(r.levelno, r.getMessage()) for r in _messages(caplog, "reconciler on_moved")] == [
            (logging.ERROR, "reconciler on_moved failed: OperationalError")
        ]
        assert MARKER not in caplog.text

    def test_update_filepath_failure(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(main, "MAILDIR_PATH", tmp_path)
        calls = []

        def update_filepath(*a, **_kw):
            calls.append(a)
            raise ValueError(MARKER)

        db = SimpleNamespace(
            is_indexed=lambda _p: True,
            has_pending_deletion=lambda _p: False,
            update_filepath=update_filepath,
        )
        handler = main.MaildirHandler(db, None)  # type: ignore[arg-type]

        handler.on_moved(self._moved(tmp_path))

        assert len(calls) == 1
        assert [(r.levelno, r.getMessage()) for r in _messages(caplog, "update_filepath")] == [
            (logging.ERROR, "update_filepath failed on rename: ValueError")
        ]
        assert MARKER not in caplog.text

    def test_tombstone_lookup_failure(self, tmp_path, monkeypatch, caplog):
        """Review round 1 on #860: the leftover-tombstone lookup that
        precedes ``update_filepath`` on an archive-mode restore runs
        inside the same error boundary. watchdog does not catch handler
        exceptions, so one escaping here would end the observer thread."""
        monkeypatch.setattr(main, "MAILDIR_PATH", tmp_path)
        calls = []

        def has_pending_deletion(_p):
            raise sqlite3.OperationalError(MARKER)

        db = SimpleNamespace(
            is_indexed=lambda _p: True,
            has_pending_deletion=has_pending_deletion,
            update_filepath=lambda *a, **_kw: calls.append(a),
        )
        handler = main.MaildirHandler(db, None)  # type: ignore[arg-type]

        handler.on_moved(self._moved(tmp_path))

        assert calls == []
        assert [(r.levelno, r.getMessage()) for r in _messages(caplog, "update_filepath")] == [
            (logging.ERROR, "update_filepath failed on rename: OperationalError")
        ]
        assert MARKER not in caplog.text
