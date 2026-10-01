"""Unit tests for the agent-level eval scorers in ``tests/agent_metrics.py``.

Each scorer is pinned with a passing trace and with the failure it
exists to catch. IDs are synthetic.
"""

from __future__ import annotations

import pytest

from tests.agent_metrics import Scenario, score_trace, summarize


def _scenario(**overrides: object) -> Scenario:
    fields: dict = {
        "id": "s1",
        "category": "exact_fact",
        "question": "synthetic question",
        "expected_tools": ["search_emails"],
        "expected_arguments": {},
        "max_calls": 2,
        "required_evidence": [["a.1@x.example"]],
        "expected_messages": [],
    }
    fields.update(overrides)
    return Scenario(**fields)


def _search(thread_ids: list[str], **arguments: object) -> dict:
    return {
        "tool": "search_emails",
        "arguments": {"query": "q", **arguments},
        "result": {"results": [{"thread_id": t} for t in thread_ids]},
    }


def _page(
    message_ids: list[str],
    *,
    has_more: bool,
    cursor: str | None = None,
    next_cursor: str = "next",
    filters: dict | None = None,
) -> dict:
    arguments: dict = {"folder": "Sent", "limit": 2} if filters is None else dict(filters)
    if cursor:
        arguments["cursor"] = cursor
    return {
        "tool": "query_messages",
        "arguments": arguments,
        "result": {
            "has_more": has_more,
            "next_cursor": next_cursor if has_more else None,
            "messages": [
                {"message_id": m, "claimant_id": f"{m}#0000abcd", "thread_id": "t.1@x.example"}
                for m in message_ids
            ],
        },
    }


def _trace(calls: list[dict], cited: list[str] | None = None) -> dict:
    return {"scenario": "s1", "calls": calls, "answer": {"text": "synthetic", "cited": cited or []}}


class TestToolSelection:
    def test_first_call_with_an_expected_tool_is_selected(self) -> None:
        score = score_trace(_scenario(), _trace([_search(["a.1@x.example"])]))
        assert score.tool_selected is True

    def test_expected_tool_only_after_a_wrong_first_call_is_not_selected(self) -> None:
        # Tool selection asks whether the agent reached for the right tool
        # first; a detour is counted here and again as an extra call.
        wrong = {"tool": "list_threads", "arguments": {}, "result": {"threads": []}}
        score = score_trace(_scenario(), _trace([wrong, _search(["a.1@x.example"])]))
        assert score.tool_selected is False
        assert "tool_selected" in score.failures

    def test_no_calls_is_not_selected(self) -> None:
        score = score_trace(_scenario(), _trace([]))
        assert score.tool_selected is False


class TestArguments:
    def test_none_when_the_scenario_expects_no_arguments(self) -> None:
        score = score_trace(_scenario(), _trace([_search(["a.1@x.example"])]))
        assert score.arguments_correct is None

    def test_one_call_carrying_every_expected_argument_passes(self) -> None:
        scenario = _scenario(expected_arguments={"folders": ["Archive"]})
        trace = _trace([_search([]), _search(["a.1@x.example"], folders=["Archive"], limit=5)])
        assert score_trace(scenario, trace).arguments_correct is True

    def test_wrong_value_fails(self) -> None:
        scenario = _scenario(expected_arguments={"folders": ["Archive"]})
        trace = _trace([_search(["a.1@x.example"], folders=["INBOX"])])
        score = score_trace(scenario, trace)
        assert score.arguments_correct is False
        assert "arguments_correct" in score.failures

    def test_arguments_split_across_calls_fail(self) -> None:
        scenario = _scenario(
            expected_arguments={"folders": ["Archive"], "participant": "p@x.example"}
        )
        trace = _trace(
            [
                _search([], folders=["Archive"]),
                _search(["a.1@x.example"], participant="p@x.example"),
            ]
        )
        assert score_trace(scenario, trace).arguments_correct is False

    def test_right_arguments_on_an_unexpected_tool_fail(self) -> None:
        scenario = _scenario(expected_arguments={"folders": ["Archive"]})
        call = {
            "tool": "get_evidence",
            "arguments": {"query": "q", "folders": ["Archive"]},
            "result": {"threads": [{"thread_id": "a.1@x.example"}]},
        }
        assert score_trace(scenario, _trace([call])).arguments_correct is False


class TestEvidenceRecall:
    def test_counts_threads_seen_in_any_result(self) -> None:
        scenario = _scenario(required_evidence=[["a.1@x.example"], ["b.1@x.example"]])
        trace = _trace([_search(["a.1@x.example"]), _search(["z.1@x.example", "b.1@x.example"])])
        assert score_trace(scenario, trace).evidence_recall == 1.0

    def test_one_of_two_required_sources_is_half(self) -> None:
        scenario = _scenario(required_evidence=[["a.1@x.example"], ["b.1@x.example"]])
        score = score_trace(scenario, _trace([_search(["a.1@x.example"])]))
        assert score.evidence_recall == 0.5
        assert "evidence_recall" in score.failures

    def test_alternatives_satisfy_one_group(self) -> None:
        scenario = _scenario(required_evidence=[["a.1@x.example", "b.1@x.example"]])
        assert score_trace(scenario, _trace([_search(["b.1@x.example"])])).evidence_recall == 1.0

    def test_finds_ids_nested_in_any_result_shape(self) -> None:
        call = {
            "tool": "ask_mailbox",
            "arguments": {"question": "q"},
            "result": {"citations": [{"label": "E1", "thread_id": "a.1@x.example"}]},
        }
        assert (
            score_trace(_scenario(expected_tools=["ask_mailbox"]), _trace([call])).evidence_recall
            == 1.0
        )

    def test_arguments_do_not_count_as_retrieved(self) -> None:
        # An ID the agent typed into a call was not retrieved by it.
        call = {
            "tool": "get_thread",
            "arguments": {"thread_id": "a.1@x.example"},
            "result": {"messages": []},
        }
        assert score_trace(_scenario(), _trace([call])).evidence_recall == 0.0

    def test_none_without_required_evidence(self) -> None:
        score = score_trace(_scenario(required_evidence=[]), _trace([_search([])]))
        assert score.evidence_recall is None


class TestCitations:
    def test_cited_ids_returned_by_the_tools_are_valid(self) -> None:
        trace = _trace([_search(["a.1@x.example"])], cited=["a.1@x.example"])
        score = score_trace(_scenario(), trace)
        assert score.citation_validity == 1.0
        assert score.citation_recall == 1.0

    def test_an_id_no_tool_returned_is_invalid(self) -> None:
        trace = _trace([_search(["a.1@x.example"])], cited=["a.1@x.example", "made-up@x.example"])
        score = score_trace(_scenario(), trace)
        assert score.citation_validity == 0.5
        assert "citation_validity" in score.failures

    def test_a_cited_message_covers_its_thread(self) -> None:
        call = {
            "tool": "get_thread",
            "arguments": {"thread_id": "a.1@x.example"},
            # GetThreadOutput: the thread ID sits in the ``thread`` summary,
            # a sibling of ``messages``, not on each message row.
            "result": {
                "thread": {"thread_id": "a.1@x.example", "message_count": 2},
                "total_messages": 2,
                "messages": [
                    {"message_id": "a.2@x.example", "claimant_id": "a.2@x.example#0000abcd"}
                ],
                "next_offset": None,
            },
        }
        trace = _trace([call], cited=["a.2@x.example#0000abcd"])
        score = score_trace(_scenario(expected_tools=["get_thread"]), trace)
        assert score.citation_validity == 1.0
        assert score.citation_recall == 1.0

    def test_a_cited_chunk_id_is_valid_and_covers_its_thread(self) -> None:
        # EvidenceChunk.chunk_id is the passage ID ask_mailbox citations name.
        call = {
            "tool": "get_evidence",
            "arguments": {"query": "q"},
            "result": {
                "threads": [
                    {
                        "thread_id": "a.1@x.example",
                        "chunks": [{"chunk_id": "c-1", "message_id": "a.1@x.example"}],
                    }
                ]
            },
        }
        trace = _trace([call], cited=["c-1"])
        score = score_trace(_scenario(expected_tools=["get_evidence"]), trace)
        assert score.citation_validity == 1.0
        assert score.citation_recall == 1.0

    def test_retrieved_but_uncited_evidence_lowers_citation_recall(self) -> None:
        scenario = _scenario(required_evidence=[["a.1@x.example"], ["b.1@x.example"]])
        trace = _trace([_search(["a.1@x.example", "b.1@x.example"])], cited=["a.1@x.example"])
        score = score_trace(scenario, trace)
        assert score.evidence_recall == 1.0
        assert score.citation_recall == 0.5
        assert "citation_recall" in score.failures

    def test_nothing_cited_has_no_validity_score(self) -> None:
        score = score_trace(_scenario(), _trace([_search(["a.1@x.example"])]))
        assert score.citation_validity is None
        assert score.citation_recall == 0.0


class TestEnumeration:
    def _scenario(self) -> Scenario:
        return _scenario(
            category="exhaustive",
            expected_tools=["query_messages"],
            expected_arguments={"folder": "Sent"},
            max_calls=3,
            required_evidence=[],
            expected_messages=["m1@x.example", "m2@x.example", "m3@x.example"],
        )

    def test_following_the_cursor_to_the_end_is_complete(self) -> None:
        trace = _trace(
            [
                _page(["m1@x.example", "m2@x.example"], has_more=True),
                _page(["m3@x.example"], has_more=False, cursor="next"),
            ]
        )
        score = score_trace(self._scenario(), trace)
        assert score.enumeration_recall == 1.0
        assert score.exhausted is True
        assert score.failures == []

    def test_stopping_after_the_first_page_is_incomplete(self) -> None:
        trace = _trace([_page(["m1@x.example", "m2@x.example"], has_more=True)])
        score = score_trace(self._scenario(), trace)
        assert score.enumeration_recall == pytest.approx(2 / 3)
        assert score.exhausted is False
        assert {"enumeration_recall", "exhausted"} <= set(score.failures)

    def test_a_different_query_does_not_complete_a_partial_one(self) -> None:
        # One partial Sent page, then an unfiltered query that happens to
        # list every expected message: the Sent enumeration never finished.
        trace = _trace(
            [
                _page(["m1@x.example"], has_more=True),
                _page(
                    ["m1@x.example", "m2@x.example", "m3@x.example"],
                    has_more=False,
                    filters={"limit": 100},
                ),
            ]
        )
        score = score_trace(self._scenario(), trace)
        assert score.enumeration_recall == pytest.approx(1 / 3)
        assert score.exhausted is False

    def test_a_page_off_the_cursor_chain_breaks_it(self) -> None:
        # The second page was fetched with a cursor the first page never
        # issued, so its has_more: false does not finish the first page's chain.
        trace = _trace(
            [
                _page(["m1@x.example", "m2@x.example"], has_more=True, next_cursor="c1"),
                _page(["m3@x.example"], has_more=False, cursor="stale"),
            ]
        )
        score = score_trace(self._scenario(), trace)
        assert score.exhausted is False
        assert score.enumeration_recall == pytest.approx(2 / 3)

    def test_a_restart_does_not_orphan_an_earlier_chain(self) -> None:
        # The first chain resumes after a second one started; its cursor
        # still continues it.
        trace = _trace(
            [
                _page(["m1@x.example", "m2@x.example"], has_more=True, next_cursor="c1"),
                _page(
                    ["m1@x.example"],
                    has_more=True,
                    next_cursor="r1",
                    filters={"folder": "Sent", "limit": 1},
                ),
                _page(["m3@x.example"], has_more=False, cursor="c1"),
            ]
        )
        score = score_trace(self._scenario(), trace)
        assert score.enumeration_recall == 1.0
        assert score.exhausted is True

    def test_three_chained_pages_are_complete(self) -> None:
        trace = _trace(
            [
                _page(["m1@x.example"], has_more=True, next_cursor="c1"),
                _page(["m2@x.example"], has_more=True, cursor="c1", next_cursor="c2"),
                _page(["m3@x.example"], has_more=False, cursor="c2"),
            ]
        )
        score = score_trace(self._scenario(), trace)
        assert score.enumeration_recall == 1.0
        assert score.exhausted is True

    def test_page_size_is_free_but_extra_filters_are_not(self) -> None:
        # The expected arguments are the enumeration's predicates: the page
        # size is the agent's choice, an extra filter narrows the set.
        everything = ["m1@x.example", "m2@x.example", "m3@x.example"]
        narrowed = _page(
            everything, has_more=False, filters={"folder": "Sent", "has_attachments": False}
        )
        assert score_trace(self._scenario(), _trace([narrowed])).enumeration_recall == 0.0
        whole = _page(everything, has_more=False, filters={"folder": "Sent", "limit": 100})
        assert score_trace(self._scenario(), _trace([whole])).exhausted is True

    def test_messages_from_other_tools_do_not_count(self) -> None:
        # Only query_messages enumerates with an exact total; a message
        # read through get_thread was not enumerated.
        call = {
            "tool": "get_thread",
            "arguments": {"thread_id": "t.1@x.example"},
            "result": {"messages": [{"message_id": "m1@x.example"}]},
        }
        score = score_trace(self._scenario(), _trace([call]))
        assert score.enumeration_recall == 0.0
        assert score.exhausted is False

    def test_none_for_a_scenario_without_expected_messages(self) -> None:
        score = score_trace(_scenario(), _trace([_search(["a.1@x.example"])]))
        assert score.enumeration_recall is None
        assert score.exhausted is None


class TestCallCounts:
    def test_calls_over_the_budget_are_extra(self) -> None:
        calls = [
            _search(["a.1@x.example"]),
            _search(["a.1@x.example"], limit=5),
            _search([], limit=6),
        ]
        score = score_trace(_scenario(max_calls=2), _trace(calls))
        assert score.extra_calls == 1
        assert "extra_calls" in score.failures

    def test_identical_repeated_calls_are_counted(self) -> None:
        calls = [_search(["a.1@x.example"]), _search(["a.1@x.example"])]
        score = score_trace(_scenario(max_calls=5), _trace(calls))
        assert score.repeated_calls == 1
        assert score.extra_calls == 0
        assert "repeated_calls" in score.failures


class TestScoreTrace:
    def test_trace_for_another_scenario_is_rejected(self) -> None:
        trace = _trace([])
        trace["scenario"] = "other"
        with pytest.raises(ValueError, match="other"):
            score_trace(_scenario(), trace)

    def test_a_clean_trace_has_no_failures(self) -> None:
        trace = _trace([_search(["a.1@x.example"])], cited=["a.1@x.example"])
        assert score_trace(_scenario(), trace).failures == []


class TestSummarize:
    def test_reports_aggregates_and_failures_by_category(self) -> None:
        good = score_trace(
            _scenario(), _trace([_search(["a.1@x.example"])], cited=["a.1@x.example"])
        )
        bad_scenario = _scenario(
            id="s2", category="narrow_filter", expected_arguments={"folders": ["Archive"]}
        )
        bad_trace = _trace([_search(["a.1@x.example"])], cited=["a.1@x.example"])
        bad_trace["scenario"] = "s2"
        bad = score_trace(bad_scenario, bad_trace)

        out = summarize([good, bad])

        assert "Tool selection:      100.00% (2/2)" in out
        assert "Argument accuracy:   0.00% (0/1)" in out
        assert "narrow_filter: s2 (arguments_correct)" in out
        assert "exact_fact" not in out.split("Failures by category:")[1]

    def test_no_failures_says_so(self) -> None:
        good = score_trace(
            _scenario(), _trace([_search(["a.1@x.example"])], cited=["a.1@x.example"])
        )
        assert "Failures by category: none" in summarize([good])
