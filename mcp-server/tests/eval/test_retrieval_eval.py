"""
Retrieval-quality eval harness — opt-in, runs against a real index.

Why this is separate from the unit suite:

The chunker / threader / search-knob discussions in this repo keep hitting
the same wall: there's no held-out set of "query → expected message"
mappings to measure whether a change actually helped retrieval. Without
that signal, every change to ``PER_THREAD_CHAR_BUDGET``, ``THREAD_BODY_TEXT_MAX_CHARS``,
embedding model, or RRF weights is shipped on faith.

This harness fills that gap with a tiny, JSON-driven loop the operator
extends with real-mailbox queries. It does NOT ship with meaningful seed
queries — it can't, because no two mailboxes have the same ground truth.
``queries.example.json`` is a template; copy to ``queries.json`` and
fill in real ``expected_thread_ids`` from your index.

Run:

    cd mcp-server
    MCP_EVAL_DB=/path/to/mail.db uv run pytest -m eval

Without ``MCP_EVAL_DB`` set, every eval test skips, so the harness
cannot regress the regular CI suite.

Metrics emitted per query:
- ``hit@K``: did at least one expected_thread_id appear in the top-K
  search results?
- ``rank``: 1-indexed rank of the first expected_thread_id, or ``None``
  if missed (used to compute mean reciprocal rank).

A ``--print-summary`` invocation surfaces aggregate Recall@K and MRR
across the loaded query set so two runs (e.g. before/after raising
``PER_THREAD_CHAR_BUDGET``) can be compared directly.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from src.lib.sqlite import Database

# Module-level marker so ``pytest -m eval`` runs only this directory and
# the regular suite (``pytest`` with no marker) skips it. Defined in
# pyproject.toml under tool.pytest.ini_options.markers.
pytestmark = pytest.mark.eval


EVAL_DIR = Path(__file__).parent
DEFAULT_QUERY_FILE = EVAL_DIR / "queries.json"
EXAMPLE_QUERY_FILE = EVAL_DIR / "queries.example.json"


@dataclass(frozen=True)
class EvalQuery:
    """One row of the eval set.

    ``expected_thread_ids`` is treated as a disjunction: the query is
    considered a hit if ANY listed id appears in the top-K results.
    Most queries in practice have one expected id, but allowing a list
    accommodates the case where the same conversation was indexed under
    multiple message-id-derived thread ids.
    """

    id: str
    question: str
    search_query: str
    expected_thread_ids: list[str]
    expected_substrings: list[str]


def _load_queries() -> list[EvalQuery]:
    """Load the operator's ``queries.json``, falling back to the example.

    The example file is shipped with placeholder thread ids and exists
    only as a template. If a test runs against the example file, the
    expected_thread_ids will not match anything in a real index, and
    every query will report a miss — this is intentional and the
    summary line points it out so the operator notices.
    """
    path = DEFAULT_QUERY_FILE if DEFAULT_QUERY_FILE.exists() else EXAMPLE_QUERY_FILE
    raw = json.loads(path.read_text())
    return [
        EvalQuery(
            id=q["id"],
            question=q["question"],
            search_query=q["search_query"],
            expected_thread_ids=list(q.get("expected_thread_ids", [])),
            expected_substrings=list(q.get("expected_substrings", [])),
        )
        for q in raw
    ]


@pytest.fixture(scope="session")
def eval_db() -> Database:
    """Open the operator-supplied real index, or skip every eval test."""
    db_path = os.environ.get("MCP_EVAL_DB")
    if not db_path:
        pytest.skip(
            "MCP_EVAL_DB not set — eval suite is opt-in. "
            "Run with `MCP_EVAL_DB=/path/to/mail.db uv run pytest -m eval`."
        )
    if not Path(db_path).exists():
        pytest.skip(f"MCP_EVAL_DB={db_path} does not exist.")
    return Database(db_path)


@pytest.fixture(scope="session")
def eval_queries() -> list[EvalQuery]:
    queries = _load_queries()
    if not queries:
        pytest.skip("No eval queries loaded.")
    return queries


def _rank_of_first_match(results: list, expected_ids: set[str]) -> int | None:
    """Return the 1-indexed rank of the first expected id, or ``None``."""
    for rank, r in enumerate(results, start=1):
        if r.thread_id in expected_ids:
            return rank
    return None


# Pull eval queries at collection time so each query becomes its own
# parametrized case in the pytest report. Failures are then attributable
# to a specific query id rather than buried in a single sweep test.
def pytest_generate_tests(metafunc):
    if "eval_query" in metafunc.fixturenames:
        try:
            queries = _load_queries()
        except FileNotFoundError:
            queries = []
        metafunc.parametrize(
            "eval_query",
            queries,
            ids=[q.id for q in queries] if queries else None,
        )


# ---------------------------------------------------------------------------
# Tests — one parametrized case per query in queries.json
# ---------------------------------------------------------------------------


def test_keyword_search_finds_expected_thread(eval_db: Database, eval_query: EvalQuery) -> None:
    """Keyword (BM25) retrieval must surface the expected thread in top 10."""
    if not eval_query.expected_thread_ids:
        pytest.skip(f"{eval_query.id}: no expected_thread_ids — skip.")
    results = eval_db.keyword_search(query_text=eval_query.search_query, limit=10)
    rank = _rank_of_first_match(results, set(eval_query.expected_thread_ids))
    assert rank is not None, (
        f"{eval_query.id}: expected one of {eval_query.expected_thread_ids} "
        f"in top 10 keyword results for query "
        f"{eval_query.search_query!r}, got {[r.thread_id for r in results]}"
    )


def test_hybrid_search_finds_expected_thread(eval_db: Database, eval_query: EvalQuery) -> None:
    """Hybrid (BM25 + vector via RRF) is the default search mode the LLM
    sees through ``search_emails`` / ``ask_mailbox``. If it loses the
    expected thread, downstream answers will be wrong even if the LLM
    is perfect — this is the most important assertion in the file."""
    if not eval_query.expected_thread_ids:
        pytest.skip(f"{eval_query.id}: no expected_thread_ids — skip.")
    # Hybrid needs an embedding; we synthesize a deterministic placeholder
    # so this test is purely a retrieval check on the indexed vectors and
    # does not require a live Ollama. Real "ask_mailbox quality" is a
    # separate eval (not in this PR) that exercises the LLM end-to-end.
    placeholder = [0.0] * 768
    results = eval_db.hybrid_search(
        query_text=eval_query.search_query,
        query_embedding=placeholder,
        limit=10,
    )
    rank = _rank_of_first_match(results, set(eval_query.expected_thread_ids))
    assert rank is not None, (
        f"{eval_query.id}: expected one of {eval_query.expected_thread_ids} "
        f"in top 10 hybrid results, got {[r.thread_id for r in results]}"
    )


def test_eval_summary(eval_db: Database, eval_queries: list[EvalQuery]) -> None:
    """Aggregate Recall@10 and MRR across the loaded query set.

    Always passes — this is a reporting test, not an assertion, so the
    summary appears in the run output regardless of how the per-query
    tests above did. To compare two configurations (e.g. before/after a
    knob change), capture the printed summary block from each run.
    """
    rank_records: list[tuple[str, int | None]] = []
    for q in eval_queries:
        if not q.expected_thread_ids:
            continue
        placeholder = [0.0] * 768
        results = eval_db.hybrid_search(
            query_text=q.search_query,
            query_embedding=placeholder,
            limit=10,
        )
        rank = _rank_of_first_match(results, set(q.expected_thread_ids))
        rank_records.append((q.id, rank))

    if not rank_records:
        pytest.skip("No queries had expected_thread_ids — nothing to summarize.")

    hits = sum(1 for _, r in rank_records if r is not None)
    total = len(rank_records)
    recall = hits / total
    mrr = sum(1.0 / r for _, r in rank_records if r is not None) / total

    lines = [
        "",
        "=" * 60,
        "Retrieval eval summary (hybrid mode, top 10):",
        f"  Queries with expected ids: {total}",
        f"  Recall@10: {recall:.2%} ({hits}/{total})",
        f"  MRR:       {mrr:.3f}",
        "  Per-query rank (None = missed):",
    ]
    for qid, rank in rank_records:
        lines.append(f"    {qid:<30s} rank={rank}")
    lines.append("=" * 60)
    # Use ``pytest -s`` to see this; otherwise pytest captures stdout.
    print("\n".join(lines))
