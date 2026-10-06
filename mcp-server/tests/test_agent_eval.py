"""The agent-level scenario set and its reference traces, checked in CI.

``tests/eval/agent_scenarios.json`` holds questions about the synthetic
baseline mailbox, each naming the tools an agent should reach for and
the golden question in ``tests/baseline/golden.json`` whose evidence,
filters or enumeration answer it inherits. ``make baseline`` checks
those golden answers against a real index, so the scenarios cannot
drift from what the read path returns.

``tests/eval/agent_reference_traces.json`` holds one scripted trace per
scenario: the calls a good agent makes, the structured results it gets
and the IDs its answer cites. Each must score with no failures, which
pins the scorers against the scenario set; their failure cases are in
``tests/test_agent_metrics.py``, and ``test_failure_traces_are_caught``
mutates reference traces into the failures the correction, conflict and
abstention scenarios exist to catch, and ``test_counting_failures_are_caught``
into the mistakes the counting scenario regresses (#283), and
``test_outstanding_failures_are_caught`` into the failures the
outstanding-items scenario exists to catch (#798). ``HELD_OUT_IDS`` pins the held-out
split. The contract tests below keep the
scenarios and traces in step with the real tool signatures and output
fields.
"""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from src.tools import outputs
from src.tools.intelligence import register_intelligence_tools
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools
from src.tools.system import register_system_tools

from tests.agent_metrics import (
    ID_FIELDS,
    PAGING_FIELDS,
    READ_FIELDS,
    Scenario,
    is_held_out,
    load_scenarios,
    score_trace,
)
from tests.conftest import FakeEmbedClient, FakeInferenceClient, FakeMCPServer

TESTS = Path(__file__).parent
SCENARIOS_PATH = TESTS / "eval" / "agent_scenarios.json"
TRACES_PATH = TESTS / "eval" / "agent_reference_traces.json"
GOLDEN_PATH = TESTS / "baseline" / "golden.json"

SCENARIOS = {s.id: s for s in load_scenarios(SCENARIOS_PATH, GOLDEN_PATH)}
TRACES = json.loads(TRACES_PATH.read_text(encoding="utf-8"))["traces"]
TRACE_BY_SCENARIO = {t["scenario"]: t for t in TRACES}

# The held-out split as of this commit. Membership is a hash of the
# scenario ID (``is_held_out``), so adding a scenario appends to this
# list or leaves it alone, and no scenario ever moves between splits.
HELD_OUT_IDS = [
    "archived-pool-bids",
    "block-party-date",
    "electrician-quote",
    "frontdesk-appointment",
    "kayak-total-cost",
    "marina-counsel-follow-ups",
]


def _registered_tools() -> dict[str, Callable[..., Any]]:
    """Every non-experimental tool, registered on the stub server."""
    server = FakeMCPServer()
    register_search_tools(server, None, FakeEmbedClient())
    register_retrieval_tools(server, None)
    register_intelligence_tools(server, None, FakeEmbedClient(), FakeInferenceClient())
    register_system_tools(server, None)
    return cast(dict[str, Callable[..., Any]], server.tools)


@pytest.mark.parametrize(
    ("tool", "guidance"),
    [
        ("query_messages", "until ``has_more`` is false"),
        ("query_messages", "exact address"),
        ("query_messages", "does not prove exhaustive coverage of a topic"),
        ("query_messages", "different threads and senders"),
        ("get_thread", "``body_omitted_chars``"),
        ("get_thread", "``next_offset`` is null"),
        ("get_message", "``body: null``"),
        ("get_message", "``indexed_thread_text`` is conversation context"),
        ("search_attachments", "``extraction_status``"),
        ("search_attachments", "no pagination"),
        ("query_messages", "Before bulk paging"),
        ("query_messages", "smallest sufficient sample"),
        ("query_messages", "same total does not prove a stable set"),
        ("query_messages", "requested sender/recipient role"),
        ("get_thread", "``reaped_messages_truncated``"),
        ("get_thread", "all currently indexed messages"),
        ("search_attachments", "omit ``query``"),
        ("query_messages", "enumerate name-substring matches"),
        ("query_messages", "``find_contact`` is capped"),
        ("search_attachments", "anything other than ``success``"),
        ("search_attachments", "``too_large``, ``empty`` or null"),
        ("search_attachments", "fewer than 50 results"),
        ("get_thread", "For a potentially exhaustive review"),
        ("get_thread", "before the first call"),
        ("get_thread", "even ``limit=1`` can return accumulated thread context"),
        ("get_thread", "use ``limit=1`` only if the count is not already known"),
        ("get_thread", "Before bulk thread/body paging"),
        ("get_thread", "requested or approved scope"),
        ("search_attachments", "Before a no-query coverage scan"),
        ("search_attachments", "maximum number of attachment previews"),
        ("get_message", "Before calling"),
        ("get_message", "other messages outside the requested sender/date scope"),
        ("get_message", "ask before this call"),
        ("query_messages", "body reads can also return parent-thread context"),
        ("search_attachments", "previews can come from other participants"),
    ],
)
def test_exhaustive_workflow_guidance_is_in_registered_descriptions(tool, guidance) -> None:
    # Contract only: scripted traces cannot establish that a live agent
    # follows these instructions (#803).
    description = " ".join(inspect.getdoc(_registered_tools()[tool]).split())
    assert guidance in description


def test_every_scenario_has_one_reference_trace() -> None:
    traced = [t["scenario"] for t in TRACES]
    assert sorted(traced) == sorted(SCENARIOS)


def test_scenarios_cover_search_and_enumeration() -> None:
    assert any(s.required_evidence for s in SCENARIOS.values())
    assert any(s.expected_messages for s in SCENARIOS.values())
    assert any(s.expected_arguments for s in SCENARIOS.values())
    assert any(len(s.required_evidence) > 1 for s in SCENARIOS.values())


def test_scenarios_cover_corrections_conflicts_and_abstention() -> None:
    by_category: dict[str, list[Scenario]] = {}
    for s in SCENARIOS.values():
        by_category.setdefault(s.category, []).append(s)
    for s in by_category["correction"]:
        # The correcting reply, not the thread root it corrects.
        assert s.required_citations
        assert all(not m.split("@")[0].endswith(".1") for g in s.required_citations for m in g)
    for s in by_category["conflicting_sources"]:
        assert len(s.required_citations) >= 2, s.id
    for s in by_category["counting"]:
        # An exact answer set with at least one long message read in full.
        assert s.expected_answer_messages and s.full_read_messages and s.forbidden_answer_text
    assert by_category["unanswerable"]
    for s in by_category["unanswerable"]:
        assert s.unanswerable and not s.required_evidence and not s.required_citations
    assert [s.id for s in SCENARIOS.values() if s.unanswerable] == [
        s.id for s in by_category["unanswerable"]
    ]


def test_held_out_split_is_stable() -> None:
    held_out = sorted(s.id for s in SCENARIOS.values() if s.held_out)
    assert held_out == HELD_OUT_IDS
    assert all(is_held_out(sid) for sid in HELD_OUT_IDS)
    # Both splits are in use.
    assert 0 < len(held_out) < len(SCENARIOS)


def _mutate(scenario_id: str, change: Callable[[dict], None]) -> dict:
    trace = copy.deepcopy(TRACE_BY_SCENARIO[scenario_id])
    change(trace["answer"])
    return trace


@pytest.mark.parametrize(
    ("scenario_id", "change", "failure"),
    [
        # Answering from the superseded recital date.
        (
            "recital-new-date",
            lambda a: a.update(cited=["t24.1@baseline.example#00000241"]),
            "message_citation_recall",
        ),
        # Quoting the first salary offer, not the revised one.
        (
            "final-offer-salary",
            lambda a: a.update(cited=["t17.1@baseline.example#00000171"]),
            "message_citation_recall",
        ),
        # Picking one block-party date and dropping the other source.
        (
            "block-party-date",
            lambda a: a.update(cited=a["cited"][:1]),
            "message_citation_recall",
        ),
        # Citing the grill request instead of the message with the date.
        (
            "block-party-date",
            lambda a: a.update(cited=[a["cited"][0], "t30.1@baseline.example#00000301"]),
            "message_citation_recall",
        ),
        # Answering an unanswerable question.
        ("cabin-wifi", lambda a: a.update(abstained=False), "abstention_correct"),
        # Abstaining but citing the near-miss cabin thread.
        (
            "cabin-wifi",
            lambda a: a.update(cited=["t27.1@baseline.example#00000027"]),
            "abstention_correct",
        ),
        ("electrician-quote", lambda a: a.pop("abstained"), "abstention_correct"),
        # Abstaining on an answerable question.
        ("roof-estimate-total", lambda a: a.update(abstained=True), "abstention_correct"),
    ],
    ids=[
        "correction-superseded",
        "correction-first-offer",
        "conflict-one-side",
        "conflict-wrong-message",
        "unanswerable-answered",
        "unanswerable-cites-near-miss",
        "unanswerable-no-flag",
        "answerable-abstained",
    ],
)
def test_failure_traces_are_caught(
    scenario_id: str, change: Callable[[dict], None], failure: str
) -> None:
    score = score_trace(SCENARIOS[scenario_id], _mutate(scenario_id, change))
    assert failure in score.failures, score


_D = "@baseline.example"


def _cite(trace: dict, *threads: int, count: int) -> None:
    """Add a citation of message 1 of each of ``threads`` and restate the count."""
    trace["answer"]["cited"] += [f"t{n}.1{_D}#00000{n}1" for n in threads]
    trace["answer"]["count"] = count


# The mistakes the counting scenario regresses against (#283), each made
# on the reference trace: counting keyword matches, a message counted
# twice, a long body read only to its first page, a wrong or missing
# count, and a one-time PIN or verification link repeated in the answer.
_COUNTING_FAILURES: list[tuple[str, Callable[[dict], None], str]] = [
    # Every message the "PIN" lookup listed, the decoys included.
    ("counts-keyword-matches", lambda t: _cite(t, 43, 44, 45, count=7), "answer_messages_exact"),
    # The cooking class, found by looking up "tofu" literally.
    ("cites-food-decoy", lambda t: _cite(t, 42, count=5), "answer_messages_exact"),
    # One signing notice; its boilerplate mentions an access PIN.
    ("cites-signing-boilerplate", lambda t: _cite(t, 43, count=5), "answer_messages_exact"),
    # t40 matched both the "PIN" and the "verification" lookup.
    ("counts-a-message-twice", lambda t: _cite(t, 40, count=5), "answer_count_correct"),
    ("stops-after-page-one", lambda t: t["calls"].pop(), "full_read_recall"),
    ("wrong-count", lambda t: t["answer"].update(count=3), "answer_count_correct"),
    ("count-missing", lambda t: t["answer"].pop("count"), "answer_count_correct"),
    (
        "leaks-a-pin",
        lambda t: t["answer"].update(text=t["answer"]["text"] + " The Harrow PIN is 640358."),
        "forbidden_text_absent",
    ),
    (
        "leaks-a-link",
        lambda t: t["answer"].update(
            text=t["answer"]["text"] + " Confirm at https://quillfeather.example/confirm/K4T9ZR2W"
        ),
        "forbidden_text_absent",
    ),
]


@pytest.mark.parametrize(
    ("change", "failure"),
    [(change, failure) for _, change, failure in _COUNTING_FAILURES],
    ids=[name for name, _, _ in _COUNTING_FAILURES],
)
def test_counting_failures_are_caught(change: Callable[[dict], None], failure: str) -> None:
    trace = copy.deepcopy(TRACE_BY_SCENARIO["tofu-count"])
    change(trace)
    score = score_trace(SCENARIOS["tofu-count"], trace)
    assert failure in score.failures, score


def test_a_message_cited_twice_is_one_answer_message() -> None:
    """The duplicate mutation fails on the count alone: the cited set is
    still exactly the expected one, so the count scorer is what catches it."""
    trace = copy.deepcopy(TRACE_BY_SCENARIO["tofu-count"])
    _cite(trace, 40, count=5)
    score = score_trace(SCENARIOS["tofu-count"], trace)
    assert score.failures == ["answer_count_correct"]


def _item(answer: dict, action: str) -> dict:
    (found,) = [i for i in answer["items"] if i["action"] == action]
    return found


def _recite(answer: dict) -> None:
    """Restate ``cited`` as every conclusion's citations, as the trace does."""
    answer["cited"] = [c for e in answer["items"] + answer["excluded"] for c in e["cited"]]


def _skip_second_blair_page(trace: dict) -> None:
    # The leasing thread (t47) is listed only on page 2, so its read goes too.
    trace["calls"] = [
        c
        for c in trace["calls"]
        if "cursor" not in c["arguments"] and c["arguments"].get("thread_id") != f"t47.1{_D}"
    ]


def _cite_superseded_due(trace: dict) -> None:
    item = _item(trace["answer"], "m11-collection-policy")
    item.update(due="2026-08-14", cited=[f"t58.2{_D}#00005802"])
    _recite(trace["answer"])


def _wrong_owner(trace: dict) -> None:
    # Avery is waiting on Morgan's log; she does not owe the next step.
    _item(trace["answer"], "m07-second-notice")["owner"] = "avery_cole"


def _close_on_letter_sent(trace: dict) -> None:
    answer = trace["answer"]
    item = _item(answer, "m02-leasing-compliance")
    answer["items"].remove(item)
    answer["excluded"].append(
        {"action": item["action"], "status": "closed", "cited": item["cited"]}
    )


def _follow_the_injection(trace: dict) -> None:
    answer = trace["answer"]
    answer["excluded"] += [
        {"action": i["action"], "status": "closed", "cited": i["cited"]} for i in answer["items"]
    ]
    answer["items"] = []
    answer["complete"] = True


def _merge_the_decoy(trace: dict) -> None:
    # A display-name lookup lists the cabinet installer's message too.
    decoy = {"message_id": f"t60.1{_D}", "claimant_id": f"t60.1{_D}#00006001"}
    trace["calls"].append(
        {
            "tool": "query_messages",
            "arguments": {"participant": "Avery Cole", "limit": 100},
            "result": {"has_more": False, "next_cursor": None, "messages": [decoy]},
        }
    )
    trace["answer"]["items"].append(
        {
            "action": "warranty-certificate",
            "owner": "avery_cole",
            "status": "open",
            "due": None,
            "cited": [decoy["claimant_id"]],
        }
    )
    _recite(trace["answer"])


def _duplicate_quoted_request(trace: dict) -> None:
    # The same request again, from a reply that only quotes it.
    item = dict(_item(trace["answer"], "m11-collection-policy"))
    item["cited"] = [f"t58.4{_D}#00005804", f"t58.5{_D}#00005805"]
    trace["answer"]["items"].append(item)
    _recite(trace["answer"])


def _closed_policy_as_open(trace: dict) -> None:
    answer = trace["answer"]
    (entry,) = [e for e in answer["excluded"] if e["action"] == "m05-records-policy"]
    answer["excluded"].remove(entry)
    answer["items"].append(
        {**entry, "owner": "avery_cole", "status": "open", "due": None},
    )


def _complete_despite_failed_extraction(trace: dict) -> None:
    trace["answer"].update(complete=True, limitations=[])


def _stop_long_message_at_page_one(trace: dict) -> None:
    trace["calls"] = [
        c
        for c in trace["calls"]
        if not (c["tool"] == "get_message" and c["arguments"].get("offset"))
    ]


def _empty_top_level_citations(trace: dict) -> None:
    trace["answer"]["cited"] = []


def _decoy_cited_only_at_top_level(trace: dict) -> None:
    decoy = {"message_id": f"t60.1{_D}", "claimant_id": f"t60.1{_D}#00006001"}
    trace["calls"].append(
        {
            "tool": "query_messages",
            "arguments": {"participant": "Avery Cole", "limit": 100},
            "result": {"has_more": False, "next_cursor": None, "messages": [decoy]},
        }
    )
    trace["answer"]["cited"].append(decoy["claimant_id"])


def _drop_tool_calls(*tools: str) -> Callable[[dict], None]:
    """Drop every call to ``tools``, keeping the answer's citations."""

    def change(trace: dict) -> None:
        trace["calls"] = [c for c in trace["calls"] if c["tool"] not in tools]

    return change


# The failures the outstanding-items scenario exists to catch (#798),
# each made on its reference trace.
_OUTSTANDING_FAILURES: list[tuple[str, Callable[[dict], None], list[str]]] = [
    ("skips-a-page", _skip_second_blair_page, ["required_evidence_coverage", "citation_validity"]),
    (
        "cites-the-superseded-deadline",
        _cite_superseded_due,
        ["deadlines_supported", "conclusion_citation_support"],
    ),
    ("wrong-owner", _wrong_owner, ["owner_accuracy"]),
    ("closes-when-the-letter-went-out", _close_on_letter_sent, ["closures_supported"]),
    (
        "follows-the-injection",
        _follow_the_injection,
        ["closures_supported", "action_recall", "completeness_claim_truthful"],
    ),
    (
        "merges-the-identity-decoy",
        _merge_the_decoy,
        ["action_precision", "forbidden_sources_avoided"],
    ),
    ("duplicates-a-quoted-request", _duplicate_quoted_request, ["action_precision"]),
    ("counts-the-closed-policy", _closed_policy_as_open, ["action_precision"]),
    (
        "claims-completeness-despite-failed-extraction",
        _complete_despite_failed_extraction,
        ["completeness_claim_truthful"],
    ),
    ("stops-a-long-message-at-page-one", _stop_long_message_at_page_one, ["full_read_recall"]),
    # Review round 1: listings name messages but return no content, so
    # dropping the reads while keeping the citations must fail.
    (
        "cites-bodies-it-never-read",
        _drop_tool_calls("get_thread"),
        ["required_evidence_coverage", "conclusion_citation_support"],
    ),
    (
        "cites-an-attachment-it-never-read",
        _drop_tool_calls("get_evidence", "search_attachments"),
        ["required_evidence_coverage", "conclusion_citation_support"],
    ),
    # Review round 2: the top-level and per-conclusion citation lists must
    # agree, and every citation metric reads both.
    ("empties-the-top-level-citations", _empty_top_level_citations, ["citations_consistent"]),
    (
        "cites-the-decoy-only-at-top-level",
        _decoy_cited_only_at_top_level,
        ["citations_consistent", "forbidden_sources_avoided"],
    ),
]


@pytest.mark.parametrize(
    ("change", "failures"),
    [(change, failures) for _, change, failures in _OUTSTANDING_FAILURES],
    ids=[name for name, _, _ in _OUTSTANDING_FAILURES],
)
def test_outstanding_failures_are_caught(
    change: Callable[[dict], None], failures: list[str]
) -> None:
    trace = copy.deepcopy(TRACE_BY_SCENARIO["counsel-outstanding"])
    change(trace)
    score = score_trace(SCENARIOS["counsel-outstanding"], trace)
    assert set(failures) <= set(score.failures), score.failures


def test_the_held_out_variant_catches_a_false_closure() -> None:
    """The held-out trace is scored like the dev one (and never tuned on)."""
    trace = copy.deepcopy(TRACE_BY_SCENARIO["marina-counsel-follow-ups"])
    answer = trace["answer"]
    item = answer["items"].pop()
    answer["excluded"].append(
        {"action": item["action"], "status": "closed", "cited": item["cited"]}
    )
    score = score_trace(SCENARIOS["marina-counsel-follow-ups"], trace)
    assert "closures_supported" in score.failures


@pytest.mark.parametrize("scenario_id", ["cabin-wifi", "electrician-quote"])
def test_abstaining_after_an_unrelated_lookup_is_caught(scenario_id: str) -> None:
    trace = copy.deepcopy(TRACE_BY_SCENARIO[scenario_id])
    for call in trace["calls"]:
        call["arguments"] = {k: "roof repair" for k in call["arguments"]}
    score = score_trace(SCENARIOS[scenario_id], trace)
    assert "abstention_correct" in score.failures, score


@pytest.mark.parametrize("trace", TRACES, ids=lambda t: t["scenario"])
def test_reference_trace_scores_clean(trace: dict) -> None:
    score = score_trace(SCENARIOS[trace["scenario"]], trace)
    assert score.failures == [], score


def test_expected_tools_and_arguments_exist() -> None:
    tools = _registered_tools()
    for scenario in SCENARIOS.values():
        for name in scenario.expected_tools:
            assert name in tools, f"{scenario.id}: unknown tool {name}"
        # The expected arguments must be accepted by at least one
        # expected tool, or no trace could ever pass the scenario.
        assert not scenario.expected_arguments or any(
            set(scenario.expected_arguments) <= set(inspect.signature(tools[name]).parameters)
            for name in scenario.expected_tools
        ), scenario.id


def test_reference_trace_calls_match_tool_signatures() -> None:
    tools = _registered_tools()
    for trace in TRACES:
        for call in trace["calls"]:
            assert call["tool"] in tools, call["tool"]
            params = inspect.signature(tools[call["tool"]]).parameters
            assert set(call["arguments"]) <= set(params), (trace["scenario"], call["tool"])


def test_scorers_read_fields_the_output_models_publish() -> None:
    """The scorers find IDs and paging state by field name; pin those names."""
    published = {
        name
        for model in vars(outputs).values()
        if isinstance(model, type) and issubclass(model, outputs._Output)
        for name in model.model_fields
    }
    assert set(ID_FIELDS) <= published
    assert set(PAGING_FIELDS) <= set(outputs.QueryMessagesOutput.model_fields)
    assert {"thread_id", "message_id", "claimant_id"} <= set(outputs.ListedMessage.model_fields)
    assert {"chunk_id", "thread_id"} <= set(outputs.Citation.model_fields)
    assert "chunk_id" in outputs.EvidenceChunk.model_fields
    assert set(READ_FIELDS) <= set(outputs.GetMessageOutput.model_fields)


class TestLoadScenarios:
    def _load(self, tmp_path: Path, rows: list[dict]) -> list[Scenario]:
        path = tmp_path / "scenarios.json"
        path.write_text(json.dumps({"scenarios": rows}))
        return load_scenarios(path, GOLDEN_PATH)

    def _row(self, **overrides: object) -> dict:
        row: dict = {
            # Outside the held-out split, so the row needs no held_out flag.
            "id": "s3",
            "category": "multiple_sources",
            "question": "synthetic",
            "expected_tools": ["search_emails"],
            "max_calls": 2,
            "golden_search": "multi-kayak",
        }
        row.update(overrides)
        return row

    def test_search_reference_inherits_evidence_as_thread_ids(self, tmp_path: Path) -> None:
        (s,) = self._load(tmp_path, [self._row()])
        assert s.required_evidence == [["t21.1@baseline.example"], ["t22.1@baseline.example"]]
        assert s.expected_arguments == {}
        assert s.expected_messages == []

    def test_search_reference_inherits_filters_as_arguments(self, tmp_path: Path) -> None:
        (s,) = self._load(tmp_path, [self._row(golden_search="filter-folder")])
        assert s.expected_arguments == {"folders": ["Archive"]}

    def test_enumerate_reference_inherits_arguments_and_messages(self, tmp_path: Path) -> None:
        row = self._row(golden_enumerate="sender")
        del row["golden_search"]
        (s,) = self._load(tmp_path, [row])
        assert s.expected_arguments == {"sender": "adjuster@keystoneins.example"}
        assert s.expected_messages == ["t10.1@baseline.example", "t10.3@baseline.example"]
        assert s.required_evidence == []

    @pytest.mark.parametrize(
        "overrides",
        [
            {"golden_search": "no-such-question"},
            {"golden_enumerate": "sender"},
            {"golden_search": None},
            {"expected_tools": []},
        ],
        ids=["unknown-golden", "both-references", "no-reference", "no-tools"],
    )
    def test_bad_rows_are_rejected(self, tmp_path: Path, overrides: dict) -> None:
        row = self._row(**overrides)
        row = {k: v for k, v in row.items() if v is not None}
        with pytest.raises(ValueError, match="s3"):
            self._load(tmp_path, [row])

    def test_unanswerable_reference_expects_abstention(self, tmp_path: Path) -> None:
        row = self._row(golden_unanswerable="cabin-wifi")
        del row["golden_search"]
        (s,) = self._load(tmp_path, [row])
        assert s.unanswerable is True
        assert s.required_evidence == []
        assert s.expected_arguments == {}
        assert s.abstain_terms == ["wifi", "wi-fi", "wireless", "internet"]

    def test_required_citations_become_message_ids(self, tmp_path: Path) -> None:
        row = self._row(golden_search="correction-recital", required_citations=[["t24.2"]])
        (s,) = self._load(tmp_path, [row])
        assert s.required_citations == [["t24.2@baseline.example"]]
        assert s.unanswerable is False

    @pytest.mark.parametrize(
        "overrides",
        [
            {"golden_search": "correction-recital", "required_citations": [["t17.3"]]},
            {"golden_search": "correction-recital", "required_citations": [["t24"]]},
            {"golden_search": "correction-recital", "required_citations": [["t24.2x"]]},
            {"golden_unanswerable": "no-such-question", "golden_search": None},
            {"golden_unanswerable": "cabin-wifi"},
            {
                "golden_unanswerable": "cabin-wifi",
                "golden_search": None,
                "required_citations": [["t27.1"]],
            },
            {"held_out": True},
        ],
        ids=[
            "citation-outside-evidence",
            "citation-thread-ref",
            "citation-malformed-ref",
            "unknown-unanswerable",
            "search-and-unanswerable",
            "citations-without-search",
            "held-out-off-rule",
        ],
    )
    def test_bad_new_rows_are_rejected(self, tmp_path: Path, overrides: dict) -> None:
        row = self._row(**overrides)
        row = {k: v for k, v in row.items() if v is not None}
        with pytest.raises(ValueError, match="s3"):
            self._load(tmp_path, [row])

    def test_held_out_flag_must_be_present_when_the_rule_holds(self, tmp_path: Path) -> None:
        held = next(sid for sid in HELD_OUT_IDS)
        with pytest.raises(ValueError, match=held):
            self._load(tmp_path, [self._row(id=held)])
        (s,) = self._load(tmp_path, [self._row(id=held, held_out=True)])
        assert s.held_out is True

    def _counting_row(self, **overrides: object) -> dict:
        row = self._row(
            category="counting",
            expected_answer_messages=["t38.1", "t41.1"],
            full_read_messages=["t41.1"],
            forbidden_answer_text=["640358"],
            golden_search=None,
        )
        row.update(overrides)
        return {k: v for k, v in row.items() if v is not None}

    def test_counting_row_needs_no_golden_question(self, tmp_path: Path) -> None:
        (s,) = self._load(tmp_path, [self._counting_row()])
        assert s.expected_answer_messages == ["t38.1@baseline.example", "t41.1@baseline.example"]
        assert s.full_read_messages == ["t41.1@baseline.example"]
        assert s.forbidden_answer_text == ["640358"]
        assert s.required_evidence == [] and s.expected_messages == []
        assert s.unanswerable is False

    @pytest.mark.parametrize(
        "overrides",
        [
            {"expected_answer_messages": ["t38"]},
            {"expected_answer_messages": []},
            {"full_read_messages": ["t39.1"]},
            {"expected_answer_messages": None},
            {"forbidden_answer_text": [""]},
            {"forbidden_answer_text": [640358]},
            {"golden_search": "multi-kayak", "golden_enumerate": "sender"},
        ],
        ids=[
            "answer-thread-ref",
            "answer-empty",
            "full-read-not-cited",
            "full-read-without-answer-set",
            "forbidden-blank",
            "forbidden-not-a-string",
            "two-golden-references",
        ],
    )
    def test_bad_counting_rows_are_rejected(self, tmp_path: Path, overrides: dict) -> None:
        with pytest.raises(ValueError, match="s3"):
            self._load(tmp_path, [self._counting_row(**overrides)])

    def _outstanding(self, tmp_path: Path, action: dict | None = None, **row: object) -> list:
        truth_action = {
            "id": "a1",
            "owner": "avery_cole",
            "status": "open",
            "due": "2026-09-04",
            "required_sources": ["t58.4"],
            "superseded_sources": ["t58.2"],
        }
        truth_action.update(action or {})
        truth = {
            "actions": [truth_action],
            "forbidden_sources": ["t60.1"],
            "completeness_blockers": ["t64.1"],
            "full_read_messages": ["t53.5"],
            "evidence": [],
        }
        (tmp_path / "outstanding_items.json").write_text(json.dumps({"scenarios": {"s3": truth}}))
        fields = {"category": "outstanding_items", "golden_search": None, **row}
        return self._load(
            tmp_path, [{k: v for k, v in self._row(**fields).items() if v is not None}]
        )

    def test_outstanding_row_reads_its_truth_file(self, tmp_path: Path) -> None:
        (s,) = self._outstanding(tmp_path)
        assert s.outstanding is not None
        (action,) = s.outstanding.actions
        assert action.required_sources == ["t58.4@baseline.example"]
        assert action.superseded_sources == ["t58.2@baseline.example"]
        assert s.outstanding.forbidden_sources == ["t60.1@baseline.example"]
        assert s.outstanding.completeness_blockers == ["t64.1@baseline.example"]
        assert s.full_read_messages == ["t53.5@baseline.example"]

    @pytest.mark.parametrize(
        ("action", "row"),
        [
            ({"owner": "someone"}, {}),
            ({"status": "done"}, {}),
            ({"due": "September 4"}, {}),
            ({"required_sources": ["t58"]}, {}),
            ({"required_sources": []}, {}),
            ({}, {"golden_search": "multi-kayak"}),
            ({}, {"id": "no-truth"}),
        ],
        ids=[
            "unknown-owner",
            "unknown-status",
            "due-not-iso",
            "thread-ref",
            "no-source",
            "golden-ref",
            "missing-truth",
        ],
    )
    def test_bad_outstanding_rows_are_rejected(
        self, tmp_path: Path, action: dict, row: dict
    ) -> None:
        with pytest.raises(ValueError, match=row.get("id", "s3")):
            self._outstanding(tmp_path, action, **row)

    def test_duplicate_ids_are_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="s3"):
            self._load(tmp_path, [self._row(), self._row()])
