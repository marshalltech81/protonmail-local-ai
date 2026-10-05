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

import email
import email.policy
import json
import os
from contextlib import closing
from pathlib import Path

import pytest
from src.lib.sqlite import Database, ThreadResult

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
        # Ordering exact score ties by thread ID keeps the snapshot from
        # depending on how the lanes happened to insert tied threads.
        # Tied threads are grouped at the first of their positions, not
        # re-sorted by score: the keyword slot (#701) places a hit above
        # higher-scored results on purpose.
        first_pos: dict[float, int] = {}
        for pos, r in enumerate(hits):
            first_pos.setdefault(r.score, pos)
        out[q["id"]] = sorted(hits, key=lambda r: (first_pos[r.score], r.thread_id))
    return out


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


def test_agent_required_citations_exist(baseline_db: Database) -> None:
    # The correction and conflict scenarios in tests/eval/agent_scenarios.json
    # name messages by ref; each must be an indexed message in its ref's thread.
    scenarios = json.loads(AGENT_SCENARIOS_PATH.read_text(encoding="utf-8"))["scenarios"]
    with closing(baseline_db._connect()) as conn:
        thread_of = {
            _message_ref(m): _thread_ref(t)
            for m, t in conn.execute("SELECT message_id, thread_id FROM messages")
        }
    refs = [ref for s in scenarios for g in s.get("required_citations", []) for ref in g]
    assert refs, "no scenario lists required_citations"
    for ref in refs:
        assert thread_of.get(ref) == ref.split(".")[0], f"{ref} is not an indexed message"


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
