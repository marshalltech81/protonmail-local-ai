"""
Retrieval tools — Group 2.
Fetch thread and message context from the local SQLite index.
"""

import asyncio
import logging

from mcp.types import TextContent

from ..lib.security import log_tool_call
from ..lib.sqlite import MessageRecord, Participant, address_match_mode, canonical_addr
from ..lib.validation import clamp_int

log = logging.getLogger("mcp.tools.retrieval")

# Ceiling on query_messages page size: enumeration pages by cursor, so a
# large page only bloats one response.
_MAX_QUERY_LIMIT = 100

# Recipients rendered per role before the rest are summarized as a count.
_MAX_LISTED_PARTICIPANTS = 10

# get_thread pages by message and cuts each body, so neither a long thread
# nor a long message makes an unbounded response; get_message returns a
# message's full body.
_DEFAULT_THREAD_PAGE = 10
_MAX_THREAD_PAGE = 50
_THREAD_BODY_CHAR_LIMIT = 4000
# Headers are sender-controlled too: get_thread lists at most this many
# References and cuts every header value at this many characters.
_MAX_LISTED_REFERENCES = 10
_THREAD_HEADER_CHAR_LIMIT = 500


def _join_limited(items: list[str], limit: int | None) -> str:
    if limit is None or len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f" (+{len(items) - limit} more)"


def _clip(value: str, limit: int | None) -> str:
    if limit is None or len(value) <= limit:
        return value
    return value[:limit] + f"… [{len(value) - limit:,} more characters]"


def _format_participants(
    people: list[Participant], limit: int | None = _MAX_LISTED_PARTICIPANTS
) -> str:
    return _join_limited(
        [f"{p.name} <{p.address}>" if p.name else p.address for p in people], limit
    )


def _header_lines(m: MessageRecord, *, full: bool) -> list[str]:
    """A message's own headers, one per line; absent ones are omitted.

    Unless ``full``, long lists are summarized and long values cut, so
    get_thread stays bounded whatever a sender put in the headers.
    """
    people_limit = None if full else _MAX_LISTED_PARTICIPANTS
    refs_limit = None if full else _MAX_LISTED_REFERENCES
    chars = None if full else _THREAD_HEADER_CHAR_LIMIT
    headers = [("Subject", m.subject)]
    for label, people in (("From", m.from_), ("To", m.to), ("Cc", m.cc)):
        if people:
            headers.append((label, _format_participants(people, people_limit)))
    headers += [("Sent", m.sent_at), ("Folder", m.folder)]
    if m.in_reply_to:
        headers.append(("In-Reply-To", m.in_reply_to))
    if m.references:
        headers.append(("References", _join_limited(m.references, refs_limit)))
    headers.append(("Attachments", "yes" if m.has_attachments else "no"))
    return [f"{label}: {_clip(value, chars)}" for label, value in headers]


def _describe_filters(args: dict) -> str:
    """State how query_messages interpreted each filter, so the caller
    knows whether an address matched exactly or as a substring."""
    parts = []
    for key, value in args.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if key in ("sender", "recipient", "participant"):
            if address_match_mode(value) == "exact":
                parts.append(f"{key}={canonical_addr(value)} (exact address)")
            else:
                parts.append(f"{key}={value.strip()!r} (substring of address or name)")
        elif key == "subject":
            parts.append(f"subject={value.strip()!r} (case-insensitive substring)")
        elif key == "text":
            parts.append(f"text={value.strip()!r} (all words, message body)")
        else:
            parts.append(f"{key}={value!r}")
    return ", ".join(parts) or "no filters (every indexed message)"


def register_retrieval_tools(server, db):
    local_only_note = (
        "mcp-server has no live Bridge access. "
        "This response is based on the local SQLite index only."
    )

    @server.tool()
    async def get_thread(
        thread_id: str,
        include_attachments_metadata: bool = True,
        offset: int = 0,
        limit: int = _DEFAULT_THREAD_PAGE,
    ) -> list[TextContent]:
        """
        Get one thread's messages by thread ID, oldest first — each
        message's own headers and body; no attachment content.

        Pages by message: the response states the thread's message
        count and, when more remain, the ``offset`` for the next call.
        Each body is cut at 4,000 characters, with a marker saying how
        much was left out; long header values and lists are shortened the
        same way. ``get_message`` returns a full body and full headers.

        DO NOT use this to read attachment content (PDFs, OCR'd
        images, scans). It returns the message bodies only; the
        attachment chunks are not in the result. For any question
        that requires the text of an attached PDF or image — "what
        does the quote PDF say?", "compare the attached statement
        against the email body" — call ``ask_mailbox`` or
        ``search_attachments`` instead. Iterating ``get_thread``
        across multiple threads to find attachment content is the
        wrong shape and will not surface it; the extracted attachment
        text lives in a chunk lane read only by ``ask_mailbox``,
        ``search_attachments``, and ``get_evidence``. Reaching for an external
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
            offset: Messages to skip, oldest first (default 0)
            limit: Messages per page (default 10, clamped to [1, 50])

        Returns:
            Thread metadata, then the page's messages oldest first: its own
            Message-ID, subject, From / To / Cc, send date (UTC),
            folder, reply headers, attachment flag, and indexed body
            (the text after quoted-reply stripping). When no message
            body is indexed yet, the accumulated thread text instead.
        """
        log_tool_call(
            log,
            "get_thread",
            {
                "thread_id": thread_id,
                "include_attachments_metadata": include_attachments_metadata,
                "offset": offset,
                "limit": limit,
            },
        )
        limit = clamp_int(limit, default=_DEFAULT_THREAD_PAGE, minimum=1, maximum=_MAX_THREAD_PAGE)
        offset = clamp_int(offset, default=0, minimum=0, maximum=1_000_000)
        try:
            page = await asyncio.to_thread(
                db.get_thread_page,
                thread_id,
                offset=offset,
                limit=limit,
                body_char_limit=_THREAD_BODY_CHAR_LIMIT,
            )
            if not page:
                return [TextContent(type="text", text=f"Thread not found: {thread_id}")]
            thread, messages, total = page.thread, page.messages, page.total_messages

            if messages:
                count = f"{total} (showing {offset + 1}-{offset + len(messages)}, oldest first)"
            else:
                count = str(total)
            lines = [
                f"Thread: {_clip(thread.subject, _THREAD_HEADER_CHAR_LIMIT)}",
                f"Thread ID: {thread.thread_id}",
                f"Folder: {thread.folder}",
                "Participants: "
                + _clip(
                    _join_limited(thread.participants, _MAX_LISTED_PARTICIPANTS),
                    _THREAD_HEADER_CHAR_LIMIT,
                ),
                f"Date range: {thread.date_first.strftime('%Y-%m-%d')} "
                f"→ {thread.date_last.strftime('%Y-%m-%d')}",
                f"Messages: {count}",
                f"Mode: {local_only_note}",
                "",
            ]
            if messages:
                lines.append(
                    "Messages, oldest first (bodies are the indexed text after "
                    "quoted-reply stripping; attachment text is not included; "
                    "long headers are shortened, get_message returns full headers):"
                )
            elif total:
                lines.append(f"No messages at offset {offset}; the thread has {total}.")
            for i, m in enumerate(messages, offset + 1):
                lines += ["", f"[{i}/{total}] Message-ID: {m.message_id}"]
                lines += _header_lines(m, full=False)
                lines.append("")
                body = page.bodies.get(m.message_id)
                if body is None:
                    lines.append("(No body text is indexed for this message.)")
                    continue
                lines.append(body.text)
                if body.omitted_chars:
                    lines.append(
                        f"[{body.omitted_chars:,} more characters not shown; "
                        f'get_message("{m.message_id}") returns the full body]'
                    )
            if offset + len(messages) < total:
                lines += [
                    "",
                    f"More messages: call get_thread with offset={offset + len(messages)}.",
                ]

            # No message body indexed yet (e.g. chunking still pending):
            # fall back to the accumulated thread text, a retrieval
            # artifact that also carries quoted replies.
            if not page.has_bodies:
                if thread.body_text:
                    lines += ["", "Indexed thread text:", "", thread.body_text]
                elif thread.snippet:
                    lines += ["", "Indexed snippet:", "", thread.snippet]

            if include_attachments_metadata and thread.has_attachments:
                lines.append("")
                lines.append(
                    "Attachments are present in this thread; use search_attachments "
                    "to search their extracted text."
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
        Get one message's own headers and indexed body.

        Headers come from the message itself: subject, every From /
        To / Cc entry, send date (UTC), folder, In-Reply-To,
        References, and the attachment flag. Reconstructs the message
        body from the per-message chunk store (in document order) — the
        index keeps no raw per-message body, so this is the indexed text
        after quoted-reply stripping, which
        is usually what you want for "show me the message from Jane on
        Tuesday". Attachment text is NOT included here; use
        get_evidence or ask_mailbox for attachment content. When no
        body chunks are indexed for the message, falls back to
        parent-thread context.

        ``message_id`` is the RFC 5322 Message-ID header value. Obtain
        it from a thread's message list (via get_thread or
        search_emails). Do NOT pass a subject line or a phrase —
        invented IDs return ``Message not found``.

        Args:
            message_id: The Message-ID header value
            folder: Retained for interface compatibility; ignored in local-only mode
            body_format: Retained for interface compatibility; ignored in local-only mode

        Returns:
            The message's headers, its thread ID and subject, and its
            reconstructed indexed body, or thread context when no body
            chunks are indexed.
        """
        log_tool_call(
            log,
            "get_message",
            {"message_id": message_id, "folder": folder, "body_format": body_format},
        )
        try:
            view = await asyncio.to_thread(db.get_message_view, message_id)
            if not view:
                return [TextContent(type="text", text=f"Message not found: {message_id}")]
            thread = view.thread

            lines = [
                f"Message-ID: {message_id}",
                *_header_lines(view.record, full=True),
                f"Thread: {thread.subject}",
                f"Thread ID: {thread.thread_id}",
                f"Mode: {local_only_note}",
            ]

            if view.body:
                lines += [
                    "",
                    "Message body (the indexed body after quoted-reply "
                    "stripping, not the raw message):",
                    "",
                    view.body.text,
                ]
            else:
                # No body chunks — a legacy thread, an empty-body
                # message, or one not chunked yet. Fall back to the
                # accumulated parent-thread context.
                lines += [
                    "",
                    "No per-message body chunks are indexed for this message. "
                    "Showing indexed thread context instead; use get_thread "
                    "for the full thread.",
                ]
                if thread.body_text:
                    lines += ["", "Indexed thread text:", "", thread.body_text]
                elif thread.snippet:
                    lines += ["", "Indexed snippet:", "", thread.snippet]

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
        ``list_threads`` call. For exhaustive listing or counting of
        messages by exact criteria ("every message from X", "how many
        in Archive since March"), use ``query_messages``.

        Args:
            folder: Folder name (default: INBOX)
            filter_type: Currently only "all" is supported by the local
                         index. Other values return a clear validation error.
            limit: Number of threads to return (default: 20)
            offset: Pagination offset (default: 0)

        Returns:
            List of threads sorted by most recent activity.
        """
        log_tool_call(
            log,
            "list_threads",
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
    async def query_messages(
        sender: str | None = None,
        recipient: str | None = None,
        participant: str | None = None,
        subject: str | None = None,
        text: str | None = None,
        folder: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> list[TextContent]:
        """
        Enumerate EVERY message matching exact criteria, with an exact
        total count. Not ranked, not fuzzy: the complete matching set,
        newest first, one message per row.

        Use this for exhaustive or counting questions — "how many
        emails did Jane send me in 2024?", "list every message from
        @example.com", "all messages in Archive with attachments since
        March". ``search_emails`` ranks by relevance and returns only
        the top threads, so it cannot answer "all" or "how many";
        this tool can.

        All given filters must match (AND). Omitted or blank filters
        are ignored; with none, every indexed message is enumerated.

        Paging: the response states ``total_matches``, how many were
        returned, and ``has_more``. When ``has_more`` is true, call
        again with the SAME filters plus ``cursor`` set to the returned
        ``next_cursor``. Never report a partial page as the complete
        answer — use ``total_matches`` for counts.

        Args:
            sender: From address. A full address ("jane@example.com")
                    matches exactly; anything else ("@example.com",
                    "Jane") is a case-insensitive substring of the
                    address or display name. The response states which.
            recipient: To or Cc, matched like ``sender``.
            participant: Any role (From, To, or Cc), matched like ``sender``.
            subject: Case-insensitive substring of the message subject.
            text: Words that must ALL appear in the message body (word
                  match with stemming). Searches the message's own
                  text only — not attachments and not quoted earlier
                  replies. For attachment content use search_attachments.
            folder: Exact folder name (see list_folders).
            date_from: ISO 8601 lower bound on the send date, inclusive.
            date_to: ISO 8601 upper bound, inclusive; a date-only value
                     covers the whole day (UTC).
            has_attachments: True for messages with attachments, False
                             for messages without.
            limit: Messages per page (default 25, clamped to [1, 100]).
            cursor: ``next_cursor`` from the previous page of the same query.

        Returns:
            The filter interpretation, total_matches, the page's
            messages (send date, folder, subject, From / To / Cc,
            Message-ID, Thread ID), and paging state.
        """
        args = {
            "sender": sender,
            "recipient": recipient,
            "participant": participant,
            "subject": subject,
            "text": text,
            "folder": folder,
            "date_from": date_from,
            "date_to": date_to,
            "has_attachments": has_attachments,
        }
        log_tool_call(log, "query_messages", {**args, "limit": limit, "cursor": cursor})
        limit = clamp_int(limit, default=25, minimum=1, maximum=_MAX_QUERY_LIMIT)

        try:
            page = await asyncio.to_thread(db.query_messages, **args, limit=limit, cursor=cursor)
        except ValueError as e:
            # Validation messages quote the offending input (an invalid
            # date echoes its text), which log_tool_call deliberately
            # withheld. Return it to the caller; log only that it failed.
            log.warning("query_messages rejected invalid input (date_from/date_to/text/cursor)")
            return [TextContent(type="text", text=f"Error: {e}")]
        except Exception as e:
            log.error(f"query_messages error: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]

        lines = [f"Query: {_describe_filters(args)}", f"total_matches: {page.total_matches}"]
        if not page.messages:
            lines.append("returned: 0")
            lines.append("has_more: false")
            lines.append("No messages match." if page.offset == 0 else "No further messages.")
            return [TextContent(type="text", text="\n".join(lines))]

        first, last = page.offset + 1, page.offset + len(page.messages)
        lines.append(f"returned: {len(page.messages)} (matches {first}-{last})")
        lines.append(f"has_more: {'true' if page.has_more else 'false'}")
        if page.next_cursor:
            lines.append(f"next_cursor: {page.next_cursor}")
            lines.append("(Call again with the same filters and this cursor for the next page.)")
        lines.append("")

        for i, m in enumerate(page.messages, first):
            flags = " | attachments" if m.has_attachments else ""
            lines.append(f"{i}. {m.sent_at} | {m.folder}{flags}")
            lines.append(f"   Subject: {m.subject}")
            for label, people in (("From", m.from_), ("To", m.to), ("Cc", m.cc)):
                if people:
                    lines.append(f"   {label}: {_format_participants(people)}")
            lines.append(f"   Message-ID: {m.message_id}")
            lines.append(f"   Thread ID: {m.thread_id}")
            lines.append("")

        return [TextContent(type="text", text="\n".join(lines))]

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
        log_tool_call(log, "find_contact", {"query": query, "limit": limit})
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
