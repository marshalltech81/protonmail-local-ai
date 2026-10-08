"""
Tool safety annotations (#899).

Every tool declares ``readOnlyHint=True``, ``destructiveHint=False`` and
``openWorldHint=False`` explicitly, plus its own human-readable
``title``: OpenAI requires all three hints and blocks calls to tools
left at the MCP defaults, and Anthropic's directory policy requires
``readOnlyHint``, ``destructiveHint`` and ``title``. The hints are
necessary, not sufficient: ChatGPT can still refuse a call to an
annotated tool with a client-side safety check this server cannot
control (#919, docs/troubleshooting.md). These tests build a real FastMCP
server with every tool group registered, experimental and intelligence
included, and read the annotations through the in-memory client, so
they check what a client receives on the wire.
"""

import asyncio
import inspect

import pytest
from fastmcp import Client, FastMCP
from mcp.types import Tool
from src.tools.brief import register_experimental_tools
from src.tools.intelligence import register_intelligence_tools
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools
from src.tools.system import register_system_tools

from tests.conftest import FakeEmbedClient, FakeInferenceClient, FakeMCPServer

# Every tool the server can register, with its title. A new tool fails
# ``test_every_registered_tool_is_classified`` until it is classified
# deliberately and added here.
EXPECTED_TITLES = {
    "search_emails": "Search Emails",
    "get_evidence": "Get Evidence Passages",
    "search_attachments": "Search Attachments",
    "get_thread": "Get Thread",
    "get_message": "Get Message",
    "list_threads": "List Threads",
    "query_messages": "Query Messages",
    "query_attachments": "Query Attachments",
    "get_attachment": "Get Attachment",
    "find_contact": "Find Contact",
    "list_folders": "List Folders",
    "get_mailbox_status": "Get Mailbox Status",
    "ask_mailbox": "Ask the Mailbox",
    "summarize_thread": "Summarize Thread",
    "extract_from_emails": "Extract from Emails",
    "brief_issue": "Brief an Issue (Experimental)",
    "check_conclusion": "Check a Conclusion (Experimental)",
}


def _server(db, server: FastMCP | None = None) -> FastMCP:
    """A real server with every tool group, as ``main`` registers them
    with inference configured and ``MCP_EXPERIMENTAL_TOOLS=true``."""
    server = server or FastMCP("annotations-test")
    embed = FakeEmbedClient()
    inference = FakeInferenceClient()
    register_search_tools(server, db, embed)
    register_retrieval_tools(server, db)
    register_intelligence_tools(server, db, embed, inference)
    register_experimental_tools(server, db, embed, inference)
    register_system_tools(server, db)
    return server


def _wire_tools(server: FastMCP) -> dict[str, dict]:
    """Each tool of the ``tools/list`` result as serialized for a client."""

    async def run():
        async with Client(server) as client:
            return await client.list_tools_mcp()

    result = asyncio.run(run()).model_dump(mode="json", by_alias=True, exclude_none=True)
    return {t["name"]: t for t in result["tools"]}


def _handlers(db) -> dict[str, object]:
    """The plain handler of every tool ``_server`` registers, captured by
    the ``FakeMCPServer`` stub so its docstring can be read directly."""
    fake = FakeMCPServer()
    embed = FakeEmbedClient()
    inference = FakeInferenceClient()
    register_search_tools(fake, db, embed)
    register_retrieval_tools(fake, db)
    register_intelligence_tools(fake, db, embed, inference)
    register_experimental_tools(fake, db, embed, inference)
    register_system_tools(fake, db)
    return fake.tools


def test_every_registered_tool_is_classified(empty_db):
    assert set(_wire_tools(_server(empty_db))) == set(EXPECTED_TITLES)


@pytest.mark.parametrize("name", sorted(EXPECTED_TITLES))
def test_wire_description_is_the_docstring_before_args(empty_db, name):
    """FastMCP builds the description from the docstring through griffe's
    Google parser and sends only the first text section (#1011): a line
    of words ending in a colon with an indented block under it starts an
    admonition section, and everything from it on is dropped from what
    the client receives. So every tool's wire description must carry its
    whole docstring before ``Args:``, whitespace normalised."""
    handlers = _handlers(empty_db)
    assert set(handlers) == set(EXPECTED_TITLES)
    doc = inspect.getdoc(handlers[name]) or ""
    expected = " ".join(doc.split("\nArgs:", 1)[0].split())
    wire = _wire_tools(_server(empty_db))[name]["description"]
    assert " ".join(wire.split()) == expected


@pytest.mark.parametrize("name", sorted(EXPECTED_TITLES))
def test_tool_declares_read_only_closed_world_annotations(empty_db, name):
    annotations = _wire_tools(_server(empty_db))[name].get("annotations")
    assert annotations is not None, name
    assert annotations["readOnlyHint"] is True
    assert annotations["destructiveHint"] is False
    assert annotations["openWorldHint"] is False
    assert annotations["title"] == EXPECTED_TITLES[name]
    assert annotations["title"].strip()


def test_annotations_leave_names_descriptions_and_schemas_unchanged(empty_db, monkeypatch):
    """The listing must differ from one built without annotations only
    in ``annotations`` and, where the wording differs from the name, the
    display ``title``: FastMCP takes it from ``annotations.title``, and
    without one title-cases the name."""

    def listing(server: FastMCP) -> dict[str, Tool]:
        async def run():
            async with Client(server) as client:
                return await client.list_tools()

        return {t.name: t for t in asyncio.run(run())}

    annotated = listing(_server(empty_db))

    plain = FastMCP("annotations-test")
    original_tool = plain.tool

    def tool_without_annotations(*args, **kwargs):
        kwargs.pop("annotations", None)
        return original_tool(*args, **kwargs)

    monkeypatch.setattr(plain, "tool", tool_without_annotations)
    unannotated = listing(_server(empty_db, plain))

    assert set(annotated) == set(unannotated) == set(EXPECTED_TITLES)
    for name, tool in annotated.items():
        other = unannotated[name]
        assert other.annotations is None, name
        assert tool.title == EXPECTED_TITLES[name]
        if name.replace("_", " ").title() == EXPECTED_TITLES[name]:
            assert tool.title == other.title, name
        unchanged = {"annotations", "title"}
        assert tool.model_dump(exclude=unchanged) == other.model_dump(exclude=unchanged), name
        assert {"name", "description", "inputSchema", "outputSchema"} <= set(
            tool.model_dump(by_alias=True, exclude=unchanged)
        )
