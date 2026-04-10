"""
SQLite query layer for the MCP server.
Read-only access to the index built by the indexer service.
Supports BM25 keyword search, vector similarity search, and hybrid fusion.
"""
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import sqlite_vec

log = logging.getLogger("mcp.sqlite")


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
    score: float = 0.0


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn = self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
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
        folders: Optional[list[str]] = None,
        from_addr: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        has_attachments: Optional[bool] = None,
        limit: int = 10,
    ) -> list[ThreadResult]:
        bm25_results  = self._keyword_search(query_text, limit * 2)
        vec_results   = self._vector_search(query_embedding, limit * 2)
        fused         = self._reciprocal_rank_fusion(bm25_results, vec_results)
        filtered      = self._apply_filters(
            fused, folders, from_addr, date_from, date_to, has_attachments
        )
        return filtered[:limit]

    def keyword_search(
        self,
        query_text: str,
        folders: Optional[list[str]] = None,
        limit: int = 10,
    ) -> list[ThreadResult]:
        results = self._keyword_search(query_text, limit * 2)
        filtered = self._apply_filters(results, folders)
        return filtered[:limit]

    def semantic_search(
        self,
        query_embedding: list[float],
        folders: Optional[list[str]] = None,
        limit: int = 10,
    ) -> list[ThreadResult]:
        results = self._vector_search(query_embedding, limit * 2)
        filtered = self._apply_filters(results, folders)
        return filtered[:limit]

    def _keyword_search(self, query: str, limit: int) -> list[ThreadResult]:
        try:
            rows = self._conn.execute("""
                SELECT
                    t.thread_id, t.subject, t.participants, t.folder,
                    t.date_first, t.date_last, t.message_ids,
                    t.snippet, t.has_attachments,
                    bm25(threads_fts) AS score
                FROM threads_fts
                JOIN threads t ON threads_fts.thread_id = t.thread_id
                WHERE threads_fts MATCH ?
                ORDER BY score
                LIMIT ?
            """, (query, limit)).fetchall()
            return [self._row_to_result(r) for r in rows]
        except Exception as e:
            log.warning(f"Keyword search error: {e}")
            return []

    def _vector_search(
        self, embedding: list[float], limit: int
    ) -> list[ThreadResult]:
        try:
            serialized = sqlite_vec.serialize_float32(embedding)
            rows = self._conn.execute("""
                SELECT
                    t.thread_id, t.subject, t.participants, t.folder,
                    t.date_first, t.date_last, t.message_ids,
                    t.snippet, t.has_attachments,
                    v.distance AS score
                FROM threads_vec v
                JOIN threads t ON v.thread_id = t.thread_id
                WHERE v.embedding MATCH ?
                  AND k = ?
                ORDER BY v.distance
            """, (serialized, limit)).fetchall()
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
            scores[result.thread_id] = scores.get(result.thread_id, 0) \
                + 1.0 / (k + rank + 1)
            index[result.thread_id] = result

        for rank, result in enumerate(vec):
            scores[result.thread_id] = scores.get(result.thread_id, 0) \
                + 1.0 / (k + rank + 1)
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
        folders: Optional[list[str]] = None,
        from_addr: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        has_attachments: Optional[bool] = None,
    ) -> list[ThreadResult]:
        filtered = results
        if folders:
            filtered = [r for r in filtered if r.folder in folders]
        if from_addr:
            fa = from_addr.lower()
            filtered = [
                r for r in filtered
                if any(fa in p.lower() for p in r.participants)
            ]
        if date_from:
            filtered = [r for r in filtered if r.date_last.isoformat() >= date_from]
        if date_to:
            filtered = [r for r in filtered if r.date_first.isoformat() <= date_to]
        if has_attachments is not None:
            filtered = [r for r in filtered if r.has_attachments == has_attachments]
        return filtered

    # -------------------------------------------------------------------------
    # Direct lookups
    # -------------------------------------------------------------------------

    def get_thread(self, thread_id: str) -> Optional[ThreadResult]:
        row = self._conn.execute(
            "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return self._row_to_result(row) if row else None

    def get_thread_message_ids(self, thread_id: str) -> list[str]:
        row = self._conn.execute(
            "SELECT message_ids FROM threads WHERE thread_id = ?",
            (thread_id,)
        ).fetchone()
        return json.loads(row["message_ids"]) if row else []

    def list_threads(
        self,
        folder: str = "INBOX",
        filter_type: str = "all",
        limit: int = 20,
        offset: int = 0,
    ) -> list[ThreadResult]:
        rows = self._conn.execute("""
            SELECT * FROM threads
            WHERE folder = ?
            ORDER BY date_last DESC
            LIMIT ? OFFSET ?
        """, (folder, limit, offset)).fetchall()
        return [self._row_to_result(r) for r in rows]

    def get_stats(self) -> dict:
        stats = {}
        stats["total_threads"] = self._conn.execute(
            "SELECT COUNT(*) FROM threads"
        ).fetchone()[0]
        stats["total_messages"] = self._conn.execute(
            "SELECT COUNT(*) FROM message_thread_map"
        ).fetchone()[0]
        row = self._conn.execute(
            "SELECT MIN(date_first), MAX(date_last) FROM threads"
        ).fetchone()
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
        return [{"name": r["folder"], "thread_count": r["thread_count"]}
                for r in rows]

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
            score=float(row["score"]) if "score" in row.keys() else 0.0,
        )
