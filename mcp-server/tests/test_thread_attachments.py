"""Bounded, occurrence-level attachment metadata in get_thread (#1238)."""

import asyncio
import logging
import sqlite3
import time
from contextlib import closing

import pytest
from src.tools.outputs import HEADER_CHAR_LIMIT, MAX_LISTED, GetThreadOutput

from tests.conftest import _insert_attachment, _insert_extraction, _insert_message, claimant_of
from tests.test_retrieval import _handlers, _text


def _call(fake_server, db, **kwargs):
    result = asyncio.run(_handlers(fake_server, db)["get_thread"](thread_id="t1", **kwargs))
    GetThreadOutput.model_validate(result.structured_content)
    return result, result.structured_content["messages"]


def _attach(conn, mid, occurrence, *, payload="shared", module="pdf", deferred=False):
    _insert_attachment(
        conn,
        message_id=mid,
        thread_id="t1",
        attachment_id=payload,
        occurrence_id=occurrence,
        filename=f"{occurrence}.pdf",
        extractor_module=module,
        deferred=deferred,
    )


def test_occurrences_stay_with_their_carrying_message(fake_server, messages_db):
    with closing(sqlite3.connect(messages_db.path)) as conn:
        _attach(conn, "m1", "first")
        _attach(conn, "m1", "second")
        _attach(conn, "m2", "third")
        _insert_extraction(conn, attachment_id="shared", status="failed", error="PRIVATE_ERROR")
    result, messages = _call(fake_server, messages_db)
    assert [[a["attachment_occurrence_id"] for a in m["attachments"]] for m in messages] == [
        ["first", "second"],
        ["third"],
    ]
    for message in messages:
        assert message["attachment_count"] == len(message["attachments"])
        assert message["attachments_truncated"] is False
        for attachment in message["attachments"]:
            assert attachment["claimant_id"] == message["claimant_id"]
            assert attachment["attachment_id"] == "shared"
            assert attachment["extraction_status"] == "failed"
            assert "text" not in attachment
            assert attachment["attachment_occurrence_id"] in _text(result)
    assert "PRIVATE_ERROR" not in _text(result)


def test_status_uses_each_occurrences_cache_key_and_deferral(fake_server, messages_db):
    with closing(sqlite3.connect(messages_db.path)) as conn:
        for occurrence, module, deferred in [
            ("success", "pdf", False),
            ("failed", "text", False),
            ("unknown", "html", False),
            ("deferred", "pdf", True),
        ]:
            _attach(conn, "m1", occurrence, module=module, deferred=deferred)
        _insert_extraction(
            conn, attachment_id="shared", status="success", extracted_text="PRIVATE_TEXT"
        )
        _insert_extraction(conn, attachment_id="shared", extractor_module="text", status="failed")
    result, messages = _call(fake_server, messages_db)
    assert {
        a["attachment_occurrence_id"]: a["extraction_status"] for a in messages[0]["attachments"]
    } == {"success": "success", "failed": "failed", "unknown": None, "deferred": "deferred"}
    assert "PRIVATE_TEXT" not in _text(result)


def test_limit_is_per_message_and_only_the_message_page_is_listed(fake_server, messages_db, caplog):
    with closing(sqlite3.connect(messages_db.path)) as conn:
        for mid in ("m1", "m2"):
            for i in range(MAX_LISTED + 3):
                _attach(conn, mid, f"{mid}-{i:03}")
    caplog.set_level(logging.INFO)
    result, messages = _call(fake_server, messages_db, limit=1)
    assert len(messages) == 1
    message = messages[0]
    assert len(message["attachments"]) == MAX_LISTED
    assert message["attachment_count"] == MAX_LISTED + 3
    assert message["attachments_truncated"] is True
    assert "3 more attachments" in _text(result)
    assert "m2-000.pdf" not in _text(result)
    assert "attachment_metadata_omitted" in caplog.text
    _, second = _call(fake_server, messages_db, offset=1, limit=1)
    assert all(a["claimant_id"] == claimant_of("m2") for a in second[0]["attachments"])


@pytest.mark.parametrize("current,complete", [(1, 1), (0, 1), (1, 0), (None, None)])
def test_stored_inventory_is_not_mistaken_for_complete_inventory(
    fake_server, messages_db, current, complete
):
    with closing(sqlite3.connect(messages_db.path)) as conn, conn:
        conn.execute(
            "UPDATE messages SET attachment_rows_current = ?, attachments_manifest_complete = ?",
            (current, complete),
        )
    result, messages = _call(fake_server, messages_db)
    for message in messages:
        assert message["attachments"] == []
        assert message["attachment_count"] == 0
        assert message["attachment_rows_current"] == (None if current is None else bool(current))
        assert message["attachments_manifest_complete"] == (
            None if complete is None else bool(complete)
        )
    if current != 1 or complete != 1:
        assert "inventory may be incomplete or obsolete" in _text(result)


def test_disabled_metadata_is_unknown_and_does_not_read_attachments(
    fake_server, messages_db, monkeypatch
):
    connect = messages_db._connect
    statements = []

    def traced():
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(messages_db, "_connect", traced)
    _, messages = _call(fake_server, messages_db, include_attachments_metadata=False)
    for message in messages:
        assert message["attachments"] is None
        assert message["attachment_count"] is None
        assert message["attachments_truncated"] is None
    assert not any("FROM attachments" in sql for sql in statements)


def test_metadata_is_bounded_before_transfer_and_never_logged(fake_server, messages_db, caplog):
    marker = "SYNTHETIC_ATTACHMENT_PRIVATE_1238"
    with closing(sqlite3.connect(messages_db.path)) as conn, conn:
        _attach(conn, "m1", "bounded")
        conn.execute(
            "UPDATE attachments SET filename = ?, content_type = ?",
            (marker + "\0" + "😀" * 5000, marker + "\0" + "x" * 5000),
        )
    caplog.set_level(logging.INFO)
    result, messages = _call(fake_server, messages_db)
    attachment = messages[0]["attachments"][0]
    assert len(attachment["filename"]) == len(attachment["content_type"]) == HEADER_CHAR_LIMIT
    assert attachment["filename_clipped"] and attachment["content_type_clipped"]
    assert "\0" in attachment["filename"]
    assert "[cut]" in _text(result)
    assert marker not in caplog.text
    assert "attachment_metadata_clipped" in caplog.text


@pytest.mark.serial  # Includes a plain-time bound; run without competing workers.
def test_only_bounded_page_metadata_crosses_sqlite_boundary(fake_server, messages_db, monkeypatch):
    with closing(sqlite3.connect(messages_db.path)) as conn, conn:
        for mid in ("m1", "m2"):
            for i in range(100):
                _attach(conn, mid, f"{mid}-{i:03}")
        conn.execute(
            "UPDATE attachments SET filename = ?, content_type = ?", ("😀" * 100_000, "x" * 100_000)
        )
    connect = messages_db._connect
    transferred = []
    statements = []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def __iter__(self):
            return iter(self.cursor)

        def fetchall(self):
            rows = self.cursor.fetchall()
            columns = {c[0] for c in self.cursor.description}
            if "filename_head" in columns:
                transferred.extend(rows)
                assert "extracted_text" not in columns
                assert all(len(r["filename_head"]) <= 4 * HEADER_CHAR_LIMIT + 1 for r in rows)
                assert all(len(r["content_type_head"]) <= 4 * HEADER_CHAR_LIMIT + 1 for r in rows)
            return rows

    class Connection:
        def __init__(self):
            self.conn = connect()

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, parameters=()):
            statements.append(sql)
            return Cursor(self.conn.execute(sql, parameters))

    monkeypatch.setattr(messages_db, "_connect", Connection)
    started = time.monotonic()
    result, messages = _call(fake_server, messages_db, limit=1)
    assert time.monotonic() - started < 5
    assert messages[0]["attachment_count"] == 100
    assert len(transferred) == MAX_LISTED
    assert {r["claimant_id"] for r in transferred} == {claimant_of("m1")}
    assert len([s for s in statements if "FROM attachments" in s]) == 2
    assert len(_text(result)) < 20_000


def test_attachment_counts_rows_and_flags_share_the_message_snapshot(
    fake_server, messages_db, monkeypatch
):
    with closing(sqlite3.connect(messages_db.path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        _attach(conn, "m1", "before")
    connect = messages_db._connect
    changed = False

    class Connection:
        def __init__(self):
            self.conn = connect()

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, parameters=()):
            nonlocal changed
            cursor = self.conn.execute(sql, parameters)
            if not changed and "AS total " in sql:
                changed = True
                with closing(sqlite3.connect(messages_db.path)) as writer, writer:
                    _attach(writer, "m1", "after")
                    writer.execute("UPDATE messages SET attachment_rows_current = 0")
            return cursor

    monkeypatch.setattr(messages_db, "_connect", Connection)
    _, messages = _call(fake_server, messages_db)
    assert changed
    assert messages[0]["attachment_count"] == 1
    assert [a["attachment_occurrence_id"] for a in messages[0]["attachments"]] == ["before"]
    assert messages[0]["attachment_rows_current"] is True


def test_metadata_is_in_the_served_schema_and_wire_result(messages_db):
    from tests.test_structured_output import _call as call_wire
    from tests.test_structured_output import _server

    with closing(sqlite3.connect(messages_db.path)) as conn:
        _attach(conn, "m1", "wire")
    output = call_wire(_server(messages_db), "get_thread", thread_id="t1", limit=1)
    assert output["messages"][0]["attachments"][0]["attachment_occurrence_id"] == "wire"


def test_two_claimants_of_one_message_id_do_not_share_attachment_metadata(fake_server, messages_db):
    with closing(sqlite3.connect(messages_db.path)) as conn:
        _insert_message(
            conn, message_id="m1", variant="other", thread_id="t1", sent_at="2024-01-01"
        )
        _attach(conn, "m1", "original")
        _insert_attachment(
            conn,
            message_id="m1",
            variant="other",
            thread_id="t1",
            attachment_id="different",
            occurrence_id="other",
            filename="other.pdf",
        )
    _, messages = _call(fake_server, messages_db)
    attachments = {m["claimant_id"]: m["attachments"] for m in messages}
    assert [a["attachment_occurrence_id"] for a in attachments[claimant_of("m1")]] == ["original"]
    assert [a["attachment_occurrence_id"] for a in attachments[claimant_of("m1", "other")]] == [
        "other"
    ]
