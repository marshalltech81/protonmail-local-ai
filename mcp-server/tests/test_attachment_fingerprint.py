"""A continuation must name the exact stored text it began reading (#1361)."""

import asyncio
import hashlib
import logging
import sqlite3
from contextlib import closing

import pytest
from fastmcp.exceptions import ToolError

from tests.test_get_attachment import MARKER, _db, _handler, _occ


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _call(handler, **kwargs):
    return asyncio.run(handler(attachment_occurrence_id=_occ(0), **kwargs))


def test_first_page_and_listing_expose_the_same_text_identity(tmp_path):
    text = "é\x00😀" * 20_000
    db = _db(tmp_path, {0: ("success", text, None)})
    first = _call(_handler(db)).structured_content
    assert first["attachment"]["text_sha256"] == _hash(text)
    assert db.query_attachments().attachments[0].text_sha256 == _hash(text)
    second = _call(_handler(db), offset=first["next_offset"], expected_text_sha256=_hash(text))
    assert second.structured_content["text"] == text[20_000:40_000]


@pytest.mark.parametrize("offset", [0, 20_000])
def test_replacement_at_same_time_and_rowid_is_rejected_before_reading(
    tmp_path, monkeypatch, caplog, offset
):
    text = "original " * 8000
    db = _db(tmp_path, {0: ("success", text, None)})
    handler = _handler(db)
    _call(handler)
    with closing(sqlite3.connect(db.path)) as conn:
        conn.execute(
            "UPDATE attachment_extractions SET extracted_text=?, text_sha256=?",
            (MARKER * 8000, _hash(MARKER * 8000)),
        )
        conn.commit()
    import src.lib.sqlite as sql

    monkeypatch.setattr(sql, "_read_text_window", lambda *a: pytest.fail("read changed text"))
    with caplog.at_level(logging.INFO), pytest.raises(ToolError, match="text changed") as exc:
        _call(handler, offset=offset, expected_text_sha256=_hash(text))
    assert MARKER not in str(exc.value) + caplog.text
    assert "text_changed" in caplog.text
    assert _hash(text) not in caplog.text


@pytest.mark.parametrize("expected", [None, "", "wrong", MARKER, "A" * 64])
def test_continuation_requires_a_valid_fingerprint(tmp_path, expected, caplog):
    handler = _handler(_db(tmp_path, {0: ("success", "x" * 40_000, None)}))
    with caplog.at_level(logging.INFO), pytest.raises(ToolError):
        _call(handler, offset=20_000, expected_text_sha256=expected)
    assert MARKER not in caplog.text


def test_unknown_identity_allows_first_read_but_refuses_a_pinned_read(tmp_path, caplog):
    db = _db(tmp_path, {0: ("success", "x" * 40_000, None)})
    with closing(sqlite3.connect(db.path)) as conn:
        conn.execute("UPDATE attachment_extractions SET text_sha256=NULL")
        conn.commit()
    handler = _handler(db)
    with caplog.at_level(logging.INFO):
        out = _call(handler)
    assert out.structured_content["attachment"]["text_sha256"] is None
    assert "identity_unknown" in caplog.text
    with pytest.raises(ToolError, match="identity is unknown"):
        _call(handler, offset=20_000, expected_text_sha256=_hash("x" * 40_000))


def test_identical_text_replacement_can_continue(tmp_path):
    text = "stable " * 8000
    db = _db(tmp_path, {0: ("success", text, None)})
    handler = _handler(db)
    first = _call(handler).structured_content
    with closing(sqlite3.connect(db.path)) as conn:
        conn.execute("UPDATE attachment_extractions SET extractor='text@999', extracted_at='later'")
        conn.commit()
    next_page = _call(handler, offset=first["next_offset"], expected_text_sha256=_hash(text))
    assert next_page.structured_content["text"] == text[20_000:40_000]


def test_compare_and_page_share_the_snapshot_even_if_a_writer_replaces_text(tmp_path, monkeypatch):
    import src.lib.sqlite as sql

    text = "snapshot " * 8000
    db = _db(tmp_path, {0: ("success", text, None)})
    with closing(sqlite3.connect(db.path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
    connect = db._connect

    class RacingConnection:
        def __init__(self):
            self.conn = connect()

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def blobopen(self, *args, **kwargs):
            with closing(sqlite3.connect(db.path)) as writer:
                replacement = MARKER * 8000
                writer.execute(
                    "UPDATE attachment_extractions SET extracted_text=?, text_sha256=?",
                    (replacement, _hash(replacement)),
                )
                writer.commit()
            return self.conn.blobopen(*args, **kwargs)

    monkeypatch.setattr(db, "_connect", RacingConnection)
    found = db.get_attachment_text(_occ(0), 20_000, 20_000, _hash(text))
    assert found.window == text[20_000:40_000]
    assert found.record.text_sha256 == _hash(text)
    # The next transaction sees the replacement and refuses the old identity.
    with pytest.raises(sql.AttachmentTextIdentityError, match="text changed"):
        db.get_attachment_text(_occ(0), 20_000, 20_000, _hash(text))


@pytest.mark.parametrize("status", ["failed", "empty", "unsupported", "too_large", "deferred"])
def test_a_pinned_text_becoming_unavailable_does_not_promise_a_backfill(tmp_path, status):
    text = "previous text " * 4000
    db = _db(tmp_path, {0: ("success", text, None)})
    with closing(sqlite3.connect(db.path)) as conn:
        if status == "deferred":
            conn.execute("UPDATE attachments SET extraction_deferred_at='2026-01-01T00:00:00Z'")
        else:
            conn.execute(
                "UPDATE attachment_extractions SET extraction_status=?, extracted_text=NULL, text_sha256=NULL",
                (status,),
            )
        conn.commit()
    with pytest.raises(ToolError, match="text changed") as exc:
        _call(_handler(db), offset=20_000, expected_text_sha256=_hash(text))
    assert "backfill" not in str(exc.value)
