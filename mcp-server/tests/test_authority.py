"""Source-authority metadata (PLAN Phase 4 item 2) in the MCP reader.

The indexer classifies each sender from the operator rules file. Here
the class is a filter (``authority_class`` on search and
``query_messages``, matching a message's From sender) and contact
metadata with its provenance rule. It never changes ranking.
"""

import asyncio
import logging

import pytest
from fastmcp.exceptions import ToolError
from src.lib.security import log_tool_call
from src.lib.sqlite import AUTHORITY_CLASSES, Database, InvalidFilterError

from tests.conftest import set_authority


def _ids(page) -> list[str]:
    return [m.message_id for m in page.messages]


@pytest.fixture
def counsel_messages_db(messages_db: Database) -> Database:
    # jane@example.com sent m1, m3 and m5 and received m2 and m4.
    set_authority(messages_db.path, "jane@example.com", "counsel", "domain:example.com")
    return messages_db


@pytest.fixture
def counsel_seeded_db(seeded_db: Database) -> Database:
    # alice sent t-alpha and only received on t-beta.
    set_authority(seeded_db.path, "alice@example.com", "counsel", "address:alice@example.com")
    return seeded_db


class TestQueryMessages:
    def test_filters_on_the_sender_class(self, counsel_messages_db):
        page = counsel_messages_db.query_messages(authority_class="counsel")
        assert _ids(page) == ["m5", "m3", "m1"]
        assert page.total_matches == 3

    def test_unclassified_is_every_other_sender(self, counsel_messages_db):
        page = counsel_messages_db.query_messages(authority_class="unclassified")
        assert _ids(page) == ["m4", "m2"]

    def test_combines_with_other_filters(self, counsel_messages_db):
        page = counsel_messages_db.query_messages(authority_class="counsel", folder="Archive")
        assert _ids(page) == ["m3"]

    def test_cursor_is_bound_to_the_class(self, counsel_messages_db):
        page = counsel_messages_db.query_messages(authority_class="counsel", limit=1)
        assert page.next_cursor
        with pytest.raises(InvalidFilterError):
            counsel_messages_db.query_messages(
                authority_class="vendor", limit=1, cursor=page.next_cursor
            )

    def test_unknown_class_is_rejected(self, counsel_messages_db):
        with pytest.raises(InvalidFilterError) as exc:
            counsel_messages_db.query_messages(authority_class="lawyers")
        assert exc.value.field_name == "authority_class"


class TestSearch:
    def test_keyword_search_keeps_threads_a_class_member_sent(self, counsel_seeded_db):
        # "lunch" (t-beta, where alice only received) and "invoice"
        # (t-alpha, which alice sent): only the latter survives.
        assert [r.thread_id for r in counsel_seeded_db.keyword_search("invoice")] == ["t-alpha"]
        assert counsel_seeded_db.keyword_search("invoice", authority_class="counsel") != []
        assert counsel_seeded_db.keyword_search("lunch", authority_class="counsel") == []

    def test_hybrid_search_filters_and_keeps_order(self, counsel_seeded_db):
        unfiltered = counsel_seeded_db.hybrid_search(
            query_text="invoice", query_embedding=[1.0, 0.0, 0.0, 0.0], limit=10
        )
        filtered = counsel_seeded_db.hybrid_search(
            query_text="invoice",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            authority_class="counsel",
            limit=10,
        )
        assert [r.thread_id for r in filtered] == ["t-alpha"]
        # Metadata only: the surviving thread keeps its unfiltered rank
        # order and score.
        kept = [r for r in unfiltered if r.thread_id == "t-alpha"]
        assert [r.score for r in filtered] == [r.score for r in kept]

    def test_semantic_search_filters(self, counsel_seeded_db):
        results = counsel_seeded_db.semantic_search(
            query_embedding=[0.0, 1.0, 0.0, 0.0], authority_class="counsel", limit=10
        )
        assert [r.thread_id for r in results] == ["t-alpha"]

    def test_counts_as_a_post_fusion_filter(self):
        assert Database._has_post_fusion_filter(authority_class="counsel") is True

    def test_unknown_class_is_rejected(self, counsel_seeded_db):
        with pytest.raises(InvalidFilterError):
            counsel_seeded_db.keyword_search("invoice", authority_class="lawyers")


class TestFindContact:
    def test_reports_class_and_rule(self, counsel_seeded_db):
        by_email = {c["email"]: c for c in counsel_seeded_db.find_contact("@example.com")}
        assert by_email["alice@example.com"]["authority_class"] == "counsel"
        assert by_email["alice@example.com"]["authority_rule"] == "address:alice@example.com"
        assert by_email["bob@example.com"]["authority_class"] == "unclassified"
        assert by_email["bob@example.com"]["authority_rule"] is None


class TestTools:
    def _handlers(self, fake_server, db):
        from src.tools.retrieval import register_retrieval_tools
        from src.tools.search import register_search_tools

        register_retrieval_tools(fake_server, db)
        register_search_tools(fake_server, db, embed_client=None)
        return fake_server.tools

    def test_query_messages_tool(self, fake_server, counsel_messages_db):
        handler = self._handlers(fake_server, counsel_messages_db)["query_messages"]
        out = asyncio.run(handler(authority_class="counsel"))
        assert out.structured_content["total_matches"] == 3
        assert {"filter": "authority_class", "value": "counsel", "match": "equals"} in (
            out.structured_content["filters"]
        )

    def test_query_messages_tool_rejects_unknown_class(self, fake_server, counsel_messages_db):
        handler = self._handlers(fake_server, counsel_messages_db)["query_messages"]
        with pytest.raises(ToolError, match="authority_class"):
            asyncio.run(handler(authority_class="lawyers"))

    def test_search_emails_tool(self, fake_server, counsel_seeded_db):
        handler = self._handlers(fake_server, counsel_seeded_db)["search_emails"]
        out = asyncio.run(handler(query="lunch", mode="keyword", authority_class="counsel"))
        assert out.structured_content["results"] == []
        out = asyncio.run(handler(query="invoice", mode="keyword", authority_class="counsel"))
        assert [r["thread_id"] for r in out.structured_content["results"]] == ["t-alpha"]

    def test_search_emails_tool_rejects_unknown_class(self, fake_server, counsel_seeded_db):
        handler = self._handlers(fake_server, counsel_seeded_db)["search_emails"]
        with pytest.raises(ToolError, match="authority_class"):
            asyncio.run(handler(query="invoice", mode="keyword", authority_class="lawyers"))

    def test_find_contact_tool_renders_authority(self, fake_server, counsel_seeded_db):
        handler = self._handlers(fake_server, counsel_seeded_db)["find_contact"]
        out = asyncio.run(handler(query="alice"))
        contact = out.structured_content["contacts"][0]
        assert contact["authority_class"] == "counsel"
        assert contact["authority_rule"] == "address:alice@example.com"
        assert "Authority: counsel (rule address:alice@example.com)" in out.content[0].text


class TestLogging:
    @pytest.mark.parametrize("value", [*AUTHORITY_CLASSES])
    def test_known_class_is_logged(self, caplog, value):
        caplog.set_level(logging.INFO)
        log_tool_call(logging.getLogger("t"), "query_messages", {"authority_class": value})
        assert f"'authority_class': '{value}'" in caplog.text

    def test_unknown_value_is_withheld(self, caplog):
        caplog.set_level(logging.INFO)
        log_tool_call(
            logging.getLogger("t"), "query_messages", {"authority_class": "secret-marker"}
        )
        assert "secret-marker" not in caplog.text
        assert "authority_class" in caplog.text

    def test_classes_match_the_indexer(self):
        assert AUTHORITY_CLASSES == (
            "counsel",
            "management",
            "vendor",
            "government",
            "personal",
            "other",
            "unclassified",
        )
