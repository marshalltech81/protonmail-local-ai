"""
SQLite database layer.
Uses FTS5 for keyword search and sqlite-vec for vector similarity search.
Thread-level indexing: one row per thread, updated as new messages arrive.
"""

import json
import logging
import sqlite3
from pathlib import Path

import sqlite_vec

log = logging.getLogger("indexer.database")

SCHEMA_VERSION = 3


class Database:
    def __init__(self, path: Path):
        self.path = path
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

    # -------------------------------------------------------------------------
    # Write operations
    # -------------------------------------------------------------------------

    def upsert_thread(self, thread, embedding: list[float]):
        """Insert or update a thread in all three indexes.

        Body text is accumulated across calls: when a new message arrives in an
        existing thread, its content is appended to the stored body_text rather
        than regenerated from scratch. This is necessary because get_thread
        returns an empty messages list (it does not re-parse files from disk),
        so thread.text_for_embedding() at update time only sees the new message.
        """
        cur = self._conn.cursor()

        participants_json = json.dumps(thread.participants)
        message_ids_json = json.dumps([m.message_id for m in thread.messages])
        snippet = thread.snippet()
        date_first = thread.date_first.isoformat()
        date_last = thread.date_last.isoformat()
        has_attachments = int(any(m.has_attachments for m in thread.messages))

        try:
            cur.execute("BEGIN IMMEDIATE")

            # Accumulate body text rather than regenerating from the (possibly
            # incomplete) messages list. On insert, text_for_embedding() has all
            # messages. On update, append only previously unseen message content.
            existing = cur.execute(
                "SELECT body_text, message_ids FROM threads WHERE thread_id = ?",
                (thread.thread_id,),
            ).fetchone()

            if existing and existing["body_text"]:
                existing_message_ids = set(json.loads(existing["message_ids"] or "[]"))
                new_messages = [
                    m for m in thread.messages if m.message_id not in existing_message_ids
                ]
                if new_messages:
                    new_content = "\n".join(
                        f"From: {m.from_addr}\nDate: {m.date.isoformat()}\n{m.body_text[:2000]}"
                        for m in new_messages
                    )
                    body = (existing["body_text"] + "\n" + new_content)[:8000]
                else:
                    body = existing["body_text"]
            else:
                body = thread.text_for_embedding()

            # Upsert main thread record
            cur.execute(
                """
                INSERT INTO threads
                    (thread_id, subject, participants, folder,
                     date_first, date_last, message_ids, snippet, has_attachments,
                     body_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    participants    = excluded.participants,
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
                    thread.folder,
                    date_first,
                    date_last,
                    message_ids_json,
                    snippet,
                    has_attachments,
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

    def find_thread_by_message_id(self, message_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT thread_id FROM message_thread_map WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return row["thread_id"] if row else None

    def find_thread_by_subject(self, normalized_subject: str, folder: str) -> str | None:
        row = self._conn.execute(
            """
            SELECT thread_id FROM threads
            WHERE subject = ? AND folder = ?
            ORDER BY date_last DESC LIMIT 1
            """,
            (normalized_subject, folder),
        ).fetchone()
        return row["thread_id"] if row else None

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

    def is_indexed(self, filepath: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM indexed_files WHERE filepath = ?", (filepath,)
        ).fetchone()
        return row is not None

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
