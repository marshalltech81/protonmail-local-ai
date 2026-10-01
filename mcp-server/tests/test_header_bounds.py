"""
#243: sender-controlled header values (subject, display names, attachment
filename and MIME type) must be cut in every tool response and inference
prompt, not only in get_thread.

One thread carries a 140K-character subject, participants whose display
names are 50K characters, and an attachment with a 50K-character
filename and MIME type, next to a tiny body. Each tool's serialized
result, and each prompt sent to the model, must stay small.
"""

import asyncio
import json
import sqlite3

import pytest
import sqlite_vec
from src.lib.sqlite import Database, ThreadResult
from src.tools.intelligence import register_intelligence_tools
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools

from tests.conftest import (
    FakeEmbedClient,
    FakeInferenceClient,
    _build_schema,
    _insert_attachment,
    _insert_chunk,
    _insert_extraction,
    _insert_thread,
)

HUGE_SUBJECT = "syntheticword " * 10_000
HUGE_NAME = "N" * 50_000
HUGE_FILENAME = "f" * 50_000 + ".pdf"
HUGE_MIME = "application/" + "x" * 50_000
PARTICIPANTS = [f"{HUGE_NAME} <p{i}@example.com>" for i in range(12)]

# Far above any bounded response (a 500-character cut per value, a few
# values per thread) and far below the unbounded one (over 100K characters).
MAX_RESULT_CHARS = 30_000


@pytest.fixture
def huge_db(tmp_path):
    path = tmp_path / "huge-headers.db"
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    _insert_thread(
        conn,
        thread_id="t",
        subject=HUGE_SUBJECT,
        participants=PARTICIPANTS,
        senders=PARTICIPANTS,
        body_text="invoice",
        snippet="invoice",
        has_attachments=True,
        embedding=[1.0, 0.0, 0.0, 0.0],
        message_ids=["m"],
    )
    _insert_attachment(
        conn,
        message_id="m",
        thread_id="t",
        attachment_id="a",
        filename=HUGE_FILENAME,
        content_type=HUGE_MIME,
    )
    _insert_extraction(conn, attachment_id="a", extracted_text="invoice total")
    _insert_chunk(
        conn,
        chunk_id="c-body",
        message_id="m",
        thread_id="t",
        text="invoice",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    _insert_chunk(
        conn,
        chunk_id="c-att",
        message_id="m",
        thread_id="t",
        text="invoice total",
        embedding=[1.0, 0.0, 0.0, 0.0],
        chunk_index=1,
        attachment_id="a",
    )
    conn.close()
    db = Database(str(path))
    yield db


def _size(result) -> int:
    """Characters in a ``CallToolResult``'s prose plus its structured output."""
    prose = sum(len(c.text) for c in result.content)
    return prose + len(json.dumps(result.structured_content))


class TestToolResponses:
    def test_search_emails(self, fake_server, huge_db):
        register_search_tools(fake_server, huge_db, FakeEmbedClient())
        for mode in ("keyword", "hybrid"):
            out = asyncio.run(fake_server.tools["search_emails"](query="invoice", mode=mode))
            assert out.structured_content["results"], mode
            assert _size(out) < MAX_RESULT_CHARS, mode

    def test_get_evidence_by_thread(self, fake_server, huge_db):
        register_search_tools(fake_server, huge_db, FakeEmbedClient())
        out = asyncio.run(fake_server.tools["get_evidence"](query="invoice", thread_id="t"))
        assert out.structured_content["chunk_count"] == 2
        assert _size(out) < MAX_RESULT_CHARS

    def test_get_evidence_by_search(self, fake_server, huge_db):
        register_search_tools(fake_server, huge_db, FakeEmbedClient())
        out = asyncio.run(fake_server.tools["get_evidence"](query="invoice"))
        assert out.structured_content["chunk_count"] == 2
        assert _size(out) < MAX_RESULT_CHARS

    def test_search_attachments(self, fake_server, huge_db):
        register_search_tools(fake_server, huge_db, FakeEmbedClient())
        out = asyncio.run(fake_server.tools["search_attachments"](query="invoice"))
        assert out.structured_content["results"]
        assert _size(out) < MAX_RESULT_CHARS

    def test_list_threads(self, fake_server, huge_db):
        register_retrieval_tools(fake_server, huge_db)
        out = asyncio.run(fake_server.tools["list_threads"](limit=1))
        assert out.structured_content["threads"]
        assert _size(out) < MAX_RESULT_CHARS

    def test_ids_stay_whole(self, fake_server, huge_db):
        # Cutting applies to header text only; IDs chain to the next call.
        register_search_tools(fake_server, huge_db, FakeEmbedClient())
        out = asyncio.run(fake_server.tools["search_attachments"](query="invoice"))
        hit = out.structured_content["results"][0]
        assert (hit["attachment_id"], hit["message_id"], hit["thread_id"]) == ("a", "m", "t")


class TestInferencePrompts:
    def _tools(self, fake_server, db, llm):
        register_intelligence_tools(fake_server, db, FakeEmbedClient(), llm)
        return fake_server.tools

    def _assert_bounded(self, llm, out):
        assert llm.complete_calls
        for _system, user in llm.complete_calls:
            assert len(user) < MAX_RESULT_CHARS
        assert sum(len(c.text) for c in out) < MAX_RESULT_CHARS

    def test_ask_mailbox(self, fake_server, huge_db):
        llm = FakeInferenceClient()
        out = asyncio.run(
            self._tools(fake_server, huge_db, llm)["ask_mailbox"](question="invoice", max_threads=1)
        )
        self._assert_bounded(llm, out.content)

    def test_summarize_thread(self, fake_server, huge_db):
        llm = FakeInferenceClient()
        out = asyncio.run(self._tools(fake_server, huge_db, llm)["summarize_thread"](thread_id="t"))
        self._assert_bounded(llm, out)

    def test_extract_from_emails(self, fake_server, huge_db):
        llm = FakeInferenceClient(response='{"total": 1}')
        out = asyncio.run(
            self._tools(fake_server, huge_db, llm)["extract_from_emails"](
                query="invoice", schema={"total": "number"}, limit=1
            )
        )
        self._assert_bounded(llm, out)


def test_rerank_candidate_text_cuts_subject(huge_db):
    thread = huge_db.get_thread("t")
    assert isinstance(thread, ThreadResult)
    assert len(Database._candidate_text(thread)) < MAX_RESULT_CHARS
