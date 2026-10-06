"""
Tool safety annotations (#899).

Every tool declares ``readOnlyHint=True``, ``destructiveHint=False`` and
``openWorldHint=False`` explicitly, plus its own human-readable
``title``: OpenAI blocks calls to tools whose hints are left at the MCP
defaults, and Anthropic's directory policy requires ``readOnlyHint``,
``destructiveHint`` and ``title``. These tests build a real FastMCP
server with every tool group registered, experimental and intelligence
included, and read the annotations through the in-memory client, so
they check what a client receives on the wire.
"""

import asyncio

import pytest
from fastmcp import Client, FastMCP
from mcp.types import Tool
from src.tools.brief import register_experimental_tools
from src.tools.intelligence import register_intelligence_tools
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools
from src.tools.system import register_system_tools

from tests.conftest import FakeEmbedClient, FakeInferenceClient

# Every tool the server can register, with its title. A new tool fails
# ``test_every_registered_tool_is_classified`` until it is classified
# deliberately and added here.
EXPECTED_TITLES = {
    "search_emails": "Search emails",
    "get_evidence": "Get evidence passages",
    "search_attachments": "Search attachments",
    "get_thread": "Get thread",
    "get_message": "Get message",
    "list_threads": "List threads",
    "query_messages": "Query messages",
    "find_contact": "Find contact",
    "list_folders": "List folders",
    "get_mailbox_status": "Get mailbox status",
    "ask_mailbox": "Ask the mailbox",
    "summarize_thread": "Summarize thread",
    "extract_from_emails": "Extract from emails",
    "brief_issue": "Brief an issue (experimental)",
    "check_conclusion": "Check a conclusion (experimental)",
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


def test_every_registered_tool_is_classified(empty_db):
    assert set(_wire_tools(_server(empty_db))) == set(EXPECTED_TITLES)


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
    in ``annotations`` and the display ``title``, which FastMCP takes
    from ``annotations.title`` (without one it title-cases the name)."""

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
        unchanged = {"annotations", "title"}
        assert tool.model_dump(exclude=unchanged) == other.model_dump(exclude=unchanged), name
        assert {"name", "description", "inputSchema", "outputSchema"} <= set(
            tool.model_dump(by_alias=True, exclude=unchanged)
        )
