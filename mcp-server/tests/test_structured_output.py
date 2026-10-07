"""
Structured output contract for the search, retrieval, evidence, and
status tools (PLAN.md Phase 1 item 2).

The handler tests drive tool functions through ``FakeMCPServer``, which
skips FastMCP entirely — so nothing there proves a tool publishes an
``outputSchema`` or that what it returns satisfies it. These tests go
through a real ``FastMCP`` instance and its in-memory client:
``list_tools`` for the published schemas and ``call_tool_mcp`` for the
results a client receives, then validate each ``structuredContent``
against its tool's schema with jsonschema.
"""

import asyncio
import json
import sqlite3

import jsonschema
import pytest
import sqlite_vec
from fastmcp import Client, FastMCP
from mcp.types import CallToolResult, Tool
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools
from src.tools.system import register_system_tools

from tests.conftest import (
    FakeEmbedClient,
    _insert_attachment,
    _insert_chunk,
    _insert_message,
    _insert_thread,
    source_sha256,
)
from tests.test_retrieval import _open_fixture_db

# Tool -> one top-level property its output schema must declare. A bare
# ``list[TextContent]`` annotation also yields an output schema — FastMCP
# wraps it as ``{"result": [TextContent...]}``, a second copy of the
# prose — so "has a schema" alone would not prove typed fields exist.
STRUCTURED_TOOLS = {
    "search_emails": "results",
    "get_evidence": "threads",
    "search_attachments": "results",
    "get_thread": "messages",
    "get_message": "message",
    "list_threads": "threads",
    "list_folders": "folders",
    "find_contact": "contacts",
    "query_messages": "total_matches",
    "get_mailbox_status": "current",
}


def _server(db) -> FastMCP:
    server = FastMCP("structured-output-test")
    register_search_tools(server, db, FakeEmbedClient())
    register_retrieval_tools(server, db)
    register_system_tools(server, db)
    return server


def _tools(server: FastMCP) -> dict[str, Tool]:
    """The tools a client lists, keyed by name."""

    async def run() -> list[Tool]:
        async with Client(server) as client:
            return await client.list_tools()

    return {t.name: t for t in asyncio.run(run())}


def _wire(server: FastMCP, name: str, args: dict) -> CallToolResult:
    """Call ``name`` through FastMCP's in-memory client and return the
    raw MCP result a client receives."""

    async def run() -> CallToolResult:
        async with Client(server) as client:
            return await client.call_tool_mcp(name, args)

    return asyncio.run(run())


def _call(server: FastMCP, name: str, **args) -> dict:
    """Call ``name`` through FastMCP and return its validated structured
    content. The prose ``content`` must still be present alongside it."""
    schemas = {n: t.output_schema for n, t in _tools(server).items()}
    result = _wire(server, name, args)
    assert not result.is_error
    assert result.content and result.content[0].text.strip()
    assert result.structured_content is not None
    jsonschema.validate(result.structured_content, schemas[name])
    return result.structured_content


def test_every_listed_tool_publishes_a_typed_output_schema(messages_db):
    tools = _tools(_server(messages_db))
    for name, key in STRUCTURED_TOOLS.items():
        schema = tools[name].output_schema
        assert schema is not None, name
        assert key in schema["properties"], name
        assert "result" not in schema["properties"], name


class TestChaining:
    """search -> thread_id -> get_thread -> message_id -> get_message,
    read from typed fields only."""

    def test_ids_chain_without_parsing_prose(self, messages_db):
        server = _server(messages_db)
        found = _call(server, "search_emails", query="budget", mode="keyword")
        thread_id = found["results"][0]["thread_id"]
        assert thread_id == "t1"

        thread = _call(server, "get_thread", thread_id=thread_id)
        assert thread["thread"]["thread_id"] == "t1"
        assert thread["total_messages"] == 2
        assert [m["message_id"] for m in thread["messages"]] == ["m1", "m2"]
        assert thread["next_offset"] is None

        message_id = thread["messages"][1]["message_id"]
        message = _call(server, "get_message", message_id=message_id)
        assert message["message"]["message_id"] == "m2"
        assert message["message"]["thread_id"] == "t1"
        assert message["message"]["in_reply_to"] == "m1"
        assert message["message"]["from"] == [{"name": None, "address": "bob@example.com"}]
        assert message["message"]["cc"] == [{"name": None, "address": "carol@other.org"}]
        assert message["body"] == "thanks, budget noted"

    def test_get_thread_pages_by_offset(self, messages_db):
        server = _server(messages_db)
        first = _call(server, "get_thread", thread_id="t1", limit=1)
        assert [m["message_id"] for m in first["messages"]] == ["m1"]
        assert first["next_offset"] == 1
        second = _call(server, "get_thread", thread_id="t1", offset=first["next_offset"])
        assert [m["message_id"] for m in second["messages"]] == ["m2"]
        assert second["next_offset"] is None

    def test_evidence_carries_attachment_ids(self, messages_db):
        evidence = _call(_server(messages_db), "get_evidence", query="spreadsheet", thread_id="t1")
        chunks = [c for t in evidence["threads"] for c in t["chunks"]]
        assert evidence["chunk_count"] == len(chunks)
        attachment = next(c for c in chunks if c["source"] == "attachment")
        assert attachment["attachment_id"] == "m2-att"
        assert attachment["message_id"] == "m2"

    def test_evidence_carries_chunk_kinds(self, messages_db):
        # #646: each passage says what it is; the prose names a non-body
        # kind of message text.
        conn = sqlite3.connect(messages_db.path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        _insert_chunk(
            conn,
            chunk_id="m1-quote",
            message_id="m1",
            thread_id="t1",
            chunk_index=1,
            char_start=len("the budget is approved\n\n"),
            text="> spreadsheet attached earlier",
            embedding=[1.0, 0.0, 0.0, 0.0],
            kind="quote",
        )
        conn.close()
        server = _server(messages_db)
        args = {"query": "spreadsheet", "thread_id": "t1"}
        evidence = _call(server, "get_evidence", **args)
        chunks = [c for t in evidence["threads"] for c in t["chunks"]]
        kinds = {c["chunk_id"]: c["kind"] for c in chunks}
        assert kinds["m1-quote"] == "quote"
        assert {kinds[c["chunk_id"]] for c in chunks if c["source"] == "attachment"} == {
            "attachment"
        }
        assert "Source: message body (quote)" in _wire(server, "get_evidence", args).content[0].text


class TestQueryMessages:
    def test_paging_state_and_filter_interpretation(self, messages_db):
        server = _server(messages_db)
        page = _call(server, "query_messages", sender="Jane", limit=2)
        assert page["total_matches"] == 3
        assert page["returned"] == 2
        assert page["offset"] == 0
        assert page["has_more"] is True
        assert page["filters"] == [{"filter": "sender", "value": "Jane", "match": "substring"}]
        rest = _call(server, "query_messages", sender="Jane", limit=2, cursor=page["next_cursor"])
        assert rest["offset"] == 2
        assert rest["returned"] == 1
        assert rest["has_more"] is False
        assert rest["next_cursor"] is None

    def test_exact_address_filter_is_named(self, messages_db):
        page = _call(
            _server(messages_db),
            "query_messages",
            sender="Jane Doe <JANE@example.com>",
            has_attachments=False,
        )
        assert page["filters"] == [
            {"filter": "sender", "value": "jane@example.com", "match": "exact_address"},
            {"filter": "has_attachments", "value": False, "match": "equals"},
        ]


class TestQueryMessagesFields:
    """#990: ``fields`` projects each row (structured and prose) onto the
    named fields plus ``claimant_id`` and ``thread_id``; without it the
    output is exactly what it was before the parameter existed."""

    ALL_FIELDS = [
        "message_id",
        "claimant_id",
        "subject",
        "sent_at",
        "occurred_at",
        "folder",
        "has_attachments",
        "seen",
        "flagged",
        "replied",
        "in_reply_to",
        "references",
        "references_count",
        "from",
        "from_count",
        "to",
        "to_count",
        "cc",
        "cc_count",
        "source_file",
        "thread_id",
        "pending_deletion",
    ]

    def test_without_fields_the_output_is_unchanged(self, messages_db):
        # Pinned on main before the parameter existed.
        server = _server(messages_db)
        result = _wire(server, "query_messages", {"subject": "Re: Budget"})
        assert result.content[0].text == (
            "Query: subject='Re: Budget' (case-insensitive substring)\n"
            "total_matches: 1\n"
            "returned: 1 (matches 1-1)\n"
            "has_more: false\n"
            "\n"
            "1. 2024-01-11T10:00:00+00:00 | INBOX | unread | attachments\n"
            "   Subject: Re: Budget review\n"
            "   From: bob@example.com\n"
            "   To: Jane Doe <jane@example.com>\n"
            "   Cc: carol@other.org\n"
            "   Message-ID: m2 | Claimant ID: m2#29c1b289e7522195\n"
            "   Thread ID: t1\n"
        )
        page = _call(server, "query_messages", sender="jane@example.com", limit=2)
        assert [list(m) for m in page["messages"]] == [self.ALL_FIELDS] * 2

    def test_every_field_named_equals_no_projection(self, messages_db):
        server = _server(messages_db)
        args = {"sender": "jane@example.com", "limit": 2}
        plain = _wire(server, "query_messages", args)
        full = _wire(server, "query_messages", {**args, "fields": self.ALL_FIELDS})
        assert full.content[0].text == plain.content[0].text
        assert full.structured_content == plain.structured_content

    def test_rows_hold_exactly_the_named_fields_and_the_ids(self, messages_db):
        server = _server(messages_db)
        page = _call(server, "query_messages", fields=["subject", "sent_at"])
        assert page["total_matches"] == 5 and page["returned"] == 5
        for row in page["messages"]:
            assert set(row) == {"claimant_id", "thread_id", "subject", "sent_at"}
        # The envelope is untouched.
        full = _call(server, "query_messages")
        assert {k: v for k, v in page.items() if k != "messages"} == {
            k: v for k, v in full.items() if k != "messages"
        }
        assert [r["claimant_id"] for r in page["messages"]] == [
            r["claimant_id"] for r in full["messages"]
        ]

    def test_empty_list_keeps_only_the_ids(self, messages_db):
        page = _call(_server(messages_db), "query_messages", fields=[], limit=1)
        assert list(page["messages"][0]) == ["claimant_id", "thread_id"]

    def test_prose_shows_only_the_named_fields(self, messages_db):
        text = (
            _wire(
                _server(messages_db),
                "query_messages",
                {"subject": "Re: Budget", "fields": ["subject", "sent_at"]},
            )
            .content[0]
            .text
        )
        assert text.endswith(
            "1. 2024-01-11T10:00:00+00:00\n"
            "   Subject: Re: Budget review\n"
            "   Claimant ID: m2#29c1b289e7522195\n"
            "   Thread ID: t1\n"
        )
        for absent in ("INBOX", "unread", "attachments", "From:", "To:", "Cc:", "Message-ID"):
            assert absent not in text

    def test_prose_with_only_the_ids(self, messages_db):
        text = (
            _wire(_server(messages_db), "query_messages", {"subject": "Re: Budget", "fields": []})
            .content[0]
            .text
        )
        assert text.endswith("1.\n   Claimant ID: m2#29c1b289e7522195\n   Thread ID: t1\n")

    def test_cursor_round_trip_with_a_projection(self, messages_db):
        server = _server(messages_db)
        args = {"sender": "jane@example.com", "limit": 2, "fields": ["from"]}
        first = _call(server, "query_messages", **args)
        assert first["has_more"] is True
        rest = _call(server, "query_messages", **args, cursor=first["next_cursor"])
        assert rest["has_more"] is False
        full = _call(server, "query_messages", sender="jane@example.com")
        assert [r["claimant_id"] for r in first["messages"] + rest["messages"]] == [
            r["claimant_id"] for r in full["messages"]
        ]
        for row in first["messages"] + rest["messages"]:
            assert set(row) == {"claimant_id", "thread_id", "from"}
        # A cursor from an unprojected page continues a projected one.
        plain = _call(server, "query_messages", sender="jane@example.com", limit=2)
        assert _call(server, "query_messages", **args, cursor=plain["next_cursor"]) == rest

    def test_overlong_fields_list_is_rejected_with_fixed_text(self, messages_db, caplog):
        # Review round 1: more names than a row has fields is rejected
        # before any check per name; the log line stays bounded.
        with caplog.at_level("DEBUG"):
            result = _wire(_server(messages_db), "query_messages", {"fields": ["subject"] * 10_000})
        assert result.is_error
        text = result.content[0].text
        assert "fields lists at most 22 names" in text
        assert len(text) < 200
        assert "'subject'" not in caplog.text
        assert "query_messages rejected invalid fields" in caplog.text
        # Up to one entry per field, repeats included, is accepted.
        page = _call(_server(messages_db), "query_messages", fields=["subject"] * 22, limit=1)
        assert set(page["messages"][0]) == {"claimant_id", "thread_id", "subject"}

    def test_unknown_field_is_rejected_by_name_and_not_logged(self, messages_db, caplog):
        marker = "privatemarkerf990"
        with caplog.at_level("DEBUG"):
            result = _wire(_server(messages_db), "query_messages", {"fields": ["subject", marker]})
        assert result.is_error
        text = result.content[0].text
        assert f"unknown field '{marker}'" in text
        assert "fields" in text
        assert marker not in caplog.text
        assert "query_messages rejected invalid fields" in caplog.text

    def test_repeated_rejections_are_rate_limited(self, messages_db, caplog):
        # Review round 2: a client repeating a rejected projection gets
        # one WARNING per reason per window, not one per request.
        server = _server(messages_db)
        marker = "privatemarkerg990"
        with caplog.at_level("DEBUG"):
            for _ in range(5):
                assert _wire(server, "query_messages", {"fields": [marker]}).is_error
                assert _wire(server, "query_messages", {"fields": ["subject"] * 23}).is_error
        lines = [
            r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING" and "rejected invalid fields" in r.getMessage()
        ]
        assert lines == [
            "query_messages rejected invalid fields: reason=unknown_name",
            "query_messages rejected invalid fields: reason=too_many",
        ]
        assert marker not in caplog.text


class TestQueryMessagesAddressMatches:
    """#801: the structured output lists, per address filter, how many
    distinct addresses it matched over the whole set."""

    def test_name_filter_reports_both_namesakes(self, namesakes_db):
        page = _call(_server(namesakes_db), "query_messages", sender="Avery Cole", limit=1)
        assert page["total_matches"] == 3
        assert page["returned"] == 1
        assert page["address_matches"] == [
            {
                "filter": "sender",
                "distinct_addresses": 2,
                "addresses": ["avery@one.example", "a.cole@two.example"],
            }
        ]

    def test_exact_filter_reports_one(self, namesakes_db):
        page = _call(_server(namesakes_db), "query_messages", sender="avery@one.example")
        assert page["address_matches"] == [
            {"filter": "sender", "distinct_addresses": 1, "addresses": ["avery@one.example"]}
        ]

    def test_without_address_filters_the_list_is_empty(self, namesakes_db):
        assert _call(_server(namesakes_db), "query_messages")["address_matches"] == []


class TestDateBoundsEcho:
    """Each date-filtered tool echoes the UTC instants its bounds resolved
    to (#802): a date-only bound is a UTC day, an offset bound is the
    instant it names. m1 was sent at 09:00 UTC on 2024-01-10, which is
    04:00 in New York, so it is inside the UTC day but before 05:00
    New York time."""

    def test_query_messages_date_only_bound_is_the_utc_day(self, messages_db):
        page = _call(_server(messages_db), "query_messages", date_from="2024-01-10")
        assert page["date_bounds"] == {
            "date_from": "2024-01-10T00:00:00+00:00",
            "date_to": None,
            "basis": "effective",
        }
        assert "m1" in [m["message_id"] for m in page["messages"]]

    def test_query_messages_offset_bound_is_converted_to_utc(self, messages_db):
        server = _server(messages_db)
        args = {"date_from": "2024-01-10T05:00:00-05:00", "date_to": "2024-01-31"}
        page = _call(server, "query_messages", **args)
        assert page["date_bounds"] == {
            "date_from": "2024-01-10T10:00:00+00:00",
            "date_to": "2024-01-31T23:59:59.999999+00:00",
            "basis": "effective",
        }
        assert "m1" not in [m["message_id"] for m in page["messages"]]
        text = _wire(server, "query_messages", args).content[0].text
        assert (
            "Date bounds (UTC): from 2024-01-10T10:00:00+00:00 "
            "to 2024-01-31T23:59:59.999999+00:00" in text
        )

    def test_no_date_filter_echoes_null(self, messages_db):
        server = _server(messages_db)
        assert _call(server, "query_messages", sender="Jane")["date_bounds"] is None
        found = _call(server, "search_emails", query="budget", mode="keyword")
        assert found["date_bounds"] is None
        assert _call(server, "search_attachments", query="zzzz")["date_bounds"] is None

    def test_search_emails_echoes_the_bounds(self, messages_db):
        server = _server(messages_db)
        same_day = _call(
            server, "search_emails", query="budget", mode="keyword", date_to="2024-01-10"
        )
        assert same_day["date_bounds"] == {
            "date_from": None,
            "date_to": "2024-01-10T23:59:59.999999+00:00",
            "basis": "effective",
        }
        assert [r["thread_id"] for r in same_day["results"]] == ["t1"]
        args = {"query": "budget", "mode": "keyword", "date_to": "2024-01-10T03:00:00-05:00"}
        before = _call(server, "search_emails", **args)
        assert before["date_bounds"]["date_to"] == "2024-01-10T08:00:00+00:00"
        assert before["results"] == []
        text = _wire(server, "search_emails", args).content[0].text
        assert "Date bounds (UTC): to 2024-01-10T08:00:00+00:00" in text

    def test_search_emails_unmatched_from_name_still_echoes(self, messages_db):
        server = _server(messages_db)
        args = {"query": "budget", "from_name": "Nobody Known", "date_from": "2024-01-10"}
        out = _call(server, "search_emails", **args)
        assert out["date_bounds"]["date_from"] == "2024-01-10T00:00:00+00:00"
        text = _wire(server, "search_emails", args).content[0].text
        assert "Date bounds (UTC): from 2024-01-10T00:00:00+00:00" in text

    def test_search_attachments_echoes_the_bounds(self, attachments_db):
        server = _server(attachments_db)
        args = {"query": "zzzz-no-match", "date_from": "2024-01-10T00:00:00+05:30"}
        out = _call(server, "search_attachments", **args)
        assert out["date_bounds"] == {
            "date_from": "2024-01-09T18:30:00+00:00",
            "date_to": None,
            "basis": "effective",
        }
        text = _wire(server, "search_attachments", args).content[0].text
        assert "Date bounds (UTC): from 2024-01-09T18:30:00+00:00" in text


@pytest.mark.parametrize(
    ("name", "args", "key"),
    [
        ("search_emails", {"query": "zzzz-no-match", "mode": "keyword"}, "results"),
        ("search_attachments", {"query": "zzzz-no-match"}, "results"),
        ("list_threads", {"folder": "NoSuchFolder"}, "threads"),
        ("find_contact", {"query": "zzzz-no-match"}, "contacts"),
        ("query_messages", {"subject": "zzzz-no-match"}, "messages"),
    ],
)
def test_empty_results_are_structured_not_errors(messages_db, name, args, key):
    assert _call(_server(messages_db), name, **args)[key] == []


def test_unmatched_from_name_reports_no_resolution(messages_db):
    out = _call(_server(messages_db), "search_emails", query="budget", from_name="Nobody Known")
    assert out["resolved_from_addr"] is None
    assert out["results"] == []


def test_search_attachments_structured(attachments_db):
    out = _call(_server(attachments_db), "search_attachments", query="acme")
    hit = out["results"][0]
    assert hit["attachment_id"] == "att-quote"
    assert hit["thread_id"] == "t-quote"
    assert hit["filename"] == "acme-quote.pdf"
    assert hit["size_bytes"] == 20480


def test_folders_contacts_and_status(messages_db):
    server = _server(messages_db)
    folders = _call(server, "list_folders")["folders"]
    assert {f["name"] for f in folders} == {"INBOX", "Archive"}
    contacts = _call(server, "find_contact", query="jane")["contacts"]
    assert contacts[0]["email"] == "jane@example.com"
    status = _call(server, "get_mailbox_status")
    assert status["total_threads"] == 3
    assert status["total_messages"] == 5
    assert status["current"] is False
    assert status["queue"] == {"pending": 0, "retrying": 0, "dead": 0}


@pytest.mark.parametrize(
    ("name", "args", "text"),
    [
        ("get_thread", {"thread_id": "no-such-thread"}, "Thread not found"),
        ("get_message", {"message_id": "no-such-message"}, "Message not found"),
        ("search_emails", {"query": "x", "mode": "fuzzy"}, "Invalid mode"),
        ("get_evidence", {"query": "  "}, "Provide a query"),
        ("find_contact", {"query": " "}, "Provide a name"),
        ("query_messages", {"cursor": "not-a-cursor"}, "cursor"),
    ],
)
def test_failures_are_error_results(messages_db, name, args, text):
    """A failure is raised, so the client receives ``isError: true`` —
    never a success result whose structured content an agent would
    trust."""
    result = _wire(_server(messages_db), name, args)
    assert result.is_error
    assert result.structured_content is None
    assert text in result.content[0].text


def test_sender_controlled_headers_stay_bounded(tmp_path):
    """The structured side must honor the same bounds as the prose: a
    sender who writes 12,000 References, 30 recipients, and a 100K-char
    subject gets shortened lists with full counts from get_thread,
    query_messages and (#489) get_message."""
    refs = [f"ref{i:05d}@example.com" for i in range(12000)]
    recipients = [f"r{i}@example.com" for i in range(30)]
    with _open_fixture_db(tmp_path) as (conn, db):
        _insert_message(
            conn,
            message_id="a",
            thread_id="t",
            sent_at="2024-01-01T00:00:00+00:00",
            subject="S" * 100_000,
            to=recipients,
            references=refs,
            body="hello",
        )
        conn.close()
        server = _server(db)
        thread = _call(server, "get_thread", thread_id="t")
        listed = _call(server, "query_messages")
        single = _call(server, "get_message", message_id="a")

    rows = (thread["messages"][0], listed["messages"][0], single["message"])
    for m in rows:
        assert len(m["references"]) == 10
        assert m["references_count"] == 12000
        assert len(m["to"]) == 10
        assert m["to_count"] == 30
        assert m["subject"].endswith("[99,500 more characters]")
    for out in (thread, listed, single):
        assert len(json.dumps(out)) < 5000


def test_get_message_pages_the_body(tmp_path):
    """#489: the structured body is one page, with its offset, the
    body's length and the next page's offset; the schema validates."""
    body = "z" * 45_000
    with _open_fixture_db(tmp_path) as (conn, db):
        _insert_message(
            conn, message_id="a", thread_id="t", sent_at="2024-01-01T00:00:00+00:00", body=body
        )
        conn.close()
        server = _server(db)
        pages = [_call(server, "get_message", message_id="a")]
        while pages[-1]["next_offset"] is not None:
            pages.append(
                _call(server, "get_message", message_id="a", offset=pages[-1]["next_offset"])
            )
    assert [p["body_offset"] for p in pages] == [0, 20_000, 40_000]
    assert {p["body_total_chars"] for p in pages} == {45_000}
    assert "".join(p["body"] for p in pages) == body


def test_query_messages_cuts_each_long_header_value(tmp_path):
    """Review round 1: query_messages bounded list lengths but not the
    values in them, so one message with a huge In-Reply-To, Reference,
    subject, or display name made a multi-megabyte page at limit=1. Both
    the prose and the structured side cut every value at 500 characters,
    as get_message does (#489)."""
    huge = "x" * 100_000
    with _open_fixture_db(tmp_path) as (conn, db):
        _insert_message(
            conn,
            message_id="a",
            thread_id="t",
            sent_at="2024-01-01T00:00:00+00:00",
            subject=huge,
            from_=[f"{huge} <jane@example.com>"],
            to=[f"{huge} <bob@example.com>"],
            in_reply_to=huge,
            references=[huge],
            body="hello",
        )
        conn.close()
        server = _server(db)
        result = _wire(server, "query_messages", {"limit": 1})
        single = _call(server, "get_message", message_id="a")

    assert isinstance(result, CallToolResult)
    assert len(result.content[0].text) < 3000
    assert len(json.dumps(result.structured_content)) < 5000
    m = result.structured_content["messages"][0]
    for value in (m["subject"], m["in_reply_to"], m["references"][0], m["from"][0]["name"]):
        assert value.endswith("[99,500 more characters]")
    assert m["from"][0]["address"] == "jane@example.com"
    m = single["message"]
    for value in (m["subject"], m["in_reply_to"], m["references"][0], m["from"][0]["name"]):
        assert value.endswith("[99,500 more characters]")


def test_get_thread_rows_do_not_repeat_the_thread_id(tmp_path):
    """Security review round 2: a thread ID is the root Message-ID, which
    the sender controls and which cannot be cut without breaking chaining.
    get_thread states it once at the top; repeating it in every message
    row turned one 100K ID into megabytes on a 20-message page."""
    long_id = "T" * 100_000
    with _open_fixture_db(tmp_path) as (conn, db):
        for i in range(20):
            _insert_message(
                conn,
                message_id=f"m{i:02d}",
                thread_id=long_id,
                sent_at=f"2024-01-01T00:{i:02d}:00+00:00",
                body="hello",
            )
        conn.close()
        thread = _call(_server(db), "get_thread", thread_id=long_id, limit=20)

    assert thread["thread"]["thread_id"] == long_id
    assert len(thread["messages"]) == 20
    assert all("thread_id" not in m for m in thread["messages"])
    assert len(json.dumps(thread)) < 150_000


def test_listing_tools_cut_long_participant_values(tmp_path):
    """Security review round 2: search_emails and list_threads list ten
    participants (search_attachments ten senders) where the prose shows
    two or three, so each value is cut at 500 characters."""
    people = [f"{'N' * 100_000}{i} <p{i}@example.com>" for i in range(12)]
    with _open_fixture_db(tmp_path) as (conn, db):
        _insert_thread(
            conn,
            thread_id="t",
            subject="quarterly report",
            participants=people,
            senders=people,
            has_attachments=True,
            body_text="quarterly report attached",
        )
        _insert_attachment(
            conn, message_id="m", thread_id="t", attachment_id="a", filename="report.pdf"
        )
        conn.close()
        server = _server(db)
        found = _call(server, "search_emails", query="quarterly", mode="keyword")
        listed = _call(server, "list_threads")
        attachments = _call(server, "search_attachments", query="report")

    for summary in (found["results"][0], listed["threads"][0]):
        assert len(summary["participants"]) == 10
        assert summary["participant_count"] == 12
        assert summary["participants"][0].endswith("more characters]")
    hit = attachments["results"][0]
    assert len(hit["senders"]) == 10
    assert hit["sender_count"] == 12
    assert hit["senders"][0].endswith("more characters]")
    for out in (found, listed, attachments):
        assert len(json.dumps(out)) < 20_000


def _expected_source(message_id: str, folder: str = "INBOX") -> dict:
    return {
        "source_type": "maildir_message",
        "locator": f"/maildir/{folder}/cur/{message_id}",
        "sha256": source_sha256(message_id),
        "size_bytes": 100,
        "indexed_at": "2024-01-01T00:00:00Z",
    }


class TestSourceProvenance:
    """Every message, evidence chunk, and attachment hit names the raw
    file it came from: path, SHA-256, size, and when it was indexed."""

    def test_message_rows_carry_their_source(self, messages_db):
        server = _server(messages_db)
        thread = _call(server, "get_thread", thread_id="t1")
        assert [m["source_file"] for m in thread["messages"]] == [
            _expected_source("m1"),
            _expected_source("m2"),
        ]
        message = _call(server, "get_message", message_id="m3")
        assert message["message"]["source_file"] == _expected_source("m3", "Archive")
        page = _call(server, "query_messages", folder="Archive")
        assert [m["source_file"] for m in page["messages"]] == [_expected_source("m3", "Archive")]

    def test_get_message_prose_names_the_source(self, messages_db):
        result = _wire(_server(messages_db), "get_message", {"message_id": "m3"})
        assert isinstance(result, CallToolResult)
        text = result.content[0].text
        assert "/maildir/Archive/cur/m3" in text
        assert source_sha256("m3") in text

    @pytest.mark.parametrize("scope", [{"thread_id": "t1"}, {}])
    def test_evidence_chunks_carry_their_message_source(self, messages_db, scope):
        evidence = _call(_server(messages_db), "get_evidence", query="budget", **scope)
        chunks = [c for t in evidence["threads"] for c in t["chunks"]]
        assert chunks
        folders = {"m3": "Archive"}
        for chunk in chunks:
            mid = chunk["message_id"]
            assert chunk["source_file"] == _expected_source(mid, folders.get(mid, "INBOX"))
        # An attachment chunk resolves to the message file that carries it.
        attachment = next(c for c in chunks if c["source"] == "attachment")
        assert attachment["source_file"] == _expected_source("m2")

    def test_attachment_hits_carry_their_message_source(self, attachments_db):
        server = _server(attachments_db)
        for args in ({"query": "acme"}, {"query": "wage"}, {}):
            hits = _call(server, "search_attachments", **args)["results"]
            assert hits
            for hit in hits:
                assert hit["source_file"] == _expected_source(hit["message_id"], hit["folder"])

    def test_unrecorded_identity_is_null(self, tmp_path):
        """A message indexed without file identity reports null hash and
        size rather than a guess."""
        with _open_fixture_db(tmp_path) as (conn, db):
            _insert_message(conn, message_id="m1", thread_id="t1", sent_at="2024-01-01T00:00:00Z")
            conn.execute("UPDATE messages SET content_hash = NULL, size_bytes = NULL")
            conn.commit()
            conn.close()
            source = _call(_server(db), "get_message", message_id="m1")["message"]["source_file"]
            assert source["sha256"] is None
            assert source["size_bytes"] is None
            assert source["locator"] == "/maildir/INBOX/cur/m1"

    def test_evidence_without_a_message_record_has_null_source(self, tmp_path):
        with _open_fixture_db(tmp_path) as (conn, db):
            _insert_message(
                conn, message_id="m1", thread_id="t1", sent_at="2024-01-01T00:00:00Z", body="hello"
            )
            conn.execute("DELETE FROM messages")
            conn.commit()
            conn.close()
            evidence = _call(_server(db), "get_evidence", query="hello", thread_id="t1")
            assert [c["source_file"] for t in evidence["threads"] for c in t["chunks"]] == [None]


class TestMessageTime:
    """Every per-message and per-passage output names its time
    ``sent_at``, as the message's stored ``sent_at`` string
    (docs/architecture.md, Message time)."""

    def test_evidence_chunks_carry_their_message_sent_at(self, messages_db):
        server = _server(messages_db)
        evidence = _call(server, "get_evidence", query="spreadsheet", thread_id="t1")
        chunks = [c for t in evidence["threads"] for c in t["chunks"]]
        assert chunks
        for chunk in chunks:
            assert "message_date" not in chunk
            message = _call(server, "get_message", message_id=chunk["claimant_id"])["message"]
            assert chunk["sent_at"] == message["sent_at"]

    def test_attachment_hits_carry_the_carrying_message_sent_at(self, attachments_db):
        server = _server(attachments_db)
        hit = _call(server, "search_attachments", query="acme")["results"][0]
        message = _call(server, "get_message", message_id=hit["claimant_id"])["message"]
        assert hit["sent_at"] == message["sent_at"] == "2024-03-10T09:00:00+00:00"


class TestOccurredAt:
    """``occurred_at`` (the delivery time from the top ``Received:``
    header) sits beside ``sent_at`` wherever a message or passage time is
    output, null when unknown; ``sent_at`` is unchanged."""

    SENT = "2024-01-31T23:00:00+00:00"
    DELIVERED = "2024-02-01T01:00:00+00:00"

    @pytest.fixture
    def server(self, tmp_path):
        with _open_fixture_db(tmp_path) as (conn, db):
            _insert_message(
                conn,
                message_id="late@example.com",
                thread_id="t-late",
                sent_at=self.SENT,
                occurred_at=self.DELIVERED,
                from_=["alice@example.com"],
                has_attachments=True,
                body="zephyr figures",
            )
            _insert_message(
                conn,
                message_id="sent@example.com",
                thread_id="t-late",
                sent_at="2024-02-02T00:00:00+00:00",
                from_=["bob@example.com"],
            )
            _insert_attachment(
                conn,
                message_id="late@example.com",
                thread_id="t-late",
                attachment_id="att-z",
                filename="zephyr.pdf",
            )
            conn.close()
            yield _server(db)

    def test_message_headers_carry_occurred_at(self, server):
        message = _call(server, "get_message", message_id="late@example.com")["message"]
        assert (message["sent_at"], message["occurred_at"]) == (self.SENT, self.DELIVERED)
        thread = _call(server, "get_thread", thread_id="t-late")["messages"]
        assert [(m["message_id"], m["occurred_at"]) for m in thread] == [
            ("late@example.com", self.DELIVERED),
            ("sent@example.com", None),
        ]
        listed = _call(server, "query_messages", date_from="2024-02-01", date_to="2024-02-01")
        assert [(m["message_id"], m["occurred_at"]) for m in listed["messages"]] == [
            ("late@example.com", self.DELIVERED)
        ]

    def test_evidence_chunks_and_attachment_hits_carry_occurred_at(self, server):
        evidence = _call(server, "get_evidence", query="zephyr", thread_id="t-late")
        chunks = [c for t in evidence["threads"] for c in t["chunks"]]
        assert {(c["sent_at"], c["occurred_at"]) for c in chunks} == {(self.SENT, self.DELIVERED)}
        hit = _call(server, "search_attachments", query="zephyr")["results"][0]
        assert (hit["sent_at"], hit["occurred_at"]) == (self.SENT, self.DELIVERED)

    def test_prose_shows_the_delivery_time(self, server):
        result = _wire(server, "get_message", {"message_id": "late@example.com"})
        assert f"Delivered: {self.DELIVERED}" in result.content[0].text
        result = _wire(server, "get_message", {"message_id": "sent@example.com"})
        assert "Delivered:" not in result.content[0].text


class TestMaildirState:
    """Read / flagged / replied state the indexer takes from each file's
    Maildir flags: on every message header, as list_threads filters and
    as query_messages filters (#644)."""

    @pytest.fixture
    def server(self, tmp_path):
        with _open_fixture_db(tmp_path) as (conn, db):
            for mid, tid, folder, day, state in (
                ("a1", "t1", "INBOX", 1, {"seen": True, "replied": True}),
                ("a2", "t1", "INBOX", 2, {}),
                ("b1", "t2", "INBOX", 3, {"seen": True, "flagged": True}),
                ("c1", "t3", "INBOX", 4, {"seen": True}),
                # An unread message outside INBOX does not make its
                # thread unread in INBOX.
                ("e1", "t3", "Archive", 6, {}),
                ("d1", "t4", "Archive", 5, {"flagged": True}),
            ):
                _insert_message(
                    conn,
                    message_id=mid,
                    thread_id=tid,
                    folder=folder,
                    sent_at=f"2024-01-0{day}T00:00:00+00:00",
                    from_=["alice@example.com"],
                    **state,
                )
            conn.close()
            yield _server(db)

    def test_message_headers_carry_the_state(self, server):
        message = _call(server, "get_message", message_id="a1")["message"]
        assert (message["seen"], message["flagged"], message["replied"]) == (True, False, True)
        thread = _call(server, "get_thread", thread_id="t1")["messages"]
        assert [(m["message_id"], m["seen"], m["flagged"], m["replied"]) for m in thread] == [
            ("a1", True, False, True),
            ("a2", False, False, False),
        ]
        listed = _call(server, "query_messages", folder="Archive")["messages"]
        assert {(m["message_id"], m["seen"], m["flagged"]) for m in listed} == {
            ("d1", False, True),
            ("e1", False, False),
        }

    def test_prose_states_the_state(self, server):
        text = _wire(server, "get_message", {"message_id": "a1"}).content[0].text
        assert "Status: read, replied" in text
        text = _wire(server, "get_message", {"message_id": "a2"}).content[0].text
        assert "Status: unread" in text
        text = _wire(server, "query_messages", {"folder": "Archive"}).content[0].text
        assert "Archive | unread | flagged" in text

    @pytest.mark.parametrize(
        ("folder", "filter_type", "expected"),
        [
            ("INBOX", "all", {"t1", "t2", "t3"}),
            ("INBOX", "unread", {"t1"}),
            ("INBOX", "flagged", {"t2"}),
            ("Archive", "unread", {"t3", "t4"}),
            ("Archive", "flagged", {"t4"}),
        ],
    )
    def test_list_threads_filters(self, server, folder, filter_type, expected):
        out = _call(server, "list_threads", folder=folder, filter_type=filter_type)
        assert {t["thread_id"] for t in out["threads"]} == expected
        assert out["filter_type"] == filter_type

    def test_list_threads_rejects_other_filters(self, server):
        result = _wire(server, "list_threads", {"filter_type": "replied"})
        assert result.is_error
        assert "filter_type must be one of" in result.content[0].text

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            ({"seen": False}, {"a2", "d1", "e1"}),
            ({"seen": True}, {"a1", "b1", "c1"}),
            ({"flagged": True}, {"b1", "d1"}),
            ({"seen": True, "flagged": True}, {"b1"}),
        ],
    )
    def test_query_messages_filters(self, server, args, expected):
        out = _call(server, "query_messages", **args)
        assert {m["message_id"] for m in out["messages"]} == expected
        assert out["total_matches"] == len(expected)
        assert out["filters"] == [
            {"filter": key, "value": value, "match": "equals"} for key, value in args.items()
        ]

    def test_a_cursor_is_bound_to_the_state_filters(self, server):
        first = _call(server, "query_messages", seen=False, limit=1)
        assert first["has_more"]
        result = _wire(server, "query_messages", {"seen": True, "cursor": first["next_cursor"]})
        assert result.is_error


class TestPendingDeletion:
    """A message whose file the reconciler tombstoned (``T``-flagged or
    missing, awaiting the reaper under mirror retention) is still listed,
    marked ``pending_deletion`` (#794)."""

    @pytest.fixture
    def server(self, tmp_path):
        with _open_fixture_db(tmp_path) as (conn, db):
            for mid, day in (("live", 1), ("doomed", 2), ("restored", 3)):
                _insert_message(
                    conn,
                    message_id=mid,
                    thread_id="t1",
                    sent_at=f"2024-01-0{day}T00:00:00+00:00",
                    from_=["alice@example.com"],
                )
            rows = {
                r[0]: (r[1], r[2])
                for r in conn.execute("SELECT message_id, claimant_id, filepath FROM messages")
            }
            claimant, path = rows["doomed"]
            # A tombstone on the message's current file marks it.
            conn.execute(
                "INSERT INTO pending_deletions VALUES (?, ?, 't1', '2024-02-01T00:00:00+00:00')",
                (path, claimant),
            )
            # One left under a path the message no longer has (the file
            # moved out of the trash) does not: the reaper clears it.
            claimant, _ = rows["restored"]
            conn.execute(
                "INSERT INTO pending_deletions VALUES (?, ?, 't1', '2024-02-01T00:00:00+00:00')",
                ("/maildir/INBOX/cur/restored:2,T", claimant),
            )
            conn.commit()
            conn.close()
            yield _server(db)

    def test_get_message_carries_the_flag(self, server):
        for mid, expected in (("doomed", True), ("live", False), ("restored", False)):
            message = _call(server, "get_message", message_id=mid)["message"]
            assert message["pending_deletion"] is expected

    def test_query_messages_lists_it_with_unchanged_totals_and_paging(self, server):
        out = _call(server, "query_messages")
        assert out["total_matches"] == 3
        assert [(m["message_id"], m["pending_deletion"]) for m in out["messages"]] == [
            ("restored", False),
            ("doomed", True),
            ("live", False),
        ]
        first = _call(server, "query_messages", limit=1)
        second = _call(server, "query_messages", limit=1, cursor=first["next_cursor"])
        assert (first["total_matches"], second["total_matches"]) == (3, 3)
        assert [m["message_id"] for m in first["messages"] + second["messages"]] == [
            "restored",
            "doomed",
        ]

    def test_prose_states_it(self, server):
        text = _wire(server, "get_message", {"message_id": "doomed"}).content[0].text
        assert "Pending deletion: yes" in text
        # The reconciler also tombstones a file that is only missing
        # locally, so the text must not claim the cause was Proton.
        assert "deleted in Proton or its file is missing locally" in text
        # Archive mode never reaps, so removal is promised only under mirror.
        assert "mirror retention removes it after the grace period" in text
        text = _wire(server, "get_message", {"message_id": "live"}).content[0].text
        assert "Pending deletion" not in text
        text = _wire(server, "query_messages", {}).content[0].text
        assert text.count("| pending deletion") == 1
