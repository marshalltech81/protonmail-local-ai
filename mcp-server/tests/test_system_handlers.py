"""
Tests for the registered ``get_mailbox_status`` handler in
src/tools/system.py, which the MCP client / LLM calls.

``test_system.py`` covers the ``current`` rules and the standalone
helper used by the Makefile.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.fastmcp.exceptions import ToolError
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
    def test_current_index(self, fake_server, seeded_db):
        write_ingestion(
            seeded_db.path,
            sync_completed_at=_ago(seconds=30),
            sync_interval_secs=60,
            indexer_seen_at=_ago(seconds=5),
        )
        out = asyncio.run(_handler(fake_server, seeded_db)())
        text = _text(out)
        assert out.structuredContent["current"] is True
        assert out.structuredContent["not_current_reasons"] == []
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
            jobs=(("queued", 0), ("queued", 1), ("dead", 5)),
        )
        out = asyncio.run(_handler(fake_server, seeded_db)())
        text = _text(out)
        reasons = out.structuredContent["not_current_reasons"]
        assert out.structuredContent["current"] is False
        assert len(reasons) == 2
        assert "Current:        no" in text
        for reason in reasons:
            assert f"  - {reason}" in text
        assert "Queue:          1 pending, 1 retrying, 1 dead" in text
        assert "1 message failed permanently" in text

    def test_empty_index_before_any_sync(self, fake_server, empty_db):
        out = asyncio.run(_handler(fake_server, empty_db)())
        text = _text(out)
        assert out.structuredContent["current"] is False
        assert out.structuredContent["last_sync_at"] is None
        assert "Last mail sync: never" in text
        assert "Indexer seen:   never" in text
        assert "Total threads:  0" in text

    def test_db_exception_raises_tool_error(self, fake_server, seeded_db, monkeypatch):
        def boom():
            raise RuntimeError("simulated stats failure")

        monkeypatch.setattr(seeded_db, "get_mailbox_status", boom)
        with pytest.raises(ToolError, match="Mailbox status error"):
            asyncio.run(_handler(fake_server, seeded_db)())
