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
import re
from datetime import UTC, datetime

import pytest
from fastmcp.exceptions import ToolError
from src.lib.inference import (
    CHARS_PER_TOKEN,
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_MAX_TOKENS,
    InferenceTruncatedError,
    PromptBudget,
    default_token_budget,
    estimate_tokens,
)
from src.lib.sqlite import ChunkResult, ScopeLabels, ThreadResult
from src.tools.brief import (
    _BRIEF_REPAIR_INSTRUCTION,
    _CHECK_REPAIR_INSTRUCTION,
    _check_repair_reason,
    _repair_reason,
    register_experimental_tools,
)
from src.tools.intelligence import (
    _REPAIR_INSTRUCTION,
    _REPAIR_REASONS,
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

    def __init__(
        self,
        threads: list[ThreadResult],
        recent: list[ChunkResult] | None = None,
        *,
        in_scope: bool = True,
    ):
        self._threads = threads
        self._recent = recent or []
        self._in_scope = in_scope

    def hybrid_search(self, **_kwargs):
        return list(self._threads)

    def get_thread(self, _thread_id):
        return self._threads[0] if self._threads else None

    def get_recent_chunks_for_thread(self, _thread_id, _limit):
        return list(self._recent)

    def message_scope(self, thread_ids, **_filters):
        # Every message in scope (no labels shown), or every message out
        # of it (#755): each header then carries a scope field.
        if not self._in_scope:
            return ScopeLabels(claimants=set(), whole_threads=set())
        return ScopeLabels(
            claimants={c.claimant_id for t in self._threads for c in t.evidence_chunks},
            whole_threads=set(thread_ids),
        )


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
        assert budget.max_output_tokens == DEFAULT_MAX_TOKENS == 1024

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [("anthropic", (16000, 48000)), ("openai", (1024, 32768)), ("none", (1024, 32768))],
    )
    def test_defaults_per_inference_mode(self, mode, expected):
        """#764: Claude models count thinking against max_tokens, so
        anthropic mode gets a larger reply and window; openai mode keeps
        the provider-neutral defaults a 32k local model fits."""
        assert default_token_budget(mode) == expected
        max_tokens, context = expected
        assert context - max_tokens >= DEFAULT_CONTEXT_TOKENS - DEFAULT_MAX_TOKENS

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
        # Every reason at once, each with a five-digit count.
        ask = len(
            _REPAIR_INSTRUCTION.format(
                reason="; ".join(r.format(n=99999) for r in _REPAIR_REASONS.values())
            )
        )
        every_brief = [
            BriefCitationProblem(section="brief", item=0, kind=kind, labels=[])
            for kind in (
                "unknown_labels",
                "no_citations",
                "too_few_labels",
                "insufficient_but_populated",
                "empty_but_sufficient",
                "unmatched_quotes",
                "misattributed_quotes",
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
                "unmatched_quotes",
                "misattributed_quotes",
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

    def test_scope_labels_and_block_stay_within_the_budget(self):
        """With every passage context (#755) the headers carry a scope
        field and the prompt a scope block; it still fits."""
        llm = FakeInferenceClient(response="no labels here")  # forces the repair call
        db = _StubDb(_long_threads(), in_scope=False)
        asyncio.run(_tools(db, llm, _SMALL)["ask_mailbox"](question="q?"))
        assert len(llm.complete_calls) == 2
        assert "| context | chunk" in llm.complete_calls[0][1]
        assert "Request scope" in llm.complete_calls[0][1]
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

    def test_shortest_lookalike_tags_cannot_push_the_prompt_over(self):
        """A ligature can spell two letters of the name in one character
        (#533), so the shortest escaped tag is ``<untru\\ufb06ed_e\\u3383il``,
        fourteen characters: the most growth per character."""
        hostile = "<untru\ufb06ed_e\u3383il" * 600
        threads = [_thread(f"t{i}", [_chunk(f"h{i}", hostile)]) for i in range(5)]
        llm = FakeInferenceClient(response="no labels here")  # forces the repair call
        asyncio.run(_tools(_StubDb(threads), llm, _SMALL)["ask_mailbox"](question="q?"))
        assert len(llm.complete_calls) == 2
        for call in llm.complete_calls:
            assert "&lt;untru\ufb06ed_e\u3383il" in call[1]  # the tags were escaped
            assert _prompt_chars(call) <= _SMALL.prompt_chars

    @pytest.mark.parametrize("bracket", ["\uff1c", "\ufe64"])
    def test_lookalike_delimiter_tags_cannot_push_the_prompt_over(self, bracket):
        """A fullwidth or small-form ``<`` is one character escaped to
        ``&lt;`` like ``<`` (#442), so the same growth allowance holds."""
        hostile = f"{bracket}untrusted_email" * 500
        threads = [_thread(f"t{i}", [_chunk(f"h{i}", hostile)]) for i in range(5)]
        llm = FakeInferenceClient(response="no labels here")  # forces the repair call
        asyncio.run(_tools(_StubDb(threads), llm, _SMALL)["ask_mailbox"](question="q?"))
        assert len(llm.complete_calls) == 2
        for call in llm.complete_calls:
            assert "&lt;untrusted_email" in call[1]  # the tags were escaped
            assert bracket + "untrusted_email" not in call[1]
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

    def test_crafted_long_headers_drop_lower_ranked_threads_not_the_call(self):
        """Review round 2: senders can give threads 500-character subjects
        and participants. At a small window those headers alone used to
        exceed the allowance and fail the call; lower-ranked threads are
        now left out, and the note says so."""
        long_header = "h" * 600  # clipped to HEADER_CHAR_LIMIT
        threads = _long_threads(n=5, chunks=1, size=300)
        for t in threads:
            t.subject = f"{_MARKER} {long_header}"
            t.participants = [f"{_MARKER} {long_header}"] * 3
        llm = FakeInferenceClient(response="no labels here")  # forces the repair call
        asyncio.run(_tools(_StubDb(threads), llm, _SMALL)["ask_mailbox"](question="q?"))
        assert len(llm.complete_calls) == 2
        for call in llm.complete_calls:
            assert _prompt_chars(call) <= _SMALL.prompt_chars
        user = llm.complete_calls[0][1]
        assert '<untrusted_email index="1">' in user  # the top-ranked thread is kept
        assert '<untrusted_email index="5">' not in user
        after_mail = user.rpartition("</untrusted_email>")[2]
        assert "lower-ranked retrieved thread(s) were left out" in after_mail
        assert _MARKER not in after_mail

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("brief_issue", {"topic": "the budget"}),
            ("check_conclusion", {"conclusion": "The budget is 700 units."}),
        ],
    )
    def test_crafted_long_headers_do_not_fail_experimental_tools(self, tool, args):
        long_header = "h" * 600
        threads = _long_threads(n=5, chunks=1, size=300)
        for t in threads:
            t.subject = long_header
            t.participants = [long_header] * 3
        llm = FakeInferenceClient(response="not json")
        asyncio.run(_tools(_StubDb(threads), llm, _SMALL, experimental=True)[tool](**args))
        assert llm.complete_calls
        for call in llm.complete_calls:
            assert _prompt_chars(call) <= _SMALL.prompt_chars

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
        assert _summarize_context(thread, recent, evidence_map={}) in llm.complete_calls[0][1]

    def test_small_window_prompt_never_exceeds_the_budget(self):
        thread, recent = self._thread_and_tail()
        small = PromptBudget(context_tokens=3072, max_output_tokens=1024)
        llm = FakeInferenceClient()
        asyncio.run(
            _tools(_StubDb([thread], recent), llm, small)["summarize_thread"](thread_id="t1")
        )
        # The uncited stub answer gets the repair call (#284); the repair
        # prompt fits too.
        assert len(llm.complete_calls) == 2
        for repair in llm.complete_calls:
            assert _prompt_chars(repair) <= small.prompt_chars
        call = llm.complete_calls[0]
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

    def test_body_takes_room_the_tail_does_not_need(self):
        """Review round 1: with no recent chunks the body had only its
        2:1 share of a small budget and left the rest unused."""
        thread, _recent = self._thread_and_tail()
        context = _summarize_context(thread, [], 6000)
        assert len(context) == 6000

    def test_tail_takes_room_a_short_body_does_not_need(self):
        _thread_long, recent = self._thread_and_tail()
        short = _thread("t2", [], body="short body")
        context = _summarize_context(short, recent, 6000)
        assert context.startswith("short body")
        assert len(context) > 4000  # the tail got more than its 1/3 share
        assert len(context) <= 6000

    def test_short_tail_leaves_the_rest_to_the_body(self):
        thread, _recent = self._thread_and_tail()
        tiny = [_chunk("r9", "latest reply", index=0)]
        context = _summarize_context(thread, tiny, 6000)
        assert len(context) <= 6000
        assert context.endswith("latest reply")
        body = context.partition("\n\n--- recent messages ---\n")[0]
        assert len(body) > 4500


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

    def test_window_cut_evidence_is_reported_with_no_records(self):
        """Review round 1: a null answer from a thread whose passages the
        window cut must not read as a genuine absence."""
        threads = [_thread("t1", [_chunk("c1", f"{_MARKER} " + "x" * 9000)])]
        small = PromptBudget(context_tokens=2200, max_output_tokens=1024)
        llm = FakeInferenceClient(response="null")
        out = asyncio.run(
            _tools(_StubDb(threads), llm, small)["extract_from_emails"](
                query="invoices", schema={"amount": "number"}
            )
        )
        text = "\n".join(c.text for c in out.content)
        assert "1 of 1 threads" in text
        assert "INFERENCE_CONTEXT_TOKENS" in text
        assert _MARKER not in text

    def test_window_cut_evidence_is_reported_beside_records(self):
        threads = [_thread("t1", [_chunk("c1", "x" * 9000)])]
        small = PromptBudget(context_tokens=2200, max_output_tokens=1024)
        llm = FakeInferenceClient(response='{"amount": 5}')
        out = asyncio.run(
            _tools(_StubDb(threads), llm, small)["extract_from_emails"](
                query="invoices", schema={"amount": "number"}
            )
        )
        assert '"amount": 5' in out.content[0].text
        assert "INFERENCE_CONTEXT_TOKENS" in out.content[1].text

    def test_default_window_adds_no_note(self):
        threads = [_thread("t1", [_chunk("c1", "x" * 9000)])]
        llm = FakeInferenceClient(response="null")
        out = asyncio.run(
            _tools(_StubDb(threads), llm)["extract_from_emails"](
                query="invoices", schema={"amount": "number"}
            )
        )
        assert len(out.content) == 1
        assert "INFERENCE_CONTEXT_TOKENS" not in out.content[0].text

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


_LIMIT_LINE = re.compile(
    r"^token limit hit: tool=(?P<tool>\w+) limits=(?P<limits>[\w,]+) (?P<counts>(?:\w+=\d+ ?)+)$"
)


def _limit_lines(caplog) -> list[dict]:
    """Every ``token limit hit`` WARNING, parsed: tool, limits, counts."""
    lines = []
    for record in caplog.records:
        message = record.getMessage()
        if not message.startswith("token limit hit"):
            continue
        assert record.levelno == logging.WARNING
        match = _LIMIT_LINE.match(message)
        assert match, message
        counts = dict(pair.split("=") for pair in match["counts"].split())
        lines.append(
            {
                "tool": match["tool"],
                "limits": match["limits"].split(","),
                "counts": {k: int(v) for k, v in counts.items()},
            }
        )
    return lines


def _one_limit_line(caplog) -> dict:
    lines = _limit_lines(caplog)
    assert len(lines) == 1, lines
    return lines[0]


def _short_threads() -> list[ThreadResult]:
    return [_thread("t1", [_chunk("c1", f"{_MARKER} the budget is 500 units.")])]


class TestTokenLimitWarnings:
    """#865: a call that hits a token limit logs one WARNING naming the
    tool, which limits it hit and the counts behind them, never the
    question, the mail or the reply (each carries ``_MARKER``)."""

    def test_ask_mailbox_evidence_cut_to_the_window(self, caplog):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(response=f"{_MARKER} ok [E1]")
        out = asyncio.run(
            _tools(_StubDb(_long_threads()), llm, _SMALL)["ask_mailbox"](question=f"{_MARKER}?")
        )
        line = _one_limit_line(caplog)
        assert line["tool"] == "ask_mailbox"
        assert line["limits"] == ["evidence_budget"]
        counts = line["counts"]
        note = out.structured_content["coverage_note"]
        # The log carries the counts the caller's note discloses.
        assert f"{counts['passages_omitted']} retrieved passages were left out" in note
        assert f"{counts['passages_truncated']} were cut short" in note
        assert counts["threads_dropped"] == 0
        assert 0 < counts["prompt_tokens"] <= _SMALL.prompt_tokens
        assert counts["prompt_budget_tokens"] == _SMALL.prompt_tokens
        assert counts["max_tokens"] == _SMALL.max_output_tokens
        assert _MARKER not in caplog.text

    def test_ask_mailbox_threads_dropped_to_fit(self, caplog):
        caplog.set_level(logging.INFO)
        threads = _long_threads(n=5, chunks=1, size=300)
        for t in threads:
            t.subject = f"{_MARKER} " + "h" * 600
            t.participants = [f"{_MARKER} " + "h" * 600] * 3
        llm = FakeInferenceClient(response="ok [E1]")
        asyncio.run(_tools(_StubDb(threads), llm, _SMALL)["ask_mailbox"](question="q?"))
        line = _one_limit_line(caplog)
        assert line["limits"] == ["evidence_budget"]
        assert line["counts"]["threads_dropped"] > 0
        assert _MARKER not in caplog.text

    def test_ask_mailbox_answer_cut_at_max_tokens(self, caplog):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(
            complete_responses=[InferenceTruncatedError(partial=f"{_MARKER} partial [E1]")]
        )
        asyncio.run(_tools(_StubDb(_short_threads()), llm)["ask_mailbox"](question=f"{_MARKER}?"))
        line = _one_limit_line(caplog)
        assert line["limits"] == ["output_max_tokens"]
        assert line["counts"]["outputs_cut"] == 1
        assert line["counts"]["max_tokens"] == DEFAULT_MAX_TOKENS
        assert _MARKER not in caplog.text

    def test_ask_mailbox_repair_answer_cut_at_max_tokens(self, caplog):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(
            complete_responses=[
                f"{_MARKER} uncited",  # forces the repair call
                InferenceTruncatedError(partial=f"{_MARKER} partial [E1]"),
            ]
        )
        asyncio.run(_tools(_StubDb(_short_threads()), llm)["ask_mailbox"](question="q?"))
        assert len(llm.complete_calls) == 2
        assert _one_limit_line(caplog)["counts"]["outputs_cut"] == 1
        assert _MARKER not in caplog.text

    def test_ask_mailbox_window_cut_and_answer_cut_share_one_line(self, caplog):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(complete_responses=[InferenceTruncatedError(partial="p [E1]")])
        asyncio.run(_tools(_StubDb(_long_threads()), llm, _SMALL)["ask_mailbox"](question="q?"))
        line = _one_limit_line(caplog)
        assert line["limits"] == ["evidence_budget", "output_max_tokens"]
        assert line["counts"]["outputs_cut"] == 1

    def test_ask_mailbox_prompt_over_budget(self, caplog):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient()
        with pytest.raises(ToolError) as err:
            asyncio.run(
                _tools(_StubDb(_long_threads(n=1)), llm, _SMALL)["ask_mailbox"](
                    question=f"{_MARKER} " + "why " * 3000
                )
            )
        line = _one_limit_line(caplog)
        assert line["tool"] == "ask_mailbox"
        assert line["limits"] == ["prompt_over_budget"]
        counts = line["counts"]
        # The same estimate the caller's error states.
        assert f"estimated at {counts['prompt_tokens']} tokens" in str(err.value)
        assert counts["prompt_tokens"] > counts["prompt_budget_tokens"] == _SMALL.prompt_tokens
        assert _MARKER not in caplog.text

    def test_summarize_thread_summary_cut_at_max_tokens(self, caplog):
        caplog.set_level(logging.INFO)
        thread = _thread("t1", [], body=f"{_MARKER} body")
        llm = FakeInferenceClient(
            complete_responses=[InferenceTruncatedError(partial=f"{_MARKER} partial [E1]")]
        )
        asyncio.run(_tools(_StubDb([thread]), llm)["summarize_thread"](thread_id="t1"))
        line = _one_limit_line(caplog)
        assert line["tool"] == "summarize_thread"
        assert line["limits"] == ["output_max_tokens"]
        assert line["counts"]["outputs_cut"] == 1
        assert _MARKER not in caplog.text

    def test_summarize_thread_prompt_over_budget(self, caplog):
        caplog.set_level(logging.INFO)
        thread = _thread("t1", [], body="body")
        thread.participants = [f"{_MARKER} " + "p" * 600] * 10
        tiny = PromptBudget(context_tokens=2112, max_output_tokens=1024)
        llm = FakeInferenceClient()
        with pytest.raises(ToolError, match="INFERENCE_CONTEXT_TOKENS"):
            asyncio.run(_tools(_StubDb([thread]), llm, tiny)["summarize_thread"](thread_id="t1"))
        line = _one_limit_line(caplog)
        assert line["tool"] == "summarize_thread"
        assert line["limits"] == ["prompt_over_budget"]
        assert line["counts"]["prompt_budget_tokens"] == tiny.prompt_tokens
        assert _MARKER not in caplog.text

    def test_extract_reply_cut_at_max_tokens(self, caplog):
        caplog.set_level(logging.INFO)
        threads = [
            _thread("t1", [_chunk("c1", f"{_MARKER} invoice 5")]),
            _thread("t2", [_chunk("c2", f"{_MARKER} invoice 6")]),
        ]
        llm = FakeInferenceClient(
            complete_responses=[InferenceTruncatedError(partial=f'{{"amount": "{_MARKER}'), "null"]
        )
        asyncio.run(
            _tools(_StubDb(threads), llm)["extract_from_emails"](
                query=_MARKER, schema={"amount": "number"}
            )
        )
        line = _one_limit_line(caplog)
        assert line["tool"] == "extract_from_emails"
        assert line["limits"] == ["output_max_tokens"]
        assert line["counts"]["outputs_cut"] == 1
        assert line["counts"]["threads_cut"] == 0
        assert _MARKER not in caplog.text

    def test_extract_evidence_cut_to_the_window(self, caplog):
        caplog.set_level(logging.INFO)
        threads = [_thread("t1", [_chunk("c1", f"{_MARKER} " + "x" * 9000)])]
        small = PromptBudget(context_tokens=2200, max_output_tokens=1024)
        llm = FakeInferenceClient(response="null")
        asyncio.run(
            _tools(_StubDb(threads), llm, small)["extract_from_emails"](
                query=_MARKER, schema={"amount": "number"}
            )
        )
        line = _one_limit_line(caplog)
        assert line["limits"] == ["evidence_budget"]
        counts = line["counts"]
        assert counts["threads_cut"] == 1
        assert counts["passages_omitted"] + counts["passages_truncated"] == 1
        assert 0 < counts["prompt_tokens"] <= small.prompt_tokens
        assert _MARKER not in caplog.text

    def test_extract_schema_over_budget(self, caplog):
        caplog.set_level(logging.INFO)
        threads = [_thread("t1", [_chunk("c1", "text")])]
        schema = {f"{_MARKER}_{i}": "string" for i in range(2000)}
        with pytest.raises(ToolError):
            asyncio.run(
                _tools(_StubDb(threads), FakeInferenceClient(), _SMALL)["extract_from_emails"](
                    query=_MARKER, schema=schema
                )
            )
        line = _one_limit_line(caplog)
        assert line["tool"] == "extract_from_emails"
        assert line["limits"] == ["prompt_over_budget"]
        assert _MARKER not in caplog.text

    def test_calls_within_every_limit_log_no_warning(self, caplog):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(response="500 units [E1]")
        tools = _tools(_StubDb(_short_threads()), llm)

        async def run_all():
            await tools["ask_mailbox"](question=f"{_MARKER}?")
            await tools["summarize_thread"](thread_id="t1")
            await tools["extract_from_emails"](query=_MARKER, schema={"amount": "number"})

        asyncio.run(run_all())
        assert _limit_lines(caplog) == []
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert _MARKER not in caplog.text

    def test_default_window_per_thread_cap_logs_no_warning(self, caplog):
        """The per-thread character cap is a fixed design limit, not the
        model window: raising INFERENCE_CONTEXT_TOKENS would not change
        it, so trimming to it is not a token-limit hit."""
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(response="ok [E1]")
        asyncio.run(
            _tools(_StubDb(_long_threads(n=10, chunks=6)), llm)["ask_mailbox"](
                question="q?", max_threads=10
            )
        )
        assert _limit_lines(caplog) == []
