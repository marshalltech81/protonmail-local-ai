"""Synthetic differential checks for exhaustive thread populations (#1166)."""

import asyncio
import collections
import inspect
import logging
import sqlite3
from types import SimpleNamespace

import pytest
from src.lib import predicates
from src.lib.sqlite import Database, InvalidFilterError
from src.tools.retrieval import register_retrieval_tools

from tests import test_aggregate_messages as aggregate_fixtures
from tests import test_population_leaves as population_fixtures
from tests.conftest import FakeMCPServer, _insert_message
from tests.test_aggregate_messages import (
    _FILTER_CASES,
    _paged_messages,
    _where,
    _work_corpus,
)
from tests.test_evidence_scope import _finish_threads
from tests.test_retrieval import _error, _handlers, _text
from tests.test_sqlite import _open_built_db_conn

# Reuse the existing synthetic catalogues without duplicating their setup.
agg_db = aggregate_fixtures.agg_db
header_db = population_fixtures.header_db
population_db = population_fixtures.population_db


def _all(db, **filters):
    rows, cursor, first = [], None, None
    while True:
        page = db.query_threads(limit=2, cursor=cursor, **filters)
        first = first or page
        rows.extend(page.threads)
        if not page.has_more:
            assert page.next_cursor is None
            return rows, first
        cursor = page.next_cursor


@pytest.mark.parametrize(
    ("filters", "items"),
    _FILTER_CASES
    + [
        ({"folder": "Trash"}, []),
        ({"sender": "ann@one.test"}, []),
        ({"recipient": "me@self.test", "seen": False}, []),
        ({"participant": "carol@two.test", "has_attachments": True}, []),
        ({"date_from": "2024-01-01", "date_to": "2024-02-29"}, []),
        ({"size_min": 0, "size_max": 100000}, []),
        ({"authority_class": "vendor"}, []),
        ({}, [{"leaf": "subject_contains", "value": "budget"}]),
        ({}, [{"leaf": "header_exists", "name": "x-test"}]),
        ({}, [{"leaf": "attachment_text_words", "value": "test", "negate": True}]),
    ],
)
def test_matches_grouping_exhaustive_messages(agg_db, filters, items):
    where = _where(*items)
    messages, message_page = _paged_messages(agg_db, filters, where)
    rows, page = _all(agg_db, **filters, where=where)
    grouped = collections.defaultdict(list)
    for message in messages:
        grouped[message.thread_id].append(message)
    assert page.total_threads == len(grouped) == len(rows)
    assert page.total_matching_messages == message_page.total_matches
    assert page.indeterminate_messages == message_page.indeterminate
    assert [r.thread_id for r in rows] == sorted(grouped)
    for row in rows:
        matches = grouped[row.thread_id]
        assert row.matching_message_count == len(matches)
        newest = max(matches, key=lambda m: (m.effective_at, m.claimant_id))
        assert row.newest_matching_claimant_id == newest.claimant_id


@pytest.fixture
def thread_db(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "thread-query.db")
    for cid, tid, subject, folder, complete in [
        ("same#one", "a", "match", "INBOX", 1),
        ("same#two", "a", "match", "INBOX", 1),
        ("unknown", "a", "other", "INBOX", None),
        ("trash", "a", "other", "Trash", 1),
        ("only-unknown", "b", "other", "INBOX", 0),
        ("false", "c", "other", "INBOX", 1),
    ]:
        _insert_message(
            conn,
            message_id="same" if cid.startswith("same#") else cid,
            variant=cid if cid.startswith("same#") else "",
            sent_at="2024-01-01T00:00:00+00:00",
            thread_id=tid,
            subject=subject,
            body="absentword" if cid == "unknown" else "normal word",
            folder=folder,
            from_=["a@synthetic.test"],
            to=["b@synthetic.test"],
            completeness={"subject_complete": complete},
        )
    _finish_threads(conn)
    conn.close()
    return Database(str(path))


def test_unknown_units_duplicate_claimants_and_full_thread_metadata(thread_db):
    page = thread_db.query_threads(subject="match")
    assert (
        page.total_threads,
        page.total_matching_messages,
        page.indeterminate_messages,
        page.indeterminate_threads,
    ) == (1, 2, 2, 1)
    row = page.threads[0]
    assert (
        row.matching_message_count,
        row.indeterminate_message_count,
        row.total_message_count,
        row.participant_count,
        row.has_attachments,
    ) == (2, 1, 4, 2, False)


@pytest.mark.parametrize(
    "flag", ["from_addresses_complete", "to_addresses_complete", "cc_addresses_complete"]
)
@pytest.mark.parametrize("state", [None, 0])
def test_incomplete_whole_thread_participants_are_unknown(thread_db, flag, state):
    with sqlite3.connect(thread_db.path) as conn:
        conn.execute(f"UPDATE messages SET {flag}=? WHERE message_id=?", (state, "trash"))
    assert thread_db.query_threads(subject="match").threads[0].participant_count is None


def test_attachment_presence_uses_complete_whole_thread_evidence(thread_db):
    with sqlite3.connect(thread_db.path) as conn:
        conn.execute(
            "UPDATE messages SET attachments_manifest_complete=NULL WHERE message_id='trash'"
        )
    assert thread_db.query_threads(subject="match").threads[0].has_attachments is None
    with sqlite3.connect(thread_db.path) as conn:
        conn.execute("UPDATE messages SET has_attachments=1 WHERE message_id='trash'")
    assert thread_db.query_threads(subject="match").threads[0].has_attachments is True


def test_undated_matches_do_not_display_indexing_time(thread_db):
    with sqlite3.connect(thread_db.path) as conn:
        conn.execute(
            "UPDATE messages SET occurred_at=NULL, sent_at_status='missing' WHERE thread_id='a'"
        )
    row = thread_db.query_threads(subject="match").threads[0]
    assert row.first_matching_at is None and row.last_matching_at is None
    assert row.newest_matching_claimant_id


def test_and_leaves_cannot_be_satisfied_by_different_messages(thread_db):
    with sqlite3.connect(thread_db.path) as conn:
        conn.execute("UPDATE messages SET subject_complete=1 WHERE message_id='unknown'")
    assert thread_db.query_messages(subject="match").total_matches == 2
    assert thread_db.query_messages(text="absentword").total_matches == 1
    where = _where(
        {"leaf": "subject_contains", "value": "match"},
        {"leaf": "body_words", "value": "absentword"},
    )
    assert thread_db.query_threads(where=where).total_threads == 0


def test_cursors_bound_to_tool_filters_and_version(agg_db):
    page = agg_db.query_threads(limit=1)
    with pytest.raises(InvalidFilterError):
        agg_db.query_threads(subject="other", cursor=page.next_cursor)
    with pytest.raises(InvalidFilterError):
        agg_db.query_messages(cursor=page.next_cursor)
    for cursor in [
        "?",
        agg_db.query_messages(limit=1).next_cursor,
        agg_db.aggregate_messages(group_by="folder", limit=1).next_cursor,
    ]:
        with pytest.raises(InvalidFilterError):
            agg_db.query_threads(cursor=cursor)


def test_empty_population_preserves_unknown_counts(thread_db):
    page = thread_db.query_threads(subject="absent")
    assert page.total_threads == 0 and page.threads == []
    assert page.indeterminate_messages == 2 and page.indeterminate_threads == 2
    assert page.next_cursor is None and not page.has_more


def test_public_filters_match_existing_message_tool_and_logs_withhold_content(thread_db, caplog):
    handlers = _handlers(FakeMCPServer(), thread_db)
    message_args = set(inspect.signature(handlers["query_messages"]).parameters) - {
        "fields",
        "date_basis",
    }
    assert set(inspect.signature(handlers["query_threads"]).parameters) == message_args
    caplog.set_level(logging.INFO)
    result = asyncio.run(handlers["query_threads"](subject="SYNTHETIC_THREAD_MARKER"))
    assert not result.is_error
    assert "SYNTHETIC_THREAD_MARKER" not in caplog.text
    assert "indeterminate_messages: 2" in _text(result)
    assert "indeterminate_threads: 2" in _text(result)


def test_invalid_tool_argument_is_logged_without_value(thread_db, caplog):
    caplog.set_level(logging.INFO)
    result = _error(
        _handlers(FakeMCPServer(), thread_db)["query_threads"](date_from="SYNTHETIC_THREAD_MARKER")
    )
    assert result
    assert "SYNTHETIC_THREAD_MARKER" not in caplog.text


def test_one_statement_and_one_evaluation_per_claimant_at_two_sizes(tmp_path, monkeypatch):
    items = [
        {
            "any": [
                {"leaf": "address_is", "role": "to", "value": "b@two.test"},
                {"leaf": "body_words", "value": "nonexistentword"},
            ]
        },
        {"leaf": "address_contains", "role": "from", "value": "zzz", "negate": True},
    ]
    shapes = []
    for size in (40, 400):
        db = _work_corpus(tmp_path, size)
        calls, statements, connections, executions = {}, [], [], []
        connect = db._connect

        def probe(tag, value):
            calls[tag] = calls.get(tag, 0) + 1
            return value

        def traced():
            conn = connect()
            connections.append(conn)
            conn.create_function("mcp_probe", 2, probe)
            conn.set_trace_callback(
                lambda sql: None if sql.startswith("-- ") else statements.append(sql)
            )

            def execute(sql, parameters=()):
                executions.append(sql)
                return conn.execute(sql, parameters)

            return SimpleNamespace(execute=execute, close=conn.close)

        with monkeypatch.context() as patch:
            patch.setattr(db, "_connect", traced)
            for name in predicates.WHERE_LEAVES:
                kind = predicates.LEAVES[name]

                def spy(value, params, _original=kind.compile, _name=name):
                    return f"mcp_probe('{_name}', ({_original(value, params)}))"

                patch.setitem(
                    predicates.LEAVES,
                    name,
                    predicates.LeafKind(kind.name, kind.param, spy, kind.evaluability),
                )
            page = db.query_threads(where=_where(*items), limit=2)
        assert (page.total_matching_messages, page.indeterminate_messages) == (size // 2, size // 4)
        assert set(calls) == {"address_is", "body_words", "address_contains"}
        assert all(count <= size for count in calls.values())
        assert len(executions) == 1
        assert executions[0].startswith("WITH e AS MATERIALIZED")
        assert sum(sql.startswith("WITH e AS MATERIALIZED") for sql in statements) == 1
        # FTS5 preparation adds internal statements whose count varies by SQLite
        # build. Count every trace, but require constant work across corpus sizes
        # rather than the macOS build's particular total.
        shapes.append((len(connections), len(statements)))
    assert shapes[0] == shapes[1]
    assert shapes[0][0] == 1


def test_newest_match_changes_do_not_repeat_returned_threads(agg_db):
    first = agg_db.query_threads(limit=1)
    tid = first.threads[0].thread_id
    with sqlite3.connect(agg_db.path) as conn:
        conn.execute(
            "UPDATE messages SET occurred_at=? WHERE thread_id=?",
            ("1900-01-01T00:00:00+00:00", tid),
        )
    second = agg_db.query_threads(limit=100, cursor=first.next_cursor)
    assert tid not in {r.thread_id for r in second.threads}
    assert first.total_threads == second.total_threads


def test_wire_output_and_page_cap(agg_db):
    from fastmcp import FastMCP

    from tests.test_structured_output import _call, _tools

    server = FastMCP("thread-test")
    register_retrieval_tools(server, agg_db)
    data = _call(server, "query_threads", subject="budget", limit=1)
    assert data["total_threads"] > data["returned"] == 1
    assert data["next_cursor"] and data["has_more"]
    assert data["total_matching_messages"] == agg_db.query_messages(subject="budget").total_matches
    schema = _tools(server)["query_threads"].input_schema["properties"]
    assert "date_basis" not in schema and "fields" not in schema
    handler = _handlers(FakeMCPServer(), agg_db)["query_threads"]
    result = asyncio.run(handler(limit=0))
    assert result.structured_content["returned"] == 1
    assert "first_matching_at:" in _text(result)


def test_query_error_is_type_only(thread_db, monkeypatch, caplog):
    def fail(**kwargs):
        raise sqlite3.OperationalError("SYNTHETIC_THREAD_MARKER")

    monkeypatch.setattr(thread_db, "query_threads", fail)
    caplog.set_level(logging.INFO)
    error = _error(_handlers(FakeMCPServer(), thread_db)["query_threads"]())
    assert error == "Error: OperationalError"
    assert "SYNTHETIC_THREAD_MARKER" not in caplog.text
    assert "query_threads failed: OperationalError" in caplog.text


_LEAF_CATALOGUE = [
    {"leaf": "address_is", "role": "from", "value": "ann@one.test"},
    {"leaf": "address_contains", "role": "from", "value": "one.test"},
    {"leaf": "display_name_contains", "role": "from", "value": "ann"},
    {"leaf": "address_or_name_contains", "role": "to", "value": "self"},
    {"leaf": "domain_is", "role": "cc", "value": "one.test"},
    {"leaf": "body_words", "value": "alpha budget"},
    {"leaf": "subject_contains", "value": "STRASSE"},
    {"leaf": "attachment_text_words", "value": "alpha budget"},
    {"leaf": "header_exists", "name": "X-List"},
    {"leaf": "header_contains", "name": "X-List", "value": "STRASSE"},
]


def test_catalogue_covers_every_public_where_leaf():
    assert {item["leaf"] for item in _LEAF_CATALOGUE} == set(predicates.WHERE_LEAVES)


@pytest.mark.parametrize("item", _LEAF_CATALOGUE)
@pytest.mark.parametrize("negate", [False, True])
def test_all_leaves_and_negations_match_message_populations(
    agg_db, header_db, population_db, item, negate
):
    for db in (agg_db, header_db, population_db):
        where = _where({**item, "negate": negate})
        messages, message_page = _paged_messages(db, {}, where)
        rows, page = _all(db, where=where)
        counts = collections.Counter(m.thread_id for m in messages)
        assert {r.thread_id: r.matching_message_count for r in rows} == dict(counts)
        assert page.total_threads == len(counts)
        assert page.total_matching_messages == message_page.total_matches
        assert page.indeterminate_messages == message_page.indeterminate


def test_counts_rows_and_metadata_share_one_snapshot(thread_db, monkeypatch):
    with sqlite3.connect(thread_db.path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    original = predicates.LEAVES["subject_contains"]
    connect = thread_db._connect
    changed = False

    def change_after_snapshot(value):
        nonlocal changed
        if not changed:
            changed = True
            with sqlite3.connect(thread_db.path) as writer:
                writer.execute(
                    "UPDATE messages SET subject='other', from_addresses_complete=NULL WHERE message_id='same'"
                )
        return value

    def traced():
        conn = connect()
        conn.create_function("change_after_snapshot", 1, change_after_snapshot)
        return conn

    def compile_with_write(value, params):
        return f"change_after_snapshot(({original.compile(value, params)}))"

    with monkeypatch.context() as patch:
        patch.setattr(thread_db, "_connect", traced)
        patch.setitem(
            predicates.LEAVES,
            "subject_contains",
            predicates.LeafKind(
                original.name, original.param, compile_with_write, original.evaluability
            ),
        )
        page = thread_db.query_threads(where=_where({"leaf": "subject_contains", "value": "match"}))
    assert changed
    assert page.total_matching_messages == page.threads[0].matching_message_count == 2
    assert page.threads[0].participant_count == 2
    assert thread_db.query_threads(subject="match").total_threads == 0
