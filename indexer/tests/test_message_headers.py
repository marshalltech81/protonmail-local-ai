"""Bounded top-level header evidence; synthetic bytes only."""

import email
from collections import Counter
from pathlib import Path

import pytest
from src import parser
from src.database import EMBEDDING_DIM
from src.parser import SourceMetadata, parse_email_bytes

from tests.conftest import make_thread

RAW = (
    b"Message-ID: <same@example.test>\r\nSubject: synthetic\r\n"
    b"X-List: first\r\nX-List: =?utf-8?q?Stra=C3=9Fe?=\r\n"
    b"X-Empty:\r\n\r\nbody"
)


def _parse(raw=RAW):
    return parse_email_bytes(
        raw,
        SourceMetadata(
            folder="INBOX",
            flags=frozenset(),
            size=len(raw),
            mtime_ns=None,
            path="/maildir/INBOX/cur/synthetic",
        ),
    )


def test_duplicates_order_empty_and_decoded_values():
    msg = _parse()
    assert msg.headers_complete is True
    assert [
        (h.ordinal, h.name, h.name_normalized, h.raw_value, h.decoded_value) for h in msg.headers
    ] == [
        (0, "Message-ID", "message-id", "<same@example.test>", "<same@example.test>"),
        (1, "Subject", "subject", "synthetic", "synthetic"),
        (2, "X-List", "x-list", "first", "first"),
        (3, "X-List", "x-list", "=?utf-8?q?Stra=C3=9Fe?=", "Straße"),
        (4, "X-Empty", "x-empty", "", ""),
    ]


@pytest.mark.parametrize("dimension", ["fields", "bytes", "value"])
def test_one_budget_stops_before_decoding_and_retaining_dropped_values(monkeypatch, dimension):
    msg = email.message_from_bytes(RAW)
    calls = []
    original = parser._decode_text_header
    monkeypatch.setattr(
        parser,
        "_decode_text_header",
        lambda value, degraded=None: calls.append(value) or original(value, degraded),
    )
    if dimension == "fields":
        monkeypatch.setattr(parser, "MAX_STORED_HEADER_FIELDS", 2)
    elif dimension == "bytes":
        monkeypatch.setattr(parser, "MAX_STORED_HEADER_BYTES", 50)
    else:
        monkeypatch.setattr(parser, "MAX_STORED_HEADER_VALUE_BYTES", 22)
    caps = Counter()
    rows, complete = parser._read_top_headers(msg, caps)
    assert complete is False
    assert caps == {"headers_budget": 1}
    assert len(calls) == len(rows)
    assert len(rows) <= 2
    assert (
        sum(len(h.name.encode("latin-1")) + len(h.raw_value.encode("latin-1")) for h in rows)
        <= parser.MAX_STORED_HEADER_BYTES
    )


def test_invalid_charset_preserves_raw_and_uses_null_decoded(caplog):
    msg = _parse(RAW.replace(b"first", b"=?SYNTHETIC_PRIVATE_MARKER?q?value?="))
    assert msg.headers[2].raw_value == "=?SYNTHETIC_PRIVATE_MARKER?q?value?="
    assert msg.headers[2].decoded_value is None
    assert "SYNTHETIC_PRIVATE_MARKER" not in caplog.text


def test_raw_8bit_header_is_storable_and_reversible():
    msg = _parse(RAW.replace(b"first", b"raw \xff"))
    assert msg.headers[2].raw_value.encode("latin-1") == b"raw \xff"
    assert msg.headers[2].decoded_value is None


def test_writer_keeps_claimants_separate_replaces_rows_and_cascades(db):
    one, two = _parse(), _parse(RAW.replace(b"first", b"second"))
    thread = make_thread([one, two])
    db.upsert_thread(thread, [0.1] * EMBEDDING_DIM)
    rows = db._conn.execute(
        "SELECT claimant_id, raw_value FROM message_headers "
        "WHERE name_normalized = 'x-list' AND ordinal = 2"
    ).fetchall()
    assert {tuple(r) for r in rows} == {(one.claimant_id, "first"), (two.claimant_id, "second")}
    db.upsert_thread(thread, [0.1] * EMBEDDING_DIM)
    assert db._conn.execute("SELECT COUNT(*) FROM message_headers").fetchone()[0] == 10
    with db.transaction():
        db._conn.execute("DELETE FROM message_thread_map WHERE claimant_id = ?", (one.claimant_id,))
    assert (
        db._conn.execute("SELECT DISTINCT claimant_id FROM message_headers").fetchall()[0][0]
        == two.claimant_id
    )


def test_budget_is_visible_without_header_values(monkeypatch, caplog):
    monkeypatch.setattr(parser, "MAX_STORED_HEADER_FIELDS", 2)
    msg = _parse(RAW.replace(b"first", b"SYNTHETIC_PRIVATE_MARKER"))
    assert msg.headers_complete is False
    assert msg.parse_caps["headers_budget"] == 1
    assert "headers_budget=1" in caplog.text
    assert "SYNTHETIC_PRIVATE_MARKER" not in caplog.text


def test_worst_case_visits_only_budgeted_fields(monkeypatch):
    import time

    class ManyHeaders(email.message.Message):
        visited = 0

        def raw_items(self):
            for _ in range(100_000):
                self.visited += 1
                yield "X-Synthetic", "=?utf-8?q?alpha?= " * 100

    msg = ManyHeaders()
    calls = []
    original = parser._decode_text_header
    monkeypatch.setattr(
        parser,
        "_decode_text_header",
        lambda value, degraded=None: calls.append(1) or original(value, degraded),
    )
    start = time.perf_counter()
    rows, complete = parser._read_top_headers(msg, Counter())
    assert time.perf_counter() - start < 5
    assert not complete
    assert msg.visited == parser.MAX_STORED_HEADER_FIELDS + 1
    assert len(rows) == len(calls) == parser.MAX_STORED_HEADER_FIELDS
    assert sum(len(h.raw_value) + len(h.name) for h in rows) <= parser.MAX_STORED_HEADER_BYTES


def test_v12_migration_unknown_backfill_and_existing_dead_job(tmp_path):
    from src.database import SCHEMA_VERSION, Database
    from src.queue import REPARSE_ENQUEUE_SQL

    from tests.test_database import _schema_shape, _v12_from_fresh

    path = tmp_path / "old.db"
    old = Database(path)
    msg = _parse()
    old.upsert_thread(make_thread([msg]), [0.1] * EMBEDDING_DIM)
    old._conn.execute(
        "INSERT OR REPLACE INTO indexed_files (filepath, indexed_at, content_hash) "
        "VALUES (?, '2024-01-01', 'synthetic')",
        (msg.filepath,),
    )
    old._conn.execute(
        "INSERT INTO indexing_jobs (filepath, reason, status, attempts, "
        "created_at, updated_at, next_attempt_at, last_error) "
        "VALUES ('/maildir/dead', 'new', 'dead', 5, 'd', 'd', 'd', 'ValueError')"
    )
    old._conn.commit()
    _v12_from_fresh(old)
    old.close()
    migrated, fresh = Database(path), Database(tmp_path / "fresh.db")
    try:
        assert SCHEMA_VERSION == 13
        assert _schema_shape(migrated._conn) == _schema_shape(fresh._conn)
        assert migrated._conn.execute("SELECT headers_complete FROM messages").fetchone()[0] is None
        assert migrated._conn.execute("SELECT COUNT(*) FROM message_headers").fetchone()[0] == 0
        assert tuple(
            migrated._conn.execute(
                "SELECT reason, status, attempts FROM indexing_jobs WHERE filepath = ?",
                (msg.filepath,),
            ).fetchone()
        ) == ("reparse", "queued", 0)
        assert tuple(
            migrated._conn.execute(
                "SELECT status, attempts, last_error FROM indexing_jobs "
                "WHERE filepath = '/maildir/dead'"
            ).fetchone()
        ) == ("dead", 5, "ValueError")
        migrated.upsert_thread(make_thread([msg]), [0.1] * EMBEDDING_DIM)
        assert migrated._conn.execute("SELECT headers_complete FROM messages").fetchone()[0] == 1
        assert migrated._conn.execute("SELECT COUNT(*) FROM message_headers").fetchone()[0] == 5
        sql = (Path(__file__).parents[1] / "src/migrations/0013_message_headers.sql").read_text()
        assert sql.rstrip().endswith(REPARSE_ENQUEUE_SQL)
    finally:
        migrated.close()
        fresh.close()


@pytest.mark.parametrize(
    "raw",
    [
        b" SYNTHETIC_PRIVATE_MARKER\r\nMessage-ID: <same@example.test>\r\nX-List: yes\r\n\r\nbody",
        b"Message-ID: <same@example.test>\r\nSYNTHETIC_PRIVATE_MARKER\r\nX-List: yes\r\n\r\nbody",
    ],
)
def test_stdlib_header_loss_is_incomplete_without_logging_defect_text(raw, caplog):
    msg = _parse(raw)
    assert msg.headers_complete is False
    assert msg.parse_caps["headers_parse"] == 1
    assert "headers_parse=1" in caplog.text
    assert "SYNTHETIC_PRIVATE_MARKER" not in caplog.text


def test_decoded_surrogate_keeps_raw_evidence_and_indexes(db, monkeypatch, caplog):
    original = parser._decode_text_header

    def decode(value, degraded=None):
        if value == "first":
            return "SYNTHETIC_PRIVATE_MARKER" + chr(0xD800)
        return original(value, degraded)

    monkeypatch.setattr(parser, "_decode_text_header", decode)
    msg = _parse()
    assert msg.headers[2].decoded_value is None
    assert msg.headers[2].raw_value == "first"
    db.upsert_thread(make_thread([msg]), [0.1] * EMBEDDING_DIM)
    assert db._conn.execute("SELECT COUNT(*) FROM message_headers").fetchone()[0] == 5
    assert "header evidence decode failed" in caplog.text
    assert "SYNTHETIC_PRIVATE_MARKER" not in caplog.text
