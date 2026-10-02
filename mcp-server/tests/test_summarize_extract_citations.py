"""
The citation contract of ask_mailbox (#284) applied to summarize_thread
and extract_from_emails.

summarize_thread labels its thread text (E1) and its recent passages
(E2 ...) and checks the summary as ask_mailbox checks an answer: labels,
statement coverage and quotes, with one bounded repair.
extract_from_emails labels each thread's passages (numbered across the
call), asks for an ``_evidence`` map per record, checks every field's
labels against the passages of its own thread and looks for string
values in the cited passages; it makes no repair call. All data is
synthetic.
"""

import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlite_vec
import src.tools.intelligence as intelligence
from fastmcp import Client, FastMCP
from src.lib.inference import InferenceTruncatedError
from src.lib.sqlite import ChunkResult, Database, ThreadResult
from src.tools.intelligence import (
    _MAX_CHECKED_QUOTES,
    EXTRACT_SYSTEM,
    SUMMARIZE_SYSTEM,
    EvidenceRef,
    _check_records,
    _summarize_context,
    register_intelligence_tools,
)
from src.tools.outputs import ExtractFromEmailsOutput, SummarizeThreadOutput

from tests.conftest import (
    FakeEmbedClient,
    FakeInferenceClient,
    FakeMCPServer,
    _build_schema,
    _insert_message,
    claimant_of,
)

_MARKER = "SYNTHETIC_CONTRACT_MARKER_5512"


def _db(tmp_path: Path, name: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / name))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    return conn


@pytest.fixture
def plan_db(tmp_path: Path):
    """One thread: an indexed body plus three messages from different
    senders on different days, each one body chunk at index 0."""
    conn = _db(tmp_path, "plan.db")
    for mid, sent, sender, body in (
        (
            "m1@example.com",
            "2024-03-01T09:00:00+00:00",
            "Alice Example <alice@example.com>",
            f"The budget is 500 units for the spring launch. {_MARKER}",
        ),
        (
            "m2@example.com",
            "2024-03-04T10:30:00+00:00",
            "bob@example.com",
            "Correction: the budget is 700 units after the review.",
        ),
        (
            "m3@example.com",
            "2024-03-06T16:00:00+00:00",
            "carol@example.com",
            "Please send the signed form by Friday.",
        ),
    ):
        _insert_message(
            conn,
            message_id=mid,
            thread_id="t-plan",
            subject="spring launch plan",
            sent_at=sent,
            from_=[sender],
            body=body,
        )
    conn.execute(
        "UPDATE threads SET body_text = ?, snippet = ?, date_last = ? WHERE thread_id = 't-plan'",
        (
            "Kickoff notes: the team met to plan the spring launch.",
            "Kickoff notes",
            "2024-03-06T16:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()
    return Database(str(tmp_path / "plan.db"))


def _summarize(db, inference, **kwargs):
    server = FakeMCPServer()
    register_intelligence_tools(server, db, FakeEmbedClient(), inference)
    return asyncio.run(server.tools["summarize_thread"](thread_id="t-plan", **kwargs))


def _headers(user_prompt: str) -> dict[str, str]:
    """Evidence label -> its rendered header line."""
    return {m.group(1): m.group(0) for m in re.finditer(r"^\[(E\d+) \|[^\n]*$", user_prompt, re.M)}


def _outside_blocks(user_prompt: str) -> str:
    return re.sub(r"<untrusted_email[^>]*>.*?</untrusted_email>", "", user_prompt, flags=re.S)


# The labels of plan_db's summarize prompt: E1 thread text, then the
# three messages oldest first.
_GOOD_SUMMARY = (
    "The team planned the spring launch [E1]. "
    'Alice set the budget: "The budget is 500 units" [E2]. '
    "Bob corrected the budget to 700 units [E3]."
)


class TestSummarizePrompt:
    def test_each_passage_is_labelled_with_its_own_message(self, plan_db):
        llm = FakeInferenceClient(_GOOD_SUMMARY)
        _summarize(plan_db, llm)
        [(system, user)] = llm.complete_calls
        assert system == SUMMARIZE_SYSTEM
        headers = _headers(user)
        assert sorted(headers) == ["E1", "E2", "E3", "E4"]
        assert headers["E1"] == "[E1 | thread text]"
        # Every recent passage names its own message, sender and sent
        # date; all three are chunk 0, so the index alone cannot.
        for label, mid, sender, sent in (
            ("E2", "m1@example.com", "Alice Example <alice@example.com>", "2024-03-01T09:00"),
            ("E3", "m2@example.com", "bob@example.com", "2024-03-04T10:30"),
            ("E4", "m3@example.com", "carol@example.com", "2024-03-06T16:00"),
        ):
            assert f"message {claimant_of(mid)}" in headers[label]
            assert f"from {sender}" in headers[label]
            assert f"sent {sent}" in headers[label]
            assert "chunk 0 chars" in headers[label]
        assert "[E1" not in _outside_blocks(user)

    def test_labelled_context_never_exceeds_its_budget(self):
        thread = ThreadResult(
            thread_id="t",
            subject="s",
            participants=[],
            folder="INBOX",
            date_first=datetime(2024, 1, 1, tzinfo=UTC),
            date_last=datetime(2024, 1, 1, tzinfo=UTC),
            message_ids=[],
            snippet="",
            has_attachments=False,
            body_text="b" * 20_000,
        )
        recent = [
            ChunkResult(
                chunk_id=f"c{k}",
                message_id=f"m{k}@example.com",
                claimant_id=f"m{k}@example.com#0000000{k}",
                thread_id="t",
                chunk_index=0,
                text="r" * 3000,
                char_start=0,
                char_end=3000,
                message_sender="x" * 2000,
                message_date="2024-01-01T00:00:00",
            )
            for k in range(4)
        ]
        for budget in (0, 10, 40, 200, 1000, 6000, 12_000, 50_000):
            evidence: dict[str, EvidenceRef] = {}
            context = _summarize_context(thread, recent, budget, evidence_map=evidence)
            assert len(context) <= budget, budget
            # Every label in the context is in the map, and the reverse.
            assert set(re.findall(r"^\[(E\d+) \|", context, re.M)) == set(evidence), budget


class TestSummarizeCheck:
    def test_valid_summary_resolves_its_citations_in_one_call(self, plan_db):
        llm = FakeInferenceClient(_GOOD_SUMMARY)
        out = _summarize(plan_db, llm, style="detailed")
        assert len(llm.complete_calls) == 1
        data = out.structured_content
        SummarizeThreadOutput.model_validate(data)
        assert data["citation_problems"] == []
        assert data["repair_attempted"] is False
        assert data["style"] == "detailed"
        assert data["thread"]["thread_id"] == "t-plan"
        by_label = {c["label"]: c for c in data["citations"]}
        assert list(by_label) == ["E1", "E2", "E3"]
        assert by_label["E1"]["source"] == "thread" and by_label["E1"]["chunk_id"] is None
        assert by_label["E2"]["claimant_id"] == claimant_of("m1@example.com")
        assert by_label["E2"]["sender"] == "Alice Example <alice@example.com>"
        assert by_label["E3"]["sent_at"].startswith("2024-03-04")
        assert [q["status"] for q in data["quotes"]] == ["verified"]
        text = out.content[0].text
        assert text.startswith("Summary (detailed) — spring launch plan:")
        assert "Citations:" in text

    @pytest.mark.parametrize(
        ("first", "kind"),
        [
            ("The launch moved to May [E9].", "unknown_labels"),
            ("The team planned the spring launch with a budget.", "no_citations"),
            (
                "The team planned the spring launch [E1]. Bob raised the budget afterwards.",
                "uncited_statements",
            ),
            ('Bob wrote "the budget is 900 units" [E3].', "unmatched_quotes"),
            ('Alice wrote "the budget is 700 units" [E2].', "misattributed_quotes"),
        ],
    )
    def test_a_failed_check_gets_exactly_one_repair(self, plan_db, first, kind):
        llm = FakeInferenceClient(complete_responses=[first, _GOOD_SUMMARY])
        out = _summarize(plan_db, llm)
        assert len(llm.complete_calls) == 2
        first_prompt, repair_prompt = (user for _system, user in llm.complete_calls)
        assert repair_prompt.startswith(first_prompt)
        assert "Citation check: your previous answer" in repair_prompt
        # The rejected summary is never replayed to the model.
        assert first not in repair_prompt
        data = out.structured_content
        assert data["repair_attempted"] is True
        assert data["citation_problems"] == []

    def test_problems_that_survive_the_repair_are_reported(self, plan_db):
        llm = FakeInferenceClient(complete_responses=["Moved to May [E9]."] * 5)
        out = _summarize(plan_db, llm)
        assert len(llm.complete_calls) == 2  # bounded: one repair, never more
        data = out.structured_content
        assert [p["kind"] for p in data["citation_problems"]] == ["unknown_labels"]
        assert data["citation_problems"][0]["labels"] == ["E9"]
        assert "cites labels that name no supplied passage: E9" in out.content[0].text

    def test_a_cut_off_summary_is_not_repaired(self, plan_db):
        llm = FakeInferenceClient(
            complete_responses=[InferenceTruncatedError(partial="The team planned the launch")]
        )
        out = _summarize(plan_db, llm)
        assert len(llm.complete_calls) == 1
        assert out.structured_content["repair_attempted"] is False
        assert "[Answer cut off" in out.structured_content["summary"]

    def test_a_not_found_summary_needs_no_citation(self, plan_db):
        llm = FakeInferenceClient(
            "Not found in the provided emails: the thread names no action items."
        )
        out = _summarize(plan_db, llm, style="action-items")
        assert len(llm.complete_calls) == 1
        assert out.structured_content["citation_problems"] == []

    def test_each_action_item_is_a_statement(self, plan_db):
        """Every style is checked the same way: the statement splitter
        cuts at line breaks, so each bullet must cite or be marked."""
        items = (
            "Action items:\n"
            "- Carol needs the signed form by Friday [E4]\n"
            "- Bob will confirm the 700 unit budget\n"
            "- Someone should book the venue [unsupported]"
        )
        llm = FakeInferenceClient(complete_responses=[items, items])
        out = _summarize(plan_db, llm, style="action-items")
        statements = out.structured_content["statements"]
        assert [s["status"] for s in statements] == [
            "not_checked",
            "cited",
            "uncited",
            "unsupported",
        ]
        [problem] = out.structured_content["citation_problems"]
        assert problem["kind"] == "uncited_statements" and problem["statements"] == [2]
        assert len(llm.complete_calls) == 2

    def test_an_unknown_style_is_summarized_as_brief(self, plan_db):
        llm = FakeInferenceClient(_GOOD_SUMMARY)
        out = _summarize(plan_db, llm, style="haiku")
        assert out.structured_content["style"] == "brief"
        assert llm.complete_calls[0][1].endswith("Task: Summarize in 2-3 sentences.")

    def test_mail_and_model_text_stay_out_of_the_logs(self, plan_db, caplog):
        caplog.set_level(logging.DEBUG)
        bad = f'{_MARKER} "{_MARKER} invented words here" [E2]. Uncited {_MARKER} statement here.'
        llm = FakeInferenceClient(complete_responses=[bad, bad])
        out = _summarize(plan_db, llm)
        assert _MARKER not in caplog.text
        assert "summarize_thread citations:" in caplog.text
        # The prose report is fixed text and counts; the summary itself is
        # the only model text in content.
        report = out.content[0].text.split(bad, 1)[1]
        assert _MARKER not in report


# --- extract_from_emails -------------------------------------------------


@pytest.fixture
def invoice_db(tmp_path: Path):
    """Two invoice threads from different senders, each one body chunk."""
    conn = _db(tmp_path, "invoices.db")
    _insert_message(
        conn,
        message_id="a1@example.com",
        thread_id="t-acme",
        subject="acme invoice",
        sent_at="2024-04-01T09:00:00+00:00",
        from_=["Alice Example <alice@example.com>"],
        body=f"Invoice INV-100 from Acme Supplies is due on 2024-05-01. {_MARKER}",
    )
    _insert_message(
        conn,
        message_id="b1@example.com",
        thread_id="t-beta",
        subject="beta invoice",
        sent_at="2024-04-02T09:00:00+00:00",
        from_=["bob@example.com"],
        body="Invoice INV-200 from Beta Works is due on 2024-06-01.",
        attachment_text="Beta Works statement: total 900 units.",
    )
    conn.close()
    return Database(str(tmp_path / "invoices.db"))


class _ByThread(FakeInferenceClient):
    """Answers each per-thread prompt by the invoice number it shows, so
    the tests do not depend on retrieval order."""

    def __init__(self, answers: dict[str, str]) -> None:
        super().__init__()
        self.answers = answers

    async def complete(self, system: str, user: str) -> str:
        self.complete_calls.append((system, user))
        for key, answer in self.answers.items():
            if key in user:
                return answer
        return "null"


_SCHEMA = {"vendor": "string", "invoice": "string", "due": "string"}


def _extract(db, inference, schema=None):
    server = FakeMCPServer()
    register_intelligence_tools(server, db, FakeEmbedClient(), inference)
    return asyncio.run(
        server.tools["extract_from_emails"](query="invoices", schema=schema or _SCHEMA)
    )


def _labels_in(user_prompt: str) -> list[str]:
    return list(_headers(user_prompt))


def _record(labels: dict, **values) -> str:
    return json.dumps({**values, "_evidence": labels})


class TestExtractPrompt:
    def test_labels_are_numbered_across_the_call(self, invoice_db):
        llm = _ByThread({})
        _extract(invoice_db, llm)
        assert len(llm.complete_calls) == 2
        seen: list[str] = []
        for system, user in llm.complete_calls:
            assert system == EXTRACT_SYSTEM
            labels = _labels_in(user)
            assert labels and not set(labels) & set(seen)
            assert "[E1" not in _outside_blocks(user)
            assert '"_evidence"' in _outside_blocks(user)
            seen += labels
        assert sorted(seen, key=lambda lb: int(lb[1:])) == [f"E{n}" for n in range(1, 4)]


def _label_of(llm: _ByThread, needle: str) -> str:
    """The label of the passage containing ``needle``."""
    for _system, user in llm.complete_calls:
        for label, header in _headers(user).items():
            start = user.index(header) + len(header) + 1
            if needle in user[start : user.find("\n", start)]:
                return label
    raise AssertionError(needle)


class TestExtractCheck:
    def _two_passes(self, invoice_db, answers):
        """Run once to learn the labels, then with answers built from them."""
        probe = _ByThread({})
        _extract(invoice_db, probe)
        acme = _label_of(probe, "INV-100")
        beta_body = _label_of(probe, "INV-200")
        beta_att = _label_of(probe, "total 900")
        llm = _ByThread(answers(acme, beta_body, beta_att))
        return llm, _extract(invoice_db, llm), (acme, beta_body, beta_att)

    def test_cited_values_are_verified_and_resolve(self, invoice_db):
        llm, out, (acme, beta_body, _att) = self._two_passes(
            invoice_db,
            lambda a, b, _c: {
                "INV-100": _record(
                    {"vendor": [a], "invoice": f"[{a}]", "due": [a]},
                    vendor="Acme Supplies",
                    invoice="INV-100",
                    due="May 1, 2024",
                ),
                "INV-200": _record({"vendor": [b]}, vendor="Beta Works"),
            },
        )
        assert len(llm.complete_calls) == 2  # one call per thread, no repair
        data = out.structured_content
        ExtractFromEmailsOutput.model_validate(data)
        assert data["citation_problems"] == []
        records = json.loads(out.content[0].text)
        assert records == data["records"]
        by_vendor = {r["vendor"]: r for r in records}
        assert by_vendor["Acme Supplies"]["_evidence"] == {
            "vendor": [acme],
            "invoice": [acme],
            "due": [acme],
        }
        assert by_vendor["Beta Works"]["_evidence"] == {"vendor": [beta_body]}
        citations = {c["label"]: c for c in data["citations"]}
        assert set(citations) == {acme, beta_body}
        assert citations[acme]["claimant_id"] == claimant_of("a1@example.com")
        assert citations[acme]["thread_id"] == "t-acme"
        assert citations[beta_body]["sender"] == "bob@example.com"
        checks = {(f["field"], f["value_check"]) for f in data["fields"]}
        # A reformatted date is unmatched, which is not a problem.
        assert ("due", "unmatched") in checks
        assert ("invoice", "verified") in checks
        prose = "\n".join(c.text for c in out.content[1:])
        assert "Citations:" in prose
        assert "Value check: 3 of 4 text value(s)" in prose

    def test_invented_and_cross_thread_labels_are_unknown(self, invoice_db):
        """A label that exists only in another thread's prompt was never
        shown for this record, so it is unknown here."""
        _llm, out, (acme, beta_body, _att) = self._two_passes(
            invoice_db,
            lambda a, b, _c: {
                "INV-100": _record({"vendor": [b], "invoice": ["E99"]}, vendor="Acme", invoice="X"),
            },
        )
        data = out.structured_content
        [problem] = data["citation_problems"]
        assert problem["kind"] == "unknown_labels"
        assert sorted(problem["labels"]) == sorted([beta_body, "E99"])
        assert {f["status"] for f in data["fields"]} == {"invalid"}
        assert data["records"][0]["_evidence"] == {"vendor": [], "invoice": []}
        assert data["citations"] == []
        assert "1 record(s) cite 2 label(s)" in "\n".join(c.text for c in out.content)

    def test_missing_evidence_is_reported_per_field(self, invoice_db):
        llm = _ByThread({"INV-100": json.dumps({"vendor": "Acme", "invoice": None, "due": ""})})
        out = _extract(invoice_db, llm)
        data = out.structured_content
        [problem] = data["citation_problems"]
        # Null and empty values make no claim, so only vendor needs a label.
        assert problem == {
            "record": 0,
            "kind": "uncited_fields",
            "labels": [],
            "fields": ["vendor"],
        }
        assert [f["value_check"] for f in data["fields"]] == ["uncited"]
        assert "1 field value(s) in 1 record(s) cite no supplied passage" in out.content[-1].text

    def test_a_misattributed_value_is_reported(self, invoice_db):
        _llm, out, (_a, beta_body, beta_att) = self._two_passes(
            invoice_db,
            lambda _a, b, _c: {"INV-200": _record({"total": [b]}, total="total 900 units")},
        )
        data = out.structured_content
        [problem] = data["citation_problems"]
        assert problem["kind"] == "misattributed_values"
        assert problem["fields"] == ["total"] and problem["labels"] == [beta_att]
        [field] = data["fields"]
        assert field["value_check"] == "misattributed" and field["found_in"] == [beta_att]

    def test_schema_declaring_evidence_is_refused_before_inference(self, invoice_db):
        llm = _ByThread({})
        with pytest.raises(Exception, match="reserved"):
            _extract(invoice_db, llm, schema={"_evidence": "string"})
        assert llm.complete_calls == []

    def test_no_results_still_has_structured_output(self, invoice_db):
        invoice_db.hybrid_search = lambda **_kw: []  # type: ignore[assignment]
        out = _extract(invoice_db, _ByThread({}))
        assert out.content[0].text == "No matching emails found."
        assert out.structured_content["records"] == []

    def test_mail_and_model_text_stay_out_of_the_logs(self, invoice_db, caplog):
        caplog.set_level(logging.DEBUG)
        llm = _ByThread(
            {
                "INV-100": _record(
                    {"vendor": ["E77"], f"{_MARKER}": ["E1"]},
                    vendor=f"{_MARKER} vendor",
                    **{_MARKER: f"{_MARKER} value"},
                )
            }
        )
        out = _extract(invoice_db, llm)
        assert out.structured_content["citation_problems"]
        assert _MARKER not in caplog.text
        assert "extract_from_emails citations:" in caplog.text
        # Only the records item carries model text; the reports are counts.
        for item in out.content[1:]:
            assert _MARKER not in item.text


class TestExtractBounds:
    def test_value_checks_are_capped_per_thread(self, invoice_db, monkeypatch):
        """Many string values in one answer: at most _MAX_CHECKED_QUOTES
        are searched, each in each passage of its thread at most once."""
        calls = 0
        real = intelligence._quote_in

        def counting(fragments, text):
            nonlocal calls
            calls += 1
            return real(fragments, text)

        monkeypatch.setattr(intelligence, "_quote_in", counting)
        probe = _ByThread({})
        _extract(invoice_db, probe)
        acme = _label_of(probe, "INV-100")
        values = {f"f{k}": f"word{k} absent here" for k in range(500)}
        answer = json.dumps({**values, "_evidence": {name: [acme] for name in values}})
        llm = _ByThread({"INV-100": answer})
        start = time.perf_counter()
        out = _extract(invoice_db, llm, schema={"f0": "string"})
        assert time.perf_counter() - start < 5
        statuses = [f["value_check"] for f in out.structured_content["fields"]]
        assert statuses.count("unmatched") == _MAX_CHECKED_QUOTES
        assert statuses.count("not_checked") == 500 - _MAX_CHECKED_QUOTES
        # The Acme thread shows one passage: each checked value searches it
        # once as cited, and there is no other passage to search.
        assert calls == _MAX_CHECKED_QUOTES

    def test_check_records_is_linear_in_labels(self):
        """A huge _evidence list, of repeated and of distinct unknown
        labels, is read in one pass and each label kept once."""
        known = {"E1": EvidenceRef("E1", "t", None, None, "alpha beta")}
        distinct = [f"E{n}" for n in range(2, 50_002)]
        record = {"v": "alpha", "w": "beta", "_evidence": {"v": ["E1"] * 50_000, "w": distinct}}
        start = time.perf_counter()
        check = _check_records([record], 0, known)
        assert time.perf_counter() - start < 5
        assert record["_evidence"] == {"v": ["E1"], "w": []}
        assert [p.kind for p in check.problems] == ["unknown_labels"]
        assert check.problems[0].labels == distinct

    def test_provenance_names_are_not_checked(self):
        """A _date or _source_thread the model wrote is replaced by the
        server's provenance, so it is not a field of the record."""
        known = {"E1": EvidenceRef("E1", "t", None, None, "alpha")}
        record = {"a": "alpha", "_date": "2020-01-01", "_evidence": {"a": ["E1"]}}
        check = _check_records([record], 0, known)
        assert [f.field for f in check.fields] == ["a"]
        assert check.problems == []
        assert record["_evidence"] == {"a": ["E1"]}

    def test_uncited_values_do_not_use_the_search_cap(self):
        known = {"E1": EvidenceRef("E1", "t", None, None, "alpha")}
        record = {f"u{k}": "alpha" for k in range(30)} | {"a": "alpha"}
        record["_evidence"] = {"a": ["E1"]}
        check = _check_records([record], 0, known)
        statuses = {f.field: f.value_check for f in check.fields}
        assert statuses["a"] == "verified"
        assert set(statuses.values()) == {"verified", "uncited"}

    def test_non_list_evidence_entries_name_no_label(self):
        known = {"E1": EvidenceRef("E1", "t", None, None, "alpha")}
        records = [
            {"a": "alpha", "_evidence": "E1"},
            {"a": "alpha", "_evidence": {"a": {"label": "E1"}}},
            {"a": "alpha", "_evidence": {"a": "E1, E1"}},
        ]
        check = _check_records(records, 5, known)
        assert [r["_evidence"] for r in records] == [{"a": []}, {"a": []}, {"a": ["E1"]}]
        assert [(p.record, p.kind) for p in check.problems] == [
            (5, "uncited_fields"),
            (6, "uncited_fields"),
        ]


class TestWire:
    """Through a real FastMCP client: the declared output schemas accept
    the structured content."""

    def _call(self, db, llm, tool, args):
        server = FastMCP("test")
        register_intelligence_tools(server, db, FakeEmbedClient(), llm)

        async def run():
            async with Client(server) as client:
                return await client.call_tool(tool, args)

        return asyncio.run(run())

    def test_summarize_thread(self, plan_db):
        result = self._call(
            plan_db, FakeInferenceClient(_GOOD_SUMMARY), "summarize_thread", {"thread_id": "t-plan"}
        )
        assert result.structured_content["citations"][0]["label"] == "E1"

    def test_extract_from_emails(self, invoice_db):
        llm = _ByThread({"INV-100": _record({"vendor": ["E1"]}, vendor="Acme")})
        result = self._call(
            invoice_db, llm, "extract_from_emails", {"query": "invoices", "schema": _SCHEMA}
        )
        assert len(result.structured_content["records"]) == 1
