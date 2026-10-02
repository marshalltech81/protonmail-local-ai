"""
Search tools — Group 1 (most frequently called).
Semantic, keyword, and hybrid search over the SQLite index.
"""

import asyncio
import logging

from fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from ..lib.embed import embed_query
from ..lib.security import log_tool_call, safe_provider_exception_text
from ..lib.sqlite import (
    PROMPT_EVIDENCE_CHUNKS_PER_THREAD,
    InvalidFilterError,
    VectorLanesUnavailableError,
    normalize_authority_class,
    validate_date_range,
)
from ..lib.timings import count, rerank_mode, stage, timed_tool
from ..lib.validation import clamp_int
from .intelligence import _MAX_ASK_THREADS, clamp_ask_threads, select_ask_threads
from .outputs import (
    HEADER_CHAR_LIMIT,
    MAX_LISTED,
    AttachmentHit,
    EvidenceChunk,
    EvidenceOutput,
    EvidenceThread,
    SearchAttachmentsOutput,
    SearchEmailsOutput,
    clip,
    source,
    thread_summary,
    tool_result,
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
    search. ``None`` skips the check (fresh install pre-indexer-run).
    """
    secrets = list(secret_values or ())
    # Config identifier for the per-call timing line.
    timing_config = {"rerank": rerank_mode(reranker)}

    @server.tool(output_schema=SearchEmailsOutput.model_json_schema())
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
            date_from: ISO 8601 date lower bound e.g. "2024-01-01"
            date_to: ISO 8601 date upper bound e.g. "2024-12-31"
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
            validate_date_range(date_from, date_to)
            authority_class = normalize_authority_class(authority_class)
        except InvalidFilterError as e:
            log.warning("search_emails rejected invalid %s", e.field_name)
            raise ToolError(f"Search error: {e}") from e

        # Resolve ``from_name`` -> canonical SENDER address via
        # find_contact. Skipped when the caller already passed a
        # strict ``from_addr`` — explicit always beats lookup. We
        # restrict find_contact to ``senders_only=True`` because the
        # next step plugs the resolved address into
        # hybrid_search(from_addr=...), which filters by From-line
        # address. Resolving over the broader participants set could
        # promote a frequent recipient/CC contact (a name on every
        # mailing-list reply but never a sender) and leave the
        # search returning zero matches. Ranking by sender count
        # picks the right Smith for the "messages from Smith" intent.
        # Senders are counted over the search's folder scope (``folders``,
        # else the default Trash exclusion), so the lookup cannot pick a
        # sender whose threads the search would then filter out.
        # When the lookup yields nothing, short-circuit with an
        # honest empty result rather than silently dropping the
        # filter and returning unrelated threads.
        resolved_from_addr = None
        if from_name and not from_addr:
            try:
                with stage("contact_lookup"):
                    contacts = await asyncio.to_thread(
                        db.find_contact, from_name, 1, senders_only=True, folders=folders
                    )
            except Exception as e:
                # Local-DB work, but a conversion error can quote stored
                # mail: the same classification as provider failures (#257).
                safe_error = safe_provider_exception_text(e, secrets)
                log.error("search_emails: find_contact lookup failed: %s", safe_error)
                raise ToolError(f"Search error: {safe_error}") from e
            if not contacts:
                return tool_result(
                    f"No results found for: '{query}' (no contact matched from_name={from_name!r})",
                    SearchEmailsOutput(mode=mode, resolved_from_addr=None, results=[]),
                )
            from_addr = resolved_from_addr = contacts[0]["email"]

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
                results=[thread_summary(r) for r in results],
            )
            if not results:
                return tool_result(f"No results found for: '{query}'", output)

            lines = [f"Found {len(results)} thread(s) for: '{query}'\n"]
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
            log.warning("search_emails rejected invalid %s", e.field_name)
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

    @server.tool(output_schema=EvidenceOutput.model_json_schema())
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
        answer, pass the same question, filters and max_threads: the
        result is the evidence that answer retrieved, in the same order.

        Args:
            query: The question or topic to gather evidence for.
            thread_id: Optional opaque thread ID to scope evidence to
                       one thread. Obtain it from search_emails or
                       list_threads — never invent it from a subject.
                       Cannot be combined with folders, from_addr,
                       date_from, date_to, has_attachments or
                       max_threads.
            folders: Restrict to threads with a message in these folders,
                     e.g. ["INBOX", "Sent"]. Without it, threads filed
                     only in Trash are left out; name "Trash" to
                     include them.
            from_addr: Restrict to a sender ADDRESS or domain
                       ("jane@example.com", "@example.com"). For a
                       person's name, resolve it via find_contact first.
            date_from: ISO 8601 date lower bound, e.g. "2024-01-01".
            date_to: ISO 8601 date upper bound, e.g. "2024-12-31".
            has_attachments: True to restrict to threads with attachments.
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
                "max_threads": max_threads,
                "limit": limit,
                "include_scores": include_scores,
            },
        )
        if not query or not query.strip():
            raise ToolError("Provide a query to gather evidence for.")
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
                log.warning("get_evidence rejected invalid %s", e.field_name)
                raise ToolError(f"Evidence error: {e}") from e

        # groups: list of (subject, thread_id, lane_ranks | None,
        # thread_score | None, chunks). ``lane_ranks`` is None for the
        # thread-scoped path because that path bypasses RRF fusion.
        groups: list[tuple[str, str, dict[str, int] | None, float | None, list]] = []
        try:
            if thread_id:
                thread = await asyncio.to_thread(db.get_thread, thread_id)
                if not thread:
                    raise ToolError(f"Thread not found: {thread_id}")
                embedding = await embed_query(embed_client, query, expected_embed_dim)
                # The same selection as ask_mailbox: chunks of attachments
                # whose filename or MIME type the query matches lead.
                with stage("evidence_fetch"):
                    grouped = await asyncio.to_thread(
                        db.get_query_evidence_chunks, query, [thread_id], embedding, limit
                    )
                chunks = grouped.get(thread_id, [])
                count("evidence_chunks", len(chunks))
                if chunks:
                    groups.append(
                        (clip(thread.subject, HEADER_CHAR_LIMIT), thread_id, None, None, chunks)
                    )
            else:
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
                )
                count("results", len(results))
                # Flatten thread-ranked evidence into a flat chunk budget:
                # ``limit`` counts chunks, threads are already ranked, and
                # chunks within a thread are ranked by similarity. Once the
                # budget is spent the slice yields [] and the thread drops.
                taken = 0
                for r in results:
                    chunks = r.evidence_chunks[: limit - taken]
                    if not chunks:
                        continue
                    subject = clip(r.subject, HEADER_CHAR_LIMIT)
                    groups.append((subject, r.thread_id, r.lane_ranks, r.score, chunks))
                    taken += len(chunks)
        except ToolError:
            raise
        except InvalidFilterError as e:
            # The message quotes the rejected value, which log_tool_call
            # withheld. Return it to the caller; log only the field name.
            log.warning("get_evidence rejected invalid %s", e.field_name)
            raise ToolError(f"Evidence error: {e}") from e
        except Exception as e:
            # Mirror search_emails: classify before logging or returning,
            # since the embed call's errors can echo the query back.
            safe_error = safe_provider_exception_text(e, secrets)
            log.error("get_evidence error: %s", safe_error)
            raise ToolError(f"Evidence error: {safe_error}") from e

        total_chunks = sum(len(chunks) for _, _, _, _, chunks in groups)
        output = EvidenceOutput(
            chunk_count=total_chunks,
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
                            attachment_id=c.attachment_id,
                            attachment_filename=_clip_optional(c.attachment_filename),
                            attachment_mime=_clip_optional(c.attachment_mime),
                            message_date=c.message_date,
                            char_start=c.char_start,
                            char_end=c.char_end,
                            text=c.text[:_EVIDENCE_CHUNK_CHARS],
                            text_truncated=len(c.text) > _EVIDENCE_CHUNK_CHARS,
                            vector_distance=c.score if include_scores else None,
                            source_file=source(c.source_file),
                        )
                        for c in chunks
                    ],
                )
                for subject, tid, lane_ranks, score, chunks in groups
            ],
        )
        if total_chunks == 0:
            return tool_result(f"No evidence found for: '{query}'", output)

        lines = [
            f"Evidence for: '{query}'",
            f"{total_chunks} chunk(s) from {len(groups)} thread(s).",
            "",
        ]
        for i, (subject, tid, lane_ranks, score, chunks) in enumerate(groups, 1):
            lines.append(f"[{i}] {subject}")
            lines.append(f"    Thread ID: {tid}")
            if include_scores and lane_ranks:
                lanes = ", ".join(f"{name}#{rank}" for name, rank in sorted(lane_ranks.items()))
                score_str = f" | retrieval score {score:.4f}" if score is not None else ""
                lines.append(f"    Lanes: {lanes}{score_str}")
            for chunk in chunks:
                msg_date = (chunk.message_date or "")[:10] or "unknown date"
                lines.append(
                    f"    --- chunk {chunk.chunk_index} | msg {chunk.claimant_id} | {msg_date}"
                )
                if chunk.attachment_id is not None:
                    fname = clip(chunk.attachment_filename or "attachment", HEADER_CHAR_LIMIT)
                    mime = clip(chunk.attachment_mime or "unknown", HEADER_CHAR_LIMIT)
                    lines.append(f'        Source: attachment "{fname}" ({mime})')
                else:
                    lines.append("        Source: message body")
                offsets = f"        Chars {chunk.char_start}-{chunk.char_end}"
                if include_scores:
                    offsets += f" | vector distance {chunk.score:.4f}"
                lines.append(offsets)
                text = chunk.text
                if len(text) > _EVIDENCE_CHUNK_CHARS:
                    text = text[:_EVIDENCE_CHUNK_CHARS] + " ... [truncated]"
                lines.append(f"        {text}")
            lines.append("")

        return tool_result("\n".join(lines).rstrip(), output)

    @server.tool(output_schema=SearchAttachmentsOutput.model_json_schema())
    @timed_tool("search_attachments")
    async def search_attachments(
        query: str | None = None,
        content_type: str | None = None,
        from_addr: str | None = None,
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
        alone (content_type / date / sender), newest thread activity
        first. Attachments on messages filed in Trash are left out.

        To read what an attachment says, use get_evidence (the matching
        passages of its extracted text, each capped at 1600 characters)
        or ask_mailbox (an answer synthesized from those passages). This
        tool LOCATES attachments and previews their extracted text; none
        of the three returns the whole document.

        Args:
            query: Text to match against filename, MIME type, and
                   extracted attachment text. Omit to list by filter
                   alone.
            content_type: Exact MIME-type filter, e.g. "application/pdf".
            from_addr: Restrict to attachments on threads sent by this
                       address or domain ("jane@example.com",
                       "@example.com").
            date_from: ISO 8601 date lower bound (parent thread activity).
            date_to: ISO 8601 date upper bound.
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
                "date_from": date_from,
                "date_to": date_to,
                "extracted_only": extracted_only,
                "limit": limit,
            },
        )
        limit = clamp_int(limit, default=20, minimum=1, maximum=_MAX_SEARCH_LIMIT)
        # Reject a bad date range before any provider or retrieval work.
        try:
            validate_date_range(date_from, date_to)
        except InvalidFilterError as e:
            log.warning("search_attachments rejected invalid %s", e.field_name)
            raise ToolError(f"Attachment search error: {e}") from e
        try:
            with stage("attachment_search"):
                results = await asyncio.to_thread(
                    db.search_attachments,
                    query=query,
                    content_type=content_type,
                    from_addr=from_addr,
                    date_from=date_from,
                    date_to=date_to,
                    extracted_only=extracted_only,
                    limit=limit,
                )
            count("results", len(results))
        except InvalidFilterError as e:
            # The message quotes the rejected value, which log_tool_call
            # withheld. Return it to the caller; log only the field name.
            log.warning("search_attachments rejected invalid %s", e.field_name)
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
                    senders=[clip(s, HEADER_CHAR_LIMIT) for s in a.senders[:MAX_LISTED]],
                    sender_count=len(a.senders),
                    extraction_status=a.extraction_status,
                    text_snippet=a.text_snippet,
                    source_file=source(a.source_file),
                )
                for a in results
            ]
        )
        if not results:
            return tool_result("No attachments found.", output)

        lines = [f"Found {len(results)} attachment(s):", ""]
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
            lines.append(f"    Date: {a.date_last.strftime('%Y-%m-%d')}")
            if a.senders:
                senders = ", ".join(clip(s, HEADER_CHAR_LIMIT) for s in a.senders[:3])
                lines.append(f"    From: {senders}")
            lines.append(f"    Text extraction: {a.extraction_status or 'not extracted'}")
            if a.text_snippet:
                lines.append(f"    Snippet: {a.text_snippet.strip()}")
            lines.append("")

        return tool_result("\n".join(lines).rstrip(), output)
