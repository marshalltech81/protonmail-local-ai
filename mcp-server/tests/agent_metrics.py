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
  answer cites (a cited message or passage covers its thread);
- **message citation recall**: the fraction of required citation groups
  (message IDs) the answer cites, a cited passage or claimant ID
  counting as its message. A correction scenario requires the
  correcting message, which thread-level citation recall cannot tell
  from the message it corrects; a conflicting-sources scenario requires
  each side of the disagreement;
- **abstention**: an unanswerable scenario passes when the answer sets
  ``"abstained": true`` and cites nothing; any other scenario passes
  when the answer does not abstain;
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
"result"}], "answer": {"text", "cited": [ids], "abstained": bool}}``
where ``result`` is the call's ``structuredContent`` and ``abstained``
(optional, false when absent) is the agent's structured statement that
the mailbox does not answer the question. IDs are read from the result fields
named in ``ID_FIELDS`` at any depth, so the scorers follow every tool's
output shape without a per-tool parser; arguments never count as
retrieved.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.lib.sqlite import _normalize_date_range

from tests.retrieval_metrics import evidence_recall

# Result fields holding an ID an answer can cite. ``thread_id`` names a
# thread; the others name a message, or a passage (``chunk_id``, the ID
# ask_mailbox citations name), within the nearest enclosing thread.
ID_FIELDS = ("thread_id", "message_id", "claimant_id", "chunk_id")
# ``query_messages`` fields that say whether an enumeration is complete.
PAGING_FIELDS = ("has_more", "next_cursor", "messages")
ENUMERATING_TOOL = "query_messages"
# Its arguments that page rather than filter: the agent picks the page
# size, and the cursor is checked against the previous page.
_PAGING_ARGUMENTS = ("limit", "cursor")
# Its string filters that ``Database.query_messages`` strips, a blank one
# being absent (``src/lib/sqlite.py``).
_STRIPPED_FILTERS = (
    "sender",
    "recipient",
    "participant",
    "subject",
    "text",
    "folder",
    "authority_class",
)

# The synthetic baseline mailbox (indexer/tests/baseline/corpus.py):
# golden.json writes thread "t05" for "t05.1@baseline.example" and
# message "t05.2" for "t05.2@baseline.example".
_BASELINE_DOMAIN = "@baseline.example"
# A golden message ref: thread "t24", message 2.
_MESSAGE_REF = re.compile(r"t[0-9]{2}\.[1-9][0-9]*")
# One scenario in HELD_OUT_MODULUS is held out, chosen by a hash of its
# ID so membership is fixed when the scenario is written and never moves
# when others are added.
HELD_OUT_MODULUS = 4


def is_held_out(scenario_id: str) -> bool:
    """Whether a scenario belongs to the held-out split.

    Held-out scenarios are scored like the rest but must not be used to
    tune anything (prompts, tool descriptions, thresholds); their score
    is the check on whatever was tuned against the dev split.
    """
    digest = hashlib.sha256(scenario_id.encode("utf-8")).hexdigest()
    return int(digest, 16) % HELD_OUT_MODULUS == 0


@dataclass(frozen=True)
class Scenario:
    """One question an agent should answer with the tools.

    ``required_evidence`` uses the groups of ``retrieval_metrics``: every
    group is required, any thread ID in a group satisfies it.
    ``expected_messages`` lists the message IDs an exhaustive question
    must enumerate. Either may be empty. ``required_citations`` groups
    message IDs the answer must cite the same way (every group, any ID
    in it). ``unanswerable`` marks a question the mailbox has no answer
    to, where the agent should abstain.
    """

    id: str
    category: str
    question: str
    expected_tools: list[str]
    expected_arguments: dict[str, Any]
    max_calls: int
    required_evidence: list[list[str]] = field(default_factory=list)
    expected_messages: list[str] = field(default_factory=list)
    required_citations: list[list[str]] = field(default_factory=list)
    unanswerable: bool = False
    held_out: bool = False


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
    message_citation_recall: float | None
    abstention_correct: bool
    enumeration_recall: float | None
    exhausted: bool | None
    extra_calls: int
    repeated_calls: int
    held_out: bool = False

    @property
    def failures(self) -> list[str]:
        """Names of the metrics this trace did not get full marks on."""
        failed = []
        for name in ("tool_selected", "arguments_correct", "abstention_correct", "exhausted"):
            if getattr(self, name) is False:
                failed.append(name)
        for name in (
            "evidence_recall",
            "citation_validity",
            "citation_recall",
            "message_citation_recall",
            "enumeration_recall",
        ):
            value = getattr(self, name)
            if value is not None and value < 1.0:
                failed.append(name)
        for name in ("extra_calls", "repeated_calls"):
            if getattr(self, name):
                failed.append(name)
        return failed


def _returned_ids(calls: Sequence[dict]) -> tuple[set[str], dict[str, str], dict[str, str]]:
    """IDs the tool results returned, each ID's thread, and each ID's message.

    A message row carries its thread in a ``thread_id`` beside it or, in
    ``get_thread``, in the ``thread`` summary of the enclosing result, so
    the nearest enclosing thread ID is the message's thread. A row with a
    ``message_id`` names that message by its ``message_id``,
    ``claimant_id`` or ``chunk_id``; a bare ``thread_id`` names no message.
    """
    seen: set[str] = set()
    thread_of: dict[str, str] = {}
    message_of: dict[str, str] = {}

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
        message = value.get("message_id")
        if isinstance(message, str):
            for name in ("message_id", "claimant_id", "chunk_id"):
                item = value.get(name)
                if isinstance(item, str):
                    message_of[item] = message
        for child in value.values():
            visit(child, thread)

    for call in calls:
        visit(call.get("result"), None)
    return seen, thread_of, message_of


def _groups_covered(ids: set[str], groups: list[list[str]]) -> float:
    return evidence_recall(sorted(ids), groups, k=len(ids))


def _query_predicates(arguments: dict[str, Any]) -> dict[str, Any]:
    """The filters of a ``query_messages`` call as the tool reads them
    when it binds a cursor to them: string filters stripped, blank or
    ``None`` ones absent, and the date bounds as their UTC ISO instants.

    Raises ``ValueError`` for a date the tool rejects.
    """
    filters: dict[str, Any] = {}
    for name, value in arguments.items():
        if name in _PAGING_ARGUMENTS:
            continue
        if name in _STRIPPED_FILTERS and isinstance(value, str):
            value = value.strip()
        if value is None or value == "":
            continue
        filters[name] = value
    if "date_from" in filters or "date_to" in filters:
        bounds = _normalize_date_range(filters.pop("date_from", None), filters.pop("date_to", None))
        for name, bound in zip(("date_from", "date_to"), bounds, strict=True):
            if bound is not None:
                filters[name] = bound
    return filters


def _enumeration_chains(calls: Sequence[dict], predicates: dict[str, Any]) -> list[list[dict]]:
    """The ``query_messages`` page results of each cursor chain over ``predicates``.

    Only calls whose filters, normalized as the tool normalizes them
    (``_query_predicates``), are exactly the expected predicates count.
    A call with no cursor starts a chain; one whose cursor is the ``next_cursor`` of the
    last page of any chain so far continues that chain, so starting a
    second chain does not orphan the first; a page with any other cursor
    joins no chain.
    """
    expected = _query_predicates(predicates)
    chains: list[list[dict]] = []
    # Open chains by the cursor that continues them.
    waiting: dict[str, list[dict]] = {}
    for call in calls:
        if call["tool"] != ENUMERATING_TOOL:
            continue
        arguments = call["arguments"]
        try:
            filters = _query_predicates(arguments)
        except ValueError:
            # The tool rejects this date filter, so the call listed nothing.
            continue
        if filters != expected:
            continue
        cursor = arguments.get("cursor")
        if not cursor:
            chain: list[dict] = []
            chains.append(chain)
        elif cursor in waiting:
            chain = waiting.pop(cursor)
        else:
            continue
        chain.append(call["result"])
        next_cursor = call["result"].get("next_cursor")
        if next_cursor:
            waiting[next_cursor] = chain
    return chains


def score_trace(scenario: Scenario, trace: dict) -> AgentScore:
    """Score one trace against its scenario."""
    if trace.get("scenario") != scenario.id:
        raise ValueError(f"trace for {trace.get('scenario')!r} scored against {scenario.id!r}")
    calls: list[dict] = trace.get("calls", [])
    answer: dict = trace.get("answer", {})
    cited: list[str] = answer.get("cited", [])
    abstained = answer.get("abstained") is True
    seen, thread_of, message_of = _returned_ids(calls)

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

    message_citation_recall: float | None = None
    if scenario.required_citations:
        cited_messages = {message_of[c] for c in cited if c in message_of}
        message_citation_recall = _groups_covered(cited_messages, scenario.required_citations)

    # Abstaining means answering nothing, so an abstention citing a source
    # presents that source as support for an answer the mailbox lacks.
    if scenario.unanswerable:
        abstention_correct = abstained and not cited
    else:
        abstention_correct = not abstained

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
        message_citation_recall=message_citation_recall,
        abstention_correct=abstention_correct,
        enumeration_recall=enumeration_recall,
        exhausted=exhausted,
        extra_calls=max(0, len(calls) - scenario.max_calls),
        repeated_calls=repeated,
        held_out=scenario.held_out,
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
    """Aggregate scores, the clean rate per split, then the failing
    scenarios grouped by category (held-out ones tagged)."""

    def present(name: str) -> list:
        return [v for s in scores if (v := getattr(s, name)) is not None]

    lines = [
        f"Agent eval summary ({len(scores)} traces):",
        f"  Tool selection:      {_rate([s.tool_selected for s in scores])}",
        f"  Argument accuracy:   {_rate(present('arguments_correct'))}",
        f"  Evidence recall:     {_mean(present('evidence_recall'))}",
        f"  Citation validity:   {_mean(present('citation_validity'))}",
        f"  Citation recall:     {_mean(present('citation_recall'))}",
        f"  Message citation recall: {_mean(present('message_citation_recall'))}",
        f"  Abstention correct:  {_rate([s.abstention_correct for s in scores])}",
        f"  Enumeration recall:  {_mean(present('enumeration_recall'))}",
        f"  Enumeration exhausted: {_rate(present('exhausted'))}",
        f"  Extra calls:         {sum(s.extra_calls for s in scores)}",
        f"  Repeated calls:      {sum(s.repeated_calls for s in scores)}",
        f"  Dev clean:           {_rate([not s.failures for s in scores if not s.held_out])}",
        f"  Held-out clean:      {_rate([not s.failures for s in scores if s.held_out])}",
    ]
    by_category: dict[str, list[str]] = defaultdict(list)
    for s in scores:
        if s.failures:
            tag = " [held-out]" if s.held_out else ""
            by_category[s.category].append(f"{s.scenario_id}{tag} ({', '.join(s.failures)})")
    if not by_category:
        lines.append("  Failures by category: none")
    else:
        lines.append("  Failures by category:")
        for category in sorted(by_category):
            lines.append(f"    {category}: {'; '.join(by_category[category])}")
    return "\n".join(lines)


def load_scenarios(path: Path, golden_path: Path) -> list[Scenario]:
    """Load scenarios, resolving each one's golden question.

    A row names exactly one of ``golden_search`` (a ``search`` question:
    its ``required_evidence`` and ``filters`` become the scenario's
    evidence and expected arguments), ``golden_enumerate`` (an
    ``enumerate`` question: its ``args`` and ``expect`` become the
    expected arguments and messages) or ``golden_unanswerable`` (an
    ``unanswerable`` question: the scenario expects abstention).
    Inheriting them keeps the scenario on answers ``make baseline``
    checks against a real index.

    A ``golden_search`` row may add ``required_citations``, groups of
    message refs (``"t24.2"``); each message must belong to a thread in
    the golden question's evidence, so the baseline has shown it can be
    found. A row's ``held_out`` flag must agree with ``is_held_out``.
    """
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    searches = {q["id"]: q for q in golden["search"]}
    enumerations = {q["id"]: q for q in golden["enumerate"]}
    unanswerables = {q["id"]: q for q in golden["unanswerable"]}
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
        if row.get("held_out", False) is not is_held_out(sid):
            raise ValueError(f"{sid}: held_out must be {is_held_out(sid)}")
        search_ref = row.get("golden_search")
        enum_ref = row.get("golden_enumerate")
        unanswerable_ref = row.get("golden_unanswerable")
        if sum(ref is not None for ref in (search_ref, enum_ref, unanswerable_ref)) != 1:
            raise ValueError(
                f"{sid}: give one of golden_search, golden_enumerate or golden_unanswerable"
            )
        citation_refs = row.get("required_citations", [])
        if citation_refs and search_ref is None:
            raise ValueError(f"{sid}: required_citations needs golden_search")
        evidence: list[list[str]] = []
        arguments: dict[str, Any] = {}
        messages: list[str] = []
        if search_ref is not None:
            if search_ref not in searches:
                raise ValueError(f"{sid}: no golden search question {search_ref!r}")
            q = searches[search_ref]
            evidence = [[f"{t}.1{_BASELINE_DOMAIN}" for t in g] for g in q["required_evidence"]]
            arguments = dict(q.get("filters", {}))
            evidence_threads = {t for g in q["required_evidence"] for t in g}
            for group in citation_refs:
                for ref in group:
                    # Shape only: ``make baseline`` checks each ref is an
                    # indexed message (test_agent_required_citations_exist).
                    if not _MESSAGE_REF.fullmatch(ref):
                        raise ValueError(f"{sid}: {ref!r} is not a message ref like 't24.2'")
                    if ref.split(".")[0] not in evidence_threads:
                        raise ValueError(
                            f"{sid}: cited message {ref!r} is outside {search_ref!r}'s evidence"
                        )
        elif enum_ref is not None:
            if enum_ref not in enumerations:
                raise ValueError(f"{sid}: no golden enumerate question {enum_ref!r}")
            q = enumerations[enum_ref]
            arguments = dict(q["args"])
            messages = [f"{m}{_BASELINE_DOMAIN}" for m in q["expect"]]
        elif unanswerable_ref not in unanswerables:
            raise ValueError(f"{sid}: no golden unanswerable question {unanswerable_ref!r}")
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
                required_citations=[
                    [f"{m}{_BASELINE_DOMAIN}" for m in group] for group in citation_refs
                ],
                unanswerable=unanswerable_ref is not None,
                held_out=is_held_out(sid),
            )
        )
    return scenarios
