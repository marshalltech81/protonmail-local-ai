"""
``get_evidence(dedupe_attachments=True)`` (#989): one attachment carried
by several messages of a thread is returned once.

Attachment passages with the same content hash (``attachment_id``) and
chunk index collapse onto the earliest carrying message, which lists
the other carriers in ``carried_by``. A different document under the
same filename stays separate, body passages are untouched, and the
default output does not change. All data is synthetic.
"""

import asyncio
import logging
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec
from src.lib.sqlite import Database
from src.tools.outputs import EvidenceOutput
from src.tools.search import register_search_tools

from tests.conftest import (
    FakeEmbedClient,
    FakeMCPServer,
    _build_schema,
    _insert_attachment,
    _insert_chunk,
    _insert_message,
    claimant_of,
)
from tests.test_evidence_scope import _finish_threads

_MARKER = "SYNTHETIC_DEDUPE_MARKER_989"
_QUERY = "venue layout"
_COACH = "Coach Rivera <coach@plan.example>"
_VOLUNTEER = "Volunteer Parent <volunteer@home.example>"
_PARENT = "parent@home.example"
_SENT = {
    "first@plan.example": ("2025-09-01T09:00:00+00:00", _COACH),
    "resent@plan.example": ("2025-09-02T09:00:00+00:00", _COACH),
    "forward@plan.example": ("2025-09-03T09:00:00+00:00", _VOLUNTEER),
}
_PLAN_0 = f"Venue plan, page one. {_MARKER}"
_PLAN_1 = "Venue plan, page two."


def _chunk(conn, chunk_id, message_id, text, x, **kwargs):
    _insert_chunk(
        conn,
        chunk_id=chunk_id,
        message_id=message_id,
        thread_id="t-plan",
        text=text,
        embedding=[x, round(1 - x, 2), 0.0, 0.0],
        **kwargs,
    )


def _dedupe_db(tmp_path: Path) -> Database:
    """One thread, ``t-plan``: the coach sends ``plan.pdf`` (two
    passages), re-sends it, and a volunteer forwards it, so three
    messages carry the same content hash. The re-send also carries a
    different document named ``plan.pdf``. The later copies rank above
    the first one, so the keeper is chosen by date, not rank."""
    path = tmp_path / "dedupe.db"
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    for message_id, (sent_at, sender) in _SENT.items():
        _insert_message(
            conn,
            message_id=message_id,
            thread_id="t-plan",
            subject="venue plan",
            sent_at=sent_at,
            from_=[sender],
            to=[_PARENT],
            has_attachments=True,
        )
        _insert_attachment(
            conn,
            message_id=message_id,
            thread_id="t-plan",
            attachment_id="att-plan",
            filename="plan.pdf",
        )
    _insert_attachment(
        conn,
        message_id="resent@plan.example",
        thread_id="t-plan",
        attachment_id="att-other",
        filename="plan.pdf",
    )
    _chunk(conn, "fwd-0", "forward@plan.example", _PLAN_0, 1.0, attachment_id="att-plan")
    _chunk(conn, "resent-0", "resent@plan.example", _PLAN_0, 0.99, attachment_id="att-plan")
    _chunk(conn, "first-0", "first@plan.example", _PLAN_0, 0.98, attachment_id="att-plan")
    _chunk(conn, "first-body", "first@plan.example", "Plan attached.", 0.9)
    for chunk_id, message_id, x in (
        ("fwd-1", "forward@plan.example", 0.85),
        ("resent-1", "resent@plan.example", 0.84),
        ("first-1", "first@plan.example", 0.83),
    ):
        _chunk(
            conn,
            chunk_id,
            message_id,
            _PLAN_1,
            x,
            attachment_id="att-plan",
            chunk_index=1,
            char_start=len(_PLAN_0),
        )
    _chunk(
        conn,
        "other-0",
        "resent@plan.example",
        "Seating chart for the hall.",
        0.8,
        attachment_id="att-other",
    )
    _finish_threads(conn)
    conn.close()
    return Database(str(path))


@pytest.fixture
def dedupe_db(tmp_path: Path) -> Database:
    return _dedupe_db(tmp_path)


def _evidence(db: Database, **kwargs):
    server = FakeMCPServer()
    register_search_tools(server, db, FakeEmbedClient())
    return asyncio.run(server.tools["get_evidence"](query=_QUERY, **kwargs))


def _chunks(out) -> list[dict]:
    (thread,) = out.structured_content["threads"]
    return thread["chunks"]


def _carrier(message_id: str, scope: str = "in_scope") -> dict:
    return {
        "claimant_id": claimant_of(message_id),
        "sent_at": _SENT[message_id][0],
        "sent_at_status": "parsed",
        "occurred_at": None,
        "scope": scope,
    }


_DEFAULT_ORDER = ["fwd-0", "resent-0", "first-0", "first-body", "fwd-1", "resent-1"]


class TestDefaultUnchanged:
    def test_without_the_flag_every_copy_is_returned_and_nothing_is_added(self, dedupe_db):
        for kwargs in ({}, {"dedupe_attachments": False}):
            out = _evidence(dedupe_db, thread_id="t-plan", limit=6, **kwargs)
            assert [c["chunk_id"] for c in _chunks(out)] == _DEFAULT_ORDER
            assert "attachment_copies_collapsed" not in out.structured_content
            assert all("carried_by" not in c for c in _chunks(out))
            assert "carried by" not in out.content[0].text.lower()


class TestCollapse:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"thread_id": "t-plan"},
            {"from_addr": "coach@plan.example"},
            {"participant": _PARENT, "max_threads": 1},
        ],
    )
    def test_one_block_per_passage_on_the_earliest_carrier(self, dedupe_db, kwargs):
        out = _evidence(dedupe_db, dedupe_attachments=True, **kwargs)
        EvidenceOutput.model_validate(out.structured_content)
        chunks = _chunks(out)
        # Collapsing frees the per-thread slots for distinct passages:
        # the other document is now within the six-passage cap.
        assert [c["chunk_id"] for c in chunks] == ["first-0", "first-body", "first-1", "other-0"]
        forward_scope = "context" if "from_addr" in kwargs else "in_scope"
        expected = [
            _carrier("resent@plan.example"),
            _carrier("forward@plan.example", forward_scope),
        ]
        by_id = {c["chunk_id"]: c for c in chunks}
        assert by_id["first-0"]["carried_by"] == expected
        assert by_id["first-1"]["carried_by"] == expected
        # A different document with the same filename stays separate.
        assert by_id["other-0"]["carried_by"] == []
        assert by_id["other-0"]["attachment_id"] == "att-other"
        # Body passages have no carriers.
        assert "carried_by" not in by_id["first-body"]
        assert out.structured_content["attachment_copies_collapsed"] == 4
        assert out.structured_content["chunk_count"] == 4

    def test_the_prose_names_the_other_carriers_and_the_count(self, dedupe_db):
        out = _evidence(dedupe_db, thread_id="t-plan", dedupe_attachments=True)
        text = out.content[0].text
        assert "dedupe_attachments collapsed 4 repeated attachment passage(s)." in text
        carriers = (
            f"        Also carried by: msg {claimant_of('resent@plan.example')} (2025-09-02), "
            f"msg {claimant_of('forward@plan.example')} (2025-09-03)"
        )
        assert text.count(carriers) == 2

    def test_scope_in_scope_drops_context_copies_before_collapsing(self, dedupe_db):
        out = _evidence(
            dedupe_db, from_addr="coach@plan.example", scope="in_scope", dedupe_attachments=True
        )
        by_id = {c["chunk_id"]: c for c in _chunks(out)}
        assert by_id["first-0"]["carried_by"] == [_carrier("resent@plan.example")]
        assert out.structured_content["attachment_copies_collapsed"] == 2
        assert out.structured_content["context_passages_left_out"] == 2

    def test_source_body_leaves_nothing_to_collapse(self, dedupe_db):
        out = _evidence(dedupe_db, thread_id="t-plan", source="body", dedupe_attachments=True)
        assert [c["chunk_id"] for c in _chunks(out)] == ["first-body"]
        assert out.structured_content["attachment_copies_collapsed"] == 0

    def test_the_cap_and_limit_apply_after_collapsing(self, dedupe_db):
        out = _evidence(dedupe_db, thread_id="t-plan", limit=2, dedupe_attachments=True)
        assert [c["chunk_id"] for c in _chunks(out)] == ["first-0", "first-body"]
        # Only the copies folded into returned passages are counted.
        assert out.structured_content["attachment_copies_collapsed"] == 2
        out = _evidence(
            dedupe_db,
            from_addr="coach@plan.example",
            max_chunks_per_thread=1,
            dedupe_attachments=True,
        )
        assert [c["chunk_id"] for c in _chunks(out)] == ["first-0"]
        assert out.structured_content["attachment_copies_collapsed"] == 2


class TestLogging:
    def test_the_flag_and_counts_are_logged_without_mail_text(self, dedupe_db, caplog):
        with caplog.at_level(logging.INFO):
            _evidence(dedupe_db, thread_id="t-plan", dedupe_attachments=True)
            _evidence(dedupe_db, thread_id="t-plan")
        call = next(
            r.getMessage() for r in caplog.records if r.getMessage().startswith("tool=get_evidence")
        )
        assert "'dedupe_attachments': True" in call
        deduped, default = (r.getMessage() for r in caplog.records if r.name == "mcp.timings")
        assert "'evidence_filtered': 1" in deduped
        assert "'evidence_attachment_copies_collapsed': 4" in deduped
        assert "evidence_attachment_copies_collapsed" not in default
        assert _MARKER not in caplog.text


def _copies_db(tmp_path: Path, texts: list[str]) -> Database:
    """One thread whose messages each carry ``att-plan`` with one
    passage; message ``i`` is sent on day ``i + 1`` with ``texts[i]``."""
    path = tmp_path / "copies.db"
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    for i, text in enumerate(texts):
        message_id = f"copy{i:02d}@plan.example"
        _insert_message(
            conn,
            message_id=message_id,
            thread_id="t-plan",
            subject="venue plan",
            sent_at=f"2025-09-{i + 1:02d}T09:00:00+00:00",
            from_=[_COACH],
            to=[_PARENT],
            has_attachments=True,
        )
        _insert_attachment(
            conn,
            message_id=message_id,
            thread_id="t-plan",
            attachment_id="att-plan",
            filename="plan.pdf",
        )
        _chunk(conn, f"copy-{i:02d}", message_id, text, 1.0 - i / 100, attachment_id="att-plan")
    _finish_threads(conn)
    conn.close()
    return Database(str(path))


class TestReviewRoundOne:
    """Codex review round 1 on PR #1046."""

    def test_copies_with_different_text_stay_separate(self, tmp_path):
        # One payload whose copies were chunked differently (another
        # extractor module, or stale chunks after an extractor version
        # bump): same hash and chunk index, different text.
        db = _copies_db(tmp_path, [_PLAN_0, f"Venue plan, re-extracted. {_MARKER}", _PLAN_0])
        out = _evidence(db, thread_id="t-plan", dedupe_attachments=True)
        by_id = {c["chunk_id"]: c for c in _chunks(out)}
        assert sorted(by_id) == ["copy-00", "copy-01"]
        assert [c["claimant_id"] for c in by_id["copy-00"]["carried_by"]] == [
            claimant_of("copy02@plan.example")
        ]
        assert by_id["copy-01"]["carried_by"] == []
        assert out.structured_content["attachment_copies_collapsed"] == 1

    def test_carried_by_lists_at_most_ten_and_counts_every_carrier(self, tmp_path, caplog):
        db = _copies_db(tmp_path, [_PLAN_0] * 13)
        with caplog.at_level(logging.INFO):
            out = _evidence(db, thread_id="t-plan", dedupe_attachments=True)
        EvidenceOutput.model_validate(out.structured_content)
        (chunk,) = _chunks(out)
        assert chunk["chunk_id"] == "copy-00"
        assert [c["claimant_id"] for c in chunk["carried_by"]] == [
            claimant_of(f"copy{i:02d}@plan.example") for i in range(1, 11)
        ]
        assert chunk["carried_by_count"] == 12
        assert out.structured_content["attachment_copies_collapsed"] == 12
        text = out.content[0].text
        assert f"msg {claimant_of('copy10@plan.example')} (2025-09-11), and 2 more" in text
        assert claimant_of("copy11@plan.example") not in text
        timing = next(r.getMessage() for r in caplog.records if r.name == "mcp.timings")
        assert "'evidence_carriers_unlisted': 2" in timing
        assert _MARKER not in caplog.text

    def test_a_short_carrier_list_reports_its_count(self, dedupe_db):
        out = _evidence(dedupe_db, thread_id="t-plan", dedupe_attachments=True)
        by_id = {c["chunk_id"]: c for c in _chunks(out)}
        assert by_id["first-0"]["carried_by_count"] == 2
        assert by_id["other-0"]["carried_by_count"] == 0
        assert "carried_by_count" not in by_id["first-body"]
