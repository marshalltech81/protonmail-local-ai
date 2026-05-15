"""
SQLite query layer for the MCP server.
Read-only access to the index built by the indexer service.
Supports BM25 keyword search, vector similarity search, and hybrid fusion.
"""

import json
import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parseaddr
from pathlib import Path

import sqlite_vec

from .reranker import RerankerBackend

log = logging.getLogger("mcp.sqlite")


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
    _, addr = parseaddr(value)
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

# Oversample factor for the chunk and attachment FTS lanes, where one
# thread can legitimately own many matching rows (a long thread, a
# popular term). Without enough oversample, those threads absorb every
# top-N row and other threads never enter the lane. The value is a
# deliberate over-correction of the prior 3× — empirically a ~10×
# oversample lets ``_best_per_thread`` still surface enough threads
# even on dense matches.
_CHUNK_LANE_OVERSAMPLE = 10


def _matches_sender(result, from_addr_lower: str) -> bool:
    """True if ``from_addr_lower`` matches one of the thread's senders.

    Senders is the list of ``From`` addresses recorded on the thread.
    Match mode depends on the shape of the query:

    * A full address (``bob@example.com``) is compared by canonical
      equality so that case variation in the stored display string
      (``Bob@Example.com``, ``Bob Smith <bob@example.com>``) still matches.
    * A bare name (``bob``) or domain fragment (``@example.com``,
      ``example.com``) keeps the previous substring behavior against the
      lowercased display string, since those shapes cannot canonicalize.
    """
    haystack = result.senders
    canonical_query = canonical_addr(from_addr_lower)
    # A canonicalizable full address requires a non-empty local part.
    # ``canonical_addr`` still returns the input for a bare domain like
    # ``@example.com`` because the ``@`` check passes, but equality against
    # ``bob@example.com`` would then miss. Route the domain-only shape through
    # the substring fallback so ``from_addr="@example.com"`` still behaves
    # like a domain filter.
    if canonical_query and not canonical_query.startswith("@"):
        return any(canonical_addr(s) == canonical_query for s in haystack)
    return any(from_addr_lower in s.lower() for s in haystack)


def _sanitize_fts_query(query: str) -> str:
    """Build a safe FTS5 MATCH expression from arbitrary user input.

    FTS5's MATCH grammar treats punctuation, hyphens, quotes, colons, and
    trailing operators as syntax, so raw human search strings often fail to
    parse (``"Who's the landlord?"``, ``alice@example.com`` with unbalanced
    quotes, etc.). The failure mode of the previous implementation was to
    catch the error and silently return no results, which looks to the user
    like their query "doesn't match anything."

    The sanitizer extracts word-like tokens (keeping ``@ . -`` so email
    addresses and hostnames survive), quotes each one as an FTS phrase, and
    joins with ``OR`` so any-term match is preserved — the typical
    search-box expectation.
    """
    tokens = re.findall(r"[\w@.\-]+", query or "")
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


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
    left ``None`` for body chunks. ``ask_mailbox``'s "reads attachment
    content" promise depends on these fields reaching the LLM context —
    without filename/MIME provenance the model sees opaque text and
    cannot cite the source attachment.
    """

    chunk_id: str
    message_id: str
    thread_id: str
    chunk_index: int
    text: str
    char_start: int
    char_end: int
    score: float = 0.0
    attachment_id: str | None = None
    attachment_filename: str | None = None
    attachment_mime: str | None = None


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
        thread_id=r["thread_id"],
        chunk_index=int(r["chunk_index"]),
        text=r["text"],
        char_start=int(r["char_start"]),
        char_end=int(r["char_end"]),
        score=float(r["score"]) if "score" in keys and r["score"] is not None else 0.0,
        attachment_id=r["attachment_id"] if "attachment_id" in keys else None,
        attachment_filename=(r["attachment_filename"] if "attachment_filename" in keys else None),
        attachment_mime=r["attachment_mime"] if "attachment_mime" in keys else None,
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
        self._closed = False
        # Fail fast at startup with the same checks ``_connect`` runs
        # on every access. Catches a missing volume / typo'd
        # SQLITE_PATH / unhealthy indexer at process start instead of
        # waiting for the first tool call.
        self._validate_path()

    def close(self) -> None:
        # No-op kept for API compatibility — per-access connections
        # are opened and closed inside each read helper, so there is
        # no persistent resource to release.
        # Existing test fixtures and main-shutdown code that call
        # ``db.close()`` continue to work; the flag is preserved so
        # any caller inspecting it sees the historical semantics.
        self._closed = True

    @property
    def _conn(self) -> sqlite3.Connection:
        """Compatibility escape hatch; caller owns closing this connection."""
        return self._connect()

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
        uri = f"file:{self.path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA query_only = ON")
        return conn

    def _fetchall(self, sql: str, params=()) -> list[sqlite3.Row]:
        with closing(self._connect()) as conn:
            return conn.execute(sql, params).fetchall()

    def _fetchone(self, sql: str, params=()) -> sqlite3.Row | None:
        with closing(self._connect()) as conn:
            return conn.execute(sql, params).fetchone()

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
        limit: int = 10,
        with_evidence: bool = False,
        reranker: RerankerBackend | None = None,
    ) -> list[ThreadResult]:
        oversample = (
            _FILTERED_OVERSAMPLE
            if self._has_post_fusion_filter(folders, from_addr, date_from, date_to, has_attachments)
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
        vec_results = self._vector_search(query_embedding, fetch_limit)
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
        chunk_hits = self._chunk_vector_search(
            query_embedding, fetch_limit * _CHUNK_LANE_OVERSAMPLE
        )
        fused = self._reciprocal_rank_fusion(bm25_results, vec_results, chunk_hits)
        filtered = self._apply_filters(
            fused, folders, from_addr, date_from, date_to, has_attachments
        )

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
            # breaking ``ask_mailbox``'s "reads attachment content"
            # promise whenever the carrier email won by metadata but
            # the attachment chunks didn't enter the chunk-vec pool.
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
            attachment_won = self._attachment_won_thread_ids(
                query_text, wanted, folders, date_from, date_to, has_attachments
            )
            grouped = self.get_evidence_chunks_for_threads(
                wanted,
                query_embedding,
                per_thread_limit=3,
                attachment_won_thread_ids=attachment_won,
            )
            for result in candidates:
                result.evidence_chunks = grouped.get(result.thread_id, [])

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
        boilerplate.
        """
        if result.evidence_chunks:
            return f"Subject: {result.subject}\n\n{result.evidence_chunks[0].text}"
        return f"Subject: {result.subject}\n\n{result.snippet}"

    def _apply_rerank(
        self,
        query: str,
        candidates: list[ThreadResult],
        reranker: RerankerBackend,
        limit: int,
    ) -> list[ThreadResult]:
        """Reorder ``candidates`` via the reranker and truncate to ``limit``.

        ``top_n`` is passed through to the reranker as the caller's
        ``limit`` so a caller asking for 20 results doesn't get
        silently capped at the reranker's default ``top_n=10``. The
        outer ``[:limit]`` is then redundant for the success path but
        kept for the rerank-failure fallback below.

        On reranker failure (returns empty list), the candidates fall
        back to RRF order — so a rerank outage degrades quality without
        failing the whole query.
        """
        docs = [self._candidate_text(c) for c in candidates]
        scored = reranker.rerank(query, docs, top_n=limit)
        if not scored:
            return candidates[:limit]
        reordered: list[ThreadResult] = []
        for orig_idx, score in scored:
            if 0 <= orig_idx < len(candidates):
                result = candidates[orig_idx]
                result.score = score
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
        limit: int = 10,
    ) -> list[ThreadResult]:
        # Previously dropped every filter except ``folders`` on the floor, so
        # a keyword search with a date or sender filter returned unfiltered
        # results. All four filters now flow through, matching hybrid_search.
        oversample = (
            _FILTERED_OVERSAMPLE
            if self._has_post_fusion_filter(folders, from_addr, date_from, date_to, has_attachments)
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
        filtered = self._apply_filters(
            results, folders, from_addr, date_from, date_to, has_attachments
        )
        return filtered[:limit]

    def semantic_search(
        self,
        query_embedding: list[float],
        folders: list[str] | None = None,
        from_addr: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
        limit: int = 10,
    ) -> list[ThreadResult]:
        """Vector retrieval over both thread- and chunk-level lanes.

        Fuses ``threads_vec`` (mean-pooled thread vector) with
        ``message_chunks_vec`` (per-message precision vectors) via RRF,
        so a long thread with one strong matching chunk can outrank a
        thread whose coarse mean only weakly aligns with the query.
        Mirrors the dense half of ``hybrid_search`` — without the
        chunk lane the mode silently returned the worse retrieval
        whenever a caller chose ``mode="semantic"``.
        """
        oversample = (
            _FILTERED_OVERSAMPLE
            if self._has_post_fusion_filter(folders, from_addr, date_from, date_to, has_attachments)
            else _UNFILTERED_OVERSAMPLE
        )
        fetch_limit = limit * oversample
        vec_results = self._vector_search(query_embedding, fetch_limit)
        # Same chunk-lane oversample reasoning as ``hybrid_search``:
        # without enough chunks, a long thread monopolises the top-K
        # and other threads never enter the lane.
        chunk_hits = self._chunk_vector_search(
            query_embedding, fetch_limit * _CHUNK_LANE_OVERSAMPLE
        )
        fused = self._reciprocal_rank_fusion(bm25=[], vec=vec_results, chunks=chunk_hits)
        filtered = self._apply_filters(
            fused, folders, from_addr, date_from, date_to, has_attachments
        )
        return filtered[:limit]

    @staticmethod
    def _has_post_fusion_filter(
        folders: list[str] | None = None,
        from_addr: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
    ) -> bool:
        return bool(folders or from_addr or date_from or date_to or has_attachments is not None)

    def _keyword_search(
        self,
        query: str,
        limit: int,
        folders: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        has_attachments: bool | None = None,
    ) -> list[ThreadResult]:
        thread_hits = self._thread_keyword_search(
            query,
            limit,
            folders=folders,
            date_from=date_from,
            date_to=date_to,
            has_attachments=has_attachments,
        )
        chunk_hits = self._chunk_keyword_search(
            query,
            limit,
            folders=folders,
            date_from=date_from,
            date_to=date_to,
            has_attachments=has_attachments,
        )
        attachment_hits = self._attachment_keyword_search(
            query,
            limit,
            folders=folders,
            date_from=date_from,
            date_to=date_to,
            has_attachments=has_attachments,
        )
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
            placeholders = ",".join(["?"] * len(folders))
            where_clauses.append(f"t.folder IN ({placeholders})")
            params.extend(folders)
        # Normalize before SQL pushdown. Stored dates are full ISO timestamps
        # (``"2024-12-31T10:00:00+00:00"``); a bare user filter ``"2024-12-31"``
        # would lexicographically sort *below* any same-day stored timestamp
        # and exclude the final day entirely. ``_parse_filter_date`` promotes
        # date-only values to start/end of day in UTC so the comparison is
        # correct. date_from also benefits from explicit UTC normalization
        # for inputs that arrive with ``Z`` or offset suffixes.
        date_from_iso = _normalize_date_bound(date_from, end_of_day=False, field_name="date_from")
        date_to_iso = _normalize_date_bound(date_to, end_of_day=True, field_name="date_to")
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
            log.warning(f"FTS keyword search error, falling back to LIKE: {e}")
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
            log.warning("Chunk keyword search unavailable: %s", e)
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
            log.warning("Attachment keyword search unavailable: %s", e)
            return []
        results = [self._row_to_result(r) for r in rows]
        return self._best_per_thread(results)[:limit]

    def _attachment_won_thread_ids(
        self,
        query: str,
        thread_ids: list[str],
        folders: list[str] | None,
        date_from: str | None,
        date_to: str | None,
        has_attachments: bool | None,
    ) -> set[str]:
        """Return the subset of ``thread_ids`` that match the attachment-FTS lane.

        Used by ``hybrid_search(with_evidence=True)`` to mark threads that
        the attachment filename / MIME index lifted into the result set
        — so per-thread evidence ranking can privilege attachment chunks
        for them. Cheap one-shot FTS5 query; falls back to an empty set
        on any error so the caller's main path is never blocked.
        """
        if not thread_ids:
            return set()
        try:
            attachment_hits = self._attachment_keyword_search(
                query,
                limit=len(thread_ids) * _CHUNK_LANE_OVERSAMPLE,
                folders=folders,
                date_from=date_from,
                date_to=date_to,
                has_attachments=has_attachments,
            )
        except sqlite3.Error as e:
            log.warning("Attachment-won lookup failed; skipping bias: %s", e)
            return set()
        candidate_set = set(thread_ids)
        return {r.thread_id for r in attachment_hits if r.thread_id in candidate_set}

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
            placeholders = ",".join(["?"] * len(folders))
            where_clauses.append(f"t.folder IN ({placeholders})")
            params.extend(folders)
        date_from_iso = _normalize_date_bound(date_from, end_of_day=False, field_name="date_from")
        date_to_iso = _normalize_date_bound(date_to, end_of_day=True, field_name="date_to")
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
    def _dedupe_ranked_results(results: list[ThreadResult]) -> list[ThreadResult]:
        seen: set[str] = set()
        deduped: list[ThreadResult] = []
        for result in results:
            if result.thread_id in seen:
                continue
            seen.add(result.thread_id)
            deduped.append(result)
        return deduped

    @staticmethod
    def _best_per_thread(results: list[ThreadResult]) -> list[ThreadResult]:
        """Collapse to one row per ``thread_id``, keeping the best score.

        The chunk and attachment FTS lanes can return many rows for the
        same thread (one row per matching chunk). Take the row with the
        lowest BM25 score (lower = better in FTS5) per thread, then
        return the surviving rows in the original BM25 order so the
        caller's ``LIMIT`` slice picks the strongest threads. Compared
        to ``_dedupe_ranked_results`` (first-seen-wins), this guarantees
        the kept row carries the thread's best chunk score.
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
        pattern = f"%{query}%"
        where_clauses = ["(subject LIKE ? OR body_text LIKE ? OR participants LIKE ?)"]
        params: list = [pattern, pattern, pattern]
        if folders:
            placeholders = ",".join(["?"] * len(folders))
            where_clauses.append(f"folder IN ({placeholders})")
            params.extend(folders)
        # See ``_keyword_search`` for why date bounds are normalized before
        # being pushed into SQL.
        date_from_iso = _normalize_date_bound(date_from, end_of_day=False, field_name="date_from")
        date_to_iso = _normalize_date_bound(date_to, end_of_day=True, field_name="date_to")
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
            log.warning(f"LIKE fallback search error: {e}")
            return []

    def _chunk_vector_search(self, embedding: list[float], limit: int) -> list[ChunkResult]:
        """Return per-message chunks whose vectors are closest to ``embedding``.

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
            # row per ``(attachment_id, message_id)`` pair (the one
            # with the lowest ``attachment_occurrence_id``). The
            # indexer permits the same content hash to be attached
            # under multiple filenames in one message (a user attaching
            # the same PDF twice with different display names); each
            # occurrence is its own row in ``attachments`` but only
            # ONE chunk set is stored per content hash, so a naive
            # ``ON (attachment_id, message_id)`` JOIN multiplies the
            # chunk by the occurrence count and emits non-deterministic
            # filename attribution. Picking the lowest occurrence id
            # gives a stable, deterministic choice and eliminates the
            # multiplication.
            rows = self._fetchall(
                """
                SELECT
                    c.chunk_id, c.message_id, c.thread_id, c.chunk_index,
                    c.text, c.char_start, c.char_end, c.attachment_id,
                    a.filename AS attachment_filename,
                    a.content_type AS attachment_mime,
                    v.distance AS score
                FROM message_chunks_vec v
                JOIN message_chunks c ON c.chunk_id = v.chunk_id
                LEFT JOIN attachments a
                    ON a.attachment_occurrence_id = (
                        SELECT MIN(a2.attachment_occurrence_id)
                        FROM attachments a2
                        WHERE a2.attachment_id = c.attachment_id
                          AND a2.message_id = c.message_id
                    )
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY v.distance
                """,
                (serialized, limit),
            )
            return [_row_to_chunk_result(r) for r in rows]
        except (sqlite3.Error, ValueError) as e:
            # ``sqlite3.Error`` covers OperationalError (missing vec
            # table) and DatabaseError (corruption); ``ValueError`` is
            # raised by sqlite-vec on malformed embedding payloads. Any
            # other exception type is unexpected and should propagate.
            log.warning(f"Chunk vector search error: {e}")
            return []

    def get_evidence_chunks_for_threads(
        self,
        thread_ids: list[str],
        embedding: list[float],
        per_thread_limit: int = 3,
        attachment_won_thread_ids: set[str] | None = None,
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
        didn't make the global pool. ``ask_mailbox``'s docstring
        promises that "this is the ONLY mailbox tool that reads
        attachment content"; the pool-reuse shape silently broke that
        promise for any non-chunk-vec retrieval lane.

        Implementation reads only chunks belonging to the surfaced
        ``thread_ids`` and computes ``vec_distance_l2`` against each.
        For typical surfaced sets (5-50 threads × 1-100 chunks each)
        this is a small sequential scan and beats N per-thread KNN
        queries on round-trip overhead.

        ``attachment_won_thread_ids``: threads that ``hybrid_search``
        surfaced via attachment-filename FTS. For those threads,
        attachment chunks are floated to the front of the per-thread
        slice so the LLM sees attachment text (the source the user
        meant) before body text — even when a body chunk has higher
        dense similarity. Closes the "filename match → wrong evidence"
        gap in ``ask_mailbox``'s "reads attachment content" promise.
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
            # occurrence row per ``(attachment_id, message_id)`` (the
            # one with the lowest ``attachment_occurrence_id``). The
            # indexer allows the same content hash to be attached
            # under multiple display filenames in one message but
            # stores ONLY ONE chunk set per content hash, so a naive
            # ``ON (attachment_id, message_id)`` JOIN multiplies the
            # chunk row by the occurrence count. See
            # ``_chunk_vector_search`` for the full rationale; both
            # call sites apply the same fix. Body chunks have
            # ``c.attachment_id IS NULL`` so the subquery returns
            # NULL and the LEFT JOIN yields NULL filename/MIME.
            sql = (
                "SELECT c.chunk_id, c.message_id, c.thread_id, c.chunk_index, "
                "c.text, c.char_start, c.char_end, c.attachment_id, "
                "a.filename AS attachment_filename, "
                "a.content_type AS attachment_mime, "
                "vec_distance_l2(v.embedding, ?) AS score "
                "FROM message_chunks c "
                "JOIN message_chunks_vec v ON c.chunk_id = v.chunk_id "
                "LEFT JOIN attachments a "
                "  ON a.attachment_occurrence_id = ( "
                "       SELECT MIN(a2.attachment_occurrence_id) "
                "       FROM attachments a2 "
                "       WHERE a2.attachment_id = c.attachment_id "
                "         AND a2.message_id = c.message_id "
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
            log.warning("Per-thread evidence chunk fetch failed: %s", e)
            return {tid: [] for tid in thread_ids}

        # First pass: gather ALL chunks per thread (still ordered by
        # vec_distance ASC within each thread) so the attachment-first
        # reorder below has the full set to work with. The cap to
        # ``per_thread_limit`` happens after the reorder.
        all_chunks: dict[str, list[ChunkResult]] = {tid: [] for tid in thread_ids}
        for r in rows:
            all_chunks[r["thread_id"]].append(_row_to_chunk_result(r))

        attachment_won = attachment_won_thread_ids or set()
        grouped: dict[str, list[ChunkResult]] = {}
        for tid, chunks in all_chunks.items():
            if tid in attachment_won:
                # Float attachment chunks to the front, preserving
                # within-group order (already vec_distance ASC). The
                # query was won by attachment-filename FTS, so the
                # user's intent is attachment content; surface that
                # first even when a body chunk dense-scored higher.
                attachment_chunks = [c for c in chunks if c.attachment_id is not None]
                body_chunks = [c for c in chunks if c.attachment_id is None]
                ordered = attachment_chunks + body_chunks
            else:
                ordered = chunks
            grouped[tid] = ordered[:per_thread_limit]
        return grouped

    def get_recent_chunks_for_thread(
        self,
        thread_id: str,
        limit: int = 6,
    ) -> list[ChunkResult]:
        """Return the most-recently-indexed BODY chunks for ``thread_id``.

        Used by ``summarize_thread`` / timeline-style intelligence tools
        that need "what does the thread say lately" — NOT "what matches
        a query." The stored ``body_text`` is front-preserving and
        token-capped, so a long thread that crosses ``THREAD_BODY_TEXT_MAX_TOKENS``
        silently drops its newest replies. The chunk store carries every
        message in full, so reading the tail of ``chunked_at`` recovers
        the missing context.

        Attachment chunks (rows with a non-NULL ``attachment_id``) are
        deliberately excluded via ``c.attachment_id IS NULL``. The tool
        contract reserves attachment-text retrieval to ``ask_mailbox``
        alone; ``summarize_thread`` is a body summary, so surfacing
        attachment extracts here would silently broaden which indexed
        content can leave the host for a remote inference endpoint.

        Returned chunks are in chronological (oldest-first within the
        selected tail) order so the LLM prompt reads naturally as a
        timeline. Caller can render them via ``_thread_context``.

        Ordering: ``COALESCE(c.message_date, c.chunked_at) DESC,
        c.chunk_index DESC``. ``message_date`` is the message's
        ``Date:`` header captured at chunk-write (schema v18+); it is
        the authoritative "when did this message arrive" signal and
        sorts correctly across reindex, reap-rebuild, dead-letter
        retry, and recovery-sweep paths. ``chunked_at`` (the chunker's
        wall-clock at insert) is the fallback for legacy v17- chunk
        rows that pre-date the column — those rows have
        ``message_date IS NULL`` and degrade to the prior heuristic
        until they are re-indexed. ``chunk_index DESC`` tiebreaks
        when a thread is freshly indexed in one batch (all chunks
        share the same ``message_date`` / ``chunked_at``) so the
        last chunk emitted by the chunker comes first in selection.
        Selection picks the latest ``limit`` chunks, then the result
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
                SELECT c.chunk_id, c.message_id, c.thread_id, c.chunk_index,
                       c.text, c.char_start, c.char_end, c.attachment_id,
                       NULL AS attachment_filename,
                       NULL AS attachment_mime,
                       0.0 AS score
                FROM message_chunks c
                WHERE c.thread_id = ?
                  AND c.attachment_id IS NULL
                ORDER BY COALESCE(c.message_date, c.chunked_at) DESC,
                         c.chunk_index DESC
                LIMIT ?
                """,
                (thread_id, limit),
            )
        except sqlite3.Error as e:
            log.warning("Recent-chunks lookup failed for %s: %s", thread_id, e)
            return []
        chunks = [_row_to_chunk_result(r) for r in rows]
        # Reverse for chronological display: SELECT picked the newest
        # ``limit`` chunks; we want them oldest-first in the prompt so
        # the timeline reads naturally.
        chunks.reverse()
        return chunks

    def _vector_search(self, embedding: list[float], limit: int) -> list[ThreadResult]:
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
                (serialized, limit),
            )
            return [self._row_to_result(r) for r in rows]
        except (sqlite3.Error, ValueError) as e:
            # Same catch surface as ``_chunk_vector_search`` —
            # ``sqlite3.Error`` for table/connection issues, ``ValueError``
            # for malformed serialised vectors. Other exception types
            # should propagate so corrupt-state bugs aren't masked.
            log.warning(f"Vector search error: {e}")
            return []

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

        for rank, result in enumerate(bm25):
            scores[result.thread_id] = scores.get(result.thread_id, 0) + 1.0 / (k + rank + 1)
            index[result.thread_id] = result

        for rank, result in enumerate(vec):
            scores[result.thread_id] = scores.get(result.thread_id, 0) + 1.0 / (k + rank + 1)
            index[result.thread_id] = result

        if chunks:
            seen_threads: set[str] = set()
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
                if tid not in index:
                    # Materialize chunk-only threads via a thread fetch.
                    # Skip silently if the thread row is missing (shouldn't
                    # happen in steady state — chunk rows live and die
                    # with their thread — but defensive against stale
                    # state mid-reap).
                    fetched = self.get_thread(tid)
                    if fetched is not None:
                        index[tid] = fetched

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        results = []
        for thread_id, score in ranked:
            if thread_id not in index:
                continue
            r = index[thread_id]
            r.score = score
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
        """
        scores: dict[str, float] = {}
        index: dict[str, ThreadResult] = {}
        for lane in lanes:
            for rank, result in enumerate(lane):
                scores[result.thread_id] = scores.get(result.thread_id, 0.0) + 1.0 / (k + rank + 1)
                index.setdefault(result.thread_id, result)

        results: list[ThreadResult] = []
        for thread_id, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            result = index[thread_id]
            result.score = score
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
    ) -> list[ThreadResult]:
        date_from_dt = (
            _parse_filter_date(date_from, end_of_day=False, _field_name="date_from")
            if date_from
            else None
        )
        date_to_dt = (
            _parse_filter_date(date_to, end_of_day=True, _field_name="date_to") if date_to else None
        )

        filtered = results
        if folders:
            filtered = [r for r in filtered if r.folder in folders]
        if from_addr:
            fa = from_addr.lower()
            # Filter by sender (the From-only subset, not all participants).
            filtered = [r for r in filtered if _matches_sender(r, fa)]
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
        return filtered

    # -------------------------------------------------------------------------
    # Direct lookups
    # -------------------------------------------------------------------------

    def get_thread(self, thread_id: str) -> ThreadResult | None:
        row = self._fetchone("SELECT * FROM threads WHERE thread_id = ?", (thread_id,))
        return self._row_to_result(row) if row else None

    def get_thread_message_ids(self, thread_id: str) -> list[str]:
        row = self._fetchone("SELECT message_ids FROM threads WHERE thread_id = ?", (thread_id,))
        return json.loads(row["message_ids"]) if row else []

    def find_thread_by_message_id(self, message_id: str) -> str | None:
        row = self._fetchone(
            "SELECT thread_id FROM message_thread_map WHERE message_id = ?",
            (message_id,),
        )
        return row["thread_id"] if row else None

    def list_threads(
        self,
        folder: str = "INBOX",
        filter_type: str = "all",
        limit: int = 20,
        offset: int = 0,
    ) -> list[ThreadResult]:
        if filter_type != "all":
            raise ValueError("filter_type must be 'all'; unread/flagged state is not indexed")
        rows = self._fetchall(
            """
            SELECT * FROM threads
            WHERE folder = ?
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

    def get_stats(self) -> dict:
        stats = {}
        with closing(self._connect()) as conn:
            stats["total_threads"] = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            stats["total_messages"] = conn.execute(
                "SELECT COUNT(*) FROM message_thread_map"
            ).fetchone()[0]
            row = conn.execute("SELECT MIN(date_first), MAX(date_last) FROM threads").fetchone()
        stats["oldest_message"] = row[0]
        stats["newest_message"] = row[1]
        return stats

    def list_folders(self) -> list[dict]:
        rows = self._fetchall("""
            SELECT folder, COUNT(*) as thread_count
            FROM threads
            GROUP BY folder
            ORDER BY thread_count DESC
        """)
        return [{"name": r["folder"], "thread_count": r["thread_count"]} for r in rows]

    def find_contact(
        self, query: str, limit: int = 10, *, senders_only: bool = False
    ) -> list[dict]:
        """Resolve a name / address / domain fragment to indexed contacts.

        Iterates ``threads.participants`` (or ``threads.senders`` when
        ``senders_only=True``), parses each entry with ``parseaddr``,
        and matches the lowercased query against either the display
        name or the email address. Aggregates by canonical email so
        the same contact across many threads collapses to one row,
        with ``thread_count`` reflecting how many threads they
        appeared on. Same-thread duplicates do not double-count.

        ``senders_only`` narrows the aggregation to the From-line
        addresses recorded on each thread. Use this when the caller's
        intent is "filter to messages this person SENT" rather than
        "find this person's address anywhere in the index": the
        broader participants ranking can promote a frequent
        recipient/CC-only contact over the actual sender, which then
        misses real results when the resolved address is plugged into
        ``search_emails(from_addr=...)``. The default is
        ``senders_only=False`` because the standalone find_contact
        tool is also used for general "find this person's email"
        lookups where recipient-only matches are still useful.

        Exists so callers (the LLM via the MCP tool) can map a
        display-name fragment (``"Jane Smith"``) to a canonical
        address (``"jsmith@example.com"``) before invoking
        ``search_emails(from_addr=...)``. Without this step a
        borderline model often abdicates when given a role label or
        partial name.
        """
        if not query or not query.strip():
            return []
        needle = query.strip().lower()

        # ``senders`` is a JSON array of From-only addresses recorded
        # on each thread; ``participants`` is the broader From + To +
        # Cc + ... set. Both are stored on the same row so we can
        # pick at query time without a separate index. Use two
        # explicit SQL strings rather than f-string interpolation so
        # there is no path for column to come from caller input —
        # the choice is bounded to the senders_only flag here.
        if senders_only:
            rows = self._fetchall("SELECT senders AS entries FROM threads")
        else:
            rows = self._fetchall("SELECT participants AS entries FROM threads")

        # canonical email -> {"names": set[str], "thread_count": int}
        by_email: dict[str, dict] = {}
        for row in rows:
            try:
                entries = json.loads(row["entries"])
            except json.JSONDecodeError, TypeError:
                continue
            seen_in_thread: set[str] = set()
            for entry in entries:
                if not isinstance(entry, str):
                    continue
                name, addr = parseaddr(entry)
                addr = addr.strip().lower()
                if "@" not in addr:
                    continue
                # Match against either the display name or the address so
                # "smith", "Jane", and "@example.com" all surface the
                # same contact.
                haystack = f"{name} {addr}".lower()
                if needle not in haystack:
                    continue
                if addr in seen_in_thread:
                    continue
                seen_in_thread.add(addr)
                bucket = by_email.setdefault(addr, {"names": set(), "thread_count": 0})
                stripped_name = name.strip()
                if stripped_name:
                    bucket["names"].add(stripped_name)
                bucket["thread_count"] += 1

        results = [
            {
                "email": addr,
                "names": sorted(bucket["names"]),
                "thread_count": bucket["thread_count"],
            }
            for addr, bucket in by_email.items()
        ]
        # Most-active contact first; tiebreak on email so the order is
        # stable across runs (important for both eval reproducibility
        # and the unit tests below).
        results.sort(key=lambda x: (-x["thread_count"], x["email"]))
        return results[:limit]

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _row_to_result(self, row) -> ThreadResult:
        # Prefer the original-cased ``display_subject`` (added in
        # SCHEMA_VERSION v13). Legacy rows written before v13 have NULL
        # ``display_subject``; fall back to the normalized ``subject``
        # so existing threads keep rendering instead of going blank.
        # The fallback also covers the read-only-DB-pre-v13 case where
        # the column itself is missing.
        try:
            display = row["display_subject"]
        except KeyError, IndexError:
            display = None
        return ThreadResult(
            thread_id=row["thread_id"],
            subject=display or row["subject"],
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

    @staticmethod
    def _validate_iso8601(field_name: str, value: str) -> None:
        try:
            _parse_filter_date(value, end_of_day=False, _field_name=field_name)
        except ValueError as exc:
            raise ValueError(
                f"{field_name} must be a valid ISO 8601 date or date/time string"
            ) from exc


def _normalize_date_bound(value: str | None, *, end_of_day: bool, field_name: str) -> str | None:
    """Return an ISO 8601 string suitable for lexicographic comparison against
    stored ``date_first`` / ``date_last`` values, or ``None`` if no filter was
    supplied. Raises ``ValueError`` on invalid input (same policy as
    ``_apply_filters``) so bad filters fail loudly instead of silently
    returning the wrong rows.
    """
    if not value:
        return None
    return _parse_filter_date(value, end_of_day=end_of_day, _field_name=field_name).isoformat()


def _parse_filter_date(
    value: str, *, end_of_day: bool, _field_name: str = "date filter"
) -> datetime:
    """Parse a user-supplied date filter into a tz-aware UTC ``datetime``.

    Accepts:
    - date-only values (``"2024-12-31"``): promoted to ``00:00:00`` when
      used as a lower bound, ``23:59:59.999999`` when used as an upper
      bound, both in UTC — so the filter includes the full day the user
      named.
    - trailing ``Z`` (``"2024-12-31T00:00:00Z"``): normalized to the
      ``+00:00`` offset form that ``datetime.fromisoformat`` accepts.
    - any other ISO 8601 datetime string: passed through.

    Naive datetimes are assumed to be UTC. Offset-aware values are
    converted to UTC before being returned, so callers that feed the
    result's ``isoformat()`` into SQL string comparisons against stored
    UTC timestamps compare the same instant rather than two offset-shifted
    strings that happen to sort differently.
    """
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value

    # Date-only: ``"YYYY-MM-DD"`` is exactly 10 chars of [digits/hyphens].
    if len(normalized) == 10 and normalized[4] == "-" and normalized[7] == "-":
        try:
            base = datetime.fromisoformat(normalized + "T00:00:00+00:00")
        except ValueError as exc:
            raise ValueError(f"{_field_name}: invalid date {value!r}") from exc
        if end_of_day:
            return base.replace(hour=23, minute=59, second=59, microsecond=999999)
        return base

    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{_field_name}: invalid datetime {value!r}") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
