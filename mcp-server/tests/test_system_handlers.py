"""
Tests for the registered handlers in src/tools/system.py.

``test_system.py`` already covers the standalone ``get_index_status``
helper used by the Makefile. This file covers the @server.tool()
handlers (``get_index_status``, ``get_sync_status``) which the MCP
client / LLM actually call. The standalone helper and the registered
handler share a name but have different signatures (one returns a dict,
the other a list[TextContent]) — they are intentionally different
surfaces and both need coverage.
"""

import asyncio

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from src.tools.system import register_system_tools


def _handlers(fake_server, db):
    register_system_tools(fake_server, db)
    return fake_server.tools


def _text(result) -> str:
    """Extract the prose from a tool's ``CallToolResult``."""
    assert len(result.content) == 1
    return result.content[0].text


def _error(coro) -> str:
    """Run a tool call that must fail; return the ``ToolError`` message
    the client receives as an ``isError`` result."""
    with pytest.raises(ToolError) as exc:
        asyncio.run(coro)
    return str(exc.value)


class TestGetIndexStatus:
    def test_returns_thread_and_message_counts_for_seeded_db(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["get_index_status"]
        out = asyncio.run(handler())
        text = _text(out)
        assert "Index Status" in text
        # seeded_db has 3 threads and 3 messages.
        assert "Total threads:  3" in text
        assert "Total messages: 3" in text
        assert "Checked at:" in text

    def test_returns_zeros_for_empty_db(self, fake_server, empty_db):
        handler = _handlers(fake_server, empty_db)["get_index_status"]
        out = asyncio.run(handler())
        text = _text(out)
        assert "Total threads:  0" in text
        assert "Total messages: 0" in text

    def test_db_exception_returns_error_text(self, fake_server, seeded_db):
        def boom():
            raise RuntimeError("simulated stats failure")

        seeded_db.get_stats = boom  # type: ignore[assignment]
        handler = _handlers(fake_server, seeded_db)["get_index_status"]
        assert "Index status error" in _error(handler())


class TestGetSyncStatus:
    def test_local_mode_returns_local_only_message(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db)["get_sync_status"]
        out = asyncio.run(handler())
        text = _text(out)
        assert "Sync Status" in text
        assert "local index only" in text
        # mcp-server never speaks directly to Bridge, so no reachability
        # probe result may appear.
        assert "Bridge IMAP" not in text
