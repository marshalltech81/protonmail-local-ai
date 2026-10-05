"""
``get_evidence`` reproduces ``ask_mailbox``'s evidence set (#461).

``ask_mailbox`` fetches each surfaced thread's evidence with the
attachments whose filename or MIME type matched the question floated to
the front. The documented audit call ``get_evidence(query, thread_id)``
ordered by vector distance alone, so a cited low-similarity attachment
chunk in a long thread could fall outside its slice. All data is
synthetic.


``get_evidence(max_threads=N)`` selects threads exactly as
``ask_mailbox(max_threads=N)`` does, so the mailbox-wide audit returns
the same evidence set (#537).
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec
from fastmcp.exceptions import ToolError
from src.lib.sqlite import PROMPT_EVIDENCE_CHUNKS_PER_THREAD, Database
from src.tools.intelligence import _MAX_ASK_THREADS, register_intelligence_tools
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


def _ask_evidence(db: Database, question: str, *, reranker=None, **filters) -> dict[str, list[str]]:
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
        register_intelligence_tools(
            server, db, FakeEmbedClient(), FakeInferenceClient(), reranker=reranker
        )
        asyncio.run(server.tools["ask_mailbox"](question=question, **filters))
    finally:
        del db.hybrid_search
    return seen


def _get_evidence(db: Database, query: str, *, reranker=None, **kwargs) -> dict[str, list[str]]:
    server = FakeMCPServer()
    register_search_tools(server, db, FakeEmbedClient(), reranker=reranker)
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


class _ReverseReranker:
    """Reranker stub that reverses the RRF order of what it is given, so
    the result depends on how many candidates ``hybrid_search`` sends."""

    candidates = 1

    def rerank(self, query, documents, top_n):
        n = len(documents)
        return [(i, float(i)) for i in reversed(range(n))][:top_n]


def _spy_limits(db: Database) -> list[int]:
    """Record the ``limit`` each ``hybrid_search`` call is given."""
    limits: list[int] = []
    real = db.hybrid_search

    def spy(**kwargs):
        limits.append(kwargs["limit"])
        return real(**kwargs)

    db.hybrid_search = spy  # type: ignore[method-assign]
    return limits


def _nonempty(groups: dict[str, list[str]]) -> list[tuple[str, list[str]]]:
    return [(tid, ids) for tid, ids in groups.items() if ids]


class TestMailboxWideParity:
    @pytest.mark.parametrize("max_threads", [1, 2])
    @pytest.mark.parametrize("with_reranker", [False, True], ids=["no-rerank", "rerank"])
    @pytest.mark.parametrize(
        ("question", "filters"),
        [
            pytest.param("budget", {}, id="plain"),
            pytest.param("proposal-quote", {}, id="filename-match"),
            pytest.param("budget", {"folders": ["INBOX", "Archive"]}, id="filtered"),
            pytest.param("budget", {"from_addr": "carol@example.org"}, id="sender-filtered"),
        ],
    )
    def test_same_chunks_in_same_order(
        self, parity_db, question, filters, max_threads, with_reranker
    ):
        """Same threads, same per-thread chunks, same order as the
        evidence ``ask_mailbox`` retrieved for the same arguments."""
        reranker = _ReverseReranker() if with_reranker else None
        asked = _ask_evidence(
            parity_db, question, reranker=reranker, max_threads=max_threads, **filters
        )
        assert asked, "the corpus must surface at least one thread"
        audited = _get_evidence(
            parity_db, question, reranker=reranker, max_threads=max_threads, **filters
        )
        assert _nonempty(audited) == _nonempty(asked)

    def test_reranker_sees_the_same_pool(self, parity_db):
        """The issue's case: with a reranker, a chunk-sized thread limit
        sent more candidates to it and changed the top thread.

        Two more matching threads make the pool larger than the rerank
        window's floor (the keyword slot, #701), so the two limits still
        send different candidate counts."""
        conn = sqlite3.connect(parity_db.path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        for n in (1, 2):
            _insert_thread(
                conn,
                thread_id=f"t-extra-{n}",
                subject=f"budget note {n}",
                participants=["erin@example.net"],
                senders=["erin@example.net"],
                body_text=f"budget note {n} text",
                embedding=[1.0, 0.0, 0.0, 0.0],
            )
        conn.commit()
        conn.close()
        asked = _ask_evidence(parity_db, "budget", reranker=_ReverseReranker(), max_threads=1)
        audited = _get_evidence(parity_db, "budget", reranker=_ReverseReranker(), max_threads=1)
        assert list(audited) == list(asked)
        legacy = _get_evidence(parity_db, "budget", reranker=_ReverseReranker())
        assert list(legacy)[0] != list(asked)[0]

    def test_chunk_budget_defaults_to_the_full_evidence_set(self, parity_db):
        audited = _get_evidence(parity_db, "budget", max_threads=2)
        assert sum(len(ids) for ids in audited.values()) == PROMPT_EVIDENCE_CHUNKS_PER_THREAD + 3

    def test_smaller_limit_returns_a_rank_order_prefix(self, parity_db):
        """A ``limit`` below the full evidence set cuts it in rank order:
        the first ``limit`` chunks of ask_mailbox's evidence, and the
        threads past the cut are left out (review round 2, documented)."""
        asked = _ask_evidence(parity_db, "budget", max_threads=2)
        flat = [(tid, c) for tid, ids in asked.items() for c in ids]
        assert len(asked) == 2 and len(flat) > PROMPT_EVIDENCE_CHUNKS_PER_THREAD
        cut = PROMPT_EVIDENCE_CHUNKS_PER_THREAD
        audited = _get_evidence(parity_db, "budget", max_threads=2, limit=cut)
        assert [(tid, c) for tid, ids in audited.items() for c in ids] == flat[:cut]
        assert list(audited) == list(asked)[:1]

    def test_explicit_limit_still_caps_chunks(self, parity_db):
        audited = _get_evidence(parity_db, "budget", max_threads=2, limit=3)
        assert sum(len(ids) for ids in audited.values()) == 3

    @pytest.mark.parametrize(
        ("given", "expected"),
        [(0, 1), (-4, 1), (500, _MAX_ASK_THREADS), ("many", 5), (None, 12)],
    )
    def test_thread_limit_is_clamped_like_ask_mailbox(self, parity_db, given, expected):
        """``max_threads`` takes ask_mailbox's clamp; omitted, the thread
        limit stays the chunk ``limit`` (default 12), as before."""
        limits = _spy_limits(parity_db)
        kwargs = {} if given is None else {"max_threads": given}
        _get_evidence(parity_db, "budget", **kwargs)
        assert limits == [expected]

    def test_omitted_max_threads_is_unchanged(self, parity_db):
        limits = _spy_limits(parity_db)
        audited = _get_evidence(parity_db, "budget", limit=4)
        assert limits == [4]
        assert sum(len(ids) for ids in audited.values()) == 4

    def test_rejected_with_thread_id(self, parity_db):
        with pytest.raises(ToolError, match="max_threads"):
            _get_evidence(parity_db, "budget", thread_id="t-quote", max_threads=2)


class TestChunklessThreads:
    """A surfaced thread with no indexed chunks reaches ask_mailbox's
    prompt as its indexed thread text, so the audit keeps it, in place
    and with no chunks (review round 1)."""

    @pytest.fixture
    def db_with_chunkless(self, parity_db):
        conn = sqlite3.connect(parity_db.path)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _insert_thread(
            conn,
            thread_id="t-bare",
            subject="budget memo",
            participants=["dave@example.net"],
            senders=["dave@example.net"],
            body_text="budget memo text with no chunks indexed",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        conn.commit()
        conn.close()
        return parity_db

    def test_same_threads_in_same_order(self, db_with_chunkless):
        asked = _ask_evidence(db_with_chunkless, "budget memo", max_threads=3)
        assert asked.get("t-bare") == [], "the corpus must surface the chunkless thread"
        audited = _get_evidence(db_with_chunkless, "budget memo", max_threads=3)
        assert list(audited.items()) == list(asked.items())

    def test_prose_names_the_thread_text_source(self, db_with_chunkless):
        server = FakeMCPServer()
        register_search_tools(server, db_with_chunkless, FakeEmbedClient())
        out = asyncio.run(server.tools["get_evidence"](query="budget memo", max_threads=3))
        text = out.content[0].text
        assert "Thread ID: t-bare" in text
        assert "get_thread" in text

    def test_only_chunkless_threads_still_list_them(self, db_with_chunkless):
        """Folder-scoped to the chunkless thread alone: the result names
        it rather than reporting no evidence."""
        audited = _get_evidence(
            db_with_chunkless, "budget memo", max_threads=3, from_addr="dave@example.net"
        )
        assert audited == {"t-bare": []}

    def test_chunkless_thread_past_the_cut_is_left_out(self, db_with_chunkless):
        """The ``limit`` cut is a rank-order prefix for every thread,
        chunkless ones included."""
        asked = _ask_evidence(db_with_chunkless, "budget memo", max_threads=3)
        assert list(asked)[-1] == "t-bare", "the chunkless thread must rank last"
        audited = _get_evidence(db_with_chunkless, "budget memo", max_threads=3, limit=1)
        assert list(audited) == list(asked)[:1]

    def test_without_max_threads_chunkless_threads_still_drop(self, db_with_chunkless):
        audited = _get_evidence(db_with_chunkless, "budget memo")
        assert "t-bare" not in audited
