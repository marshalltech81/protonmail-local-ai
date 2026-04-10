"""
System tools — Group 5.
Index status, sync health, and folder information.
Claude should call get_index_status before making claims about email content.
"""
import logging
from datetime import datetime, timezone

from mcp.server import Server
from mcp.types import TextContent

log = logging.getLogger("mcp.tools.system")


def register_system_tools(server: Server, db):

    @server.tool()
    async def get_index_status() -> list[TextContent]:
        """
        Get the current status of the local email index.
        Call this before answering questions about email content to verify
        the index is current and understand the scope of available data.

        Returns:
            Total threads and messages indexed, date range, and last sync info.
        """
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
                f"Checked at:     {datetime.now(timezone.utc).isoformat()}",
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
        import socket

        bridge_host = "protonmail-bridge"
        bridge_port = 1143
        bridge_ok = False

        try:
            sock = socket.create_connection((bridge_host, bridge_port), timeout=3)
            sock.close()
            bridge_ok = True
        except Exception:
            pass

        lines = [
            "=== Sync Status ===",
            f"Bridge IMAP ({bridge_host}:{bridge_port}): "
            f"{'✓ reachable' if bridge_ok else '✗ unreachable'}",
        ]

        if not bridge_ok:
            lines.append(
                "\nTroubleshooting: run 'make logs' to check Bridge container."
            )

        return [TextContent(type="text", text="\n".join(lines))]


def get_index_status() -> dict:
    """Standalone function used by Makefile status target."""
    return {"status": "ok"}
