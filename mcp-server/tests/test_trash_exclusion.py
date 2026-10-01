"""
Default Trash exclusion across the MCP tools (#441).

Mail filed in Trash stays indexed but leaves every mailbox-wide tool by
default; naming Trash in ``folders`` (``folder`` for query_messages)
brings it back, and tools that read one named thread or message are
unaffected. The mailbox is ``_trash_db`` from ``test_sqlite``: one
INBOX thread, one thread wholly in Trash, and one with a Trash root and
an INBOX reply. All data is synthetic.
"""

import asyncio

import pytest
from src.lib.sqlite import Database
from src.tools.brief import register_experimental_tools
from src.tools.intelligence import register_intelligence_tools
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools

from tests.conftest import FakeEmbedClient, FakeInferenceClient, FakeMCPServer
from tests.test_sqlite import _trash_db

_DEFAULT = {"t-kept", "t-mixed"}
_WITH_TRASH = {"t-trashed", "t-mixed"}


def _tools(db: Database) -> dict:
    server = FakeMCPServer()
    embed, inference = FakeEmbedClient(), FakeInferenceClient()
    register_search_tools(server, db, embed)
    register_retrieval_tools(server, db)
    register_intelligence_tools(server, db, embed, inference)
    register_experimental_tools(server, db, embed, inference)
    return server.tools


def _record_searches(db: Database) -> list[set[str]]:
    """Record the thread IDs each ``hybrid_search`` call returns."""
    seen: list[set[str]] = []
    real = db.hybrid_search

    def spy(*args, **kwargs):
        results = real(*args, **kwargs)
        seen.append({r.thread_id for r in results})
        return results

    db.hybrid_search = spy  # type: ignore[method-assign]
    return seen


class TestSearchTools:
    @pytest.mark.parametrize("mode", ["hybrid", "semantic", "keyword"])
    def test_search_emails(self, tmp_path, mode):
        tools = _tools(_trash_db(tmp_path))
        out = asyncio.run(tools["search_emails"](query="invoice", mode=mode))
        assert {r["thread_id"] for r in out.structured_content["results"]} == _DEFAULT
        out = asyncio.run(tools["search_emails"](query="invoice", mode=mode, folders=["Trash"]))
        assert {r["thread_id"] for r in out.structured_content["results"]} == _WITH_TRASH

    def test_get_evidence_mailbox_wide(self, tmp_path):
        tools = _tools(_trash_db(tmp_path))
        out = asyncio.run(tools["get_evidence"](query="invoice"))
        assert {t["thread_id"] for t in out.structured_content["threads"]} == _DEFAULT
        out = asyncio.run(tools["get_evidence"](query="invoice", folders=["Trash"]))
        assert {t["thread_id"] for t in out.structured_content["threads"]} == _WITH_TRASH

    def test_get_evidence_for_a_named_thread_is_unaffected(self, tmp_path):
        tools = _tools(_trash_db(tmp_path))
        out = asyncio.run(tools["get_evidence"](query="invoice", thread_id="t-trashed"))
        assert [t["thread_id"] for t in out.structured_content["threads"]] == ["t-trashed"]

    def test_search_attachments(self, tmp_path):
        tools = _tools(_trash_db(tmp_path))
        out = asyncio.run(tools["search_attachments"](query="invoice"))
        assert [a["filename"] for a in out.structured_content["results"]] == ["invoice-t-kept.pdf"]


class TestRetrievalTools:
    def test_query_messages(self, tmp_path):
        tools = _tools(_trash_db(tmp_path))
        out = asyncio.run(tools["query_messages"](subject="invoice"))
        assert {m["message_id"] for m in out.structured_content["messages"]} == {
            "m-kept",
            "m-mixed-reply",
        }
        out = asyncio.run(tools["query_messages"](subject="invoice", folder="Trash"))
        assert {m["message_id"] for m in out.structured_content["messages"]} == {
            "m-trashed",
            "m-mixed-root",
        }

    def test_get_thread_and_message_are_unaffected(self, tmp_path):
        tools = _tools(_trash_db(tmp_path))
        thread = asyncio.run(tools["get_thread"](thread_id="t-trashed"))
        assert thread.structured_content["thread"]["thread_id"] == "t-trashed"
        message = asyncio.run(tools["get_message"](message_id="m-trashed"))
        assert message.structured_content["message"]["message_id"] == "m-trashed"


class TestSynthesisTools:
    """These all retrieve through ``hybrid_search``; the spy records
    what each retrieval returned."""

    @pytest.mark.parametrize(
        ("tool", "kwargs"),
        [
            ("ask_mailbox", {"question": "invoice"}),
            ("extract_from_emails", {"query": "invoice", "schema": {"amount": "string"}}),
            ("brief_issue", {"topic": "invoice"}),
            ("check_conclusion", {"conclusion": "the invoice was paid"}),
        ],
    )
    def test_default_and_named_trash(self, tmp_path, tool, kwargs):
        db = _trash_db(tmp_path)
        seen = _record_searches(db)
        tools = _tools(db)
        asyncio.run(tools[tool](**kwargs))
        asyncio.run(tools[tool](**kwargs, folders=["Trash"]))
        assert seen == [_DEFAULT, _WITH_TRASH]

    def test_summarize_thread_by_id_is_unaffected(self, tmp_path):
        db = _trash_db(tmp_path)
        seen = _record_searches(db)
        asyncio.run(_tools(db)["summarize_thread"](thread_id="t-trashed"))
        # The named thread is read directly; no search runs.
        assert seen == []
