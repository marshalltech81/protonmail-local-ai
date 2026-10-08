"""
Search tools — Group 1 (most frequently called).
Semantic, keyword, and hybrid search over the SQLite index.
"""

import asyncio
import logging
import sys

from fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from ..lib.embed import embed_query
from ..lib.predicates import _given, address_match_mode, validate_date_range
from ..lib.rate_limited_log import ArgumentRejections, RateLimitedLog
from ..lib.security import log_tool_call, safe_provider_exception_text
from ..lib.sqlite import (
    PROMPT_EVIDENCE_CHUNKS_PER_THREAD,
    InvalidFilterError,
    ReapedSource,
    ScopeLabels,
    VectorLanesUnavailableError,
    normalize_authority_class,
)
from ..lib.timings import count, rerank_mode, stage, timed_tool
from ..lib.validation import clamp_int
from .intelligence import (
    _MAX_ASK_THREADS,
    FromNameResolution,
    blank_to_none,
    clamp_ask_threads,
    resolve_from_name,
    select_ask_threads,
)
from .outputs import (
    HEADER_CHAR_LIMIT,
    MAX_LISTED,
    AttachmentHit,
    EvidenceCarrier,
    EvidenceChunk,
    EvidenceOutput,
    EvidenceScope,
    EvidenceThread,
    SearchAttachmentsOutput,
    SearchEmailsOutput,
    clip,
    date_bounds,
    describe_date_bounds,
    read_only,
    reaped_source,
    thread_summary,
    tool_result,
)
from .outputs import (
    source as source_ref,
)

log = logging.getLogger("mcp.tools.search")


def _clip_optional(value: str | None) -> str | None:
    """``value`` cut at ``HEADER_CHAR_LIMIT``, or ``None`` when absent."""
    return None if value is None else clip(value, HEADER_CHAR_LIMIT)


# Hard ceiling on ``limit``. MCP tool calls can originate from an LLM,
# which may hallucinate values like ``limit=100000`` — without a clamp
# that becomes a large FTS + vector + RRF workload and a large result
# payload to ship back through the protocol. 50 is well above any
# reasonable interactive use of the search tool.
_MAX_SEARCH_LIMIT = 50

# Hard ceiling on ``get_evidence``'s ``limit`` (evidence chunks). It is
# the audit view of ``ask_mailbox``, whose prompt can hold up to
# ``_MAX_ASK_THREADS`` threads x ``PROMPT_EVIDENCE_CHUNKS_PER_THREAD``
# chunks, so the ceiling is derived from those constants and never falls
# below what an answer drew on (#449). At 1,600 characters a chunk the
# largest response stays near 100k characters of passage text.
_MAX_EVIDENCE_LIMIT = max(_MAX_SEARCH_LIMIT, _MAX_ASK_THREADS * PROMPT_EVIDENCE_CHUNKS_PER_THREAD)

_VALID_SEARCH_MODES = frozenset({"hybrid", "semantic", "keyword"})

# Per-chunk character cap for ``get_evidence`` output. Indexed chunks are
# already paragraph-bounded by the indexer; this is a defensive ceiling so
# one pathologically long attachment chunk can't bloat the tool response.
_EVIDENCE_CHUNK_CHARS = 1600

# ``get_evidence``'s precision controls (#988). The per-thread and
# per-chunk caps can only lower ``PROMPT_EVIDENCE_CHUNKS_PER_THREAD`` and
# ``_EVIDENCE_CHUNK_CHARS``; ``lib/security._LOGGABLE_TOOL_PARAMS`` logs
# exactly these values.
_EVIDENCE_SOURCES = ("any", "body", "attachment")
_EVIDENCE_SCOPES = ("any", "in_scope")
_PRECISION_CONTROLS = ("source", "scope", "max_chunks_per_thread", "max_chars_per_chunk")
# Seconds per window of the rate-limited rejection warning.
_PRECISION_REJECTION_LOG_SECS = 60.0
# ``per_thread_limit`` that keeps every ranked chunk of a thread. The
# evidence query already reads and ranks all of a thread's chunks before
# its cap, so ``source`` and ``scope`` filter that full list and the
# cap applies after them.
_ALL_THREAD_CHUNKS = sys.maxsize


def _of_source(chunks: list, source: str) -> list:
    """``chunks`` of ``source`` (``body`` or ``attachment``), in order."""
    return [c for c in chunks if (c.attachment_id is None) == (source == "body")]


def _carrier_date(chunk) -> tuple[bool, str]:
    """Sort key of a passage's message: its delivery date, else its send
    date (as the date filters read it), an unknown date last."""
    date = chunk.message_occurred_at or chunk.message_date
    return (date is None, date or "")


def _collapse_attachment_copies(chunks: list) -> tuple[list, dict[str, list]]:
    """``chunks`` with each attachment passage (same content hash, chunk
    index and text) kept once, on its earliest carrying message, at the
    rank of its best-ranked copy (#989); and, per kept ``chunk_id``, the
    other copies, earliest first. Body passages pass through unchanged.

    The text is part of the key: copies of one payload can be chunked
    differently (another extractor module, or a message left on an older
    extractor version), and those are different passages."""
    copies: dict[tuple[str, int, str], list] = {}
    for c in chunks:
        if c.attachment_id is not None:
            copies.setdefault((c.attachment_id, c.chunk_index, c.text), []).append(c)
    kept: list = []
    carried: dict[str, list] = {}
    for c in chunks:
        if c.attachment_id is None:
            kept.append(c)
            continue
        group = copies[(c.attachment_id, c.chunk_index, c.text)]
        if c is not group[0]:
            continue
        # A stable sort: copies of one date keep their rank order.
        earliest, *others = sorted(group, key=_carrier_date)
        kept.append(earliest)
        carried[earliest.chunk_id] = others
    return kept, carried


def _msg_date(chunk) -> str:
    """A passage's message date for the prose: its send day, plus its
    delivery day when known."""
    msg_date = (chunk.message_date or "")[:10] or "unknown date"
    if chunk.message_occurred_at:
        msg_date += f" (delivered {chunk.message_occurred_at[:10]})"
    return msg_date


def _check_precision_controls(
    source: str,
    scope: str,
    max_chunks_per_thread: int | None,
    max_chars_per_chunk: int | None,
) -> None:
    """Reject an out-of-range precision control with fixed text (#988):
    the message names the field and its range, never the value."""
    if source not in _EVIDENCE_SOURCES:
        raise InvalidFilterError("source", "source must be any, body or attachment.")
    if scope not in _EVIDENCE_SCOPES:
        raise InvalidFilterError("scope", "scope must be any or in_scope.")
    for name, value, ceiling in (
        ("max_chunks_per_thread", max_chunks_per_thread, PROMPT_EVIDENCE_CHUNKS_PER_THREAD),
        ("max_chars_per_chunk", max_chars_per_chunk, _EVIDENCE_CHUNK_CHARS),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling
        ):
            raise InvalidFilterError(name, f"{name} must be an integer from 1 to {ceiling}.")


def _chunk_scope(chunk, scope: ScopeLabels | None) -> EvidenceScope:
    """A ``get_evidence`` chunk's scope label (#755): ``in_scope`` when
    its message is in ``scope.claimants``, or when there is no scope
    (the thread path, which takes no filters)."""
    if scope is None or chunk.claimant_id in scope.claimants:
        return "in_scope"
    return "context"


def register_search_tools(
    server,
    db,
    embed_client,
    *,
    reranker=None,
    secret_values=None,
    expected_embed_dim: int | None = None,
):
    """Register search tools.

    ``secret_values`` is the list of operator-configured API keys
    (embed / rerank) to scrub from any exception text echoed back to
    the caller or written to logs. Provider-SDK exceptions can include
    auth headers and request/response body fragments; passing the
    configured keys here means a stringified exception that happens to
    quote the bearer token gets redacted before it leaves the process.

    ``expected_embed_dim`` is the dimension declared by the indexer's
    ``message_chunks_vec`` table (read at startup via
    ``Database.get_embedding_dim()``). When set, every embed call is
    validated against it so a misconfigured ``EMBED_MODEL`` surfaces
    as an actionable error instead of silently degrading to keyword
    search. ``None`` skips the check (no declared dim found; see
    ``Database.get_embedding_dim``).
    """
    secrets = list(secret_values or ())
    # Config identifier for the per-call timing line.
    timing_config = {"rerank": rerank_mode(reranker)}
    # A client can repeat a rejected precision control as fast as it
    # likes: the first rejection per field and window is logged, the
    # rest are counted into one summary line.
    precision_rejections = RateLimitedLog(
        log,
        _PRECISION_CONTROLS,
        _PRECISION_REJECTION_LOG_SECS,
        first_msg="get_evidence rejected invalid %s",
        summary_msg="get_evidence rejected invalid controls in the last %ds: %s",
    )
    # The same for every other rejected argument, keyed by tool and field (#1039).
    rejections = ArgumentRejections(log, ("search_emails", "get_evidence", "search_attachments"))

    @server.tool(
        output_schema=SearchEmailsOutput.model_json_schema(),
        annotations=read_only("Search Emails"),
    )
    @timed_tool("search_emails", **timing_config)
    async def search_emails(
        query: str,
        mode: str = "hybrid",
        folders: list[str] | None = None,
        from_addr: str | None = None,
        from_name: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
        participant: str | None = None,
        limit: int = 10,
        authority_class: str | None = None,
    ) -> CallToolResult:
        """
        Search the mailbox and return matching THREADS (conversations),
        not individual messages.

        Use this whenever the user asks about email, threads, messages,
        senders, dates, attachments, or anything else stored in the
        local mailbox index. This is the default tool for any mailbox
        question that names a topic, keyword, sender, or date range.
        For broad cross-thread synthesis questions (e.g. "what's open?",
        "summarize my recent vendor activity"), reach for ask_mailbox
        instead — it bundles retrieval and synthesis in one call.
        Results are ranked and capped at ``limit`` threads, so this tool
        cannot answer "all" or "how many" questions ("every email from
        Jane in 2024", "how many invoices arrived in March") — use
        query_messages for those; it enumerates the complete set with an
        exact count.

        Filtering by sender — read this before iterating queries:
            - User said a NAME or ROLE ("Jane Smith", "the accountant",
              "Smith"): pass it as ``from_name``. Do NOT pass it as
              ``from_addr``; the address filter is exact and a name
              won't match. Do NOT call ``find_contact`` first either —
              ``from_name`` already resolves the name internally.
            - User said an EMAIL ADDRESS ("jane@example.com") or a
              DOMAIN ("@example.com"): pass it as ``from_addr``.
            - If a sender filter returns no hits, do not silently fall
              back to a query without a filter — surface the empty
              result. Iterating broader keyword queries to compensate
              for a missing sender match is the wrong shape.

        Each result is one thread bundling its messages, with subject,
        participants, date range, folder, and a short snippet. The
        snippet is BODY content only — attachment text (PDFs, OCR'd
        images) is not in the result. To read messages inside a thread,
        call get_thread or summarize_thread with the result's
        ``Thread ID``. To read attachment content, use
        ``search_attachments`` (keyword search over extracted text),
        ``ask_mailbox`` (synthesized answers), or
        ``extract_from_emails`` (structured fields) — neither this tool nor
        get_thread surfaces attachment chunks. Never invent a
        thread_id from the subject — IDs are opaque; pass only a
        ``Thread ID`` that a tool result returned.

        Args:
            query: Natural language or keyword query
            mode: "hybrid" (default), "semantic", or "keyword"
                  hybrid = BM25 + vector merged via RRF (best for most queries)
                  semantic = vector similarity only (best for conceptual queries)
                  keyword = BM25 only (best for exact names, numbers, dates)
            folders: Filter to threads with a message in these folders,
                     e.g. ["INBOX", "Sent"]. Without it, threads filed
                     only in Trash are left out; name "Trash" to
                     include them.
            from_addr: Filter by canonical sender ADDRESS — only use when
                       the user gave an email address or domain
                       ("jane@example.com", "@example.com"). For names
                       or role descriptors, use ``from_name`` instead.
                       Substring match against the stored sender display
                       string for shapes that can't canonicalize.
            from_name: PREFERRED sender filter when the user names a
                       person or role. Pass the user's exact words
                       ("Jane Smith", "the accountant", "Smith",
                       "my CPA"). The tool resolves through
                       find_contact and applies the most-active
                       matching contact's canonical address — you do
                       not need to call find_contact yourself. If both
                       ``from_addr`` and ``from_name`` are given,
                       ``from_addr`` wins.
            date_from: ISO 8601 date lower bound e.g. "2024-01-01".
                       A thread qualifies when its span (its messages'
                       delivery dates, else send dates) overlaps the
                       range.
            date_to: ISO 8601 date upper bound e.g. "2024-12-31".
                     For either bound, a date-only value is a UTC day;
                     for the user's time zone give an offset
                     ("2024-01-01T00:00:00-05:00"). The response's
                     ``date_bounds`` echoes the UTC instants applied.
            has_attachments: True to only show threads with attachments
            participant: Filter to threads where this person appears in
                         ANY role — sender, To, or Cc. Use this for
                         "threads involving Jane" / "anything with
                         legal@example.com on it". Distinct from
                         from_addr/from_name, which are sender-only.
                         Accepts an address, a domain (@example.com),
                         or a bare name fragment.
            limit: Maximum number of threads to return (default 10)
            authority_class: Keep only threads with a message whose
                             sender the operator's rules file classes
                             as this: "counsel", "management",
                             "vendor", "government", "personal",
                             "other", or "unclassified" (no rule
                             matched). A filter only; it never changes
                             ranking. Spam-folder messages never count.
                             find_contact shows a sender's class.

        Returns:
            List of matching email threads with subject, participants,
            dates, folder, and a short snippet.
        """
        log_tool_call(
            log,
            "search_emails",
            {
                "query": query,
                "mode": mode,
                "folders": folders,
                "from_addr": from_addr,
                "from_name": from_name,
                "date_from": date_from,
                "date_to": date_to,
                "has_attachments": has_attachments,
                "participant": participant,
                "limit": limit,
                "authority_class": authority_class,
            },
        )
        if mode not in _VALID_SEARCH_MODES:
            raise ToolError(f"Invalid mode {mode!r}. Use 'hybrid', 'semantic', or 'keyword'.")
        # Clamp to [1, _MAX_SEARCH_LIMIT] so an out-of-range or
        # non-numeric caller value never drives an unbounded query
        # against the index. clamp_int returns the default (10) when the
        # raw value is missing or unparseable rather than raising a bare
        # ValueError before the try/except below.
        limit = clamp_int(limit, default=10, minimum=1, maximum=_MAX_SEARCH_LIMIT)
        # Reject a bad date range before any provider or retrieval work.
        try:
            bounds = date_bounds(*validate_date_range(date_from, date_to))
            authority_class = normalize_authority_class(authority_class)
        except InvalidFilterError as e:
            rejections.reject("search_emails", e.field_name)
            raise ToolError(f"Search error: {e}") from e
        # A blank person filter is absent and padding is stripped, as in
        # get_evidence and ask_mailbox (#702, #705).
        from_name = blank_to_none(from_name)
        participant = blank_to_none(participant)

        # Resolve ``from_name`` -> canonical SENDER address via
        # find_contact (``resolve_from_name``). Skipped when the caller
        # already passed a strict ``from_addr`` — explicit always beats
        # lookup. When the lookup yields nothing, short-circuit with an
        # honest empty result rather than silently dropping the
        # filter and returning unrelated threads.
        # Reported in the structured output only (#864), never logged.
        resolved_from_addr = None
        from_name_matches = None
        if from_name and not from_addr:
            try:
                resolution = await resolve_from_name(db, from_name, folders)
            except Exception as e:
                # Local-DB work, but a conversion error can quote stored
                # mail: the same classification as provider failures (#257).
                safe_error = safe_provider_exception_text(e, secrets)
                log.error("search_emails: find_contact lookup failed: %s", safe_error)
                raise ToolError(f"Search error: {safe_error}") from e
            resolved_from_addr = resolution.address
            from_name_matches = resolution.senders
            if resolved_from_addr is None:
                empty = (
                    f"No results found for: '{query}' (no contact matched from_name={from_name!r})"
                )
                bounds_line = describe_date_bounds(bounds)
                return tool_result(
                    f"{empty}\n{bounds_line}" if bounds_line else empty,
                    SearchEmailsOutput(
                        mode=mode,
                        resolved_from_addr=None,
                        from_name_matches=0,
                        date_bounds=bounds,
                        results=[],
                    ),
                )
            from_addr = resolved_from_addr

        try:
            # All three modes accept the same filter set; keyword and
            # semantic modes previously only forwarded ``folders`` and
            # silently dropped sender/date/attachment filters, returning
            # unfiltered results without warning.
            # SQLite work runs in a worker thread so the asyncio event
            # loop stays responsive while FTS / vector / RRF (and now
            # the additive chunk lane) execute against the index. The
            # FastMCP server is async-first; without ``to_thread`` a
            # multi-second hybrid query would block every other tool
            # call concurrently in flight.
            if mode == "keyword":
                results = await asyncio.to_thread(
                    db.keyword_search,
                    query_text=query,
                    folders=folders,
                    from_addr=from_addr,
                    date_from=date_from,
                    date_to=date_to,
                    has_attachments=has_attachments,
                    participant=participant,
                    limit=limit,
                    authority_class=authority_class,
                )
            elif mode == "semantic":
                embedding = await embed_query(embed_client, query, expected_embed_dim)
                results = await asyncio.to_thread(
                    db.semantic_search,
                    query_embedding=embedding,
                    folders=folders,
                    from_addr=from_addr,
                    date_from=date_from,
                    date_to=date_to,
                    has_attachments=has_attachments,
                    participant=participant,
                    limit=limit,
                    authority_class=authority_class,
                )
            else:  # hybrid (default)
                embedding = await embed_query(embed_client, query, expected_embed_dim)
                # When a reranker is configured, ask for evidence
                # chunks so the cross-encoder scores against the actual
                # passage that lifted the thread into ranking — not
                # ``Subject + snippet`` (the snippet is the latest
                # message's first 200 chars, almost certainly the wrong
                # passage for the reranker to score against). Without
                # ``with_evidence=True`` the reranker can demote the
                # genuinely-relevant thread because it never sees the
                # passage that made the dense or chunk lane retrieve
                # it. The flag is gated on reranker presence so we
                # don't pay the chunk-attach cost on the rerank-less
                # default path.
                results = await asyncio.to_thread(
                    db.hybrid_search,
                    query_text=query,
                    query_embedding=embedding,
                    folders=folders,
                    from_addr=from_addr,
                    date_from=date_from,
                    date_to=date_to,
                    has_attachments=has_attachments,
                    participant=participant,
                    limit=limit,
                    with_evidence=reranker is not None,
                    reranker=reranker,
                    authority_class=authority_class,
                )

            count("results", len(results))
            output = SearchEmailsOutput(
                mode=mode,
                resolved_from_addr=resolved_from_addr,
                from_name_matches=from_name_matches,
                date_bounds=bounds,
                results=[thread_summary(r) for r in results],
            )
            bounds_line = describe_date_bounds(bounds)
            if not results:
                empty = f"No results found for: '{query}'"
                return tool_result(f"{empty}\n{bounds_line}" if bounds_line else empty, output)

            lines = [f"Found {len(results)} thread(s) for: '{query}'"]
            if bounds_line:
                lines.append(bounds_line)
            lines[-1] += "\n"
            for i, r in enumerate(results, 1):
                participants = ", ".join(clip(p, HEADER_CHAR_LIMIT) for p in r.participants[:3])
                lines.append(
                    f"{i}. [{r.folder}] {clip(r.subject, HEADER_CHAR_LIMIT)}\n"
                    f"   Participants: {participants}"
                    f"{'...' if len(r.participants) > 3 else ''}\n"
                    f"   Date: {r.date_last.strftime('%Y-%m-%d')}"
                    f" | Messages: {len(r.message_ids)}"
                    f" | {'📎 ' if r.has_attachments else ''}"
                    f"Thread ID: {r.thread_id}\n"
                    f"   {r.snippet[:120]}...\n"
                )

            return tool_result("\n".join(lines), output)

        except InvalidFilterError as e:
            # The message quotes the rejected value, which log_tool_call
            # withheld. Return it to the caller; log only the field name.
            rejections.reject("search_emails", e.field_name)
            raise ToolError(f"Search error: {e}") from e
        except VectorLanesUnavailableError as e:
            # Fixed text naming the fix; it quotes nothing.
            log.error("search_emails error: %s", e)
            raise ToolError(f"Search error: {e}") from e
        except Exception as e:
            # A provider error (the embed call) can echo the query, and a
            # parse or database error quotes the values it rejects.
            # ``safe_provider_exception_text`` keeps a status error's
            # status, the text of connection, timeout and our own
            # fixed-message errors, and only the type of anything else.
            safe_error = safe_provider_exception_text(e, secrets)
            log.error("search_emails error: %s", safe_error)
            raise ToolError(f"Search error: {safe_error}") from e

    @server.tool(
        output_schema=EvidenceOutput.model_json_schema(),
        annotations=read_only("Get Evidence Passages"),
    )
    @timed_tool("get_evidence", **timing_config)
    async def get_evidence(
        query: str,
        thread_id: str | None = None,
        folders: list[str] | None = None,
        from_addr: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
        max_threads: int | None = None,
        limit: int | None = None,
        include_scores: bool = False,
        participant: str | None = None,
        from_name: str | None = None,
        source: str = "any",
        scope: str = "any",
        max_chunks_per_thread: int | None = None,
        max_chars_per_chunk: int | None = None,
        dedupe_attachments: bool = False,
    ) -> CallToolResult:
        """
        Return the exact indexed passages (evidence chunks) that back a
        question — no LLM synthesis, just the retrieved source text.

        Use this to AUDIT or CITE an answer: it surfaces the same chunks
        ask_mailbox feeds its model, so you can show the user precisely
        which emails and attachments ground a claim. It is also the
        fast, synthesis-free path when you only need the source
        passages and not a written answer.

        Each chunk carries its parent thread and Message-ID, the source
        (message body, or an attachment with filename + MIME type), the
        message date, and the character offsets of the passage.
        Attachment chunks — extracted PDF / OCR / document text — are
        included here, unlike get_thread, which is body-only.

        Pass thread_id to scope evidence to a single thread ("which
        part of this thread mentions the deadline?"); omit it to gather
        evidence across the whole mailbox. To audit an ask_mailbox
        answer, pass the same question, filters and max_threads and
        leave limit unset: the result is the evidence that answer
        retrieved, in the same order. A smaller limit keeps the first
        limit chunks of it.

        Each thread's passages are ordered by similarity to the query,
        after up to two that lead: when the query matches one of its
        attachments' filename or MIME type, that attachment's first
        passage, then the nearest passage holding a word of the query
        (each chunk's selected_by says which). The rest of that
        attachment follows, then its other attachments, then the body,
        each by similarity, and attachments can then fill every slot but the
        keyword one (six per thread mailbox-wide, limit with
        thread_id) before a body message. A thread with no passages is
        listed with an empty chunks list (with max_threads) or left
        out; read it with get_thread. So passages can stop before a
        late resolution in a long thread: for status or closure,
        re-ask about the resolution without the attachment's filename
        or file-type words (for example "PDF"), since either keeps
        that attachment first, or read the thread's later messages
        with get_thread or get_message.

        Args:
            query: The question or topic to gather evidence for.
            thread_id: Optional opaque thread ID to scope evidence to
                       one thread. Obtain it from search_emails or
                       list_threads — never invent it from a subject.
                       Cannot be combined with folders, from_addr,
                       from_name, participant, date_from, date_to,
                       has_attachments or max_threads.
            folders: Restrict to threads with a message in these folders,
                     e.g. ["INBOX", "Sent"]. Without it, threads filed
                     only in Trash are left out; name "Trash" to
                     include them.
            from_addr: Restrict to a sender ADDRESS or domain
                       ("jane@example.com", "@example.com"). For a
                       person's name, use from_name.
            date_from: ISO 8601 date lower bound, e.g. "2024-01-01".
                       A thread qualifies when its span (its messages'
                       occurred_at, else sent_at) overlaps the range,
                       and any of its passages may be returned; check
                       each chunk's occurred_at and sent_at, which can
                       fall outside the range. Each chunk's scope is
                       in_scope when its own message meets every
                       sender, participant, date and folder filter,
                       as ask_mailbox labels it, else context.
            date_to: ISO 8601 date upper bound, e.g. "2024-12-31".
            has_attachments: True to restrict to threads with attachments.
            participant: Restrict to threads where this person appears
                         in ANY role (sender, To or Cc), as in
                         search_emails and ask_mailbox.
            from_name: Restrict to mail FROM a named person or role,
                       resolved to a sender address exactly as
                       ask_mailbox and search_emails resolve it. If
                       both are given, from_addr wins.
            max_threads: Rank threads exactly as ask_mailbox does with
                         this max_threads (clamped to [1, 10]) and
                         return their evidence. Omit it to rank by
                         limit instead.
            limit: Maximum evidence chunks to return (default 12, or
                   max_threads x 6 when max_threads is given; clamped
                   to [1, 60], ask_mailbox's largest evidence set).
            include_scores: When true, annotate each thread with the
                            retrieval lanes that matched (thread_fts /
                            chunk_fts / attachment_fts / thread_vec /
                            chunk_vec / rerank) and each chunk with its
                            vector distance — useful for debugging
                            retrieval quality.
            source: "body" or "attachment" passages only (default "any").
            scope: "in_scope" drops context passages (default "any").
            max_chunks_per_thread: 1 to 6 (default 6; limit with thread_id).
            max_chars_per_chunk: 1 to 1600 (default 1600).
            dedupe_attachments: When true, an attachment carried by
                                several messages of a thread (sent,
                                re-sent, forwarded) returns each passage
                                once, on the earliest carrier, with the
                                others in carried_by (default false).

        Returns:
            Ranked evidence chunks grouped by thread, with full
            provenance (thread, message, source, offsets, date).
        """
        log_tool_call(
            log,
            "get_evidence",
            {
                "query": query,
                "thread_id": thread_id,
                "folders": folders,
                "from_addr": from_addr,
                "date_from": date_from,
                "date_to": date_to,
                "has_attachments": has_attachments,
                "participant": participant,
                "from_name": from_name,
                "max_threads": max_threads,
                "limit": limit,
                "include_scores": include_scores,
                "source": source,
                "scope": scope,
                "max_chunks_per_thread": max_chunks_per_thread,
                "max_chars_per_chunk": max_chars_per_chunk,
                "dedupe_attachments": dedupe_attachments,
            },
        )
        from_name = blank_to_none(from_name)
        participant = blank_to_none(participant)
        if not query or not query.strip():
            raise ToolError("Provide a query to gather evidence for.")
        try:
            _check_precision_controls(source, scope, max_chunks_per_thread, max_chars_per_chunk)
        except InvalidFilterError as e:
            precision_rejections.record(e.field_name)
            raise ToolError(f"Evidence error: {e}") from e
        source_filter = None if source == "any" else source
        in_scope_only = scope == "in_scope"
        per_thread = max_chunks_per_thread or PROMPT_EVIDENCE_CHUNKS_PER_THREAD
        chunk_chars = max_chars_per_chunk or _EVIDENCE_CHUNK_CHARS
        # The timing line marks a call that a precision control narrowed.
        if (
            source_filter
            or in_scope_only
            or max_chunks_per_thread
            or max_chars_per_chunk
            or dedupe_attachments
        ):
            count("evidence_filtered", 1)
        # With ``source`` or ``dedupe_attachments`` the thread path reads
        # every ranked passage, filters or collapses them, then applies
        # ``limit``.
        full_thread_list = bool(source_filter or dedupe_attachments)
        if thread_id:
            # These filters select threads; thread_id already names one.
            # Applying them would only keep or drop that whole thread (a
            # sender filter would not narrow to that sender's passages),
            # and ignoring them returned evidence that looked filtered.
            given = [
                name
                for name, value in (
                    ("folders", folders),
                    ("from_addr", from_addr),
                    ("date_from", date_from),
                    ("date_to", date_to),
                    ("has_attachments", has_attachments),
                    ("participant", participant),
                    ("from_name", from_name),
                    ("max_threads", max_threads),
                )
                # Blank optionals (``""``, ``[]``) are absent, as on the
                # mailbox-wide path; ``has_attachments=False`` is a filter.
                if value is not None
                and value != []
                and not (isinstance(value, str) and not value.strip())
            ]
            if given:
                raise ToolError(
                    f"{', '.join(given)} cannot be combined with thread_id: those "
                    "filters choose threads, and thread_id already names one. Drop "
                    "them to read this thread's evidence, or drop thread_id to "
                    "search the mailbox with them."
                )
        # ``max_threads`` takes ask_mailbox's clamp, so the same argument
        # ranks the same threads (#537); its default chunk budget is that
        # many threads' full evidence.
        default_limit = 12
        if max_threads is not None:
            max_threads = clamp_ask_threads(max_threads)
            default_limit = max_threads * PROMPT_EVIDENCE_CHUNKS_PER_THREAD
        # ``limit`` counts evidence chunks; the ceiling covers ask_mailbox's
        # largest evidence set, and an LLM-inflated value would otherwise
        # drive a large per-thread chunk fetch and an oversized payload.
        limit = clamp_int(limit, default=default_limit, minimum=1, maximum=_MAX_EVIDENCE_LIMIT)
        # Reject a bad date range before any provider or retrieval work.
        # The thread-scoped path takes no dates (blank ones are ignored
        # above), so only the mailbox-wide path checks them.
        if not thread_id:
            try:
                validate_date_range(date_from, date_to)
            except InvalidFilterError as e:
                rejections.reject("get_evidence", e.field_name)
                raise ToolError(f"Evidence error: {e}") from e

        # groups: list of (subject, thread_id, lane_ranks | None,
        # thread_score | None, chunks). ``lane_ranks`` is None for the
        # thread-scoped path because that path bypasses RRF fusion.
        groups: list[tuple[str, str, dict[str, int] | None, float | None, list]] = []
        # The ``from_name`` lookup, reported in the structured output only
        # (#864), never logged.
        resolution: FromNameResolution | None = None
        # Scope labels of the mailbox-wide path (#755); the thread path
        # has no filters, so every passage there is in scope.
        labels: ScopeLabels | None = None
        # With ``scope=in_scope``: context passages left out, per listed
        # thread (#988).
        context_dropped: dict[str, int] = {}
        # With ``source``: ranked threads left out for having no passage
        # of that source (mailbox-wide path).
        source_emptied = 0
        # With ``max_chunks_per_thread``: passages the cap removed.
        capped_out = 0
        # With ``dedupe_attachments``: per kept attachment passage, the
        # copies on later messages of its thread (#989).
        carried: dict[str, list] = {}
        try:
            if thread_id:
                thread = await asyncio.to_thread(db.get_thread_or_reaped, thread_id)
                if isinstance(thread, ReapedSource):
                    raise ToolError(reaped_source("Thread", thread_id, thread.reaped_at))
                if not thread:
                    raise ToolError(f"Thread not found: {thread_id}")
                embedding = await embed_query(embed_client, query, expected_embed_dim)
                # The same selection as ask_mailbox: chunks of attachments
                # whose filename or MIME type the query matches lead.
                with stage("evidence_fetch"):
                    grouped = await asyncio.to_thread(
                        db.get_query_evidence_chunks,
                        query,
                        [thread_id],
                        embedding,
                        _ALL_THREAD_CHUNKS if full_thread_list else limit,
                    )
                chunks = grouped.get(thread_id, [])
                if not chunks:
                    # The reaper may have committed while the query was
                    # embedded; a thread gone since the first read reads
                    # as reaped (or not found), not as "No evidence".
                    current = await asyncio.to_thread(db.get_thread_or_reaped, thread_id)
                    if isinstance(current, ReapedSource):
                        raise ToolError(reaped_source("Thread", thread_id, current.reaped_at))
                    if not current:
                        raise ToolError(f"Thread not found: {thread_id}")
                if source_filter:
                    chunks = _of_source(chunks, source_filter)
                if dedupe_attachments:
                    chunks, copies = _collapse_attachment_copies(chunks)
                    carried.update(copies)
                if full_thread_list:
                    chunks = chunks[:limit]
                if max_chunks_per_thread:
                    capped_out = max(0, len(chunks) - max_chunks_per_thread)
                    chunks = chunks[:max_chunks_per_thread]
                count("evidence_chunks", len(chunks))
                if in_scope_only:
                    # No filters on this path: nothing is context.
                    context_dropped[thread_id] = 0
                if chunks:
                    groups.append(
                        (clip(thread.subject, HEADER_CHAR_LIMIT), thread_id, None, None, chunks)
                    )
            else:
                # ``from_name`` resolves as in ask_mailbox, so an answer
                # scoped with it can be audited; an explicit ``from_addr``
                # wins, and no match is an empty result, never a search
                # without the filter.
                if from_name and not from_addr:
                    resolution = await resolve_from_name(db, from_name, folders)
                    from_addr = resolution.address
                    if from_addr is None:
                        return tool_result(
                            f"No evidence found for: '{query}' "
                            f"(no contact matched from_name={from_name!r})",
                            EvidenceOutput(
                                chunk_count=0,
                                resolved_from_addr=None,
                                from_name_matches=0,
                                threads=[],
                                context_passages_left_out=0 if in_scope_only else None,
                                threads_without_source_passages=0 if source_filter else None,
                                attachment_copies_collapsed=0 if dedupe_attachments else None,
                            ),
                        )
                embedding = await embed_query(embed_client, query, expected_embed_dim)
                # ask_mailbox's retrieval, with the same per-thread chunk
                # cap. Without ``max_threads`` the chunk ``limit`` also
                # sets how many threads are ranked, as it always has.
                results = await asyncio.to_thread(
                    select_ask_threads,
                    db,
                    query,
                    embedding,
                    max_threads=limit if max_threads is None else max_threads,
                    folders=folders,
                    from_addr=from_addr,
                    date_from=date_from,
                    date_to=date_to,
                    reranker=reranker,
                    has_attachments=has_attachments,
                    participant=participant,
                )
                count("results", len(results))
                # ``source`` and ``scope`` filter each ranked thread's full
                # ranked passage list, so the six-passage cap applies after
                # them. The threads and their order stay ask_mailbox's:
                # the reranker saw the unfiltered evidence.
                every_chunk: dict[str, list] | None = None
                if (source_filter or in_scope_only or dedupe_attachments) and results:
                    with stage("evidence_precision"):
                        every_chunk = await asyncio.to_thread(
                            db.get_query_evidence_chunks,
                            query,
                            [r.thread_id for r in results],
                            embedding,
                            _ALL_THREAD_CHUNKS,
                        )
                # Each passage labelled as ask_mailbox labels it, before
                # ``scope=in_scope`` filters on the label.
                with stage("scope_labels"):
                    labels = await asyncio.to_thread(
                        db.message_scope,
                        [r.thread_id for r in results],
                        folders=folders,
                        from_addr=from_addr,
                        participant=participant,
                        date_from=date_from,
                        date_to=date_to,
                    )
                # Flatten thread-ranked evidence into a flat chunk budget:
                # ``limit`` counts chunks, threads are already ranked, and
                # chunks within a thread are ranked by similarity. Once the
                # budget is spent the slice yields [] and the thread drops.
                taken = 0
                for r in results:
                    ranked = r.evidence_chunks
                    dropped = 0
                    if every_chunk is not None:
                        ranked = every_chunk.get(r.thread_id, [])
                        if source_filter:
                            ranked = _of_source(ranked, source_filter)
                            if not ranked:
                                # No passage of that source, a thread
                                # with no indexed passages included.
                                source_emptied += 1
                                continue
                        if in_scope_only:
                            kept = [c for c in ranked if _chunk_scope(c, labels) == "in_scope"]
                            dropped = len(ranked) - len(kept)
                            ranked = kept
                        if dedupe_attachments:
                            ranked, copies = _collapse_attachment_copies(ranked)
                            carried.update(copies)
                        ranked = ranked[:PROMPT_EVIDENCE_CHUNKS_PER_THREAD]
                    capped = ranked[:per_thread]
                    chunks = capped[: limit - taken]
                    # With ``max_threads``, a thread that has no indexed
                    # chunks stays, empty: ask_mailbox shows the model its
                    # indexed thread text instead, so the audit must still
                    # list the thread in its place. A thread ``scope``
                    # emptied stays too, with its count (#988). Once
                    # ``limit`` is spent, later threads drop whether or not
                    # they have chunks, so a smaller ``limit`` is a
                    # rank-order prefix.
                    keep_chunkless = (
                        max_threads is not None and not r.evidence_chunks and taken < limit
                    )
                    keep_emptied = dropped > 0 and not ranked and taken < limit
                    if not chunks and not keep_chunkless and not keep_emptied:
                        continue
                    subject = clip(r.subject, HEADER_CHAR_LIMIT)
                    groups.append((subject, r.thread_id, r.lane_ranks, r.score, chunks))
                    if in_scope_only:
                        context_dropped[r.thread_id] = dropped
                    capped_out += len(ranked) - len(capped)
                    taken += len(chunks)
        except ToolError:
            raise
        except InvalidFilterError as e:
            # The message quotes the rejected value, which log_tool_call
            # withheld. Return it to the caller; log only the field name.
            rejections.reject("get_evidence", e.field_name)
            raise ToolError(f"Evidence error: {e}") from e
        except Exception as e:
            # Mirror search_emails: classify before logging or returning,
            # since the embed call's errors can echo the query back.
            safe_error = safe_provider_exception_text(e, secrets)
            log.error("get_evidence error: %s", safe_error)
            raise ToolError(f"Evidence error: {safe_error}") from e

        total_chunks = sum(len(chunks) for _, _, _, _, chunks in groups)
        # What the precision controls left out (#988), on the timing line
        # (counts only) and in the response.
        total_dropped = sum(context_dropped.values())
        scope_emptied = sum(
            1 for _, tid, _, _, chunks in groups if not chunks and context_dropped.get(tid)
        )
        notes: list[str] = []
        if in_scope_only:
            count("evidence_context_dropped", total_dropped)
            count("evidence_threads_scope_emptied", scope_emptied)
            if total_dropped:
                notes.append(
                    f"scope=in_scope left out {total_dropped} context passage(s); "
                    f"{scope_emptied} thread(s) had no in-scope passage."
                )
        if max_chunks_per_thread:
            count("evidence_chunks_capped", capped_out)
        if max_chars_per_chunk:
            count(
                "evidence_chunks_truncated",
                sum(len(c.text) > chunk_chars for *_, chunks in groups for c in chunks),
            )
        collapsed = 0
        if dedupe_attachments:
            collapsed = sum(
                len(carried.get(c.chunk_id, ())) for *_, chunks in groups for c in chunks
            )
            count("evidence_attachment_copies_collapsed", collapsed)
            # ``carried_by`` lists ``MAX_LISTED`` carriers per passage; the
            # rest are counted in ``carried_by_count`` and here.
            count(
                "evidence_carriers_unlisted",
                sum(
                    max(0, len(carried.get(c.chunk_id, ())) - MAX_LISTED)
                    for *_, chunks in groups
                    for c in chunks
                ),
            )
            if collapsed:
                notes.append(
                    f"dedupe_attachments collapsed {collapsed} repeated attachment passage(s)."
                )
        searched_by_source = source_filter is not None and not thread_id
        if searched_by_source:
            count("evidence_threads_source_emptied", source_emptied)
            if source_emptied:
                notes.append(
                    f"source={source}: {source_emptied} ranked thread(s) had no "
                    f"{source} passages and are not listed."
                )
        output = EvidenceOutput(
            chunk_count=total_chunks,
            resolved_from_addr=resolution.address if resolution else None,
            from_name_matches=resolution.senders if resolution else None,
            context_passages_left_out=total_dropped if in_scope_only else None,
            threads_without_source_passages=source_emptied if searched_by_source else None,
            attachment_copies_collapsed=collapsed if dedupe_attachments else None,
            threads=[
                EvidenceThread(
                    thread_id=tid,
                    subject=subject,
                    lane_ranks=lane_ranks if include_scores else None,
                    retrieval_score=score if include_scores else None,
                    chunks=[
                        EvidenceChunk(
                            chunk_id=c.chunk_id,
                            message_id=c.message_id,
                            claimant_id=c.claimant_id,
                            chunk_index=c.chunk_index,
                            source="body" if c.attachment_id is None else "attachment",
                            kind=c.kind,
                            attachment_id=c.attachment_id,
                            attachment_filename=_clip_optional(c.attachment_filename),
                            attachment_mime=_clip_optional(c.attachment_mime),
                            sent_at=c.message_date,
                            occurred_at=c.message_occurred_at,
                            char_start=c.char_start,
                            char_end=c.char_end,
                            text=c.text[:chunk_chars],
                            text_truncated=len(c.text) > chunk_chars,
                            vector_distance=c.score if include_scores else None,
                            selected_by=c.selected_by,
                            source_file=source_ref(c.source_file),
                            scope=_chunk_scope(c, labels),
                            carried_by=(
                                [
                                    EvidenceCarrier(
                                        claimant_id=o.claimant_id,
                                        sent_at=o.message_date,
                                        occurred_at=o.message_occurred_at,
                                        scope=_chunk_scope(o, labels),
                                    )
                                    for o in carried.get(c.chunk_id, [])[:MAX_LISTED]
                                ]
                                if dedupe_attachments and c.attachment_id is not None
                                else None
                            ),
                            carried_by_count=(
                                len(carried.get(c.chunk_id, ()))
                                if dedupe_attachments and c.attachment_id is not None
                                else None
                            ),
                        )
                        for c in chunks
                    ],
                    context_passages_left_out=context_dropped.get(tid),
                )
                for subject, tid, lane_ranks, score, chunks in groups
            ],
        )
        if not groups:
            filters = ", ".join(
                f"{name}={value}"
                for name, value, given in (
                    ("source", source, source_filter is not None),
                    ("scope", scope, in_scope_only),
                )
                if given
            )
            missing = f"No evidence found for: '{query}'" + (f" ({filters})" if filters else "")
            return tool_result("\n".join([missing, *notes]), output)

        lines = [
            f"Evidence for: '{query}'",
            f"{total_chunks} chunk(s) from {len(groups)} thread(s).",
            *notes,
            "",
        ]
        for i, (subject, tid, lane_ranks, score, chunks) in enumerate(groups, 1):
            lines.append(f"[{i}] {subject}")
            lines.append(f"    Thread ID: {tid}")
            if include_scores and lane_ranks:
                lanes = ", ".join(f"{name}#{rank}" for name, rank in sorted(lane_ranks.items()))
                score_str = f" | retrieval score {score:.4f}" if score is not None else ""
                lines.append(f"    Lanes: {lanes}{score_str}")
            left_out = context_dropped.get(tid)
            if not chunks and left_out:
                lines.append(
                    f"    No in-scope passages: {left_out} context passage(s) left out "
                    "(scope=in_scope)."
                )
            elif not chunks:
                lines.append(
                    "    No indexed passages: ask_mailbox shows this thread's indexed "
                    "text instead; read it with get_thread."
                )
            elif left_out:
                lines.append(f"    {left_out} context passage(s) left out (scope=in_scope).")
            for chunk in chunks:
                msg_date = _msg_date(chunk)
                in_scope = _chunk_scope(chunk, labels) == "in_scope"
                lines.append(
                    f"    --- chunk {chunk.chunk_index} | msg {chunk.claimant_id} | {msg_date}"
                    f" | {'in scope' if in_scope else 'context'}"
                )
                if chunk.attachment_id is not None:
                    fname = clip(chunk.attachment_filename or "attachment", HEADER_CHAR_LIMIT)
                    mime = clip(chunk.attachment_mime or "unknown", HEADER_CHAR_LIMIT)
                    lines.append(f'        Source: attachment "{fname}" ({mime})')
                    others = carried.get(chunk.chunk_id, [])
                    if others:
                        unlisted = len(others) - MAX_LISTED
                        lines.append(
                            "        Also carried by: "
                            + ", ".join(
                                f"msg {o.claimant_id} ({_msg_date(o)})" for o in others[:MAX_LISTED]
                            )
                            + (f", and {unlisted} more" if unlisted > 0 else "")
                        )
                elif chunk.kind != "body":
                    lines.append(f"        Source: message body ({chunk.kind})")
                else:
                    lines.append("        Source: message body")
                offsets = f"        Chars {chunk.char_start}-{chunk.char_end}"
                if include_scores:
                    offsets += f" | vector distance {chunk.score:.4f}"
                lines.append(offsets)
                text = chunk.text
                if len(text) > chunk_chars:
                    text = text[:chunk_chars] + " ... [truncated]"
                lines.append(f"        {text}")
            lines.append("")

        return tool_result("\n".join(lines).rstrip(), output)

    @server.tool(
        output_schema=SearchAttachmentsOutput.model_json_schema(),
        annotations=read_only("Search Attachments"),
    )
    @timed_tool("search_attachments")
    async def search_attachments(
        query: str | None = None,
        content_type: str | None = None,
        from_addr: str | None = None,
        sender: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        extracted_only: bool = False,
        limit: int = 20,
    ) -> CallToolResult:
        """
        Search indexed email attachments by filename, MIME type, and
        extracted text.

        Use this for attachment-centric questions — "find the quote PDF
        from Acme", "which emails had W-2 attachments?", "list the
        spreadsheets from last quarter". It matches attachment filenames
        and content types AND the text extracted from them (PDF / OCR /
        document parsing), and reports each attachment's parent thread
        so you can follow up with get_thread or get_evidence.

        With no query it lists attachments by the structured filters
        alone (content_type / date / sender), newest message first.
        Attachments on messages filed in Trash are left out.

        To read what an attachment says, use get_evidence (the matching
        passages of its extracted text, each capped at 1600 characters)
        or ask_mailbox (an answer synthesized from those passages). This
        tool LOCATES attachments and previews their extracted text; none
        of the three returns the whole document.

        Before a no-query coverage scan, disclose the filters and planned
        maximum number of attachment previews; those previews go to the
        calling model, which may be remote. Use the smallest sufficient
        ``limit`` and stay within the requested or approved scope; ask
        before expanding it. The tool does not report an exact total.
        ``from_addr`` selects threads, so previews can come from other
        participants; include that conversation scope in the disclosure.
        ``sender`` instead keeps only attachments the address's own
        messages carried.

        Check each result's ``extraction_status``: anything other than
        ``success`` (``failed``, ``unsupported``, ``too_large``, ``empty`` or null)
        means no extracted text is available, not that the attachment
        says nothing relevant. To check coverage, omit ``query``
        in a separate call with applicable structured filters and leave
        ``extracted_only`` false: a text query cannot reveal unextracted
        files whose filename and MIME type do not match. There is no pagination
        beyond the 50-result cap, so report limited results and unread document text
        as coverage limits; do not claim an exhaustive attachment audit.
        With ``from_addr``, the sender filter runs after a bounded candidate
        scan: even fewer than 50 results (including zero) can omit matches.
        ``sender`` is applied inside the search, before the cap. With
        ``sender``, ``indeterminate`` counts the attachments left out
        because ``sender`` could not decide their carrying message (its
        sender ambiguous or not yet checked, or, for a name or domain
        fragment, its display names not all indexed; not limited by
        ``limit``); report it when it is not 0, and read null as
        unavailable, never as 0.

        Args:
            query: Text to match against filename, MIME type, and
                   extracted attachment text. Omit to list by filter
                   alone.
            content_type: Exact MIME-type filter, e.g. "application/pdf".
            from_addr: Restrict to attachments on threads sent by this
                       address or domain ("jane@example.com",
                       "@example.com").
            sender: Restrict to attachments whose message carrying the
                    attachment is From this address, matched as
                    query_messages matches its ``sender``: a full
                    address exactly, anything else ("@example.com",
                    "Jane") as a case-insensitive substring of the
                    address or display name. Attachments on messages
                    it cannot decide (sender ambiguous or not yet
                    checked; for a fragment, display names not all
                    indexed) are left out and counted as
                    ``indeterminate``.
            date_from: ISO 8601 date lower bound on the message
                       carrying the attachment: its delivery date
                       (occurred_at), else its send date (sent_at).
            date_to: ISO 8601 date upper bound, likewise. For
                     either bound, a date-only value is a UTC day; for
                     the user's time zone give an offset
                     ("2024-01-01T00:00:00-05:00"). The response's
                     ``date_bounds`` echoes the UTC instants applied.
            extracted_only: True to return only attachments whose text
                            extraction succeeded.
            limit: Maximum attachments to return (default 20, clamped
                   to [1, 50]).

        Returns:
            Matching attachments with filename, type, size, parent
            thread, sender, text-extraction status, and a preview of
            the extracted text.
        """
        log_tool_call(
            log,
            "search_attachments",
            {
                "query": query,
                "content_type": content_type,
                "from_addr": from_addr,
                "sender": sender,
                "date_from": date_from,
                "date_to": date_to,
                "extracted_only": extracted_only,
                "limit": limit,
            },
        )
        limit = clamp_int(limit, default=20, minimum=1, maximum=_MAX_SEARCH_LIMIT)
        # Reject a bad date range before any provider or retrieval work.
        try:
            bounds = date_bounds(*validate_date_range(date_from, date_to))
        except InvalidFilterError as e:
            rejections.reject("search_attachments", e.field_name)
            raise ToolError(f"Attachment search error: {e}") from e
        try:
            with stage("attachment_search"):
                found = await asyncio.to_thread(
                    db.search_attachments_with_count,
                    query=query,
                    content_type=content_type,
                    from_addr=from_addr,
                    sender=sender,
                    date_from=date_from,
                    date_to=date_to,
                    extracted_only=extracted_only,
                    limit=limit,
                )
            results = found.results
            count("results", len(results))
        except InvalidFilterError as e:
            # The message quotes the rejected value, which log_tool_call
            # withheld. Return it to the caller; log only the field name.
            rejections.reject("search_attachments", e.field_name)
            raise ToolError(f"Attachment search error: {e}") from e
        except Exception as e:
            # search_attachments is local-DB work (FTS + joins), but an
            # SQLite or row-conversion error can quote the query or stored
            # mail, so it is classified like a provider failure: type only
            # (#257).
            safe_error = safe_provider_exception_text(e, secrets)
            log.error("search_attachments error: %s", safe_error)
            raise ToolError(f"Attachment search error: {safe_error}") from e

        output = SearchAttachmentsOutput(
            date_bounds=bounds,
            results=[
                AttachmentHit(
                    attachment_id=a.attachment_id,
                    filename=clip(a.filename, HEADER_CHAR_LIMIT),
                    content_type=clip(a.content_type, HEADER_CHAR_LIMIT),
                    size_bytes=a.size_bytes,
                    thread_id=a.thread_id,
                    message_id=a.message_id,
                    claimant_id=a.claimant_id,
                    subject=clip(a.subject, HEADER_CHAR_LIMIT),
                    folder=a.folder,
                    date_last=a.date_last,
                    sent_at=a.sent_at,
                    occurred_at=a.occurred_at,
                    senders=[clip(s, HEADER_CHAR_LIMIT) for s in a.senders[:MAX_LISTED]],
                    sender_count=len(a.senders),
                    extraction_status=a.extraction_status,
                    text_snippet=a.text_snippet,
                    source_file=source_ref(a.source_file),
                )
                for a in results
            ],
            indeterminate=found.indeterminate,
        )
        # Stated whenever ``sender`` is given, so a short or empty list is
        # never read as complete when carrying messages could not be
        # decided (#1204). Fixed text and a count only.
        indeterminate_line = None
        if given_sender := _given(sender):
            # The causes the leaf can have for this value, as
            # query_messages names them: a name or domain fragment that
            # matches nothing is unknown while display names are not all
            # indexed (#1140); a full address never is.
            causes = "sender ambiguous or not yet checked"
            if address_match_mode(given_sender) == "substring":
                causes += "; display names not all indexed"
            if found.indeterminate is None:
                indeterminate_line = (
                    "indeterminate: unavailable (the count of attachments whose carrying "
                    f"message sender could not decide ({causes}) failed; such attachments "
                    "may be left out)"
                )
            else:
                count("indeterminate", found.indeterminate)
                indeterminate_line = f"indeterminate: {found.indeterminate}"
                if found.indeterminate:
                    indeterminate_line += (
                        " (attachments whose carrying message sender could neither accept "
                        f"nor reject: {causes}; not in the results)"
                    )
        bounds_line = describe_date_bounds(bounds)
        if not results:
            # Undecided attachments may still match.
            known = found.indeterminate is None or found.indeterminate > 0
            lines = [
                "No attachments are known to match."
                if indeterminate_line and known
                else "No attachments found."
            ]
            lines += [line for line in (indeterminate_line, bounds_line) if line]
            return tool_result("\n".join(lines), output)

        lines = [f"Found {len(results)} attachment(s):"]
        if indeterminate_line:
            lines.append(indeterminate_line)
        if bounds_line:
            lines.append(bounds_line)
        lines.append("")
        for i, a in enumerate(results, 1):
            size_kb = a.size_bytes / 1024
            fname = clip(a.filename, HEADER_CHAR_LIMIT)
            mime = clip(a.content_type, HEADER_CHAR_LIMIT)
            lines.append(f"[{i}] {fname}  ({mime}, {size_kb:.1f} KB)")
            lines.append(f"    Thread: {clip(a.subject, HEADER_CHAR_LIMIT)}  [{a.folder}]")
            lines.append(
                f"    Thread ID: {a.thread_id} | Message-ID: {a.message_id} "
                f"| Claimant ID: {a.claimant_id}"
            )
            lines.append(f"    Sent: {(a.sent_at or '')[:10] or 'unknown date'}")
            if a.occurred_at:
                lines.append(f"    Delivered: {a.occurred_at[:10]}")
            if a.senders:
                senders = ", ".join(clip(s, HEADER_CHAR_LIMIT) for s in a.senders[:3])
                lines.append(f"    From: {senders}")
            lines.append(f"    Text extraction: {a.extraction_status or 'not extracted'}")
            if a.text_snippet:
                lines.append(f"    Snippet: {a.text_snippet.strip()}")
            lines.append("")

        return tool_result("\n".join(lines).rstrip(), output)
