"""Versioned case schema for the answer-quality evaluation.

A case names the tool call to make (one of ``TOOLS`` and its exact
arguments), the evidence an answer needs, independently verified
expected facts with the excerpts of the synthetic corpus that establish
them, machine-checkable values, and which rubric dimensions apply. The
split rule is the agent scenarios' (``tests/agent_metrics.is_held_out``):
membership is fixed by the case ID, so adding cases never moves one.

A case ID starts with its tool's short name (``ask-``, ``summarize-``,
``extract-``, ``brief-``, ``check-``). Per tool (#656): ``ask_mailbox`` needs a
``question``; ``summarize_thread`` needs a baseline ``thread_id`` (a
direct lookup, so nothing is embedded; a subject-phrase lookup is not a
case shape yet), and its evidence and fact sources must be in that
thread. The experimental tools (#1240) need a ``topic``
(``brief_issue``) or a ``conclusion`` of at most the handler's 2,000
characters (``check_conclusion``). ``ask_mailbox`` and both experimental
tools also take the scope filters, whose types are checked here; their
values are the handler's to validate. ``extract_from_emails`` (#1137)
needs a ``query`` and a non-empty ``schema`` object declaring no
provenance field (the handler refuses one before any work); its
``limit``, when given, is a whole number from 1 to the handler's 50
(each searched thread is one paid model call), and its filters
(``folders``, ``date_from``, ``date_to``, ``from_name``,
``participant``) have the handler's types.

An answerable case may carry golden ``chronology`` labels (#291): the
positions it rests on (the person's names, kind, the one message that
states each, its date and whether that is the sent date, a date the
message mentions or one relative to it, and the values it states, as
groups of accepted spellings), the changes between them, the
conflicts neither side of which supersedes the other, and the positions
in force as of the date the question asks about. Every source an answer
must cite is also a required evidence group of its own.

Refs follow the baseline's convention: ``t24`` is the thread rooted at
``t24.1@baseline.example`` and ``t24.2`` the message
``t24.2@baseline.example``. They are resolved against the built index
(``tests/baseline/test_answer_eval_cases.py``), never assumed.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, NoReturn, TypeGuard, get_args

from src.lib.inference import MIN_PROMPT_TOKENS
from src.tools.brief import _MAX_CONCLUSION_CHARS
from src.tools.intelligence import (
    _MAX_ASK_THREADS,
    _MAX_EXTRACT_LIMIT,
    _PROVENANCE_FIELDS,
    _declared_fields,
)
from src.tools.outputs import SummaryStyle

from tests.agent_metrics import is_held_out

CASES_SCHEMA_VERSION = 1
CASES_PATH = Path(__file__).with_name("cases.json")
BASELINE_DOMAIN = "@baseline.example"
# The tools the evaluation has an adapter for (``adapters.py``).
TOOLS = (
    "ask_mailbox",
    "summarize_thread",
    "extract_from_emails",
    "brief_issue",
    "check_conclusion",
)
_ID_PREFIX = {
    "ask_mailbox": "ask",
    "summarize_thread": "summarize",
    "extract_from_emails": "extract",
    "brief_issue": "brief",
    "check_conclusion": "check",
}
# Each tool's one required text argument, which it embeds for retrieval;
# ``summarize_thread`` takes a thread ID instead and embeds nothing.
_TEXT_ARGUMENT = {
    "ask_mailbox": "question",
    "extract_from_emails": "query",
    "brief_issue": "topic",
    "check_conclusion": "conclusion",
}

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
# The scope filters ``ask_mailbox`` and the experimental tools share.
_SCOPE_FILTERS = frozenset({"from_addr", "date_from", "date_to", "folders", "max_threads"})
_ARGUMENTS = {
    "ask_mailbox": _SCOPE_FILTERS | {"question"},
    "summarize_thread": frozenset({"thread_id", "style"}),
    "extract_from_emails": frozenset(
        {"query", "schema", "folders", "date_from", "date_to", "limit", "from_name", "participant"}
    ),
    "brief_issue": _SCOPE_FILTERS | {"topic"},
    "check_conclusion": _SCOPE_FILTERS | {"conclusion"},
}
_CASE_ID = re.compile(r"(?:ask|summarize|extract|brief|check)-[a-z0-9]+(?:-[a-z0-9]+)*")
# A baseline thread number as ``corpus.thread_id`` writes it: two
# digits, or three past t99 (#975).
_THREAD_NUMBER = r"t(?:[0-9]{2}|[1-9][0-9]{2})"
# A baseline thread's ID: its root message (``thread_id_of``).
_THREAD_ID = re.compile(_THREAD_NUMBER + r"\.1" + re.escape(BASELINE_DOMAIN))
# The handler summarizes any other style as ``brief`` (Codex round 1 on
# #656's PR): a typo would grade a task the case does not state.
_SUMMARY_STYLES = frozenset(get_args(SummaryStyle))
CASE_ID_MAX_LEN = 64
_FACT_ID = re.compile(r"f[1-9][0-9]*")
# Chronology labels (#291).
_POSITION_ID = re.compile(r"p[1-9][0-9]*")
POSITION_KINDS = frozenset(
    {"proposal", "approval", "correction", "cancellation", "statement", "disposition"}
)
CHANGE_KINDS = frozenset({"correction", "cancellation", "supersession"})
# Where a position's date comes from, in ``brief_issue``'s own terms,
# plus ``relative``: the message dates the event relative to itself
# ("this morning"), so the date is its sent date and an answer may call
# it either (``Position.date_sources``).
DATE_SOURCES = frozenset({"sent", "mentioned", "relative"})
_CHRONOLOGY_KEYS = frozenset({"as_of", "positions", "changes", "conflicts", "in_force"})
_POSITION_KEYS = frozenset(
    {"id", "actor", "kind", "source", "date", "date_source", "values", "excerpt"}
)
_REF = re.compile(_THREAD_NUMBER + r"(?:\.[1-9][0-9]*)?")


class CaseError(ValueError):
    """A case file that breaks the schema. Messages name case IDs and
    fields only (both ours), never case text."""


def is_case_id(value: object) -> TypeGuard[str]:
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
class Position:
    """One golden position or event of a chronology case (#291): who held
    or did it (``actor``, the person's accepted names, each matched as
    whole words; never a role such as "clerk", which a relayer's
    description can contain too), what kind of step it is, the one
    message that states it, the date the source supports for it and
    where that date comes from (``date_source``: ``sent``, ``mentioned``
    or ``relative``, for an event the message dates relative to itself,
    such as "this morning", where an answer may give either source), and
    the whole values that message states for it (``values``: groups of
    accepted spellings, such as "30 June" and "June 30", which pair a
    value in an answer with the message it must cite). ``excerpt`` is
    verbatim from the source's indexed text."""

    id: str
    actor: tuple[str, ...]
    kind: str
    source: str
    date: str
    date_source: str
    values: tuple[tuple[str, ...], ...]
    excerpt: str

    def date_sources(self) -> frozenset[str]:
        """The date sources an answer may give for this position."""
        if self.date_source == "relative":
            return frozenset({"sent", "mentioned"})
        return frozenset({self.date_source})


@dataclass(frozen=True)
class Change:
    """A later position that corrects, cancels or supersedes an earlier
    one. An answer must cite both sides' sources."""

    before: str
    after: str
    kind: str


@dataclass(frozen=True)
class Chronology:
    """A case's golden chronology labels (#291): its positions, the
    changes between them, the positions that disagree with neither
    superseding the other, and those in force as of ``as_of`` (the date
    the question asks about; ``None`` asks about now)."""

    as_of: str | None
    positions: tuple[Position, ...]
    changes: tuple[Change, ...]
    conflicts: tuple[tuple[str, ...], ...]
    in_force: tuple[str, ...]

    def position(self, pid: str) -> Position:
        return next(p for p in self.positions if p.id == pid)

    def must_cite(self) -> list[str]:
        """The source refs an answer must cite: each position in force,
        both sides of every change and every side of every conflict."""
        ids = [*self.in_force]
        ids += [i for c in self.changes for i in (c.before, c.after)]
        ids += [i for group in self.conflicts for i in group]
        return list(dict.fromkeys(self.position(i).source for i in ids))


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
    chronology: Chronology | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def question(self) -> str:
        """The case's task in words, for the judge prompt and the detail
        record: ``ask_mailbox``'s question, or a fixed sentence naming a
        summary's thread and style, an extraction's request and fields, a
        brief's topic or the conclusion to check."""
        if self.tool == "ask_mailbox":
            return str(self.arguments["question"])
        if self.tool == "brief_issue":
            return f"Brief the issue: {self.arguments['topic']}"
        if self.tool == "check_conclusion":
            return f"Check this conclusion against the mailbox: {self.arguments['conclusion']}"
        if self.tool == "extract_from_emails":
            fields = ", ".join(sorted(_declared_fields(self.arguments["schema"])))
            query = self.arguments["query"]
            return f"Extract records for the request {query!r} with the fields {fields}."
        style = self.arguments.get("style", "brief")
        return f"Summarize the thread {self.arguments['thread_id']} in the {style} style."

    @property
    def embedded_query(self) -> str | None:
        """The text the tool embeds for retrieval, which the index build
        must hold a query vector for: the question, extraction query, topic
        or conclusion.
        ``None`` for a summary, whose thread is looked up by ID."""
        argument = _TEXT_ARGUMENT.get(self.tool)
        return None if argument is None else str(self.arguments[argument])


def _fail(case_id: str, what: str) -> NoReturn:
    raise CaseError(f"case {case_id}: {what}")


def _require(cond: bool, case_id: str, what: str) -> None:
    """Raise unless ``cond`` holds. A check whose value is used afterwards
    calls ``_fail`` under an ``if not`` instead, so mypy narrows it."""
    if not cond:
        _fail(case_id, what)


def _str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def _scope_filters_ok(args: dict[str, Any]) -> bool:
    """The shared scope filters, when given, have the handler's types:
    ``folders`` a list of names, ``from_addr`` and the dates strings,
    ``max_threads`` an integer in the handler's range (it would coerce or
    clamp any other value, a task the case does not state)."""
    max_threads = args.get("max_threads", 1)
    return (
        ("folders" not in args or _str_list(args["folders"]))
        and all(isinstance(args.get(k, ""), str) for k in ("from_addr", "date_from", "date_to"))
        and type(max_threads) is int
        and 1 <= max_threads <= _MAX_ASK_THREADS
    )


def _extract_arguments_ok(args: dict[str, Any]) -> bool:
    """An extraction's ``schema`` is a non-empty object, its ``limit``,
    when given, an integer in the handler's range (it would clamp any
    other value, a task and a call count the case does not state), and
    its filters have the handler's types."""
    limit = args.get("limit", 1)
    return (
        isinstance(args.get("schema"), dict)
        and bool(args["schema"])
        and type(limit) is int
        and 1 <= limit <= _MAX_EXTRACT_LIMIT
        and ("folders" not in args or _str_list(args["folders"]))
        and all(
            isinstance(args.get(k, ""), str)
            for k in ("date_from", "date_to", "from_name", "participant")
        )
    )


def _refs(value: object) -> bool:
    return isinstance(value, list) and _str_list(value) and all(_REF.fullmatch(v) for v in value)


def _iso_date(value: object) -> bool:
    if not (isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _parse_chronology(cid: str, raw: object, groups: list[list[str]]) -> Chronology:
    """Golden chronology labels (#291). Every source an answer must cite
    is also a required evidence group of its own, so a miss is attributed
    to retrieval, prompt assembly or synthesis like any other group."""
    if not (isinstance(raw, dict) and set(raw) <= _CHRONOLOGY_KEYS and "positions" in raw):
        _fail(cid, "chronology keys")
    as_of = raw.get("as_of")
    _require(as_of is None or _iso_date(as_of), cid, "chronology as_of")
    rows = raw["positions"]
    _require(isinstance(rows, list) and bool(rows), cid, "chronology positions")
    positions = []
    for p in rows:
        if not (isinstance(p, dict) and set(p) == _POSITION_KEYS):
            _fail(cid, "position keys")
        _require(bool(_POSITION_ID.fullmatch(str(p["id"]))), cid, "position id")
        _require(_str_list(p["actor"]) and bool(p["actor"]), cid, "position actor")
        _require(p["kind"] in POSITION_KINDS, cid, "position kind")
        source = p["source"]
        _require(_refs([source]) and "." in source, cid, "position source must be a message")
        _require(_iso_date(p["date"]), cid, "position date")
        _require(p["date_source"] in DATE_SOURCES, cid, "position date_source")
        _require(
            isinstance(p["values"], list) and all(_str_list(g) and g for g in p["values"]),
            cid,
            "position values must be groups of spellings",
        )
        _require(
            isinstance(p["excerpt"], str) and bool(p["excerpt"].strip()), cid, "position excerpt"
        )
        positions.append(
            Position(
                p["id"],
                tuple(p["actor"]),
                p["kind"],
                source,
                p["date"],
                p["date_source"],
                tuple(tuple(g) for g in p["values"]),
                p["excerpt"],
            )
        )
    ids = [p.id for p in positions]
    _require(len(set(ids)) == len(ids), cid, "duplicate position ids")
    changes = []
    for c in raw.get("changes", []):
        if not (isinstance(c, dict) and set(c) == {"from", "to", "kind"}):
            _fail(cid, "change keys")
        _require(
            c["from"] in ids and c["to"] in ids and c["from"] != c["to"], cid, "change positions"
        )
        _require(c["kind"] in CHANGE_KINDS, cid, "change kind")
        changes.append(Change(c["from"], c["to"], c["kind"]))
    conflicts = raw.get("conflicts", [])
    _require(
        isinstance(conflicts, list)
        and all(
            isinstance(g, list) and len(set(g)) == len(g) >= 2 and set(g) <= set(ids)
            for g in conflicts
        ),
        cid,
        "conflicts must list two or more position ids",
    )
    in_force = raw.get("in_force", [])
    _require(isinstance(in_force, list) and set(in_force) <= set(ids), cid, "in_force position ids")
    chronology = Chronology(
        as_of,
        tuple(positions),
        tuple(changes),
        tuple(tuple(g) for g in conflicts),
        tuple(in_force),
    )
    _require(bool(chronology.must_cite()), cid, "chronology must ask an answer to cite something")
    _require(
        all([ref] in groups for ref in chronology.must_cite()),
        cid,
        "every source an answer must cite is a required_evidence group of its own",
    )
    return chronology


def _parse_case(row: dict[str, Any]) -> Case:
    cid = row.get("id")
    if not is_case_id(cid):
        raise CaseError("a case has a missing or malformed id")
    tool = row.get("tool")
    if tool not in TOOLS:
        _fail(cid, "unknown tool")
    prefix = _ID_PREFIX[tool]
    _require(cid.startswith(f"{prefix}-"), cid, f"id must start with {prefix}-")
    _require(row.get("category") in CATEGORIES, cid, "unknown category")
    args = row.get("arguments")
    if not (isinstance(args, dict) and set(args) <= _ARGUMENTS[tool]):
        _fail(cid, "bad arguments keys")
    if tool in _TEXT_ARGUMENT:
        name = _TEXT_ARGUMENT[tool]
        _require(isinstance(args.get(name), str) and args[name].strip(), cid, name)
        if tool == "check_conclusion":
            # The handler refuses a longer conclusion as a tool error.
            _require(
                len(args[name]) <= _MAX_CONCLUSION_CHARS,
                cid,
                f"conclusion must be at most {_MAX_CONCLUSION_CHARS} characters",
            )
        if tool == "extract_from_emails":
            _require(_extract_arguments_ok(args), cid, "schema, limit or filter types")
            # The handler refuses these names before any work (#329),
            # which would grade as a tool error rather than a bad case.
            _require(
                not set(_PROVENANCE_FIELDS) & _declared_fields(args["schema"]),
                cid,
                f"schema must not declare {', '.join(_PROVENANCE_FIELDS)}",
            )
        else:
            _require(_scope_filters_ok(args), cid, "scope filter types")
    else:
        thread_id = args.get("thread_id")
        _require(
            isinstance(thread_id, str) and bool(_THREAD_ID.fullmatch(thread_id)), cid, "thread_id"
        )
        _require(
            args.get("style", "brief") in _SUMMARY_STYLES,
            cid,
            f"style must be one of {sorted(_SUMMARY_STYLES)}",
        )
    _require(row.get("held_out") is is_held_out(cid), cid, "held_out must equal is_held_out(id)")
    answerable = row.get("answerable")
    if not isinstance(answerable, bool):
        _fail(cid, "answerable must be a boolean")
    handling = row.get("expected_handling")
    if handling not in HANDLINGS:
        _fail(cid, "unknown expected_handling")
    _require((handling == "abstain") is (not answerable), cid, "abstain iff not answerable")

    groups = row.get("required_evidence")
    if not (isinstance(groups, list) and all(_refs(g) and g for g in groups)):
        _fail(cid, "evidence")
    _require(bool(groups) is answerable, cid, "answerable cases, and only they, need evidence")

    facts_raw = row.get("expected_facts")
    if not isinstance(facts_raw, list):
        _fail(cid, "expected_facts must be a list")
    facts = []
    for f in facts_raw:
        if not (isinstance(f, dict) and _FACT_ID.fullmatch(str(f.get("id")))):
            _fail(cid, "fact id")
        _require(isinstance(f.get("fact"), str) and f["fact"].strip(), cid, "fact text")
        _require(_refs(f.get("sources")) and f["sources"], cid, "fact sources")
        _require(isinstance(f.get("excerpt"), str) and f["excerpt"].strip(), cid, "fact excerpt")
        values = f.get("values", [])
        _require(isinstance(values, list) and (not values or _str_list(values)), cid, "values")
        facts.append(Fact(f["id"], f["fact"], tuple(f["sources"]), f["excerpt"], tuple(values)))
    _require(len({f.id for f in facts}) == len(facts), cid, "duplicate fact ids")
    _require(bool(facts) is answerable, cid, "answerable cases, and only they, need facts")
    if tool == "summarize_thread":
        # The handler sees only the named thread, so evidence elsewhere
        # could never be retrieved and would grade as a regression.
        refs = [r for g in groups for r in g] + [r for f in facts for r in f.sources]
        _require(
            all(thread_id_of(r) == args["thread_id"] for r in refs),
            cid,
            "evidence and fact sources must be in the summarized thread",
        )

    _require(_str_list(row.get("must_not_assert")), cid, "must_not_assert")
    det = row.get("deterministic")
    if not isinstance(det, dict):
        _fail(cid, "deterministic must be an object")
    include = det.get("must_include")
    if not (isinstance(include, list) and all(_str_list(g) and g for g in include)):
        _fail(cid, "include")
    _require(_str_list(det.get("must_not_include")), cid, "must_not_include")

    criteria = row.get("criteria")
    if not (isinstance(criteria, dict) and set(criteria) == set(DIMENSIONS)):
        _fail(cid, "criteria keys")
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
    chronology = None
    if "chronology" in row:
        _require(answerable, cid, "only answerable cases have a chronology")
        chronology = _parse_chronology(cid, row["chronology"], groups)

    return Case(
        id=cid,
        category=row["category"],
        tool=tool,
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
        chronology=chronology,
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
