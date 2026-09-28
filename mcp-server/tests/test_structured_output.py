"""
Structured output contract for the search, retrieval, evidence, and
status tools (PLAN.md Phase 1 item 2).

The handler tests drive tool functions through ``FakeMCPServer``, which
skips FastMCP entirely — so nothing there proves a tool publishes an
``outputSchema`` or that what it returns satisfies it. These tests go
through a real ``FastMCP`` instance: ``list_tools`` for the published
schemas and ``call_tool`` for results, then validate each
``structuredContent`` against its tool's schema with jsonschema, the same
check the MCP low-level server applies before a result is sent.
"""

import asyncio
import json

import jsonschema
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools
from src.tools.system import register_system_tools

from tests.conftest import FakeEmbedClient, _insert_message
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
    "get_index_status": "total_threads",
    "get_sync_status": "mode",
}


def _server(db) -> FastMCP:
    server = FastMCP("structured-output-test")
    register_search_tools(server, db, FakeEmbedClient())
    register_retrieval_tools(server, db)
    register_system_tools(server, db)
    return server


def _call(server: FastMCP, name: str, **args) -> dict:
    """Call ``name`` through FastMCP and return its validated structured
    content. The prose ``content`` must still be present alongside it."""
    schemas = {t.name: t.outputSchema for t in asyncio.run(server.list_tools())}
    result = asyncio.run(server.call_tool(name, args))
    assert isinstance(result, CallToolResult)
    assert not result.isError
    assert result.content and result.content[0].text.strip()
    assert result.structuredContent is not None
    jsonschema.validate(result.structuredContent, schemas[name])
    return result.structuredContent


def test_every_listed_tool_publishes_a_typed_output_schema(messages_db):
    tools = {t.name: t for t in asyncio.run(_server(messages_db).list_tools())}
    for name, key in STRUCTURED_TOOLS.items():
        schema = tools[name].outputSchema
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
    status = _call(server, "get_index_status")
    assert status["total_threads"] == 3
    assert status["total_messages"] == 5
    assert _call(server, "get_sync_status")["mode"] == "local_index_only"


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
    with pytest.raises(ToolError, match=text):
        asyncio.run(_server(messages_db).call_tool(name, args))


def test_sender_controlled_headers_stay_bounded(tmp_path):
    """The structured side must honor the same bounds as the prose: a
    sender who writes 12,000 References, 30 recipients, and a 100K-char
    subject gets shortened lists with full counts from get_thread and
    query_messages; get_message alone returns everything."""
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
        full = _call(server, "get_message", message_id="a")

    for m in (thread["messages"][0], listed["messages"][0]):
        assert len(m["references"]) == 10
        assert m["references_count"] == 12000
        assert len(m["to"]) == 10
        assert m["to_count"] == 30
    for out in (thread, listed):
        assert len(json.dumps(out)) < 5000
    for m in (thread["messages"][0], listed["messages"][0]):
        assert m["subject"].endswith("[99,500 more characters]")
    assert full["message"]["references"] == refs
    assert len(full["message"]["to"]) == 30
    assert full["message"]["subject"] == "S" * 100_000


def test_query_messages_cuts_each_long_header_value(tmp_path):
    """Review round 1: query_messages bounded list lengths but not the
    values in them, so one message with a huge In-Reply-To, Reference,
    subject, or display name made a multi-megabyte page at limit=1. Both
    the prose and the structured side cut every value at 500 characters;
    get_message returns them in full."""
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
        result = asyncio.run(server.call_tool("query_messages", {"limit": 1}))
        full = _call(server, "get_message", message_id="a")

    assert isinstance(result, CallToolResult)
    assert len(result.content[0].text) < 3000
    assert len(json.dumps(result.structuredContent)) < 5000
    m = result.structuredContent["messages"][0]
    for value in (m["subject"], m["in_reply_to"], m["references"][0], m["from"][0]["name"]):
        assert value.endswith("[99,500 more characters]")
    assert m["from"][0]["address"] == "jane@example.com"
    assert full["message"]["in_reply_to"] == huge
    assert full["message"]["references"] == [huge]
    assert full["message"]["from"][0]["name"] == huge
