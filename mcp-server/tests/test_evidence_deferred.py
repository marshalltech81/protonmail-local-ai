"""Passages from a deferred attachment are flagged (#1236, owner 2026-10-09).

The indexer keeps the chunks of an attachment whose extraction it
deferred: the text indexed before, until the refresh. Every passage,
carrier and citation from such a payload (any copy of it in the same
message deferred) carries ``extraction_deferred`` and the fixed prose
note "retained indexed text; extraction refresh pending"; each tool
counts them on its timing line. Citation labels and quote checks are
unchanged. All data is synthetic.
"""

import logging
import sqlite3

import pytest
from src.lib.sqlite import ChunkResult
from src.tools.brief import _finding_lines
from src.tools.intelligence import (
    _LABELLED_HEADER_MAX_CHARS,
    EvidenceRef,
    _build_evidence,
    _chunk_header,
    _citation,
    _citation_lines,
)
from src.tools.outputs import EXTRACTION_DEFERRED_NOTE, CheckedFinding, FindingSource

from tests.conftest import claimant_of
from tests.test_evidence_dedupe import _chunks, _dedupe_db, _evidence
from tests.test_search import _wire_descriptions

NOTE = "retained indexed text; extraction refresh pending"


def test_the_note_is_the_fixed_text():
    assert EXTRACTION_DEFERRED_NOTE == NOTE


def _defer(db, message_id: str, attachment_id: str = "att-plan") -> None:
    conn = sqlite3.connect(db.path)
    conn.execute(
        "UPDATE attachments SET extraction_deferred_at = '2025-09-04T00:00:00+00:00' "
        "WHERE claimant_id = ? AND attachment_id = ?",
        (claimant_of(message_id), attachment_id),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def deferred_db(tmp_path):
    """The dedupe thread with the re-sent copy of plan.pdf deferred."""
    db = _dedupe_db(tmp_path)
    _defer(db, "resent@plan.example")
    return db


def _timing(caplog) -> str:
    [line] = [r.getMessage() for r in caplog.records if r.name == "mcp.timings"]
    return line


class TestGetEvidence:
    def test_passages_of_a_deferred_payload_are_flagged(self, deferred_db, caplog):
        caplog.set_level(logging.INFO)
        out = _evidence(deferred_db, thread_id="t-plan", limit=20)
        flags = {c["chunk_id"]: c["extraction_deferred"] for c in _chunks(out)}
        # Only the re-send's copy of plan.pdf: the same payload carried by
        # other messages, its other document and body text are not.
        assert flags == {
            "fwd-0": False,
            "resent-0": True,
            "first-0": False,
            "first-body": False,
            "fwd-1": False,
            "resent-1": True,
            "first-1": False,
            "other-0": False,
        }
        text = out.content[0].text
        assert text.count(f'Source: attachment "plan.pdf" (application/pdf); {NOTE}') == 2
        assert "'evidence_extraction_deferred': 2" in _timing(caplog)
        assert "SYNTHETIC_DEDUPE_MARKER" not in caplog.text

    def test_each_carrier_keeps_its_own_flag(self, deferred_db, caplog):
        caplog.set_level(logging.INFO)
        out = _evidence(deferred_db, thread_id="t-plan", limit=20, dedupe_attachments=True)
        by_id = {c["chunk_id"]: c for c in _chunks(out)}
        keeper = by_id["first-0"]
        assert keeper["extraction_deferred"] is False
        assert {c["claimant_id"]: c["extraction_deferred"] for c in keeper["carried_by"]} == {
            claimant_of("resent@plan.example"): True,
            claimant_of("forward@plan.example"): False,
        }
        text = out.content[0].text
        assert f"msg {claimant_of('resent@plan.example')} (2025-09-02; {NOTE})" in text
        assert f"msg {claimant_of('forward@plan.example')} (2025-09-03)" in text
        # Two listed deferred carriers (one per passage of plan.pdf).
        assert "'evidence_extraction_deferred': 2" in _timing(caplog)

    def test_nothing_deferred_means_no_flag_and_no_count(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        out = _evidence(_dedupe_db(tmp_path), thread_id="t-plan", limit=20)
        assert not any(c["extraction_deferred"] for c in _chunks(out))
        assert NOTE not in out.content[0].text
        assert "'evidence_extraction_deferred': 0" in _timing(caplog)


def _steps_for_flags(tmp_path, parts: int, *, drop_index: bool = False) -> int:
    """SQLite VM steps (progress-handler calls every 100) to compute the
    deferral flag of every chunk of one message with ``parts`` distinct
    attachments, each with one chunk, one in ten deferred."""
    from src.lib.sqlite import _CHUNK_DEFERRED_SQL

    from tests.conftest import _insert_attachment, _insert_chunk, _insert_message
    from tests.test_sqlite import _open_built_db_conn

    conn, _ = _open_built_db_conn(tmp_path, f"steps-{parts}-{drop_index}.db")
    _insert_message(
        conn,
        message_id="m@x",
        thread_id="t",
        sent_at="2025-01-01T00:00:00+00:00",
        has_attachments=True,
    )
    for i in range(parts):
        _insert_attachment(
            conn,
            message_id="m@x",
            thread_id="t",
            attachment_id=f"p{i}",
            filename=f"f{i}.txt",
            occurrence_id=f"occ{i}",
            deferred=i % 10 == 0,
        )
        _insert_chunk(
            conn,
            chunk_id=f"c{i}",
            message_id="m@x",
            thread_id="t",
            text="synthetic",
            embedding=[0.1, 0.2, 0.3, 0.4],
            attachment_id=f"p{i}",
            kind="attachment",
        )
    if drop_index:
        conn.execute("DROP INDEX idx_attachments_deferred")
    conn.commit()
    steps = [0]

    def tick() -> int:
        steps[0] += 1
        return 0

    conn.set_progress_handler(tick, 100)
    rows = conn.execute(f"SELECT {_CHUNK_DEFERRED_SQL} FROM message_chunks c").fetchall()
    conn.close()
    assert sum(r[0] for r in rows) == parts // 10
    return steps[0]


class TestFlagCost:
    def test_the_per_chunk_flag_grows_linearly(self, tmp_path):
        """Codex round 11 on #1355: the flag's EXISTS runs once per chunk;
        the partial composite index keeps each probe constant, so the
        work grows with the chunks, not with chunks times attachments."""
        small = _steps_for_flags(tmp_path, 300)
        large = _steps_for_flags(tmp_path, 1200)
        assert large < 6 * small

    def test_without_the_index_it_grows_quadratically(self, tmp_path):
        """The gate fails on the known-bad shape: without the index each
        probe scans the message's attachments."""
        small = _steps_for_flags(tmp_path, 300, drop_index=True)
        large = _steps_for_flags(tmp_path, 1200, drop_index=True)
        assert large > 10 * small


class TestQueryPaths:
    def test_the_vector_lane_flags_a_deferred_payload(self, deferred_db):
        chunks = deferred_db._chunk_vector_search([1.0, 0.0, 0.0, 0.0], 20)
        assert chunks is not None
        flags = {c.chunk_id: c.extraction_deferred for c in chunks}
        assert flags["resent-0"] is True
        assert flags["fwd-0"] is False and flags["first-body"] is False


def _chunk(**kwargs) -> ChunkResult:
    base = dict(
        chunk_id="c1",
        message_id="m@x",
        claimant_id="m@x#0123456789abcdef",
        thread_id="t1",
        chunk_index=0,
        text="synthetic attachment text",
        char_start=0,
        char_end=25,
        attachment_id="att",
        attachment_filename="plan.pdf",
        attachment_mime="application/pdf",
        message_date="2025-09-02T09:00:00+00:00",
        message_sender="Sender <s@example.com>",
        message_sender_ambiguous=False,
        kind="attachment",
    )
    base.update(kwargs)
    return ChunkResult(**base)


class TestPromptAndCitations:
    def test_the_prompt_header_carries_the_note(self):
        header = _chunk_header(_chunk(extraction_deferred=True), 25, "E1")
        assert header.endswith(f"chars 0-25; {NOTE}]")
        assert NOTE not in _chunk_header(_chunk(), 25, "E1")
        # Unlabelled headers (summaries) carry it too.
        assert NOTE in _chunk_header(_chunk(extraction_deferred=True), 25)

    @pytest.mark.parametrize("deferred", [False, True])
    def test_the_labelled_header_cap_holds_with_the_note(self, deferred):
        long = "x" * 2000
        chunk = _chunk(
            claimant_id="m" * 500 + "#0123456789abcdef",
            message_sender=long,
            message_sender_ambiguous=True,
            attachment_filename=long,
            attachment_mime=long,
            chunk_index=99999,
            char_start=999999,
            char_end=9999999,
            extraction_deferred=deferred,
        )
        header = _chunk_header(chunk, 9999999, "E99999", "context")
        assert len(header) <= _LABELLED_HEADER_MAX_CHARS
        assert (NOTE in header) is deferred

    def test_build_evidence_counts_shown_deferred_passages(self):
        from datetime import UTC, datetime

        from src.lib.sqlite import ThreadResult

        thread = ThreadResult(
            thread_id="t1",
            subject="s",
            participants=[],
            folder="INBOX",
            date_first=datetime(2025, 1, 1, tzinfo=UTC),
            date_last=datetime(2025, 1, 1, tzinfo=UTC),
            message_ids=[],
            snippet="",
            has_attachments=True,
            evidence_chunks=[
                _chunk(chunk_id="a", extraction_deferred=True),
                _chunk(chunk_id="b", attachment_id="att2", text="other synthetic text"),
            ],
        )
        refs: dict[str, EvidenceRef] = {}
        [rendered], coverage = _build_evidence([thread], 10_000, evidence_map=refs)
        assert coverage.extraction_deferred == 1
        assert rendered.count(NOTE) == 1
        citations = [_citation(ref) for ref in refs.values()]
        assert [c.extraction_deferred for c in citations] == [True, False]
        # Labels are unchanged by the flag.
        assert [c.label for c in citations] == ["E1", "E2"]
        lines = _citation_lines(citations)
        assert lines[1].endswith(f"attachment plan.pdf, {NOTE} (thread t1, chunk a)")
        assert NOTE not in lines[2]

    def test_finding_lines_carry_the_note(self):
        thread = EvidenceRef("E1", "t1", _chunk(extraction_deferred=True), 25, "text")
        source = FindingSource(**_citation(thread).model_dump(), excerpt="synthetic excerpt")
        finding = CheckedFinding(
            relation="supports", explanation="synthetic", labels=["E1"], sources=[source]
        )
        [_, _, line] = _finding_lines([finding])
        assert f"attachment plan.pdf, {NOTE}: " in line


@pytest.mark.parametrize(
    "tool",
    ["ask_mailbox", "get_evidence", "extract_from_emails", "brief_issue", "check_conclusion"],
)
def test_descriptions_on_the_wire_state_the_flag(empty_db, tool):
    doc = _wire_descriptions(empty_db)[tool]
    assert "extraction_deferred=true" in doc
    assert NOTE in doc


def _deferred_threads():
    from tests.test_prompt_budget import _MARKER, _thread
    from tests.test_prompt_budget import _chunk as pchunk

    deferred = pchunk("c1", f"{_MARKER} retained text", attachment=True)
    deferred.extraction_deferred = True
    return [_thread("t1", [deferred, pchunk("c2", f"{_MARKER} current body text")])]


class TestInferenceTools:
    def test_ask_mailbox_shows_flags_and_counts(self, caplog):
        import asyncio

        from tests.conftest import FakeInferenceClient
        from tests.test_prompt_budget import _MARKER, _StubDb, _tools
        from tests.test_timings import _one_line

        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(response="answer [E1] and [E2]")
        out = asyncio.run(_tools(_StubDb(_deferred_threads()), llm)["ask_mailbox"](question="q?"))
        [(_, prompt)] = llm.complete_calls
        assert prompt.count(NOTE) == 1
        citations = out.structured_content["citations"]
        assert [(c["label"], c["extraction_deferred"]) for c in citations] == [
            ("E1", True),
            ("E2", False),
        ]
        assert f"attachment quote.pdf, {NOTE}" in out.content[0].text
        assert _one_line(caplog)["counts"]["evidence_extraction_deferred"] == 1
        assert _MARKER not in caplog.text

    @pytest.mark.parametrize(
        ("tool", "args", "experimental"),
        [
            ("extract_from_emails", {"query": "q", "schema": {"amount": "number"}}, False),
            ("brief_issue", {"topic": "synthetic topic"}, True),
            ("check_conclusion", {"conclusion": "synthetic conclusion"}, True),
        ],
    )
    def test_other_tools_show_the_note_and_count(self, caplog, tool, args, experimental):
        import asyncio

        from tests.conftest import FakeInferenceClient
        from tests.test_prompt_budget import _MARKER, _StubDb, _tools
        from tests.test_timings import _one_line

        caplog.set_level(logging.INFO)
        llm = FakeInferenceClient(response="null")
        tools = _tools(_StubDb(_deferred_threads()), llm, experimental=experimental)
        asyncio.run(tools[tool](**args))
        assert any(NOTE in prompt for _, prompt in llm.complete_calls)
        assert _one_line(caplog)["counts"]["evidence_extraction_deferred"] == 1
        assert _MARKER not in caplog.text
