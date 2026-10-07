"""Answer-evaluation adapters for ``summarize_thread`` and
``extract_from_emails`` (#656): the case schema, capture, the shared
view each tool's output is graded through, the deterministic graders,
the judge prompt, the reports and the call plan, with scripted
providers only (no network). The real handler path over the synthetic
index runs in ``tests/baseline/test_answer_eval_cases.py``.
"""

import asyncio
import dataclasses
import json
import logging
from types import SimpleNamespace

import pytest
from src.lib.inference import PromptBudget
from src.tools.outputs import AnswerStatement, ExtractFromEmailsOutput, SummarizeThreadOutput

from tests.answer_eval import __main__ as cli
from tests.answer_eval.adapters import (
    NO_RECORDS,
    OUTPUT_MODELS,
    planned_calls,
    select_passages,
    view_of,
)
from tests.answer_eval.cases import CASES_PATH, TOOLS, CaseError, load_cases
from tests.answer_eval.graders import FAIL, NA, PASS, attribute, grade_run, is_abstention
from tests.answer_eval.harness import evaluate
from tests.answer_eval.judge import JudgeOutcome, build_judge_prompt
from tests.answer_eval.report import build_report, case_record, detail_record
from tests.answer_eval.runner import CaseRun, RunContext, run_case
from tests.conftest import FakeEmbedClient
from tests.test_answer_eval import MARKER, ScriptedClient, _identity, _passage

CASES = {c.id: c for c in load_cases()}
SUMMARIZE = "summarize-pool-bids"
SUMMARIZE_WINDOW = "summarize-hall-open-points"
EXTRACT = "extract-pool-bids"
EXTRACT_ATTACHMENT = "extract-roof-estimate"


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


def _extract_run(records, passages, cited, *, notice=None, threads=("t05",)) -> CaseRun:
    """A hand-built extract_from_emails run."""
    output = SimpleNamespace(
        records=records,
        citations=[SimpleNamespace(label=label) for label in cited],
        citation_problems=[],
        notice=notice,
        threads=[SimpleNamespace(thread_id=f"{t}.1@baseline.example") for t in threads],
    )
    return CaseRun(
        case_id="x",
        status="ok",
        tool="extract_from_emails",
        output=output,  # type: ignore[arg-type]
        passages={p.label: p for p in passages},
        timings_ms={"answer_total": 1.0},
    )


def _pool_record(contractor="Bluewater Pools", bid="$38,400", labels=("E1",)) -> dict:
    return {
        "contractor": contractor,
        "bid": bid,
        "_source_thread": "Pool resurfacing bids",
        "_date": "2024-01-10",
        "_evidence": {"contractor": list(labels), "bid": list(labels)},
    }


# ------------------------------------------------------------------ cases


class TestCaseSchema:
    def test_shipped_cases_cover_every_tool(self):
        by_tool: dict[str, list] = {}
        for case in CASES.values():
            by_tool.setdefault(case.tool, []).append(case)
        assert set(by_tool) == set(TOOLS)
        for tool in ("summarize_thread", "extract_from_emails"):
            assert len(by_tool[tool]) >= 2, tool
            assert all(c.id.startswith(tool.split("_")[0] + "-") for c in by_tool[tool])
        # One plain case and one with a known gap per tool.
        assert CASES[SUMMARIZE].expected_handling == "answer"
        assert CASES[SUMMARIZE_WINDOW].expected_handling == "disclose_missing"
        assert CASES[SUMMARIZE_WINDOW].prompt_tokens is not None
        assert CASES[EXTRACT].category == "exact_fact"
        assert CASES[EXTRACT_ATTACHMENT].category == "attachment_only"
        for cid in (EXTRACT, EXTRACT_ATTACHMENT):
            # A bounded number of paid calls per extract case.
            assert CASES[cid].arguments["limit"] <= 3

    def test_task_text_and_embedded_query_per_tool(self):
        ask = CASES["ask-roof-total"]
        assert ask.question == ask.arguments["question"] == ask.embedded_query
        summarize = CASES[SUMMARIZE]
        assert summarize.embedded_query is None  # a direct thread lookup embeds nothing
        assert "brief" in summarize.question and "t05.1@baseline.example" in summarize.question
        extract = CASES[EXTRACT]
        assert extract.embedded_query == extract.arguments["query"]
        assert extract.arguments["query"] in extract.question
        assert "contractor" in extract.question and "bid" in extract.question

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
            (EXTRACT, lambda r: r["arguments"].pop("schema")),
            (EXTRACT, lambda r: r["arguments"].update(schema="vendor")),
            (EXTRACT, lambda r: r["arguments"].update(schema={})),
            (EXTRACT, lambda r: r["arguments"].update(query="")),
            (EXTRACT, lambda r: r["arguments"].update(limit=0)),
            (EXTRACT, lambda r: r["arguments"].update(limit="5")),
            (EXTRACT, lambda r: r["arguments"].update(question="why?")),
            (EXTRACT, lambda r: r.update(tool="brief_issue")),
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
        assert view.repair_attempted is False and view.records is None

    def test_extract_runs_the_real_handler_and_captures_every_threads_passages(self, chunked_db):
        case = dataclasses.replace(
            CASES[EXTRACT], arguments={"query": "invoice", "schema": {"invoice": "string"}}
        )
        first = json.dumps({"invoice": "12345", "_evidence": {"invoice": ["E1"]}})
        inference = ScriptedClient(first, "null")
        run = asyncio.run(run_case(case, _ctx(chunked_db, inference)))
        assert run.status == "ok" and isinstance(run.output, ExtractFromEmailsOutput)
        assert run.tool == "extract_from_emails"
        # One call per searched thread, each with its own labelled passages;
        # the capture keeps every thread's map, numbered across the call.
        threads = [t.thread_id for t in run.output.threads]
        assert len(run.calls) == len(threads) >= 2
        assert sorted(run.passages) == sorted(f"E{n}" for n in range(1, len(run.passages) + 1))
        assert {p.thread_id for p in run.passages.values()} == set(threads)
        for p in run.passages.values():
            assert any(p.header in call.user for call in run.calls)
        assert run.prompt_consistent
        view = run.view
        assert view.records == run.output.records
        assert view.statements == [
            AnswerStatement(text='invoice: "12345" [E1].', labels=["E1"], status="cited")
        ]
        assert view.answer == 'invoice: "12345" [E1].'
        assert [c.label for c in view.citations] == ["E1"]
        assert view.repair_attempted is None and view.coverage_note is None

    def test_extract_structured_call_records_the_schema_and_maps_records(self, chunked_db):
        """#1095: with structured outputs on, the schema each call sent is
        recorded and the neutral keys are mapped back to the case's names."""

        class StructuredClient(ScriptedClient):
            structured_output = True

            async def complete(self, system: str, user: str, *, json_schema=None) -> str:
                self.schemas.append(json_schema)
                return await super().complete(system, user)

            schemas: list = []

        reply = json.dumps({"records": [{"f1": "12345", "_evidence": {"f1": ["E1"]}}]})
        inference = StructuredClient(reply, json.dumps({"records": []}))
        case = dataclasses.replace(
            CASES[EXTRACT], arguments={"query": "invoice", "schema": {"invoice": "string"}}
        )
        run = asyncio.run(run_case(case, _ctx(chunked_db, inference)))
        assert run.status == "ok" and run.output is not None
        assert run.calls[0].json_schema is not None
        assert run.calls[0].json_schema == inference.schemas[0]
        assert run.output.records[0]["invoice"] == "12345"

    def test_extract_provider_failure_keeps_content_out_of_logs(self, chunked_db, caplog):
        case = dataclasses.replace(
            CASES[EXTRACT], arguments={"query": "invoice", "schema": {"invoice": "string"}}
        )
        inference = ScriptedClient(RuntimeError(f"provider echoed {MARKER}"))
        with caplog.at_level(logging.DEBUG):
            run = asyncio.run(run_case(case, _ctx(chunked_db, inference)))
        assert (run.status, run.error) == ("tool_error", "tool_error")
        assert run.calls[0].outcome == "error"
        assert MARKER not in caplog.text
        assert MARKER not in (run.error_detail or "")

    def test_select_passages_per_tool(self):
        shown, wanted = {"E1": "shown"}, {"E1": "wanted", "E2": "wanted"}
        # summarize_thread builds the shown map first, then (when the
        # window cut the context) what the caps alone would show.
        assert select_passages("summarize_thread", [shown, wanted]) == shown
        # ask_mailbox's one map is the last one built.
        assert select_passages("ask_mailbox", [shown, wanted]) == wanted
        # extract_from_emails builds one map per thread, numbered across.
        assert select_passages("extract_from_emails", [{"E1": "a"}, {"E2": "b"}]) == {
            "E1": "a",
            "E2": "b",
        }
        assert select_passages("summarize_thread", []) == {}

    def test_output_models_cover_every_tool(self):
        assert set(OUTPUT_MODELS) == set(TOOLS)


# ------------------------------------------------------------------ views


class TestViews:
    def test_extract_view_renders_one_statement_per_record(self):
        output = SimpleNamespace(
            records=[
                _pool_record(),
                {**_pool_record("Crestline Aquatics", "$44,900", ("E2",)), "note": None},
            ],
            citations=[SimpleNamespace(label="E1"), SimpleNamespace(label="E2")],
            citation_problems=[],
            notice="Evidence note: 1 of 2 threads cut.",
            threads=[SimpleNamespace(thread_id="t05.1@baseline.example")],
        )
        view = view_of("extract_from_emails", output)
        assert [s.text for s in view.statements] == [
            'contractor: "Bluewater Pools" [E1]; bid: "$38,400" [E1].',
            'contractor: "Crestline Aquatics" [E2]; bid: "$44,900" [E2]; note: null.',
        ]
        assert [s.labels for s in view.statements] == [["E1"], ["E2"]]
        assert all(s.status == "cited" for s in view.statements)
        assert view.answer == "\n".join(s.text for s in view.statements)
        assert view.coverage_note == output.notice
        assert view.repair_attempted is None

    def test_extract_view_without_records_is_an_abstention(self):
        output = SimpleNamespace(
            records=[], citations=[], citation_problems=[], notice=None, threads=[]
        )
        view = view_of("extract_from_emails", output)
        assert view.answer == NO_RECORDS and view.statements == []
        assert is_abstention(view.answer)

    def test_uncited_record_field_is_an_uncited_statement(self):
        record = {"bid": "$38,400", "_source_thread": "s", "_date": "d", "_evidence": {}}
        output = SimpleNamespace(
            records=[record], citations=[], citation_problems=[], notice=None, threads=[]
        )
        [statement] = view_of("extract_from_emails", output).statements
        assert statement == AnswerStatement(text='bid: "$38,400".', labels=[], status="uncited")

    def test_unknown_tool_is_refused(self):
        with pytest.raises(KeyError):
            view_of("brief_issue", SimpleNamespace())


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
        assert det.checks["records_conform"] == NA
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

    def test_extract_right_and_wrong_records(self):
        case = CASES[EXTRACT]
        passages = [_passage("E1", "t05.1")]
        right = _extract_run([_pool_record()], passages, ["E1"])
        det = grade_run(case, right)
        assert det.passed, det.checks
        assert det.checks["records_conform"] == PASS
        assert det.checks["required_evidence_cited"] == PASS
        assert det.checks["expected_values"] == PASS
        assert det.abstained is False
        wrong = _extract_run([_pool_record(bid="$44,900")], passages, ["E1"])
        det = grade_run(case, wrong)
        assert det.checks["expected_values"] == FAIL
        assert "synthesis" in attribute(case, wrong, det, False, False)

    def test_extract_record_shape_is_checked(self):
        """Shape only: the declared fields' JSON types and the provenance
        fields every record carries; a breach is the tool's, not the model's."""
        case = CASES[EXTRACT]
        passages = [_passage("E1", "t05.1")]
        for record in (
            _pool_record(bid=38400),  # a number where the schema says string
            {k: v for k, v in _pool_record().items() if k != "_evidence"},
            {**_pool_record(), "_evidence": ["E1"]},
            {**_pool_record(), "_date": None},
        ):
            run = _extract_run([record], passages, ["E1"])
            det = grade_run(case, run)
            assert det.checks["records_conform"] == FAIL, record
            assert "answer_infrastructure" in attribute(case, run, det, False, False)

    def test_extract_without_records_fails_an_answerable_case(self):
        case = CASES[EXTRACT]
        run = _extract_run([], [_passage("E1", "t05.1")], [])
        det = grade_run(case, run)
        assert det.abstained is True
        assert det.checks["abstention"] == FAIL
        assert det.checks["expected_values"] == FAIL
        assert det.checks["records_conform"] == PASS  # nothing to check
        assert det.checks["required_evidence_cited"] == FAIL


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
        case = CASES[EXTRACT]
        prompt = build_judge_prompt(case, "r [E1]", {"E1": _passage("E1", "t05.1")}, ["r [E1]"])
        assert prompt.startswith(f"Task (extract_from_emails): {case.question}\n")
        assert "numbered statement is one extracted record" in prompt


# ---------------------------------------------------------------- reports


class TestReports:
    def test_records_carry_the_tool_and_reports_aggregate_by_tool(self, chunked_db):
        summarize = dataclasses.replace(
            CASES[SUMMARIZE], arguments={"thread_id": "t-alpha", "style": "brief"}
        )
        extract = dataclasses.replace(
            CASES[EXTRACT], arguments={"query": "invoice", "schema": {"invoice": "string"}}
        )
        record = json.dumps({"invoice": "12345", "_evidence": {"invoice": ["E1"]}})
        rows_s, _ = asyncio.run(
            evaluate([summarize], _ctx(chunked_db, ScriptedClient("Invoice 12345 [E2].")))
        )
        rows_e, _ = asyncio.run(evaluate([extract], _ctx(chunked_db, ScriptedClient(record))))
        rows = rows_s + rows_e
        assert [r["tool"] for r in rows] == ["summarize_thread", "extract_from_emails"]
        assert rows[0]["repair_attempted"] is False and rows[1]["repair_attempted"] is None
        assert rows[1]["inference_calls"] >= 2
        report = build_report(_identity(), rows, False)
        by_tool = report["aggregates"]["by_tool"]
        assert set(by_tool) == {"summarize_thread", "extract_from_emails"}
        assert by_tool["extract_from_emails"]["cases"] == 1

    def test_record_values_stay_out_of_the_default_report(self, chunked_db):
        extract = dataclasses.replace(
            CASES[EXTRACT], arguments={"query": "invoice", "schema": {"invoice": "string"}}
        )
        reply = json.dumps({"invoice": MARKER, "_evidence": {"invoice": ["E1"]}})
        run = asyncio.run(run_case(extract, _ctx(chunked_db, ScriptedClient(reply))))
        assert run.status == "ok"
        det = grade_run(extract, run)
        judge = JudgeOutcome(status="not_configured")
        row = case_record(extract, run, det, judge, attribute(extract, run, det, False, False))
        assert MARKER not in json.dumps(row)
        detail = detail_record(extract, run, judge)
        assert detail["records"] == run.output.records
        assert MARKER in json.dumps(detail["records"])


# ------------------------------------------------------------------- plan


class TestPlan:
    def test_planned_calls_per_tool(self):
        assert planned_calls(CASES["ask-roof-total"]) == (1, 1)
        assert planned_calls(CASES[SUMMARIZE]) == (1, 1)
        case = CASES[EXTRACT]
        assert planned_calls(case) == (case.arguments["limit"], 0)
        # The tool clamps limit to [1, 50]; the plan counts what it will do.
        assert planned_calls(
            dataclasses.replace(case, arguments={**case.arguments, "limit": 500})
        ) == (50, 0)
        without = {k: v for k, v in case.arguments.items() if k != "limit"}
        assert planned_calls(dataclasses.replace(case, arguments=without)) == (20, 0)

    def test_preflight_counts_extract_threads_without_repairs(self, tmp_path, monkeypatch, capsys):
        def no_provider(*_a, **_k):
            raise AssertionError("a provider was configured")

        monkeypatch.setattr(cli, "load_layer", no_provider)
        monkeypatch.delenv("EVAL_MAX_CALLS", raising=False)
        monkeypatch.setenv("INFERENCE_MODEL", "stub-answerer")
        monkeypatch.setenv("JUDGE_MODE", "openai")
        monkeypatch.setenv("JUDGE_MODEL", "stub-j")
        limit = CASES[EXTRACT].arguments["limit"]
        code = cli.main(
            [
                "run",
                "--preflight",
                "--index-dir",
                str(tmp_path / "not-built"),
                "--out",
                str(tmp_path / "r.json"),
                "--case",
                EXTRACT,
                "--case",
                "ask-roof-total",
            ]
        )
        assert code == cli.EXIT_OK
        assert capsys.readouterr().err == (
            f"Planned provider calls: {limit + 1} answer calls to stub-answerer (up to 1 more "
            f"for citation repairs) and 2 judge calls to stub-j; at most {limit + 4} provider "
            "calls.\n"
        )
