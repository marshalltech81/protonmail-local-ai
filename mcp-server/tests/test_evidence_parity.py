"""
``get_evidence`` reproduces ``ask_mailbox``'s evidence set (#461).

``ask_mailbox`` fetches each surfaced thread's evidence with the
attachments whose filename or MIME type matched the question floated to
the front. The documented audit call ``get_evidence(query, thread_id)``
ordered by vector distance alone, so a cited low-similarity attachment
chunk in a long thread could fall outside its slice. All data is
synthetic.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec
from src.lib.sqlite import PROMPT_EVIDENCE_CHUNKS_PER_THREAD, Database
from src.tools.intelligence import register_intelligence_tools
from src.tools.search import register_search_tools

from tests.conftest import (
    FakeEmbedClient,
    FakeInferenceClient,
    FakeMCPServer,
    _build_schema,
    _insert_attachment,
    _insert_chunk,
    _insert_thread,
)

# More body chunks than ask_mailbox keeps per thread, all closer to the
# query vector than the attachment chunk, so dense order alone leaves
# the attachment out of every slice ask_mailbox would use.
_BODY_CHUNKS = PROMPT_EVIDENCE_CHUNKS_PER_THREAD + 4


@pytest.fixture
def parity_db(tmp_path: Path) -> Database:
    db_path = tmp_path / "parity.db"
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)

    _insert_thread(
        conn,
        thread_id="t-quote",
        subject="budget proposal cover note",
        participants=["alice@example.com", "bob@example.com"],
        senders=["alice@example.com"],
        body_text="budget discussion, please see the attached proposal",
        has_attachments=True,
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    for i in range(_BODY_CHUNKS):
        _insert_chunk(
            conn,
            chunk_id=f"t-quote-body-{i}",
            message_id="t-quote",
            thread_id="t-quote",
            text=f"budget discussion part {i}",
            embedding=[1.0, 0.01 * i, 0.0, 0.0],
            chunk_index=i,
            char_start=100 * i,
        )
    _insert_attachment(
        conn,
        message_id="t-quote",
        thread_id="t-quote",
        attachment_id="att-quote",
        filename="proposal-quote.pdf",
    )
    _insert_chunk(
        conn,
        chunk_id="t-quote-att",
        message_id="t-quote",
        thread_id="t-quote",
        text="line item: solar installation total USD 18450",
        embedding=[0.0, 0.0, 0.0, 1.0],
        chunk_index=0,
        attachment_id="att-quote",
    )

    _insert_thread(
        conn,
        thread_id="t-other",
        subject="budget review",
        participants=["carol@example.org", "bob@example.com"],
        senders=["carol@example.org"],
        folder="Archive",
        body_text="budget review notes",
        embedding=[0.9, 0.1, 0.0, 0.0],
    )
    for i in range(3):
        _insert_chunk(
            conn,
            chunk_id=f"t-other-body-{i}",
            message_id="t-other",
            thread_id="t-other",
            text=f"budget review notes part {i}",
            embedding=[0.9, 0.1 * i, 0.0, 0.0],
            chunk_index=i,
            char_start=100 * i,
        )
    conn.close()
    return Database(str(db_path))


def _ask_evidence(db: Database, question: str, **filters) -> dict[str, list[str]]:
    """The per-thread chunk IDs ``ask_mailbox`` puts in front of its model."""
    seen: dict[str, list[str]] = {}
    real = db.hybrid_search

    def capture(**kwargs):
        results = real(**kwargs)
        for r in results:
            seen[r.thread_id] = [c.chunk_id for c in r.evidence_chunks]
        return results

    db.hybrid_search = capture  # type: ignore[method-assign]
    try:
        server = FakeMCPServer()
        register_intelligence_tools(server, db, FakeEmbedClient(), FakeInferenceClient())
        asyncio.run(server.tools["ask_mailbox"](question=question, **filters))
    finally:
        del db.hybrid_search
    return seen


def _get_evidence(db: Database, query: str, **kwargs) -> dict[str, list[str]]:
    server = FakeMCPServer()
    register_search_tools(server, db, FakeEmbedClient())
    out = asyncio.run(server.tools["get_evidence"](query=query, **kwargs))
    return {
        t["thread_id"]: [c["chunk_id"] for c in t["chunks"]]
        for t in out.structured_content["threads"]
    }


class TestThreadScopedAudit:
    def test_filename_matched_attachment_chunk_is_returned(self, parity_db):
        """The issue's case: ask_mailbox cites the attachment chunk, and
        the thread-scoped audit call with the same query returns it."""
        asked = _ask_evidence(parity_db, "proposal-quote")
        assert "t-quote-att" in asked["t-quote"]

        audited = _get_evidence(
            parity_db,
            "proposal-quote",
            thread_id="t-quote",
            limit=PROMPT_EVIDENCE_CHUNKS_PER_THREAD,
        )
        assert "t-quote-att" in audited["t-quote"]

    def test_attachment_leads_when_the_query_names_it(self, parity_db):
        audited = _get_evidence(parity_db, "proposal-quote", thread_id="t-quote", limit=1)
        assert audited["t-quote"] == ["t-quote-att"]

    def test_unmatched_query_keeps_dense_order(self, parity_db):
        """No filename match: ranking is unchanged (dense order only)."""
        audited = _get_evidence(parity_db, "budget", thread_id="t-quote", limit=3)
        assert audited["t-quote"] == [f"t-quote-body-{i}" for i in range(3)]


class TestEvidenceParity:
    @pytest.mark.parametrize(
        ("question", "filters"),
        [
            pytest.param("budget", {}, id="plain"),
            pytest.param("proposal-quote", {}, id="filename-match"),
            pytest.param("budget proposal-quote", {"folders": ["INBOX"]}, id="filtered"),
            pytest.param(
                "proposal-quote", {"from_addr": "alice@example.com"}, id="sender-filtered"
            ),
        ],
    )
    def test_audit_reproduces_ask_mailbox_evidence(self, parity_db, question, filters):
        asked = _ask_evidence(parity_db, question, **filters)
        assert asked, "the corpus must surface at least one thread"

        # Per thread: the thread-scoped audit at ask_mailbox's per-thread
        # cap returns the same chunks in the same order.
        for thread_id, chunk_ids in asked.items():
            audited = _get_evidence(
                parity_db,
                question,
                thread_id=thread_id,
                limit=PROMPT_EVIDENCE_CHUNKS_PER_THREAD,
            )
            assert audited.get(thread_id, []) == chunk_ids

        # Mailbox-wide with the same filters: every chunk ask_mailbox
        # used is in the audit result.
        wide = _get_evidence(parity_db, question, limit=60, **filters)
        asked_ids = {c for ids in asked.values() for c in ids}
        wide_ids = {c for ids in wide.values() for c in ids}
        assert asked_ids <= wide_ids
