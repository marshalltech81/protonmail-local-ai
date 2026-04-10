"""
Retrieval tools — Group 2.
Fetch full thread or message content directly from the index or Bridge IMAP.
"""

import logging

from mcp.types import TextContent

log = logging.getLogger("mcp.tools.retrieval")


def register_retrieval_tools(server, db, imap):

    @server.tool()
    async def get_thread(
        thread_id: str,
        include_attachments_metadata: bool = True,
    ) -> list[TextContent]:
        """
        Get the full contents of an email thread by thread ID.

        Args:
            thread_id: The thread ID returned by search_emails
            include_attachments_metadata: Include attachment names and sizes

        Returns:
            Full thread with all messages, participants, and timeline.
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
                "",
            ]

            # Fetch each message from IMAP for full body content
            for i, message_id in enumerate(thread.message_ids, 1):
                msg = await imap.fetch_message(message_id, thread.folder)
                if msg:
                    lines.append(f"--- Message {i} ---")
                    lines.append(f"From: {msg.from_addr}")
                    lines.append(f"Date: {msg.date.strftime('%Y-%m-%d %H:%M')}")
                    lines.append(f"To: {', '.join(msg.to_addrs)}")
                    if msg.cc_addrs:
                        lines.append(f"Cc: {', '.join(msg.cc_addrs)}")
                    lines.append("")
                    lines.append(msg.body_text[:3000])
                    if include_attachments_metadata and msg.attachments:
                        lines.append("")
                        lines.append(f"Attachments ({len(msg.attachments)}):")
                        for att in msg.attachments:
                            size_kb = att["size"] // 1024
                            lines.append(
                                f"  - {att['filename']} ({att['content_type']}, {size_kb}KB)"
                            )
                    lines.append("")

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
        Get the full content of a single email message.

        Args:
            message_id: The Message-ID header value
            folder: The folder the message lives in (default: INBOX)
            body_format: "text" or "html" (default: text)

        Returns:
            Full message content including headers and body.
        """
        try:
            msg = await imap.fetch_message(message_id, folder)
            if not msg:
                return [TextContent(type="text", text=f"Message not found: {message_id}")]

            body = msg.body_html if body_format == "html" else msg.body_text

            lines = [
                f"Subject: {msg.subject}",
                f"From: {msg.from_addr}",
                f"To: {', '.join(msg.to_addrs)}",
            ]
            if msg.cc_addrs:
                lines.append(f"Cc: {', '.join(msg.cc_addrs)}")
            lines += [
                f"Date: {msg.date.strftime('%Y-%m-%d %H:%M')}",
                f"Folder: {msg.folder}",
                "",
                body,
            ]
            if msg.attachments:
                lines.append("")
                lines.append(f"Attachments ({len(msg.attachments)}):")
                for att in msg.attachments:
                    lines.append(f"  - {att['filename']} ({att['content_type']})")

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
