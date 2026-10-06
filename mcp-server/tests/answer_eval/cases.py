"""Versioned case schema for the answer-quality evaluation.

A case names the tool call to make (``ask_mailbox`` and its exact
arguments), the evidence an answer needs, independently verified
expected facts with the excerpts of the synthetic corpus that establish
them, machine-checkable values, and which rubric dimensions apply. The
split rule is the agent scenarios' (``tests/agent_metrics.is_held_out``):
membership is fixed by the case ID, so adding cases never moves one.

Refs follow the baseline's convention: ``t24`` is the thread rooted at
``t24.1@baseline.example`` and ``t24.2`` the message
``t24.2@baseline.example``. They are resolved against the built index
(``tests/baseline/test_answer_eval_cases.py``), never assumed.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.lib.inference import MIN_PROMPT_TOKENS

from tests.agent_metrics import is_held_out

CASES_SCHEMA_VERSION = 1
CASES_PATH = Path(__file__).with_name("cases.json")
BASELINE_DOMAIN = "@baseline.example"

# Rubric dimensions; a case says which apply (see judge.RUBRIC).
DIMENSIONS = (
    "factual_correctness",
    "citation_support",
    "completeness",
    "temporal_reasoning",
    "conflict_uncertainty",
    "relevance",
)
CATEGORIES = frozenset(
    {
        "exact_fact",
        "attachment_only",
        "multi_thread",
        "narrow_filter",
        "correction",
        "conflicting_sources",
        "unanswerable",
        "empty_result",
        "prompt_budget",
        "injection_answerer",
        "injection_judge",
    }
)
HANDLINGS = frozenset({"answer", "disclose_conflict", "disclose_missing", "abstain"})
REVIEW_STATES = frozenset({"ai_drafted", "owner_verified"})
_ARGUMENTS = frozenset({"question", "from_addr", "date_from", "date_to", "folders", "max_threads"})
_CASE_ID = re.compile(r"ask-[a-z0-9]+(?:-[a-z0-9]+)*")
CASE_ID_MAX_LEN = 64
_FACT_ID = re.compile(r"f[1-9][0-9]*")
_REF = re.compile(r"t[0-9]{2}(?:\.[1-9][0-9]*)?")


class CaseError(ValueError):
    """A case file that breaks the schema. Messages name case IDs and
    fields only (both ours), never case text."""


def is_case_id(value: object) -> bool:
    """A well-formed case ID: the one rule for case files and for the
    reports ``compare`` reads (#771)."""
    return (
        isinstance(value, str)
        and len(value) <= CASE_ID_MAX_LEN
        and _CASE_ID.fullmatch(value) is not None
    )


def thread_id_of(ref: str) -> str:
    """``t24`` or ``t24.2`` -> ``t24.1@baseline.example`` (the thread's root)."""
    return f"{ref.split('.')[0]}.1{BASELINE_DOMAIN}"


def message_id_of(ref: str) -> str | None:
    """``t24.2`` -> ``t24.2@baseline.example``; ``None`` for a thread ref."""
    return f"{ref}{BASELINE_DOMAIN}" if "." in ref else None


@dataclass(frozen=True)
class Fact:
    id: str
    fact: str
    sources: tuple[str, ...]
    excerpt: str
    # Whole values an answer states only by asserting this fact (or
    # something derived from it); a disclose_missing grade uses them to
    # tell a disclosed omission from a guess. Optional.
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    tool: str
    arguments: dict[str, Any]
    held_out: bool
    answerable: bool
    expected_handling: str
    required_evidence: tuple[tuple[str, ...], ...]
    expected_facts: tuple[Fact, ...]
    must_not_assert: tuple[str, ...]
    must_include: tuple[tuple[str, ...], ...]
    must_not_include: tuple[str, ...]
    criteria: dict[str, bool]
    review: str
    prompt_tokens: int | None = None
    golden_unanswerable: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def question(self) -> str:
        return str(self.arguments["question"])


def _require(cond: bool, case_id: str, what: str) -> None:
    if not cond:
        raise CaseError(f"case {case_id}: {what}")


def _str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def _refs(value: object) -> bool:
    return isinstance(value, list) and _str_list(value) and all(_REF.fullmatch(v) for v in value)


def _parse_case(row: dict[str, Any]) -> Case:
    cid = row.get("id")
    if not is_case_id(cid):
        raise CaseError("a case has a missing or malformed id")
    _require(row.get("tool") == "ask_mailbox", cid, "tool must be ask_mailbox")
    _require(row.get("category") in CATEGORIES, cid, "unknown category")
    args = row.get("arguments")
    _require(isinstance(args, dict) and set(args) <= _ARGUMENTS, cid, "bad arguments keys")
    _require(isinstance(args.get("question"), str) and args["question"].strip(), cid, "question")
    _require(row.get("held_out") is is_held_out(cid), cid, "held_out must equal is_held_out(id)")
    answerable = row.get("answerable")
    _require(isinstance(answerable, bool), cid, "answerable must be a boolean")
    handling = row.get("expected_handling")
    _require(handling in HANDLINGS, cid, "unknown expected_handling")
    _require((handling == "abstain") is (not answerable), cid, "abstain iff not answerable")

    groups = row.get("required_evidence")
    _require(isinstance(groups, list) and all(_refs(g) and g for g in groups), cid, "evidence")
    _require(bool(groups) is answerable, cid, "answerable cases, and only they, need evidence")

    facts_raw = row.get("expected_facts")
    _require(isinstance(facts_raw, list), cid, "expected_facts must be a list")
    facts = []
    for f in facts_raw:
        _require(isinstance(f, dict) and _FACT_ID.fullmatch(str(f.get("id"))), cid, "fact id")
        _require(isinstance(f.get("fact"), str) and f["fact"].strip(), cid, "fact text")
        _require(_refs(f.get("sources")) and f["sources"], cid, "fact sources")
        _require(isinstance(f.get("excerpt"), str) and f["excerpt"].strip(), cid, "fact excerpt")
        values = f.get("values", [])
        _require(isinstance(values, list) and (not values or _str_list(values)), cid, "values")
        facts.append(Fact(f["id"], f["fact"], tuple(f["sources"]), f["excerpt"], tuple(values)))
    _require(len({f.id for f in facts}) == len(facts), cid, "duplicate fact ids")
    _require(bool(facts) is answerable, cid, "answerable cases, and only they, need facts")

    _require(_str_list(row.get("must_not_assert")), cid, "must_not_assert")
    det = row.get("deterministic")
    _require(isinstance(det, dict), cid, "deterministic must be an object")
    include = det.get("must_include")
    _require(isinstance(include, list) and all(_str_list(g) and g for g in include), cid, "include")
    _require(_str_list(det.get("must_not_include")), cid, "must_not_include")

    criteria = row.get("criteria")
    _require(isinstance(criteria, dict) and set(criteria) == set(DIMENSIONS), cid, "criteria keys")
    _require(all(isinstance(v, bool) for v in criteria.values()), cid, "criteria values")
    _require(criteria["relevance"] is True, cid, "relevance always applies")
    if not answerable or handling != "answer":
        # Abstention and disclosure are what these cases test.
        _require(criteria["conflict_uncertainty"] is True, cid, "conflict_uncertainty applies")
    _require(row.get("review") in REVIEW_STATES, cid, "review")

    settings = row.get("settings", {})
    _require(isinstance(settings, dict) and set(settings) <= {"prompt_tokens"}, cid, "settings")
    prompt_tokens = settings.get("prompt_tokens")
    if prompt_tokens is not None:
        _require(
            isinstance(prompt_tokens, int) and prompt_tokens >= MIN_PROMPT_TOKENS,
            cid,
            f"prompt_tokens must be an integer >= {MIN_PROMPT_TOKENS}",
        )
    golden = row.get("golden_unanswerable")
    _require(golden is None or (isinstance(golden, str) and not answerable), cid, "golden")

    return Case(
        id=cid,
        category=row["category"],
        tool=row["tool"],
        arguments=dict(args),
        held_out=row["held_out"],
        answerable=answerable,
        expected_handling=handling,
        required_evidence=tuple(tuple(g) for g in groups),
        expected_facts=tuple(facts),
        must_not_assert=tuple(row["must_not_assert"]),
        must_include=tuple(tuple(g) for g in include),
        must_not_include=tuple(det["must_not_include"]),
        criteria=dict(criteria),
        review=row["review"],
        prompt_tokens=prompt_tokens,
        golden_unanswerable=golden,
        raw=row,
    )


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    """Load and validate a case file; raises ``CaseError`` on any breach."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != CASES_SCHEMA_VERSION:
        raise CaseError(f"cases schema_version must be {CASES_SCHEMA_VERSION}")
    rows = data.get("cases")
    if not isinstance(rows, list) or not rows:
        raise CaseError("cases must be a non-empty list")
    cases = [_parse_case(r) for r in rows]
    ids = [c.id for c in cases]
    if len(set(ids)) != len(ids):
        raise CaseError("duplicate case ids")
    return cases
