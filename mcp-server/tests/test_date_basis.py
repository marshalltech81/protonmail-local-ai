"""Selected clocks through the real MCP wire, using synthetic mail only (#1150)."""

import base64
import json
import logging

import pytest
from src.lib.sqlite import Database

from tests.conftest import _insert_attachment, _insert_message, claimant_of
from tests.test_sqlite import _open_built_db_conn
from tests.test_structured_output import _call, _server, _tools, _wire


@pytest.fixture
def clock_server(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "selected-clocks.db")
    # Send and delivery orders disagree; ties exercise both cursor tie-breaks.
    for mid, sent, occurred, status, folder in (
        ("a", "2024-01-01", "2024-03-01", "parsed", "INBOX"),
        ("b", "2024-02-01", "2024-01-01", "parsed", "INBOX"),
        ("c", "2024-02-01", None, "parsed", "INBOX"),
        ("d", None, "2024-03-01", "missing", "INBOX"),
        ("e", None, None, "missing", "INBOX"),
        ("f", "2024-04-01", None, None, "INBOX"),
        ("trash", "2024-05-01", "2024-05-01", "parsed", "Trash"),
    ):
        _insert_message(
            conn,
            message_id=mid,
            thread_id="t",
            folder=folder,
            sent_at=f"{sent}T00:00:00+00:00" if sent else None,
            occurred_at=f"{occurred}T00:00:00+00:00" if occurred else None,
            sent_at_status=status,
            has_attachments=True,
            first_indexed_at="2024-06-01T00:00:00+00:00",
        )
        for n in range(2):
            _insert_attachment(
                conn,
                message_id=mid,
                thread_id="t",
                attachment_id="shared",
                occurrence_id=f"{claimant_of(mid)}:{n}",
                filename="synthetic.pdf",
            )
    conn.commit()
    conn.close()
    return _server(Database(str(path)))


@pytest.mark.parametrize(
    "tool,rows", [("query_messages", "messages"), ("query_attachments", "attachments")]
)
@pytest.mark.parametrize(
    "basis,expected,unknown",
    [
        ("effective", ["e", "f", "d", "a", "c", "b"], 0),
        ("sent", ["c", "b", "a"], 3),
        ("occurred", ["d", "a", "b"], 3),
    ],
)
def test_clock_controls_order_count_and_paging(clock_server, tool, rows, basis, expected, unknown):
    seen = []
    cursor = None
    while True:
        out = _call(clock_server, tool, date_basis=basis, limit=1, cursor=cursor)
        assert out["date_basis"] == basis
        assert out["total_matches"] == len(expected) * (2 if rows == "attachments" else 1)
        assert out["indeterminate"] == unknown * (2 if rows == "attachments" else 1)
        seen.extend(r["claimant_id"] for r in out[rows])
        cursor = out["next_cursor"]
        if not out["has_more"]:
            break
    assert seen == [
        claimant_of(mid) for mid in expected for _ in range(2 if rows == "attachments" else 1)
    ]


@pytest.mark.parametrize(
    "basis,expected,unknown",
    [
        ("effective", ["d", "a", "c"], 2),
        ("sent", ["c", "b"], 3),
        ("occurred", ["d", "a"], 3),
    ],
)
def test_bounds_and_cross_tool_carrying_message_sets(clock_server, basis, expected, unknown):
    sets = []
    for tool, rows, multiplier in (
        ("query_messages", "messages", 1),
        ("query_attachments", "attachments", 2),
    ):
        out = _call(
            clock_server,
            tool,
            date_basis=basis,
            date_from="2024-02-01T01:00:00+01:00",
            date_to="2024-03-01",
            limit=50,
        )
        assert out["date_bounds"] == {
            "date_basis": basis,
            "date_from": "2024-02-01T00:00:00+00:00",
            "date_to": "2024-03-01T23:59:59.999999+00:00",
        }
        assert out["indeterminate"] == unknown * multiplier
        assert out["total_matches"] == len(expected) * multiplier
        sets.append({r["claimant_id"] for r in out[rows]})
    assert sets[0] == sets[1] == {claimant_of(mid) for mid in expected}


@pytest.mark.parametrize("tool", ["query_messages", "query_attachments"])
def test_default_cursors_keep_working_but_other_clocks_reject_them(clock_server, tool):
    first = _call(clock_server, tool, limit=1)
    explicit = _call(clock_server, tool, date_basis=" effective ", limit=1)
    assert first["next_cursor"] == explicit["next_cursor"]
    assert (
        _call(clock_server, tool, date_basis="effective", cursor=first["next_cursor"])["offset"]
        == 1
    )
    for basis in ("sent", "occurred"):
        assert _wire(
            clock_server, tool, {"date_basis": basis, "cursor": first["next_cursor"]}
        ).is_error
        selected = _call(clock_server, tool, date_basis=basis, limit=1)
        assert _wire(
            clock_server, tool, {"date_basis": "effective", "cursor": selected["next_cursor"]}
        ).is_error


@pytest.mark.parametrize("tool", ["query_messages", "query_attachments"])
@pytest.mark.parametrize("value", [None, [], 3, True, "internal", "zq-private-clock-marker"])
def test_invalid_clock_is_timed_and_logged_without_value(clock_server, tool, value, caplog):
    with caplog.at_level(logging.INFO):
        out = _wire(clock_server, tool, {"date_basis": value})
    assert out.is_error
    assert "date_basis" in out.content[0].text
    assert f"rejected invalid argument: {tool}.date_basis" in caplog.text
    assert f"tool={tool}" in caplog.text and "outcome=error" in caplog.text
    assert "zq-private-clock-marker" not in caplog.text


@pytest.mark.parametrize("tool", ["query_messages", "query_attachments"])
def test_clock_and_missing_date_are_visible_without_bounds(clock_server, tool, caplog):
    with caplog.at_level(logging.INFO):
        out = _wire(clock_server, tool, {"date_basis": "occurred"})
    assert not out.is_error
    assert out.structured_content["date_bounds"] is None
    assert "date_basis: occurred" in out.content[0].text
    assert "no delivery date" in out.content[0].text
    assert "'indeterminate_cause_date': 1" in caplog.text


@pytest.mark.parametrize(
    "tool,rows", [("query_messages", "messages"), ("query_attachments", "attachments")]
)
def test_served_schema_describes_selected_clock(clock_server, tool, rows):
    spec = _tools(clock_server)[tool]
    assert spec.input_schema["properties"]["date_basis"]["type"] == "string"
    assert spec.input_schema["properties"]["date_basis"]["default"] == "effective"
    assert "date_basis" in spec.output_schema["properties"][rows]["description"]
    assert "date_basis" in spec.input_schema["properties"]["date_from"]["description"]
    description = spec.input_schema["properties"]["date_basis"]["description"]
    assert all(name in description for name in ("effective", "sent", "occurred"))
    assert "date_basis unchanged" in spec.input_schema["properties"]["cursor"]["description"]
    assert "Keep the same date_basis on every page" in spec.description


@pytest.mark.parametrize(
    "tool,payload",
    [
        (
            "query_messages",
            {
                "v": 2,
                "q": "87abdee2e05f7cac",  # pragma: allowlist secret — synthetic query digest
                "s": "2024-06-01T00:00:00+00:00",
                "m": "e#3f79bb7b435b0532",
                "o": 1,
            },
        ),
        (
            "query_attachments",
            {
                "v": 3,
                "q": "5f553cc61687bbf2",  # pragma: allowlist secret — synthetic query digest
                "s": "2024-06-01T00:00:00+00:00",
                "m": "e#3f79bb7b435b0532",
                "a": "e#3f79bb7b435b0532:1",
                "o": 1,
            },
        ),
    ],
)
def test_pre_change_effective_cursors_remain_valid(clock_server, tool, payload):
    # Payloads decoded from HEAD's pre-#1150 cursors on this synthetic fixture.
    cursor = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    first = _call(clock_server, tool, limit=1)
    assert first["next_cursor"] == cursor
    resumed = _call(clock_server, tool, date_basis="effective", cursor=cursor)
    assert resumed["offset"] == 1
    assert resumed["returned"] == first["total_matches"] - 1


@pytest.mark.parametrize("tool", ["query_messages", "query_attachments"])
def test_clock_rejections_are_rate_limited(clock_server, tool, caplog):
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            assert _wire(clock_server, tool, {"date_basis": "zq-private-clock-marker"}).is_error
    assert caplog.text.count(f"rejected invalid argument: {tool}.date_basis") == 1
    assert caplog.text.count(f"tool={tool} outcome=error") == 3
    assert "zq-private-clock-marker" not in caplog.text


@pytest.mark.parametrize("tool", ["query_messages", "query_attachments"])
def test_missing_clock_outside_scope_does_not_inflate_indeterminate(clock_server, tool):
    out = _call(clock_server, tool, date_basis="sent", folder="Trash")
    assert out["indeterminate"] == 0
    assert out["total_matches"] == (1 if tool == "query_messages" else 2)


@pytest.mark.parametrize("tool", ["query_messages", "query_attachments"])
@pytest.mark.parametrize("basis", ["sent", "occurred"])
def test_unfiltered_selected_clock_summary_discloses_missing_dates(clock_server, tool, basis):
    out = _wire(clock_server, tool, {"date_basis": basis})
    assert not out.is_error
    summary = out.content[0].text.splitlines()[0]
    assert "every indexed" not in summary
    assert "missing selected dates are indeterminate" in summary
    assert out.structured_content["indeterminate"] > 0


@pytest.mark.parametrize("tool", ["query_messages", "query_attachments"])
def test_blank_clock_means_effective(clock_server, tool):
    assert _call(clock_server, tool, date_basis="") == _call(clock_server, tool)


@pytest.mark.parametrize(
    "tool,rows", [("query_messages", "messages"), ("query_attachments", "attachments")]
)
@pytest.mark.parametrize("basis", ["sent", "occurred"])
def test_bounded_cursor_walk_preserves_fractional_clock_text(tmp_path, tool, rows, basis):
    conn, path = _open_built_db_conn(tmp_path, "fractional-clocks.db")
    clocks = [
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T00:00:00.001000+00:00",
        "2024-01-01T00:00:00.010000+00:00",
        "2024-01-01T00:00:00.100000+00:00",
        "2024-01-01T00:00:00.900000+00:00",
        "2024-01-01T00:00:01+00:00",
    ]
    stored = {}
    for i, sent in enumerate(clocks):
        mid = f"fraction-{i}"
        occurred = clocks[-i - 1]
        stored[claimant_of(mid)] = sent if basis == "sent" else occurred
        _insert_message(
            conn,
            message_id=mid,
            thread_id="t",
            sent_at=sent,
            occurred_at=occurred,
            has_attachments=True,
        )
        for n in range(2):
            _insert_attachment(
                conn,
                message_id=mid,
                thread_id="t",
                attachment_id="shared",
                occurrence_id=f"{claimant_of(mid)}:{n}",
                filename="synthetic.pdf",
            )
    conn.commit()
    conn.close()
    server = _server(Database(str(path)))
    cursor = None
    seen = []
    occurrences = []
    multiplier = 1 if rows == "messages" else 2
    while True:
        out = _call(
            server,
            tool,
            date_basis=basis,
            limit=1,
            cursor=cursor,
            date_from=clocks[0],
            date_to=clocks[4],
        )
        assert out["total_matches"] == 5 * multiplier
        assert out["indeterminate"] == 0
        seen.extend(r["claimant_id"] for r in out[rows])
        if rows == "attachments":
            occurrences.extend(r["attachment_occurrence_id"] for r in out[rows])
        if not out["has_more"]:
            break
        cursor = out["next_cursor"]
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        assert payload["s"] == stored[out[rows][-1]["claimant_id"]]
    expected = range(4, -1, -1) if basis == "sent" else range(1, 6)
    assert seen == [claimant_of(f"fraction-{i}") for i in expected for _ in range(multiplier)]
    if rows == "attachments":
        assert occurrences == [
            f"{claimant_of(f'fraction-{i}')}:{n}" for i in expected for n in (1, 0)
        ]
