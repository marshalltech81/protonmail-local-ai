"""Tests for src/tools/system.py: the ``current`` rules behind
get_mailbox_status and the standalone helper used by ``make status``.

The registered MCP handler is covered in ``test_system_handlers.py``.
"""

from datetime import UTC, datetime, timedelta

import pytest
from src.tools.outputs import QueueCounts
from src.tools.system import (
    FUTURE_SKEW_TOLERANCE_SECS,
    get_mailbox_status,
    not_current_reasons,
)

from tests.conftest import write_ingestion

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
IDLE = QueueCounts(pending=0, retrying=0, dead=0)


def _reasons(
    *,
    sync_age: timedelta | None = timedelta(seconds=30),
    interval: int | None = 60,
    indexer_age: timedelta | None = timedelta(seconds=10),
    queue: QueueCounts = IDLE,
) -> list[str]:
    return not_current_reasons(
        last_sync_at=NOW - sync_age if sync_age is not None else None,
        sync_interval_secs=interval,
        indexer_last_seen_at=NOW - indexer_age if indexer_age is not None else None,
        queue=queue,
        now=NOW,
    )


class TestNotCurrentReasons:
    def test_recent_sync_live_indexer_and_empty_queue_is_current(self):
        assert _reasons() == []

    def test_dead_letters_do_not_block_current(self):
        """Dead messages are a terminal state: nothing more will happen
        to them without an operator, so they are reported, not waited on."""
        assert _reasons(queue=QueueCounts(pending=0, retrying=0, dead=4)) == []

    def test_no_sync_recorded(self):
        assert _reasons(sync_age=None, interval=None) == [
            "no successful mail sync has been recorded"
        ]

    @pytest.mark.parametrize(
        ("interval", "age_secs", "stale"),
        [
            (60, 300, False),  # 5-minute floor applies to short intervals
            (60, 301, True),
            (600, 1800, False),  # three intervals for long ones
            (600, 1801, True),
        ],
    )
    def test_sync_staleness_threshold(self, interval, age_secs, stale):
        reasons = _reasons(sync_age=timedelta(seconds=age_secs), interval=interval)
        assert bool(reasons) is stale
        if stale:
            assert reasons[0].startswith("last successful mail sync was ")

    def test_indexer_never_reported(self):
        assert _reasons(sync_age=None, interval=None, indexer_age=None) == [
            "no successful mail sync has been recorded",
            "the indexer has not reported",
        ]

    def test_indexer_stale(self):
        assert _reasons(indexer_age=timedelta(minutes=11)) == ["the indexer last reported 11m ago"]

    def test_future_sync_stamp_is_not_current(self):
        """A stamp ahead of the checking clock (a clock rollback or a bad
        write) cannot vouch for a recent sync."""
        assert _reasons(sync_age=-timedelta(days=1)) == [
            "the last successful mail sync is timestamped 1d 0h in the future"
        ]

    def test_future_indexer_stamp_is_not_current(self):
        assert _reasons(indexer_age=-timedelta(days=1)) == [
            "the indexer last reported 1d 0h in the future"
        ]

    def test_both_stamps_in_the_future(self):
        assert _reasons(sync_age=-timedelta(days=1), indexer_age=-timedelta(minutes=10)) == [
            "the last successful mail sync is timestamped 1d 0h in the future",
            "the indexer last reported 10m in the future",
        ]

    @pytest.mark.parametrize(
        ("skew_secs", "flagged"),
        [
            (FUTURE_SKEW_TOLERANCE_SECS, False),
            (FUTURE_SKEW_TOLERANCE_SECS + 1, True),
        ],
    )
    def test_future_skew_tolerance(self, skew_secs, flagged):
        skew = -timedelta(seconds=skew_secs)
        reasons = _reasons(sync_age=skew, indexer_age=skew)
        assert len(reasons) == (2 if flagged else 0)

    def test_waiting_messages(self):
        reasons = _reasons(queue=QueueCounts(pending=3, retrying=2, dead=1))
        assert reasons == ["5 messages waiting to be indexed (3 pending, 2 retrying)"]


class TestGetMailboxStatusStandalone:
    def test_returns_real_status_from_populated_index(self, seeded_db, monkeypatch):
        write_ingestion(
            seeded_db.path,
            sync_completed_at=datetime.now(UTC).isoformat(),
            sync_interval_secs=60,
            indexer_seen_at=datetime.now(UTC).isoformat(),
        )
        monkeypatch.setenv("SQLITE_PATH", seeded_db.path)
        status = get_mailbox_status()
        assert status["status"] == "ok"
        assert status["current"] is True
        assert status["total_threads"] == 3
        assert status["total_messages"] == 3
        assert status["queue"] == {"pending": 0, "retrying": 0, "dead": 0}
        assert "checked_at" in status

    def test_empty_index_is_not_current(self, empty_db, monkeypatch):
        monkeypatch.setenv("SQLITE_PATH", empty_db.path)
        status = get_mailbox_status()
        assert status["status"] == "ok"
        assert status["current"] is False
        assert status["total_threads"] == 0
        assert status["last_sync_at"] is None

    def test_returns_error_when_db_cannot_be_opened(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "does-not-exist.db"))
        status = get_mailbox_status()
        assert status["status"] == "error"
        assert "error" in status

    def test_error_reports_type_name_without_exception_text(self, seeded_db, monkeypatch, caplog):
        # #732: a malformed stored value makes fromisoformat raise a
        # ValueError quoting it; only the type name may be reported.
        marker = "SYNTHETIC_STATUS_PRIVATE_MARKER_839"
        write_ingestion(
            seeded_db.path,
            sync_completed_at=marker,
            sync_interval_secs=60,
            indexer_seen_at=datetime.now(UTC).isoformat(),
        )
        monkeypatch.setenv("SQLITE_PATH", seeded_db.path)
        with caplog.at_level("DEBUG"):
            status = get_mailbox_status()
        assert status == {"status": "error", "error": "Mailbox status error: ValueError"}
        assert marker not in caplog.text
