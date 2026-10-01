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
``tests/test_agent_metrics.py``. The contract tests below keep the
scenarios and traces in step with the real tool signatures and output
fields.
"""

from __future__ import annotations

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
    assert set(ID_FIELDS) <= set(outputs.ListedMessage.model_fields)


class TestLoadScenarios:
    def _load(self, tmp_path: Path, rows: list[dict]) -> list[Scenario]:
        path = tmp_path / "scenarios.json"
        path.write_text(json.dumps({"scenarios": rows}))
        return load_scenarios(path, GOLDEN_PATH)

    def _row(self, **overrides: object) -> dict:
        row: dict = {
            "id": "s1",
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
        with pytest.raises(ValueError, match="s1"):
            self._load(tmp_path, [row])

    def test_duplicate_ids_are_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="s1"):
            self._load(tmp_path, [self._row(), self._row()])
