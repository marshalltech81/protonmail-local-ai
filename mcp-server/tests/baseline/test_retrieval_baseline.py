"""Retrieval regression baseline, step 2 of 2.

Step 1 (``indexer/tests/baseline/build.py``) indexes a synthetic
mailbox with the real indexer and a deterministic hashed embedder, and
writes ``mail.db`` plus the golden questions' query vectors into
``BASELINE_DIR``. This module runs the golden questions through the real
read path (``hybrid_search`` with no reranker, ``query_messages``) and
checks two layers:

1. Golden assertions — correctness. Each question lists its
   ``required_evidence`` as groups (every group required, any thread in
   a group satisfies it; see ``tests/retrieval_metrics.py``). The first
   hit from any group must rank within the question's ``max_rank``;
   evidence and vector-only questions add their own checks. MRR and
   evidence recall@10 (the fraction of groups found, which is where a
   multi-source question can fall short of a hit) must stay above the
   floors in ``golden.json``. Enumeration questions must return the
   exact message set. Unanswerable questions' terms must occur nowhere
   in the synthetic Maildir.
2. Rank snapshot — unchanged behaviour. The top-10 order of every
   search question must equal ``snapshot.json``. After an intended
   ranking change, regenerate it with ``--update-baseline`` and review
   the diff in the PR.

Skipped unless ``BASELINE_DIR`` is set; ``make baseline`` runs both steps.
"""

import asyncio
import email
import email.policy
import itertools
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from mcp.types import CallToolResult
from src.lib.sqlite import Database, ThreadResult
from src.tools.retrieval import register_retrieval_tools

from tests.conftest import FakeMCPServer
from tests.retrieval_metrics import evidence_recall, first_hit_rank, group_ranks

pytestmark = pytest.mark.baseline

_HERE = Path(__file__).parent
GOLDEN = json.loads((_HERE / "golden.json").read_text(encoding="utf-8"))
SNAPSHOT_PATH = _HERE / "snapshot.json"
AGENT_SCENARIOS_PATH = _HERE.parent / "eval" / "agent_scenarios.json"
SNAPSHOT_DEPTH = 10
_DOMAIN = "@baseline.example"


def _thread_ref(thread_id: str) -> str:
    """``t05.1@baseline.example`` -> ``t05``."""
    return thread_id.removesuffix(_DOMAIN).split(".")[0]


def _message_ref(message_id: str) -> str:
    """``t05.2@baseline.example`` -> ``t05.2``."""
    return message_id.removesuffix(_DOMAIN)


@pytest.fixture(scope="module")
def baseline_dir() -> Path:
    baseline_dir = os.environ.get("BASELINE_DIR")
    if not baseline_dir:
        pytest.skip("BASELINE_DIR not set; run `make baseline`")
    return Path(baseline_dir)


@pytest.fixture(scope="module")
def baseline_db(baseline_dir: Path) -> Database:
    return Database(str(baseline_dir / "mail.db"))


@pytest.fixture(scope="module")
def results(baseline_dir: Path, baseline_db: Database) -> dict[str, list[ThreadResult]]:
    """Run every golden search question once against the built baseline."""
    vectors = json.loads((baseline_dir / "query_vectors.json").read_text(encoding="utf-8"))
    out = {}
    for q in GOLDEN["search"]:
        hits = baseline_db.hybrid_search(
            q["query"],
            vectors[q["query"]],
            limit=SNAPSHOT_DEPTH,
            with_evidence="evidence" in q,
            **q.get("filters", {}),
        )
        out[q["id"]] = _order_ties(hits)
    return out


def _order_ties(hits: list[ThreadResult]) -> list[ThreadResult]:
    """Order each run of adjacent equal scores by thread ID, keeping the
    returned order otherwise.

    This keeps the snapshot from depending on how the lanes happened to
    insert tied threads. Only adjacent results are reordered: the keyword
    slot (#701) places a hit above higher-scored results on purpose, so
    a tie on either side of it must not move across it. The promoted hit
    is a run of its own even when its score equals its neighbours' (#748).
    """
    return [
        r
        for _, run in itertools.groupby(
            hits, key=lambda r: (r.score, "keyword_slot" in r.lane_ranks)
        )
        for r in sorted(run, key=lambda r: r.thread_id)
    ]


def test_order_ties_keeps_ties_on_their_side_of_the_slot() -> None:
    def hit(thread_id: str, score: float) -> ThreadResult:
        return ThreadResult(
            thread_id=thread_id,
            subject="s",
            participants=[],
            folder="INBOX",
            date_first=datetime(2024, 1, 1, tzinfo=UTC),
            date_last=datetime(2024, 1, 2, tzinfo=UTC),
            message_ids=[thread_id],
            snippet="",
            has_attachments=False,
            score=score,
        )

    hits = [hit("b", 0.5), hit("a", 0.5), hit("slot", 0.1), hit("c", 0.5), hit("d", 0.4)]
    assert [r.thread_id for r in _order_ties(hits)] == ["a", "b", "slot", "c", "d"]


def test_order_ties_keeps_a_tied_slot_hit_in_place() -> None:
    """#748: a promoted hit whose fused score equals its neighbours'
    must not join their run and be re-sorted below rank 3."""

    def hit(thread_id: str, score: float, slot: bool = False) -> ThreadResult:
        result = ThreadResult(
            thread_id=thread_id,
            subject="s",
            participants=[],
            folder="INBOX",
            date_first=datetime(2024, 1, 1, tzinfo=UTC),
            date_last=datetime(2024, 1, 2, tzinfo=UTC),
            message_ids=[thread_id],
            snippet="",
            has_attachments=False,
            score=score,
        )
        if slot:
            result.lane_ranks["keyword_slot"] = 2
        return result

    hits = [hit("b", 0.5), hit("a", 0.5), hit("z", 0.5, slot=True), hit("c", 0.5)]
    assert [r.thread_id for r in _order_ties(hits)] == ["a", "b", "z", "c"]


def _refs(hits: list[ThreadResult]) -> list[str]:
    return [_thread_ref(r.thread_id) for r in hits]


@pytest.mark.parametrize("q", GOLDEN["search"], ids=lambda q: q["id"])
def test_search_golden(results: dict[str, list[ThreadResult]], q: dict) -> None:
    hits = results[q["id"]]
    groups = q["required_evidence"]
    rank = first_hit_rank(_refs(hits), groups)
    assert rank is not None and rank <= q["max_rank"], (
        f"{groups}: first hit at rank {rank}, want <= {q['max_rank']}; got {_refs(hits)}"
    )
    # The best-ranked thread of each group found answers for that group;
    # groups missing from the top 10 count against evidence recall.
    ranks = group_ranks(_refs(hits), groups)
    answering = [hits[r - 1] for r in ranks if r is not None]
    if "evidence" in q:
        texts = [c.text for hit in answering for c in hit.evidence_chunks]
        assert any(q["evidence"] in t for t in texts), f"{q['evidence']!r} not in {texts}"
    if q.get("vector_only"):
        # Proves the vector lanes are wired in: keyword search alone
        # cannot find this thread, so a broken vector lane fails here.
        for hit in answering:
            keyword_lanes = {lane for lane in hit.lane_ranks if lane.endswith("_fts")}
            assert not keyword_lanes, f"expected vector lanes only, got {hit.lane_ranks}"


def test_mrr_floor(results: dict[str, list[ThreadResult]]) -> None:
    reciprocal = [
        1 / rank
        if (rank := first_hit_rank(_refs(results[q["id"]]), q["required_evidence"]))
        else 0.0
        for q in GOLDEN["search"]
    ]
    mrr = sum(reciprocal) / len(reciprocal)
    assert mrr >= GOLDEN["floors"]["mrr"], f"MRR {mrr:.3f} below floor"


def test_evidence_recall_floors(results: dict[str, list[ThreadResult]]) -> None:
    """Evidence recall@10, over every question and over the multi-source ones.

    A wiring check: with the hashed embedder these floors catch a lane or
    fusion change that drops one source of a multi-source answer, not
    semantic quality.
    """
    floors = GOLDEN["floors"]
    multi = [q for q in GOLDEN["search"] if len(q["required_evidence"]) > 1]
    for name, questions in (
        ("evidence_recall_at_10", GOLDEN["search"]),
        ("multi_source_evidence_recall_at_10", multi),
    ):
        recalls = [
            evidence_recall(_refs(results[q["id"]]), q["required_evidence"], k=10)
            for q in questions
        ]
        recall = sum(recalls) / len(recalls)
        assert recall >= floors[name], f"{name} {recall:.3f} below floor {floors[name]}"


@pytest.mark.parametrize("e", GOLDEN["enumerate"], ids=lambda e: e["id"])
def test_enumerate_golden(baseline_db: Database, e: dict) -> None:
    page = baseline_db.query_messages(limit=100, **e["args"])
    got = sorted(_message_ref(m.message_id) for m in page.messages)
    assert got == sorted(e["expect"])
    assert page.total_matches == len(e["expect"])


def _corpus_texts(maildir: Path) -> list[str]:
    """Every message's subject and text parts (body and attachments), casefolded."""
    texts = []
    for path in sorted(maildir.rglob("*.eml")):
        msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
        texts.append(str(msg["Subject"] or "").casefold())
        for part in msg.walk():
            if part.get_content_maintype() == "text":
                texts.append(part.get_content().casefold())
    return texts


@pytest.mark.parametrize("u", GOLDEN["unanswerable"], ids=lambda u: u["id"])
def test_unanswerable_golden(baseline_dir: Path, u: dict) -> None:
    # The abstention scenarios in tests/eval/agent_scenarios.json rest on
    # these questions having no answer in the corpus.
    texts = _corpus_texts(baseline_dir / "maildir")
    assert texts, "no messages found in the baseline maildir"
    for term in u["absent_terms"]:
        assert not any(term.casefold() in t for t in texts), f"{u['id']}: {term!r} is in the corpus"


AGENT_SCENARIOS = json.loads(AGENT_SCENARIOS_PATH.read_text(encoding="utf-8"))["scenarios"]
COUNTING_SCENARIOS = [s for s in AGENT_SCENARIOS if s.get("expected_answer_messages")]


def test_agent_required_citations_exist(baseline_db: Database) -> None:
    # The correction, conflict and counting scenarios in
    # tests/eval/agent_scenarios.json name messages by ref; each must be an
    # indexed message in its ref's thread.
    with closing(baseline_db._connect()) as conn:
        thread_of = {
            _message_ref(m): _thread_ref(t)
            for m, t in conn.execute("SELECT message_id, thread_id FROM messages")
        }
    refs = [ref for s in AGENT_SCENARIOS for g in s.get("required_citations", []) for ref in g]
    assert refs, "no scenario lists required_citations"
    counted = [
        ref
        for s in COUNTING_SCENARIOS
        for ref in s["expected_answer_messages"] + s.get("full_read_messages", [])
    ]
    assert counted, "no scenario lists expected_answer_messages"
    for ref in refs + counted:
        assert thread_of.get(ref) == ref.split(".")[0], f"{ref} is not an indexed message"


def _read_body_pages(baseline_db: Database, message_id: str) -> list[dict]:
    """Every ``get_message`` body page of ``message_id``, via the real tool."""
    server = FakeMCPServer()
    register_retrieval_tools(server, baseline_db)
    get_message = cast(Callable[..., Awaitable[CallToolResult]], server.tools["get_message"])
    pages: list[dict] = []
    offset: int | None = 0
    while offset is not None:
        result = asyncio.run(get_message(message_id=message_id, offset=offset))
        page = cast(dict, result.structured_content)
        pages.append(page)
        offset = page["next_offset"]
    return pages


@pytest.mark.parametrize("s", COUNTING_SCENARIOS, ids=lambda s: s["id"])
def test_agent_full_read_passage_is_past_the_first_page(baseline_db: Database, s: dict) -> None:
    """Each full-read message holds one of its scenario's forbidden values
    (the PIN the answer must not repeat) only after the first body page,
    so an agent that stops at page 1 never sees it."""
    assert s.get("full_read_messages"), s["id"]
    for ref in s["full_read_messages"]:
        pages = _read_body_pages(baseline_db, f"{ref}{_DOMAIN}")
        assert len(pages) > 1, f"{ref}: the body fits one page"
        first, rest = pages[0]["body"], "".join(p["body"] for p in pages[1:])
        assert any(v in rest and v not in first for v in s["forbidden_answer_text"]), ref


@pytest.mark.parametrize("s", COUNTING_SCENARIOS, ids=lambda s: s["id"])
def test_agent_forbidden_text_is_in_the_answer_messages(baseline_db: Database, s: dict) -> None:
    """Every forbidden value is real: some expected answer message's body
    holds it, so leaking it means copying it from the mail."""
    bodies = "".join(
        p["body"]
        for ref in s["expected_answer_messages"]
        for p in _read_body_pages(baseline_db, f"{ref}{_DOMAIN}")
    )
    for value in s["forbidden_answer_text"]:
        assert value in bodies, value


# The words an agent would look up for the counting scenario; each must
# list at least one decoy, or the scenario's exact-set check tests nothing.
_COUNTING_TRAPS = {"tofu-count": ["tofu", "PIN", "verification"]}


@pytest.mark.parametrize("s", COUNTING_SCENARIOS, ids=lambda s: s["id"])
def test_agent_counting_trap_is_real(baseline_db: Database, s: dict) -> None:
    expected = set(s["expected_answer_messages"])
    listed: set[str] = set()
    for word in _COUNTING_TRAPS[s["id"]]:
        page = baseline_db.query_messages(text=word, limit=100)
        found = {_message_ref(m.message_id) for m in page.messages}
        assert found - expected, f"{word!r} lists no decoy"
        listed |= found
    # The lookups between them reach every genuine message, so the answer
    # set is findable with the obvious words; telling it from the decoys
    # is the agent's job.
    assert expected <= listed


def test_rank_snapshot(
    results: dict[str, list[ThreadResult]], request: pytest.FixtureRequest
) -> None:
    current = {qid: _refs(hits) for qid, hits in results.items()}
    if request.config.getoption("--update-baseline"):
        SNAPSHOT_PATH.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        return
    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    changed = {
        qid: {"snapshot": expected.get(qid), "now": current.get(qid)}
        for qid in sorted(expected.keys() | current.keys())
        if expected.get(qid) != current.get(qid)
    }
    assert not changed, (
        "ranking changed; if intended, rerun with --update-baseline and "
        f"review the snapshot diff:\n{json.dumps(changed, indent=2)}"
    )


# Vector distances closer than this can come back in either order on
# another platform (sqlite-vec's float32 SIMD rounding differs between
# macOS and Linux CI by a step or two).
_NEAR_TIE = 1e-6


def test_rank_snapshot_survives_near_tied_vector_distances(
    baseline_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#755: a corpus edit left one golden question's snapshot on a knife
    edge, where two chunk distances one float32 step apart came back
    swapped on Linux and moved a thread past its neighbour. Swap every
    adjacent near-tied pair of different threads in the vector lanes, one
    at a time, and require the snapshot order to stay the same, so such
    an edit fails here on any platform rather than only in CI."""
    vectors = json.loads(
        (Path(os.environ["BASELINE_DIR"]) / "query_vectors.json").read_text(encoding="utf-8")
    )
    fuse = baseline_db._reciprocal_rank_fusion
    state: dict = {}

    def swapping_fusion(bm25, vec, chunks):
        lanes = {"vec": list(vec), "chunk": list(chunks)}
        if "swap" in state:
            name, i = state["swap"]
            lanes[name][i - 1], lanes[name][i] = lanes[name][i], lanes[name][i - 1]
        else:
            state["ties"] = [
                (name, i)
                for name, lane in lanes.items()
                for i in range(1, len(lane))
                if abs(lane[i].score - lane[i - 1].score) < _NEAR_TIE
                and lane[i].thread_id != lane[i - 1].thread_id
            ]
        return fuse(bm25, lanes["vec"], lanes["chunk"])

    monkeypatch.setattr(baseline_db, "_reciprocal_rank_fusion", swapping_fusion)
    swaps, flips = 0, []
    for q in GOLDEN["search"]:

        def run(q=q) -> list[str]:
            hits = baseline_db.hybrid_search(
                q["query"],
                vectors[q["query"]],
                limit=SNAPSHOT_DEPTH,
                with_evidence="evidence" in q,
                **q.get("filters", {}),
            )
            return _refs(_order_ties(hits))

        state.clear()
        expected = run()
        for tie in state["ties"]:
            state["swap"] = tie
            swaps += 1
            if run() != expected:
                flips.append((q["id"], *tie))
    # The hashed embedder leaves near-ties in many lanes; check some were tried.
    assert swaps > 0
    assert flips == [], f"snapshot order depends on a near-tied distance: {flips}"
