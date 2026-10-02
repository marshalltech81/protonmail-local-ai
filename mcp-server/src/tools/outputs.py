"""
Structured output for the search, retrieval, evidence, and status tools,
and for the checked citations of ask_mailbox, summarize_thread and
extract_from_emails, and the experimental brief_issue
and check_conclusion.

Each tool declares one of these models as its ``outputSchema`` (via
``@server.tool(output_schema=Model.model_json_schema())``; FastMCP does
not derive a schema from a ``CallToolResult`` return) and returns it as
``structuredContent`` next to the unchanged prose in ``content``. A client
chains IDs (thread_id -> claimant_id -> attachment_id) from typed fields
instead of scraping text.

Failures are raised as ``ToolError``, so the client receives an
``isError`` result with the message and no structured content: a success
result always satisfies its schema.

Everything in these models except IDs is sender-controlled, so the
responses bound lists and cut long values (``MAX_LISTED``,
``HEADER_CHAR_LIMIT``) and report the full count alongside
(``participant_count``, ``to_count``, ...). Every builder bounds them,
get_message's included (#489).
"""

from datetime import date, datetime
from typing import Any, Literal

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..lib.sqlite import (
    MAX_LISTED_CLAIMANTS,
    REAPED_RECORD_RETENTION_DAYS,
    MessageRecord,
    SourceFile,
    ThreadResult,
)
from ..lib.sqlite import Participant as ParticipantRecord

# Entries listed per bounded list (thread participants, senders, one
# message's recipients per role, References) before the rest are counted.
MAX_LISTED = 10
# Characters of one sender-controlled value (subject, name, address,
# reply header) before it is cut with a marker. IDs are never cut: a
# shortened ID would not chain to the next call.
HEADER_CHAR_LIMIT = 500


class _Output(BaseModel):
    # ``from`` is a Python keyword: the field is ``from_`` in code and
    # ``from`` in the schema and the payload.
    model_config = ConfigDict(validate_by_name=True)


def tool_result(text: str, output: _Output) -> CallToolResult:
    """The prose as ``content`` plus ``output`` as ``structuredContent``."""
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=output.model_dump(mode="json", by_alias=True),
    )


def clip(value: str, limit: int | None) -> str:
    """``value`` cut at ``limit`` characters with a marker counting the rest."""
    if limit is None or len(value) <= limit:
        return value
    return value[:limit] + f"… [{len(value) - limit:,} more characters]"


class Source(_Output):
    """The raw file an answer can be checked against."""

    source_type: Literal["maildir_message"] = Field(
        description="What locator points at; an RFC 5322 message file in the Maildir."
    )
    locator: str = Field(
        description="Path of the raw message file in the Maildir volume, as the "
        "indexer sees it (/maildir/...). Kept current when mbsync renames the file."
    )
    sha256: str | None = Field(
        description="SHA-256 of the file's bytes when it was indexed; the file's "
        "content identity. Null when it was not recorded."
    )
    size_bytes: int | None = Field(description="File size in bytes; null when not recorded.")
    indexed_at: datetime = Field(description="When the indexer last wrote this message.")


def source(f: SourceFile | None) -> Source | None:
    """``f`` as a ``Source``, or ``None`` when no source file is recorded."""
    if f is None:
        return None
    return Source(
        source_type="maildir_message",
        locator=f.locator,
        sha256=f.sha256,
        size_bytes=f.size_bytes,
        indexed_at=datetime.fromisoformat(f.indexed_at),
    )


_SOURCE_FILE_DESCRIPTION = "The raw message file this came from; null when none is recorded."


class Participant(_Output):
    name: str | None = Field(description="Display name as written; null when the header had none.")
    address: str = Field(description="Canonical (lowercased) email address.")


class ThreadSummary(_Output):
    thread_id: str = Field(
        description="Opaque thread ID; pass it to get_thread, get_evidence, or summarize_thread."
    )
    subject: str
    folder: str = Field(
        description="Representative folder: where the message that started the thread "
        "was filed when first indexed. list_threads lists every folder holding one of its messages."
    )
    participants: list[str] = Field(
        description=f"Thread participants, at most {MAX_LISTED}; see participant_count."
    )
    participant_count: int
    date_first: datetime
    date_last: datetime
    message_count: int
    has_attachments: bool
    snippet: str = Field(description="Start of the latest message body; body text only.")


def thread_summary(t: ThreadResult) -> ThreadSummary:
    """``t`` as a ``ThreadSummary``, with sender-controlled text cut."""
    return ThreadSummary(
        thread_id=t.thread_id,
        subject=clip(t.subject, HEADER_CHAR_LIMIT),
        folder=t.folder,
        participants=[clip(p, HEADER_CHAR_LIMIT) for p in t.participants[:MAX_LISTED]],
        participant_count=len(t.participants),
        date_first=t.date_first,
        date_last=t.date_last,
        message_count=len(t.message_ids),
        has_attachments=t.has_attachments,
        snippet=t.snippet,
    )


class MessageHeaders(_Output):
    message_id: str = Field(
        description="RFC 5322 Message-ID. The sender sets it, so two indexed messages "
        "can share one; get_message accepts it while only one message has it."
    )
    claimant_id: str = Field(
        description="This message's own ID: the Message-ID plus '#' and a short hash "
        "of its raw file. Differs from message_id only in suffix; distinguishes "
        "different files that claim the same Message-ID. Pass it to get_message."
    )
    subject: str
    sent_at: str = Field(description="Send date (the sender's Date: header) in UTC, ISO 8601.")
    occurred_at: str | None = Field(
        description=(
            "Delivery date of the message (the date of its topmost Received: header) in UTC, ISO 8601; null when the header is absent (sent mail) or unparseable. "
            "Date filters bound occurred_at, else sent_at."
        )
    )
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
    source_file: Source | None = Field(description=_SOURCE_FILE_DESCRIPTION)


class ListedMessage(MessageHeaders):
    """A message's headers where rows may span threads."""

    thread_id: str


def message_headers(m: MessageRecord) -> MessageHeaders:
    """``m``'s headers: at most ``MAX_LISTED`` entries per role and
    References are listed and values are cut at ``HEADER_CHAR_LIMIT``."""
    people = refs = MAX_LISTED
    chars = HEADER_CHAR_LIMIT

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
        claimant_id=m.claimant_id,
        subject=clip(m.subject, chars),
        sent_at=m.sent_at,
        occurred_at=m.occurred_at,
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
        source_file=source(m.source_file),
    )


def listed_message(m: MessageRecord) -> ListedMessage:
    """``message_headers`` plus the message's thread ID."""
    return ListedMessage(**message_headers(m).model_dump(), thread_id=m.thread_id)


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
    chunk_id: str = Field(
        description="Stable ID of the passage; ask_mailbox citations name it (chunk_id)."
    )
    message_id: str
    claimant_id: str = Field(description="The chunk's message; pass it to get_message.")
    chunk_index: int
    source: Literal["body", "attachment"]
    attachment_id: str | None = Field(
        description="Content-hash attachment ID for attachment chunks."
    )
    attachment_filename: str | None
    attachment_mime: str | None
    sent_at: str | None = Field(
        description="Send date of the passage's message (its Date: header) in UTC, ISO 8601; "
        "null when unknown."
    )
    occurred_at: str | None = Field(
        description=(
            "Delivery date of the passage's message (the date of its topmost Received: header) in UTC, ISO 8601; null when the header is absent (sent mail) or unparseable. "
            "Date filters bound occurred_at, else sent_at."
        )
    )
    char_start: int = Field(description="Start offset of the passage in its source text.")
    char_end: int
    text: str
    text_truncated: bool = Field(description="True when text was cut for length.")
    vector_distance: float | None = Field(description="Only with include_scores.")
    source_file: Source | None = Field(
        description="The raw file of message_id (for an attachment chunk, the message "
        "carrying the attachment); null when none is recorded."
    )


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
    claimant_id: str = Field(
        description="The message carrying the attachment; pass it to get_message."
    )
    subject: str = Field(description="Parent thread subject.")
    folder: str
    date_last: datetime = Field(description="Parent thread's latest activity.")
    sent_at: str | None = Field(
        description="Send date of the message carrying the attachment (its Date: header) "
        "in UTC, ISO 8601. Null when unknown."
    )
    occurred_at: str | None = Field(
        description="Delivery date of the message carrying the attachment (the date of its "
        "topmost Received: header) in UTC, ISO 8601; null when absent or unparseable. "
        "Date filters and the no-query order use occurred_at, else sent_at."
    )
    senders: list[str] = Field(description=f"Thread senders, at most {MAX_LISTED}.")
    sender_count: int
    extraction_status: str | None = Field(description="Null when no extraction has run.")
    text_snippet: str
    source_file: Source | None = Field(
        description="The raw file of the message carrying the attachment; null when "
        "none is recorded."
    )


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


class ReapedMessage(_Output):
    claimant_id: str
    reaped_at: str = Field(
        description="When the local index reaped it (ISO 8601 UTC), not when it was "
        "deleted upstream."
    )


def reaped_source(kind: str, identifier: str, reaped_at: str) -> str:
    """The fixed error for a lookup whose source was reaped: ``kind`` is
    ``Message`` or ``Thread``. Only the caller's own ID and the local
    reap date appear; the index holds nothing else about the source,
    not even why it was reaped or when it was deleted upstream."""
    return (
        f"{kind} reaped from the index on {reaped_at[:10]} (mirror retention): "
        f"{identifier}. Mirror retention reaps a message after a grace window once "
        "it was deleted upstream or its file went missing from the local Maildir; "
        "its content is no longer available."
    )


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
    reaped_messages: list[ReapedMessage] = Field(
        description="Messages of this thread reaped from the index (mirror retention: "
        "deleted upstream or missing from the Maildir), oldest reap first; empty in the "
        "usual case. A record "
        f"lasts {REAPED_RECORD_RETENTION_DAYS} days. At most {MAX_LISTED_CLAIMANTS} "
        "are listed."
    )
    reaped_messages_truncated: bool = Field(
        description="True when more reaped messages exist than reaped_messages lists."
    )


class GetMessageOutput(_Output):
    message: ListedMessage
    other_claimants: list[str] = Field(
        description="Claimant IDs of other indexed messages with the same Message-ID "
        "(different files reusing it), in claimant-ID order; empty in the usual case. "
        f"At most {MAX_LISTED_CLAIMANTS} are listed."
    )
    other_claimants_truncated: bool = Field(
        description="True when more files claim the Message-ID than other_claimants lists."
    )
    thread_subject: str
    body: str | None = Field(
        description="One page of the indexed body after quoted-reply stripping: the "
        "characters from body_offset, at most 20,000; empty when body_offset is the "
        "body's end; null when no body is indexed."
    )
    body_offset: int = Field(description="Body character this page starts at.")
    body_total_chars: int = Field(description="Characters in the whole body; 0 when none.")
    next_offset: int | None = Field(
        description="Pass as offset for the next page of the body; null when this page "
        "reaches the end. Paging from 0 to the end returns the whole body."
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
    messages: list[ListedMessage] = Field(description="Newest send date first.")


class Contact(_Output):
    email: str
    names: list[str] = Field(
        description=f"Display names the contact was written with, at most {MAX_LISTED}; "
        "see name_count."
    )
    name_count: int
    thread_count: int
    organization: str | None = Field(
        description="The contact's organization: its exact address domain, or null for a "
        "free-mail provider. Never inferred from display names."
    )
    authority_class: str = Field(
        description="Source authority from the operator's rules file: counsel, "
        "management, vendor, government, personal, other, or unclassified. "
        "Metadata only; it does not affect ranking. It reflects the claimed From "
        "address, not a verified sender; the authority_class search filters skip "
        "Spam-folder mail."
    )
    authority_rule: str | None = Field(
        description="The rule that assigned authority_class (address:<pattern> or "
        "domain:<pattern>); null when unclassified."
    )


class FindContactOutput(_Output):
    contacts: list[Contact] = Field(description="Most threads first.")


class Folder(_Output):
    name: str
    thread_count: int = Field(
        description="Threads with at least one message in this folder; "
        "a thread spanning folders counts in each."
    )


class ListFoldersOutput(_Output):
    folders: list[Folder]


# --- system tools -------------------------------------------------------


class QueueCounts(_Output):
    pending: int = Field(description="Messages found in the Maildir, not yet indexed.")
    retrying: int = Field(description="Messages that failed to index and will be retried.")
    dead: int = Field(
        description="Messages that failed permanently and are incompletely indexed: "
        "missing from search, or found only by keyword, until an operator requeues them."
    )


class MailboxStatusOutput(_Output):
    current: bool = Field(
        description="True only when mail synced from Proton recently, the indexer is "
        "running, and no message is waiting to be indexed. Mail that reached Proton "
        "after last_sync_at is not searchable either way."
    )
    not_current_reasons: list[str] = Field(description="Why current is false; empty when true.")
    last_sync_at: datetime | None = Field(
        description="When mbsync last completed a successful sync from Proton."
    )
    sync_interval_secs: int | None = Field(description="How often mbsync syncs.")
    indexer_last_seen_at: datetime | None = Field(
        description="When the indexer last reported that it was running."
    )
    queue: QueueCounts
    total_threads: int
    total_messages: int
    oldest_message: str | None
    newest_message: str | None
    conflicting_message_ids: int = Field(
        description="How many Message-IDs are claimed by more than one indexed file. "
        "get_message on such a Message-ID lists its claimant IDs; pass one to read "
        "that file."
    )
    extra_claimant_files: int = Field(
        description="Indexed files beyond the first claimant of each conflicting Message-ID."
    )
    checked_at: datetime


# --- intelligence tools -------------------------------------------------


class Citation(_Output):
    label: str = Field(description="The evidence label as cited in the answer, e.g. E3.")
    chunk_id: str | None = Field(
        description="The cited passage; get_evidence(query, thread_id) returns it under this "
        "ID. Null when the passage was the thread's indexed text (source thread)."
    )
    claimant_id: str | None = Field(
        description="The passage's message; pass it to get_message. Null for source thread."
    )
    message_id: str | None
    thread_id: str
    sender: str | None = Field(
        description="That message's sender, cut for length; null when none is recorded."
    )
    sent_at: str | None = Field(
        description="That message's own sent date (not the thread's); null when unknown."
    )
    occurred_at: str | None = Field(
        description="That message's own delivery date (its topmost Received: header); "
        "null when unknown."
    )
    source: Literal["body", "attachment", "thread"]
    attachment_id: str | None
    attachment_filename: str | None
    char_start: int | None
    char_end: int | None = Field(
        description="End offset of the part of the passage the model was shown."
    )


class CitationProblem(_Output):
    kind: Literal[
        "unknown_labels",
        "no_citations",
        "uncited_statements",
        "unmatched_quotes",
        "misattributed_quotes",
    ] = Field(
        description="unknown_labels: the answer cites labels no supplied passage has. "
        "no_citations: the answer cites nothing and does not say the evidence lacks an answer. "
        "uncited_statements: statements that cite no supplied passage and are not marked "
        "[unsupported] or [uncertain]. unmatched_quotes: quotes found in no supplied passage. "
        "misattributed_quotes: quotes found in a supplied passage other than the ones cited."
    )
    labels: list[str] = Field(
        description="The unknown labels; for misattributed_quotes, the labels of the passages "
        "the quotes were found in. Empty otherwise."
    )
    statements: list[int] = Field(
        default=[], description="uncited_statements: 0-based indexes into statements."
    )
    quotes: list[int] = Field(
        default=[], description="The quote problems: 0-based indexes into quotes."
    )


class AnswerStatement(_Output):
    text: str = Field(description="One statement of the answer, with its inline labels.")
    labels: list[str] = Field(description="The supplied passages it cites, in cited order.")
    status: Literal["cited", "unsupported", "uncertain", "uncited", "invalid", "not_checked"] = (
        Field(
            description="cited: it cites a supplied passage. unsupported / uncertain: it is "
            "marked so and cites none. uncited: it cites nothing and is not marked. invalid: it "
            "cites only unknown labels. not_checked: a heading, a list introduction, a "
            "fragment under three words, or a statement of a not-found answer."
        )
    )


class QuoteCheck(_Output):
    text: str = Field(description="The quoted words as the answer gives them, cut for length.")
    statement: int = Field(description="0-based index of the statement holding the quote.")
    status: Literal["verified", "misattributed", "unmatched", "uncited", "not_checked"] = Field(
        description="verified: found in the indexed text shown for a passage its statement "
        "cites. misattributed: found only in other supplied passages. unmatched: found in no "
        "supplied passage. uncited: its statement cites no supplied passage. not_checked: over "
        "the per-answer quote cap or the quote length cap. The comparison ignores whitespace "
        "and quote-mark style and allows an ellipsis to skip text; indexed text is extracted "
        "and normalized, so a verified quote is not proof of the raw message bytes."
    )
    found_in: list[str] = Field(
        description="Labels of the supplied passages the quote was found in."
    )


class AskMailboxOutput(_Output):
    answer: str = Field(description="The model's answer, with inline labels such as [E1].")
    citations: list[Citation] = Field(
        description="Each cited label that names a supplied passage, in first-cited order."
    )
    statements: list[AnswerStatement] = Field(
        default=[], description="The answer split into statements, each with its labels."
    )
    quotes: list[QuoteCheck] = Field(
        default=[],
        description="Each quotation of three or more words, checked against the "
        "passages its statement cites, and each quotation over 1,000 characters (not_checked).",
    )
    citation_problems: list[CitationProblem] = Field(
        description="Empty when the citation check passed. Labels and quotes are checked: a "
        "valid label or a verified quote does not prove the passage supports the claim."
    )
    repair_attempted: bool = Field(
        description="True when the first answer failed the check and the model was asked once more."
    )
    threads: list[ThreadSummary] = Field(description="The threads searched, best match first.")


SummaryStyle = Literal["brief", "detailed", "action-items", "timeline"]


class SummarizeThreadOutput(_Output):
    summary: str = Field(description="The model's summary, with inline labels such as [E1].")
    style: SummaryStyle = Field(
        description="The style used; an unknown style is summarized as brief."
    )
    thread: ThreadSummary = Field(description="The thread summarized.")
    citations: list[Citation] = Field(
        description="Each cited label that names a supplied passage, in first-cited order. "
        "E1 is the thread's indexed text (source thread); the others are its recent messages."
    )
    statements: list[AnswerStatement] = Field(
        description="The summary split into statements (sentences, list items and lines), "
        "each with its labels."
    )
    quotes: list[QuoteCheck] = Field(
        description="Each quotation of three or more words, checked against the passages "
        "its statement cites, and each quotation over 1,000 characters (not_checked)."
    )
    citation_problems: list[CitationProblem] = Field(
        description="Empty when the citation check passed. Labels and quotes are checked: a "
        "valid label or a verified quote does not prove the passage supports the claim."
    )
    repair_attempted: bool = Field(
        description="True when the first summary failed the check and the model was asked "
        "once more."
    )


class ExtractedField(_Output):
    record: int = Field(description="0-based index of the record in records.")
    field: str = Field(description="The field's name in the record.")
    labels: list[str] = Field(
        description="The supplied passages its _evidence entry cites, in cited order."
    )
    status: Literal["cited", "uncited", "invalid"] = Field(
        description="cited: it cites a passage supplied for its thread. uncited: it cites "
        "none. invalid: it cites only unknown labels."
    )
    value_check: Literal["verified", "misattributed", "unmatched", "uncited", "not_checked"] = (
        Field(
            description="For a string value, whether its words occur in the indexed text shown "
            "for a cited passage (verified), only in another passage of its thread "
            "(misattributed) or in none (unmatched; often a normalized value such as a "
            "reformatted date). uncited: the field cites no supplied passage. not_checked: not "
            "a string, over 1,000 characters, or over the 20 values searched per thread. The "
            "comparison is the quote check's: whitespace and quote-mark style are ignored, "
            "case is not, and indexed text is extracted and normalized, so a verified value is "
            "not proof of the raw message bytes."
        )
    )
    found_in: list[str] = Field(description="Labels of the passages the value was found in.")


class ExtractCitationProblem(_Output):
    record: int = Field(description="0-based index of the record in records.")
    kind: Literal["unknown_labels", "uncited_fields", "misattributed_values"] = Field(
        description="unknown_labels: the record cites labels no passage supplied for its "
        "thread has. uncited_fields: fields with a value whose _evidence entry cites no "
        "label. misattributed_values: string values found only in a passage their field "
        "does not cite."
    )
    labels: list[str] = Field(
        description="The unknown labels; for misattributed_values, the labels of the passages "
        "the values were found in. Empty otherwise."
    )
    fields: list[str] = Field(
        description="The fields concerned (uncited_fields, misattributed_values); empty for "
        "unknown_labels."
    )


class ExtractFromEmailsOutput(_Output):
    records: list[dict[str, Any]] = Field(
        description="The extracted records, as in the first content item. Each carries "
        "_source_thread (thread subject), _date (the thread's last date) and _evidence (each "
        "field with a value mapped to the valid labels of the passages it was taken from)."
    )
    citations: list[Citation] = Field(
        description="Each valid label any record cites, in first-cited order. Labels are "
        "numbered across the whole call, so one label names one passage."
    )
    fields: list[ExtractedField] = Field(
        description="Each field with a value, per record: its labels and checks."
    )
    citation_problems: list[ExtractCitationProblem] = Field(
        description="Empty when every field cites a supplied passage. Records with problems "
        "are kept. Labels and words are checked: a valid label does not prove the passage "
        "states the value."
    )
    notice: str | None = Field(
        description="The incomplete-extraction and evidence note in content, if any: "
        "counts of threads that could not be extracted or whose passages were cut."
    )
    threads: list[ThreadSummary] = Field(description="The threads searched, best match first.")


# --- experimental tools -------------------------------------------------
#
# brief_issue (MCP_EXPERIMENTAL_TOOLS=true only). The Brief* models are
# both the JSON shape the model is asked for (a reply missing a section
# or with a wrong type does not parse as a brief) and the brief in the
# output. Every string in them is model output.

_LABELS_DESCRIPTION = "Evidence labels (E1, ...) the model cited for this entry."


class BriefEvent(_Output):
    # ASCII digits only and a real calendar date: the chronology sorts
    # on this string, so any other value fails validation and gets the
    # repair call.
    date: str | None = Field(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
        description="YYYY-MM-DD as the model gave it; null when undated.",
    )
    date_source: Literal["sent", "mentioned", "unknown"] = Field(
        description="sent: the cited message's own sent date. mentioned: a date the "
        "passage states for the event. unknown: neither."
    )
    actor: str
    event: str
    labels: list[str] = Field(description=_LABELS_DESCRIPTION)

    @field_validator("date")
    @classmethod
    def _calendar_date(cls, value: str | None) -> str | None:
        """Reject a well-shaped impossible date such as 2023-02-29; the
        pattern has already fixed the shape, so this parses ten ASCII
        characters. The string is kept as given."""
        if value is not None:
            date.fromisoformat(value)
        return value


class BriefPosition(_Output):
    actor: str
    position: str
    labels: list[str] = Field(description=_LABELS_DESCRIPTION)


class BriefDecision(_Output):
    decision: str
    labels: list[str] = Field(description=_LABELS_DESCRIPTION)


class BriefQuestion(_Output):
    question: str
    labels: list[str] = Field(description=_LABELS_DESCRIPTION)


class BriefConflict(_Output):
    description: str
    labels: list[str] = Field(
        description="Labels of the passages that disagree; at least two are expected."
    )


class Brief(_Output):
    chronology: list[BriefEvent] = Field(
        description="Events sorted oldest first by date by the server; undated entries "
        "last, in the model's order."
    )
    positions: list[BriefPosition] = Field(description="Actors and the positions they state.")
    decisions: list[BriefDecision]
    open_questions: list[BriefQuestion]
    conflicts: list[BriefConflict] = Field(
        description="Passages that disagree where none states which is right."
    )
    insufficient_evidence: bool = Field(
        description="True when the model found nothing about the topic in the passages; "
        "true with any entries is reported as insufficient_but_populated, false with "
        "none as empty_but_sufficient."
    )


BriefSection = Literal["chronology", "positions", "decisions", "open_questions", "conflicts"]


class BriefCitationProblem(_Output):
    section: BriefSection | Literal["brief"] = Field(
        description="The entry's section, or brief for a problem with the brief as a whole."
    )
    item: int = Field(
        description="0-based index of the entry within its section; 0 for section brief."
    )
    kind: Literal[
        "unknown_labels",
        "no_citations",
        "too_few_labels",
        "insufficient_but_populated",
        "empty_but_sufficient",
        "unmatched_quotes",
        "misattributed_quotes",
    ] = Field(
        description="unknown_labels: the entry cites labels no supplied passage has. "
        "no_citations: it cites none. too_few_labels: a conflict cites fewer than two "
        "supplied passages. insufficient_but_populated (section brief): "
        "insufficient_evidence is true but a section has entries. empty_but_sufficient "
        "(section brief): insufficient_evidence is false but every section is empty. "
        "unmatched_quotes: the entry quotes words found in no supplied passage. "
        "misattributed_quotes: it quotes words found only in passages it does not cite."
    )
    labels: list[str] = Field(
        description="The unknown labels; for misattributed_quotes, the passages the quotes "
        "were found in. Empty for the other kinds."
    )


class ReplyQuoteCheck(_Output):
    text: str = Field(description="The quoted words as the reply gives them, cut for length.")
    status: Literal["verified", "misattributed", "unmatched", "uncited", "not_checked"] = Field(
        description="verified: found in the indexed text shown for a passage its entry "
        "cites. misattributed: found only in other supplied passages. unmatched: found in no "
        "supplied passage. uncited: its entry cites no supplied passage. not_checked: over "
        "the per-reply quote cap or the quote length cap. Checked as ask_mailbox's quotes "
        "are: whitespace and quote-mark style are ignored and an ellipsis may skip text; "
        "indexed text is extracted and normalized, so a verified quote is not proof of the "
        "raw message bytes, nor that the passage supports the entry."
    )
    found_in: list[str] = Field(
        description="Labels of the supplied passages the quote was found in."
    )


class BriefQuoteCheck(ReplyQuoteCheck):
    section: BriefSection = Field(description="The section of the entry holding the quote.")
    item: int = Field(description="0-based index of that entry within its section.")


class BriefIssueOutput(_Output):
    experimental: Literal[True] = Field(
        description="Always true: brief_issue is experimental and this format may change."
    )
    status: Literal["ok", "invalid_json", "truncated"] = Field(
        description="ok: brief holds the parsed brief. invalid_json: the reply was not "
        "the brief JSON even after one repair; raw_text holds it. truncated: the reply "
        "was cut off at INFERENCE_MAX_TOKENS; raw_text holds the part produced."
    )
    brief: Brief | None = Field(description="The parsed brief; null unless status is ok.")
    raw_text: str | None = Field(
        description="The model's unparsed reply when status is not ok; null otherwise."
    )
    as_of: str | None = Field(
        description="Latest sent date (YYYY-MM-DD) among the passages supplied to the "
        "model; the brief describes the evidence up to then. Null when none is dated."
    )
    citations: list[Citation] = Field(
        description="Each cited label that names a supplied passage, in first-cited order."
    )
    quotes: list[BriefQuoteCheck] = Field(
        default=[],
        description="Each quotation of three or more words in an entry, checked against "
        "the passages the entry cites, and each quotation over 1,000 characters "
        "(not_checked); in section order.",
    )
    citation_problems: list[BriefCitationProblem] = Field(
        description="Empty when every entry passed the check. Labels and quoted words are "
        "checked: a valid label or a verified quote does not prove the passage supports "
        "the entry."
    )
    repair_attempted: bool = Field(
        description="True when the first reply failed the check and the model was asked once more."
    )
    threads: list[ThreadSummary] = Field(description="The threads searched, best match first.")


# check_conclusion (MCP_EXPERIMENTAL_TOOLS=true only). ConclusionFinding
# and ConclusionCheck are the JSON shape the model is asked for; the
# output adds the server's attribution and excerpt of each cited passage.

# Findings a reply may hold. A reply with more is not a check (it gets
# the one repair call), which bounds the sources attached to findings.
MAX_CONCLUSION_FINDINGS = 20


class ConclusionFinding(_Output):
    # A plain string, so one bad relation is a problem of that finding
    # rather than a reply that does not parse at all.
    relation: str = Field(
        description="supports, contradicts, qualifies or supersedes, as the model gave it; "
        "any other value is reported as an invalid_relation problem."
    )
    explanation: str
    labels: list[str] = Field(description=_LABELS_DESCRIPTION)


class ConclusionCheck(_Output):
    verdict_summary: str
    findings: list[ConclusionFinding] = Field(max_length=MAX_CONCLUSION_FINDINGS)
    insufficient_evidence: bool = Field(
        description="True when the model found nothing about the conclusion in the "
        "passages, with no findings. True with findings is reported as "
        "insufficient_but_populated, false with none as no_findings_but_sufficient."
    )


class FindingSource(Citation):
    excerpt: str = Field(
        description="The start of the cited passage, verbatim from the indexed text the "
        "model was shown, cut for length. Supplied by the server, not the model."
    )


class CheckedFinding(ConclusionFinding):
    sources: list[FindingSource] = Field(
        description="Each cited label that names a supplied passage: its message, sender, "
        "sent and delivery dates and excerpt. Unknown labels have no source."
    )


class ConclusionCitationProblem(_Output):
    item: int | None = Field(
        description="0-based index of the finding; null for a problem of the check as a whole."
    )
    kind: Literal[
        "unknown_labels",
        "no_citations",
        "invalid_relation",
        "insufficient_but_populated",
        "no_findings_but_sufficient",
        "unmatched_quotes",
        "misattributed_quotes",
    ] = Field(
        description="unknown_labels: the finding cites labels no supplied passage has. "
        "no_citations: it cites none. invalid_relation: its relation is not supports, "
        "contradicts, qualifies or supersedes. insufficient_but_populated (item null): "
        "insufficient_evidence is true but there are findings. no_findings_but_sufficient "
        "(item null): there are no findings but insufficient_evidence is false. "
        "unmatched_quotes: the finding (item null: the verdict summary) quotes words found "
        "in no supplied passage. misattributed_quotes: it quotes words found only in "
        "passages it does not cite."
    )
    labels: list[str] = Field(
        description="The unknown labels; for misattributed_quotes, the passages the quotes "
        "were found in. Empty for the other kinds."
    )


class ConclusionQuoteCheck(ReplyQuoteCheck):
    item: int | None = Field(
        description="0-based index of the finding whose explanation holds the quote; null "
        "for the verdict summary, whose quotes are checked against the passages the "
        "findings cite."
    )


class CheckConclusionOutput(_Output):
    experimental: Literal[True] = Field(
        description="Always true: check_conclusion is experimental and this format may change."
    )
    status: Literal["ok", "invalid_json", "truncated"] = Field(
        description="ok: the reply parsed as a check. invalid_json: it did not, even after "
        "one repair; raw_text holds it. truncated: the reply was cut off at "
        "INFERENCE_MAX_TOKENS; raw_text holds the part produced."
    )
    verdict_summary: str | None = Field(
        description="The model's short overall verdict, cut for length; null unless ok."
    )
    findings: list[CheckedFinding] = Field(
        description="The findings with their sources; empty unless status is ok."
    )
    insufficient_evidence: bool | None = Field(
        description="The model's abstention flag; null unless status is ok."
    )
    raw_text: str | None = Field(
        description="The model's unparsed reply when status is not ok; null otherwise."
    )
    as_of: str | None = Field(
        description="Latest sent date (YYYY-MM-DD) among the passages supplied to the "
        "model. Null when none is dated."
    )
    quotes: list[ConclusionQuoteCheck] = Field(
        default=[],
        description="Each quotation of three or more words in the verdict summary and the "
        "findings' explanations, checked against the passages cited, and each quotation "
        "over 1,000 characters (not_checked); verdict first, then findings in order.",
    )
    citation_problems: list[ConclusionCitationProblem] = Field(
        description="Empty when every finding passed the check. Labels and quoted words are "
        "checked: a valid label or a verified quote does not prove the passage supports "
        "the finding."
    )
    repair_attempted: bool = Field(
        description="True when the first reply failed the check and the model was asked once more."
    )
    threads: list[ThreadSummary] = Field(description="The threads searched, best match first.")
