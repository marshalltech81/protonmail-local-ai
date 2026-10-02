"""Unit tests for the agent-level eval scorers in ``tests/agent_metrics.py``.

Each scorer is pinned with a passing trace and with the failure it
exists to catch. IDs are synthetic.
"""

from __future__ import annotations

import pytest

from tests.agent_metrics import Scenario, is_held_out, score_trace, summarize


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


def _trace(calls: list[dict], cited: list[str] | None = None, *, abstained: object = None) -> dict:
    answer: dict = {"text": "synthetic", "cited": cited or []}
    if abstained is not None:
        answer["abstained"] = abstained
    return {"scenario": "s1", "calls": calls, "answer": answer}


def _passages(*message_ids: str) -> dict:
    """A get_evidence call returning one passage per message of thread a.1."""
    return {
        "tool": "get_evidence",
        "arguments": {"query": "q"},
        "result": {
            "threads": [
                {
                    "thread_id": "a.1@x.example",
                    "chunks": [
                        {
                            "chunk_id": f"c-{m}",
                            "message_id": m,
                            "claimant_id": f"{m}#0000abcd",
                        }
                        for m in message_ids
                    ],
                }
            ]
        },
    }


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


class TestMessageCitations:
    """Corrections and conflicts are scored on the messages the answer cites."""

    def _correction(self) -> Scenario:
        # a.2 corrects a.1 in the same thread: citing the thread is not enough.
        return _scenario(
            category="correction",
            expected_tools=["get_evidence"],
            required_evidence=[["a.1@x.example"]],
            required_citations=[["a.2@x.example"]],
        )

    def test_citing_the_correcting_message_passes(self) -> None:
        trace = _trace([_passages("a.1@x.example", "a.2@x.example")], cited=["c-a.2@x.example"])
        score = score_trace(self._correction(), trace)
        assert score.message_citation_recall == 1.0
        assert score.failures == []

    def test_citing_only_the_superseded_message_fails(self) -> None:
        # Thread-level citation recall passes (a.1 covers its thread); the
        # message-level score catches the stale answer.
        trace = _trace([_passages("a.1@x.example", "a.2@x.example")], cited=["c-a.1@x.example"])
        score = score_trace(self._correction(), trace)
        assert score.citation_recall == 1.0
        assert score.message_citation_recall == 0.0
        assert "message_citation_recall" in score.failures

    def test_citing_both_the_old_and_the_correcting_message_passes(self) -> None:
        trace = _trace(
            [_passages("a.1@x.example", "a.2@x.example")],
            cited=["a.1@x.example#0000abcd", "a.2@x.example#0000abcd"],
        )
        assert score_trace(self._correction(), trace).message_citation_recall == 1.0

    def test_a_cited_message_id_counts_when_a_result_returned_it(self) -> None:
        trace = _trace([_passages("a.2@x.example")], cited=["a.2@x.example"])
        assert score_trace(self._correction(), trace).message_citation_recall == 1.0

    def test_a_cited_id_no_tool_returned_does_not_count(self) -> None:
        # The agent names the correcting message without having retrieved it.
        trace = _trace([_passages("a.1@x.example")], cited=["a.2@x.example"])
        score = score_trace(self._correction(), trace)
        assert score.message_citation_recall == 0.0
        assert score.citation_validity == 0.0

    def test_a_cited_thread_id_does_not_cover_its_later_messages(self) -> None:
        # The thread ID is its root's Message-ID, so it names the root
        # message only, never the reply that corrects it.
        trace = _trace(
            [_search(["a.1@x.example"]), _passages("a.1@x.example", "a.2@x.example")],
            cited=["a.1@x.example"],
        )
        assert score_trace(self._correction(), trace).message_citation_recall == 0.0

    def test_a_conflict_needs_both_sides_cited(self) -> None:
        scenario = _scenario(
            category="conflicting_sources",
            expected_tools=["get_evidence"],
            required_evidence=[["a.1@x.example"]],
            required_citations=[["a.1@x.example"], ["a.2@x.example"]],
        )
        calls = [_passages("a.1@x.example", "a.2@x.example")]
        one_side = score_trace(scenario, _trace(calls, cited=["c-a.2@x.example"]))
        assert one_side.message_citation_recall == 0.5
        assert "message_citation_recall" in one_side.failures
        both = score_trace(scenario, _trace(calls, cited=["c-a.1@x.example", "c-a.2@x.example"]))
        assert both.failures == []

    def test_none_without_required_citations(self) -> None:
        trace = _trace([_search(["a.1@x.example"])], cited=["a.1@x.example"])
        assert score_trace(_scenario(), trace).message_citation_recall is None


class TestAbstention:
    def _unanswerable(self) -> Scenario:
        return _scenario(
            category="unanswerable",
            required_evidence=[],
            unanswerable=True,
            abstain_terms=["wifi", "wireless"],
        )

    def _looked(self, query: str = "cabin WiFi network") -> dict:
        return _search(["a.1@x.example"], query=query)

    def test_abstaining_after_looking_without_citations_passes(self) -> None:
        trace = _trace([self._looked()], abstained=True)
        score = score_trace(self._unanswerable(), trace)
        assert score.abstention_correct is True
        assert score.failures == []

    def test_any_listed_term_in_any_string_argument_counts_as_looking(self) -> None:
        call = {
            "tool": "ask_mailbox",
            "arguments": {"question": "Is there Wireless at the cabin?"},
            "result": {"citations": []},
        }
        trace = _trace([_search([], query="cabin"), call], abstained=True)
        assert score_trace(self._unanswerable(), trace).abstention_correct is True

    def test_abstaining_after_an_unrelated_lookup_fails(self) -> None:
        # Searching for something else and refusing is not an evidence-based
        # abstention, even with an allowed tool.
        trace = _trace([self._looked(query="roof")], abstained=True)
        score = score_trace(self._unanswerable(), trace)
        assert score.tool_selected is True
        assert score.abstention_correct is False
        assert "abstention_correct" in score.failures

    def test_a_term_only_in_a_result_does_not_count_as_looking(self) -> None:
        call = {
            "tool": "search_emails",
            "arguments": {"query": "roof"},
            "result": {"results": [{"thread_id": "a.1@x.example", "snippet": "wifi"}]},
        }
        trace = _trace([call], abstained=True)
        assert score_trace(self._unanswerable(), trace).abstention_correct is False

    def test_answering_an_unanswerable_question_fails(self) -> None:
        score = score_trace(self._unanswerable(), _trace([self._looked()]))
        assert score.abstention_correct is False
        assert "abstention_correct" in score.failures

    def test_abstaining_while_citing_unrelated_evidence_fails(self) -> None:
        # The search returned a near-miss thread; citing it presents it as
        # support for an answer the mailbox does not hold.
        trace = _trace([self._looked()], cited=["a.1@x.example"], abstained=True)
        score = score_trace(self._unanswerable(), trace)
        assert score.abstention_correct is False
        assert "abstention_correct" in score.failures

    def test_only_a_boolean_true_marks_abstention(self) -> None:
        for flag in ("true", 1, "yes"):
            trace = _trace([self._looked()], abstained=flag)
            assert score_trace(self._unanswerable(), trace).abstention_correct is False, flag

    def test_abstaining_on_an_answerable_question_fails(self) -> None:
        trace = _trace([_search(["a.1@x.example"])], cited=[], abstained=True)
        score = score_trace(_scenario(), trace)
        assert score.abstention_correct is False
        assert "abstention_correct" in score.failures

    def test_an_answer_without_the_flag_did_not_abstain(self) -> None:
        trace = _trace([_search(["a.1@x.example"])], cited=["a.1@x.example"])
        assert score_trace(_scenario(), trace).abstention_correct is True

    def test_abstaining_without_looking_fails(self) -> None:
        score = score_trace(self._unanswerable(), _trace([], abstained=True))
        assert score.abstention_correct is False
        assert set(score.failures) == {"tool_selected", "abstention_correct"}


class TestHeldOut:
    def test_membership_is_a_fixed_function_of_the_id(self) -> None:
        # Pinned values: changing the rule moves scenarios between splits.
        assert is_held_out("archived-pool-bids") is True
        assert is_held_out("roof-estimate-total") is False
        assert is_held_out("archived-pool-bids") is is_held_out("archived-pool-bids")

    def test_scores_carry_the_split(self) -> None:
        trace = _trace([_search(["a.1@x.example"])], cited=["a.1@x.example"])
        assert score_trace(_scenario(held_out=True), trace).held_out is True
        assert score_trace(_scenario(), trace).held_out is False


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

    @pytest.mark.parametrize(
        "second",
        [
            {"folder": "Sent", "subject": "   ", "limit": 2},
            {"folder": "Sent", "authority_class": "\t", "limit": 2},
            {"folder": "  Sent ", "limit": 2},
            {"folder": "Sent", "date_from": "", "limit": 2},
        ],
        ids=["whitespace-subject", "whitespace-authority", "padded-folder", "empty-date"],
    )
    def test_filters_the_tool_normalizes_away_continue_the_chain(self, second: dict) -> None:
        # query_messages strips its string filters and ignores a blank one,
        # so these pages are the same query and its cursor continues (#502).
        trace = _trace(
            [
                _page(["m1@x.example", "m2@x.example"], has_more=True),
                _page(["m3@x.example"], has_more=False, cursor="next", filters=second),
            ]
        )
        score = score_trace(self._scenario(), trace)
        assert score.enumeration_recall == 1.0
        assert score.exhausted is True

    def test_equivalent_date_forms_continue_the_chain(self) -> None:
        # The tool binds its cursor to the normalized UTC bounds, so a
        # date-only bound and its midnight-UTC datetime are one query.
        scenario = _scenario(
            category="exhaustive",
            expected_tools=["query_messages"],
            expected_arguments={"folder": "Sent", "date_from": "2024-01-01"},
            max_calls=3,
            required_evidence=[],
            expected_messages=["m1@x.example", "m2@x.example"],
        )
        trace = _trace(
            [
                _page(
                    ["m1@x.example"],
                    has_more=True,
                    filters={"folder": "Sent", "date_from": "2024-01-01T00:00:00Z"},
                ),
                _page(
                    ["m2@x.example"],
                    has_more=False,
                    cursor="next",
                    filters={"folder": "Sent", "date_from": "2024-01-01"},
                ),
            ]
        )
        score = score_trace(scenario, trace)
        assert score.enumeration_recall == 1.0
        assert score.exhausted is True

    def test_a_call_with_a_date_the_tool_rejects_joins_no_chain(self) -> None:
        # The tool refuses an unparseable date, so the call returned no page.
        trace = _trace(
            [
                _page(
                    ["m1@x.example", "m2@x.example", "m3@x.example"],
                    has_more=False,
                    filters={"folder": "Sent", "date_from": "not a date"},
                ),
            ]
        )
        score = score_trace(self._scenario(), trace)
        assert score.enumeration_recall == 0.0
        assert score.exhausted is False

    def test_a_non_blank_extra_filter_still_starts_its_own_chain(self) -> None:
        # Only blank values are absent: a real subject is another query, so
        # its has_more: false does not finish the unfiltered Sent chain.
        trace = _trace(
            [
                _page(["m1@x.example", "m2@x.example"], has_more=True),
                _page(
                    ["m3@x.example"],
                    has_more=False,
                    cursor="next",
                    filters={"folder": "Sent", "subject": " report ", "limit": 2},
                ),
            ]
        )
        score = score_trace(self._scenario(), trace)
        assert score.enumeration_recall == pytest.approx(2 / 3)
        assert score.exhausted is False

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

    def test_reports_every_aggregate_per_split(self) -> None:
        # Tuning reads the dev block; held-out outcomes must not leak into it.
        clean = _trace([_search(["a.1@x.example"])], cited=["a.1@x.example"])
        dev = score_trace(_scenario(), clean)
        held = score_trace(_scenario(held_out=True), _trace([]))
        out = summarize([dev, held])
        dev_block = out.split("Dev split (1 traces):")[1].split("Held-out split")[0]
        held_block = out.split("Held-out split (1 traces):")[1].split("Failures by category")[0]
        assert "Tool selection:      100.00% (1/1)" in dev_block
        assert "Citation recall:     100.00% (mean over 1)" in dev_block
        assert "Clean:               100.00% (1/1)" in dev_block
        assert "Tool selection:      0.00% (0/1)" in held_block
        assert "Citation recall:     0.00% (mean over 1)" in held_block
        assert "Clean:               0.00% (0/1)" in held_block
        assert "exact_fact: s1 [held-out] (" in out
        assert "Message citation recall: n/a" in dev_block

    def test_an_empty_split_is_reported_as_such(self) -> None:
        clean = _trace([_search(["a.1@x.example"])], cited=["a.1@x.example"])
        out = summarize([score_trace(_scenario(), clean)])
        assert "Held-out split (0 traces): none" in out

    def test_no_failures_says_so(self) -> None:
        good = score_trace(
            _scenario(), _trace([_search(["a.1@x.example"])], cited=["a.1@x.example"])
        )
        assert "Failures by category: none" in summarize([good])
