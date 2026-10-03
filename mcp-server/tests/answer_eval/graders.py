"""Deterministic graders: run first, and never overridden by the judge.

Each check is ``pass``, ``fail`` or ``not_applicable``. Evidence groups
are scored at three stages, so a miss can be traced to where it
happened: *retrieved* (the ref's thread is among the threads the tool
searched), *supplied* (a passage of the ref's message, or of its thread
for a thread ref, was in the prompt the model received) and *cited* (the
answer cites such a passage). A message ref is met only by a passage of
that message; a passage of the thread's indexed text (no single
message) meets thread refs only.
"""

from dataclasses import dataclass, field

from src.tools.intelligence import _NOT_FOUND_PREFIX, _TRUNCATED_NOTICE

from tests.answer_eval.cases import Case, message_id_of, thread_id_of
from tests.answer_eval.runner import CaseRun, Passage

PASS, FAIL, NA = "pass", "fail", "not_applicable"

# What ask_mailbox says when nothing matched, and the prefix its prompt
# asks the model to open with when the evidence holds no answer.
_NO_RESULTS = "No relevant emails found"

# Checks whose failure points at what the model did with its evidence.
SYNTHESIS_CHECKS = (
    "answer_complete",
    "citations_resolve",
    "citation_checks",
    "expected_values",
    "forbidden_values",
    "abstention",
)


@dataclass
class GroupStatus:
    retrieved: bool
    supplied: bool
    cited: bool


@dataclass
class DeterministicResult:
    checks: dict[str, str] = field(default_factory=dict)
    groups: list[GroupStatus] = field(default_factory=list)
    abstained: bool | None = None
    citation_problem_kinds: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and FAIL not in self.checks.values()

    def _rate(self, stage: str) -> float | None:
        if not self.groups:
            return None
        return sum(getattr(g, stage) for g in self.groups) / len(self.groups)

    @property
    def retrieval_recall(self) -> float | None:
        return self._rate("retrieved")

    @property
    def prompt_coverage(self) -> float | None:
        return self._rate("supplied")

    @property
    def citation_coverage(self) -> float | None:
        return self._rate("cited")


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def _meets(ref: str, passage: Passage) -> bool:
    message_id = message_id_of(ref)
    if message_id is None:
        return passage.thread_id == thread_id_of(ref)
    return passage.message_id == message_id


def is_abstention(answer: str) -> bool:
    stripped = answer.lstrip()
    return stripped.startswith(_NOT_FOUND_PREFIX) or stripped.startswith(_NO_RESULTS)


def grade_run(case: Case, run: CaseRun) -> DeterministicResult:
    """Grade one completed run; a run that did not complete gets no checks
    (its status already counts it as an error)."""
    result = DeterministicResult()
    if run.status != "ok" or run.output is None:
        return result
    out = run.output
    answer = out.answer
    retrieved = {t.thread_id for t in out.threads}
    cited_labels = [c.label for c in out.citations]
    cited = [run.passages[label] for label in cited_labels if label in run.passages]
    supplied = list(run.passages.values())

    for group in case.required_evidence:
        result.groups.append(
            GroupStatus(
                retrieved=any(thread_id_of(ref) in retrieved for ref in group),
                supplied=any(_meets(ref, p) for ref in group for p in supplied),
                cited=any(_meets(ref, p) for ref in group for p in cited),
            )
        )

    checks = result.checks
    checks["answer_complete"] = FAIL if answer.endswith(_TRUNCATED_NOTICE) else PASS
    checks["prompt_matches_capture"] = PASS if run.prompt_consistent else FAIL
    result.citation_problem_kinds = sorted({p.kind for p in out.citation_problems})
    unknown = "unknown_labels" in result.citation_problem_kinds
    resolves = all(label in run.passages for label in cited_labels) and not unknown
    checks["citations_resolve"] = PASS if resolves else FAIL
    checks["citation_checks"] = FAIL if out.citation_problems else PASS
    if case.answerable:
        checks["required_evidence_cited"] = PASS if all(g.cited for g in result.groups) else FAIL
    else:
        checks["required_evidence_cited"] = NA

    folded = _fold(answer)
    if case.must_include:
        present = all(any(_fold(alt) in folded for alt in group) for group in case.must_include)
        checks["expected_values"] = PASS if present else FAIL
    else:
        checks["expected_values"] = NA
    if case.must_not_include:
        leaked = any(_fold(s) in folded for s in case.must_not_include)
        checks["forbidden_values"] = FAIL if leaked else PASS
    else:
        checks["forbidden_values"] = NA

    result.abstained = is_abstention(answer)
    if case.answerable:
        checks["abstention"] = FAIL if result.abstained else PASS
    else:
        checks["abstention"] = PASS if result.abstained and not out.citations else FAIL
    return result


def attribute(
    case: Case, run: CaseRun, det: DeterministicResult, semantic_failed: bool, judge_error: bool
) -> list[str]:
    """Likely causes of a failing case, from what the capture shows.

    Several can apply. ``unknown`` means the case failed and the capture
    does not say why.
    """
    causes: list[str] = []
    if run.status != "ok":
        causes.append("answer_infrastructure" if run.status != "skipped" else "skipped")
        return causes
    if any(not g.retrieved for g in det.groups):
        causes.append("retrieval")
    if any(g.retrieved and not g.supplied for g in det.groups):
        causes.append("prompt_assembly")
    synthesis = any(det.checks.get(c) == FAIL for c in SYNTHESIS_CHECKS) or any(
        g.supplied and not g.cited for g in det.groups
    )
    if synthesis or semantic_failed:
        causes.append("synthesis")
    if det.checks.get("prompt_matches_capture") == FAIL or judge_error:
        causes.append("evaluator_infrastructure")
    failing = not det.passed or semantic_failed or judge_error
    if failing and not causes:
        causes.append("unknown")
    return causes if failing else []
