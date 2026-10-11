"""
ask_mailbox answers from a ranked sample of threads, and says so (#1233).

Every result carries a structured ``retrieval`` disclosure (threads
retrieved, threads whose evidence reached the prompt, the effective
``max_threads``, ``exhaustive: false``, the remainder unknown) and one
fixed sentence in the text. Neither empty return claims anything about
the mailbox, and the unmatched-``from_name`` return, where retrieval
never ran, is told apart from a search that ranked nothing. All data
is synthetic.
"""

import asyncio
import logging
import re

import pytest
from src.tools.intelligence import ASK_SYSTEM
from src.tools.outputs import AskMailboxOutput, AskRetrieval

from tests.conftest import FakeInferenceClient
from tests.test_prompt_budget import _SMALL, _chunk, _long_threads, _StubDb, _thread, _tools

_MARKER = "SYNTHETIC_SAMPLE_MARKER_1233"
_BLOCK_RE = re.compile(r'<untrusted_email index="\d+">\n(.*?)\n</untrusted_email>', re.DOTALL)
_SAMPLE_SENTENCE = "this answer comes from a ranked sample"
_NOT_RUN_SENTENCE = "Retrieval note: retrieval did not run"


class _LimitDb(_StubDb):
    """Returns at most ``limit`` threads, as the real ranked search does,
    and no contact for any name."""

    def hybrid_search(self, **kwargs):
        return list(self._threads)[: kwargs["limit"]]

    def find_contact(self, *_args, **_kwargs):
        return []


def _threads(n: int) -> list:
    return [
        _thread(f"t{i}", [_chunk(f"c{i}", f"{_MARKER} passage of thread {i}")]) for i in range(n)
    ]


def _supplied_in_prompt(user_prompt: str) -> int:
    """Thread blocks in the prompt the handler built that hold at least
    one labelled passage: the threads whose evidence reached the model."""
    blocks = _BLOCK_RE.findall(user_prompt)
    assert blocks
    return sum(1 for b in blocks if re.search(r"^\[E\d+ \|", b.split("Body:\n", 1)[1], re.M))


def _call(db, llm=None, budget=None, **args):
    llm = llm or FakeInferenceClient(response=f"{_MARKER} answer [E1]")
    out = asyncio.run(_tools(db, llm, budget)["ask_mailbox"](question=f"{_MARKER}?", **args))
    return out, llm


def _disclosure(out) -> dict:
    return out.structured_content["retrieval"]


def _text(out) -> str:
    return "\n".join(c.text for c in out.content)


class TestRetrievalDisclosure:
    def test_ordinary_answer_discloses_the_sample(self):
        out, llm = _call(_LimitDb(_threads(2)))
        d = _disclosure(out)
        assert d == {
            "retrieval_ran": True,
            "threads_retrieved": 2,
            "threads_supplied": 2,
            "max_threads": 5,
            "exhaustive": False,
            "remainder": "unknown",
        }
        assert d["threads_supplied"] == _supplied_in_prompt(llm.complete_calls[0][1])
        text = _text(out)
        assert _SAMPLE_SENTENCE in text
        assert "2 retrieved thread(s) (max_threads=5), 2 with evidence in the prompt" in text
        assert "not an exhaustive search" in text
        # The disclosure is not a budget notice.
        assert out.structured_content["coverage_note"] is None

    @pytest.mark.parametrize(("asked", "effective"), [(3, 3), (1, 1), (50, 10), (0, 1)])
    def test_capped_at_max_threads_reports_the_effective_cap(self, asked, effective):
        out, llm = _call(_LimitDb(_threads(12)), max_threads=asked)
        d = _disclosure(out)
        assert d["max_threads"] == effective
        assert d["threads_retrieved"] == effective
        assert d["threads_supplied"] == effective
        assert d["threads_supplied"] == _supplied_in_prompt(llm.complete_calls[0][1])
        assert d["exhaustive"] is False and d["remainder"] == "unknown"
        assert f"(max_threads={effective})" in _text(out)

    def test_threads_shown_with_headers_only_are_not_counted_as_supplied(self):
        # A thread with no chunks, no indexed text and no snippet renders no
        # passage: it was retrieved, but no evidence of it reached the model.
        # The prompt budget cut nothing, so there is no coverage note.
        threads = [*_threads(2), _thread("bare", [])]
        out, llm = _call(_LimitDb(threads))
        d = _disclosure(out)
        assert d["threads_retrieved"] == 3
        assert d["threads_supplied"] == 2
        assert _supplied_in_prompt(llm.complete_calls[0][1]) == 2
        assert out.structured_content["coverage_note"] is None

    def test_schema_names_both_causes_of_a_lower_supplied_count(self):
        # Review round 1: a thread with no indexed text is not a budget cut.
        description = AskRetrieval.model_fields["threads_supplied"].description or ""
        assert "prompt budget" in description
        assert "no indexed text to show (no coverage_note for that)" in description

    def test_budget_dropped_threads_are_counted_from_the_rendered_evidence(self):
        threads = _long_threads(n=5, chunks=1, size=300)
        for t in threads:
            t.subject = "h" * 600
            t.participants = ["h" * 600] * 3
        out, llm = _call(_LimitDb(threads), budget=_SMALL)
        d = _disclosure(out)
        user = llm.complete_calls[0][1]
        assert d["threads_retrieved"] == 5
        assert 0 < d["threads_supplied"] < 5
        assert d["threads_supplied"] == _supplied_in_prompt(user)
        # ``Sources searched`` still lists all five; the supplied count does not.
        assert len(out.structured_content["threads"]) == 5
        # coverage_note stays the budget notice, without the sample sentence.
        note = out.structured_content["coverage_note"]
        assert "lower-ranked retrieved thread(s) were left out entirely" in note
        assert _SAMPLE_SENTENCE not in note
        assert _SAMPLE_SENTENCE in _text(out)

    def test_empty_retrieval_says_the_search_ran_and_ranked_nothing(self):
        llm = FakeInferenceClient()
        out, _ = _call(_LimitDb([]), llm)
        assert llm.complete_calls == []
        d = _disclosure(out)
        assert d == {
            "retrieval_ran": True,
            "threads_retrieved": 0,
            "threads_supplied": 0,
            "max_threads": 5,
            "exhaustive": False,
            "remainder": "unknown",
        }
        answer = out.structured_content["answer"]
        assert answer.startswith("No relevant emails were retrieved")
        assert "ranked no threads" in answer
        text = _text(out)
        assert _SAMPLE_SENTENCE in text and "0 retrieved thread(s)" in text
        assert _NOT_RUN_SENTENCE not in text

    def test_unmatched_from_name_says_retrieval_did_not_run(self):
        llm = FakeInferenceClient()
        out, _ = _call(_LimitDb(_threads(2)), llm, from_name="zzz-no-such-sender", max_threads=7)
        assert llm.complete_calls == []
        d = _disclosure(out)
        assert d == {
            "retrieval_ran": False,
            "threads_retrieved": 0,
            "threads_supplied": 0,
            "max_threads": 7,
            "exhaustive": False,
            "remainder": "unknown",
        }
        answer = out.structured_content["answer"]
        assert answer.startswith("No relevant emails were retrieved")
        assert "retrieval did not run" in answer
        assert "no contact matched from_name" in answer
        text = _text(out)
        assert _NOT_RUN_SENTENCE in text
        assert _SAMPLE_SENTENCE not in text

    @pytest.mark.parametrize("from_name", [None, "zzz-no-such-sender"])
    def test_empty_returns_make_no_claim_about_the_mailbox(self, from_name):
        out, _ = _call(_LimitDb([]), FakeInferenceClient(), from_name=from_name)
        text = _text(out).lower()
        assert "found" not in text
        assert "no relevant emails were retrieved" in text

    def test_disclosure_and_logs_carry_no_mail(self, caplog):
        caplog.set_level(logging.DEBUG)
        out, _ = _call(_LimitDb(_threads(3)))
        assert _MARKER not in str(out.structured_content["retrieval"])
        assert _MARKER not in caplog.text


class TestSamplePrompt:
    def test_system_prompt_calls_the_evidence_a_ranked_sample(self):
        prompt = " ".join(ASK_SYSTEM.split())
        assert "ranked sample of the mailbox, not all of it" in prompt
        assert "absence from them says nothing about the rest of the mailbox" in prompt
        # Review round 2: the ban is on mailbox-wide completeness only.
        assert "that a list or count covers all of it" in prompt
        assert "within one message or attachment, a list or count can be complete" in prompt
        assert "a list or count is complete." not in prompt

    def test_schema_does_not_promise_what_the_prompt_only_asks(self):
        # Review round 2: the citation check does not detect a mailbox-wide
        # claim, so the schema points at the disclosure, not the answer.
        description = AskMailboxOutput.model_fields["retrieval"].description or ""
        assert "never a statement about the whole mailbox" not in description
        assert "an instruction, not a check" in description

    def test_initial_and_repair_prompts_carry_the_sample_instruction(self):
        llm = FakeInferenceClient(complete_responses=["no labels here", "answer [E1]"])
        _call(_LimitDb(_threads(2)), llm)
        assert len(llm.complete_calls) == 2
        for system, _user in llm.complete_calls:
            assert "ranked sample of the mailbox" in " ".join(system.split())
