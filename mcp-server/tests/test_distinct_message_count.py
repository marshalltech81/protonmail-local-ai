"""The first, count-only delivery of the approved #991 contract."""

import sqlite3
from contextlib import closing

import pytest

from tests.test_structured_output import _server, _tools, _wire


@pytest.fixture
def conflicting_ids(messages_db):
    # Separate claimants with different subjects/senders share a claimed ID.
    with closing(sqlite3.connect(messages_db.path)) as conn:
        conn.execute(
            "UPDATE messages SET message_id = 'shared@example.test' WHERE message_id IN ('m1', 'm2')"
        )
        conn.execute("UPDATE messages SET sender_ambiguous = NULL WHERE message_id = 'm5'")
        conn.commit()
    return messages_db


@pytest.mark.parametrize(
    ("filters", "total", "distinct", "unknown"),
    [
        ({}, 5, 4, 0),
        ({"subject": "budget"}, 2, 1, 0),
        ({"sender": "jane@example.com"}, 2, 2, 1),
        ({"subject": "not present"}, 0, 0, 0),
        ({"subject": "budget", "sender": "jane@example.com"}, 1, 1, 0),
    ],
)
def test_counts_definite_matches_before_paging(conflicting_ids, filters, total, distinct, unknown):
    page = conflicting_ids.query_messages(limit=1, **filters)
    assert (page.total_matches, page.distinct_message_ids, page.indeterminate) == (
        total,
        distinct,
        unknown,
    )
    while page.next_cursor:
        page = conflicting_ids.query_messages(limit=1, cursor=page.next_cursor, **filters)
        assert (page.total_matches, page.distinct_message_ids, page.indeterminate) == (
            total,
            distinct,
            unknown,
        )


def test_distinct_count_is_served_with_an_explicit_unit(conflicting_ids):
    server = _server(conflicting_ids)
    schema = _tools(server)["query_messages"].output_schema
    description = schema["properties"]["distinct_message_ids"]["description"]
    assert "Message-ID" in description and "sender-controlled" in description
    result = _wire(server, "query_messages", {"limit": 1, "fields": ["subject"]})
    assert not result.is_error
    assert result.structured_content["total_matches"] == 5
    assert result.structured_content["distinct_message_ids"] == 4
    assert "distinct_message_ids: 4" in result.content[0].text
    assert len(result.structured_content["messages"]) == 1
