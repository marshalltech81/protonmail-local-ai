"""Exact population predicates; all mail and attachment text is synthetic."""

import asyncio
import logging

import pytest
from src.lib.predicates import InvalidFilterError, compile_where, normalize_where
from src.lib.sqlite import Database

from tests.conftest import _insert_attachment, _insert_message, claimant_of
from tests.test_retrieval import _handlers
from tests.test_sqlite import _open_built_db_conn
from tests.test_where import _where


@pytest.fixture
def population_db(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "population.db")
    for i, (subject, subject_complete, text, complete, manifest) in enumerate(
        [
            ("Straße review", 1, "alpha budget", 1, 1),
            ("other", 1, None, 0, 1),
            ("other", 0, "omega", 1, 1),
            ("other", None, "omega", None, 1),
            ("other", 1, "omega", 1, 0),
            ("other", 1, "alpha budget", 0, 1),
            ("other", 1, None, None, 1),
        ]
    ):
        mid = f"m{i}"
        _insert_message(
            conn,
            message_id=mid,
            thread_id=f"t{i}",
            sent_at=f"2024-01-01T00:0{i}:00+00:00",
            subject=subject,
            body="alpha budget",
            attachment_text=text,
            has_attachments=i != 6,
            completeness={
                "subject_complete": subject_complete,
                "attachments_manifest_complete": manifest,
            },
        )
        if i != 6:
            _insert_attachment(
                conn,
                message_id=mid,
                thread_id=f"t{i}",
                attachment_id=f"m{i}-att",
                filename="test.pdf",
            )
            conn.execute(
                "UPDATE attachments SET text_complete = ? WHERE claimant_id = ?",
                (complete, claimant_of(mid)),
            )
    conn.commit()
    conn.close()
    return Database(str(path))


def _query(db, *items):
    return db.query_messages(where=normalize_where(_where(*items)), limit=50)


def test_subject_and_attachment_union_is_one_population(population_db):
    page = _query(
        population_db,
        {
            "any": [
                {"leaf": "subject_contains", "value": "STRASSE"},
                {"leaf": "attachment_text_words", "value": "alpha budget"},
            ]
        },
    )
    assert {m.message_id for m in page.messages} == {"m0", "m5"}
    assert (page.total_matches, page.indeterminate) == (2, 4)
    assert [r.leaf for r in page.leaf_results] == ["subject_contains", "attachment_text_words"]


def test_negation_never_claims_absence_after_failed_or_incomplete_extraction(population_db):
    page = _query(
        population_db, {"leaf": "attachment_text_words", "value": "alpha budget", "negate": True}
    )
    assert {m.message_id for m in page.messages} == {"m2", "m6"}
    assert (page.total_matches, page.indeterminate) == (2, 3)


def test_subject_reuses_flat_subject_semantics(population_db):
    explicit = _query(population_db, {"leaf": "subject_contains", "value": "STRASSE"})
    flat = population_db.query_messages(subject="STRASSE", limit=50)
    assert (explicit.messages, explicit.total_matches, explicit.indeterminate) == (
        flat.messages,
        flat.total_matches,
        flat.indeterminate,
    )


def test_attachment_words_have_shared_cap_and_one_fts_subquery_per_word():
    leaves = normalize_where(
        _where({"leaf": "attachment_text_words", "value": "alpha alpha budget"})
    )
    sql, params = compile_where(leaves)
    assert sql.count("message_chunks_fts MATCH ?") == 2
    assert params == ['"alpha"', '"budget"']
    with pytest.raises(InvalidFilterError, match="at most 16 words"):
        normalize_where(
            _where(
                {"leaf": "attachment_text_words", "value": " ".join(f"word{i}" for i in range(17))}
            )
        )


def test_tool_reports_counts_causes_and_does_not_log_values(population_db, caplog, fake_server):
    handlers = _handlers(fake_server, population_db)
    with caplog.at_level(logging.INFO):
        result = asyncio.run(
            handlers["query_messages"](
                where=_where({"leaf": "attachment_text_words", "value": "SYNTHETIC_PRIVATE_MARKER"})
            )
        )
    assert result.structured_content["indeterminate"] == 4
    assert "attachment_text" in caplog.text
    assert "SYNTHETIC_PRIVATE_MARKER" not in caplog.text
    grouped = asyncio.run(
        handlers["aggregate_messages"](
            group_by="folder", where=_where({"leaf": "attachment_text_words", "value": "alpha"})
        )
    )
    assert grouped.structured_content["total_matches"] == 2
    assert grouped.structured_content["indeterminate"] == 3


@pytest.fixture
def header_db(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "headers.db")
    for i, complete in enumerate([1, 0, None, 1]):
        _insert_message(
            conn,
            message_id=f"h{i}",
            thread_id=f"t{i}",
            sent_at=f"2024-01-01T00:0{i}:00+00:00",
            subject="synthetic",
        )
        conn.execute(
            "UPDATE messages SET headers_complete = ? WHERE claimant_id = ?",
            (complete, claimant_of(f"h{i}")),
        )
    conn.executemany(
        "INSERT INTO message_headers VALUES (?, ?, ?, ?, ?, ?)",
        [
            (claimant_of("h0"), 0, "X-List", "x-list", "first", "first"),
            (claimant_of("h0"), 1, "X-List", "x-list", "second", "Straße"),
            (claimant_of("h1"), 0, "X-List", "x-list", "SYNTHETIC_PRIVATE_MARKER", None),
            (claimant_of("h3"), 0, "X-Empty", "x-empty", "", ""),
        ],
    )
    conn.commit()
    conn.close()
    return Database(str(path))


def test_header_presence_and_duplicate_decoded_contains(header_db):
    presence = _query(header_db, {"leaf": "header_exists", "name": "X-LIST"})
    assert (presence.total_matches, presence.indeterminate) == (2, 1)
    contains = _query(header_db, {"leaf": "header_contains", "name": "X-List", "value": "STRASSE"})
    assert (contains.total_matches, contains.indeterminate) == (1, 2)
    raw = _query(
        header_db, {"leaf": "header_contains", "name": "X-List", "value": "PRIVATE_MARKER"}
    )
    assert {m.message_id for m in raw.messages} == {"h1"}
    empty = _query(header_db, {"leaf": "header_exists", "name": "X-Empty"})
    assert {m.message_id for m in empty.messages} == {"h3"}


def test_header_negation_keeps_capped_or_unassessed_absence_unknown(header_db):
    page = _query(header_db, {"leaf": "header_exists", "name": "List-ID", "negate": True})
    assert (page.total_matches, page.indeterminate) == (2, 2)
    assert {m.message_id for m in page.messages} == {"h0", "h3"}


def test_header_values_are_not_logged(header_db, fake_server, caplog):
    handlers = _handlers(fake_server, header_db)
    with caplog.at_level(logging.INFO):
        result = asyncio.run(
            handlers["query_messages"](
                where=_where(
                    {
                        "leaf": "header_contains",
                        "name": "X-List",
                        "value": "SYNTHETIC_PRIVATE_MARKER",
                    }
                )
            )
        )
    assert result.structured_content["total_matches"] == 1
    assert "indeterminate_cause_headers" in caplog.text
    assert "SYNTHETIC_PRIVATE_MARKER" not in caplog.text
    assert "X-List" not in caplog.text


@pytest.mark.parametrize("name", ["", "x" * 129, "X:List", "X List", "X-É", "X\nList"])
def test_header_name_is_bounded_ascii_token(name):
    with pytest.raises(InvalidFilterError, match="1 to 128 ASCII token"):
        normalize_where(_where({"leaf": "header_exists", "name": name}))


def test_header_contract_refuses_irrelevant_arguments_and_bounds_values():
    from pydantic import ValidationError

    for item in [
        {"leaf": "header_exists", "name": "X-List", "value": None},
        {"leaf": "header_exists", "name": "X-List", "role": None},
        {"leaf": "header_contains", "name": "X-List"},
        {"leaf": "body_words", "value": "alpha", "name": None},
    ]:
        with pytest.raises(ValidationError):
            _where(item)
    for value in ["", " " * 10, "x" * 501]:
        with pytest.raises(InvalidFilterError):
            normalize_where(_where({"leaf": "header_contains", "name": "X-List", "value": value}))


def test_header_capability_reports_pending_and_capped_messages(header_db, fake_server):
    from src.tools.system import register_system_tools

    register_system_tools(fake_server, header_db)
    handler = fake_server.tools["get_mailbox_status"]
    result = asyncio.run(handler())
    assert result.structured_content["header_predicates"] == {
        "status": "supported after backfill",
        "unassessed_messages": 1,
        "incomplete_messages": 1,
    }
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(header_db.path)) as conn:
        conn.execute("UPDATE messages SET headers_complete = 1 WHERE headers_complete IS NULL")
        conn.commit()
    result = asyncio.run(handler())
    assert result.structured_content["header_predicates"]["status"] == "supported"
    assert result.structured_content["header_predicates"]["incomplete_messages"] == 1


@pytest.mark.parametrize(
    "item",
    [
        {"leaf": "header_exists", "name": "X-List"},
        {"leaf": "header_contains", "name": "X-List", "value": "first"},
    ],
)
def test_header_query_plan_uses_name_claimant_index(header_db, item):
    from contextlib import closing

    sql, params = compile_where(normalize_where(_where(item)))
    with closing(header_db._connect()) as conn:
        plan = [
            r[3]
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM messages m WHERE " + sql, params
            )
        ]
    assert any(
        "idx_message_headers_name_claimant" in line and "name_normalized=?" in line for line in plan
    )


def test_header_unknown_is_rescued_by_a_true_or_leaf_but_not_by_and(header_db):
    header = {"leaf": "header_exists", "name": "X-List"}
    subject = {"leaf": "subject_contains", "value": "synthetic"}
    union = _query(header_db, {"any": [header, subject]})
    assert (union.total_matches, union.indeterminate) == (4, 0)
    intersection = _query(header_db, header, subject)
    assert (intersection.total_matches, intersection.indeterminate) == (2, 1)
