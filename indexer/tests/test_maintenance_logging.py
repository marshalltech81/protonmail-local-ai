"""
Log lines for the indexer's recurring work: failure recoveries (#873),
the queue heartbeat and maintenance summaries (#874), and WAL and
storage (#875).

Each line carries counts, durations, fixed text and type names only, so
every test also checks that a synthetic marker placed in mail, a path or
a provider error stays out of the log.
"""

import logging
import sqlite3
from types import SimpleNamespace

import pytest
from src import main
from src.database import Database

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
        assert extractors.drain_extractor_counts()["warnings_suppressed"] == 5


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
