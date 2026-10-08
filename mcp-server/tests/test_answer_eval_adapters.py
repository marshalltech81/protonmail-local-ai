"""Answer-evaluation adapters for ``summarize_thread`` (#656), the
experimental tools (#1240) and ``extract_from_emails`` (#1137): the case
schema, capture, the shared
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
from src.lib.inference import InferenceTruncatedError, PromptBudget
from src.tools import brief as brief_tools
from src.tools import intelligence
from src.tools.outputs import (
    AnswerStatement,
    BriefIssueOutput,
    CheckConclusionOutput,
    ExtractFromEmailsOutput,
    SummarizeThreadOutput,
)

from tests.answer_eval import __main__ as cli
from tests.answer_eval.adapters import (
    EXTRACTION_INCOMPLETE,
    NO_RECORDS,
    OUTPUT_MODELS,
    planned_calls,
    select_passages,
    view_of,
    window_cut_labels,
)
from tests.answer_eval.cases import CASES_PATH, TOOLS, CaseError, load_cases, thread_id_of
from tests.answer_eval.graders import FAIL, NA, PASS, attribute, grade_run, is_abstention
from tests.answer_eval.harness import evaluate
from tests.answer_eval.judge import JudgeOutcome, build_judge_prompt
from tests.answer_eval.report import build_report, case_record, detail_record
from tests.answer_eval.runner import CaseRun, RunContext, capture_evidence_maps, run_case
from tests.conftest import FakeEmbedClient
from tests.test_answer_eval import MARKER, ScriptedClient, _identity, _judge_config, _passage

CASES = {c.id: c for c in load_cases()}
SUMMARIZE = "summarize-pool-bids"
SUMMARIZE_WINDOW = "summarize-hall-open-points"
# The experimental tools' smoke cases (#1240): one answer and one
# abstention each.
BRIEF = "brief-pool-bids"
BRIEF_ABSTAIN = "brief-cabin-wifi"
CHECK = "check-roof-total"
CHECK_ABSTAIN = "check-electrician-quote"
# extract_from_emails smoke cases (#1137): a value in a body, one only in
# an attachment, and an abstention.
EXTRACT = "extract-pool-bids"
EXTRACT_ATTACHMENT = "extract-roof-estimate"
EXTRACT_ABSTAIN = "extract-cabin-wifi"
# The handler's notice when a thread's reply was cut off, malformed or
# nonconforming (fixed text in ``intelligence.extract_from_emails``).
_INCOMPLETE = (
    "Incomplete: 2 of 2 threads could not be extracted (2 cut off at the "
    "INFERENCE_MAX_TOKENS limit), "
    "so any matching data in them is missing."
)


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


def _pool_record(contractor="Bluewater Pools", bid="$38,400", labels=("E1",)) -> dict:
    """One record as ``extract_from_emails`` returns it: the schema's
    fields, the provenance fields and the server-checked ``_evidence``."""
    return {
        "contractor": contractor,
        "bid": bid,
        "_source_thread": "Pool resurfacing bids",
        "_date": "2024-01-10",
        "_evidence": {"contractor": list(labels), "bid": list(labels)},
    }


def _extract_output(records, cited=(), *, notice=None, threads=("t05",)) -> ExtractFromEmailsOutput:
    """An ``extract_from_emails`` output as the tool returns it (#1137)."""
    return ExtractFromEmailsOutput.model_validate(
        {
            "records": list(records),
            "citations": [_citation(label, ref) for label, ref in cited],
            "fields": [],
            "citation_problems": [],
            "notice": notice,
            "resolved_from_addr": None,
            "from_name_matches": None,
            "threads": [_thread(t) for t in threads],
        }
    )


def _extract_run(records, passages, cited=(), *, notice=None, threads=("t05",)) -> CaseRun:
    return _experimental_run(
        "extract_from_emails",
        _extract_output(records, cited, notice=notice, threads=threads),
        passages,
    )


# ------------------------------------------------------------------ cases


class TestCaseSchema:
    def test_shipped_cases_cover_every_tool(self):
        by_tool: dict[str, list] = {}
        for case in CASES.values():
            by_tool.setdefault(case.tool, []).append(case)
        assert set(by_tool) == set(TOOLS)
        assert set(TOOLS) == {
            "ask_mailbox",
            "summarize_thread",
            "extract_from_emails",
            "brief_issue",
            "check_conclusion",
        }
        # #1137: smoke cases only, each with a small limit, since each
        # searched thread is one paid model call.
        extract = by_tool["extract_from_emails"]
        assert {c.id for c in extract} == {EXTRACT, EXTRACT_ATTACHMENT, EXTRACT_ABSTAIN}
        assert CASES[EXTRACT].category == "exact_fact"
        assert CASES[EXTRACT_ATTACHMENT].category == "attachment_only"
        assert CASES[EXTRACT_ABSTAIN].expected_handling == "abstain"
        assert all(c.arguments["limit"] <= 3 for c in extract)
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
        # #1137: an extraction embeds its query; its task names the query
        # and the schema's fields.
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
            # Codex round 3: the handler sees only the named thread, so
            # evidence or a fact source in another thread can never be
            # retrieved and would grade as a product regression.
            (SUMMARIZE, lambda r: r["required_evidence"].append(["t01.1"])),
            (SUMMARIZE, lambda r: r["required_evidence"][0].append("t01")),
            (SUMMARIZE, lambda r: r["expected_facts"][0]["sources"].append("t01.1")),
            (SUMMARIZE, lambda r: r.update(tool="extract_from_emails")),  # summarize- prefix
            (SUMMARIZE, lambda r: r.update(tool="brief_issue")),  # summarize- prefix
            # #1137: an extraction's query, schema, limit and filters.
            (EXTRACT, lambda r: r.update(id="ask-pool-bids-extract")),
            (EXTRACT, lambda r: r["arguments"].pop("schema")),
            (EXTRACT, lambda r: r["arguments"].pop("query")),
            (EXTRACT, lambda r: r["arguments"].update(query="  ")),
            (EXTRACT, lambda r: r["arguments"].update(query=3)),
            (EXTRACT, lambda r: r["arguments"].update(schema="vendor")),
            (EXTRACT, lambda r: r["arguments"].update(schema={})),
            # Codex round 3 on #1129: the handler refuses a schema declaring
            # a provenance field before any work, which would grade as a
            # tool error rather than a bad case.
            (EXTRACT, lambda r: r["arguments"]["schema"].update(_source_thread="string")),
            (EXTRACT, lambda r: r["arguments"]["schema"].update(_date="string")),
            (EXTRACT, lambda r: r["arguments"]["schema"].update(_evidence="string")),
            (
                EXTRACT,
                lambda r: r["arguments"].update(
                    schema={"type": "object", "properties": {"_date": {"type": "string"}}}
                ),
            ),
            (
                EXTRACT,
                lambda r: r["arguments"].update(
                    schema={"type": "object", "properties": {}, "required": ["_evidence"]}
                ),
            ),
            # The handler clamps limit to [1, 50]: any other value is a task
            # (and a call count) the case does not state.
            (EXTRACT, lambda r: r["arguments"].update(limit=0)),
            (EXTRACT, lambda r: r["arguments"].update(limit=51)),
            (EXTRACT, lambda r: r["arguments"].update(limit="5")),
            (EXTRACT, lambda r: r["arguments"].update(limit=True)),
            (EXTRACT, lambda r: r["arguments"].update(limit=2.0)),
            (EXTRACT, lambda r: r["arguments"].update(question="why?")),
            (EXTRACT, lambda r: r["arguments"].update(max_threads=2)),
            (EXTRACT, lambda r: r["arguments"].update(from_addr="a@baseline.example")),
            (EXTRACT, lambda r: r["arguments"].update(folders="INBOX")),
            (EXTRACT, lambda r: r["arguments"].update(folders=["INBOX", 3])),
            (EXTRACT, lambda r: r["arguments"].update(date_from=20240101)),
            (EXTRACT, lambda r: r["arguments"].update(date_to=None)),
            (EXTRACT, lambda r: r["arguments"].update(from_name=["Pat"])),
            (EXTRACT, lambda r: r["arguments"].update(participant=3)),
            (EXTRACT, lambda r: r.update(tool="brief_issue")),
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

    def test_extract_runs_the_real_handler_and_captures_every_threads_passages(self, chunked_db):
        """#1137: one call per searched thread, each with its own labelled
        passages; the capture merges every thread's map (labels are
        numbered across the call) and each label is in some request."""
        case = dataclasses.replace(
            CASES[EXTRACT], arguments={"query": "invoice", "schema": {"invoice": "string"}}
        )
        first = json.dumps({"invoice": "12345", "_evidence": {"invoice": ["E1"]}})
        inference = ScriptedClient(first, "null")
        run = asyncio.run(run_case(case, _ctx(chunked_db, inference)))
        assert run.status == "ok" and isinstance(run.output, ExtractFromEmailsOutput)
        assert run.tool == "extract_from_emails"
        threads = [t.thread_id for t in run.output.threads]
        assert len(run.calls) == len(threads) >= 2
        assert sorted(run.passages) == sorted(f"E{n}" for n in range(1, len(run.passages) + 1))
        assert {p.thread_id for p in run.passages.values()} == set(threads)
        for p in run.passages.values():
            assert p.header and any(p.header in call.user for call in run.calls)
        # Labels of a later thread are not in the first request.
        assert any(p.header not in run.calls[0].user for p in run.passages.values())
        assert run.prompt_consistent
        view = run.view
        assert view.records == run.output.records
        assert view.statements == [
            AnswerStatement(text='invoice: "12345" [E1].', labels=["E1"], status="cited")
        ]
        assert view.answer == 'invoice: "12345" [E1].'
        assert [c.label for c in view.citations] == ["E1"]
        assert view.repair_attempted is None and view.coverage_note is None
        assert view.abstained is False and view.complete is True

    def test_extract_label_missing_from_every_request_is_inconsistent(self, chunked_db):
        """The merged capture is checked against every request, so a label
        no request carries still fails the consistency check."""
        case = dataclasses.replace(
            CASES[EXTRACT], arguments={"query": "invoice", "schema": {"invoice": "string"}}
        )
        original = intelligence._build_evidence

        def extra_label(*args, **kwargs):
            result = original(*args, **kwargs)
            evidence_map = kwargs.get("evidence_map")
            if evidence_map:
                ref = next(iter(evidence_map.values()))
                evidence_map["E99"] = dataclasses.replace(ref, label="E99")
            return result

        intelligence._build_evidence = extra_label
        try:
            run = asyncio.run(run_case(case, _ctx(chunked_db, ScriptedClient("null"))))
        finally:
            intelligence._build_evidence = original
        assert run.status == "ok" and "E99" in run.passages
        assert run.prompt_consistent is False

    def test_extract_all_threads_failed_is_incomplete_not_an_abstention(self, chunked_db):
        """Codex round 2 on #1129: a reply cut off for every searched thread
        gives no records with the handler's ``Incomplete:`` notice, which
        must not grade as a genuine empty result."""
        case = dataclasses.replace(
            CASES[EXTRACT], arguments={"query": "invoice", "schema": {"invoice": "string"}}
        )
        inference = ScriptedClient(InferenceTruncatedError("{"))
        run = asyncio.run(run_case(case, _ctx(chunked_db, inference)))
        assert run.status == "ok" and run.output is not None
        assert run.output.records == []
        assert (run.output.notice or "").startswith("Incomplete:")
        view = run.view
        assert view.answer == EXTRACTION_INCOMPLETE
        assert view.abstained is False and view.complete is False
        det = grade_run(case, run)
        assert det.abstained is False
        assert det.checks["answer_complete"] == FAIL
        assert "synthesis" in attribute(case, run, det, False, False)

    def test_extract_all_threads_failed_can_be_judged(self, chunked_db):
        """Codex round 2 on #1273: a non-abstaining answer needs a statement
        for the judge's claims to name, so the incomplete marker is one
        (citing nothing); a valid verdict is then possible and the case is
        not recorded as an evaluator error."""
        case = dataclasses.replace(
            CASES[EXTRACT], arguments={"query": "invoice", "schema": {"invoice": "string"}}
        )
        verdict = json.dumps(
            {
                "claims": [
                    {
                        "claim": "c",
                        "statement": 1,
                        "cited": [],
                        "verdict": "insufficient_evidence",
                        "explanation": "nothing extracted",
                    }
                ],
                "facts": [{"id": f.id, "covered": False} for f in case.expected_facts],
                "prohibited": [
                    {"index": i, "asserted": False} for i in range(1, len(case.must_not_assert) + 1)
                ],
                "dimensions": {
                    d: {"result": "fail" if case.criteria[d] else "not_applicable"}
                    for d in case.criteria
                },
            }
        )
        judge = ScriptedClient(verdict)
        rows, _ = asyncio.run(
            evaluate(
                [case],
                _ctx(chunked_db, ScriptedClient(InferenceTruncatedError("{"))),
                judge_client=judge,
                judge_config=_judge_config(),
            )
        )
        [row] = rows
        view_statements = judge.calls[0][1]
        assert EXTRACTION_INCOMPLETE in view_statements
        assert row["judge"]["status"] == "ok", row["judge"]
        assert row["deterministic"]["checks"]["answer_complete"] == FAIL
        assert "evaluator_infrastructure" not in row["attribution"]
        assert "synthesis" in row["attribution"]

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
        # #1137: extract_from_emails builds one map per searched thread,
        # labels numbered across the call, so they merge.
        assert select_passages("extract_from_emails", [{"E1": "a"}, {"E2": "b"}]) == {
            "E1": "a",
            "E2": "b",
        }
        assert select_passages("extract_from_emails", []) == {}

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
        assert window_cut_labels("extract_from_emails", [shown, wanted]) == set()


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
        with pytest.raises(KeyError):
            view_of("search_emails", SimpleNamespace())

    def test_abstention_is_the_tools_own_not_found_text_only(self):
        """Codex round 4 on #1129: abstention is recognized from the tools'
        own not-found and no-results texts, for every tool alike; an
        extraction's no-records marker is not among them (its view sets
        the abstention flag instead), so a summary or answer that merely
        opens with the same words is an answer."""
        assert is_abstention("Not found in the provided emails: nothing answers this.")
        assert is_abstention("No relevant emails found for this question.")
        assert not is_abstention("No records extracted from the thread were disputed [E1].")
        assert not is_abstention(NO_RECORDS) and not is_abstention(EXTRACTION_INCOMPLETE)

    def test_prose_opening_with_the_no_records_marker_is_an_answer(self):
        """Codex round 4 on #1129: a summary that starts with the
        extraction marker's words is graded as an answer, not an
        abstention (``ask_mailbox`` shares ``is_abstention``, pinned above)."""
        answer = f"{NO_RECORDS} were disputed: Bluewater Pools bid $38,400 [E1]."
        run = _summarize_run(answer, [_passage("E1", "t05.1")], ["E1"])
        det = grade_run(CASES[SUMMARIZE], run)
        assert det.abstained is False
        assert det.checks["abstention"] == PASS

    def test_extract_view_renders_one_statement_per_record(self):
        """Each record is one statement, ``field: value [E1]; ...`` with the
        labels its server-checked ``_evidence`` cites; provenance fields
        are left out, and the tool's notice is the coverage note."""
        output = _extract_output(
            [
                _pool_record(),
                {**_pool_record("Crestline Aquatics", "$44,900", ("E2",)), "note": None},
            ],
            [("E1", "t05.1"), ("E2", "t05.1")],
            notice="Evidence note: in 1 of 2 threads, matched passages were left out.",
        )
        view = view_of("extract_from_emails", output)
        assert [s.text for s in view.statements] == [
            'contractor: "Bluewater Pools" [E1]; bid: "$38,400" [E1].',
            'contractor: "Crestline Aquatics" [E2]; bid: "$44,900" [E2].',
        ]
        assert [s.labels for s in view.statements] == [["E1"], ["E2"]]
        assert all(s.status == "cited" for s in view.statements)
        assert view.answer == "\n".join(s.text for s in view.statements)
        assert view.coverage_note == output.notice
        assert view.records == output.records
        assert view.repair_attempted is None
        assert view.abstained is False and view.complete is True

    def test_uncited_record_is_an_uncited_statement(self):
        record = {"bid": "$38,400", "_source_thread": "s", "_date": "d", "_evidence": {}}
        [statement] = view_of("extract_from_emails", _extract_output([record])).statements
        assert statement == AnswerStatement(text='bid: "$38,400".', labels=[], status="uncited")

    @pytest.mark.parametrize("absent", [None, "", [], {}])
    def test_absent_values_are_not_statements(self, absent):
        """Codex round 3 on #1273: the values the tool's own citation check
        treats as no value (``None``, ``""``, ``[]``, ``{}``) need no
        evidence there, so they are not rendered as uncited claims; a
        record holding only such values states nothing, and records that
        all state nothing are no data (an abstention)."""
        record = {**_pool_record(), "note": absent}
        [statement] = view_of("extract_from_emails", _extract_output([record])).statements
        assert statement.text == 'contractor: "Bluewater Pools" [E1]; bid: "$38,400" [E1].'
        empty = {
            "contractor": None,
            "bid": absent,
            "_source_thread": "s",
            "_date": "d",
            "_evidence": {},
        }
        view = view_of("extract_from_emails", _extract_output([empty]))
        assert view.statements == [] and view.answer == NO_RECORDS
        assert view.abstained is True and view.records == [empty]
        # False and 0 are values, not absences.
        falsy = {**empty, "contractor": False, "bid": 0}
        [statement] = view_of("extract_from_emails", _extract_output([falsy])).statements
        assert statement.text == "contractor: false; bid: 0."

    def test_extract_without_records_is_an_abstention(self):
        """No records and no incomplete notice: the tool found nothing to
        extract, which is its abstention (by flag, not by text)."""
        for notice in (
            None,
            # A window-only note is no incomplete extraction.
            "Evidence note: in 1 of 2 threads, matched passages were left out.",
        ):
            view = view_of("extract_from_emails", _extract_output([], notice=notice))
            assert view.answer == NO_RECORDS and view.statements == []
            assert view.abstained is True and view.complete is True
            assert view.coverage_note == notice

    @pytest.mark.parametrize("records", [[], [_pool_record()]])
    @pytest.mark.parametrize(
        "notice",
        [
            _INCOMPLETE,
            "Incomplete: 1 of 2 threads could not be extracted (1 returned records that did "
            "not match the schema's declared fields and types), so any matching data in them "
            "is missing. Evidence note: in 1 of 2 threads, matched passages were left out.",
        ],
    )
    def test_incomplete_extraction_is_never_an_abstention(self, records, notice):
        """Codex round 2 on #1129: the handler's ``Incomplete:`` notice means
        some thread's reply was cut off, malformed or nonconforming, so
        data may be missing with or without records; it is never a
        genuine empty result."""
        view = view_of("extract_from_emails", _extract_output(records, notice=notice))
        assert view.complete is False and view.abstained is False
        assert view.coverage_note == notice
        if not records:
            assert view.answer == EXTRACTION_INCOMPLETE
            # Codex round 2 on #1273: one statement for the judge to name.
            assert view.statements == [
                AnswerStatement(text=EXTRACTION_INCOMPLETE, labels=[], status="not_checked")
            ]


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

    def test_extract_right_and_wrong_records(self):
        case = CASES[EXTRACT]
        passages = [_passage("E1", "t05.1")]
        right = _extract_run([_pool_record()], passages, [("E1", "t05.1")])
        det = grade_run(case, right)
        assert det.passed, det.checks
        assert det.checks["records_conform"] == PASS
        assert det.checks["required_evidence_cited"] == PASS
        assert det.checks["expected_values"] == PASS
        assert det.abstained is False
        assert attribute(case, right, det, False, False) == []
        wrong = _extract_run([_pool_record(bid="$44,900")], passages, [("E1", "t05.1")])
        det = grade_run(case, wrong)
        assert det.checks["expected_values"] == FAIL
        assert "synthesis" in attribute(case, wrong, det, False, False)

    def test_other_tools_have_no_records_check(self):
        passages = [_passage("E1", "t05.1")]
        run = _summarize_run("Bluewater Pools bid $38,400 [E1].", passages, ["E1"])
        assert grade_run(CASES[SUMMARIZE], run).checks["records_conform"] == NA

    @pytest.mark.parametrize(
        "record",
        [
            _pool_record(bid=38400),  # a number where the schema says string
            {k: v for k, v in _pool_record().items() if k != "_evidence"},
            {k: v for k, v in _pool_record().items() if k != "_source_thread"},
            {**_pool_record(), "_evidence": ["E1"]},
            {**_pool_record(), "_date": None},
            {**_pool_record(), "_source_thread": 3},
            # Codex round 3 on #1273: the _evidence mapping's contents, as
            # the tool writes them: a list of supplied labels per field
            # that has a value.
            {**_pool_record(), "_evidence": {"bid": "E1"}},
            {**_pool_record(), "_evidence": {"bid": [1]}},
            {**_pool_record(), "_evidence": {"bid": ["E99"]}},
            {**_pool_record(), "_evidence": {"other": ["E1"]}},
            {**_pool_record(), "_evidence": {"_date": ["E1"]}},
            {**_pool_record(), "note": None, "_evidence": {"note": ["E1"]}},
        ],
    )
    def test_extract_record_shape_is_checked(self, record):
        """Shape only: the provenance fields every record carries, the
        schema's declared fields and types (the tool's own
        ``_record_conforms``) and the ``_evidence`` mapping (supplied
        labels for fields with a value); a breach is the tool's, not the
        model's."""
        case = CASES[EXTRACT]
        run = _extract_run([record], [_passage("E1", "t05.1")], [("E1", "t05.1")])
        det = grade_run(case, run)
        assert det.checks["records_conform"] == FAIL
        assert "answer_infrastructure" in attribute(case, run, det, False, False)

    def test_extract_evidence_mapping_as_the_tool_writes_it_conforms(self):
        """An empty mapping (no field cited) and a field cited by several
        supplied labels both conform; values are not judged here."""
        case = CASES[EXTRACT]
        passages = [_passage("E1", "t05.1"), _passage("E2", "t05.1")]
        for evidence in ({}, {"bid": ["E1", "E2"]}, {"contractor": [], "bid": ["E2"]}):
            record = {**_pool_record(), "_evidence": evidence}
            run = _extract_run([record], passages, [("E1", "t05.1")])
            assert grade_run(case, run).checks["records_conform"] == PASS, evidence

    def test_extract_without_records_fails_an_answerable_case(self):
        case = CASES[EXTRACT]
        run = _extract_run([], [_passage("E1", "t05.1")])
        det = grade_run(case, run)
        assert det.abstained is True
        assert det.checks["abstention"] == FAIL
        assert det.checks["expected_values"] == FAIL
        assert det.checks["required_evidence_cited"] == FAIL
        assert det.checks["records_conform"] == PASS  # nothing to check
        assert det.checks["answer_complete"] == PASS

    def test_failed_extraction_is_not_a_correct_abstention(self):
        """Codex round 2 on #1129: the unanswerable smoke case passes only
        on a genuine empty result; every thread failing is a failure, and
        a partial failure is reported alongside the records it kept."""
        case = CASES[EXTRACT_ABSTAIN]
        genuine = _extract_run([], [_passage("E1", "t05.1")])
        det = grade_run(case, genuine)
        assert det.checks["abstention"] == PASS
        assert det.passed, det.checks
        failed = _extract_run([], [_passage("E1", "t05.1")], notice=_INCOMPLETE)
        det = grade_run(case, failed)
        assert det.abstained is False
        assert det.checks["abstention"] == FAIL
        assert det.checks["answer_complete"] == FAIL
        assert "synthesis" in attribute(case, failed, det, False, False)
        partial = _extract_run(
            [_pool_record()], [_passage("E1", "t05.1")], [("E1", "t05.1")], notice=_INCOMPLETE
        )
        det = grade_run(CASES[EXTRACT], partial)
        assert det.checks["expected_values"] == PASS
        assert det.checks["answer_complete"] == FAIL
        assert not det.passed


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
            assert "extracted record" not in prompt
        # #1137: an extraction also says what its numbered statements are.
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

    def test_extract_records_carry_their_tool_and_every_call(self, chunked_db):
        """#1137: an extraction's row goes through the shared report path,
        with one inference call per searched thread and no repair."""
        case = dataclasses.replace(
            CASES[EXTRACT], arguments={"query": "invoice", "schema": {"invoice": "string"}}
        )
        reply = json.dumps({"invoice": "12345", "_evidence": {"invoice": ["E1"]}})
        rows, details = asyncio.run(evaluate([case], _ctx(chunked_db, ScriptedClient(reply))))
        assert [r["tool"] for r in rows] == ["extract_from_emails"]
        assert rows[0]["status"] == "ok" and rows[0]["repair_attempted"] is None
        assert rows[0]["inference_calls"] >= 2
        assert details[0]["answer"].startswith('invoice: "12345" [E1].')
        assert details[0]["records"][0]["_evidence"] == {"invoice": ["E1"]}
        report = build_report(_identity(), rows, False)
        assert report["aggregates"]["by_tool"]["extract_from_emails"]["cases"] == 1

    def test_record_values_stay_out_of_the_default_report(self, chunked_db):
        case = dataclasses.replace(
            CASES[EXTRACT], arguments={"query": "invoice", "schema": {"invoice": "string"}}
        )
        reply = json.dumps({"invoice": MARKER, "_evidence": {"invoice": ["E1"]}})
        run = asyncio.run(run_case(case, _ctx(chunked_db, ScriptedClient(reply))))
        assert run.status == "ok" and run.output is not None
        det = grade_run(case, run)
        judge = JudgeOutcome(status="not_configured")
        row = case_record(case, run, det, judge, attribute(case, run, det, False, False))
        assert MARKER not in json.dumps(row)
        detail = detail_record(case, run, judge)
        assert detail["records"] == run.output.records
        assert MARKER in json.dumps(detail["records"])


# ------------------------------------------------------------------- plan


class TestPlan:
    def test_planned_calls_per_tool(self):
        """(answer calls, possible repair calls) per case: one and one for
        the prose and experimental tools; an extraction makes one call per
        searched thread, at most its limit as the handler clamps it, and
        never repairs (#1137)."""
        for cid in ("ask-roof-total", SUMMARIZE, BRIEF, CHECK):
            assert planned_calls(CASES[cid]) == (1, 1)
        case = CASES[EXTRACT]
        assert planned_calls(case) == (case.arguments["limit"], 0)
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
        argv = ["run", "--preflight", "--index-dir", str(tmp_path / "not-built")]
        argv += ["--out", str(tmp_path / "r.json"), "--case", EXTRACT, "--case", "ask-roof-total"]
        assert cli.main(argv) == cli.EXIT_OK
        assert capsys.readouterr().err == (
            f"Planned provider calls: {limit + 1} answer calls to stub-answerer (up to 1 more "
            f"for citation repairs) and 2 judge calls to stub-j; at most {limit + 4} provider "
            "calls.\n"
        )
        # The cap counts every searched thread.
        monkeypatch.setenv("EVAL_MAX_CALLS", str(limit + 3))
        assert cli.main(argv) == cli.EXIT_CONFIG
        assert f"this run can make {limit + 4} provider calls" in capsys.readouterr().err
