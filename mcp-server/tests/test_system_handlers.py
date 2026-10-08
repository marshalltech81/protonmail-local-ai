"""
Tests for the registered ``get_mailbox_status`` handler in
src/tools/system.py, which the MCP client / LLM calls.

``test_system.py`` covers the ``current`` rules and the standalone
helper used by the Makefile.
"""

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastmcp.exceptions import ToolError
from src.tools.system import register_system_tools

from tests.conftest import write_ingestion


def _handler(fake_server, db):
    register_system_tools(fake_server, db)
    return fake_server.tools["get_mailbox_status"]


def _text(result) -> str:
    """Extract the prose from a tool's ``CallToolResult``."""
    assert len(result.content) == 1
    return result.content[0].text


def _ago(**kw) -> str:
    return (datetime.now(UTC) - timedelta(**kw)).isoformat()


class TestGetMailboxStatus:
    @pytest.mark.parametrize(
        ("commit", "expected"),
        [
            ("abc1234", "abc1234"),
            ("abc1234-dirty", "abc1234-dirty"),
            (None, "unknown"),
            ("", "unknown"),
            ("abc\nforged line", "unknown"),
        ],
    )
    def test_reports_server_version(self, fake_server, seeded_db, monkeypatch, commit, expected):
        if commit is None:
            monkeypatch.delenv("GIT_COMMIT", raising=False)
        else:
            monkeypatch.setenv("GIT_COMMIT", commit)
        out = asyncio.run(_handler(fake_server, seeded_db)())
        assert out.structured_content["server_version"] == expected
        assert f"Server version: {expected}" in _text(out)

    def test_current_index(self, fake_server, seeded_db):
        write_ingestion(
            seeded_db.path,
            sync_completed_at=_ago(seconds=30),
            sync_interval_secs=60,
            indexer_seen_at=_ago(seconds=5),
        )
        out = asyncio.run(_handler(fake_server, seeded_db)())
        text = _text(out)
        assert out.structured_content["current"] is True
        assert out.structured_content["not_current_reasons"] == []
        assert "Current:        yes" in text
        # seeded_db has 3 threads and 3 messages.
        assert "Total threads:  3" in text
        assert "Total messages: 3" in text
        assert "Checked at:" in text

    def test_not_current_lists_every_reason(self, fake_server, seeded_db):
        write_ingestion(
            seeded_db.path,
            sync_completed_at=_ago(hours=2),
            sync_interval_secs=60,
            indexer_seen_at=_ago(seconds=5),
            jobs=(("queued", 0, None), ("queued", 1, "retryable"), ("dead", 5, "retryable")),
        )
        out = asyncio.run(_handler(fake_server, seeded_db)())
        text = _text(out)
        reasons = out.structured_content["not_current_reasons"]
        assert out.structured_content["current"] is False
        assert len(reasons) == 2
        assert "Current:        no" in text
        for reason in reasons:
            assert f"  - {reason}" in text
        assert (
            "Queue:          1 pending, 1 retrying, 0 deferred, 0 parked (trashed), 1 dead" in text
        )
        assert "1 message failed permanently and is incompletely indexed" in text
        assert "reparse" not in text

    def test_a_reparse_backlog_is_told_apart(self, fake_server, seeded_db):
        """#1078: a reparse backlog is not an empty queue, and not new
        mail either."""
        write_ingestion(
            seeded_db.path,
            sync_completed_at=_ago(seconds=30),
            sync_interval_secs=60,
            indexer_seen_at=_ago(seconds=5),
            jobs=(("queued", 0, None, "reparse"), ("queued", 0, None)),
        )
        out = asyncio.run(_handler(fake_server, seeded_db)())
        text = _text(out)
        assert out.structured_content["current"] is False
        assert out.structured_content["queue"]["reparse"] == 1
        assert (
            "Queue:          2 pending, 0 retrying, 0 deferred, 0 parked (trashed), 0 dead" in text
        )
        assert (
            "  1 waiting message is already indexed and being reparsed after an upgrade: "
            "search finds it, but data the upgrade adds is missing until the reparse "
            "finishes." in text
        )

    def test_parked_trashed_files_leave_the_index_current(self, fake_server, seeded_db):
        """#1165: trashed files parked until the reaper runs are already
        indexed; they are counted, not reported as retrying or waiting."""
        parked = ("queued", 0, "retryable", "x", "trashed", "file is T-flagged; parked")
        write_ingestion(
            seeded_db.path,
            sync_completed_at=_ago(seconds=30),
            sync_interval_secs=60,
            indexer_seen_at=_ago(seconds=5),
            jobs=(parked, parked),
        )
        out = asyncio.run(_handler(fake_server, seeded_db)())
        text = _text(out)
        queue = out.structured_content["queue"]
        assert out.structured_content["current"] is True
        assert (queue["retrying"], queue["parked_trashed"]) == (0, 2)
        assert (
            "Queue:          0 pending, 0 retrying, 0 deferred, 2 parked (trashed), 0 dead" in text
        )
        assert (
            "  2 trashed messages are already indexed and wait for the reaper to "
            "remove them (or for their files to be restored); they do not make the "
            "index non-current." in text
        )

    def test_deferred_messages_keep_the_index_not_current(self, fake_server, seeded_db):
        """#1165: a permission deferral and an embedder-outage deferral
        are reported as deferred and keep current false."""
        write_ingestion(
            seeded_db.path,
            sync_completed_at=_ago(seconds=30),
            sync_interval_secs=60,
            indexer_seen_at=_ago(seconds=5),
            jobs=(
                (
                    "queued",
                    0,
                    "retryable",
                    "x",
                    "parse",
                    "PermissionError: deferred until mbsync opens the file",
                ),
                ("queued", 0, "retryable", "x", "embed", "APIConnectionError"),
            ),
        )
        out = asyncio.run(_handler(fake_server, seeded_db)())
        text = _text(out)
        queue = out.structured_content["queue"]
        assert out.structured_content["current"] is False
        assert (queue["retrying"], queue["deferred"]) == (0, 2)
        assert out.structured_content["not_current_reasons"] == [
            "2 messages waiting to be indexed (0 pending, 0 retrying, 2 deferred)"
        ]
        assert (
            "  2 messages are deferred without a failure of their own (a file the "
            "indexer cannot read yet, an embedder outage or configuration error, or a "
            "job waiting for a rename); the indexer retries them without spending "
            "attempts." in text
        )

    def test_one_deferred_and_one_parked_message_read_in_the_singular(self, fake_server, seeded_db):
        write_ingestion(
            seeded_db.path,
            sync_completed_at=_ago(seconds=30),
            sync_interval_secs=60,
            indexer_seen_at=_ago(seconds=5),
            jobs=(
                ("queued", 0, "retryable", "x", "trashed", "file is T-flagged; parked"),
                ("queued", 0, "operator_action_required", "x", "embed", "AuthenticationError"),
            ),
        )
        text = _text(asyncio.run(_handler(fake_server, seeded_db)()))
        assert (
            "  1 message is deferred without a failure of its own (a file the indexer "
            "cannot read yet, an embedder outage or configuration error, or a job "
            "waiting for a rename); the indexer retries it without spending attempts." in text
        )
        assert (
            "  1 trashed message is already indexed and waits for the reaper to remove "
            "it (or for its file to be restored); it does not make the index non-current." in text
        )

    def test_no_message_id_conflicts(self, fake_server, seeded_db):
        out = asyncio.run(_handler(fake_server, seeded_db)())
        text = _text(out)
        assert out.structured_content["conflicting_message_ids"] == 0
        assert out.structured_content["extra_claimant_files"] == 0
        assert "Message-ID conflicts: none" in text
        assert "get_message" not in text

    def test_message_id_conflicts_reported_as_counts_only(self, fake_server, conflicts_db):
        """#455: status reports how many Message-IDs several files claim,
        never the IDs, and points at get_message for the detail."""
        out = asyncio.run(_handler(fake_server, conflicts_db)())
        text = _text(out)
        assert out.structured_content["conflicting_message_ids"] == 2
        assert out.structured_content["extra_claimant_files"] == 3
        assert (
            "Message-ID conflicts: 2 Message-IDs are claimed by more than one file "
            "(3 extra files)" in text
        )
        assert "get_message" in text
        for message_id in ("two@example.com", "three@example.com"):
            assert message_id not in text
            assert message_id not in str(out.structured_content)

    def test_empty_index_before_any_sync(self, fake_server, empty_db):
        out = asyncio.run(_handler(fake_server, empty_db)())
        text = _text(out)
        assert out.structured_content["current"] is False
        assert out.structured_content["last_sync_at"] is None
        assert "Last mail sync: never" in text
        assert "Indexer seen:   never" in text
        assert "Total threads:  0" in text

    def test_db_exception_raises_tool_error(self, fake_server, seeded_db, monkeypatch):
        def boom():
            raise RuntimeError("simulated stats failure")

        monkeypatch.setattr(seeded_db, "get_mailbox_status", boom)
        with pytest.raises(ToolError, match="Mailbox status error"):
            asyncio.run(_handler(fake_server, seeded_db)())

    def test_db_error_text_is_withheld(self, fake_server, seeded_db, monkeypatch, caplog):
        """An SQLite error can quote stored data; the log and the caller
        get its type only (#257)."""

        def boom():
            raise sqlite3.OperationalError("no such column: privatemarkerq7z")

        monkeypatch.setattr(seeded_db, "get_mailbox_status", boom)
        with caplog.at_level("DEBUG"), pytest.raises(ToolError) as exc:
            asyncio.run(_handler(fake_server, seeded_db)())
        assert "privatemarkerq7z" not in str(exc.value)
        assert "privatemarkerq7z" not in caplog.text
        assert "OperationalError" in str(exc.value)
        assert "OperationalError" in caplog.text


class TestEventLoopResponsiveness:
    """#320: the handler's SQLite work runs in a worker thread, so a slow
    status query cannot stall every other request on the shared loop."""

    def test_status_query_does_not_block_the_event_loop(self, fake_server, seeded_db):
        entered = threading.Event()
        release = threading.Event()
        released_by_loop: list[bool] = []
        real = seeded_db.get_mailbox_status

        def gated():
            entered.set()
            # Only the event loop sets ``release``. Run on the loop, this
            # wait cannot be answered and times out.
            released_by_loop.append(release.wait(timeout=2))
            return real()

        seeded_db.get_mailbox_status = gated  # type: ignore[method-assign]

        async def scenario():
            task = asyncio.create_task(_handler(fake_server, seeded_db)())
            while not entered.is_set() and not task.done():
                await asyncio.sleep(0.001)
            # The loop runs this while the query is still in progress.
            release.set()
            return await task

        out = asyncio.run(scenario())
        assert released_by_loop == [True]
        assert out.structured_content["total_threads"] == 3
