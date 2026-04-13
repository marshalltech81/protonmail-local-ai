"""
Retrieval tools — Group 2.
Fetch thread and message context from the local SQLite index.
"""

import logging

from mcp.types import TextContent

log = logging.getLogger("mcp.tools.retrieval")


def register_retrieval_tools(server, db):
    local_only_note = (
        "Live Bridge retrieval is disabled in the default local-first deployment. "
        "This response is based on the local SQLite index only."
    )

    @server.tool()
    async def get_thread(
        thread_id: str,
        include_attachments_metadata: bool = True,
    ) -> list[TextContent]:
        """
        Get indexed context for an email thread by thread ID.

        Args:
            thread_id: The thread ID returned by search_emails
            include_attachments_metadata: Include the local attachment availability note

        Returns:
            Indexed thread context, participants, and timeline from the local index.
        """
        try:
            thread = db.get_thread(thread_id)
            if not thread:
                return [TextContent(type="text", text=f"Thread not found: {thread_id}")]

            lines = [
                f"Thread: {thread.subject}",
                f"Folder: {thread.folder}",
                f"Participants: {', '.join(thread.participants)}",
                f"Date range: {thread.date_first.strftime('%Y-%m-%d')} "
                f"→ {thread.date_last.strftime('%Y-%m-%d')}",
                f"Messages: {len(thread.message_ids)}",
                f"Mode: {local_only_note}",
                "",
            ]

            if thread.body_text:
                lines.append("Indexed thread text:")
                lines.append("")
                lines.append(thread.body_text)
                lines.append("")
            elif thread.snippet:
                lines.append("Indexed snippet:")
                lines.append("")
                lines.append(thread.snippet)
                lines.append("")

            lines.append("Message IDs:")
            for i, message_id in enumerate(thread.message_ids, 1):
                lines.append(f"  {i}. {message_id}")

            if include_attachments_metadata and thread.has_attachments:
                lines.append("")
                lines.append(
                    "Attachments are present in this thread, but local-only retrieval "
                    "currently exposes attachment metadata through search/index flags "
                    "rather than live per-message attachment listings."
                )

            return [TextContent(type="text", text="\n".join(lines))]

        except Exception as e:
            log.error(f"get_thread error: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]

    @server.tool()
    async def get_message(
        message_id: str,
        folder: str = "INBOX",
        body_format: str = "text",
    ) -> list[TextContent]:
        """
        Get local index context for a single email message.

        Args:
            message_id: The Message-ID header value
            folder: Retained for interface compatibility; ignored in local-only mode
            body_format: Retained for interface compatibility; ignored in local-only mode

        Returns:
            Local index context for the message and its parent thread.
        """
        try:
            thread_id = db.find_thread_by_message_id(message_id)
            if not thread_id:
                return [TextContent(type="text", text=f"Message not found: {message_id}")]

            thread = db.get_thread(thread_id)
            if not thread:
                return [TextContent(type="text", text=f"Message not found: {message_id}")]

            lines = [
                f"Message-ID: {message_id}",
                f"Thread: {thread.subject}",
                f"Folder: {thread.folder}",
                f"Thread date range: {thread.date_first.strftime('%Y-%m-%d')} "
                f"→ {thread.date_last.strftime('%Y-%m-%d')}",
                f"Participants: {', '.join(thread.participants)}",
                f"Mode: {local_only_note}",
                "",
                "The local index does not currently store per-message full bodies. "
                "Use get_thread for indexed thread context.",
            ]
            if thread.body_text:
                lines += [
                    "",
                    "Indexed thread text:",
                    "",
                    thread.body_text,
                ]
            elif thread.snippet:
                lines.append("")
                lines.append("Indexed snippet:")
                lines.append("")
                lines.append(thread.snippet)

            return [TextContent(type="text", text="\n".join(lines))]

        except Exception as e:
            log.error(f"get_message error: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]

    @server.tool()
    async def list_threads(
        folder: str = "INBOX",
        filter_type: str = "all",
        limit: int = 20,
        offset: int = 0,
    ) -> list[TextContent]:
        """
        List email threads in a folder.

        Args:
            folder: Folder name (default: INBOX)
            filter_type: "all", "unread", or "flagged" (default: all)
            limit: Number of threads to return (default: 20)
            offset: Pagination offset (default: 0)

        Returns:
            List of threads sorted by most recent activity.
        """
        try:
            threads = db.list_threads(
                folder=folder,
                filter_type=filter_type,
                limit=limit,
                offset=offset,
            )

            if not threads:
                return [TextContent(type="text", text=f"No threads found in {folder}.")]

            lines = [f"Threads in {folder} ({len(threads)} shown):\n"]
            for i, t in enumerate(threads, 1 + offset):
                lines.append(
                    f"{i}. {t.subject}\n"
                    f"   {', '.join(t.participants[:2])}"
                    f"{'...' if len(t.participants) > 2 else ''} | "
                    f"{t.date_last.strftime('%Y-%m-%d')} | "
                    f"{len(t.message_ids)} msg(s)"
                    f"{'  📎' if t.has_attachments else ''}\n"
                    f"   ID: {t.thread_id}\n"
                )

            return [TextContent(type="text", text="\n".join(lines))]

        except Exception as e:
            log.error(f"list_threads error: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]

    @server.tool()
    async def list_folders() -> list[TextContent]:
        """
        List all available email folders and their thread counts.

        Returns:
            All folders with thread counts from the local index.
        """
        try:
            folders = db.list_folders()
            if not folders:
                return [TextContent(type="text", text="No folders found in index.")]

            lines = ["Folders:\n"]
            for f in folders:
                lines.append(f"  {f['name']}  ({f['thread_count']} threads)")

            return [TextContent(type="text", text="\n".join(lines))]

        except Exception as e:
            log.error(f"list_folders error: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]
