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

from src.tools.system import register_system_tools


def _handlers(fake_server, db, *, bridge_enabled=False):
    register_system_tools(fake_server, db, bridge_enabled=bridge_enabled)
    return fake_server.tools


def _text(result) -> str:
    assert len(result) == 1
    return result[0].text


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
        out = asyncio.run(handler())
        assert "Index status error" in _text(out)


class TestGetSyncStatus:
    def test_local_mode_returns_local_only_message(self, fake_server, seeded_db):
        handler = _handlers(fake_server, seeded_db, bridge_enabled=False)["get_sync_status"]
        out = asyncio.run(handler())
        text = _text(out)
        assert "Sync Status" in text
        assert "local index only" in text
        # Bridge reachability check must not run in default deployment —
        # mcp-server is documented to never speak directly to Bridge.
        assert "Bridge IMAP" not in text

    def test_bridge_enabled_path_runs_reachability_probe(self, fake_server, seeded_db, monkeypatch):
        # Force socket.create_connection to fail so the test does not
        # depend on whether anything is listening on port 1143 locally.
        import socket as _socket

        def fake_create_connection(*_args, **_kwargs):
            raise OSError("test: connection refused")

        monkeypatch.setattr(_socket, "create_connection", fake_create_connection)

        handler = _handlers(fake_server, seeded_db, bridge_enabled=True)["get_sync_status"]
        out = asyncio.run(handler())
        text = _text(out)
        assert "Bridge IMAP" in text
        assert "unreachable" in text
        assert "Troubleshooting" in text

    def test_bridge_enabled_with_reachable_bridge_reports_ok(
        self, fake_server, seeded_db, monkeypatch
    ):
        import socket as _socket

        class FakeSock:
            def close(self):
                return None

        def fake_create_connection(*_args, **_kwargs):
            return FakeSock()

        monkeypatch.setattr(_socket, "create_connection", fake_create_connection)

        handler = _handlers(fake_server, seeded_db, bridge_enabled=True)["get_sync_status"]
        out = asyncio.run(handler())
        text = _text(out)
        assert "reachable" in text
        # Troubleshooting hint must be omitted when the probe succeeds.
        assert "Troubleshooting" not in text
