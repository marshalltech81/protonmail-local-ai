"""
SQLite database layer.
Uses FTS5 for keyword search and sqlite-vec for vector similarity search.
Thread-level indexing: one row per thread, updated as new messages arrive.
"""

import functools
import json
import logging
import sqlite3
import struct
import threading
import weakref
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from email.utils import parseaddr
from pathlib import Path

import sqlite_vec

from .chunker import l2_normalize, truncate_to_tokens
from .extractors import OCR_DISABLED_ERROR, SCANNED_PDF_OCR_DISABLED_ERROR
from .threader import (
    PER_MESSAGE_BODY_CAP_CHARS,
    THREAD_BODY_TEXT_MAX_TOKENS,
    Thread,
    canonical_addr,
)

log = logging.getLogger("indexer.database")


def _close_connection(conn: sqlite3.Connection) -> None:
    conn.close()


def _dedupe_by_canonical(addrs: list[str]) -> list[str]:
    """Dedup address display strings, first-seen display wins.

    Keys on the canonical bare address (``parseaddr`` + lowercase) so
    variants like ``Bob Smith <bob@x>`` and ``bob@x`` collapse into a
    single entry rather than accumulating both. Entries with no
    recoverable email address (``canonical_addr`` returns ``""``) are
    keyed on their lowercased stripped value instead of being dropped.
    """
    seen: set[str] = set()
    result: list[str] = []
    for addr in addrs:
        stripped = (addr or "").strip()
        if not stripped:
            continue
        canonical = canonical_addr(stripped)
        key = canonical or stripped.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(stripped)
    return result


# ``_apply_initial_schema`` builds the complete current schema, stamped
# version 0. Earlier history (v1-v22) was squashed into it and
# renumbered while no deployed database existed; a database from that
# numbering reads as newer than the code and must be rebuilt from
# Maildir.
#
# Bumping ``SCHEMA_VERSION`` requires shipping a forward migration file
# at ``src/migrations/<NNNN>_<slug>.sql`` covering the new version.
# Fresh installs apply ``_apply_initial_schema`` directly and stamp the
# current version; existing installs run the migration runner to catch
# up. See ``src/migrations/runner.py`` for the file layout and
# transactional guarantees.
SCHEMA_VERSION = 0

# The schema uses FTS5 ``contentless_delete=1``, which SQLite added in 3.43.
# Validate the runtime version at Database init and fail fast with a clear
# message instead of degrading silently.
MIN_SQLITE_VERSION = (3, 43, 0)

# Vector dimension reserved by the ``*_vec`` schemas. Must match the
# active embedding model's output dimension or vec0 inserts fail.
# Qwen3-Embedding-8B is 4096-dim.
EMBEDDING_DIM = 4096

# Upper bound on the ``?`` placeholders bound into one ``IN (...)``
# lookup. The connection's ``SQLITE_LIMIT_VARIABLE_NUMBER`` depends on
# the SQLite build, so lookups over an unbounded ID list batch under it.
_IN_CLAUSE_BATCH_SIZE = 500


class SQLiteTooOldError(RuntimeError):
    """Raised when the runtime SQLite library is older than required."""


def _require_minimum_sqlite() -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        required = ".".join(str(x) for x in MIN_SQLITE_VERSION)
        raise SQLiteTooOldError(
            f"indexer requires SQLite >= {required}, "
            f"runtime is {sqlite3.sqlite_version}. FTS5 "
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
        self._transaction_depth = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._connect()
        self._closed = False
        self._finalizer = weakref.finalize(self, _close_connection, self._conn)
        try:
            self._migrate()
        except Exception:
            self.close()
            raise

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Load sqlite-vec extension for vector search
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA foreign_keys = ON")
        # Performance tuning
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        return conn

    def close(self) -> None:
        if self._closed:
            return
        self._finalizer.detach()
        self._conn.close()
        self._closed = True

    def __del__(self) -> None:
        # Best-effort cleanup during GC; never raise from a destructor
        # (interpreter shutdown can leave referenced modules torn down,
        # so any exception here would be unactionable noise).
        with suppress(Exception):
            self.close()

    @_synchronized
    def wal_checkpoint_truncate(self) -> tuple[int, int, int]:
        """Run ``PRAGMA wal_checkpoint(TRUNCATE)`` and return the counters.

        Returns ``(busy, log_pages, checkpointed_pages)`` straight from
        SQLite. ``busy`` is non-zero when another connection holds an
        open read or write transaction that the checkpoint must wait
        for, so it could not complete — the next pass will retry. An
        open connection does not pin a snapshot by itself; only an
        open transaction does. SQLite's automatic checkpoint copies
        frames back and lets later writes reuse the WAL from the
        start, but it never shrinks the file, and while any reader
        holds a transaction open the WAL keeps growing. Without this
        periodic truncate the file stays at its high-water size, which
        on a long-running container has been observed to reach
        hundreds of MB.

        Goes through ``_synchronized`` so a checkpoint cannot interleave
        with an open writer's transaction. The PRAGMA itself is a
        single SQL statement so the lock hold is short.
        """
        row = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None:
            return 0, 0, 0
        # Row order is (busy, log, checkpointed) per SQLite docs.
        return int(row[0]), int(row[1]), int(row[2])

    def _begin_if_needed(self, cur: sqlite3.Cursor) -> bool:
        if self._transaction_depth > 0:
            return False
        cur.execute("BEGIN IMMEDIATE")
        return True

    def _commit_if_started(self, started: bool) -> None:
        if started:
            self._conn.commit()

    def _rollback_if_started(self, started: bool) -> None:
        if started:
            self._conn.rollback()

    @contextmanager
    def transaction(self):
        """Run several database writes as one atomic unit.

        Public write helpers normally manage their own ``BEGIN``/``COMMIT``.
        The indexing pipeline needs a wider boundary so thread, chunk,
        attachment, and vector rows cannot be left half-written if the final
        step fails. Nested calls reuse the outer transaction and only the
        outermost context commits or rolls back.
        """
        with self._lock:
            outermost = self._transaction_depth == 0
            if outermost:
                self._conn.execute("BEGIN IMMEDIATE")
            self._transaction_depth += 1
            try:
                yield
            except Exception:
                self._transaction_depth -= 1
                if outermost:
                    self._conn.rollback()
                raise
            else:
                self._transaction_depth -= 1
                if outermost:
                    self._conn.commit()

    # -------------------------------------------------------------------------
    # Schema setup
    # Fresh installs apply ``_apply_initial_schema`` directly. Existing
    # installs at a lower stored version run forward migration files
    # from ``src/migrations/`` via the runner. Existing installs at a
    # higher stored version (a downgrade) are rejected — see ``_migrate``.
    # -------------------------------------------------------------------------

    def _migrate(self):
        """Create the schema if it doesn't exist; otherwise migrate or verify.

        Fresh installs apply ``_apply_initial_schema`` directly and stamp
        the current ``SCHEMA_VERSION`` — they skip every historical
        migration file. Existing installs at a lower stored version run
        the forward migration files in ``src/migrations/`` to catch up.
        Stored versions higher than the code's ``SCHEMA_VERSION`` (a
        downgrade attempt) are rejected — wipe the volume or upgrade
        the image.
        """
        from .migrations import runner as migration_runner

        cur = self._conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        row = cur.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            self._apply_initial_schema(cur)
            log.info(f"Database initialized at {self.path} (schema v{SCHEMA_VERSION})")
            return

        stored = row["version"]
        if stored == SCHEMA_VERSION:
            log.info(f"Database ready at {self.path} (schema v{SCHEMA_VERSION})")
            return

        if stored > SCHEMA_VERSION:
            raise RuntimeError(
                f"Schema version mismatch: stored v{stored} is newer than code "
                f"v{SCHEMA_VERSION}. Downgrade migrations are not supported; "
                "either upgrade the indexer image or wipe the sqlite-volume "
                "and let the indexer rebuild from Maildir."
            )

        migration_dir = Path(__file__).parent / "migrations"
        log.info(f"Migrating database at {self.path}: v{stored} -> v{SCHEMA_VERSION}")
        applied = migration_runner.apply_pending(
            self._conn,
            current_version=stored,
            target_version=SCHEMA_VERSION,
            migration_dir=migration_dir,
        )
        log.info(
            f"Database ready at {self.path} (schema v{SCHEMA_VERSION}, "
            f"applied migrations: {applied})"
        )

    def _apply_initial_schema(self, cur: sqlite3.Cursor):
        """Create every table the indexer needs, in their final shape.

        Three families of tables, each with its FTS5 + sqlite-vec
        sidecar where applicable:

        * **Thread-level coarse retrieval** — ``threads`` (one row per
          conversation) plus ``threads_fts`` (BM25 keyword search) and
          ``threads_vec`` (vector search). The thread vector is the
          mean of its chunks' vectors so coarse and precise retrieval
          share source data. ``fts_rowid`` on ``threads`` lets the
          writer delete a specific FTS row before re-inserting updated
          content.
        * **Per-message chunk precision retrieval** — ``message_chunks``
          (paragraph-packed slices keyed by deterministic SHA-256
          chunk_id), ``message_chunks_fts``, and ``message_chunks_vec``.
          ``attachment_id`` is non-null for chunks derived from a
          specific attachment; null for body chunks.
        * **Attachment indexing** — ``attachments`` (one per occurrence,
          captures filename/MIME), ``attachments_fts`` (filename + MIME
          search), and ``attachment_extractions`` (per content-hash
          cache so OCR / PDF parse cost runs at most once per unique
          payload regardless of forwarding count).

        Plus the cross-cutting tables: ``message_thread_map`` (message
        → thread index), ``indexed_files`` (file identity for rename
        detection), ``pending_deletions`` (tombstones for the opt-in
        deletion reconciler), ``indexing_jobs`` (durable retry +
        dead-letter queue for the parse → embed → upsert pipeline), and
        ``ingestion_state`` (last sync + indexer liveness for status).
        """
        # One transaction for every table and the version stamp, so an
        # interruption part-way leaves an empty database that the next
        # start initializes again, never half a schema with no version
        # row. ``executescript`` commits anything pending and adds no
        # transaction of its own, so the script opens one with BEGIN and
        # leaves it open for the stamp.
        try:
            self._run_initial_schema_script(cur)
            cur.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            self._conn.commit()
        except BaseException:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise

    def _run_initial_schema_script(self, cur: sqlite3.Cursor) -> None:
        cur.executescript(f"""
            BEGIN IMMEDIATE;
            -- Thread-level coarse retrieval
            CREATE TABLE threads (
                thread_id       TEXT PRIMARY KEY,
                subject         TEXT NOT NULL,           -- normalized matching key (lowercased, prefix-stripped)
                participants    TEXT NOT NULL,           -- JSON array
                senders         TEXT NOT NULL DEFAULT '[]',  -- JSON array (From only)
                folder          TEXT NOT NULL,
                date_first      TEXT NOT NULL,
                date_last       TEXT NOT NULL,
                message_ids     TEXT NOT NULL,           -- JSON array
                snippet         TEXT,
                has_attachments INTEGER DEFAULT 0,
                body_text       TEXT,
                fts_rowid       INTEGER,
                display_subject TEXT                     -- original-cased subject for retrieval; NULL until set, COALESCE'd back to subject by readers
            );

            -- mcp-server hybrid-search joins ``threads`` back from ``threads_fts``
            -- on ``threads_fts.rowid = threads.fts_rowid``. Without this index
            -- SQLite planned ``SCAN threads`` for every FTS hit, which turned
            -- search_emails O(N_fts × N_threads) and blew the 4-min MCP timeout
            -- on populated mailboxes.
            CREATE INDEX idx_threads_fts_rowid ON threads(fts_rowid);

            -- Threader subject-fallback lookup runs once per incoming
            -- message that fails In-Reply-To and References lookups —
            -- equality on subject + folder, DESC by date_last, projects
            -- thread_id (see find_threads_by_subject). The first three
            -- columns satisfy filter + sort in one B-tree walk;
            -- including thread_id as the fourth key column makes the
            -- index COVERING for the projection, so SQLite returns the
            -- thread_id straight from the index without a per-match
            -- table seek. A 50k-message initial scan does not serialize
            -- 50k full table scans through the ``_synchronized`` writer
            -- lock.
            CREATE INDEX idx_threads_subject_folder
                ON threads(subject, folder, date_last, thread_id);

            CREATE VIRTUAL TABLE threads_fts USING fts5(
                subject,
                participants,
                body,
                content='',
                contentless_delete=1,
                tokenize='porter unicode61'
            );

            CREATE VIRTUAL TABLE threads_vec USING vec0(
                thread_id TEXT PRIMARY KEY,
                embedding FLOAT[{EMBEDDING_DIM}]
            );

            -- Per-message chunks (precision retrieval)
            CREATE TABLE message_chunks (
                chunk_id        TEXT PRIMARY KEY,
                message_id      TEXT NOT NULL,
                thread_id       TEXT NOT NULL,
                chunk_index     INTEGER NOT NULL,
                text            TEXT NOT NULL,
                char_start      INTEGER NOT NULL,
                char_end        INTEGER NOT NULL,
                token_est       INTEGER NOT NULL,
                chunked_at      TEXT NOT NULL,
                fts_rowid       INTEGER,
                attachment_id   TEXT,
                message_date    TEXT NOT NULL,           -- source message's Date: header; timeline retrieval orders by it
                FOREIGN KEY (message_id) REFERENCES message_thread_map(message_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX idx_message_chunks_message ON message_chunks(message_id);
            CREATE INDEX idx_message_chunks_thread ON message_chunks(thread_id);
            CREATE INDEX idx_message_chunks_attachment ON message_chunks(attachment_id);
            -- Hybrid-search chunk lane joins ``message_chunks`` back from
            -- ``message_chunks_fts`` on ``fts_rowid``. Critical for query
            -- latency on populated mailboxes.
            CREATE INDEX idx_message_chunks_fts_rowid ON message_chunks(fts_rowid);

            CREATE VIRTUAL TABLE message_chunks_fts USING fts5(
                text,
                content='',
                contentless_delete=1,
                tokenize='porter unicode61'
            );

            CREATE VIRTUAL TABLE message_chunks_vec USING vec0(
                chunk_id TEXT PRIMARY KEY,
                embedding FLOAT[{EMBEDDING_DIM}]
            );

            -- Attachment indexing
            CREATE TABLE attachments (
                attachment_occurrence_id TEXT PRIMARY KEY,
                message_id                TEXT NOT NULL,
                attachment_id             TEXT NOT NULL,
                thread_id                 TEXT NOT NULL,
                filename                  TEXT NOT NULL,
                content_type              TEXT NOT NULL,
                size_bytes                INTEGER NOT NULL,
                seen_at                   TEXT NOT NULL,
                fts_rowid                 INTEGER,
                FOREIGN KEY (message_id) REFERENCES message_thread_map(message_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX idx_attachments_attachment_id ON attachments(attachment_id);
            CREATE INDEX idx_attachments_thread ON attachments(thread_id);
            CREATE INDEX idx_attachments_message ON attachments(message_id);
            -- Hybrid-search attachment lane joins ``attachments`` back from
            -- ``attachments_fts`` on ``fts_rowid``.
            CREATE INDEX idx_attachments_fts_rowid ON attachments(fts_rowid);

            CREATE TABLE attachment_extractions (
                attachment_id      TEXT PRIMARY KEY,
                extraction_status  TEXT NOT NULL,
                extractor          TEXT,
                extracted_text     TEXT,
                extraction_error   TEXT,
                extracted_at       TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE attachments_fts USING fts5(
                filename,
                content_type,
                content='',
                contentless_delete=1,
                tokenize='porter unicode61'
            );

            -- Cross-cutting tables
            CREATE TABLE message_thread_map (
                message_id TEXT PRIMARY KEY,
                thread_id  TEXT NOT NULL,
                filepath   TEXT NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
            );
            -- Flag renames match rows by filepath; thread rebuilds and
            -- removals match them by thread_id.
            CREATE INDEX idx_message_thread_map_filepath ON message_thread_map(filepath);
            CREATE INDEX idx_message_thread_map_thread ON message_thread_map(thread_id);

            -- One row per indexed message: the authoritative per-message
            -- record (own headers, send time, folder, source locator and
            -- content hash) behind exact enumeration and provenance.
            -- Cascades from ``message_thread_map`` so every existing
            -- message / thread removal path cleans it up.
            CREATE TABLE messages (
                message_id      TEXT PRIMARY KEY,
                thread_id       TEXT NOT NULL,
                filepath        TEXT NOT NULL,
                folder          TEXT NOT NULL,
                subject         TEXT NOT NULL,
                sent_at         TEXT NOT NULL,
                in_reply_to     TEXT,
                references_json TEXT NOT NULL,
                has_attachments INTEGER NOT NULL,
                size_bytes      INTEGER,
                content_hash    TEXT,
                indexed_at      TEXT NOT NULL,
                FOREIGN KEY (message_id) REFERENCES message_thread_map(message_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX idx_messages_thread_sent ON messages(thread_id, sent_at);
            CREATE INDEX idx_messages_folder_sent ON messages(folder, sent_at);
            CREATE INDEX idx_messages_sent ON messages(sent_at);
            -- Flag renames and cross-folder moves update by filepath.
            CREATE INDEX idx_messages_filepath ON messages(filepath);

            -- Normalized From / To / Cc. ``address`` is the canonical
            -- lowercased bare address, so "every message from/to X" is an
            -- indexed lookup rather than a scan of JSON participant lists.
            CREATE TABLE message_participants (
                message_id TEXT NOT NULL,
                role       TEXT NOT NULL CHECK (role IN ('from', 'to', 'cc')),
                address    TEXT NOT NULL,
                name       TEXT,
                PRIMARY KEY (message_id, role, address),
                FOREIGN KEY (message_id) REFERENCES messages(message_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX idx_message_participants_address
                ON message_participants(address, role);

            CREATE TABLE indexed_files (
                filepath     TEXT PRIMARY KEY,
                indexed_at   TEXT NOT NULL,
                size         INTEGER,
                mtime_ns     INTEGER,
                content_hash TEXT
            );

            -- Tombstones for opt-in deletion reconciler. ``marked_at``
            -- is ISO 8601 UTC so the reaper's lexicographic cutoff
            -- comparison is well-defined.
            CREATE TABLE pending_deletions (
                filepath   TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                thread_id  TEXT NOT NULL,
                marked_at  TEXT NOT NULL
            );
            CREATE INDEX idx_pending_deletions_thread
                ON pending_deletions(thread_id);

            -- Durable retry / dead-letter queue. Every discovered file
            -- is enqueued first; the worker loop claims due rows and
            -- runs parse/thread/embed/upsert. Failures back off
            -- exponentially up to ``INDEXER_MAX_ATTEMPTS`` then
            -- transition to ``status='dead'``.
            CREATE TABLE indexing_jobs (
                filepath        TEXT PRIMARY KEY,
                reason          TEXT NOT NULL,
                status          TEXT NOT NULL,
                attempts        INTEGER NOT NULL DEFAULT 0,
                last_error      TEXT,
                last_stage      TEXT,
                last_error_class TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                next_attempt_at TEXT NOT NULL
            );
            CREATE INDEX idx_indexing_jobs_status_next
                ON indexing_jobs(status, next_attempt_at);

            -- One row: mbsync's last successful sync as the indexer last
            -- read it from the Maildir stamp, and when the indexer last
            -- reported. mcp-server's ``get_mailbox_status`` reads it.
            CREATE TABLE ingestion_state (
                id                 INTEGER PRIMARY KEY CHECK (id = 1),
                sync_completed_at  TEXT,
                sync_interval_secs INTEGER,
                indexer_seen_at    TEXT NOT NULL
            );
        """)

    # -------------------------------------------------------------------------
    # Write operations
    # -------------------------------------------------------------------------

    @staticmethod
    def _compute_body(thread, existing) -> str:
        """Pure function: body_text given the incoming thread and existing row.

        On insert, ``text_for_embedding()`` already sees all messages. On
        update, ``thread.messages`` only holds the newly-arrived message,
        so append its content to the stored ``body_text`` rather than
        regenerating from scratch.
        """
        if existing and existing["body_text"]:
            existing_message_ids = set(json.loads(existing["message_ids"]))
            new_messages = [m for m in thread.messages if m.message_id not in existing_message_ids]
            if new_messages:
                # Per-message char cap shared with ``Thread.text_for_embedding``
                # so a thread that arrived as one message gets the same FTS
                # body coverage as a thread that arrived as a sequence of
                # replies.
                new_content = "\n".join(
                    f"From: {m.from_addr}\nDate: {m.date.isoformat()}\n"
                    f"{m.body_text[:PER_MESSAGE_BODY_CAP_CHARS]}"
                    for m in new_messages
                )
                return truncate_to_tokens(
                    existing["body_text"] + "\n" + new_content,
                    THREAD_BODY_TEXT_MAX_TOKENS,
                )
            return existing["body_text"]
        return thread.text_for_embedding()

    @_synchronized
    def upsert_thread(self, thread, embedding: list[float]):
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
        """
        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding has {len(embedding)} dims but threads_vec reserves "
                f"{EMBEDDING_DIM}. Check the embedder's output dimension."
            )
        # Storage invariant: every vector in ``threads_vec`` is unit-norm
        # so cosine similarity equals dot product downstream. Callers
        # like Phase 1 seed (``mean_vector(existing_chunks)``) pass
        # non-unit means; normalize at the boundary so no caller has to
        # remember. The placeholder all-zero seed survives — see
        # ``l2_normalize``.
        embedding = l2_normalize(embedding)

        cur = self._conn.cursor()

        incoming_message_ids = [m.message_id for m in thread.messages]
        incoming_participants = list(thread.participants)
        incoming_senders = [m.from_addr for m in thread.messages if m.from_addr]
        incoming_has_attachments = int(any(m.has_attachments for m in thread.messages))
        # display_subject: pick the oldest incoming message's original
        # subject as the human-facing label. The threader strips Re:/Fwd:
        # and lowercases ``thread.subject`` for grouping, so the original
        # only survives on the Message objects. The on-update merge runs
        # in Python below (``merged_display_subject``) — a naive COALESCE
        # in ON CONFLICT cannot distinguish the "in-order arrival, keep
        # original" case from the "reply arrived first, replace with the
        # later-discovered older root" case, and would trap a ``Re:``
        # subject as the display label whenever messages are indexed
        # out of order.
        incoming_display_subject: str | None = None
        incoming_earliest_date_iso: str | None = None
        if thread.messages:
            earliest = min(thread.messages, key=lambda m: m.date)
            incoming_display_subject = earliest.subject or None
            incoming_earliest_date_iso = earliest.date.isoformat()

        started = False
        try:
            started = self._begin_if_needed(cur)

            existing = cur.execute(
                "SELECT body_text, message_ids, participants, senders, "
                "has_attachments, date_first, date_last, snippet, "
                "display_subject "
                "FROM threads WHERE thread_id = ?",
                (thread.thread_id,),
            ).fetchone()

            if existing:
                existing_ids = json.loads(existing["message_ids"])
                merged_ids = list(dict.fromkeys(existing_ids + incoming_message_ids))
                existing_participants = json.loads(existing["participants"])
                merged_participants = _dedupe_by_canonical(
                    existing_participants + incoming_participants
                )
                existing_senders = json.loads(existing["senders"])
                merged_senders = _dedupe_by_canonical(existing_senders + incoming_senders)
                merged_has_attachments = int(
                    bool(existing["has_attachments"]) or bool(incoming_has_attachments)
                )
                # Lexicographic min() is safe on ISO 8601 datetime strings
                # once they are normalized to UTC (parser._parse_date).
                merged_date_first = min(existing["date_first"], thread.date_first.isoformat())
                # display_subject merge: prefer the subject of the
                # oldest message we have ever seen for this thread.
                # Three cases:
                #   1. Existing display_subject is NULL (the first
                #      non-NULL writer hasn't arrived yet) →
                #      take the incoming.
                #   2. The incoming earliest message is older than the
                #      currently-recorded ``date_first`` → the new
                #      message is the new "root" and its subject is
                #      cleaner than whatever ``Re:`` reply may have
                #      been recorded as the display label first → take
                #      the incoming.
                #   3. Otherwise (the existing row was already populated
                #      and the incoming message is not older) → keep
                #      the existing label so a later ``Re:`` reply
                #      cannot clobber the cleaner original subject.
                existing_display = existing["display_subject"]
                if not existing_display:
                    merged_display_subject = incoming_display_subject
                elif (
                    incoming_earliest_date_iso is not None
                    and incoming_earliest_date_iso < existing["date_first"]
                ):
                    merged_display_subject = incoming_display_subject or existing_display
                else:
                    merged_display_subject = existing_display
            else:
                merged_ids = incoming_message_ids
                merged_participants = _dedupe_by_canonical(incoming_participants)
                merged_senders = _dedupe_by_canonical(incoming_senders)
                merged_has_attachments = incoming_has_attachments
                merged_date_first = thread.date_first.isoformat()
                merged_display_subject = incoming_display_subject

            body = self._compute_body(thread, existing)

            participants_json = json.dumps(merged_participants)
            senders_json = json.dumps(merged_senders)
            message_ids_json = json.dumps(merged_ids)
            # Preserve the existing snippet when the newly-arrived message is
            # strictly older than the stored date_last. thread.snippet() is
            # derived from the appended message only (get_thread returns
            # messages=[] by design), so an out-of-order older message would
            # otherwise replace a preview that still represents the actual
            # newest message in the thread — date_last is merged via max()
            # above, and the snippet should track that same rule.
            snippet = thread.snippet()
            if existing and existing["snippet"] and thread.messages:
                newest_incoming = max(m.date for m in thread.messages).isoformat()
                if newest_incoming < existing["date_last"]:
                    snippet = existing["snippet"]
            date_last = thread.date_last.isoformat()

            # Upsert main thread record. ``display_subject`` was merged
            # in Python above (``merged_display_subject``) — see that
            # block for the date-driven precedence rules. Passing the
            # already-resolved value lets the SQL stay simple and lets
            # ON CONFLICT just overwrite, mirroring how every other
            # column flows through the upsert.
            cur.execute(
                """
                INSERT INTO threads
                    (thread_id, subject, participants, senders, folder,
                     date_first, date_last, message_ids, snippet, has_attachments,
                     body_text, display_subject)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    participants    = excluded.participants,
                    senders         = excluded.senders,
                    date_first      = excluded.date_first,
                    date_last       = excluded.date_last,
                    message_ids     = excluded.message_ids,
                    snippet         = excluded.snippet,
                    has_attachments = excluded.has_attachments,
                    body_text       = excluded.body_text,
                    display_subject = excluded.display_subject
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
                    merged_display_subject,
                ),
            )

            # Update message→thread mapping for all messages
            for msg in thread.messages:
                cur.execute(
                    """
                    INSERT INTO message_thread_map
                        (message_id, thread_id, filepath)
                    VALUES (?, ?, ?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        thread_id = excluded.thread_id,
                        filepath  = excluded.filepath
                    """,
                    (msg.message_id, thread.thread_id, msg.filepath),
                )

                cur.execute(
                    """
                    INSERT OR REPLACE INTO indexed_files
                        (filepath, indexed_at, size, mtime_ns, content_hash)
                    VALUES (?, datetime('now'), ?, ?, ?)
                    """,
                    (msg.filepath, msg.size, msg.mtime_ns, msg.content_hash),
                )

                self._write_message_record(cur, msg, thread.thread_id)

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

            self._commit_if_started(started)
        except Exception:
            self._rollback_if_started(started)
            raise

    # -------------------------------------------------------------------------
    # Per-message chunks — diff-based idempotent write,
    # cascading delete, mean-of-chunks thread vector aggregation.
    # -------------------------------------------------------------------------

    @_synchronized
    def get_chunk_ids_for_message(
        self, message_id: str, attachment_id: str | None = None
    ) -> set[str]:
        """Return the set of stored ``chunk_id`` values for ``message_id``.

        Used by the indexer write path to compute the diff between newly
        chunked output and what is already stored. Chunks are paragraph-
        packed and the chunker's IDs are deterministic
        (``sha256(message_pk || index || text)``) — so an unchanged body
        yields a byte-identical ID set, and only genuinely new chunks
        need an embed call.

        ``attachment_id`` selects which slice of the message's chunks to
        return:

        * ``None`` (default) — chunks derived from the message body only
          (``attachment_id IS NULL`` rows). Matches the schema-v9 contract.
        * a string — chunks derived from that specific attachment within
          the message. Used by the indexer to diff per-attachment chunks
          independently of body chunks.
        """
        if attachment_id is None:
            rows = self._conn.execute(
                "SELECT chunk_id FROM message_chunks "
                "WHERE message_id = ? AND attachment_id IS NULL",
                (message_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT chunk_id FROM message_chunks WHERE message_id = ? AND attachment_id = ?",
                (message_id, attachment_id),
            ).fetchall()
        return {row["chunk_id"] for row in rows}

    @_synchronized
    def replace_message_chunks(
        self,
        *,
        message_id: str,
        thread_id: str,
        chunks,
        embeddings_by_chunk_id: dict[str, list[float]],
        attachment_id: str | None = None,
        message_date: str,
    ) -> dict[str, int]:
        """Idempotently sync the chunk rows for one slice of a message.

        ``chunks`` is the full ordered ``list[MessageChunk]`` the chunker
        emitted for the slice. ``embeddings_by_chunk_id`` must contain
        an embedding for every chunk_id in ``chunks`` that is *new*
        relative to what's already stored — embeddings for existing
        chunk_ids are not touched (the chunk text is unchanged so the
        prior embedding is still valid). Returns ``{"inserted": n,
        "deleted": m, "kept": k}`` for observability.

        ``attachment_id`` selects which slice of the message's chunks
        this call manages:

        * ``None`` (default) — body chunks for the message
          (``attachment_id IS NULL`` rows). Body and attachment chunks
          coexist for the same message; passing ``None`` only diffs
          against body rows so an attachment write does not delete body
          chunks and vice versa.
        * a string — attachment chunks for that specific
          ``attachment_id`` within the message. The same content
          forwarded across N messages produces N distinct chunk
          occurrences (one per parent thread) so any chunk hit can lift
          its parent thread into ranking.

        ``message_date`` is the source message's ``Date:`` header in
        ISO 8601 form (``msg.date.isoformat()``), stored on every new
        chunk row so timeline-style retrieval
        (``get_recent_chunks_for_thread`` / ``summarize_thread``) can
        order by message time instead of the chunker's wall-clock
        insert time. The parser always yields a date (falling back to
        ingest time for a missing or unparseable header), so the
        column is ``NOT NULL`` and readers need no fallback.

        All inserts / deletes across ``message_chunks``,
        ``message_chunks_fts`` and ``message_chunks_vec`` happen inside
        one transaction so the three indexes never disagree about which
        chunks exist for a (message, slice) pair.
        """
        incoming_ids = {c.chunk_id for c in chunks}
        cur = self._conn.cursor()

        started = False
        try:
            started = self._begin_if_needed(cur)

            if attachment_id is None:
                existing_rows = cur.execute(
                    "SELECT chunk_id, fts_rowid FROM message_chunks "
                    "WHERE message_id = ? AND attachment_id IS NULL",
                    (message_id,),
                ).fetchall()
            else:
                existing_rows = cur.execute(
                    "SELECT chunk_id, fts_rowid FROM message_chunks "
                    "WHERE message_id = ? AND attachment_id = ?",
                    (message_id, attachment_id),
                ).fetchall()
            existing_ids = {row["chunk_id"] for row in existing_rows}
            existing_fts_rowids = {
                row["chunk_id"]: row["fts_rowid"]
                for row in existing_rows
                if row["fts_rowid"] is not None
            }

            to_delete = existing_ids - incoming_ids
            to_insert = [c for c in chunks if c.chunk_id not in existing_ids]

            for chunk_id in to_delete:
                fts_rowid = existing_fts_rowids.get(chunk_id)
                if fts_rowid is not None:
                    cur.execute("DELETE FROM message_chunks_fts WHERE rowid = ?", (fts_rowid,))
                cur.execute("DELETE FROM message_chunks_vec WHERE chunk_id = ?", (chunk_id,))
                cur.execute("DELETE FROM message_chunks WHERE chunk_id = ?", (chunk_id,))

            now_iso = datetime.now(UTC).isoformat()
            for chunk in to_insert:
                embedding = embeddings_by_chunk_id.get(chunk.chunk_id)
                if embedding is None:
                    raise ValueError(
                        f"missing embedding for new chunk {chunk.chunk_id!r} "
                        f"(message_id={message_id!r})"
                    )
                if len(embedding) != EMBEDDING_DIM:
                    raise ValueError(
                        f"chunk embedding has {len(embedding)} dims but "
                        f"message_chunks_vec reserves {EMBEDDING_DIM}"
                    )
                # Storage invariant — see ``upsert_thread``. Production
                # writes flow through ``OpenAIEmbedder`` which already
                # normalizes provider output, but the ``EmbeddingBackend``
                # contract is a generic ``embed_batch`` call — a fake
                # backend in a test or a future caller computing
                # embeddings outside that path could pass non-unit
                # vectors. Enforce here so ``message_chunks_vec`` shares
                # the same end-to-end invariant as ``threads_vec``.
                normalized = l2_normalize(embedding)
                cur.execute(
                    "INSERT INTO message_chunks_fts (text) VALUES (?)",
                    (chunk.text,),
                )
                fts_rowid = cur.lastrowid
                cur.execute(
                    "INSERT INTO message_chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                    (chunk.chunk_id, sqlite_vec.serialize_float32(normalized)),
                )
                cur.execute(
                    """
                    INSERT INTO message_chunks
                        (chunk_id, message_id, thread_id, chunk_index, text,
                         char_start, char_end, token_est,
                         chunked_at, fts_rowid, attachment_id, message_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.chunk_id,
                        message_id,
                        thread_id,
                        chunk.chunk_index,
                        chunk.text,
                        chunk.char_start,
                        chunk.char_end,
                        chunk.token_est,
                        now_iso,
                        fts_rowid,
                        attachment_id,
                        message_date,
                    ),
                )

            self._commit_if_started(started)
        except Exception:
            self._rollback_if_started(started)
            raise

        return {
            "inserted": len(to_insert),
            "deleted": len(to_delete),
            "kept": len(existing_ids & incoming_ids),
        }

    @_synchronized
    def upsert_attachment(
        self,
        *,
        message_id: str,
        thread_id: str,
        attachment_id: str,
        filename: str,
        content_type: str,
        size_bytes: int,
        occurrence_id: str,
    ) -> bool:
        """Record one attachment occurrence on a message.

        Returns True if the row was newly inserted, False if it already
        existed. Idempotent — re-indexing the same message produces the
        same occurrence id for a specific attachment slot, and this call
        no-ops on the second call rather than churning ``seen_at`` or the
        FTS row.

        ``occurrence_id`` must be derived via
        ``attachment_indexing.attachment_occurrence_id`` so the formula
        stays in one place and write callers cannot drift from the
        indexer's own production path.

        The filename + MIME type are mirrored into the ``attachments_fts``
        contentless table for direct keyword search ("find the .pdf
        named contract"). The deterministic ``attachment_id`` (sha256
        of payload bytes) is what links the occurrence to its single
        cached extraction in ``attachment_extractions``.
        """
        cur = self._conn.cursor()
        started = False
        try:
            started = self._begin_if_needed(cur)
            existing = cur.execute(
                "SELECT 1 FROM attachments WHERE attachment_occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            if existing is not None:
                self._commit_if_started(started)
                return False

            now_iso = datetime.now(UTC).isoformat()
            cur.execute(
                "INSERT INTO attachments_fts (filename, content_type) VALUES (?, ?)",
                (filename, content_type),
            )
            fts_rowid = cur.lastrowid
            cur.execute(
                """
                INSERT INTO attachments
                    (attachment_occurrence_id, message_id, attachment_id, thread_id, filename,
                     content_type, size_bytes, seen_at, fts_rowid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    occurrence_id,
                    message_id,
                    attachment_id,
                    thread_id,
                    filename,
                    content_type,
                    size_bytes,
                    now_iso,
                    fts_rowid,
                ),
            )
            self._commit_if_started(started)
            return True
        except Exception:
            self._rollback_if_started(started)
            raise

    @_synchronized
    def get_extractor_names(self) -> list[str]:
        """Distinct extractor names recorded in the extraction cache."""
        rows = self._conn.execute(
            "SELECT DISTINCT extractor FROM attachment_extractions WHERE extractor IS NOT NULL"
        ).fetchall()
        return [r["extractor"] for r in rows]

    @_synchronized
    def find_filepaths_with_extractors(self, extractors: list[str]) -> list[str]:
        """Maildir filepaths of messages carrying an attachment whose
        cached extraction was written by one of ``extractors``, whatever
        that occurrence's own filename or MIME type."""
        if not extractors:
            return []
        placeholders = ",".join(["?"] * len(extractors))
        rows = self._conn.execute(
            f"""
            SELECT DISTINCT m.filepath
            FROM attachment_extractions e
            JOIN attachments a ON a.attachment_id = e.attachment_id
            JOIN message_thread_map m ON m.message_id = a.message_id
            WHERE e.extractor IN ({placeholders})
            ORDER BY m.filepath
            """,  # nosec B608 — placeholders only, values are bound
            extractors,
        ).fetchall()
        return [r["filepath"] for r in rows]

    @_synchronized
    def find_ocr_disabled_attachments(self) -> list[sqlite3.Row]:
        """Every attachment occurrence whose cached extraction is an "OCR
        disabled" result, with its message's Maildir filepath, filename,
        MIME type and the cached error."""
        return self._conn.execute(
            """
            SELECT m.filepath, a.filename, a.content_type, e.extraction_error
            FROM attachment_extractions e
            JOIN attachments a ON a.attachment_id = e.attachment_id
            JOIN message_thread_map m ON m.message_id = a.message_id
            WHERE e.extraction_status = 'unsupported'
              AND e.extraction_error IN (?, ?)
            ORDER BY m.filepath
            """,
            (OCR_DISABLED_ERROR, SCANNED_PDF_OCR_DISABLED_ERROR),
        ).fetchall()

    @_synchronized
    def get_attachment_extraction(self, attachment_id: str) -> sqlite3.Row | None:
        """Return the cached extraction row for an attachment, or None.

        Used by the indexer write path to skip extraction work whenever
        the same payload has already been processed. Even a failed prior
        extraction is returned — the caller can decide whether to retry
        based on ``extraction_status`` and how recent ``extracted_at``
        is.
        """
        return self._conn.execute(
            "SELECT attachment_id, extraction_status, extractor, "
            "extracted_text, extraction_error, extracted_at "
            "FROM attachment_extractions WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchone()

    @_synchronized
    def store_attachment_extraction(
        self,
        *,
        attachment_id: str,
        extraction_status: str,
        extractor: str | None,
        extracted_text: str | None,
        extraction_error: str | None,
    ) -> None:
        """Persist (or replace) the extraction record for ``attachment_id``.

        ``extraction_status`` is one of:

        * ``"success"`` — text extracted; ``extracted_text`` populated
        * ``"empty"`` — extractor ran but produced no text (image of a
          blank page, password-protected PDF with no fallback, etc.)
        * ``"unsupported"`` — no extractor registered for the MIME type
        * ``"too_large"`` — payload exceeds the configured byte cap
        * ``"failed"`` — extractor raised; ``extraction_error`` populated

        The same (attachment_id) is OR-REPLACE'd so a follow-up pass
        (e.g. after enabling OCR or bumping ``INDEXER_OCR_MAX_PAGES``)
        can upgrade a prior ``unsupported`` / ``empty`` status without
        churning the schema.
        """
        cur = self._conn.cursor()
        started = False
        try:
            started = self._begin_if_needed(cur)
            cur.execute(
                """
                INSERT OR REPLACE INTO attachment_extractions
                    (attachment_id, extraction_status, extractor,
                     extracted_text, extraction_error, extracted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    attachment_id,
                    extraction_status,
                    extractor,
                    extracted_text,
                    extraction_error,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._commit_if_started(started)
        except Exception:
            self._rollback_if_started(started)
            raise

    def _delete_attachments_for_message(self, cur: sqlite3.Cursor, message_id: str) -> None:
        """Drop all ``attachments`` occurrences and their FTS rows for ``message_id``.

        Cached ``attachment_extractions`` rows are left in place: another
        message may still reference the same content_hash, and even when
        nothing does today, retaining the cached extraction means a
        future re-arrival (forwarded from outside) skips the extract
        cost. A separate sweep can prune true orphans periodically.
        """
        rows = cur.execute(
            "SELECT fts_rowid FROM attachments WHERE message_id = ?", (message_id,)
        ).fetchall()
        for row in rows:
            if row["fts_rowid"] is not None:
                cur.execute("DELETE FROM attachments_fts WHERE rowid = ?", (row["fts_rowid"],))
        cur.execute("DELETE FROM attachments WHERE message_id = ?", (message_id,))

    @_synchronized
    def replace_thread_vector(self, thread_id: str, embedding: list[float]) -> None:
        """Replace the row in ``threads_vec`` for ``thread_id``.

        Used by the reconciler reap path to rewrite a thread vector as the
        mean of newly-emitted chunk vectors, without going through the
        full ``upsert_thread`` path (which requires a materialized
        ``Thread`` and would also rewrite the FTS row, body_text, and
        every metadata field unnecessarily). Validates the embedding
        dimension so a misconfigured embed model fails loud here rather
        than as a cryptic vec0 insert error.
        """
        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding has {len(embedding)} dims but threads_vec reserves "
                f"{EMBEDDING_DIM}. Check the embedder's output dimension."
            )
        # Storage invariant — see the matching note in ``upsert_thread``.
        # Phase 2c writes ``mean_vector(chunk_embs)`` here, which is
        # generally non-unit; normalize at the boundary.
        embedding = l2_normalize(embedding)
        cur = self._conn.cursor()
        started = False
        try:
            started = self._begin_if_needed(cur)
            cur.execute("DELETE FROM threads_vec WHERE thread_id = ?", (thread_id,))
            cur.execute(
                "INSERT INTO threads_vec (thread_id, embedding) VALUES (?, ?)",
                (thread_id, sqlite_vec.serialize_float32(embedding)),
            )
            self._commit_if_started(started)
        except Exception:
            self._rollback_if_started(started)
            raise

    @_synchronized
    def get_chunk_embeddings_for_messages(self, message_ids: list[str]) -> list[list[float]]:
        """Return every chunk embedding for the given ``message_ids``.

        Used by the reconciler's reap path to compute a survivor-only
        thread vector after a partial reap: the caller passes the
        surviving message ids, gets back their chunk embeddings, and
        means them with ``chunker.mean_vector``. Skipping the reaped
        messages here (rather than after a thread-wide fetch) keeps the
        reconciler's pre-transaction read cheap on threads with a long
        tail of historical messages.
        """
        if not message_ids:
            return []
        placeholders = ",".join(["?"] * len(message_ids))
        # Composed SQL is a fixed SELECT; user values are bound through
        # ``?`` placeholders. nosec B608.
        # ``ORDER BY c.chunk_id`` pins read order so ``mean_vector`` sums
        # in a deterministic sequence. Float64 addition is not associative,
        # so without this an idempotent replay can rewrite ``threads_vec``
        # with a marginally different blob, churning WAL pages.
        sql = (
            "SELECT v.embedding AS embedding "
            "FROM message_chunks c "
            "JOIN message_chunks_vec v ON v.chunk_id = c.chunk_id "
            f"WHERE c.message_id IN ({placeholders}) "  # nosec B608
            "ORDER BY c.chunk_id"
        )
        rows = self._conn.execute(sql, list(message_ids)).fetchall()
        result: list[list[float]] = []
        for row in rows:
            blob = row["embedding"]
            count = len(blob) // 4
            result.append(list(struct.unpack(f"{count}f", blob)))
        return result

    @_synchronized
    def get_phase1_seed_state(self, thread_id: str) -> tuple[list[list[float]], list[float] | None]:
        """Combined fetch for the batched indexer's Phase 1 seed selection.

        Returns ``(chunk_embeddings, prior_thread_vector)`` so the caller
        applies the three-case priority chain:
        non-empty chunks → ``mean(chunks)``; empty chunks + non-zero
        prior → prior; else → zero placeholder.

        Folds three reads into one method body (PK existence check + chunk
        embeddings JOIN + ``threads_vec`` lookup) under a single lock
        acquisition. The previous shape ran the chunk-embeddings fetch and the
        ``threads_vec`` lookup as separate calls after the existence check, each
        re-entering the ``_synchronized`` RLock and adding Python frames
        per Phase 1 message — measurable on a 50-message batch. The
        ``LEFT JOIN`` from ``threads`` collapses the PK check and the
        chunk fetch into a single statement: zero rows means no thread
        (new-thread fast path); one row with NULL ``chunk_emb`` means
        thread exists but is chunkless; N rows means N chunk vectors,
        ordered by ``chunk_id`` so the downstream mean is deterministic.
        """
        rows = self._conn.execute(
            """
            SELECT v.embedding AS chunk_emb
            FROM threads t
            LEFT JOIN message_chunks c ON c.thread_id = t.thread_id
            LEFT JOIN message_chunks_vec v ON v.chunk_id = c.chunk_id
            WHERE t.thread_id = ?
            ORDER BY c.chunk_id
            """,
            (thread_id,),
        ).fetchall()
        if not rows:
            return [], None
        chunk_embeddings: list[list[float]] = []
        for row in rows:
            blob = row["chunk_emb"]
            if blob is None:
                # Thread exists but has no chunks — the LEFT JOIN emits a
                # single NULL-bearing row in that case. Don't mistake it
                # for an empty embedding.
                continue
            count = len(blob) // 4
            chunk_embeddings.append(list(struct.unpack(f"{count}f", blob)))

        vec_row = self._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        prior_vec: list[float] | None = None
        if vec_row is not None:
            blob = vec_row["embedding"]
            count = len(blob) // 4
            prior_vec = list(struct.unpack(f"{count}f", blob))

        return chunk_embeddings, prior_vec

    @_synchronized
    def find_zero_vector_chunkless_thread_filepaths(self) -> list[str]:
        """Return Maildir filepaths of messages stuck on a chunkless,
        zero-vector thread.

        Symptom: thread + ``message_thread_map`` + ``indexed_files``
        rows committed (so ``is_indexed`` returns True; the normal
        ``initial_index`` walk won't re-enqueue them), but
        ``message_chunks`` is empty for the thread AND
        ``threads_vec`` carries the all-zero placeholder. The cause
        is one of:

          1. A hard crash (SIGKILL, OOM, power loss) between the
             batched indexer's Phase 1 and Phase 2c, on a thread
             that had no prior chunk vectors. Phase 1's commits
             durably landed but Phase 2c never ran. Queue
             state on restart depends on whether the queue row was
             already mark_failed'd before the crash; in either case
             the file is text-indexed but vectorless.
          2. A Phase 2 retry cascade that exhausted ``max_attempts``
             and dead-lettered the file. Phase 1's commits remain
             but ``claim_batch`` no longer returns a 'dead' row.

        Used by the indexer's startup recovery sweep
        (``_recover_zero_vector_threads``) to re-enqueue these
        files so the next drain pass can complete the indexing.

        Excludes chunkless threads with a non-zero ``threads_vec``
        row — those are healthy subject-fallback threads from
        blank-body messages and must not be re-indexed.
        """
        # Step 1: chunkless threads (typically a small set vs. the
        # full thread table on a healthy mailbox).
        chunkless_rows = self._conn.execute(
            """
            SELECT t.thread_id
            FROM threads t
            WHERE NOT EXISTS (
                SELECT 1 FROM message_chunks c WHERE c.thread_id = t.thread_id
            )
            """
        ).fetchall()
        if not chunkless_rows:
            return []

        # Step 2: pull the chunkless threads' stored vectors in batches
        # of ``_IN_CLAUSE_BATCH_SIZE``, then filter all-zero rows in
        # Python. vec0 supports PK ``WHERE thread_id IN (...)`` lookups
        # (each becomes an internal PK seek), so this is one SELECT per
        # batch instead of one per thread; batching keeps each
        # statement under the variable limit and holds at most one
        # batch of embedding blobs in memory. The all-zero check stays
        # in Python because vec0 doesn't expose equality predicates
        # against the embedding payload itself.
        chunkless_ids = [r["thread_id"] for r in chunkless_rows]
        stuck_thread_ids: list[str] = []
        for start in range(0, len(chunkless_ids), _IN_CLAUSE_BATCH_SIZE):
            id_batch = chunkless_ids[start : start + _IN_CLAUSE_BATCH_SIZE]
            placeholders = ",".join(["?"] * len(id_batch))
            vec_rows = self._conn.execute(
                f"SELECT thread_id, embedding FROM threads_vec WHERE thread_id IN ({placeholders})",  # nosec B608
                id_batch,
            ).fetchall()
            for row in vec_rows:
                blob = row["embedding"]
                count = len(blob) // 4
                vec = struct.unpack(f"{count}f", blob)
                if all(v == 0.0 for v in vec):
                    stuck_thread_ids.append(row["thread_id"])

        if not stuck_thread_ids:
            return []

        # Step 3: gather the Maildir filepaths for messages on stuck
        # threads. ``message_thread_map.filepath`` is the same value
        # the queue uses, so re-enqueueing routes through the same
        # parse → thread → embed → commit pipeline as a fresh scan.
        # Batched for the same variable limit as step 2.
        filepaths: list[str] = []
        for start in range(0, len(stuck_thread_ids), _IN_CLAUSE_BATCH_SIZE):
            id_batch = stuck_thread_ids[start : start + _IN_CLAUSE_BATCH_SIZE]
            placeholders = ",".join(["?"] * len(id_batch))
            rows = self._conn.execute(
                f"SELECT filepath FROM message_thread_map WHERE thread_id IN ({placeholders})",  # nosec B608
                id_batch,
            ).fetchall()
            filepaths.extend(r["filepath"] for r in rows)
        return filepaths

    @_synchronized
    def get_thread_display_subject(self, thread_id: str) -> str | None:
        """Return ``threads.display_subject`` for ``thread_id``, or ``None``.

        ``display_subject`` is the oldest message's original-case
        subject (with any ``Re:`` / ``Fwd:`` prefixes intact),
        maintained by ``upsert_thread``'s merge so the value is stable
        across the lifetime of the thread except when a genuinely
        older message arrives out of order. ``None`` when the thread
        does not exist or its ``display_subject`` is not set.

        Used by the chunkless-thread subject-fallback path in the
        indexer's Phase 2a so it embeds a stable subject text for the
        thread. Without it, the fallback used the newly-arrived
        message's subject (overwriting prior fallbacks on every
        chunkless reply, producing order-dependent thread vectors).
        The reconciler's reap rebuild does not read it: the stored
        value can still hold a reaped message's subject, so the
        rebuild embeds from survivors only.
        """
        row = self._conn.execute(
            "SELECT display_subject FROM threads WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            return None
        return row["display_subject"]

    @_synchronized
    def thread_has_chunks(self, thread_id: str) -> bool:
        """Return True iff at least one chunk row exists for ``thread_id``.

        Cheap existence check for the subject-fallback gate in the
        batched indexer's Phase 2a. ``get_thread_chunk_embeddings`` is
        the wrong tool for that check — it loads, blob-unpacks, and
        copies every chunk vector for the thread just so the caller
        can take ``bool(list)``. On chatty threads with hundreds of
        chunks that's wasted I/O on a hot per-message path.
        """
        row = self._conn.execute(
            "SELECT 1 FROM message_chunks WHERE thread_id = ? LIMIT 1",
            (thread_id,),
        ).fetchone()
        return row is not None

    @_synchronized
    def get_thread_chunk_embeddings(self, thread_id: str) -> list[list[float]]:
        """Return every chunk embedding stored for ``thread_id``.

        The indexer averages these to produce the thread-level vector,
        so coarse thread retrieval and precise chunk retrieval both
        derive from the same per-chunk source data. Returns an empty
        list when the thread has no chunks yet (a thread whose only
        message had an empty body, or where every embed previously
        failed).
        """
        # ``ORDER BY c.chunk_id`` pins read order — see the matching note
        # in ``get_chunk_embeddings_for_messages`` for why a deterministic
        # mean read matters.
        rows = self._conn.execute(
            """
            SELECT v.embedding AS embedding
            FROM message_chunks c
            JOIN message_chunks_vec v ON v.chunk_id = c.chunk_id
            WHERE c.thread_id = ?
            ORDER BY c.chunk_id
            """,
            (thread_id,),
        ).fetchall()
        # sqlite-vec stores embeddings as packed float32. Each row's
        # ``embedding`` blob is ``EMBEDDING_DIM * 4`` bytes; unpack to a
        # plain Python list so the caller can mean-pool without depending
        # on numpy.
        result: list[list[float]] = []
        for row in rows:
            blob = row["embedding"]
            count = len(blob) // 4
            result.append(list(struct.unpack(f"{count}f", blob)))
        return result

    @staticmethod
    def _delete_chunks_in_batches(
        cur: sqlite3.Cursor,
        rows: list[sqlite3.Row],
    ) -> None:
        """Bulk-delete the ``message_chunks_fts`` and ``message_chunks_vec``
        rows for a list of ``(chunk_id, fts_rowid)`` results.

        Issuing one ``DELETE`` per chunk is correct but slow on threads
        with thousands of chunks (FTS5 contentless tables and vec0 each
        take a per-row hit, so a 1000-chunk thread runs 2000 statements).
        Chunked ``WHERE ... IN (?, ?, ...)`` deletes amortise the
        per-statement overhead inside the same transaction.
        """
        if not rows:
            return

        chunk_ids: list[str] = []
        fts_rowids: list[int] = []
        for row in rows:
            chunk_ids.append(row["chunk_id"])
            if row["fts_rowid"] is not None:
                fts_rowids.append(row["fts_rowid"])

        # SQLite default ``SQLITE_LIMIT_VARIABLE_NUMBER`` is 32766 in
        # 3.32+, well above 500. Smaller batches keep memory/log noise
        # bounded for very large reaps.
        batch_size = 500
        for start in range(0, len(fts_rowids), batch_size):
            int_batch = fts_rowids[start : start + batch_size]
            placeholders = ",".join(["?"] * len(int_batch))
            cur.execute(
                f"DELETE FROM message_chunks_fts WHERE rowid IN ({placeholders})",  # nosec B608
                int_batch,
            )
        for start in range(0, len(chunk_ids), batch_size):
            str_batch = chunk_ids[start : start + batch_size]
            placeholders = ",".join(["?"] * len(str_batch))
            cur.execute(
                f"DELETE FROM message_chunks_vec WHERE chunk_id IN ({placeholders})",  # nosec B608
                str_batch,
            )

    def _delete_chunks_for_message(self, cur: sqlite3.Cursor, message_id: str) -> None:
        """Drop every chunk row + FTS + vec entry for ``message_id``.

        Internal helper used inside an enclosing transaction by
        ``_remove_message_row`` and the reconciler's reap path.
        """
        rows = cur.execute(
            "SELECT chunk_id, fts_rowid FROM message_chunks WHERE message_id = ?",
            (message_id,),
        ).fetchall()
        self._delete_chunks_in_batches(cur, rows)
        cur.execute("DELETE FROM message_chunks WHERE message_id = ?", (message_id,))

    def _delete_chunks_for_thread(self, cur: sqlite3.Cursor, thread_id: str) -> None:
        """Drop every chunk row + FTS + vec entry for ``thread_id``.

        Used when a thread is deleted in its entirety (last message
        reaped). The per-message helper would also work in a loop, but
        a single thread-id query is cheaper and matches the cascade
        semantics of ``delete_thread_completely``.
        """
        rows = cur.execute(
            "SELECT chunk_id, fts_rowid FROM message_chunks WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()
        self._delete_chunks_in_batches(cur, rows)
        cur.execute("DELETE FROM message_chunks WHERE thread_id = ?", (thread_id,))

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
        index (see the pre-squash migration notes in git history).
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

    # -------------------------------------------------------------------------
    # indexing_jobs — durable retry / dead-letter queue.
    #
    # The ``IndexingQueue`` abstraction in ``queue.py`` owns the retry /
    # backoff / dead-letter semantics. These methods are the thin SQL
    # layer — they serialize through the same ``_synchronized`` lock as
    # every other writer so queue updates never interleave with
    # ``upsert_thread`` / reconciler writes.
    # -------------------------------------------------------------------------

    @_synchronized
    def queue_enqueue(self, *, filepath: str, reason: str, status: str, now_iso: str) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO indexing_jobs
                (filepath, reason, status, attempts,
                 last_error, last_stage, last_error_class,
                 created_at, updated_at, next_attempt_at)
            VALUES (?, ?, ?, 0, NULL, NULL, NULL, ?, ?, ?)
            """,
            (filepath, reason, status, now_iso, now_iso, now_iso),
        )
        self._conn.commit()

    @_synchronized
    def queue_fetch_due_batch(self, status: str, now_iso: str, limit: int) -> list[sqlite3.Row]:
        """Return up to ``limit`` due ``status`` rows ordered by oldest-due first.

        One SELECT, so the batched indexer's gather phase picks up N
        distinct rows at once; the claim has no in-flight tracking, so
        rows stay due until the caller marks them.
        """
        return self._conn.execute(
            """
            SELECT filepath, reason, status, attempts,
                   last_error, last_stage,
                   created_at, updated_at, next_attempt_at
            FROM indexing_jobs
            WHERE status = ? AND next_attempt_at <= ?
            ORDER BY next_attempt_at ASC
            LIMIT ?
            """,
            (status, now_iso, limit),
        ).fetchall()

    @_synchronized
    def queue_delete(self, filepath: str) -> None:
        self._conn.execute("DELETE FROM indexing_jobs WHERE filepath = ?", (filepath,))
        self._conn.commit()

    @_synchronized
    def queue_get_attempts(self, filepath: str) -> int | None:
        row = self._conn.execute(
            "SELECT attempts FROM indexing_jobs WHERE filepath = ?", (filepath,)
        ).fetchone()
        return int(row["attempts"]) if row else None

    @_synchronized
    def queue_charge_attempt(
        self,
        *,
        filepath: str,
        max_attempts: int,
        marker_stage: str,
        marker_error: str,
        error_class: str,
        now_iso: str,
    ) -> bool:
        """Charge one attempt and mark the row as mid-step; return False
        (and dead-letter it) when interruptions already exhausted it.

        Conditional updates under the connection lock, so a concurrent
        ``queue_enqueue`` from the watchdog thread is either fully before
        (the reset row is charged from zero) or fully after (it resets
        the charge) — never interleaved with a stale read.
        """
        exhausted = self._conn.execute(
            """
            UPDATE indexing_jobs
            SET status = 'dead', last_error_class = ?, updated_at = ?
            WHERE filepath = ? AND status = 'queued'
              AND last_stage = ? AND attempts >= ?
            """,
            (error_class, now_iso, filepath, marker_stage, max_attempts),
        ).rowcount
        if not exhausted:
            self._conn.execute(
                """
                UPDATE indexing_jobs
                SET attempts = attempts + 1, last_stage = ?, last_error = ?
                WHERE filepath = ? AND status = 'queued'
                """,
                (marker_stage, marker_error, filepath),
            )
        self._conn.commit()
        return not exhausted

    @_synchronized
    def queue_mark_interrupted(
        self, *, filepaths: list[str], marker_stage: str, marker_error: str
    ) -> None:
        """Mark queued rows as mid-step without charging an attempt."""
        self._conn.executemany(
            """
            UPDATE indexing_jobs SET last_stage = ?, last_error = ?
            WHERE filepath = ? AND status = 'queued'
            """,
            [(marker_stage, marker_error, filepath) for filepath in filepaths],
        )
        self._conn.commit()

    @_synchronized
    def queue_refund_attempt(self, *, filepath: str, marker_stage: str) -> None:
        """Undo ``queue_charge_attempt`` if its mark is still on the row.
        A row re-enqueued meanwhile no longer carries the mark and is
        left alone."""
        self._conn.execute(
            """
            UPDATE indexing_jobs
            SET attempts = attempts - 1, last_stage = NULL, last_error = NULL
            WHERE filepath = ? AND last_stage = ? AND attempts > 0
            """,
            (filepath, marker_stage),
        )
        self._conn.commit()

    @_synchronized
    def queue_get_status(self, filepath: str) -> str | None:
        """Return ``"queued"`` / ``"dead"`` / ``None`` for ``filepath``.

        ``None`` when the row does not exist — the file has never been
        enqueued or has succeeded and been deleted. Callers (initial
        scan, reconciler) use this to decide whether to re-enqueue a
        path that's known to be dead-lettered: blindly re-enqueuing
        every dead row on every container restart turns one failed
        payload into a recurring retry storm against the same
        upstream (embedding service 500s on a poison-pill text), so the
        initial scan should skip those rows. Genuinely-fresh events
        (watchdog ``IN_MOVED_TO`` / ``IN_CREATED``) still go through
        ``enqueue`` which intentionally resets prior state — those
        signal real change in the file.
        """
        row = self._conn.execute(
            "SELECT status FROM indexing_jobs WHERE filepath = ?", (filepath,)
        ).fetchone()
        return row["status"] if row else None

    @_synchronized
    def queue_mark_failed(
        self,
        *,
        filepath: str,
        attempts: int,
        last_stage: str,
        last_error: str,
        error_class: str,
        now_iso: str,
        next_attempt_iso: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE indexing_jobs
            SET attempts = ?, last_stage = ?, last_error = ?, last_error_class = ?,
                updated_at = ?, next_attempt_at = ?, status = 'queued'
            WHERE filepath = ?
            """,
            (attempts, last_stage, last_error, error_class, now_iso, next_attempt_iso, filepath),
        )
        self._conn.commit()

    @_synchronized
    def queue_mark_dead(
        self,
        *,
        filepath: str,
        attempts: int,
        last_stage: str,
        last_error: str,
        error_class: str,
        now_iso: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE indexing_jobs
            SET attempts = ?, last_stage = ?, last_error = ?, last_error_class = ?,
                updated_at = ?, status = 'dead'
            WHERE filepath = ?
            """,
            (attempts, last_stage, last_error, error_class, now_iso, filepath),
        )
        self._conn.commit()

    @_synchronized
    def queue_requeue_dead(self, *, error_class: str | None, now_iso: str) -> int:
        """Reset dead rows (optionally only one ``last_error_class``) to
        a fresh, immediately-due ``queued`` state. Returns the row count."""
        cur = self._conn.execute(
            """
            UPDATE indexing_jobs
            SET status = 'queued', attempts = 0, last_error_class = NULL,
                updated_at = ?, next_attempt_at = ?
            WHERE status = 'dead' AND (? IS NULL OR last_error_class = ?)
            """,
            (now_iso, now_iso, error_class, error_class),
        )
        self._conn.commit()
        return cur.rowcount

    @_synchronized
    def record_ingestion_state(
        self,
        *,
        sync_completed_at: str | None,
        sync_interval_secs: int | None,
        seen_at: str,
    ) -> None:
        """Replace the single ``ingestion_state`` row."""
        self._conn.execute(
            """
            INSERT INTO ingestion_state
                (id, sync_completed_at, sync_interval_secs, indexer_seen_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                sync_completed_at = excluded.sync_completed_at,
                sync_interval_secs = excluded.sync_interval_secs,
                indexer_seen_at = excluded.indexer_seen_at
            """,
            (sync_completed_at, sync_interval_secs, seen_at),
        )
        self._conn.commit()

    @_synchronized
    def queue_stats(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM indexing_jobs GROUP BY status"
        ).fetchall()
        out = {"queued": 0, "dead": 0}
        for row in rows:
            out[row["status"]] = int(row["n"])
        return out

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
    def find_message_entry_by_filepath(self, filepath: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT message_id, thread_id, filepath FROM message_thread_map WHERE filepath = ?",
            (filepath,),
        ).fetchone()

    @_synchronized
    def get_message_sent_at(self, message_id: str) -> datetime | None:
        """The ``messages.sent_at`` already stored for ``message_id``, if any."""
        row = self._conn.execute(
            "SELECT sent_at FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return datetime.fromisoformat(row["sent_at"]) if row else None

    def keep_persisted_fallback_date(self, msg) -> None:
        """Give a fallback-dated ``msg`` the date first persisted for it.

        The parser dates a message with a missing or unparseable Date
        header at the current time, so every reprocess (a rename seen
        while the indexer was down, a retry, a reap rebuild) would
        otherwise re-date it (#297). A real header date is left alone.
        """
        if msg.date_is_fallback:
            msg.date = self.get_message_sent_at(msg.message_id) or msg.date

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

    def _write_message_record(self, cur: sqlite3.Cursor, msg, thread_id: str) -> None:
        """Upsert ``msg``'s ``messages`` row and replace its participants.

        Runs inside ``upsert_thread``'s transaction, after the
        ``message_thread_map`` row it references. ``ON CONFLICT DO
        UPDATE`` (never ``REPLACE``) keeps the row in place, so the
        participant cascade only fires when the message itself is removed.
        """
        cur.execute(
            """
            INSERT INTO messages
                (message_id, thread_id, filepath, folder, subject, sent_at,
                 in_reply_to, references_json, has_attachments, size_bytes,
                 content_hash, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                thread_id       = excluded.thread_id,
                filepath        = excluded.filepath,
                folder          = excluded.folder,
                subject         = excluded.subject,
                sent_at         = excluded.sent_at,
                in_reply_to     = excluded.in_reply_to,
                references_json = excluded.references_json,
                has_attachments = excluded.has_attachments,
                size_bytes      = excluded.size_bytes,
                content_hash    = excluded.content_hash,
                indexed_at      = excluded.indexed_at
            """,
            (
                msg.message_id,
                thread_id,
                msg.filepath,
                msg.folder,
                msg.subject,
                msg.date.isoformat(),
                msg.in_reply_to,
                json.dumps(msg.references),
                int(bool(msg.has_attachments)),
                msg.size,
                msg.content_hash,
                datetime.now(UTC).isoformat(),
            ),
        )
        cur.execute("DELETE FROM message_participants WHERE message_id = ?", (msg.message_id,))
        # ``from_addrs`` holds every author; ``from_addr`` alone for callers
        # that build a Message by hand.
        authors = msg.from_addrs or [msg.from_addr]
        roles = [("from", authors), ("to", msg.to_addrs), ("cc", msg.cc_addrs)]
        for role, values in roles:
            for value in values:
                address = canonical_addr(value or "")
                if not address:
                    continue
                name = parseaddr(value)[0].strip() or None
                cur.execute(
                    "INSERT OR IGNORE INTO message_participants (message_id, role, address, name) "
                    "VALUES (?, ?, ?, ?)",
                    (msg.message_id, role, address, name),
                )

    @_synchronized
    def update_filepath(
        self,
        old_path: str,
        new_path: str,
        *,
        folder: str | None = None,
        clear_tombstone: bool = False,
    ) -> None:
        """Update message_thread_map, indexed_files and the queue row after a Maildir rename.

        ``folder`` is the destination's folder when the rename crosses
        Maildir folders (``None`` for flag-only renames). It is written in
        the same transaction as the locator, so the per-message record can
        never point at one folder's file while claiming another.

        ``clear_tombstone`` drops the file's tombstone in the same
        transaction, for a rename that restores it (T flag cleared): the
        reaper runs on another thread, and seeing the restored path with
        its tombstone still in place it would delete the message.

        mbsync renames a Maildir file whenever flags change (e.g. S → SR when
        the message is replied to). Keep the stored path in sync so later
        reconciliation sweeps can still find the file.

        Goes through ``_begin_if_needed`` / ``_commit_if_started`` /
        ``_rollback_if_started`` like every other write helper so a
        future caller wrapping this in ``with db.transaction():`` does
        not crash on a nested ``BEGIN``.
        """
        if old_path == new_path:
            return
        cur = self._conn.cursor()
        started = False
        try:
            started = self._begin_if_needed(cur)
            cur.execute(
                "UPDATE message_thread_map SET filepath = ? WHERE filepath = ?",
                (new_path, old_path),
            )
            cur.execute(
                "UPDATE messages SET filepath = ? WHERE filepath = ?",
                (new_path, old_path),
            )
            if folder is not None:
                cur.execute(
                    "UPDATE messages SET folder = ? WHERE filepath = ?",
                    (folder, new_path),
                )
            # Carry the file-identity columns forward on rename. mbsync
            # renames files in place for flag changes; the content on
            # disk is unchanged, so reindexing just to recompute ``size``
            # / ``mtime_ns`` / ``content_hash`` would be wasted I/O.
            # Preserve whatever identity the previous indexing captured.
            prior = cur.execute(
                "SELECT size, mtime_ns, content_hash FROM indexed_files WHERE filepath = ?",
                (old_path,),
            ).fetchone()
            cur.execute("DELETE FROM indexed_files WHERE filepath = ?", (old_path,))
            cur.execute(
                "INSERT OR REPLACE INTO indexed_files "
                "(filepath, indexed_at, size, mtime_ns, content_hash) "
                "VALUES (?, datetime('now'), ?, ?, ?)",
                (
                    new_path,
                    prior["size"] if prior else None,
                    prior["mtime_ns"] if prior else None,
                    prior["content_hash"] if prior else None,
                ),
            )
            if clear_tombstone:
                cur.execute("DELETE FROM pending_deletions WHERE filepath = ?", (old_path,))
            else:
                cur.execute(
                    "UPDATE pending_deletions SET filepath = ? WHERE filepath = ?",
                    (new_path, old_path),
                )
            # The file's queue row moves too, retry or dead state intact:
            # a rename of a file whose Phase 1 committed (so the path is
            # indexed and ``on_moved`` does not re-enqueue it) but whose
            # Phase 2 is still pending would otherwise leave the job on a
            # path that no longer exists, to be dropped as missing.
            cur.execute(
                "UPDATE OR REPLACE indexing_jobs SET filepath = ? WHERE filepath = ?",
                (new_path, old_path),
            )
            # A job the drain parked because its file was T-flagged is due
            # at once on any rename: a restore must not wait out the park
            # delay (there may be no tombstone yet to clear), and a job
            # still on a trashed path is simply parked again.
            cur.execute(
                "UPDATE indexing_jobs SET next_attempt_at = ? "
                "WHERE filepath = ? AND status = 'queued' AND last_stage = 'trashed'",
                (datetime.now(UTC).isoformat(), new_path),
            )
            self._commit_if_started(started)
        except Exception:
            self._rollback_if_started(started)
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

        Refused when ``message_thread_map`` maps ``message_id`` to another
        path (#301): the caller's path is stale because the watcher renamed
        the file meanwhile (a restore, or a move to another folder). The
        reaper matches tombstones by message ID, and no sweep revisits the
        old path, so the tombstone would delete the live message. The check
        and the insert are one statement, so a rename cannot land between
        them.
        """
        cur = self._conn.cursor()
        marked_at = datetime.now(UTC).isoformat()
        cur.execute(
            "INSERT OR IGNORE INTO pending_deletions "
            "(filepath, message_id, thread_id, marked_at) "
            "SELECT ?, ?, ?, ? WHERE NOT EXISTS ("
            "SELECT 1 FROM message_thread_map WHERE message_id = ? AND filepath != ?)",
            (filepath, message_id, thread_id, marked_at, message_id, filepath),
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
    def list_pending_deletions_older_than(self, cutoff_iso: str) -> list[sqlite3.Row]:
        """Tombstones marked at or before ``cutoff_iso``, each with
        ``mapped_filepath``: the path ``message_thread_map`` holds for the
        message now (``None`` when the message is unmapped)."""
        return self._conn.execute(
            "SELECT p.filepath, p.message_id, p.thread_id, p.marked_at, "
            "m.filepath AS mapped_filepath "
            "FROM pending_deletions p "
            "LEFT JOIN message_thread_map m ON m.message_id = p.message_id "
            "WHERE p.marked_at <= ? ORDER BY p.marked_at ASC",
            (cutoff_iso,),
        ).fetchall()

    @_synchronized
    def delete_thread_completely(self, thread_id: str, *, grace_cutoff: str | None = None) -> bool:
        """Remove a thread and every derived row. Used when the last message
        in a thread has been reaped.

        Returns False, changing nothing, when a message in the thread is no
        longer tombstoned — the watcher restored it (or a new message
        joined the thread) after the reaper read its tombstones — or, with
        ``grace_cutoff``, has a tombstone newer than it (restored and
        trashed again, so its grace period restarted).
        """
        cur = self._conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            if self._has_untombstoned_messages(cur, thread_id, grace_cutoff=grace_cutoff):
                self._conn.rollback()
                return False
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
            self._delete_chunks_for_thread(cur, thread_id)
            # Walk every message in the thread to drop its attachments
            # rows + FTS shadows. ``message_id``-keyed deletes from
            # ``message_thread_map`` happen below; do attachments first
            # so the per-message lookup still finds rows.
            message_ids = [
                r["message_id"]
                for r in cur.execute(
                    "SELECT message_id FROM message_thread_map WHERE thread_id = ?",
                    (thread_id,),
                ).fetchall()
            ]
            for mid in message_ids:
                self._delete_attachments_for_message(cur, mid)
            cur.execute("DELETE FROM message_thread_map WHERE thread_id = ?", (thread_id,))
            cur.execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))
            cur.execute("DELETE FROM pending_deletions WHERE thread_id = ?", (thread_id,))
            for fp in filepaths:
                cur.execute("DELETE FROM indexed_files WHERE filepath = ?", (fp,))
                # A job still queued for the file (an embedder outage past
                # the grace window) would re-index it from the kept .eml.
                cur.execute("DELETE FROM indexing_jobs WHERE filepath = ?", (fp,))
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return True

    @staticmethod
    def _has_untombstoned_messages(
        cur: sqlite3.Cursor,
        thread_id: str,
        message_ids: list[str] | None = None,
        *,
        grace_cutoff: str | None = None,
    ) -> bool:
        """Whether a message of ``thread_id`` (only ``message_ids``, when
        given) has no tombstone, or none marked at or before
        ``grace_cutoff``. Read inside the reap transaction, so a restore
        (or a restore and a new tombstone) after the reaper's snapshot is
        seen."""
        # Only fixed SQL fragments are interpolated; values are bound.
        marked = " AND p.marked_at <= ?" if grace_cutoff is not None else ""
        sql = (
            "SELECT 1 FROM message_thread_map m WHERE m.thread_id = ? "  # nosec B608
            "AND NOT EXISTS (SELECT 1 FROM pending_deletions p "
            f"WHERE p.message_id = m.message_id{marked})"
        )
        params: list[str] = [thread_id]
        if grace_cutoff is not None:
            params.append(grace_cutoff)
        if message_ids is not None:
            sql += f" AND m.message_id IN ({','.join('?' * len(message_ids))})"  # nosec B608
            params.extend(message_ids)
        return cur.execute(sql + " LIMIT 1", params).fetchone() is not None

    @_synchronized
    def reap_thread_messages(
        self,
        thread,
        embedding: list[float],
        reaped_message_ids: list[str],
        *,
        grace_cutoff: str | None = None,
    ) -> list[str] | None:
        """Atomically rewrite a thread and remove reaped messages.

        All writes happen inside a single ``BEGIN IMMEDIATE`` / commit so
        either the whole reap lands or none of it does: a crash cannot
        leave the thread row reflecting only survivors while
        ``message_thread_map`` and ``pending_deletions`` still hold rows
        for the reaped messages.

        Returns the filepaths that were removed, so the caller can perform
        any on-disk unlink work outside the transaction, or ``None``,
        changing nothing, when a reaped message is no longer tombstoned
        (the watcher restored it after the reaper read its tombstones) or,
        with ``grace_cutoff``, its tombstone is newer than that.
        """
        cur = self._conn.cursor()
        removed_filepaths: list[str] = []
        try:
            cur.execute("BEGIN IMMEDIATE")
            if self._has_untombstoned_messages(
                cur, thread.thread_id, reaped_message_ids, grace_cutoff=grace_cutoff
            ):
                self._conn.rollback()
                return None
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

        Used by ``reap_thread_messages`` so the rewrite participates in
        its larger transaction. The caller owns ``BEGIN`` / ``COMMIT`` /
        ``ROLLBACK``.
        """
        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding has {len(embedding)} dims but threads_vec reserves "
                f"{EMBEDDING_DIM}. Check the embedder's output dimension."
            )
        # Storage invariant — see ``upsert_thread``. The reconciler reap
        # path passes ``mean_vector(survivor_chunks)``, which is
        # generally non-unit; normalize before the vec0 insert.
        embedding = l2_normalize(embedding)

        participants_json = json.dumps(_dedupe_by_canonical(thread.participants))
        senders_json = json.dumps(
            _dedupe_by_canonical([m.from_addr for m in thread.messages if m.from_addr])
        )
        message_ids_json = json.dumps([m.message_id for m in thread.messages])
        snippet = thread.snippet()
        date_first = thread.date_first.isoformat()
        date_last = thread.date_last.isoformat()
        has_attachments = int(any(m.has_attachments for m in thread.messages))
        body = thread.text_for_embedding()
        # display_subject: derive from the surviving messages — the
        # first original subject, in date order, that is non-empty, so a
        # blank oldest survivor does not drop the label.
        # Without this rewrite, reaping the original root of a thread
        # leaves the dead message's subject as the user-facing label —
        # search results would render with text from a message that no
        # longer exists in the index. None when no survivor has a
        # subject, or when the thread has no messages (the deletion-reconciler then drops the thread row
        # entirely a few lines below; the value never reaches storage).
        display_subject = next(
            (m.subject for m in sorted(thread.messages, key=lambda m: m.date) if m.subject),
            None,
        )

        cur.execute(
            """
            INSERT INTO threads
                (thread_id, subject, participants, senders, folder,
                 date_first, date_last, message_ids, snippet, has_attachments,
                 body_text, display_subject)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                subject         = excluded.subject,
                participants    = excluded.participants,
                senders         = excluded.senders,
                date_first      = excluded.date_first,
                date_last       = excluded.date_last,
                message_ids     = excluded.message_ids,
                snippet         = excluded.snippet,
                has_attachments = excluded.has_attachments,
                body_text       = excluded.body_text,
                display_subject = excluded.display_subject
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
                display_subject,
            ),
        )

        self._replace_fts_row(cur, thread.thread_id, thread.subject, participants_json, body)

        cur.execute("DELETE FROM threads_vec WHERE thread_id = ?", (thread.thread_id,))
        cur.execute(
            "INSERT INTO threads_vec (thread_id, embedding) VALUES (?, ?)",
            (thread.thread_id, sqlite_vec.serialize_float32(embedding)),
        )

    def _remove_message_row(self, cur: sqlite3.Cursor, message_id: str) -> str | None:
        """Remove a message's map / indexed_files / tombstone / chunk /
        attachment rows using ``cur``. Returns the message's filepath
        (for optional on-disk cleanup), or ``None`` if no such message
        was tracked. Used by ``reap_thread_messages``; the caller owns
        the enclosing transaction.
        """
        row = cur.execute(
            "SELECT filepath FROM message_thread_map WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            return None
        filepath = row["filepath"]
        # Per-message chunk cascade. ``_delete_chunks_for_message``
        # drops both body-chunk and attachment-chunk rows because both
        # carry this message_id; the deduped extraction cache stays.
        self._delete_chunks_for_message(cur, message_id)
        # Attachment occurrences for this message. Cached extractions
        # in ``attachment_extractions`` are deliberately kept — see
        # ``_delete_attachments_for_message``.
        self._delete_attachments_for_message(cur, message_id)
        cur.execute("DELETE FROM message_thread_map WHERE message_id = ?", (message_id,))
        cur.execute("DELETE FROM indexed_files WHERE filepath = ?", (filepath,))
        cur.execute("DELETE FROM pending_deletions WHERE filepath = ?", (filepath,))
        # A job still queued for the file would re-index it from the kept
        # .eml (see ``delete_thread_completely``).
        cur.execute("DELETE FROM indexing_jobs WHERE filepath = ?", (filepath,))
        return filepath
