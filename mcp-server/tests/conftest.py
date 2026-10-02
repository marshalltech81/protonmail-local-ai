"""Shared fixtures for mcp-server tests."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from email.utils import parseaddr
from pathlib import Path

import pytest
import sqlite_vec
from src.lib.sqlite import Database


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-baseline",
        action="store_true",
        default=False,
        help="Rewrite tests/baseline/snapshot.json from the current retrieval results.",
    )


def _build_schema(conn: sqlite3.Connection) -> None:
    """Build the schema the MCP reader depends on.

    Mirrors the indexer's tables (``threads`` + ``threads_fts`` +
    ``threads_vec``, ``message_thread_map``, ``message_chunks`` family,
    ``attachments`` family) with toy 4-dim embedding columns so test
    vectors stay readable.
    """
    conn.executescript(
        """
        CREATE TABLE threads (
            thread_id       TEXT PRIMARY KEY,
            subject         TEXT NOT NULL,
            participants    TEXT NOT NULL,
            senders         TEXT NOT NULL DEFAULT '[]',
            folder          TEXT NOT NULL,
            date_first      TEXT NOT NULL,
            date_last       TEXT NOT NULL,
            message_ids     TEXT NOT NULL,
            snippet         TEXT,
            has_attachments INTEGER DEFAULT 0,
            body_text       TEXT,
            fts_rowid       INTEGER,
            display_subject TEXT
        );

        -- Per-message rows are keyed by claimant ID (Message-ID plus a
        -- short hash of the file's bytes); see ``claimant_of``.
        CREATE TABLE message_thread_map (
            claimant_id TEXT PRIMARY KEY,
            message_id  TEXT NOT NULL,
            thread_id   TEXT NOT NULL,
            filepath    TEXT NOT NULL
        );

        -- Per-message records behind query_messages and find_contact.
        CREATE TABLE messages (
            claimant_id     TEXT PRIMARY KEY,
            message_id      TEXT NOT NULL,
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
            indexed_at      TEXT NOT NULL
        );

        -- The indexer's ``messages`` indexes, so query plans match.
        CREATE INDEX idx_messages_message ON messages(message_id, claimant_id);
        CREATE INDEX idx_messages_message_sent
            ON messages(message_id, sent_at, claimant_id);
        CREATE INDEX idx_messages_thread_sent ON messages(thread_id, sent_at);
        CREATE INDEX idx_messages_folder_sent ON messages(folder, sent_at);
        CREATE INDEX idx_messages_sent ON messages(sent_at);
        CREATE INDEX idx_messages_filepath ON messages(filepath);

        CREATE TABLE message_participants (
            claimant_id TEXT NOT NULL,
            role        TEXT NOT NULL CHECK (role IN ('from', 'to', 'cc')),
            address     TEXT NOT NULL,
            name        TEXT,
            PRIMARY KEY (claimant_id, role, address)
        );
        CREATE INDEX idx_message_participants_address
            ON message_participants(address, role);

        CREATE VIRTUAL TABLE threads_fts USING fts5(
            subject, participants, body,
            content='',
            contentless_delete=1,
            tokenize='porter unicode61'
        );

        CREATE VIRTUAL TABLE threads_vec USING vec0(
            thread_id TEXT PRIMARY KEY,
            embedding FLOAT[4]
        );

        -- Per-message chunks. Tests use the same toy 4-dim embedding
        -- space as the thread vec table so synthetic vectors like
        -- ``[1, 0, 0, 0]`` work uniformly across both lanes.
        --
        -- ``message_date`` mirrors the indexer schema: NOT NULL, and
        -- the ordering key for ``get_recent_chunks_for_thread``.
        CREATE TABLE message_chunks (
            chunk_id        TEXT PRIMARY KEY,
            claimant_id     TEXT NOT NULL,
            thread_id       TEXT NOT NULL,
            chunk_index     INTEGER NOT NULL,
            text            TEXT NOT NULL,
            char_start      INTEGER NOT NULL,
            char_end        INTEGER NOT NULL,
            token_est       INTEGER NOT NULL,
            chunked_at      TEXT NOT NULL,
            fts_rowid       INTEGER,
            attachment_id   TEXT,
            message_date    TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE message_chunks_fts USING fts5(
            text,
            content='',
            contentless_delete=1,
            tokenize='porter unicode61'
        );

        CREATE VIRTUAL TABLE message_chunks_vec USING vec0(
            chunk_id TEXT PRIMARY KEY,
            embedding FLOAT[4]
        );

        CREATE TABLE attachments (
            attachment_occurrence_id TEXT PRIMARY KEY,
            claimant_id               TEXT NOT NULL,
            attachment_id             TEXT NOT NULL,
            thread_id                 TEXT NOT NULL,
            filename                  TEXT NOT NULL,
            content_type              TEXT NOT NULL,
            size_bytes                INTEGER NOT NULL,
            seen_at                   TEXT NOT NULL,
            fts_rowid                 INTEGER
        );

        CREATE VIRTUAL TABLE attachments_fts USING fts5(
            filename,
            content_type,
            content='',
            contentless_delete=1,
            tokenize='porter unicode61'
        );

        -- Per-content-hash extracted-text cache. ``search_attachments``
        -- LEFT JOINs this for extraction status + a text snippet, so
        -- the table must exist on every fixture DB even when empty.
        CREATE TABLE attachment_extractions (
            attachment_id     TEXT PRIMARY KEY,
            extraction_status TEXT NOT NULL,
            extractor         TEXT,
            extracted_text    TEXT,
            extraction_error  TEXT,
            extracted_at      TEXT NOT NULL
        );
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
        CREATE TABLE ingestion_state (
            id                 INTEGER PRIMARY KEY CHECK (id = 1),
            sync_completed_at  TEXT,
            sync_interval_secs INTEGER,
            indexer_seen_at    TEXT NOT NULL
        );

        -- Deterministic entities (indexer ``_run_entity_schema_script``).
        CREATE TABLE entities (
            entity_id       TEXT PRIMARY KEY,
            kind            TEXT NOT NULL CHECK (kind IN ('person', 'organization')),
            canonical_key   TEXT NOT NULL,
            organization_id TEXT REFERENCES entities(entity_id),
            authority_class TEXT NOT NULL DEFAULT 'unclassified',
            authority_rule  TEXT
        );
        CREATE INDEX idx_entities_organization ON entities(organization_id);
        CREATE INDEX idx_entities_authority ON entities(authority_class);
        CREATE TABLE entity_aliases (
            entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
            alias     TEXT NOT NULL,
            PRIMARY KEY (entity_id, alias)
        );
        """
    )


def write_ingestion(
    db_path: str,
    *,
    sync_completed_at: str | None = None,
    sync_interval_secs: int | None = None,
    indexer_seen_at: str | None = None,
    jobs: tuple[tuple[str, int, str | None], ...] = (),
) -> None:
    """Write the indexer-owned ``ingestion_state`` row (when
    ``indexer_seen_at`` is given) and ``indexing_jobs`` rows as
    ``(status, attempts, last_error_class)`` into a fixture database."""
    conn = sqlite3.connect(db_path)
    try:
        if indexer_seen_at is not None:
            conn.execute(
                "INSERT INTO ingestion_state VALUES (1, ?, ?, ?)",
                (sync_completed_at, sync_interval_secs, indexer_seen_at),
            )
        for i, (status, attempts, error_class) in enumerate(jobs):
            conn.execute(
                "INSERT INTO indexing_jobs (filepath, reason, status, attempts, "
                "last_error_class, created_at, updated_at, next_attempt_at) "
                "VALUES (?, 'x', ?, ?, ?, '', '', '')",
                (f"/maildir/INBOX/cur/{i}", status, attempts, error_class),
            )
        conn.commit()
    finally:
        conn.close()


def _insert_chunk(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    message_id: str,
    thread_id: str,
    text: str,
    embedding: list[float],
    chunk_index: int = 0,
    chunked_at: str = "2024-01-01T00:00:00+00:00",
    attachment_id: str | None = None,
    message_date: str | None = None,
    char_start: int = 0,
    variant: str = "",
) -> None:
    """Insert one ``message_chunks`` + matching FTS + vec row.

    Mirrors the indexer's ``replace_message_chunks`` write path closely
    enough that the chunk-aware retrieval lane in the MCP reader can
    exercise it end-to-end, without requiring a real indexer pipeline
    in the unit-test stack.

    ``message_date`` defaults to ``chunked_at``, so tests that only
    care about insert order get a matching message order. ``char_start``
    is the chunk's offset in its message body; a message's later chunks
    must set it, since bodies are reconstructed by offset. The chunk
    belongs to the claimant ``claimant_of(message_id, variant)``.
    """
    if message_date is None:
        message_date = chunked_at
    cur = conn.cursor()
    cur.execute("INSERT INTO message_chunks_fts (text) VALUES (?)", (text,))
    fts_rowid = cur.lastrowid
    cur.execute(
        "INSERT INTO message_chunks_vec (chunk_id, embedding) VALUES (?, ?)",
        (chunk_id, sqlite_vec.serialize_float32(embedding)),
    )
    cur.execute(
        """
        INSERT INTO message_chunks
            (chunk_id, claimant_id, thread_id, chunk_index, text,
             char_start, char_end, token_est,
             chunked_at, fts_rowid, attachment_id, message_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chunk_id,
            claimant_of(message_id, variant),
            thread_id,
            chunk_index,
            text,
            char_start,
            char_start + len(text),
            max(1, len(text) // 4),
            chunked_at,
            fts_rowid,
            attachment_id,
            message_date,
        ),
    )
    conn.commit()


def _insert_attachment(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    thread_id: str,
    attachment_id: str,
    filename: str,
    content_type: str = "application/pdf",
    size_bytes: int = 1234,
    occurrence_id: str | None = None,
    variant: str = "",
) -> None:
    claimant = claimant_of(message_id, variant)
    occurrence_id = occurrence_id or f"{claimant}:{attachment_id}:{filename}"
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO attachments_fts (filename, content_type) VALUES (?, ?)",
        (filename, content_type),
    )
    fts_rowid = cur.lastrowid
    cur.execute(
        """
        INSERT INTO attachments
            (attachment_occurrence_id, claimant_id, attachment_id, thread_id, filename,
             content_type, size_bytes, seen_at, fts_rowid)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            occurrence_id,
            claimant,
            attachment_id,
            thread_id,
            filename,
            content_type,
            size_bytes,
            "2024-01-01T00:00:00+00:00",
            fts_rowid,
        ),
    )
    conn.commit()


def _insert_extraction(
    conn: sqlite3.Connection,
    *,
    attachment_id: str,
    status: str = "success",
    extracted_text: str | None = None,
    extractor: str = "pdf",
    error: str | None = None,
) -> None:
    """Insert one ``attachment_extractions`` row (per content hash).

    Mirrors the indexer's extracted-text cache: ``status`` is
    ``success`` / ``failed`` / ``pending``, ``extracted_text`` carries
    the parsed text on success and is ``None`` otherwise.
    """
    conn.execute(
        """
        INSERT INTO attachment_extractions
            (attachment_id, extraction_status, extractor,
             extracted_text, extraction_error, extracted_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            attachment_id,
            status,
            extractor,
            extracted_text,
            error,
            "2024-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()


def _split_address(value: str) -> tuple[str, str]:
    """``parseaddr`` that degrades to no address, as the indexer's writer does
    for strings that make it recurse."""
    try:
        name, address = parseaddr(value)
    except RecursionError:
        return "", ""
    return name, address.lower()


def source_sha256(message_id: str, variant: str = "") -> str:
    """The raw-file SHA-256 the fixtures record for ``message_id``.

    ``variant`` stands for a different file claiming the same
    Message-ID (#217): it changes the bytes, so the hash."""
    return hashlib.sha256((message_id + variant).encode()).hexdigest()


def claimant_of(message_id: str, variant: str = "") -> str:
    """The claimant ID the indexer gives the fixture file for
    ``message_id``: the Message-ID plus the first eight hex digits of
    the file hash (``indexer/src/parser.py`` ``claimant_id``)."""
    return f"{message_id}#{source_sha256(message_id, variant)[:8]}"


def _insert_message_record(
    cur: sqlite3.Cursor,
    *,
    message_id: str,
    thread_id: str,
    folder: str,
    subject: str,
    sent_at: str,
    has_attachments: bool,
    participants: list[tuple[str, str]],
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    variant: str = "",
) -> None:
    """Insert one ``messages`` row and its ``message_participants``.

    ``participants`` is ``(role, display string)`` pairs; addresses are
    canonicalized and names split out the way the indexer writes them.
    """
    cur.execute(
        """
        INSERT INTO messages
            (claimant_id, message_id, thread_id, filepath, folder, subject, sent_at,
             in_reply_to, references_json, has_attachments, size_bytes,
             content_hash, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 100, ?, ?)
        """,
        (
            claimant_of(message_id, variant),
            message_id,
            thread_id,
            f"/maildir/{folder}/cur/{message_id}{variant}",
            folder,
            subject,
            sent_at,
            in_reply_to,
            json.dumps(references or []),
            1 if has_attachments else 0,
            source_sha256(message_id, variant),
            "2024-01-01T00:00:00+00:00",
        ),
    )
    for role, value in participants:
        name, address = _split_address(value)
        if "@" not in address:
            continue
        cur.execute(
            "INSERT OR IGNORE INTO message_participants VALUES (?, ?, ?, ?)",
            (claimant_of(message_id, variant), role, address, name or None),
        )
        _insert_entity(cur, address, name)


def _insert_entity(cur: sqlite3.Cursor, address: str, name: str | None) -> None:
    """The indexer's entity rows for one participant: a person per
    address with an organization per domain (the fixtures do not model
    the indexer's free-mail exclusion) and the display name as alias."""
    domain = address.rpartition("@")[2]
    cur.execute(
        "INSERT OR IGNORE INTO entities (entity_id, kind, canonical_key, organization_id) "
        "VALUES (?, 'organization', ?, NULL)",
        (f"org:{domain}", domain),
    )
    cur.execute(
        "INSERT OR IGNORE INTO entities (entity_id, kind, canonical_key, organization_id) "
        "VALUES (?, 'person', ?, ?)",
        (f"person:{address}", address, f"org:{domain}"),
    )
    if name:
        cur.execute(
            "INSERT OR IGNORE INTO entity_aliases VALUES (?, ?)", (f"person:{address}", name)
        )


def set_authority(db_path, address: str, authority_class: str, rule: str) -> None:
    """Classify ``address``'s person entity as the indexer's rules file would."""
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute(
            "UPDATE entities SET authority_class = ?, authority_rule = ? WHERE entity_id = ?",
            (authority_class, rule, f"person:{address}"),
        )
        conn.commit()


def _insert_message(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    thread_id: str,
    sent_at: str,
    subject: str = "subject",
    folder: str = "INBOX",
    from_: list[str] | None = None,
    to: list[str] | None = None,
    cc: list[str] | None = None,
    has_attachments: bool = False,
    body: str | None = None,
    attachment_text: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    variant: str = "",
) -> None:
    """Insert one message with full per-message control.

    For ``query_messages`` tests, which need several messages per thread
    with distinct senders, dates, and bodies. Creates the parent thread
    row on first use; ``body`` / ``attachment_text`` become a body chunk
    and an attachment chunk respectively. A non-empty ``variant`` makes
    it another claimant of an already inserted ``message_id`` (#217).
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO threads (
            thread_id, subject, participants, senders, folder,
            date_first, date_last, message_ids
        ) VALUES (?, ?, '[]', '[]', ?, ?, ?, '[]')
        """,
        (thread_id, subject, folder, sent_at, sent_at),
    )
    # Like the indexer, the thread's ``senders`` JSON records only each
    # message's primary author (``from_addr``, the first From entry, even
    # when it has no usable address).
    if from_:
        row = cur.execute("SELECT senders FROM threads WHERE thread_id = ?", (thread_id,))
        senders = json.loads(row.fetchone()[0])
        if from_[0] not in senders:
            cur.execute(
                "UPDATE threads SET senders = ? WHERE thread_id = ?",
                (json.dumps([*senders, from_[0]]), thread_id),
            )
    claimant = claimant_of(message_id, variant)
    row = cur.execute("SELECT message_ids FROM threads WHERE thread_id = ?", (thread_id,))
    cur.execute(
        "UPDATE threads SET message_ids = ? WHERE thread_id = ?",
        (json.dumps([*json.loads(row.fetchone()[0]), claimant]), thread_id),
    )
    cur.execute(
        "INSERT INTO message_thread_map VALUES (?, ?, ?, ?)",
        (claimant, message_id, thread_id, f"/maildir/{folder}/cur/{message_id}{variant}"),
    )
    participants = (
        [("from", v) for v in from_ or []]
        + [("to", v) for v in to or []]
        + [("cc", v) for v in cc or []]
    )
    _insert_message_record(
        cur,
        message_id=message_id,
        thread_id=thread_id,
        folder=folder,
        subject=subject,
        sent_at=sent_at,
        has_attachments=has_attachments,
        participants=participants,
        in_reply_to=in_reply_to,
        references=references,
        variant=variant,
    )
    conn.commit()
    if body is not None:
        _insert_chunk(
            conn,
            chunk_id=f"{message_id}{variant}-body",
            message_id=message_id,
            variant=variant,
            thread_id=thread_id,
            text=body,
            embedding=[1.0, 0.0, 0.0, 0.0],
            message_date=sent_at,
        )
    if attachment_text is not None:
        _insert_chunk(
            conn,
            chunk_id=f"{message_id}{variant}-att",
            message_id=message_id,
            variant=variant,
            thread_id=thread_id,
            text=attachment_text,
            embedding=[1.0, 0.0, 0.0, 0.0],
            attachment_id=f"{message_id}-att",
            message_date=sent_at,
        )


def _insert_thread(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    subject: str,
    participants: list[str],
    senders: list[str] | None = None,
    folder: str = "INBOX",
    date_first: str = "2024-01-01T10:00:00+00:00",
    date_last: str = "2024-01-01T10:00:00+00:00",
    message_ids: list[str] | None = None,
    snippet: str = "",
    has_attachments: bool = False,
    body_text: str = "",
    embedding: list[float] | None = None,
    display_subject: str | None = None,
) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO threads_fts (subject, participants, body)
        VALUES (?, ?, ?)
        """,
        (subject, " ".join(participants), body_text or snippet or subject),
    )
    fts_rowid = cur.lastrowid
    cur.execute(
        """
        INSERT INTO threads (
            thread_id, subject, participants, senders, folder,
            date_first, date_last, message_ids, snippet,
            has_attachments, body_text, fts_rowid, display_subject
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            thread_id,
            subject,
            json.dumps(participants),
            json.dumps(senders if senders is not None else []),
            folder,
            date_first,
            date_last,
            json.dumps([claimant_of(mid) for mid in message_ids or [thread_id]]),
            snippet,
            1 if has_attachments else 0,
            body_text,
            fts_rowid,
            display_subject,
        ),
    )
    # Senders become ``from`` participants and everyone else ``to``, on
    # every message of the thread; the first message is sent at
    # ``date_first`` and the rest at ``date_last``.
    sender_keys = {_split_address(s)[1] for s in senders or []}
    roles = [("from" if _split_address(p)[1] in sender_keys else "to", p) for p in participants]
    for i, mid in enumerate(message_ids or [thread_id]):
        cur.execute(
            "INSERT INTO message_thread_map VALUES (?, ?, ?, ?)",
            (claimant_of(mid), mid, thread_id, f"/maildir/{folder}/cur/{mid}"),
        )
        _insert_message_record(
            cur,
            message_id=mid,
            thread_id=thread_id,
            folder=folder,
            subject=display_subject or subject,
            sent_at=date_first if i == 0 else date_last,
            has_attachments=has_attachments,
            participants=roles,
        )
    if embedding is not None:
        cur.execute(
            "INSERT INTO threads_vec (thread_id, embedding) VALUES (?, ?)",
            (thread_id, sqlite_vec.serialize_float32(embedding)),
        )
    conn.commit()


@pytest.fixture
def seeded_db(tmp_path: Path):
    """Build a populated read-only DB matching the indexer schema."""
    db_path = tmp_path / "mcp-test.db"
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)

    _insert_thread(
        conn,
        thread_id="t-alpha",
        subject="invoice for march",
        participants=["alice@example.com", "bob@example.com"],
        senders=["alice@example.com"],
        folder="INBOX",
        date_first="2024-03-01T09:00:00+00:00",
        date_last="2024-03-02T09:00:00+00:00",
        snippet="please find the invoice attached",
        has_attachments=True,
        body_text="please find the invoice attached for march",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    _insert_attachment(
        conn,
        message_id="t-alpha",
        thread_id="t-alpha",
        attachment_id="att-alpha",
        filename="march-statement-unique.pdf",
        content_type="application/pdf",
    )
    _insert_thread(
        conn,
        thread_id="t-beta",
        subject="lunch plans",
        participants=["carol@example.com", "alice@example.com"],
        senders=["carol@example.com"],
        folder="INBOX",
        date_first="2024-03-05T12:00:00+00:00",
        date_last="2024-03-05T12:30:00+00:00",
        snippet="want to grab lunch tomorrow",
        body_text="want to grab lunch tomorrow at the usual spot",
        embedding=[0.0, 1.0, 0.0, 0.0],
    )
    _insert_thread(
        conn,
        thread_id="t-gamma",
        subject="meeting notes archive",
        participants=["dave@example.com"],
        senders=["dave@example.com"],
        folder="Archive",
        date_first="2024-02-15T08:00:00+00:00",
        date_last="2024-02-15T08:00:00+00:00",
        snippet="notes from the planning meeting",
        body_text="notes from the planning meeting last week",
        embedding=[0.0, 0.0, 1.0, 0.0],
    )
    conn.close()

    db = Database(str(db_path))
    yield db


@pytest.fixture
def chunked_db(tmp_path: Path):
    """Populated read-only DB with both thread-level rows and v9 per-message
    chunks.

    Each thread also carries one or two chunks aligned to the same toy
    4-dim embedding axis as its parent thread vector. Lets chunk-search
    and chunk-aware RRF tests assert that a chunk hit lifts its parent
    thread into ranking, without re-deriving the indexer pipeline.
    """
    db_path = tmp_path / "mcp-chunks.db"
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)

    # Same three threads as ``seeded_db`` but with chunks attached.
    _insert_thread(
        conn,
        thread_id="t-alpha",
        subject="invoice for march",
        participants=["alice@example.com", "bob@example.com"],
        senders=["alice@example.com"],
        date_first="2024-03-01T09:00:00+00:00",
        date_last="2024-03-02T09:00:00+00:00",
        snippet="please find the invoice attached",
        body_text="please find the invoice attached for march",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    _insert_chunk(
        conn,
        chunk_id="alpha-c1",
        message_id="t-alpha",
        thread_id="t-alpha",
        text="invoice number 12345 due march 31",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )

    _insert_thread(
        conn,
        thread_id="t-beta",
        subject="lunch plans",
        participants=["carol@example.com", "alice@example.com"],
        senders=["carol@example.com"],
        date_first="2024-03-05T12:00:00+00:00",
        date_last="2024-03-05T12:30:00+00:00",
        snippet="want to grab lunch tomorrow",
        body_text="want to grab lunch tomorrow at the usual spot",
        embedding=[0.0, 1.0, 0.0, 0.0],
    )
    _insert_chunk(
        conn,
        chunk_id="beta-c1",
        message_id="t-beta",
        thread_id="t-beta",
        text="lets grab lunch at noon tomorrow",
        embedding=[0.0, 1.0, 0.0, 0.0],
    )

    # Third thread has no chunks — exercises the empty-body path:
    # thread vector is still present, but chunk lane will not surface
    # this thread.
    _insert_thread(
        conn,
        thread_id="t-gamma",
        subject="meeting notes archive",
        participants=["dave@example.com"],
        senders=["dave@example.com"],
        folder="Archive",
        date_first="2024-02-15T08:00:00+00:00",
        date_last="2024-02-15T08:00:00+00:00",
        snippet="notes from the planning meeting",
        body_text="notes from the planning meeting last week",
        embedding=[0.0, 0.0, 1.0, 0.0],
    )

    conn.close()
    db = Database(str(db_path))
    yield db


@pytest.fixture
def messages_db(tmp_path):
    """Five messages across three threads for ``query_messages``.

    m4 and m5 share a ``sent_at`` so paging must break the tie on
    message_id; m2 replies to m1 and has an attachment chunk whose text
    must not satisfy ``text``; m3 has two body chunks so ``text`` terms
    can span them.
    """
    path = tmp_path / "mcp-messages.db"
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    _insert_message(
        conn,
        message_id="m1",
        thread_id="t1",
        sent_at="2024-01-10T09:00:00+00:00",
        subject="Budget review",
        from_=["Jane Doe <jane@example.com>"],
        to=["bob@example.com"],
        body="the budget is approved",
    )
    _insert_message(
        conn,
        message_id="m2",
        thread_id="t1",
        sent_at="2024-01-11T10:00:00+00:00",
        subject="Re: Budget review",
        from_=["bob@example.com"],
        to=["Jane Doe <jane@example.com>"],
        cc=["carol@other.org"],
        has_attachments=True,
        body="thanks, budget noted",
        attachment_text="spreadsheet totals",
        in_reply_to="m1",
        references=["m1"],
    )
    _insert_message(
        conn,
        message_id="m3",
        thread_id="t2",
        sent_at="2024-02-01T08:00:00+00:00",
        subject="Lunch",
        folder="Archive",
        from_=["jane@example.com"],
        to=["carol@other.org"],
        body="lunch friday?",
    )
    _insert_chunk(
        conn,
        chunk_id="m3-body-2",
        message_id="m3",
        thread_id="t2",
        chunk_index=1,
        char_start=len("lunch friday?\n\n"),
        text="at the noodle place",
        embedding=[1.0, 0.0, 0.0, 0.0],
        message_date="2024-02-01T08:00:00+00:00",
    )
    _insert_message(
        conn,
        message_id="m4",
        thread_id="t3",
        sent_at="2024-03-05T12:00:00+00:00",
        subject="Contrato",
        from_=["José Álvarez <jose@other.org>"],
        to=["jane@example.com"],
        has_attachments=True,
        body="adjunto el contrato",
    )
    _insert_message(
        conn,
        message_id="m5",
        thread_id="t3",
        sent_at="2024-03-05T12:00:00+00:00",
        subject="Re: Contrato",
        from_=["jane@example.com"],
        to=["jose@other.org"],
        body="gracias",
    )
    conn.close()
    db = Database(str(path))
    yield db


# Real indexer chunker output (``chunk_message(target_tokens=40,
# max_tokens=60, overlap_tokens=20)``) for OVERLAP_BODY: adjacent chunks
# repeat trailing paragraphs, and "Same line again." legitimately occurs
# at two distinct offsets.
_OVERLAP_PARAGRAPHS = [
    f"Paragraph P{i} says the synthetic sentence number {i} twice over for padding purposes."
    for i in range(1, 9)
]
_OVERLAP_PARAGRAPHS.insert(5, "Same line again.")
_OVERLAP_PARAGRAPHS.insert(2, "Same line again.")
OVERLAP_BODY = "\n\n".join(_OVERLAP_PARAGRAPHS)
OVERLAP_SPANS = [(0, 268), (168, 436), (354, 622), (540, 706)]


@pytest.fixture
def overlap_db(tmp_path: Path):
    """One message ("ov1", thread "t-ov") whose body is stored as the
    overlapping chunks above."""
    path = tmp_path / "mcp-overlap.db"
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    _insert_message(conn, message_id="ov1", thread_id="t-ov", sent_at="2024-01-01T00:00:00+00:00")
    for i, (start, end) in enumerate(OVERLAP_SPANS):
        _insert_chunk(
            conn,
            chunk_id=f"ov1-c{i}",
            message_id="ov1",
            thread_id="t-ov",
            text=OVERLAP_BODY[start:end],
            embedding=[1.0, 0.0, 0.0, 0.0],
            chunk_index=i,
            char_start=start,
        )
    conn.close()
    db = Database(str(path))
    yield db


@pytest.fixture
def empty_db(tmp_path: Path):
    db_path = tmp_path / "mcp-empty.db"
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    conn.close()
    db = Database(str(db_path))
    yield db


@pytest.fixture
def conflicts_db(tmp_path: Path):
    """Message-ID conflicts (#455): one Message-ID claimed by two files,
    another by three, and one claimed by a single file."""
    db_path = tmp_path / "mcp-conflicts.db"
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    for message_id, variants in (
        ("two@example.com", ("", "b")),
        ("three@example.com", ("", "b", "c")),
        ("single@example.com", ("",)),
    ):
        for variant in variants:
            _insert_message(
                conn,
                message_id=message_id,
                variant=variant,
                thread_id=f"t-{message_id}",
                sent_at="2024-01-10T09:00:00+00:00",
            )
    conn.commit()
    conn.close()
    yield Database(str(db_path))


@pytest.fixture
def attachments_db(tmp_path: Path):
    """Populated read-only DB exercising the attachment search lanes.

    Three threads, each with one attachment: two PDFs whose text
    extraction succeeded (each with an attachment-derived chunk so the
    filename FTS lane AND the extracted-text FTS lane both have
    something to match), plus a spreadsheet whose extraction failed
    (so ``extracted_only`` and the status field can be exercised).
    """
    db_path = tmp_path / "mcp-attachments.db"
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)

    # Thread 1 — Acme quote PDF, extracted successfully (newest).
    # Carries a display_subject so the original-cased subject path in
    # _row_to_attachment_result is exercised alongside the NULL-fallback.
    _insert_thread(
        conn,
        thread_id="t-quote",
        subject="acme quotation",
        participants=["alice@example.com", "buyer@example.com"],
        senders=["alice@example.com"],
        folder="INBOX",
        date_first="2024-03-10T09:00:00+00:00",
        date_last="2024-03-10T09:00:00+00:00",
        has_attachments=True,
        body_text="our quote is attached",
        embedding=[1.0, 0.0, 0.0, 0.0],
        display_subject="Acme Quotation",
    )
    _insert_attachment(
        conn,
        message_id="t-quote",
        thread_id="t-quote",
        attachment_id="att-quote",
        filename="acme-quote.pdf",
        content_type="application/pdf",
        size_bytes=20480,
    )
    _insert_extraction(
        conn,
        attachment_id="att-quote",
        extracted_text="Acme Corporation quotation. Total 5000 USD. Offer valid 30 days.",
    )
    _insert_chunk(
        conn,
        chunk_id="quote-att-c1",
        message_id="t-quote",
        thread_id="t-quote",
        text="Acme Corporation quotation. Total 5000 USD. Offer valid 30 days.",
        embedding=[0.0, 0.0, 0.0, 1.0],
        attachment_id="att-quote",
    )
    # A second chunk of the same attachment so the extracted-text lane
    # can return two rows for one occurrence — exercising its dedup.
    _insert_chunk(
        conn,
        chunk_id="quote-att-c2",
        message_id="t-quote",
        thread_id="t-quote",
        text="Acme Corporation purchase order and payment terms.",
        embedding=[0.0, 0.0, 0.0, 1.0],
        chunk_index=1,
        attachment_id="att-quote",
    )

    # Thread 2 — W-2 tax PDF, extracted successfully.
    _insert_thread(
        conn,
        thread_id="t-tax",
        subject="payroll documents",
        participants=["payroll@example.com", "buyer@example.com"],
        senders=["payroll@example.com"],
        folder="INBOX",
        date_first="2024-02-01T09:00:00+00:00",
        date_last="2024-02-01T09:00:00+00:00",
        has_attachments=True,
        body_text="your year-end forms are attached",
        embedding=[0.0, 1.0, 0.0, 0.0],
    )
    _insert_attachment(
        conn,
        message_id="t-tax",
        thread_id="t-tax",
        attachment_id="att-w2",
        filename="w2-statement.pdf",
        content_type="application/pdf",
        size_bytes=15360,
    )
    _insert_extraction(
        conn,
        attachment_id="att-w2",
        extracted_text="Wage and Tax Statement. Form W-2 for tax year 2024.",
    )
    _insert_chunk(
        conn,
        chunk_id="tax-att-c1",
        message_id="t-tax",
        thread_id="t-tax",
        text="Wage and Tax Statement. Form W-2 for tax year 2024.",
        embedding=[0.0, 0.0, 0.0, 1.0],
        attachment_id="att-w2",
    )

    # Thread 3 — spreadsheet whose text extraction failed (oldest).
    _insert_thread(
        conn,
        thread_id="t-budget",
        subject="annual budget",
        participants=["dave@example.com"],
        senders=["dave@example.com"],
        folder="Archive",
        date_first="2024-01-15T09:00:00+00:00",
        date_last="2024-01-15T09:00:00+00:00",
        has_attachments=True,
        body_text="budget spreadsheet attached",
        embedding=[0.0, 0.0, 1.0, 0.0],
    )
    _insert_attachment(
        conn,
        message_id="t-budget",
        thread_id="t-budget",
        attachment_id="att-budget",
        filename="annual-budget.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=8192,
    )
    _insert_extraction(
        conn,
        attachment_id="att-budget",
        status="failed",
        extracted_text=None,
        error="unsupported spreadsheet encoding",
    )

    conn.close()
    db = Database(str(db_path))
    yield db


@pytest.fixture
def _build_thread_on():
    """Build a fresh DB with a single thread on caller-supplied dates.

    Regression tests for date-filter SQL pushdown need a ``date_first`` on
    a specific boundary day; ``seeded_db`` only covers Feb/Mar 2024.
    """

    def _factory(
        tmp_path: Path,
        *,
        thread_id: str = "on-last-day",
        subject: str = "year end report",
        body_text: str = "final year end report numbers",
        date_first: str,
        date_last: str,
    ) -> Database:
        db_path = tmp_path / "mcp-boundary.db"
        conn = sqlite3.connect(str(db_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        _insert_thread(
            conn,
            thread_id=thread_id,
            subject=subject,
            participants=["alice@example.com"],
            senders=["alice@example.com"],
            folder="INBOX",
            date_first=date_first,
            date_last=date_last,
            snippet=subject,
            body_text=body_text,
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        conn.close()
        return Database(str(db_path))

    yield _factory


def _make_result(thread_id: str, folder: str = "INBOX"):
    """Tiny ThreadResult factory for pure fusion/filter tests."""
    from src.lib.sqlite import ThreadResult

    return ThreadResult(
        thread_id=thread_id,
        subject=f"subject-{thread_id}",
        participants=["alice@example.com"],
        folder=folder,
        date_first=datetime(2024, 1, 1, tzinfo=UTC),
        date_last=datetime(2024, 1, 2, tzinfo=UTC),
        message_ids=[thread_id],
        snippet="",
        has_attachments=False,
    )


@pytest.fixture
def make_result():
    return _make_result


# ---------------------------------------------------------------------------
# MCP tool handler test scaffolding
# ---------------------------------------------------------------------------
#
# The tool modules in ``src/tools/`` call ``@server.tool()`` to register
# handlers with a FastMCP server. Exercising the real FastMCP machinery in
# unit tests pulls in MCP protocol scaffolding that has no bearing on the
# handler logic we want to cover. ``FakeMCPServer`` captures each decorated
# function under its ``__name__`` so tests can call the handlers directly
# as plain async callables, isolating the code under test from the framework.


class FakeMCPServer:
    """Minimal stub of the FastMCP surface used by tool-registration functions.

    Captures every function passed to ``@server.tool()`` in ``tools`` keyed
    by the function's name. ``custom_route`` is a no-op decorator so
    ``main.py`` registration paths run without needing a real Starlette
    app. Nothing about the captured callables is wrapped or instrumented —
    tests invoke them exactly as the FastMCP dispatcher would, which is
    the behavior we want to verify.
    """

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}
        self.custom_routes: dict[str, object] = {}

    def tool(self, *_args, **_kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator

    def custom_route(self, path: str, *_args, **_kwargs):
        def decorator(fn):
            self.custom_routes[path] = fn
            return fn

        return decorator


class FakeEmbedClient:
    """Async stub of ``src.lib.embed.EmbedClient``.

    Returns a canned 4-dim embedding that matches ``seeded_db`` /
    ``empty_db`` vec0 schema so retrieval tests can run without hitting
    a live embedding backend.
    """

    def __init__(self, embedding: list[float] | None = None) -> None:
        self._embedding = embedding if embedding is not None else [1.0, 0.0, 0.0, 0.0]
        self.embed_calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return list(self._embedding)


class FakeInferenceClient:
    """Async stub of ``src.lib.inference.InferenceClient``.

    Returns a canned ``complete()`` response so intelligence tools can
    be exercised without hitting a live LLM backend.
    ``complete_responses`` lets a test queue up successive distinct
    responses for the per-thread ``extract_from_emails`` loop; a queued
    exception is raised instead of returned.
    """

    def __init__(
        self,
        response: str = "mock answer",
        complete_responses: list[str | BaseException] | None = None,
        mode: str = "anthropic",
    ) -> None:
        self._default_response = response
        self._queued = list(complete_responses) if complete_responses is not None else []
        self.mode = mode
        self.complete_calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> str:
        self.complete_calls.append((system, user))
        if self._queued:
            queued = self._queued.pop(0)
            if isinstance(queued, BaseException):
                raise queued
            return queued
        return self._default_response


@pytest.fixture
def fake_server() -> FakeMCPServer:
    return FakeMCPServer()


@pytest.fixture
def fake_embed() -> FakeEmbedClient:
    return FakeEmbedClient()


@pytest.fixture
def fake_inference() -> FakeInferenceClient:
    return FakeInferenceClient()
