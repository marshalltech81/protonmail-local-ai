"""
``get_evidence`` precision controls (#988): ``source``, ``scope``,
``max_chunks_per_thread`` and ``max_chars_per_chunk``.

Each control removes exactly the passages it names and nothing else;
leaving all four out returns the output pinned in
``evidence_precision_default.json`` (written before the controls
existed). A thread that ``scope=in_scope`` empties stays listed with
counts, and the timing line records the filtering. All data is
synthetic.
"""

import asyncio
import json
import logging
import sqlite3
from pathlib import Path

import pytest
import sqlite_vec
from fastmcp.exceptions import ToolError
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
    _insert_thread,
    source_sha256,
)
from tests.test_evidence_scope import _finish_threads

_MARKER = "SYNTHETIC_PRECISION_MARKER_988"
_QUERY = "field trip plans"
_COACH = "Coach Rivera <coach@trip.example>"
_VOLUNTEER = "Volunteer Parent <volunteer@home.example>"
_PARENT = "parent@home.example"
_SNAPSHOT = Path(__file__).parent / "evidence_precision_default.json"

# The chunks of ``t-trip`` in their default rank order (vector distance
# to the fake query embedding [1, 0, 0, 0]).
_TRIP_ORDER = ["lead-b0", "help-body", "lead-b1", "lead-att-permit", "lead-att-packing"]
_BODY = {"lead-b0", "lead-b1", "help-body"}
_ATTACHMENT = {"lead-att-permit", "lead-att-packing"}
_LONG_TEXT = "Permission form: sign and return by Friday. " * 6


def _chunk(conn, chunk_id, message_id, thread_id, text, x, **kwargs):
    _insert_chunk(
        conn,
        chunk_id=chunk_id,
        message_id=message_id,
        thread_id=thread_id,
        text=text,
        embedding=[x, round(1 - x, 2), 0.0, 0.0],
        **kwargs,
    )


def _precision_db(tmp_path: Path) -> Database:
    """Two threads:

    - ``t-trip``: the coach's message (two body chunks and two
      attachments, one of them longer than a small character cap) and a
      volunteer's reply, which a ``from_addr`` filter on the coach labels
      context. The reply carries the synthetic marker.
    - ``t-note``: a later coach message with one body chunk and no
      attachments.
    """
    path = tmp_path / "precision.db"
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    _insert_message(
        conn,
        message_id="lead@trip.example",
        thread_id="t-trip",
        subject="field trip",
        sent_at="2025-09-10T09:00:00+00:00",
        from_=[_COACH],
        to=[_PARENT],
        has_attachments=True,
    )
    _insert_message(
        conn,
        message_id="help@trip.example",
        thread_id="t-trip",
        subject="field trip",
        sent_at="2025-09-20T09:00:00+00:00",
        from_=[_VOLUNTEER],
        to=[_PARENT],
    )
    _insert_message(
        conn,
        message_id="note@trip.example",
        thread_id="t-note",
        subject="trip reminder",
        sent_at="2025-09-25T09:00:00+00:00",
        from_=[_COACH],
        to=[_PARENT],
    )
    for attachment_id, filename in (("att-permit", "permit.pdf"), ("att-packing", "packing.pdf")):
        _insert_attachment(
            conn,
            message_id="lead@trip.example",
            thread_id="t-trip",
            attachment_id=attachment_id,
            filename=filename,
        )
    _chunk(conn, "lead-b0", "lead@trip.example", "t-trip", "The bus leaves at 8am.", 1.0)
    _chunk(
        conn,
        "help-body",
        "help@trip.example",
        "t-trip",
        f"I can drive two children. {_MARKER}",
        0.95,
    )
    _chunk(
        conn,
        "lead-b1",
        "lead@trip.example",
        "t-trip",
        "Bring a packed lunch.",
        0.9,
        chunk_index=1,
        char_start=23,
    )
    _chunk(
        conn,
        "lead-att-permit",
        "lead@trip.example",
        "t-trip",
        _LONG_TEXT,
        0.8,
        attachment_id="att-permit",
    )
    _chunk(
        conn,
        "lead-att-packing",
        "lead@trip.example",
        "t-trip",
        "Packing list: hat, water bottle.",
        0.7,
        attachment_id="att-packing",
    )
    _chunk(conn, "note-body", "note@trip.example", "t-note", "Reminder: trip forms due.", 0.6)
    _finish_threads(conn)
    conn.close()
    return Database(str(path))


@pytest.fixture
def precision_db(tmp_path: Path) -> Database:
    return _precision_db(tmp_path)


def _tools(db: Database) -> dict:
    server = FakeMCPServer()
    register_search_tools(server, db, FakeEmbedClient())
    return server.tools


def _evidence(db: Database, **kwargs):
    return asyncio.run(_tools(db)["get_evidence"](query=_QUERY, **kwargs))


def _ids(out, thread_id: str = "t-trip") -> list[str]:
    for thread in out.structured_content["threads"]:
        if thread["thread_id"] == thread_id:
            return [c["chunk_id"] for c in thread["chunks"]]
    raise AssertionError(f"{thread_id} not listed")


def _thread(out, thread_id: str = "t-trip") -> dict:
    return next(t for t in out.structured_content["threads"] if t["thread_id"] == thread_id)


# The calls whose output is pinned with every control left out.
_PINNED_CALLS = {
    "sender_filtered": {"from_addr": "coach@trip.example"},
    "max_threads": {"from_addr": "coach@trip.example", "max_threads": 2},
    "thread": {"thread_id": "t-trip"},
    "unfiltered_scores": {"include_scores": True},
}


def _render(out) -> dict:
    """The call's prose and structured content. Each fixture file hash is
    swapped for a ``sha256(<message-id>)`` placeholder, an exact and
    reversible substitution, so the snapshot holds no hex strings that
    secret scanners flag."""
    rendered = json.dumps({"text": out.content[0].text, "structured": out.structured_content})
    for message_id in ("lead@trip.example", "help@trip.example", "note@trip.example"):
        rendered = rendered.replace(source_sha256(message_id), f"sha256({message_id})")
    return json.loads(rendered)


class TestDefaultsUnchanged:
    @pytest.mark.parametrize("name", sorted(_PINNED_CALLS))
    def test_omitting_every_control_matches_the_pinned_output(self, precision_db, name):
        pinned = json.loads(_SNAPSHOT.read_text())
        assert _render(_evidence(precision_db, **_PINNED_CALLS[name])) == pinned[name]

    @pytest.mark.parametrize("name", sorted(_PINNED_CALLS))
    def test_explicit_any_matches_the_pinned_output(self, precision_db, name):
        pinned = json.loads(_SNAPSHOT.read_text())
        out = _evidence(precision_db, source="any", scope="any", **_PINNED_CALLS[name])
        assert _render(out) == pinned[name]

    def test_the_default_order_is_the_fixtures_rank_order(self, precision_db):
        out = _evidence(precision_db, from_addr="coach@trip.example")
        assert _ids(out) == _TRIP_ORDER
        assert _ids(out, "t-note") == ["note-body"]


class TestEachControlRemovesOnlyWhatItNames:
    _FILTER = {"from_addr": "coach@trip.example"}

    def test_source_body_drops_only_attachment_passages(self, precision_db):
        out = _evidence(precision_db, source="body", **self._FILTER)
        assert _ids(out) == [c for c in _TRIP_ORDER if c in _BODY]
        assert _ids(out, "t-note") == ["note-body"]
        assert out.structured_content["threads_without_source_passages"] == 0

    def test_source_attachment_drops_body_passages_and_says_which_threads_had_none(
        self, precision_db
    ):
        out = _evidence(precision_db, source="attachment", **self._FILTER)
        assert _ids(out) == [c for c in _TRIP_ORDER if c in _ATTACHMENT]
        assert [t["thread_id"] for t in out.structured_content["threads"]] == ["t-trip"]
        assert out.structured_content["threads_without_source_passages"] == 1
        assert "1 ranked thread(s) had no attachment passages" in out.content[0].text

    def test_scope_in_scope_drops_only_context_passages(self, precision_db):
        out = _evidence(precision_db, scope="in_scope", **self._FILTER)
        assert _ids(out) == [c for c in _TRIP_ORDER if c != "help-body"]
        assert _ids(out, "t-note") == ["note-body"]
        assert _thread(out)["context_passages_left_out"] == 1
        assert _thread(out, "t-note")["context_passages_left_out"] == 0
        assert out.structured_content["context_passages_left_out"] == 1
        assert out.structured_content["chunk_count"] == 5
        text = out.content[0].text
        assert "scope=in_scope left out 1 context passage(s)" in text
        assert "    1 context passage(s) left out (scope=in_scope)." in text

    def test_max_chunks_per_thread_keeps_each_threads_top_passages(self, precision_db):
        out = _evidence(precision_db, max_chunks_per_thread=2, **self._FILTER)
        assert _ids(out) == _TRIP_ORDER[:2]
        assert _ids(out, "t-note") == ["note-body"]

    def test_max_chars_per_chunk_cuts_only_longer_passages(self, precision_db):
        default = _evidence(precision_db, **self._FILTER)
        out = _evidence(precision_db, max_chars_per_chunk=22, **self._FILTER)
        assert _ids(out) == _TRIP_ORDER
        before = {
            c["chunk_id"]: c for t in default.structured_content["threads"] for c in t["chunks"]
        }
        for t in out.structured_content["threads"]:
            for c in t["chunks"]:
                full = before[c["chunk_id"]]["text"]
                assert c["text"] == full[:22]
                assert c["text_truncated"] is (len(full) > 22)
        # "The bus leaves at 8am." is exactly 22 characters: not cut.
        assert "        The bus leaves at 8am.\n" in out.content[0].text
        assert f"        {_LONG_TEXT[:22]} ... [truncated]" in out.content[0].text

    def test_controls_combine_scope_before_the_per_thread_cap(self, precision_db):
        out = _evidence(
            precision_db, scope="in_scope", source="body", max_chunks_per_thread=2, **self._FILTER
        )
        assert _ids(out) == ["lead-b0", "lead-b1"]

    def test_max_threads_audit_keeps_its_thread_ranking(self, precision_db):
        default = _evidence(precision_db, max_threads=2, **self._FILTER)
        out = _evidence(precision_db, max_threads=2, source="body", **self._FILTER)
        ranked = [t["thread_id"] for t in default.structured_content["threads"]]
        assert [t["thread_id"] for t in out.structured_content["threads"]] == ranked


class TestThreadPath:
    def test_source_and_caps_apply_on_the_thread_path(self, precision_db):
        out = _evidence(precision_db, thread_id="t-trip", source="attachment")
        assert _ids(out) == [c for c in _TRIP_ORDER if c in _ATTACHMENT]
        out = _evidence(precision_db, thread_id="t-trip", max_chunks_per_thread=3)
        assert _ids(out) == _TRIP_ORDER[:3]

    def test_scope_in_scope_drops_nothing_without_filters(self, precision_db):
        out = _evidence(precision_db, thread_id="t-trip", scope="in_scope")
        assert _ids(out) == _TRIP_ORDER
        assert _thread(out)["context_passages_left_out"] == 0

    def test_a_thread_without_passages_of_the_source_says_so(self, precision_db):
        out = _evidence(precision_db, thread_id="t-note", source="attachment")
        assert out.structured_content["threads"] == []
        assert out.content[0].text == (f"No evidence found for: '{_QUERY}' (source=attachment)")


class TestSourceAppliesBeforeTheCap:
    def test_body_passages_ranked_below_six_attachment_passages_are_returned(self, tmp_path):
        path = tmp_path / "cap.db"
        conn = sqlite3.connect(str(path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        _build_schema(conn)
        _insert_message(
            conn,
            message_id="scan@cap.example",
            thread_id="t-cap",
            subject="scans",
            sent_at="2025-09-10T09:00:00+00:00",
            from_=[_COACH],
            to=[_PARENT],
            has_attachments=True,
        )
        for i in range(7):
            _chunk(
                conn,
                f"att-{i}",
                "scan@cap.example",
                "t-cap",
                f"Scanned page {i}.",
                1.0 - i / 100,
                attachment_id=f"scan-{i}",
            )
        _chunk(conn, "cap-body", "scan@cap.example", "t-cap", "Pages attached.", 0.1)
        _finish_threads(conn)
        conn.close()
        db = Database(str(path))

        default = _evidence(db, from_addr="coach@trip.example")
        assert _ids(default, "t-cap") == [f"att-{i}" for i in range(6)]
        out = _evidence(db, from_addr="coach@trip.example", source="body")
        assert _ids(out, "t-cap") == ["cap-body"]
        out = _evidence(db, thread_id="t-cap", source="body")
        assert _ids(out, "t-cap") == ["cap-body"]


class TestScopeEmptiedThreadIsVisible:
    # The thread spans 10-20 September, so this range selects it, but
    # neither of its messages falls inside the range.
    _RANGE = {"date_from": "2025-09-12", "date_to": "2025-09-15"}

    def test_the_thread_stays_listed_with_counts(self, precision_db, caplog):
        with caplog.at_level(logging.INFO):
            out = _evidence(precision_db, scope="in_scope", **self._RANGE)
        EvidenceOutput.model_validate(out.structured_content)
        thread = _thread(out)
        assert thread["chunks"] == []
        assert thread["context_passages_left_out"] == 5
        assert out.structured_content["context_passages_left_out"] == 5
        assert out.structured_content["chunk_count"] == 0
        text = out.content[0].text
        assert "0 chunk(s) from 1 thread(s)." in text
        assert (
            "scope=in_scope left out 5 context passage(s); 1 thread(s) had no in-scope passage."
        ) in text
        assert ("    No in-scope passages: 5 context passage(s) left out (scope=in_scope).") in text
        timing = next(r.getMessage() for r in caplog.records if r.name == "mcp.timings")
        assert "'evidence_filtered': 1" in timing
        assert "'evidence_context_dropped': 5" in timing
        assert "'evidence_threads_scope_emptied': 1" in timing
        assert _MARKER not in caplog.text

    def test_without_scope_the_same_call_returns_the_context(self, precision_db, caplog):
        with caplog.at_level(logging.INFO):
            out = _evidence(precision_db, **self._RANGE)
        assert _ids(out) == _TRIP_ORDER
        assert "context_passages_left_out" not in out.structured_content
        timing = next(r.getMessage() for r in caplog.records if r.name == "mcp.timings")
        assert "evidence_filtered" not in timing


_EXPECTED = {
    "source": "source must be any, body or attachment.",
    "scope": "scope must be any or in_scope.",
    "max_chunks_per_thread": "max_chunks_per_thread must be an integer from 1 to 6.",
    "max_chars_per_chunk": "max_chars_per_chunk must be an integer from 1 to 1600.",
}


class TestValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("source", _MARKER),
            ("source", "Body"),
            ("scope", _MARKER),
            ("scope", "context"),
            ("max_chunks_per_thread", 0),
            ("max_chunks_per_thread", 7),
            ("max_chars_per_chunk", 0),
            ("max_chars_per_chunk", 1601),
        ],
    )
    def test_out_of_range_values_are_rejected_with_fixed_text(
        self, precision_db, caplog, field, value
    ):
        embed = FakeEmbedClient()
        server = FakeMCPServer()
        register_search_tools(server, precision_db, embed)
        with caplog.at_level(logging.INFO), pytest.raises(ToolError) as e:
            asyncio.run(server.tools["get_evidence"](query=_QUERY, **{field: value}))
        # Fixed text naming the field and its range, never the value.
        assert str(e.value) == f"Evidence error: {_EXPECTED[field]}"
        assert embed.embed_calls == []
        assert f"get_evidence rejected invalid {field}" in caplog.text
        assert _MARKER not in caplog.text

    def test_the_largest_valid_values_are_logged(self, precision_db, caplog):
        with caplog.at_level(logging.INFO):
            _evidence(
                precision_db,
                source="body",
                scope="in_scope",
                max_chunks_per_thread=6,
                max_chars_per_chunk=1600,
            )
        call = next(
            r.getMessage() for r in caplog.records if r.getMessage().startswith("tool=get_evidence")
        )
        for fragment in (
            "'source': 'body'",
            "'scope': 'in_scope'",
            "'max_chunks_per_thread': 6",
            "'max_chars_per_chunk': 1600",
        ):
            assert fragment in call
        assert _MARKER not in caplog.text


def _new_db(tmp_path: Path, name: str) -> tuple[sqlite3.Connection, Path]:
    path = tmp_path / name
    conn = sqlite3.connect(str(path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _build_schema(conn)
    return conn, path


class TestReviewRoundOne:
    """Codex review round 1 on PR #1038."""

    def test_scope_considers_passages_ranked_below_the_six_passage_cap(self, tmp_path):
        # Seven context passages rank above the only in-scope one.
        conn, path = _new_db(tmp_path, "crowd.db")
        _insert_message(
            conn,
            message_id="lead@crowd.example",
            thread_id="t-crowd",
            subject="field trip",
            sent_at="2025-09-10T09:00:00+00:00",
            from_=[_COACH],
            to=[_PARENT],
        )
        _insert_message(
            conn,
            message_id="reply@crowd.example",
            thread_id="t-crowd",
            subject="field trip",
            sent_at="2025-09-11T09:00:00+00:00",
            from_=[_VOLUNTEER],
            to=[_PARENT],
        )
        for i in range(7):
            _chunk(
                conn,
                f"reply-{i}",
                "reply@crowd.example",
                "t-crowd",
                f"Reply part {i}. {_MARKER}",
                1.0 - i / 100,
                chunk_index=i,
                char_start=i * 40,
            )
        _chunk(conn, "lead-body", "lead@crowd.example", "t-crowd", "Bus at 8am.", 0.1)
        _finish_threads(conn)
        conn.close()
        db = Database(str(path))

        out = _evidence(db, from_addr="coach@trip.example", scope="in_scope")
        assert _ids(out, "t-crowd") == ["lead-body"]
        assert _thread(out, "t-crowd")["context_passages_left_out"] == 7
        out = _evidence(db, from_addr="coach@trip.example", scope="in_scope", source="body")
        assert _ids(out, "t-crowd") == ["lead-body"]

    def test_the_timing_line_counts_what_the_caps_removed(self, precision_db, caplog):
        with caplog.at_level(logging.INFO):
            _evidence(precision_db, from_addr="coach@trip.example", max_chunks_per_thread=2)
            _evidence(precision_db, from_addr="coach@trip.example", max_chars_per_chunk=22)
            _evidence(precision_db, from_addr="coach@trip.example")
        capped, truncated, default = (
            r.getMessage() for r in caplog.records if r.name == "mcp.timings"
        )
        # t-trip: five passages cut to two; t-note's one passage stays.
        assert "'evidence_chunks_capped': 3" in capped
        # Four passages exceed 22 characters.
        assert "'evidence_chunks_truncated': 4" in truncated
        assert "evidence_chunks_capped" not in default
        assert "evidence_chunks_truncated" not in default
        assert _MARKER not in caplog.text

    def test_the_thread_path_keeps_limit_as_its_default_per_thread_cap(self, tmp_path):
        conn, path = _new_db(tmp_path, "pages.db")
        _insert_message(
            conn,
            message_id="scan@cap.example",
            thread_id="t-cap",
            subject="scans",
            sent_at="2025-09-10T09:00:00+00:00",
            from_=[_COACH],
            to=[_PARENT],
            has_attachments=True,
        )
        for i in range(8):
            _chunk(
                conn,
                f"att-{i}",
                "scan@cap.example",
                "t-cap",
                f"Scanned page {i}.",
                1.0 - i / 100,
                attachment_id=f"scan-{i}",
            )
        _chunk(conn, "cap-body", "scan@cap.example", "t-cap", "Pages attached.", 0.1)
        conn.close()
        db = Database(str(path))

        def count(**kwargs) -> int:
            return len(_ids(_evidence(db, thread_id="t-cap", **kwargs), "t-cap"))

        assert count() == 9
        assert count(source="attachment") == 8
        assert count(limit=3, source="attachment") == 3
        assert count(max_chunks_per_thread=6) == 6

    def test_repeated_invalid_controls_log_one_warning_per_field(self, precision_db, caplog):
        tools = _tools(precision_db)
        with caplog.at_level(logging.INFO):
            for _ in range(3):
                for kwargs in ({"source": _MARKER}, {"max_chunks_per_thread": 9}):
                    with pytest.raises(ToolError):
                        asyncio.run(tools["get_evidence"](query=_QUERY, **kwargs))
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == [
            "get_evidence rejected invalid source",
            "get_evidence rejected invalid max_chunks_per_thread",
        ]
        assert _MARKER not in caplog.text

    def test_a_chunkless_thread_counts_as_having_no_passage_of_the_source(self, tmp_path):
        conn, path = _new_db(tmp_path, "chunkless.db")
        _insert_message(
            conn,
            message_id="lead@trip.example",
            thread_id="t-trip",
            subject="field trip plans",
            sent_at="2025-09-10T09:00:00+00:00",
            from_=[_COACH],
            to=[_PARENT],
        )
        _chunk(conn, "lead-b0", "lead@trip.example", "t-trip", "The bus leaves at 8am.", 1.0)
        _insert_thread(
            conn,
            thread_id="t-empty",
            subject="field trip plans",
            participants=["coach@trip.example", _PARENT],
            senders=["coach@trip.example"],
            message_ids=["empty@trip.example"],
            body_text="field trip plans",
        )
        _finish_threads(conn)
        conn.close()
        db = Database(str(path))

        default = _evidence(db, from_addr="coach@trip.example", max_threads=3)
        assert _ids(default, "t-empty") == []
        out = _evidence(db, from_addr="coach@trip.example", max_threads=3, source="body")
        assert [t["thread_id"] for t in out.structured_content["threads"]] == ["t-trip"]
        assert out.structured_content["threads_without_source_passages"] == 1

    def test_an_unmatched_from_name_still_reports_the_controls(self, precision_db):
        out = _evidence(precision_db, from_name="Nobody Here", source="body", scope="in_scope")
        assert out.structured_content["threads"] == []
        assert out.structured_content["threads_without_source_passages"] == 0
        assert out.structured_content["context_passages_left_out"] == 0
        out = _evidence(precision_db, from_name="Nobody Here")
        assert "threads_without_source_passages" not in out.structured_content
        assert "context_passages_left_out" not in out.structured_content
