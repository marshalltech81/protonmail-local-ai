"""Committed currentness is independent of both manifest and extracted text."""

import asyncio
import logging
import sqlite3
from contextlib import closing

import pytest

from tests.test_attachment_manifest_coverage import MARKER, build
from tests.test_retrieval import _handlers


@pytest.mark.parametrize("current", [None, 0, 1])
@pytest.mark.parametrize("manifest", [None, 0, 1])
@pytest.mark.parametrize("stored", [False, True])
def test_inventory_currentness_grid(tmp_path, current, manifest, stored):
    db = build(tmp_path, manifest, stored)
    with closing(sqlite3.connect(db.path)) as conn, conn:
        conn.execute("UPDATE messages SET attachment_rows_current=?", (current,))
    page = db.query_attachments(limit=1)
    assert page.messages_with_noncurrent_inventory == int(current != 1)
    assert page.messages_with_incomplete_manifest == int(manifest != 1)
    assert page.indeterminate == 0
    assert page.total_matches == 2 * int(stored)
    if stored:
        assert page.attachments[0].attachment_rows_current is (
            None if current is None else bool(current)
        )
        later = db.query_attachments(cursor=page.next_cursor)
        assert later.messages_with_noncurrent_inventory == page.messages_with_noncurrent_inventory
    assert db.query_attachments(filename="absent").messages_with_noncurrent_inventory == int(
        current != 1
    )
    assert db.query_attachments(thread_id="other").messages_with_noncurrent_inventory == 0


@pytest.mark.parametrize("stored", [False, True])
def test_no_attachments_distinguishes_empty_inventory_from_retained_stale_rows(tmp_path, stored):
    db = build(tmp_path, 1, stored)
    with closing(sqlite3.connect(db.path)) as conn, conn:
        conn.execute("UPDATE messages SET has_attachments=0, attachment_rows_current=0")
    page = db.query_attachments()
    assert page.messages_with_noncurrent_inventory == int(stored)
    assert page.messages_with_incomplete_manifest == 0


def test_current_partial_extraction_is_not_a_stale_inventory(tmp_path):
    db = build(tmp_path, 1, True)
    with closing(sqlite3.connect(db.path)) as conn, conn:
        conn.execute("UPDATE attachments SET text_complete=0")
    assert db.query_attachments().messages_with_noncurrent_inventory == 0


def test_empty_first_pass_warns_without_logging_query_values(tmp_path, fake_server, caplog):
    db = build(tmp_path, 1, False)
    with closing(sqlite3.connect(db.path)) as conn, conn:
        conn.execute("UPDATE messages SET attachment_rows_current=0")
    handler = _handlers(fake_server, db)["query_attachments"]
    with caplog.at_level(logging.INFO):
        out = asyncio.run(handler(filename=MARKER))
    assert out.structured_content["messages_with_noncurrent_inventory"] == 1
    text = "\n".join(c.text for c in out.content if hasattr(c, "text"))
    assert "stored rows may be stale or current occurrences missing" in text
    assert "No attachments match." not in text
    assert "'messages_with_noncurrent_inventory': 1" in caplog.text
    assert MARKER not in caplog.text
