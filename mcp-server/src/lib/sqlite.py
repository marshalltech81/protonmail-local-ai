"""
SQLite query layer for the MCP server.
Read-only access to the index built by the indexer service.
Supports BM25 keyword search, vector similarity search, and hybrid fusion.
"""

import json
import logging
import re
import sqlite3
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parseaddr
from pathlib import Path

import sqlite_vec

log = logging.getLogger("mcp.sqlite")


def _close_connection(conn: sqlite3.Connection) -> None:
    conn.close()


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
    """

    chunk_id: str
    message_id: str
    thread_id: str
    chunk_index: int
    text: str
    char_start: int
    char_end: int
    score: float = 0.0


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
    def __init__(self, path: str):
        self.path = path
        self._conn = self._connect()
        self._closed = False
        self._finalizer = weakref.finalize(self, _close_connection, self._conn)

    def close(self) -> None:
        if self._closed:
            return
        self._finalizer.detach()
        self._conn.close()
        self._closed = True

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _connect(self) -> sqlite3.Connection:
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
    ) -> list[ThreadResult]:
        oversample = (
            _FILTERED_OVERSAMPLE
            if self._has_post_fusion_filter(folders, from_addr, date_from, date_to, has_attachments)
            else _UNFILTERED_OVERSAMPLE
        )
        fetch_limit = limit * oversample
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
        chunk_hits = self._chunk_vector_search(query_embedding, fetch_limit * 3)
        fused = self._reciprocal_rank_fusion(bm25_results, vec_results, chunk_hits)
        filtered = self._apply_filters(
            fused, folders, from_addr, date_from, date_to, has_attachments
        )
        top = filtered[:limit]

        if with_evidence and top:
            # Reuse the chunk lane already fetched: group its hits by
            # parent thread, take the best per thread for the surfaced
            # results. Avoids a second sqlite-vec round-trip when
            # ``chunk_hits`` already covers the surfaced threads.
            wanted = {r.thread_id for r in top}
            grouped: dict[str, list[ChunkResult]] = {tid: [] for tid in wanted}
            for chunk in chunk_hits:
                if chunk.thread_id in wanted and len(grouped[chunk.thread_id]) < 3:
                    grouped[chunk.thread_id].append(chunk)
            for result in top:
                result.evidence_chunks = grouped.get(result.thread_id, [])

        return top

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
        oversample = (
            _FILTERED_OVERSAMPLE
            if self._has_post_fusion_filter(folders, from_addr, date_from, date_to, has_attachments)
            else _UNFILTERED_OVERSAMPLE
        )
        results = self._vector_search(query_embedding, limit * oversample)
        filtered = self._apply_filters(
            results, folders, from_addr, date_from, date_to, has_attachments
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
            "t.snippet, t.has_attachments, t.body_text, "
            "bm25(threads_fts) AS score "
            "FROM threads_fts "
            "JOIN threads t ON threads_fts.rowid = t.fts_rowid "
            "WHERE " + " AND ".join(where_clauses) + " "  # nosec B608
            "ORDER BY score LIMIT ?"
        )
        params.append(limit)

        try:
            rows = self._conn.execute(sql, params).fetchall()
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
        sql = (
            "SELECT "
            "t.thread_id, t.subject, t.participants, t.senders, t.folder, "
            "t.date_first, t.date_last, t.message_ids, "
            "t.snippet, t.has_attachments, t.body_text, "
            "bm25(message_chunks_fts) AS score "
            "FROM message_chunks_fts "
            "JOIN message_chunks c ON message_chunks_fts.rowid = c.fts_rowid "
            "JOIN threads t ON c.thread_id = t.thread_id "
            "WHERE " + " AND ".join(where_clauses) + " "  # nosec B608
            "ORDER BY score LIMIT ?"
        )
        params.append(limit * 3)
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            # Older databases may not have the v9 chunk FTS tables yet.
            log.debug("Chunk keyword search unavailable: %s", e)
            return []
        return self._dedupe_ranked_results([self._row_to_result(r) for r in rows])[:limit]

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
        sql = (
            "SELECT "
            "t.thread_id, t.subject, t.participants, t.senders, t.folder, "
            "t.date_first, t.date_last, t.message_ids, "
            "t.snippet, t.has_attachments, t.body_text, "
            "bm25(attachments_fts) AS score "
            "FROM attachments_fts "
            "JOIN attachments a ON attachments_fts.rowid = a.fts_rowid "
            "JOIN threads t ON a.thread_id = t.thread_id "
            "WHERE " + " AND ".join(where_clauses) + " "  # nosec B608
            "ORDER BY score LIMIT ?"
        )
        params.append(limit * 3)
        try:
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:
            # Older databases may not have the v10 attachment FTS tables yet.
            log.debug("Attachment keyword search unavailable: %s", e)
            return []
        return self._dedupe_ranked_results([self._row_to_result(r) for r in rows])[:limit]

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
            "snippet, has_attachments, body_text, 0.0 AS score "
            "FROM threads "
            "WHERE " + " AND ".join(where_clauses) + " "  # nosec B608
            "ORDER BY date_last DESC LIMIT ?"
        )
        params.append(limit)

        try:
            rows = self._conn.execute(sql, params).fetchall()
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
            rows = self._conn.execute(
                """
                SELECT
                    c.chunk_id, c.message_id, c.thread_id, c.chunk_index,
                    c.text, c.char_start, c.char_end,
                    v.distance AS score
                FROM message_chunks_vec v
                JOIN message_chunks c ON c.chunk_id = v.chunk_id
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY v.distance
                """,
                (serialized, limit),
            ).fetchall()
            return [
                ChunkResult(
                    chunk_id=r["chunk_id"],
                    message_id=r["message_id"],
                    thread_id=r["thread_id"],
                    chunk_index=int(r["chunk_index"]),
                    text=r["text"],
                    char_start=int(r["char_start"]),
                    char_end=int(r["char_end"]),
                    score=float(r["score"]),
                )
                for r in rows
            ]
        except Exception as e:
            log.warning(f"Chunk vector search error: {e}")
            return []

    def get_evidence_chunks_for_threads(
        self,
        thread_ids: list[str],
        embedding: list[float],
        per_thread_limit: int = 3,
        candidate_pool: int = 200,
    ) -> dict[str, list[ChunkResult]]:
        """Return up to ``per_thread_limit`` best-matching chunks per thread.

        Pulls a single vector-search candidate pool of size
        ``candidate_pool`` and groups the results by thread, keeping
        only the top chunks for the requested ``thread_ids``. One sqlite-
        vec round-trip serves N threads at once; per-thread queries
        would scale linearly.

        Used by intelligence tools after ``hybrid_search`` to attach
        precise passage citations to the threads it returned.
        """
        if not thread_ids:
            return {}
        chunks = self._chunk_vector_search(embedding, candidate_pool)
        wanted = set(thread_ids)
        grouped: dict[str, list[ChunkResult]] = {tid: [] for tid in thread_ids}
        for chunk in chunks:
            if chunk.thread_id not in wanted:
                continue
            bucket = grouped[chunk.thread_id]
            if len(bucket) < per_thread_limit:
                bucket.append(chunk)
        return grouped

    def _vector_search(self, embedding: list[float], limit: int) -> list[ThreadResult]:
        try:
            serialized = sqlite_vec.serialize_float32(embedding)
            rows = self._conn.execute(
                """
                SELECT
                    t.thread_id, t.subject, t.participants, t.senders, t.folder,
                    t.date_first, t.date_last, t.message_ids,
                    t.snippet, t.has_attachments, t.body_text,
                    v.distance AS score
                FROM threads_vec v
                JOIN threads t ON v.thread_id = t.thread_id
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY v.distance
            """,
                (serialized, limit),
            ).fetchall()
            return [self._row_to_result(r) for r in rows]
        except Exception as e:
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
        row = self._conn.execute(
            "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return self._row_to_result(row) if row else None

    def get_thread_message_ids(self, thread_id: str) -> list[str]:
        row = self._conn.execute(
            "SELECT message_ids FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return json.loads(row["message_ids"]) if row else []

    def find_thread_by_message_id(self, message_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT thread_id FROM message_thread_map WHERE message_id = ?",
            (message_id,),
        ).fetchone()
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
        rows = self._conn.execute(
            """
            SELECT * FROM threads
            WHERE folder = ?
            ORDER BY date_last DESC
            LIMIT ? OFFSET ?
        """,
            (folder, limit, offset),
        ).fetchall()
        return [self._row_to_result(r) for r in rows]

    def ping(self) -> None:
        """Trivial query used as a liveness probe by the HTTP /health route.

        Exercises the read-only SQLite connection without touching any
        application table so it stays cheap under a per-30s healthcheck
        interval and surfaces a missing/unreadable DB as an exception.
        """
        self._conn.execute("SELECT 1").fetchone()

    def get_stats(self) -> dict:
        stats = {}
        stats["total_threads"] = self._conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        stats["total_messages"] = self._conn.execute(
            "SELECT COUNT(*) FROM message_thread_map"
        ).fetchone()[0]
        row = self._conn.execute("SELECT MIN(date_first), MAX(date_last) FROM threads").fetchone()
        stats["oldest_message"] = row[0]
        stats["newest_message"] = row[1]
        return stats

    def list_folders(self) -> list[dict]:
        rows = self._conn.execute("""
            SELECT folder, COUNT(*) as thread_count
            FROM threads
            GROUP BY folder
            ORDER BY thread_count DESC
        """).fetchall()
        return [{"name": r["folder"], "thread_count": r["thread_count"]} for r in rows]

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _row_to_result(self, row) -> ThreadResult:
        return ThreadResult(
            thread_id=row["thread_id"],
            subject=row["subject"],
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
