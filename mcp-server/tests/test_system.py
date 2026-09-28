"""Tests for src/tools/system.py: the ``current`` rules behind
get_mailbox_status and the standalone helper used by ``make status``.

The registered MCP handler is covered in ``test_system_handlers.py``.
"""

from datetime import UTC, datetime, timedelta

import pytest
from src.tools.outputs import QueueCounts
from src.tools.system import get_mailbox_status, not_current_reasons

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
