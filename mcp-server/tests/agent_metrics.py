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
  counting as its message, and only when a result returned that
  message's content (``_content_reads``: its whole body, or an
  attachment passage; a listing or a body passage reads nothing, #804).
  A correction scenario requires the
  correcting message, which thread-level citation recall cannot tell
  from the message it corrects; a conflicting-sources scenario requires
  each side of the disagreement;
- **abstention**: an unanswerable scenario passes when some call's
  string argument contains one of its ``abstain_terms`` (the agent
  looked for the missing fact), the answer sets ``"abstained": true``
  and it cites nothing; any other scenario passes when the answer does
  not abstain;
- **enumeration completeness**: for an exhaustive question, the
  fraction of expected messages listed by one ``query_messages``
  cursor chain over exactly the expected filters (any page size), and
  whether that chain's last page said ``has_more: false``;
- **unnecessary calls**: calls over the scenario's budget, and calls
  identical (tool and arguments) to an earlier one;
- **counting** (scenarios listing ``expected_answer_messages``): whether
  the answer cites exactly those messages (a decoy cited, a message
  left out or a cited message whose content no result returned fails;
  two IDs of one message count once), and whether its
  ``count`` is a JSON integer equal to their number;
- **full reads**: the fraction of ``full_read_messages`` whose body the
  ``get_message`` results cover from offset 0 through each
  ``next_offset`` to a page with none;
- **forbidden text**: whether ``answer.text`` is free of every
  ``forbidden_answer_text`` string (case-insensitive substring; a value
  reformatted with spaces or dashes is not caught);
- **outstanding items** (scenarios with an ``outstanding`` ground
  truth, #798): action recall and precision, owner and status
  accuracy, supported closures and deadlines, conclusion citation
  support and required evidence coverage (both counting only sources a
  result returned the content of, ``_content_reads``), forbidden sources
  avoided and
  completeness-claim truthfulness (``_score_outstanding``).

Nothing here judges whether the answer's prose is right: answer quality
is graded by hand (``tests/eval/README.md``). A valid citation shows
the agent saw the source, not that the source supports the statement.

A trace is JSON: ``{"scenario": id, "calls": [{"tool", "arguments",
"result"}], "answer": {"text", "cited": [ids], "abstained": bool,
"count": int}}`` where ``result`` is the call's ``structuredContent``,
``abstained`` (optional, false when absent) is the agent's structured
statement that the mailbox does not answer the question, and ``count``
(counting scenarios) is the number of messages the answer reports. IDs
are read from the result fields
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
# ``get_message`` fields that say which body page a result holds: the
# message, the page's text (``None`` when no body is indexed), the page's
# start and the next page's start (``None`` at the end).
READ_FIELDS = ("message", "body", "body_offset", "next_offset")
READING_TOOL = "get_message"
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
class OutstandingAction:
    """One next action in an outstanding-items ground truth (#798).

    ``status`` is ``open``, ``waiting``, ``disputed``, ``unknown`` (all
    outstanding) or ``closed`` (to be excluded). ``due`` is the due date
    the mail supports (ISO date), or ``None`` when it supports none.
    ``required_sources`` are message IDs a conclusion about the action
    must cite (any one of them); ``superseded_sources`` hold evidence a
    later message replaced, which never supports it. ``known_loss`` names
    the issue under which the tools cannot return the action's evidence;
    such an action counts as found when the answer lists it or names one
    of its sources among its limitations.
    """

    id: str
    owner: str
    status: str
    due: str | None
    required_sources: list[str]
    superseded_sources: list[str] = field(default_factory=list)
    known_loss: str | None = None

    @property
    def outstanding(self) -> bool:
        return self.status != "closed"


@dataclass(frozen=True)
class OutstandingTruth:
    """Ground truth of an outstanding-items scenario.

    ``forbidden_sources`` are messages no conclusion may cite (a decoy
    sharing counsel's display name, a prompt injection, mail outside the
    window). ``completeness_blockers`` are messages whose decisive
    content the tools cannot return (a failed attachment extraction,
    text the indexer stripped): an answer must name each among its
    limitations and must not claim to be complete.
    """

    actions: list[OutstandingAction]
    forbidden_sources: list[str] = field(default_factory=list)
    completeness_blockers: list[str] = field(default_factory=list)
    # Sources whose decisive text is in an attachment, so only an
    # attachment passage of theirs reads it (``_content_reads``).
    attachment_sources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Scenario:
    """One question an agent should answer with the tools.

    ``required_evidence`` uses the groups of ``retrieval_metrics``: every
    group is required, any thread ID in a group satisfies it.
    ``expected_messages`` lists the message IDs an exhaustive question
    must enumerate. Either may be empty. ``required_citations`` groups
    message IDs the answer must cite the same way (every group, any ID
    in it). ``unanswerable`` marks a question the mailbox has no answer
    to, where the agent should abstain after asking for one of the
    ``abstain_terms``.
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
    abstain_terms: list[str] = field(default_factory=list)
    held_out: bool = False
    expected_answer_messages: list[str] = field(default_factory=list)
    full_read_messages: list[str] = field(default_factory=list)
    forbidden_answer_text: list[str] = field(default_factory=list)
    outstanding: OutstandingTruth | None = None


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
    answer_messages_exact: bool | None = None
    answer_count_correct: bool | None = None
    full_read_recall: float | None = None
    forbidden_text_absent: bool | None = None
    action_recall: float | None = None
    action_precision: float | None = None
    owner_accuracy: float | None = None
    status_accuracy: float | None = None
    closures_supported: bool | None = None
    deadlines_supported: bool | None = None
    conclusion_citation_support: float | None = None
    required_evidence_coverage: float | None = None
    forbidden_sources_avoided: bool | None = None
    citations_consistent: bool | None = None
    completeness_claim_truthful: bool | None = None

    @property
    def failures(self) -> list[str]:
        """Names of the metrics this trace did not get full marks on."""
        failed = []
        for name in (
            "tool_selected",
            "arguments_correct",
            "abstention_correct",
            "exhausted",
            "answer_messages_exact",
            "answer_count_correct",
            "forbidden_text_absent",
            "closures_supported",
            "deadlines_supported",
            "forbidden_sources_avoided",
            "completeness_claim_truthful",
            "citations_consistent",
        ):
            if getattr(self, name) is False:
                failed.append(name)
        for name in (
            "evidence_recall",
            "citation_validity",
            "citation_recall",
            "message_citation_recall",
            "enumeration_recall",
            "full_read_recall",
            "action_recall",
            "action_precision",
            "owner_accuracy",
            "status_accuracy",
            "conclusion_citation_support",
            "required_evidence_coverage",
        ):
            value = getattr(self, name)
            if value is not None and value < 1.0:
                failed.append(name)
        for name in ("extra_calls", "repeated_calls"):
            if getattr(self, name):
                failed.append(name)
        return failed


def _returned_ids(
    calls: Sequence[dict],
) -> tuple[set[str], dict[str, str], dict[str, str], dict[str, set[str]]]:
    """IDs the tool results returned, each ID's thread, each ID's message,
    and the claimant IDs each ID was returned with.

    A message row carries its thread in a ``thread_id`` beside it or, in
    ``get_thread``, in the ``thread`` summary of the enclosing result, so
    the nearest enclosing thread ID is the message's thread. A row with a
    ``message_id`` names that message by its ``message_id``,
    ``claimant_id`` or ``chunk_id``; a bare ``thread_id`` names no message.
    Two files can claim one Message-ID (#217), so a bare ``message_id``
    may name several claimants while a claimant or chunk ID names one.
    """
    seen: set[str] = set()
    thread_of: dict[str, str] = {}
    message_of: dict[str, str] = {}
    claimants_of: dict[str, set[str]] = defaultdict(set)

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
            claimant = value.get("claimant_id")
            for name in ("message_id", "claimant_id", "chunk_id"):
                item = value.get(name)
                if isinstance(item, str):
                    message_of[item] = message
                    if isinstance(claimant, str):
                        claimants_of[item].add(claimant)
        for child in value.values():
            visit(child, thread)

    for call in calls:
        visit(call.get("result"), None)
    return seen, thread_of, message_of, dict(claimants_of)


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


def _fully_read(calls: Sequence[dict]) -> dict[str, str]:
    """The message ID, by claimant ID, of each message whose body some
    ``get_message`` results cover from the first page to the last.

    Pages are read from the results (``READ_FIELDS``), never from the
    arguments, and chained per claimant ID: the page at offset 0, then the
    page starting at its ``next_offset``, and so on until a page whose
    ``next_offset`` is ``None``. A skipped or missing page breaks the chain.
    A page counts only when it returned body text (``body`` a string): a
    message with no indexed body answers ``body: null`` at offset 0 with
    no next page, and reads nothing.
    """
    pages: dict[str, dict[int, int | None]] = defaultdict(dict)
    message_of: dict[str, str] = {}
    for call in calls:
        if call["tool"] != READING_TOOL:
            continue
        result = call.get("result") or {}
        message = result.get("message")
        offset = result.get("body_offset")
        if (
            not isinstance(message, dict)
            or not isinstance(result.get("body"), str)
            or not isinstance(offset, int)
            or "next_offset" not in result
        ):
            continue
        claimant, message_id = message.get("claimant_id"), message.get("message_id")
        if isinstance(claimant, str) and isinstance(message_id, str):
            pages[claimant][offset] = result["next_offset"]
            message_of[claimant] = message_id
    read: dict[str, str] = {}
    for claimant, chain in pages.items():
        start: int | None = 0
        # Each step moves forward, so the walk ends within len(chain) steps.
        while isinstance(start, int) and start in chain:
            following = chain[start]
            if following is None:
                read[claimant] = message_of[claimant]
                break
            start = following if isinstance(following, int) and following > start else None
    return read


def _content_reads(calls: Sequence[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """The message ID, by claimant ID, of each message whose whole body,
    and of each with an attachment passage, some result returned.

    A listing (``query_messages``, ``list_threads``, ``find_contact``,
    ``search_emails``) names messages but returns no content, so it reads
    nothing. A body is read when ``get_message`` pages it from offset 0 to
    the end (``_fully_read``) or a ``get_thread`` row returns it uncut
    (``body`` a string, ``body_omitted_chars`` 0): evidence past a cut needs
    ``get_message``. Attachment text is read only through a ``get_evidence``
    passage whose ``source`` is ``attachment``: no tool returns a whole
    attachment (#796), and a ``search_attachments`` snippet is a preview
    the scorer cannot check holds the evidence, since traces carry IDs, not
    text. Reads are keyed by claimant ID (#217): reading one file that
    claims a Message-ID says nothing about another file claiming it.
    """
    bodies = _fully_read(calls)
    attachments: dict[str, str] = {}
    for call in calls:
        result = call.get("result") or {}
        if call["tool"] == "get_thread":
            for row in result.get("messages", []):
                if (
                    isinstance(row, dict)
                    and isinstance(row.get("message_id"), str)
                    and isinstance(row.get("claimant_id"), str)
                    and isinstance(row.get("body"), str)
                    and row.get("body_omitted_chars") == 0
                ):
                    bodies[row["claimant_id"]] = row["message_id"]
        elif call["tool"] == "get_evidence":
            for thread in result.get("threads", []):
                for chunk in thread.get("chunks", []) if isinstance(thread, dict) else []:
                    if (
                        isinstance(chunk, dict)
                        and chunk.get("source") == "attachment"
                        and isinstance(chunk.get("message_id"), str)
                        and isinstance(chunk.get("claimant_id"), str)
                    ):
                        attachments[chunk["claimant_id"]] = chunk["message_id"]
    return bodies, attachments


def _score_outstanding(
    truth: OutstandingTruth,
    answer: dict,
    all_cited: list[str],
    calls: Sequence[dict],
    message_of: dict[str, str],
    claimants_of: dict[str, set[str]],
    full_read_recall: float | None,
) -> dict[str, Any]:
    """The outstanding-items scores of ``answer`` (see ``score_trace``).

    The answer lists ``items`` (outstanding next actions) and
    ``excluded`` (actions it found closed), each naming its ground-truth
    ``action`` and the IDs it ``cited``; items also give ``owner``,
    ``status`` and ``due``. ``complete`` is the answer's claim that
    nothing was left unread, and ``limitations`` names the messages it
    could not read. A cited or named ID counts as the message a tool
    result returned it with; anything else names nothing.
    """
    by_id = {a.id: a for a in truth.actions}
    outstanding = [a for a in truth.actions if a.outstanding]
    items: list[dict] = answer.get("items", [])
    excluded: list[dict] = answer.get("excluded", [])

    def messages(ids: list[str]) -> set[str]:
        return {message_of[i] for i in ids if i in message_of}

    limitations = messages(answer.get("limitations", []))

    # Each outstanding action once; a second item for it (a duplicate task
    # from a request quoted in several replies) or an item for a closed,
    # unknown or forbidden action is a false item.
    first_item: dict[str, dict] = {}
    for item in items:
        action = by_id.get(item.get("action", ""))
        if action is not None and action.outstanding and action.id not in first_item:
            first_item[action.id] = item
    found = [
        a
        for a in outstanding
        if a.id in first_item or (a.known_loss and limitations & set(a.required_sources))
    ]
    matched = [(by_id[a], item) for a, item in first_item.items()]

    # Closing an outstanding action: listing it as excluded, or as an item
    # whose status says closed.
    closures_supported = not any(
        (a := by_id.get(entry.get("action", ""))) is not None and a.outstanding
        for entry in excluded
    ) and not any(item.get("status") == "closed" for _, item in matched)

    # A due date the ground truth does not hold (a superseded one, or one
    # on an action with none) is invented; leaving one out is not.
    deadlines_supported = all(
        item.get("due") is None
        or ((a := by_id.get(item.get("action", ""))) is not None and item["due"] == a.due)
        for item in items
    )

    # A source counts as read only when a result returned its content: an
    # attachment passage for an attachment source, the whole body for any
    # other (``_content_reads``). A listing that only names it reads nothing.
    bodies, attachment_passages = _content_reads(calls)
    attachment_sources = set(truth.attachment_sources)

    def reads_of(message: str) -> dict[str, str]:
        return attachment_passages if message in attachment_sources else bodies

    read = {m for a in truth.actions for m in a.required_sources if m in set(reads_of(m).values())}

    # Every conclusion must cite a required source of its action that the
    # trace read; a superseded source is not one. The read must be of the
    # file the citation names (#217): a cited claimant or passage needs its
    # own claimant read, a bare Message-ID any file claiming it.
    def source_read(cited_id: str, required: set[str]) -> bool:
        message = message_of.get(cited_id)
        if message is None or message not in required:
            return False
        return bool(claimants_of.get(cited_id, set()) & set(reads_of(message)))

    conclusions = items + excluded
    supported = sum(
        1
        for entry in conclusions
        if (a := by_id.get(entry.get("action", ""))) is not None
        and any(source_read(c, set(a.required_sources)) for c in entry.get("cited", []))
    )
    # The canonical citation set (top-level and per-conclusion, see
    # ``score_trace``), so a forbidden source cited anywhere counts.
    cited = messages(all_cited)

    # Every required source the tools can return must have been read; one
    # named only on a later query page is read only after that page.
    required = {m for a in truth.actions if not a.known_loss for m in a.required_sources}
    coverage = len(required & read) / len(required) if required else 1.0

    blockers = set(truth.completeness_blockers)
    if answer.get("complete") is True:
        truthful = not blockers and coverage == 1.0 and full_read_recall in (None, 1.0)
    else:
        truthful = blockers <= limitations

    return {
        "action_recall": len(found) / len(outstanding) if outstanding else 1.0,
        "action_precision": len(first_item) / len(items) if items else None,
        "owner_accuracy": (
            sum(item.get("owner") == a.owner for a, item in matched) / len(matched)
            if matched
            else None
        ),
        "status_accuracy": (
            sum(item.get("status") == a.status for a, item in matched) / len(matched)
            if matched
            else None
        ),
        "closures_supported": closures_supported,
        "deadlines_supported": deadlines_supported,
        "conclusion_citation_support": supported / len(conclusions) if conclusions else None,
        "required_evidence_coverage": coverage,
        "forbidden_sources_avoided": not cited & set(truth.forbidden_sources),
        "completeness_claim_truthful": truthful,
    }


def score_trace(scenario: Scenario, trace: dict) -> AgentScore:
    """Score one trace against its scenario."""
    if trace.get("scenario") != scenario.id:
        raise ValueError(f"trace for {trace.get('scenario')!r} scored against {scenario.id!r}")
    calls: list[dict] = trace.get("calls", [])
    answer: dict = trace.get("answer", {})
    cited: list[str] = answer.get("cited", [])
    # An outstanding-items answer cites in two places: the top-level list
    # and each conclusion's own list. Invariant: the two are one set, and
    # every citation metric reads that set (their union), so a citation
    # in only one place is both a ``citations_consistent`` failure and
    # still scored (validity, forbidden sources).
    citations_consistent: bool | None = None
    if scenario.outstanding is not None:
        own = [
            c
            for entry in answer.get("items", []) + answer.get("excluded", [])
            for c in entry.get("cited", [])
        ]
        citations_consistent = set(cited) == set(own)
        cited = list(dict.fromkeys(cited + own))
    abstained = answer.get("abstained") is True
    seen, thread_of, message_of, claimants_of = _returned_ids(calls)

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

    # A cited message counts for the message-level scorers only when a
    # result returned its content (``_content_reads``, as for outstanding
    # items): a listing or a body passage that named it reads nothing.
    # Reads and citations are matched by claimant ID (#217): a cited
    # claimant or passage needs its own file read, while a bare Message-ID
    # names every file claiming it, so any of their reads covers it.
    bodies, attachment_passages = _content_reads(calls)
    read_claimants = set(bodies) | set(attachment_passages)

    def content_read(cited_id: str) -> bool:
        return bool(claimants_of.get(cited_id, set()) & read_claimants)

    message_citation_recall: float | None = None
    if scenario.required_citations:
        cited_messages = {message_of[c] for c in cited if c in message_of and content_read(c)}
        message_citation_recall = _groups_covered(cited_messages, scenario.required_citations)

    # Abstaining means answering nothing, so an abstention citing a source
    # presents that source as support for an answer the mailbox lacks. It
    # also has to follow a lookup for the missing fact: refusing after an
    # unrelated search is not evidence that the mailbox lacks the answer.
    if scenario.unanswerable:
        looked = any(
            term.casefold() in value.casefold()
            for call in calls
            for value in call["arguments"].values()
            if isinstance(value, str)
            for term in scenario.abstain_terms
        )
        abstention_correct = abstained and not cited and looked
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

    # A counting answer must cite exactly the expected messages: a decoy
    # cited or a message left out fails. A cited ID counts as the message
    # a result returned it with, so two IDs of one message are one message,
    # and only when that message's content was read (``content_read``
    # above). An ID no result returned as a message fails, even when it
    # equals a returned thread ID: a root's Message-ID is also its thread's ID.
    answer_messages_exact: bool | None = None
    answer_count_correct: bool | None = None
    if scenario.expected_answer_messages:
        expected_answers = set(scenario.expected_answer_messages)
        answer_messages_exact = (
            all(c in message_of and content_read(c) for c in cited)
            and {message_of[c] for c in cited} == expected_answers
        )
        count = answer.get("count")
        # Only a JSON integer counts (``True`` is an int in Python).
        answer_count_correct = (
            isinstance(count, int)
            and not isinstance(count, bool)
            and count == len(expected_answers)
        )

    full_read_recall: float | None = None
    if scenario.full_read_messages:
        read = set(_fully_read(calls).values())
        full_read_recall = sum(m in read for m in scenario.full_read_messages) / len(
            scenario.full_read_messages
        )

    forbidden_text_absent: bool | None = None
    if scenario.forbidden_answer_text:
        text = str(answer.get("text", "")).casefold()
        forbidden_text_absent = not any(
            value.casefold() in text for value in scenario.forbidden_answer_text
        )

    outstanding: dict[str, Any] = {}
    if scenario.outstanding is not None:
        outstanding = _score_outstanding(
            scenario.outstanding, answer, cited, calls, message_of, claimants_of, full_read_recall
        )

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
        answer_messages_exact=answer_messages_exact,
        answer_count_correct=answer_count_correct,
        full_read_recall=full_read_recall,
        forbidden_text_absent=forbidden_text_absent,
        citations_consistent=citations_consistent,
        **outstanding,
    )


def _rate(values: list[bool]) -> str:
    if not values:
        return "n/a"
    return f"{sum(values) / len(values):.2%} ({sum(values)}/{len(values)})"


def _mean(values: list[float]) -> str:
    if not values:
        return "n/a"
    return f"{sum(values) / len(values):.2%} (mean over {len(values)})"


def _aggregates(scores: Sequence[AgentScore]) -> list[str]:
    def present(name: str) -> list:
        return [v for s in scores if (v := getattr(s, name)) is not None]

    return [
        f"Tool selection:      {_rate([s.tool_selected for s in scores])}",
        f"Argument accuracy:   {_rate(present('arguments_correct'))}",
        f"Evidence recall:     {_mean(present('evidence_recall'))}",
        f"Citation validity:   {_mean(present('citation_validity'))}",
        f"Citation recall:     {_mean(present('citation_recall'))}",
        f"Message citation recall: {_mean(present('message_citation_recall'))}",
        f"Abstention correct:  {_rate([s.abstention_correct for s in scores])}",
        f"Enumeration recall:  {_mean(present('enumeration_recall'))}",
        f"Enumeration exhausted: {_rate(present('exhausted'))}",
        f"Answer set exact:    {_rate(present('answer_messages_exact'))}",
        f"Answer count correct: {_rate(present('answer_count_correct'))}",
        f"Full-read recall:    {_mean(present('full_read_recall'))}",
        f"Forbidden text absent: {_rate(present('forbidden_text_absent'))}",
        f"Action recall:       {_mean(present('action_recall'))}",
        f"Action precision:    {_mean(present('action_precision'))}",
        f"Owner accuracy:      {_mean(present('owner_accuracy'))}",
        f"Status accuracy:     {_mean(present('status_accuracy'))}",
        f"Closures supported:  {_rate(present('closures_supported'))}",
        f"Deadlines supported: {_rate(present('deadlines_supported'))}",
        f"Conclusion citation support: {_mean(present('conclusion_citation_support'))}",
        f"Required evidence coverage: {_mean(present('required_evidence_coverage'))}",
        f"Forbidden sources avoided: {_rate(present('forbidden_sources_avoided'))}",
        f"Completeness claim truthful: {_rate(present('completeness_claim_truthful'))}",
        f"Citations consistent: {_rate(present('citations_consistent'))}",
        f"Extra calls:         {sum(s.extra_calls for s in scores)}",
        f"Repeated calls:      {sum(s.repeated_calls for s in scores)}",
        f"Clean:               {_rate([not s.failures for s in scores])}",
    ]


def summarize(scores: Sequence[AgentScore]) -> str:
    """Aggregate scores for each split apart, then the failing scenarios
    grouped by category (held-out ones tagged).

    Every aggregate is per split, so tuning against the dev block never
    reads a held-out outcome.
    """
    lines = [f"Agent eval summary ({len(scores)} traces):"]
    for label, held_out in (("Dev", False), ("Held-out", True)):
        split = [s for s in scores if s.held_out is held_out]
        if not split:
            lines.append(f"  {label} split (0 traces): none")
            continue
        lines.append(f"  {label} split ({len(split)} traces):")
        lines.extend(f"    {line}" for line in _aggregates(split))
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

    A counting row lists ``expected_answer_messages`` (message refs) and
    may then name no golden question; ``full_read_messages`` must be
    among them, and ``forbidden_answer_text`` must be non-blank strings.
    ``make baseline`` checks these against the index.

    An ``outstanding_items`` row names no golden question and no
    counting fields: its ground truth, full reads included, is read from
    ``OUTSTANDING_TRUTH_FILE`` beside ``path`` (``_load_outstanding``).
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
        answer_refs = _message_refs(sid, row, "expected_answer_messages")
        full_read_refs = _message_refs(sid, row, "full_read_messages")
        if not set(full_read_refs) <= set(answer_refs):
            raise ValueError(f"{sid}: full_read_messages must be in expected_answer_messages")
        forbidden = row.get("forbidden_answer_text", [])
        if not isinstance(forbidden, list) or not all(
            isinstance(text, str) and text.strip() for text in forbidden
        ):
            raise ValueError(f"{sid}: forbidden_answer_text must be non-blank strings")
        golden_refs = sum(ref is not None for ref in (search_ref, enum_ref, unanswerable_ref))
        outstanding: OutstandingTruth | None = None
        full_read_ids = [f"{m}{_BASELINE_DOMAIN}" for m in full_read_refs]
        if row["category"] == OUTSTANDING_CATEGORY:
            if golden_refs or answer_refs or full_read_refs:
                raise ValueError(
                    f"{sid}: an outstanding-items scenario takes its truth from "
                    f"{OUTSTANDING_TRUTH_FILE} only"
                )
            outstanding, full_read_ids = _load_outstanding(
                sid, path.parent / OUTSTANDING_TRUTH_FILE
            )
        # A counting scenario's answer set, and an outstanding-items
        # scenario's truth, are checked against the index directly
        # (tests/baseline), so neither needs a golden question.
        if golden_refs > 1 or (golden_refs == 0 and not answer_refs and outstanding is None):
            raise ValueError(
                f"{sid}: give one of golden_search, golden_enumerate or golden_unanswerable"
            )
        citation_refs = row.get("required_citations", [])
        if citation_refs and search_ref is None:
            raise ValueError(f"{sid}: required_citations needs golden_search")
        evidence: list[list[str]] = []
        arguments: dict[str, Any] = {}
        messages: list[str] = []
        abstain_terms: list[str] = []
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
        elif unanswerable_ref is not None:
            if unanswerable_ref not in unanswerables:
                raise ValueError(f"{sid}: no golden unanswerable question {unanswerable_ref!r}")
            abstain_terms = unanswerables[unanswerable_ref]["absent_terms"]
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
                abstain_terms=list(abstain_terms),
                held_out=is_held_out(sid),
                expected_answer_messages=[f"{m}{_BASELINE_DOMAIN}" for m in answer_refs],
                full_read_messages=full_read_ids,
                forbidden_answer_text=list(forbidden),
                outstanding=outstanding,
            )
        )
    return scenarios


OUTSTANDING_CATEGORY = "outstanding_items"
# Beside the scenario file; never part of what an answering agent reads.
OUTSTANDING_TRUTH_FILE = "outstanding_items.json"
OWNERS = frozenset(
    {
        "avery_cole",
        "blair_reed",
        "sasha_ortiz",
        "emery_vance",
        "jordan",
        "management",
        "jordan_or_management",
        "unknown",
    }
)
STATUSES = frozenset({"open", "waiting", "disputed", "unknown", "closed"})
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def load_outstanding_truth(path: Path, sid: str) -> dict:
    """The raw ground-truth entry for scenario ``sid`` (layer A reads its
    ``evidence``; ``_load_outstanding`` the rest)."""
    truth = json.loads(path.read_text(encoding="utf-8"))["scenarios"]
    if sid not in truth:
        raise ValueError(f"{sid}: no ground truth in {path.name}")
    return truth[sid]


def _load_outstanding(sid: str, path: Path) -> tuple[OutstandingTruth, list[str]]:
    """Scenario ``sid``'s ground truth and full-read message IDs, validated
    for shape: every ref a message ref, every owner and status from the
    fixed vocabulary, every due date an ISO date, every action ID unique.
    ``make baseline`` checks the refs against the index."""
    entry = load_outstanding_truth(path, sid)

    def ids(refs: object, name: str) -> list[str]:
        if not isinstance(refs, list) or not all(
            isinstance(r, str) and _MESSAGE_REF.fullmatch(r) for r in refs
        ):
            raise ValueError(f"{sid}: {name} must be a list of message refs like 't24.2'")
        return [f"{r}{_BASELINE_DOMAIN}" for r in refs]

    actions = []
    for row in entry["actions"]:
        aid = row["id"]
        if row["owner"] not in OWNERS or row["status"] not in STATUSES:
            raise ValueError(f"{sid}: {aid} has an unknown owner or status")
        due = row["due"]
        if due is not None and not (isinstance(due, str) and _ISO_DATE.fullmatch(due)):
            raise ValueError(f"{sid}: {aid} due must be an ISO date or null")
        required = ids(row["required_sources"], f"{aid} required_sources")
        if not required:
            raise ValueError(f"{sid}: {aid} needs a required source")
        actions.append(
            OutstandingAction(
                id=aid,
                owner=row["owner"],
                status=row["status"],
                due=due,
                required_sources=required,
                superseded_sources=ids(row.get("superseded_sources", []), f"{aid} superseded"),
                known_loss=row.get("known_loss"),
            )
        )
    if len({a.id for a in actions}) != len(actions):
        raise ValueError(f"{sid}: duplicate action id")
    truth = OutstandingTruth(
        actions=actions,
        forbidden_sources=ids(entry["forbidden_sources"], "forbidden_sources"),
        completeness_blockers=ids(entry["completeness_blockers"], "completeness_blockers"),
        # The evidence list says which passages sit in an attachment.
        attachment_sources=ids(
            [e["ref"] for e in entry.get("evidence", []) if e.get("source") == "attachment"],
            "attachment evidence refs",
        ),
    )
    return truth, ids(entry["full_read_messages"], "full_read_messages")


def _message_refs(sid: str, row: dict, name: str) -> list[str]:
    """Row field ``name``: absent, or a non-empty list of message refs
    like ``"t41.1"``. Shape only: ``make baseline`` checks each is an
    indexed message (test_agent_required_citations_exist)."""
    refs = row.get(name)
    if refs is None:
        return []
    if not isinstance(refs, list) or not refs:
        raise ValueError(f"{sid}: {name} must be a non-empty list of message refs")
    for ref in refs:
        if not isinstance(ref, str) or not _MESSAGE_REF.fullmatch(ref):
            raise ValueError(f"{sid}: {ref!r} is not a message ref like 't24.2'")
    return list(refs)
