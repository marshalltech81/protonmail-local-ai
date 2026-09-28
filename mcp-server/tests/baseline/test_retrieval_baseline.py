"""Retrieval regression baseline, step 2 of 2.

Step 1 (``indexer/tests/baseline/build.py``) indexes a synthetic
mailbox with the real indexer and a deterministic hashed embedder, and
writes ``mail.db`` plus the golden questions' query vectors into
``BASELINE_DIR``. This module runs the golden questions through the real
read path (``hybrid_search`` with no reranker, ``query_messages``) and
checks two layers:

1. Golden assertions — correctness. Each question's expected thread
   must rank within its ``max_rank``; evidence and vector-only
   questions add their own checks; MRR must stay above the floor in
   ``golden.json``. Enumeration questions must return the exact
   message set.
2. Rank snapshot — unchanged behaviour. The top-10 order of every
   search question must equal ``snapshot.json``. After an intended
   ranking change, regenerate it with ``--update-baseline`` and review
   the diff in the PR.

Skipped unless ``BASELINE_DIR`` is set; ``make baseline`` runs both steps.
"""

import json
import os
from pathlib import Path

import pytest
from src.lib.sqlite import Database, ThreadResult

pytestmark = pytest.mark.baseline

_HERE = Path(__file__).parent
GOLDEN = json.loads((_HERE / "golden.json").read_text(encoding="utf-8"))
SNAPSHOT_PATH = _HERE / "snapshot.json"
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
        # Results arrive sorted by fused score; ordering exact ties by
        # thread ID keeps the snapshot from depending on how the lanes
        # happened to insert tied threads.
        out[q["id"]] = sorted(hits, key=lambda r: (-r.score, r.thread_id))
    return out


def _rank(hits: list[ThreadResult], ref: str) -> int | None:
    refs = [_thread_ref(r.thread_id) for r in hits]
    return refs.index(ref) + 1 if ref in refs else None


@pytest.mark.parametrize("q", GOLDEN["search"], ids=lambda q: q["id"])
def test_search_golden(results: dict[str, list[ThreadResult]], q: dict) -> None:
    hits = results[q["id"]]
    rank = _rank(hits, q["expect"])
    assert rank is not None and rank <= q["max_rank"], (
        f"{q['expect']} at rank {rank}, want <= {q['max_rank']}; "
        f"got {[_thread_ref(r.thread_id) for r in hits]}"
    )
    hit = hits[rank - 1]
    if "evidence" in q:
        texts = [c.text for c in hit.evidence_chunks]
        assert any(q["evidence"] in t for t in texts), f"{q['evidence']!r} not in {texts}"
    if q.get("vector_only"):
        # Proves the vector lanes are wired in: keyword search alone
        # cannot find this thread, so a broken vector lane fails here.
        keyword_lanes = {lane for lane in hit.lane_ranks if lane.endswith("_fts")}
        assert not keyword_lanes, f"expected vector lanes only, got {hit.lane_ranks}"


def test_mrr_floor(results: dict[str, list[ThreadResult]]) -> None:
    reciprocal = [
        1 / rank if (rank := _rank(results[q["id"]], q["expect"])) else 0.0
        for q in GOLDEN["search"]
    ]
    mrr = sum(reciprocal) / len(reciprocal)
    assert mrr >= GOLDEN["floors"]["mrr"], f"MRR {mrr:.3f} below floor"


@pytest.mark.parametrize("e", GOLDEN["enumerate"], ids=lambda e: e["id"])
def test_enumerate_golden(baseline_db: Database, e: dict) -> None:
    page = baseline_db.query_messages(limit=100, **e["args"])
    got = sorted(_message_ref(m.message_id) for m in page.messages)
    assert got == sorted(e["expect"])
    assert page.total_matches == len(e["expect"])


def test_rank_snapshot(
    results: dict[str, list[ThreadResult]], request: pytest.FixtureRequest
) -> None:
    current = {qid: [_thread_ref(r.thread_id) for r in hits] for qid, hits in results.items()}
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
