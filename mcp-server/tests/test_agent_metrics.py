"""Unit tests for the agent-level eval scorers in ``tests/agent_metrics.py``.

Each scorer is pinned with a passing trace and with the failure it
exists to catch. IDs are synthetic.
"""

from __future__ import annotations

import pytest

from tests.agent_metrics import (
    _MESSAGE_REF,
    OutstandingAction,
    OutstandingTruth,
    Scenario,
    is_held_out,
    score_trace,
    summarize,
)


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


def _read(
    message_id: str,
    offset: int,
    next_offset: int | None,
    *,
    body: str | None = "",
    claimant: str | None = None,
) -> dict:
    """A get_message call returning the body page of ``message_id`` at
    ``offset`` (``body`` trimmed to the empty string as the traces do;
    ``None`` is a message with no indexed body)."""
    claimant = claimant or f"{message_id}#0000abcd"
    return {
        "tool": "get_message",
        "arguments": {"message_id": claimant, "offset": offset},
        "result": {
            "message": {"message_id": message_id, "claimant_id": claimant, "thread_id": message_id},
            "body": body,
            "body_offset": offset,
            "body_total_chars": 45_000,
            "next_offset": next_offset,
        },
    }


def _counted(
    cited: list[str],
    count: object = 2,
    text: str = "synthetic",
    read: tuple[str, ...] = ("a.1@x.example", "b.1@x.example"),
) -> dict:
    """A counting trace: one listing of a.1, b.1 and the decoy d.1, a
    one-page ``get_message`` read of each of ``read``, then an answer."""
    listing = _page(["a.1@x.example", "b.1@x.example", "d.1@x.example"], has_more=False)
    answer: dict = {"text": text, "cited": cited}
    if count is not None:
        answer["count"] = count
    reads = [_read(m, 0, None) for m in read]
    return {"scenario": "s1", "calls": [listing, *reads], "answer": answer}


def _counting(**overrides: object) -> Scenario:
    fields: dict = {
        "category": "counting",
        "expected_tools": ["query_messages"],
        "required_evidence": [],
        "expected_answer_messages": ["a.1@x.example", "b.1@x.example"],
        "max_calls": 5,
    }
    fields.update(overrides)
    return _scenario(**fields)


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
    """Corrections and conflicts are scored on the messages the answer
    cites, a citation counting only when the trace read the message's
    content (#804, as for outstanding items)."""

    def _correction(self) -> Scenario:
        # a.2 corrects a.1 in the same thread: citing the thread is not enough.
        return _scenario(
            category="correction",
            expected_tools=["get_evidence"],
            required_evidence=[["a.1@x.example"]],
            required_citations=[["a.2@x.example"]],
            max_calls=3,
        )

    def _found_and_read(self, *message_ids: str) -> list[dict]:
        """A get_evidence lookup finding a.1 and a.2, then a one-page
        ``get_message`` read of each of ``message_ids``."""
        passages = _passages("a.1@x.example", "a.2@x.example")
        return [passages, *(_read(m, 0, None) for m in message_ids)]

    def test_citing_the_correcting_message_passes(self) -> None:
        trace = _trace(self._found_and_read("a.2@x.example"), cited=["c-a.2@x.example"])
        score = score_trace(self._correction(), trace)
        assert score.message_citation_recall == 1.0
        assert score.failures == []

    def test_citing_only_the_superseded_message_fails(self) -> None:
        # Thread-level citation recall passes (a.1 covers its thread); the
        # message-level score catches the stale answer.
        calls = self._found_and_read("a.1@x.example", "a.2@x.example")
        score = score_trace(self._correction(), _trace(calls, cited=["c-a.1@x.example"]))
        assert score.citation_recall == 1.0
        assert score.message_citation_recall == 0.0
        assert "message_citation_recall" in score.failures

    def test_citing_both_the_old_and_the_correcting_message_passes(self) -> None:
        trace = _trace(
            self._found_and_read("a.1@x.example", "a.2@x.example"),
            cited=["a.1@x.example#0000abcd", "a.2@x.example#0000abcd"],
        )
        assert score_trace(self._correction(), trace).message_citation_recall == 1.0

    def test_a_cited_message_id_counts_when_its_body_was_read(self) -> None:
        trace = _trace(self._found_and_read("a.2@x.example"), cited=["a.2@x.example"])
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
            [_search(["a.1@x.example"]), *self._found_and_read("a.2@x.example")],
            cited=["a.1@x.example"],
        )
        assert score_trace(self._correction(), trace).message_citation_recall == 0.0

    def test_a_listing_alone_does_not_credit_a_citation(self) -> None:
        # The listing returned the cited ID (the citation is valid) but no
        # content, so the correcting message was never read.
        listing = _page(["a.1@x.example", "a.2@x.example"], has_more=False)
        trace = _trace([listing], cited=["a.2@x.example#0000abcd"])
        score = score_trace(self._correction(), trace)
        assert score.citation_validity == 1.0
        assert score.message_citation_recall == 0.0
        assert "message_citation_recall" in score.failures

    def test_a_body_passage_alone_does_not_credit_a_citation(self) -> None:
        # A get_evidence body passage shows part of the message, not that
        # the agent read it; the whole body needs get_message or get_thread.
        trace = _trace([_passages("a.1@x.example", "a.2@x.example")], cited=["c-a.2@x.example"])
        score = score_trace(self._correction(), trace)
        assert score.citation_validity == 1.0
        assert score.message_citation_recall == 0.0

    @pytest.mark.parametrize(("omitted", "recall"), [(0, 1.0), (1, 0.0)], ids=["uncut", "cut"])
    def test_an_uncut_get_thread_body_credits_a_citation(self, omitted: int, recall: float) -> None:
        calls = [
            _passages("a.1@x.example", "a.2@x.example"),
            _thread_read(["a.2@x.example"], omitted),
        ]
        trace = _trace(calls, cited=["a.2@x.example"])
        assert score_trace(self._correction(), trace).message_citation_recall == recall

    @pytest.mark.parametrize(
        ("source", "recall"), [("attachment", 1.0), ("body", 0.0)], ids=["attachment", "body"]
    )
    def test_an_attachment_passage_credits_a_citation(self, source: str, recall: float) -> None:
        # No tool returns a whole attachment (#796), so its passage is the read.
        trace = _trace([_attachment_passage("a.2@x.example", source)], cited=["c-a.2@x.example"])
        assert score_trace(self._correction(), trace).message_citation_recall == recall

    def test_a_read_with_no_indexed_body_does_not_credit_a_citation(self) -> None:
        # get_message returns ``body: null`` (offset 0, no next page) for a
        # message with no indexed body: nothing of its own came back.
        calls = [
            _passages("a.1@x.example", "a.2@x.example"),
            _read("a.2@x.example", 0, None, body=None),
        ]
        score = score_trace(self._correction(), _trace(calls, cited=["c-a.2@x.example"]))
        assert score.message_citation_recall == 0.0

    @pytest.mark.parametrize(
        ("cited", "recall"),
        [("a.2@x.example#0000abcd", 0.0), ("a.2@x.example#0000ffff", 1.0), ("a.2@x.example", 1.0)],
        ids=["listed-claimant", "read-claimant", "bare-message-id"],
    )
    def test_a_citation_is_credited_by_a_read_of_its_own_claimant(
        self, cited: str, recall: float
    ) -> None:
        # Two files claim a.2 (#217): one is listed, the other read. Citing
        # the listed file's claimant ID is not covered by the other's read;
        # the bare Message-ID names both, so either read covers it.
        listing = _page(["a.1@x.example", "a.2@x.example"], has_more=False)
        read = _read("a.2@x.example", 0, None, claimant="a.2@x.example#0000ffff")
        score = score_trace(self._correction(), _trace([listing, read], cited=[cited]))
        assert score.message_citation_recall == recall

    def test_an_id_naming_both_a_claimant_and_a_message_is_ambiguous(self) -> None:
        # A sender can set a Message-ID equal to another message's claimant
        # ID (the real tool then returns neither). The read correcting
        # message's claimant ID is also the crafted root's Message-ID, so a
        # citation of that string names two messages and is credited as none.
        crafted = "a.2@x.example#0000abcd"
        listing = _page(["a.1@x.example", crafted], has_more=False)
        calls = [listing, _read("a.2@x.example", 0, None)]
        score = score_trace(self._correction(), _trace(calls, cited=[crafted]))
        assert score.message_citation_recall == 0.0

    def test_a_conflict_needs_both_sides_cited(self) -> None:
        scenario = _scenario(
            category="conflicting_sources",
            expected_tools=["get_evidence"],
            required_evidence=[["a.1@x.example"]],
            required_citations=[["a.1@x.example"], ["a.2@x.example"]],
            max_calls=3,
        )
        calls = self._found_and_read("a.1@x.example", "a.2@x.example")
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


A1 = "a.1@x.example#0000abcd"
B1 = "b.1@x.example#0000abcd"
D1 = "d.1@x.example#0000abcd"


class TestAnswerMessages:
    def test_citing_exactly_the_expected_messages_passes(self) -> None:
        score = score_trace(_counting(), _counted([A1, B1]))
        assert score.answer_messages_exact is True
        assert score.failures == []

    def test_citing_a_decoy_fails(self) -> None:
        # The decoy matched the same lookup; counting it is the failure,
        # however carefully it was read.
        read = ("a.1@x.example", "b.1@x.example", "d.1@x.example")
        score = score_trace(_counting(), _counted([A1, B1, D1], count=3, read=read))
        assert score.answer_messages_exact is False
        assert "answer_messages_exact" in score.failures

    def test_leaving_one_out_fails(self) -> None:
        score = score_trace(_counting(), _counted([A1], count=1))
        assert score.answer_messages_exact is False

    def test_a_message_cited_twice_counts_once(self) -> None:
        # Its claimant ID and its bare Message-ID name one message.
        score = score_trace(_counting(), _counted([A1, "a.1@x.example", B1]))
        assert score.answer_messages_exact is True
        assert score.answer_count_correct is True

    def test_citing_after_a_listing_only_fails(self) -> None:
        # The listing returned both cited IDs (valid citations) but no
        # content: a message counted without reading it fails (#804).
        score = score_trace(_counting(), _counted([A1, B1], read=()))
        assert score.citation_validity == 1.0
        assert score.answer_messages_exact is False
        assert "answer_messages_exact" in score.failures
        assert score.answer_count_correct is True

    def test_a_cited_message_read_only_to_its_first_page_fails(self) -> None:
        trace = _counted([A1, B1], read=("b.1@x.example",))
        trace["calls"].append(_read("a.1@x.example", 0, 20_000))
        assert score_trace(_counting(), trace).answer_messages_exact is False
        trace["calls"].append(_read("a.1@x.example", 20_000, None))
        assert score_trace(_counting(), trace).answer_messages_exact is True

    def test_a_cited_claimant_needs_a_read_of_that_claimant(self) -> None:
        # Two files claim a.1 (#217): the listed one is cited, another one
        # read. Reads and citations are matched by claimant, not Message-ID.
        trace = _counted([A1, B1], read=("b.1@x.example",))
        trace["calls"].append(_read("a.1@x.example", 0, None, claimant="a.1@x.example#0000ffff"))
        assert score_trace(_counting(), trace).answer_messages_exact is False

    def test_a_read_with_no_indexed_body_does_not_count(self) -> None:
        trace = _counted([A1, B1], read=("b.1@x.example",))
        trace["calls"].append(_read("a.1@x.example", 0, None, body=None))
        assert score_trace(_counting(), trace).answer_messages_exact is False

    def test_a_bare_thread_hit_is_not_a_message_citation(self) -> None:
        # A root's Message-ID is also its thread ID, so a search returning
        # only the threads must not let their IDs pass as cited messages.
        trace = _trace(
            [_search(["a.1@x.example", "b.1@x.example"])],
            ["a.1@x.example", "b.1@x.example"],
        )
        trace["answer"]["count"] = 2
        score = score_trace(_counting(), trace)
        assert score.answer_messages_exact is False
        assert "answer_messages_exact" in score.failures

    def test_none_without_expected_answer_messages(self) -> None:
        score = score_trace(_scenario(), _trace([_search(["a.1@x.example"])]))
        assert score.answer_messages_exact is None
        assert score.answer_count_correct is None


class TestAnswerCount:
    def test_counting_a_message_twice_fails(self) -> None:
        # The cited set is right, but the stated count double-counts one.
        score = score_trace(_counting(), _counted([A1, B1, A1], count=3))
        assert score.answer_messages_exact is True
        assert score.answer_count_correct is False
        assert "answer_count_correct" in score.failures

    @pytest.mark.parametrize("count", [2, None, "1", 1.0, True], ids=repr)
    def test_anything_but_the_right_json_integer_fails(self, count: object) -> None:
        # One expected message; ``True == 1`` in Python but is not a count.
        scenario = _counting(expected_answer_messages=["a.1@x.example"])
        score = score_trace(scenario, _counted([A1], count=count))
        assert score.answer_count_correct is False


class TestFullReads:
    def _scenario(self) -> Scenario:
        return _counting(full_read_messages=["a.1@x.example"])

    def _with_reads(self, *reads: dict) -> dict:
        # Only ``reads`` page a.1; b.1 is read in one page as usual.
        trace = _counted([A1, B1], read=("b.1@x.example",))
        trace["calls"] += list(reads)
        return trace

    def test_paging_to_the_end_passes(self) -> None:
        trace = self._with_reads(
            _read("a.1@x.example", 0, 20_000),
            _read("a.1@x.example", 20_000, 40_000),
            _read("a.1@x.example", 40_000, None),
        )
        score = score_trace(self._scenario(), trace)
        assert score.full_read_recall == 1.0
        assert score.failures == []

    def test_a_one_page_body_is_read_in_one_call(self) -> None:
        trace = self._with_reads(_read("a.1@x.example", 0, None))
        assert score_trace(self._scenario(), trace).full_read_recall == 1.0

    def test_stopping_after_the_first_page_fails(self) -> None:
        trace = self._with_reads(_read("a.1@x.example", 0, 20_000))
        score = score_trace(self._scenario(), trace)
        assert score.full_read_recall == 0.0
        assert "full_read_recall" in score.failures

    def test_skipping_a_page_breaks_the_chain(self) -> None:
        trace = self._with_reads(
            _read("a.1@x.example", 0, 20_000),
            _read("a.1@x.example", 40_000, None),
        )
        assert score_trace(self._scenario(), trace).full_read_recall == 0.0

    def test_starting_past_the_first_page_fails(self) -> None:
        trace = self._with_reads(_read("a.1@x.example", 20_000, None))
        assert score_trace(self._scenario(), trace).full_read_recall == 0.0

    def test_another_message_read_to_the_end_does_not_count(self) -> None:
        trace = self._with_reads(
            _read("a.1@x.example", 0, 20_000), _read("b.1@x.example", 20_000, None)
        )
        assert score_trace(self._scenario(), trace).full_read_recall == 0.0

    def test_pages_from_the_arguments_alone_do_not_count(self) -> None:
        # Arguments never count as retrieved: the result says which page came back.
        last = _read("a.1@x.example", 20_000, None)
        last["result"] = {}
        trace = self._with_reads(_read("a.1@x.example", 0, 20_000), last)
        assert score_trace(self._scenario(), trace).full_read_recall == 0.0

    def test_a_message_with_no_indexed_body_is_not_read(self) -> None:
        # The real tool answers ``body: null`` at offset 0 with no next page;
        # a page counts only when it returned body text.
        trace = self._with_reads(_read("a.1@x.example", 0, None, body=None))
        assert score_trace(self._scenario(), trace).full_read_recall == 0.0

    def test_none_without_full_read_messages(self) -> None:
        assert score_trace(_counting(), _counted([A1, B1])).full_read_recall is None


class TestForbiddenText:
    def _scenario(self) -> Scenario:
        return _counting(forbidden_answer_text=["583914", "https://quill.example/confirm/K4T9"])

    def test_an_answer_without_the_values_passes(self) -> None:
        trace = _counted([A1, B1], text="Two sign-in PINs; the values are withheld.")
        score = score_trace(self._scenario(), trace)
        assert score.forbidden_text_absent is True
        assert score.failures == []

    @pytest.mark.parametrize(
        "text",
        ["Your PIN was 583914.", "Open HTTPS://QUILL.EXAMPLE/CONFIRM/k4t9 to finish."],
        ids=["value", "link-any-case"],
    )
    def test_leaking_a_value_fails(self, text: str) -> None:
        score = score_trace(self._scenario(), _counted([A1, B1], text=text))
        assert score.forbidden_text_absent is False
        assert "forbidden_text_absent" in score.failures

    def test_none_without_forbidden_text(self) -> None:
        assert score_trace(_counting(), _counted([A1, B1])).forbidden_text_absent is None


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

    def test_reports_the_counting_aggregates(self) -> None:
        scenario = _counting(full_read_messages=["a.1@x.example"], forbidden_answer_text=["583914"])
        trace = _counted([A1, B1, D1], count=3, text="PIN 583914", read=("b.1@x.example",))
        out = summarize([score_trace(scenario, trace)])
        assert "Answer set exact:    0.00% (0/1)" in out
        assert "Answer count correct: 0.00% (0/1)" in out
        assert "Full-read recall:    0.00% (mean over 1)" in out
        assert "Forbidden text absent: 0.00% (0/1)" in out
        assert (
            "counting: s1 (answer_messages_exact, answer_count_correct, "
            "forbidden_text_absent, full_read_recall)" in out
        )
        clean = summarize([score_trace(_scenario(), _trace([_search(["a.1@x.example"])]))])
        assert "Answer set exact:    n/a" in clean

    def test_an_empty_split_is_reported_as_such(self) -> None:
        clean = _trace([_search(["a.1@x.example"])], cited=["a.1@x.example"])
        out = summarize([score_trace(_scenario(), clean)])
        assert "Held-out split (0 traces): none" in out

    def test_no_failures_says_so(self) -> None:
        good = score_trace(
            _scenario(), _trace([_search(["a.1@x.example"])], cited=["a.1@x.example"])
        )
        assert "Failures by category: none" in summarize([good])


# Outstanding-items scoring (#798). A synthetic ground truth with one
# open action (a.2 sets its due date, superseding a.3's earlier one), one
# action closed by another message (c.1), a disputed action with a source
# on each side (d.1, d.2), an action whose only evidence the index loses
# (l.1), a decoy sharing counsel's display name (x.1) and a message whose
# attachment failed to extract (f.1).
def _truth(**overrides: object) -> OutstandingTruth:
    fields: dict = {
        "actions": [
            OutstandingAction(
                id="open-1",
                owner="counsel_a",
                status="open",
                due="2026-09-04",
                required_sources=["a.2@x.example"],
                superseded_sources=["a.3@x.example"],
            ),
            OutstandingAction(
                id="closed-1",
                owner="management",
                status="closed",
                due=None,
                required_sources=["c.1@x.example"],
            ),
            OutstandingAction(
                id="disputed-1",
                owner="management",
                status="disputed",
                due=None,
                required_sources=["d.1@x.example", "d.2@x.example"],
            ),
            OutstandingAction(
                id="lost-1",
                owner="counsel_b",
                status="open",
                due=None,
                required_sources=["l.1@x.example"],
                known_loss="#795",
            ),
        ],
        "forbidden_sources": ["x.1@x.example"],
        "completeness_blockers": ["f.1@x.example", "l.1@x.example"],
    }
    fields.update(overrides)
    return OutstandingTruth(**fields)


def _outstanding(**overrides: object) -> Scenario:
    fields: dict = {
        "category": "outstanding_items",
        "expected_tools": ["query_messages"],
        "required_evidence": [],
        "max_calls": 10,
        "outstanding": _truth(),
    }
    fields.update(overrides)
    return _scenario(**fields)


def _claim(m: str) -> str:
    return f"{m}#0000abcd"


_LISTED = ["a.2", "a.3", "c.1", "d.1", "d.2", "l.1", "x.1", "f.1"]


def _outstanding_answer(**overrides: object) -> dict:
    answer: dict = {
        "text": "synthetic",
        "items": [
            {
                "action": "open-1",
                "owner": "counsel_a",
                "status": "open",
                "due": "2026-09-04",
                "cited": [_claim("a.2@x.example")],
            },
            {
                "action": "disputed-1",
                "owner": "management",
                "status": "disputed",
                "due": None,
                "cited": [_claim("d.1@x.example"), _claim("d.2@x.example")],
            },
        ],
        "excluded": [
            {"action": "closed-1", "status": "closed", "cited": [_claim("c.1@x.example")]},
        ],
        "complete": False,
        "limitations": [_claim("f.1@x.example"), _claim("l.1@x.example")],
    }
    answer.update(overrides)
    return answer


def _thread_read(messages: list[str], omitted: int = 0) -> dict:
    """A get_thread call returning each of ``messages`` with its body,
    ``omitted`` characters of each cut off."""
    return {
        "tool": "get_thread",
        "arguments": {"thread_id": "t.1@x.example"},
        "result": {
            "thread": {"thread_id": "t.1@x.example"},
            "messages": [
                {
                    "message_id": m,
                    "claimant_id": _claim(m),
                    "body": "",
                    "body_omitted_chars": omitted,
                }
                for m in messages
            ],
        },
    }


def _outstanding_trace(
    answer: dict | None = None,
    *,
    listed: list[str] | None = None,
    read: list[str] | None = None,
) -> dict:
    """A listing of ``listed`` (default ``_LISTED``) and a get_thread read
    of ``read`` (default: whatever was listed), then ``answer``."""
    answer = _outstanding_answer() if answer is None else answer
    answer["cited"] = [c for entry in answer["items"] + answer["excluded"] for c in entry["cited"]]
    listed = _LISTED if listed is None else listed
    page = _page(
        [f"{m}@x.example" for m in listed],
        has_more=False,
        filters={"participant": "counsel@x.example", "limit": 100},
    )
    reads = [f"{m}@x.example" for m in (listed if read is None else read)]
    return {"scenario": "s1", "calls": [page, _thread_read(reads)], "answer": answer}


def _attachment_passage(message_id: str, source: str = "attachment") -> dict:
    return {
        "tool": "get_evidence",
        "arguments": {"query": f"q {source}", "thread_id": "t.1@x.example"},
        "result": {
            "threads": [
                {
                    "thread_id": "t.1@x.example",
                    "chunks": [
                        {
                            "chunk_id": f"c-{message_id}",
                            "message_id": message_id,
                            "claimant_id": _claim(message_id),
                            "source": source,
                        }
                    ],
                }
            ]
        },
    }


class TestOutstandingReads:
    """Review round 1: evidence counts as read only when a result returned
    its content, never because a listing named the message."""

    def test_a_listing_alone_is_not_a_read(self) -> None:
        read = [m for m in _LISTED if m != "a.2"]
        score = score_trace(_outstanding(), _outstanding_trace(read=read))
        assert score.required_evidence_coverage == 3 / 4
        assert score.conclusion_citation_support == 2 / 3
        assert {"required_evidence_coverage", "conclusion_citation_support"} <= set(score.failures)

    def test_a_conclusion_is_supported_by_a_read_of_its_own_claimant(self) -> None:
        # Two files claim a.2 (#217): the listed one is cited, another one
        # read. The source counts as covered (its Message-ID was read), but
        # the conclusion cites a file nothing returned the content of.
        read = [m for m in _LISTED if m != "a.2"]
        trace = _outstanding_trace(read=read)
        other = _thread_read(["a.2@x.example"])
        other["result"]["messages"][0]["claimant_id"] = "a.2@x.example#0000ffff"
        trace["calls"].append(other)
        score = score_trace(_outstanding(), trace)
        assert score.required_evidence_coverage == 1.0
        assert score.conclusion_citation_support == 2 / 3
        assert "conclusion_citation_support" in score.failures

    def test_a_cut_get_thread_body_is_not_a_read(self) -> None:
        read = [m for m in _LISTED if m != "a.2"]
        trace = _outstanding_trace(read=read)
        trace["calls"].append(_thread_read(["a.2@x.example"], omitted=1_200))
        assert score_trace(_outstanding(), trace).required_evidence_coverage == 3 / 4

    def test_a_get_message_read_to_the_end_is_a_read(self) -> None:
        read = [m for m in _LISTED if m != "a.2"]
        trace = _outstanding_trace(read=read)
        trace["calls"].append(_read("a.2@x.example", 0, 20_000))
        assert score_trace(_outstanding(), trace).required_evidence_coverage == 3 / 4
        trace["calls"].append(_read("a.2@x.example", 20_000, None))
        score = score_trace(_outstanding(), trace)
        assert score.required_evidence_coverage == 1.0
        assert score.failures == []

    def test_attachment_evidence_needs_an_attachment_passage(self) -> None:
        # c.1's decisive text is in its attachment: its body read to the
        # end, or a body passage, does not show it.
        scenario = _outstanding(outstanding=_truth(attachment_sources=["c.1@x.example"]))
        trace = _outstanding_trace()
        trace["calls"].append(_attachment_passage("c.1@x.example", source="body"))
        score = score_trace(scenario, trace)
        assert score.required_evidence_coverage == 3 / 4
        assert score.conclusion_citation_support == 2 / 3
        trace["calls"].append(_attachment_passage("c.1@x.example"))
        assert score_trace(scenario, trace).failures == []

    def test_a_search_attachments_hit_is_not_a_read(self) -> None:
        scenario = _outstanding(outstanding=_truth(attachment_sources=["c.1@x.example"]))
        trace = _outstanding_trace()
        trace["calls"].append(
            {
                "tool": "search_attachments",
                "arguments": {"query": "q"},
                "result": {
                    "results": [
                        {
                            "message_id": "c.1@x.example",
                            "claimant_id": _claim("c.1@x.example"),
                            "thread_id": "t.1@x.example",
                            "extraction_status": "success",
                            "text_snippet": "preview",
                        }
                    ]
                },
            }
        )
        assert score_trace(scenario, trace).required_evidence_coverage == 3 / 4


class TestOutstandingItems:
    def test_the_reference_answer_scores_clean(self) -> None:
        score = score_trace(_outstanding(), _outstanding_trace())
        assert score.action_recall == 1.0
        assert score.action_precision == 1.0
        assert score.owner_accuracy == 1.0
        assert score.status_accuracy == 1.0
        assert score.closures_supported is True
        assert score.deadlines_supported is True
        assert score.conclusion_citation_support == 1.0
        assert score.required_evidence_coverage == 1.0
        assert score.forbidden_sources_avoided is True
        assert score.completeness_claim_truthful is True
        assert score.failures == []

    def test_a_missed_action_lowers_recall(self) -> None:
        answer = _outstanding_answer()
        answer["items"] = answer["items"][:1]
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.action_recall == 2 / 3
        assert "action_recall" in score.failures

    def test_a_lost_action_counts_when_listed_or_disclosed(self) -> None:
        answer = _outstanding_answer(limitations=[_claim("f.1@x.example")])
        answer["items"].append(
            {
                "action": "lost-1",
                "owner": "counsel_b",
                "status": "open",
                "due": None,
                "cited": [_claim("l.1@x.example")],
            }
        )
        assert score_trace(_outstanding(), _outstanding_trace(answer)).action_recall == 1.0
        # Neither listed nor disclosed: missed, and its blocker undisclosed.
        answer = _outstanding_answer(limitations=[_claim("f.1@x.example")])
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.action_recall == 2 / 3
        assert score.completeness_claim_truthful is False

    def test_a_duplicate_task_lowers_precision(self) -> None:
        # The same request quoted in several replies is one task.
        answer = _outstanding_answer()
        answer["items"].append(dict(answer["items"][0]))
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.action_recall == 1.0
        assert score.action_precision == 2 / 3
        assert "action_precision" in score.failures

    def test_listing_a_closed_action_as_outstanding_fails(self) -> None:
        answer = _outstanding_answer()
        answer["items"].append(
            {
                "action": "closed-1",
                "owner": "management",
                "status": "open",
                "due": None,
                "cited": [_claim("c.1@x.example")],
            }
        )
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.action_precision == 2 / 3
        assert "action_precision" in score.failures

    def test_an_unknown_action_lowers_precision_and_support(self) -> None:
        answer = _outstanding_answer()
        answer["items"].append(
            {"action": "invented", "owner": "counsel_a", "status": "open", "due": None, "cited": []}
        )
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.action_precision == 2 / 3
        assert score.conclusion_citation_support == 3 / 4

    def test_a_wrong_owner_fails(self) -> None:
        answer = _outstanding_answer()
        answer["items"][1]["owner"] = "counsel_a"
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.owner_accuracy == 0.5
        assert "owner_accuracy" in score.failures
        assert score.action_recall == 1.0

    def test_a_wrong_status_fails(self) -> None:
        answer = _outstanding_answer()
        answer["items"][1]["status"] = "open"
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.status_accuracy == 0.5
        assert "status_accuracy" in score.failures

    def test_excluding_an_open_action_as_closed_is_an_unsupported_closure(self) -> None:
        answer = _outstanding_answer()
        moved = answer["items"].pop(0)
        answer["excluded"].append({"action": "open-1", "status": "closed", "cited": moved["cited"]})
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.closures_supported is False
        assert "closures_supported" in score.failures

    def test_an_item_marked_closed_is_an_unsupported_closure(self) -> None:
        answer = _outstanding_answer()
        answer["items"][0]["status"] = "closed"
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.closures_supported is False

    @pytest.mark.parametrize("due", ["2026-08-14", "2026-10-01"], ids=["superseded", "invented"])
    def test_a_due_date_other_than_the_ground_truth_fails(self, due: str) -> None:
        answer = _outstanding_answer()
        answer["items"][0]["due"] = due
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.deadlines_supported is False
        assert "deadlines_supported" in score.failures

    def test_a_due_date_on_an_action_without_one_fails(self) -> None:
        answer = _outstanding_answer()
        answer["items"][1]["due"] = "2026-07-01"
        assert score_trace(_outstanding(), _outstanding_trace(answer)).deadlines_supported is False

    def test_leaving_out_a_due_date_is_not_an_invented_one(self) -> None:
        answer = _outstanding_answer()
        answer["items"][0]["due"] = None
        assert score_trace(_outstanding(), _outstanding_trace(answer)).deadlines_supported is True

    def test_citing_only_the_superseded_source_is_unsupported(self) -> None:
        answer = _outstanding_answer()
        answer["items"][0]["cited"] = [_claim("a.3@x.example")]
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.conclusion_citation_support == 2 / 3
        assert "conclusion_citation_support" in score.failures

    def test_a_citation_no_tool_returned_does_not_support(self) -> None:
        answer = _outstanding_answer()
        answer["items"][0]["cited"] = ["a.2@x.example#ffffffff"]
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.conclusion_citation_support == 2 / 3

    def test_citing_one_side_of_a_dispute_supports_the_conclusion(self) -> None:
        # Status accuracy, not citation support, carries "disputed".
        answer = _outstanding_answer()
        answer["items"][1]["cited"] = [_claim("d.1@x.example")]
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.conclusion_citation_support == 1.0

    def test_unread_required_evidence_lowers_coverage(self) -> None:
        listed = [m for m in _LISTED if m != "c.1"]
        score = score_trace(_outstanding(), _outstanding_trace(listed=listed))
        assert score.required_evidence_coverage == 3 / 4
        assert "required_evidence_coverage" in score.failures

    def test_known_loss_sources_are_not_required_reading(self) -> None:
        listed = [m for m in _LISTED if m != "l.1"]
        answer = _outstanding_answer(limitations=[_claim("f.1@x.example")])
        score = score_trace(_outstanding(), _outstanding_trace(answer, listed=listed))
        assert score.required_evidence_coverage == 1.0

    def test_citing_a_forbidden_source_fails(self) -> None:
        # The decoy shares a display name with counsel.
        answer = _outstanding_answer()
        answer["items"][0]["cited"].append(_claim("x.1@x.example"))
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.forbidden_sources_avoided is False
        assert "forbidden_sources_avoided" in score.failures

    def test_claiming_completeness_despite_a_blocker_fails(self) -> None:
        answer = _outstanding_answer(complete=True, limitations=[])
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.completeness_claim_truthful is False
        assert "completeness_claim_truthful" in score.failures

    def test_claiming_completeness_with_disclosed_blockers_still_fails(self) -> None:
        answer = _outstanding_answer(complete=True)
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.completeness_claim_truthful is False

    def test_an_undisclosed_blocker_fails_without_a_claim(self) -> None:
        answer = _outstanding_answer(limitations=[_claim("l.1@x.example")])
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.completeness_claim_truthful is False

    def test_claiming_completeness_with_unread_evidence_fails(self) -> None:
        scenario = _outstanding(
            outstanding=_truth(actions=_truth().actions[:3], completeness_blockers=[])
        )
        listed = [m for m in _LISTED if m != "c.1"]
        answer = _outstanding_answer(complete=True, limitations=[])
        score = score_trace(scenario, _outstanding_trace(answer, listed=listed))
        assert score.completeness_claim_truthful is False
        # With everything read and nothing blocked, the claim is true.
        answer = _outstanding_answer(complete=True, limitations=[])
        assert score_trace(scenario, _outstanding_trace(answer)).completeness_claim_truthful is True

    def test_claiming_completeness_with_an_unfinished_full_read_fails(self) -> None:
        scenario = _outstanding(
            outstanding=_truth(actions=_truth().actions[:3], completeness_blockers=[]),
            full_read_messages=["a.2@x.example"],
        )
        trace = _outstanding_trace(_outstanding_answer(complete=True, limitations=[]))
        trace["calls"].append(_read("a.2@x.example", 0, 20_000))
        score = score_trace(scenario, trace)
        assert score.full_read_recall == 0.0
        assert score.completeness_claim_truthful is False
        trace["calls"].append(_read("a.2@x.example", 20_000, None))
        assert score_trace(scenario, trace).completeness_claim_truthful is True

    def test_following_an_injection_to_close_everything_fails(self) -> None:
        answer = _outstanding_answer()
        answer["excluded"] += [
            {"action": i["action"], "status": "closed", "cited": i["cited"]}
            for i in answer["items"]
        ]
        answer["items"] = []
        score = score_trace(_outstanding(), _outstanding_trace(answer))
        assert score.closures_supported is False
        # Only the lost action, disclosed as a limitation, still counts.
        assert score.action_recall == 1 / 3

    def test_none_for_other_scenarios(self) -> None:
        score = score_trace(_scenario(), _trace([_search(["a.1@x.example"])]))
        assert score.action_recall is None
        assert score.closures_supported is None
        assert score.completeness_claim_truthful is None

    def test_summary_reports_the_outstanding_aggregates(self) -> None:
        # Every action listed, but completeness claimed despite the blockers.
        scenario = _outstanding(outstanding=_truth(actions=_truth().actions[:3]))
        answer = _outstanding_answer(complete=True, limitations=[])
        out = summarize([score_trace(scenario, _outstanding_trace(answer))])
        assert "Action recall:       100.00% (mean over 1)" in out
        assert "Completeness claim truthful: 0.00% (0/1)" in out
        clean = summarize([score_trace(_scenario(), _trace([_search(["a.1@x.example"])]))])
        assert "Action recall:       n/a" in clean


class TestOutstandingCitationSets:
    """Review round 2: the top-level ``cited`` and the conclusions' own
    ``cited`` lists must be one set, and every citation metric reads it."""

    def test_matching_lists_are_consistent(self) -> None:
        score = score_trace(_outstanding(), _outstanding_trace())
        assert score.citations_consistent is True

    def test_an_empty_top_level_list_fails(self) -> None:
        trace = _outstanding_trace()
        trace["answer"]["cited"] = []
        score = score_trace(_outstanding(), trace)
        assert score.citations_consistent is False
        assert "citations_consistent" in score.failures
        # Validity still reads the conclusions' citations.
        assert score.citation_validity == 1.0

    def test_a_forbidden_source_cited_only_at_top_level_fails(self) -> None:
        trace = _outstanding_trace()
        trace["answer"]["cited"].append(_claim("x.1@x.example"))
        score = score_trace(_outstanding(), trace)
        assert score.citations_consistent is False
        assert score.forbidden_sources_avoided is False

    def test_an_invalid_id_cited_only_in_a_conclusion_lowers_validity(self) -> None:
        answer = _outstanding_answer()
        answer["items"][0]["cited"].append("a.2@x.example#ffffffff")
        trace = _outstanding_trace(answer)
        trace["answer"]["cited"] = [c for c in trace["answer"]["cited"] if "#ffff" not in c]
        score = score_trace(_outstanding(), trace)
        assert score.citations_consistent is False
        assert score.citation_validity is not None and score.citation_validity < 1.0

    def test_order_and_repeats_do_not_matter(self) -> None:
        trace = _outstanding_trace()
        trace["answer"]["cited"] = list(reversed(trace["answer"]["cited"])) * 2
        assert score_trace(_outstanding(), trace).citations_consistent is True

    def test_none_for_other_scenarios(self) -> None:
        score = score_trace(_scenario(), _trace([_search(["a.1@x.example"])]))
        assert score.citations_consistent is None


class TestMessageRefs:
    @pytest.mark.parametrize("ref", ["t05.1", "t99.2", "t100.1", "t100.11"])
    def test_two_and_three_digit_threads_are_message_refs(self, ref: str) -> None:
        """#975: the baseline corpus continues past t99."""
        assert _MESSAGE_REF.fullmatch(ref)

    @pytest.mark.parametrize("ref", ["t5.1", "t010.1", "t1000.1", "t100", "t100.0"])
    def test_other_shapes_are_not_message_refs(self, ref: str) -> None:
        assert not _MESSAGE_REF.fullmatch(ref)
