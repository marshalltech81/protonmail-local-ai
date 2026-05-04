"""
Retrieval tools — Group 2.
Fetch thread and message context from the local SQLite index.
"""

import asyncio
import logging

from mcp.types import TextContent

from ..lib.validation import clamp_int

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
        Get one thread's body content by thread ID — body only,
        no attachment content.

        DO NOT use this to read attachment content (PDFs, OCR'd
        images, scans). It returns the message bodies only; the
        attachment chunks are not in the result. For any question
        that requires the text of an attached PDF or image — "what
        does the quote PDF say?", "compare the attached statement
        against the email body" — call ``ask_mailbox`` instead.
        Iterating ``get_thread`` across multiple threads to find
        attachment content is the wrong shape and will not surface
        it; the extracted attachment text lives in the chunk lane
        that only ``ask_mailbox`` consumes. Reaching for an external
        tool (Google Drive, web search) to read a PDF that arrived
        as an email attachment is also wrong — the local index has
        already extracted that PDF's text.

        ``thread_id`` is OPAQUE. Obtain it from search_emails,
        list_threads, or get_message. Do NOT pass a subject line, a
        slugged phrase like ``"weekly_status_update"``, or any other
        human-readable string — those are not valid thread IDs and
        will return ``Thread not found``.

        Args:
            thread_id: The opaque thread ID returned by search_emails
            include_attachments_metadata: Include the local attachment availability note

        Returns:
            Indexed thread context, participants, and timeline from the local index.
        """
        log.info(
            "tool=get_thread %s",
            {
                "thread_id": thread_id,
                "include_attachments_metadata": include_attachments_metadata,
            },
        )
        try:
            thread = await asyncio.to_thread(db.get_thread, thread_id)
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

        ``message_id`` is the RFC 5322 Message-ID header value
        (e.g. ``"<CAH2Z4_a...@mail.gmail.com>"``). Obtain it from a
        thread's message list (via get_thread or search_emails). Do
        NOT pass a subject line or a phrase — invented IDs return
        ``Message not found``.

        Args:
            message_id: The Message-ID header value
            folder: Retained for interface compatibility; ignored in local-only mode
            body_format: Retained for interface compatibility; ignored in local-only mode

        Returns:
            Local index context for the message and its parent thread.
        """
        log.info(
            "tool=get_message %s",
            {"message_id": message_id, "folder": folder, "body_format": body_format},
        )
        try:
            thread_id = await asyncio.to_thread(db.find_thread_by_message_id, message_id)
            if not thread_id:
                return [TextContent(type="text", text=f"Message not found: {message_id}")]

            thread = await asyncio.to_thread(db.get_thread, thread_id)
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
        List email threads in a folder from the local index.

        Use this ONLY for unfiltered browse-style requests — "show me
        my recent emails", "what's in my inbox", "list my latest
        threads". This tool has no keyword, sender, date, or topic
        filter; it returns threads sorted by most recent activity.

        For ANY filtered request — by topic, keyword, sender (name OR
        address), date range, or attachment status — use
        ``search_emails`` instead, which exposes all of those filters.
        In particular, "5 most recent from <person>" is a
        ``search_emails(from_name=..., limit=5)`` call, not a
        ``list_threads`` call.

        Args:
            folder: Folder name (default: INBOX)
            filter_type: Currently only "all" is supported by the local
                         index. Other values return a clear validation error.
            limit: Number of threads to return (default: 20)
            offset: Pagination offset (default: 0)

        Returns:
            List of threads sorted by most recent activity.
        """
        log.info(
            "tool=list_threads %s",
            {"folder": folder, "filter_type": filter_type, "limit": limit, "offset": offset},
        )
        # Clamp both values so a caller-supplied ``limit=100000``,
        # ``offset=-1``, or non-numeric value can't drive an unbounded
        # or malformed query. 100 is well above any reasonable
        # interactive use of list_threads.
        limit = clamp_int(limit, default=20, minimum=1, maximum=100)
        offset = clamp_int(offset, default=0, minimum=0, maximum=1_000_000)

        try:
            threads = await asyncio.to_thread(
                db.list_threads,
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
    async def find_contact(
        query: str,
        limit: int = 10,
    ) -> list[TextContent]:
        """
        Resolve a name / address / domain fragment to indexed contacts.

        Use this when the user is asking ABOUT a person — "do I have
        Jane Smith's email?", "show me everyone at example.com", "who
        is the accountant?". Returns a ranked list of
        (email, display name(s), thread count).

        For "emails FROM <person>" — i.e. you want messages from them,
        not the contact record itself — call ``search_emails`` directly
        with ``from_name=<the user's words>``. ``search_emails`` resolves
        the name through this tool internally, so chaining
        ``find_contact`` → ``search_emails(from_addr=...)`` is an extra
        round-trip with no quality benefit.

        Args:
            query: Name, address, or domain fragment (case-insensitive).
                   Examples: "Smith", "@example.com", "Jane".
            limit: Maximum contacts to return (default 10, capped at 50).

        Returns:
            Ranked contact list with thread counts. Empty result for
            unknown names.
        """
        log.info("tool=find_contact %s", {"query": query, "limit": limit})
        # Same clamp ceiling as list_threads — a hallucinated
        # ``limit=10000`` shouldn't drive a giant aggregation/sort.
        limit = clamp_int(limit, default=10, minimum=1, maximum=50)

        if not query or not query.strip():
            return [
                TextContent(
                    type="text",
                    text="Provide a name, address, or domain fragment to search for.",
                )
            ]

        try:
            contacts = await asyncio.to_thread(db.find_contact, query, limit)
        except Exception as e:
            log.error(f"find_contact error: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]

        if not contacts:
            return [TextContent(type="text", text=f"No contacts found matching: '{query}'")]

        lines = [f"Contacts matching '{query}' ({len(contacts)} shown):\n"]
        for i, c in enumerate(contacts, 1):
            names = ", ".join(c["names"]) if c["names"] else "(no display name)"
            lines.append(
                f"{i}. {c['email']}\n   Name(s): {names}\n   Threads: {c['thread_count']}\n"
            )
        return [TextContent(type="text", text="\n".join(lines))]

    @server.tool()
    async def list_folders() -> list[TextContent]:
        """
        List all available email folders and their thread counts.

        Use this when the user asks structural questions about the
        mailbox — "what folders do I have?", "what mailboxes are
        synced?", "is the Archive folder indexed?". Don't call this
        before search_emails as a discovery step; search_emails
        already understands folder filters when the user names them.

        Returns:
            All folders with thread counts from the local index.
        """
        log.info("tool=list_folders")
        try:
            folders = await asyncio.to_thread(db.list_folders)
            if not folders:
                return [TextContent(type="text", text="No folders found in index.")]

            lines = ["Folders:\n"]
            for f in folders:
                lines.append(f"  {f['name']}  ({f['thread_count']} threads)")

            return [TextContent(type="text", text="\n".join(lines))]

        except Exception as e:
            log.error(f"list_folders error: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]
