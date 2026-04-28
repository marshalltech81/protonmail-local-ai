"""Shared fixtures for mcp-server tests."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlite_vec
from src.lib.sqlite import Database


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
            thread_id    TEXT PRIMARY KEY,
            subject      TEXT NOT NULL,
            participants TEXT NOT NULL,
            senders      TEXT NOT NULL DEFAULT '[]',
            folder       TEXT NOT NULL,
            date_first   TEXT NOT NULL,
            date_last    TEXT NOT NULL,
            message_ids  TEXT NOT NULL,
            snippet      TEXT,
            has_attachments INTEGER DEFAULT 0,
            body_text    TEXT,
            fts_rowid    INTEGER
        );

        CREATE TABLE message_thread_map (
            message_id TEXT PRIMARY KEY,
            thread_id  TEXT NOT NULL,
            filepath   TEXT NOT NULL
        );

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
            attachment_id   TEXT
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
            message_id                TEXT NOT NULL,
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
        """
    )


def _insert_chunk(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    message_id: str,
    thread_id: str,
    text: str,
    embedding: list[float],
    chunk_index: int = 0,
) -> None:
    """Insert one ``message_chunks`` + matching FTS + vec row.

    Mirrors the indexer's ``replace_message_chunks`` write path closely
    enough that the chunk-aware retrieval lane in the MCP reader can
    exercise it end-to-end, without requiring a real indexer pipeline
    in the unit-test stack.
    """
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
            (chunk_id, message_id, thread_id, chunk_index, text,
             char_start, char_end, token_est,
             chunked_at, fts_rowid)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chunk_id,
            message_id,
            thread_id,
            chunk_index,
            text,
            0,
            len(text),
            max(1, len(text) // 4),
            "2024-01-01T00:00:00+00:00",
            fts_rowid,
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
) -> None:
    occurrence_id = occurrence_id or f"{message_id}:{attachment_id}:{filename}"
    cur = conn.cursor()
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
            "2024-01-01T00:00:00+00:00",
            fts_rowid,
        ),
    )
    conn.commit()


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
            has_attachments, body_text, fts_rowid
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            thread_id,
            subject,
            json.dumps(participants),
            json.dumps(senders if senders is not None else []),
            folder,
            date_first,
            date_last,
            json.dumps(message_ids or [thread_id]),
            snippet,
            1 if has_attachments else 0,
            body_text,
            fts_rowid,
        ),
    )
    for mid in message_ids or [thread_id]:
        cur.execute(
            "INSERT INTO message_thread_map VALUES (?, ?, ?)",
            (mid, thread_id, f"/maildir/{folder}/cur/{mid}"),
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
    try:
        yield db
    finally:
        db.close()


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
    try:
        yield db
    finally:
        db.close()


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
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def _build_thread_on():
    """Build a fresh DB with a single thread on caller-supplied dates.

    Regression tests for date-filter SQL pushdown need a ``date_first`` on
    a specific boundary day; ``seeded_db`` only covers Feb/Mar 2024.
    """

    created: list[Database] = []

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
        db = Database(str(db_path))
        created.append(db)
        return db

    try:
        yield _factory
    finally:
        for db in created:
            db.close()


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


class FakeOllama:
    """Async stub of ``src.lib.ollama.OllamaClient``.

    Returns a canned 4-dim embedding that matches ``seeded_db`` /
    ``empty_db`` vec0 schema, and a canned ``complete()`` response so
    intelligence tools can be exercised without hitting a live Ollama
    instance. ``complete_responses`` lets a test queue up successive
    distinct responses for the per-thread ``extract_from_emails`` loop.
    """

    def __init__(
        self,
        embedding: list[float] | None = None,
        response: str = "mock answer",
        complete_responses: list[str] | None = None,
    ) -> None:
        self._embedding = embedding if embedding is not None else [1.0, 0.0, 0.0, 0.0]
        self._default_response = response
        self._queued = list(complete_responses) if complete_responses is not None else []
        self.embed_calls: list[str] = []
        self.complete_calls: list[tuple[str, str]] = []

    async def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return list(self._embedding)

    async def complete(self, system: str, user: str) -> str:
        self.complete_calls.append((system, user))
        if self._queued:
            return self._queued.pop(0)
        return self._default_response


class FakeIMAP:
    """Stub of the IMAP/SMTP client used by action tools.

    Calls are recorded for assertion. ``send_email`` is sync on the real
    client; ``move_message`` / ``set_flag`` are async. The stub preserves
    that split so tests catch a caller that awaits the wrong one.
    """

    def __init__(self, send_ok: bool = True, move_ok: bool = True, flag_ok: bool = True) -> None:
        self._send_ok = send_ok
        self._move_ok = move_ok
        self._flag_ok = flag_ok
        self.send_calls: list[dict] = []
        self.move_calls: list[tuple[str, str, str]] = []
        self.flag_calls: list[tuple[str, str, str, bool]] = []

    def send_email(self, **kwargs) -> bool:
        self.send_calls.append(kwargs)
        return self._send_ok

    async def move_message(self, uid: str, src: str, dst: str) -> bool:
        self.move_calls.append((uid, src, dst))
        return self._move_ok

    async def set_flag(self, uid: str, folder: str, flag: str, value: bool) -> bool:
        self.flag_calls.append((uid, folder, flag, value))
        return self._flag_ok


@pytest.fixture
def fake_server() -> FakeMCPServer:
    return FakeMCPServer()


@pytest.fixture
def fake_ollama() -> FakeOllama:
    return FakeOllama()


@pytest.fixture
def fake_imap() -> FakeIMAP:
    return FakeIMAP()
