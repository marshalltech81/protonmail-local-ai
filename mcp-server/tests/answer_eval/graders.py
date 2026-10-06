"""Deterministic graders: run first, and never overridden by the judge.

Each check is ``pass``, ``fail`` or ``not_applicable``. Evidence groups
are scored at three stages, so a miss can be traced to where it
happened: *retrieved* (the ref's thread is among the threads the tool
searched), *supplied* (a passage of the ref's message, or of its thread
for a thread ref, was in the prompt the model received) and *cited* (the
answer cites such a passage). A message ref is met only by a passage of
that message; a passage of the thread's indexed text (no single
message) meets thread refs only.

A ``disclose_missing`` case is graded on the tool's whole disclosure
(#820): when a required group was retrieved but left out of the prompt,
or reached it only cut short of its evidence, the server's ``coverage_note`` is what
reports it, so a non-null note is the disclosure, those groups are
excused from citation and an abstention may stand. A group supplied
whole must still be cited, and the note cannot disclose a group
retrieval never found.
"""

import re
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
    whole: bool = False  # supplied with its evidence intact (``_shows_evidence``)
    cited_intact: bool = False  # cited through a passage that shows its evidence
    cited_cut: bool = False  # cited through a passage cut before its evidence
    met: bool = False  # required_evidence_cited holds for this group


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


def _mentions(folded_answer: str, value: str) -> bool:
    """Whether the answer states ``value`` as a whole value, not inside a
    longer one: ``4,860`` is not in ``14,860`` or ``4,860,000``, and
    ``2019`` is not in ``20190``. An ordinal suffix (``June 13th``) or
    zero cents (``$4,860.00``) may follow a number. Both inputs are case-folded and whitespace-collapsed,
    and the answer is bounded by the answerer's token limit."""
    pattern = (
        r"(?<![^\W_])(?<![0-9][.,])"
        + re.escape(_fold(value))
        + r"(?:(?<=[0-9])(?:st|nd|rd|th))?(?![^\W_])(?!,[0-9])(?!\.(?!00(?![0-9]))[0-9])"
    )
    return re.search(pattern, folded_answer) is not None


def _meets(ref: str, passage: Passage) -> bool:
    message_id = message_id_of(ref)
    if message_id is None:
        return passage.thread_id == thread_id_of(ref)
    return passage.message_id == message_id


def is_abstention(answer: str) -> bool:
    stripped = answer.lstrip()
    return stripped.startswith(_NOT_FOUND_PREFIX) or stripped.startswith(_NO_RESULTS)


def _shows_evidence(case: Case, passage: Passage) -> bool:
    """Whether a supplied passage still shows the evidence it carries: a
    whole passage does; one cut to fit the budget does when it keeps the
    excerpt of some reference fact sourced from its message. A cut that
    removed only trailing text is no omission (review round 3)."""
    if not passage.truncated:
        return True
    text = _fold(passage.text)
    return any(
        _fold(f.excerpt) in text
        for f in case.expected_facts
        if any(_meets(s, passage) for s in f.sources)
    )


def budget_omitted_facts(case: Case, run: CaseRun) -> list[str]:
    """IDs of the reference facts whose evidence the tool retrieved but the
    prompt budget left out or cut away: some source's thread was retrieved,
    and no supplied passage of a source shows the fact (whole, or cut but
    keeping its excerpt). These are the only facts a ``coverage_note`` can
    disclose; a fact retrieval never found is not among them."""
    if run.output is None:
        return []
    retrieved = {t.thread_id for t in run.output.threads}
    return [
        f.id
        for f in case.expected_facts
        if any(thread_id_of(s) in retrieved for s in f.sources)
        and not any(
            _meets(s, p) and (not p.truncated or _fold(f.excerpt) in _fold(p.text))
            for s in f.sources
            for p in run.passages.values()
        )
    ]


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
                cited_intact=any(
                    _meets(ref, p) and _shows_evidence(case, p) for ref in group for p in cited
                ),
                cited_cut=any(
                    _meets(ref, p) and not _shows_evidence(case, p) for ref in group for p in cited
                ),
                whole=any(
                    _meets(ref, p) and _shows_evidence(case, p) for ref in group for p in supplied
                ),
            )
        )

    checks = result.checks
    # Retrieved evidence the prompt budget left out or cut away, which is all
    # the server's note can report: a group retrieval missed stays a miss.
    omission = case.expected_handling == "disclose_missing" and any(
        g.retrieved and not g.whole for g in result.groups
    )
    disclosed = omission and out.coverage_note is not None
    if omission:
        checks["omission_disclosed"] = PASS if disclosed else FAIL
    else:
        checks["omission_disclosed"] = NA
    checks["answer_complete"] = FAIL if answer.endswith(_TRUNCATED_NOTICE) else PASS
    checks["prompt_matches_capture"] = PASS if run.prompt_consistent else FAIL
    result.citation_problem_kinds = sorted({p.kind for p in out.citation_problems})
    unknown = "unknown_labels" in result.citation_problem_kinds
    resolves = all(label in run.passages for label in cited_labels) and not unknown
    checks["citations_resolve"] = PASS if resolves else FAIL
    checks["citation_checks"] = FAIL if out.citation_problems else PASS
    if case.answerable:
        # A group is met by citing a passage that shows its evidence, or
        # excused by a disclosed omission unless the answer cites a passage
        # cut before that evidence (a guess, not a disclosure). The excuse
        # also needs an answer that abstains or rests on at least one
        # intact citation: an uncited claim about omitted evidence is a
        # guess too (review round 8).
        grounded = is_abstention(answer) or any(g.cited_intact for g in result.groups)
        stated = _fold(answer)
        for refs, g in zip(case.required_evidence, result.groups, strict=True):
            # Review round 9: when the group's facts list the values that
            # assert them, the excuse needs an abstention or an answer
            # stating none of them; one intact citation elsewhere is not
            # enough.
            threads = {thread_id_of(r) for r in refs}
            values = [
                v
                for f in case.expected_facts
                if any(thread_id_of(s) in threads for s in f.sources)
                for v in f.values
            ]
            if values:
                avoids = is_abstention(answer) or not any(_mentions(stated, v) for v in values)
            else:
                avoids = grounded
            g.met = g.cited_intact or (
                disclosed and avoids and g.retrieved and not g.whole and not g.cited_cut
            )
        met = all(g.met for g in result.groups)
        checks["required_evidence_cited"] = PASS if met else FAIL
    else:
        checks["required_evidence_cited"] = NA

    folded = _fold(answer)
    if case.must_include:
        present = all(any(_mentions(folded, alt) for alt in group) for group in case.must_include)
        checks["expected_values"] = PASS if present else FAIL
    else:
        checks["expected_values"] = NA
    if case.must_not_include:
        # Plain containment: a forbidden string (a canary) counts anywhere.
        leaked = any(_fold(s) in folded for s in case.must_not_include)
        checks["forbidden_values"] = FAIL if leaked else PASS
    else:
        checks["forbidden_values"] = NA

    result.abstained = is_abstention(answer)
    if case.answerable:
        # Abstaining is the disclosure only when no group was lost to retrieval.
        excused = disclosed and all(g.retrieved for g in result.groups)
        checks["abstention"] = FAIL if result.abstained and not excused else PASS
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
    # Not supplied, or supplied only cut short of its evidence.
    if any(g.retrieved and not g.whole for g in det.groups):
        causes.append("prompt_assembly")
    # A supplied group left uncited, or cited only through a passage cut
    # before its evidence (a guess), is the model's doing, as is a group
    # the budget dropped that stays unmet after the server disclosed the
    # omission: only the answer's own claims can fail it then (review
    # round 8). A group retrieval never found is not counted here.
    guessed = det.checks.get("omission_disclosed") == PASS and any(
        not g.met and g.retrieved and not g.whole for g in det.groups
    )
    synthesis = (
        any(det.checks.get(c) == FAIL for c in SYNTHESIS_CHECKS)
        or guessed
        or any((g.supplied and not g.cited) or g.cited_cut for g in det.groups)
    )
    if synthesis or semantic_failed:
        causes.append("synthesis")
    if det.checks.get("prompt_matches_capture") == FAIL or judge_error:
        causes.append("evaluator_infrastructure")
    failing = not det.passed or semantic_failed or judge_error
    if failing and not causes:
        causes.append("unknown")
    return causes if failing else []
