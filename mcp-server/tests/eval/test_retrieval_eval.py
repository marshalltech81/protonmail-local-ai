"""
Retrieval-quality eval harness — opt-in, runs against a real index.

Why this is separate from the unit suite:

The chunker / threader / search-knob discussions in this repo keep hitting
the same wall: there's no held-out set of "query → expected message"
mappings to measure whether a change actually helped retrieval. Without
that signal, every change to chunking, ``THREAD_BODY_TEXT_MAX_TOKENS``,
the embedding model, or the RRF fusion in ``hybrid_search`` is shipped
on faith. The harness measures retrieval only (the thread ids and ranks
``keyword_search`` / ``hybrid_search`` return); prompt-side settings such
as ``PER_THREAD_CHAR_BUDGET`` never reach it.

This harness fills that gap with a tiny, JSON-driven loop the operator
extends with real-mailbox queries. It does NOT ship with meaningful seed
queries — it can't, because no two mailboxes have the same ground truth.
``queries.example.json`` is a template; copy to ``queries.json`` and
fill in real thread ids from your index, as ``expected_thread_ids``
(alternatives) or ``required_evidence`` (groups that are all needed).

Run:

    cd mcp-server
    MCP_EVAL_DB=/path/to/mail.db uv run pytest -o addopts= -m eval tests/eval -s

``-o addopts=`` drops the default options, which exclude this directory
(``--ignore=tests/eval``) and enforce the coverage floor; with them, a
plain ``pytest -m eval`` selects no tests.

Without ``MCP_EVAL_DB`` set, every eval test skips, so the harness
cannot regress the regular CI suite.

Metrics emitted per query (definitions in ``tests/retrieval_metrics.py``):
- ``rank``: 1-indexed rank of the first expected thread from any
  evidence group, or ``None`` if missed. Hit@K and mean reciprocal
  rank come from it.
- ``groups``: how many of the query's required evidence groups have a
  thread in the top K. Evidence recall@K averages that fraction; it
  differs from Hit@K only for questions that need several sources.

``test_eval_summary`` prints aggregate Hit@K, MRR and evidence recall@K
across the loaded query set (``-s`` keeps pytest from capturing it) so
two runs (e.g. before/after a change to RRF fusion) can be compared
directly.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from src.lib.embed import EmbedClient, embed_query
from src.lib.sqlite import Database
from src.main import _resolve_base_url

from tests.retrieval_metrics import first_hit_rank, group_ranks

# Module-level marker so ``-m eval`` selects only this directory; the
# regular suite never collects it (``addopts`` ignores ``tests/eval``). Defined in
# pyproject.toml under tool.pytest.ini_options.markers.
pytestmark = pytest.mark.eval


EVAL_DIR = Path(__file__).parent
DEFAULT_QUERY_FILE = EVAL_DIR / "queries.json"
EXAMPLE_QUERY_FILE = EVAL_DIR / "queries.example.json"


@dataclass(frozen=True)
class EvalQuery:
    """One row of the eval set.

    ``evidence_groups`` lists the evidence the query needs: every group
    is required, and any one thread id inside a group satisfies it. The
    JSON row gives either ``required_evidence`` (the groups themselves)
    or the older ``expected_thread_ids``, a list of alternatives that
    becomes one group (for example the same conversation indexed under
    several message-id-derived thread ids).
    """

    id: str
    search_query: str
    evidence_groups: list[list[str]]


def _using_example_queries() -> bool:
    """True when no operator ``queries.json`` exists and the example is used."""
    return not DEFAULT_QUERY_FILE.exists()


def _load_queries() -> list[EvalQuery]:
    """Load the operator's ``queries.json``, falling back to the example.

    The example file is shipped with placeholder thread ids and exists
    only as a template. If a test runs against the example file, the
    expected_thread_ids will not match anything in a real index, and
    every query will report a miss — this is intentional and
    ``test_eval_summary`` prints a notice so the operator notices.

    Keys other than ``id``, ``search_query``, ``expected_thread_ids``
    and ``required_evidence`` (``notes``, or the retired ``question`` /
    ``expected_substrings``) are ignored.
    """
    path = EXAMPLE_QUERY_FILE if _using_example_queries() else DEFAULT_QUERY_FILE
    raw = json.loads(path.read_text())
    return [
        EvalQuery(id=q["id"], search_query=q["search_query"], evidence_groups=_evidence_groups(q))
        for q in raw
    ]


def _evidence_groups(row: dict) -> list[list[str]]:
    """Read a row's evidence as groups (see ``EvalQuery``)."""
    if "required_evidence" in row:
        if "expected_thread_ids" in row:
            raise ValueError(
                f"{row['id']}: give required_evidence or expected_thread_ids, not both"
            )
        raw_groups = row["required_evidence"]
        # Each group must itself be a list of thread IDs: a flat list of
        # IDs would otherwise split each ID into characters and pass.
        if not isinstance(raw_groups, list) or not all(
            isinstance(group, list) and all(isinstance(t, str) and t for t in group)
            for group in raw_groups
        ):
            raise ValueError(
                f"{row['id']}: required_evidence must be a list of lists of thread IDs"
            )
        if not raw_groups:
            raise ValueError(f"{row['id']}: required_evidence has no groups")
        if not all(raw_groups):
            raise ValueError(f"{row['id']}: required_evidence has an empty group")
        return [list(group) for group in raw_groups]
    expected = list(row.get("expected_thread_ids", []))
    return [expected] if expected else []


@pytest.fixture(scope="session")
def eval_db() -> Database:
    """Open the operator-supplied real index, or skip every eval test."""
    db_path = os.environ.get("MCP_EVAL_DB")
    if not db_path:
        pytest.skip(
            "MCP_EVAL_DB not set — eval suite is opt-in. "
            "Run with `MCP_EVAL_DB=/path/to/mail.db uv run pytest -o addopts= -m eval tests/eval -s`."
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


@pytest.fixture(scope="session")
def eval_embedder(eval_db: Database):
    """Return a callable that embeds a query string for the hybrid lane.

    Codex P2: the prior placeholder ``[0.0] * 768`` lied about hybrid
    coverage on two counts — the dim was wrong (schema is 4096) so
    ``_vector_search`` swallowed an OperationalError and returned an
    empty lane, and an all-zeros query against unit-normalised vectors
    is semantically meaningless anyway. Skip the hybrid path entirely
    unless the operator wired up a real embedder, so a green run
    actually means hybrid retrieval is working — not "keyword-only
    silently passing as hybrid."
    """
    model = os.environ.get("EMBED_MODEL")
    api_key = os.environ.get("EMBED_API_KEY")
    if not model or not api_key:
        pytest.skip(
            "Hybrid eval requires a real embedder. Set EMBED_MODEL, "
            "EMBED_API_KEY and EMBED_BASE_URL (a URL, or 'default' for "
            "OpenAI proper) to "
            "exercise the chunk-vec + thread-vec lanes; otherwise only "
            "the keyword-eval cases run."
        )
    # Same rule as the server: empty fails, ``default`` is the SDK default (#750).
    base_url = _resolve_base_url("EMBED_BASE_URL", os.environ.get("EMBED_BASE_URL", ""), "openai")
    client = EmbedClient(base_url=base_url, model=model, api_key=api_key)
    expected_dim = eval_db.get_embedding_dim()

    def _embed(text: str) -> list[float]:
        # Each call gets its own event loop so the eval suite stays sync
        # at the test layer; ``asyncio.run`` avoids the 3.14 deprecation
        # warning on ``get_event_loop()`` with no running loop.
        return asyncio.run(embed_query(client, text, expected_dim))

    return _embed


class HybridResults:
    """Each query's top-10 hybrid results, computed once per run (#840).

    The per-query hybrid test and ``test_eval_summary`` both need them;
    sharing them in memory halves the embed calls and searches. Nothing
    is written anywhere: a new run starts empty.
    """

    def __init__(self, db: Database, embed: Callable[[str], list[float]]) -> None:
        self._db = db
        self._embed = embed
        self._results: dict[tuple[str, str], list] = {}

    def __call__(self, query: EvalQuery) -> list:
        key = (query.id, query.search_query)
        if key not in self._results:
            self._results[key] = self._db.hybrid_search(
                query_text=query.search_query,
                query_embedding=self._embed(query.search_query),
                limit=10,
            )
        return self._results[key]


@pytest.fixture(scope="session")
def hybrid_results(eval_db: Database, eval_embedder) -> HybridResults:
    return HybridResults(eval_db, eval_embedder)


def _missing_groups(results: list, groups: list[list[str]]) -> list[list[str]]:
    """Evidence groups with no thread anywhere in ``results``."""
    ranked = [r.thread_id for r in results]
    return [g for g, rank in zip(groups, group_ranks(ranked, groups), strict=True) if rank is None]


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
    """Keyword (BM25) retrieval must surface every required evidence
    group in the top 10."""
    if not eval_query.evidence_groups:
        pytest.skip(f"{eval_query.id}: no expected evidence — skip.")
    results = eval_db.keyword_search(query_text=eval_query.search_query, limit=10)
    missing = _missing_groups(results, eval_query.evidence_groups)
    assert not missing, (
        f"{eval_query.id}: no thread from groups {missing} "
        f"in top 10 keyword results for query "
        f"{eval_query.search_query!r}, got {[r.thread_id for r in results]}"
    )


def test_hybrid_search_finds_expected_thread(
    hybrid_results: HybridResults, eval_query: EvalQuery
) -> None:
    """Hybrid (BM25 + vector via RRF) is the default search mode the LLM
    sees through ``search_emails`` / ``ask_mailbox``. If it loses the
    expected thread, downstream answers will be wrong even if the LLM
    is perfect — this is the most important assertion in the file. Every
    required evidence group must be in the top 10."""
    if not eval_query.evidence_groups:
        pytest.skip(f"{eval_query.id}: no expected evidence — skip.")
    results = hybrid_results(eval_query)
    missing = _missing_groups(results, eval_query.evidence_groups)
    assert not missing, (
        f"{eval_query.id}: no thread from groups {missing} "
        f"in top 10 hybrid results, got {[r.thread_id for r in results]}"
    )


def test_eval_summary(hybrid_results: HybridResults, eval_queries: list[EvalQuery]) -> None:
    """Aggregate Hit@10, MRR and evidence recall@10 across the loaded
    query set.

    Always passes — this is a reporting test, not an assertion, so the
    summary appears in the run output regardless of how the per-query
    tests above did. It reuses the results those tests fetched
    (``HybridResults``), searching only queries they did not run. To
    compare two configurations (e.g. before/after a knob change),
    capture the printed summary block from each run.
    """
    # (query id, first-hit rank, groups found in top 10, groups required)
    records: list[tuple[str, int | None, int, int]] = []
    for q in eval_queries:
        if not q.evidence_groups:
            continue
        ranked = [r.thread_id for r in hybrid_results(q)]
        found = sum(1 for r in group_ranks(ranked, q.evidence_groups) if r is not None)
        records.append(
            (q.id, first_hit_rank(ranked, q.evidence_groups), found, len(q.evidence_groups))
        )

    if not records:
        pytest.skip("No queries had expected evidence — nothing to summarize.")

    total = len(records)
    hits = sum(1 for _, r, _, _ in records if r is not None)
    mrr = sum(1.0 / r for _, r, _, _ in records if r is not None) / total
    recall = sum(found / groups for _, _, found, groups in records) / total
    multi = sum(1 for *_, groups in records if groups > 1)

    lines = [
        "",
        "=" * 60,
        "Retrieval eval summary (hybrid mode, top 10):",
        f"  Queries with expected evidence: {total} ({multi} need several sources)",
        f"  Hit@10:          {hits / total:.2%} ({hits}/{total})",
        f"  MRR:             {mrr:.3f}",
        f"  Evidence recall@10: {recall:.2%}",
        "  Per-query first-hit rank (None = missed), required groups found:",
    ]
    for qid, rank, found, groups in records:
        lines.append(f"    {qid:<30s} rank={rank} groups={found}/{groups}")
    if _using_example_queries():
        lines.append(
            f"  NOTE: no {DEFAULT_QUERY_FILE.name} found; ran the placeholder "
            f"queries in {EXAMPLE_QUERY_FILE.name}, so every miss above is expected."
        )
    lines.append("=" * 60)
    # Use ``pytest -s`` to see this; otherwise pytest captures stdout.
    print("\n".join(lines))
