"""
Shared fixtures for indexer tests.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from src.database import Database
from src.parser import Message
from src.threader import Thread, Threader


def make_message(
    message_id: str = "msg1@example.com",
    subject: str = "Hello world",
    from_addr: str = "alice@example.com",
    to_addrs: list[str] | None = None,
    cc_addrs: list[str] | None = None,
    body_text: str = "This is the message body.",
    folder: str = "INBOX",
    filepath: str = "/maildir/INBOX/cur/msg1",
    date: datetime | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    has_attachments: bool = False,
) -> Message:
    return Message(
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references or [],
        subject=subject,
        from_addr=from_addr,
        to_addrs=to_addrs or ["bob@example.com"],
        cc_addrs=cc_addrs or [],
        date=date or datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        body_text=body_text,
        folder=folder,
        filepath=filepath,
        has_attachments=has_attachments,
    )


def make_thread(
    messages: list[Message] | None = None,
    thread_id: str | None = None,
    subject: str = "hello world",
    folder: str = "INBOX",
) -> Thread:
    msgs = messages or [make_message()]
    tid = thread_id or msgs[0].message_id
    return Thread(
        thread_id=tid,
        subject=subject,
        participants=[msgs[0].from_addr] + msgs[0].to_addrs,
        messages=msgs,
        folder=folder,
        date_first=msgs[0].date,
        date_last=msgs[-1].date,
    )


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def threader(db: Database) -> Threader:
    return Threader(db)
