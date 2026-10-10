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
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from pathlib import Path

import sqlite_vec

from . import vector_sums
from .chunker import l2_normalize, truncate_to_tokens
from .entities import (
    PERSON_PREFIX,
    AuthorityRules,
    OperatorIdentity,
    identity_digest,
    org_entity_id,
    organization_domain,
    person_entity_id,
)
from .extractors import (
    LEGACY_OLE2_ERROR,
    NO_EXTRACTOR_ERROR,
    OCR_DISABLED_ERROR,
    SCANNED_PDF_OCR_DISABLED_ERROR,
    warn_rate_limited,
)
from .maildir import message_state
from .parser import PARSE_CAPS, participant_names
from .queue import REASON_REPARSE, REPARSE_ENQUEUE_SQL
from .threader import (
    FTS_SUBJECT_SCAN_CHARS,
    FTS_SUBJECT_SCAN_ROWS,
    PER_MESSAGE_BODY_CAP_CHARS,
    THREAD_BODY_TEXT_MAX_TOKENS,
    Thread,
    canonical_addr,
    fts_subject_text,
    message_date_line,
)

log = logging.getLogger("indexer.database")


def _caps_json(caps: dict[str, int]) -> str:
    """``caps`` as the ``messages.caps_json`` value: ``PARSE_CAPS`` names
    to positive counts, in ``PARSE_CAPS`` order (#1086).

    Raises ``ValueError`` with fixed text for any other name or value,
    so nothing but those names and integers can reach the column.
    """
    if any(name not in PARSE_CAPS for name in caps) or any(
        isinstance(count, bool) or not isinstance(count, int) or count < 1
        for count in caps.values()
    ):
        raise ValueError("parse caps must map PARSE_CAPS names to positive counts")
    return json.dumps({name: caps[name] for name in PARSE_CAPS if name in caps})


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
# numbering must be rebuilt from Maildir. Its version numbers will
# collide with the new sequence, so the initial schema also stamps
# ``SCHEMA_APPLICATION_ID`` into the SQLite header and a database
# without it is refused whatever its version.
#
# Bumping ``SCHEMA_VERSION`` requires shipping a forward migration file
# at ``src/migrations/<NNNN>_<slug>.sql`` covering the new version.
# Fresh installs apply ``_apply_initial_schema`` directly and stamp the
# current version; existing installs run the migration runner to catch
# up. See ``src/migrations/runner.py`` for the file layout and
# transactional guarantees. Before the first deployment, schema changes
# folded into ``_apply_initial_schema`` with no migration and no bump
# (owner, 2026-10-01). The first deployment was 2026-10-03, so every
# schema change now needs the bump and a migration.
#
# v1 (#928): ``attachment_extractions`` is keyed by (content hash,
# extractor module) and each ``attachments`` occurrence names the module
# whose row it uses (``migrations/0001_extraction_cache_per_module.sql``).
# v2 (#1144): ``messages.sender_ambiguous`` records whether the sender
# attribution is safe, NULL until a reparse assesses the message
# (``migrations/0002_messages_sender_ambiguous.sql``).
# v3 (#891): ``attachment_extractions.ocr_pages_skipped`` records the
# scanned PDF pages the OCR cap left unread, NULL (unknown) on rows
# cached before it (``migrations/0003_extraction_ocr_pages_skipped.sql``).
# v4 (#1140): ``message_participant_names`` keeps every distinct display
# name per (message, role, address), seeded from the stored first names
# and filled by a reparse (``migrations/0004_participant_names.sql``).
# v5 (#1086): per-message completeness on ``messages`` (subject, From /
# To / Cc addresses, attachment list, body) and the parse's cap counts,
# NULL until a reparse assesses the message
# (``migrations/0005_message_completeness.sql``).
# v6 (#1242): per-occurrence attachment text completeness
# (``attachments.text_complete`` and the ``text_extractor`` stamp that
# applied) and the cached result's (``attachment_extractions.text_complete``),
# NULL until assessed (``migrations/0006_attachment_text_complete.sql``).
# v7 (#824): ``operator_addresses`` and ``operator_identity`` hold the
# operator's own addresses from ``config/identity.toml``, its loaded
# state, address count and digest, replaced at every indexer start; no
# per-message column and no reparse
# (``migrations/0007_operator_identity.sql``).
# v8 (#1080): ``messages.sent_at`` is NULL for a missing or unparseable
# Date header, with ``sent_at_status`` (``parsed`` / ``missing`` /
# ``invalid``, NULL until a reparse assesses the row), and
# ``first_indexed_at`` is the last fallback of ``effective_at``; the
# table is rebuilt (``migrations/0008_unknown_sent_dates.sql``).
# v9 (#1236): ``attachments.extraction_deferred_at`` marks an occurrence
# whose extraction the per-message budget deferred to a later pass, NULL
# otherwise; no reparse (``migrations/0009_attachment_extraction_deferral.sql``).
# v10 (#1356): ``thread_vector_sums`` keeps each thread's exact running
# sum of its chunk vectors and their count (``vector_sums``), so the
# thread vector is derived without reading every chunk vector; no
# reparse, the rows are filled inline and by a backfill sweep
# (``migrations/0010_thread_vector_sums.sql``).
SCHEMA_VERSION = 10
SCHEMA_APPLICATION_ID = 0x504D4149  # "PMAI"

# How long a ``reaped_messages`` record outlives the reap. The record
# holds identifiers derived from the sender's Message-ID, kept only so a
# citation from a recent answer resolves to "reaped" instead
# of "not found"; after this window the lookup reads as not found.
REAPED_RECORD_RETENTION_DAYS = 30

# Drop each cached extraction of a payload that no ``attachments`` row
# uses any more (#562): a row is used by the occurrences of its content
# hash whose ``extractor_module`` names it (#928). Both lookups use an
# index (the extraction primary key and ``idx_attachments_attachment_id``),
# so a removal costs one indexed statement per payload it carried,
# whatever the table sizes.
_PURGE_ORPHAN_EXTRACTION_SQL = (
    "DELETE FROM attachment_extractions WHERE attachment_id = ? "
    "AND NOT EXISTS (SELECT 1 FROM attachments a "
    "WHERE a.attachment_id = attachment_extractions.attachment_id "
    "AND a.extractor_module = attachment_extractions.extractor_module)"
)

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

# Rows the startup extraction sweep reads at a time from each of its
# per-occurrence queries (#1289), so its memory follows the messages it
# re-queues, not the attachment occurrences it inspects.
SWEEP_FETCH_ROWS = 1000

# Participants per message that get entity and alias writes (From first,
# then To, then Cc). Later participants still get ``message_participants``
# rows, just no new entities, so one crafted message cannot drive an
# unbounded number of entity writes.
MAX_ENTITY_PARTICIPANTS_PER_MESSAGE = 200


class _SumDeltas(dict[str, tuple[list[int], int]]):
    """Per thread, the change one write makes to its chunk-vector sum and
    count (#1356): ``add`` each stored vector the write inserts (sign
    +1) or deletes (sign -1), then ``Database._apply_thread_sum_deltas``."""

    def add(self, thread_id: str, blob: bytes, sign: int) -> None:
        units = vector_sums.vector_units(blob)
        delta, count = self.get(thread_id, ([0] * len(units), 0))
        self[thread_id] = (vector_sums.add_units(delta, units, sign), count + sign)


class CompletenessClearing:
    """Clears ``attachments.text_complete`` on the occurrences the startup
    sweep finds due a refresh, as it finds them (#1236): at most
    ``SWEEP_FETCH_ROWS`` IDs are held, and each full batch is cleared in
    its own transaction, so memory stays bounded whatever the number of
    occurrences. ``cleared`` counts the rows changed. An ID seen twice
    (two refresh classes) changes nothing the second time."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._pending: list[str] = []
        self.cleared = 0

    def add(self, occurrence_id: str) -> None:
        self._pending.append(occurrence_id)
        if len(self._pending) >= SWEEP_FETCH_ROWS:
            self.flush()

    def flush(self) -> None:
        if self._pending:
            batch, self._pending = self._pending, []
            self.cleared += self._db._clear_text_complete_batch(batch)


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


# Every FTS5 table in the schema, in the order ``scrub_reaped_fts``
# visits them.
_FTS_TABLES = ("threads_fts", "message_chunks_fts", "attachments_fts")

# One ``scrub_reaped_fts`` step: FTS5 ``merge`` with this page budget,
# committed on its own so the write lock is free between steps (#670).
# Measured on a 20,000-thread synthetic table (plain timing, SQLite
# 3.46.1 and 3.53.4): 9 steps of at most 0.2 s and a 17 MB WAL peak,
# against 1.2 s and 69 MB for one ``optimize``.
_FTS_SCRUB_STEP_PAGES = 2000

# Steps one pass may spend on one table before leaving it pending for
# the next pass. 1,000 steps of 2,000 pages is far beyond any index this
# project expects; the cap only keeps a pass from running unbounded.
_FTS_SCRUB_MAX_STEPS = 1000


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
        # Operator source-authority rules; empty until ``set_authority_rules``.
        self._authority_rules = AuthorityRules()
        # FTS5 tables a reap deleted from since their last
        # ``scrub_reaped_fts`` (#641, #670). In memory only, so every
        # table starts pending: the first pass after the database is
        # opened also covers a reap whose scrub a restart cut short.
        self._fts_pending_scrub: set[str] = set(_FTS_TABLES)
        # Pending tables whose merge a capped pass started: the next
        # step continues it with a positive budget (#670).
        self._fts_scrub_started: set[str] = set()
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
        # Overwrite deleted content with zeros, so a reaped message's
        # text does not stay in freed pages of mail.db (#602). Set
        # explicitly because the default depends on how SQLite was
        # compiled (Debian's build defaults ON, many others OFF). Per
        # connection; ON rather than FAST, which leaves freed overflow
        # pages (long bodies and extractions) unzeroed. FTS5 index
        # terms are removed by ``scrub_reaped_fts`` (#641); WAL
        # frames are not covered; see docs/architecture.md.
        conn.execute("PRAGMA secure_delete = ON")
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

    def scrub_reaped_fts(self) -> list[str]:
        """Merge each FTS5 table a reap deleted from into one segment,
        and return the tables finished.

        The FTS5 tables are ``contentless_delete=1``: a ``DELETE``
        records a tombstone and leaves the row's index terms (its words
        and their positions) in live ``*_fts_data`` segment pages, which
        ``secure_delete`` never frees and FTS5's ``secure-delete``
        option does not cover for this table type. Merging every
        segment into one drops the deleted terms (#641), those of
        earlier deletes included. Only a reap marks a table: a re-index
        replaces live mail with a newer version of itself, and FTS5's
        automerge keeps its superseded rows in check (#670).

        The merge runs in steps, the incremental form of ``optimize``
        the FTS5 documentation gives: the first ``'merge'`` with a
        negative page budget starts it, and each later one continues it
        with a positive budget, so a segment indexing writes between
        steps cannot restart it. Each step is committed under the lock on
        its own, so a writer waits for one step at most. A table is
        finished when a step changes fewer than two rows. One not
        finished within ``_FTS_SCRUB_MAX_STEPS`` stays pending and
        continues its merge on the next pass; one whose step fails stays
        pending, and the failure propagates to the caller.

        Only a reap marks a table, and a message's chunk replacement,
        which happens only when the extractor or chunker output changes
        (the replaced text may be what a fix removed).
        """
        done: list[str] = []
        for table in _FTS_TABLES:
            if table not in self._fts_pending_scrub:
                continue
            for _ in range(_FTS_SCRUB_MAX_STEPS):
                with self._lock:
                    started = table in self._fts_scrub_started
                    budget = _FTS_SCRUB_STEP_PAGES if started else -_FTS_SCRUB_STEP_PAGES
                    before = self._conn.total_changes
                    try:
                        self._conn.execute(
                            f"INSERT INTO {table}({table}, rank) VALUES('merge', ?)",  # nosec B608 - table from the fixed _FTS_TABLES tuple
                            (budget,),
                        )
                        self._conn.commit()
                    except Exception:
                        self._conn.rollback()
                        raise
                    self._fts_scrub_started.add(table)
                    finished = self._conn.total_changes - before < 2
                    if finished:
                        self._fts_pending_scrub.discard(table)
                        self._fts_scrub_started.discard(table)
                if finished:
                    done.append(table)
                    break
        return done

    def _mark_fts_scrub(self, table: str) -> None:
        """Mark ``table`` for ``scrub_reaped_fts``. A merge already under
        way may not cover the segment this delete touched (one written
        after it began), so the next step starts a new one."""
        self._fts_pending_scrub.add(table)
        self._fts_scrub_started.discard(table)

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

        if cur.execute("PRAGMA application_id").fetchone()[0] != SCHEMA_APPLICATION_ID:
            raise RuntimeError(
                "Database predates the v0 schema renumbering and cannot be "
                "migrated. Stop the stack, wipe the sqlite-volume and let the "
                "indexer rebuild the index from Maildir."
            )

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
          captures filename/MIME and the extractor module whose cached
          row it uses), ``attachments_fts`` (filename + MIME search), and
          ``attachment_extractions`` (a cache keyed by content hash and
          extractor module, so OCR / PDF parse cost runs at most once per
          unique payload and extractor regardless of forwarding count; a
          row is deleted with the last occurrence that uses it).

        Plus the cross-cutting tables: ``message_thread_map`` (message
        → thread index), ``indexed_files`` (file identity for rename
        detection), ``pending_deletions`` (tombstones for the
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
            cur.execute(f"PRAGMA application_id = {SCHEMA_APPLICATION_ID}")
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
                message_ids     TEXT NOT NULL,           -- JSON array of claimant IDs
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

            -- The exact running sum of the thread's chunk vectors and
            -- their count (#1356, ``vector_sums``): ``sum`` is in units of
            -- 2**-149 in the ``encoding_version`` encoding. Missing for a
            -- thread not yet filled (the first write that touches it, or
            -- the backfill sweep, fills it).
            CREATE TABLE thread_vector_sums (
                thread_id        TEXT PRIMARY KEY,
                count            INTEGER NOT NULL CHECK (count >= 0),
                encoding_version INTEGER NOT NULL,
                sum              BLOB NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE
            );

            -- Per-message chunks (precision retrieval)
            CREATE TABLE message_chunks (
                chunk_id        TEXT PRIMARY KEY,
                claimant_id     TEXT NOT NULL,           -- the message's claimant ID (parser.claimant_id)
                thread_id       TEXT NOT NULL,
                chunk_index     INTEGER NOT NULL,
                text            TEXT NOT NULL,
                char_start      INTEGER NOT NULL,
                char_end        INTEGER NOT NULL,
                token_est       INTEGER NOT NULL,
                chunked_at      TEXT NOT NULL,
                fts_rowid       INTEGER,
                attachment_id   TEXT,
                -- What the chunk's text is (chunker.CHUNK_KINDS, #646);
                -- ``attachment`` exactly when attachment_id is set.
                kind            TEXT NOT NULL CHECK (kind IN (
                                    'body', 'quote', 'signature', 'forwarded',
                                    'calendar', 'attachment')),
                FOREIGN KEY (claimant_id) REFERENCES message_thread_map(claimant_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX idx_message_chunks_claimant ON message_chunks(claimant_id);
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
                claimant_id               TEXT NOT NULL,
                attachment_id             TEXT NOT NULL,
                thread_id                 TEXT NOT NULL,
                filename                  TEXT NOT NULL,
                content_type              TEXT NOT NULL,
                size_bytes                INTEGER NOT NULL,
                seen_at                   TEXT NOT NULL,
                fts_rowid                 INTEGER,
                -- The extractor module this occurrence's MIME type and
                -- filename run on its bytes ('' for none): with ``attachment_id``,
                -- the key of the ``attachment_extractions`` row it uses
                -- (#928).
                extractor_module          TEXT NOT NULL DEFAULT '',
                -- Whether this occurrence's committed attachment chunks
                -- hold all its text (#1242): 1 complete, 0 a known loss
                -- or a status that never certifies absence, NULL not
                -- assessed. Written with the chunks in phase 2c, with
                -- ``text_extractor``, the extractor stamp of the result
                -- that applied (NULL when none ran); an
                -- ``EXTRACTOR_VERSIONS`` bump clears it at startup.
                text_complete             INTEGER CHECK (text_complete IN (0, 1)),
                text_extractor            TEXT,
                -- When the per-message extraction budget deferred this
                -- occurrence's extraction to a later pass of its message
                -- (#1236), NULL otherwise. A deferred occurrence has
                -- ``text_complete`` 0, keeps the chunks it had and has
                -- no ``attachment_extractions`` row written for it;
                -- the pass that extracts it clears the mark.
                extraction_deferred_at    TEXT,
                FOREIGN KEY (claimant_id) REFERENCES message_thread_map(claimant_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX idx_attachments_attachment_id ON attachments(attachment_id);
            CREATE INDEX idx_attachments_thread ON attachments(thread_id);
            CREATE INDEX idx_attachments_claimant ON attachments(claimant_id);
            -- Hybrid-search attachment lane joins ``attachments`` back from
            -- ``attachments_fts`` on ``fts_rowid``.
            CREATE INDEX idx_attachments_fts_rowid ON attachments(fts_rowid);
            -- A chunk's payload has a deferred copy in its message (#1236):
            -- read once per chunk by the MCP server's evidence lanes, so
            -- partial and composite, and small (deferred rows only).
            CREATE INDEX idx_attachments_deferred ON attachments(claimant_id, attachment_id)
                WHERE extraction_deferred_at IS NOT NULL;

            -- One row per payload and extractor module (#928): the same
            -- bytes under labels that pick different extractors get
            -- separate results. ``extractor_module`` is '' for an
            -- occurrence that selects no extractor.
            -- ``ocr_pages_skipped`` is the scanned PDF pages the OCR page
            -- cap left unread (#891): 0 when none, NULL when unknown (a
            -- non-PDF result, or a row cached before schema v3).
            -- ``text_complete`` is whether the result lost no text
            -- (#1242): 0 / 1, NULL when unknown (a row cached before
            -- schema v6).
            CREATE TABLE attachment_extractions (
                attachment_id      TEXT NOT NULL,
                extractor_module   TEXT NOT NULL,
                extraction_status  TEXT NOT NULL,
                extractor          TEXT,
                extracted_text     TEXT,
                extraction_error   TEXT,
                extracted_at       TEXT NOT NULL,
                ocr_pages_skipped  INTEGER CHECK (ocr_pages_skipped >= 0),
                text_complete      INTEGER CHECK (text_complete IN (0, 1)),
                PRIMARY KEY (attachment_id, extractor_module)
            );

            CREATE VIRTUAL TABLE attachments_fts USING fts5(
                filename,
                content_type,
                content='',
                contentless_delete=1,
                tokenize='porter unicode61'
            );

            -- Cross-cutting tables
            --
            -- Every per-message row is keyed by ``claimant_id``: the
            -- Message-ID plus a short hash of the file's bytes
            -- (``parser.claimant_id``). A Message-ID is sender-controlled,
            -- so two different files can claim one; both are kept, under
            -- distinct claimant IDs, and neither's rows can overwrite or
            -- delete the other's (#217). ``message_id`` stays alongside
            -- as the bare Message-ID: threading resolves In-Reply-To and
            -- References through it, and readers look a message up by it.
            CREATE TABLE message_thread_map (
                claimant_id TEXT PRIMARY KEY,
                message_id  TEXT NOT NULL,
                thread_id   TEXT NOT NULL,
                filepath    TEXT NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES threads(thread_id)
            );
            CREATE INDEX idx_message_thread_map_message ON message_thread_map(message_id);
            -- Flag renames match rows by filepath; thread rebuilds and
            -- removals match them by thread_id.
            CREATE INDEX idx_message_thread_map_filepath ON message_thread_map(filepath);
            CREATE INDEX idx_message_thread_map_thread ON message_thread_map(thread_id);

            -- One row per indexed message: the authoritative per-message
            -- record (own headers, send and delivery time, folder, source
            -- locator and content hash) behind exact enumeration and
            -- provenance. Cascades from ``message_thread_map`` so every
            -- existing message / thread removal path cleans it up.
            -- ``sent_at`` is the parsed ``Date:`` header, NULL when it is
            -- absent or unparseable, with ``sent_at_status`` saying which
            -- (#1080; NULL status = not yet assessed, a row from before
            -- v8 the reparse has not reached, whose ``sent_at`` may be a
            -- made-up fallback). ``occurred_at`` is the date of the
            -- topmost ``Received:`` header, NULL when absent or
            -- unparseable. ``first_indexed_at`` is when the indexer first
            -- stored the message, kept across reprocessing.
            -- ``effective_at`` is the message's effective time, which
            -- thread spans and time ordering use; its last fallback,
            -- ``first_indexed_at``, is an ordering position, not
            -- evidence of a date (docs/architecture.md "Message time").
            CREATE TABLE messages (
                claimant_id     TEXT PRIMARY KEY,
                message_id      TEXT NOT NULL,
                thread_id       TEXT NOT NULL,
                filepath        TEXT NOT NULL,
                folder          TEXT NOT NULL,
                subject         TEXT NOT NULL,
                sent_at         TEXT,
                sent_at_status  TEXT CHECK (sent_at_status IN ('parsed', 'missing', 'invalid')),
                occurred_at     TEXT,
                effective_at    TEXT GENERATED ALWAYS AS
                                (COALESCE(occurred_at, sent_at, first_indexed_at)) VIRTUAL,
                in_reply_to     TEXT,
                references_json TEXT NOT NULL,
                has_attachments INTEGER NOT NULL,
                size_bytes      INTEGER,
                content_hash    TEXT,
                indexed_at      TEXT NOT NULL,
                first_indexed_at TEXT NOT NULL,
                -- Maildir S / F / R flags of ``filepath`` (maildir.message_state),
                -- written with it on every insert and rename.
                seen            INTEGER NOT NULL DEFAULT 0,
                flagged         INTEGER NOT NULL DEFAULT 0,
                replied         INTEGER NOT NULL DEFAULT 0,
                -- #1144: 0 = one From header; 1 = sender attribution unsafe
                -- (a repeated From, or the header scan stopped before a
                -- second could be ruled out); NULL = not yet assessed (a
                -- row from before v2 the reparse has not reached). No
                -- default; mcp-server reads NULL as "can't tell".
                sender_ambiguous INTEGER CHECK (sender_ambiguous IN (0, 1)),
                -- 1 when the parser's display-name stage stored every
                -- distinct name in ``message_participant_names``, 0 when
                -- its per-message budget dropped one (#1140); NULL = not
                -- yet known (a row from before v4 the reparse has not
                -- reached). It covers that stage only: it does not
                -- certify that the participant rows or addresses are
                -- complete, which #1086's per-role term needs its own
                -- column or backfill design for. No default; mcp-server
                -- reads NULL as "can't tell".
                participant_names_complete INTEGER
                    CHECK (participant_names_complete IN (0, 1)),
                -- #1086: whether the stored content a filter reads is
                -- complete. 1 = complete within the documented indexing
                -- semantics, 0 = a parse cap or a rejected element lost
                -- part of it, NULL = not assessed (a row from before v5
                -- the reparse has not reached). Written by
                -- ``_write_message_record`` from the parse, except
                -- ``body_complete``, which that write resets to NULL and
                -- phase 2c sets in the transaction that commits the body
                -- chunks (``set_body_complete``). No defaults; mcp-server
                -- reads NULL as "can't tell".
                subject_complete INTEGER CHECK (subject_complete IN (0, 1)),
                from_addresses_complete INTEGER CHECK (from_addresses_complete IN (0, 1)),
                to_addresses_complete INTEGER CHECK (to_addresses_complete IN (0, 1)),
                cc_addresses_complete INTEGER CHECK (cc_addresses_complete IN (0, 1)),
                attachments_manifest_complete INTEGER
                    CHECK (attachments_manifest_complete IN (0, 1)),
                body_complete INTEGER CHECK (body_complete IN (0, 1)),
                -- The parse's nonzero ``PARSE_CAPS`` counts as a JSON
                -- object of fixed names to integers, never content;
                -- NULL before v5's reparse.
                caps_json TEXT,
                -- A send date is stored exactly when it was parsed, and a
                -- row not yet assessed keeps the one it had before v8.
                CHECK ((sent_at_status = 'parsed') = (sent_at IS NOT NULL)),
                CHECK (sent_at_status IS NOT NULL OR sent_at IS NOT NULL),
                FOREIGN KEY (claimant_id) REFERENCES message_thread_map(claimant_id)
                    ON DELETE CASCADE
            );
            -- ``get_message`` lists the claimants of one Message-ID in
            -- claimant-ID order and oldest first; each index matches one
            -- order so ``LIMIT`` stops the walk instead of every file
            -- claiming the Message-ID being read and sorted (#538).
            CREATE INDEX idx_messages_message ON messages(message_id, claimant_id);
            CREATE INDEX idx_messages_message_effective
                ON messages(message_id, effective_at, claimant_id);
            CREATE INDEX idx_messages_thread_effective ON messages(thread_id, effective_at);
            CREATE INDEX idx_messages_folder_effective ON messages(folder, effective_at);
            CREATE INDEX idx_messages_effective ON messages(effective_at);
            -- Flag renames and cross-folder moves update by filepath.
            CREATE INDEX idx_messages_filepath ON messages(filepath);

            -- Normalized From / To / Cc. ``address`` is the canonical
            -- lowercased bare address, so "every message from/to X" is an
            -- indexed lookup rather than a scan of JSON participant lists.
            CREATE TABLE message_participants (
                claimant_id TEXT NOT NULL,
                role        TEXT NOT NULL CHECK (role IN ('from', 'to', 'cc')),
                address     TEXT NOT NULL,
                name        TEXT,
                PRIMARY KEY (claimant_id, role, address),
                FOREIGN KEY (claimant_id) REFERENCES messages(claimant_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX idx_message_participants_address
                ON message_participants(address, role);
            -- Every distinct display name a participant was written
            -- with in one message (#1140); ``message_participants.name``
            -- keeps the first. Name matching, find_contact and entity
            -- aliases read these. Bounded per message by the parser's
            -- ``MAX_EXTRA_PARTICIPANT_*`` budget.
            CREATE TABLE message_participant_names (
                claimant_id TEXT NOT NULL,
                role        TEXT NOT NULL,
                address     TEXT NOT NULL,
                name        TEXT NOT NULL,
                PRIMARY KEY (claimant_id, role, address, name),
                FOREIGN KEY (claimant_id, role, address)
                    REFERENCES message_participants(claimant_id, role, address)
                    ON DELETE CASCADE
            );
            -- The reap's alias prune asks whether any surviving row
            -- still carries an (address, display name) pair (#464).
            CREATE INDEX idx_message_participant_names_address_name
                ON message_participant_names(address, name);

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
                filepath    TEXT PRIMARY KEY,
                claimant_id TEXT NOT NULL,
                thread_id   TEXT NOT NULL,
                marked_at   TEXT NOT NULL
            );
            CREATE INDEX idx_pending_deletions_thread
                ON pending_deletions(thread_id);

            -- One row per message the reconciler reaped, written in the
            -- reap transaction, so mcp-server can answer a lookup of a
            -- cited claimant ID or thread ID with "reaped"
            -- instead of "not found" (PLAN Phase 4 item 4). Identifiers
            -- and the reap time only, never content; pruned after
            -- ``REAPED_RECORD_RETENTION_DAYS`` (``prune_reaped_messages``).
            CREATE TABLE reaped_messages (
                claimant_id TEXT PRIMARY KEY,
                message_id  TEXT NOT NULL,
                thread_id   TEXT NOT NULL,
                reaped_at   TEXT NOT NULL
            );
            CREATE INDEX idx_reaped_messages_message ON reaped_messages(message_id, reaped_at);
            CREATE INDEX idx_reaped_messages_thread
                ON reaped_messages(thread_id, reaped_at, claimant_id);
            CREATE INDEX idx_reaped_messages_reaped_at ON reaped_messages(reaped_at);

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
        self._run_entity_schema_script(cur)
        self._run_vector_generation_schema_script(cur)
        self._run_operator_identity_schema_script(cur)

    @staticmethod
    def _run_entity_schema_script(cur: sqlite3.Cursor) -> None:
        """Deterministic entities (PLAN Phase 4), inside the initial
        schema's open transaction (``execute`` per statement, so nothing
        commits early).

        ``entities`` holds one person per canonical address
        (``person:<address>``) and one organization per non-free-mail
        sender domain (``org:<domain>``); see ``src/entities.py``.
        ``entity_aliases`` records every display name seen for a person.
        ``authority_class`` / ``authority_rule`` are the operator rules
        file's class for the entity and the rule that matched it
        (``unclassified`` / NULL when none did); metadata only, never a
        ranking weight. Rows are written alongside ``message_participants``
        and pruned with them: when a reap removes the last participant row
        for an address or display name, the entity or alias goes in the
        same transaction (``_prune_orphan_entities``, #464).
        """
        for statement in (
            """
            CREATE TABLE entities (
                entity_id       TEXT PRIMARY KEY,
                kind            TEXT NOT NULL CHECK (kind IN ('person', 'organization')),
                canonical_key   TEXT NOT NULL,
                organization_id TEXT REFERENCES entities(entity_id),
                authority_class TEXT NOT NULL DEFAULT 'unclassified',
                authority_rule  TEXT
            )
            """,
            "CREATE INDEX idx_entities_organization ON entities(organization_id)",
            "CREATE INDEX idx_entities_authority ON entities(authority_class)",
            """
            CREATE TABLE entity_aliases (
                entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
                alias     TEXT NOT NULL,
                PRIMARY KEY (entity_id, alias)
            )
            """,
        ):
            cur.execute(statement)

    @staticmethod
    def _run_vector_generation_schema_script(cur: sqlite3.Cursor) -> None:
        """The embedder identity record (PLAN Phase 2 item 1, first slice;
        see ``src/embed_identity.py``), inside the initial schema's open
        transaction.

        One row per embedding generation. Today exactly one exists, status
        ``active``, written on a fresh index. ``endpoint`` is the SDK's
        resolved base URL without userinfo, query or fragment;
        ``calibration_vector`` is the float32 embedding of the fixed
        calibration text whose SHA-256 is ``calibration_sha256``.
        ``revision``, ``tokenizer``, ``context_window``,
        ``chunk_config_hash`` and ``label`` are reserved for the
        generation lifecycle and stay NULL: the OpenAI-compatible
        embeddings API exposes none of them.
        """
        for statement in (
            """
            CREATE TABLE vector_generations (
                generation_id      INTEGER PRIMARY KEY,
                provider           TEXT NOT NULL,
                endpoint           TEXT NOT NULL,
                model              TEXT NOT NULL,
                revision           TEXT,
                dimensions         INTEGER NOT NULL,
                tokenizer          TEXT,
                context_window     INTEGER,
                chunk_config_hash  TEXT,
                label              TEXT,
                calibration_sha256 TEXT NOT NULL,
                calibration_vector BLOB NOT NULL,
                created_at         TEXT NOT NULL,
                status             TEXT NOT NULL CHECK (status IN
                    ('building', 'caught-up', 'active', 'retained', 'retired'))
            )
            """,
            "CREATE UNIQUE INDEX idx_vector_generations_active "
            "ON vector_generations(status) WHERE status = 'active'",
        ):
            cur.execute(statement)

    @staticmethod
    def _run_operator_identity_schema_script(cur: sqlite3.Cursor) -> None:
        """The operator's own addresses (#824), inside the initial
        schema's open transaction; ``migrations/0007_operator_identity.sql``
        creates the same tables on an existing index.

        ``operator_addresses`` holds the canonical addresses listed in
        ``config/identity.toml``. ``operator_identity`` is one row: the
        loaded ``state`` (``configured``, or ``unconfigured`` when the
        file is absent), the address count and ``identity_digest`` of
        the set (``src/entities.py``). ``set_operator_identity`` replaces
        both at every indexer start; until the first one the row reads
        unconfigured.
        """
        for statement in (
            "CREATE TABLE operator_addresses (address TEXT PRIMARY KEY) WITHOUT ROWID",
            """
            CREATE TABLE operator_identity (
                id             INTEGER PRIMARY KEY CHECK (id = 1),
                state          TEXT NOT NULL CHECK (state IN ('configured', 'unconfigured')),
                address_count  INTEGER NOT NULL,
                address_digest TEXT NOT NULL
            )
            """,
        ):
            cur.execute(statement)
        cur.execute(
            "INSERT INTO operator_identity (id, state, address_count, address_digest) "
            "VALUES (1, 'unconfigured', 0, ?)",
            (identity_digest(frozenset()),),
        )

    # -------------------------------------------------------------------------
    # Write operations
    # -------------------------------------------------------------------------

    @staticmethod
    def _compute_body(thread, existing) -> str:
        """Pure function: body_text given the incoming thread and existing row.

        On insert, ``build_body_text()`` already sees all messages. On
        update, ``thread.messages`` only holds the newly-arrived message,
        so append its content to the stored ``body_text`` rather than
        regenerating from scratch.
        """
        if existing and existing["body_text"]:
            # ``message_ids`` lists claimant IDs, so a second claimant of
            # a Message-ID already in the thread is new content (#217).
            existing_claimant_ids = set(json.loads(existing["message_ids"]))
            new_messages = [
                m for m in thread.messages if m.claimant_id not in existing_claimant_ids
            ]
            if new_messages:
                # Per-message char cap shared with ``Thread.build_body_text``
                # so a thread that arrived as one message gets the same FTS
                # body coverage as a thread that arrived as a sequence of
                # replies.
                new_content = "\n".join(
                    f"From: {m.from_addr}\n{message_date_line(m)}"
                    f"{m.body_text[:PER_MESSAGE_BODY_CAP_CHARS]}"
                    for m in new_messages
                )
                return truncate_to_tokens(
                    existing["body_text"] + "\n" + new_content,
                    THREAD_BODY_TEXT_MAX_TOKENS,
                )
            return existing["body_text"]
        return thread.build_body_text()

    @staticmethod
    def _drop_fallback_date_lines(cur: sqlite3.Cursor, thread, body: str) -> str:
        """``body`` without the made-up ``Date:`` line the v7 writer put in
        it for an undated incoming message (#1080, review rounds 1-2).

        Only a row not yet assessed (``sent_at_status`` NULL, from before
        v8) whose message now parses as undated has one: its stored
        ``sent_at`` is that fallback. The writer put it in the header it
        starts each message's block with, ``From: <from_addr>`` then
        ``Date: <sent_at>`` on lines of their own, so that pair is what is
        removed (the ``Date:`` line only), and only when it occurs exactly
        once: message text is sender-controlled, and a body that repeats
        the pair leaves the match ambiguous, so the text is kept and the
        skip logged at WARNING, rate limited (counts only). Runs before the message's row is
        rewritten; one indexed lookup and one scan per undated message.
        """
        ambiguous = 0
        for msg in thread.messages:
            if msg.date is not None:
                continue
            row = cur.execute(
                "SELECT sent_at FROM messages WHERE claimant_id = ? AND sent_at_status IS NULL",
                (msg.claimant_id,),
            ).fetchone()
            if row is None:
                continue
            header = f"\nFrom: {msg.from_addr}\nDate: {row[0]}\n"
            if body.count(header) == 1:
                body = body.replace(header, f"\nFrom: {msg.from_addr}\n")
            elif header in body:
                ambiguous += 1
        if ambiguous:
            # Lowers retrieval quality and sender text can trigger it:
            # WARNING, rate limited (review round 4).
            warn_rate_limited(
                log,
                "thread text kept the old fallback date line of %d message(s): "
                "it occurs more than once",
                ambiguous,
                attachment=False,
            )
        return body

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

        - ``message_ids``: union existing and incoming claimant IDs, preserving order
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
        # so cosine similarity equals dot product downstream. A caller
        # may pass a non-unit vector; normalize at the boundary so no
        # caller has to remember. The placeholder all-zero seed survives — see
        # ``l2_normalize``.
        embedding = l2_normalize(embedding)

        cur = self._conn.cursor()

        incoming_message_ids = [m.claimant_id for m in thread.messages]
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
            earliest = min(thread.messages, key=lambda m: m.effective_date)
            incoming_display_subject = earliest.subject or None
            incoming_earliest_date_iso = earliest.effective_date.isoformat()

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

            body = self._drop_fallback_date_lines(cur, thread, self._compute_body(thread, existing))

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
                newest_incoming = max(m.effective_date for m in thread.messages).isoformat()
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

            if not existing:
                # A new thread has no chunks yet: its chunk-vector sum
                # starts empty, so no write ever fills it (#1356).
                cur.execute(
                    "INSERT INTO thread_vector_sums (thread_id, count, encoding_version, sum) "
                    "VALUES (?, 0, ?, ?) ON CONFLICT(thread_id) DO NOTHING",
                    (thread.thread_id, vector_sums.ENCODING_VERSION, vector_sums.encode([])),
                )

            # Update message→thread mapping for all messages
            for msg in thread.messages:
                cur.execute(
                    """
                    INSERT INTO message_thread_map
                        (claimant_id, message_id, thread_id, filepath)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(claimant_id) DO UPDATE SET
                        thread_id = excluded.thread_id,
                        filepath  = excluded.filepath
                    """,
                    (msg.claimant_id, msg.message_id, thread.thread_id, msg.filepath),
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

            # The thread's range is its messages' effective-time
            # (``effective_at``) range. The merge above can only widen
            # it, so a reprocess that re-dates a message (a parser fix)
            # would leave the old date as an endpoint; recompute both
            # from the rows just written.
            cur.execute(
                """
                UPDATE threads SET
                    date_first = COALESCE(
                        (SELECT MIN(effective_at) FROM messages WHERE thread_id = :t),
                        date_first),
                    date_last = COALESCE(
                        (SELECT MAX(effective_at) FROM messages WHERE thread_id = :t),
                        date_last)
                WHERE thread_id = :t
                """,
                {"t": thread.thread_id},
            )

            # Update FTS5 index. threads_fts is contentless_delete=1 so DELETE
            # requires a specific rowid — read the existing fts_rowid and then
            # record the new rowid after INSERT.
            # The FTS subject column also carries the thread's changed
            # reply subjects (#303). ``thread.messages`` holds only the
            # new arrival here, so read the stored subjects; the rows
            # for this arrival were written just above. Rows and
            # characters read are capped (``FTS_SUBJECT_SCAN_*``) so a
            # long thread does not make every upsert re-read it all.
            stored_subjects = [
                r[0]
                for r in cur.execute(
                    "SELECT substr(subject, 1, ?) FROM messages WHERE thread_id = ? "
                    "ORDER BY effective_at LIMIT ?",
                    (FTS_SUBJECT_SCAN_CHARS, thread.thread_id, FTS_SUBJECT_SCAN_ROWS),
                )
            ]
            self._replace_fts_row(
                cur,
                thread.thread_id,
                fts_subject_text(thread.subject, stored_subjects),
                participants_json,
                body,
            )

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
        self, claimant_id: str, attachment_id: str | None = None
    ) -> set[str]:
        """Return the set of stored ``chunk_id`` values for ``claimant_id``.

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
                "WHERE claimant_id = ? AND attachment_id IS NULL",
                (claimant_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT chunk_id FROM message_chunks WHERE claimant_id = ? AND attachment_id = ?",
                (claimant_id, attachment_id),
            ).fetchall()
        return {row["chunk_id"] for row in rows}

    @_synchronized
    def replace_message_chunks(
        self,
        *,
        claimant_id: str,
        thread_id: str,
        chunks,
        embeddings_by_chunk_id: dict[str, list[float]],
        attachment_id: str | None = None,
        delete_missing: bool = True,
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

        A chunk stores no copy of its message's date: readers take a
        passage's dates from its ``messages`` row (``sent_at``,
        ``occurred_at``), so a re-dated message whose chunks were not
        rewritten never shows a stale date (#575).

        ``delete_missing=False`` only adds: stored chunks missing from
        ``chunks`` are kept. For an attachment slice that an occurrence
        whose extraction was deferred also holds chunks in (#1236).

        All inserts / deletes across ``message_chunks``,
        ``message_chunks_fts`` and ``message_chunks_vec`` happen inside
        one transaction so the three indexes never disagree about which
        chunks exist for a (message, slice) pair. The chunk-vector sums
        of the threads whose chunks change (``thread_id`` for an insert,
        a deleted row's own thread) are updated in the same transaction
        (#1356); a call that inserts and deletes nothing reads no vector.
        """
        # A body slice holds message-text kinds, an attachment slice
        # only ``attachment`` chunks (#646).
        if any((c.kind == "attachment") != (attachment_id is not None) for c in chunks):
            raise ValueError("chunk kind does not match its slice")
        incoming_ids = {c.chunk_id for c in chunks}
        cur = self._conn.cursor()

        started = False
        try:
            started = self._begin_if_needed(cur)

            if attachment_id is None:
                existing_rows = cur.execute(
                    "SELECT chunk_id, fts_rowid, thread_id FROM message_chunks "
                    "WHERE claimant_id = ? AND attachment_id IS NULL",
                    (claimant_id,),
                ).fetchall()
            else:
                existing_rows = cur.execute(
                    "SELECT chunk_id, fts_rowid, thread_id FROM message_chunks "
                    "WHERE claimant_id = ? AND attachment_id = ?",
                    (claimant_id, attachment_id),
                ).fetchall()
            existing_ids = {row["chunk_id"] for row in existing_rows}
            existing_fts_rowids = {
                row["chunk_id"]: row["fts_rowid"]
                for row in existing_rows
                if row["fts_rowid"] is not None
            }

            to_delete = existing_ids - incoming_ids if delete_missing else set()
            to_insert = [c for c in chunks if c.chunk_id not in existing_ids]
            row_threads = {row["chunk_id"]: row["thread_id"] for row in existing_rows}
            deltas = _SumDeltas()

            for chunk_id in to_delete:
                old_vec = cur.execute(
                    "SELECT embedding FROM message_chunks_vec WHERE chunk_id = ?", (chunk_id,)
                ).fetchone()
                if old_vec is not None:
                    deltas.add(row_threads[chunk_id], old_vec[0], -1)
                fts_rowid = existing_fts_rowids.get(chunk_id)
                if fts_rowid is not None:
                    cur.execute("DELETE FROM message_chunks_fts WHERE rowid = ?", (fts_rowid,))
                    # Chunks change only when the extractor or chunker
                    # output does; the replaced text may be what a fix
                    # removed (#670).
                    self._mark_fts_scrub("message_chunks_fts")
                cur.execute("DELETE FROM message_chunks_vec WHERE chunk_id = ?", (chunk_id,))
                cur.execute("DELETE FROM message_chunks WHERE chunk_id = ?", (chunk_id,))

            now_iso = datetime.now(UTC).isoformat()
            for chunk in to_insert:
                embedding = embeddings_by_chunk_id.get(chunk.chunk_id)
                if embedding is None:
                    raise ValueError(f"missing embedding for new chunk {chunk.chunk_id!r}")
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
                stored = sqlite_vec.serialize_float32(normalized)
                cur.execute(
                    "INSERT INTO message_chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                    (chunk.chunk_id, stored),
                )
                deltas.add(thread_id, stored, 1)
                cur.execute(
                    """
                    INSERT INTO message_chunks
                        (chunk_id, claimant_id, thread_id, chunk_index, text,
                         char_start, char_end, token_est,
                         chunked_at, fts_rowid, attachment_id, kind)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.chunk_id,
                        claimant_id,
                        thread_id,
                        chunk.chunk_index,
                        chunk.text,
                        chunk.char_start,
                        chunk.char_end,
                        chunk.token_est,
                        now_iso,
                        fts_rowid,
                        attachment_id,
                        chunk.kind,
                    ),
                )

            self._apply_thread_sum_deltas(cur, deltas)
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
        claimant_id: str,
        thread_id: str,
        attachment_id: str,
        filename: str,
        content_type: str,
        size_bytes: int,
        occurrence_id: str,
        extractor_module: str,
    ) -> bool:
        """Record one attachment occurrence on a message.

        Returns True if the row was newly inserted, False if it already
        existed. Idempotent — re-indexing the same message produces the
        same occurrence id for a specific attachment slot, and this call
        no-ops on the second call rather than churning ``seen_at`` or the
        FTS row. The one exception is ``extractor_module``, the module
        whose ``attachment_extractions`` row the occurrence uses (#928):
        when it changed (a release routes the label to another
        extractor, or a migrated row's module came from its stamp), it is
        updated and a cached row of the payload no occurrence uses any
        more is purged.

        ``occurrence_id`` must be derived via
        ``attachment_indexing.attachment_occurrence_id`` so the formula
        stays in one place and write callers cannot drift from the
        indexer's own production path.

        The filename + MIME type are mirrored into the ``attachments_fts``
        contentless table for direct keyword search ("find the .pdf
        named contract"). The deterministic ``attachment_id`` (sha256
        of payload bytes) and ``extractor_module`` are what link the
        occurrence to its cached extraction in ``attachment_extractions``.
        """
        cur = self._conn.cursor()
        started = False
        try:
            started = self._begin_if_needed(cur)
            existing = cur.execute(
                "SELECT extractor_module FROM attachments WHERE attachment_occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            if existing is not None:
                if existing["extractor_module"] != extractor_module:
                    cur.execute(
                        "UPDATE attachments SET extractor_module = ? "
                        "WHERE attachment_occurrence_id = ?",
                        (extractor_module, occurrence_id),
                    )
                    cur.execute(_PURGE_ORPHAN_EXTRACTION_SQL, (attachment_id,))
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
                    (attachment_occurrence_id, claimant_id, attachment_id, thread_id, filename,
                     content_type, size_bytes, seen_at, fts_rowid, extractor_module)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    occurrence_id,
                    claimant_id,
                    attachment_id,
                    thread_id,
                    filename,
                    content_type,
                    size_bytes,
                    now_iso,
                    fts_rowid,
                    extractor_module,
                ),
            )
            self._commit_if_started(started)
            return True
        except Exception:
            self._rollback_if_started(started)
            raise

    @_synchronized
    def set_attachment_text_complete(
        self, occurrence_id: str, complete: bool | None, text_extractor: str | None
    ) -> None:
        """Record whether an occurrence's attachment chunks hold all its
        text, and the extractor stamp of the result that applied (#1242).

        Called in phase 2c inside the transaction that commits the
        occurrence's chunks (``apply_attachment_writes``), so it rolls
        back with them and always describes the committed chunks. The
        occurrence's result applied, so any deferral mark is cleared
        (#1236)."""
        cur = self._conn.cursor()
        started = False
        try:
            started = self._begin_if_needed(cur)
            cur.execute(
                "UPDATE attachments SET text_complete = ?, text_extractor = ?, "
                "extraction_deferred_at = NULL WHERE attachment_occurrence_id = ?",
                (None if complete is None else int(complete), text_extractor, occurrence_id),
            )
            self._commit_if_started(started)
        except Exception:
            self._rollback_if_started(started)
            raise

    @_synchronized
    def mark_attachment_extraction_deferred(self, occurrence_id: str) -> None:
        """Mark an occurrence whose extraction the per-message budget
        deferred to a later pass (#1236): ``text_complete`` 0, since its
        chunks may not hold its text, and ``extraction_deferred_at`` now.
        ``text_extractor`` keeps the stamp of the result its chunks came
        from. In phase 2c's transaction, like ``set_attachment_text_complete``."""
        cur = self._conn.cursor()
        started = False
        try:
            started = self._begin_if_needed(cur)
            cur.execute(
                "UPDATE attachments SET text_complete = 0, extraction_deferred_at = ? "
                "WHERE attachment_occurrence_id = ?",
                (datetime.now(UTC).isoformat(), occurrence_id),
            )
            self._commit_if_started(started)
        except Exception:
            self._rollback_if_started(started)
            raise

    @_synchronized
    def get_attachment_payloads(
        self, claimant_id: str, occurrence_ids: list[str]
    ) -> dict[str, set[str]]:
        """The payloads (``attachment_id``) of ``claimant_id``'s stored
        occurrences among ``occurrence_ids``, each with the extractor
        modules those occurrences ran."""
        payloads: dict[str, set[str]] = {}
        for occurrence_id in occurrence_ids:
            row = self._conn.execute(
                "SELECT attachment_id, extractor_module FROM attachments "
                "WHERE attachment_occurrence_id = ? AND claimant_id = ?",
                (occurrence_id, claimant_id),
            ).fetchone()
            if row is not None:
                payloads.setdefault(row["attachment_id"], set()).add(row["extractor_module"])
        return payloads

    @_synchronized
    def delete_attachment_occurrences(
        self,
        claimant_id: str,
        occurrence_ids: list[str],
        keep_extractions: set[tuple[str, str]],
        purge_later: set[tuple[str, str]],
    ) -> tuple[int, int]:
        """Drop occurrences of ``claimant_id`` its current parse no longer
        produces (#1375) and return how many rows went and how many
        payload slices went with them.

        Each occurrence's ``attachments`` row and ``attachments_fts`` row
        go (a deferral mark goes with the row, #1236). The message's
        chunk slice of a payload goes only when no remaining occurrence
        of the message carries that payload, since the slice is keyed by
        message and payload and a surviving sibling (the same bytes under
        a corrected filename) uses it; its vectors are subtracted from
        their threads' chunk-vector sums (#1356). A cached extraction of
        the payload that no remaining occurrence uses is purged, as when
        the whole message is removed, except a (payload, module) row in
        ``keep_extractions``: a later message of the same batch prepared
        against that cached row and writes its occurrence after this
        one. Such a row is added to ``purge_later``, for
        ``purge_orphan_extractions`` once the batch's commits are known
        (a peer whose commit fails leaves the row without a carrier). The thread's ``has_attachments`` is recomputed, since
        ``upsert_thread`` only ever sets it: it stays true while any
        occurrence of the thread is stored or any message of it has
        attachments without a stored occurrence yet (a batch peer
        whose commit has not run). In phase 2c's
        transaction, after the pass's own occurrence writes, so the
        remaining occurrences are the parse's.
        """
        cur = self._conn.cursor()
        started = False
        try:
            started = self._begin_if_needed(cur)
            payloads: set[str] = set()
            deleted = 0
            slices = 0
            for occurrence_id in occurrence_ids:
                row = cur.execute(
                    "SELECT fts_rowid, attachment_id FROM attachments "
                    "WHERE attachment_occurrence_id = ? AND claimant_id = ?",
                    (occurrence_id, claimant_id),
                ).fetchone()
                if row is None:
                    continue
                payloads.add(row["attachment_id"])
                if row["fts_rowid"] is not None:
                    cur.execute("DELETE FROM attachments_fts WHERE rowid = ?", (row["fts_rowid"],))
                    self._mark_fts_scrub("attachments_fts")
                cur.execute(
                    "DELETE FROM attachments WHERE attachment_occurrence_id = ?",
                    (occurrence_id,),
                )
                deleted += 1
            for attachment_id in sorted(payloads):
                still_carried = cur.execute(
                    "SELECT 1 FROM attachments WHERE claimant_id = ? AND attachment_id = ? LIMIT 1",
                    (claimant_id, attachment_id),
                ).fetchone()
                if still_carried is None:
                    rows = cur.execute(
                        "SELECT chunk_id, fts_rowid, thread_id FROM message_chunks "
                        "WHERE claimant_id = ? AND attachment_id = ?",
                        (claimant_id, attachment_id),
                    ).fetchall()
                    self._delete_chunks_in_batches(cur, rows)
                    cur.execute(
                        "DELETE FROM message_chunks WHERE claimant_id = ? AND attachment_id = ?",
                        (claimant_id, attachment_id),
                    )
                    slices += bool(rows)
                for (module,) in cur.execute(
                    "SELECT extractor_module FROM attachment_extractions WHERE attachment_id = ?",
                    (attachment_id,),
                ).fetchall():
                    if (attachment_id, module) in keep_extractions:
                        purge_later.add((attachment_id, module))
                    else:
                        cur.execute(
                            _PURGE_ORPHAN_EXTRACTION_SQL + " AND extractor_module = ?",
                            (attachment_id, module),
                        )
            if deleted:
                cur.execute(
                    "UPDATE threads SET has_attachments = EXISTS ("
                    "SELECT 1 FROM message_thread_map t "
                    "JOIN attachments a ON a.claimant_id = t.claimant_id "
                    "WHERE t.thread_id = threads.thread_id) OR EXISTS ("
                    "SELECT 1 FROM message_thread_map t "
                    "JOIN messages m ON m.claimant_id = t.claimant_id "
                    "WHERE t.thread_id = threads.thread_id AND m.has_attachments = 1 "
                    "AND NOT EXISTS (SELECT 1 FROM attachments a "
                    "WHERE a.claimant_id = t.claimant_id)) "
                    "WHERE thread_id = (SELECT thread_id FROM message_thread_map "
                    "WHERE claimant_id = ?)",
                    (claimant_id,),
                )
            self._commit_if_started(started)
        except Exception:
            self._rollback_if_started(started)
            raise
        return deleted, slices

    @_synchronized
    def purge_orphan_extractions(self, keys: set[tuple[str, str]]) -> None:
        """Drop each cached extraction among ``keys`` (payload, module)
        that no ``attachments`` row uses (#1375), in one transaction."""
        if not keys:
            return
        cur = self._conn.cursor()
        started = False
        try:
            started = self._begin_if_needed(cur)
            for attachment_id, module in sorted(keys):
                cur.execute(
                    _PURGE_ORPHAN_EXTRACTION_SQL + " AND extractor_module = ?",
                    (attachment_id, module),
                )
            self._commit_if_started(started)
        except Exception:
            self._rollback_if_started(started)
            raise

    @_synchronized
    def get_attachment_occurrence_states(
        self, claimant_id: str
    ) -> dict[str, tuple[int | None, bool]]:
        """Each stored attachment occurrence of a message, by occurrence
        ID: ``(text_complete, deferred)`` (#1236). A recorded
        ``text_complete`` with no deferral mark is work an extraction
        continuation does not reopen; ``deferred`` is a deferral mark."""
        rows = self._conn.execute(
            "SELECT attachment_occurrence_id, text_complete, extraction_deferred_at "
            "FROM attachments WHERE claimant_id = ?",
            (claimant_id,),
        ).fetchall()
        return {
            r["attachment_occurrence_id"]: (
                r["text_complete"],
                r["extraction_deferred_at"] is not None,
            )
            for r in rows
        }

    @_synchronized
    def get_assessed_text_extractors(self) -> list[str]:
        """Distinct extractor stamps of occurrences whose text
        completeness is recorded (#1242)."""
        rows = self._conn.execute(
            "SELECT DISTINCT text_extractor FROM attachments "
            "WHERE text_extractor IS NOT NULL AND text_complete IS NOT NULL"
        ).fetchall()
        return [r["text_extractor"] for r in rows]

    @_synchronized
    def clear_text_complete_for_extractors(self, extractors: list[str]) -> int:
        """Set ``text_complete`` to NULL on every occurrence whose text
        came from one of ``extractors`` (#1242), dead-lettered messages'
        included, and return how many changed. One statement, so it
        commits whole."""
        if not extractors:
            return 0
        placeholders = ",".join(["?"] * len(extractors))
        cur = self._conn.cursor()
        started = False
        try:
            started = self._begin_if_needed(cur)
            cur.execute(
                "UPDATE attachments SET text_complete = NULL WHERE text_complete IS NOT NULL "
                f"AND text_extractor IN ({placeholders})",  # nosec B608 — placeholders only
                extractors,
            )
            changed = cur.rowcount
            self._commit_if_started(started)
        except Exception:
            self._rollback_if_started(started)
            raise
        return changed

    @_synchronized
    def get_extractor_names(self) -> list[str]:
        """Distinct extractor names recorded in the extraction cache."""
        rows = self._conn.execute(
            "SELECT DISTINCT extractor FROM attachment_extractions WHERE extractor IS NOT NULL"
        ).fetchall()
        return [r["extractor"] for r in rows]

    @_synchronized
    def find_filepaths_with_extractors(self, extractors: list[str]) -> list[str]:
        """Maildir filepaths of messages carrying an attachment occurrence
        whose cached extraction (the row its ``extractor_module`` names)
        was written by one of ``extractors``."""
        if not extractors:
            return []
        placeholders = ",".join(["?"] * len(extractors))
        rows = self._conn.execute(
            f"""
            SELECT DISTINCT m.filepath
            FROM attachment_extractions e
            JOIN attachments a ON a.attachment_id = e.attachment_id
                AND a.extractor_module = e.extractor_module
            JOIN message_thread_map m ON m.claimant_id = a.claimant_id
            WHERE e.extractor IN ({placeholders})
            ORDER BY m.filepath
            """,  # nosec B608 — placeholders only, values are bound
            extractors,
        ).fetchall()
        return [r["filepath"] for r in rows]

    def _stream_filepaths(
        self,
        sql: str,
        params: Sequence[object],
        qualifies: Callable[[sqlite3.Row], bool],
        assessed: CompletenessClearing | None = None,
    ) -> set[str]:
        """The filepaths of the rows ``sql`` returns for which
        ``qualifies`` holds, read ``SWEEP_FETCH_ROWS`` rows at a time
        (#1289). A message can carry thousands of occurrences with
        sender-chosen labels and sizes, so only the qualifying filepaths
        are kept, never the rows. Callers hold the database lock for the
        whole read; the cursor is closed before they return.

        With ``assessed``, a qualifying row whose occurrence still has a
        ``text_complete`` record (``occurrence_complete``) has it cleared
        through ``assessed``, batch by batch as the rows stream: its text
        is due a refresh (#1236). The column cleared is neither filtered
        nor indexed by these queries, so the open read is unaffected."""
        cursor = self._conn.execute(sql, params)
        filepaths: set[str] = set()
        try:
            while rows := cursor.fetchmany(SWEEP_FETCH_ROWS):
                for row in rows:
                    if not qualifies(row):
                        continue
                    filepaths.add(row["filepath"])
                    if assessed is not None and row["occurrence_complete"] is not None:
                        assessed.add(row["occurrence_id"])
        finally:
            cursor.close()
        return filepaths

    @_synchronized
    def find_unrecorded_completeness_occurrences(
        self, qualifies: Callable[[sqlite3.Row], bool], assessed: CompletenessClearing | None = None
    ) -> set[str]:
        """The filepaths of the messages with an occurrence whose cached
        ``success`` or ``empty`` result has no completeness record (#1285)
        and for which ``qualifies`` holds (it gets the row's status, stamp
        and cached ``text_complete``), read ``SWEEP_FETCH_ROWS`` rows at a
        time and keeping filepaths only, so a message with many such
        occurrences costs one entry (review round 3 on #1286). Each such
        occurrence that still has a record of its own (a parse cap can
        set 0 beside a cached row with none) has it cleared through
        ``assessed`` (#1236)."""
        return self._stream_filepaths(
            """
            SELECT m.filepath, e.extraction_status, e.extractor, e.text_complete,
                   a.attachment_occurrence_id AS occurrence_id,
                   a.text_complete AS occurrence_complete
            FROM attachment_extractions e
            JOIN attachments a ON a.attachment_id = e.attachment_id
                AND a.extractor_module = e.extractor_module
            JOIN message_thread_map m ON m.claimant_id = a.claimant_id
            WHERE e.text_complete IS NULL
              AND e.extraction_status IN ('success', 'empty')
            """,
            (),
            qualifies,
            assessed,
        )

    @_synchronized
    def _clear_text_complete_batch(self, occurrence_ids: list[str]) -> int:
        """Set ``text_complete`` to NULL on the named occurrences in one
        transaction (``CompletenessClearing``, #1236). A failed batch rolls
        back alone; the batches before it stay, and a restart clears the
        rest, since a cleared occurrence is not found again. Deferral
        marks and queue rows are untouched."""
        cur = self._conn.cursor()
        started = False
        try:
            started = self._begin_if_needed(cur)
            cur.execute(
                "UPDATE attachments SET text_complete = NULL WHERE text_complete IS NOT NULL "
                "AND attachment_occurrence_id IN (SELECT value FROM json_each(?))",
                (json.dumps(occurrence_ids),),
            )
            changed = cur.rowcount
            self._commit_if_started(started)
        except Exception:
            self._rollback_if_started(started)
            raise
        return changed

    @_synchronized
    def find_ocr_disabled_attachment_filepaths(
        self,
        qualifies: Callable[[sqlite3.Row], bool],
        assessed: CompletenessClearing | None = None,
    ) -> set[str]:
        """The Maildir filepaths of the messages with an attachment
        occurrence whose cached extraction is an "OCR disabled" result
        and for which ``qualifies`` holds. ``qualifies`` gets one row per
        occurrence with the filepath, filename, MIME type, the cached
        error and the row's extractor module."""
        return self._stream_filepaths(
            """
            SELECT m.filepath, a.filename, a.content_type, e.extraction_error,
                   e.extractor_module, a.attachment_occurrence_id AS occurrence_id,
                   a.text_complete AS occurrence_complete
            FROM attachment_extractions e
            JOIN attachments a ON a.attachment_id = e.attachment_id
                AND a.extractor_module = e.extractor_module
            JOIN message_thread_map m ON m.claimant_id = a.claimant_id
            WHERE e.extraction_status = 'unsupported'
              AND e.extraction_error IN (?, ?)
            """,
            (OCR_DISABLED_ERROR, SCANNED_PDF_OCR_DISABLED_ERROR),
            qualifies,
            assessed,
        )

    @_synchronized
    def find_no_extractor_attachment_filepaths(
        self,
        qualifies: Callable[[sqlite3.Row], bool],
        assessed: CompletenessClearing | None = None,
    ) -> set[str]:
        """The Maildir filepaths of the messages with an attachment
        occurrence whose cached extraction is the "no extractor for this
        content type or filename extension" result, or the OLE2 result
        recorded when no extractor read an OLE2 payload (#694, #935), and
        for which ``qualifies`` holds, with the same row columns as
        ``find_ocr_disabled_attachment_filepaths``."""
        return self._stream_filepaths(
            """
            SELECT m.filepath, a.filename, a.content_type, e.extraction_error,
                   e.extractor_module, a.attachment_occurrence_id AS occurrence_id,
                   a.text_complete AS occurrence_complete
            FROM attachment_extractions e
            JOIN attachments a ON a.attachment_id = e.attachment_id
                AND a.extractor_module = e.extractor_module
            JOIN message_thread_map m ON m.claimant_id = a.claimant_id
            WHERE e.extraction_status = 'unsupported'
              AND e.extraction_error IN (?, ?)
            """,
            (NO_EXTRACTOR_ERROR, LEGACY_OLE2_ERROR),
            qualifies,
            assessed,
        )

    @_synchronized
    def find_fitting_too_large_attachment_filepaths(
        self, max_bytes: int, assessed: CompletenessClearing | None = None
    ) -> set[str]:
        """The Maildir filepaths of the messages with an attachment
        occurrence whose cached extraction is ``too_large`` and whose
        payload now fits under ``max_bytes``: the comparison
        ``attachment_indexing.too_large_fits`` makes, run in SQL. The row
        is keyed by content hash, so every occurrence using it carries
        the same size."""
        return self._stream_filepaths(
            """
            SELECT m.filepath, a.attachment_occurrence_id AS occurrence_id,
                   a.text_complete AS occurrence_complete
            FROM attachment_extractions e
            JOIN attachments a ON a.attachment_id = e.attachment_id
                AND a.extractor_module = e.extractor_module
            JOIN message_thread_map m ON m.claimant_id = a.claimant_id
            WHERE e.extraction_status = 'too_large'
              AND a.size_bytes <= ?
            """,
            (max_bytes,),
            lambda _row: True,
            assessed,
        )

    @_synchronized
    def find_deferred_extraction_filepaths(self) -> set[str]:
        """The Maildir filepaths of the messages with an attachment
        occurrence whose extraction the per-message budget deferred
        (#1236), so the startup sweep can re-queue one whose continuation
        was lost (a pass with attachment extraction switched off marks
        the message succeeded and leaves the mark)."""
        return self._stream_filepaths(
            """
            SELECT m.filepath
            FROM attachments a
            JOIN message_thread_map m ON m.claimant_id = a.claimant_id
            WHERE a.extraction_deferred_at IS NOT NULL
            """,
            (),
            lambda _row: True,
        )

    @_synchronized
    def get_attachment_extraction(
        self, attachment_id: str, extractor_module: str
    ) -> sqlite3.Row | None:
        """Return the cached extraction row for a payload under one
        extractor module ('' for none), or None.

        Used by the indexer write path to skip extraction work whenever
        the same payload has already been processed by the extractor this
        occurrence selects. Even a failed prior extraction is returned —
        the caller can decide whether to retry based on
        ``extraction_status`` and how recent ``extracted_at`` is.
        """
        return self._conn.execute(
            "SELECT attachment_id, extractor_module, extraction_status, extractor, "
            "extracted_text, extraction_error, extracted_at, ocr_pages_skipped, text_complete "
            "FROM attachment_extractions WHERE attachment_id = ? AND extractor_module = ?",
            (attachment_id, extractor_module),
        ).fetchone()

    @_synchronized
    def store_attachment_extraction(
        self,
        *,
        attachment_id: str,
        extractor_module: str,
        extraction_status: str,
        extractor: str | None,
        extracted_text: str | None,
        extraction_error: str | None,
        ocr_pages_skipped: int | None = None,
        text_complete: bool | None = None,
    ) -> None:
        """Persist (or replace) the extraction record for ``attachment_id``
        under ``extractor_module`` ('' when the occurrence selects no
        extractor).

        ``extraction_status`` is one of:

        * ``"success"`` — text extracted; ``extracted_text`` populated
        * ``"empty"`` — extractor ran but produced no text (image of a
          blank page, password-protected PDF with no fallback, etc.)
        * ``"unsupported"`` — no extractor registered for the MIME type
        * ``"too_large"`` — payload exceeds the configured byte cap
        * ``"failed"`` — extractor raised; ``extraction_error`` populated

        ``ocr_pages_skipped`` is the scanned PDF pages the OCR page cap
        left unread (#891), ``None`` when unknown. ``text_complete`` is
        whether the result lost no text (#1242), ``None`` when unknown.

        The same (attachment_id, extractor_module) is OR-REPLACE'd so a follow-up pass
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
                    (attachment_id, extractor_module, extraction_status, extractor,
                     extracted_text, extraction_error, extracted_at, ocr_pages_skipped,
                     text_complete)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attachment_id,
                    extractor_module,
                    extraction_status,
                    extractor,
                    extracted_text,
                    extraction_error,
                    datetime.now(UTC).isoformat(),
                    ocr_pages_skipped,
                    None if text_complete is None else int(text_complete),
                ),
            )
            self._commit_if_started(started)
        except Exception:
            self._rollback_if_started(started)
            raise

    def _delete_attachments_for_message(self, cur: sqlite3.Cursor, claimant_id: str) -> None:
        """Drop all ``attachments`` occurrences and their FTS rows for
        ``claimant_id``, then each cached ``attachment_extractions`` row
        of their payloads that no remaining occurrence uses (#562, #928).

        A row another message's occurrence still uses is kept. Purging
        an orphan gives up the extraction cache for a later re-arrival of
        the same bytes, which is extracted again; the extracted text must
        not outlive every message that carried it. Runs on the caller's
        cursor, inside the caller's transaction.
        """
        rows = cur.execute(
            "SELECT fts_rowid, attachment_id FROM attachments WHERE claimant_id = ?",
            (claimant_id,),
        ).fetchall()
        for row in rows:
            if row["fts_rowid"] is not None:
                cur.execute("DELETE FROM attachments_fts WHERE rowid = ?", (row["fts_rowid"],))
                self._mark_fts_scrub("attachments_fts")
        cur.execute("DELETE FROM attachments WHERE claimant_id = ?", (claimant_id,))
        for attachment_id in sorted({row["attachment_id"] for row in rows}):
            cur.execute(_PURGE_ORPHAN_EXTRACTION_SQL, (attachment_id,))

    @_synchronized
    def replace_thread_vector(self, thread_id: str, embedding: list[float]) -> None:
        """Replace the row in ``threads_vec`` for ``thread_id``.

        Used by Phase 2c to write the thread's chunk mean
        (``thread_chunk_mean``) or its subject fallback, without going through the
        full ``upsert_thread`` path (which requires a materialized
        ``Thread`` and would also rewrite the FTS row, body_text, and
        every metadata field unnecessarily). Validates the embedding
        dimension so a misconfigured embed model fails loud here rather
        than as a cryptic vec0 insert error.
        """
        cur = self._conn.cursor()
        started = False
        try:
            started = self._begin_if_needed(cur)
            self._write_thread_vector(cur, thread_id, embedding)
            self._commit_if_started(started)
        except Exception:
            self._rollback_if_started(started)
            raise

    @staticmethod
    def _write_thread_vector(cur: sqlite3.Cursor, thread_id: str, embedding: list[float]) -> None:
        """Replace ``thread_id``'s ``threads_vec`` row on ``cur``, in the
        caller's transaction."""
        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding has {len(embedding)} dims but threads_vec reserves "
                f"{EMBEDDING_DIM}. Check the embedder's output dimension."
            )
        # Storage invariant — see the matching note in ``upsert_thread``.
        # A caller may pass a non-unit vector; normalize at the boundary
        # (a no-op on ``vector_sums.thread_vector``, already normalized).
        embedding = l2_normalize(embedding)
        cur.execute("DELETE FROM threads_vec WHERE thread_id = ?", (thread_id,))
        cur.execute(
            "INSERT INTO threads_vec (thread_id, embedding) VALUES (?, ?)",
            (thread_id, sqlite_vec.serialize_float32(embedding)),
        )

    # -------------------------------------------------------------------------
    # Thread vector sums (#1356): the exact running sum of each thread's
    # chunk vectors and their count, so the thread vector is derived
    # without reading every chunk vector of the thread (``vector_sums``).
    #
    # The sum covers the ``message_chunks`` rows whose ``thread_id`` is
    # the thread and that have a ``message_chunks_vec`` row, the set the
    # thread-wide read joined. Every write that inserts or deletes chunk
    # vectors applies ``S += new - old`` and the count change in its own
    # transaction (``replace_message_chunks``, ``_delete_chunks_in_batches``).
    # Moving a message to another thread in ``message_thread_map`` does
    # not move its chunk rows, so it changes no sum. A thread created by
    # ``upsert_thread`` starts with an empty sum; a thread from before v10
    # is filled the first time a write touches it, in that write's
    # transaction (one thread-wide read in its lifetime), or by the
    # backfill sweep.
    # -------------------------------------------------------------------------

    def _compute_thread_sums(self, thread_id: str) -> tuple[list[int], int]:
        """The exact sum and count of ``thread_id``'s chunk vectors, read
        from every one of them: the thread-wide read the stored sums
        replace. Only an inline fill, the backfill and the check run it.
        Rows are streamed, so memory holds one vector at a time."""
        total = [0] * EMBEDDING_DIM
        count = 0
        for row in self._conn.execute(
            "SELECT v.embedding FROM message_chunks c "
            "JOIN message_chunks_vec v ON v.chunk_id = c.chunk_id "
            "WHERE c.thread_id = ?",
            (thread_id,),
        ):
            total = vector_sums.add_units(total, vector_sums.vector_units(row[0]))
            count += 1
        return total, count

    def _read_thread_sums(
        self, cur: sqlite3.Cursor, thread_id: str
    ) -> tuple[list[int], int] | None:
        """The stored sum and count of ``thread_id``, or ``None`` when it
        has no row, or a row this code cannot read (then it is refilled
        from the chunk vectors)."""
        row = cur.execute(
            "SELECT count, encoding_version, sum FROM thread_vector_sums WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            return None
        if row["encoding_version"] == vector_sums.ENCODING_VERSION:
            try:
                return vector_sums.decode(row["sum"], EMBEDDING_DIM), row["count"]
            except ValueError:
                pass
        log.warning("thread vector sums: an unreadable row is recomputed")
        return None

    @staticmethod
    def _write_thread_sums(
        cur: sqlite3.Cursor, thread_id: str, total: list[int], count: int
    ) -> None:
        cur.execute(
            "INSERT INTO thread_vector_sums (thread_id, count, encoding_version, sum) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(thread_id) DO UPDATE SET "
            "count = excluded.count, encoding_version = excluded.encoding_version, "
            "sum = excluded.sum",
            (thread_id, count, vector_sums.ENCODING_VERSION, vector_sums.encode(total)),
        )

    def _thread_sums(self, cur: sqlite3.Cursor, thread_id: str) -> tuple[list[int], int]:
        """The sum and count of ``thread_id``, filled inline when it has
        no row. Runs in the caller's transaction."""
        state = self._read_thread_sums(cur, thread_id)
        if state is None:
            state = self._compute_thread_sums(thread_id)
            self._write_thread_sums(cur, thread_id, *state)
        return state

    def _apply_thread_sum_deltas(self, cur: sqlite3.Cursor, deltas: _SumDeltas) -> None:
        """Apply each thread's ``S += new - old`` and count change, after
        the chunk rows were written in the same transaction. A thread
        without a row is filled from its chunk vectors as they are now,
        which already include the change. A count that would go negative
        or an empty sum that is not zero means a write was missed: the
        thread is recomputed and a warning logged."""
        for thread_id, (delta, count_delta) in deltas.items():
            state = self._read_thread_sums(cur, thread_id)
            if state is None:
                total, count = self._compute_thread_sums(thread_id)
            else:
                total = vector_sums.add_units(state[0], delta)
                count = state[1] + count_delta
                if count < 0 or (count == 0 and any(total)):
                    log.warning(
                        "thread vector sums: a running sum disagreed with its "
                        "chunks and is recomputed"
                    )
                    total, count = self._compute_thread_sums(thread_id)
            self._write_thread_sums(cur, thread_id, total, count)

    @_synchronized
    def thread_chunk_mean(self, thread_id: str) -> list[float] | None:
        """The thread vector derived from ``thread_id``'s chunk vectors
        (``vector_sums.thread_vector``), or ``None`` when it has none (the
        caller keeps its chunkless fallback). Fills the thread's sums when
        it has no row, in the caller's transaction when there is one."""
        cur = self._conn.cursor()
        started = False
        try:
            started = self._begin_if_needed(cur)
            total, count = self._thread_sums(cur, thread_id)
            self._commit_if_started(started)
        except Exception:
            self._rollback_if_started(started)
            raise
        return vector_sums.thread_vector(total, count) if count else None

    @_synchronized
    def get_phase1_seed_state(
        self, thread_id: str
    ) -> tuple[list[float] | None, list[float] | None]:
        """Combined fetch for the batched indexer's Phase 1 seed selection.

        Returns ``(chunk_mean, prior_thread_vector)`` so the caller
        applies the three-case priority chain: a chunk mean → that mean;
        no chunk vectors + non-zero prior → prior; else → zero
        placeholder. Both are ``None`` for a thread that does not exist
        yet (the new-thread fast path, one primary-key read).

        The chunk mean is derived from the thread's stored sums
        (``thread_chunk_mean``), filled here in its own transaction when
        the thread has no row, so no chunk vector is read once it has
        one. The prior vector is read only for a thread without chunk
        vectors.
        """
        if (
            self._conn.execute("SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
            is None
        ):
            return None, None
        chunk_mean = self.thread_chunk_mean(thread_id)
        if chunk_mean is not None:
            return chunk_mean, None
        vec_row = self._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        prior_vec: list[float] | None = None
        if vec_row is not None:
            blob = vec_row["embedding"]
            count = len(blob) // 4
            prior_vec = list(struct.unpack(f"{count}f", blob))
        return None, prior_vec

    @_synchronized
    def thread_has_chunks_outside(self, thread_id: str, claimant_ids: list[str]) -> bool:
        """Whether ``thread_id`` has a chunk row of a message not in
        ``claimant_ids``: whether its chunk sum stays non-empty once
        those messages are removed. The reaper asks it before a partial
        reap, to choose between the chunk mean and the subject fallback.
        Reads chunk IDs, never vectors."""
        row = self._conn.execute(
            "SELECT 1 FROM message_chunks WHERE thread_id = ? "
            "AND claimant_id NOT IN (SELECT value FROM json_each(?)) LIMIT 1",
            (thread_id, json.dumps(claimant_ids)),
        ).fetchone()
        return row is not None

    def _fill_thread_sums_if_missing(self, thread_id: str) -> int | None:
        """Fill one thread's sums in its own transaction when it still
        has no row and still exists. Returns the chunk vectors read, or
        ``None`` when nothing was filled."""
        with self._lock:
            cur = self._conn.cursor()
            started = False
            try:
                started = self._begin_if_needed(cur)
                if (
                    cur.execute(
                        "SELECT 1 FROM threads t WHERE t.thread_id = ? AND NOT EXISTS "
                        "(SELECT 1 FROM thread_vector_sums s WHERE s.thread_id = t.thread_id)",
                        (thread_id,),
                    ).fetchone()
                    is None
                ):
                    self._commit_if_started(started)
                    return None
                total, count = self._compute_thread_sums(thread_id)
                self._write_thread_sums(cur, thread_id, total, count)
                self._commit_if_started(started)
            except Exception:
                self._rollback_if_started(started)
                raise
        return count

    def fill_missing_thread_vector_sums(
        self, *, max_threads: int, max_rows: int
    ) -> tuple[int, int, int]:
        """One bounded batch of the backfill sweep: fill the sums of up to
        ``max_threads`` threads that have no row, stopping once
        ``max_rows`` chunk vectors were read (a thread is always filled
        whole, so one larger thread can exceed it). Progress is the rows
        themselves, so an interrupted sweep resumes where it stopped.
        Each thread is its own transaction; the lock is released between
        them. Returns ``(filled, rows_read, remaining)``."""
        with self._lock:
            thread_ids = [
                r[0]
                for r in self._conn.execute(
                    "SELECT t.thread_id FROM threads t WHERE NOT EXISTS "
                    "(SELECT 1 FROM thread_vector_sums s WHERE s.thread_id = t.thread_id) "
                    "LIMIT ?",
                    (max_threads,),
                )
            ]
        filled = rows_read = 0
        for thread_id in thread_ids:
            if rows_read >= max_rows:
                break
            read = self._fill_thread_sums_if_missing(thread_id)
            if read is not None:
                filled += 1
                rows_read += read
        with self._lock:
            remaining = self._conn.execute(
                "SELECT COUNT(*) FROM threads t WHERE NOT EXISTS "
                "(SELECT 1 FROM thread_vector_sums s WHERE s.thread_id = t.thread_id)"
            ).fetchone()[0]
        return filled, rows_read, remaining

    def reconcile_thread_vector_sums(self, thread_id: str) -> tuple[bool, int]:
        """Recompute ``thread_id``'s sum and count exactly from its chunk
        vectors and, when the stored row differs (or is missing or
        unreadable), repair it and rewrite the thread vector from the
        recomputed sum, all in one transaction. A thread without chunk
        vectors keeps its stored vector (its chunkless fallback). Returns
        ``(repaired, rows_read)``; a thread that no longer exists is
        ``(False, 0)``."""
        with self._lock:
            cur = self._conn.cursor()
            started = False
            try:
                started = self._begin_if_needed(cur)
                if (
                    cur.execute(
                        "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)
                    ).fetchone()
                    is None
                ):
                    self._commit_if_started(started)
                    return False, 0
                stored = cur.execute(
                    "SELECT count, encoding_version, sum FROM thread_vector_sums "
                    "WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                total, count = self._compute_thread_sums(thread_id)
                expected = (count, vector_sums.ENCODING_VERSION, vector_sums.encode(total))
                if stored is not None and tuple(stored) == expected:
                    self._commit_if_started(started)
                    return False, count
                self._write_thread_sums(cur, thread_id, total, count)
                if count:
                    self._write_thread_vector(
                        cur, thread_id, vector_sums.thread_vector(total, count)
                    )
                self._commit_if_started(started)
            except Exception:
                self._rollback_if_started(started)
                raise
        return True, count

    def reconcile_thread_vector_sums_batch(
        self, *, after: str | None, max_threads: int, max_rows: int
    ) -> tuple[int, int, int, str | None]:
        """Check the next filled threads in ``thread_id`` order after
        ``after`` (from the start when ``None``) with
        ``reconcile_thread_vector_sums``, up to ``max_threads`` threads,
        stopping once ``max_rows`` chunk vectors were read. Returns
        ``(checked, repaired, rows_read, cursor)``: pass ``cursor`` as
        the next ``after``; it is ``None`` once the last thread was
        checked, so the next batch starts over."""
        with self._lock:
            thread_ids = [
                r[0]
                for r in self._conn.execute(
                    "SELECT thread_id FROM thread_vector_sums WHERE thread_id > ? "
                    "ORDER BY thread_id LIMIT ?",
                    (after or "", max_threads),
                )
            ]
        checked = repaired = rows_read = 0
        cursor: str | None = None
        for thread_id in thread_ids:
            if rows_read >= max_rows:
                break
            fixed, read = self.reconcile_thread_vector_sums(thread_id)
            checked += 1
            repaired += int(fixed)
            rows_read += read
            cursor = thread_id
        if cursor is not None and cursor == thread_ids[-1] and len(thread_ids) < max_threads:
            cursor = None
        return checked, repaired, rows_read, cursor

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
    def thread_has_chunks_not_in(self, thread_id: str, chunk_ids: set[str]) -> bool:
        """Whether ``thread_id`` has a chunk row outside ``chunk_ids``:
        whether it keeps a chunk once those are deleted. Reads chunk IDs,
        never vectors."""
        row = self._conn.execute(
            "SELECT 1 FROM message_chunks WHERE thread_id = ? "
            "AND chunk_id NOT IN (SELECT value FROM json_each(?)) LIMIT 1",
            (thread_id, json.dumps(sorted(chunk_ids))),
        ).fetchone()
        return row is not None

    @_synchronized
    def thread_has_chunks(self, thread_id: str) -> bool:
        """Return True iff at least one chunk row exists for ``thread_id``.

        Cheap existence check for the subject-fallback gate in the
        batched indexer's Phase 2a. Reading the thread's chunk vectors
        (or filling its chunk-vector sum) is the wrong tool for that
        check: on chatty threads with hundreds of chunks that's wasted
        I/O on a hot per-message path.
        """
        row = self._conn.execute(
            "SELECT 1 FROM message_chunks WHERE thread_id = ? LIMIT 1",
            (thread_id,),
        ).fetchone()
        return row is not None

    def _delete_chunks_in_batches(
        self,
        cur: sqlite3.Cursor,
        rows: list[sqlite3.Row],
        *,
        track_sums: bool = True,
    ) -> None:
        """Bulk-delete the ``message_chunks_fts`` and ``message_chunks_vec``
        rows for a list of ``(chunk_id, fts_rowid, thread_id)`` results.

        With ``track_sums`` the deleted vectors are read, one batch at a
        time, and subtracted from their threads' chunk-vector sums in the
        caller's transaction (#1356). A caller deleting the whole thread,
        whose sums row goes with it, passes ``False``.

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
        row_threads: dict[str, str] = {}
        for row in rows:
            chunk_ids.append(row["chunk_id"])
            row_threads[row["chunk_id"]] = row["thread_id"]
            if row["fts_rowid"] is not None:
                fts_rowids.append(row["fts_rowid"])
        deltas = _SumDeltas()

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
            self._mark_fts_scrub("message_chunks_fts")
        for start in range(0, len(chunk_ids), batch_size):
            str_batch = chunk_ids[start : start + batch_size]
            placeholders = ",".join(["?"] * len(str_batch))
            if track_sums:
                for vec_row in cur.execute(
                    f"SELECT chunk_id, embedding FROM message_chunks_vec "  # nosec B608
                    f"WHERE chunk_id IN ({placeholders})",
                    str_batch,
                ).fetchall():
                    deltas.add(row_threads[vec_row[0]], vec_row[1], -1)
            cur.execute(
                f"DELETE FROM message_chunks_vec WHERE chunk_id IN ({placeholders})",  # nosec B608
                str_batch,
            )
        if deltas:
            self._apply_thread_sum_deltas(cur, deltas)

    def _delete_chunks_for_message(self, cur: sqlite3.Cursor, claimant_id: str) -> None:
        """Drop every chunk row + FTS + vec entry for ``claimant_id``.

        Internal helper used inside an enclosing transaction by
        ``_remove_message_row`` and the reconciler's reap path.
        """
        rows = cur.execute(
            "SELECT chunk_id, fts_rowid, thread_id FROM message_chunks WHERE claimant_id = ?",
            (claimant_id,),
        ).fetchall()
        self._delete_chunks_in_batches(cur, rows)
        cur.execute("DELETE FROM message_chunks WHERE claimant_id = ?", (claimant_id,))

    def _delete_chunks_for_thread(self, cur: sqlite3.Cursor, thread_id: str) -> None:
        """Drop every chunk row + FTS + vec entry for ``thread_id``.

        Used when a thread is deleted in its entirety (last message
        reaped). The per-message helper would also work in a loop, but
        a single thread-id query is cheaper and matches the cascade
        semantics of ``delete_thread_completely``.
        """
        rows = cur.execute(
            "SELECT chunk_id, fts_rowid, thread_id FROM message_chunks WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()
        # The thread's sums row goes with it.
        self._delete_chunks_in_batches(cur, rows, track_sums=False)
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
        """The thread holding a message with the bare ``message_id``.

        Every claimant of one Message-ID joins the thread the first
        claimant was filed in (the threader looks the Message-ID up here
        before anything else), so any of their rows answers; the lowest
        claimant ID is taken so the answer does not depend on row order.
        """
        row = self._conn.execute(
            "SELECT thread_id FROM message_thread_map WHERE message_id = ? "
            "ORDER BY claimant_id LIMIT 1",
            (message_id,),
        ).fetchone()
        return row["thread_id"] if row else None

    @_synchronized
    def thread_has_assessed_correspondents(
        self, thread_id: str, authors: list[str], recipients: list[str]
    ) -> bool:
        """Whether ``thread_id`` has a message assessed safe
        (``sender_ambiguous = 0``) carrying one of ``authors`` and one
        (possibly another such message) carrying one of ``recipients``,
        in any role (#1144). Both lists are canonical addresses. The
        subject fallback trusts only this evidence: an ambiguous message
        (1) or one not yet assessed (NULL) contributes nothing. Each
        list is bound as one JSON parameter, so a long recipient list
        stays under SQLite's variable limit."""
        if not authors or not recipients:
            return False
        leg = (
            "EXISTS (SELECT 1 FROM message_participants p "
            "JOIN messages m ON m.claimant_id = p.claimant_id "
            "WHERE m.thread_id = ? AND m.sender_ambiguous = 0 "
            "AND p.address IN (SELECT value FROM json_each(?)))"
        )
        row = self._conn.execute(
            f"SELECT {leg} AND {leg}",  # nosec B608 -- constant SQL, values bound
            (thread_id, json.dumps(authors), thread_id, json.dumps(recipients)),
        ).fetchone()
        return bool(row[0])

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
    def queue_redate_untried(self, *, filepath: str, due_iso: str) -> None:
        """Set a queued, never-tried row's due time (#699). Rows with an
        attempt, an error or a parked stage keep theirs."""
        self._conn.execute(
            "UPDATE indexing_jobs SET next_attempt_at = ? "
            "WHERE filepath = ? AND status = 'queued' AND attempts = 0 "
            "AND last_error_class IS NULL AND last_stage IS NULL",
            (due_iso, filepath),
        )
        self._conn.commit()

    @_synchronized
    def queue_enqueue(
        self,
        *,
        filepath: str,
        reason: str,
        status: str,
        now_iso: str,
        due_iso: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO indexing_jobs
                (filepath, reason, status, attempts,
                 last_error, last_stage, last_error_class,
                 created_at, updated_at, next_attempt_at)
            VALUES (?, ?, ?, 0, NULL, NULL, NULL, ?, ?, ?)
            """,
            (filepath, reason, status, now_iso, now_iso, due_iso or now_iso),
        )
        self._conn.commit()

    @_synchronized
    def queue_enqueue_reparse(self) -> int:
        """Run ``REPARSE_ENQUEUE_SQL`` (the statement a migration ends
        with) and return how many jobs it queued."""
        cur = self._conn.execute(REPARSE_ENQUEUE_SQL)
        self._conn.commit()
        return cur.rowcount

    @_synchronized
    def queue_fetch_due_batch(
        self, status: str, now_iso: str, limit: int, *, reparse_turn: bool = False
    ) -> list[sqlite3.Row]:
        """Return up to ``limit`` distinct due ``status`` rows, sharing the
        batch between foreground rows and ``reparse`` rows (#1142).

        Foreground rows (every reason but ``reparse``: fresh mail,
        recovery, rescans, re-extraction) come first, so a reparse
        backlog does not hold them back. When both classes are due and
        ``limit > 1``, one slot is kept for the oldest due reparse row,
        so a reparse still advances under a sustained foreground
        backlog. Capacity one class leaves unused goes to the other, so
        no slot is left empty while rows are due. At ``limit == 1``,
        ``reparse_turn`` gives the slot to the reparse row when both are
        due (``IndexingQueue.claim_batch`` alternates it). Each class
        keeps its due order, and foreground rows are returned first.

        Two SELECTs under the one lock, on disjoint classes, so the
        batched indexer's gather phase picks up distinct rows; the claim
        has no in-flight tracking, so rows stay due until the caller
        marks them.
        """
        query = """
            SELECT filepath, reason, status, attempts,
                   last_error, last_stage,
                   created_at, updated_at, next_attempt_at
            FROM indexing_jobs
            WHERE status = ? AND next_attempt_at <= ? AND (reason = ?) = ?
            ORDER BY next_attempt_at ASC
            LIMIT ?
            """
        reparse = self._conn.execute(query, (status, now_iso, REASON_REPARSE, 1, limit)).fetchall()
        reserved = min(1, len(reparse)) if limit > 1 or reparse_turn else 0
        foreground = self._conn.execute(
            query, (status, now_iso, REASON_REPARSE, 0, limit - reserved)
        ).fetchall()
        return foreground + reparse[: limit - len(foreground)]

    @_synchronized
    def queue_delete(self, filepath: str) -> None:
        self._conn.execute("DELETE FROM indexing_jobs WHERE filepath = ?", (filepath,))
        self._conn.commit()

    @_synchronized
    def queue_get_attempts_and_stage(self, filepath: str) -> tuple[int, str | None] | None:
        """A job's attempts and last stage, or ``None`` with no row."""
        row = self._conn.execute(
            "SELECT attempts, last_stage FROM indexing_jobs WHERE filepath = ?", (filepath,)
        ).fetchone()
        return (int(row["attempts"]), row["last_stage"]) if row else None

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
        # Inside ``transaction()`` the write commits or rolls back with
        # the caller's: an extraction continuation commits with the
        # pass's results (#1236).
        if self._transaction_depth == 0:
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

    @_synchronized
    def queue_heartbeat_counts(
        self,
        *,
        now_iso: str,
        permission_stage: str,
        permission_deferred_error: str,
        trashed_stage: str,
        extract_stage: str,
        extraction_deferred_error: str,
        reparse_reason: str,
    ) -> tuple[dict[str, int], str | None]:
        """Rows per heartbeat bucket and the earliest due time among due
        queued rows, in one pass over ``indexing_jobs`` (see
        ``IndexingQueue.heartbeat_counts``)."""
        rows = self._conn.execute(
            """
            SELECT CASE
                       WHEN status = 'dead' THEN 'dead'
                       WHEN last_stage = :trashed THEN 'parked_trashed'
                       WHEN last_stage = :perm_stage AND last_error = :perm_deferred
                           THEN 'deferred_permission'
                       WHEN last_stage = :extract_stage AND last_error = :extract_deferred
                           THEN 'extraction_deferred'
                       WHEN last_error IS NULL THEN 'pending'
                       ELSE 'retrying'
                   END AS bucket,
                   COUNT(*) AS n,
                   SUM(reason = :reparse) AS reparse,
                   MIN(CASE WHEN status = 'queued' AND next_attempt_at <= :now
                            THEN next_attempt_at END) AS oldest_due
            FROM indexing_jobs
            GROUP BY bucket
            """,
            {
                "trashed": trashed_stage,
                "perm_stage": permission_stage,
                "perm_deferred": permission_deferred_error,
                "extract_stage": extract_stage,
                "extract_deferred": extraction_deferred_error,
                "now": now_iso,
                "reparse": reparse_reason,
            },
        ).fetchall()
        counts = {row["bucket"]: int(row["n"]) for row in rows}
        # Reparse jobs across the buckets (#1078): the queued ones that can
        # drain, the ones parked as trashed (they drain only if restored,
        # #1331), then dead.
        reparse = {row["bucket"]: int(row["reparse"]) for row in rows}
        counts["reparse"] = sum(
            n for bucket, n in reparse.items() if bucket not in ("dead", "parked_trashed")
        )
        counts["reparse_parked_trashed"] = reparse.get("parked_trashed", 0)
        counts["reparse_dead"] = reparse.get("dead", 0)
        due = [row["oldest_due"] for row in rows if row["oldest_due"] is not None]
        return counts, min(due) if due else None

    # -------------------------------------------------------------------------
    # Reconciliation support — filepath tracking, tombstones, thread rebuild
    # -------------------------------------------------------------------------

    @_synchronized
    def iter_message_map(self) -> list[sqlite3.Row]:
        """Return every (claimant_id, message_id, thread_id, filepath) row for sweeping."""
        return self._conn.execute(
            "SELECT claimant_id, message_id, thread_id, filepath FROM message_thread_map"
        ).fetchall()

    @_synchronized
    def find_identical_copies(self, claimant_ids: list[str]) -> dict[str, list[str]]:
        """Other indexed paths holding the same bytes as each claimant.

        Byte-identical files share one claimant ID, and the mapping keeps
        only the last path indexed; ``indexed_files`` keeps every path
        (#1102). Returns ``claimant_id -> [filepath, ...]`` (path order)
        for the paths whose ``content_hash`` matches the claimant's and
        that are not its mapped path.

        Two statements, whatever the count: the claimants' hashes by
        primary key, then one pass over ``indexed_files`` (which has no
        ``content_hash`` index) against that set. A join on the hash
        would rescan ``indexed_files`` once per claimant.
        """
        if not claimant_ids:
            return {}
        wanted = self._conn.execute(
            "SELECT m.claimant_id, m.filepath, msg.content_hash "
            "FROM message_thread_map m "
            "JOIN messages msg ON msg.claimant_id = m.claimant_id "
            "WHERE m.claimant_id IN (SELECT value FROM json_each(?)) "
            "AND msg.content_hash IS NOT NULL",
            (json.dumps(claimant_ids),),
        ).fetchall()
        # The hash covers the raw bytes, Message-ID included, so it names
        # one claimant.
        by_hash = {row["content_hash"]: row for row in wanted}
        if not by_hash:
            return {}
        rows = self._conn.execute(
            "SELECT filepath, content_hash FROM indexed_files "
            "WHERE content_hash IN (SELECT value FROM json_each(?)) "
            "ORDER BY filepath",
            (json.dumps(list(by_hash)),),
        ).fetchall()
        copies: dict[str, list[str]] = {}
        for row in rows:
            owner = by_hash[row["content_hash"]]
            if row["filepath"] != owner["filepath"]:
                copies.setdefault(owner["claimant_id"], []).append(row["filepath"])
        return copies

    @_synchronized
    def remap_to_identical_copy(
        self, old_path: str, new_path: str, *, folder: str | None = None
    ) -> bool:
        """Point a message whose file is gone at a byte-identical copy.

        Returns False, changing nothing, unless ``old_path`` is still a
        mapped path and ``new_path`` still exists, checked inside the
        transaction: the watcher may have moved the mapping or renamed the
        copy since the caller resolved it, and a rename that lands after
        the commit carries the new mapping along with it.

        A rename (``update_filepath``) carries the gone path's tombstone
        and queue row to ``new_path``. The copy is a file of its own and
        can already hold either (#1102). Its tombstone describes the file
        that still exists, so it is kept and the gone path's dropped, but
        its grace period never starts before the message lost its last
        live path: of two tombstones the later mark is kept, and when the
        gone path had none (the message was live through it) the copy's
        restarts now. Of two queue rows the runnable one is kept: the
        copy's, unless it is dead and the gone path's is not, since both
        index the same bytes.
        """
        with self.transaction():
            cur = self._conn.cursor()
            if (
                cur.execute(
                    "SELECT 1 FROM message_thread_map WHERE filepath = ?", (old_path,)
                ).fetchone()
                is None
                or not Path(new_path).exists()
            ):
                return False
            # The later of the two marks, or now when the gone path had none.
            cur.execute(
                "UPDATE pending_deletions SET marked_at = COALESCE("
                "(SELECT MAX(marked_at) FROM pending_deletions WHERE filepath IN (?, ?) "
                "AND EXISTS (SELECT 1 FROM pending_deletions WHERE filepath = ?)), ?) "
                "WHERE filepath = ?",
                (old_path, new_path, old_path, datetime.now(UTC).isoformat(), new_path),
            )
            cur.execute(
                "DELETE FROM pending_deletions WHERE filepath = ? "
                "AND EXISTS (SELECT 1 FROM pending_deletions WHERE filepath = ?)",
                (old_path, new_path),
            )
            # A dead row on the copy gives way to a runnable one on the gone
            # path, which the rename below then moves onto the copy;
            # otherwise the copy's row stays and the gone path's goes.
            cur.execute(
                "DELETE FROM indexing_jobs WHERE filepath = ? AND status = 'dead' "
                "AND EXISTS (SELECT 1 FROM indexing_jobs "
                "WHERE filepath = ? AND status != 'dead')",
                (new_path, old_path),
            )
            cur.execute(
                "DELETE FROM indexing_jobs WHERE filepath = ? "
                "AND EXISTS (SELECT 1 FROM indexing_jobs WHERE filepath = ?)",
                (old_path, new_path),
            )
            self.update_filepath(old_path, new_path, folder=folder)
        return True

    @staticmethod
    def _unmark_paths(cur: sqlite3.Cursor, filepaths: list[str]) -> None:
        """Drop the ``indexed_files`` rows of ``filepaths``: the other paths
        holding the bytes of a message being reaped (#1102), looked up once
        per reap pass with ``find_identical_copies``.

        The reap runs only once the sweep found no surviving copy, so any
        such path is gone or trashed now. Left marked, a copy that comes
        back (a transient mount or folder outage) would be skipped by every
        Maildir walk with no mapping left to repair it; unmarked, the walk
        re-indexes it. Their ``indexing_jobs`` rows go too: the walk
        skips a path with a dead job, so a returning copy would otherwise
        wait for ``make requeue-dead``. Primary-key deletes; the caller
        owns the transaction.
        """
        if filepaths:
            paths = json.dumps(filepaths)
            for table in ("indexed_files", "indexing_jobs"):
                # Only fixed table names are interpolated; values are bound.
                cur.execute(
                    f"DELETE FROM {table} "  # nosec B608
                    "WHERE filepath IN (SELECT value FROM json_each(?))",
                    (paths,),
                )

    @_synchronized
    def find_message_entry_by_filepath(self, filepath: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT claimant_id, message_id, thread_id, filepath FROM message_thread_map "
            "WHERE filepath = ?",
            (filepath,),
        ).fetchone()

    @_synchronized
    def keep_persisted_first_indexed_at(self, msg) -> None:
        """Give ``msg`` the ``first_indexed_at`` stored for it, if any.

        The parser sets it to the parse time, so every reprocess (a
        rename seen while the indexer was down, a retry, a reap rebuild)
        would otherwise move the ordering position of a message with no
        send or delivery date (#297), and its thread's span with it.
        Mirrors what ``_write_message_record`` stores on conflict: for a
        row not yet assessed (before v8) that is now undated, the old
        ``sent_at``, which held the time it was first indexed (#1080).
        """
        row = self._conn.execute(
            "SELECT sent_at, sent_at_status, first_indexed_at FROM messages WHERE claimant_id = ?",
            (msg.claimant_id,),
        ).fetchone()
        if row is None:
            return
        if row["sent_at_status"] is None and msg.date is None:
            msg.first_indexed_at = datetime.fromisoformat(row["sent_at"])
        else:
            msg.first_indexed_at = datetime.fromisoformat(row["first_indexed_at"])

    @_synchronized
    def count_total_messages(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM message_thread_map").fetchone()
        return int(row[0]) if row else 0

    @_synchronized
    def get_active_vector_generation(self) -> dict | None:
        """The active ``vector_generations`` row, its calibration vector
        unpacked to floats; ``None`` before the indexer records one.

        Raises on a database from before the table existed: the index
        must be rebuilt, since nothing records which embedder wrote it.
        """
        if (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vector_generations'"
            ).fetchone()
            is None
        ):
            raise RuntimeError(
                "The index predates the embedder identity record. Stop the stack, "
                "wipe the sqlite-volume and let the indexer rebuild the index from "
                "Maildir."
            )
        row = self._conn.execute(
            "SELECT * FROM vector_generations WHERE status = 'active'"
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        blob = record["calibration_vector"]
        record["calibration_vector"] = list(struct.unpack(f"{len(blob) // 4}f", blob))
        return record

    @_synchronized
    def record_vector_generation(
        self,
        *,
        provider: str,
        endpoint: str,
        model: str,
        calibration_sha256: str,
        calibration_vector: list[float],
    ) -> int:
        """Insert the active generation; returns its ``generation_id``.

        The partial unique index refuses a second active row.
        """
        with self.transaction():
            cur = self._conn.execute(
                "INSERT INTO vector_generations (provider, endpoint, model, dimensions, "
                "calibration_sha256, calibration_vector, created_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
                (
                    provider,
                    endpoint,
                    model,
                    len(calibration_vector),
                    calibration_sha256,
                    sqlite_vec.serialize_float32(calibration_vector),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return int(cur.lastrowid or 0)

    @_synchronized
    def get_thread_messages(self, thread_id: str) -> list[sqlite3.Row]:
        """All (claimant_id, message_id, filepath) rows for a thread, used to rebuild it."""
        return self._conn.execute(
            "SELECT claimant_id, message_id, filepath FROM message_thread_map WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()

    @_synchronized
    def set_body_complete(self, claimant_id: str, complete: bool) -> None:
        """Store whether the message's body chunks are complete (#1086).

        Phase 2c calls it inside the transaction that commits the body
        chunks, so the flag and the chunks it describes land or roll
        back together; ``_write_message_record`` resets it to NULL first.
        """
        with self.transaction():
            self._conn.execute(
                "UPDATE messages SET body_complete = ? WHERE claimant_id = ?",
                (int(complete), claimant_id),
            )

    def _write_message_record(self, cur: sqlite3.Cursor, msg, thread_id: str) -> None:
        """Upsert ``msg``'s ``messages`` row and replace its participants.

        Runs inside ``upsert_thread``'s transaction, after the
        ``message_thread_map`` row it references. ``ON CONFLICT DO
        UPDATE`` (never ``REPLACE``) keeps the row in place, so the
        participant cascade only fires when the message itself is removed.
        """
        state = message_state(msg.filepath)
        # ``from_addrs`` holds every author; ``from_addr`` alone for callers
        # that build a Message by hand.
        authors = msg.from_addrs or [msg.from_addr]
        roles = [("from", authors), ("to", msg.to_addrs), ("cc", msg.cc_addrs)]
        # Every distinct display name per (role, address) (#1140), within
        # the parser's per-message budget, and whether that budget kept
        # them all. A Message built without parsing derives both here.
        names = msg.participant_names
        complete = msg.participant_names_complete
        if names is None:
            dropped: Counter[str] = Counter()
            names = participant_names(roles, dropped)
            complete = not dropped["participant_names"]
        caps_json = _caps_json(msg.parse_caps)
        cur.execute(
            """
            INSERT INTO messages
                (claimant_id, message_id, thread_id, filepath, folder, subject, sent_at,
                 sent_at_status, occurred_at, in_reply_to, references_json, has_attachments,
                 size_bytes, content_hash, indexed_at, first_indexed_at, seen, flagged,
                 replied, sender_ambiguous, participant_names_complete, subject_complete,
                 from_addresses_complete, to_addresses_complete, cc_addresses_complete,
                 attachments_manifest_complete, body_complete, caps_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, NULL, ?)
            ON CONFLICT(claimant_id) DO UPDATE SET
                thread_id       = excluded.thread_id,
                filepath        = excluded.filepath,
                folder          = excluded.folder,
                subject         = excluded.subject,
                sent_at         = excluded.sent_at,
                sent_at_status  = excluded.sent_at_status,
                -- Kept from the first insert (#297). A row from before
                -- v8 (status not yet assessed) that is now undated held
                -- that time in ``sent_at``, the old parser's fallback
                -- (#1080); ``keep_persisted_first_indexed_at`` mirrors
                -- this for the in-memory message.
                first_indexed_at = CASE
                    WHEN messages.sent_at_status IS NULL AND excluded.sent_at IS NULL
                    THEN messages.sent_at ELSE messages.first_indexed_at END,
                occurred_at     = excluded.occurred_at,
                in_reply_to     = excluded.in_reply_to,
                references_json = excluded.references_json,
                has_attachments = excluded.has_attachments,
                size_bytes      = excluded.size_bytes,
                content_hash    = excluded.content_hash,
                indexed_at      = excluded.indexed_at,
                seen            = excluded.seen,
                flagged         = excluded.flagged,
                replied         = excluded.replied,
                sender_ambiguous = excluded.sender_ambiguous,
                participant_names_complete = excluded.participant_names_complete,
                subject_complete = excluded.subject_complete,
                from_addresses_complete = excluded.from_addresses_complete,
                to_addresses_complete = excluded.to_addresses_complete,
                cc_addresses_complete = excluded.cc_addresses_complete,
                attachments_manifest_complete = excluded.attachments_manifest_complete,
                -- Not known until phase 2c commits this parse's chunks.
                body_complete = NULL,
                caps_json = excluded.caps_json
            """,
            (
                msg.claimant_id,
                msg.message_id,
                thread_id,
                msg.filepath,
                msg.folder,
                msg.subject,
                msg.date.isoformat() if msg.date is not None else None,
                msg.date_status,
                msg.occurred_at.isoformat() if msg.occurred_at is not None else None,
                msg.in_reply_to,
                json.dumps(msg.references),
                int(bool(msg.has_attachments)),
                msg.size,
                msg.content_hash,
                datetime.now(UTC).isoformat(),
                msg.first_indexed_at.isoformat(),
                int(state.seen),
                int(state.flagged),
                int(state.replied),
                int(msg.sender_ambiguous),
                int(complete),
                int(msg.subject_complete),
                int(msg.from_addresses_complete),
                int(msg.to_addresses_complete),
                int(msg.cc_addresses_complete),
                int(msg.attachments_manifest_complete),
                caps_json,
            ),
        )
        # Cascades to the message's ``message_participant_names`` rows.
        cur.execute("DELETE FROM message_participants WHERE claimant_id = ?", (msg.claimant_id,))
        # Entity writes are bounded per message (a crafted header can list
        # thousands of recipients) by distinct address, so a repeated
        # address cannot spend the budget; authors come first, so the
        # senders keep their entities. Participant rows are written for
        # everyone.
        entity_addresses: set[str] = set()
        for role, values in roles:
            for value in values:
                address = canonical_addr(value or "")
                if not address:
                    continue
                name = parseaddr(value)[0].strip() or None
                cur.execute(
                    "INSERT OR IGNORE INTO message_participants "
                    "(claimant_id, role, address, name) VALUES (?, ?, ?, ?)",
                    (msg.claimant_id, role, address, name),
                )
                if (
                    address not in entity_addresses
                    and len(entity_addresses) < MAX_ENTITY_PARTICIPANTS_PER_MESSAGE
                ):
                    entity_addresses.add(address)
                    self._write_entity(cur, address)
        # Each stored name of an entity's address is one of its aliases.
        cur.executemany(
            "INSERT INTO message_participant_names (claimant_id, role, address, name) "
            "VALUES (?, ?, ?, ?)",
            [(msg.claimant_id, role, address, name) for role, address, name in names],
        )
        cur.executemany(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
            [
                (person_entity_id(address), name)
                for _, address, name in names
                if address in entity_addresses
            ],
        )

    def _write_entity(self, cur: sqlite3.Cursor, address: str) -> None:
        """Record ``address`` as a person entity (with its organization,
        if any), classified by the current authority rules. Its aliases
        are written by the caller. Deterministic IDs and ``ON CONFLICT``
        writes make a reprocess rewrite the same rows. Runs inside
        ``_write_message_record``'s transaction."""
        rules = self._authority_rules
        org_id = None
        domain = organization_domain(address)
        if domain:
            org_id = org_entity_id(domain)
            cur.execute(
                "INSERT INTO entities (entity_id, kind, canonical_key, organization_id, "
                "authority_class, authority_rule) "
                "VALUES (?, 'organization', ?, NULL, ?, ?) ON CONFLICT(entity_id) DO UPDATE SET "
                "authority_class = excluded.authority_class, "
                "authority_rule = excluded.authority_rule",
                (org_id, domain, *rules.classify_domain(domain)),
            )
        person_id = person_entity_id(address)
        cur.execute(
            "INSERT INTO entities (entity_id, kind, canonical_key, organization_id, "
            "authority_class, authority_rule) "
            "VALUES (?, 'person', ?, ?, ?, ?) ON CONFLICT(entity_id) DO UPDATE SET "
            "authority_class = excluded.authority_class, "
            "authority_rule = excluded.authority_rule",
            (person_id, address, org_id, *rules.classify(address)),
        )

    @_synchronized
    def set_authority_rules(self, rules: AuthorityRules) -> int:
        """Use ``rules`` for every later entity write and reclassify every
        existing entity under them in one transaction, so an edited rules
        file takes effect at the next indexer start. Returns the number
        of entities whose class or rule changed. One dictionary lookup
        per entity (plus one per domain label), so cost follows the
        entity count, not the rule count."""
        self._authority_rules = rules
        with self.transaction():
            rows = self._conn.execute(
                "SELECT entity_id, kind, canonical_key, authority_class, authority_rule "
                "FROM entities"
            ).fetchall()
            changes = []
            for row in rows:
                key = row["canonical_key"]
                cls, rule = (
                    rules.classify(key) if row["kind"] == "person" else rules.classify_domain(key)
                )
                if (cls, rule) != (row["authority_class"], row["authority_rule"]):
                    changes.append((cls, rule, row["entity_id"]))
            self._conn.executemany(
                "UPDATE entities SET authority_class = ?, authority_rule = ? WHERE entity_id = ?",
                changes,
            )
        return len(changes)

    @_synchronized
    def set_operator_identity(self, identity: OperatorIdentity | None) -> None:
        """Replace the stored operator addresses, state, count and digest
        in one transaction (#824). ``None`` (no identity file) clears the
        set and records ``unconfigured``. A failure rolls the whole
        replacement back, so readers never see a mixed set."""
        addresses = identity.addresses if identity is not None else frozenset()
        with self.transaction():
            self._conn.execute("DELETE FROM operator_addresses")
            self._conn.executemany(
                "INSERT INTO operator_addresses (address) VALUES (?)",
                [(address,) for address in sorted(addresses)],
            )
            self._conn.execute(
                "INSERT INTO operator_identity (id, state, address_count, address_digest) "
                "VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET state = excluded.state, "
                "address_count = excluded.address_count, "
                "address_digest = excluded.address_digest",
                (
                    "configured" if identity is not None else "unconfigured",
                    len(addresses),
                    identity_digest(addresses),
                ),
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
            # mbsync carries Proton's read / flagged / replied changes as
            # renames, so the state moves with the path, never re-parsed.
            state = message_state(new_path)
            cur.execute(
                "UPDATE messages SET filepath = ?, seen = ?, flagged = ?, replied = ? "
                "WHERE filepath = ?",
                (new_path, int(state.seen), int(state.flagged), int(state.replied), old_path),
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
    def add_pending_deletion(self, filepath: str, claimant_id: str, thread_id: str) -> bool:
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

        Refused when ``message_thread_map`` maps ``claimant_id`` to another
        path (#301): the caller's path is stale because the watcher renamed
        the file meanwhile (a restore, or a move to another folder). The
        reaper matches tombstones by claimant ID, and no sweep revisits the
        old path, so the tombstone would delete the live message. The check
        and the insert are one statement, so a rename cannot land between
        them.
        """
        cur = self._conn.cursor()
        marked_at = datetime.now(UTC).isoformat()
        cur.execute(
            "INSERT OR IGNORE INTO pending_deletions "
            "(filepath, claimant_id, thread_id, marked_at) "
            "SELECT ?, ?, ?, ? WHERE NOT EXISTS ("
            "SELECT 1 FROM message_thread_map WHERE claimant_id = ? AND filepath != ?)",
            (filepath, claimant_id, thread_id, marked_at, claimant_id, filepath),
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
            "SELECT p.filepath, p.claimant_id, p.thread_id, p.marked_at, "
            "m.filepath AS mapped_filepath "
            "FROM pending_deletions p "
            "LEFT JOIN message_thread_map m ON m.claimant_id = p.claimant_id "
            "WHERE p.marked_at <= ? ORDER BY p.marked_at ASC",
            (cutoff_iso,),
        ).fetchall()

    @_synchronized
    def prune_reaped_messages(self, *, now: datetime | None = None) -> int:
        """Delete ``reaped_messages`` records older than
        ``REAPED_RECORD_RETENTION_DAYS`` and return how many went. Runs
        in both retention modes, so records written before a switch to
        archive mode still expire."""
        cutoff = (now or datetime.now(UTC)) - timedelta(days=REAPED_RECORD_RETENTION_DAYS)
        cur = self._conn.execute(
            "DELETE FROM reaped_messages WHERE reaped_at < ?", (cutoff.isoformat(),)
        )
        self._conn.commit()
        return cur.rowcount

    @_synchronized
    def delete_thread_completely(
        self,
        thread_id: str,
        *,
        grace_cutoff: str | None = None,
        copy_paths: Sequence[str] = (),
    ) -> bool:
        """Remove a thread and every derived row. Used when the last message
        in a thread has been reaped.

        Returns False, changing nothing, when a message in the thread is no
        longer tombstoned — the watcher restored it (or a new message
        joined the thread) after the reaper read its tombstones — or, with
        ``grace_cutoff``, has a tombstone newer than it (restored and
        trashed again, so its grace period restarted).

        ``copy_paths`` are other paths holding the bytes of the thread's
        messages; they are unmarked in the same transaction (see
        ``_unmark_paths``).
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
                self._mark_fts_scrub("threads_fts")
            cur.execute("DELETE FROM threads_vec WHERE thread_id = ?", (thread_id,))
            cur.execute("DELETE FROM thread_vector_sums WHERE thread_id = ?", (thread_id,))
            self._delete_chunks_for_thread(cur, thread_id)
            # Walk every message in the thread to drop its attachments
            # rows + FTS shadows. ``claimant_id``-keyed deletes from
            # ``message_thread_map`` happen below; do attachments first
            # so the per-message lookup still finds rows.
            claimant_ids = [
                r["claimant_id"]
                for r in cur.execute(
                    "SELECT claimant_id FROM message_thread_map WHERE thread_id = ?",
                    (thread_id,),
                ).fetchall()
            ]
            for cid in claimant_ids:
                self._delete_attachments_for_message(cur, cid)
            self._unmark_paths(cur, list(copy_paths))
            # Read before the map delete cascades the participant rows away.
            mentions = self._participant_mentions(cur, claimant_ids)
            cur.execute(
                "INSERT OR REPLACE INTO reaped_messages "
                "(claimant_id, message_id, thread_id, reaped_at) "
                "SELECT claimant_id, message_id, thread_id, ? FROM message_thread_map "
                "WHERE thread_id = ?",
                (datetime.now(UTC).isoformat(), thread_id),
            )
            cur.execute("DELETE FROM message_thread_map WHERE thread_id = ?", (thread_id,))
            cur.execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))
            cur.execute("DELETE FROM pending_deletions WHERE thread_id = ?", (thread_id,))
            for fp in filepaths:
                cur.execute("DELETE FROM indexed_files WHERE filepath = ?", (fp,))
                # A job still queued for the file (an embedder outage past
                # the grace window) would re-index it from the kept .eml.
                cur.execute("DELETE FROM indexing_jobs WHERE filepath = ?", (fp,))
            self._prune_orphan_entities(cur, mentions)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return True

    @staticmethod
    def _has_untombstoned_messages(
        cur: sqlite3.Cursor,
        thread_id: str,
        claimant_ids: list[str] | None = None,
        *,
        grace_cutoff: str | None = None,
    ) -> bool:
        """Whether a message of ``thread_id`` (only ``claimant_ids``, when
        given) has no tombstone, or none marked at or before
        ``grace_cutoff``. Read inside the reap transaction, so a restore
        (or a restore and a new tombstone) after the reaper's snapshot is
        seen."""
        # Only fixed SQL fragments are interpolated; values are bound.
        marked = " AND p.marked_at <= ?" if grace_cutoff is not None else ""
        sql = (
            "SELECT 1 FROM message_thread_map m WHERE m.thread_id = ? "  # nosec B608
            "AND NOT EXISTS (SELECT 1 FROM pending_deletions p "
            f"WHERE p.claimant_id = m.claimant_id{marked})"
        )
        params: list[str] = [thread_id]
        if grace_cutoff is not None:
            params.append(grace_cutoff)
        if claimant_ids is not None:
            sql += f" AND m.claimant_id IN ({','.join('?' * len(claimant_ids))})"  # nosec B608
            params.extend(claimant_ids)
        return cur.execute(sql + " LIMIT 1", params).fetchone() is not None

    @_synchronized
    def reap_thread_messages(
        self,
        thread,
        embedding: list[float] | None,
        reaped_claimant_ids: list[str],
        *,
        grace_cutoff: str | None = None,
        copy_paths: Sequence[str] = (),
    ) -> list[str] | None:
        """Atomically rewrite a thread and remove reaped messages.

        All writes happen inside a single ``BEGIN IMMEDIATE`` / commit so
        either the whole reap lands or none of it does: a crash cannot
        leave the thread row reflecting only survivors while
        ``message_thread_map`` and ``pending_deletions`` still hold rows
        for the reaped messages.

        ``embedding`` is the thread's subject fallback, or ``None`` for
        its chunk mean: the survivors' chunk-vector sum, once the reaped
        messages' chunks are subtracted in this transaction (#1356).

        Returns the filepaths that were removed from the index (the files
        themselves stay on disk), or ``None``,
        changing nothing, when a reaped message is no longer tombstoned
        (the watcher restored it after the reaper read its tombstones) or,
        with ``grace_cutoff``, its tombstone is newer than that, or when
        ``embedding`` is ``None`` and no chunk vector survives (the thread
        changed since the caller checked; the next pass embeds the
        fallback).

        ``copy_paths`` are other paths holding the reaped messages' bytes;
        they are unmarked in the same transaction (see ``_unmark_paths``).
        """
        cur = self._conn.cursor()
        removed_filepaths: list[str] = []
        try:
            cur.execute("BEGIN IMMEDIATE")
            if self._has_untombstoned_messages(
                cur, thread.thread_id, reaped_claimant_ids, grace_cutoff=grace_cutoff
            ):
                self._conn.rollback()
                return None
            self._rewrite_thread_row(cur, thread, embedding)
            # The replaced thread row held the reaped messages' words.
            self._mark_fts_scrub("threads_fts")
            mentions = self._participant_mentions(cur, reaped_claimant_ids)
            self._unmark_paths(cur, list(copy_paths))
            for cid in reaped_claimant_ids:
                fp = self._remove_message_row(cur, cid)
                if fp is not None:
                    removed_filepaths.append(fp)
            if embedding is None:
                total, count = self._thread_sums(cur, thread.thread_id)
                if not count:
                    self._conn.rollback()
                    return None
                self._write_thread_vector(
                    cur, thread.thread_id, vector_sums.thread_vector(total, count)
                )
            self._prune_orphan_entities(cur, mentions)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return removed_filepaths

    def _rewrite_thread_row(
        self, cur: sqlite3.Cursor, thread, embedding: list[float] | None
    ) -> None:
        """Replace a thread row and its FTS/vec entries using ``cur``.

        Used by ``reap_thread_messages`` so the rewrite participates in
        its larger transaction. The caller owns ``BEGIN`` / ``COMMIT`` /
        ``ROLLBACK``. With ``embedding`` ``None`` the vec entry is left
        for the caller, which writes the chunk mean once the reaped
        chunks are gone.
        """
        if embedding is not None and len(embedding) != EMBEDDING_DIM:
            raise ValueError(
                f"embedding has {len(embedding)} dims but threads_vec reserves "
                f"{EMBEDDING_DIM}. Check the embedder's output dimension."
            )

        participants_json = json.dumps(_dedupe_by_canonical(thread.participants))
        senders_json = json.dumps(
            _dedupe_by_canonical([m.from_addr for m in thread.messages if m.from_addr])
        )
        message_ids_json = json.dumps([m.claimant_id for m in thread.messages])
        snippet = thread.snippet()
        date_first = thread.date_first.isoformat()
        date_last = thread.date_last.isoformat()
        has_attachments = int(any(m.has_attachments for m in thread.messages))
        body = thread.build_body_text()
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
            (
                m.subject
                for m in sorted(thread.messages, key=lambda m: m.effective_date)
                if m.subject
            ),
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

        # Survivors only: the reaped messages' rows are removed after
        # this rewrite, in the same transaction (#303). Oldest first,
        # as the upsert path reads them; ``fts_subject_text`` caps the
        # rows and characters it examines.
        fts_subject = fts_subject_text(
            thread.subject,
            (m.subject for m in sorted(thread.messages, key=lambda m: m.effective_date)),
        )
        self._replace_fts_row(cur, thread.thread_id, fts_subject, participants_json, body)

        if embedding is not None:
            self._write_thread_vector(cur, thread.thread_id, embedding)

    @staticmethod
    def _participant_mentions(
        cur: sqlite3.Cursor, claimant_ids: list[str]
    ) -> set[tuple[str, str | None]]:
        """The given messages' participants that own a person entity, as
        distinct ``(address, alias)`` pairs, one per display name the
        message stored for the address (``message_participant_names``);
        ``alias`` is ``None`` when a name is not one of the entity's
        aliases, or the address has no stored name. Read
        before the rows cascade away, so ``_prune_orphan_entities`` knows
        which entities and aliases the reap may have orphaned.

        Filtering on the entity and alias in this one query keeps the
        prune's per-address statements to addresses that own an entity:
        a crafted header can list thousands of recipients, but only
        ``MAX_ENTITY_PARTICIPANTS_PER_MESSAGE`` of them get one."""
        mentions: set[tuple[str, str | None]] = set()
        for cid in claimant_ids:
            mentions.update(
                (r["address"], r["alias"])
                for r in cur.execute(
                    "SELECT DISTINCT p.address, a.alias FROM message_participants p "
                    "JOIN entities e ON e.entity_id = ? || p.address "
                    "LEFT JOIN message_participant_names n "
                    "ON n.claimant_id = p.claimant_id AND n.role = p.role "
                    "AND n.address = p.address "
                    "LEFT JOIN entity_aliases a "
                    "ON a.entity_id = e.entity_id AND a.alias = n.name "
                    "WHERE p.claimant_id = ?",
                    (PERSON_PREFIX, cid),
                )
            )
        return mentions

    @staticmethod
    def _prune_orphan_entities(cur: sqlite3.Cursor, mentions: set[tuple[str, str | None]]) -> None:
        """Delete the entities and aliases that only the reaped messages
        mentioned (#464), after their participant rows are gone. The
        caller owns the transaction.

        Only the entities behind ``mentions`` are examined, each with
        indexed lookups, so the work follows the reaped messages'
        participants, not the size of ``entities``:

        - a person with no participant row left is deleted, and its
          aliases with it (``ON DELETE CASCADE``);
        - a surviving person loses each alias no remaining message
          stores as a display name of its address
          (``message_participant_names``);
        - an organization of a deleted person is deleted once no person
          belongs to it.
        """
        deleted: set[str] = set()
        orgs: set[str] = set()
        for address in sorted({address for address, _ in mentions}):
            if cur.execute(
                "SELECT 1 FROM message_participants WHERE address = ? LIMIT 1", (address,)
            ).fetchone():
                continue
            person_id = person_entity_id(address)
            row = cur.execute(
                "SELECT organization_id FROM entities WHERE entity_id = ?", (person_id,)
            ).fetchone()
            if row is None:
                # Past the per-message entity cap: never had an entity.
                continue
            if row["organization_id"] is not None:
                orgs.add(row["organization_id"])
            cur.execute("DELETE FROM entities WHERE entity_id = ?", (person_id,))
            deleted.add(address)
        for address, name in sorted(
            (a, n) for a, n in mentions if n is not None and a not in deleted
        ):
            cur.execute(
                "DELETE FROM entity_aliases WHERE entity_id = ? AND alias = ? "
                "AND NOT EXISTS (SELECT 1 FROM message_participant_names "
                "WHERE address = ? AND name = ?)",
                (person_entity_id(address), name, address, name),
            )
        for org_id in sorted(orgs):
            cur.execute(
                "DELETE FROM entities WHERE entity_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM entities WHERE organization_id = ?)",
                (org_id, org_id),
            )

    def _remove_message_row(self, cur: sqlite3.Cursor, claimant_id: str) -> str | None:
        """Remove a message's map / indexed_files / tombstone / chunk /
        attachment rows using ``cur``. Returns the message's filepath
        (for optional on-disk cleanup), or ``None`` if no such message
        was tracked. Used by ``reap_thread_messages``; the caller owns
        the enclosing transaction.
        """
        row = cur.execute(
            "SELECT filepath FROM message_thread_map WHERE claimant_id = ?",
            (claimant_id,),
        ).fetchone()
        if row is None:
            return None
        filepath = row["filepath"]
        # Per-message chunk cascade. ``_delete_chunks_for_message``
        # drops both body-chunk and attachment-chunk rows because both
        # carry this claimant_id.
        self._delete_chunks_for_message(cur, claimant_id)
        # Attachment occurrences for this message, and the cached
        # extraction of any payload no other message still carries.
        self._delete_attachments_for_message(cur, claimant_id)
        # Identifiers only, so a later lookup can report the message as
        # reaped (see ``reaped_messages`` in the schema).
        cur.execute(
            "INSERT OR REPLACE INTO reaped_messages "
            "(claimant_id, message_id, thread_id, reaped_at) "
            "SELECT claimant_id, message_id, thread_id, ? FROM message_thread_map "
            "WHERE claimant_id = ?",
            (datetime.now(UTC).isoformat(), claimant_id),
        )
        cur.execute("DELETE FROM message_thread_map WHERE claimant_id = ?", (claimant_id,))
        cur.execute("DELETE FROM indexed_files WHERE filepath = ?", (filepath,))
        cur.execute("DELETE FROM pending_deletions WHERE filepath = ?", (filepath,))
        # A job still queued for the file would re-index it from the kept
        # .eml (see ``delete_thread_completely``).
        cur.execute("DELETE FROM indexing_jobs WHERE filepath = ?", (filepath,))
        return filepath
