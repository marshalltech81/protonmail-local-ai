"""Unknown send dates (#1080).

A message whose ``Date:`` header is missing or unparseable is stored
with ``sent_at`` NULL and a ``sent_at_status`` saying why; one indexed
before the upgrade keeps its old ``sent_at`` with the status NULL (not
yet assessed) until its reparse. ``effective_at`` orders an undated
message by ``first_indexed_at``, which no date bound reads: under a
bound such a message is indeterminate, never matched or left out.
"""

import asyncio
import logging

import pytest
import src.lib.sqlite as sqlite_mod
from fastmcp.exceptions import ToolError
from src.lib.predicates import InvalidFilterError
from src.lib.sqlite import Database
from src.tools.outputs import GetMessageOutput, ListedMessage
from src.tools.retrieval import register_retrieval_tools

from tests.conftest import (
    FakeMCPServer,
    _insert_attachment,
    _insert_extraction,
    _insert_message,
    claimant_of,
)
from tests.test_evidence_scope import _finish_threads
from tests.test_sqlite import _open_built_db_conn

_MARKER = "Zq-undated-marker-1080"
_MARCH = {"date_from": "2024-03-01", "date_to": "2024-03-31"}

# message_id -> (sent_at, sent_at_status, occurred_at, first_indexed_at)
_ROWS = {
    "dated": ("2024-03-10T09:00:00+00:00", "parsed", None, "2024-03-10T10:00:00+00:00"),
    "delivered": (None, "missing", "2024-03-12T09:00:00+00:00", "2024-03-12T10:00:00+00:00"),
    # Undated, first indexed inside March: never matched by that.
    "undated": (None, "missing", None, "2024-03-15T09:00:00+00:00"),
    # Undated, first indexed outside March: never left out by that.
    "undated_out": (None, "invalid", None, "2026-01-01T09:00:00+00:00"),
    # Indexed before the upgrade, not yet re-parsed: its sent_at may be
    # the old indexer's fallback.
    "unchecked": ("2024-03-20T09:00:00+00:00", None, None, "2024-03-20T10:00:00+00:00"),
    "unchecked_delivered": (
        "2024-03-21T09:00:00+00:00",
        None,
        "2024-03-22T09:00:00+00:00",
        "2024-03-21T10:00:00+00:00",
    ),
}


@pytest.fixture
def dates_db(tmp_path) -> Database:
    conn, path = _open_built_db_conn(tmp_path, "dates.db")
    for message_id, (sent, status, occurred, first) in _ROWS.items():
        _insert_message(
            conn,
            message_id=message_id,
            thread_id=f"t-{message_id}",
            subject=f"{_MARKER} {message_id}",
            sent_at=sent,
            sent_at_status=status,
            occurred_at=occurred,
            first_indexed_at=first,
            from_=["alice@example.test"],
            body=f"{_MARKER} body",
        )
        _insert_attachment(
            conn,
            message_id=message_id,
            thread_id=f"t-{message_id}",
            attachment_id=f"att-{message_id}",
            filename=f"{message_id}.pdf",
        )
        _insert_extraction(conn, attachment_id=f"att-{message_id}", extracted_text="text")
    _finish_threads(conn)
    conn.commit()
    conn.close()
    return Database(str(path))


def _shown(sent: str | None, status: str | None) -> str | None:
    """The send date a tool returns: only a parsed one (review round 1:
    a row not yet assessed may hold the v7 fallback, so it is withheld)."""
    return sent if status == "parsed" else None


def _ids(records) -> list[str]:
    return [r.message_id for r in records]


class TestQueryMessages:
    def test_an_effective_bound_never_reads_the_first_index_time(self, dates_db):
        page = dates_db.query_messages(**_MARCH)
        assert sorted(_ids(page.messages)) == ["dated", "delivered", "unchecked_delivered"]
        assert page.total_matches == 3
        # Neither the one first indexed in March nor the one outside it
        # is decided; nor is a send date not yet checked.
        assert page.indeterminate == 3

    def test_without_a_bound_every_message_is_listed_by_effective_time(self, dates_db):
        page = dates_db.query_messages()
        expected = [
            "undated_out",
            "unchecked_delivered",
            "unchecked",
            "undated",
            "delivered",
            "dated",
        ]
        assert _ids(page.messages) == expected
        assert (page.total_matches, page.indeterminate) == (6, 0)
        listed = []
        cursor = None
        while True:
            part = dates_db.query_messages(limit=2, cursor=cursor)
            listed += _ids(part.messages)
            if not part.has_more:
                break
            cursor = part.next_cursor
        assert listed == expected

    def test_the_sent_basis_reads_parsed_send_dates_only(self, dates_db):
        page = dates_db.query_messages(date_basis="sent")
        assert _ids(page.messages) == ["dated"]
        assert (page.total_matches, page.indeterminate) == (1, 5)
        bounded = dates_db.query_messages(date_basis="sent", **_MARCH)
        assert _ids(bounded.messages) == ["dated"]
        assert bounded.indeterminate == 5

    def test_the_occurred_basis_is_unchanged(self, dates_db):
        page = dates_db.query_messages(date_basis="occurred", **_MARCH)
        assert _ids(page.messages) == ["unchecked_delivered", "delivered"]
        assert page.indeterminate == 4

    def test_records_carry_the_status(self, dates_db):
        by_id = {m.message_id: m for m in dates_db.query_messages().messages}
        for message_id, (sent, status, _occurred, first) in _ROWS.items():
            record = by_id[message_id]
            assert (record.sent_at, record.sent_at_status) == (_shown(sent, status), status)
        assert by_id["undated"].effective_at == _ROWS["undated"][3]


class TestTools:
    @staticmethod
    def _call(db, name, **kwargs):
        server = FakeMCPServer()
        register_retrieval_tools(server, db)
        return asyncio.run(server.tools[name](**kwargs))

    def test_query_messages_reports_and_explains_the_unknown(self, dates_db):
        out = self._call(dates_db, "query_messages", **_MARCH)
        assert out.structured_content["indeterminate"] == 3
        text = out.content[0].text
        assert "no delivery date and no parseable Date header, or not yet checked" in text

    def test_query_messages_rows_never_show_a_substitute(self, dates_db):
        out = self._call(dates_db, "query_messages")
        rows = {r["message_id"]: r for r in out.structured_content["messages"]}
        for message_id, (sent, status, _occurred, _first) in _ROWS.items():
            ListedMessage.model_validate(rows[message_id])
            assert (rows[message_id]["sent_at"], rows[message_id]["sent_at_status"]) == (
                _shown(sent, status),
                status,
            )
        text = out.content[0].text
        assert "send date unknown (no Date header)" in text
        assert "send date unknown (unparseable Date header)" in text
        assert "send date not yet checked" in text
        for sent, status, _occurred, first in _ROWS.values():
            assert first not in text
            if status is None:
                assert sent not in text

    def test_the_status_can_be_projected(self, dates_db):
        out = self._call(dates_db, "query_messages", fields=["sent_at_status"])
        rows = out.structured_content["messages"]
        assert {r["sent_at_status"] for r in rows} == {"parsed", "missing", "invalid", None}
        assert all(set(r) == {"claimant_id", "thread_id", "sent_at_status"} for r in rows)

    @pytest.mark.parametrize(
        ("message_id", "words"),
        [
            ("undated", "send date unknown (no Date header)"),
            ("undated_out", "send date unknown (unparseable Date header)"),
            ("unchecked", "send date not yet checked"),
            ("dated", "2024-03-10T09:00:00+00:00"),
        ],
    )
    def test_get_message_shows_the_status(self, dates_db, message_id, words):
        out = self._call(dates_db, "get_message", message_id=message_id)
        GetMessageOutput.model_validate(out.structured_content)
        sent, status, _occurred, first = _ROWS[message_id]
        message = out.structured_content["message"]
        assert (message["sent_at"], message["sent_at_status"]) == (_shown(sent, status), status)
        assert f"Sent: {words}\n" in out.content[0].text
        assert first not in out.content[0].text
        if status is None:
            assert sent not in out.content[0].text

    def test_query_attachments_counts_an_undated_carrier_indeterminate(self, dates_db):
        out = self._call(dates_db, "query_attachments", **_MARCH)
        data = out.structured_content
        assert sorted(a["claimant_id"] for a in data["attachments"]) == sorted(
            claimant_of(m) for m in ("dated", "delivered", "unchecked_delivered")
        )
        assert (data["total_matches"], data["indeterminate"]) == (3, 3)
        rows = {
            a["claimant_id"]: a
            for a in self._call(dates_db, "query_attachments").structured_content["attachments"]
        }
        undated = rows[claimant_of("undated")]
        assert (undated["sent_at"], undated["sent_at_status"]) == (None, "missing")
        assert (
            "send date unknown (no Date header)"
            in self._call(dates_db, "query_attachments", thread_id="t-undated").content[0].text
        )

    def test_aggregate_puts_undated_mail_in_the_null_period(self, dates_db):
        out = self._call(dates_db, "aggregate_messages", group_by="month")
        groups = {g["value"]: g for g in out.structured_content["groups"]}
        assert groups["2024-03"]["messages"] == 3
        assert groups[None]["messages"] == 3
        assert groups[None]["first_at"] is None
        assert "2026-01" not in groups
        assert out.structured_content["indeterminate"] == 0
        assert "(no known date): 3 messages" in out.content[0].text

    def test_no_mail_text_reaches_the_log(self, dates_db, caplog):
        caplog.set_level(logging.DEBUG)
        for name, kwargs in (
            ("query_messages", _MARCH),
            ("query_attachments", _MARCH),
            ("aggregate_messages", {"group_by": "year", **_MARCH}),
            ("get_message", {"message_id": "undated"}),
        ):
            self._call(dates_db, name, **kwargs)
        assert _MARKER not in caplog.text


def test_message_scope_never_places_an_undated_message_in_range(dates_db):
    labels = dates_db.message_scope([f"t-{m}" for m in _ROWS], **_MARCH)
    assert labels.claimants == {
        claimant_of(m) for m in ("dated", "delivered", "unchecked_delivered")
    }


@pytest.mark.parametrize("message_id", ["dated", "undated", "unchecked", "unchecked_delivered"])
def test_evidence_passages_carry_only_a_parsed_send_date(dates_db, message_id):
    """Review round 1: every consumer of a passage's date (the prompt
    headers, citations, brief_issue's as_of) reads ``message_date``, so
    a send date not yet assessed never reaches it."""
    sent, status, occurred, _first = _ROWS[message_id]
    chunks = dates_db.get_recent_chunks_for_thread(f"t-{message_id}")
    assert [(c.message_date, c.message_sent_at_status, c.message_occurred_at) for c in chunks] == [
        (_shown(sent, status), status, occurred)
    ]


def test_a_cursor_from_before_the_date_change_is_foreign(dates_db, monkeypatch):
    """Review round 1: query_attachments' cursor digest covers
    ``QUERY_DIGEST_FORMAT``, so a page issued under the old date
    semantics is not resumed under the new ones."""
    first = dates_db.query_attachments(limit=1, **_MARCH)
    assert first.next_cursor
    monkeypatch.setattr(sqlite_mod, "QUERY_DIGEST_FORMAT", 2)
    with pytest.raises(InvalidFilterError, match="issued for different filters"):
        dates_db.query_attachments(limit=1, cursor=first.next_cursor, **_MARCH)


def test_an_ambiguous_message_id_lists_each_claimants_send_date(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "claimants.db")
    _insert_message(conn, message_id="dup", thread_id="t-dup", sent_at="2024-03-10T09:00:00+00:00")
    _insert_message(conn, message_id="dup", thread_id="t-dup", sent_at=None, variant="b")
    conn.close()
    server = FakeMCPServer()
    register_retrieval_tools(server, Database(str(path)))
    with pytest.raises(ToolError) as excinfo:
        asyncio.run(server.tools["get_message"](message_id="dup"))
    text = str(excinfo.value)
    assert f"{claimant_of('dup')} (sent 2024-03-10T09:00:00+00:00, folder INBOX)" in text
    assert f"{claimant_of('dup', 'b')} (send date unknown (no Date header), folder INBOX)" in text
