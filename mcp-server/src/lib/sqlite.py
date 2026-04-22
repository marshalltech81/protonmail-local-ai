"""
SQLite query layer for the MCP server.
Read-only access to the index built by the indexer service.
Supports BM25 keyword search, vector similarity search, and hybrid fusion.
"""

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import sqlite_vec

log = logging.getLogger("mcp.sqlite")

# When any filter (folder, sender, date range, attachment flag) is active,
# the filtered result set is a subset of the raw ranked candidates. Pulling
# only ``limit * 2`` raw candidates means a filter can wipe out the page —
# valid matches ranked deeper are never considered. Oversample when filters
# are present to preserve recall.
_UNFILTERED_OVERSAMPLE = 2
_FILTERED_OVERSAMPLE = 4


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
    score: float = 0.0


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn = self._connect()

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
    ) -> list[ThreadResult]:
        oversample = (
            _FILTERED_OVERSAMPLE
            if self._has_post_fusion_filter(folders, from_addr, date_from, date_to, has_attachments)
            else _UNFILTERED_OVERSAMPLE
        )
        fetch_limit = limit * oversample
        bm25_results = self._keyword_search(query_text, fetch_limit)
        vec_results = self._vector_search(query_embedding, fetch_limit)
        fused = self._reciprocal_rank_fusion(bm25_results, vec_results)
        filtered = self._apply_filters(
            fused, folders, from_addr, date_from, date_to, has_attachments
        )
        return filtered[:limit]

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
        results = self._keyword_search(query_text, limit * oversample)
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

    def _keyword_search(self, query: str, limit: int) -> list[ThreadResult]:
        # threads_fts is a contentless FTS5 table, so its columns (including
        # any UNINDEXED ones) always read back as NULL. The reliable way to
        # link an FTS row back to its thread is the rowid, which the indexer
        # captures on write into threads.fts_rowid.
        fts_query = _sanitize_fts_query(query)
        if not fts_query:
            return []
        try:
            rows = self._conn.execute(
                """
                SELECT
                    t.thread_id, t.subject, t.participants, t.folder,
                    t.date_first, t.date_last, t.message_ids,
                    t.snippet, t.has_attachments, t.body_text,
                    bm25(threads_fts) AS score
                FROM threads_fts
                JOIN threads t ON threads_fts.rowid = t.fts_rowid
                WHERE threads_fts MATCH ?
                ORDER BY score
                LIMIT ?
            """,
                (fts_query, limit),
            ).fetchall()
            return [self._row_to_result(r) for r in rows]
        except sqlite3.OperationalError as e:
            # Defense-in-depth: if the sanitized query still trips FTS5, fall
            # back to a LIKE scan against subject/body/participants so valid
            # searches still return recall rather than empty.
            log.warning(f"FTS keyword search error, falling back to LIKE: {e}")
            return self._like_fallback(query, limit)

    def _like_fallback(self, query: str, limit: int) -> list[ThreadResult]:
        pattern = f"%{query}%"
        try:
            rows = self._conn.execute(
                """
                SELECT
                    thread_id, subject, participants, folder,
                    date_first, date_last, message_ids,
                    snippet, has_attachments, body_text,
                    0.0 AS score
                FROM threads
                WHERE subject LIKE ? OR body_text LIKE ? OR participants LIKE ?
                ORDER BY date_last DESC
                LIMIT ?
            """,
                (pattern, pattern, pattern, limit),
            ).fetchall()
            return [self._row_to_result(r) for r in rows]
        except sqlite3.OperationalError as e:
            log.warning(f"LIKE fallback search error: {e}")
            return []

    def _vector_search(self, embedding: list[float], limit: int) -> list[ThreadResult]:
        try:
            serialized = sqlite_vec.serialize_float32(embedding)
            rows = self._conn.execute(
                """
                SELECT
                    t.thread_id, t.subject, t.participants, t.folder,
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
        k: int = 60,
    ) -> list[ThreadResult]:
        """Merge two ranked lists via RRF. Higher score = better."""
        scores: dict[str, float] = {}
        index: dict[str, ThreadResult] = {}

        for rank, result in enumerate(bm25):
            scores[result.thread_id] = scores.get(result.thread_id, 0) + 1.0 / (k + rank + 1)
            index[result.thread_id] = result

        for rank, result in enumerate(vec):
            scores[result.thread_id] = scores.get(result.thread_id, 0) + 1.0 / (k + rank + 1)
            index[result.thread_id] = result

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        results = []
        for thread_id, score in ranked:
            r = index[thread_id]
            r.score = score
            results.append(r)
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
            filtered = [r for r in filtered if any(fa in p.lower() for p in r.participants)]
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
            folder=row["folder"],
            date_first=datetime.fromisoformat(row["date_first"]),
            date_last=datetime.fromisoformat(row["date_last"]),
            message_ids=json.loads(row["message_ids"]),
            snippet=row["snippet"] or "",
            has_attachments=bool(row["has_attachments"]),
            body_text=row["body_text"] if "body_text" in row.keys() and row["body_text"] else "",
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

    Naive datetimes are assumed to be UTC.
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
        from datetime import UTC

        dt = dt.replace(tzinfo=UTC)
    return dt
