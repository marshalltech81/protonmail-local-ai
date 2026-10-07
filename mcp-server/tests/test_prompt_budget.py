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
    _TRUNCATED_NOTICES,
    PER_THREAD_CHAR_BUDGET,
    REPAIR_RESERVE_CHARS,
    _build_evidence,
    _schema_reserve_chars,
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
from tests.test_timings import _one_line

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


_LIMIT_PREFIX = "token limit hit: "


def _limit_lines(caplog) -> list[dict]:
    """Every ``token limit hit`` WARNING, parsed: tool, limits, counts.

    Split on spaces and ``=`` rather than matched with a regex, so the
    parse is linear whatever the line holds."""
    lines = []
    for record in caplog.records:
        message = record.getMessage()
        if not message.startswith("token limit hit"):
            continue
        assert record.levelno == logging.WARNING
        assert message.startswith(_LIMIT_PREFIX), message
        pairs = [field.split("=") for field in message.removeprefix(_LIMIT_PREFIX).split(" ")]
        assert all(len(pair) == 2 for pair in pairs), message
        fields = dict(pairs)
        tool = fields.pop("tool")
        limits = fields.pop("limits")
        assert tool.isidentifier() and all(v.isdigit() for v in fields.values()), message
        lines.append(
            {
                "tool": tool,
                "limits": limits.split(","),
                "counts": {k: int(v) for k, v in fields.items()},
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

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            # Each tool caps its argument's length, so the window is the
            # smallest allowed and the argument just under its cap.
            ("brief_issue", {"topic": f"{_MARKER} " + "why " * 470}),
            ("check_conclusion", {"conclusion": f"{_MARKER} " + "why " * 470}),
        ],
    )
    def test_experimental_tools_log_prompt_over_budget(self, caplog, tool, args):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient()
        tiny = PromptBudget(context_tokens=2112, max_output_tokens=1024)
        tools = _tools(_StubDb(_long_threads(n=1)), llm, tiny, experimental=True)
        with pytest.raises(ToolError, match="INFERENCE_CONTEXT_TOKENS") as err:
            asyncio.run(tools[tool](**args))
        assert llm.complete_calls == []
        line = _one_limit_line(caplog)
        assert line["tool"] == tool
        assert line["limits"] == ["prompt_over_budget"]
        counts = line["counts"]
        assert f"estimated at {counts['prompt_tokens']} tokens" in str(err.value)
        assert counts["prompt_budget_tokens"] == tiny.prompt_tokens
        assert _MARKER not in caplog.text


def _capped(caplog) -> int | None:
    """``evidence_capped_threads`` on the call's one timing line, or None."""
    return _one_line(caplog)["counts"].get("evidence_capped_threads")


class TestPerThreadCapCount:
    """The fixed per-thread evidence cap is not a token limit (no WARNING),
    but trimming to it is a cap, so it is counted on the call's own
    timing line as ``evidence_capped_threads``: the threads whose
    passages it left out or cut."""

    def test_ask_mailbox_counts_capped_threads(self, caplog):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(response=f"{_MARKER} ok [E1]")
        asyncio.run(
            _tools(_StubDb(_long_threads(n=10, chunks=6)), llm)["ask_mailbox"](
                question=f"{_MARKER}?", max_threads=10
            )
        )
        assert _capped(caplog) == 10
        assert _limit_lines(caplog) == []
        assert _MARKER not in caplog.text

    def test_ask_mailbox_counts_only_threads_that_were_trimmed(self, caplog):
        caplog.set_level(logging.INFO)
        threads = [
            _thread("t-short", [_chunk("s1", f"{_MARKER} short")]),
            *_long_threads(n=2, chunks=6),
        ]
        llm = FakeInferenceClient(response="ok [E1]")
        asyncio.run(_tools(_StubDb(threads), llm)["ask_mailbox"](question="q?"))
        assert _capped(caplog) == 2
        assert _MARKER not in caplog.text

    def test_window_cut_is_not_counted_as_the_cap(self, caplog):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(response="ok [E1]")
        asyncio.run(_tools(_StubDb(_long_threads()), llm, _SMALL)["ask_mailbox"](question="q?"))
        assert _capped(caplog) is None
        assert _one_limit_line(caplog)["limits"] == ["evidence_budget"]

    def test_evidence_within_the_cap_is_not_counted(self, caplog):
        caplog.set_level(logging.INFO)
        tools = _tools(_StubDb(_short_threads()), FakeInferenceClient(response="ok [E1]"))
        asyncio.run(tools["ask_mailbox"](question="q?"))
        assert _capped(caplog) is None

    def test_extract_counts_capped_threads(self, caplog):
        caplog.set_level(logging.INFO)
        threads = [
            _thread("t1", [_chunk("c1", f"{_MARKER} " + "x" * 9000)]),
            _thread("t2", [_chunk("c2", f"{_MARKER} short")]),
        ]
        asyncio.run(
            _tools(_StubDb(threads), FakeInferenceClient(response="null"))["extract_from_emails"](
                query=_MARKER, schema={"amount": "number"}
            )
        )
        assert _capped(caplog) == 1
        assert _limit_lines(caplog) == []
        assert _MARKER not in caplog.text

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("brief_issue", {"topic": f"{_MARKER} budget"}),
            ("check_conclusion", {"conclusion": f"{_MARKER} budget"}),
        ],
    )
    def test_experimental_tools_count_capped_threads(self, caplog, tool, args):
        caplog.set_level(logging.INFO)
        tools = _tools(
            _StubDb(_long_threads(n=3, chunks=6)),
            FakeInferenceClient(response="not json"),
            experimental=True,
        )
        asyncio.run(tools[tool](**args))
        assert _capped(caplog) == 3
        assert _MARKER not in caplog.text


def _long_summary_thread() -> tuple[ThreadResult, list[ChunkResult]]:
    thread = _thread("t1", [], body=f"{_MARKER} body " + "b" * 20_000)
    recent = [_chunk(f"r{k}", f"{_MARKER} recent {k} " + "r" * 2000, index=k) for k in range(4)]
    return thread, recent


class TestReviewRound1:
    """Codex review round 1 on #883, and the coordinator's P3."""

    # Item 2: summarize_thread's context cut by the window.
    def test_summarize_context_cut_by_the_window_is_an_evidence_budget_hit(self, caplog):
        caplog.set_level(logging.INFO)
        thread, recent = _long_summary_thread()
        small = PromptBudget(context_tokens=3072, max_output_tokens=1024)
        llm = FakeInferenceClient(response="summary [E1]")
        asyncio.run(
            _tools(_StubDb([thread], recent), llm, small)["summarize_thread"](thread_id="t1")
        )
        line = _one_limit_line(caplog)
        assert line["tool"] == "summarize_thread"
        assert line["limits"] == ["evidence_budget"]
        counts = line["counts"]
        # What the default cap shows, with the labelled headers the tool uses.
        full = len(_summarize_context(thread, recent, evidence_map={}))
        assert counts["context_chars_wanted"] == full
        assert 0 < counts["context_chars_kept"] < full
        assert _one_line(caplog)["counts"]["token_limit_evidence_budget"] == 1
        assert _MARKER not in caplog.text

    def test_summarize_default_window_logs_no_context_cut(self, caplog):
        caplog.set_level(logging.INFO)
        thread, recent = _long_summary_thread()
        llm = FakeInferenceClient(response="summary [E1]")
        asyncio.run(_tools(_StubDb([thread], recent), llm)["summarize_thread"](thread_id="t1"))
        assert _limit_lines(caplog) == []

    # Item 3: an answer cut at max_tokens with nothing to show.
    def test_ask_mailbox_empty_partial_logs_output_max_tokens(self, caplog):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(complete_responses=[InferenceTruncatedError(partial="  ")])
        with pytest.raises(ToolError):
            asyncio.run(
                _tools(_StubDb(_short_threads()), llm)["ask_mailbox"](question=f"{_MARKER}?")
            )
        line = _one_limit_line(caplog)
        assert line["tool"] == "ask_mailbox"
        assert line["limits"] == ["output_max_tokens"]
        assert line["counts"]["outputs_cut"] == 1
        timing = _one_line(caplog)
        assert timing["outcome"] == "error"
        assert timing["counts"]["token_limit_output_max_tokens"] == 1
        assert _MARKER not in caplog.text

    def test_ask_mailbox_empty_partial_keeps_the_window_cut(self, caplog):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(complete_responses=[InferenceTruncatedError(partial="")])
        with pytest.raises(ToolError):
            asyncio.run(_tools(_StubDb(_long_threads()), llm, _SMALL)["ask_mailbox"](question="q?"))
        assert _one_limit_line(caplog)["limits"] == ["evidence_budget", "output_max_tokens"]

    def test_summarize_thread_empty_partial_logs_output_max_tokens(self, caplog):
        caplog.set_level(logging.INFO)
        thread = _thread("t1", [], body=f"{_MARKER} body")
        llm = FakeInferenceClient(complete_responses=[InferenceTruncatedError(partial="")])
        with pytest.raises(ToolError):
            asyncio.run(_tools(_StubDb([thread]), llm)["summarize_thread"](thread_id="t1"))
        line = _one_limit_line(caplog)
        assert line["tool"] == "summarize_thread"
        assert line["limits"] == ["output_max_tokens"]
        assert line["counts"]["outputs_cut"] == 1
        assert _MARKER not in caplog.text

    # Item 4: every limit hit is marked on the call's own timing line.
    def test_token_limits_are_marked_on_the_timing_line(self, caplog):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(complete_responses=[InferenceTruncatedError(partial="p [E1]")])
        asyncio.run(_tools(_StubDb(_long_threads()), llm, _SMALL)["ask_mailbox"](question="q?"))
        counts = _one_line(caplog)["counts"]
        assert counts["token_limit_evidence_budget"] == 1
        assert counts["token_limit_output_max_tokens"] == 1
        assert "token_limit_prompt_over_budget" not in counts

    def test_prompt_over_budget_is_marked_on_the_timing_line(self, caplog):
        caplog.set_level(logging.INFO)
        with pytest.raises(ToolError):
            asyncio.run(
                _tools(_StubDb(_long_threads(n=1)), FakeInferenceClient(), _SMALL)["ask_mailbox"](
                    question="why " * 3000
                )
            )
        timing = _one_line(caplog)
        assert timing["outcome"] == "error"
        assert timing["counts"] == {"results": 1, "token_limit_prompt_over_budget": 1}

    def test_no_limit_leaves_no_timing_marker(self, caplog):
        caplog.set_level(logging.INFO)
        tools = _tools(_StubDb(_short_threads()), FakeInferenceClient(response="ok [E1]"))
        asyncio.run(tools["ask_mailbox"](question="q?"))
        assert not [k for k in _one_line(caplog)["counts"] if k.startswith("token_limit_")]

    # Item 6: the structured-output schema counts toward the prompt.
    def test_extract_prompt_tokens_include_the_structured_schema(self, caplog):
        caplog.set_level(logging.INFO)
        threads = [_thread("t1", [_chunk("c1", f"{_MARKER} " + "x" * 9000)])]
        # Room for the schema reserve, while the passage is still cut.
        small = PromptBudget(context_tokens=2800, max_output_tokens=1024)
        llm = FakeInferenceClient(response='{"records": []}', structured_output=True)
        asyncio.run(
            _tools(_StubDb(threads), llm, small)["extract_from_emails"](
                query=_MARKER, schema={"amount": "number"}
            )
        )
        (system, user), schema = llm.complete_calls[0], llm.json_schemas[0]
        assert schema is not None
        reserve = _schema_reserve_chars(schema)
        assert reserve > 0
        expected = -(-(len(system) + len(user) + reserve) // CHARS_PER_TOKEN)
        assert _one_limit_line(caplog)["counts"]["prompt_tokens"] == expected
        assert _MARKER not in caplog.text

    # Coordinator P3: a dropped thread with the cap trimming the rest is
    # counted once, as the cap, not also as window-cut passages.
    def test_dropped_thread_with_cap_trim_is_not_counted_twice(self, caplog):
        caplog.set_level(logging.INFO)
        threads = [
            _thread(f"t{i}", [_chunk(f"c{i}", f"{_MARKER} " + "x" * 9000)]) for i in range(2)
        ]
        for t in threads:
            t.subject = f"{_MARKER} " + "h" * 600
            t.participants = [f"{_MARKER} " + "h" * 600] * 3
        budget = PromptBudget(context_tokens=3480, max_output_tokens=1024)
        llm = FakeInferenceClient(response="ok [E1]")
        asyncio.run(_tools(_StubDb(threads), llm, budget)["ask_mailbox"](question="q?"))
        line = _one_limit_line(caplog)
        assert line["limits"] == ["evidence_budget"]
        assert line["counts"]["threads_dropped"] == 1
        assert line["counts"]["passages_omitted"] == 0
        assert line["counts"]["passages_truncated"] == 0
        assert _capped(caplog) == 1
        assert _MARKER not in caplog.text


def _summary_thread() -> ThreadResult:
    return _thread("t1", [], body=f"{_MARKER} body")


class TestReviewRound2:
    """Codex review round 2 on #883."""

    # Item 1: a cut repair reply is counted against the repair prompt.
    @pytest.mark.parametrize("partial", [f"{_MARKER} partial [E1]", ""])
    def test_ask_mailbox_cut_repair_counts_the_repair_prompt(self, caplog, partial):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(
            complete_responses=[
                f"{_MARKER} uncited",  # forces the repair call
                InferenceTruncatedError(partial=partial),
            ]
        )
        tool = _tools(_StubDb(_short_threads()), llm)["ask_mailbox"]
        if partial:
            asyncio.run(tool(question="q?"))
        else:
            with pytest.raises(ToolError):
                asyncio.run(tool(question="q?"))
        assert len(llm.complete_calls) == 2
        repair_system, repair_user = llm.complete_calls[1]
        assert repair_user != llm.complete_calls[0][1]
        line = _one_limit_line(caplog)
        assert line["limits"] == ["output_max_tokens"]
        assert line["counts"]["prompt_tokens"] == estimate_tokens(repair_system + repair_user)
        assert _MARKER not in caplog.text

    def test_ask_mailbox_cut_first_reply_counts_the_first_prompt(self, caplog):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(complete_responses=[InferenceTruncatedError(partial="p [E1]")])
        asyncio.run(_tools(_StubDb(_short_threads()), llm)["ask_mailbox"](question="q?"))
        system, user = llm.complete_calls[0]
        assert _one_limit_line(caplog)["counts"]["prompt_tokens"] == estimate_tokens(system + user)

    @pytest.mark.parametrize("partial", [f"{_MARKER} partial [E1]", ""])
    def test_summarize_thread_cut_repair_counts_the_repair_prompt(self, caplog, partial):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(
            complete_responses=[f"{_MARKER} uncited", InferenceTruncatedError(partial=partial)]
        )
        tool = _tools(_StubDb([_summary_thread()]), llm)["summarize_thread"]
        if partial:
            asyncio.run(tool(thread_id="t1"))
        else:
            with pytest.raises(ToolError):
                asyncio.run(tool(thread_id="t1"))
        assert len(llm.complete_calls) == 2
        repair_system, repair_user = llm.complete_calls[1]
        line = _one_limit_line(caplog)
        assert line["limits"] == ["output_max_tokens"]
        assert line["counts"]["prompt_tokens"] == estimate_tokens(repair_system + repair_user)
        assert _MARKER not in caplog.text

    # Item 3: a stop at the model's context window is its own limit.
    @pytest.mark.parametrize("partial", [f"{_MARKER} partial [E1]", ""])
    def test_ask_mailbox_context_window_stop(self, caplog, partial):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(
            complete_responses=[InferenceTruncatedError(partial=partial, reason="context_window")]
        )
        tool = _tools(_StubDb(_short_threads()), llm)["ask_mailbox"]
        if partial:
            out = asyncio.run(tool(question="q?"))
            # The caller-facing notice names the setting for this stop (#890).
            assert "INFERENCE_CONTEXT_TOKENS" in out.structured_content["answer"]
            assert "INFERENCE_MAX_TOKENS" not in out.structured_content["answer"]
        else:
            with pytest.raises(ToolError):
                asyncio.run(tool(question="q?"))
        line = _one_limit_line(caplog)
        assert line["limits"] == ["context_window"]
        assert line["counts"]["outputs_cut"] == 1
        assert line["counts"]["context_window_cuts"] == 1
        counts = _one_line(caplog)["counts"]
        assert counts["token_limit_context_window"] == 1
        assert "token_limit_output_max_tokens" not in counts
        assert _MARKER not in caplog.text

    def test_summarize_thread_context_window_stop(self, caplog):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(
            complete_responses=[InferenceTruncatedError(partial="s [E1]", reason="context_window")]
        )
        asyncio.run(_tools(_StubDb([_summary_thread()]), llm)["summarize_thread"](thread_id="t1"))
        assert _one_limit_line(caplog)["limits"] == ["context_window"]

    def test_extract_counts_each_stop_reason(self, caplog):
        caplog.set_level(logging.INFO)
        threads = [_thread(f"t{i}", [_chunk(f"c{i}", f"{_MARKER} invoice {i}")]) for i in range(3)]
        llm = FakeInferenceClient(
            complete_responses=[
                InferenceTruncatedError(partial="{", reason="context_window"),
                InferenceTruncatedError(partial="{"),
                "null",
            ]
        )
        asyncio.run(
            _tools(_StubDb(threads), llm)["extract_from_emails"](
                query=_MARKER, schema={"amount": "number"}
            )
        )
        line = _one_limit_line(caplog)
        assert line["limits"] == ["output_max_tokens", "context_window"]
        assert line["counts"]["outputs_cut"] == 2
        assert line["counts"]["context_window_cuts"] == 1
        counts = _one_line(caplog)["counts"]
        assert counts["token_limit_output_max_tokens"] == 1
        assert counts["token_limit_context_window"] == 1
        assert _MARKER not in caplog.text


class TestTruncationText:
    """#890: the text the caller sees for a cut reply names the setting
    that fixes that stop. A ``max_tokens`` stop points to
    ``INFERENCE_MAX_TOKENS``; a stop at the model's own context window
    points to ``INFERENCE_CONTEXT_TOKENS`` (or another model), never to
    the output reserve, which a larger value would only make worse. Both
    texts are fixed: nothing from the reply or the mail is in them."""

    _NOTICES = {
        "max_tokens": (
            "\n\n[Answer cut off at the INFERENCE_MAX_TOKENS limit; raise it for a complete "
            "answer.]"
        ),
        "context_window": (
            "\n\n[Answer cut off at the model's context window; lower INFERENCE_CONTEXT_TOKENS "
            "to the model's real window or below, or use a model with a larger one, for a "
            "complete answer.]"
        ),
    }
    _ERRORS = {
        "max_tokens": (
            "Inference output hit the max_tokens limit before finishing "
            "(raise INFERENCE_MAX_TOKENS)"
        ),
        "context_window": (
            "Inference output hit the model's context window before finishing "
            "(lower INFERENCE_CONTEXT_TOKENS to the model's real window or below, or use "
            "a model with a larger one)"
        ),
    }
    _WRONG = {"max_tokens": "INFERENCE_CONTEXT_TOKENS", "context_window": "INFERENCE_MAX_TOKENS"}

    def test_context_window_text_advises_lowering_the_window(self):
        """Review round 1 on #914: the window can already match the
        model's and still fill, because the three-characters-per-token
        estimate undercounts dense text (CJK, digit or base64 runs). The
        fix that always applies is a lower INFERENCE_CONTEXT_TOKENS."""
        for text in (
            _TRUNCATED_NOTICES["context_window"],
            str(InferenceTruncatedError(partial="", reason="context_window")),
        ):
            assert "lower INFERENCE_CONTEXT_TOKENS to the model's real window or below" in text

    @pytest.mark.parametrize("reason", ["max_tokens", "context_window"])
    def test_notice_text_is_fixed_per_reason(self, reason):
        assert _TRUNCATED_NOTICES[reason] == self._NOTICES[reason]
        assert self._WRONG[reason] not in _TRUNCATED_NOTICES[reason]

    @pytest.mark.parametrize("reason", ["max_tokens", "context_window"])
    def test_error_text_is_fixed_per_reason(self, reason):
        err = InferenceTruncatedError(partial=f"{_MARKER} partial", reason=reason)
        assert str(err) == self._ERRORS[reason]
        assert self._WRONG[reason] not in str(err)
        assert _MARKER not in str(err)

    @pytest.mark.parametrize("reason", ["max_tokens", "context_window"])
    def test_ask_mailbox_partial_answer_carries_the_reason_notice(self, caplog, reason):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(
            complete_responses=[InferenceTruncatedError(partial="p [E1]", reason=reason)]
        )
        out = asyncio.run(_tools(_StubDb(_short_threads()), llm)["ask_mailbox"](question="q?"))
        answer = out.structured_content["answer"]
        assert answer == "p [E1]" + self._NOTICES[reason]
        assert self._WRONG[reason] not in answer
        # The notice is not a citation problem: the check strips it (#284).
        assert out.structured_content["citation_problems"] == []
        assert _one_limit_line(caplog)["counts"]["outputs_cut"] == 1

    @pytest.mark.parametrize("reason", ["max_tokens", "context_window"])
    def test_summarize_thread_partial_summary_carries_the_reason_notice(self, reason):
        llm = FakeInferenceClient(
            complete_responses=[InferenceTruncatedError(partial="s [E1]", reason=reason)]
        )
        out = asyncio.run(
            _tools(_StubDb([_summary_thread()]), llm)["summarize_thread"](thread_id="t1")
        )
        text = out.content[0].text
        assert "s [E1]" + self._NOTICES[reason] + "\n\nCitations:" in text
        assert self._WRONG[reason] not in text

    @pytest.mark.parametrize("reason", ["max_tokens", "context_window"])
    def test_empty_reply_error_names_the_reason_setting(self, caplog, reason):
        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(
            complete_responses=[InferenceTruncatedError(partial="", reason=reason)]
        )
        with pytest.raises(ToolError) as err:
            asyncio.run(
                _tools(_StubDb(_short_threads()), llm)["ask_mailbox"](question=f"{_MARKER}?")
            )
        assert str(err.value) == f"Error: {self._ERRORS[reason]}"
        assert self._WRONG[reason] not in str(err.value)
        assert _MARKER not in str(err.value)
        assert _MARKER not in caplog.text

    # #950: extract_from_emails's failure line names the setting per stop
    # reason, counting each reason apart when one call has both.
    _EXTRACT_CUTS = {
        "max_tokens": "cut off at the INFERENCE_MAX_TOKENS limit",
        "context_window": (
            "cut off at the model's context window: lower INFERENCE_CONTEXT_TOKENS "
            "to the model's real window or below, or use a model with a larger one"
        ),
    }

    @pytest.mark.parametrize(
        ("stops", "expected"),
        [
            (["max_tokens"], "1 {max_tokens}"),
            (["context_window"], "1 {context_window}"),
            (
                ["context_window", "max_tokens", "context_window"],
                "1 {max_tokens}; 2 {context_window}",
            ),
        ],
        ids=["max_tokens", "context_window", "mixed"],
    )
    def test_extract_failure_line_names_the_reason_setting(self, caplog, stops, expected):
        caplog.set_level(logging.INFO)
        threads = [_thread(f"t{i}", [_chunk(f"c{i}", f"{_MARKER} invoice {i}")]) for i in range(3)]
        llm = FakeInferenceClient(
            complete_responses=[
                *(InferenceTruncatedError(partial=f"{_MARKER} {{", reason=s) for s in stops),
                *(["null"] * (3 - len(stops))),
            ]
        )
        out = asyncio.run(
            _tools(_StubDb(threads), llm)["extract_from_emails"](
                query=_MARKER, schema={"amount": "number"}
            )
        )
        reasons = expected.format(**self._EXTRACT_CUTS)
        notice = (
            f"Incomplete: {len(stops)} of 3 threads could not be extracted ({reasons}), "
            "so any matching data in them is missing."
        )
        assert out.structured_content["notice"] == notice
        assert out.content[0].text == f"No records extracted. {notice}"
        if "max_tokens" not in stops:
            assert "INFERENCE_MAX_TOKENS" not in notice
        line = _one_limit_line(caplog)
        assert line["counts"]["outputs_cut"] == len(stops)
        assert line["counts"]["context_window_cuts"] == stops.count("context_window")
        assert _MARKER not in notice
        assert _MARKER not in caplog.text
