"""Real listing SQL: selected clocks seek through ties without changing counts."""

import base64
import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from src.lib.predicates import DATE_BASES, Where, normalize_where
from src.lib.sqlite import Database

from tests.conftest import _insert_message
from tests.test_date_basis import (
    clock_server as clock_fixture,  # noqa: F401 - shared synthetic fixture
)
from tests.test_sqlite import _open_built_db_conn
from tests.test_structured_output import _call


class _Capture:
    def __init__(self, conn, statements):
        self.conn = conn
        self.statements = statements

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, params=()):
        self.statements.append((sql, list(params)))
        return self.conn.execute(sql, params)


def _capture(db, monkeypatch):
    statements = []
    connect = db._connect
    monkeypatch.setattr(db, "_connect", lambda: _Capture(connect(), statements))
    return statements, connect


@pytest.mark.parametrize("basis", ["sent", "occurred"])
@pytest.mark.parametrize("bounded", [False, True])
def test_real_cursor_page_seeks_through_large_timestamp_ties(tmp_path, monkeypatch, basis, bounded):
    work = []
    for size in (5000, 50000):
        conn, path = _open_built_db_conn(tmp_path, f"ties-{size}.db")
        _insert_message(
            conn,
            message_id="seed",
            thread_id="t",
            sent_at="2024-01-01T00:00:00+00:00",
            occurred_at="2024-01-01T00:00:00+00:00",
        )
        columns = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
        # Real row widths and the reader's actual projections/joins, not a toy key SELECT.
        template = list(conn.execute(f"SELECT {','.join(columns)} FROM messages").fetchone())
        claimant_position = columns.index("claimant_id")
        template.pop(claimant_position)
        conn.executemany(
            f"INSERT INTO messages ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [
                tuple(
                    [*template[:claimant_position], f"claim-{i:06d}", *template[claimant_position:]]
                )
                for i in range(size)
            ],
        )
        conn.commit()
        conn.close()
        db = Database(str(path))
        statements, connect = _capture(db, monkeypatch)
        kwargs = dict(date_basis=basis, limit=25)
        if bounded:
            kwargs.update(date_from="2023-01-01", date_to="2025-01-01")
        first = db.query_messages(**kwargs)
        payload = json.loads(
            base64.urlsafe_b64decode(first.next_cursor + "=" * (-len(first.next_cursor) % 4))
        )
        payload["m"] = f"claim-{size // 10:06d}"
        cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        statements.clear()
        page = db.query_messages(**kwargs, cursor=cursor)
        assert [m.claimant_id for m in page.messages] == [
            f"claim-{i:06d}" for i in range(size // 10 - 1, size // 10 - 26, -1)
        ]
        sql, params = next(s for s in statements if s[0].startswith("SELECT m.message_id"))
        with closing(connect()) as reader:
            plan = " ".join(r[3] for r in reader.execute("EXPLAIN QUERY PLAN " + sql, params))
            assert f"SEARCH m USING INDEX idx_messages_{basis}" in plan
            assert "TEMP B-TREE" not in plan
            steps = []
            reader.set_progress_handler(lambda: steps.append(1) or 0, 100)
            assert len(reader.execute(sql, params).fetchall()) == 26
            reader.set_progress_handler(None, 0)
            work.append(len(steps))
    # A cursor near the bottom of one huge tie must not walk all earlier IDs.
    assert work[1] <= work[0] + 2
    assert work[1] < 100


@pytest.mark.parametrize("basis", ["sent", "occurred"])
@pytest.mark.parametrize("tool", ["query_messages", "query_attachments"])
def test_clock_pages_preserve_evidence_order_and_old_cursors(request, monkeypatch, basis, tool):
    clock_client = request.getfixturevalue("clock_fixture")
    kwargs = dict(date_basis=basis, limit=1, date_from="2024-01-01", date_to="2024-03-31")
    original = DATE_BASES[basis]
    # Reference is the previously shipped CASE/evidence ordering, with its old cursor.
    monkeypatch.setitem(
        DATE_BASES, basis, replace(original, page_clock=original.clock, page_guard="")
    )
    old_first = _call(clock_client, tool, **kwargs)
    old_next = _call(clock_client, tool, **kwargs, cursor=old_first["next_cursor"])
    monkeypatch.setitem(DATE_BASES, basis, original)
    assert _call(clock_client, tool, **kwargs) == old_first
    assert _call(clock_client, tool, **kwargs, cursor=old_first["next_cursor"]) == old_next


@pytest.mark.parametrize("basis", ["sent", "occurred"])
@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"folder": "INBOX"},
        {"seen": False},
        {"subject": "synthetic"},
        {"sender": "author@test.example"},
        {
            "where": normalize_where(
                Where.model_validate(
                    {"all": [{"leaf": "subject_contains", "value": "synthetic", "negate": True}]}
                )
            )
        },
    ],
)
def test_count_and_evidence_plans_are_unchanged(tmp_path, monkeypatch, basis, filters):
    conn, path = _open_built_db_conn(tmp_path)
    _insert_message(
        conn,
        message_id="count",
        thread_id="t",
        subject="synthetic",
        from_=["author@test.example"],
        sent_at="2024-01-01T00:00:00+00:00",
        occurred_at="2024-01-01T00:00:00+00:00",
    )
    conn.close()
    db = Database(str(path))
    statements, connect = _capture(db, monkeypatch)
    db.query_messages(date_basis=basis, date_from="2023-01-01", date_to="2025-01-01", **filters)
    counts = [
        (sql, params)
        for sql, params in statements
        if sql.startswith("SELECT") and not sql.startswith("SELECT m.message_id")
    ]

    def plans():
        with closing(connect()) as reader:
            return [
                [r[3] for r in reader.execute("EXPLAIN QUERY PLAN " + sql, params)]
                for sql, params in counts
            ]

    indexed = plans()
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("DROP INDEX idx_messages_sent")
        writer.execute("DROP INDEX idx_messages_occurred")
    assert plans() == indexed
