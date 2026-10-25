"""Typed state, size and date predicates preserve unknowns under OR/NOT."""

import pytest
from pydantic import ValidationError
from src.lib.predicates import InvalidFilterError, Where, normalize_where
from src.lib.sqlite import Database

from tests.conftest import _insert_message
from tests.test_evidence_scope import _finish_threads
from tests.test_occurrence_predicates import mailbox as mailbox
from tests.test_sqlite import _open_built_db_conn
from tests.test_structured_output import _call, _wire
from tests.test_where import _server


def normalized(*items):
    return normalize_where(Where.model_validate({"all": list(items)}))


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("seen", True, True),
        ("flagged", False, False),
        ("replied", True, True),
        ("has_attachments", False, False),
        ("size_min", 0, 0),
        ("size_max", 2**63 - 1, 2**63 - 1),
        ("folder", " Archive ", ("Archive",)),
        ("authority_class", "personal", "personal"),
        ("sent_from", "2024-01-01", "2024-01-01T00:00:00+00:00"),
        ("sent_to", "2024-01-01", "2024-01-01T23:59:59.999999+00:00"),
        ("occurred_from", "2024-01-01T03:00:00+03:00", "2024-01-01T00:00:00+00:00"),
        ("occurred_to", "2024-01-01", "2024-01-01T23:59:59.999999+00:00"),
        ("effective_from", "2024-01-01", "2024-01-01T00:00:00+00:00"),
        ("effective_to", "2024-01-01", "2024-01-01T23:59:59.999999+00:00"),
    ],
)
def test_typed_normalization(name, value, expected):
    leaf = normalized({"leaf": name, "value": value})[0].leaf
    assert leaf.name == name
    assert type(leaf.value) is type(expected)
    assert leaf.value == expected


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("seen", "false"),
        ("flagged", 1),
        ("replied", None),
        ("has_attachments", 0),
        ("size_min", True),
        ("size_max", "10"),
        ("size_min", -1),
        ("size_max", 2**63),
        ("size_min", 1.5),
        ("folder", False),
        ("folder", ""),
        ("sent_from", True),
        ("authority_class", "unknown"),
        ("body_words", True),
        ("subject_contains", 17),
        ("list_id_is", False),
    ],
)
def test_wrong_types_and_values_rejected(name, value):
    with pytest.raises((InvalidFilterError, ValidationError)):
        normalized({"leaf": name, "value": value})


def test_negated_size_or_flag_preserves_unknown(typed_db):
    where = normalized(
        {
            "any": [
                {"leaf": "size_min", "value": 100, "negate": True},
                {"leaf": "seen", "value": True},
            ]
        }
    )
    page = typed_db.query_messages(where=where)
    assert (page.total_matches, page.indeterminate) == (2, 1)


@pytest.fixture
def typed_db(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "typed.db")
    for index, (size, seen) in enumerate([(None, False), (None, True), (50, False), (150, False)]):
        _insert_message(
            conn,
            message_id=f"m{index}",
            thread_id=f"t{index}",
            sent_at=None,
            size_bytes=size,
            seen=seen,
        )
    _finish_threads(conn)
    conn.close()
    return Database(str(path))


def test_typed_expression_across_all_three_readers(typed_db):
    where = normalized(
        {
            "any": [
                {"leaf": "size_min", "value": 100, "negate": True},
                {"leaf": "seen", "value": True},
            ]
        }
    )
    page = typed_db.query_messages(where=where, limit=1)
    assert (page.total_matches, page.indeterminate) == (2, 1)
    assert page.next_cursor
    following = typed_db.query_messages(where=where, limit=1, cursor=page.next_cursor)
    assert following.messages[0].claimant_id != page.messages[0].claimant_id
    threads = typed_db.query_threads(where=where, limit=1)
    assert (threads.total_matching_messages, threads.indeterminate_messages) == (2, 1)
    groups = typed_db.aggregate_messages(where=where, group_by="folder", limit=1)
    assert (groups.total_matches, groups.indeterminate) == (2, 1)


def test_wire_typed_expression_and_clock_diagnostics(typed_db):
    server = _server(typed_db)
    result = _call(
        server,
        "query_messages",
        where={
            "all": [
                {
                    "any": [
                        {"leaf": "seen", "value": True},
                        {"leaf": "sent_from", "value": "2024-01-01"},
                    ]
                }
            ]
        },
    )
    assert (result["total_matches"], result["indeterminate"]) == (1, 3)
    wire = _wire(
        server,
        "query_messages",
        {
            "where": {
                "all": [
                    {"leaf": "sent_from", "value": "2024-01-01"},
                ]
            }
        },
    )
    assert "no parseable Date header, or not yet checked" in wire.content[0].text


@pytest.mark.parametrize("reader", ["query_messages", "query_threads", "aggregate_messages"])
def test_boolean_folder_and_flags_stay_on_one_copy(mailbox, reader):
    where = normalize_where(
        Where.model_validate(
            {
                "all": [
                    {"leaf": "folder", "value": "INBOX"},
                    {"leaf": "flagged", "value": True},
                ]
            }
        )
    )
    args = {"where": where}
    if reader == "aggregate_messages":
        args["group_by"] = "folder"
    page = getattr(mailbox, reader)(**args)
    assert (page.total_matching_messages if reader == "query_threads" else page.total_matches) == 0


def test_boolean_copy_groups_and_negation_use_the_matching_copy(mailbox):
    where = normalize_where(
        Where.model_validate(
            {
                "all": [
                    {"leaf": "folder", "value": "Archive"},
                    {"leaf": "seen", "value": True, "negate": True},
                ]
            }
        )
    )
    page = mailbox.query_messages(where=where)
    assert [m.message_id for m in page.messages] == ["two"]
    groups = mailbox.aggregate_messages(where=where, group_by="folder")
    assert [(g.value, g.messages) for g in groups.groups] == [("Archive", 1)]


def test_typed_flags_cannot_qualify_through_an_out_of_scope_trash_copy(mailbox):
    import sqlite3
    from contextlib import closing

    from tests.conftest import claimant_of, insert_occurrence

    with closing(sqlite3.connect(mailbox.path)) as conn:
        insert_occurrence(
            conn, claimant_of("plain"), "Trash", "Trash/cur/plain-flagged", flagged=True
        )
        conn.commit()
    page = mailbox.query_messages(
        where=normalize_where(Where.model_validate({"all": [{"leaf": "flagged", "value": True}]}))
    )
    assert [m.message_id for m in page.messages] == ["two"]


def test_a_decided_copy_does_not_turn_another_copys_unknown_filter_true(mailbox):
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(mailbox.path)) as conn:
        conn.execute("UPDATE messages SET body_complete=NULL")
        conn.commit()
    where = normalize_where(
        Where.model_validate(
            {
                "all": [
                    {
                        "any": [
                            {"leaf": "flagged", "value": True},
                            {"leaf": "body_words", "value": "absent"},
                        ]
                    }
                ]
            }
        )
    )
    page = mailbox.aggregate_messages(where=where, group_by="folder")
    assert (page.total_matches, page.indeterminate) == (1, 1)
    assert {g.value: (g.messages, g.indeterminate) for g in page.groups} == {
        "Archive": (1, 0),
        "INBOX": (0, 2),
    }


def test_copy_leaf_counts_do_not_absorb_an_unknown_flat_message_filter(mailbox):
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(mailbox.path)) as conn:
        conn.execute("UPDATE messages SET sender_ambiguous=NULL")
        conn.commit()
    page = mailbox.query_messages(
        authority_class="vendor",
        where=normalize_where(
            Where.model_validate({"all": [{"leaf": "folder", "value": "INBOX"}]})
        ),
    )
    assert (page.total_matches, page.indeterminate) == (0, 2)
    [leaf] = page.leaf_results
    assert (leaf.true, leaf.false, leaf.indeterminate) == (2, 0, 0)


@pytest.mark.parametrize(
    "flags,expected", [((False, True), (1, 0)), ((False, False), (0, 1)), ((True, True), (1, 0))]
)
def test_folder_truth_collapses_copies_with_true_before_unknown(empty_db, flags, expected):
    import sqlite3
    from contextlib import closing

    from tests.conftest import claimant_of, insert_occurrence

    with closing(sqlite3.connect(empty_db.path)) as conn:
        _insert_message(
            conn,
            message_id="same-folder",
            thread_id="one-thread",
            sent_at="2024-01-01T00:00:00+00:00",
            size_bytes=None,
            flagged=flags[0],
        )
        insert_occurrence(
            conn, claimant_of("same-folder"), "INBOX", "INBOX/cur/another-copy", flagged=flags[1]
        )
        _finish_threads(conn)
        conn.commit()
    where = normalized(
        {"any": [{"leaf": "flagged", "value": True}, {"leaf": "size_min", "value": 100}]}
    )
    messages = empty_db.query_messages(where=where)
    assert (messages.total_matches, messages.indeterminate) == expected
    groups = empty_db.aggregate_messages(where=where, group_by="folder")
    assert (groups.total_matches, groups.indeterminate) == expected
    assert [(g.value, g.messages, g.indeterminate) for g in groups.groups] == [("INBOX", *expected)]
