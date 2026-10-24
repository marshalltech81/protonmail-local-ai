"""Missing occurrences must be visible even when the stored-row query is empty."""

import asyncio
import logging
from contextlib import closing

import pytest
from src.lib.sqlite import Database

from tests.conftest import _insert_attachment, _insert_message, claimant_of
from tests.test_retrieval import _handlers
from tests.test_sqlite import _open_built_db_conn

MARKER = "SYNTHETIC_PRIVATE_MANIFEST_MARKER"


def build(tmp_path, manifest, stored, **message):
    conn, path = _open_built_db_conn(tmp_path, "manifest.db")
    _insert_message(
        conn,
        message_id="m",
        thread_id="t",
        sent_at="2024-01-01T00:00:00+00:00",
        from_=[f"{MARKER} <sender@example.test>"],
        has_attachments=True,
        completeness={"attachments_manifest_complete": manifest},
        **message,
    )
    if stored:
        for i in range(2):
            _insert_attachment(
                conn,
                message_id="m",
                thread_id="t",
                attachment_id=f"a{i}",
                filename=f"{MARKER}-{i}.pdf",
            )
    conn.commit()
    conn.close()
    return Database(str(path))


@pytest.mark.parametrize("manifest", [None, 0, 1])
@pytest.mark.parametrize("stored", [False, True])
def test_manifest_coverage_includes_messages_without_rows(tmp_path, manifest, stored):
    db = build(tmp_path, manifest, stored)
    page = db.query_attachments(limit=1)
    assert page.messages_with_incomplete_manifest == int(manifest != 1)
    assert (page.total_matches, page.indeterminate) == (2 * int(stored), 0)
    if stored:
        assert page.attachments[0].attachments_manifest_complete is (
            None if manifest is None else bool(manifest)
        )
        later = db.query_attachments(cursor=page.next_cursor, limit=1)
        assert later.messages_with_incomplete_manifest == page.messages_with_incomplete_manifest
        text = db.get_attachment_text(page.attachments[0].attachment_occurrence_id, 0, 10)
        assert (
            text.record.attachments_manifest_complete
            is page.attachments[0].attachments_manifest_complete
        )
    # Missing occurrences cannot be ruled out with attachment-row filters.
    for filters in (
        {"filename": "absent"},
        {"content_type": "image/png"},
        {"attachment_id": "absent"},
        {"extraction_status": "success"},
    ):
        assert db.query_attachments(**filters).messages_with_incomplete_manifest == int(
            manifest != 1
        )


@pytest.mark.parametrize(
    "filters",
    [
        {"sender": "other@example.test"},
        {"recipient": "other@example.test"},
        {"folder": "Archive"},
        {"date_from": "2025-01-01"},
        {"thread_id": "other"},
        {"claimant_id": "other"},
    ],
)
def test_manifest_coverage_respects_message_scope(tmp_path, filters):
    db = build(tmp_path, 0, False)
    assert db.query_attachments(**filters).messages_with_incomplete_manifest == 0
    assert (
        db.query_attachments(
            thread_id="t", claimant_id=claimant_of("m")
        ).messages_with_incomplete_manifest
        == 1
    )


def test_unknown_sender_cannot_hide_a_manifest_gap(tmp_path):
    db = build(tmp_path, 0, False, sender_ambiguous=None)
    assert db.query_attachments(sender="other@example.test").messages_with_incomplete_manifest == 1


def test_tool_reports_gap_before_an_empty_page_without_logging_values(
    tmp_path, fake_server, caplog
):
    db = build(tmp_path, 0, False)
    handler = _handlers(fake_server, db)["query_attachments"]
    with caplog.at_level(logging.INFO):
        out = asyncio.run(handler(filename=MARKER))
    assert out.structured_content["messages_with_incomplete_manifest"] == 1
    text = "\n".join(c.text for c in out.content if hasattr(c, "text"))
    assert "further attachment occurrences may be missing" in text
    assert "No attachments match." not in text
    assert "'messages_with_incomplete_manifest': 1" in caplog.text
    assert MARKER not in caplog.text


def test_manifest_count_adds_one_bounded_message_scan(tmp_path, monkeypatch):
    db = build(tmp_path, 0, False)
    statements = []
    connect = db._connect

    def traced():
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(db, "_connect", traced)
    db.query_attachments()
    counts = [s for s in statements if "COUNT(*) FROM messages m" in s]
    assert len(counts) == 1
    assert "attachments" not in counts[0].split("FROM messages m", 1)[1].replace(
        "attachments_manifest_complete", ""
    )
    with closing(connect()) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1


def test_manifest_count_and_row_flags_share_one_snapshot(tmp_path):
    import sqlite3

    from tests.test_sqlite import _commit_between_reads

    db = build(tmp_path, 0, True)
    with closing(sqlite3.connect(db.path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        _commit_between_reads(db, writer, "UPDATE messages SET attachments_manifest_complete=1;")
        page = db.query_attachments()
        assert page.messages_with_incomplete_manifest == 1
        assert {row.attachments_manifest_complete for row in page.attachments} == {False}
        assert db.query_attachments().messages_with_incomplete_manifest == 0
