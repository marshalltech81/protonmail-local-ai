"""Source-authority metadata (PLAN Phase 4 item 2) in the MCP reader.

The indexer classifies each sender from the operator rules file. Here
the class is a filter (``authority_class`` on search and
``query_messages``, matching a message's From sender) and contact
metadata with its provenance rule. It never changes ranking.
"""

import asyncio
import logging
import sqlite3
from contextlib import closing

import pytest
import sqlite_vec
from fastmcp.exceptions import ToolError
from src.lib.predicates import AUTHORITY_CLASSES
from src.lib.security import log_tool_call
from src.lib.sqlite import Database, InvalidFilterError

from tests.conftest import _build_schema, _insert_thread, claimant_of, set_authority
from tests.test_sqlite import _TARGET, _scoped_recall_db, _search


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


class TestReviewRound2Blank:
    """Codex round 2 on #459: a blank ``authority_class`` is ignored, like
    every other blank string filter, at every entry point."""

    @pytest.mark.parametrize("blank", ["", "  "])
    def test_query_messages_ignores_blank(self, counsel_messages_db, blank):
        page = counsel_messages_db.query_messages(authority_class=blank)
        assert page.total_matches == 5
        # Same filters as no class at all, so the cursor carries over.
        first = counsel_messages_db.query_messages(authority_class=blank, limit=2)
        nxt = counsel_messages_db.query_messages(limit=2, cursor=first.next_cursor)
        assert _ids(nxt) == ["m3", "m2"]

    @pytest.mark.parametrize("blank", ["", "  "])
    def test_searches_ignore_blank(self, counsel_seeded_db, blank):
        everything = [r.thread_id for r in counsel_seeded_db.keyword_search("lunch")]
        assert everything
        assert [
            r.thread_id for r in counsel_seeded_db.keyword_search("lunch", authority_class=blank)
        ] == everything
        assert counsel_seeded_db.hybrid_search(
            query_text="lunch",
            query_embedding=[0.0, 1.0, 0.0, 0.0],
            authority_class=blank,
        )
        assert counsel_seeded_db.semantic_search(
            query_embedding=[0.0, 1.0, 0.0, 0.0], authority_class=blank
        )

    @pytest.mark.parametrize("blank", ["", "  "])
    def test_tools_ignore_blank(self, fake_server, counsel_seeded_db, blank):
        handlers = TestTools()._handlers(fake_server, counsel_seeded_db)
        out = asyncio.run(
            handlers["search_emails"](query="lunch", mode="keyword", authority_class=blank)
        )
        assert out.structured_content["results"]
        out = asyncio.run(handlers["query_messages"](authority_class=blank))
        assert out.structured_content["filters"] == []

    def test_surrounding_whitespace_is_stripped(self, counsel_messages_db):
        page = counsel_messages_db.query_messages(authority_class=" counsel ")
        assert _ids(page) == ["m5", "m3", "m1"]


_COUNSEL = "counsel@firm.example"
_READER = "reader@home.example"


@pytest.fixture
def spam_db(tmp_path) -> Database:
    """A counsel-classified sender with mail in INBOX and in Spam (#463).

    - ``t-inbox``: the sender's message in INBOX.
    - ``t-spam``: an identical message from the same sender in Spam.
    - ``t-mixed``: two messages from the sender, ``mixed-1`` in Spam
      and ``mixed-2`` in INBOX.
    - ``t-spam-reply``: the sender's ``sr-1`` in Spam answered by the
      unclassified reader's ``sr-2`` in INBOX.
    """
    db_path = tmp_path / "spam.db"
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    for thread_id, folder, message_ids in [
        ("t-inbox", "INBOX", None),
        ("t-spam", "Spam", None),
        ("t-mixed", "INBOX", ["mixed-1", "mixed-2"]),
        ("t-spam-reply", "INBOX", ["sr-1", "sr-2"]),
    ]:
        _insert_thread(
            conn,
            thread_id=thread_id,
            subject="retainer terms",
            participants=[_COUNSEL, _READER],
            senders=[_COUNSEL],
            folder=folder,
            body_text="retainer terms",
            message_ids=message_ids,
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
    conn.execute("UPDATE messages SET folder = 'Spam' WHERE message_id IN ('mixed-1', 'sr-1')")
    # sr-2 is the reader's reply: the reader sent it, counsel received it.
    conn.execute(
        "UPDATE message_participants SET role = CASE address WHEN ? THEN 'to' ELSE 'from' END "
        "WHERE claimant_id = ?",
        (_COUNSEL, claimant_of("sr-2")),
    )
    conn.commit()
    conn.close()
    set_authority(db_path, _COUNSEL, "counsel", "domain:firm.example")
    return Database(str(db_path))


class TestSpamIsNeverAuthority:
    """#463 (first step): authority comes from the claimed From address,
    and Proton files most spoofed mail in Spam, so a Spam message never
    counts toward an authority filter. A thread counts only through its
    non-Spam messages."""

    def test_query_messages_skips_spam(self, spam_db):
        page = spam_db.query_messages(authority_class="counsel")
        assert sorted(_ids(page)) == ["mixed-2", "t-inbox"]

    def test_spam_is_not_unclassified_either(self, spam_db):
        # The filter ignores Spam for every class: with no rule for the
        # sender, only the non-Spam messages are unclassified matches.
        with closing(sqlite3.connect(spam_db.path)) as conn:
            conn.execute(
                "UPDATE entities SET authority_class = 'unclassified', authority_rule = NULL"
            )
            conn.commit()
        page = spam_db.query_messages(authority_class="unclassified")
        assert sorted(_ids(page)) == ["mixed-2", "sr-2", "t-inbox"]

    def test_spam_still_matches_without_the_filter(self, spam_db):
        assert spam_db.query_messages(folder="Spam").total_matches == 3

    def test_keyword_search(self, spam_db):
        assert len(spam_db.keyword_search("retainer", limit=10)) == 4
        results = spam_db.keyword_search("retainer", authority_class="counsel", limit=10)
        assert sorted(r.thread_id for r in results) == ["t-inbox", "t-mixed"]

    def test_semantic_search(self, spam_db):
        results = spam_db.semantic_search(
            query_embedding=[1.0, 0.0, 0.0, 0.0], authority_class="counsel", limit=10
        )
        assert sorted(r.thread_id for r in results) == ["t-inbox", "t-mixed"]

    def test_hybrid_search(self, spam_db):
        results = spam_db.hybrid_search(
            query_text="retainer",
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            authority_class="counsel",
            limit=10,
        )
        assert sorted(r.thread_id for r in results) == ["t-inbox", "t-mixed"]

    def test_find_contact_stays_entity_level(self, spam_db):
        (contact,) = [c for c in spam_db.find_contact("firm.example") if c["email"] == _COUNSEL]
        assert contact["authority_class"] == "counsel"

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_vector_window_widens_past_spam(self, tmp_path, mode):
        # Every noise thread nearer the query than the target is now a
        # counsel sender's mail in Spam. The window must not count them
        # as eligible, so it still widens until it reaches the target.
        db = _scoped_recall_db(tmp_path)
        with closing(sqlite3.connect(db.path)) as conn:
            conn.execute("UPDATE messages SET folder = 'Spam' WHERE thread_id LIKE 't-noise-%'")
            conn.commit()
        set_authority(db.path, "digest@noise.example", "counsel", "domain:noise.example")
        results = _search(db, mode, authority_class="counsel")
        assert [r.thread_id for r in results] == [_TARGET]
        assert {"thread_vec", "chunk_vec"} <= results[0].lane_ranks.keys()
