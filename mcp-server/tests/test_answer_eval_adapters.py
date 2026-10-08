"""Answer-evaluation adapter for ``summarize_thread`` (#656): the case schema, capture, the shared
view each tool's output is graded through, the deterministic graders,
the judge prompt, the reports and the call plan, with scripted
providers only (no network). The real handler path over the synthetic
index runs in ``tests/baseline/test_answer_eval_cases.py``.
"""

import asyncio
import dataclasses
import json
from types import SimpleNamespace

import pytest
from src.lib.inference import PromptBudget
from src.tools import brief as brief_tools
from src.tools import intelligence
from src.tools.outputs import BriefIssueOutput, CheckConclusionOutput, SummarizeThreadOutput

from tests.answer_eval.adapters import OUTPUT_MODELS, select_passages, view_of, window_cut_labels
from tests.answer_eval.cases import CASES_PATH, TOOLS, CaseError, load_cases, thread_id_of
from tests.answer_eval.graders import FAIL, NA, PASS, attribute, grade_run, is_abstention
from tests.answer_eval.harness import evaluate
from tests.answer_eval.judge import build_judge_prompt
from tests.answer_eval.report import build_report
from tests.answer_eval.runner import CaseRun, RunContext, capture_evidence_maps, run_case
from tests.conftest import FakeEmbedClient
from tests.test_answer_eval import ScriptedClient, _identity, _passage

CASES = {c.id: c for c in load_cases()}
SUMMARIZE = "summarize-pool-bids"
SUMMARIZE_WINDOW = "summarize-hall-open-points"
# The experimental tools' smoke cases (#1240): one answer and one
# abstention each.
BRIEF = "brief-pool-bids"
BRIEF_ABSTAIN = "brief-cabin-wifi"
CHECK = "check-roof-total"
CHECK_ABSTAIN = "check-electrician-quote"


def _ctx(db, inference, **kw) -> RunContext:
    return RunContext(
        db=db,
        embed_client=FakeEmbedClient(),
        inference_client=inference,
        prompt_budget=PromptBudget(),
        **kw,
    )


def _summarize_run(answer: str, passages, cited, *, coverage_note=None, thread="t05") -> CaseRun:
    """A hand-built summarize_thread run: the tool's output shape, as
    ``AnswerView`` reads it."""
    output = SimpleNamespace(
        summary=answer,
        coverage_note=coverage_note,
        thread=SimpleNamespace(thread_id=f"{thread}.1@baseline.example"),
        citations=[SimpleNamespace(label=label) for label in cited],
        statements=[],
        citation_problems=[],
        repair_attempted=False,
    )
    return CaseRun(
        case_id="x",
        status="ok",
        tool="summarize_thread",
        output=output,  # type: ignore[arg-type]
        passages={p.label: p for p in passages},
        timings_ms={"answer_total": 1.0},
    )


# ------------------------------------------- recorded experimental outputs


def _thread(ref: str = "t05") -> dict:
    return {
        "thread_id": thread_id_of(ref),
        "subject": "synthetic subject",
        "folder": "INBOX",
        "participants": [],
        "participant_count": 0,
        "date_first": "2026-01-01T00:00:00Z",
        "date_last": "2026-01-02T00:00:00Z",
        "message_count": 1,
        "has_attachments": False,
        "snippet": "",
    }


def _citation(label: str, ref: str) -> dict:
    message = f"{ref}@baseline.example"
    return {
        "label": label,
        "chunk_id": f"chunk-{label}",
        "claimant_id": f"{message}#0000abcd",
        "message_id": message,
        "thread_id": thread_id_of(ref),
        "sender": None,
        "sent_at": None,
        "occurred_at": None,
        "source": "body",
        "attachment_id": None,
        "attachment_filename": None,
        "char_start": 0,
        "char_end": 10,
    }


def _brief(**sections) -> dict:
    brief = {
        "chronology": [],
        "positions": [],
        "decisions": [],
        "open_questions": [],
        "conflicts": [],
        "insufficient_evidence": False,
    }
    brief.update(sections)
    return brief


def _brief_output(
    brief: dict | None,
    citations=(),
    problems=(),
    *,
    status: str = "ok",
    raw_text: str | None = None,
    thread: str = "t05",
) -> BriefIssueOutput:
    """A ``brief_issue`` output as the tool returns it (#1240)."""
    return BriefIssueOutput.model_validate(
        {
            "experimental": True,
            "status": status,
            "brief": brief,
            "raw_text": raw_text,
            "as_of": None,
            "citations": list(citations),
            "citation_problems": list(problems),
            "repair_attempted": bool(problems),
            "threads": [_thread(thread)],
        }
    )


def _finding(relation: str, explanation: str, sources: list[tuple[str, str]]) -> dict:
    return {
        "relation": relation,
        "explanation": explanation,
        "labels": [label for label, _ in sources],
        "sources": [{**_citation(label, ref), "excerpt": "start"} for label, ref in sources],
    }


def _check_output(
    findings=(),
    *,
    verdict: str | None = "The passages bear on it.",
    insufficient: bool | None = False,
    problems=(),
    status: str = "ok",
    raw_text: str | None = None,
    thread: str = "t01",
) -> CheckConclusionOutput:
    """A ``check_conclusion`` output as the tool returns it (#1240)."""
    return CheckConclusionOutput.model_validate(
        {
            "experimental": True,
            "status": status,
            "verdict_summary": verdict,
            "findings": list(findings),
            "insufficient_evidence": insufficient,
            "raw_text": raw_text,
            "as_of": None,
            "citation_problems": list(problems),
            "repair_attempted": False,
            "threads": [_thread(thread)],
        }
    )


def _experimental_run(tool: str, output, passages) -> CaseRun:
    return CaseRun(
        case_id="x",
        status="ok",
        tool=tool,
        output=output,
        passages={p.label: p for p in passages},
        timings_ms={"answer_total": 1.0},
    )


# ------------------------------------------------------------------ cases


class TestCaseSchema:
    def test_shipped_cases_cover_every_tool(self):
        by_tool: dict[str, list] = {}
        for case in CASES.values():
            by_tool.setdefault(case.tool, []).append(case)
        assert set(by_tool) == set(TOOLS)
        assert set(TOOLS) == {"ask_mailbox", "summarize_thread", "brief_issue", "check_conclusion"}
        # #1240: smoke cases only, an answer and an abstention per tool;
        # the measurement cases are #291's.
        for tool, prefix, answer, abstain in (
            ("brief_issue", "brief-", BRIEF, BRIEF_ABSTAIN),
            ("check_conclusion", "check-", CHECK, CHECK_ABSTAIN),
        ):
            assert all(c.id.startswith(prefix) for c in by_tool[tool])
            assert CASES[answer].tool == CASES[abstain].tool == tool
            assert CASES[answer].expected_handling == "answer"
            assert CASES[abstain].expected_handling == "abstain"
        assert len(by_tool["summarize_thread"]) >= 2
        assert all(c.id.startswith("summarize-") for c in by_tool["summarize_thread"])
        # One plain case and one with a known gap.
        assert CASES[SUMMARIZE].expected_handling == "answer"
        assert CASES[SUMMARIZE_WINDOW].expected_handling == "disclose_missing"
        assert CASES[SUMMARIZE_WINDOW].prompt_tokens is not None

    def test_task_text_and_embedded_query_per_tool(self):
        ask = CASES["ask-roof-total"]
        assert ask.question == ask.arguments["question"] == ask.embedded_query
        summarize = CASES[SUMMARIZE]
        assert summarize.embedded_query is None  # a direct thread lookup embeds nothing
        assert "brief" in summarize.question and "t05.1@baseline.example" in summarize.question
        # #1240: each experimental tool embeds its topic or conclusion.
        brief = CASES[BRIEF]
        assert brief.embedded_query == brief.arguments["topic"]
        assert brief.question.endswith(brief.arguments["topic"])
        check = CASES[CHECK]
        assert check.embedded_query == check.arguments["conclusion"]
        assert check.question.endswith(check.arguments["conclusion"])

    @pytest.mark.parametrize(
        ("case_id", "mutate"),
        [
            (SUMMARIZE, lambda r: r.update(id="ask-pool-bids")),  # prefix names the tool
            (SUMMARIZE, lambda r: r["arguments"].update(question="why?")),
            (SUMMARIZE, lambda r: r["arguments"].update(thread_id="the pool thread")),
            (SUMMARIZE, lambda r: r["arguments"].pop("thread_id")),
            (SUMMARIZE, lambda r: r["arguments"].update(style=3)),
            # Codex round 1: the handler summarizes an unknown style as
            # brief, so a typo would grade a task the case does not state.
            (SUMMARIZE, lambda r: r["arguments"].update(style="action_item")),
            (SUMMARIZE, lambda r: r["arguments"].update(style="Brief")),
            # Codex round 3: the handler sees only the named thread, so
            # evidence or a fact source in another thread can never be
            # retrieved and would grade as a product regression.
            (SUMMARIZE, lambda r: r["required_evidence"].append(["t01.1"])),
            (SUMMARIZE, lambda r: r["required_evidence"][0].append("t01")),
            (SUMMARIZE, lambda r: r["expected_facts"][0]["sources"].append("t01.1")),
            (SUMMARIZE, lambda r: r.update(tool="extract_from_emails")),  # no adapter (#1137)
            (SUMMARIZE, lambda r: r.update(tool="brief_issue")),  # summarize- prefix
            # #1240: each experimental tool's prefix, required argument,
            # allowed keys and scope filter types.
            (BRIEF, lambda r: r.update(id="check-pool-bids")),
            (BRIEF, lambda r: r.update(tool="check_conclusion")),
            (BRIEF, lambda r: r["arguments"].pop("topic")),
            (BRIEF, lambda r: r["arguments"].update(topic="  ")),
            (BRIEF, lambda r: r["arguments"].update(topic=3)),
            (BRIEF, lambda r: r["arguments"].update(question="why?")),
            (BRIEF, lambda r: r["arguments"].update(conclusion="it is")),
            (BRIEF, lambda r: r["arguments"].update(thread_id="t05.1@baseline.example")),
            (BRIEF, lambda r: r["arguments"].update(folders="INBOX")),
            (BRIEF, lambda r: r["arguments"].update(folders=["INBOX", 3])),
            (BRIEF, lambda r: r["arguments"].update(from_addr=["a@baseline.example"])),
            (BRIEF, lambda r: r["arguments"].update(date_from=20240101)),
            (BRIEF, lambda r: r["arguments"].update(date_to=None)),
            (BRIEF, lambda r: r["arguments"].update(max_threads="5")),
            (BRIEF, lambda r: r["arguments"].update(max_threads=True)),
            (BRIEF, lambda r: r["arguments"].update(max_threads=0)),
            (BRIEF, lambda r: r["arguments"].update(max_threads=1000)),  # would be clamped
            (CHECK, lambda r: r.update(id="brief-roof-total")),
            (CHECK, lambda r: r["arguments"].pop("conclusion")),
            (CHECK, lambda r: r["arguments"].update(conclusion="")),
            (CHECK, lambda r: r["arguments"].update(conclusion=["it is"])),
            # The handler refuses a conclusion over its 2,000 characters.
            (CHECK, lambda r: r["arguments"].update(conclusion="x" * 2001)),
            (CHECK, lambda r: r["arguments"].update(topic="roof")),
            (CHECK, lambda r: r["arguments"].update(style="brief")),
            (CHECK, lambda r: r["arguments"].update(max_threads=2.5)),
            # The same filter types for ask_mailbox, which shares them.
            ("ask-roof-total", lambda r: r["arguments"].update(folders="INBOX")),
            ("ask-roof-total", lambda r: r["arguments"].update(max_threads="5")),
        ],
    )
    def test_schema_breaches_are_rejected(self, tmp_path, case_id, mutate):
        data = json.loads(CASES_PATH.read_text())
        row = next(r for r in data["cases"] if r["id"] == case_id)
        mutate(row)
        path = tmp_path / "cases.json"
        path.write_text(json.dumps(data))
        with pytest.raises(CaseError):
            load_cases(path)


# ---------------------------------------------------------------- capture


class TestCapture:
    def test_summarize_runs_the_real_handler_and_captures_the_shown_passages(self, chunked_db):
        case = dataclasses.replace(
            CASES[SUMMARIZE], arguments={"thread_id": "t-alpha", "style": "brief"}
        )
        inference = ScriptedClient("Invoice 12345 is due March 31 [E2].")
        run = asyncio.run(run_case(case, _ctx(chunked_db, inference)))
        assert run.status == "ok" and isinstance(run.output, SummarizeThreadOutput)
        assert run.tool == "summarize_thread"
        # E1 is the thread's indexed text, E2 its one body chunk.
        assert {label: p.source for label, p in run.passages.items()} == {
            "E1": "thread",
            "E2": "body",
        }
        assert run.passages["E2"].chunk_id == "alpha-c1"
        assert run.passages["E1"].header == "[E1 | thread text]"
        for p in run.passages.values():
            assert p.header in run.calls[0].user
        assert run.prompt_consistent
        assert len(run.calls) == 1 and run.calls[0].json_schema is None
        view = run.view
        assert view.answer == run.output.summary
        assert [t.thread_id for t in view.threads] == ["t-alpha"]
        assert [c.label for c in view.citations] == ["E2"]
        assert view.repair_attempted is False

    @pytest.mark.parametrize(
        ("case_id", "reply", "model"),
        [
            (
                BRIEF,
                json.dumps(
                    _brief(decisions=[{"decision": "Invoice 12345 is due", "labels": ["E1"]}])
                ),
                BriefIssueOutput,
            ),
            (
                CHECK,
                json.dumps(
                    {
                        "verdict_summary": "Supported.",
                        "findings": [
                            {"relation": "supports", "explanation": "Due", "labels": ["E1"]}
                        ],
                        "insufficient_evidence": False,
                    }
                ),
                CheckConclusionOutput,
            ),
        ],
    )
    def test_experimental_tools_run_their_real_handlers(self, chunked_db, case_id, reply, model):
        """#1240: the eval registers the experimental handlers on its own
        stub server, whatever MCP_EXPERIMENTAL_TOOLS says, and captures
        the evidence map they build through ``brief``'s own import of
        ``_build_evidence``."""
        case = CASES[case_id]
        argument = "topic" if case.tool == "brief_issue" else "conclusion"
        case = dataclasses.replace(case, arguments={argument: "invoice march"})
        inference = ScriptedClient(reply)
        run = asyncio.run(run_case(case, _ctx(chunked_db, inference)))
        assert run.status == "ok", (run.error, run.error_detail)
        assert isinstance(run.output, model) and run.tool == case.tool
        assert run.output.repair_attempted is False
        assert len(run.calls) == 1
        assert run.passages and all(p.source == "body" for p in run.passages.values())
        assert run.passages["E1"].chunk_id == "alpha-c1"
        for p in run.passages.values():
            assert p.header and p.header in run.calls[0].user
        assert run.prompt_consistent
        view = run.view
        assert [c.label for c in view.citations] == ["E1"]
        assert [s.labels for s in view.statements][-1] == ["E1"]
        assert view.abstained is False and view.complete is True

    def test_capture_restores_both_evidence_builder_names(self):
        original = intelligence._build_evidence
        with capture_evidence_maps([]):
            assert brief_tools._build_evidence is not original
            assert intelligence._build_evidence is not original
        assert brief_tools._build_evidence is original
        assert intelligence._build_evidence is original

    def test_select_passages_per_tool(self):
        shown, wanted = {"E1": "shown"}, {"E1": "wanted", "E2": "wanted"}
        # summarize_thread builds the shown map first, then (when the
        # window cut the context) what the caps alone would show.
        assert select_passages("summarize_thread", [shown, wanted]) == shown
        # ask_mailbox's one map is the last one built.
        assert select_passages("ask_mailbox", [shown, wanted]) == wanted
        assert select_passages("summarize_thread", []) == {}
        # The experimental tools build one map, through _build_evidence;
        # none when retrieval found no message passage.
        assert select_passages("brief_issue", [shown]) == shown
        assert select_passages("check_conclusion", [shown]) == shown
        assert select_passages("brief_issue", []) == {}

    def test_output_models_cover_every_tool(self):
        assert set(OUTPUT_MODELS) == set(TOOLS)

    def test_window_cut_summary_passages_are_marked_truncated(self):
        """Codex round 2: when the window cut the summary context, the
        handler also builds the map its caps alone would show; a passage
        shorter in the shown map than there (E1's thread text included,
        which has no chunk offsets) was cut by the window."""
        shown = {
            "E1": SimpleNamespace(text="start of the thread"),
            "E7": SimpleNamespace(text="newest reply, whole"),
        }
        wanted = {
            "E1": SimpleNamespace(text="start of the thread and the rest of it"),
            "E4": SimpleNamespace(text="left out entirely"),
            "E7": SimpleNamespace(text="newest reply, whole"),
        }
        assert window_cut_labels("summarize_thread", [shown, wanted]) == {"E1"}
        assert window_cut_labels("summarize_thread", [shown]) == set()  # nothing cut
        assert window_cut_labels("ask_mailbox", [shown, wanted]) == set()
        assert window_cut_labels("brief_issue", [shown, wanted]) == set()


# ------------------------------------------------------------------ views


class TestExperimentalViews:
    """#1240: ``brief_issue`` and ``check_conclusion`` outputs, as the tools
    return them, seen through their adapters."""

    def test_brief_entries_become_statements(self):
        output = _brief_output(
            _brief(
                chronology=[
                    {
                        "date": "2026-01-02",
                        "date_source": "sent",
                        "actor": "Committee",
                        "event": "received three bids",
                        "labels": ["E1"],
                    }
                ],
                positions=[{"actor": "Chair", "position": "prefers Bluewater", "labels": ["E2"]}],
                decisions=[{"decision": "present two bids", "labels": ["E2"]}],
                open_questions=[{"question": "which bid wins", "labels": ["E1"]}],
                conflicts=[{"description": "the totals differ", "labels": ["E1", "E2"]}],
            ),
            [_citation("E1", "t05.1"), _citation("E2", "t05.3")],
        )
        view = view_of("brief_issue", output)
        assert [(s.section, s.item, s.labels) for s in view.statements] == [
            ("chronology", 0, ["E1"]),
            ("positions", 0, ["E2"]),
            ("decisions", 0, ["E2"]),
            ("open_questions", 0, ["E1"]),
            ("conflicts", 0, ["E1", "E2"]),
        ]
        assert all(s.status == "cited" and s.stance is None for s in view.statements)
        texts = [s.text for s in view.statements]
        assert texts[0] == "2026-01-02 (sent) Committee: received three bids [E1]"
        assert texts[1] == "Chair: prefers Bluewater [E2]"
        assert texts[4] == "Conflict: the totals differ [E1, E2]"
        # The answer is the entries, one per line, as the graders read it.
        assert view.answer == "\n".join(texts)
        assert [c.label for c in view.citations] == ["E1", "E2"]
        assert [t.thread_id for t in view.threads] == ["t05.1@baseline.example"]
        assert view.citation_problems == []
        assert (view.abstained, view.complete, view.coverage_note) == (False, True, None)
        assert view.repair_attempted is False

    def test_insufficient_evidence_brief_is_an_abstention(self):
        output = _brief_output(_brief(insufficient_evidence=True), thread="t03")
        view = view_of("brief_issue", output)
        assert view.statements == [] and view.citations == []
        assert view.abstained is True
        assert view.answer.startswith("Insufficient evidence")
        assert not is_abstention(view.answer)  # the tool's flag, not a prefix, says so
        case = CASES[BRIEF_ABSTAIN]
        det = grade_run(case, _experimental_run("brief_issue", output, []))
        assert det.abstained is True
        assert det.checks["abstention"] == PASS
        assert det.passed, det.checks
        # The same brief on an answerable case is a failed abstention.
        det = grade_run(CASES[BRIEF], _experimental_run("brief_issue", output, []))
        assert det.checks["abstention"] == FAIL

    def test_brief_with_citation_problems(self):
        output = _brief_output(
            _brief(
                decisions=[
                    {"decision": "present two bids", "labels": ["E1", "E9"]},
                    {"decision": "a guess", "labels": ["E9"]},
                    {"decision": "no source", "labels": []},
                ]
            ),
            [_citation("E1", "t05.1")],
            [
                {"section": "decisions", "item": 0, "kind": "unknown_labels", "labels": ["E9"]},
                {"section": "decisions", "item": 1, "kind": "unknown_labels", "labels": ["E9"]},
                {"section": "decisions", "item": 2, "kind": "no_citations", "labels": []},
            ],
        )
        view = view_of("brief_issue", output)
        # Statement labels are the supplied passages an entry cites, as
        # ask_mailbox's are; its text keeps every label it gave.
        assert [(s.labels, s.status) for s in view.statements] == [
            (["E1"], "cited"),
            ([], "invalid"),
            ([], "uncited"),
        ]
        assert view.statements[1].text == "Decision: a guess [E9]"
        assert [p.kind for p in view.citation_problems] == [
            "unknown_labels",
            "unknown_labels",
            "no_citations",
        ]
        assert view.repair_attempted is True
        run = _experimental_run("brief_issue", output, [_passage("E1", "t05.1")])
        det = grade_run(CASES[BRIEF], run)
        assert det.checks["citation_checks"] == FAIL
        assert det.checks["citations_resolve"] == FAIL
        assert det.citation_problem_kinds == ["no_citations", "unknown_labels"]

    @pytest.mark.parametrize("status", ["invalid_json", "truncated"])
    def test_brief_without_a_parsed_reply_is_incomplete(self, status):
        output = _brief_output(None, status=status, raw_text='{"chronology": [')
        view = view_of("brief_issue", output)
        assert view.complete is False and view.abstained is False
        assert view.statements == [] and view.answer == '{"chronology": ['
        det = grade_run(CASES[BRIEF], _experimental_run("brief_issue", output, []))
        assert det.checks["answer_complete"] == FAIL
        assert not det.passed

    def test_check_findings_become_statements(self):
        output = _check_output(
            [
                _finding("supports", "The estimate totals $14,200", [("E1", "t01.1")]),
                _finding("qualifies", "Before tax", [("E1", "t01.1"), ("E2", "t01.2")]),
            ],
            verdict="Supported, with a qualification.",
        )
        view = view_of("check_conclusion", output)
        # The verdict first, citing what the findings cite (the tool
        # checks its quotes against those passages), then each finding.
        assert [(s.section, s.item, s.stance, s.labels) for s in view.statements] == [
            ("verdict", 0, None, ["E1", "E2"]),
            ("findings", 0, "supports", ["E1"]),
            ("findings", 1, "qualifies", ["E1", "E2"]),
        ]
        assert view.statements[0].text == "Verdict: Supported, with a qualification."
        assert view.statements[1].text == "SUPPORTS: The estimate totals $14,200 [E1]"
        assert view.answer == "\n".join(s.text for s in view.statements)
        # Each cited passage once, in first-cited order.
        assert [c.label for c in view.citations] == ["E1", "E2"]
        assert [t.thread_id for t in view.threads] == ["t01.1@baseline.example"]
        assert (view.abstained, view.complete, view.coverage_note) == (False, True, None)

    def test_check_with_no_findings(self):
        output = _check_output(
            verdict="The passages do not address this conclusion.", insufficient=True
        )
        view = view_of("check_conclusion", output)
        assert view.citations == []
        assert [(s.section, s.labels, s.status) for s in view.statements] == [
            ("verdict", [], "uncited")
        ]
        assert view.abstained is True
        assert view.answer.startswith("Insufficient evidence")
        det = grade_run(CASES[CHECK_ABSTAIN], _experimental_run("check_conclusion", output, []))
        assert det.checks["abstention"] == PASS
        assert det.passed, det.checks
        # No findings without the flag is the tool's own citation problem,
        # not an abstention.
        unflagged = _check_output(
            verdict="Nothing found.",
            problems=[{"item": None, "kind": "no_findings_but_sufficient", "labels": []}],
        )
        view = view_of("check_conclusion", unflagged)
        assert view.abstained is False
        det = grade_run(CASES[CHECK], _experimental_run("check_conclusion", unflagged, []))
        assert det.checks["citation_checks"] == FAIL
        assert det.checks["abstention"] == PASS  # it did not abstain
        assert det.checks["required_evidence_cited"] == FAIL

    def test_check_without_a_parsed_reply_is_incomplete(self):
        output = _check_output(
            verdict=None, insufficient=None, status="invalid_json", raw_text="not json"
        )
        view = view_of("check_conclusion", output)
        assert (view.complete, view.abstained, view.answer) == (False, False, "not json")
        assert view.statements == []
        det = grade_run(CASES[CHECK], _experimental_run("check_conclusion", output, []))
        assert det.checks["answer_complete"] == FAIL


class TestViews:
    def test_unknown_tool_is_refused(self):
        # No extraction adapter (#1137), so no extraction marker either.
        with pytest.raises(KeyError):
            view_of("extract_from_emails", SimpleNamespace())

    def test_abstention_is_the_tools_own_not_found_text_only(self):
        """Codex round 4: abstention is recognized from the tools' own
        not-found and no-results texts, for every tool alike; a summary
        that merely opens with other words is an answer."""
        assert is_abstention("Not found in the provided emails: nothing answers this.")
        assert is_abstention("No relevant emails found for this question.")
        assert not is_abstention("No records extracted from the thread were disputed [E1].")


# ---------------------------------------------------------------- grading


class TestGrading:
    def test_summarize_right_and_wrong_answers(self):
        case = CASES[SUMMARIZE]
        passages = [_passage("E1", "t05", source="thread"), _passage("E2", "t05.1")]
        right = _summarize_run(
            "Bluewater Pools bid $38,400 [E2]. The committee will present Bluewater and "
            "Crestline to the board [E1].",
            passages,
            ["E2", "E1"],
        )
        det = grade_run(case, right)
        assert det.passed, det.checks
        assert attribute(case, right, det, False, False) == []
        wrong = _summarize_run("Tidewater Plaster was chosen [E1].", passages, ["E1"])
        det = grade_run(case, wrong)
        assert det.checks["expected_values"] == FAIL
        assert det.checks["required_evidence_cited"] == PASS
        assert "synthesis" in attribute(case, wrong, det, False, False)
        uncited = _summarize_run("Bluewater Pools bid $38,400 and Crestline too.", passages, [])
        det = grade_run(case, uncited)
        assert det.checks["required_evidence_cited"] == FAIL

    def test_summarize_disclosed_window_omission(self):
        """The capped-thread case: the window left out the message holding
        the open points, the server's note says so, and a summary that
        does not guess them passes while one that states them fails."""
        case = CASES[SUMMARIZE_WINDOW]
        shown = [_passage("E1", "t62", source="thread"), _passage("E7", "t62.56")]
        note = "Evidence note: to fit the prompt budget, 5 retrieved passages were left out."
        honest = _summarize_run(
            "Morgan reports the hall work is on track [E7].",
            shown,
            ["E7"],
            coverage_note=note,
            thread="t62",
        )
        det = grade_run(case, honest)
        assert det.checks["omission_disclosed"] == PASS
        assert det.checks["required_evidence_cited"] == PASS
        assert det.passed, det.checks
        assert det.prompt_coverage == 0.0 and det.retrieval_recall == 1.0
        guess = _summarize_run(
            "Blair will send the warranty redline by October 16 [E7].",
            shown,
            ["E7"],
            coverage_note=note,
            thread="t62",
        )
        det = grade_run(case, guess)
        assert det.checks["required_evidence_cited"] == FAIL
        assert "synthesis" in attribute(case, guess, det, False, False)
        silent = _summarize_run(
            "Morgan reports the hall work is on track [E7].", shown, ["E7"], thread="t62"
        )
        assert grade_run(case, silent).checks["omission_disclosed"] == FAIL

    def test_brief_smoke_case_right_and_wrong(self):
        """#1240: the deterministic graders run on a brief unchanged, through
        its view; only abstention and completeness read the tool's flags."""
        case = CASES[BRIEF]
        passages = [_passage("E1", "t05.1"), _passage("E2", "t05.3")]
        citations = [_citation("E1", "t05.1"), _citation("E2", "t05.3")]
        right = _brief_output(
            _brief(
                chronology=[
                    {
                        "date": None,
                        "date_source": "unknown",
                        "actor": "Committee",
                        "event": "Bluewater Pools bid $38,400 and Crestline Aquatics $44,900",
                        "labels": ["E1"],
                    }
                ],
                decisions=[
                    {"decision": "present Bluewater and Crestline to the board", "labels": ["E2"]}
                ],
            ),
            citations,
        )
        det = grade_run(case, _experimental_run("brief_issue", right, passages))
        assert det.passed, det.checks
        assert det.checks["omission_disclosed"] == NA
        assert det.citation_coverage == 1.0
        wrong = _brief_output(
            _brief(decisions=[{"decision": "Tidewater Plaster was chosen", "labels": ["E1"]}]),
            citations[:1],
        )
        run = _experimental_run("brief_issue", wrong, passages)
        det = grade_run(case, run)
        assert det.checks["expected_values"] == FAIL
        assert "synthesis" in attribute(case, run, det, False, False)

    def test_check_smoke_case_right_and_wrong(self):
        case = CASES[CHECK]
        passages = [_passage("E1", "t01.1")]
        right = _check_output(
            [_finding("supports", "The estimate's total is $14,200", [("E1", "t01.1")])],
            verdict="Supported: the estimate totals $14,200.",
        )
        det = grade_run(case, _experimental_run("check_conclusion", right, passages))
        assert det.passed, det.checks
        wrong = _check_output(
            [_finding("contradicts", "The estimate's total is $12,400", [("E1", "t01.1")])],
            verdict="Contradicted.",
        )
        det = grade_run(case, _experimental_run("check_conclusion", wrong, passages))
        assert det.checks["expected_values"] == FAIL
        assert det.checks["required_evidence_cited"] == PASS


# ------------------------------------------------------------------ judge


class TestJudgePrompt:
    def test_ask_prompt_opens_with_the_question_as_before(self):
        case = CASES["ask-roof-total"]
        prompt = build_judge_prompt(case, "a [E1]", {"E1": _passage("E1", "t01.1")}, ["a [E1]"])
        assert prompt.startswith(f"Question: {case.question}\n")
        assert "Task (" not in prompt

    def test_other_tools_state_the_tool_and_its_task(self):
        case = CASES[SUMMARIZE]
        prompt = build_judge_prompt(case, "s [E1]", {"E1": _passage("E1", "t05")}, ["s [E1]"])
        assert prompt.startswith(f"Task (summarize_thread): {case.question}\n")
        for case_id, tool in ((BRIEF, "brief_issue"), (CHECK, "check_conclusion")):
            case = CASES[case_id]
            prompt = build_judge_prompt(case, "a [E1]", {"E1": _passage("E1", "t01.1")}, ["a"])
            assert prompt.startswith(f"Task ({tool}): {case.question}\n")


# ---------------------------------------------------------------- reports


class TestReports:
    def test_records_carry_the_tool_and_reports_aggregate_by_tool(self, chunked_db):
        summarize = dataclasses.replace(
            CASES[SUMMARIZE], arguments={"thread_id": "t-alpha", "style": "brief"}
        )
        ask = dataclasses.replace(CASES["ask-roof-total"], arguments={"question": "invoice"})
        rows_s, _ = asyncio.run(
            evaluate([summarize], _ctx(chunked_db, ScriptedClient("Invoice 12345 [E2].")))
        )
        rows_a, _ = asyncio.run(
            evaluate([ask], _ctx(chunked_db, ScriptedClient("Invoice 12345 [E1].")))
        )
        rows = rows_s + rows_a
        assert [r["tool"] for r in rows] == ["summarize_thread", "ask_mailbox"]
        assert rows[0]["repair_attempted"] is False
        report = build_report(_identity(), rows, False)
        by_tool = report["aggregates"]["by_tool"]
        assert set(by_tool) == {"summarize_thread", "ask_mailbox"}
        assert by_tool["summarize_thread"]["cases"] == 1

    def test_experimental_records_carry_their_tool(self, chunked_db):
        """#1240: a brief's record goes through the shared report path."""
        case = dataclasses.replace(CASES[BRIEF], arguments={"topic": "invoice march"})
        reply = json.dumps(_brief(decisions=[{"decision": "Invoice 12345", "labels": ["E1"]}]))
        rows, details = asyncio.run(evaluate([case], _ctx(chunked_db, ScriptedClient(reply))))
        assert [r["tool"] for r in rows] == ["brief_issue"]
        assert rows[0]["status"] == "ok" and rows[0]["repair_attempted"] is False
        assert details[0]["answer"] == "Decision: Invoice 12345 [E1]"
        report = build_report(_identity(), rows, False)
        assert report["aggregates"]["by_tool"]["brief_issue"]["cases"] == 1
