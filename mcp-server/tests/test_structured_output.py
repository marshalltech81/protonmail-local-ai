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

import jsonschema
import pytest
from fastmcp import Client, FastMCP
from mcp.types import CallToolResult, Tool
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools
from src.tools.system import register_system_tools

from tests.conftest import (
    FakeEmbedClient,
    _insert_attachment,
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
