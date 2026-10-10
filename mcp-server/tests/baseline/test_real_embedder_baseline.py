"""Real-embedder retrieval baseline, step 2 of 2 (#1439).

Step 1 (``indexer/tests/baseline/real_embedder.py``) builds the
synthetic baseline once per repeat with the operator's real embedding
model into ``REAL_EMBED_DIR/repeat-<N>`` and writes ``run.json``. This
module ranks every golden ``search`` and ``semantic`` question on each
repeat through ``hybrid_search`` (no reranker), checks the model's
floors in ``golden.json`` ``real_embedder_floors`` on every repeat,
and prints a report: the four metrics per repeat (all questions, then
the lexical ``search`` and the paraphrase ``semantic`` subsets), the
questions whose top 10 differ between repeats (ranking flips), the
vector variation and the spend.

The floors are synthetic regression evidence for one model, not a
claim about mailbox quality. Per-question ``max_rank`` and the rank
snapshot belong to the hashed baseline and are not checked here.

Skipped unless ``REAL_EMBED_DIR`` is set; ``make baseline-real-embedder``
runs both steps.
"""

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from src.lib.sqlite import Database

from tests.baseline.test_retrieval_baseline import SNAPSHOT_DEPTH, _order_ties, _thread_ref
from tests.retrieval_metrics import evidence_recall, first_hit_rank

pytestmark = pytest.mark.baseline

GOLDEN = json.loads((Path(__file__).parent / "golden.json").read_text(encoding="utf-8"))
METRICS = (
    "hit_at_10",
    "mrr",
    "evidence_recall_at_10",
    "multi_source_evidence_recall_at_10",
)
K = 10


def questions() -> list[dict]:
    """Every ranked question: the lexical ``search`` set and the
    ``semantic`` paraphrases."""
    return GOLDEN["search"] + GOLDEN["semantic"]


def metrics(rankings: Mapping[str, Sequence[str]], qs: Sequence[dict]) -> dict[str, float | None]:
    """The four floor metrics over ``qs``, from each question's ranked
    thread refs. Multi-source recall is ``None`` when no question in
    ``qs`` has more than one evidence group."""
    ranks = [first_hit_rank(rankings[q["id"]][:K], q["required_evidence"]) for q in qs]
    recalls = [evidence_recall(rankings[q["id"]], q["required_evidence"], k=K) for q in qs]
    multi = [
        evidence_recall(rankings[q["id"]], q["required_evidence"], k=K)
        for q in qs
        if len(q["required_evidence"]) > 1
    ]
    return {
        "hit_at_10": sum(1 for r in ranks if r is not None) / len(qs),
        "mrr": sum(1 / r for r in ranks if r is not None) / len(qs),
        "evidence_recall_at_10": sum(recalls) / len(recalls),
        "multi_source_evidence_recall_at_10": sum(multi) / len(multi) if multi else None,
    }


def floor_failures(measured: Mapping[str, float | None], floors: Mapping[str, float]) -> list[str]:
    """One line per metric below its floor (or missing), in ``METRICS`` order."""
    failures = []
    for name in METRICS:
        value = measured.get(name)
        if value is None or value < floors[name]:
            shown = "missing" if value is None else f"{value:.4f}"
            failures.append(f"{name} {shown} below floor {floors[name]}")
    return failures


def ranking_flips(per_repeat: Sequence[Mapping[str, Sequence[str]]]) -> list[str]:
    """Questions whose top-10 thread order differs between repeat 1 and
    any later repeat."""
    first = per_repeat[0]
    return sorted(
        {qid for later in per_repeat[1:] for qid in first if list(first[qid]) != list(later[qid])}
    )


def evidence_misses(rankings: Mapping[str, Sequence[str]], qs: Sequence[dict]) -> list[str]:
    """Questions with an evidence group missing from the top 10, each
    with the number of groups it found."""
    out = []
    for q in qs:
        recall = evidence_recall(rankings[q["id"]], q["required_evidence"], k=K)
        if recall < 1:
            groups = len(q["required_evidence"])
            out.append(f"{q['id']} ({round(recall * groups)}/{groups})")
    return out


def model_floors(model: str) -> dict[str, float] | None:
    floors = GOLDEN["real_embedder_floors"].get(model)
    return None if floors is None else {name: floors[name] for name in METRICS}


@pytest.fixture(scope="module")
def run_dir() -> Path:
    value = os.environ.get("REAL_EMBED_DIR")
    if not value:
        pytest.skip("REAL_EMBED_DIR not set; run `make baseline-real-embedder`")
    return Path(value)


@pytest.fixture(scope="module")
def run_report(run_dir: Path) -> dict:
    return json.loads((run_dir / "run.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rankings(run_dir: Path, run_report: dict) -> list[dict[str, list[str]]]:
    """Each repeat's top-10 thread refs per question."""
    out = []
    for repeat in range(1, run_report["repeats"] + 1):
        built = run_dir / f"repeat-{repeat}"
        db = Database(str(built / "mail.db"))
        vectors = json.loads((built / "query_vectors.json").read_text(encoding="utf-8"))
        out.append(
            {
                q["id"]: [
                    _thread_ref(r.thread_id)
                    for r in _order_ties(
                        db.hybrid_search(
                            q["query"],
                            vectors[q["query"]],
                            limit=SNAPSHOT_DEPTH,
                            **q.get("filters", {}),
                        )
                    )
                ]
                for q in questions()
            }
        )
    return out


def _format(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def test_real_embedder_report_and_floors(run_report: dict, rankings: list) -> None:
    """Print the report, then fail on any metric of any repeat below the
    model's floor. A model with no recorded floors fails: its measured
    values are printed so they can be reviewed and recorded."""
    subsets = {
        "all": questions(),
        "search": GOLDEN["search"],
        "semantic": GOLDEN["semantic"],
    }
    measured = [metrics(r, questions()) for r in rankings]
    lines = [
        "",
        f"Real-embedder retrieval baseline (synthetic corpus; not a mailbox-quality claim):"
        f" model {run_report['model']}, endpoint {run_report['endpoint']},"
        f" {run_report['repeats']} repeats, batch size {run_report['batch_size']}",
    ]
    for repeat, ranking in enumerate(rankings, start=1):
        for subset, qs in subsets.items():
            values = metrics(ranking, qs)
            shown = ", ".join(f"{name} {_format(values[name])}" for name in METRICS)
            lines.append(f"  repeat {repeat} {subset} ({len(qs)} questions): {shown}")
        missed = evidence_misses(ranking, questions())
        lines.append(f"  repeat {repeat} evidence missing from the top 10: {missed or 'none'}")
    flips = ranking_flips(rankings)
    lines.append(f"  ranking flips between repeats: {len(flips)} {flips if flips else ''}".rstrip())
    for name, variation in run_report["variation"].items():
        lines.append(
            f"  {name} vector variation: max cosine distance"
            f" {variation['max_cosine_distance']:.3g},"
            f" mean {variation['mean_cosine_distance']:.3g},"
            f" {variation['pairs_not_identical']} of {variation['pairs']} pairs not identical"
        )
    tokens = run_report["input_tokens"]
    lines.append(
        f"  spend: {run_report['requests']} requests (cap {run_report['max_requests']},"
        f" {run_report['query_retries']} query retries),"
        f" {'unreported' if tokens is None else tokens} input tokens,"
        f" {run_report['wall_seconds']} s; cache hits {run_report['cache_hits']},"
        f" misses {run_report['cache_misses']}"
    )
    print("\n".join(lines))

    floors = model_floors(run_report["model"])
    assert floors is not None, (
        f"golden.json real_embedder_floors has no entry for {run_report['model']}; measure"
        " and record one (see tests/baseline/README.md)"
    )
    failures = [
        f"repeat {repeat}: {line}"
        for repeat, values in enumerate(measured, start=1)
        for line in floor_failures(values, floors)
    ]
    assert not failures, "; ".join(failures)


# ---- the scoring itself, checked without a run --------------------------


def _q(qid: str, groups: list[list[str]]) -> dict:
    return {"id": qid, "query": qid, "required_evidence": groups}


def test_metrics_score_hits_ranks_and_groups() -> None:
    qs = [
        _q("a", [["t01"]]),
        _q("b", [["t02"], ["t03"]]),
        _q("c", [["t04"]]),
    ]
    rankings = {
        "a": ["t09", "t01"],
        "b": ["t02"] + [f"x{i}" for i in range(10)] + ["t03"],
        "c": ["t08"],
    }
    got = metrics(rankings, qs)
    assert got["hit_at_10"] == pytest.approx(2 / 3)
    assert got["mrr"] == pytest.approx((1 / 2 + 1) / 3)
    # b finds t02 in the top 10 and t03 only at rank 12.
    assert got["evidence_recall_at_10"] == pytest.approx((1 + 0.5 + 0) / 3)
    assert got["multi_source_evidence_recall_at_10"] == pytest.approx(0.5)


def test_metrics_ignore_a_hit_past_rank_10() -> None:
    got = metrics({"a": [f"x{i}" for i in range(10)] + ["t01"]}, [_q("a", [["t01"]])])
    assert got["hit_at_10"] == 0 and got["mrr"] == 0


def test_metrics_without_multi_source_questions() -> None:
    got = metrics({"a": ["t01"]}, [_q("a", [["t01"]])])
    assert got["multi_source_evidence_recall_at_10"] is None


def test_floor_failures_name_each_metric_independently() -> None:
    floors = dict.fromkeys(METRICS, 0.9)
    at_floor = dict.fromkeys(METRICS, 0.9)
    assert floor_failures(at_floor, floors) == []
    for name in METRICS:
        below = {**at_floor, name: 0.8999}
        assert floor_failures(below, floors) == [f"{name} 0.8999 below floor 0.9"]
    missing = {**at_floor, "multi_source_evidence_recall_at_10": None}
    assert floor_failures(missing, floors) == [
        "multi_source_evidence_recall_at_10 missing below floor 0.9"
    ]


def test_evidence_misses_list_each_short_question_with_its_groups_found() -> None:
    qs = [_q("a", [["t01"]]), _q("b", [["t02"], ["t03"]]), _q("c", [["t04"]])]
    rankings = {"a": ["t01"], "b": ["t02"], "c": ["t09"]}
    assert evidence_misses(rankings, qs) == ["b (1/2)", "c (0/1)"]


def test_ranking_flips_compare_every_repeat_with_the_first() -> None:
    one = {"a": ["t01", "t02"], "b": ["t03"]}
    assert ranking_flips([one, dict(one)]) == []
    swapped = {"a": ["t02", "t01"], "b": ["t03"]}
    assert ranking_flips([one, one, swapped]) == ["a"]
    # A question that flips in two repeats is listed once.
    assert ranking_flips([one, swapped, swapped]) == ["a"]


def test_semantic_questions_follow_the_golden_shape() -> None:
    """Every paraphrase question has a unique id across both sets, a
    query and evidence groups of thread refs, and nothing the hashed
    baseline's per-question checks would read."""
    ids = [q["id"] for q in questions()]
    assert len(ids) == len(set(ids))
    assert GOLDEN["semantic"], "no semantic questions"
    for q in GOLDEN["semantic"]:
        assert set(q) == {"id", "query", "required_evidence"}, q["id"]
        assert q["query"].strip()
        assert q["required_evidence"] and all(
            group and all(ref.startswith("t") and ref[1:].isdigit() for ref in group)
            for group in q["required_evidence"]
        ), q["id"]
    assert any(len(q["required_evidence"]) > 1 for q in GOLDEN["semantic"])


def test_every_model_floor_names_the_four_metrics() -> None:
    assert GOLDEN["real_embedder_floors"], "no real-embedder floors recorded"
    for model, floors in GOLDEN["real_embedder_floors"].items():
        assert set(METRICS) <= set(floors), model
        for name in METRICS:
            assert 0 < floors[name] <= 1, (model, name)
