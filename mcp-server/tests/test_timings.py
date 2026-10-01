"""
Per-call stage timings on the MCP query path (#287).

Each timed tool logs one ``mcp.timings`` line holding stage durations,
counts and config identifiers. These tests pin which stages appear for
each tool and mode, that a stage that did not run is absent, and that
nothing from the query, the mailbox or the provider reaches the log.
"""

import ast
import asyncio
import logging
import re
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec
from fastmcp.exceptions import ToolError
from src.lib.sqlite import Database
from src.lib.timings import count, rerank_mode, stage, timed_tool
from src.tools.intelligence import register_intelligence_tools
from src.tools.search import register_search_tools

from tests.conftest import (
    FakeEmbedClient,
    FakeInferenceClient,
    _build_schema,
    _insert_chunk,
    _insert_thread,
)
from tests.test_sqlite import _scoped_recall_db, _search, _spy_vector_k

_LINE = re.compile(
    r"^tool=(?P<tool>\w+) outcome=(?P<outcome>\w+) total_ms=(?P<total>[\d.]+) "
    r"stages_ms=(?P<stages>\{.*?\}) counts=(?P<counts>\{.*?\}) config=(?P<config>\{.*?\})$"
)

# Invented token; it must never appear in any log record.
MARKER = "zqxmarker7731"

_VECTOR_LANES = {"thread_vec", "chunk_vec"}
_KEYWORD_LANES = {"thread_fts", "chunk_fts", "attachment_fts"}


def _timing_lines(caplog) -> list[dict]:
    lines = []
    for record in caplog.records:
        if record.name != "mcp.timings":
            continue
        match = _LINE.match(record.getMessage())
        assert match, record.getMessage()
        lines.append(
            {
                "tool": match["tool"],
                "outcome": match["outcome"],
                "total_ms": float(match["total"]),
                "stages": ast.literal_eval(match["stages"]),
                "counts": ast.literal_eval(match["counts"]),
                "config": ast.literal_eval(match["config"]),
            }
        )
    return lines


def _one_line(caplog) -> dict:
    lines = _timing_lines(caplog)
    assert len(lines) == 1, lines
    return lines[0]


class _Reranker:
    mode = "cohere"
    candidates = 10

    def rerank(self, query, documents, top_n):
        return [(i, float(len(documents) - i)) for i in range(len(documents))][:top_n]


@pytest.fixture
def marker_db(tmp_path: Path):
    """A one-thread index whose subject, participants, body and chunk
    all carry ``MARKER``, so retrieval returns it to every tool."""
    db_path = tmp_path / "marker.db"
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    _insert_thread(
        conn,
        thread_id="t-marker",
        subject=f"quarterly {MARKER} report",
        participants=[f"{MARKER}@example.com"],
        senders=[f"{MARKER}@example.com"],
        snippet=f"the {MARKER} figures",
        body_text=f"the {MARKER} figures are attached",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    _insert_chunk(
        conn,
        chunk_id="marker-c1",
        message_id="t-marker",
        thread_id="t-marker",
        text=f"the {MARKER} figures are attached",
        embedding=[1.0, 0.0, 0.0, 0.0],
    )
    conn.close()
    return Database(str(db_path))


class TestHelpers:
    def test_stage_and_count_are_noops_outside_a_timed_call(self, caplog):
        caplog.set_level(logging.INFO)
        with stage("thread_fts"):
            pass
        count("results", 3)
        assert _timing_lines(caplog) == []

    def test_stage_accumulates_and_failure_is_logged_as_error(self, caplog):
        caplog.set_level(logging.INFO)

        @timed_tool("demo", inference="openai")
        async def demo():
            with stage("inference"):
                pass
            with stage("inference"):
                pass
            count("inference_calls", 1)
            count("inference_calls", 1)
            raise ValueError("boom")

        with pytest.raises(ValueError):
            asyncio.run(demo())
        line = _one_line(caplog)
        assert line["tool"] == "demo"
        assert line["outcome"] == "error"
        assert set(line["stages"]) == {"inference"}
        assert line["counts"] == {"inference_calls": 2}
        assert line["config"] == {"inference": "openai"}

    def test_rerank_mode_names_the_layer(self):
        from src.lib.reranker import CohereReranker, RerankConfig

        cohere = CohereReranker(
            RerankConfig(
                base_url="",
                model="rerank-v4.0-pro",
                api_key="ck-test",  # pragma: allowlist secret
                candidates=10,
            )
        )
        assert rerank_mode(None) == "none"
        assert rerank_mode(cohere) == "cohere"
        assert rerank_mode(object()) == "enabled"

    def test_wrapper_keeps_handler_name_and_docstring(self):
        @timed_tool("demo")
        async def handler(query: str) -> str:
            """Doc."""
            return query

        assert handler.__name__ == "handler"
        assert handler.__doc__ == "Doc."
        assert asyncio.run(handler("x")) == "x"


class TestSearchEmailsTimings:
    def test_hybrid_records_every_lane_and_no_rerank(
        self, caplog, fake_server, fake_embed, chunked_db
    ):
        caplog.set_level(logging.INFO)
        register_search_tools(fake_server, chunked_db, fake_embed)
        asyncio.run(fake_server.tools["search_emails"](query="invoice", mode="hybrid"))
        line = _one_line(caplog)
        assert line["tool"] == "search_emails"
        assert line["outcome"] == "ok"
        assert set(line["stages"]) == {"query_embedding", "fusion"} | _KEYWORD_LANES | (
            _VECTOR_LANES
        )
        assert line["config"] == {"rerank": "none"}
        counts = line["counts"]
        assert counts["thread_vec"] == 3
        assert counts["chunk_vec"] == 2
        assert counts["thread_fts"] >= 1
        assert counts["results"] >= 1
        assert all(isinstance(v, int) for v in counts.values())

    def test_keyword_mode_has_no_embedding_or_vector_lanes(
        self, caplog, fake_server, fake_embed, chunked_db
    ):
        caplog.set_level(logging.INFO)
        register_search_tools(fake_server, chunked_db, fake_embed)
        asyncio.run(fake_server.tools["search_emails"](query="invoice", mode="keyword"))
        stages = set(_one_line(caplog)["stages"])
        assert stages == _KEYWORD_LANES | {"fusion"}

    def test_semantic_mode_has_no_keyword_lanes(self, caplog, fake_server, fake_embed, chunked_db):
        caplog.set_level(logging.INFO)
        register_search_tools(fake_server, chunked_db, fake_embed)
        asyncio.run(fake_server.tools["search_emails"](query="invoice", mode="semantic"))
        stages = set(_one_line(caplog)["stages"])
        assert stages == {"query_embedding", "fusion"} | _VECTOR_LANES

    def test_reranker_adds_evidence_and_rerank_stages(
        self, caplog, fake_server, fake_embed, chunked_db
    ):
        caplog.set_level(logging.INFO)
        register_search_tools(fake_server, chunked_db, fake_embed, reranker=_Reranker())
        asyncio.run(fake_server.tools["search_emails"](query="invoice", mode="hybrid"))
        line = _one_line(caplog)
        assert {"evidence_fetch", "rerank"} <= set(line["stages"])
        assert line["config"] == {"rerank": "cohere"}
        assert line["counts"]["evidence_chunks"] >= 1

    def test_from_name_records_contact_lookup(self, caplog, fake_server, fake_embed, seeded_db):
        caplog.set_level(logging.INFO)
        register_search_tools(fake_server, seeded_db, fake_embed)
        asyncio.run(
            fake_server.tools["search_emails"](query="invoice", mode="keyword", from_name="alice")
        )
        assert "contact_lookup" in _one_line(caplog)["stages"]

    def test_embed_failure_logs_error_outcome_with_the_stage_reached(
        self, caplog, fake_server, chunked_db
    ):
        caplog.set_level(logging.INFO)

        class _BrokenEmbed(FakeEmbedClient):
            async def embed(self, text: str) -> list[float]:
                raise ConnectionError("embedder down")

        register_search_tools(fake_server, chunked_db, _BrokenEmbed())
        with pytest.raises(ToolError):
            asyncio.run(fake_server.tools["search_emails"](query="invoice"))
        line = _one_line(caplog)
        assert line["outcome"] == "error"
        assert set(line["stages"]) == {"query_embedding"}


class TestVectorLaneExpansion:
    """#286 widens a filtered search's vector windows step by step. Each
    lane's stage covers all of its steps and ``*_expansions`` counts the
    re-queries."""

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_expansion_steps_are_counted_per_lane(self, tmp_path, monkeypatch, caplog, mode):
        caplog.set_level(logging.INFO)
        db = _scoped_recall_db(tmp_path)
        ks = _spy_vector_k(db, monkeypatch)

        @timed_tool("probe")
        async def probe():
            _search(db, mode, folders=["Nowhere"])

        asyncio.run(probe())
        line = _one_line(caplog)
        assert line["counts"]["thread_vec_expansions"] == len(ks["thread"]) - 1 > 0
        assert line["counts"]["chunk_vec_expansions"] == len(ks["chunk"]) - 1 > 0
        assert _VECTOR_LANES <= set(line["stages"])

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_unfiltered_search_reports_no_expansion(self, tmp_path, caplog, mode):
        caplog.set_level(logging.INFO)
        db = _scoped_recall_db(tmp_path)

        @timed_tool("probe")
        async def probe():
            _search(db, mode)

        asyncio.run(probe())
        counts = _one_line(caplog)["counts"]
        assert "thread_vec_expansions" not in counts
        assert "chunk_vec_expansions" not in counts

    @pytest.mark.parametrize("mode", ["semantic", "hybrid"])
    def test_default_trash_exclusion_counts_its_expansions(
        self, tmp_path, monkeypatch, caplog, mode
    ):
        """#441: with the nearest records all in Trash, the default
        exclusion widens the windows, and the steps are counted like
        any filter's."""
        caplog.set_level(logging.INFO)
        db = _scoped_recall_db(tmp_path, noise_folder="Trash")
        ks = _spy_vector_k(db, monkeypatch)

        @timed_tool("probe")
        async def probe():
            _search(db, mode)

        asyncio.run(probe())
        line = _one_line(caplog)
        assert line["counts"]["thread_vec_expansions"] == len(ks["thread"]) - 1 > 0
        assert line["counts"]["chunk_vec_expansions"] == len(ks["chunk"]) - 1 > 0
        assert line["counts"]["filtered"] == 1
        assert _VECTOR_LANES <= set(line["stages"])


class TestOtherSearchTools:
    def test_get_evidence_mailbox_wide(self, caplog, fake_server, fake_embed, chunked_db):
        caplog.set_level(logging.INFO)
        register_search_tools(fake_server, chunked_db, fake_embed)
        asyncio.run(fake_server.tools["get_evidence"](query="invoice"))
        line = _one_line(caplog)
        assert line["tool"] == "get_evidence"
        assert "evidence_fetch" in line["stages"]
        assert "rerank" not in line["stages"]
        assert line["counts"]["evidence_chunks"] >= 1

    def test_get_evidence_thread_scoped(self, caplog, fake_server, fake_embed, chunked_db):
        caplog.set_level(logging.INFO)
        register_search_tools(fake_server, chunked_db, fake_embed)
        asyncio.run(fake_server.tools["get_evidence"](query="invoice", thread_id="t-alpha"))
        line = _one_line(caplog)
        # The thread-scoped path bypasses the lanes and fusion.
        assert set(line["stages"]) == {"query_embedding", "evidence_fetch"}
        assert line["counts"]["evidence_chunks"] == 1

    def test_search_attachments(self, caplog, fake_server, fake_embed, attachments_db):
        caplog.set_level(logging.INFO)
        register_search_tools(fake_server, attachments_db, fake_embed)
        asyncio.run(fake_server.tools["search_attachments"](query="invoice"))
        line = _one_line(caplog)
        assert line["tool"] == "search_attachments"
        assert set(line["stages"]) == {"attachment_search"}
        assert "results" in line["counts"]


class TestIntelligenceTimings:
    def test_ask_mailbox_records_retrieval_and_inference(
        self, caplog, fake_server, fake_embed, chunked_db
    ):
        caplog.set_level(logging.INFO)
        register_intelligence_tools(
            fake_server,
            chunked_db,
            fake_embed,
            # Cites a label so #284's citation check passes without a repair call.
            FakeInferenceClient(response="mock answer [E1]", mode="openai"),
        )
        asyncio.run(fake_server.tools["ask_mailbox"](question="invoice"))
        line = _one_line(caplog)
        assert line["tool"] == "ask_mailbox"
        assert line["outcome"] == "ok"
        assert {"query_embedding", "fusion", "evidence_fetch", "inference"} <= set(line["stages"])
        assert "rerank" not in line["stages"]
        assert line["config"] == {"rerank": "none", "inference": "openai"}
        assert line["counts"]["inference_calls"] == 1

    def test_extract_counts_one_inference_call_per_thread(
        self, caplog, fake_server, fake_embed, chunked_db
    ):
        caplog.set_level(logging.INFO)
        register_intelligence_tools(
            fake_server, chunked_db, fake_embed, FakeInferenceClient(response="null")
        )
        asyncio.run(
            fake_server.tools["extract_from_emails"](query="invoice", schema={"n": "string"})
        )
        line = _one_line(caplog)
        assert line["counts"]["inference_calls"] == line["counts"]["results"]
        assert "inference" in line["stages"]

    def test_summarize_thread_records_inference(self, caplog, fake_server, fake_embed, chunked_db):
        caplog.set_level(logging.INFO)
        register_intelligence_tools(fake_server, chunked_db, fake_embed, FakeInferenceClient())
        asyncio.run(fake_server.tools["summarize_thread"](thread_id="t-alpha"))
        line = _one_line(caplog)
        assert line["tool"] == "summarize_thread"
        assert "inference" in line["stages"]
        # A direct ID hit runs no retrieval lanes.
        assert not (_VECTOR_LANES | _KEYWORD_LANES) & set(line["stages"])


class TestNoContentInLogs:
    """The marker sits in the query and in every mail field of the only
    indexed thread, and the fake provider echoes it back. No log record,
    timing line or otherwise, may carry it."""

    def test_search_and_intelligence_tools(self, caplog, fake_server, marker_db):
        caplog.set_level(logging.DEBUG)
        embed = FakeEmbedClient()
        inference = FakeInferenceClient(response=f"the answer is {MARKER} [E1]")
        register_search_tools(fake_server, marker_db, embed, reranker=_Reranker())
        register_intelligence_tools(fake_server, marker_db, embed, inference, reranker=_Reranker())
        tools = fake_server.tools

        async def run_all():
            for mode in ("hybrid", "semantic", "keyword"):
                await tools["search_emails"](query=f"{MARKER} figures", mode=mode)
            await tools["get_evidence"](query=f"{MARKER} figures")
            await tools["search_attachments"](query=MARKER)
            out = await tools["ask_mailbox"](question=f"what are the {MARKER} figures?")
            assert MARKER in out.content[0].text  # the marker did flow through the call
            await tools["extract_from_emails"](query=MARKER, schema={"n": "string"})
            await tools["summarize_thread"](thread_id="t-marker")

        asyncio.run(run_all())
        assert len(_timing_lines(caplog)) == 8
        assert MARKER not in caplog.text
