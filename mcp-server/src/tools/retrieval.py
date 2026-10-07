"""
Retrieval tools — Group 2.
Fetch thread and message context from the local SQLite index.
"""

import asyncio
import logging
import unicodedata

from fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

# Module import: get_thread and get_message have a local named ``count``.
from ..lib import timings
from ..lib.rate_limited_log import RateLimitedLog
from ..lib.security import QUERY_MESSAGE_FIELDS, log_tool_call
from ..lib.sqlite import (
    FILTER_TYPE_ERROR,
    LIST_THREAD_FILTERS,
    AmbiguousMessageId,
    InvalidFilterError,
    MessageBody,
    MessageRecord,
    Participant,
    ReapedSource,
    address_match_mode,
    canonical_addr,
    validate_date_range,
)
from ..lib.validation import clamp_int
from .outputs import (
    HEADER_CHAR_LIMIT,
    MAX_LISTED,
    AddressFilterMatch,
    Contact,
    FilterUse,
    FindContactOutput,
    Folder,
    GetMessageOutput,
    GetThreadOutput,
    ListFoldersOutput,
    ListThreadsOutput,
    QueryMessagesOutput,
    ReapedMessage,
    ThreadMessage,
    clip,
    date_bounds,
    describe_date_bounds,
    listed_message,
    message_headers,
    project_rows,
    query_messages_output_schema,
    read_only,
    reaped_source,
    thread_summary,
    tool_result,
)

log = logging.getLogger("mcp.tools.retrieval")

# Ceiling on query_messages page size: enumeration pages by cursor, so a
# large page only bloats one response.
_MAX_QUERY_LIMIT = 100

# Seconds per window of the rate-limited ``fields`` rejection warning:
# a client can repeat a rejected projection as fast as it likes.
_FIELDS_REJECTION_LOG_INTERVAL_SECS = 60.0

# Recipients rendered per role before the rest are summarized as a count.
_MAX_LISTED_PARTICIPANTS = 10

# get_thread pages by message and cuts each body, so neither a long thread
# nor a long message makes an unbounded response; get_message pages one
# message's body by character offset, so every page is bounded and the
# pages together hold the whole body.
_DEFAULT_THREAD_PAGE = 10
_MAX_THREAD_PAGE = 50
_THREAD_BODY_CHAR_LIMIT = 4000
# One get_message body page (#489): about 6,700 tokens at the 3
# characters per token the inference budget counts
# (``lib/inference.py`` ``CHARS_PER_TOKEN``), a fifth of the default
# 32,768-token window and half of a full get_thread page (10 bodies of
# 4,000 characters). Five get_thread body cuts, so a long message reads in
# a few calls.
_MESSAGE_BODY_PAGE_CHARS = 20_000
# A page cut that would split a combining sequence moves back at most
# this many code points; past that (a run of marks longer than any real
# grapheme) the cut stays where it is. Reconstruction is exact either way.
_MAX_CUT_BACKOFF = 32
# Headers are sender-controlled too: every tool, get_message included,
# lists at most this many References and cuts every header value at
# HEADER_CHAR_LIMIT characters.
_MAX_LISTED_REFERENCES = 10


def _join_limited(items: list[str], limit: int) -> str:
    """At most ``limit`` of ``items``, joined and cut at
    ``HEADER_CHAR_LIMIT``, then a count of the entries not listed. The
    count follows the cut so a list of long entries keeps it."""
    joined = clip(", ".join(items[:limit]), HEADER_CHAR_LIMIT)
    if len(items) <= limit:
        return joined
    return joined + f" (+{len(items) - limit} more)"


def _format_participants(people: list[Participant], limit: int = _MAX_LISTED_PARTICIPANTS) -> str:
    return _join_limited(
        [f"{p.name} <{p.address}>" if p.name else p.address for p in people], limit
    )


def _header_lines(m: MessageRecord) -> list[str]:
    """A message's own headers, one per line; absent ones are omitted.

    Long lists are summarized and long values cut, so a response stays
    bounded whatever a sender put in the headers. List values come cut
    from ``_join_limited``; the others are cut here.
    """
    headers = [("Subject", clip(m.subject, HEADER_CHAR_LIMIT))]
    for label, people in (("From", m.from_), ("To", m.to), ("Cc", m.cc)):
        if people:
            headers.append((label, _format_participants(people)))
    headers.append(("Sent", m.sent_at))
    if m.occurred_at:
        headers.append(("Delivered", m.occurred_at))
    headers.append(("Folder", clip(m.folder, HEADER_CHAR_LIMIT)))
    if m.in_reply_to:
        headers.append(("In-Reply-To", clip(m.in_reply_to, HEADER_CHAR_LIMIT)))
    if m.references:
        headers.append(("References", _join_limited(m.references, _MAX_LISTED_REFERENCES)))
    headers.append(("Attachments", "yes" if m.has_attachments else "no"))
    headers.append(("Status", ", ".join(_state_words(m))))
    return [f"{label}: {value}" for label, value in headers]


def _state_words(m: MessageRecord) -> list[str]:
    """``m``'s Maildir state in words: read or unread, then flagged and
    replied when set."""
    words = ["read" if m.seen else "unread"]
    if m.flagged:
        words.append("flagged")
    if m.replied:
        words.append("replied")
    return words


def _joins_previous(text: str, i: int) -> bool:
    """Whether ``text[i]`` belongs to the same grapheme as ``text[i - 1]``:
    a combining mark, or either side of a zero-width joiner."""
    return (
        unicodedata.category(text[i]).startswith("M")
        or text[i] == "\u200d"
        or text[i - 1] == "\u200d"
    )


def _body_page(text: str, offset: int) -> tuple[str, int | None]:
    """The page of ``text`` starting at ``offset`` and the next page's
    offset (``None`` at the end).

    Python strings index by code point, so a cut never splits one; a cut
    that would separate a combining mark or a zero-width-joined pair from
    the character before it moves back to that character (at most
    ``_MAX_CUT_BACKOFF`` code points, and never to ``offset`` itself, so
    every page makes progress).
    """
    if offset < 0 or offset > len(text):
        raise InvalidFilterError(
            "offset",
            f"offset {offset} is past the end of the body ({len(text):,} characters)"
            if offset > 0
            else f"offset must be 0 or more, got {offset}",
        )
    end = offset + _MESSAGE_BODY_PAGE_CHARS
    if end >= len(text):
        return text[offset:], None
    cut = end
    while cut > offset + 1 and end - cut < _MAX_CUT_BACKOFF and _joins_previous(text, cut):
        cut -= 1
    if _joins_previous(text, cut):
        cut = end
    return text[offset:cut], cut


def _thread_message(m: MessageRecord, body: MessageBody | None) -> ThreadMessage:
    """One get_thread message: bounded headers plus its cut body."""
    return ThreadMessage(
        **message_headers(m).model_dump(),
        body=body.text if body else None,
        body_omitted_chars=body.omitted_chars if body else 0,
    )


def _listed_lines(i: int, m: MessageRecord, fields: frozenset[str] | None) -> list[str]:
    """Row ``i`` of a query_messages page in prose. With a ``fields``
    projection (#990) only the projected fields appear; the claimant and
    thread IDs always do."""

    def shown(name: str) -> bool:
        return fields is None or name in fields

    head = []
    if shown("sent_at"):
        head.append(m.sent_at)
    if m.occurred_at and shown("occurred_at"):
        head.append(f"delivered {m.occurred_at}")
    if shown("folder"):
        head.append(m.folder)
    for name, word, on in (
        ("seen", "unread", not m.seen),
        ("flagged", "flagged", m.flagged),
        ("replied", "replied", m.replied),
        ("has_attachments", "attachments", m.has_attachments),
        ("pending_deletion", "pending deletion", m.pending_deletion),
    ):
        if on and shown(name):
            head.append(word)
    lines = [f"{i}. " + " | ".join(head) if head else f"{i}."]
    if shown("subject"):
        lines.append(f"   Subject: {clip(m.subject, HEADER_CHAR_LIMIT)}")
    for name, label, people in (("from", "From", m.from_), ("to", "To", m.to), ("cc", "Cc", m.cc)):
        if people and shown(name):
            lines.append(f"   {label}: {_format_participants(people)}")
    ids = [f"Claimant ID: {m.claimant_id}"]
    if shown("message_id"):
        ids.insert(0, f"Message-ID: {m.message_id}")
    lines.append("   " + " | ".join(ids))
    lines.append(f"   Thread ID: {m.thread_id}")
    return lines


def _projected(result: CallToolResult, fields: frozenset[str] | None) -> CallToolResult:
    """``result`` with its structured rows cut to ``fields``, if given."""
    if fields is not None and result.structured_content is not None:
        result.structured_content = project_rows(result.structured_content, fields)
    return result


def _filter_uses(args: dict) -> list[FilterUse]:
    """How query_messages applied each given filter, so the caller knows
    whether an address matched exactly or as a substring."""
    uses = []
    for key, value in args.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if key in ("sender", "recipient", "participant"):
            if address_match_mode(value) == "exact":
                uses.append(
                    FilterUse(filter=key, value=canonical_addr(value), match="exact_address")
                )
            else:
                uses.append(FilterUse(filter=key, value=value.strip(), match="substring"))
        elif key == "subject":
            uses.append(FilterUse(filter=key, value=value.strip(), match="substring"))
        elif key == "text":
            uses.append(FilterUse(filter=key, value=value.strip(), match="all_words"))
        elif key in ("date_from", "date_to"):
            uses.append(FilterUse(filter=key, value=value, match="inclusive_bound"))
        else:
            uses.append(FilterUse(filter=key, value=value, match="equals"))
    return uses


def _describe_filters(uses: list[FilterUse]) -> str:
    """The prose form of ``_filter_uses``."""
    parts = []
    for u in uses:
        if u.match == "exact_address":
            parts.append(f"{u.filter}={u.value} (exact address)")
        elif u.match == "substring" and u.filter != "subject":
            parts.append(f"{u.filter}={u.value!r} (substring of address or name)")
        elif u.match == "substring":
            parts.append(f"subject={u.value!r} (case-insensitive substring)")
        elif u.match == "all_words":
            parts.append(f"text={u.value!r} (all words, message body)")
        else:
            parts.append(f"{u.filter}={u.value!r}")
    return ", ".join(parts) or "no filters (every indexed message outside Trash)"


def register_retrieval_tools(server, db):
    fields_rejections = RateLimitedLog(
        log,
        ("too_many", "unknown_name"),
        _FIELDS_REJECTION_LOG_INTERVAL_SECS,
        first_msg="query_messages rejected invalid fields: reason=%s",
        summary_msg="query_messages rejected invalid fields in the last %ds: %s",
    )
    local_only_note = (
        "mcp-server has no live Bridge access. "
        "This response is based on the local SQLite index only."
    )

    @server.tool(
        output_schema=GetThreadOutput.model_json_schema(),
        annotations=read_only("Get Thread"),
    )
    @timings.timed_tool("get_thread")
    async def get_thread(
        thread_id: str,
        include_attachments_metadata: bool = True,
        offset: int = 0,
        limit: int = _DEFAULT_THREAD_PAGE,
    ) -> CallToolResult:
        """
        Get one thread's messages by thread ID, oldest first — each
        message's own headers and body; no attachment content.

        For a potentially exhaustive review, disclose the target thread
        and planned content read before the first call, including transfer
        to the calling model, which may be remote. This is not a metadata
        probe: even ``limit=1`` can return accumulated thread context when
        per-message bodies are absent; say that the initial read can include
        the whole conversation's indexed context. After that disclosure,
        use ``limit=1`` only if the count is not already known, to learn
        ``total_messages``. Before bulk thread/body paging, disclose how
        many messages you plan to read. Stay within the requested or approved
        scope; ask before expanding it. A narrower task may need only one page.

        Pages by message: the response states the thread's message
        count and, when more remain, the ``offset`` for the next call.
        Each body is cut at 4,000 characters, with a marker saying how
        much was left out; long header values and lists are shortened the
        same way. When a message's ``body_omitted_chars`` is positive,
        call ``get_message`` with its claimant ID and follow that tool's
        ``next_offset`` until null to read the rest of its indexed body.
        To read all currently indexed messages, also page this tool
        until its own ``next_offset`` is null. Report unread pages,
        ``reaped_messages`` and ``reaped_messages_truncated`` as coverage
        limits: paging cannot recover removed messages. Pages use separate
        snapshots, so concurrent indexing can change the conversation.

        DO NOT use this to read attachment content (PDFs, OCR'd
        images, scans). It returns the message bodies only; the
        attachment chunks are not in the result. For any question
        that requires the text of an attached PDF or image — "what
        does the quote PDF say?", "compare the attached statement
        against the email body" — call ``ask_mailbox`` or
        ``search_attachments`` instead. Iterating ``get_thread``
        across multiple threads to find attachment content is the
        wrong shape and will not surface it; the extracted attachment
        text lives in a separate chunk lane that get_thread never
        reads. Reaching for an external tool (Google Drive, web
        search) to read a PDF that arrived as an email attachment is
        also wrong — the local index has
        already extracted that PDF's text.

        ``thread_id`` is OPAQUE: pass only a ``Thread ID`` that a tool
        result returned. Do NOT pass a subject line, a
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
            Message-ID, subject, From / To / Cc, send date and (when
            known) delivery date (UTC), folder, reply headers,
            attachment flag, and indexed body
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
            # Fixed-text causes: the ID is the caller's and stays out of the log.
            if isinstance(page, ReapedSource):
                log.warning("get_thread failed: reaped")
                raise ToolError(reaped_source("Thread", thread_id, page.reaped_at))
            if not page:
                log.warning("get_thread failed: not found")
                raise ToolError(f"Thread not found: {thread_id}")
            thread, messages, total = page.thread, page.messages, page.total_messages
            timings.count("messages", len(messages))

            if messages:
                count = f"{total} (showing {offset + 1}-{offset + len(messages)}, oldest first)"
            else:
                count = str(total)
            lines = [
                f"Thread: {clip(thread.subject, HEADER_CHAR_LIMIT)}",
                f"Thread ID: {thread.thread_id}",
                f"Folder: {thread.folder}",
                "Participants: " + _join_limited(thread.participants, _MAX_LISTED_PARTICIPANTS),
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
                    "long headers are shortened):"
                )
            elif total:
                lines.append(f"No messages at offset {offset}; the thread has {total}.")
            for i, m in enumerate(messages, offset + 1):
                lines += ["", f"[{i}/{total}] Message-ID: {m.message_id}"]
                lines.append(f"Claimant ID: {m.claimant_id}")
                lines += _header_lines(m)
                lines.append("")
                body = page.bodies.get(m.claimant_id)
                if body is None:
                    lines.append("(No body text is indexed for this message.)")
                    continue
                lines.append(body.text)
                if body.omitted_chars:
                    lines.append(
                        f"[{body.omitted_chars:,} more characters not shown; "
                        f'get_message("{m.claimant_id}") pages through the full body: '
                        "follow next_offset]"
                    )
            if offset + len(messages) < total:
                lines += [
                    "",
                    f"More messages: call get_thread with offset={offset + len(messages)}.",
                ]
            if page.reaped:
                more = " (more not listed)" if page.reaped_truncated else ""
                lines += [
                    "",
                    "Messages reaped from the index (mirror retention: deleted upstream "
                    f"or missing from the Maildir; content no longer available){more}:",
                    *(f"  {r.claimant_id} (reaped {r.reaped_at[:10]})" for r in page.reaped),
                ]

            # No message body indexed yet (e.g. chunking still pending):
            # fall back to the accumulated thread text, a retrieval
            # artifact that also carries quoted replies.
            # It is labelled context (#755): no one message's own text.
            thread_text = None
            if not page.has_bodies:
                if thread.body_text:
                    thread_text = thread.body_text
                    lines += [
                        "",
                        "Indexed thread text (context, not any one message's text):",
                        "",
                        thread.body_text,
                    ]
                elif thread.snippet:
                    thread_text = thread.snippet
                    lines += [
                        "",
                        "Indexed snippet (context, not any one message's text):",
                        "",
                        thread.snippet,
                    ]

            if include_attachments_metadata and thread.has_attachments:
                lines.append("")
                lines.append(
                    "Attachments are present in this thread; use search_attachments "
                    "to search their extracted text."
                )

            next_offset = offset + len(messages)
            output = GetThreadOutput(
                thread=thread_summary(thread),
                total_messages=total,
                offset=offset,
                messages=[_thread_message(m, page.bodies.get(m.claimant_id)) for m in messages],
                next_offset=next_offset if next_offset < total else None,
                indexed_thread_text=thread_text,
                indexed_thread_text_scope="context" if thread_text is not None else None,
                reaped_messages=[
                    ReapedMessage(claimant_id=r.claimant_id, reaped_at=r.reaped_at)
                    for r in page.reaped
                ],
                reaped_messages_truncated=page.reaped_truncated,
            )
            return tool_result("\n".join(lines), output)

        except ToolError:
            raise
        except Exception as e:
            log.error("get_thread error: %s", type(e).__name__)
            raise ToolError(f"Error: {type(e).__name__}") from e

    @server.tool(
        output_schema=GetMessageOutput.model_json_schema(),
        annotations=read_only("Get Message"),
    )
    @timings.timed_tool("get_message")
    async def get_message(
        message_id: str,
        offset: int = 0,
    ) -> CallToolResult:
        """
        Get one message's own headers and indexed body, one page of the
        body at a time.

        For reads within a filtered or exhaustive review, disclose before
        calling that this content read may also return bounded parent-thread
        context when the message has no indexed body,
        including other messages outside the requested sender/date scope.
        This context reaches the calling model, which may be remote. If
        that exceeds the requested or approved scope, ask before this call;
        a message ID or body offset does not prevent the context fallback.

        Headers come from the message itself: subject, From / To / Cc,
        send date and (when known) delivery date (UTC), folder,
        In-Reply-To, References, and the attachment flag. Headers are
        sender-controlled, so at most 10 entries per recipient role and
        10 References are listed (with a "+N more" count) and any value
        past 500 characters is cut with a marker.

        Reconstructs the message body from the per-message chunk store
        (in document order) — the index keeps no raw per-message body,
        so this is the indexed text after quoted-reply stripping, which
        is usually what you want for "show me the message from Jane on
        Tuesday". The body is returned in pages of 20,000 characters:
        the response states which characters it shows of how many and,
        when more remain, the ``offset`` for the next call. Calling
        with each ``next_offset`` in turn returns the whole body.
        Attachment text is NOT included here; use get_evidence or
        ask_mailbox for attachment content. When no body chunks are
        indexed for the message, ``body: null`` means no indexed body;
        ``indexed_thread_text`` is conversation context, not this
        message's text (``indexed_thread_text_scope`` says context, as
        ask_mailbox labels such passages). Report this gap rather than attributing the
        context to this message or treating a missing body as proof
        that the message contained no relevant evidence.

        ``message_id`` is a ``Claimant ID`` (the Message-ID plus
        ``#`` and a short hash, which names exactly one message) or the
        bare RFC 5322 Message-ID. Obtain it from a thread's message list
        (via get_thread), query_messages, or get_evidence. The sender
        sets the Message-ID, so different messages can share one: a
        bare Message-ID several messages claim returns an error listing
        their claimant IDs; call again with one of them. Do NOT pass a
        subject line or a phrase — invented IDs return
        ``Message not found``.

        Args:
            message_id: A claimant ID, or the Message-ID header value
            offset: Body character to start the page at (default 0); pass
                the previous response's next_offset to read on

        Returns:
            The message's headers, its thread ID and subject, and one
            page of its reconstructed indexed body, or thread context
            when no body chunks are indexed.
        """
        log_tool_call(
            log,
            "get_message",
            {"message_id": message_id, "offset": offset},
        )
        try:
            if offset < 0:
                # Rejected before any read; past-the-end needs the body.
                _body_page("", offset)
            view = await asyncio.to_thread(db.get_message_view, message_id)
            # Fixed-text causes: the ID is the caller's and stays out of the log.
            if isinstance(view, ReapedSource):
                log.warning("get_message failed: reaped")
                raise ToolError(reaped_source("Message", message_id, view.reaped_at))
            if not view:
                log.warning("get_message failed: not found")
                raise ToolError(f"Message not found: {message_id}")
            if isinstance(view, AmbiguousMessageId):
                log.warning(
                    "get_message failed: ambiguous Message-ID (%d claimants listed)",
                    len(view.claimants),
                )
                # Never pick one: either claimant may be the reused ID.
                listed = "; ".join(
                    f"{c.claimant_id} (sent {c.sent_at}, folder {c.folder})" for c in view.claimants
                )
                count = (
                    f"more than {len(view.claimants)}"
                    if view.truncated
                    else str(len(view.claimants))
                )
                shown = (
                    f"the oldest {len(view.claimants)} of these claimant IDs"
                    if view.truncated
                    else "these claimant IDs"
                )
                raise ToolError(
                    f"Message-ID {view.message_id} names {count} messages "
                    f"(different files claim it). Call get_message with one of {shown}: "
                    f"{listed}"
                )
            thread = view.thread
            thread_text = None
            body_text = view.body.text if view.body else ""
            page, next_offset = _body_page(body_text, offset)

            lines = [
                f"Message-ID: {view.record.message_id}",
                f"Claimant ID: {view.record.claimant_id}",
            ]
            if view.other_claimants:
                shown = (
                    f" (first {len(view.other_claimants)} of more than "
                    f"{len(view.other_claimants)}, by claimant ID)"
                    if view.other_claimants_truncated
                    else ""
                )
                lines.append(
                    f"Other messages with this Message-ID (different files claim it){shown}: "
                    + ", ".join(view.other_claimants)
                )
            lines += [
                *_header_lines(view.record),
                f"Thread: {clip(thread.subject, HEADER_CHAR_LIMIT)}",
                f"Thread ID: {thread.thread_id}",
                f"Mode: {local_only_note}",
            ]
            if view.record.pending_deletion:
                lines.append(
                    "Pending deletion: yes (deleted in Proton or its file is missing locally; "
                    "mirror retention removes it after the grace period)"
                )
            if f := view.record.source_file:
                size = "unknown size" if f.size_bytes is None else f"{f.size_bytes:,} bytes"
                lines.append(f"Source file: {f.locator} ({size}, sha256 {f.sha256 or 'unknown'})")

            if view.body and not page:
                lines += [
                    "",
                    f"No body text past offset {offset}; the body has "
                    f"{len(body_text):,} characters.",
                ]
            elif view.body:
                shown = (
                    ""
                    if offset == 0 and next_offset is None
                    else f"; characters {offset + 1:,}-{offset + len(page):,} of {len(body_text):,}"
                )
                lines += [
                    "",
                    "Message body (the indexed body after quoted-reply "
                    f"stripping, not the raw message{shown}):",
                    "",
                    page,
                ]
                if next_offset is not None:
                    lines += [
                        "",
                        f"[{len(body_text) - next_offset:,} more characters: "
                        f"call get_message with offset={next_offset}]",
                    ]
            else:
                # No body chunks — an empty-body message or one not
                # chunked yet. Fall back to the
                # accumulated parent-thread context.
                lines += [
                    "",
                    "No per-message body chunks are indexed for this message. "
                    "Showing indexed thread context instead; use get_thread "
                    "for the full thread.",
                ]
                # Labelled context (#755): not this message's own text.
                if thread.body_text:
                    thread_text = thread.body_text
                    lines += [
                        "",
                        "Indexed thread text (context, not this message's text):",
                        "",
                        thread.body_text,
                    ]
                elif thread.snippet:
                    thread_text = thread.snippet
                    lines += [
                        "",
                        "Indexed snippet (context, not this message's text):",
                        "",
                        thread.snippet,
                    ]

            output = GetMessageOutput(
                message=listed_message(view.record),
                other_claimants=view.other_claimants,
                other_claimants_truncated=view.other_claimants_truncated,
                thread_subject=clip(thread.subject, HEADER_CHAR_LIMIT),
                body=page if view.body else None,
                body_offset=offset,
                body_total_chars=len(body_text),
                next_offset=next_offset,
                indexed_thread_text=thread_text,
                indexed_thread_text_scope="context" if thread_text is not None else None,
            )
            timings.count("messages", 1)
            return tool_result("\n".join(lines), output)

        except ToolError:
            raise
        except InvalidFilterError as e:
            # The message quotes the offset; log only the field.
            log.warning("get_message rejected invalid %s", e.field_name)
            raise ToolError(f"Error: {e}") from e
        except Exception as e:
            log.error("get_message error: %s", type(e).__name__)
            raise ToolError(f"Error: {type(e).__name__}") from e

    @server.tool(
        output_schema=ListThreadsOutput.model_json_schema(),
        annotations=read_only("List Threads"),
    )
    @timings.timed_tool("list_threads")
    async def list_threads(
        folder: str = "INBOX",
        filter_type: str = "all",
        limit: int = 20,
        offset: int = 0,
    ) -> CallToolResult:
        """
        List email threads in a folder from the local index.

        Use this ONLY for unfiltered browse-style requests — "show me
        my recent emails", "what's in my inbox", "list my latest
        threads". This tool has no keyword, sender, date, or topic
        filter; it returns threads sorted by most recent activity.

        For ANY filtered request — by topic, keyword, sender (name OR
        address), date range, or attachment status — use
        ``search_emails`` instead, which exposes all of those filters.
        Exception: "5 most recent from <person>" is a
        ``query_messages(sender=..., limit=5)`` call (newest first),
        not a ``list_threads`` call; ``search_emails`` ranks by
        relevance, not date. For exhaustive listing or counting of
        messages by exact criteria ("every message from X", "how many
        in Archive since March"), use ``query_messages``.

        Args:
            folder: Folder name (default: INBOX)
            filter_type: "all" (default), "unread" (threads with an
                         unread message in the folder) or "flagged"
                         (threads with a flagged / starred message in
                         the folder). Other values return a validation
                         error.
            limit: Number of threads to return (default: 20)
            offset: Pagination offset (default: 0)

        Returns:
            Threads with at least one message in the folder, sorted by
            most recent activity. Each thread's ``folder`` is its
            representative folder (where the message that started it
            was filed when first indexed).
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
        # Validated here so its fixed message is the only text returned;
        # every other failure below is reported by type (#257).
        if filter_type not in LIST_THREAD_FILTERS:
            log.warning("list_threads rejected invalid input (filter_type)")
            raise ToolError(f"Error: {FILTER_TYPE_ERROR}")

        try:
            threads = await asyncio.to_thread(
                db.list_threads,
                folder=folder,
                filter_type=filter_type,
                limit=limit,
                offset=offset,
            )

            output = ListThreadsOutput(
                folder=folder,
                filter_type=filter_type,
                offset=offset,
                threads=[thread_summary(t) for t in threads],
            )
            timings.count("threads", len(threads))
            scope = "" if filter_type == "all" else f" with {filter_type} messages"
            if not threads:
                return tool_result(f"No threads{scope} found in {folder}.", output)

            lines = [f"Threads{scope} in {folder} ({len(threads)} shown):\n"]
            for i, t in enumerate(threads, 1 + offset):
                participants = ", ".join(clip(p, HEADER_CHAR_LIMIT) for p in t.participants[:2])
                lines.append(
                    f"{i}. {clip(t.subject, HEADER_CHAR_LIMIT)}\n"
                    f"   {participants}"
                    f"{'...' if len(t.participants) > 2 else ''} | "
                    f"{t.date_last.strftime('%Y-%m-%d')} | "
                    f"{len(t.message_ids)} msg(s)"
                    f"{'  📎' if t.has_attachments else ''}\n"
                    f"   ID: {t.thread_id}\n"
                )

            return tool_result("\n".join(lines), output)

        except Exception as e:
            # Type only, here and in the other handlers: an SQLite error
            # can quote query text or stored mail (#257).
            log.error("list_threads error: %s", type(e).__name__)
            raise ToolError(f"Error: {type(e).__name__}") from e

    @server.tool(
        output_schema=query_messages_output_schema(),
        annotations=read_only("Query Messages"),
    )
    @timings.timed_tool("query_messages")
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
        authority_class: str | None = None,
        seen: bool | None = None,
        flagged: bool | None = None,
        limit: int = 25,
        cursor: str | None = None,
        fields: list[str] | None = None,
    ) -> CallToolResult:
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
        are ignored; with none, every indexed message outside Trash is
        enumerated. Messages in Trash are counted only by a separate
        call with ``folder="Trash"``.

        Start with narrow filters and ``limit=1`` to obtain the count.
        Before bulk paging or reading bodies, tell the user the scope
        and how many messages you will read: tool results go to the
        calling model, which may be remote. Prefer the smallest sufficient
        sample when it answers the question; a sample cannot establish
        an exhaustive content audit.
        Subsequent body reads can also return parent-thread context beyond
        these message filters. Include that possible context in the pre-read
        scope disclosure; ask before a content call would exceed the
        requested or approved scope. Filtering this list does not restrict
        the context returned by ``get_message`` or ``get_thread``.

        Paging: the response states ``total_matches``, how many were
        returned, and ``has_more``. When ``has_more`` is true, call
        again with the SAME filters plus ``cursor`` set to the returned
        ``next_cursor``. Never report a partial page as the complete
        answer. To examine every match, continue until ``has_more`` is
        false; a count of these exact criteria needs only ``total_matches``.
        An exhausted keyword query does not prove exhaustive coverage of
        a topic: consider alternate wording, read candidate messages,
        and distinguish messages from threads or distinct bills/items.
        Each page uses a fresh index snapshot; new matches ahead of the
        cursor can be missed. A changed ``total_matches`` signals churn,
        but the same total does not prove a stable set. Scope coverage to
        the indexed results observed during the run, not a point-in-time
        complete mailbox. For large pages, ``fields`` keeps only the named
        row fields, e.g. ``["subject", "sent_at", "from", "has_attachments"]``.

        When a person's exact address is unknown, enumerate name-substring
        matches in the requested sender/recipient role and folder with
        this tool, following the disclosure and paging guidance above.
        ``find_contact`` is capped and ranks across all roles/folders;
        it cannot establish the complete candidate set. Prefer the
        intended person's exact address once resolved. Report truncated
        headers or unresolved identities as limits; ask the user if
        identity remains ambiguous rather than combining namesakes.
        For outstanding-item questions,
        check for completion, corrections and reopening in different threads and
        senders before calling an item open or closed. A sent request or
        delivered advice does not establish that the action was completed.
        State the scope and any unread pages, missing indexed bodies or
        unavailable attachment text instead of claiming full coverage.

        Args:
            sender: From address. A full address ("jane@example.com")
                    matches exactly; anything else ("@example.com",
                    "Jane") is a case-insensitive substring of the
                    address or display name. The response states which,
                    and how many distinct addresses the filter matched
                    across all matches; above 1, a name may cover
                    different people.
            recipient: To or Cc, matched like ``sender``.
            participant: Any role (From, To, or Cc), matched like ``sender``.
            subject: Case-insensitive substring of the message subject.
            text: Words that must ALL appear in the message body (word
                  match with stemming). Searches the message's own
                  text only — not attachments and not quoted earlier
                  replies. For attachment content use search_attachments.
            folder: Exact folder name (see list_folders). Without it,
                    messages filed in Trash are left out; pass "Trash"
                    to list them.
            date_from: ISO 8601 lower bound, inclusive, on the
                       message's time: its delivery date (occurred_at),
                       else its send date (sent_at).
            date_to: ISO 8601 upper bound, inclusive; a date-only value
                     covers the whole day (UTC). For either bound in
                     the user's time zone, give an offset
                     ("2026-01-01T00:00:00-05:00"). The response's
                     ``date_bounds`` echoes the UTC instants applied.
            has_attachments: True for messages with attachments, False
                             for messages without.
            authority_class: Messages whose sender the operator's rules
                             file classes as this: "counsel",
                             "management", "vendor", "government",
                             "personal", "other", or "unclassified"
                             (no rule matched). Spam-folder messages
                             never match.
            seen: True for messages read in Proton, False for unread.
            flagged: True for flagged (starred) messages, False for the rest.
            limit: Messages per page (default 25, clamped to [1, 100]).
            cursor: ``next_cursor`` from the previous page of the same query.
            fields: Row fields to return; claimant_id and thread_id are
                    always kept. Omit for every field.

        Returns:
            The filter interpretation, total_matches, the page's
            messages newest first by that time (send and delivery
            date, folder, subject, From / To / Cc, Message-ID, Thread
            ID), and paging state.
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
            "authority_class": authority_class,
            "seen": seen,
            "flagged": flagged,
        }
        log_tool_call(
            log, "query_messages", {**args, "limit": limit, "cursor": cursor, "fields": fields}
        )
        limit = clamp_int(limit, default=25, minimum=1, maximum=_MAX_QUERY_LIMIT)
        projection = None
        if fields is not None:
            if len(fields) > len(QUERY_MESSAGE_FIELDS):
                # Repeats add nothing: refuse the list before checking
                # each name, with fixed text.
                fields_rejections.record("too_many")
                raise ToolError(f"Error: fields lists at most {len(QUERY_MESSAGE_FIELDS)} names")
            unknown = [f for f in fields if f not in QUERY_MESSAGE_FIELDS]
            if unknown:
                # The name goes back to the caller only; the log names
                # the parameter and reason, rate-limited.
                fields_rejections.record("unknown_name")
                raise ToolError(
                    f"Error: unknown field {clip(unknown[0], 100)!r} in fields; "
                    f"valid: {', '.join(QUERY_MESSAGE_FIELDS)}"
                )
            projection = frozenset(fields) | {"claimant_id", "thread_id"}
        # Reject a bad date range before any retrieval work.
        try:
            bounds = date_bounds(*validate_date_range(date_from, date_to))
        except InvalidFilterError as e:
            log.warning("query_messages rejected invalid %s", e.field_name)
            raise ToolError(f"Error: {e}") from e

        try:
            page = await asyncio.to_thread(db.query_messages, **args, limit=limit, cursor=cursor)
        except InvalidFilterError as e:
            # Validation messages quote the offending input (an invalid
            # date echoes its text), which log_tool_call deliberately
            # withheld. Return it to the caller; log only the field. Any
            # other ValueError (converting stored rows) can quote mail and
            # falls through to the type-only branch (#257).
            log.warning("query_messages rejected invalid input (%s)", e.field_name)
            raise ToolError(f"Error: {e}") from e
        except Exception as e:
            log.error("query_messages error: %s", type(e).__name__)
            raise ToolError(f"Error: {type(e).__name__}") from e

        timings.count("total_matches", page.total_matches)
        timings.count("returned", len(page.messages))
        uses = _filter_uses(args)
        output = QueryMessagesOutput(
            filters=uses,
            address_matches=[
                AddressFilterMatch(
                    filter=name,
                    distinct_addresses=match.distinct,
                    addresses=[clip(a, HEADER_CHAR_LIMIT) for a in match.addresses],
                )
                for name, match in page.address_matches.items()
            ],
            date_bounds=bounds,
            total_matches=page.total_matches,
            returned=len(page.messages),
            offset=page.offset,
            has_more=page.has_more,
            next_cursor=page.next_cursor,
            messages=[listed_message(m) for m in page.messages],
        )
        lines = [f"Query: {_describe_filters(uses)}"]
        if bounds_line := describe_date_bounds(bounds):
            lines.append(bounds_line)
        lines.append(f"total_matches: {page.total_matches}")
        # Counts only: the addresses themselves are in the structured
        # output (#801).
        for name, match in page.address_matches.items():
            noun = "address" if match.distinct == 1 else "addresses"
            line = f"{name} matched {match.distinct} distinct {noun} across all matches"
            if match.distinct > 1:
                line += " (possibly different people; filter by one exact address to separate them)"
            lines.append(line)
        if not page.messages:
            lines.append("returned: 0")
            lines.append("has_more: false")
            lines.append("No messages match." if page.offset == 0 else "No further messages.")
            return _projected(tool_result("\n".join(lines), output), projection)

        first, last = page.offset + 1, page.offset + len(page.messages)
        lines.append(f"returned: {len(page.messages)} (matches {first}-{last})")
        lines.append(f"has_more: {'true' if page.has_more else 'false'}")
        if page.next_cursor:
            lines.append(f"next_cursor: {page.next_cursor}")
            lines.append("(Call again with the same filters and this cursor for the next page.)")
        lines.append("")

        for i, m in enumerate(page.messages, first):
            lines.extend(_listed_lines(i, m, projection))
            lines.append("")

        return _projected(tool_result("\n".join(lines), output), projection)

    @server.tool(
        output_schema=FindContactOutput.model_json_schema(),
        annotations=read_only("Find Contact"),
    )
    @timings.timed_tool("find_contact")
    async def find_contact(
        query: str,
        limit: int = 10,
    ) -> CallToolResult:
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

        Each contact's authority class comes from the operator's rules
        file matched against the claimed From address, not a verified
        sender; the authority_class filters on search_emails and
        query_messages skip Spam-folder mail.

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
            log.warning("find_contact rejected an empty query")
            raise ToolError("Provide a name, address, or domain fragment to search for.")

        try:
            contacts = await asyncio.to_thread(db.find_contact, query, limit)
        except Exception as e:
            log.error("find_contact error: %s", type(e).__name__)
            raise ToolError(f"Error: {type(e).__name__}") from e
        timings.count("contacts", len(contacts))

        # A contact's names are sender-controlled and unbounded in number
        # and length: list at most MAX_LISTED, each cut, with the count.
        output = FindContactOutput(
            contacts=[
                Contact(
                    email=c["email"],
                    names=[clip(n, HEADER_CHAR_LIMIT) for n in c["names"][:MAX_LISTED]],
                    name_count=len(c["names"]),
                    thread_count=c["thread_count"],
                    organization=c["organization"],
                    authority_class=c["authority_class"],
                    authority_rule=c["authority_rule"],
                )
                for c in contacts
            ]
        )
        if not contacts:
            return tool_result(f"No contacts found matching: '{query}'", output)

        lines = [f"Contacts matching '{query}' ({len(contacts)} shown):\n"]
        for i, c in enumerate(output.contacts, 1):
            names = ", ".join(c.names) if c.names else "(no display name)"
            if c.name_count > len(c.names):
                names += f" (+{c.name_count - len(c.names)} more)"
            lines.append(
                f"{i}. {c.email}\n   Name(s): {names}\n"
                f"   Organization: {c.organization or '(none)'}\n"
                f"   Authority: {c.authority_class}"
                f"{f' (rule {c.authority_rule})' if c.authority_rule else ''}\n"
                f"   Threads: {c.thread_count}\n"
            )
        return tool_result("\n".join(lines), output)

    @server.tool(
        output_schema=ListFoldersOutput.model_json_schema(),
        annotations=read_only("List Folders"),
    )
    @timings.timed_tool("list_folders")
    async def list_folders() -> CallToolResult:
        """
        List all available email folders and their thread counts.

        Use this when the user asks structural questions about the
        mailbox — "what folders do I have?", "what mailboxes are
        synced?", "is the Archive folder indexed?". Don't call this
        before search_emails as a discovery step; search_emails
        already understands folder filters when the user names them.

        Returns:
            All folders with thread counts from the local index. A
            folder's count is the threads with a message in it, so a
            thread spanning folders counts in each.
        """
        log.info("tool=list_folders")
        try:
            folders = await asyncio.to_thread(db.list_folders)
            timings.count("folders", len(folders))
            output = ListFoldersOutput(
                folders=[Folder(name=f["name"], thread_count=f["thread_count"]) for f in folders]
            )
            if not folders:
                return tool_result("No folders found in index.", output)

            lines = ["Folders:\n"]
            for f in folders:
                lines.append(f"  {f['name']}  ({f['thread_count']} threads)")

            return tool_result("\n".join(lines), output)

        except Exception as e:
            log.error("list_folders error: %s", type(e).__name__)
            raise ToolError(f"Error: {type(e).__name__}") from e
