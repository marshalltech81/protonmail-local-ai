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
- ``extract_from_emails``: each record is one statement, rendered as
  ``field: value [E1]; ...`` with the labels its ``_evidence`` cites, so
  the citation checks, ``must_include`` and the judge's per-statement
  claims apply to records as they do to prose. The answer is those
  statements, one per line, or, when the tool extracted none,
  ``NO_RECORDS`` (graded as an abstention) unless the tool's ``notice``
  says the extraction was incomplete (a reply cut off, malformed or
  nonconforming for some thread): then it is ``EXTRACTION_INCOMPLETE``,
  which is no abstention, and ``incomplete`` is set for the
  ``extraction_complete`` check (Codex round 2 on #656's PR). The
  ``notice`` is the coverage note; there is no repair call. Field names
  are the case's own; values are provider output and reach only the
  detail artifact, as answers do.

The experimental tools (``brief_issue``, ``check_conclusion``) have no
adapter yet (#656, #291).
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.lib.validation import clamp_int
from src.tools.intelligence import _MAX_EXTRACT_LIMIT, _PROVENANCE_FIELDS
from src.tools.outputs import (
    AnswerStatement,
    AskMailboxOutput,
    ExtractFromEmailsOutput,
    SummarizeThreadOutput,
)

from tests.answer_eval.cases import Case

# Each tool's structured output, which the runner validates a result against.
OUTPUT_MODELS: dict[str, type] = {
    "ask_mailbox": AskMailboxOutput,
    "summarize_thread": SummarizeThreadOutput,
    "extract_from_emails": ExtractFromEmailsOutput,
}

# The extract view's answer when the tool returned no records: fixed
# text, graded as an abstention (``graders.is_abstention``).
NO_RECORDS = "No records extracted"
# ... unless the tool's notice reports an incomplete extraction: fixed
# text that is no abstention.
EXTRACTION_INCOMPLETE = "Extraction incomplete: no records"
# How ``extract_from_emails`` opens its notice when a thread's reply was
# cut off, not JSON, or a record failed the schema check (the fixed text
# ``f"Incomplete: {failed} of {len(results)} threads could not be
# extracted ..."`` in ``src/tools/intelligence.py``). A window-only
# evidence note does not start with it.
_INCOMPLETE_PREFIX = "Incomplete:"

# ``extract_from_emails``'s default ``limit`` (one model call per thread).
_EXTRACT_DEFAULT_LIMIT = 20


@dataclass
class AnswerView:
    """A tool's output as the graders, judge and reports read it."""

    answer: str = field(repr=False)
    threads: Sequence[Any]  # the threads searched or summarized (``thread_id``)
    citations: Sequence[Any]  # each cited label (``label``)
    citation_problems: Sequence[Any]  # the tool's citation check (``kind``)
    statements: Sequence[AnswerStatement]
    coverage_note: str | None = field(default=None, repr=False)
    repair_attempted: bool | None = None  # None for a tool without a repair call
    records: list[dict[str, Any]] | None = field(default=None, repr=False)  # extract only
    # extract only: some searched thread's reply was cut off, malformed
    # or nonconforming, so data may be missing whatever the records say.
    incomplete: bool = False


def _record_statement(record: Mapping[str, Any]) -> AnswerStatement:
    """One extracted record as a statement: ``field: value [E1]; ...``,
    each value followed by the labels its (server-checked) ``_evidence``
    entry cites; the provenance fields the server adds are left out."""
    evidence = record.get("_evidence")
    cited: Mapping[str, Any] = evidence if isinstance(evidence, Mapping) else {}
    parts: list[str] = []
    labels: list[str] = []
    for name, value in record.items():
        if name in _PROVENANCE_FIELDS:
            continue
        own = cited.get(name)
        own_labels = [x for x in own if isinstance(x, str)] if isinstance(own, list) else []
        parts.append(
            f"{name}: {json.dumps(value, ensure_ascii=False)}"
            + "".join(f" [{label}]" for label in own_labels)
        )
        labels += [label for label in own_labels if label not in labels]
    return AnswerStatement(
        text="; ".join(parts) + ".", labels=labels, status="cited" if labels else "uncited"
    )


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
        records = list(output.records)
        statements = [_record_statement(r) for r in records if isinstance(r, Mapping)]
        notice = output.notice
        incomplete = isinstance(notice, str) and notice.startswith(_INCOMPLETE_PREFIX)
        if statements:
            answer = "\n".join(s.text for s in statements)
        else:
            answer = EXTRACTION_INCOMPLETE if incomplete else NO_RECORDS
        return AnswerView(
            answer,
            output.threads,
            output.citations,
            output.citation_problems,
            statements,
            notice,
            None,
            records,
            incomplete,
        )
    raise KeyError(tool)


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

    ``ask_mailbox`` builds one map (the last captured). ``summarize_thread``
    builds the shown map first and then, when the window cut the context,
    the map the tool's own caps alone would show (#949), so the first is
    the prompt's. ``extract_from_emails`` builds one map per thread, each
    for its own prompt, with labels numbered across the call, so they
    merge without collision.
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

    ``ask_mailbox`` and ``summarize_thread`` make one call plus one repair
    when the first answer fails the citation check. ``extract_from_emails``
    makes one call per searched thread, at most its ``limit`` as the tool
    clamps it, and never repairs.
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
