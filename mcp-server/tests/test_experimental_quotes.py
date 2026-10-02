"""
Quote verification in the experimental ``check_conclusion`` and
``brief_issue`` tools (PLAN.md Phase 5 item 2).

Each double-quoted passage of three or more words in a finding's
explanation, the verdict summary or a brief entry is searched in the
indexed text of the passages it cites, with ask_mailbox's quote checker
(#284, #495, #519): verified, misattributed, unmatched, uncited or
not_checked. A misattributed or unmatched quote is a citation problem
and gets the tools' one repair call. A fake inference client answers
from the labels it finds in the prompt. All data is synthetic.
"""

import logging
from pathlib import Path

import pytest
from src.lib.sqlite import Database
from src.tools import intelligence

from tests.conftest import claimant_of
from tests.test_brief_issue import _MAILBOX as _BRIEF_MAILBOX
from tests.test_brief_issue import ScriptedInference, _build, _labels
from tests.test_brief_issue import _brief as _brief_reply
from tests.test_brief_issue import _run as _run_brief
from tests.test_check_conclusion import _MAILBOX, _check, _run


@pytest.fixture
def check_db(tmp_path: Path) -> Database:
    return _build(tmp_path / "check.db", _MAILBOX)


@pytest.fixture
def brief_db(tmp_path: Path) -> Database:
    return _build(tmp_path / "brief.db", _BRIEF_MAILBOX)


_MARKER = "SYNTHETIC_QUOTE_MARKER_9052"

# Words of the supporting and contradicting passages, verbatim.
_SUPPORT_QUOTE = "renews automatically each year"
_CONTRADICT_QUOTE = "renewal needs a signed order"


def _label(user: str, message_id: str) -> str:
    return _labels(user)[claimant_of(message_id)]


def _finding(explanation: str, *message_ids: str, user: str, relation: str = "supports"):
    return {
        "relation": relation,
        "explanation": explanation,
        "labels": [_label(user, mid) for mid in message_ids],
    }


def _one_finding(explanation: str, *message_ids: str, verdict: str = "Mixed."):
    """A reply script: one finding citing ``message_ids``."""

    def reply(user: str) -> str:
        return _check(
            verdict_summary=verdict,
            findings=[_finding(explanation, *message_ids, user=user)],
        )

    return reply


def _corrective(llm: ScriptedInference) -> str:
    (_s1, user1), (_s2, user2) = llm.complete_calls
    assert user2.startswith(user1)
    return user2[len(user1) :]


class TestConclusionQuotes:
    def test_a_quote_from_the_cited_passage_is_verified(self, check_db):
        llm = ScriptedInference(
            _one_finding(f'Alice writes "{_SUPPORT_QUOTE}".', "support@example.com")
        )
        out = _run(check_db, llm)
        assert len(llm.complete_calls) == 1
        data = out.structured_content
        label = data["findings"][0]["labels"][0]
        assert data["quotes"] == [
            {"text": _SUPPORT_QUOTE, "status": "verified", "found_in": [label], "item": 0}
        ]
        assert data["citation_problems"] == []
        assert data["repair_attempted"] is False
        text = out.content[0].text
        assert "Quote check: 1 of 1 quote(s) match the indexed text of a cited passage" in text
        assert "quotes are checked" in text.splitlines()[0]

    def test_an_altered_quote_is_unmatched_and_gets_one_repair(self, check_db):
        altered = _one_finding('It "renews automatically every year".', "support@example.com")
        llm = ScriptedInference(altered, altered)
        out = _run(check_db, llm)
        assert len(llm.complete_calls) == 2  # never more than one repair
        data = out.structured_content
        assert data["repair_attempted"] is True
        assert [q["status"] for q in data["quotes"]] == ["unmatched"]
        assert data["citation_problems"] == [{"item": 0, "kind": "unmatched_quotes", "labels": []}]
        corrective = _corrective(llm)
        assert "quote" in corrective
        assert "every year" not in corrective  # the rejected reply is not replayed
        text = out.content[0].text
        assert "Citation check: finding 1: unmatched_quotes." in text
        assert "Quote check: 0 of 1 quote(s)" in text

    def test_a_quote_from_an_uncited_passage_is_misattributed(self, check_db):
        wrong = _one_finding(f'It says "{_CONTRADICT_QUOTE}".', "support@example.com")
        llm = ScriptedInference(wrong, wrong)
        out = _run(check_db, llm)
        assert len(llm.complete_calls) == 2
        data = out.structured_content
        contradict = _label(llm.complete_calls[0][1], "contradict@example.com")
        [quote] = data["quotes"]
        assert quote["status"] == "misattributed"
        assert quote["found_in"] == [contradict]
        assert data["citation_problems"] == [
            {"item": 0, "kind": "misattributed_quotes", "labels": [contradict]}
        ]
        assert f"misattributed_quotes: {contradict}." in out.content[0].text

    def test_the_repaired_reply_is_rechecked(self, check_db):
        wrong = _one_finding(f'It says "{_CONTRADICT_QUOTE}".', "support@example.com")
        right = _one_finding(f'It says "{_CONTRADICT_QUOTE}".', "contradict@example.com")
        llm = ScriptedInference(wrong, right)
        out = _run(check_db, llm)
        assert len(llm.complete_calls) == 2
        data = out.structured_content
        assert data["repair_attempted"] is True
        assert data["citation_problems"] == []
        assert [q["status"] for q in data["quotes"]] == ["verified"]

    def test_an_overlong_quote_is_not_checked_and_not_searched(self, check_db, monkeypatch):
        calls = 0
        real = intelligence._quote_in

        def counting(fragments, text):
            nonlocal calls
            calls += 1
            return real(fragments, text)

        monkeypatch.setattr(intelligence, "_quote_in", counting)
        long_quote = "renews " * 200  # over 1,000 characters
        llm = ScriptedInference(
            _one_finding(f'It says "{long_quote.strip()}".', "support@example.com")
        )
        out = _run(check_db, llm)
        assert len(llm.complete_calls) == 1  # not a problem, so no repair
        data = out.structured_content
        [quote] = data["quotes"]
        assert quote["status"] == "not_checked"
        assert len(quote["text"]) == 1001
        assert data["citation_problems"] == []
        assert calls == 0

    def test_quotes_past_the_cap_are_not_checked(self, check_db, monkeypatch):
        calls = 0
        real = intelligence._quote_in

        def counting(fragments, text):
            nonlocal calls
            calls += 1
            return real(fragments, text)

        monkeypatch.setattr(intelligence, "_quote_in", counting)
        many = " ".join(f'"{_SUPPORT_QUOTE}"' for _ in range(25))
        llm = ScriptedInference(_one_finding(many, "support@example.com"))
        out = _run(check_db, llm)
        statuses = [q["status"] for q in out.structured_content["quotes"]]
        assert statuses == ["verified"] * 20 + ["not_checked"] * 5
        assert calls == 20  # one cited passage per checked quote

    def test_scare_quotes_are_not_checked(self, check_db):
        llm = ScriptedInference(
            _one_finding('The "auto renewal" clause applies.', "support@example.com")
        )
        out = _run(check_db, llm)
        assert out.structured_content["quotes"] == []
        assert "Quote check" not in out.content[0].text

    def test_a_quote_in_an_uncited_finding_is_uncited_not_a_quote_problem(self, check_db):
        def reply(_u: str) -> str:
            return _check(
                findings=[
                    {"relation": "supports", "explanation": f'"{_SUPPORT_QUOTE}"', "labels": []}
                ]
            )

        llm = ScriptedInference(reply, reply)
        out = _run(check_db, llm)
        data = out.structured_content
        assert [q["status"] for q in data["quotes"]] == ["uncited"]
        assert [p["kind"] for p in data["citation_problems"]] == ["no_citations"]

    def test_verdict_quotes_are_checked_against_the_passages_the_findings_cite(self, check_db):
        llm = ScriptedInference(
            _one_finding(
                "Section 4 states it.",
                "support@example.com",
                verdict=f'Supported: "{_SUPPORT_QUOTE}".',
            )
        )
        out = _run(check_db, llm)
        data = out.structured_content
        assert [(q["item"], q["status"]) for q in data["quotes"]] == [(None, "verified")]
        assert data["citation_problems"] == []
        assert len(llm.complete_calls) == 1

    def test_a_misattributed_verdict_quote_is_a_problem_of_the_verdict(self, check_db):
        wrong = _one_finding(
            "Section 4 states it.",
            "support@example.com",
            verdict=f'But "{_CONTRADICT_QUOTE}".',
        )
        llm = ScriptedInference(wrong, wrong)
        out = _run(check_db, llm)
        assert len(llm.complete_calls) == 2
        data = out.structured_content
        contradict = _label(llm.complete_calls[0][1], "contradict@example.com")
        assert data["citation_problems"] == [
            {"item": None, "kind": "misattributed_quotes", "labels": [contradict]}
        ]
        assert "Citation check: the verdict summary: misattributed_quotes" in out.content[0].text

    def test_quote_text_never_reaches_the_logs_or_the_repair(self, check_db, caplog):
        bad = _one_finding(f'It says "{_MARKER} renews each year".', "support@example.com")
        llm = ScriptedInference(bad, bad)
        with caplog.at_level(logging.DEBUG):
            out = _run(check_db, llm)
        assert out.structured_content["quotes"][0]["status"] == "unmatched"
        assert _MARKER not in caplog.text
        assert _MARKER not in _corrective(llm)

    def test_instructions_ask_for_exact_quotes(self):
        from src.tools.brief import BRIEF_SYSTEM, CHECK_SYSTEM

        for system in (CHECK_SYSTEM, BRIEF_SYSTEM):
            flat = " ".join(system.split())
            assert "copy its words exactly inside double quotes" in flat


class TestBriefQuotes:
    def test_entry_quotes_are_checked_and_located(self, brief_db):
        def reply(user: str) -> str:
            label = {mid: _labels(user)[claimant_of(mid)] for mid in _BRIEF_MAILBOX}
            return _brief_reply(
                chronology=[
                    {
                        "date": "2024-04-01",
                        "date_source": "sent",
                        "actor": "Alice",
                        "event": 'proposed to "book the lakeside venue"',
                        "labels": [label["proposal@example.com"]],
                    }
                ],
                positions=[
                    {
                        "actor": "Dana",
                        "position": 'says "the offsite moves online"',
                        "labels": [label["hall-a@example.com"]],
                    }
                ],
            )

        llm = ScriptedInference(reply, reply)
        out = _run_brief(brief_db, llm)
        assert len(llm.complete_calls) == 2
        data = out.structured_content
        user = llm.complete_calls[0][1]
        cancel = _label(user, "cancel@example.com")
        proposal = _label(user, "proposal@example.com")
        assert data["quotes"] == [
            {
                "text": "book the lakeside venue",
                "status": "verified",
                "found_in": [proposal],
                "section": "chronology",
                "item": 0,
            },
            {
                "text": "the offsite moves online",
                "status": "misattributed",
                "found_in": [cancel],
                "section": "positions",
                "item": 0,
            },
        ]
        assert data["citation_problems"] == [
            {
                "section": "positions",
                "item": 0,
                "kind": "misattributed_quotes",
                "labels": [cancel],
            }
        ]
        text = out.content[0].text
        assert "quotes are not verified" not in text
        assert "Quote check: 1 of 2 quote(s)" in text
        assert "Citation check: positions entry 1: misattributed_quotes" in text
        assert "quote" in _corrective(llm)

    def test_an_unmatched_quote_is_repaired_once(self, brief_db):
        def reply(quote: str):
            def make(user: str) -> str:
                label = _label(user, "approval@example.com")
                return _brief_reply(decisions=[{"decision": f'"{quote}"', "labels": [label]}])

            return make

        llm = ScriptedInference(reply("book the hillside venue"), reply("book the lakeside venue"))
        out = _run_brief(brief_db, llm)
        assert len(llm.complete_calls) == 2
        data = out.structured_content
        assert data["repair_attempted"] is True
        assert data["citation_problems"] == []
        assert [q["status"] for q in data["quotes"]] == ["verified"]
        assert "hillside" not in _corrective(llm)


def test_check_db_fixture_mailbox_holds_the_quoted_words():
    """The quotes above are verbatim words of the synthetic mailbox."""
    bodies = {mid: body for mid, (_t, _s, _f, body) in _MAILBOX.items()}
    assert _SUPPORT_QUOTE in bodies["support@example.com"]
    assert _CONTRADICT_QUOTE in bodies["contradict@example.com"]
