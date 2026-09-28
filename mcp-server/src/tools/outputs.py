"""
Structured output for the search, retrieval, evidence, and status tools.

Each tool declares one of these models as its ``outputSchema`` (via an
``Annotated[CallToolResult, Model]`` return annotation) and returns it as
``structuredContent`` next to the unchanged prose in ``content``. A client
chains IDs (thread_id -> message_id -> attachment_id) from typed fields
instead of scraping text.

Failures are raised as ``ToolError``, so the client receives an
``isError`` result with the message and no structured content: a success
result always satisfies its schema.

Everything in these models except IDs is sender-controlled, so the
responses bound lists the way the prose does and report the full count
alongside (``participant_count``, ``to_count``, ...).
"""

from datetime import datetime
from typing import Literal

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field

from ..lib.sqlite import MessageRecord, ThreadResult
from ..lib.sqlite import Participant as ParticipantRecord

# Entries listed per bounded list (thread participants, senders, one
# message's recipients per role, References) before the rest are counted.
MAX_LISTED = 10


class _Output(BaseModel):
    # ``from`` is a Python keyword: the field is ``from_`` in code and
    # ``from`` in the schema and the payload.
    model_config = ConfigDict(validate_by_name=True)


def tool_result(text: str, output: _Output) -> CallToolResult:
    """The prose as ``content`` plus ``output`` as ``structuredContent``."""
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=output.model_dump(mode="json", by_alias=True),
    )


def clip(value: str, limit: int | None) -> str:
    """``value`` cut at ``limit`` characters with a marker counting the rest."""
    if limit is None or len(value) <= limit:
        return value
    return value[:limit] + f"… [{len(value) - limit:,} more characters]"


class Participant(_Output):
    name: str | None = Field(description="Display name as written; null when the header had none.")
    address: str = Field(description="Canonical (lowercased) email address.")


class ThreadSummary(_Output):
    thread_id: str = Field(
        description="Opaque thread ID; pass it to get_thread, get_evidence, or summarize_thread."
    )
    subject: str
    folder: str
    participants: list[str] = Field(
        description=f"Thread participants, at most {MAX_LISTED}; see participant_count."
    )
    participant_count: int
    date_first: datetime
    date_last: datetime
    message_count: int
    has_attachments: bool
    snippet: str = Field(description="Start of the latest message body; body text only.")


def thread_summary(t: ThreadResult, *, chars: int | None = None) -> ThreadSummary:
    """``t`` as a ``ThreadSummary``; ``chars`` cuts sender-controlled text."""
    return ThreadSummary(
        thread_id=t.thread_id,
        subject=clip(t.subject, chars),
        folder=t.folder,
        participants=[clip(p, chars) for p in t.participants[:MAX_LISTED]],
        participant_count=len(t.participants),
        date_first=t.date_first,
        date_last=t.date_last,
        message_count=len(t.message_ids),
        has_attachments=t.has_attachments,
        snippet=t.snippet,
    )


class MessageHeaders(_Output):
    message_id: str = Field(description="RFC 5322 Message-ID; pass it to get_message.")
    thread_id: str
    subject: str
    sent_at: str = Field(description="Send date (the sender's Date: header) in UTC, ISO 8601.")
    folder: str
    has_attachments: bool
    in_reply_to: str | None
    references: list[str] = Field(description="References; may be shortened, see references_count.")
    references_count: int
    from_: list[Participant] = Field(validation_alias="from", serialization_alias="from")
    from_count: int
    to: list[Participant]
    to_count: int
    cc: list[Participant]
    cc_count: int


def message_headers(
    m: MessageRecord,
    *,
    people: int | None = None,
    refs: int | None = None,
    chars: int | None = None,
) -> MessageHeaders:
    """``m``'s headers, listing at most ``people`` entries per role and
    ``refs`` References and cutting values at ``chars`` (None: in full)."""

    def listed(entries: list[ParticipantRecord], limit: int | None) -> list[Participant]:
        return [
            Participant(
                name=None if p.name is None else clip(p.name, chars),
                address=clip(p.address, chars),
            )
            for p in entries[:limit]
        ]

    return MessageHeaders(
        message_id=m.message_id,
        thread_id=m.thread_id,
        subject=clip(m.subject, chars),
        sent_at=m.sent_at,
        folder=m.folder,
        has_attachments=m.has_attachments,
        in_reply_to=None if m.in_reply_to is None else clip(m.in_reply_to, chars),
        references=[clip(r, chars) for r in m.references[:refs]],
        references_count=len(m.references),
        from_=listed(m.from_, people),
        from_count=len(m.from_),
        to=listed(m.to, people),
        to_count=len(m.to),
        cc=listed(m.cc, people),
        cc_count=len(m.cc),
    )


# --- search tools -------------------------------------------------------


class SearchEmailsOutput(_Output):
    mode: str = Field(description="The search mode used: hybrid, semantic, or keyword.")
    resolved_from_addr: str | None = Field(
        description=(
            "When from_name was given without from_addr: the sender address it "
            "resolved to and filtered by, or null if no contact matched (results "
            "are then empty). Null when from_name was not used."
        )
    )
    results: list[ThreadSummary] = Field(description="Threads, best match first.")


class EvidenceChunk(_Output):
    message_id: str
    chunk_index: int
    source: Literal["body", "attachment"]
    attachment_id: str | None = Field(
        description="Content-hash attachment ID for attachment chunks."
    )
    attachment_filename: str | None
    attachment_mime: str | None
    message_date: str | None
    char_start: int = Field(description="Start offset of the passage in its source text.")
    char_end: int
    text: str
    text_truncated: bool = Field(description="True when text was cut for length.")
    vector_distance: float | None = Field(description="Only with include_scores.")


class EvidenceThread(_Output):
    thread_id: str
    subject: str
    lane_ranks: dict[str, int] | None = Field(
        description=(
            "Only with include_scores on the mailbox-wide path: retrieval lane -> "
            "0-based rank the thread held in that lane before fusion."
        )
    )
    retrieval_score: float | None = Field(description="Only with include_scores.")
    chunks: list[EvidenceChunk]


class EvidenceOutput(_Output):
    chunk_count: int
    threads: list[EvidenceThread] = Field(
        description="Threads in rank order, chunks ranked within."
    )


class AttachmentHit(_Output):
    attachment_id: str = Field(description="Content hash of the attachment payload.")
    filename: str
    content_type: str
    size_bytes: int
    thread_id: str
    message_id: str
    subject: str = Field(description="Parent thread subject.")
    folder: str
    date_last: datetime = Field(description="Parent thread's latest activity.")
    senders: list[str] = Field(description=f"Thread senders, at most {MAX_LISTED}.")
    sender_count: int
    extraction_status: str | None = Field(description="Null when no extraction has run.")
    text_snippet: str


class SearchAttachmentsOutput(_Output):
    results: list[AttachmentHit]


# --- retrieval tools ----------------------------------------------------


class ThreadMessage(MessageHeaders):
    body: str | None = Field(
        description=(
            "Indexed body after quoted-reply stripping, cut at the page's body "
            "limit; null when no body is indexed for the message."
        )
    )
    body_omitted_chars: int = Field(description="Characters of the body not included.")


class GetThreadOutput(_Output):
    thread: ThreadSummary
    total_messages: int
    offset: int
    messages: list[ThreadMessage] = Field(description="This page's messages, oldest first.")
    next_offset: int | None = Field(description="offset for the next page; null on the last page.")
    indexed_thread_text: str | None = Field(
        description=(
            "Accumulated thread text (with quoted replies) or snippet; set only "
            "when no message of the thread has an indexed body."
        )
    )


class GetMessageOutput(_Output):
    message: MessageHeaders
    thread_subject: str
    body: str | None = Field(
        description="Full indexed body after quoted-reply stripping; null when none is indexed."
    )
    indexed_thread_text: str | None = Field(
        description="Parent-thread text or snippet; set only when body is null."
    )


class ListThreadsOutput(_Output):
    folder: str
    offset: int
    threads: list[ThreadSummary] = Field(description="Most recent activity first.")


class FilterUse(_Output):
    filter: str
    value: str | bool = Field(
        description="The value as applied (a canonical address for exact_address)."
    )
    match: Literal["exact_address", "substring", "all_words", "equals", "inclusive_bound"]


class QueryMessagesOutput(_Output):
    filters: list[FilterUse] = Field(description="How each given filter was applied; empty: none.")
    total_matches: int = Field(description="Every matching message, not just this page.")
    returned: int
    offset: int = Field(description="Matches returned by earlier pages.")
    has_more: bool
    next_cursor: str | None = Field(
        description="Pass with the same filters for the next page; null when has_more is false."
    )
    messages: list[MessageHeaders] = Field(description="Newest send date first.")


class Contact(_Output):
    email: str
    names: list[str]
    thread_count: int


class FindContactOutput(_Output):
    contacts: list[Contact] = Field(description="Most threads first.")


class Folder(_Output):
    name: str
    thread_count: int


class ListFoldersOutput(_Output):
    folders: list[Folder]


# --- system tools -------------------------------------------------------


class IndexStatusOutput(_Output):
    total_threads: int
    total_messages: int
    oldest_message: str | None
    newest_message: str | None
    checked_at: datetime


class SyncStatusOutput(_Output):
    mode: Literal["local_index_only"]
    synced_by: Literal["mbsync"] = Field(
        description="mcp-server never contacts Bridge; mbsync refreshes the Maildir."
    )
