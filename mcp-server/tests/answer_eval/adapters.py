"""Per-tool adapters: what differs between the tools the evaluation runs.

The case schema, the capture, the deterministic graders, the judge
rubric and the reports are shared by every tool (#656: extend, not
fork). What differs per tool is collected here: the handler's output
model, which captured evidence map describes the prompt the model saw,
and ``AnswerView``, the one shape of a tool's output that the graders,
the judge and the reports read.

- ``ask_mailbox``: the answer, its statements, citations, the threads
  searched and the server's coverage note, as the tool returns them.
- ``summarize_thread``: the summary is the answer and the one thread
  summarized is the threads; the tool's ``coverage_note`` (the model
  window's trimming notice, #949) is the coverage note.

``extract_from_emails`` (#1137) and the experimental tools (``brief_issue``,
``check_conclusion``) have no adapter yet (#656, #291).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.tools.outputs import AnswerStatement, AskMailboxOutput, SummarizeThreadOutput

# Each tool's structured output, which the runner validates a result against.
OUTPUT_MODELS: dict[str, type] = {
    "ask_mailbox": AskMailboxOutput,
    "summarize_thread": SummarizeThreadOutput,
}


@dataclass
class AnswerView:
    """A tool's output as the graders, judge and reports read it."""

    answer: str = field(repr=False)
    threads: Sequence[Any]  # the threads searched or summarized (``thread_id``)
    citations: Sequence[Any]  # each cited label (``label``)
    citation_problems: Sequence[Any]  # the tool's citation check (``kind``)
    statements: Sequence[AnswerStatement]
    coverage_note: str | None = field(default=None, repr=False)
    repair_attempted: bool | None = None


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
    the prompt's.
    """
    if not maps:
        return {}
    return dict(maps[0] if tool == "summarize_thread" else maps[-1])
