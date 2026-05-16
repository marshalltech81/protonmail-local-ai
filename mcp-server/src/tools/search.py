"""
Search tools — Group 1 (most frequently called).
Semantic, keyword, and hybrid search over the SQLite index.
"""

import asyncio
import logging

from mcp.types import TextContent

from ..lib.embed import embed_query
from ..lib.security import safe_exception_text, safe_provider_exception_text
from ..lib.validation import clamp_int

log = logging.getLogger("mcp.tools.search")

# Hard ceiling on ``limit``. MCP tool calls can originate from an LLM,
# which may hallucinate values like ``limit=100000`` — without a clamp
# that becomes a large FTS + vector + RRF workload and a large result
# payload to ship back through the protocol. 50 is well above any
# reasonable interactive use of the search tool.
_MAX_SEARCH_LIMIT = 50

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

    @server.tool()
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
    ) -> list[TextContent]:
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
        ``Thread ID``. To read attachment content, the only path is
        ``ask_mailbox`` — neither this tool nor get_thread surface
        attachment chunks. Never invent a thread_id from the subject —
        IDs are opaque values returned only from this tool,
        list_threads, or get_message.

        Args:
            query: Natural language or keyword query
            mode: "hybrid" (default), "semantic", or "keyword"
                  hybrid = BM25 + vector merged via RRF (best for most queries)
                  semantic = vector similarity only (best for conceptual queries)
                  keyword = BM25 only (best for exact names, numbers, dates)
            folders: Filter to specific folders e.g. ["INBOX", "Sent"]
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

        Returns:
            List of matching email threads with subject, participants,
            dates, folder, and a short snippet.
        """
        log.info(
            "tool=search_emails %s",
            {
                k: v
                for k, v in {
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
                }.items()
                if v is not None
            },
        )
        if mode not in _VALID_SEARCH_MODES:
            return [
                TextContent(
                    type="text",
                    text=(f"Invalid mode {mode!r}. Use 'hybrid', 'semantic', or 'keyword'."),
                )
            ]
        # Clamp to [1, _MAX_SEARCH_LIMIT] so an out-of-range or
        # non-numeric caller value never drives an unbounded query
        # against the index. clamp_int returns the default (10) when the
        # raw value is missing or unparseable rather than raising a bare
        # ValueError before the try/except below.
        limit = clamp_int(limit, default=10, minimum=1, maximum=_MAX_SEARCH_LIMIT)

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
        # When the lookup yields nothing, short-circuit with an
        # honest empty result rather than silently dropping the
        # filter and returning unrelated threads.
        if from_name and not from_addr:
            try:
                contacts = await asyncio.to_thread(db.find_contact, from_name, 1, senders_only=True)
            except Exception as e:
                safe_error = safe_exception_text(e, secrets)
                log.error("search_emails: find_contact(%r) failed: %s", from_name, safe_error)
                return [TextContent(type="text", text=f"Search error: {safe_error}")]
            if not contacts:
                return [
                    TextContent(
                        type="text",
                        text=(
                            f"No results found for: '{query}' "
                            f"(no contact matched from_name={from_name!r})"
                        ),
                    )
                ]
            from_addr = contacts[0]["email"]

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
                )

            if not results:
                return [TextContent(type="text", text=f"No results found for: '{query}'")]

            output = [f"Found {len(results)} thread(s) for: '{query}'\n"]
            for i, r in enumerate(results, 1):
                output.append(
                    f"{i}. [{r.folder}] {r.subject}\n"
                    f"   Participants: {', '.join(r.participants[:3])}"
                    f"{'...' if len(r.participants) > 3 else ''}\n"
                    f"   Date: {r.date_last.strftime('%Y-%m-%d')}"
                    f" | Messages: {len(r.message_ids)}"
                    f" | {'📎 ' if r.has_attachments else ''}"
                    f"Thread ID: {r.thread_id}\n"
                    f"   {r.snippet[:120]}...\n"
                )

            return [TextContent(type="text", text="\n".join(output))]

        except Exception as e:
            # Provider-SDK status errors (embed call, reranker call) can
            # echo request/response body fragments — for search that
            # would leak the user's query string into logs and the MCP
            # response. ``safe_provider_exception_text`` reduces SDK
            # status errors to ``type + status`` only and falls through
            # to the standard secret-redacting formatter for non-provider
            # exceptions (DB errors, validation errors), so DB diagnostics
            # keep their detail. The inner ``find_contact`` except above
            # stays on ``safe_exception_text`` because that path is pure
            # local DB work.
            safe_error = safe_provider_exception_text(e, secrets)
            log.error("search_emails error: %s", safe_error)
            return [TextContent(type="text", text=f"Search error: {safe_error}")]

    @server.tool()
    async def get_evidence(
        query: str,
        thread_id: str | None = None,
        folders: list[str] | None = None,
        from_addr: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
        limit: int = 12,
        include_scores: bool = False,
    ) -> list[TextContent]:
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
        evidence across the whole mailbox.

        Args:
            query: The question or topic to gather evidence for.
            thread_id: Optional opaque thread ID to scope evidence to
                       one thread. Obtain it from search_emails or
                       list_threads — never invent it from a subject.
            folders: Restrict to specific folders, e.g. ["INBOX", "Sent"].
            from_addr: Restrict to a sender ADDRESS or domain
                       ("jane@example.com", "@example.com"). For a
                       person's name, resolve it via find_contact first.
            date_from: ISO 8601 date lower bound, e.g. "2024-01-01".
            date_to: ISO 8601 date upper bound, e.g. "2024-12-31".
            has_attachments: True to restrict to threads with attachments.
            limit: Maximum evidence chunks to return (default 12,
                   clamped to [1, 50]).
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
        log.info(
            "tool=get_evidence %s",
            {
                k: v
                for k, v in {
                    "query": query,
                    "thread_id": thread_id,
                    "folders": folders,
                    "from_addr": from_addr,
                    "date_from": date_from,
                    "date_to": date_to,
                    "has_attachments": has_attachments,
                    "limit": limit,
                    "include_scores": include_scores,
                }.items()
                if v is not None
            },
        )
        if not query or not query.strip():
            return [TextContent(type="text", text="Provide a query to gather evidence for.")]
        # Same clamp ceiling as search_emails — ``limit`` here counts
        # evidence chunks, and an LLM-inflated value would drive a large
        # per-thread chunk fetch and an oversized response payload.
        limit = clamp_int(limit, default=12, minimum=1, maximum=_MAX_SEARCH_LIMIT)

        # groups: list of (subject, thread_id, lane_ranks | None,
        # thread_score | None, chunks). ``lane_ranks`` is None for the
        # thread-scoped path because that path bypasses RRF fusion.
        groups: list[tuple[str, str, dict[str, int] | None, float | None, list]] = []
        try:
            if thread_id:
                thread = await asyncio.to_thread(db.get_thread, thread_id)
                if not thread:
                    return [TextContent(type="text", text=f"Thread not found: {thread_id}")]
                embedding = await embed_query(embed_client, query, expected_embed_dim)
                grouped = await asyncio.to_thread(
                    db.get_evidence_chunks_for_threads, [thread_id], embedding, limit
                )
                chunks = grouped.get(thread_id, [])
                if chunks:
                    groups.append((thread.subject, thread_id, None, None, chunks))
            else:
                embedding = await embed_query(embed_client, query, expected_embed_dim)
                results = await asyncio.to_thread(
                    db.hybrid_search,
                    query_text=query,
                    query_embedding=embedding,
                    folders=folders,
                    from_addr=from_addr,
                    date_from=date_from,
                    date_to=date_to,
                    has_attachments=has_attachments,
                    limit=limit,
                    with_evidence=True,
                    reranker=reranker,
                )
                # Flatten thread-ranked evidence into a flat chunk budget:
                # ``limit`` counts chunks, threads are already ranked, and
                # chunks within a thread are ranked by similarity. Once the
                # budget is spent the slice yields [] and the thread drops.
                taken = 0
                for r in results:
                    chunks = r.evidence_chunks[: limit - taken]
                    if not chunks:
                        continue
                    groups.append((r.subject, r.thread_id, r.lane_ranks, r.score, chunks))
                    taken += len(chunks)
        except Exception as e:
            # Mirror search_emails: provider-SDK status errors (the embed
            # call) can echo the query back, so reduce them to type +
            # status and redact any quoted secret.
            safe_error = safe_provider_exception_text(e, secrets)
            log.error("get_evidence error: %s", safe_error)
            return [TextContent(type="text", text=f"Evidence error: {safe_error}")]

        total_chunks = sum(len(chunks) for _, _, _, _, chunks in groups)
        if total_chunks == 0:
            return [TextContent(type="text", text=f"No evidence found for: '{query}'")]

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
                    f"    --- chunk {chunk.chunk_index} | msg {chunk.message_id} | {msg_date}"
                )
                if chunk.attachment_id is not None:
                    fname = chunk.attachment_filename or "attachment"
                    mime = chunk.attachment_mime or "unknown"
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

        return [TextContent(type="text", text="\n".join(lines).rstrip())]

    @server.tool()
    async def search_attachments(
        query: str | None = None,
        content_type: str | None = None,
        from_addr: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        extracted_only: bool = False,
        limit: int = 20,
    ) -> list[TextContent]:
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
        first.

        To read the full text inside an attachment, use ask_mailbox or
        get_evidence — this tool LOCATES attachments and previews their
        extracted text; it does not return the whole document.

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
        log.info(
            "tool=search_attachments %s",
            {
                k: v
                for k, v in {
                    "query": query,
                    "content_type": content_type,
                    "from_addr": from_addr,
                    "date_from": date_from,
                    "date_to": date_to,
                    "extracted_only": extracted_only,
                    "limit": limit,
                }.items()
                if v is not None
            },
        )
        limit = clamp_int(limit, default=20, minimum=1, maximum=_MAX_SEARCH_LIMIT)
        try:
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
        except Exception as e:
            # search_attachments is pure local-DB work (FTS + joins); the
            # only expected failure is a bad date filter (ValueError) or
            # a DB error. No provider call, so the standard secret-aware
            # formatter is enough.
            safe_error = safe_exception_text(e, secrets)
            log.error("search_attachments error: %s", safe_error)
            return [TextContent(type="text", text=f"Attachment search error: {safe_error}")]

        if not results:
            return [TextContent(type="text", text="No attachments found.")]

        lines = [f"Found {len(results)} attachment(s):", ""]
        for i, a in enumerate(results, 1):
            size_kb = a.size_bytes / 1024
            lines.append(f"[{i}] {a.filename}  ({a.content_type}, {size_kb:.1f} KB)")
            lines.append(f"    Thread: {a.subject}  [{a.folder}]")
            lines.append(f"    Thread ID: {a.thread_id} | Message-ID: {a.message_id}")
            lines.append(f"    Date: {a.date_last.strftime('%Y-%m-%d')}")
            if a.senders:
                lines.append(f"    From: {', '.join(a.senders[:3])}")
            lines.append(f"    Text extraction: {a.extraction_status or 'not extracted'}")
            if a.text_snippet:
                lines.append(f"    Snippet: {a.text_snippet.strip()}")
            lines.append("")

        return [TextContent(type="text", text="\n".join(lines).rstrip())]
