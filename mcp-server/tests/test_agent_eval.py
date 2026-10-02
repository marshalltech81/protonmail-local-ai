"""The agent-level scenario set and its reference traces, checked in CI.

``tests/eval/agent_scenarios.json`` holds questions about the synthetic
baseline mailbox, each naming the tools an agent should reach for and
the golden question in ``tests/baseline/golden.json`` whose evidence,
filters or enumeration answer it inherits. ``make baseline`` checks
those golden answers against a real index, so the scenarios cannot
drift from what the read path returns.

``tests/eval/agent_reference_traces.json`` holds one scripted trace per
scenario: the calls a good agent makes, the structured results it gets
and the IDs its answer cites. Each must score with no failures, which
pins the scorers against the scenario set; their failure cases are in
``tests/test_agent_metrics.py``, and ``test_failure_traces_are_caught``
mutates reference traces into the failures the correction, conflict and
abstention scenarios exist to catch. ``HELD_OUT_IDS`` pins the held-out
split. The contract tests below keep the
scenarios and traces in step with the real tool signatures and output
fields.
"""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from src.tools import outputs
from src.tools.intelligence import register_intelligence_tools
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools
from src.tools.system import register_system_tools

from tests.agent_metrics import (
    ID_FIELDS,
    PAGING_FIELDS,
    Scenario,
    is_held_out,
    load_scenarios,
    score_trace,
)
from tests.conftest import FakeEmbedClient, FakeInferenceClient, FakeMCPServer

TESTS = Path(__file__).parent
SCENARIOS_PATH = TESTS / "eval" / "agent_scenarios.json"
TRACES_PATH = TESTS / "eval" / "agent_reference_traces.json"
GOLDEN_PATH = TESTS / "baseline" / "golden.json"

SCENARIOS = {s.id: s for s in load_scenarios(SCENARIOS_PATH, GOLDEN_PATH)}
TRACES = json.loads(TRACES_PATH.read_text(encoding="utf-8"))["traces"]
TRACE_BY_SCENARIO = {t["scenario"]: t for t in TRACES}

# The held-out split as of this commit. Membership is a hash of the
# scenario ID (``is_held_out``), so adding a scenario appends to this
# list or leaves it alone, and no scenario ever moves between splits.
HELD_OUT_IDS = [
    "archived-pool-bids",
    "block-party-date",
    "electrician-quote",
    "frontdesk-appointment",
    "kayak-total-cost",
]


def _registered_tools() -> dict[str, Callable[..., Any]]:
    """Every non-experimental tool, registered on the stub server."""
    server = FakeMCPServer()
    register_search_tools(server, None, FakeEmbedClient())
    register_retrieval_tools(server, None)
    register_intelligence_tools(server, None, FakeEmbedClient(), FakeInferenceClient())
    register_system_tools(server, None)
    return cast(dict[str, Callable[..., Any]], server.tools)


def test_every_scenario_has_one_reference_trace() -> None:
    traced = [t["scenario"] for t in TRACES]
    assert sorted(traced) == sorted(SCENARIOS)


def test_scenarios_cover_search_and_enumeration() -> None:
    assert any(s.required_evidence for s in SCENARIOS.values())
    assert any(s.expected_messages for s in SCENARIOS.values())
    assert any(s.expected_arguments for s in SCENARIOS.values())
    assert any(len(s.required_evidence) > 1 for s in SCENARIOS.values())


def test_scenarios_cover_corrections_conflicts_and_abstention() -> None:
    by_category: dict[str, list[Scenario]] = {}
    for s in SCENARIOS.values():
        by_category.setdefault(s.category, []).append(s)
    for s in by_category["correction"]:
        # The correcting reply, not the thread root it corrects.
        assert s.required_citations
        assert all(not m.split("@")[0].endswith(".1") for g in s.required_citations for m in g)
    for s in by_category["conflicting_sources"]:
        assert len(s.required_citations) >= 2, s.id
    assert by_category["unanswerable"]
    for s in by_category["unanswerable"]:
        assert s.unanswerable and not s.required_evidence and not s.required_citations
    assert [s.id for s in SCENARIOS.values() if s.unanswerable] == [
        s.id for s in by_category["unanswerable"]
    ]


def test_held_out_split_is_stable() -> None:
    held_out = sorted(s.id for s in SCENARIOS.values() if s.held_out)
    assert held_out == HELD_OUT_IDS
    assert all(is_held_out(sid) for sid in HELD_OUT_IDS)
    # Both splits are in use.
    assert 0 < len(held_out) < len(SCENARIOS)


def _mutate(scenario_id: str, change: Callable[[dict], None]) -> dict:
    trace = copy.deepcopy(TRACE_BY_SCENARIO[scenario_id])
    change(trace["answer"])
    return trace


@pytest.mark.parametrize(
    ("scenario_id", "change", "failure"),
    [
        # Answering from the superseded recital date.
        (
            "recital-new-date",
            lambda a: a.update(cited=["t24.1@baseline.example#00000241"]),
            "message_citation_recall",
        ),
        # Quoting the first salary offer, not the revised one.
        (
            "final-offer-salary",
            lambda a: a.update(cited=["t17.1@baseline.example#00000171"]),
            "message_citation_recall",
        ),
        # Picking one block-party date and dropping the other source.
        (
            "block-party-date",
            lambda a: a.update(cited=a["cited"][:1]),
            "message_citation_recall",
        ),
        # Citing the grill request instead of the message with the date.
        (
            "block-party-date",
            lambda a: a.update(cited=[a["cited"][0], "t30.1@baseline.example#00000301"]),
            "message_citation_recall",
        ),
        # Answering an unanswerable question.
        ("cabin-wifi", lambda a: a.update(abstained=False), "abstention_correct"),
        # Abstaining but citing the near-miss cabin thread.
        (
            "cabin-wifi",
            lambda a: a.update(cited=["t27.1@baseline.example#00000027"]),
            "abstention_correct",
        ),
        ("electrician-quote", lambda a: a.pop("abstained"), "abstention_correct"),
        # Abstaining on an answerable question.
        ("roof-estimate-total", lambda a: a.update(abstained=True), "abstention_correct"),
    ],
    ids=[
        "correction-superseded",
        "correction-first-offer",
        "conflict-one-side",
        "conflict-wrong-message",
        "unanswerable-answered",
        "unanswerable-cites-near-miss",
        "unanswerable-no-flag",
        "answerable-abstained",
    ],
)
def test_failure_traces_are_caught(
    scenario_id: str, change: Callable[[dict], None], failure: str
) -> None:
    score = score_trace(SCENARIOS[scenario_id], _mutate(scenario_id, change))
    assert failure in score.failures, score


@pytest.mark.parametrize("trace", TRACES, ids=lambda t: t["scenario"])
def test_reference_trace_scores_clean(trace: dict) -> None:
    score = score_trace(SCENARIOS[trace["scenario"]], trace)
    assert score.failures == [], score


def test_expected_tools_and_arguments_exist() -> None:
    tools = _registered_tools()
    for scenario in SCENARIOS.values():
        for name in scenario.expected_tools:
            assert name in tools, f"{scenario.id}: unknown tool {name}"
        # The expected arguments must be accepted by at least one
        # expected tool, or no trace could ever pass the scenario.
        assert not scenario.expected_arguments or any(
            set(scenario.expected_arguments) <= set(inspect.signature(tools[name]).parameters)
            for name in scenario.expected_tools
        ), scenario.id


def test_reference_trace_calls_match_tool_signatures() -> None:
    tools = _registered_tools()
    for trace in TRACES:
        for call in trace["calls"]:
            assert call["tool"] in tools, call["tool"]
            params = inspect.signature(tools[call["tool"]]).parameters
            assert set(call["arguments"]) <= set(params), (trace["scenario"], call["tool"])


def test_scorers_read_fields_the_output_models_publish() -> None:
    """The scorers find IDs and paging state by field name; pin those names."""
    published = {
        name
        for model in vars(outputs).values()
        if isinstance(model, type) and issubclass(model, outputs._Output)
        for name in model.model_fields
    }
    assert set(ID_FIELDS) <= published
    assert set(PAGING_FIELDS) <= set(outputs.QueryMessagesOutput.model_fields)
    assert {"thread_id", "message_id", "claimant_id"} <= set(outputs.ListedMessage.model_fields)
    assert {"chunk_id", "thread_id"} <= set(outputs.Citation.model_fields)
    assert "chunk_id" in outputs.EvidenceChunk.model_fields


class TestLoadScenarios:
    def _load(self, tmp_path: Path, rows: list[dict]) -> list[Scenario]:
        path = tmp_path / "scenarios.json"
        path.write_text(json.dumps({"scenarios": rows}))
        return load_scenarios(path, GOLDEN_PATH)

    def _row(self, **overrides: object) -> dict:
        row: dict = {
            # Outside the held-out split, so the row needs no held_out flag.
            "id": "s3",
            "category": "multiple_sources",
            "question": "synthetic",
            "expected_tools": ["search_emails"],
            "max_calls": 2,
            "golden_search": "multi-kayak",
        }
        row.update(overrides)
        return row

    def test_search_reference_inherits_evidence_as_thread_ids(self, tmp_path: Path) -> None:
        (s,) = self._load(tmp_path, [self._row()])
        assert s.required_evidence == [["t21.1@baseline.example"], ["t22.1@baseline.example"]]
        assert s.expected_arguments == {}
        assert s.expected_messages == []

    def test_search_reference_inherits_filters_as_arguments(self, tmp_path: Path) -> None:
        (s,) = self._load(tmp_path, [self._row(golden_search="filter-folder")])
        assert s.expected_arguments == {"folders": ["Archive"]}

    def test_enumerate_reference_inherits_arguments_and_messages(self, tmp_path: Path) -> None:
        row = self._row(golden_enumerate="sender")
        del row["golden_search"]
        (s,) = self._load(tmp_path, [row])
        assert s.expected_arguments == {"sender": "adjuster@keystoneins.example"}
        assert s.expected_messages == ["t10.1@baseline.example", "t10.3@baseline.example"]
        assert s.required_evidence == []

    @pytest.mark.parametrize(
        "overrides",
        [
            {"golden_search": "no-such-question"},
            {"golden_enumerate": "sender"},
            {"golden_search": None},
            {"expected_tools": []},
        ],
        ids=["unknown-golden", "both-references", "no-reference", "no-tools"],
    )
    def test_bad_rows_are_rejected(self, tmp_path: Path, overrides: dict) -> None:
        row = self._row(**overrides)
        row = {k: v for k, v in row.items() if v is not None}
        with pytest.raises(ValueError, match="s3"):
            self._load(tmp_path, [row])

    def test_unanswerable_reference_expects_abstention(self, tmp_path: Path) -> None:
        row = self._row(golden_unanswerable="cabin-wifi")
        del row["golden_search"]
        (s,) = self._load(tmp_path, [row])
        assert s.unanswerable is True
        assert s.required_evidence == []
        assert s.expected_arguments == {}

    def test_required_citations_become_message_ids(self, tmp_path: Path) -> None:
        row = self._row(golden_search="correction-recital", required_citations=[["t24.2"]])
        (s,) = self._load(tmp_path, [row])
        assert s.required_citations == [["t24.2@baseline.example"]]
        assert s.unanswerable is False

    @pytest.mark.parametrize(
        "overrides",
        [
            {"golden_search": "correction-recital", "required_citations": [["t17.3"]]},
            {"golden_unanswerable": "no-such-question", "golden_search": None},
            {"golden_unanswerable": "cabin-wifi"},
            {
                "golden_unanswerable": "cabin-wifi",
                "golden_search": None,
                "required_citations": [["t27.1"]],
            },
            {"held_out": True},
        ],
        ids=[
            "citation-outside-evidence",
            "unknown-unanswerable",
            "search-and-unanswerable",
            "citations-without-search",
            "held-out-off-rule",
        ],
    )
    def test_bad_new_rows_are_rejected(self, tmp_path: Path, overrides: dict) -> None:
        row = self._row(**overrides)
        row = {k: v for k, v in row.items() if v is not None}
        with pytest.raises(ValueError, match="s3"):
            self._load(tmp_path, [row])

    def test_held_out_flag_must_be_present_when_the_rule_holds(self, tmp_path: Path) -> None:
        held = next(sid for sid in HELD_OUT_IDS)
        with pytest.raises(ValueError, match=held):
            self._load(tmp_path, [self._row(id=held)])
        (s,) = self._load(tmp_path, [self._row(id=held, held_out=True)])
        assert s.held_out is True

    def test_duplicate_ids_are_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="s3"):
            self._load(tmp_path, [self._row(), self._row()])
