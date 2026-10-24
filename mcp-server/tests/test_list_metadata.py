"""Synthetic raw-header normalization contract approved for #825."""

from contextlib import closing

import pytest
from src.lib.list_metadata import normalize_fields


@pytest.mark.parametrize(
    ("raw", "value", "status"),
    [
        ("Example list <List.Example.COM>", "list.example.com", "valid"),
        ('"quoted <phrase>" (comment) <list.example.com>', "list.example.com", "valid"),
        ("Obsolete. Phrase <li st.\r\n example.com>\t", "list.example.com", "valid"),
        ("=?utf-8?q?List?= <list.example.com>", "list.example.com", "valid"),
        ("<list.example.com> (trailing)", None, "malformed"),
        ("prefix <a.example> <b.example>", None, "malformed"),
        ("<example>", None, "malformed"),
        (". <a.example>", None, "malformed"),
        ("name. <a.example>", "a.example", "valid"),
        ("<a..example>", None, "malformed"),
        ("<a.example>\x00private", None, "malformed"),
        ("<a.example>\n", None, "malformed"),
        ("unclosed (comment <a.example>", None, "malformed"),
        ("<" + "a" * 250 + ".example>", None, "malformed"),
    ],
)
def test_list_id(raw, value, status):
    assert normalize_fields("list-id", [raw]) == (value, status)


@pytest.mark.parametrize(
    ("raw", "value", "status"),
    [
        (" BULK\r\n \t", "bulk", "valid"),
        ("list", "list", "valid"),
        ("junk", "junk", "valid"),
        ("normal", "unsupported", "unsupported"),
        ("", None, "malformed"),
        ("bulk list", None, "malformed"),
        ("bulk(comment)", None, "malformed"),
        ("=?ascii?q?bulk?=", None, "malformed"),
        ("bul\x00k", None, "malformed"),
        ("bülk", None, "malformed"),
    ],
)
def test_precedence(raw, value, status):
    assert normalize_fields("precedence", [raw]) == (value, status)


def test_absence_duplicate_and_conflicting_raw_tokens():
    assert normalize_fields("list-id", []) == (None, "absent")
    assert normalize_fields("list-id", ["<A.example>", "Display <a.EXAMPLE>"]) == (
        "a.example",
        "duplicate",
    )
    assert normalize_fields("list-id", ["<a.example>", "<b.example>"]) == (None, "conflict")
    assert normalize_fields("precedence", ["normal", "normal"]) == ("unsupported", "duplicate")
    assert normalize_fields("precedence", ["normal", "other"]) == (None, "conflict")
    assert normalize_fields("precedence", ["bulk", "list"]) == (None, "conflict")


def test_sql_budget_guards_precede_python_and_materialize_once(tmp_path):
    import json

    from src.lib.list_metadata import metadata_result_sql, with_metadata

    from tests.conftest import _insert_message, claimant_of
    from tests.test_sqlite import _open_built_db_conn

    conn, _ = _open_built_db_conn(tmp_path, "metadata-guards.db")
    try:
        calls = []

        def normalize(name, encoded):
            fields = json.loads(encoded)
            calls.append(sum(len(f) for f in fields))
            return json.dumps(normalize_fields(name, fields))

        conn.create_function("mcp_list_metadata", 2, normalize)
        cases = {
            "valid": "List <a.example>",
            "large": "\x00" + "x" * 40000,
            "nul": "<a.example>\x00",
        }
        for mid, raw in cases.items():
            _insert_message(
                conn,
                message_id=mid,
                thread_id=mid,
                sent_at=None,
                completeness={"headers_complete": 1},
            )
            conn.execute(
                "INSERT INTO message_headers VALUES (?, 0, 'List-Id', 'list-id', ?, NULL)",
                (claimant_of(mid), raw),
            )
        result = metadata_result_sql("list_id")
        rows = conn.execute(
            with_metadata(f"SELECT m.message_id, {result}, {result} FROM messages m", ("list_id",))
        ).fetchall()
        assert len(calls) == 1
        assert calls == [len(cases["valid"])]
        # A known-bad nonmaterialized shape repeats the UDF for the same
        # selected field; prove the one-call gate detects that regression.
        calls.clear()
        bad = with_metadata(f"SELECT {result}, {result} FROM messages m", ("list_id",)).replace(
            " AS MATERIALIZED", " AS NOT MATERIALIZED"
        )
        conn.execute(bad).fetchall()
        assert len(calls) > 1
        values = {r[0]: json.loads(r[1]) for r in rows}
        assert values == {
            "valid": ["a.example", "valid"],
            "large": [None, "budget_exceeded"],
            "nul": [None, "malformed"],
        }
    finally:
        conn.close()


@pytest.mark.parametrize("name", ["list-id", "precedence"])
def test_normalizer_worst_case_is_bounded_and_private(name, caplog):
    import time

    from src.lib.list_metadata import FIELD_BYTES, FIELD_COUNT

    payload = "(" * 33 + ")" * 33 + " <a.example>" if name == "list-id" else "x" * FIELD_BYTES
    started = time.perf_counter()
    assert normalize_fields(name, [payload] * (FIELD_COUNT + 1)) == (None, "budget_exceeded")
    assert time.perf_counter() - started < 2
    assert normalize_fields(name, ["\x00SYNTHETIC_PRIVATE_MARKER"]) == (None, "malformed")
    assert "SYNTHETIC_PRIVATE_MARKER" not in caplog.text


def test_phrase_utf8_quotes_escapes_and_comment_depth():
    raw = 'Grüße (a(b)) "quoted \\"word\\"" <a.example>'.encode().decode("latin-1")
    assert normalize_fields("list-id", [raw]) == ("a.example", "valid")
    for raw in [
        '"unterminated <a.example>',
        "(" * 33 + ")" * 33 + "<a.example>",
        "\xff <a.example>",
        "a@b <a.example>",
    ]:
        assert normalize_fields("list-id", [raw]) == (None, "malformed")


@pytest.fixture
def metadata_db(tmp_path):
    from src.lib.sqlite import Database

    from tests.conftest import _insert_message, claimant_of
    from tests.test_sqlite import _open_built_db_conn

    conn, path = _open_built_db_conn(tmp_path, "metadata-reader.db")
    cases = [
        ("valid", 1, ["Label <A.Example>"], ["bulk"]),
        ("duplicate", 1, ["<a.example>", "<A.EXAMPLE>"], ["normal", "NORMAL"]),
        ("absent", 1, [], []),
        ("conflict", 1, ["<a.example>", "<b.example>"], ["normal", "other"]),
        ("malformed", 1, ["<a.example> tail"], ["=?ascii?q?bulk?="]),
        ("incomplete", 0, ["<a.example>"], ["list"]),
        ("unassessed", None, [], []),
    ]
    for mid, complete, ids, precedences in cases:
        _insert_message(
            conn,
            message_id=mid,
            thread_id=mid,
            sent_at="2024-01-01T00:00:00+00:00",
            from_=["sender@example.test"],
            completeness={"headers_complete": complete},
        )
        for ordinal, (name, value) in enumerate(
            [*(("list-id", v) for v in ids), *(("precedence", v) for v in precedences)]
        ):
            conn.execute(
                "INSERT INTO message_headers VALUES (?, ?, ?, ?, ?, NULL)",
                (claimant_of(mid), ordinal, name, name, value),
            )
    conn.commit()
    conn.close()
    return Database(str(path))


def where_metadata(name, value, negate=False):
    from src.lib.predicates import Where, normalize_where

    return normalize_where(
        Where.model_validate({"all": [{"leaf": name + "_is", "value": value, "negate": negate}]})
    )


@pytest.mark.parametrize(
    ("name", "operand", "expected"),
    [
        ("list_id", "a.example", {"valid", "duplicate"}),
        ("precedence", "bulk", {"valid"}),
        ("precedence", "unsupported", {"duplicate"}),
        ("list_id", None, {"absent"}),
        ("precedence", None, {"absent"}),
    ],
)
@pytest.mark.parametrize("negate", [False, True])
def test_population_queries_preserve_absence_and_unknown(
    metadata_db, name, operand, expected, negate
):
    known = {"valid", "duplicate", "absent"}
    expected = known - expected if negate else expected
    where = where_metadata(name, operand, negate)
    messages = metadata_db.query_messages(where=where, metadata_fields=(name,), limit=100)
    threads = metadata_db.query_threads(where=where, limit=100)
    aggregate = metadata_db.aggregate_messages(group_by=name, where=where, sample_per_group=2)
    assert {m.message_id for m in messages.messages} == expected
    assert messages.indeterminate == threads.indeterminate_messages == aggregate.indeterminate == 4
    assert (
        messages.total_matches
        == threads.total_matching_messages
        == aggregate.total_matches
        == len(expected)
    )
    assert sum(g.messages for g in aggregate.groups) == len(expected)
    assert aggregate.unknown_group_messages == 0
    assert aggregate.unknown_group_indeterminate_messages == 4


@pytest.mark.parametrize("name", ["list_id", "precedence"])
def test_unknown_group_memberships_are_excluded_not_merged_with_absence(metadata_db, name):
    page = metadata_db.aggregate_messages(group_by=name, sample_per_group=2)
    assert page.total_matches == 7 and page.unknown_group_messages == 4
    assert page.unknown_group_indeterminate_messages == 0
    assert sum(g.messages for g in page.groups) == 3
    absent = next(g for g in page.groups if g.value is None)
    assert absent.messages == 1 and len(absent.samples) == 1
    assert page.metadata_diagnostics[name]["conflict"] == 1


def test_values_are_opt_in_with_status_and_default_does_not_normalize(
    metadata_db, fake_server, monkeypatch
):
    import asyncio

    from src.lib import sqlite as sqlite_module
    from src.tools.retrieval import register_retrieval_tools

    original = sqlite_module.sqlite_normalize_fields
    calls = []

    def counted(name, raw):
        calls.append(name)
        return original(name, raw)

    monkeypatch.setattr(sqlite_module, "sqlite_normalize_fields", counted)
    register_retrieval_tools(fake_server, metadata_db)
    default = asyncio.run(fake_server.tools["query_messages"]())
    assert not calls
    assert "metadata_diagnostics" not in default.structured_content
    assert "list_id" not in default.structured_content["messages"][0]
    page = asyncio.run(fake_server.tools["query_messages"](fields=["list_id"]))
    assert calls
    assert all(
        set(m) == {"claimant_id", "thread_id", "list_id", "list_id_status"}
        for m in page.structured_content["messages"]
    )
    assert page.structured_content["metadata_diagnostics"]["list_id"]["conflict"] == 1


def test_joint_message_budget_covers_both_headers_before_any_normalization(tmp_path):
    import json

    from src.lib.list_metadata import FIELD_BYTES, metadata_result_sql, with_metadata

    from tests.conftest import _insert_message, claimant_of
    from tests.test_sqlite import _open_built_db_conn

    conn, _ = _open_built_db_conn(tmp_path, "joint-budget.db")
    try:
        _insert_message(
            conn, message_id="m", thread_id="t", sent_at=None, completeness={"headers_complete": 1}
        )
        calls = []
        conn.create_function("mcp_list_metadata", 2, lambda *args: calls.append(args))
        # Each header family alone fits; the one per-message combined budget does not.
        for i in range(65):
            name = "list-id" if i < 32 else "precedence"
            conn.execute(
                "INSERT INTO message_headers VALUES (?, ?, ?, ?, ?, NULL)",
                (claimant_of("m"), i, name, name, "x" * FIELD_BYTES),
            )
        rows = conn.execute(
            with_metadata(
                "SELECT "
                + metadata_result_sql("list_id")
                + ", "
                + metadata_result_sql("precedence")
                + " FROM messages m",
                ("list_id", "precedence"),
            )
        ).fetchall()
        assert not calls
        assert all(json.loads(value) == [None, "budget_exceeded"] for value in rows[0])
    finally:
        conn.close()


@pytest.mark.serial
def test_actual_metadata_readers_scale_and_normalize_once_per_statement(tmp_path, monkeypatch):
    import json
    import time

    from src.lib import sqlite as sqlite_module
    from src.lib.sqlite import Database

    from tests.conftest import _insert_message, claimant_of
    from tests.test_sqlite import _open_built_db_conn

    measurements = []
    original_normalize = sqlite_module.sqlite_normalize_fields
    original_connect = __import__("sqlite3").connect
    for size in (5000, 50000):
        conn, path = _open_built_db_conn(tmp_path, f"growth-{size}.db")
        raw = "Synthetic recurring bulletin <series.example>"
        body = " ".join(f"syntheticword{i}" for i in range(30))
        for i in range(size):
            mid = f"m{i:06}"
            _insert_message(
                conn,
                message_id=mid,
                thread_id=mid,
                sent_at="2024-01-01T00:00:00+00:00",
                body=body,
                completeness={"headers_complete": 1},
            )
        conn.executemany(
            "INSERT INTO message_headers VALUES (?, 0, 'List-Id', 'list-id', ?, NULL)",
            ((claimant_of(f"m{i:06}"), raw) for i in range(size)),
        )
        conn.commit()
        conn.close()
        db = Database(str(path))
        where = where_metadata("list_id", "series.example")
        readers = [
            lambda: db.query_messages(where=where, limit=1),
            lambda: db.query_threads(where=where, limit=1),
            lambda: db.aggregate_messages(
                group_by="list_id", where=where, sample_per_group=1, limit=1
            ),
        ]
        # Plain wall-clock measurement, before installing work counters.
        started = time.perf_counter()
        for read in readers:
            read()
        elapsed = time.perf_counter() - started
        work = {"connections": 0, "calls": 0, "bytes": 0}

        def normalize(name, encoded):
            work["calls"] += 1
            work["bytes"] += sum(len(value) for value in json.loads(encoded))
            return original_normalize(name, encoded)

        def connect(*args, **kwargs):
            work["connections"] += 1
            return original_connect(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(sqlite_module, "sqlite_normalize_fields", normalize)
            patch.setattr(__import__("sqlite3"), "connect", connect)
            for read in readers:
                read()
        # Messages: count, uncertainty, leaf counts, page, diagnostics.
        # Threads/aggregate: population statement and diagnostics each.
        assert work["connections"] == 3
        assert work["calls"] == 9 * size
        assert work["bytes"] == work["calls"] * len(raw)
        assert elapsed < 90
        measurements.append(elapsed)
    assert measurements[1] < max(2, measurements[0]) * 18


def test_metadata_status_is_unmeasured_without_parsing(metadata_db, monkeypatch):
    from src.lib import sqlite as sqlite_module

    def forbidden(*args):
        raise AssertionError("status must not normalize headers")

    monkeypatch.setattr(sqlite_module, "sqlite_normalize_fields", forbidden)
    report = metadata_db.get_mailbox_status()["query_capabilities"]
    for name in ("list_id_is", "precedence_is"):
        assert report[name]["status"] == "unmeasured"
        assert all(
            report[name][key] is None
            for key in ("available_messages", "missing_messages", "unassessed_messages")
        )


def test_explicit_null_is_required_and_invalid_operands_are_rejected():
    from pydantic import ValidationError
    from src.lib.predicates import InvalidFilterError, Where, normalize_where

    for name in ("list_id", "precedence"):
        with pytest.raises(ValidationError, match="value is required"):
            Where.model_validate({"all": [{"leaf": name + "_is"}]})
        for value in ("", "<a.example>", "SYNTHETIC_PRIVATE_MARKER"):
            with pytest.raises(InvalidFilterError):
                normalize_where(
                    Where.model_validate({"all": [{"leaf": name + "_is", "value": value}]})
                )


def test_degradation_is_count_only_on_the_call_timing_line(metadata_db, fake_server, caplog):
    import asyncio
    import logging
    import sqlite3

    from src.tools.retrieval import register_retrieval_tools

    with closing(sqlite3.connect(metadata_db.path)) as conn, conn:
        conn.execute(
            "UPDATE message_headers SET raw_value='SYNTHETIC_PRIVATE_MARKER' WHERE name_normalized='list-id'"
        )
    register_retrieval_tools(fake_server, metadata_db)
    with caplog.at_level(logging.INFO):
        result = asyncio.run(fake_server.tools["aggregate_messages"](group_by="list_id"))
    assert result.structured_content["unknown_group_messages"] == 6
    timing = next(
        r.message for r in caplog.records if "tool=aggregate_messages outcome=ok" in r.message
    )
    assert "list_id_malformed" in timing and "unknown_group_messages" in timing
    assert "SYNTHETIC_PRIVATE_MARKER" not in caplog.text
