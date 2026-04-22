"""Shared fixtures for mcp-server tests."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlite_vec
from src.lib.sqlite import Database


def _build_schema(conn: sqlite3.Connection) -> None:
    """Build the minimal thread-level schema the MCP reader depends on.

    Mirrors indexer SCHEMA_VERSION 5: threads (with ``senders``), threads_fts
    (contentless with contentless_delete=1), threads_vec, and
    message_thread_map.
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
        """
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
def seeded_db(tmp_path: Path) -> Database:
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

    return Database(str(db_path))


@pytest.fixture
def empty_db(tmp_path: Path) -> Database:
    db_path = tmp_path / "mcp-empty.db"
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    conn.close()
    return Database(str(db_path))


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
