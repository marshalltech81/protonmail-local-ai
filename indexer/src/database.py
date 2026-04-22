"""
SQLite database layer.
Uses FTS5 for keyword search and sqlite-vec for vector similarity search.
Thread-level indexing: one row per thread, updated as new messages arrive.
"""

import functools
import json
import logging
import sqlite3
import threading
from pathlib import Path

import sqlite_vec

log = logging.getLogger("indexer.database")

SCHEMA_VERSION = 6

# Schema v3 uses FTS5 ``contentless_delete=1``, which SQLite added in 3.43.
# Running against an older runtime silently degrades (migration errors caught
# by a broad except) or hard-fails at boot — both difficult to diagnose in
# production. Validate the runtime version at Database init and fail fast
# with a clear message instead.
MIN_SQLITE_VERSION = (3, 43, 0)

# Vector dimension reserved by the threads_vec schema (FLOAT[768]). The
# embedding model's output dimension must match this, or vec0 inserts fail.
# nomic-embed-text is 768-dim and is the default. A startup-time check in
# main.py validates the running model against this constant.
EMBEDDING_DIM = 768


class SQLiteTooOldError(RuntimeError):
    """Raised when the runtime SQLite library is older than required."""


def _require_minimum_sqlite() -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        required = ".".join(str(x) for x in MIN_SQLITE_VERSION)
        raise SQLiteTooOldError(
            f"indexer requires SQLite >= {required}, "
            f"runtime is {sqlite3.sqlite_version}. Schema v3 FTS5 "
            "contentless_delete=1 will not work on this runtime. Rebuild "
            "the indexer image from a base that ships a newer SQLite "
            "(python:3.14-slim-trixie ships 3.46.1)."
        )


def _synchronized(fn):
    """Serialize ``Database`` method calls across threads.

    The indexer runs two concurrent DB writers: the watchdog observer
    (``MaildirHandler`` callbacks) and the main loop (periodic
    reconciler sweeps). Python's ``sqlite3`` module allows cross-thread
    connection use via ``check_same_thread=False``, but individual
    ``BEGIN IMMEDIATE``/execute/``commit`` sequences are not atomic at
    the Python layer — interleaving can trigger ``sqlite3.OperationalError``
    ("cannot start a transaction within a transaction") or silently
    commit partial state. A per-instance re-entrant lock around every
    public method makes the whole transaction atomic from the caller's
    perspective.
    """

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


class Database:
    def __init__(self, path: Path):
        _require_minimum_sqlite()
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Load sqlite-vec extension for vector search
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        # Performance tuning
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        return conn

    # -------------------------------------------------------------------------
    # Schema migrations
    # Each _apply_vN method is idempotent and only runs when the stored version
    # is below N. Bump SCHEMA_VERSION and add a new _apply_vN for each change.
    # -------------------------------------------------------------------------

    def _migrate(self):
        cur = self._conn.cursor()

        # schema_version must exist before we can read from it
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
        """)
        self._conn.commit()

        row = cur.execute("SELECT version FROM schema_version").fetchone()
        current = row["version"] if row else 0

        if current < 1:
            self._apply_v1(cur)
        if current < 2:
            self._apply_v2(cur)
        if current < 3:
            self._apply_v3(cur)
        if current < 4:
            self._apply_v4(cur)
        if current < 5:
            self._apply_v5(cur)
        if current < 6:
            self._apply_v6(cur)

        # UPDATE if a row exists, INSERT if this is a fresh database.
        # INSERT OR REPLACE would insert a new row (new primary key) rather
        # than overwriting the existing one, leaving both rows in the table.
        if current == 0:
            cur.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
        else:
            cur.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        self._conn.commit()
        log.info(f"Database ready at {self.path} (schema v{SCHEMA_VERSION})")

    def _apply_v1(self, cur: sqlite3.Cursor):
        """Initial schema."""
        cur.execute("""
            CREATE TABLE IF NOT EXISTS threads (
                thread_id    TEXT PRIMARY KEY,
                subject      TEXT NOT NULL,
                participants TEXT NOT NULL,  -- JSON array
                folder       TEXT NOT NULL,
                date_first   TEXT NOT NULL,
                date_last    TEXT NOT NULL,
                message_ids  TEXT NOT NULL,  -- JSON array
                snippet      TEXT,
                has_attachments INTEGER DEFAULT 0
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS message_thread_map (
                message_id TEXT PRIMARY KEY,
                thread_id  TEXT NOT NULL,
                filepath   TEXT NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
            )
        """)

        cur.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS threads_fts
            USING fts5(
                thread_id UNINDEXED,
                subject,
                participants,
                body,
                content='',
                tokenize='porter unicode61'
            )
        """)

        cur.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS threads_vec
            USING vec0(
                thread_id TEXT PRIMARY KEY,
                embedding FLOAT[768]
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS indexed_files (
                filepath TEXT PRIMARY KEY,
                indexed_at TEXT NOT NULL
            )
        """)
        self._conn.commit()

    def _apply_v2(self, cur: sqlite3.Cursor):
        """Add body_text column to threads.

        Stores the accumulated embedding text for a thread so that when a new
        message arrives, its content can be appended to the existing body
        rather than regenerated only from the new message. Without this column,
        get_thread returns an empty messages list and upsert_thread would embed
        only the latest message, discarding all thread history.
        """
        try:
            cur.execute("ALTER TABLE threads ADD COLUMN body_text TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists on a fresh database built from v1
        self._conn.commit()

    def _apply_v3(self, cur: sqlite3.Cursor):
        """Rebuild ``threads_fts`` with ``contentless_delete=1`` and track
        its rowid on each thread so updates/deletes actually remove rows.

        The v1 schema created ``threads_fts`` as a contentless FTS5 table
        without the ``contentless_delete`` option, and with an ``UNINDEXED``
        ``thread_id`` column. Two issues follow from that:

        1. ``DELETE FROM threads_fts WHERE thread_id = ?`` silently no-ops on
           a contentless FTS5 table — so every update or rebuild accumulated
           stale rows rather than replacing them.
        2. UNINDEXED columns in a contentless FTS5 table always read back as
           ``NULL``, which broke the MCP keyword-search join
           (``JOIN threads ON threads_fts.thread_id = threads.thread_id``).

        SQLite >= 3.43 adds ``contentless_delete=1`` which makes
        ``DELETE FROM fts WHERE rowid = ?`` work. We store each row's rowid
        in a new ``threads.fts_rowid`` column so the writer side can delete
        a specific FTS row before re-inserting updated content, and the
        reader side can join on the rowid instead of the always-null
        ``thread_id``.
        """
        try:
            cur.execute("ALTER TABLE threads ADD COLUMN fts_rowid INTEGER")
        except sqlite3.OperationalError:
            pass
        cur.execute("DROP TABLE IF EXISTS threads_fts")
        cur.execute(
            """
            CREATE VIRTUAL TABLE threads_fts
            USING fts5(
                subject,
                participants,
                body,
                content='',
                contentless_delete=1,
                tokenize='porter unicode61'
            )
            """
        )
        rows = cur.execute(
            "SELECT thread_id, subject, participants, body_text FROM threads"
        ).fetchall()
        for r in rows:
            cur.execute(
                "INSERT INTO threads_fts (subject, participants, body) VALUES (?, ?, ?)",
                (r["subject"], r["participants"], r["body_text"] or ""),
            )
            cur.execute(
                "UPDATE threads SET fts_rowid = ? WHERE thread_id = ?",
                (cur.lastrowid, r["thread_id"]),
            )
        self._conn.commit()

    def _apply_v4(self, cur: sqlite3.Cursor):
        """Add pending_deletions table for deletion reconciliation.

        Records messages whose Maildir file has been flagged deleted by mbsync
        (via the IMAP \\Deleted / Maildir ``T`` flag). The reconciler sweeps
        this table and removes thread/FTS/vec rows only after a configurable
        grace window — nothing in the primary tables is changed at tombstone
        time, which keeps the soft-delete reversible if mbsync un-sets the
        flag on a subsequent pull.
        """
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_deletions (
                filepath   TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                thread_id  TEXT NOT NULL,
                marked_at  TEXT NOT NULL
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_deletions_thread "
            "ON pending_deletions(thread_id)"
        )
        self._conn.commit()

    def _apply_v5(self, cur: sqlite3.Cursor):
        """Normalize ``pending_deletions.marked_at`` to ISO 8601 UTC.

        ``add_pending_deletion`` previously wrote timestamps via SQLite's
        ``datetime('now')`` function, which produces ``"YYYY-MM-DD HH:MM:SS"``
        (space separator, no TZ). The reaper computes its cutoff from Python's
        ``datetime.now(UTC).isoformat()`` → ``"YYYY-MM-DDTHH:MM:SS+00:00"``
        (``T`` separator, explicit TZ). The ``WHERE marked_at <= ?`` query
        is a lexicographic string compare, and space (0x20) sorts before
        ``T`` (0x54), so a tombstone marked e.g. ``"2024-12-25 10:00:00"``
        compared against cutoff ``"2024-12-25T00:00:00+00:00"`` looks older
        than the cutoff even though it was actually created *after*. Result:
        messages get reaped up to a day earlier than the grace window
        promises.

        Rewrite existing rows to the ISO 8601 UTC format so the sorted
        comparison is well-defined. Going forward ``add_pending_deletion``
        stores timestamps in the same format.
        """
        cur.execute(
            "UPDATE pending_deletions "
            "SET marked_at = REPLACE(marked_at, ' ', 'T') || '+00:00' "
            "WHERE marked_at LIKE '____-__-__ __:__:__'"
        )
        self._conn.commit()

    def _apply_v6(self, cur: sqlite3.Cursor):
        """Add a ``senders`` JSON column to ``threads``.

        The ``from_addr`` MCP search filter previously matched against
        ``participants`` (the union of ``From`` / ``To`` / ``Cc``), so
        "from alice" returned threads where alice was a recipient. That
        overpromises — users reading the filter name expect sender-only
        matching. ``senders`` stores only the ``From`` addresses of each
        message in the thread, so the filter can be honest.

        Existing rows get an empty JSON array; the filter implementation
        falls back to ``participants`` for rows with empty senders, so
        behavior is unchanged until the indexer reprocesses a thread and
        populates the new column.
        """
        try:
            cur.execute("ALTER TABLE threads ADD COLUMN senders TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # Column already exists on a fresh database built from v1
        self._conn.commit()

    # -------------------------------------------------------------------------
    # Write operations
    # -------------------------------------------------------------------------

    @_synchronized
    def build_merged_body(self, thread) -> str:
        """Compute the final ``body_text`` this thread will have after upsert.

        Read-only. Intended for callers that need to embed the merged body
        before it is written — e.g. the indexer computes a thread embedding
        from the accumulated thread text, not from only the newly-arrived
        message's ``text_for_embedding()``. Without this step the FTS body
        reflects the whole thread while the vector embedding drifts toward
        the latest message alone, degrading semantic search.
        """
        existing = self._conn.execute(
            "SELECT body_text, message_ids FROM threads WHERE thread_id = ?",
            (thread.thread_id,),
        ).fetchone()
        return self._compute_body(thread, existing)

    @staticmethod
    def _compute_body(thread, existing) -> str:
        """Pure function: body_text given the incoming thread and existing row.

        On insert, ``text_for_embedding()`` already sees all messages. On
        update, ``thread.messages`` only holds the newly-arrived message,
        so append its content to the stored ``body_text`` rather than
        regenerating from scratch.
        """
        if existing and existing["body_text"]:
            existing_message_ids = set(json.loads(existing["message_ids"] or "[]"))
            new_messages = [m for m in thread.messages if m.message_id not in existing_message_ids]
            if new_messages:
                new_content = "\n".join(
                    f"From: {m.from_addr}\nDate: {m.date.isoformat()}\n{m.body_text[:2000]}"
                    for m in new_messages
                )
                return (existing["body_text"] + "\n" + new_content)[:8000]
            return existing["body_text"]
        return thread.text_for_embedding()

    @_synchronized
    def upsert_thread(self, thread, embedding: list[float], body: str | None = None):
        """Insert or update a thread in all three indexes.

        On update, accumulated thread metadata is merged with the incoming
        ``Thread`` rather than replaced. ``threader.assign_thread`` returns a
        Thread whose ``messages`` list only contains the newly-arrived message
        (``get_thread`` deliberately returns ``messages=[]``), so blindly
        serializing ``thread.messages`` / ``thread.participants`` /
        ``has_attachments`` into the ON CONFLICT UPDATE would clobber the
        existing thread's accumulated state. Merge rules:

        - ``message_ids``: union existing and incoming, preserving order
        - ``participants``: union existing and incoming, preserving order
        - ``has_attachments``: true if previously true or newly true
        - ``date_first``: min(existing, incoming)
        - ``body_text``: existing body plus any previously-unseen messages

        ``body`` override: callers that computed the merged body via
        ``build_merged_body`` and embedded from it should pass the same
        value back in so the stored body matches what the embedding
        represents. When omitted, the merged body is recomputed here.
        """
        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding has {len(embedding)} dims but threads_vec reserves "
                f"{EMBEDDING_DIM}. Check OLLAMA_EMBED_MODEL."
            )

        cur = self._conn.cursor()

        incoming_message_ids = [m.message_id for m in thread.messages]
        incoming_participants = list(thread.participants)
        incoming_senders = [
            m.from_addr.strip() for m in thread.messages if m.from_addr and m.from_addr.strip()
        ]
        incoming_has_attachments = int(any(m.has_attachments for m in thread.messages))

        try:
            cur.execute("BEGIN IMMEDIATE")

            existing = cur.execute(
                "SELECT body_text, message_ids, participants, senders, "
                "has_attachments, date_first "
                "FROM threads WHERE thread_id = ?",
                (thread.thread_id,),
            ).fetchone()

            if existing:
                existing_ids = json.loads(existing["message_ids"] or "[]")
                merged_ids = list(dict.fromkeys(existing_ids + incoming_message_ids))
                existing_participants = json.loads(existing["participants"] or "[]")
                merged_participants = list(
                    dict.fromkeys(existing_participants + incoming_participants)
                )
                existing_senders = json.loads(existing["senders"] or "[]")
                merged_senders = list(dict.fromkeys(existing_senders + incoming_senders))
                merged_has_attachments = int(
                    bool(existing["has_attachments"]) or bool(incoming_has_attachments)
                )
                # Lexicographic min() is safe on ISO 8601 datetime strings
                # once they are normalized to UTC (parser._parse_date).
                merged_date_first = min(existing["date_first"], thread.date_first.isoformat())
            else:
                merged_ids = incoming_message_ids
                merged_participants = incoming_participants
                merged_senders = list(dict.fromkeys(incoming_senders))
                merged_has_attachments = incoming_has_attachments
                merged_date_first = thread.date_first.isoformat()

            # Either use the caller's pre-computed merged body (so the stored
            # body matches what they embedded) or compute it here. Both paths
            # produce the same result when nothing else has written between
            # the caller's build_merged_body() and this upsert.
            if body is None:
                body = self._compute_body(thread, existing)

            participants_json = json.dumps(merged_participants)
            senders_json = json.dumps(merged_senders)
            message_ids_json = json.dumps(merged_ids)
            snippet = thread.snippet()
            date_last = thread.date_last.isoformat()

            # Upsert main thread record
            cur.execute(
                """
                INSERT INTO threads
                    (thread_id, subject, participants, senders, folder,
                     date_first, date_last, message_ids, snippet, has_attachments,
                     body_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    participants    = excluded.participants,
                    senders         = excluded.senders,
                    date_first      = excluded.date_first,
                    date_last       = excluded.date_last,
                    message_ids     = excluded.message_ids,
                    snippet         = excluded.snippet,
                    has_attachments = excluded.has_attachments,
                    body_text       = excluded.body_text
                """,
                (
                    thread.thread_id,
                    thread.subject,
                    participants_json,
                    senders_json,
                    thread.folder,
                    merged_date_first,
                    date_last,
                    message_ids_json,
                    snippet,
                    merged_has_attachments,
                    body,
                ),
            )

            # Update message→thread mapping for all messages
            for msg in thread.messages:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO message_thread_map
                        (message_id, thread_id, filepath)
                    VALUES (?, ?, ?)
                    """,
                    (msg.message_id, thread.thread_id, msg.filepath),
                )

                cur.execute(
                    """
                    INSERT OR REPLACE INTO indexed_files
                        (filepath, indexed_at)
                    VALUES (?, datetime('now'))
                    """,
                    (msg.filepath,),
                )

            # Update FTS5 index. threads_fts is contentless_delete=1 so DELETE
            # requires a specific rowid — read the existing fts_rowid and then
            # record the new rowid after INSERT.
            self._replace_fts_row(cur, thread.thread_id, thread.subject, participants_json, body)

            # Update vector index — vec0 virtual tables do not support
            # INSERT OR REPLACE conflict resolution; use DELETE + INSERT instead.
            cur.execute("DELETE FROM threads_vec WHERE thread_id = ?", (thread.thread_id,))
            cur.execute(
                "INSERT INTO threads_vec (thread_id, embedding) VALUES (?, ?)",
                (thread.thread_id, sqlite_vec.serialize_float32(embedding)),
            )

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _replace_fts_row(
        self,
        cur: sqlite3.Cursor,
        thread_id: str,
        subject: str,
        participants_json: str,
        body: str,
    ) -> None:
        """Delete any prior FTS row for ``thread_id`` and insert a fresh one.

        Depends on ``threads.fts_rowid`` tracking the FTS rowid; without it
        the DELETE would no-op silently and stale tokens would linger in the
        index (see v3 migration notes).
        """
        existing = cur.execute(
            "SELECT fts_rowid FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if existing and existing["fts_rowid"] is not None:
            cur.execute("DELETE FROM threads_fts WHERE rowid = ?", (existing["fts_rowid"],))
        cur.execute(
            "INSERT INTO threads_fts (subject, participants, body) VALUES (?, ?, ?)",
            (subject, participants_json, body),
        )
        cur.execute(
            "UPDATE threads SET fts_rowid = ? WHERE thread_id = ?",
            (cur.lastrowid, thread_id),
        )

    # -------------------------------------------------------------------------
    # Read operations
    # -------------------------------------------------------------------------

    @_synchronized
    def find_thread_by_message_id(self, message_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT thread_id FROM message_thread_map WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return row["thread_id"] if row else None

    @_synchronized
    def find_threads_by_subject(
        self, normalized_subject: str, folder: str, limit: int = 10
    ) -> list[str]:
        """Return up to ``limit`` candidate thread ids matching the normalized
        subject within ``folder``, newest first.

        Multiple candidates are returned so the subject-fallback gate in
        ``Threader`` can keep looking when the most recent same-subject
        thread fails participant/date checks (e.g. an unrelated "Invoice"
        reply beat a valid older thread to the top of the list).
        """
        rows = self._conn.execute(
            """
            SELECT thread_id FROM threads
            WHERE subject = ? AND folder = ?
            ORDER BY date_last DESC
            LIMIT ?
            """,
            (normalized_subject, folder, limit),
        ).fetchall()
        return [r["thread_id"] for r in rows]

    @_synchronized
    def get_thread(self, thread_id: str):
        """Load a thread from the database (for adding new messages to)."""
        row = self._conn.execute(
            "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if not row:
            return None

        from datetime import datetime

        from .threader import Thread

        return Thread(
            thread_id=row["thread_id"],
            subject=row["subject"],
            participants=json.loads(row["participants"]),
            # messages is intentionally empty — the caller appends the new
            # message. Body accumulation is handled in upsert_thread via the
            # stored body_text column, not by re-parsing messages from disk.
            messages=[],
            folder=row["folder"],
            date_first=datetime.fromisoformat(row["date_first"]),
            date_last=datetime.fromisoformat(row["date_last"]),
        )

    @_synchronized
    def is_indexed(self, filepath: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM indexed_files WHERE filepath = ?", (filepath,)
        ).fetchone()
        return row is not None

    @_synchronized
    def get_stats(self) -> dict:
        stats = {}
        stats["total_threads"] = self._conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        stats["total_messages"] = self._conn.execute(
            "SELECT COUNT(*) FROM message_thread_map"
        ).fetchone()[0]
        stats["oldest_message"] = self._conn.execute(
            "SELECT MIN(date_first) FROM threads"
        ).fetchone()[0]
        stats["newest_message"] = self._conn.execute(
            "SELECT MAX(date_last) FROM threads"
        ).fetchone()[0]
        return stats

    # -------------------------------------------------------------------------
    # Reconciliation support — filepath tracking, tombstones, thread rebuild
    # -------------------------------------------------------------------------

    @_synchronized
    def iter_message_map(self) -> list[sqlite3.Row]:
        """Return every (message_id, thread_id, filepath) row for sweeping."""
        return self._conn.execute(
            "SELECT message_id, thread_id, filepath FROM message_thread_map"
        ).fetchall()

    @_synchronized
    def get_message_map_entry(self, message_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT message_id, thread_id, filepath FROM message_thread_map WHERE message_id = ?",
            (message_id,),
        ).fetchone()

    @_synchronized
    def find_message_entry_by_filepath(self, filepath: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT message_id, thread_id, filepath FROM message_thread_map WHERE filepath = ?",
            (filepath,),
        ).fetchone()

    @_synchronized
    def count_total_messages(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM message_thread_map").fetchone()
        return int(row[0]) if row else 0

    @_synchronized
    def get_thread_messages(self, thread_id: str) -> list[sqlite3.Row]:
        """All (message_id, filepath) rows for a thread, used to rebuild it."""
        return self._conn.execute(
            "SELECT message_id, filepath FROM message_thread_map WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()

    @_synchronized
    def update_filepath(self, old_path: str, new_path: str) -> None:
        """Update message_thread_map + indexed_files after a Maildir rename.

        mbsync renames a Maildir file whenever flags change (e.g. S → SR when
        the message is replied to). Keep the stored path in sync so later
        reconciliation sweeps can still find the file.
        """
        if old_path == new_path:
            return
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                "UPDATE message_thread_map SET filepath = ? WHERE filepath = ?",
                (new_path, old_path),
            )
            cur.execute("DELETE FROM indexed_files WHERE filepath = ?", (old_path,))
            cur.execute(
                "INSERT OR REPLACE INTO indexed_files (filepath, indexed_at) "
                "VALUES (?, datetime('now'))",
                (new_path,),
            )
            cur.execute(
                "UPDATE pending_deletions SET filepath = ? WHERE filepath = ?",
                (new_path, old_path),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def add_pending_deletion(self, filepath: str, message_id: str, thread_id: str) -> bool:
        """Record a tombstone. Returns True if newly inserted, False if already present.

        Uses INSERT OR IGNORE so repeated sweeps over the same T-flagged file
        do not churn the marked_at timestamp — the grace window is measured
        from when the file was *first* seen as tombstoned.

        ``marked_at`` is written as an ISO 8601 UTC string so that the
        reaper's ``WHERE marked_at <= ?`` comparison against
        ``datetime.now(UTC).isoformat()`` is well-defined. SQLite's own
        ``datetime('now')`` returns a space-separated format that sorts
        lexicographically before ``T``-separated ISO strings and would
        cause tombstones to be reaped up to a day early.
        """
        from datetime import UTC, datetime

        cur = self._conn.cursor()
        marked_at = datetime.now(UTC).isoformat()
        cur.execute(
            "INSERT OR IGNORE INTO pending_deletions "
            "(filepath, message_id, thread_id, marked_at) "
            "VALUES (?, ?, ?, ?)",
            (filepath, message_id, thread_id, marked_at),
        )
        self._conn.commit()
        return cur.rowcount > 0

    @_synchronized
    def clear_pending_deletion(self, filepath: str) -> None:
        self._conn.execute("DELETE FROM pending_deletions WHERE filepath = ?", (filepath,))
        self._conn.commit()

    @_synchronized
    def has_pending_deletion(self, filepath: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM pending_deletions WHERE filepath = ?", (filepath,)
        ).fetchone()
        return row is not None

    @_synchronized
    def count_pending_deletions(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM pending_deletions").fetchone()
        return int(row[0]) if row else 0

    @_synchronized
    def list_pending_deletions_older_than(self, cutoff_iso: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT filepath, message_id, thread_id, marked_at "
            "FROM pending_deletions WHERE marked_at <= ? ORDER BY marked_at ASC",
            (cutoff_iso,),
        ).fetchall()

    @_synchronized
    def remove_message(self, message_id: str) -> None:
        """Remove a message's map + indexed_files + tombstone rows.

        Does not touch the parent thread row — the caller is responsible for
        rebuilding or deleting the thread after determining how many messages
        remain. Prefer ``reap_thread_messages`` when the thread rebuild and
        the message removals need to land atomically as one transaction.
        """
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            self._remove_message_row(cur, message_id)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def delete_thread_completely(self, thread_id: str) -> None:
        """Remove a thread and every derived row. Used when the last message
        in a thread has been reaped.
        """
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute(
                "SELECT fts_rowid FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            filepaths = [
                r["filepath"]
                for r in cur.execute(
                    "SELECT filepath FROM message_thread_map WHERE thread_id = ?",
                    (thread_id,),
                ).fetchall()
            ]
            if row and row["fts_rowid"] is not None:
                cur.execute("DELETE FROM threads_fts WHERE rowid = ?", (row["fts_rowid"],))
            cur.execute("DELETE FROM threads_vec WHERE thread_id = ?", (thread_id,))
            cur.execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))
            cur.execute("DELETE FROM message_thread_map WHERE thread_id = ?", (thread_id,))
            cur.execute("DELETE FROM pending_deletions WHERE thread_id = ?", (thread_id,))
            for fp in filepaths:
                cur.execute("DELETE FROM indexed_files WHERE filepath = ?", (fp,))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def rebuild_thread(self, thread, embedding: list[float]) -> None:
        """Fully rewrite a thread row after a message has been removed.

        Unlike ``upsert_thread``, this path always regenerates ``body_text``
        from the supplied messages rather than appending to the stored body.
        The caller is expected to pass a ``Thread`` whose ``messages`` list
        reflects the surviving messages only (re-parsed from disk).
        """
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            self._rewrite_thread_row(cur, thread, embedding)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @_synchronized
    def reap_thread_messages(
        self,
        thread,
        embedding: list[float],
        reaped_message_ids: list[str],
    ) -> list[str]:
        """Atomically rewrite a thread and remove reaped messages.

        The reconciler previously called ``rebuild_thread`` and then looped
        ``remove_message`` — three or more separate transactions. If the
        process crashed between them, the thread row reflected only
        survivors while ``message_thread_map`` and ``pending_deletions``
        still held rows for the reaped messages. The recovery path worked
        (a second reap pass completed idempotently) but any observer
        running between the two commits saw inconsistent state.

        All writes now happen inside a single ``BEGIN IMMEDIATE`` / commit
        so either the whole reap lands or none of it does.

        Returns the filepaths that were removed, so the caller can perform
        any on-disk unlink work outside the transaction.
        """
        cur = self._conn.cursor()
        removed_filepaths: list[str] = []
        try:
            cur.execute("BEGIN IMMEDIATE")
            self._rewrite_thread_row(cur, thread, embedding)
            for mid in reaped_message_ids:
                fp = self._remove_message_row(cur, mid)
                if fp is not None:
                    removed_filepaths.append(fp)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return removed_filepaths

    def _rewrite_thread_row(self, cur: sqlite3.Cursor, thread, embedding: list[float]) -> None:
        """Replace a thread row and its FTS/vec entries using ``cur``.

        Shared by ``rebuild_thread`` and ``reap_thread_messages`` so the
        same rewrite can participate in a larger transaction when needed.
        The caller owns ``BEGIN`` / ``COMMIT`` / ``ROLLBACK``.
        """
        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding has {len(embedding)} dims but threads_vec reserves "
                f"{EMBEDDING_DIM}. Check OLLAMA_EMBED_MODEL."
            )

        participants_json = json.dumps(thread.participants)
        senders_json = json.dumps(
            list(
                dict.fromkeys(
                    m.from_addr.strip()
                    for m in thread.messages
                    if m.from_addr and m.from_addr.strip()
                )
            )
        )
        message_ids_json = json.dumps([m.message_id for m in thread.messages])
        snippet = thread.snippet()
        date_first = thread.date_first.isoformat()
        date_last = thread.date_last.isoformat()
        has_attachments = int(any(m.has_attachments for m in thread.messages))
        body = thread.text_for_embedding()

        cur.execute(
            """
            INSERT INTO threads
                (thread_id, subject, participants, senders, folder,
                 date_first, date_last, message_ids, snippet, has_attachments,
                 body_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                subject         = excluded.subject,
                participants    = excluded.participants,
                senders         = excluded.senders,
                date_first      = excluded.date_first,
                date_last       = excluded.date_last,
                message_ids     = excluded.message_ids,
                snippet         = excluded.snippet,
                has_attachments = excluded.has_attachments,
                body_text       = excluded.body_text
            """,
            (
                thread.thread_id,
                thread.subject,
                participants_json,
                senders_json,
                thread.folder,
                date_first,
                date_last,
                message_ids_json,
                snippet,
                has_attachments,
                body,
            ),
        )

        self._replace_fts_row(cur, thread.thread_id, thread.subject, participants_json, body)

        cur.execute("DELETE FROM threads_vec WHERE thread_id = ?", (thread.thread_id,))
        cur.execute(
            "INSERT INTO threads_vec (thread_id, embedding) VALUES (?, ?)",
            (thread.thread_id, sqlite_vec.serialize_float32(embedding)),
        )

    @staticmethod
    def _remove_message_row(cur: sqlite3.Cursor, message_id: str) -> str | None:
        """Remove a message's map / indexed_files / tombstone rows using
        ``cur``. Returns the message's filepath (for optional on-disk
        cleanup), or ``None`` if no such message was tracked. Shared by
        ``remove_message`` and ``reap_thread_messages``; the caller owns
        the enclosing transaction.
        """
        row = cur.execute(
            "SELECT filepath FROM message_thread_map WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            return None
        filepath = row["filepath"]
        cur.execute("DELETE FROM message_thread_map WHERE message_id = ?", (message_id,))
        cur.execute("DELETE FROM indexed_files WHERE filepath = ?", (filepath,))
        cur.execute("DELETE FROM pending_deletions WHERE filepath = ?", (filepath,))
        return filepath
