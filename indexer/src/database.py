"""
SQLite database layer.
Uses FTS5 for keyword search and sqlite-vec for vector similarity search.
Thread-level indexing: one row per thread, updated as new messages arrive.
"""

import functools
import json
import logging
import os
import sqlite3
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

from .threader import THREAD_BODY_TEXT_MAX_CHARS, canonical_addr

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


# SCHEMA_VERSION jumped from v8 to v12 in one PR; the four logical steps
# that the bump represents (and that a future reader will see in the
# ``_apply_initial_schema`` body) are:
#   v9  — ``message_chunks`` / ``message_chunks_fts`` / ``message_chunks_vec``
#         tables: the precision-retrieval lane on top of thread-level rows.
#   v10 — ``attachments`` + ``attachments_fts``: per-message attachment
#         occurrences so filename / MIME filters work uniformly.
#   v11 — ``attachment_extractions`` cache: extracted *text* per
#         ``content_hash`` (never payload bytes) so OCR / PDF parse cost
#         runs at most once per unique payload.
#   v12 — FK ``ON DELETE CASCADE`` across chunk + attachment tables so
#         a thread or message deletion takes its dependent rows with it
#         without relying on application-side helpers.
#   v13 — ``threads.display_subject``: retrieval-facing original-cased
#         subject (with ``Re:``/``Fwd:`` prefixes intact). The existing
#         ``subject`` column stays as the normalized matching key used
#         by the threader for grouping. Existing threads receive
#         ``NULL`` for ``display_subject``; the retrieval layer
#         coalesces back to ``subject`` so old threads render with the
#         legacy normalized value until a future indexer pass refreshes
#         them.
# Bumping this constant requires shipping a forward migration file at
# ``src/migrations/<NNNN>_<slug>.sql`` covering the new version. Fresh
# installs continue to apply ``_apply_initial_schema`` directly and stamp
# the current version; existing installs run the migration runner to
# catch up. See ``src/migrations/runner.py`` for the file layout and
# transactional guarantees.
SCHEMA_VERSION = 14

# The schema uses FTS5 ``contentless_delete=1``, which SQLite added in 3.43.
# Validate the runtime version at Database init and fail fast with a clear
# message instead of degrading silently.
MIN_SQLITE_VERSION = (3, 43, 0)

# Vector dimension reserved by the ``*_vec`` schemas. Must match the
# active embedding model's output dimension or vec0 inserts fail.
# Qwen3-Embedding-8B (served by mlx-service) is 4096-dim. The legacy
# Ollama ``nomic-embed-text`` is 768-dim — running it against this
# schema requires switching ``USE_MLX_EMBEDDER=false`` AND restoring
# the previous dimension; the v13→v14 migration is one-way for the
# current MLX-default deployment.
EMBEDDING_DIM = 4096


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
        try:
            self.close()
        except Exception:
            pass

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
            cur.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            self._conn.commit()
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
        self._guard_destructive_migrations(stored, SCHEMA_VERSION)
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

    # Tables the v14 migration drops / recreates / clears. Each entry
    # contributes to the "populated v13?" decision below — if any of
    # them carries rows, the operator pays for losing it on upgrade,
    # not just ``message_chunks``. Both ``threads_vec`` and
    # ``message_chunks_vec`` are vec0 virtual tables; the same
    # ``SELECT COUNT(*)`` shape works against vec0 once sqlite-vec is
    # loaded (which the connection always does — see ``_connect``).
    _V14_DESTRUCTIVE_TABLES = (
        "message_chunks",
        "message_chunks_fts",
        "indexed_files",
        "indexing_jobs",
        "threads_vec",
        "message_chunks_vec",
    )

    def _guard_destructive_migrations(self, stored: int, target: int) -> None:
        """Refuse known-destructive migrations against populated databases
        unless the operator has explicitly opted in.

        v14 (768→4096-dim Qwen3 embeddings) is destructive — it drops
        and recreates the vector tables, and clears
        ``message_chunks`` / ``message_chunks_fts`` / ``indexed_files``
        / ``indexing_jobs`` so the next scan re-embeds. On a populated
        v13 install that means hours of indexing work + queue state +
        scan-tracking get reset; the operator must reindex from
        Maildir afterward. Worth a confirmation gate so a routine
        container restart doesn't silently kick off a full backfill.

        The gate counts every table the migration touches, not just
        ``message_chunks``. A v13 install that has indexed files, a
        non-empty queue, or thread vectors but zero chunks (e.g.
        every chunk write failed mid-pipeline; chunk rows manually
        truncated) would otherwise silently lose its scan / queue /
        vector state on upgrade.

        Set ``INDEXER_MIGRATION_V14_FORCE=true`` to acknowledge and
        proceed. Fresh databases (every checked table empty or
        absent) skip the gate — there's nothing to lose.
        """
        if not (stored < 14 <= target):
            return
        populated: dict[str, int] = {}
        for table in self._V14_DESTRUCTIVE_TABLES:
            # Table names come from the hardcoded ``_V14_DESTRUCTIVE_TABLES``
            # tuple above — never operator input — so the f-string
            # interpolation cannot be exploited as SQL injection.
            # SQLite does not parameterize table names, so string
            # formatting is the only option here.
            try:
                row = self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"  # nosec B608
                ).fetchone()
            except sqlite3.OperationalError:
                # Table doesn't exist in this older schema — counts as empty.
                continue
            n = row["n"] if row is not None else 0
            if n > 0:
                populated[table] = n
        if not populated:
            return
        force = os.environ.get("INDEXER_MIGRATION_V14_FORCE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        populated_summary = ", ".join(f"{t}={n}" for t, n in populated.items())
        if force:
            log.warning(
                "INDEXER_MIGRATION_V14_FORCE=true: applying destructive "
                "v14 migration over populated tables (%s). All chunks, "
                "vector data, file cache, and queue state will be "
                "cleared; the next scan will re-embed every message.",
                populated_summary,
            )
            return
        raise RuntimeError(
            f"Refusing destructive migration v14 — populated tables: "
            f"{populated_summary}. v14 resizes the embedding vector "
            "tables from 768-dim to 4096-dim (Qwen3-Embedding-8B) and "
            "clears message_chunks / message_chunks_fts / indexed_files "
            "/ indexing_jobs so the next scan re-embeds. Every existing "
            "row in those tables will be DROPPED, plus the existing "
            "threads_vec table itself. To proceed, set "
            "INDEXER_MIGRATION_V14_FORCE=true and restart. The next scan "
            "will re-chunk and re-embed every message — expect hours of "
            "work on a populated mailbox."
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
        deletion reconciler), and ``indexing_jobs`` (durable retry +
        dead-letter queue for the parse → embed → upsert pipeline).
        """
        cur.executescript(f"""
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
                display_subject TEXT                     -- original-cased subject for retrieval; NULL on legacy rows, COALESCE'd back to subject by readers
            );

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
                FOREIGN KEY (message_id) REFERENCES message_thread_map(message_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX idx_message_chunks_message ON message_chunks(message_id);
            CREATE INDEX idx_message_chunks_thread ON message_chunks(thread_id);
            CREATE INDEX idx_message_chunks_attachment ON message_chunks(attachment_id);

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
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                next_attempt_at TEXT NOT NULL
            );
            CREATE INDEX idx_indexing_jobs_status_next
                ON indexing_jobs(status, next_attempt_at);
        """)
        self._conn.commit()

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
                new_content = "\n".join(
                    f"From: {m.from_addr}\nDate: {m.date.isoformat()}\n{m.body_text[:2000]}"
                    for m in new_messages
                )
                return (existing["body_text"] + "\n" + new_content)[:THREAD_BODY_TEXT_MAX_CHARS]
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
                f"{EMBEDDING_DIM}. Check OLLAMA_EMBED_MODEL."
            )

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
                #   1. Existing display_subject is NULL (legacy v12 row,
                #      or first non-NULL writer hasn't arrived yet) →
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

        All inserts / deletes across ``message_chunks``,
        ``message_chunks_fts`` and ``message_chunks_vec`` happen inside
        one transaction so the three indexes never disagree about which
        chunks exist for a (message, slice) pair.
        """
        from datetime import UTC, datetime

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
                cur.execute(
                    "INSERT INTO message_chunks_fts (text) VALUES (?)",
                    (chunk.text,),
                )
                fts_rowid = cur.lastrowid
                cur.execute(
                    "INSERT INTO message_chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                    (chunk.chunk_id, sqlite_vec.serialize_float32(embedding)),
                )
                cur.execute(
                    """
                    INSERT INTO message_chunks
                        (chunk_id, message_id, thread_id, chunk_index, text,
                         char_start, char_end, token_est,
                         chunked_at, fts_rowid, attachment_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        from datetime import UTC, datetime

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
        from datetime import UTC, datetime

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
                f"{EMBEDDING_DIM}. Check OLLAMA_EMBED_MODEL."
            )
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
        import struct

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
    def get_thread_chunk_embeddings(self, thread_id: str) -> list[list[float]]:
        """Return every chunk embedding stored for ``thread_id``.

        The indexer averages these to produce the thread-level vector,
        so coarse thread retrieval and precise chunk retrieval both
        derive from the same per-chunk source data. Returns an empty
        list when the thread has no chunks yet (a thread whose only
        message had an empty body, or where every embed previously
        failed).
        """
        import struct

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
    def find_indexed_paths_by_content_hash(self, content_hash: str) -> list[str]:
        """Return every indexed filepath whose content_hash matches.

        Enables future reconciler passes to spot "file at path A
        disappeared, but the same content_hash is indexed at path B" —
        a rename mbsync performed without emitting an ``on_moved`` event
        (e.g. across folder moves or restarts) — and to catch genuine
        duplicate deliveries. Rows whose identity capture failed have
        ``content_hash IS NULL`` and are excluded from the match.
        """
        if not content_hash:
            return []
        rows = self._conn.execute(
            "SELECT filepath FROM indexed_files WHERE content_hash = ?",
            (content_hash,),
        ).fetchall()
        return [row["filepath"] for row in rows]

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
                 last_error, last_stage,
                 created_at, updated_at, next_attempt_at)
            VALUES (?, ?, ?, 0, NULL, NULL, ?, ?, ?)
            """,
            (filepath, reason, status, now_iso, now_iso, now_iso),
        )
        self._conn.commit()

    @_synchronized
    def queue_claim_next(self, status: str, now_iso: str) -> sqlite3.Row | None:
        """Return the oldest ``status`` row whose ``next_attempt_at`` is due."""
        return self._conn.execute(
            """
            SELECT filepath, reason, status, attempts,
                   last_error, last_stage,
                   created_at, updated_at, next_attempt_at
            FROM indexing_jobs
            WHERE status = ? AND next_attempt_at <= ?
            ORDER BY next_attempt_at ASC
            LIMIT 1
            """,
            (status, now_iso),
        ).fetchone()

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
    def queue_get_status(self, filepath: str) -> str | None:
        """Return ``"queued"`` / ``"dead"`` / ``None`` for ``filepath``.

        ``None`` when the row does not exist — the file has never been
        enqueued or has succeeded and been deleted. Callers (initial
        scan, reconciler) use this to decide whether to re-enqueue a
        path that's known to be dead-lettered: blindly re-enqueuing
        every dead row on every container restart turns one failed
        payload into a recurring retry storm against the same
        upstream (Ollama embed 500s on a poison-pill text), so the
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
        now_iso: str,
        next_attempt_iso: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE indexing_jobs
            SET attempts = ?, last_stage = ?, last_error = ?,
                updated_at = ?, next_attempt_at = ?, status = 'queued'
            WHERE filepath = ?
            """,
            (attempts, last_stage, last_error, now_iso, next_attempt_iso, filepath),
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
        now_iso: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE indexing_jobs
            SET attempts = ?, last_stage = ?, last_error = ?,
                updated_at = ?, status = 'dead'
            WHERE filepath = ?
            """,
            (attempts, last_stage, last_error, now_iso, filepath),
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

    def _remove_message_row(self, cur: sqlite3.Cursor, message_id: str) -> str | None:
        """Remove a message's map / indexed_files / tombstone / chunk /
        attachment rows using ``cur``. Returns the message's filepath
        (for optional on-disk cleanup), or ``None`` if no such message
        was tracked. Shared by ``remove_message`` and
        ``reap_thread_messages``; the caller owns the enclosing
        transaction.
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
        return filepath
