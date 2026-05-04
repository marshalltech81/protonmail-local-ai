"""
System tools — Group 5.
Index status, sync health, and folder information.
Claude should call get_index_status before making claims about email content.
"""

import logging
from datetime import UTC, datetime

from mcp.types import TextContent

log = logging.getLogger("mcp.tools.system")


def register_system_tools(server, db, bridge_enabled: bool = False):
    @server.tool()
    async def get_index_status() -> list[TextContent]:
        """
        Get the current status of the local email index.
        Call this before answering questions about email content to verify
        the index is current and understand the scope of available data.

        Returns:
            Total threads and messages indexed, date range, and last sync info.
        """
        log.info("tool=get_index_status")
        try:
            stats = db.get_stats()

            oldest = stats.get("oldest_message", "unknown")
            newest = stats.get("newest_message", "unknown")

            lines = [
                "=== Index Status ===",
                f"Total threads:  {stats.get('total_threads', 0):,}",
                f"Total messages: {stats.get('total_messages', 0):,}",
                f"Oldest message: {oldest}",
                f"Newest message: {newest}",
                f"Checked at:     {datetime.now(UTC).isoformat()}",
            ]

            return [TextContent(type="text", text="\n".join(lines))]

        except Exception as e:
            log.error(f"get_index_status error: {e}")
            return [TextContent(type="text", text=f"Index status error: {e}")]

    @server.tool()
    async def get_sync_status() -> list[TextContent]:
        """
        Check whether ProtonBridge and mbsync are operating correctly.

        Returns:
            Connection status for Bridge IMAP and sync daemon health.
        """
        log.info("tool=get_sync_status")
        if not bridge_enabled:
            lines = [
                "=== Sync Status ===",
                "Mode: local index only",
                "Bridge reachability is not checked by mcp-server in the default deployment.",
                "mbsync remains responsible for talking to Bridge and refreshing Maildir.",
            ]
            return [TextContent(type="text", text="\n".join(lines))]

        import socket
        from contextlib import closing

        bridge_host = "protonmail-bridge"
        bridge_port = 1143
        bridge_ok = False

        try:
            with closing(socket.create_connection((bridge_host, bridge_port), timeout=3)):
                bridge_ok = True
        except OSError as e:
            log.debug("Bridge reachability check failed: %s", e)

        lines = [
            "=== Sync Status ===",
            f"Bridge IMAP ({bridge_host}:{bridge_port}): "
            f"{'✓ reachable' if bridge_ok else '✗ unreachable'}",
        ]

        if not bridge_ok:
            lines.append("\nTroubleshooting: run 'make logs' to check Bridge container.")

        return [TextContent(type="text", text="\n".join(lines))]


def get_index_status() -> dict:
    """Standalone helper used by the Makefile ``status`` target.

    Opens the local SQLite index directly (in read-only URI mode, same as
    the running MCP server) and returns real stats. Previous behavior
    unconditionally returned ``{"status": "ok"}`` regardless of index state,
    so ``make status`` never reflected reality.
    """
    import os
    from datetime import UTC, datetime

    from ..lib.sqlite import Database

    try:
        db = Database(os.environ.get("SQLITE_PATH", "/data/mail.db"))
        stats = db.get_stats()
    except Exception as e:
        return {"status": "error", "error": str(e)}
    return {
        "status": "ok",
        "total_threads": stats.get("total_threads", 0),
        "total_messages": stats.get("total_messages", 0),
        "oldest_message": stats.get("oldest_message"),
        "newest_message": stats.get("newest_message"),
        "checked_at": datetime.now(UTC).isoformat(),
    }
