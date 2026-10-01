"""
Structured output for the search, retrieval, evidence, and status tools,
and for ask_mailbox's checked citations and the experimental brief_issue
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
(``participant_count``, ``to_count``, ...). The builders bound by
default; only get_message asks for full headers.
"""

from datetime import datetime
from typing import Literal

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field

from ..lib.sqlite import MessageRecord, SourceFile, ThreadResult
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


def message_headers(m: MessageRecord, *, full: bool = False) -> MessageHeaders:
    """``m``'s headers. Unless ``full``, at most ``MAX_LISTED`` entries per
    role and References are listed and values are cut at
    ``HEADER_CHAR_LIMIT``."""
    people = refs = None if full else MAX_LISTED
    chars = None if full else HEADER_CHAR_LIMIT

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


def listed_message(m: MessageRecord, *, full: bool = False) -> ListedMessage:
    """``message_headers`` plus the message's thread ID."""
    return ListedMessage(**message_headers(m, full=full).model_dump(), thread_id=m.thread_id)


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
    message_date: str | None
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
    message: ListedMessage
    other_claimants: list[str] = Field(
        description="Claimant IDs of other indexed messages with the same Message-ID "
        "(different files reusing it); empty in the usual case."
    )
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
    source: Literal["body", "attachment", "thread"]
    attachment_id: str | None
    attachment_filename: str | None
    char_start: int | None
    char_end: int | None = Field(
        description="End offset of the part of the passage the model was shown."
    )


class CitationProblem(_Output):
    kind: Literal["unknown_labels", "no_citations"] = Field(
        description="unknown_labels: the answer cites labels no supplied passage has. "
        "no_citations: the answer cites nothing and does not say the evidence lacks an answer."
    )
    labels: list[str] = Field(description="The unknown labels; empty for no_citations.")


class AskMailboxOutput(_Output):
    answer: str = Field(description="The model's answer, with inline labels such as [E1].")
    citations: list[Citation] = Field(
        description="Each cited label that names a supplied passage, in first-cited order."
    )
    citation_problems: list[CitationProblem] = Field(
        description="Empty when the citation check passed. It checks labels only: a "
        "valid label does not prove the passage supports the claim."
    )
    repair_attempted: bool = Field(
        description="True when the first answer failed the check and the model was asked once more."
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
    date: str | None = Field(description="YYYY-MM-DD as the model gave it; null when undated.")
    date_source: Literal["sent", "mentioned", "unknown"] = Field(
        description="sent: the cited message's own sent date. mentioned: a date the "
        "passage states for the event. unknown: neither."
    )
    actor: str
    event: str
    labels: list[str] = Field(description=_LABELS_DESCRIPTION)


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
        "true with any entries is reported as insufficient_but_populated."
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
        "unknown_labels", "no_citations", "too_few_labels", "insufficient_but_populated"
    ] = Field(
        description="unknown_labels: the entry cites labels no supplied passage has. "
        "no_citations: it cites none. too_few_labels: a conflict cites fewer than two "
        "supplied passages. insufficient_but_populated (section brief): "
        "insufficient_evidence is true but a section has entries."
    )
    labels: list[str] = Field(description="The unknown labels; empty for the other kinds.")


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
    citation_problems: list[BriefCitationProblem] = Field(
        description="Empty when every entry passed the label check. Labels only: a valid "
        "label does not prove the passage supports the entry, and quotes are not verified."
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
        "sent date and excerpt. Unknown labels have no source."
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
    ] = Field(
        description="unknown_labels: the finding cites labels no supplied passage has. "
        "no_citations: it cites none. invalid_relation: its relation is not supports, "
        "contradicts, qualifies or supersedes. insufficient_but_populated (item null): "
        "insufficient_evidence is true but there are findings. no_findings_but_sufficient "
        "(item null): there are no findings but insufficient_evidence is false."
    )
    labels: list[str] = Field(description="The unknown labels; empty for the other kinds.")


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
    citation_problems: list[ConclusionCitationProblem] = Field(
        description="Empty when every finding passed the check. Labels only: a valid label "
        "does not prove the passage supports the finding."
    )
    repair_attempted: bool = Field(
        description="True when the first reply failed the check and the model was asked once more."
    )
    threads: list[ThreadSummary] = Field(description="The threads searched, best match first.")
