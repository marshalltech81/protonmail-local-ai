"""
Search tools — Group 1 (most frequently called).
Semantic, keyword, and hybrid search over the SQLite index.
"""

import asyncio
import logging

from mcp.types import TextContent

from ..lib.validation import clamp_int

log = logging.getLogger("mcp.tools.search")

# Hard ceiling on ``limit``. MCP tool calls can originate from an LLM,
# which may hallucinate values like ``limit=100000`` — without a clamp
# that becomes a large FTS + vector + RRF workload and a large result
# payload to ship back through the protocol. 50 is well above any
# reasonable interactive use of the search tool.
_MAX_SEARCH_LIMIT = 50

_VALID_SEARCH_MODES = frozenset({"hybrid", "semantic", "keyword"})


def register_search_tools(server, db, ollama, *, reranker=None):
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
            limit: Maximum number of threads to return (default 10)

        Returns:
            List of matching email threads with subject, participants,
            dates, folder, and a short snippet.
        """
        # Tool-call audit: ``locals()`` at the top of an async tool
        # function captures only the call arguments (no other locals
        # exist yet). Logging non-None args lets us see *which tool +
        # what filters* the model actually invoked, which is the
        # difference between "model picked search_emails(from_name=...)"
        # vs "model picked search_emails(query=...)" when debugging
        # routing. Same one-liner is added to every registered tool.
        log.info("tool=search_emails %s", {k: v for k, v in locals().items() if v is not None})
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
                log.error(f"search_emails: find_contact({from_name!r}) failed: {e}")
                return [TextContent(type="text", text=f"Search error: {e}")]
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
                    limit=limit,
                )
            elif mode == "semantic":
                embedding = await ollama.embed(query)
                results = await asyncio.to_thread(
                    db.semantic_search,
                    query_embedding=embedding,
                    folders=folders,
                    from_addr=from_addr,
                    date_from=date_from,
                    date_to=date_to,
                    has_attachments=has_attachments,
                    limit=limit,
                )
            else:  # hybrid (default)
                embedding = await ollama.embed(query)
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
            log.error(f"search_emails error: {e}")
            return [TextContent(type="text", text=f"Search error: {e}")]
