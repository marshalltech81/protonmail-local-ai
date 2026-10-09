"""Per-tool adapters: what differs between the tools the evaluation runs.

The case schema, the capture, the deterministic graders, the judge
rubric and the reports are shared by every tool (#656: extend, not
fork). What differs per tool is collected here: the handler's output
model, which captured evidence map describes the prompt the model saw,
how many provider calls a case makes, and ``AnswerView``, the one shape
of a tool's output that the graders, the judge and the reports read.

- ``ask_mailbox``: the answer, its statements, citations, the threads
  searched and the server's coverage note, as the tool returns them.
- ``summarize_thread``: the summary is the answer and the one thread
  summarized is the threads; the tool's ``coverage_note`` (the model
  window's trimming notice, #949) is the coverage note.
- ``brief_issue`` and ``check_conclusion`` (experimental, #1240) return
  structured entries, not prose. Each entry becomes an ``EntryStatement``
  (a brief's chronology, positions, decisions, open questions and
  conflicts with their section and index; the check's verdict, then each
  finding with its stance), and the answer is those statements, one per
  line, after the tool's insufficient-evidence line when it set the
  flag. The tool's own ``insufficient_evidence`` flag is the abstention,
  and a reply the tool could not parse (status ``invalid_json`` or
  ``truncated``) is an incomplete answer whose text is the raw reply.
  Neither tool writes a coverage note: what the prompt budget left out
  is stated only in the prompt, so a ``disclose_missing`` case cannot
  pass for them.
- ``extract_from_emails`` (#1137): each record is one statement,
  rendered as ``field: value [E1]; ...`` with the labels its
  server-checked ``_evidence`` cites, so the citation checks,
  ``must_include`` and the judge's per-statement claims apply to records
  as they do to prose. Fields with no value (``None``, ``""``, ``[]``,
  ``{}``, which the tool's own check asks no evidence for) are left
  out, and a record with none states nothing. The answer is those
  statements, one per line.
  The tool's ``notice`` is the coverage note, and there is no repair
  call. A notice opening ``Incomplete:`` (some searched thread's reply
  was cut off, malformed or nonconforming) makes the view incomplete,
  with or without records. With no statements and a complete extraction
  the view abstains (the flag, not the text, so no other tool's answer
  can match it); with none and an incomplete one it does not.
  Field names are the case's own; values are provider output and reach
  only the detail artifact, as answers do.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from src.lib.validation import clamp_int
from src.tools.intelligence import _MAX_EXTRACT_LIMIT, _PROVENANCE_FIELDS
from src.tools.outputs import (
    AnswerStatement,
    AskMailboxOutput,
    BriefIssueOutput,
    CheckConclusionOutput,
    ExtractFromEmailsOutput,
    SummarizeThreadOutput,
)

from tests.answer_eval.cases import Case

# Each tool's structured output, which the runner validates a result against.
OUTPUT_MODELS: dict[
    str,
    type[
        AskMailboxOutput
        | SummarizeThreadOutput
        | ExtractFromEmailsOutput
        | BriefIssueOutput
        | CheckConclusionOutput
    ],
] = {
    "ask_mailbox": AskMailboxOutput,
    "summarize_thread": SummarizeThreadOutput,
    "extract_from_emails": ExtractFromEmailsOutput,
    "brief_issue": BriefIssueOutput,
    "check_conclusion": CheckConclusionOutput,
}

# The extract view's answer when the tool returned no records (fixed
# text): an abstention through the view's flag ...
NO_RECORDS = "No records extracted"
# ... unless the tool's notice reports an incomplete extraction.
EXTRACTION_INCOMPLETE = "Extraction incomplete: no records"
# How ``extract_from_emails`` opens its notice when a thread's reply was
# cut off, not JSON, or a record failed the schema check (the fixed text
# ``f"Incomplete: {failed} of {len(results)} threads could not be
# extracted ..."`` in ``src/tools/intelligence.py``). An evidence or
# structured-output note alone does not start with it.
_INCOMPLETE_PREFIX = "Incomplete:"
# ``extract_from_emails``'s default ``limit`` (one model call per thread).
_EXTRACT_DEFAULT_LIMIT = 20


@dataclass(frozen=True)
class EntryStatement:
    """One entry of an experimental tool's reply, read as a statement
    (``text`` and ``labels``, as ``AnswerStatement``). ``labels`` are the
    supplied passages it cites, as for ``AnswerStatement``; ``text`` keeps
    every label the entry gave. ``status``: ``cited``, ``invalid`` (it
    cites only unknown labels) or ``uncited``."""

    text: str
    labels: list[str]
    status: str
    section: str  # a brief's section; ``verdict`` or ``findings`` for a check
    item: int  # 0-based index within the section
    stance: str | None = None  # a finding's relation, as the model gave it
    # A brief's chronology entry: its date, date source and actor as the
    # model gave them, which the chronology labels check (#291).
    date: str | None = None
    date_source: str | None = None
    actor: str | None = None


@dataclass
class AnswerView:
    """A tool's output as the graders, judge and reports read it."""

    answer: str = field(repr=False)
    threads: Sequence[Any]  # the threads searched or summarized (``thread_id``)
    citations: Sequence[Any]  # each cited label (``label``)
    citation_problems: Sequence[Any]  # the tool's citation check (``kind``)
    statements: Sequence[AnswerStatement | EntryStatement]
    coverage_note: str | None = field(default=None, repr=False)
    repair_attempted: bool | None = None
    # The tool's own abstention flag (the experimental tools'
    # ``insufficient_evidence``); the other tools abstain in words only.
    abstained: bool = False
    # False when the tool could not parse the model's reply at all, or
    # (``extract_from_emails``) could not extract some searched thread.
    complete: bool = True
    # ``extract_from_emails`` only: the records, for ``records_conform``.
    records: list[dict[str, Any]] | None = field(default=None, repr=False)


def view_of(tool: str, output: Any) -> AnswerView:
    """``output`` (the tool's structured output) seen through its adapter."""
    if tool == "ask_mailbox":
        return AnswerView(
            output.answer,
            output.threads,
            output.citations,
            output.citation_problems,
            output.statements,
            output.coverage_note,
            output.repair_attempted,
        )
    if tool == "summarize_thread":
        return AnswerView(
            output.summary,
            [output.thread],
            output.citations,
            output.citation_problems,
            output.statements,
            output.coverage_note,
            output.repair_attempted,
        )
    if tool == "extract_from_emails":
        return _extract_view(output)
    if tool == "brief_issue":
        return _brief_view(output)
    if tool == "check_conclusion":
        return _check_view(output)
    raise KeyError(tool)


def has_value(value: object) -> bool:
    """Whether an extracted field holds a value: the tool's citation check
    (``intelligence._check_records``) skips ``None``, ``""``, ``[]`` and
    ``{}`` and asks no evidence for them."""
    return value is not None and value not in ("", [], {})


def _record_statement(record: Mapping[str, Any]) -> AnswerStatement | None:
    """One extracted record as a statement: ``field: value [E1]; ...``,
    each value followed by the labels its (server-checked) ``_evidence``
    entry cites. The provenance fields the server adds and fields with
    no value (``has_value``) are left out; ``None`` for a record that
    states nothing."""
    evidence = record.get("_evidence")
    cited: Mapping[str, Any] = evidence if isinstance(evidence, Mapping) else {}
    parts: list[str] = []
    labels: list[str] = []
    for name, value in record.items():
        if name in _PROVENANCE_FIELDS or not has_value(value):
            continue
        own = cited.get(name)
        own_labels = [x for x in own if isinstance(x, str)] if isinstance(own, list) else []
        parts.append(f"{name}: {json.dumps(value, ensure_ascii=False)}" + _cites(own_labels))
        labels += [label for label in own_labels if label not in labels]
    if not parts:
        return None
    return AnswerStatement(
        text="; ".join(parts) + ".", labels=labels, status="cited" if labels else "uncited"
    )


def _extract_view(output: ExtractFromEmailsOutput) -> AnswerView:
    """Records as statements (``_record_statement``), the notice as the
    coverage note; see the module docstring for no records."""
    records = list(output.records)
    statements = [s for s in map(_record_statement, records) if s is not None]
    notice = output.notice
    incomplete = notice is not None and notice.startswith(_INCOMPLETE_PREFIX)
    abstained = not statements and not incomplete
    if statements:
        answer = "\n".join(s.text for s in statements)
    elif incomplete:
        answer = EXTRACTION_INCOMPLETE
        # A non-abstaining answer needs a statement for the judge's
        # claims to name (``judge.parse_verdict``); this one cites nothing.
        statements = [AnswerStatement(text=answer, labels=[], status="not_checked")]
    else:
        answer = NO_RECORDS
    return AnswerView(
        answer,
        output.threads,
        output.citations,
        output.citation_problems,
        statements,
        notice,
        None,
        abstained=abstained,
        complete=not incomplete,
        records=records,
    )


# The tools' own prose for a reply that set ``insufficient_evidence``.
_BRIEF_INSUFFICIENT = "Insufficient evidence: the passages do not cover this topic."
_CHECK_INSUFFICIENT = "Insufficient evidence: the passages do not address this conclusion."


def _cites(labels: Sequence[str]) -> str:
    return f" [{', '.join(labels)}]" if labels else ""


def _entry(
    text: str, labels: list[str], known: set[str], section: str, item: int, stance=None
) -> EntryStatement:
    supplied = [label for label in labels if label in known]
    status = "cited" if supplied else "invalid" if labels else "uncited"
    return EntryStatement(text + _cites(labels), supplied, status, section, item, stance)


def _unparsed_view(output: Any, citation_problems: Sequence[Any]) -> AnswerView:
    """A reply the tool could not parse: its raw text, nothing cited."""
    return AnswerView(
        output.raw_text or "",
        output.threads,
        [],
        citation_problems,
        [],
        None,
        output.repair_attempted,
        complete=False,
    )


def _answer(statements: Sequence[EntryStatement], insufficient: str | None) -> str:
    lines = ([insufficient] if insufficient else []) + [s.text for s in statements]
    return "\n".join(lines)


def _brief_view(output: BriefIssueOutput) -> AnswerView:
    """A brief's entries as statements, section by section in the tool's
    order. Entry labels are already canonical (the tool rewrites them)."""
    brief = output.brief
    if brief is None:
        return _unparsed_view(output, output.citation_problems)
    known = {c.label for c in output.citations}
    statements: list[EntryStatement] = []
    for i, e in enumerate(brief.chronology):
        text = f"{e.date or 'undated'} ({e.date_source}) {e.actor}: {e.event}"
        entry = _entry(text, e.labels, known, "chronology", i)
        statements.append(replace(entry, date=e.date, date_source=e.date_source, actor=e.actor))
    for i, p in enumerate(brief.positions):
        statements.append(_entry(f"{p.actor}: {p.position}", p.labels, known, "positions", i))
    for i, d in enumerate(brief.decisions):
        statements.append(_entry(f"Decision: {d.decision}", d.labels, known, "decisions", i))
    for i, q in enumerate(brief.open_questions):
        text = f"Open question: {q.question}"
        statements.append(_entry(text, q.labels, known, "open_questions", i))
    for i, c in enumerate(brief.conflicts):
        statements.append(_entry(f"Conflict: {c.description}", c.labels, known, "conflicts", i))
    insufficient = _BRIEF_INSUFFICIENT if brief.insufficient_evidence else None
    return AnswerView(
        _answer(statements, insufficient),
        output.threads,
        output.citations,
        output.citation_problems,
        statements,
        None,
        output.repair_attempted,
        abstained=brief.insufficient_evidence,
    )


def _check_view(output: CheckConclusionOutput) -> AnswerView:
    """The verdict, then each finding with its stance. A finding's sources
    are the supplied passages it cites; the verdict cites what the
    findings cite, the passages the tool checks its quotes against.
    Each source once, in first-cited order, is the citations."""
    if output.status != "ok":
        return _unparsed_view(output, output.citation_problems)
    citations: dict[str, Any] = {}
    for f in output.findings:
        for source in f.sources:
            citations.setdefault(source.label, source)
    known = set(citations)
    statements: list[EntryStatement] = []
    if output.verdict_summary is not None:
        every = [label for f in output.findings for label in f.labels if label in known]
        labels = list(dict.fromkeys(every))
        verdict = f"Verdict: {output.verdict_summary}"
        statements.append(
            EntryStatement(verdict, labels, "cited" if labels else "uncited", "verdict", 0)
        )
    for i, f in enumerate(output.findings):
        text = f"{f.relation.upper()}: {f.explanation}"
        statements.append(_entry(text, f.labels, known, "findings", i, f.relation))
    abstained = bool(output.insufficient_evidence)
    return AnswerView(
        _answer(statements, _CHECK_INSUFFICIENT if abstained else None),
        output.threads,
        list(citations.values()),
        output.citation_problems,
        statements,
        None,
        output.repair_attempted,
        abstained=abstained,
    )


def window_cut_labels(tool: str, maps: Sequence[Mapping[str, Any]]) -> set[str]:
    """Labels of the shown passages the model window cut short, beyond what
    the capture's chunk offsets record.

    ``summarize_thread`` builds a second map, what its caps alone would
    show, only when the window cut the context (#949); a passage whose
    text is shorter in the shown map than there was cut by the window.
    This covers ``E1``, the thread's indexed text, which has no chunk
    offsets for ``runner._passage`` to compare (Codex round 2 on #656's
    PR). The other tools build no such map (``ask_mailbox``'s thread-text
    fallback is #1128).
    """
    if tool != "summarize_thread" or len(maps) < 2:
        return set()
    shown, wanted = maps[0], maps[1]
    return {
        label
        for label, ref in shown.items()
        if label in wanted and len(ref.text) < len(wanted[label].text)
    }


def select_passages(tool: str, maps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The captured evidence map that describes the prompt the model saw.

    ``ask_mailbox`` and the experimental tools build one map (the last
    captured; none when retrieval found no message passage). ``summarize_thread``
    builds the shown map first and then, when the window cut the context,
    the map the tool's own caps alone would show (#949), so the first is
    the prompt's. ``extract_from_emails`` builds one map per searched
    thread, each for its own prompt, with labels numbered across the
    call, so they merge without collision.
    """
    if tool == "extract_from_emails":
        merged: dict[str, Any] = {}
        for evidence_map in maps:
            merged.update(evidence_map)
        return merged
    if not maps:
        return {}
    return dict(maps[0] if tool == "summarize_thread" else maps[-1])


def planned_calls(case: Case) -> tuple[int, int]:
    """(answer calls, possible citation-repair calls) one case makes.

    Every tool makes one call plus one repair when the first reply fails
    its check, except ``extract_from_emails``: one call per searched
    thread, at most its ``limit`` as the handler clamps it, and no repair.
    """
    if case.tool == "extract_from_emails":
        limit = clamp_int(
            case.arguments.get("limit", _EXTRACT_DEFAULT_LIMIT),
            default=_EXTRACT_DEFAULT_LIMIT,
            minimum=1,
            maximum=_MAX_EXTRACT_LIMIT,
        )
        return limit, 0
    return 1, 1
