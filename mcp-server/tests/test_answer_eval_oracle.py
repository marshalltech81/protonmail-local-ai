"""The scripted oracle cites visible fact evidence, not just a source ID (#1582)."""

from dataclasses import replace

import pytest

from tests.answer_eval.cases import Fact, load_cases
from tests.baseline.test_answer_eval_cases import _OracleAnswerer

_MESSAGE = "t01.1@baseline.example"
_EXCERPT = "The approved count is 4."


def _case(*, source="t01.1"):
    return replace(
        load_cases()[0],
        tool="ask_mailbox",
        answerable=True,
        required_evidence=(("t01.1",),),
        expected_facts=(Fact("f1", "The count is four", (source,), _EXCERPT),),
        must_include=(("4",),),
        chronology=None,
    )


def _passage(label, text):
    return f"[{label} | message {_MESSAGE}#0000abcd | chunk 0 chars 0-{len(text)}]\n{text}"


@pytest.mark.parametrize("text", ["Introductory text", "The value 4 appears before the evidence"])
def test_oracle_does_not_cite_a_passage_cut_before_its_fact(text):
    oracle = _OracleAnswerer(_case())
    assert oracle._items(_passage("E1", text)) == []


def test_oracle_chooses_a_later_passage_that_still_shows_the_fact():
    oracle = _OracleAnswerer(_case())
    items = oracle._items(_passage("E1", "Introduction") + "\n" + _passage("E2", _EXCERPT))
    assert items
    assert all(item.labels == ["E2"] for item in items)


def test_oracle_keeps_a_passage_cut_after_the_fact():
    items = _OracleAnswerer(_case())._items(_passage("E1", _EXCERPT))
    assert items
    assert all(item.labels == ["E1"] for item in items)


def test_another_sources_excerpt_does_not_qualify_a_passage():
    assert _OracleAnswerer(_case(source="t02.1"))._items(_passage("E1", _EXCERPT)) == []


def test_thread_text_without_message_headers_still_requires_visible_fact_text():
    oracle = _OracleAnswerer(_case())
    assert oracle._items("[E1 | thread text]\nIntroduction with the value 4") == []
    items = oracle._items("[E1 | thread text]\n" + _EXCERPT)
    assert items
    assert all(item.labels == ["E1"] for item in items)
