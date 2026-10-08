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
from src.tools.outputs import SummarizeThreadOutput

from tests.answer_eval.adapters import OUTPUT_MODELS, select_passages, view_of, window_cut_labels
from tests.answer_eval.cases import CASES_PATH, TOOLS, CaseError, load_cases
from tests.answer_eval.graders import FAIL, PASS, attribute, grade_run, is_abstention
from tests.answer_eval.harness import evaluate
from tests.answer_eval.judge import build_judge_prompt
from tests.answer_eval.report import build_report
from tests.answer_eval.runner import CaseRun, RunContext, run_case
from tests.conftest import FakeEmbedClient
from tests.test_answer_eval import ScriptedClient, _identity, _passage

CASES = {c.id: c for c in load_cases()}
SUMMARIZE = "summarize-pool-bids"
SUMMARIZE_WINDOW = "summarize-hall-open-points"


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


# ------------------------------------------------------------------ cases


class TestCaseSchema:
    def test_shipped_cases_cover_every_tool(self):
        by_tool: dict[str, list] = {}
        for case in CASES.values():
            by_tool.setdefault(case.tool, []).append(case)
        assert set(by_tool) == set(TOOLS)
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
            (SUMMARIZE, lambda r: r.update(tool="brief_issue")),
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

    def test_select_passages_per_tool(self):
        shown, wanted = {"E1": "shown"}, {"E1": "wanted", "E2": "wanted"}
        # summarize_thread builds the shown map first, then (when the
        # window cut the context) what the caps alone would show.
        assert select_passages("summarize_thread", [shown, wanted]) == shown
        # ask_mailbox's one map is the last one built.
        assert select_passages("ask_mailbox", [shown, wanted]) == wanted
        assert select_passages("summarize_thread", []) == {}

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


# ------------------------------------------------------------------ views


class TestViews:
    def test_unknown_tool_is_refused(self):
        with pytest.raises(KeyError):
            view_of("brief_issue", SimpleNamespace())
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
