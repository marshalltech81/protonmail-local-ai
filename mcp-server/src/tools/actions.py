"""
Action tools — Group 4 (write operations).
These interfaces remain defined for a future opt-in write backend, but the
default local-first deployment does not register them.
"""

import logging

from mcp.types import TextContent

log = logging.getLogger("mcp.tools.actions")


def register_action_tools(server, imap, read_only: bool = True):
    def _write_blocked_message() -> list[TextContent]:
        if read_only:
            return [
                TextContent(
                    type="text",
                    text=(
                        "Mailbox actions are disabled because MCP read-only mode is enabled. "
                        "Set MCP_READ_ONLY=false only after explicitly enabling a safe write path."
                    ),
                )
            ]

        if imap is None:
            return [
                TextContent(
                    type="text",
                    text=(
                        "Mailbox actions are unavailable because no live Bridge-backed action "
                        "transport is configured for mcp-server."
                    ),
                )
            ]

        return []

    @server.tool()
    async def send_email(
        to: list[str],
        subject: str,
        body: str,
        body_format: str = "text",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to_message_id: str | None = None,
    ) -> list[TextContent]:
        """
        Send a new email via ProtonBridge SMTP.

        Args:
            to: List of recipient email addresses
            subject: Email subject line
            body: Email body content
            body_format: "text" (default) or "html"
            cc: Optional CC recipients
            bcc: Optional BCC recipients
            reply_to_message_id: If replying, the Message-ID of the original
                                  (sets In-Reply-To and References headers)

        Returns:
            Confirmation of send or error message.
        """
        try:
            blocked = _write_blocked_message()
            if blocked:
                return blocked

            in_reply_to = None
            references = None

            if reply_to_message_id:
                in_reply_to = f"<{reply_to_message_id}>"
                references = f"<{reply_to_message_id}>"

            success = imap.send_email(
                to=to,
                subject=subject,
                body=body,
                body_format=body_format,
                cc=cc,
                bcc=bcc,
                in_reply_to=in_reply_to,
                references=references,
            )

            if success:
                recipients = ", ".join(to)
                return [TextContent(type="text", text=f"Email sent successfully to {recipients}.")]
            else:
                return [TextContent(type="text", text="Failed to send email. Check logs.")]

        except Exception as e:
            log.error(f"send_email error: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]

    @server.tool()
    async def reply_to_thread(
        thread_id: str,
        body: str,
        reply_all: bool = False,
        body_format: str = "text",
    ) -> list[TextContent]:
        """
        Reply to an email thread.
        Automatically sets In-Reply-To and References headers for
        correct threading in all email clients.

        Args:
            thread_id: The thread ID to reply to
            body: Your reply body
            reply_all: Reply to all participants (default: False)
            body_format: "text" or "html" (default: text)

        Returns:
            Confirmation of send or error message.
        """
        return [
            TextContent(
                type="text",
                text=(
                    "reply_to_thread is not yet implemented. "
                    "Use send_email with reply_to_message_id set to the Message-ID "
                    "of the last message in the thread to send a reply manually."
                ),
            )
        ]

    @server.tool()
    async def move_message(
        uid: str,
        src_folder: str,
        dst_folder: str,
    ) -> list[TextContent]:
        """
        Move a message from one folder to another.

        Args:
            uid: IMAP UID of the message
            src_folder: Source folder name
            dst_folder: Destination folder name

        Returns:
            Confirmation or error.
        """
        try:
            blocked = _write_blocked_message()
            if blocked:
                return blocked

            success = await imap.move_message(uid, src_folder, dst_folder)
            if success:
                return [
                    TextContent(
                        type="text", text=f"Message moved from {src_folder} to {dst_folder}."
                    )
                ]
            return [TextContent(type="text", text="Move failed.")]
        except Exception as e:
            log.error(f"move_message error: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]

    @server.tool()
    async def mark_read(
        uids: list[str],
        folder: str = "INBOX",
        read: bool = True,
    ) -> list[TextContent]:
        """
        Mark one or more messages as read or unread.

        Args:
            uids: List of IMAP UIDs
            folder: Folder containing the messages (default: INBOX)
            read: True to mark read, False to mark unread (default: True)

        Returns:
            Confirmation or error.
        """
        try:
            blocked = _write_blocked_message()
            if blocked:
                return blocked

            results = []
            for uid in uids:
                success = await imap.set_flag(uid, folder, "\\Seen", read)
                results.append(
                    f"UID {uid}: {'read' if read else 'unread'} {'✓' if success else '✗'}"
                )
            return [TextContent(type="text", text="\n".join(results))]
        except Exception as e:
            log.error(f"mark_read error: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]

    @server.tool()
    async def flag_message(
        uid: str,
        folder: str = "INBOX",
        flagged: bool = True,
    ) -> list[TextContent]:
        """
        Flag or unflag a message (starred/important).

        Args:
            uid: IMAP UID of the message
            folder: Folder containing the message (default: INBOX)
            flagged: True to flag, False to unflag (default: True)

        Returns:
            Confirmation or error.
        """
        try:
            blocked = _write_blocked_message()
            if blocked:
                return blocked

            success = await imap.set_flag(uid, folder, "\\Flagged", flagged)
            state = "flagged" if flagged else "unflagged"
            if success:
                return [TextContent(type="text", text=f"Message {state} successfully.")]
            return [TextContent(type="text", text=f"Failed to {state} message.")]
        except Exception as e:
            log.error(f"flag_message error: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]

    @server.tool()
    async def create_draft(
        to: list[str],
        subject: str,
        body: str,
        reply_to_message_id: str | None = None,
    ) -> list[TextContent]:
        """
        Save a draft email to the Drafts folder.

        Args:
            to: Recipient email addresses
            subject: Email subject
            body: Email body
            reply_to_message_id: Optional Message-ID if replying

        Returns:
            Confirmation that draft was saved.
        """
        return [
            TextContent(
                type="text",
                text=(
                    "create_draft is not yet implemented. "
                    "IMAP APPEND to the Drafts folder is required but not yet built."
                ),
            )
        ]
