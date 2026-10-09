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
import src.lib.sqlite as sqlite_module
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from src.lib.sqlite import Database
from src.lib.timings import count, rerank_mode, stage, timed_tool
from src.tools.intelligence import register_intelligence_tools
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools
from src.tools.system import register_system_tools

from tests.conftest import (
    RECENT_REAP_AT,
    FakeEmbedClient,
    FakeInferenceClient,
    _build_schema,
    _insert_attachment,
    _insert_chunk,
    _insert_extraction,
    _insert_message,
    _insert_thread,
    insert_reaped,
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
                base_url="https://api.cohere.com",
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


def _fail_sql(db: Database, monkeypatch, *fragments: str) -> None:
    """Make every ``_fetchall`` whose SQL holds one of ``fragments``
    raise ``OperationalError``, as a missing or corrupt table would; the
    attachment lanes' ``_lane_rows`` (a caller's snapshot, #1204) too."""
    real = db._fetchall
    real_lane_rows = db._lane_rows

    def fetchall(sql, params=()):
        if any(fragment in sql for fragment in fragments):
            raise sqlite3.OperationalError("no such table")
        return real(sql, params)

    def lane_rows(sql, params, conn):
        if any(fragment in sql for fragment in fragments):
            raise sqlite3.OperationalError("no such table")
        return real_lane_rows(sql, params, conn)

    monkeypatch.setattr(db, "_fetchall", fetchall)
    monkeypatch.setattr(db, "_lane_rows", lane_rows)


def _degraded(line: dict) -> dict:
    return {k: v for k, v in line["counts"].items() if k.startswith("degraded_")}


class TestDegradedRetrieval:
    """#877: a lane that fails and falls back marks the call's own timing
    line with ``degraded_<lane>``, so the quality drop can be joined to
    the tool call it affected. Counts and lane names only."""

    @pytest.mark.parametrize(
        ("fragments", "mode", "expected"),
        [
            (["FROM threads_vec v"], "hybrid", {"degraded_thread_vec": 1}),
            (["FROM message_chunks_vec v"], "hybrid", {"degraded_chunk_vec": 1}),
            (["FROM threads_fts"], "keyword", {"degraded_thread_fts": 1}),
            (
                ["FROM threads_fts", "ORDER BY date_last DESC LIMIT ?"],
                "keyword",
                {"degraded_thread_fts": 1, "degraded_like_fallback": 1},
            ),
            (
                ["JOIN message_chunks c ON message_chunks_fts.rowid"],
                "keyword",
                {"degraded_chunk_fts": 1},
            ),
            (["FROM attachments_fts"], "keyword", {"degraded_attachment_fts": 1}),
        ],
    )
    def test_search_emails_lane_failure_marks_the_timing_line(
        self, caplog, monkeypatch, fake_server, fake_embed, chunked_db, fragments, mode, expected
    ):
        caplog.set_level(logging.INFO)
        _fail_sql(chunked_db, monkeypatch, *fragments)
        register_search_tools(fake_server, chunked_db, fake_embed)
        asyncio.run(fake_server.tools["search_emails"](query=f"invoice {MARKER}", mode=mode))
        line = _one_line(caplog)
        assert line["outcome"] == "ok"
        assert _degraded(line) == expected
        assert MARKER not in caplog.text

    def test_attachment_filename_lane_failure_marks_the_timing_line(
        self, caplog, monkeypatch, fake_server, fake_embed, attachments_db
    ):
        caplog.set_level(logging.INFO)
        _fail_sql(attachments_db, monkeypatch, "bm25(attachments_fts) AS score")
        register_search_tools(fake_server, attachments_db, fake_embed)
        asyncio.run(fake_server.tools["search_attachments"](query=f"invoice {MARKER}"))
        line = _one_line(caplog)
        assert line["outcome"] == "ok"
        assert _degraded(line) == {"degraded_attachment_filename": 1}
        assert MARKER not in caplog.text

    def test_attachment_text_lane_failure_marks_the_timing_line(
        self, caplog, monkeypatch, fake_server, fake_embed, attachments_db
    ):
        caplog.set_level(logging.INFO)
        _fail_sql(attachments_db, monkeypatch, "WITH hits AS MATERIALIZED")
        register_search_tools(fake_server, attachments_db, fake_embed)
        asyncio.run(fake_server.tools["search_attachments"](query=f"invoice {MARKER}"))
        assert _degraded(_one_line(caplog)) == {"degraded_attachment_text": 1}
        assert MARKER not in caplog.text

    def test_attachment_scan_failure_marks_the_timing_line(
        self, caplog, monkeypatch, fake_server, fake_embed, attachments_db
    ):
        caplog.set_level(logging.INFO)
        _fail_sql(attachments_db, monkeypatch, "0.0 AS score FROM attachments a")
        register_search_tools(fake_server, attachments_db, fake_embed)
        asyncio.run(fake_server.tools["search_attachments"](content_type="application/pdf"))
        assert _degraded(_one_line(caplog)) == {"degraded_attachment_scan": 1}

    @pytest.mark.parametrize(
        ("fragment", "expected"),
        [
            ("vec_distance_l2(v.embedding, ?)", {"degraded_evidence_chunks": 1}),
            ("SELECT a.thread_id, a.attachment_id, bm25", {"degraded_attachment_match": 1}),
        ],
    )
    def test_get_evidence_lane_failure_marks_the_timing_line(
        self, caplog, monkeypatch, fake_server, fake_embed, chunked_db, fragment, expected
    ):
        caplog.set_level(logging.INFO)
        _fail_sql(chunked_db, monkeypatch, fragment)
        register_search_tools(fake_server, chunked_db, fake_embed)
        asyncio.run(fake_server.tools["get_evidence"](query=f"invoice {MARKER}"))
        assert _degraded(_one_line(caplog)) == expected
        assert MARKER not in caplog.text

    def test_keyword_ranking_failure_marks_the_timing_line(
        self, caplog, monkeypatch, fake_server, fake_embed, chunked_db
    ):
        """The keyword ranking runs on its own scratch connection (#1246)."""
        caplog.set_level(logging.INFO)

        def scratch():
            raise sqlite3.OperationalError(f"no such module: fts5 {MARKER}")

        monkeypatch.setattr(sqlite_module, "_scratch_connection", scratch)
        register_search_tools(fake_server, chunked_db, fake_embed)
        asyncio.run(fake_server.tools["get_evidence"](query=f"invoice {MARKER}"))
        assert _degraded(_one_line(caplog)) == {"degraded_keyword_chunks": 1}
        assert MARKER not in caplog.text

    def test_recent_chunks_failure_marks_the_timing_line(
        self, caplog, monkeypatch, fake_server, fake_embed, chunked_db
    ):
        caplog.set_level(logging.INFO)
        _fail_sql(chunked_db, monkeypatch, "ORDER BY m.effective_at DESC, c.chunk_index")
        register_intelligence_tools(fake_server, chunked_db, fake_embed, FakeInferenceClient())
        asyncio.run(fake_server.tools["summarize_thread"](thread_id="t-alpha"))
        assert _degraded(_one_line(caplog)) == {"degraded_recent_chunks": 1}

    @pytest.mark.parametrize(
        "ranking",
        [
            [],  # the reranker failed
            [(0, 1.0), (0, 0.5)],  # a repeated index
            [(99, 1.0)],  # an out-of-range index
        ],
    )
    def test_rerank_fallback_marks_the_timing_line(
        self, caplog, fake_server, fake_embed, chunked_db, ranking
    ):
        caplog.set_level(logging.INFO)

        class _BadReranker(_Reranker):
            def rerank(self, query, documents, top_n):
                return ranking

        register_search_tools(fake_server, chunked_db, fake_embed, reranker=_BadReranker())
        asyncio.run(fake_server.tools["search_emails"](query=f"invoice {MARKER}"))
        line = _one_line(caplog)
        assert line["outcome"] == "ok"
        assert line["config"] == {"rerank": "cohere"}
        assert _degraded(line) == {"degraded_rerank": 1}
        assert MARKER not in caplog.text

    def test_rerank_subject_lookup_failure_marks_the_timing_line(
        self, caplog, monkeypatch, fake_server, fake_embed, chunked_db
    ):
        caplog.set_level(logging.INFO)
        monkeypatch.setattr("src.lib.sqlite._RERANK_SUBJECT_SQL", "SELECT * FROM no_such_table")
        register_search_tools(fake_server, chunked_db, fake_embed, reranker=_Reranker())
        asyncio.run(fake_server.tools["search_emails"](query="invoice"))
        assert _degraded(_one_line(caplog)) == {"degraded_rerank_subjects": 1}

    def test_healthy_calls_carry_no_degraded_marker(
        self, caplog, fake_server, fake_embed, chunked_db, attachments_db
    ):
        caplog.set_level(logging.INFO)
        register_search_tools(fake_server, chunked_db, fake_embed, reranker=_Reranker())
        register_intelligence_tools(fake_server, chunked_db, fake_embed, FakeInferenceClient())

        async def run_chunked():
            for mode in ("hybrid", "semantic", "keyword"):
                await fake_server.tools["search_emails"](query="invoice", mode=mode)
            await fake_server.tools["get_evidence"](query="invoice")
            await fake_server.tools["summarize_thread"](thread_id="t-alpha")

        asyncio.run(run_chunked())
        register_search_tools(fake_server, attachments_db, fake_embed)

        async def run_attachments():
            await fake_server.tools["search_attachments"](query="invoice")
            await fake_server.tools["search_attachments"](content_type="application/pdf")

        asyncio.run(run_attachments())
        lines = _timing_lines(caplog)
        assert len(lines) == 7
        assert all(_degraded(line) == {} for line in lines)


# The seven tools #886 gave a completion line, query_attachments and
# get_attachment (#796), and aggregate_messages (#823).
_COMPLETION_TOOLS = (
    "query_messages",
    "aggregate_messages",
    "query_attachments",
    "get_attachment",
    "get_message",
    "get_thread",
    "list_threads",
    "find_contact",
    "list_folders",
    "get_mailbox_status",
)

_MARKER_MESSAGE_ID = f"{MARKER}-mid@example.com"
_MARKER_THREAD_ID = f"t-{MARKER}"
_MARKER_FOLDER = f"{MARKER}-folder"
_MARKER_ADDRESS = f"{MARKER}@example.com"
_MARKER_OCCURRENCE_ID = f"{MARKER}-occurrence"


def _open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    return conn


def _insert_marker_message(conn: sqlite3.Connection, variant: str = "") -> None:
    _insert_message(
        conn,
        message_id=_MARKER_MESSAGE_ID,
        thread_id=_MARKER_THREAD_ID,
        sent_at="2024-01-10T09:00:00+00:00",
        subject=f"quarterly {MARKER} report",
        folder=_MARKER_FOLDER,
        from_=[f"{MARKER} Person <{_MARKER_ADDRESS}>"],
        to=["bob@example.com"],
        body=f"the {MARKER} figures are attached",
        variant=variant,
    )


@pytest.fixture
def marker_messages_db(tmp_path: Path):
    """One message whose Message-ID, thread ID, folder, sender, subject
    and body all carry ``MARKER``."""
    db_path = tmp_path / "marker-messages.db"
    conn = _open_db(db_path)
    _insert_marker_message(conn)
    # One attachment whose occurrence ID, filename and extracted text
    # carry the marker, for query_attachments and get_attachment.
    _insert_attachment(
        conn,
        message_id=_MARKER_MESSAGE_ID,
        thread_id=_MARKER_THREAD_ID,
        attachment_id=f"{MARKER}-payload",
        filename=f"{MARKER}.txt",
        occurrence_id=_MARKER_OCCURRENCE_ID,
        extractor_module="text",
    )
    _insert_extraction(
        conn,
        attachment_id=f"{MARKER}-payload",
        extracted_text=f"the {MARKER} attachment text",
        extractor_module="text",
    )
    conn.close()
    return Database(str(db_path))


def _register_completion_tools(server, db) -> None:
    register_retrieval_tools(server, db)
    register_system_tools(server, db)


# Each tool's success call: arguments carrying the marker where the tool
# takes any, and the counts its line must show.
_SUCCESS_CALLS: dict[str, tuple[dict, dict]] = {
    "query_messages": (
        {"sender": _MARKER_ADDRESS, "text": MARKER, "folder": _MARKER_FOLDER},
        {"total_matches": 1, "indeterminate": 0, "returned": 1},
    ),
    "aggregate_messages": (
        {
            "group_by": "sender_address",
            "sender": _MARKER_ADDRESS,
            "text": MARKER,
            "folder": _MARKER_FOLDER,
        },
        {"total_matches": 1, "indeterminate": 0, "groups": 1, "returned": 1},
    ),
    "query_attachments": (
        {"sender": _MARKER_ADDRESS, "filename": MARKER, "folder": _MARKER_FOLDER},
        {"total_matches": 1, "indeterminate": 0, "returned": 1},
    ),
    "get_attachment": ({"attachment_occurrence_id": _MARKER_OCCURRENCE_ID}, {"attachments": 1}),
    "get_message": ({"message_id": _MARKER_MESSAGE_ID}, {"messages": 1}),
    "get_thread": ({"thread_id": _MARKER_THREAD_ID}, {"messages": 1}),
    "list_threads": ({"folder": _MARKER_FOLDER}, {"threads": 1}),
    "find_contact": ({"query": MARKER}, {"contacts": 1}),
    "list_folders": ({}, {"folders": 1}),
    "get_mailbox_status": ({}, {}),
}

# The ``Database`` method each tool reads through, made to fail below.
_DB_METHODS = {
    "query_messages": "query_messages",
    "aggregate_messages": "aggregate_messages",
    "query_attachments": "query_attachments",
    "get_attachment": "get_attachment_text",
    "get_message": "get_message_view",
    "get_thread": "get_thread_page",
    "list_threads": "list_threads",
    "find_contact": "find_contact",
    "list_folders": "list_folders",
    "get_mailbox_status": "get_mailbox_status",
}

# Errors a tool raises to the caller without a database failure: the
# arguments, and the fixed text the tool logs as the cause.
_CALLER_ERRORS = [
    ("query_messages", {"text": MARKER, "date_from": f"{MARKER}-01"}, "date_from"),
    (
        "aggregate_messages",
        {"group_by": "folder", "text": MARKER, "date_from": f"{MARKER}-01"},
        "date_from",
    ),
    ("aggregate_messages", {"group_by": "folder", "cursor": MARKER}, "aggregate_messages.cursor"),
    ("query_attachments", {"filename": MARKER, "date_from": f"{MARKER}-01"}, "date_from"),
    ("query_attachments", {"cursor": MARKER}, "query_attachments.cursor"),
    (
        "get_attachment",
        {"attachment_occurrence_id": f"missing-{MARKER}"},
        "get_attachment failed: not_found",
    ),
    ("get_attachment", {"attachment_occurrence_id": _MARKER_OCCURRENCE_ID, "offset": -1}, "offset"),
    (
        "get_attachment",
        {"attachment_occurrence_id": _MARKER_OCCURRENCE_ID, "offset": 10**6},
        "offset",
    ),
    ("get_message", {"message_id": f"missing-{MARKER}"}, "get_message failed: not found"),
    ("get_message", {"message_id": _MARKER_MESSAGE_ID, "offset": -1}, "offset"),
    ("get_message", {"message_id": _MARKER_MESSAGE_ID, "offset": 10**6}, "offset"),
    ("get_thread", {"thread_id": f"missing-{MARKER}"}, "get_thread failed: not found"),
    ("list_threads", {"folder": _MARKER_FOLDER, "filter_type": MARKER}, "filter_type"),
    ("find_contact", {"query": "   "}, "rejected invalid argument: find_contact.query"),
]


def _logged_cause(caplog, cause: str) -> bool:
    return any(r.levelno >= logging.WARNING and cause in r.getMessage() for r in caplog.records)


class TestRetrievalAndStatusCompletionLines:
    """#886: the retrieval tools and ``get_mailbox_status`` log one
    completion line per call, on success and on error, with counts only.
    The marker sits in the arguments, the mail and the database error,
    and must reach no log record."""

    @pytest.mark.parametrize("tool", _COMPLETION_TOOLS)
    def test_success_logs_one_ok_line_with_counts(
        self, caplog, fake_server, marker_messages_db, tool
    ):
        caplog.set_level(logging.DEBUG)
        _register_completion_tools(fake_server, marker_messages_db)
        args, counts = _SUCCESS_CALLS[tool]
        out = asyncio.run(fake_server.tools[tool](**args))
        if tool != "get_mailbox_status":
            assert MARKER in out.content[0].text  # the marker did flow through the call
        line = _one_line(caplog)
        assert line["tool"] == tool
        assert line["outcome"] == "ok"
        assert line["counts"] == counts
        assert line["config"] == {}
        assert MARKER not in caplog.text

    @pytest.mark.parametrize("tool", _COMPLETION_TOOLS)
    def test_database_error_logs_one_error_line(
        self, caplog, monkeypatch, fake_server, marker_messages_db, tool
    ):
        caplog.set_level(logging.DEBUG)

        def fail(*_args, **_kwargs):
            raise sqlite3.OperationalError(f"no such table: {MARKER}")

        monkeypatch.setattr(marker_messages_db, _DB_METHODS[tool], fail)
        _register_completion_tools(fake_server, marker_messages_db)
        args, _ = _SUCCESS_CALLS[tool]
        with pytest.raises(ToolError):
            asyncio.run(fake_server.tools[tool](**args))
        line = _one_line(caplog)
        assert line["tool"] == tool
        assert line["outcome"] == "error"
        assert line["counts"] == {}
        assert _logged_cause(caplog, "OperationalError")
        assert MARKER not in caplog.text

    @pytest.mark.parametrize(("tool", "args", "cause"), _CALLER_ERRORS)
    def test_caller_error_logs_one_error_line_and_its_cause(
        self, caplog, fake_server, marker_messages_db, tool, args, cause
    ):
        caplog.set_level(logging.DEBUG)
        _register_completion_tools(fake_server, marker_messages_db)
        with pytest.raises(ToolError):
            asyncio.run(fake_server.tools[tool](**args))
        line = _one_line(caplog)
        assert line["tool"] == tool
        assert line["outcome"] == "error"
        assert _logged_cause(caplog, cause)
        assert MARKER not in caplog.text

    def test_reaped_and_ambiguous_ids_log_their_cause(self, caplog, fake_server, tmp_path):
        """The other ``ToolError`` paths of the two ID readers: a reaped
        thread or message, and a Message-ID two files claim."""
        caplog.set_level(logging.DEBUG)
        db_path = tmp_path / "reaped.db"
        conn = _open_db(db_path)
        _insert_marker_message(conn)
        _insert_marker_message(conn, variant="b")
        insert_reaped(
            conn,
            message_id=f"gone-{MARKER}",
            thread_id=f"t-gone-{MARKER}",
            reaped_at=RECENT_REAP_AT,
        )
        conn.close()
        _register_completion_tools(fake_server, Database(str(db_path)))
        tools = fake_server.tools
        calls = [
            (tools["get_message"], {"message_id": _MARKER_MESSAGE_ID}, "ambiguous Message-ID"),
            (tools["get_message"], {"message_id": f"gone-{MARKER}"}, "get_message failed: reaped"),
            (tools["get_thread"], {"thread_id": f"t-gone-{MARKER}"}, "get_thread failed: reaped"),
        ]
        for handler, args, cause in calls:
            caplog.clear()
            with pytest.raises(ToolError):
                asyncio.run(handler(**args))
            assert _one_line(caplog)["outcome"] == "error"
            assert _logged_cause(caplog, cause)
            assert MARKER not in caplog.text

    def test_decorator_leaves_every_tool_schema_unchanged(self, monkeypatch, empty_db):
        """FastMCP builds each tool's schemas and description from the
        handler: the listing with ``timed_tool`` must equal the listing
        with it replaced by a no-op."""

        def listing() -> dict[str, dict]:
            server = FastMCP("schema-test")
            _register_completion_tools(server, empty_db)

            async def run():
                async with Client(server) as client:
                    return await client.list_tools()

            return {
                t.name: {
                    "description": t.description,
                    "input": t.input_schema,
                    "output": t.output_schema,
                }
                for t in asyncio.run(run())
            }

        timed = listing()
        monkeypatch.setattr(
            "src.lib.timings.timed_tool", lambda *_a, **_k: lambda fn: fn, raising=False
        )
        untimed = listing()
        assert set(timed) == set(_COMPLETION_TOOLS)
        for tool in _COMPLETION_TOOLS:
            assert timed[tool] == untimed[tool], tool
        assert set(timed["query_messages"]["input"]["properties"]) >= {"sender", "cursor"}
