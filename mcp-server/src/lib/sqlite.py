"""
SQLite query layer for the MCP server.
Read-only access to the index built by the indexer service.
Supports BM25 keyword search, vector similarity search, and hybrid fusion.
"""

import base64
import hashlib
import json
import logging
import math
import re
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from email.utils import parseaddr
from itertools import groupby
from pathlib import Path
from urllib.parse import quote

import sqlite_vec

from . import timings
from .reranker import RerankerBackend

log = logging.getLogger("mcp.sqlite")


class InvalidFilterError(ValueError):
    """A filter or query argument the caller supplied was rejected (a
    date, a cursor, the ``query_messages`` text), or the two date bounds
    name an empty interval.

    The message may quote the rejected value so the caller learns why,
    which means it must never be logged: tool handlers catch this and log
    only which field failed.
    """

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field_name = field_name


# Source-authority classes the indexer assigns from the operator rules
# file (``indexer/src/entities.py`` ``AUTHORITY_CLASSES``) plus
# ``unclassified`` for senders no rule matched.
AUTHORITY_CLASSES = (
    "counsel",
    "management",
    "vendor",
    "government",
    "personal",
    "other",
    "unclassified",
)


def normalize_authority_class(value: str | None) -> str | None:
    """The ``authority_class`` filter to apply: ``None`` for a missing or
    blank value (ignored, like every blank string filter), otherwise the
    stripped value, which must be one of ``AUTHORITY_CLASSES``."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    if value not in AUTHORITY_CLASSES:
        raise InvalidFilterError(
            "authority_class",
            f"authority_class must be one of {', '.join(AUTHORITY_CLASSES)}",
        )
    return value


# Claimants (per-message keys) whose From sender's person entity carries
# the bound class.
# Driven from ``idx_entities_authority`` into the participant address
# index.
_SENDER_CLASS_MESSAGES = (
    "SELECT p.claimant_id FROM entities e "
    "JOIN message_participants p ON p.address = e.canonical_key AND p.role = 'from' "
    "WHERE e.kind = 'person' AND e.authority_class = ?"
)


class VectorLanesUnavailableError(RuntimeError):
    """Neither vector lane could be queried, so semantic search has no
    retrieval path left. Carries fixed text only: the underlying SQLite
    error is logged by the lane helper, never quoted here."""

    def __init__(self) -> None:
        super().__init__(
            "semantic search unavailable: no vector index could be queried. "
            "Check the indexer and mcp-server logs, or use mode='keyword'."
        )


def canonical_addr(value: str) -> str:
    """Extract the bare lowercased email from a display string.

    Mirrors ``indexer.threader.canonical_addr`` (the two services are separate
    ``uv`` projects, so the helper is duplicated intentionally until a shared
    package exists). Returns ``""`` when no ``@``-bearing address is
    recoverable, so a callee can distinguish "no email found" from a
    successful normalization.
    """
    if not value:
        return ""
    try:
        _, addr = parseaddr(value)
    except Exception:
        # parseaddr recurses on nested comments. Stored senders and
        # participants are indexed mail, so one hostile entry must count
        # as "no address", not abort a search whose filter is valid.
        return ""
    addr = addr.strip().lower()
    if "@" not in addr:
        return ""
    return addr


# When any filter (folder, sender, date range, attachment flag) is active,
# the filtered result set is a subset of the raw ranked candidates. Pulling
# only ``limit * 2`` raw candidates means a filter can wipe out the page —
# valid matches ranked deeper are never considered. Oversample when filters
# are present to preserve recall.
_UNFILTERED_OVERSAMPLE = 2
_FILTERED_OVERSAMPLE = 4

# Folders whose mail stays synced and indexed but is left out of
# mailbox-wide retrieval unless the caller names them (#441). Under
# mirror retention a deleted message lives on as its Trash copy until
# it is purged from Trash. Matched exactly, as ``folders`` filters are.
# Thread searches leave out a thread only when every message of it is
# in one of these folders (the per-message membership the ``folders``
# filter uses); message and attachment lookups leave out the messages
# filed there. Lookups of one named thread or message are unaffected.
DEFAULT_EXCLUDED_FOLDERS = ("Trash",)

# Oversample factor for the chunk and attachment FTS lanes, where one
# thread can legitimately own many matching rows (a long thread, a
# popular term). Without enough oversample, those threads absorb every
# top-N row and other threads never enter the lane. The value is a
# deliberate over-correction of the prior 3× — empirically a ~10×
# oversample lets ``_best_per_thread`` still surface enough threads
# even on dense matches.
_CHUNK_LANE_OVERSAMPLE = 10

# Evidence chunks per thread that ``ask_mailbox`` puts in its prompt and
# ``get_evidence`` returns from the same retrieval, so the audit tool
# shows the passages the model saw (#285). Chunks are per-message, so a
# thread's short replies often fit several to the prompt's shared
# budget. It costs no extra query: the per-thread chunk scan already
# reads every chunk of each surfaced thread.
PROMPT_EVIDENCE_CHUNKS_PER_THREAD = 6

# sqlite-vec's largest accepted KNN ``k``; a larger one is an error, which
# the vector lanes catch as "lane unavailable". The fetch windows multiply
# RERANK_CANDIDATES by filter and chunk oversampling (200 x 4 x 10 = 8000),
# so both lanes clamp here rather than silently lose the lane.
_SQLITE_VEC_MAX_K = 4096

# Characters of a candidate's subject sent to the reranker; matches the
# tools' ``HEADER_CHAR_LIMIT``.
_RERANK_SUBJECT_CHARS = 500

# Upper bound on the ``?`` placeholders bound into one ``IN (...)``
# lookup. The connection's ``SQLITE_LIMIT_VARIABLE_NUMBER`` depends on
# the SQLite build, so lookups over an unbounded ID list batch under it
# (same bound as the indexer's).
_IN_CLAUSE_BATCH_SIZE = 500


def _addr_matches(haystack: list[str], query_lower: str) -> bool:
    """True if ``query_lower`` matches an address string in ``haystack``.

    Match mode depends on the shape of the query:

    * A full address (``bob@example.com``) is compared by canonical
      equality so that case variation in the stored display string
      (``Bob@Example.com``, ``Bob Smith <bob@example.com>``) still matches.
    * A bare name (``bob``) or domain fragment (``@example.com``,
      ``example.com``) keeps substring behavior against the display
      string, both sides casefolded (Unicode caseless: ``STRASSE``
      matches ``Straße``), since those shapes cannot canonicalize.
    """
    canonical_query = canonical_addr(query_lower)
    # A canonicalizable full address requires a non-empty local part.
    # ``canonical_addr`` still returns the input for a bare domain like
    # ``@example.com`` because the ``@`` check passes, but equality against
    # ``bob@example.com`` would then miss. Route the domain-only shape through
    # the substring fallback so ``"@example.com"`` still behaves like a
    # domain filter.
    if canonical_query and not canonical_query.startswith("@"):
        return any(canonical_addr(s) == canonical_query for s in haystack)
    query_folded = query_lower.casefold()
    return any(query_folded in s.casefold() for s in haystack)


def _matches_sender(result, from_addr_lower: str) -> bool:
    """True if ``from_addr_lower`` matches one of the thread's senders.

    Senders is the list of ``From`` addresses recorded on the thread.
    See ``_addr_matches`` for the full-address vs. substring match rules.
    """
    return _addr_matches(result.senders, from_addr_lower)


def _matches_participant(result, participant_lower: str) -> bool:
    """True if ``participant_lower`` matches anyone on the thread.

    Participants is the broader From + To + Cc set, so this surfaces
    threads a person was on in *any* role — distinct from
    ``_matches_sender``, which is From-line only. See ``_addr_matches``
    for the full-address vs. substring match rules.
    """
    return _addr_matches(result.participants, participant_lower)


def _is_fts_query_token_char(ch: str) -> bool:
    """Whether ``ch`` belongs inside a sanitized query token: a regex word
    character (alphanumeric or ``_``), one of ``@ . -``, or a combining
    mark. The regex word class excludes marks, but unicode61 keeps them in
    the word, so splitting there turned a decomposed ``résumé`` (``e``
    followed by U+0301) into the unrelated terms ``re`` and ``sume``."""
    return ch.isalnum() or ch in "_@.-" or unicodedata.category(ch).startswith("M")


def _sanitize_fts_query(query: str) -> str:
    """Build a safe FTS5 MATCH expression from arbitrary user input.

    FTS5's MATCH grammar treats punctuation, hyphens, quotes, colons, and
    trailing operators as syntax, so raw human search strings often fail to
    parse (``"Who's the landlord?"``, ``alice@example.com`` with unbalanced
    quotes, etc.). The failure mode of the previous implementation was to
    catch the error and silently return no results, which looks to the user
    like their query "doesn't match anything."

    The sanitizer extracts word-like tokens (keeping ``@ . -`` so email
    addresses and hostnames survive, and combining marks so a decomposed
    accent stays in its word), quotes each one as an FTS phrase, and
    joins with ``OR`` so any-term match is preserved — the typical
    search-box expectation. One pass over the characters, so the work is
    linear in the query length.
    """
    tokens = [
        "".join(chars)
        for is_token, chars in groupby(query or "", key=_is_fts_query_token_char)
        if is_token
    ]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


@dataclass
class SourceFile:
    """The raw Maildir file a message was indexed from, as the indexer
    recorded it in ``messages``: path, SHA-256 and size of the file's
    bytes (``None`` when not captured), and when the record was written."""

    locator: str
    sha256: str | None
    size_bytes: int | None
    indexed_at: str


# Selected by every query that reports a message's source file; ``m`` is
# the ``messages`` row (LEFT JOINed where the row may be missing).
# Chunk and attachment rows carry only the claimant ID (#217); their bare
# Message-ID comes from that same ``messages`` row, falling back to the
# claimant ID itself (which begins with the Message-ID) when it is missing.
_SOURCE_COLUMNS = (
    "m.filepath AS source_locator, m.content_hash AS source_sha256, "
    "m.size_bytes AS source_size_bytes, m.indexed_at AS source_indexed_at"
)


# Characters of a sender's display name and of its address fetched with
# each evidence chunk. Both are sender-controlled and repeat on every
# chunk row of the message, so they are cut in SQL; the cap is above
# every clip applied to them later (``HEADER_CHAR_LIMIT``).
_SENDER_FETCH_CHARS = 1000


def _row_to_source(r) -> SourceFile | None:
    """The ``_SOURCE_COLUMNS`` of ``r``; ``None`` when the row has none or
    no ``messages`` row joined."""
    if "source_locator" not in r.keys() or r["source_locator"] is None:
        return None
    size = r["source_size_bytes"]
    return SourceFile(
        locator=r["source_locator"],
        sha256=r["source_sha256"],
        size_bytes=None if size is None else int(size),
        indexed_at=r["source_indexed_at"],
    )


@dataclass
class ChunkResult:
    """One per-message chunk hit, used as precise evidence for a thread.

    The indexer stores per-message paragraph-packed chunks alongside
    the coarse thread row. A chunk hit carries its parent ``thread_id``
    so the hybrid-search RRF can lift it into thread ranking, and its
    ``text`` + ``char_start`` / ``char_end`` so intelligence tools can
    cite the exact passage they used.

    ``attachment_id`` / ``attachment_filename`` / ``attachment_mime`` are
    populated for chunks derived from an attachment's extracted text and
    left ``None`` for body chunks. The tools that surface attachment
    text (``ask_mailbox``, ``get_evidence``, ``extract_from_emails``)
    depend on these fields reaching the caller or LLM context — without
    filename/MIME provenance the text is opaque and the source
    attachment cannot be cited.

    ``message_date`` is the source message's ``Date:`` header, carried
    so ``get_evidence`` can show *when* a cited passage arrived. Left
    ``None`` only for query paths that do not SELECT it.

    ``source_file`` is the raw file of the chunk's message (for an
    attachment chunk, the message that carries the attachment); ``None``
    for query paths that do not SELECT it.

    ``claimant_id`` identifies the chunk's message among every file
    claiming its ``message_id`` (see ``MessageRecord``).

    ``message_sender`` is that message's first ``From`` entry as
    ``Name <address>`` (or the bare address), so a prompt can attribute
    the passage to its own author; ``None`` for query paths that do not
    SELECT it or a message with no recorded sender.
    """

    chunk_id: str
    message_id: str
    claimant_id: str
    thread_id: str
    chunk_index: int
    text: str
    char_start: int
    char_end: int
    score: float = 0.0
    attachment_id: str | None = None
    attachment_filename: str | None = None
    attachment_mime: str | None = None
    message_date: str | None = None
    source_file: SourceFile | None = None
    message_sender: str | None = None


def _row_to_chunk_result(r) -> ChunkResult:
    """Build a ``ChunkResult`` from a row that may or may not have JOINed
    attachment columns.

    Centralizes the ``attachment_*`` extraction so every chunk-producing
    query path materializes the same shape — without this, callers that
    only ``SELECT c.*`` (no JOIN) would silently emit ``ChunkResult``
    instances with ``attachment_filename=None`` even when filename data
    existed for the chunk.
    """
    keys = r.keys()
    return ChunkResult(
        chunk_id=r["chunk_id"],
        message_id=r["message_id"],
        claimant_id=r["claimant_id"],
        thread_id=r["thread_id"],
        chunk_index=int(r["chunk_index"]),
        text=r["text"],
        char_start=int(r["char_start"]),
        char_end=int(r["char_end"]),
        score=float(r["score"]) if "score" in keys and r["score"] is not None else 0.0,
        attachment_id=r["attachment_id"] if "attachment_id" in keys else None,
        attachment_filename=(r["attachment_filename"] if "attachment_filename" in keys else None),
        attachment_mime=r["attachment_mime"] if "attachment_mime" in keys else None,
        message_date=r["message_date"] if "message_date" in keys else None,
        source_file=_row_to_source(r),
        message_sender=r["message_sender"] if "message_sender" in keys else None,
    )


@dataclass
class ThreadResult:
    thread_id: str
    subject: str
    participants: list[str]
    folder: str
    date_first: datetime
    date_last: datetime
    message_ids: list[str]
    snippet: str
    has_attachments: bool
    body_text: str = ""
    # Senders = only the From addresses of messages in this thread (a subset
    # of participants). The indexer populates this on every thread upsert.
    senders: list[str] = field(default_factory=list)
    score: float = 0.0
    # Per-message chunk hits backing this thread's ranking. Populated only
    # when the caller passes ``with_evidence=True``; left empty otherwise so
    # the existing hybrid_search consumers see the same shape they always
    # did. Intelligence tools surface these to the LLM as precise passage
    # citations rather than feeding the whole accumulated body.
    evidence_chunks: list[ChunkResult] = field(default_factory=list)
    # Retrieval-lane provenance: lane name -> 0-based rank the thread held
    # in that lane before fusion. Lanes are ``thread_fts`` / ``chunk_fts`` /
    # ``attachment_fts`` (BM25), ``thread_vec`` / ``chunk_vec`` (dense),
    # and ``rerank`` (final cross-encoder position when a reranker ran).
    # Populated additively by the RRF fusion as pure observability — it
    # never feeds back into scoring or ordering — and surfaced by
    # ``get_evidence(include_scores=True)``. Empty for retrieval paths
    # that bypass fusion (e.g. a thread addressed directly by ID).
    lane_ranks: dict[str, int] = field(default_factory=dict)


def _tag_lane_ranks(results: list[ThreadResult], lane: str) -> list[ThreadResult]:
    """Stamp each result's 0-based position in ``lane`` onto ``lane_ranks``.

    Called on a lane's ranked output *before* it enters RRF fusion so the
    fusion step can merge every lane a thread appeared in onto the single
    surviving ``ThreadResult``. Mutates and returns the same list for
    inline use at the call site. Purely additive — does not touch scores.
    """
    for rank, result in enumerate(results):
        result.lane_ranks[lane] = rank
    return results


@dataclass
class AttachmentResult:
    """One attachment occurrence surfaced by ``search_attachments``.

    An *occurrence* is one (message, attachment) pairing — the same
    content hash (``attachment_id``) can be attached to several messages,
    and each is its own row in the ``attachments`` table.

    ``extraction_status`` and ``text_snippet`` come from
    ``attachment_extractions`` (keyed by content hash). ``extraction_status``
    is ``None`` and ``text_snippet`` empty when the index has no extraction
    row for the attachment yet — distinct from a failed extraction, which
    has a non-NULL status.
    """

    attachment_id: str
    message_id: str
    claimant_id: str
    thread_id: str
    filename: str
    content_type: str
    size_bytes: int
    subject: str
    folder: str
    date_last: datetime
    senders: list[str] = field(default_factory=list)
    extraction_status: str | None = None
    text_snippet: str = ""
    score: float = 0.0
    # Raw file of the message carrying the attachment.
    source_file: SourceFile | None = None


def _row_to_attachment_result(r) -> AttachmentResult:
    """Build an ``AttachmentResult`` from a search-lane row.

    All three attachment lanes SELECT the same fixed column list (the
    subject pair, ``senders`` JSON, the extraction columns, the
    source-file columns, a ``score`` alias), so no per-column key guard
    is needed. ``display_subject``
    is preferred over the normalized ``subject`` when the row carries
    one, mirroring ``_row_to_result``.
    """
    return AttachmentResult(
        attachment_id=r["attachment_id"],
        message_id=r["message_id"],
        claimant_id=r["claimant_id"],
        thread_id=r["thread_id"],
        filename=r["filename"],
        content_type=r["content_type"],
        size_bytes=int(r["size_bytes"]),
        subject=r["display_subject"] or r["subject"],
        folder=r["folder"],
        date_last=datetime.fromisoformat(r["date_last"]),
        senders=json.loads(r["senders"]),
        extraction_status=r["extraction_status"],
        text_snippet=r["text_snippet"] or "",
        score=float(r["score"]),
        source_file=_row_to_source(r),
    )


@dataclass
class Participant:
    """One From / To / Cc entry of a message: canonical address plus the
    display name as written (``None`` when the header had none)."""

    name: str | None
    address: str


@dataclass
class MessageRecord:
    """One message's own headers, from ``messages`` +
    ``message_participants``.

    ``message_id`` is the RFC 5322 Message-ID, which the sender controls,
    so several indexed files can claim one. ``claimant_id`` tells them
    apart: the Message-ID plus ``#`` and the first eight hex digits of
    the file's SHA-256 (#217). Every per-message row is keyed by it."""

    message_id: str
    claimant_id: str
    thread_id: str
    subject: str
    sent_at: str
    folder: str
    has_attachments: bool
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    from_: list[Participant] = field(default_factory=list)
    to: list[Participant] = field(default_factory=list)
    cc: list[Participant] = field(default_factory=list)
    source_file: SourceFile | None = None


_MESSAGE_COLUMNS = (
    "m.message_id, m.claimant_id, m.thread_id, m.subject, m.sent_at, m.folder, "
    "m.has_attachments, m.in_reply_to, m.references_json, " + _SOURCE_COLUMNS
)


def _row_to_message_record(r) -> MessageRecord:
    return MessageRecord(
        message_id=r["message_id"],
        claimant_id=r["claimant_id"],
        thread_id=r["thread_id"],
        subject=r["subject"],
        sent_at=r["sent_at"],
        folder=r["folder"],
        has_attachments=bool(r["has_attachments"]),
        in_reply_to=r["in_reply_to"],
        references=json.loads(r["references_json"]),
        source_file=_row_to_source(r),
    )


def _attach_participants(conn: sqlite3.Connection, records: list[MessageRecord]) -> None:
    """Fill each record's From / To / Cc from ``message_participants``."""
    by_id = {rec.claimant_id: rec for rec in records}
    if not by_id:
        return
    placeholders = ",".join(["?"] * len(by_id))
    # rowid order is insertion order, i.e. header order.
    rows = conn.execute(
        "SELECT claimant_id, role, address, name FROM message_participants "
        f"WHERE claimant_id IN ({placeholders}) ORDER BY rowid",  # nosec B608
        list(by_id),
    ).fetchall()
    for p in rows:
        role_list = {"from": "from_", "to": "to", "cc": "cc"}[p["role"]]
        getattr(by_id[p["claimant_id"]], role_list).append(
            Participant(name=p["name"], address=p["address"])
        )


def _message_records(
    conn: sqlite3.Connection, where_sql: str, params: tuple, *, limit: int = -1, offset: int = 0
) -> list[MessageRecord]:
    """Messages matching ``where_sql``, oldest first (``claimant_id`` breaks
    ties), with participants. ``sent_at`` is stored as UTC ISO 8601, so
    string order is chronological order. ``limit=-1`` means no limit."""
    rows = conn.execute(
        f"SELECT {_MESSAGE_COLUMNS} FROM messages m WHERE {where_sql} "  # nosec B608
        "ORDER BY m.sent_at ASC, m.claimant_id ASC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    records = [_row_to_message_record(r) for r in rows]
    _attach_participants(conn, records)
    return records


@dataclass
class MessageBody:
    """A message's indexed body, rebuilt from its body chunks."""

    text: str
    # Characters of the indexed body past ``text`` that were not read.
    omitted_chars: int = 0


def _rebuild_body(chunks: list[sqlite3.Row], total_chars: int, limit: int | None) -> MessageBody:
    """Stitch a message's body chunks (in ``chunk_index`` order) back
    together, stopping at body offset ``limit``.

    Each chunk is an exact slice of the normalized body
    (``body[char_start:char_end] == text``) and adjacent chunks overlap by
    design, so a chunk contributes only what lies past the offset already
    emitted. Text repeated at a distinct offset is kept. Chunks that do
    not touch are joined by a paragraph break.
    """
    parts: list[str] = []
    end = 0
    for c in chunks:
        start, text = c["char_start"], c["text"]
        if limit is not None:
            if start >= limit:
                break
            text = text[: limit - start]
        if start + len(text) <= end:
            continue
        if parts and start > end:
            parts.append("\n\n")
        parts.append(text[max(0, end - start) :])
        end = start + len(text)
    return MessageBody(text="".join(parts), omitted_chars=max(0, total_chars - end))


def _message_bodies(
    conn: sqlite3.Connection, claimant_ids: list[str], limit: int | None
) -> dict[str, MessageBody]:
    """Bodies for ``claimant_ids`` (absent when a message has no body
    chunks), each cut at body offset ``limit``. Chunks starting past the
    limit are never read, so a huge message loads only what is shown."""
    if not claimant_ids:
        return {}
    placeholders = ",".join(["?"] * len(claimant_ids))
    scope = f"claimant_id IN ({placeholders}) AND attachment_id IS NULL"
    totals = dict(
        conn.execute(
            f"SELECT claimant_id, MAX(char_end) FROM message_chunks WHERE {scope} "  # nosec B608
            "GROUP BY claimant_id",
            claimant_ids,
        ).fetchall()
    )
    cutoff = "" if limit is None else " AND char_start < ?"
    rows = conn.execute(
        f"SELECT claimant_id, text, char_start FROM message_chunks WHERE {scope}{cutoff} "  # nosec B608
        "ORDER BY claimant_id, chunk_index",
        [*claimant_ids] + ([] if limit is None else [limit]),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        grouped.setdefault(r["claimant_id"], []).append(r)
    return {cid: _rebuild_body(chunks, totals[cid], limit) for cid, chunks in grouped.items()}


@dataclass
class ThreadPage:
    """One page of a thread's messages, read from one snapshot."""

    thread: ThreadResult
    total_messages: int
    offset: int
    messages: list[MessageRecord]
    # By claimant_id; a message with no indexed body is absent.
    bodies: dict[str, MessageBody]
    # Whether any message of the whole thread has an indexed body.
    has_bodies: bool


@dataclass
class MessageView:
    """One message, its thread, and its full body, read from one snapshot.

    ``other_claimants`` lists the claimant IDs of every other indexed
    file claiming the same Message-ID (#217); empty in the usual case."""

    record: MessageRecord
    thread: ThreadResult
    body: MessageBody | None
    other_claimants: list[str] = field(default_factory=list)


@dataclass
class AmbiguousMessageId:
    """A bare Message-ID that several indexed files claim (#217).

    ``get_message_view`` returns this rather than choosing one: an
    arrival-order rule would let a later (or earlier) file with a reused
    Message-ID stand in for the message the caller meant. ``claimants``
    holds each one's headers, oldest first, so the caller can pick a
    claimant ID."""

    message_id: str
    claimants: list[MessageRecord]


@dataclass
class MessagePage:
    """One page of an exhaustive enumeration.

    ``total_matches`` counts every message matching the predicates, not
    just this page; ``offset`` is how many matches earlier pages returned.
    ``next_cursor`` is ``None`` exactly when ``has_more`` is false.
    """

    total_matches: int
    offset: int
    messages: list[MessageRecord]
    has_more: bool
    next_cursor: str | None


# Each ``text`` term is its own FTS subquery; bound the count so one call
# cannot fan out into hundreds of them.
_MAX_TEXT_TERMS = 16

_INVALID_CURSOR = "invalid cursor; restart the query without a cursor"


def _sql_casefold(value):
    """Unicode caseless folding for SQL. SQLite's built-in ``lower`` folds
    ASCII only, so ``JOSÉ`` would never match ``josé``; ``casefold`` also
    expands ``ß`` so ``STRASSE`` matches ``Straße``. Compare against a
    needle folded the same way."""
    return value.casefold() if isinstance(value, str) else value


def _text_terms(text: str) -> list[str]:
    """Split ``text`` into distinct words exactly as the chunk index does.

    ``message_chunks_fts`` tokenizes with ``porter unicode61``. Hand-rolled
    splitting kept drifting from unicode61 — combining marks, underscores
    (separators to FTS), private-use characters (word characters to FTS)
    — and every drift silently changed an exhaustive count. So the words
    come from unicode61 itself: a throwaway in-memory FTS5 table and its
    ``fts5vocab`` instance view, in text order. Porter is left out so each
    word stays unstemmed; the index's tokenizer stems it once at MATCH
    time. unicode61's case and diacritic folding is idempotent, so a word
    quoted as a phrase re-tokenizes to itself. The text is not normalized
    (indexed chunks are stored as written).
    """
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='unicode61')")
        conn.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, 'instance')")
        conn.execute("INSERT INTO t(x) VALUES (?)", (text,))
        rows = conn.execute("SELECT term FROM v ORDER BY offset").fetchall()
    return list(dict.fromkeys(row[0] for row in rows))


def address_match_mode(value: str) -> str:
    """How a sender / recipient / participant predicate matches.

    ``"exact"`` when ``value`` holds a full address (``jane@example.com``,
    ``Jane <jane@example.com>``): canonical equality, an indexed lookup.
    ``"substring"`` otherwise (a domain like ``@example.com`` or a name
    fragment): case-insensitive substring of the address or display name.
    """
    # Nested-comment input that makes parseaddr recurse canonicalizes to
    # "", so it can only be a substring.
    canonical = canonical_addr(value)
    return "exact" if canonical and not canonical.startswith("@") else "substring"


def _participant_clause(value: str, roles: tuple[str, ...], params: list) -> str:
    """SQL restricting ``messages m`` to those where ``value`` appears in
    one of ``roles``; appends the bound values to ``params``."""
    role_sql = ",".join(["?"] * len(roles))
    if address_match_mode(value) == "exact":
        params.extend([canonical_addr(value), *roles])
        return (
            "m.claimant_id IN (SELECT claimant_id FROM message_participants "  # nosec B608
            f"WHERE address = ? AND role IN ({role_sql}))"
        )
    # Addresses are stored lowercased; names fold with ``mcp_casefold``.
    params.extend([*roles, value.strip().lower(), value.strip().casefold()])
    return (
        "m.claimant_id IN (SELECT claimant_id FROM message_participants "  # nosec B608
        f"WHERE role IN ({role_sql}) "
        "AND (instr(address, ?) > 0 OR instr(mcp_casefold(name), ?) > 0))"
    )


def _encode_cursor(digest: str, last: MessageRecord, offset: int) -> str:
    payload = json.dumps(
        {"v": 1, "q": digest, "s": last.sent_at, "m": last.claimant_id, "o": offset}
    )
    return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()


def _decode_cursor(cursor: str, digest: str) -> tuple[str, str, int]:
    """Return ``(sent_at, claimant_id, offset)`` of the last row already
    returned. Raises ``InvalidFilterError`` (a ``ValueError``) on a
    malformed cursor or one issued for different predicates (keyset positions only mean something within
    the same filtered ordering)."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidFilterError("cursor", _INVALID_CURSOR) from exc
    if not (
        isinstance(data, dict)
        and data.get("v") == 1
        and isinstance(data.get("q"), str)
        and isinstance(data.get("s"), str)
        and isinstance(data.get("m"), str)
        and isinstance(data.get("o"), int)
        and data["o"] >= 0
    ):
        raise InvalidFilterError("cursor", _INVALID_CURSOR)
    if data["q"] != digest:
        raise InvalidFilterError(
            "cursor",
            "cursor was issued for different filters; pass the same filters "
            "as the call that returned it, or restart without a cursor",
        )
    return data["s"], data["m"], data["o"]


def _add_contact(by_email: dict[str, dict], address: str, name: str | None, thread_id: str) -> None:
    """Record ``address`` on ``thread_id`` under display ``name`` in a
    ``find_contact`` aggregation (canonical email -> names, threads)."""
    bucket = by_email.setdefault(address, {"names": set(), "threads": set()})
    if name and name.strip():
        bucket["names"].add(name.strip())
    bucket["threads"].add(thread_id)


def _aggregate_participants(
    conn: sqlite3.Connection, needle: str, name_needle: str
) -> dict[str, dict]:
    """``find_contact``'s default aggregation, on ``conn``.

    The query selects addresses; every row of a selected address then
    aggregates, so a name match reports the contact's other names and
    threads too. Addresses are stored canonical (lowercased); names
    need the Unicode-aware ``mcp_casefold``.
    """
    by_email: dict[str, dict] = {}
    rows = conn.execute(
        """
        SELECT DISTINCT p.address, p.name, m.thread_id
        FROM message_participants p
        JOIN messages m ON m.claimant_id = p.claimant_id
        WHERE p.address IN (
            SELECT address FROM message_participants
            WHERE instr(address, ?) > 0 OR instr(mcp_casefold(name), ?) > 0
        )
        """,
        (needle, name_needle),
    ).fetchall()
    for row in rows:
        _add_contact(by_email, row["address"], row["name"], row["thread_id"])
    return by_email


def _thread_primaries(conn: sqlite3.Connection, thread_ids: list[str]) -> dict[str, dict[str, str]]:
    """``thread_id -> {canonical address: display name}`` from each listed
    thread's ``senders`` (every message's primary author)."""
    primaries: dict[str, dict[str, str]] = {}
    rows = conn.execute(
        "SELECT thread_id, senders FROM threads WHERE thread_id IN (SELECT value FROM json_each(?))",
        (json.dumps(thread_ids),),
    )
    for row in rows:
        senders: dict[str, str] = {}
        try:
            entries = json.loads(row["senders"])
        except json.JSONDecodeError, TypeError:
            entries = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, str):
                continue
            try:
                name, addr = parseaddr(entry)
            except Exception:
                # Sender strings come from indexed mail; an entry that
                # blows up parseaddr (nested-comment recursion) must cost
                # that entry, not the lookup.
                continue
            addr = addr.strip().lower()
            if "@" in addr:
                senders.setdefault(addr, name)
        primaries[row["thread_id"]] = senders
    return primaries


def _aggregate_senders(
    conn: sqlite3.Connection, needle: str, name_needle: str, folders: list[str] | None = None
) -> dict[str, dict]:
    """``find_contact(senders_only=True)``'s aggregation, on ``conn``.

    An address counts on a thread whose ``senders`` (each message's
    primary author, one display string per address) lists it. Its
    names are that senders entry plus its From rows on those threads,
    so a name first used on a later message still matches. The index
    records no author order within a message, so a name written for
    the address as a secondary author on a thread it primarily sent
    counts too.

    Work follows the query, not the mailbox: candidate addresses come
    from the From rows the query matches, and only the threads those
    candidates appear on have their ``senders`` parsed.

    ``folders`` keeps only threads with a message in one of them (the
    ``folders`` filter's membership), so names and counts come from the
    threads a search over that scope can return.
    """
    candidates = [
        row["address"]
        for row in conn.execute(
            """
            SELECT DISTINCT address FROM message_participants
            WHERE role = 'from'
              AND (instr(address, ?) > 0 OR instr(mcp_casefold(name), ?) > 0)
            """,
            (needle, name_needle),
        )
    ]
    if not candidates:
        return {}
    where = ["p.role = 'from'", "p.address IN (SELECT value FROM json_each(?))"]
    params: list = [json.dumps(candidates)]
    if folders:
        _append_folder_membership_sql(where, params, "m.thread_id", folders)
    rows = conn.execute(
        "SELECT DISTINCT p.address, p.name, m.thread_id "
        "FROM message_participants p "
        "JOIN messages m ON m.claimant_id = p.claimant_id "
        "WHERE " + " AND ".join(where),  # nosec B608
        params,
    ).fetchall()
    primaries = _thread_primaries(conn, sorted({row["thread_id"] for row in rows}))
    by_email: dict[str, dict] = {}
    for row in rows:
        senders = primaries.get(row["thread_id"], {})
        if row["address"] in senders:
            _add_contact(by_email, row["address"], senders[row["address"]], row["thread_id"])
            _add_contact(by_email, row["address"], row["name"], row["thread_id"])
    # A candidate matched on some From row; keep it only if the match
    # holds on a thread it primarily sent. Each kept address keeps its
    # whole sent history.
    return {
        addr: bucket
        for addr, bucket in by_email.items()
        if needle in addr or any(name_needle in n.casefold() for n in bucket["names"])
    }


def _contact_entities(conn: sqlite3.Connection, addresses: list[str]) -> dict[str, sqlite3.Row]:
    """``address -> entity row`` from the indexer's person entities
    (``person:<address>``): ``organization`` (the organization domain,
    ``None`` for a free-mail address), ``authority_class`` and
    ``authority_rule``."""
    rows = conn.execute(
        """
        SELECT p.canonical_key AS address, o.canonical_key AS organization,
               p.authority_class, p.authority_rule
        FROM entities p
        LEFT JOIN entities o ON o.entity_id = p.organization_id
        WHERE p.entity_id IN (SELECT 'person:' || value FROM json_each(?))
        """,
        (json.dumps(addresses),),
    ).fetchall()
    return {row["address"]: row for row in rows}


def _append_folder_membership_sql(
    where_clauses: list[str], params: list, thread_id_column: str, folders: list[str]
) -> None:
    """Keep rows whose thread has a message filed in one of ``folders``.

    Membership is per message (``messages.folder``), the rule
    ``list_threads`` and ``list_folders`` use, not ``threads.folder``,
    which only records where the thread's first message was filed
    (#308, #415). ``thread_id_column`` is a fixed literal from the
    caller; folder names are ``?``-bound.
    """
    placeholders = ",".join("?" * len(folders))
    where_clauses.append(
        f"{thread_id_column} IN (SELECT thread_id FROM messages "  # nosec B608
        f"WHERE folder IN ({placeholders}))"
    )
    params.extend(folders)


class Database:
    """Read-only handle to the indexer's SQLite output.

    Opens a fresh ``sqlite3.Connection`` for each read helper instead
    of holding one persistent connection for the lifetime of the
    process. Every production query uses an explicit ``closing(...)``
    context so file descriptors and WAL read marks are released
    immediately when the query finishes.

    Why per-access: a long-lived ``?mode=ro`` reader holds a WAL
    read mark that blocks ``PRAGMA wal_checkpoint(TRUNCATE)`` from
    the indexer (writer) side — so ``mail.db-wal`` grew unbounded
    under sustained writer activity (159 MB observed). Per-access
    connections release the read mark when the expression returns,
    letting the next checkpoint succeed and keeping the WAL bounded.
    Cost is the per-call connection setup (path stat, sqlite3
    open, ``sqlite_vec.load``, ``query_only`` pragma) — a few ms;
    negligible against the search/rerank work each call already
    does. Snapshot consistency is unchanged: each fresh connection
    sees the latest committed state, same as the prior single
    persistent reader.
    """

    def __init__(self, path: str):
        self.path = path
        # Fail fast at startup with the same checks ``_connect`` runs
        # on every access. Catches a missing volume / typo'd
        # SQLITE_PATH / unhealthy indexer at process start instead of
        # waiting for the first tool call.
        self._validate_path()

    def _validate_path(self) -> None:
        # MCP is a read-only consumer of the indexer's output; the indexer
        # is the only component allowed to create the data directory or
        # initialize the SQLite file. If either is missing at MCP startup,
        # the deployment topology is wrong (indexer unhealthy, wrong volume
        # mount, typo in SQLITE_PATH) — fail fast with a specific message
        # rather than silently creating an empty directory or opening a
        # non-existent ``?mode=ro`` URI and surfacing as "unable to open
        # database file" later.
        db_path = Path(self.path)
        if not db_path.parent.exists():
            raise FileNotFoundError(
                f"SQLite data directory does not exist: {db_path.parent}. "
                "mcp-server reads from the indexer's shared volume; verify "
                "that indexer is running and that SQLITE_PATH points at the "
                "mounted volume."
            )
        if not db_path.exists():
            raise FileNotFoundError(
                f"SQLite index not found at {db_path}. The indexer must "
                "initialize the database before mcp-server starts; check "
                "'docker compose logs indexer' for migration errors."
            )

    def _connect(self) -> sqlite3.Connection:
        self._validate_path()
        # Open the SQLite file in read-only URI mode so the MCP server never
        # attempts to mutate the shared index and so WAL readers can operate
        # without the connection trying to create or write a journal sidecar.
        # ``PRAGMA query_only`` is kept as defense-in-depth — any accidental
        # mutation via extension or future code path still fails fast.
        # The path is percent-encoded so URI-special characters (``#``,
        # ``?``, ``%``) name the file instead of starting a fragment or
        # query that would drop ``mode=ro`` and open or create a
        # different file.
        uri = f"file:{quote(str(self.path))}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.create_function("mcp_casefold", 1, _sql_casefold, deterministic=True)
        conn.execute("PRAGMA query_only = ON")
        return conn

    def _fetchall(self, sql: str, params=()) -> list[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(sql, params).fetchall()

    def _fetchone(self, sql: str, params=()) -> sqlite3.Row | None:
        with closing(self._connect()) as conn:
            return conn.execute(sql, params).fetchone()

    def _default_folder_scope(
        self, folders: list[str] | None, conn: sqlite3.Connection | None = None
    ) -> list[str] | None:
        """The ``folders`` filter a mailbox-wide thread search applies.

        A caller's non-empty ``folders`` is kept as given, so naming a
        ``DEFAULT_EXCLUDED_FOLDERS`` folder includes it. Otherwise, when
        the mailbox holds mail in an excluded folder, the scope is every
        other folder holding mail (``[]`` when there is none), so the
        exclusion runs through the ``folders`` path: pushed into the
        keyword SQL, applied post-fusion, and counted as a filter that
        widens the vector windows (#286). A mailbox with no excluded
        mail gets ``None``, the unfiltered search exactly as before.
        ``conn`` runs the folder lookup inside a caller's snapshot.
        """
        if folders:
            return folders
        sql = "SELECT DISTINCT folder FROM messages"
        rows = conn.execute(sql).fetchall() if conn is not None else self._fetchall(sql)
        present = [r["folder"] for r in rows]
        if not any(f in DEFAULT_EXCLUDED_FOLDERS for f in present):
            return None
        return [f for f in present if f not in DEFAULT_EXCLUDED_FOLDERS]

    # -------------------------------------------------------------------------
    # Hybrid search — BM25 + vector, merged via Reciprocal Rank Fusion
    # -------------------------------------------------------------------------

    def hybrid_search(
        self,
        query_text: str,
        query_embedding: list[float],
        folders: list[str] | None = None,
        from_addr: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
        participant: str | None = None,
        limit: int = 10,
        with_evidence: bool = False,
        reranker: RerankerBackend | None = None,
        evidence_per_thread: int = 3,
        authority_class: str | None = None,
    ) -> list[ThreadResult]:
        authority_class = normalize_authority_class(authority_class)
        folders = self._default_folder_scope(folders)
        if folders == []:
            return []
        oversample = (
            _FILTERED_OVERSAMPLE
            if self._has_post_fusion_filter(
                folders,
                from_addr,
                date_from,
                date_to,
                has_attachments,
                participant,
                authority_class,
            )
            else _UNFILTERED_OVERSAMPLE
        )
        # When a reranker is configured the fetch must size for the
        # rerank input window (``RERANK_CANDIDATES``), not the final
        # ``limit``. Sizing for ``limit`` here would silently underfeed
        # the reranker — e.g. ``ask_mailbox`` with limit=5 and
        # oversample=10 fetches 50 lane candidates, dedupes/filters
        # down to ~10-20 unique threads, and the rerank stage sees
        # nowhere near its configured 50.
        rerank_floor = reranker.candidates if reranker is not None else 0
        target_count = max(limit, rerank_floor)
        fetch_limit = target_count * oversample
        # Push folder / date / has_attachments filters into the keyword SQL
        # so that deep-ranked candidates aren't truncated by the fetch
        # limit before they could qualify. Vector search has no equivalent
        # pushdown in sqlite-vec, so it stays unfiltered; _apply_filters
        # catches everything post-fusion for uniformity.
        bm25_results = self._keyword_search(
            query_text,
            fetch_limit,
            folders=folders,
            date_from=date_from,
            date_to=date_to,
            has_attachments=has_attachments,
        )
        # Per-message chunks. Oversample heavily because many chunks
        # may belong to a single thread — without enough chunks the lane
        # only contributes a handful of unique threads. The chunk lane
        # "lifts" precise-passage hits into thread ranking, so a thread
        # with one strong chunk can outrank a thread with a mediocre
        # coarse vector score. Threads with no chunks (empty bodies)
        # simply don't appear in this lane and rely on the other two
        # for ranking.
        #
        # ``_CHUNK_LANE_OVERSAMPLE`` (=10) matches the FTS chunk and
        # attachment lanes that already use this factor. The shared
        # constant exists precisely to address the "long thread with
        # many similar chunks monopolises the top-K and contributes
        # only one credit to RRF" failure mode — leaving the vec lane
        # at the prior ``* 3`` would re-create that asymmetry between
        # the keyword and dense chunk paths.
        #
        # A failed lane (``None``) contributes nothing; hybrid keeps its
        # existing silent fallback to the remaining lanes.
        vec_results, chunk_hits = self._vector_lanes(
            query_embedding,
            fetch_limit,
            fetch_limit * _CHUNK_LANE_OVERSAMPLE,
            target_count,
            folders=folders,
            from_addr=from_addr,
            date_from=date_from,
            date_to=date_to,
            has_attachments=has_attachments,
            participant=participant,
            authority_class=authority_class,
        )
        vec_results = vec_results or []
        chunk_hits = chunk_hits or []
        timings.count("thread_vec", len(vec_results))
        timings.count("chunk_vec", len(chunk_hits))
        with timings.stage("fusion"):
            fused = self._reciprocal_rank_fusion(bm25_results, vec_results, chunk_hits)
            filtered = self._apply_filters(
                fused,
                folders,
                from_addr,
                date_from,
                date_to,
                has_attachments,
                participant,
                authority_class,
            )
        timings.count("filtered", len(filtered))

        # Decide how many candidates to keep before any rerank. The
        # reranker's ``candidates`` knob is a *funnel size* — how many
        # results to feed into the rerank stage — not a result cap. A
        # caller asking for ``limit=20`` against ``RERANK_CANDIDATES=10``
        # must still get up to 20 results back, so the slice has to
        # honour ``max(limit, candidates)``. Without the ``max`` an
        # operator who tightened ``RERANK_CANDIDATES`` for latency
        # would silently cap recall on bigger callers like
        # ``extract_from_emails(limit=50)``.
        if reranker is not None:
            candidates_n = max(limit, reranker.candidates)
        else:
            candidates_n = limit
        candidates = filtered[:candidates_n]

        if with_evidence and candidates:
            # Fetch evidence chunks per surfaced thread, not from the
            # global chunk-vec pool. A thread can win the candidates
            # slice via BM25, thread-vector, or any of the keyword
            # filter lanes (sender, date, attachment filename FTS) —
            # and its specific chunks may not rank anywhere in the
            # global chunk-vec top-K. The prior pool-reuse shape left
            # those threads with empty ``evidence_chunks``, silently
            # dropping the attachment evidence that ``ask_mailbox``,
            # ``get_evidence`` and ``extract_from_emails`` read whenever
            # the carrier email won by metadata but the attachment
            # chunks didn't enter the chunk-vec pool.
            wanted = [r.thread_id for r in candidates]
            # Recompute attachment-FTS hits standalone so we know which
            # candidates won via filename match. The keyword lane's RRF
            # output is opaque to lane provenance, so we re-run the
            # narrow query here (cheap FTS5 lookup, only when the
            # caller wants evidence). For these threads, attachment
            # chunks are floated to the front of the per-thread
            # evidence slice — fixes the "filename match → wrong
            # evidence" gap where the LLM saw body text instead of
            # the attachment the user asked about.
            with timings.stage("evidence_fetch"):
                matched_attachments = self._matched_attachments(query_text, wanted)
                grouped = self.get_evidence_chunks_for_threads(
                    wanted,
                    query_embedding,
                    per_thread_limit=evidence_per_thread,
                    matched_attachments=matched_attachments,
                )
            for result in candidates:
                result.evidence_chunks = grouped.get(result.thread_id, [])
                timings.count("evidence_chunks", len(result.evidence_chunks))

        if reranker is not None and candidates:
            return self._apply_rerank(query_text, candidates, reranker, limit)

        return candidates[:limit]

    @staticmethod
    def _candidate_text(result: ThreadResult) -> str:
        """The text fed to the reranker for one candidate.

        Prefer the best evidence chunk (richest signal — ~1500 tokens of
        the actual passage that lifted this thread into ranking) when
        available; fall back to ``subject + snippet`` (which is what
        callers without ``with_evidence=True`` have to work with). The
        subject is included in both shapes so a query like "invoice
        from acme" can rerank on the subject even when the body is
        boilerplate. The subject is sender-controlled and unbounded, so it
        is cut at ``_RERANK_SUBJECT_CHARS``: every candidate is sent to the
        rerank provider in one request.
        """
        subject = result.subject[:_RERANK_SUBJECT_CHARS]
        if result.evidence_chunks:
            return f"Subject: {subject}\n\n{result.evidence_chunks[0].text}"
        return f"Subject: {subject}\n\n{result.snippet}"

    def _apply_rerank(
        self,
        query: str,
        candidates: list[ThreadResult],
        reranker: RerankerBackend,
        limit: int,
    ) -> list[ThreadResult]:
        """Reorder ``candidates`` via the reranker and truncate to ``limit``.

        The caller's ``limit`` is passed to the reranker as ``top_n``,
        so the rerank stage returns up to ``limit`` results. The outer
        ``[:limit]`` is then redundant for the success path but kept
        for the rerank-failure fallback below.

        On reranker failure (returns empty list), the candidates fall
        back to RRF order — so a rerank outage degrades quality without
        failing the whole query. A ranking with an out-of-range or
        repeated index is a failure too: applying it would drop or
        duplicate results. It is checked before any candidate is touched.
        """
        docs = [self._candidate_text(c) for c in candidates]
        timings.count("rerank_candidates", len(docs))
        with timings.stage("rerank"):
            scored = reranker.rerank(query, docs, top_n=limit)
        if not scored:
            return candidates[:limit]
        indices = [orig_idx for orig_idx, _ in scored]
        if len(set(indices)) != len(indices) or not all(0 <= i < len(candidates) for i in indices):
            # Fixed text only: the ranking came from the provider.
            log.warning("rerank returned invalid indices; falling back to RRF order")
            return candidates[:limit]
        reordered: list[ThreadResult] = []
        for orig_idx, score in scored:
            result = candidates[orig_idx]
            result.score = score
            # Record the post-rerank position as the ``rerank`` lane
            # rank — observability only, surfaced by get_evidence.
            result.lane_ranks["rerank"] = len(reordered)
            reordered.append(result)
        return reordered[:limit]

    def keyword_search(
        self,
        query_text: str,
        folders: list[str] | None = None,
        from_addr: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
        participant: str | None = None,
        limit: int = 10,
        authority_class: str | None = None,
    ) -> list[ThreadResult]:
        authority_class = normalize_authority_class(authority_class)
        folders = self._default_folder_scope(folders)
        if folders == []:
            return []
        # Previously dropped every filter except ``folders`` on the floor, so
        # a keyword search with a date or sender filter returned unfiltered
        # results. All four filters now flow through, matching hybrid_search.
        oversample = (
            _FILTERED_OVERSAMPLE
            if self._has_post_fusion_filter(
                folders,
                from_addr,
                date_from,
                date_to,
                has_attachments,
                participant,
                authority_class,
            )
            else _UNFILTERED_OVERSAMPLE
        )
        results = self._keyword_search(
            query_text,
            limit * oversample,
            folders=folders,
            date_from=date_from,
            date_to=date_to,
            has_attachments=has_attachments,
        )
        with timings.stage("fusion"):
            filtered = self._apply_filters(
                results,
                folders,
                from_addr,
                date_from,
                date_to,
                has_attachments,
                participant,
                authority_class,
            )
        timings.count("filtered", len(filtered))
        return filtered[:limit]

    def semantic_search(
        self,
        query_embedding: list[float],
        folders: list[str] | None = None,
        from_addr: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
        participant: str | None = None,
        limit: int = 10,
        authority_class: str | None = None,
    ) -> list[ThreadResult]:
        """Vector retrieval over both thread- and chunk-level lanes.

        Fuses ``threads_vec`` (mean-pooled thread vector) with
        ``message_chunks_vec`` (per-message precision vectors) via RRF,
        so a long thread with one strong matching chunk can outrank a
        thread whose coarse mean only weakly aligns with the query.
        Mirrors the dense half of ``hybrid_search`` — without the
        chunk lane the mode silently returned the worse retrieval
        whenever a caller chose ``mode="semantic"``.

        Raises ``VectorLanesUnavailableError`` when both lanes fail, so a
        broken index is not reported as "no matches". One failed lane
        still answers from the other.
        """
        authority_class = normalize_authority_class(authority_class)
        folders = self._default_folder_scope(folders)
        if folders == []:
            return []
        oversample = (
            _FILTERED_OVERSAMPLE
            if self._has_post_fusion_filter(
                folders,
                from_addr,
                date_from,
                date_to,
                has_attachments,
                participant,
                authority_class,
            )
            else _UNFILTERED_OVERSAMPLE
        )
        fetch_limit = limit * oversample
        # Same chunk-lane oversample reasoning as ``hybrid_search``:
        # without enough chunks, a long thread monopolises the top-K
        # and other threads never enter the lane.
        vec_results, chunk_hits = self._vector_lanes(
            query_embedding,
            fetch_limit,
            fetch_limit * _CHUNK_LANE_OVERSAMPLE,
            limit,
            folders=folders,
            from_addr=from_addr,
            date_from=date_from,
            date_to=date_to,
            has_attachments=has_attachments,
            participant=participant,
            authority_class=authority_class,
        )
        timings.count("thread_vec", len(vec_results or []))
        timings.count("chunk_vec", len(chunk_hits or []))
        if vec_results is None and chunk_hits is None:
            raise VectorLanesUnavailableError()
        with timings.stage("fusion"):
            fused = self._reciprocal_rank_fusion(
                bm25=[], vec=vec_results or [], chunks=chunk_hits or []
            )
            filtered = self._apply_filters(
                fused,
                folders,
                from_addr,
                date_from,
                date_to,
                has_attachments,
                participant,
                authority_class,
            )
        timings.count("filtered", len(filtered))
        return filtered[:limit]

    # -------------------------------------------------------------------------
    # Attachment search
    # -------------------------------------------------------------------------

    def search_attachments(
        self,
        query: str | None = None,
        content_type: str | None = None,
        from_addr: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        extracted_only: bool = False,
        limit: int = 20,
    ) -> list[AttachmentResult]:
        """Search indexed attachments by filename, MIME type, and extracted text.

        Two FTS lanes run when ``query`` is set: ``attachments_fts``
        (filename + content type) and ``message_chunks_fts`` restricted to
        attachment-derived chunks (the extracted PDF / OCR / document
        text). Filename / MIME matches are listed first, then extracted-text
        matches not already surfaced — a finder ("the quote PDF from
        Acme") is better served by the obvious filename hit on top. With
        no ``query`` the index is scanned by the structured filters
        alone, newest thread activity first.

        Filters: ``content_type`` is an exact MIME match; ``date_from`` /
        ``date_to`` bound the parent thread's activity (a bare date
        includes the whole day); ``extracted_only`` keeps only
        attachments whose text extraction succeeded; ``from_addr`` keeps
        only attachments on threads the address sent on (matched against
        the thread's From-line senders, post-query in Python). A blank
        ``content_type`` is no filter, normalized here once so every lane
        applies the same rule.
        """
        if content_type is not None and not content_type.strip():
            content_type = None
        extra_clauses, extra_params = self._attachment_filter_clauses(
            content_type, date_from, date_to, extracted_only
        )
        # ``from_addr`` is matched in Python against the parent thread's
        # senders (the attachments table carries no sender column).
        # Oversample the SQL fetch when it is set so a deep match still
        # survives the filter — same reasoning as the thread-search
        # post-fusion filters.
        fetch_limit = limit * (_FILTERED_OVERSAMPLE if from_addr else 1)

        fts_query = _sanitize_fts_query(query) if query else ""
        if fts_query:
            results = self._attachment_filename_lane(
                fts_query, extra_clauses, extra_params, fetch_limit
            )
            seen = {(r.attachment_id, r.claimant_id, r.filename) for r in results}
            for r in self._attachment_text_lane(
                fts_query, content_type, extra_clauses, extra_params, fetch_limit
            ):
                key = (r.attachment_id, r.claimant_id, r.filename)
                if key not in seen:
                    seen.add(key)
                    results.append(r)
        elif query:
            # A query that sanitized to nothing (pure punctuation) is an
            # explicit no-match rather than a silent unfiltered scan.
            return []
        else:
            results = self._attachment_scan(extra_clauses, extra_params, fetch_limit)

        if from_addr:
            fa = from_addr.lower()
            results = [r for r in results if _addr_matches(r.senders, fa)]
        return results[:limit]

    @staticmethod
    def _attachment_filter_clauses(
        content_type: str | None,
        date_from: str | None,
        date_to: str | None,
        extracted_only: bool,
    ) -> tuple[list[str], list]:
        """Build the SQL WHERE fragment shared by every attachment lane.

        Returns ``(clauses, params)``: literal clause strings (referencing
        the ``a`` / ``t`` / ``e`` aliases every lane query declares) and
        their ``?``-bound parameter values. Date bounds are normalized the
        same way the thread-search lanes normalize them so a bare
        ``"2024-12-31"`` includes the full day it names.
        """
        # Attachments on messages filed in an excluded folder are left
        # out (#441); the tool has no folder filter to name them.
        excluded = ",".join("?" * len(DEFAULT_EXCLUDED_FOLDERS))
        clauses: list[str] = [
            "a.claimant_id NOT IN (SELECT claimant_id FROM messages "  # nosec B608
            f"WHERE folder IN ({excluded}))"
        ]
        params: list = [*DEFAULT_EXCLUDED_FOLDERS]
        if content_type:
            clauses.append("a.content_type = ?")
            params.append(content_type)
        date_from_iso, date_to_iso = _normalize_date_range(date_from, date_to)
        if date_from_iso is not None:
            clauses.append("t.date_last >= ?")
            params.append(date_from_iso)
        if date_to_iso is not None:
            clauses.append("t.date_first <= ?")
            params.append(date_to_iso)
        if extracted_only:
            # ``e`` is LEFT JOINed, so this also drops attachments with no
            # extraction row at all (status reads NULL) — the intent of
            # "only attachments whose text I could actually read".
            clauses.append("e.extraction_status = 'success'")
        return clauses, params

    def _attachment_filename_lane(
        self,
        fts_query: str,
        extra_clauses: list[str],
        extra_params: list,
        limit: int,
    ) -> list[AttachmentResult]:
        """Attachments whose filename / content type match ``fts_query``."""
        where = ["attachments_fts MATCH ?", *extra_clauses]
        params = [fts_query, *extra_params, limit]
        # WHERE clauses are fixed literals (the MATCH plus the
        # filter-clause literals); every user value is ``?``-bound.
        sql = (
            "SELECT a.attachment_id, COALESCE(m.message_id, a.claimant_id) AS message_id, "
            "a.claimant_id, a.thread_id, a.filename, "
            "a.content_type, a.size_bytes, t.subject, t.display_subject, "
            "t.folder, t.date_last, t.senders, e.extraction_status, "
            "substr(e.extracted_text, 1, 240) AS text_snippet, "
            f"{_SOURCE_COLUMNS}, "
            "bm25(attachments_fts) AS score "
            "FROM attachments_fts "
            "JOIN attachments a ON attachments_fts.rowid = a.fts_rowid "
            "JOIN threads t ON a.thread_id = t.thread_id "
            "LEFT JOIN attachment_extractions e ON e.attachment_id = a.attachment_id "
            "LEFT JOIN messages m ON m.claimant_id = a.claimant_id "
            "WHERE " + " AND ".join(where) + " "  # nosec B608
            "ORDER BY score LIMIT ?"
        )
        try:
            rows = self._fetchall(sql, params)
        except sqlite3.OperationalError as e:
            log.warning("Attachment filename search unavailable: %s", type(e).__name__)
            return []
        return [_row_to_attachment_result(r) for r in rows]

    def _attachment_text_lane(
        self,
        fts_query: str,
        content_type: str | None,
        extra_clauses: list[str],
        extra_params: list,
        limit: int,
    ) -> list[AttachmentResult]:
        """Attachments whose extracted text matches ``fts_query``.

        Searches ``message_chunks_fts`` restricted to attachment-derived
        chunks, then resolves each match to its attachment occurrence.
        One content hash can be attached under several filenames in a
        single message; the JOIN anchors on the lowest
        ``attachment_occurrence_id`` for the pair so the row count is
        deterministic (see ``_chunk_vector_search`` for the full
        rationale). The anchor is chosen among the occurrences that pass
        ``content_type``, the one filter that can differ between them
        (thread and extraction are shared by the pair): choosing first
        dropped the match when only another occurrence passed (#309).

        Many chunks can match one attachment, so each attachment is ranked
        by its best chunk *before* the LIMIT: limiting chunk rows first
        let one long document fill every slot and hide other matching
        attachments. ``bm25()`` cannot be used inside a grouped query, so
        the scored hits are a MATERIALIZED CTE (which SQLite does not
        flatten into the GROUP BY). The filters apply before the grouping,
        so a narrowly filtered search does not aggregate every matching
        chunk in the mailbox.
        """
        where = ["c.attachment_id IS NOT NULL", *extra_clauses]
        params = [fts_query, content_type, content_type, *extra_params, limit]
        sql = (
            "WITH hits AS MATERIALIZED ( "
            "    SELECT rowid AS fts_rowid, bm25(message_chunks_fts) AS score "
            "    FROM message_chunks_fts WHERE message_chunks_fts MATCH ? ), "
            "best AS ( "
            "    SELECT a.attachment_occurrence_id, MIN(h.score) AS score "
            "    FROM hits h JOIN message_chunks c ON c.fts_rowid = h.fts_rowid "
            "    JOIN attachments a ON a.attachment_occurrence_id = ( "
            "        SELECT MIN(a2.attachment_occurrence_id) FROM attachments a2 "
            "        WHERE a2.attachment_id = c.attachment_id "
            "          AND a2.claimant_id = c.claimant_id "
            "          AND (? IS NULL OR a2.content_type = ?) ) "
            "    JOIN threads t ON a.thread_id = t.thread_id "
            "    LEFT JOIN attachment_extractions e ON e.attachment_id = a.attachment_id "
            "    WHERE " + " AND ".join(where) + " "  # nosec B608
            "    GROUP BY a.attachment_occurrence_id ) "
            "SELECT a.attachment_id, COALESCE(m.message_id, a.claimant_id) AS message_id, "
            "a.claimant_id, a.thread_id, a.filename, "
            "a.content_type, a.size_bytes, t.subject, t.display_subject, "
            "t.folder, t.date_last, t.senders, e.extraction_status, "
            "substr(e.extracted_text, 1, 240) AS text_snippet, "
            f"{_SOURCE_COLUMNS}, "
            "best.score AS score "
            "FROM best "
            "JOIN attachments a ON a.attachment_occurrence_id = best.attachment_occurrence_id "
            "JOIN threads t ON a.thread_id = t.thread_id "
            "LEFT JOIN attachment_extractions e ON e.attachment_id = a.attachment_id "
            "LEFT JOIN messages m ON m.claimant_id = a.claimant_id "
            "ORDER BY score LIMIT ?"
        )
        try:
            rows = self._fetchall(sql, params)
        except sqlite3.OperationalError as e:
            log.warning("Attachment text search unavailable: %s", type(e).__name__)
            return []
        results: list[AttachmentResult] = []
        seen: set[tuple[str, str, str]] = set()
        for r in rows:
            result = _row_to_attachment_result(r)
            key = (result.attachment_id, result.claimant_id, result.filename)
            if key in seen:
                continue
            seen.add(key)
            results.append(result)
        return results

    def _attachment_scan(
        self,
        extra_clauses: list[str],
        extra_params: list,
        limit: int,
    ) -> list[AttachmentResult]:
        """List attachments by structured filters only (no text query)."""
        # ``1=1`` keeps the AND-join valid when no filter clause is set.
        where = ["1=1", *extra_clauses]
        params = [*extra_params, limit]
        sql = (
            "SELECT a.attachment_id, COALESCE(m.message_id, a.claimant_id) AS message_id, "
            "a.claimant_id, a.thread_id, a.filename, "
            "a.content_type, a.size_bytes, t.subject, t.display_subject, "
            "t.folder, t.date_last, t.senders, e.extraction_status, "
            "substr(e.extracted_text, 1, 240) AS text_snippet, "
            f"{_SOURCE_COLUMNS}, "
            "0.0 AS score "
            "FROM attachments a "
            "JOIN threads t ON a.thread_id = t.thread_id "
            "LEFT JOIN attachment_extractions e ON e.attachment_id = a.attachment_id "
            "LEFT JOIN messages m ON m.claimant_id = a.claimant_id "
            "WHERE " + " AND ".join(where) + " "  # nosec B608
            "ORDER BY t.date_last DESC LIMIT ?"
        )
        try:
            rows = self._fetchall(sql, params)
        except sqlite3.OperationalError as e:
            log.warning("Attachment scan unavailable: %s", type(e).__name__)
            return []
        return [_row_to_attachment_result(r) for r in rows]

    @staticmethod
    def _has_post_fusion_filter(
        folders: list[str] | None = None,
        from_addr: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
        participant: str | None = None,
        authority_class: str | None = None,
    ) -> bool:
        return bool(
            folders
            or from_addr
            or date_from
            or date_to
            or has_attachments is not None
            or participant
            or authority_class
        )

    def _keyword_search(
        self,
        query: str,
        limit: int,
        folders: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
    ) -> list[ThreadResult]:
        with timings.stage("thread_fts"):
            thread_hits = self._thread_keyword_search(
                query,
                limit,
                folders=folders,
                date_from=date_from,
                date_to=date_to,
                has_attachments=has_attachments,
            )
        with timings.stage("chunk_fts"):
            chunk_hits = self._chunk_keyword_search(
                query,
                limit,
                folders=folders,
                date_from=date_from,
                date_to=date_to,
                has_attachments=has_attachments,
            )
        with timings.stage("attachment_fts"):
            attachment_hits = self._attachment_keyword_search(
                query,
                limit,
                folders=folders,
                date_from=date_from,
                date_to=date_to,
                has_attachments=has_attachments,
            )
        timings.count("thread_fts", len(thread_hits))
        timings.count("chunk_fts", len(chunk_hits))
        timings.count("attachment_fts", len(attachment_hits))
        # Tag each lane's pre-fusion rank so the fusion step can record
        # lane provenance on the surviving thread row (surfaced by
        # ``get_evidence(include_scores=True)``). Pure observability —
        # tagging mutates ``lane_ranks`` only and never affects scoring.
        _tag_lane_ranks(thread_hits, "thread_fts")
        _tag_lane_ranks(chunk_hits, "chunk_fts")
        _tag_lane_ranks(attachment_hits, "attachment_fts")
        with timings.stage("fusion"):
            return self._reciprocal_rank_fusion_threads(thread_hits, chunk_hits, attachment_hits)[
                :limit
            ]

    def _thread_keyword_search(
        self,
        query: str,
        limit: int,
        folders: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
    ) -> list[ThreadResult]:
        # threads_fts is a contentless FTS5 table, so its columns (including
        # any UNINDEXED ones) always read back as NULL. The reliable way to
        # link an FTS row back to its thread is the rowid, which the indexer
        # captures on write into threads.fts_rowid.
        #
        # Filters that live on the ``threads`` table — folder, date range,
        # attachment flag — are pushed into SQL rather than applied after a
        # Python slice. Otherwise the ``LIMIT`` truncates before the filter
        # runs, and a user searching "2024-06 emails in Sent" can see empty
        # results even when matching mail exists outside the top N BM25
        # candidates. ``from_addr``/sender filtering stays in Python because
        # it hits a JSON-in-column value.
        fts_query = _sanitize_fts_query(query)
        if not fts_query:
            return []

        where_clauses = ["threads_fts MATCH ?"]
        params: list = [fts_query]
        if folders:
            _append_folder_membership_sql(where_clauses, params, "t.thread_id", folders)
        # Normalize before SQL pushdown. Stored dates are full ISO timestamps
        # (``"2024-12-31T10:00:00+00:00"``); a bare user filter ``"2024-12-31"``
        # would lexicographically sort *below* any same-day stored timestamp
        # and exclude the final day entirely. ``_parse_filter_date`` promotes
        # date-only values to start/end of day in UTC so the comparison is
        # correct. date_from also benefits from explicit UTC normalization
        # for inputs that arrive with ``Z`` or offset suffixes.
        date_from_iso, date_to_iso = _normalize_date_range(date_from, date_to)
        if date_from_iso is not None:
            where_clauses.append("t.date_last >= ?")
            params.append(date_from_iso)
        if date_to_iso is not None:
            where_clauses.append("t.date_first <= ?")
            params.append(date_to_iso)
        if has_attachments is not None:
            where_clauses.append("t.has_attachments = ?")
            params.append(1 if has_attachments else 0)

        # The WHERE clauses composed here are fixed literals chosen by the
        # branches above; every user-supplied value goes through ``?``
        # parameter binding. nosec B608 suppresses the hardcoded-SQL
        # heuristic that bandit can't verify statically.
        sql = (
            "SELECT "
            "t.thread_id, t.subject, t.participants, t.senders, t.folder, "
            "t.date_first, t.date_last, t.message_ids, "
            "t.snippet, t.has_attachments, t.body_text, t.display_subject, "
            "bm25(threads_fts) AS score "
            "FROM threads_fts "
            "JOIN threads t ON threads_fts.rowid = t.fts_rowid "
            "WHERE " + " AND ".join(where_clauses) + " "  # nosec B608
            "ORDER BY score LIMIT ?"
        )
        params.append(limit)

        try:
            rows = self._fetchall(sql, params)
            return [self._row_to_result(r) for r in rows]
        except sqlite3.OperationalError as e:
            # Defense-in-depth: if the sanitized query still trips FTS5, fall
            # back to a LIKE scan against subject/body/participants so valid
            # searches still return recall rather than empty.
            log.warning("FTS keyword search error, falling back to LIKE: %s", type(e).__name__)
            return self._like_fallback(query, limit, folders, date_from, date_to, has_attachments)

    def _chunk_keyword_search(
        self,
        query: str,
        limit: int,
        folders: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
    ) -> list[ThreadResult]:
        fts_query = _sanitize_fts_query(query)
        if not fts_query:
            return []

        where_clauses = ["message_chunks_fts MATCH ?"]
        params: list = [fts_query]
        self._append_thread_filter_sql(
            where_clauses,
            params,
            folders=folders,
            date_from=date_from,
            date_to=date_to,
            has_attachments=has_attachments,
        )
        # SQLite FTS5 doesn't allow ``bm25(...)`` inside aggregates or
        # subqueries the outer query aggregates over, so the dedupe-by-
        # thread step happens in Python below. The SQL oversample is
        # ``limit * _CHUNK_LANE_OVERSAMPLE`` so that a long thread with
        # many matching chunks doesn't absorb every row before other
        # threads get a chance to enter the chunk lane (the failure
        # mode that hurts RRF diversity in the hybrid fuse).
        sql = (
            "SELECT "
            "t.thread_id, t.subject, t.participants, t.senders, t.folder, "
            "t.date_first, t.date_last, t.message_ids, "
            "t.snippet, t.has_attachments, t.body_text, t.display_subject, "
            "bm25(message_chunks_fts) AS score "
            "FROM message_chunks_fts "
            "JOIN message_chunks c ON message_chunks_fts.rowid = c.fts_rowid "
            "JOIN threads t ON c.thread_id = t.thread_id "
            "WHERE " + " AND ".join(where_clauses) + " "  # nosec B608
            "ORDER BY score LIMIT ?"
        )
        params.append(limit * _CHUNK_LANE_OVERSAMPLE)
        try:
            rows = self._fetchall(sql, params)
        except sqlite3.OperationalError as e:
            # Indexer fails fast on schema-version mismatch, so an older
            # DB is impossible at runtime. Reaching this branch implies
            # corruption or a missing FTS shadow — log at warning so the
            # operator notices precision retrieval has degraded to none.
            log.warning("Chunk keyword search unavailable: %s", type(e).__name__)
            return []
        results = [self._row_to_result(r) for r in rows]
        return self._best_per_thread(results)[:limit]

    def _attachment_keyword_search(
        self,
        query: str,
        limit: int,
        folders: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
    ) -> list[ThreadResult]:
        fts_query = _sanitize_fts_query(query)
        if not fts_query:
            return []

        where_clauses = ["attachments_fts MATCH ?"]
        params: list = [fts_query]
        self._append_thread_filter_sql(
            where_clauses,
            params,
            folders=folders,
            date_from=date_from,
            date_to=date_to,
            has_attachments=has_attachments,
        )
        # Same dedupe-in-Python pattern as ``_chunk_keyword_search`` —
        # FTS5 forbids aggregating over ``bm25()``.
        sql = (
            "SELECT "
            "t.thread_id, t.subject, t.participants, t.senders, t.folder, "
            "t.date_first, t.date_last, t.message_ids, "
            "t.snippet, t.has_attachments, t.body_text, t.display_subject, "
            "bm25(attachments_fts) AS score "
            "FROM attachments_fts "
            "JOIN attachments a ON attachments_fts.rowid = a.fts_rowid "
            "JOIN threads t ON a.thread_id = t.thread_id "
            "WHERE " + " AND ".join(where_clauses) + " "  # nosec B608
            "ORDER BY score LIMIT ?"
        )
        params.append(limit * _CHUNK_LANE_OVERSAMPLE)
        try:
            rows = self._fetchall(sql, params)
        except sqlite3.OperationalError as e:
            # Indexer fails fast on schema-version mismatch, so an older
            # DB is impossible at runtime. Reaching this branch implies
            # corruption or a missing FTS shadow — log at warning so the
            # operator notices attachment retrieval has degraded to none.
            log.warning("Attachment keyword search unavailable: %s", type(e).__name__)
            return []
        results = [self._row_to_result(r) for r in rows]
        return self._best_per_thread(results)[:limit]

    def _matched_attachments(self, query: str, thread_ids: list[str]) -> dict[str, list[str]]:
        """Map each of ``thread_ids`` to the attachments whose filename or
        MIME type matches ``query``, strongest match first.

        The index holds MIME types too and query words are OR'd, so
        "proposal-quote pdf" matches every PDF; ranking by BM25 keeps the
        file the query named ahead of the generic hits.

        Used by ``hybrid_search(with_evidence=True)`` so per-thread
        evidence leads with the specific attachment the query named, not
        just any attachment in a thread that has one. The candidate
        threads already passed the search's filters, so none are
        re-applied. Cheap FTS5 query bounded by the candidates; falls
        back to an empty map on any error so the caller's main path is
        never blocked.
        """
        fts_query = _sanitize_fts_query(query)
        if not thread_ids or not fts_query:
            return {}
        placeholders = ",".join(["?"] * len(thread_ids))
        sql = (
            "SELECT a.thread_id, a.attachment_id, bm25(attachments_fts) AS score "
            "FROM attachments_fts "
            "JOIN attachments a ON attachments_fts.rowid = a.fts_rowid "
            "WHERE attachments_fts MATCH ? "
            f"AND a.thread_id IN ({placeholders}) "  # nosec B608
            "ORDER BY score"
        )
        try:
            rows = self._fetchall(sql, [fts_query, *thread_ids])
        except sqlite3.Error as e:
            log.warning("Attachment match lookup failed; skipping bias: %s", type(e).__name__)
            return {}
        matched: dict[str, list[str]] = {}
        for r in rows:
            ranked = matched.setdefault(r["thread_id"], [])
            if r["attachment_id"] not in ranked:
                ranked.append(r["attachment_id"])
        return matched

    @staticmethod
    def _append_thread_filter_sql(
        where_clauses: list[str],
        params: list,
        *,
        folders: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
    ) -> None:
        if folders:
            _append_folder_membership_sql(where_clauses, params, "t.thread_id", folders)
        date_from_iso, date_to_iso = _normalize_date_range(date_from, date_to)
        if date_from_iso is not None:
            where_clauses.append("t.date_last >= ?")
            params.append(date_from_iso)
        if date_to_iso is not None:
            where_clauses.append("t.date_first <= ?")
            params.append(date_to_iso)
        if has_attachments is not None:
            where_clauses.append("t.has_attachments = ?")
            params.append(1 if has_attachments else 0)

    @staticmethod
    def _best_per_thread(results: list[ThreadResult]) -> list[ThreadResult]:
        """Collapse to one row per ``thread_id``, keeping the best score.

        The chunk and attachment FTS lanes can return many rows for the
        same thread (one row per matching chunk). Take the row with the
        lowest BM25 score (lower = better in FTS5) per thread, then
        return the surviving rows in the original BM25 order so the
        caller's ``LIMIT`` slice picks the strongest threads. Unlike a
        first-seen-wins dedupe, this guarantees the kept row carries the
        thread's best chunk score.
        """
        best_score: dict[str, float] = {}
        best_idx: dict[str, int] = {}
        for index, result in enumerate(results):
            if result.thread_id not in best_score or result.score < best_score[result.thread_id]:
                best_score[result.thread_id] = result.score
                best_idx[result.thread_id] = index
        # Preserve original input order — results came in already sorted
        # by ``score`` ASC from the SQL, so threads with their best
        # chunk encountered first stay near the top.
        kept: list[ThreadResult] = []
        seen: set[str] = set()
        for index, result in enumerate(results):
            if best_idx.get(result.thread_id) != index:
                continue
            if result.thread_id in seen:
                continue
            seen.add(result.thread_id)
            kept.append(result)
        return kept

    def _like_fallback(
        self,
        query: str,
        limit: int,
        folders: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
    ) -> list[ThreadResult]:
        # The query is a literal substring: escape LIKE's wildcards and the
        # escape character itself so ``_`` and ``%`` match only themselves
        # (#333). Every LIKE below names the same ``ESCAPE`` character.
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        where_clauses = [
            "(subject LIKE ? ESCAPE '\\' OR body_text LIKE ? ESCAPE '\\' "
            "OR participants LIKE ? ESCAPE '\\')"
        ]
        params: list = [pattern, pattern, pattern]
        if folders:
            _append_folder_membership_sql(where_clauses, params, "thread_id", folders)
        # See ``_keyword_search`` for why date bounds are normalized before
        # being pushed into SQL.
        date_from_iso, date_to_iso = _normalize_date_range(date_from, date_to)
        if date_from_iso is not None:
            where_clauses.append("date_last >= ?")
            params.append(date_from_iso)
        if date_to_iso is not None:
            where_clauses.append("date_first <= ?")
            params.append(date_to_iso)
        if has_attachments is not None:
            where_clauses.append("has_attachments = ?")
            params.append(1 if has_attachments else 0)

        # Same reasoning as _keyword_search: WHERE clauses are literals,
        # user values are bound via ``?``. nosec B608.
        sql = (
            "SELECT "
            "thread_id, subject, participants, senders, folder, "
            "date_first, date_last, message_ids, "
            "snippet, has_attachments, body_text, display_subject, 0.0 AS score "
            "FROM threads "
            "WHERE " + " AND ".join(where_clauses) + " "  # nosec B608
            "ORDER BY date_last DESC LIMIT ?"
        )
        params.append(limit)

        try:
            rows = self._fetchall(sql, params)
            return [self._row_to_result(r) for r in rows]
        except sqlite3.OperationalError as e:
            log.warning("LIKE fallback search error: %s", type(e).__name__)
            return []

    def _chunk_vector_search(self, embedding: list[float], limit: int) -> list[ChunkResult] | None:
        """Return per-message chunks whose vectors are closest to ``embedding``.

        Returns ``None`` when the lane itself fails (missing or corrupt
        vec table, malformed vector), distinct from ``[]`` for no matches.

        The chunk vec table is populated incrementally by the indexer.
        Threads with no chunks (empty bodies) simply do not appear in
        this lane and fall back to the coarse thread-vector lane in
        the hybrid fuse.
        """
        try:
            serialized = sqlite_vec.serialize_float32(embedding)
            # LEFT JOIN ``attachments`` so each attachment chunk carries
            # filename + MIME through to the LLM context. Body chunks
            # have ``c.attachment_id IS NULL`` and the JOIN yields
            # ``NULL`` for filename/MIME — handled in the dataclass
            # construction below.
            #
            # The JOIN anchors on the SINGLE representative occurrence
            # row per ``(attachment_id, claimant_id)`` pair (the one
            # with the lowest ``attachment_occurrence_id``). The
            # indexer permits the same content hash to be attached
            # under multiple filenames in one message (a user attaching
            # the same PDF twice with different display names); each
            # occurrence is its own row in ``attachments`` but only
            # ONE chunk set is stored per content hash, so a naive
            # ``ON (attachment_id, claimant_id)`` JOIN multiplies the
            # chunk by the occurrence count and emits non-deterministic
            # filename attribution. Picking the lowest occurrence id
            # gives a stable, deterministic choice and eliminates the
            # multiplication.
            rows = self._fetchall(
                """
                SELECT
                    c.chunk_id, COALESCE(m.message_id, c.claimant_id) AS message_id,
                    c.claimant_id, c.thread_id, c.chunk_index,
                    c.text, c.char_start, c.char_end, c.attachment_id,
                    a.filename AS attachment_filename,
                    a.content_type AS attachment_mime,
                    v.distance AS score
                FROM message_chunks_vec v
                JOIN message_chunks c ON c.chunk_id = v.chunk_id
                LEFT JOIN messages m ON m.claimant_id = c.claimant_id
                LEFT JOIN attachments a
                    ON a.attachment_occurrence_id = (
                        SELECT MIN(a2.attachment_occurrence_id)
                        FROM attachments a2
                        WHERE a2.attachment_id = c.attachment_id
                          AND a2.claimant_id = c.claimant_id
                    )
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY v.distance
                """,
                (serialized, min(limit, _SQLITE_VEC_MAX_K)),
            )
            return [_row_to_chunk_result(r) for r in rows if _has_valid_distance(r)]
        except (sqlite3.Error, ValueError) as e:
            # ``sqlite3.Error`` covers OperationalError (missing vec
            # table) and DatabaseError (corruption); ``ValueError`` is
            # raised by sqlite-vec on malformed embedding payloads. Any
            # other exception type is unexpected and should propagate.
            log.warning("Chunk vector search error: %s", type(e).__name__)
            return None

    def get_evidence_chunks_for_threads(
        self,
        thread_ids: list[str],
        embedding: list[float],
        per_thread_limit: int = 3,
        matched_attachments: dict[str, list[str]] | None = None,
    ) -> dict[str, list[ChunkResult]]:
        """Return up to ``per_thread_limit`` best-matching chunks per thread.

        Scans the chunks belonging to ``thread_ids`` directly (rather
        than filtering a global vector-search pool by thread_id),
        sorts each thread's chunks by similarity to ``embedding``, and
        returns the top ``per_thread_limit`` per thread.

        This shape matters for the ``hybrid_search(with_evidence=True)``
        path. The prior pool-reuse implementation depended on the
        thread's chunks happening to rank in the global chunk-vector
        top-K — meaning a thread won via BM25, thread-vector,
        sender/date filter, or attachment filename FTS could end up
        with empty ``evidence_chunks`` whenever its specific chunks
        didn't make the global pool. ``ask_mailbox``, ``get_evidence``
        and ``extract_from_emails`` read attachment text through these
        evidence chunks; the pool-reuse shape silently dropped it for
        any non-chunk-vec retrieval lane.

        Implementation reads only chunks belonging to the surfaced
        ``thread_ids`` and computes ``vec_distance_l2`` against each.
        For typical surfaced sets (5-50 threads × 1-100 chunks each)
        this is a small sequential scan and beats N per-thread KNN
        queries on round-trip overhead.

        ``matched_attachments``: per thread, the attachments whose
        filename / MIME matched the query, strongest first. Their chunks
        lead the per-thread slice in that order, then the thread's other
        attachment chunks,
        then body chunks — so the LLM sees the document the user named
        even when a body chunk, or another attachment's chunk, has
        higher dense similarity. Remembering only the thread let the cap
        keep unrelated attachments and drop the one that matched.
        """
        if not thread_ids:
            return {}
        try:
            serialized = sqlite_vec.serialize_float32(embedding)
            placeholders = ",".join(["?"] * len(thread_ids))
            # Composed SQL — the ``IN (?, ...)`` is built from a
            # placeholder count, not from thread_id values, and every
            # bound parameter goes through the driver. nosec B608.
            #
            # LEFT JOIN ``attachments`` on the SINGLE representative
            # occurrence row per ``(attachment_id, claimant_id)`` (the
            # one with the lowest ``attachment_occurrence_id``). The
            # indexer allows the same content hash to be attached
            # under multiple display filenames in one message but
            # stores ONLY ONE chunk set per content hash, so a naive
            # ``ON (attachment_id, claimant_id)`` JOIN multiplies the
            # chunk row by the occurrence count. See
            # ``_chunk_vector_search`` for the full rationale; both
            # call sites apply the same fix. Body chunks have
            # ``c.attachment_id IS NULL`` so the subquery returns
            # NULL and the LEFT JOIN yields NULL filename/MIME.
            sql = (
                "SELECT c.chunk_id, COALESCE(m.message_id, c.claimant_id) AS message_id, "
                "c.claimant_id, c.thread_id, c.chunk_index, "
                "c.text, c.char_start, c.char_end, c.attachment_id, c.message_date, "
                "a.filename AS attachment_filename, "
                "a.content_type AS attachment_mime, "
                f"{_SOURCE_COLUMNS}, "
                # The message's own first From entry, for per-passage
                # attribution in prompts (#284).
                # Name and address are cut to ``_SENDER_FETCH_CHARS`` here
                # so a huge display name is not copied onto every row.
                "(SELECT CASE WHEN p.name IS NOT NULL AND p.name != '' "
                f"  THEN substr(p.name, 1, {_SENDER_FETCH_CHARS}) || ' <' "
                f"    || substr(p.address, 1, {_SENDER_FETCH_CHARS}) || '>' "
                f"  ELSE substr(p.address, 1, {_SENDER_FETCH_CHARS}) END "
                "  FROM message_participants p "
                "  WHERE p.claimant_id = c.claimant_id AND p.role = 'from' "
                "  ORDER BY p.address LIMIT 1) AS message_sender, "
                "vec_distance_l2(v.embedding, ?) AS score "
                "FROM message_chunks c "
                "JOIN message_chunks_vec v ON c.chunk_id = v.chunk_id "
                "LEFT JOIN messages m ON m.claimant_id = c.claimant_id "
                "LEFT JOIN attachments a "
                "  ON a.attachment_occurrence_id = ( "
                "       SELECT MIN(a2.attachment_occurrence_id) "
                "       FROM attachments a2 "
                "       WHERE a2.attachment_id = c.attachment_id "
                "         AND a2.claimant_id = c.claimant_id "
                "  ) "
                f"WHERE c.thread_id IN ({placeholders}) "  # nosec B608
                "ORDER BY score ASC"
            )
            rows = self._fetchall(sql, [serialized, *thread_ids])
        except (sqlite3.Error, ValueError) as e:
            # Same catch surface as ``_chunk_vector_search`` — missing
            # vec extension, corrupt vec row, malformed serialised
            # embedding. Degrade to empty evidence rather than failing
            # the whole hybrid_search call; coarse retrieval still
            # works and the LLM falls back to ``body_text``.
            log.warning("Per-thread evidence chunk fetch failed: %s", type(e).__name__)
            return {tid: [] for tid in thread_ids}

        # First pass: gather ALL chunks per thread (still ordered by
        # vec_distance ASC within each thread) so the attachment-first
        # reorder below has the full set to work with. The cap to
        # ``per_thread_limit`` happens after the reorder.
        all_chunks: dict[str, list[ChunkResult]] = {tid: [] for tid in thread_ids}
        for r in rows:
            if _has_valid_distance(r):
                all_chunks[r["thread_id"]].append(_row_to_chunk_result(r))

        matched_by_thread = matched_attachments or {}
        grouped: dict[str, list[ChunkResult]] = {}
        for tid, chunks in all_chunks.items():
            matched = matched_by_thread.get(tid)
            if matched:
                # Matched attachments by match strength, then other
                # attachments, then body; the stable sort keeps
                # vec_distance order within each group.
                rank = {attachment_id: i for i, attachment_id in enumerate(matched)}
                others = len(matched)
                ordered = sorted(
                    chunks,
                    key=lambda c: (
                        rank.get(c.attachment_id, others) if c.attachment_id else others + 1
                    ),
                )
            else:
                ordered = chunks
            grouped[tid] = ordered[:per_thread_limit]
        return grouped

    def get_recent_chunks_for_thread(
        self,
        thread_id: str,
        limit: int = 6,
    ) -> list[ChunkResult]:
        """Return the BODY chunks of ``thread_id``'s latest-dated messages.

        Used by ``summarize_thread`` / timeline-style intelligence tools
        that need "what does the thread say lately" — NOT "what matches
        a query." The stored ``body_text`` is front-preserving and
        token-capped, so a long thread that crosses ``THREAD_BODY_TEXT_MAX_TOKENS``
        silently drops its newest replies. The chunk store carries every
        message in full, so reading the chunks of the latest-dated
        messages recovers the missing context.

        Attachment chunks (rows with a non-NULL ``attachment_id``) are
        deliberately excluded via ``c.attachment_id IS NULL``.
        ``summarize_thread`` is a body summary that never reads
        attachment text (the attachment-reading tools are
        ``ask_mailbox``, ``get_evidence``, ``search_attachments`` and
        ``extract_from_emails``), so surfacing attachment extracts here
        would silently broaden which indexed content can leave the host
        for a remote inference endpoint.

        Returned chunks are in chronological (oldest-first by message
        date within the selected tail) order so the LLM prompt reads naturally as a
        timeline. Caller can render them via ``_summarize_context``.

        Ordering: ``c.message_date DESC, c.chunk_index DESC``.
        ``message_date`` is the indexed message date stored at
        chunk-write: the sender-supplied ``Date:`` header, or the
        indexer's ingest time when that header is missing or
        unparseable. It is not an IMAP delivery timestamp, but it is
        stable across reindex, reap-rebuild, dead-letter retry, and
        recovery-sweep paths (unlike ``chunked_at``, the chunker's
        wall-clock at insert, which this query does not use).
        ``chunk_index DESC`` tiebreaks chunks of the same message so the
        last chunk emitted by the chunker comes first in selection.
        Selection picks the latest-dated ``limit`` chunks, then the result
        is reversed in Python for ascending display order.

        Body-only filter: because attachment chunks are excluded, no
        ``attachments`` JOIN is needed — ``attachment_filename`` and
        ``attachment_mime`` are emitted as literal ``NULL`` so the row
        shape still matches ``_row_to_chunk_result``.
        """
        if limit <= 0:
            return []
        try:
            rows = self._fetchall(
                """
                SELECT c.chunk_id, COALESCE(m.message_id, c.claimant_id) AS message_id,
                       c.claimant_id, c.thread_id, c.chunk_index,
                       c.text, c.char_start, c.char_end, c.attachment_id,
                       NULL AS attachment_filename,
                       NULL AS attachment_mime,
                       0.0 AS score
                FROM message_chunks c
                LEFT JOIN messages m ON m.claimant_id = c.claimant_id
                WHERE c.thread_id = ?
                  AND c.attachment_id IS NULL
                ORDER BY c.message_date DESC,
                         c.chunk_index DESC
                LIMIT ?
                """,
                (thread_id, limit),
            )
        except sqlite3.Error as e:
            log.warning("Recent-chunks lookup failed: %s", type(e).__name__)
            return []
        chunks = [_row_to_chunk_result(r) for r in rows]
        # Reverse for chronological display: SELECT picked the newest
        # ``limit`` chunks; we want them oldest-first in the prompt so
        # the timeline reads naturally.
        chunks.reverse()
        return chunks

    def _vector_lanes(
        self,
        embedding: list[float],
        thread_k: int,
        chunk_k: int,
        target: int,
        **filters,
    ) -> tuple[list[ThreadResult] | None, list[ChunkResult] | None]:
        """Run the thread- and chunk-vector lanes for one query embedding.

        Unfiltered, each lane runs once with the given ``k``. With a
        post-fusion filter active (#286), the KNN windows are global and
        the filters apply afterwards, so a selective filter can leave a
        lane holding few or no eligible threads. Each lane then doubles
        its ``k`` and re-queries until its window holds ``target``
        eligible threads, the window comes back short (the table is
        exhausted), or ``k`` reaches sqlite-vec's ``_SQLITE_VEC_MAX_K``.
        Ranked search stays ranked, not exhaustive: an eligible thread
        beyond the 4096 nearest rows of a lane is still not seen by it.

        Each query opens its own short-lived connection, and the caller
        embeds once before this runs, so no read transaction spans the
        provider call and the expansion never re-embeds.

        Each lane's timing stage covers its first query and every
        expansion step, eligibility checks included; the
        ``*_expansions`` counts give the number of re-queries.
        """
        with timings.stage("thread_vec"):
            vec = self._vector_search(embedding, thread_k)
        with timings.stage("chunk_vec"):
            chunks = self._chunk_vector_search(embedding, chunk_k)
        if not self._has_post_fusion_filter(**filters):
            return vec, chunks

        timings.count("thread_vec_expansions", 0)
        thread_k = min(thread_k, _SQLITE_VEC_MAX_K)
        with timings.stage("thread_vec"):
            while (
                vec is not None
                and len(vec) >= thread_k
                and thread_k < _SQLITE_VEC_MAX_K
                and len(self._apply_filters(vec, **filters)) < target
            ):
                thread_k = min(thread_k * 2, _SQLITE_VEC_MAX_K)
                vec = self._vector_search(embedding, thread_k)
                timings.count("thread_vec_expansions", 1)

        timings.count("chunk_vec_expansions", 0)
        chunk_k = min(chunk_k, _SQLITE_VEC_MAX_K)
        with timings.stage("chunk_vec"):
            while (
                chunks is not None
                and len(chunks) >= chunk_k
                and chunk_k < _SQLITE_VEC_MAX_K
                and self._eligible_chunk_threads(chunks, filters) < target
            ):
                chunk_k = min(chunk_k * 2, _SQLITE_VEC_MAX_K)
                chunks = self._chunk_vector_search(embedding, chunk_k)
                timings.count("chunk_vec_expansions", 1)
        return vec, chunks

    def _eligible_chunk_threads(self, chunks: list[ChunkResult], filters: dict) -> int:
        """How many distinct parent threads of ``chunks`` pass ``filters``."""
        thread_ids = list(dict.fromkeys(c.thread_id for c in chunks))
        threads = self._get_threads(thread_ids)
        return len(self._apply_filters(list(threads.values()), **filters))

    def _vector_search(self, embedding: list[float], limit: int) -> list[ThreadResult] | None:
        """Thread-vector lane. ``None`` means the lane failed (see
        ``_chunk_vector_search``); ``[]`` means no matches."""
        try:
            serialized = sqlite_vec.serialize_float32(embedding)
            rows = self._fetchall(
                """
                SELECT
                    t.thread_id, t.subject, t.participants, t.senders, t.folder,
                    t.date_first, t.date_last, t.message_ids,
                    t.snippet, t.has_attachments, t.body_text, t.display_subject,
                    v.distance AS score
                FROM threads_vec v
                JOIN threads t ON v.thread_id = t.thread_id
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY v.distance
            """,
                (serialized, min(limit, _SQLITE_VEC_MAX_K)),
            )
            # Tag the dense thread lane so RRF fusion can record it as
            # ``thread_vec`` provenance on the surviving thread row.
            return _tag_lane_ranks(
                [self._row_to_result(r) for r in rows if _has_valid_distance(r)], "thread_vec"
            )
        except (sqlite3.Error, ValueError) as e:
            # Same catch surface as ``_chunk_vector_search`` —
            # ``sqlite3.Error`` for table/connection issues, ``ValueError``
            # for malformed serialised vectors. Other exception types
            # should propagate so corrupt-state bugs aren't masked.
            log.warning("Vector search error: %s", type(e).__name__)
            return None

    def _reciprocal_rank_fusion(
        self,
        bm25: list[ThreadResult],
        vec: list[ThreadResult],
        chunks: list[ChunkResult] | None = None,
        k: int = 60,
    ) -> list[ThreadResult]:
        """Merge ranked lanes via RRF. Higher score = better.

        Three lanes participate when ``chunks`` is supplied:
        BM25 keyword over thread bodies, dense vector over thread-level
        embeddings, and dense vector over per-message chunks. Chunk
        hits are lifted to their parent ``thread_id`` — the best (lowest
        rank) chunk per thread is what counts toward thread ranking, so
        a thread doesn't accumulate inflated score from many similar
        sibling chunks. Threads found only via the chunk lane are still
        materialized into the result set via a thread fetch so the
        merged list never references a thread the caller can't display.
        """
        scores: dict[str, float] = {}
        index: dict[str, ThreadResult] = {}
        # Side accumulator for lane provenance, keyed by thread_id.
        # ``index`` is last-write-wins per thread, so a thread's
        # ``thread_fts`` ranks (carried on the bm25 copy) would be lost
        # when the vec copy overwrites the entry. Merging into this dict
        # and stamping the survivor at the end keeps every lane the
        # thread appeared in. Pure observability — never feeds scoring.
        lane_ranks: dict[str, dict[str, int]] = {}

        for rank, result in enumerate(bm25):
            scores[result.thread_id] = scores.get(result.thread_id, 0) + 1.0 / (k + rank + 1)
            index[result.thread_id] = result
            lane_ranks.setdefault(result.thread_id, {}).update(result.lane_ranks)

        for rank, result in enumerate(vec):
            scores[result.thread_id] = scores.get(result.thread_id, 0) + 1.0 / (k + rank + 1)
            index[result.thread_id] = result
            lane_ranks.setdefault(result.thread_id, {}).update(result.lane_ranks)

        if chunks:
            seen_threads: set[str] = set()
            chunk_only: list[str] = []
            for rank, chunk in enumerate(chunks):
                tid = chunk.thread_id
                # Best-rank-only contribution: skip any later (worse-
                # ranked) chunk from a thread we've already credited.
                # Without this, a thread with ten near-duplicate chunks
                # would drown out a thread with one strong chunk.
                if tid in seen_threads:
                    continue
                seen_threads.add(tid)
                scores[tid] = scores.get(tid, 0) + 1.0 / (k + rank + 1)
                lane_ranks.setdefault(tid, {})["chunk_vec"] = rank
                if tid not in index:
                    chunk_only.append(tid)
            # Materialize chunk-only threads in one batched fetch. A
            # missing thread row is skipped silently (shouldn't happen
            # in steady state — chunk rows live and die with their
            # thread — but defensive against stale state mid-reap).
            index.update(self._get_threads(chunk_only))

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        results = []
        for thread_id, score in ranked:
            if thread_id not in index:
                continue
            r = index[thread_id]
            r.score = score
            r.lane_ranks = lane_ranks.get(thread_id, {})
            results.append(r)
        return results

    @staticmethod
    def _reciprocal_rank_fusion_threads(
        *lanes: list[ThreadResult],
        k: int = 60,
    ) -> list[ThreadResult]:
        """Merge ranked ThreadResult lanes via RRF.

        Used by keyword search, where thread-body FTS, chunk-text FTS, and
        attachment filename/MIME FTS all already materialize parent threads.

        Each lane's results are expected to carry their pre-fusion rank in
        ``lane_ranks`` (stamped by ``_tag_lane_ranks`` at the call site);
        those per-lane ranks are merged onto the surviving thread row so
        downstream callers can see which keyword sub-lanes matched.
        """
        scores: dict[str, float] = {}
        index: dict[str, ThreadResult] = {}
        lane_ranks: dict[str, dict[str, int]] = {}
        for lane in lanes:
            for rank, result in enumerate(lane):
                scores[result.thread_id] = scores.get(result.thread_id, 0.0) + 1.0 / (k + rank + 1)
                index.setdefault(result.thread_id, result)
                lane_ranks.setdefault(result.thread_id, {}).update(result.lane_ranks)

        results: list[ThreadResult] = []
        for thread_id, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            result = index[thread_id]
            result.score = score
            result.lane_ranks = lane_ranks.get(thread_id, {})
            results.append(result)
        return results

    def _apply_filters(
        self,
        results: list[ThreadResult],
        folders: list[str] | None = None,
        from_addr: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
        participant: str | None = None,
        authority_class: str | None = None,
    ) -> list[ThreadResult]:
        date_from_dt, date_to_dt = _parse_date_range(date_from, date_to)

        filtered = results
        if from_addr:
            fa = from_addr.lower()
            # Filter by sender (the From-only subset, not all participants).
            filtered = [r for r in filtered if _matches_sender(r, fa)]
        if participant:
            # Filter by anyone on the thread — From, To, or Cc. Distinct
            # from ``from_addr``, which is sender-only. Runs post-fusion
            # like the sender filter; ``_has_post_fusion_filter`` counts
            # it so the caller oversampled raw candidates and a deep
            # match still survives the filter.
            pa = participant.lower()
            filtered = [r for r in filtered if _matches_participant(r, pa)]
        # Compare as datetimes rather than as strings: a user-supplied
        # date-only ``date_to="2024-12-31"`` was previously compared against
        # stored ISO timestamps like ``"2024-12-31T10:00:00+00:00"`` and
        # excluded the entire last day because the stored string sorts
        # lexicographically greater than the bare date. ``_parse_filter_date``
        # promotes date-only values to start/end of day in UTC.
        if date_from_dt is not None:
            filtered = [r for r in filtered if r.date_last >= date_from_dt]
        if date_to_dt is not None:
            filtered = [r for r in filtered if r.date_first <= date_to_dt]
        if has_attachments is not None:
            filtered = [r for r in filtered if r.has_attachments == has_attachments]
        if folders and filtered:
            # Last, so the lookup covers only the survivors of the
            # in-memory filters.
            members = self._threads_in_folders([r.thread_id for r in filtered], folders)
            filtered = [r for r in filtered if r.thread_id in members]
        if authority_class and filtered:
            # A pure filter: survivors keep their fused order and score.
            sent = self._threads_sent_by_class([r.thread_id for r in filtered], authority_class)
            filtered = [r for r in filtered if r.thread_id in sent]
        return filtered

    def _threads_sent_by_class(self, thread_ids: list[str], authority_class: str) -> set[str]:
        """The subset of ``thread_ids`` with a message whose From sender
        the indexer classified as ``authority_class``. Batched like
        ``_threads_in_folders``."""
        found: set[str] = set()
        with closing(self._connect()) as conn:
            for start in range(0, len(thread_ids), _IN_CLAUSE_BATCH_SIZE):
                batch = thread_ids[start : start + _IN_CLAUSE_BATCH_SIZE]
                id_marks = ",".join("?" * len(batch))
                rows = conn.execute(
                    "SELECT DISTINCT thread_id FROM messages "  # nosec B608
                    f"WHERE thread_id IN ({id_marks}) "
                    f"AND claimant_id IN ({_SENDER_CLASS_MESSAGES})",
                    [*batch, authority_class],
                ).fetchall()
                found.update(r["thread_id"] for r in rows)
        return found

    def _threads_in_folders(self, thread_ids: list[str], folders: list[str]) -> set[str]:
        """The subset of ``thread_ids`` with a message filed in one of
        ``folders``: the same per-message membership ``list_threads``
        uses (#415). One connection; the id list is batched under
        ``_IN_CLAUSE_BATCH_SIZE``."""
        found: set[str] = set()
        folder_marks = ",".join("?" * len(folders))
        with closing(self._connect()) as conn:
            for start in range(0, len(thread_ids), _IN_CLAUSE_BATCH_SIZE):
                batch = thread_ids[start : start + _IN_CLAUSE_BATCH_SIZE]
                id_marks = ",".join("?" * len(batch))
                rows = conn.execute(
                    "SELECT DISTINCT thread_id FROM messages "  # nosec B608
                    f"WHERE thread_id IN ({id_marks}) AND folder IN ({folder_marks})",
                    [*batch, *folders],
                ).fetchall()
                found.update(r["thread_id"] for r in rows)
        return found

    # -------------------------------------------------------------------------
    # Direct lookups
    # -------------------------------------------------------------------------

    def get_thread(self, thread_id: str) -> ThreadResult | None:
        row = self._fetchone("SELECT * FROM threads WHERE thread_id = ?", (thread_id,))
        return self._row_to_result(row) if row else None

    def _get_threads(self, thread_ids: list[str]) -> dict[str, ThreadResult]:
        """Thread rows for ``thread_ids`` keyed by id, over one connection.

        The IN list is batched under ``_IN_CLAUSE_BATCH_SIZE``; ids with
        no row are absent from the result.
        """
        found: dict[str, ThreadResult] = {}
        if not thread_ids:
            return found
        with closing(self._connect()) as conn:
            for start in range(0, len(thread_ids), _IN_CLAUSE_BATCH_SIZE):
                batch = thread_ids[start : start + _IN_CLAUSE_BATCH_SIZE]
                placeholders = ",".join("?" * len(batch))
                rows = conn.execute(
                    f"SELECT * FROM threads WHERE thread_id IN ({placeholders})",  # nosec B608
                    batch,
                ).fetchall()
                for row in rows:
                    found[row["thread_id"]] = self._row_to_result(row)
        return found

    def get_thread_page(
        self, thread_id: str, *, offset: int, limit: int, body_char_limit: int
    ) -> ThreadPage | None:
        """One page of a thread's messages, oldest first, each with its
        own headers and its body cut at ``body_char_limit``.

        Every read shares one snapshot, so an indexer commit landing
        mid-call cannot mix two database states. Only the page's messages
        and the chunks inside the limit are read.
        """
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
            if row is None:
                return None
            total = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
            messages = _message_records(
                conn, "m.thread_id = ?", (thread_id,), limit=limit, offset=offset
            )
            bodies = _message_bodies(conn, [m.claimant_id for m in messages], body_char_limit)
            has_bodies = bool(bodies) or bool(
                conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM message_chunks "
                    "WHERE thread_id = ? AND attachment_id IS NULL)",
                    (thread_id,),
                ).fetchone()[0]
            )
        return ThreadPage(
            thread=self._row_to_result(row),
            total_messages=total,
            offset=offset,
            messages=messages,
            bodies=bodies,
            has_bodies=has_bodies,
        )

    def get_message_view(self, identifier: str) -> MessageView | AmbiguousMessageId | None:
        """One message's headers, its thread, and its full body, from one
        read snapshot.

        ``identifier`` is a claimant ID or a bare Message-ID. One that
        names several messages (#217) returns an ``AmbiguousMessageId``
        listing them instead of one of them: a bare Message-ID several
        indexed files claim, or a crafted Message-ID equal to another
        message's claimant ID.
        """
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            records = _message_records(
                conn, "m.claimant_id = ? OR m.message_id = ?", (identifier, identifier)
            )
            if len(records) > 1:
                return AmbiguousMessageId(message_id=identifier, claimants=records)
            if not records:
                return None
            record = records[0]
            row = conn.execute(
                "SELECT * FROM threads WHERE thread_id = ?", (record.thread_id,)
            ).fetchone()
            if row is None:
                return None
            others = [
                r["claimant_id"]
                for r in conn.execute(
                    "SELECT claimant_id FROM messages WHERE message_id = ? AND claimant_id != ? "
                    "ORDER BY claimant_id",
                    (record.message_id, record.claimant_id),
                )
            ]
            body = _message_bodies(conn, [record.claimant_id], None).get(record.claimant_id)
        return MessageView(
            record=record, thread=self._row_to_result(row), body=body, other_claimants=others
        )

    def list_threads(
        self,
        folder: str = "INBOX",
        filter_type: str = "all",
        limit: int = 20,
        offset: int = 0,
    ) -> list[ThreadResult]:
        """Threads with at least one message in ``folder``, newest first.

        Membership comes from each message's own folder (``messages``),
        not ``threads.folder``: that is the folder of the message that
        started the thread, so a folder holding only replies to threads
        started elsewhere would never list (#308). The returned
        ``folder`` stays the thread's representative folder, set when
        the thread was first indexed.
        """
        if filter_type != "all":
            raise ValueError("filter_type must be 'all'; unread/flagged state is not indexed")
        rows = self._fetchall(
            """
            SELECT * FROM threads
            WHERE thread_id IN (SELECT thread_id FROM messages WHERE folder = ?)
            ORDER BY date_last DESC
            LIMIT ? OFFSET ?
        """,
            (folder, limit, offset),
        )
        return [self._row_to_result(r) for r in rows]

    def ping(self) -> None:
        """Trivial query used as a liveness probe by the HTTP /health route.

        Exercises the read-only SQLite connection without touching any
        application table so it stays cheap under a per-30s healthcheck
        interval and surfaces a missing/unreadable DB as an exception.
        """
        self._fetchone("SELECT 1")

    def get_embedding_dim(self) -> int | None:
        """Return the embedding dimension declared by ``message_chunks_vec``.

        The indexer writes vec0 virtual tables with a dim baked into
        the CREATE statement (``embedding FLOAT[N]``). Reading that
        value lets mcp-server validate query vectors before they reach
        sqlite-vec — otherwise a misconfigured ``EMBED_MODEL`` whose
        output dim doesn't match the index produces an
        ``OperationalError`` that the broad ``except`` in
        ``_chunk_vector_search`` swallows, silently degrading search
        to keyword-only.

        Returns ``None`` when ``message_chunks_vec`` doesn't exist
        yet — a fresh install where mcp-server starts before the
        indexer has run its schema migrations. Callers treat ``None``
        as "skip validation"; semantic / hybrid queries then fail at
        the DB layer with the missing-table message, which is the
        right operator-visible signal for that state.
        """
        row = self._fetchone(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='message_chunks_vec'"
        )
        if row is None:
            return None
        match = re.search(r"FLOAT\s*\[\s*(\d+)\s*\]", row["sql"], re.IGNORECASE)
        if match is None:
            return None
        return int(match.group(1))

    def get_mailbox_status(self) -> dict:
        """Index counts, queue depth, and the indexer's ``ingestion_state``
        row (``None`` until the indexer first reports), read in one
        snapshot so the counts and the queue agree.

        Queue rows are ``pending`` (not yet failed), ``retrying``
        (failed at least once, will retry), or ``dead`` (gave up). A job
        deferred during an embedder outage keeps ``attempts = 0`` but
        records its failure class, so the class marks it as retrying; a
        dead job requeued by ``make requeue-dead`` clears both and is
        pending again.
        """
        stats: dict = {}
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            stats["total_threads"] = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            stats["total_messages"] = conn.execute(
                "SELECT COUNT(*) FROM message_thread_map"
            ).fetchone()[0]
            row = conn.execute("SELECT MIN(date_first), MAX(date_last) FROM threads").fetchone()
            stats["oldest_message"] = row[0]
            stats["newest_message"] = row[1]
            queue = conn.execute(
                """
                SELECT
                    COALESCE(SUM(status = 'queued' AND NOT (attempts > 0 OR last_error_class IS NOT NULL)), 0),
                    COALESCE(SUM(status = 'queued' AND (attempts > 0 OR last_error_class IS NOT NULL)), 0),
                    COALESCE(SUM(status = 'dead'), 0)
                FROM indexing_jobs
                """
            ).fetchone()
            stats["queue"] = {"pending": queue[0], "retrying": queue[1], "dead": queue[2]}
            state = conn.execute(
                "SELECT sync_completed_at, sync_interval_secs, indexer_seen_at FROM ingestion_state"
            ).fetchone()
            stats["ingestion"] = dict(state) if state is not None else None
            conn.execute("COMMIT")
        return stats

    def list_folders(self) -> list[dict]:
        """Folders holding at least one indexed message, with the number
        of distinct threads that have a message in each — the same set
        ``list_threads(folder=...)`` pages through. A thread with
        messages in several folders counts once in each of them.
        """
        rows = self._fetchall("""
            SELECT folder, COUNT(DISTINCT thread_id) AS thread_count
            FROM messages
            GROUP BY folder
            ORDER BY thread_count DESC, folder
        """)
        return [{"name": r["folder"], "thread_count": r["thread_count"]} for r in rows]

    def find_contact(
        self,
        query: str,
        limit: int = 10,
        *,
        senders_only: bool = False,
        folders: list[str] | None = None,
    ) -> list[dict]:
        """Resolve a name / address / domain fragment to indexed contacts.

        Matches the query against each indexed ``message_participants``
        row's address (lowercased) or display name (Unicode caseless,
        both sides casefolded), then
        aggregates every row of each matched canonical email (not only
        the matching rows), so the same contact across many threads
        collapses to one row, with ``thread_count`` reflecting how many
        threads they appeared on and ``names`` every display name they
        were written with. Same-thread duplicates do not double-count.

        ``senders_only`` instead aggregates ``threads.senders`` — each
        message's primary From author as the thread records it — with
        the display names that author's From rows carry on those same
        threads (``threads.senders`` keeps only one per address; see
        ``_aggregate_senders`` for the thread-level limit). That is
        exactly the set ``search_emails(from_addr=...)`` filters on, so a
        resolved address always matches that filter; ranking over the
        participant table's From rows could promote a secondary author
        of a multi-author From, or one standing behind an unparseable
        primary, and return nothing. Use this when the caller's intent is
        "filter to messages this person SENT" rather than "find this
        person's address anywhere in the index": the broader
        participants ranking can promote a frequent recipient/CC-only
        contact over the actual sender. The default is
        ``senders_only=False`` because the standalone find_contact tool
        is also used for general "find this person's email" lookups
        where recipient-only matches are still useful.

        With ``senders_only``, only threads in the search scope count:
        ``folders`` when given, else the default exclusion
        (``_default_folder_scope``). Otherwise a sender whose threads
        are all in Trash could win the lookup and then be filtered out
        of the search it feeds (#441). ``folders`` is ignored without
        ``senders_only``.

        Exists so callers (the LLM via the MCP tool) can map a
        display-name fragment (``"Jane Smith"``) to a canonical
        address (``"jsmith@example.com"``) before invoking
        ``search_emails(from_addr=...)``. Without this step a
        borderline model often abdicates when given a role label or
        partial name.
        """
        if not query or not query.strip():
            return []
        # Addresses are stored lowercased; names compare casefolded
        # (Unicode caseless, so ``STRASSE`` matches ``Straße``).
        needle = query.strip().lower()
        name_needle = query.strip().casefold()

        with closing(self._connect()) as conn:
            # One read transaction: every query below sees the same
            # snapshot even while the indexer commits.
            conn.execute("BEGIN")
            if senders_only:
                scope = self._default_folder_scope(folders, conn)
                by_email = (
                    {} if scope == [] else _aggregate_senders(conn, needle, name_needle, scope)
                )
            else:
                by_email = _aggregate_participants(conn, needle, name_needle)
            # Most-active contact first; tiebreak on email so the order is
            # stable across runs (important for both eval reproducibility
            # and the unit tests below).
            ranked = sorted(by_email.items(), key=lambda item: (-len(item[1]["threads"]), item[0]))[
                :limit
            ]
            entities = _contact_entities(conn, [addr for addr, _ in ranked])
            conn.rollback()

        contacts = []
        for addr, bucket in ranked:
            entity = entities.get(addr)
            contacts.append(
                {
                    "email": addr,
                    "names": sorted(bucket["names"]),
                    "thread_count": len(bucket["threads"]),
                    "organization": entity["organization"] if entity else None,
                    "authority_class": entity["authority_class"] if entity else "unclassified",
                    "authority_rule": entity["authority_rule"] if entity else None,
                }
            )
        return contacts

    def query_messages(
        self,
        *,
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
        limit: int = 25,
        cursor: str | None = None,
    ) -> MessagePage:
        """Enumerate every message matching all given predicates.

        Unlike the search methods this does not rank: the result is the
        exact matching set, newest ``sent_at`` first (``claimant_id``
        breaks ties), with ``total_matches`` counted over the whole set
        and keyset pagination through ``cursor``. Blank predicates are
        ignored.

        - ``sender`` (From), ``recipient`` (To or Cc), ``participant``
          (any role): see ``address_match_mode``.
        - ``subject``: Unicode caseless (casefolded) substring of the
          message's own subject.
        - ``text``: every word must occur in the message's indexed body
          (FTS word match with stemming, any chunk; attachment text and
          stripped quoted replies are not searched).
        - ``folder``: exact folder name. Without it, messages filed in a
          ``DEFAULT_EXCLUDED_FOLDERS`` folder are left out.
        - ``date_from`` / ``date_to``: inclusive ``sent_at`` bounds;
          date-only values cover the whole UTC day.
        - ``has_attachments``: the message's own attachment flag.
        - ``authority_class``: the class the indexer gave the message's
          From sender (``AUTHORITY_CLASSES``).

        Raises ``ValueError`` for an invalid date, a ``text`` with no
        words or more than ``_MAX_TEXT_TERMS``, or a malformed / foreign
        cursor.
        """
        sender, recipient, participant, subject, text, folder = (
            v.strip() if v and v.strip() else None
            for v in (sender, recipient, participant, subject, text, folder)
        )
        date_from_iso, date_to_iso = _normalize_date_range(date_from, date_to)
        authority_class = normalize_authority_class(authority_class)

        where: list[str] = []
        params: list = []
        for value, roles in (
            (sender, ("from",)),
            (recipient, ("to", "cc")),
            (participant, ("from", "to", "cc")),
        ):
            if value:
                where.append(_participant_clause(value, roles, params))
        if subject:
            where.append("instr(mcp_casefold(m.subject), ?) > 0")
            params.append(subject.casefold())
        if text:
            terms = _text_terms(text)
            if not terms:
                raise InvalidFilterError("text", "text must contain at least one word")
            if len(terms) > _MAX_TEXT_TERMS:
                raise InvalidFilterError("text", f"text supports at most {_MAX_TEXT_TERMS} words")
            # One subquery per word, so the words may fall in different
            # chunks of the same message. Each is a quoted FTS phrase;
            # unicode61 never keeps a quote inside a token, but doubling
            # any (FTS5 string escaping) keeps FTS syntax out regardless.
            for term in terms:
                where.append(
                    "m.claimant_id IN (SELECT c.claimant_id FROM message_chunks_fts f "
                    "JOIN message_chunks c ON c.fts_rowid = f.rowid "
                    "WHERE message_chunks_fts MATCH ? AND c.attachment_id IS NULL)"
                )
                params.append('"' + term.replace('"', '""') + '"')
        if folder:
            where.append("m.folder = ?")
            params.append(folder)
        else:
            # Without a folder, messages filed in an excluded folder are
            # left out (#441); ``folder="Trash"`` lists them.
            marks = ",".join("?" * len(DEFAULT_EXCLUDED_FOLDERS))
            where.append(f"m.folder NOT IN ({marks})")
            params.extend(DEFAULT_EXCLUDED_FOLDERS)
        if date_from_iso is not None:
            where.append("m.sent_at >= ?")
            params.append(date_from_iso)
        if date_to_iso is not None:
            where.append("m.sent_at <= ?")
            params.append(date_to_iso)
        if has_attachments is not None:
            where.append("m.has_attachments = ?")
            params.append(1 if has_attachments else 0)
        if authority_class:
            where.append(f"m.claimant_id IN ({_SENDER_CLASS_MESSAGES})")
            params.append(authority_class)

        # A cursor is only meaningful for the predicates it was issued
        # under; bind it to a digest of them.
        digest = hashlib.sha256(
            json.dumps(
                [sender, recipient, participant, subject, text, folder]
                + [date_from_iso, date_to_iso, has_attachments, authority_class]
            ).encode()
        ).hexdigest()[:16]
        page_where = list(where)
        page_params = list(params)
        offset = 0
        if cursor:
            last_sent_at, last_id, offset = _decode_cursor(cursor, digest)
            # Row-value form: SQLite seeks idx_messages_sent to the cursor;
            # the equivalent OR expansion sorted every earlier row.
            page_where.append("(m.sent_at, m.claimant_id) < (?, ?)")
            page_params += [last_sent_at, last_id]

        where_sql = " AND ".join(where) or "1"
        page_where_sql = " AND ".join(page_where) or "1"
        with closing(self._connect()) as conn:
            # One read transaction: the count, the page, and its
            # participants come from the same snapshot even while the
            # indexer commits.
            conn.execute("BEGIN")
            total = conn.execute(
                "SELECT COUNT(*) FROM messages m WHERE " + where_sql,  # nosec B608
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT {_MESSAGE_COLUMNS} FROM messages m WHERE "  # nosec B608
                + page_where_sql
                + " ORDER BY m.sent_at DESC, m.claimant_id DESC LIMIT ?",
                [*page_params, limit + 1],
            ).fetchall()
            has_more = len(rows) > limit
            records = [_row_to_message_record(r) for r in rows[:limit]]
            _attach_participants(conn, records)
            conn.rollback()

        next_offset = offset + len(records)
        return MessagePage(
            total_matches=total,
            offset=offset,
            messages=records,
            has_more=has_more,
            next_cursor=_encode_cursor(digest, records[-1], next_offset) if has_more else None,
        )

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _row_to_result(self, row) -> ThreadResult:
        # Prefer the original-cased ``display_subject``; fall back to the
        # normalized ``subject`` while it is NULL.
        return ThreadResult(
            thread_id=row["thread_id"],
            subject=row["display_subject"] or row["subject"],
            participants=json.loads(row["participants"]),
            senders=json.loads(row["senders"]),
            folder=row["folder"],
            date_first=datetime.fromisoformat(row["date_first"]),
            date_last=datetime.fromisoformat(row["date_last"]),
            message_ids=json.loads(row["message_ids"]),
            snippet=row["snippet"] or "",
            has_attachments=bool(row["has_attachments"]),
            body_text=row["body_text"] or "",
            score=float(row["score"]) if "score" in row.keys() else 0.0,
        )


def _has_valid_distance(row: sqlite3.Row) -> bool:
    """False for a vector row stored with NaN or inf (#232).

    sqlite-vec returns a NULL (NaN) or infinite distance for such a row.
    Skipping it keeps one bad row from crashing the thread lane or
    ranking as a perfect match in the chunk lanes. The indexer now
    rejects these vectors; a full reindex removes any stored earlier.
    """
    score = row["score"]
    return score is not None and math.isfinite(score)


def _parse_date_range(
    date_from: str | None, date_to: str | None
) -> tuple[datetime | None, datetime | None]:
    """Parse the ``date_from`` / ``date_to`` filters into tz-aware UTC
    bounds, ``None`` for a bound that was not supplied.

    Every tool that takes both bounds parses them here, so they all
    reject the same input. Raises ``InvalidFilterError`` (a
    ``ValueError``) on an unparseable value, and on ``date_from`` after
    ``date_to`` (#312): that interval is empty, but the thread overlap
    predicates (``date_last >= from AND date_first <= to``) would still
    accept a thread spanning it. The comparison runs on the parsed UTC
    instants, after date-only promotion, so a single date names its whole
    day and two offsets for one instant compare equal.
    """
    start = (
        _parse_filter_date(date_from, end_of_day=False, _field_name="date_from")
        if date_from
        else None
    )
    end = _parse_filter_date(date_to, end_of_day=True, _field_name="date_to") if date_to else None
    if start is not None and end is not None and start > end:
        raise InvalidFilterError("date_from/date_to", "date_from must not be after date_to")
    return start, end


def validate_date_range(date_from: str | None, date_to: str | None) -> None:
    """Raise ``InvalidFilterError`` for a date filter pair the search
    methods would reject. Tool handlers call it on entry so a bad range
    fails before any embedding, retrieval or model call; the database
    methods still check for themselves.
    """
    _parse_date_range(date_from, date_to)


def _normalize_date_range(
    date_from: str | None, date_to: str | None
) -> tuple[str | None, str | None]:
    """``_parse_date_range`` as ISO 8601 strings for SQL pushdown, where
    they are compared lexicographically against stored ``+00:00``
    timestamps (``date_first`` / ``date_last`` / ``sent_at``).
    """
    start, end = _parse_date_range(date_from, date_to)
    return (
        start.isoformat() if start is not None else None,
        end.isoformat() if end is not None else None,
    )


def _parse_filter_date(
    value: str, *, end_of_day: bool, _field_name: str = "date filter"
) -> datetime:
    """Parse a user-supplied date filter into a tz-aware UTC ``datetime``.

    Accepts:
    - date-only values, meaning any form ``date.fromisoformat`` accepts
      (``"2024-12-31"``, ``"20241231"``, ``"2025-W01-2"``, ...), with or
      without a trailing ``Z``: promoted to ``00:00:00`` when used as a
      lower bound, ``23:59:59.999999`` when used as an upper bound, both
      in UTC — so the filter includes the full day the user named (#330).
    - any ISO 8601 datetime ``datetime.fromisoformat`` accepts, including
      a trailing ``Z``: the instant it names, for either bound.

    Naive datetimes are assumed to be UTC. Offset-aware values are
    converted to UTC before being returned, so callers that feed the
    result's ``isoformat()`` into SQL string comparisons against stored
    UTC timestamps compare the same instant rather than two offset-shifted
    strings that happen to sort differently.
    """
    # Date-only is whatever the date parser accepts, not a string shape:
    # a length check missed the basic and week-date forms (#330). The
    # ``Z`` is dropped because the date parser rejects it and it only
    # restates the UTC the day is already read in.
    try:
        day = date.fromisoformat(value.removesuffix("Z"))
    except ValueError:
        day = None
    if day is not None:
        return datetime.combine(day, time.max if end_of_day else time.min, tzinfo=UTC)

    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidFilterError(_field_name, f"{_field_name}: invalid datetime {value!r}") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    try:
        return dt.astimezone(UTC)
    except OverflowError as exc:
        # A parseable value at datetime's limit with an outward offset
        # ("0001-01-01T00:00:00+14:00") has no UTC instant.
        raise InvalidFilterError(_field_name, f"{_field_name}: invalid datetime {value!r}") from exc
