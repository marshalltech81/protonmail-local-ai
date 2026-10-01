"""Deterministic agent-level scorers for MCP tool-call traces (#283).

The retrieval metrics in ``tests/retrieval_metrics.py`` ask whether
search ranks the right threads. These ask what an agent did with the
tools: given a scenario (a question with known answers on the
synthetic mailbox) and a trace of the calls an agent made, score

- **tool selection**: the first call used one of the expected tools;
- **argument accuracy**: one call to an expected tool carried every
  expected argument with the expected value (other arguments are free);
- **evidence recall**: the fraction of required evidence groups with a
  thread ID somewhere in the tool results, the agent-level form of
  evidence recall (see ``retrieval_metrics``);
- **citation validity**: the fraction of IDs the answer cites that some
  tool result returned (an ID no tool returned is fabricated);
- **citation recall**: the fraction of required evidence groups the
  answer cites (a cited message covers its thread);
- **enumeration completeness**: for an exhaustive question, the
  fraction of expected messages listed by one ``query_messages``
  cursor chain over exactly the expected filters (any page size), and
  whether that chain's last page said ``has_more: false``;
- **unnecessary calls**: calls over the scenario's budget, and calls
  identical (tool and arguments) to an earlier one.

Nothing here judges whether the answer's prose is right: answer quality
is graded by hand (``tests/eval/README.md``). A valid citation shows
the agent saw the source, not that the source supports the statement.

A trace is JSON: ``{"scenario": id, "calls": [{"tool", "arguments",
"result"}], "answer": {"text", "cited": [ids]}}`` where ``result`` is
the call's ``structuredContent``. IDs are read from the result fields
named in ``ID_FIELDS`` at any depth, so the scorers follow every tool's
output shape without a per-tool parser; arguments never count as
retrieved.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.retrieval_metrics import evidence_recall

# Result fields holding an ID an answer can cite. ``thread_id`` names a
# thread; the other two name a message within the thread named beside it.
ID_FIELDS = ("thread_id", "message_id", "claimant_id")
# ``query_messages`` fields that say whether an enumeration is complete.
PAGING_FIELDS = ("has_more", "next_cursor", "messages")
ENUMERATING_TOOL = "query_messages"
# Its arguments that page rather than filter: the agent picks the page
# size, and the cursor is checked against the previous page.
_PAGING_ARGUMENTS = ("limit", "cursor")

# The synthetic baseline mailbox (indexer/tests/baseline/corpus.py):
# golden.json writes thread "t05" for "t05.1@baseline.example" and
# message "t05.2" for "t05.2@baseline.example".
_BASELINE_DOMAIN = "@baseline.example"


@dataclass(frozen=True)
class Scenario:
    """One question an agent should answer with the tools.

    ``required_evidence`` uses the groups of ``retrieval_metrics``: every
    group is required, any thread ID in a group satisfies it.
    ``expected_messages`` lists the message IDs an exhaustive question
    must enumerate. Either may be empty.
    """

    id: str
    category: str
    question: str
    expected_tools: list[str]
    expected_arguments: dict[str, Any]
    max_calls: int
    required_evidence: list[list[str]] = field(default_factory=list)
    expected_messages: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentScore:
    """A trace's scores; ``None`` means the metric does not apply."""

    scenario_id: str
    category: str
    tool_selected: bool
    arguments_correct: bool | None
    evidence_recall: float | None
    citation_validity: float | None
    citation_recall: float | None
    enumeration_recall: float | None
    exhausted: bool | None
    extra_calls: int
    repeated_calls: int

    @property
    def failures(self) -> list[str]:
        """Names of the metrics this trace did not get full marks on."""
        failed = []
        for name in ("tool_selected", "arguments_correct", "exhausted"):
            if getattr(self, name) is False:
                failed.append(name)
        for name in (
            "evidence_recall",
            "citation_validity",
            "citation_recall",
            "enumeration_recall",
        ):
            value = getattr(self, name)
            if value is not None and value < 1.0:
                failed.append(name)
        for name in ("extra_calls", "repeated_calls"):
            if getattr(self, name):
                failed.append(name)
        return failed


def _returned_ids(calls: Sequence[dict]) -> tuple[set[str], dict[str, str]]:
    """IDs the tool results returned, and each message ID's thread.

    A message row carries its thread in a ``thread_id`` beside it or, in
    ``get_thread``, in the ``thread`` summary of the enclosing result, so
    the nearest enclosing thread ID is the message's thread.
    """
    seen: set[str] = set()
    thread_of: dict[str, str] = {}

    def visit(value: Any, thread: str | None) -> None:
        if isinstance(value, list):
            for child in value:
                visit(child, thread)
            return
        if not isinstance(value, dict):
            return
        # get_thread puts its thread ID in the ``thread`` summary, a
        # sibling of ``messages``, so read it before visiting children.
        summary = value.get("thread")
        for own in (value.get("thread_id"), isinstance(summary, dict) and summary.get("thread_id")):
            if isinstance(own, str):
                thread = own
                break
        for name in ID_FIELDS:
            item = value.get(name)
            if isinstance(item, str):
                seen.add(item)
                if name != "thread_id" and thread:
                    thread_of[item] = thread
        for child in value.values():
            visit(child, thread)

    for call in calls:
        visit(call.get("result"), None)
    return seen, thread_of


def _groups_covered(ids: set[str], groups: list[list[str]]) -> float:
    return evidence_recall(sorted(ids), groups, k=len(ids))


def _enumeration_chains(calls: Sequence[dict], predicates: dict[str, Any]) -> list[list[dict]]:
    """The ``query_messages`` page results of each cursor chain over ``predicates``.

    Only calls whose filters are exactly the expected predicates count
    (blank filters are ignored, as the tool ignores them). A call with no
    cursor starts a chain; one whose cursor is the previous page's
    ``next_cursor`` continues it; any other cursor breaks the chain, and
    its page joins none.
    """
    chains: list[list[dict]] = []
    current: list[dict] | None = None
    for call in calls:
        if call["tool"] != ENUMERATING_TOOL:
            continue
        arguments = call["arguments"]
        filters = {
            k: v for k, v in arguments.items() if k not in _PAGING_ARGUMENTS and v not in (None, "")
        }
        if filters != predicates:
            continue
        cursor = arguments.get("cursor")
        if not cursor:
            current = [call["result"]]
            chains.append(current)
        elif current is not None and cursor == current[-1].get("next_cursor"):
            current.append(call["result"])
        else:
            current = None
    return chains


def score_trace(scenario: Scenario, trace: dict) -> AgentScore:
    """Score one trace against its scenario."""
    if trace.get("scenario") != scenario.id:
        raise ValueError(f"trace for {trace.get('scenario')!r} scored against {scenario.id!r}")
    calls: list[dict] = trace.get("calls", [])
    cited: list[str] = trace.get("answer", {}).get("cited", [])
    seen, thread_of = _returned_ids(calls)

    tool_selected = bool(calls) and calls[0]["tool"] in scenario.expected_tools

    arguments_correct: bool | None = None
    if scenario.expected_arguments:
        arguments_correct = any(
            call["tool"] in scenario.expected_tools
            and all(
                call["arguments"].get(name) == value
                for name, value in scenario.expected_arguments.items()
            )
            for call in calls
        )

    evidence: float | None = None
    citation_recall: float | None = None
    if scenario.required_evidence:
        evidence = _groups_covered(seen, scenario.required_evidence)
        covered = set(cited) | {thread_of[c] for c in cited if c in thread_of}
        citation_recall = _groups_covered(covered, scenario.required_evidence)

    citation_validity = sum(1 for c in cited if c in seen) / len(cited) if cited else None

    enumeration_recall: float | None = None
    exhausted: bool | None = None
    if scenario.expected_messages:
        expected = set(scenario.expected_messages)
        enumeration_recall, exhausted = 0.0, False
        for chain in _enumeration_chains(calls, scenario.expected_arguments):
            listed = {m["message_id"] for page in chain for m in page.get("messages", [])}
            chain_score = (
                len(expected & listed) / len(expected),
                chain[-1].get("has_more") is False,
            )
            enumeration_recall, exhausted = max((enumeration_recall, exhausted), chain_score)

    signatures = [json.dumps([c["tool"], c["arguments"]], sort_keys=True) for c in calls]
    repeated = len(signatures) - len(set(signatures))

    return AgentScore(
        scenario_id=scenario.id,
        category=scenario.category,
        tool_selected=tool_selected,
        arguments_correct=arguments_correct,
        evidence_recall=evidence,
        citation_validity=citation_validity,
        citation_recall=citation_recall,
        enumeration_recall=enumeration_recall,
        exhausted=exhausted,
        extra_calls=max(0, len(calls) - scenario.max_calls),
        repeated_calls=repeated,
    )


def _rate(values: list[bool]) -> str:
    if not values:
        return "n/a"
    return f"{sum(values) / len(values):.2%} ({sum(values)}/{len(values)})"


def _mean(values: list[float]) -> str:
    if not values:
        return "n/a"
    return f"{sum(values) / len(values):.2%} (mean over {len(values)})"


def summarize(scores: Sequence[AgentScore]) -> str:
    """Aggregate scores, then the failing scenarios grouped by category."""

    def present(name: str) -> list:
        return [v for s in scores if (v := getattr(s, name)) is not None]

    lines = [
        f"Agent eval summary ({len(scores)} traces):",
        f"  Tool selection:      {_rate([s.tool_selected for s in scores])}",
        f"  Argument accuracy:   {_rate(present('arguments_correct'))}",
        f"  Evidence recall:     {_mean(present('evidence_recall'))}",
        f"  Citation validity:   {_mean(present('citation_validity'))}",
        f"  Citation recall:     {_mean(present('citation_recall'))}",
        f"  Enumeration recall:  {_mean(present('enumeration_recall'))}",
        f"  Enumeration exhausted: {_rate(present('exhausted'))}",
        f"  Extra calls:         {sum(s.extra_calls for s in scores)}",
        f"  Repeated calls:      {sum(s.repeated_calls for s in scores)}",
    ]
    by_category: dict[str, list[str]] = defaultdict(list)
    for s in scores:
        if s.failures:
            by_category[s.category].append(f"{s.scenario_id} ({', '.join(s.failures)})")
    if not by_category:
        lines.append("  Failures by category: none")
    else:
        lines.append("  Failures by category:")
        for category in sorted(by_category):
            lines.append(f"    {category}: {'; '.join(by_category[category])}")
    return "\n".join(lines)


def load_scenarios(path: Path, golden_path: Path) -> list[Scenario]:
    """Load scenarios, resolving each one's golden question.

    A row names ``golden_search`` (a ``search`` question: its
    ``required_evidence`` and ``filters`` become the scenario's evidence
    and expected arguments) or ``golden_enumerate`` (an ``enumerate``
    question: its ``args`` and ``expect`` become the expected arguments
    and messages). Inheriting them keeps the scenario on answers
    ``make baseline`` checks against a real index.
    """
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    searches = {q["id"]: q for q in golden["search"]}
    enumerations = {q["id"]: q for q in golden["enumerate"]}
    rows = json.loads(path.read_text(encoding="utf-8"))["scenarios"]

    scenarios: list[Scenario] = []
    ids: set[str] = set()
    for row in rows:
        sid = row["id"]
        if sid in ids:
            raise ValueError(f"{sid}: duplicate scenario id")
        ids.add(sid)
        if not row.get("expected_tools"):
            raise ValueError(f"{sid}: expected_tools is empty")
        search_ref, enum_ref = row.get("golden_search"), row.get("golden_enumerate")
        if (search_ref is None) == (enum_ref is None):
            raise ValueError(f"{sid}: give one of golden_search or golden_enumerate")
        if search_ref is not None:
            if search_ref not in searches:
                raise ValueError(f"{sid}: no golden search question {search_ref!r}")
            q = searches[search_ref]
            evidence = [[f"{t}.1{_BASELINE_DOMAIN}" for t in g] for g in q["required_evidence"]]
            arguments, messages = dict(q.get("filters", {})), []
        else:
            if enum_ref not in enumerations:
                raise ValueError(f"{sid}: no golden enumerate question {enum_ref!r}")
            q = enumerations[enum_ref]
            evidence = []
            arguments = dict(q["args"])
            messages = [f"{m}{_BASELINE_DOMAIN}" for m in q["expect"]]
        scenarios.append(
            Scenario(
                id=sid,
                category=row["category"],
                question=row["question"],
                expected_tools=list(row["expected_tools"]),
                expected_arguments=arguments,
                max_calls=row["max_calls"],
                required_evidence=evidence,
                expected_messages=messages,
            )
        )
    return scenarios
