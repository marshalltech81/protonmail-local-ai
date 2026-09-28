"""
System tools — Group 4.
Index status and sync mode.
Claude should call get_index_status before making claims about email content.
"""

import logging
from datetime import UTC, datetime

from mcp.types import TextContent

log = logging.getLogger("mcp.tools.system")


def register_system_tools(server, db):
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
        Report how this server sees mail sync.

        mcp-server serves the local SQLite index only and never talks to
        ProtonBridge; mbsync owns Bridge access and Maildir refresh.

        Returns:
            The sync mode and which service is responsible for syncing.
        """
        log.info("tool=get_sync_status")
        lines = [
            "=== Sync Status ===",
            "Mode: local index only",
            "Bridge reachability is not checked by mcp-server.",
            "mbsync remains responsible for talking to Bridge and refreshing Maildir.",
        ]
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
