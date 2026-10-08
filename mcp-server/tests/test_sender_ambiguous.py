"""``messages.sender_ambiguous`` in the MCP reader (#1144).

The indexer records 0 for a message with one From header, 1 when its
sender attribution is unsafe (a repeated From, or a header scan cut
short) and leaves NULL for a message it has not assessed yet. Only 0
qualifies for source authority; the value (true / false / null) is
shown on message rows, citations and the sender attribution of
intelligence prompts. All data is synthetic.
"""

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from src.lib.sqlite import ChunkResult, Database
from src.tools.intelligence import EvidenceRef, _chunk_header, _citation, _citation_lines
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools

from tests.conftest import claimant_of, set_authority

_QUERY = [1.0, 0.0, 0.0, 0.0]


def _set_flag(db: Database, where: str, value: int | None) -> None:
    with closing(sqlite3.connect(db.path)) as conn:
        conn.execute(f"UPDATE messages SET sender_ambiguous = ? WHERE {where}", (value,))  # nosec B608
        conn.commit()


@pytest.fixture
def counsel_messages_db(messages_db: Database) -> Database:
    # jane@example.com sent m1, m3 and m5.
    set_authority(messages_db.path, "jane@example.com", "counsel", "domain:example.com")
    return messages_db


@pytest.fixture
def counsel_seeded_db(seeded_db: Database) -> Database:
    # alice sent t-alpha (matching "invoice", vector [1, 0, 0, 0]).
    set_authority(seeded_db.path, "alice@example.com", "counsel", "address:alice@example.com")
    return seeded_db


def _ids(page) -> list[str]:
    return [m.message_id for m in page.messages]


class TestAuthority:
    """Only ``sender_ambiguous = 0`` qualifies, at every caller."""

    @pytest.mark.parametrize("value", [1, None])
    def test_query_messages_excludes_unsafe_and_unassessed(self, counsel_messages_db, value):
        assert _ids(counsel_messages_db.query_messages(authority_class="counsel")) == [
            "m5",
            "m3",
            "m1",
        ]
        _set_flag(counsel_messages_db, "message_id = 'm3'", value)
        page = counsel_messages_db.query_messages(authority_class="counsel")
        assert _ids(page) == ["m5", "m1"]
        assert page.total_matches == 2
        # The unclassified side does not gain it either: the leaf is a
        # positive class test, not a complement.
        assert "m3" not in _ids(counsel_messages_db.query_messages(authority_class="unclassified"))
        # The from rows are kept: the sender filter still finds it.
        assert "m3" in _ids(counsel_messages_db.query_messages(sender="jane@example.com"))

    @pytest.mark.parametrize("value", [1, None])
    def test_every_search_path_excludes_unsafe_and_unassessed(self, counsel_seeded_db, value):
        db = counsel_seeded_db
        assert [r.thread_id for r in db.keyword_search("invoice", authority_class="counsel")] == [
            "t-alpha"
        ]
        _set_flag(db, "thread_id = 't-alpha'", value)
        assert db.keyword_search("invoice", authority_class="counsel") == []
        assert (
            db.hybrid_search(
                query_text="invoice", query_embedding=_QUERY, authority_class="counsel", limit=10
            )
            == []
        )
        assert db.semantic_search(query_embedding=_QUERY, authority_class="counsel", limit=10) == []

    def test_search_emails_tool_excludes_an_ambiguous_sender(self, fake_server, counsel_seeded_db):
        register_search_tools(fake_server, counsel_seeded_db, embed_client=None)
        handler = fake_server.tools["search_emails"]
        before = asyncio.run(handler(query="invoice", authority_class="counsel", mode="keyword"))
        assert [t["thread_id"] for t in before.structured_content["results"]] == ["t-alpha"]
        _set_flag(counsel_seeded_db, "thread_id = 't-alpha'", 1)
        after = asyncio.run(handler(query="invoice", authority_class="counsel", mode="keyword"))
        assert after.structured_content["results"] == []


class TestMessageRows:
    @pytest.mark.parametrize(
        "value, expected, prose",
        [
            (0, False, None),
            (1, True, "Sender: ambiguous (sender attribution unsafe)"),
            (None, None, "Sender: not yet checked"),
        ],
    )
    def test_get_message_shows_the_flag(self, fake_server, messages_db, value, expected, prose):
        _set_flag(messages_db, "message_id = 'm1'", value)
        register_retrieval_tools(fake_server, messages_db)
        out = asyncio.run(fake_server.tools["get_message"](message_id=claimant_of("m1")))
        assert out.structured_content["message"]["sender_ambiguous"] is expected
        text = out.content[0].text
        if prose is None:
            assert "Sender:" not in text
        else:
            assert prose in text

    def test_query_messages_rows_carry_the_flag(self, fake_server, messages_db):
        _set_flag(messages_db, "message_id = 'm3'", 1)
        _set_flag(messages_db, "message_id = 'm5'", None)
        register_retrieval_tools(fake_server, messages_db)
        out = asyncio.run(fake_server.tools["query_messages"](sender="jane@example.com"))
        rows = {m["message_id"]: m["sender_ambiguous"] for m in out.structured_content["messages"]}
        assert rows == {"m1": False, "m3": True, "m5": None}
        text = out.content[0].text
        assert "sender ambiguous" in text
        assert "sender not yet checked" in text

    def test_the_flag_can_be_projected(self, fake_server, messages_db):
        register_retrieval_tools(fake_server, messages_db)
        out = asyncio.run(
            fake_server.tools["query_messages"](
                sender="jane@example.com", fields=["sender_ambiguous"]
            )
        )
        rows = out.structured_content["messages"]
        assert len(rows) == 3
        for row in rows:
            assert set(row) == {"claimant_id", "thread_id", "sender_ambiguous"}


class TestPassages:
    @pytest.mark.parametrize("value, expected", [(0, False), (1, True), (None, None)])
    def test_chunks_carry_the_flag_on_both_query_paths(self, messages_db, value, expected):
        _set_flag(messages_db, "message_id = 'm1'", value)
        by_thread = messages_db.get_evidence_chunks_for_threads(["t1"], _QUERY)["t1"]
        [m1] = [c for c in by_thread if c.message_id == "m1"]
        assert m1.message_sender_ambiguous is expected
        recent = messages_db.get_recent_chunks_for_thread("t1", 10)
        [m1] = [c for c in recent if c.message_id == "m1"]
        assert m1.message_sender_ambiguous is expected

    @staticmethod
    def _chunk(value: bool | None) -> ChunkResult:
        return ChunkResult(
            chunk_id="c1",
            message_id="m@example.test",
            claimant_id="m@example.test#0011223344556677",
            thread_id="t",
            chunk_index=0,
            text="Pay 900 units.",
            char_start=0,
            char_end=14,
            message_sender="SYNTHETIC_SENDER_MARKER <first@example.test>",
            message_sender_ambiguous=value,
            message_date="2024-03-04T10:30:00+00:00",
        )

    @pytest.mark.parametrize(
        "value, note",
        [
            (False, ""),
            (True, " (unverified: sender attribution unsafe)"),
            (None, " (unverified: sender not yet checked)"),
        ],
    )
    def test_prompt_attribution_and_citation(self, value, note):
        chunk = self._chunk(value)
        header = _chunk_header(chunk, chunk.char_end, label="E1")
        assert f"| from SYNTHETIC_SENDER_MARKER <first@example.test>{note} | sent" in header
        citation = _citation(EvidenceRef(label="E1", thread_id="t", chunk=chunk, char_end=14))
        assert citation.sender_ambiguous is value
        [_, line] = _citation_lines([citation])
        assert f"SYNTHETIC_SENDER_MARKER <first@example.test>{note}, 2024-03-04" in line

    def test_the_note_survives_a_long_sender_in_the_short_header(self):
        """A sender-controlled name cannot push the fixed note out of the
        bounded header: the name gives up its room instead."""
        from src.tools.intelligence import _LABELLED_HEADER_MAX_CHARS

        chunk = self._chunk(True)
        chunk.message_sender = "S" * 100_000
        chunk.claimant_id = "x" * 5_000 + "#0011223344556677"
        chunk.attachment_id = "att"
        chunk.attachment_filename = "f" * 10_000
        chunk.attachment_mime = "m" * 10_000
        header = _chunk_header(chunk, chunk.char_end, label="E1")
        assert len(header) <= _LABELLED_HEADER_MAX_CHARS
        assert "… (unverified: sender attribution unsafe) | sent" in header

    def test_thread_text_citation_has_no_flag(self):
        citation = _citation(EvidenceRef(label="E1", thread_id="t", chunk=None, char_end=None))
        assert citation.sender_ambiguous is None


def test_fixture_dates_are_utc():
    # Guards the parametrized dates above against a local-time reading.
    assert datetime.fromisoformat("2024-03-04T10:30:00+00:00").tzinfo == UTC


# Both causes the indexer stores as 1 (#1144): a repeated From, and a
# header scan stopped at its field cap with no second From seen. The
# stored value cannot tell them apart, so every label for 1 is neutral.
_CAUSES = ["repeated_from", "scan_capped"]


@pytest.mark.parametrize("cause", _CAUSES)
def test_both_causes_get_the_reason_neutral_label(fake_server, messages_db, cause):
    from src.tools.intelligence import sender_check

    _set_flag(messages_db, "message_id = 'm1'", 1)
    register_retrieval_tools(fake_server, messages_db)
    out = asyncio.run(fake_server.tools["get_message"](message_id=claimant_of("m1")))
    labels = [out.content[0].text, sender_check(True)]
    for label in labels:
        assert "sender attribution unsafe" in label
        assert "repeated" not in label
