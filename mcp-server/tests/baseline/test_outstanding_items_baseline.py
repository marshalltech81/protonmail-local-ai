"""Outstanding-items scenarios against the built baseline: layer A (#798).

Is the decisive evidence of each planted matter reachable through the
real read tools, and where is it lost when it is not? The ground truth
(``tests/eval/outstanding_items.json``) lists, per scenario, each
decisive passage and which layers hold it (``corpus_evidence`` lists
passages of corpus shapes outside any scenario the same way, #907):

- ``raw``: a text part of the ``.eml`` the build wrote;
- ``thread_text``: ``threads.body_text``, the thread FTS input (each
  message's parsed body cut at ``PER_MESSAGE_BODY_CAP_CHARS`` = 2,000
  characters, the whole thread at ``THREAD_BODY_TEXT_MAX_TOKENS`` =
  4,000 tokens, ``indexer/src/threader.py``). No tool returns it except
  as a fallback when a thread has no body chunks;
- ``chunks``: the message's ``message_chunks`` (body, or attachment);
- ``get_message``: every body page joined (20,000 characters a page);
- ``get_thread``: the message's body on its page (each body cut at
  4,000 characters, 50 messages a page at most);
- ``get_evidence`` (thread-scoped, the item's query) and
  ``search_attachments`` (the item's query, extraction succeeded): for
  attachment evidence, the only tools that read attachment text.

``test_evidence_layers`` pins the measured matrix. A passage no tool
returns is a measured loss, not a pass: ``test_evidence_is_reachable``
is a strict xfail for it naming the issue, so a fix flips it. The
boundary tests then check the paging, identity, date and extraction
cases the scenarios rest on. Matching ignores whitespace differences.

Skipped unless ``BASELINE_DIR`` is set; ``make baseline`` runs it.
"""

import asyncio
import email
import email.policy
import json
import os
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from src.lib.sqlite import Database
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools

from tests.agent_metrics import load_scenarios
from tests.answer_eval.runner import PrecomputedEmbedder
from tests.conftest import FakeMCPServer

pytestmark = pytest.mark.baseline

_HERE = Path(__file__).parent
_EVAL = _HERE.parent / "eval"
_TRUTH_FILE = json.loads((_EVAL / "outstanding_items.json").read_text(encoding="utf-8"))
TRUTH = _TRUTH_FILE["scenarios"]
SCENARIOS = {
    s.id: s
    for s in load_scenarios(_EVAL / "agent_scenarios.json", _HERE / "golden.json")
    if s.outstanding is not None
}
_DOMAIN = "@baseline.example"
# Plus the corpus shapes outside any scenario (#907: a sentence past the
# build's lowered extracted-characters cap).
CORPUS_EVIDENCE = _TRUTH_FILE["corpus_evidence"]
EVIDENCE = [e for truth in TRUTH.values() for e in truth["evidence"]] + CORPUS_EVIDENCE
TOOL_LAYERS = ("get_message", "get_thread", "get_evidence", "search_attachments")

AVERY = "avery.cole@colereedlaw.example"
BLAIR = "blair.reed@colereedlaw.example"


def _norm(text: str) -> str:
    return " ".join(text.split())


def _mid(ref: str) -> str:
    return f"{ref}{_DOMAIN}"


@pytest.fixture(scope="module")
def baseline_dir() -> Path:
    baseline_dir = os.environ.get("BASELINE_DIR")
    if not baseline_dir:
        pytest.skip("BASELINE_DIR not set; run `make baseline`")
    return Path(baseline_dir)


@pytest.fixture(scope="module")
def baseline_db(baseline_dir: Path) -> Database:
    return Database(str(baseline_dir / "mail.db"))


@pytest.fixture(scope="module")
def tools(baseline_dir: Path, baseline_db: Database) -> dict[str, Any]:
    vectors = json.loads((baseline_dir / "query_vectors.json").read_text(encoding="utf-8"))
    server = FakeMCPServer()
    register_retrieval_tools(server, baseline_db)
    register_search_tools(server, baseline_db, PrecomputedEmbedder(vectors))
    return server.tools


def _call(tools: dict[str, Any], name: str, **arguments: Any) -> dict:
    return asyncio.run(tools[name](**arguments)).structured_content


@pytest.fixture(scope="module")
def raw_texts(baseline_dir: Path) -> dict[str, str]:
    """Every corpus message's text parts (body and attachments), by Message-ID."""
    texts = {}
    for path in (baseline_dir / "maildir").rglob("*.eml"):
        msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
        parts = [p.get_content() for p in msg.walk() if p.get_content_maintype() == "text"]
        texts[str(msg["Message-ID"]).strip("<>")] = _norm(" ".join(parts))
    return texts


def _query_all(tools: dict[str, Any], **filters: Any) -> list[dict]:
    """Every page of a query_messages lookup, following next_cursor."""
    pages = [_call(tools, "query_messages", limit=100, **filters)]
    while pages[-1]["has_more"]:
        pages.append(
            _call(tools, "query_messages", limit=100, cursor=pages[-1]["next_cursor"], **filters)
        )
    return pages


def _listed(pages: list[dict]) -> list[str]:
    return [m["message_id"].removesuffix(_DOMAIN) for p in pages for m in p["messages"]]


def _message_pages(tools: dict[str, Any], message_id: str) -> list[dict]:
    pages = [_call(tools, "get_message", message_id=message_id)]
    while pages[-1]["next_offset"] is not None:
        pages.append(
            _call(tools, "get_message", message_id=message_id, offset=pages[-1]["next_offset"])
        )
    return pages


def _thread_pages(tools: dict[str, Any], thread_id: str) -> list[dict]:
    pages = [_call(tools, "get_thread", thread_id=thread_id, limit=50)]
    while pages[-1]["next_offset"] is not None:
        pages.append(
            _call(
                tools, "get_thread", thread_id=thread_id, limit=50, offset=pages[-1]["next_offset"]
            )
        )
    return pages


def _measure(item: dict, tools: dict, db: Database, raw: dict[str, str]) -> dict[str, bool]:
    """Which layers hold ``item``'s excerpt, for the layers its truth lists."""
    message_id = _mid(item["ref"])
    excerpt = _norm(item["excerpt"])
    attachment = item["source"] == "attachment"
    with closing(db._connect()) as conn:
        thread_id, thread_text = conn.execute(
            "SELECT t.thread_id, t.body_text FROM messages m "
            "JOIN threads t ON t.thread_id = m.thread_id WHERE m.message_id = ?",
            (message_id,),
        ).fetchone()
        chunks = [
            text
            for (text,) in conn.execute(
                "SELECT c.text FROM message_chunks c JOIN messages m "
                "ON m.claimant_id = c.claimant_id WHERE m.message_id = ? "
                "AND (c.attachment_id IS NOT NULL) = ? ORDER BY c.chunk_index",
                (message_id, attachment),
            )
        ]
    body = "".join(p["body"] or "" for p in _message_pages(tools, message_id))
    thread_bodies = [
        m["body"] or ""
        for page in _thread_pages(tools, thread_id)
        for m in page["messages"]
        if m["message_id"] == message_id
    ]
    measured = {
        "raw": excerpt in raw[message_id],
        "thread_text": excerpt in _norm(thread_text or ""),
        "chunks": any(excerpt in _norm(c) for c in chunks),
        "get_message": excerpt in _norm(body),
        "get_thread": any(excerpt in _norm(b) for b in thread_bodies),
    }
    if attachment:
        evidence = _call(tools, "get_evidence", query=item["query"], thread_id=thread_id, limit=60)
        measured["get_evidence"] = any(
            c["message_id"] == message_id
            and c["source"] == "attachment"
            and excerpt in _norm(c["text"])
            for t in evidence["threads"]
            for c in t["chunks"]
        )
        hits = _call(tools, "search_attachments", query=item["query"], limit=50)["results"]
        measured["search_attachments"] = any(
            h["message_id"] == message_id and h["extraction_status"] == "success" for h in hits
        )
    return measured


@pytest.mark.parametrize("item", EVIDENCE, ids=lambda e: f"{e['ref']}-{e['id']}")
def test_evidence_layers(
    item: dict, tools: dict, baseline_db: Database, raw_texts: dict[str, str]
) -> None:
    """The measured layer matrix; a change here is a behaviour change to explain."""
    assert _measure(item, tools, baseline_db, raw_texts) == item["layers"]


def _reachable_params() -> list:
    params = []
    for item in EVIDENCE:
        marks = []
        if loss := item.get("known_loss"):
            marks.append(
                pytest.mark.xfail(
                    strict=True,
                    reason=f"{loss['issue']}: lost at {loss['lost_at']}; no tool returns it",
                )
            )
        params.append(pytest.param(item, marks=marks, id=f"{item['ref']}-{item['id']}"))
    return params


@pytest.mark.parametrize("item", _reachable_params())
def test_evidence_is_reachable(
    item: dict, tools: dict, baseline_db: Database, raw_texts: dict[str, str]
) -> None:
    """Some read tool returns the decisive passage."""
    measured = _measure(item, tools, baseline_db, raw_texts)
    assert any(measured.get(layer) for layer in TOOL_LAYERS), measured


@pytest.mark.parametrize(
    "item", [e for e in EVIDENCE if e.get("known_loss")], ids=lambda e: e["ref"]
)
def test_known_loss_is_where_the_truth_says(
    item: dict, tools: dict, baseline_db: Database, raw_texts: dict[str, str]
) -> None:
    """The first layer, in pipeline order, that no longer holds the passage."""
    measured = _measure(item, tools, baseline_db, raw_texts)
    first_lost = next(layer for layer in ("raw", "chunks", "get_message") if not measured[layer])
    assert first_lost == item["known_loss"]["lost_at"]


def test_truth_refs_are_indexed_messages(baseline_db: Database) -> None:
    with closing(baseline_db._connect()) as conn:
        indexed = {
            m.removesuffix(_DOMAIN) for (m,) in conn.execute("SELECT message_id FROM messages")
        }
    for sid, truth in TRUTH.items():
        refs = [
            r
            for a in truth["actions"]
            for r in a["required_sources"] + a.get("superseded_sources", [])
        ]
        refs += truth["forbidden_sources"] + truth["completeness_blockers"]
        refs += truth["full_read_messages"] + [e["ref"] for e in truth["evidence"]]
        assert set(refs) <= indexed, (sid, sorted(set(refs) - indexed))
    assert {e["ref"] for e in CORPUS_EVIDENCE} <= indexed
    # The loader read the same file.
    assert set(SCENARIOS) == set(TRUTH)


def test_a_counsel_participant_lookup_pages_past_100(tools: dict) -> None:
    """More than 100 messages name Blair Reed; the leasing follow-up is
    only on the second page, so an agent that stops at page 1 misses it."""
    pages = _query_all(tools, participant=BLAIR, date_from="2026-01-01T00:00:00-05:00")
    assert pages[0]["total_matches"] > 100
    assert len(pages) == 2 and pages[0]["has_more"] and not pages[-1]["has_more"]
    assert "t47.2" not in _listed(pages[:1])
    assert "t47.2" in _listed(pages[1:])
    assert len(_listed(pages)) == len(set(_listed(pages))) == pages[0]["total_matches"]


def test_a_thread_of_more_than_50_pages_to_its_late_message(tools: dict) -> None:
    pages = _thread_pages(tools, _mid("t62.1"))
    assert pages[0]["total_messages"] == 56
    first = [m["message_id"] for m in pages[0]["messages"]]
    assert len(first) == 50 and _mid("t62.53") not in first
    assert _mid("t62.53") in [m["message_id"] for m in pages[1]["messages"]]


def test_the_late_message_is_past_the_thread_text_cap(baseline_db: Database) -> None:
    """t62.53 is past the thread's 4,000-token text, so the thread FTS
    lane cannot match it; only its chunks hold it."""
    with closing(baseline_db._connect()) as conn:
        (text,) = conn.execute(
            "SELECT body_text FROM threads WHERE thread_id = ?", (_mid("t62.1"),)
        ).fetchone()
    assert "October 16" not in text
    assert "Hall update 1:" in text


def test_get_thread_cuts_the_long_report_and_get_message_does_not(tools: dict) -> None:
    (page,) = _thread_pages(tools, _mid("t46.1"))
    (message,) = page["messages"]
    assert message["body_omitted_chars"] > 0
    assert "November 30, 2026" not in message["body"]
    (whole,) = _message_pages(tools, _mid("t46.1"))
    assert "November 30, 2026" in whole["body"]


def test_the_next_step_is_on_the_second_message_page(tools: dict) -> None:
    pages = _message_pages(tools, _mid("t53.5"))
    assert len(pages) == 2
    assert "incident log" not in pages[0]["body"]
    assert "incident log" in pages[1]["body"]


def test_quoted_repeats_do_not_match_a_text_lookup(tools: dict) -> None:
    """The collection-policy request is quoted in three later messages;
    query_messages(text=...) reads each message's own text only, so the
    request is listed where it was written, not once per quote."""
    pages = _query_all(tools, text="revised collection policy")
    assert sorted(_listed(pages)) == ["t58.1", "t58.2"]


def test_an_address_lookup_keeps_the_identity_decoy_apart(tools: dict) -> None:
    exact = _listed(_query_all(tools, participant=AVERY))
    assert "t60.1" not in exact and "t46.1" in exact
    # A display-name lookup is a substring match, so it merges the decoy:
    # telling them apart is the caller's job.
    by_name = _listed(_query_all(tools, participant="Avery Cole"))
    assert "t60.1" in by_name and set(exact) < set(by_name)
    contacts = _call(tools, "find_contact", query="Avery Cole")["contacts"]
    assert {c["email"] for c in contacts} == {AVERY, "avery.cole@brightcabinetry.example"}


def test_the_new_year_boundary_is_utc_unless_the_bound_has_an_offset(tools: dict) -> None:
    """t65.1 was sent at 21:30 New York time on December 31, 2025. Its
    sent_at is 02:30 UTC on January 1, 2026; it has no delivery date
    (``occurred_at``), so that is its effective time. A date-only bound
    is a UTC day, so it counts; a bound at New York midnight does not."""
    (page,) = _query_all(tools, sender=AVERY, date_to="2026-01-31")
    (row,) = [m for m in page["messages"] if m["message_id"] == _mid("t65.1")]
    assert row["sent_at"] == "2026-01-01T02:30:00+00:00"
    assert row["occurred_at"] is None
    utc_day = _query_all(tools, sender=AVERY, date_from="2026-01-01")
    assert utc_day[0]["date_bounds"]["date_from"] == "2026-01-01T00:00:00+00:00"
    assert "t65.1" in _listed(utc_day)
    new_york = _query_all(tools, sender=AVERY, date_from="2026-01-01T00:00:00-05:00")
    assert new_york[0]["date_bounds"]["date_from"] == "2026-01-01T05:00:00+00:00"
    assert "t65.1" not in _listed(new_york)


def test_a_failed_extraction_is_listed_but_has_no_text(tools: dict) -> None:
    hits = _call(tools, "search_attachments", query="engagement amendment")["results"]
    (hit,) = [h for h in hits if h["message_id"] == _mid("t64.1")]
    assert hit["extraction_status"] == "failed"
    assert not hit["text_snippet"]
    extracted = _call(
        tools, "search_attachments", query="engagement amendment", extracted_only=True
    )
    assert _mid("t64.1") not in [h["message_id"] for h in extracted["results"]]
    evidence = _call(
        tools, "get_evidence", query="engagement amendment scope", thread_id=_mid("t64.1")
    )
    assert all(c["source"] == "body" for t in evidence["threads"] for c in t["chunks"])


def test_attachment_only_evidence_is_not_in_the_body_tools(tools: dict) -> None:
    """t55.1's body says only "attached": a text lookup for the decisive
    words lists nothing, and get_message has no attachment text."""
    assert _listed(_query_all(tools, text="stipulated dismissal")) == []
    (page,) = _message_pages(tools, _mid("t55.1"))
    assert "June 12" not in page["body"]
