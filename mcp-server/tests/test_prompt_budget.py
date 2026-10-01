"""
The whole-prompt token budget (#285).

Every intelligence prompt (system prompt, instructions, provenance
headers, question and evidence, plus room for a repair instruction) is
counted against ``INFERENCE_CONTEXT_TOKENS`` less the
``INFERENCE_MAX_TOKENS`` reply reserve, at a conservative
``CHARS_PER_TOKEN``. At the default window the per-tool character caps
still bind, so default prompts are unchanged; a small window (the
small-model profile) cuts evidence to fit and says so. All data is
synthetic.
"""

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from fastmcp.exceptions import ToolError
from src.lib.inference import (
    CHARS_PER_TOKEN,
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_MAX_TOKENS,
    PromptBudget,
    estimate_tokens,
)
from src.lib.sqlite import ChunkResult, ThreadResult
from src.tools.brief import (
    _BRIEF_REPAIR_INSTRUCTION,
    _CHECK_REPAIR_INSTRUCTION,
    _check_repair_reason,
    _repair_reason,
    register_experimental_tools,
)
from src.tools.intelligence import (
    _REPAIR_INSTRUCTION,
    PER_THREAD_CHAR_BUDGET,
    REPAIR_RESERVE_CHARS,
    _build_evidence,
    _summarize_context,
    register_intelligence_tools,
)
from src.tools.outputs import (
    Brief,
    BriefCitationProblem,
    ConclusionCheck,
    ConclusionCitationProblem,
)

from tests.conftest import FakeEmbedClient, FakeInferenceClient, FakeMCPServer

_MARKER = "SYNTHETIC_BUDGET_MARKER_7731"
# A window small enough that five long threads cannot all fit.
_SMALL = PromptBudget(context_tokens=4096, max_output_tokens=1024)
# Parsed replies, for the repair reasons that apply to a reply that parsed.
_BRIEF = Brief(
    chronology=[],
    positions=[],
    decisions=[],
    open_questions=[],
    conflicts=[],
    insufficient_evidence=False,
)
_CHECK = ConclusionCheck(verdict_summary="v", findings=[], insufficient_evidence=False)


def _chunk(cid: str, text: str, *, index: int = 0, attachment: bool = False) -> ChunkResult:
    return ChunkResult(
        chunk_id=cid,
        message_id=f"{cid}@example.com",
        claimant_id=f"{cid}@example.com#00000000",
        thread_id="t",
        chunk_index=index,
        text=text,
        char_start=0,
        char_end=len(text),
        attachment_id="att-1" if attachment else None,
        attachment_filename="quote.pdf" if attachment else None,
        attachment_mime="application/pdf" if attachment else None,
        message_date="2024-03-01T09:00:00+00:00",
        message_sender="sender@example.com",
    )


def _thread(tid: str, chunks: list[ChunkResult], *, body: str = "") -> ThreadResult:
    return ThreadResult(
        thread_id=tid,
        subject=f"synthetic subject {tid}",
        participants=["a@example.com", "b@example.com"],
        folder="INBOX",
        date_first=datetime(2024, 1, 1, tzinfo=UTC),
        date_last=datetime(2024, 1, 2, tzinfo=UTC),
        message_ids=[f"{tid}@example.com"],
        snippet="",
        has_attachments=False,
        body_text=body,
        evidence_chunks=chunks,
    )


def _long_threads(n: int = 5, chunks: int = 3, size: int = 1500) -> list[ThreadResult]:
    return [
        _thread(
            f"t{i}",
            [
                _chunk(f"c{i}-{k}", f"{_MARKER} thread {i} passage {k} " + "x" * size, index=k)
                for k in range(chunks)
            ],
        )
        for i in range(n)
    ]


class _StubDb:
    """Just the reads the intelligence tools make, returning fixed data."""

    def __init__(self, threads: list[ThreadResult], recent: list[ChunkResult] | None = None):
        self._threads = threads
        self._recent = recent or []

    def hybrid_search(self, **_kwargs):
        return list(self._threads)

    def get_thread(self, _thread_id):
        return self._threads[0] if self._threads else None

    def get_recent_chunks_for_thread(self, _thread_id, _limit):
        return list(self._recent)


def _tools(db, inference, budget: PromptBudget | None = None, *, experimental=False):
    server = FakeMCPServer()
    kwargs = {"prompt_budget": budget} if budget is not None else {}
    register_intelligence_tools(server, db, FakeEmbedClient(), inference, **kwargs)
    if experimental:
        register_experimental_tools(server, db, FakeEmbedClient(), inference, **kwargs)
    return server.tools


def _prompt_chars(call: tuple[str, str]) -> int:
    system, user = call
    return len(system) + len(user)


class TestPromptBudget:
    def test_prompt_allowance_reserves_the_reply_and_template(self):
        budget = PromptBudget(context_tokens=8192, max_output_tokens=1024)
        assert budget.prompt_tokens == 8192 - 1024 - 64
        assert budget.prompt_chars == budget.prompt_tokens * CHARS_PER_TOKEN

    def test_defaults_match_the_inference_defaults(self):
        budget = PromptBudget()
        assert budget.context_tokens == DEFAULT_CONTEXT_TOKENS == 32768
        assert budget.max_output_tokens == DEFAULT_MAX_TOKENS

    @pytest.mark.parametrize(("context", "output"), [(1024, 1024), (2048, 1024), (4096, 4000)])
    def test_a_window_without_room_for_a_prompt_is_rejected(self, context, output):
        with pytest.raises(ValueError, match="INFERENCE_CONTEXT_TOKENS"):
            PromptBudget(context_tokens=context, max_output_tokens=output)

    def test_estimate_rounds_up(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens("a") == 1
        assert estimate_tokens("a" * CHARS_PER_TOKEN) == 1
        assert estimate_tokens("a" * (CHARS_PER_TOKEN + 1)) == 2

    def test_repair_reserve_covers_every_repair_instruction(self):
        """The reserve must hold the longest repair suffix any tool appends."""
        ask = max(
            len(_REPAIR_INSTRUCTION.format(reason=r))
            for r in ("cited evidence labels that no passage header has", "cited no evidence label")
        )
        every_brief = [
            BriefCitationProblem(section="brief", item=0, kind=kind, labels=[])
            for kind in (
                "unknown_labels",
                "no_citations",
                "too_few_labels",
                "insufficient_but_populated",
            )
        ]
        brief = len(_BRIEF_REPAIR_INSTRUCTION.format(reason=_repair_reason(_BRIEF, every_brief)))
        every_check = [
            ConclusionCitationProblem(item=None, kind=kind, labels=[])
            for kind in (
                "invalid_relation",
                "unknown_labels",
                "no_citations",
                "insufficient_but_populated",
                "no_findings_but_sufficient",
            )
        ]
        check = len(
            _CHECK_REPAIR_INSTRUCTION.format(reason=_check_repair_reason(_CHECK, every_check))
        )
        assert max(ask, brief, check) <= REPAIR_RESERVE_CHARS


class TestAskMailboxBudget:
    def test_default_window_keeps_todays_evidence_budget(self):
        """At the default window the 2,000-characters-per-thread cap binds,
        even for the largest request, so the evidence is what it was."""
        threads = _long_threads(n=10, chunks=6)
        llm = FakeInferenceClient(response="ok [E1]")
        asyncio.run(_tools(_StubDb(threads), llm)["ask_mailbox"](question="q?", max_threads=10))
        expected, _ = _build_evidence(threads, PER_THREAD_CHAR_BUDGET * 10, evidence_map={})
        _system, user = llm.complete_calls[0]
        for rendered in expected:
            assert rendered in user

    def test_small_window_prompt_never_exceeds_the_budget(self):
        llm = FakeInferenceClient(response="no labels here")  # forces the repair call
        asyncio.run(_tools(_StubDb(_long_threads()), llm, _SMALL)["ask_mailbox"](question="q?"))
        assert len(llm.complete_calls) == 2
        for call in llm.complete_calls:
            assert _prompt_chars(call) <= _SMALL.prompt_chars
            assert estimate_tokens(call[0] + call[1]) <= _SMALL.prompt_tokens

    def test_small_window_discloses_what_was_left_out(self):
        llm = FakeInferenceClient(response="ok [E1]")
        asyncio.run(_tools(_StubDb(_long_threads()), llm, _SMALL)["ask_mailbox"](question="q?"))
        _system, user = llm.complete_calls[0]
        after_mail = user.rpartition("</untrusted_email>")[2]
        assert "Evidence note" in after_mail
        assert "were left out" in after_mail
        assert _MARKER not in after_mail

    def test_delimiter_tags_in_evidence_cannot_push_the_prompt_over(self):
        """Escaping ``<`` as ``&lt;`` lengthens hostile evidence after it
        was budgeted; the budget allows for that."""
        hostile = "<untrusted_email" * 500  # the shortest tag: the most growth
        threads = [_thread(f"t{i}", [_chunk(f"h{i}", hostile)]) for i in range(5)]
        llm = FakeInferenceClient(response="no labels here")  # forces the repair call
        asyncio.run(_tools(_StubDb(threads), llm, _SMALL)["ask_mailbox"](question="q?"))
        for call in llm.complete_calls:
            assert _prompt_chars(call) <= _SMALL.prompt_chars

    def test_answer_in_a_later_passage_is_kept_when_it_fits(self):
        thread = _thread(
            "t1",
            [
                _chunk("p1", "Proposal: the budget is 500 units.", index=0),
                _chunk("p2", "Discussion of the proposal.", index=1),
                _chunk("p3", f"Correction {_MARKER}: the budget is 700 units.", index=2),
            ],
        )
        llm = FakeInferenceClient(response="700 units [E3]")
        asyncio.run(_tools(_StubDb([thread]), llm, _SMALL)["ask_mailbox"](question="budget?"))
        assert f"Correction {_MARKER}" in llm.complete_calls[0][1]

    def test_evidence_across_messages_and_an_attachment_is_kept(self):
        thread = _thread(
            "t1",
            [
                _chunk("m1", "First message: delivery on Monday.", index=0),
                _chunk("m2", "Second message: delivery moved to Friday.", index=0),
                _chunk("m2", "Quoted price: 42 units.", index=0, attachment=True),
            ],
        )
        llm = FakeInferenceClient(response="Friday [E2], 42 units [E3]")
        out = asyncio.run(
            _tools(_StubDb([thread]), llm, _SMALL)["ask_mailbox"](question="delivery and price?")
        )
        user = llm.complete_calls[0][1]
        assert "delivery on Monday" in user
        assert "moved to Friday" in user
        assert "attachment quote.pdf (application/pdf)" in user
        assert "Quoted price: 42 units." in user
        assert {c["source"] for c in out.structured_content["citations"]} == {
            "body",
            "attachment",
        }

    def test_question_too_long_for_the_window_fails_before_inference(self):
        llm = FakeInferenceClient()
        with pytest.raises(ToolError, match="INFERENCE_CONTEXT_TOKENS"):
            asyncio.run(
                _tools(_StubDb(_long_threads(n=1)), llm, _SMALL)["ask_mailbox"](
                    question="why " * 3000
                )
            )
        assert llm.complete_calls == []

    def test_budget_logs_carry_counts_not_mail(self, caplog):
        caplog.set_level(logging.DEBUG)
        llm = FakeInferenceClient(response="ok [E1]")
        asyncio.run(_tools(_StubDb(_long_threads()), llm, _SMALL)["ask_mailbox"](question="q?"))
        assert "evidence budget" in caplog.text
        assert _MARKER not in caplog.text


class TestSummarizeBudget:
    def _thread_and_tail(self):
        thread = _thread("t1", [], body=f"{_MARKER} body " + "b" * 20_000)
        recent = [_chunk(f"r{k}", f"{_MARKER} recent {k} " + "r" * 2000, index=k) for k in range(4)]
        return thread, recent

    def test_default_window_keeps_todays_context(self):
        thread, recent = self._thread_and_tail()
        llm = FakeInferenceClient()
        asyncio.run(_tools(_StubDb([thread], recent), llm)["summarize_thread"](thread_id="t1"))
        assert _summarize_context(thread, recent) in llm.complete_calls[0][1]

    def test_small_window_prompt_never_exceeds_the_budget(self):
        thread, recent = self._thread_and_tail()
        small = PromptBudget(context_tokens=3072, max_output_tokens=1024)
        llm = FakeInferenceClient()
        asyncio.run(
            _tools(_StubDb([thread], recent), llm, small)["summarize_thread"](thread_id="t1")
        )
        call = llm.complete_calls[0]
        assert _prompt_chars(call) <= small.prompt_chars
        # Both the start of the thread and its newest reply survive the cut.
        assert f"{_MARKER} body" in call[1]
        assert f"{_MARKER} recent 3" in call[1]

    def test_context_shrinks_both_sections_in_proportion(self):
        thread, recent = self._thread_and_tail()
        context = _summarize_context(thread, recent, 6000)
        assert len(context) <= 6000
        body, _, tail = context.partition("\n\n--- recent messages ---\n")
        assert 3500 <= len(body) <= 4000
        assert 1500 <= len(tail) <= 2000


class TestExtractBudget:
    def test_small_window_prompt_never_exceeds_the_budget(self):
        threads = [_thread("t1", [_chunk("c1", f"{_MARKER} " + "x" * 9000)])]
        small = PromptBudget(context_tokens=2200, max_output_tokens=1024)
        llm = FakeInferenceClient(response="null")
        asyncio.run(
            _tools(_StubDb(threads), llm, small)["extract_from_emails"](
                query="invoices", schema={"amount": "number"}
            )
        )
        assert _prompt_chars(llm.complete_calls[0]) <= small.prompt_chars

    def test_schema_too_large_for_the_window_fails_before_inference(self):
        threads = [_thread("t1", [_chunk("c1", "text")])]
        llm = FakeInferenceClient(response="null")
        schema = {f"field_{i}": "string" for i in range(2000)}
        with pytest.raises(ToolError, match="INFERENCE_CONTEXT_TOKENS"):
            asyncio.run(
                _tools(_StubDb(threads), llm, _SMALL)["extract_from_emails"](
                    query="invoices", schema=schema
                )
            )
        assert llm.complete_calls == []


class TestExperimentalBudget:
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("brief_issue", {"topic": "the budget"}),
            ("check_conclusion", {"conclusion": "The budget is 700 units."}),
        ],
    )
    def test_small_window_prompt_never_exceeds_the_budget(self, tool, args):
        llm = FakeInferenceClient(response="not json")  # forces the repair call
        asyncio.run(_tools(_StubDb(_long_threads()), llm, _SMALL, experimental=True)[tool](**args))
        assert len(llm.complete_calls) == 2
        for call in llm.complete_calls:
            assert _prompt_chars(call) <= _SMALL.prompt_chars
