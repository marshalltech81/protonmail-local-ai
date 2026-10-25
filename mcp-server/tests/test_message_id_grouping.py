"""The owner-approved #991 grouped view; all records are synthetic."""

import sqlite3
from contextlib import closing

import pytest
from src.lib.predicates import InvalidFilterError, Where, normalize_where

from tests.test_structured_output import _server, _wire


@pytest.fixture
def grouped_db(messages_db):
    with closing(sqlite3.connect(messages_db.path)) as conn:
        conn.execute(
            "UPDATE messages SET message_id='shared@example.test' WHERE message_id IN ('m1','m2')"
        )
        conn.execute(
            "UPDATE messages SET message_id='second@example.test' WHERE message_id IN ('m3','m5')"
        )
        conn.commit()
    return messages_db


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"subject": "budget"},
        {"sender": "jane@example.com"},
        {"recipient": "jane@example.com"},
        {"folder": "Archive"},
        {"has_attachments": True},
        {"subject": "budget", "sender": "bob@example.com"},
        {"subject": "not present"},
        {"date_basis": "sent"},
        {
            "where": normalize_where(
                Where.model_validate(
                    {
                        "all": [
                            {
                                "any": [
                                    {"leaf": "subject_contains", "value": "budget"},
                                    {"leaf": "subject_contains", "value": "lunch"},
                                ]
                            }
                        ]
                    }
                )
            )
        },
    ],
)
def test_grouped_pages_equal_client_grouping(grouped_db, filters):
    ungrouped = grouped_db.query_messages(limit=100, **filters)
    groups = {}
    for record in ungrouped.messages:
        groups.setdefault(record.message_id, []).append(record)
    expected = [
        min(records, key=lambda r: (r.effective_at, r.claimant_id)) for records in groups.values()
    ]
    expected.sort(
        key=lambda r: (
            r.sent_at if filters.get("date_basis") == "sent" else r.effective_at,
            r.claimant_id,
        ),
        reverse=True,
    )
    found = []
    cursor = None
    while True:
        page = grouped_db.query_messages(collapse="message_id", limit=1, cursor=cursor, **filters)
        assert page.total_matches == len(groups) == page.distinct_message_ids
        found.extend(page.messages)
        for record in page.messages:
            assert record.claimant_count == len(groups[record.message_id])
        if not page.next_cursor:
            break
        cursor = page.next_cursor
    assert [r.claimant_id for r in found] == [r.claimant_id for r in expected]


def test_unknown_copy_does_not_make_a_matching_group_indeterminate(grouped_db):
    with closing(sqlite3.connect(grouped_db.path)) as conn:
        conn.execute(
            "UPDATE messages SET sender_ambiguous=NULL WHERE message_id='second@example.test'"
        )
        conn.execute("UPDATE messages SET sender_ambiguous=NULL WHERE subject='Re: Budget review'")
        conn.commit()
    page = grouped_db.query_messages(collapse="message_id", sender="jane@example.com")
    assert (page.total_matches, page.indeterminate) == (1, 1)
    assert page.messages[0].claimant_count == 1
    assert page.messages[0].subject == "Budget review"


def test_leaves_cannot_be_satisfied_by_different_claimants(grouped_db):
    page = grouped_db.query_messages(
        collapse="message_id", subject="Re: Budget", sender="jane@example.com"
    )
    assert page.total_matches == 0


def test_grouping_mode_is_bound_into_cursors(grouped_db):
    for first, second in [(None, "message_id"), ("message_id", None)]:
        page = grouped_db.query_messages(collapse=first, limit=1)
        assert page.next_cursor
        with pytest.raises(InvalidFilterError):
            grouped_db.query_messages(collapse=second, limit=1, cursor=page.next_cursor)


def test_bad_grouping_mode_is_rejected(grouped_db):
    with pytest.raises(InvalidFilterError):
        grouped_db.query_messages(collapse="anything")


def test_grouped_output_keeps_count_when_fields_are_projected(grouped_db):
    result = _wire(
        _server(grouped_db),
        "query_messages",
        {"collapse": "message_id", "subject": "budget", "fields": ["subject"]},
    )
    assert not result.is_error
    data = result.structured_content
    assert data["collapse"] == "message_id"
    assert data["total_matches"] == 1
    assert data["messages"][0]["claimant_count"] == 2
    assert "claimant_count: 2" in result.content[0].text


def test_representative_prefers_matching_claimant_with_a_nontrash_copy(grouped_db):
    with closing(sqlite3.connect(grouped_db.path)) as conn:
        conn.row_factory = sqlite3.Row
        claimants = conn.execute(
            "SELECT claimant_id FROM messages WHERE message_id='shared@example.test' ORDER BY effective_at"
        ).fetchall()
        early, later = [row[0] for row in claimants]
        conn.execute("UPDATE message_occurrences SET folder='Trash' WHERE claimant_id=?", (early,))
        row = dict(
            conn.execute(
                "SELECT * FROM message_occurrences WHERE claimant_id=?", (later,)
            ).fetchone()
        )
        row.pop("id")
        row.update(
            folder="Trash",
            uniq="synthetic-trash-copy",
            relative_path="Trash/cur/synthetic-trash-copy",
        )
        columns = ",".join(row)
        conn.execute(
            f"INSERT INTO message_occurrences ({columns}) VALUES ({','.join('?' for _ in row)})",
            list(row.values()),
        )
        conn.commit()
    page = grouped_db.query_messages(collapse="message_id", folder="Trash")
    assert page.total_matches == 1
    assert page.messages[0].claimant_id == later
    assert page.messages[0].claimant_count == 2


def test_equal_representative_times_use_claimant_id(grouped_db):
    with closing(sqlite3.connect(grouped_db.path)) as conn:
        conn.execute(
            "UPDATE messages SET sent_at='2024-01-01T00:00:00+00:00' WHERE message_id='shared@example.test'"
        )
        expected = conn.execute(
            "SELECT MIN(claimant_id) FROM messages WHERE message_id='shared@example.test'"
        ).fetchone()[0]
        conn.commit()
    page = grouped_db.query_messages(collapse="message_id", subject="budget")
    assert page.messages[0].claimant_id == expected


def test_diagnostic_schema_keeps_claimant_units_under_grouping():
    from src.tools.outputs import QueryMessagesOutput

    properties = QueryMessagesOutput.model_json_schema()["properties"]
    assert "Message-ID groups" not in properties["metadata_diagnostics"]["description"]
    assert "before grouping" in properties["leaf_results"]["description"]
