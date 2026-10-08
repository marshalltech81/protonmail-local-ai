"""The ``sender`` and ``participant`` leaves answer unknown for a
message whose sender attribution is not known safe (#1153).

``messages.sender_ambiguous`` is 0 (one From header), 1 (a repeated
From, or a header scan cut short) or NULL (not assessed yet, #1144).
The From role decides ``sender`` only when the flag is 0; otherwise
the leaf is unknown, so ``query_messages`` leaves the message out of
the matches and counts it as ``indeterminate``. ``participant`` is the
recipient match OR that sender expression (SQL three-valued OR): a
recipient match decides it whatever the flag. ``recipient`` does not
read ``sender_ambiguous``. All data is synthetic.
"""

import asyncio
import logging
import sqlite3
from contextlib import closing

import pytest
from src.lib.predicates import LEAVES, Evaluability, Leaf, compile_leaves
from src.lib.sqlite import Database
from src.tools.retrieval import register_retrieval_tools

from tests.conftest import _insert_message, claimant_of
from tests.test_evidence_scope import _finish_threads
from tests.test_sqlite import _open_built_db_conn

JANE = "Jane Roe <jane@one.test>"
BOB = "bob@two.test"
CAROL = "carol@three.test"
_MARKER = "Zq-sender-marker-1153"

# (message_id, thread_id, From, To, sender_ambiguous)
_ROWS = [
    ("a0", "t-a", JANE, BOB, 0),
    ("a1", "t-a", JANE, BOB, 1),
    ("aN", "t-n", JANE, BOB, None),
    ("b0", "t-b", BOB, JANE, 0),
    ("b1", "t-b", BOB, JANE, 1),
    ("bN", "t-b", BOB, JANE, None),
    ("c0", "t-c", CAROL, BOB, 0),
    ("c1", "t-c", CAROL, BOB, 1),
]


@pytest.fixture
def flags_db(tmp_path) -> Database:
    conn, path = _open_built_db_conn(tmp_path, "sender-flags.db")
    for i, (message_id, thread_id, from_, to, flag) in enumerate(_ROWS):
        _insert_message(
            conn,
            message_id=message_id,
            thread_id=thread_id,
            sent_at=f"2024-01-{i + 1:02d}T09:00:00+00:00",
            from_=[from_],
            to=[to],
            sender_ambiguous=flag,
        )
    _finish_threads(conn)
    conn.close()
    return Database(str(path))


def _truth(db: Database, leaf: Leaf) -> dict[str, int | None]:
    """Each message's value of ``leaf``: 1, 0 or None (unknown)."""
    sql, params = compile_leaves([leaf])
    with closing(db._connect()) as conn:
        rows = conn.execute(
            f"SELECT m.message_id, ({sql}) FROM messages m",  # nosec B608
            params,
        ).fetchall()
    return {r[0]: (None if r[1] is None else int(bool(r[1]))) for r in rows}


# The expected value of each leaf per message, for an exact address
# and a name fragment of the same person.
_SENDER_TRUTH = {
    "a0": 1,
    "a1": None,
    "aN": None,
    # Ambiguous or unassessed: unknown even when the stored From does
    # not carry the value, since the author cannot be told.
    "b0": 0,
    "b1": None,
    "bN": None,
    "c0": 0,
    "c1": None,
}
_PARTICIPANT_TRUTH = {
    "a0": 1,
    "a1": None,
    "aN": None,
    # A recipient match decides it, whatever the flag.
    "b0": 1,
    "b1": 1,
    "bN": 1,
    "c0": 0,
    "c1": None,
}
_RECIPIENT_TRUTH = {"a0": 0, "a1": 0, "aN": 0, "b0": 1, "b1": 1, "bN": 1, "c0": 0, "c1": 0}


@pytest.mark.parametrize("value", ["jane@one.test", "Jane Roe <jane@one.test>", "jane", "ROE"])
class TestTruthTable:
    def test_sender(self, flags_db, value):
        assert _truth(flags_db, Leaf("sender", value)) == _SENDER_TRUTH

    def test_participant(self, flags_db, value):
        assert _truth(flags_db, Leaf("participant", value)) == _PARTICIPANT_TRUTH

    def test_recipient_is_unchanged(self, flags_db, value):
        assert _truth(flags_db, Leaf("recipient", value)) == _RECIPIENT_TRUTH


def test_evaluability_is_declared():
    assert LEAVES["sender"].evaluability is Evaluability.UNKNOWN_WHEN_NULL
    assert LEAVES["participant"].evaluability is Evaluability.UNKNOWN_WHEN_NULL
    # A substring recipient can be unknown too (#1140).
    assert LEAVES["recipient"].evaluability is Evaluability.UNKNOWN_WHEN_NULL


def _ids(page) -> set[str]:
    return {m.message_id for m in page.messages}


class TestQueryMessages:
    @pytest.mark.parametrize(
        ("filters", "matches", "indeterminate"),
        [
            ({"sender": "jane@one.test"}, {"a0"}, 5),
            ({"sender": "jane"}, {"a0"}, 5),
            ({"participant": "jane@one.test"}, {"a0", "b0", "b1", "bN"}, 3),
            ({"participant": "jane"}, {"a0", "b0", "b1", "bN"}, 3),
            ({"recipient": "jane@one.test"}, {"b0", "b1", "bN"}, 0),
            # An unknown sender leaf AND a false leaf is false (SQL
            # three-valued AND): nothing is indeterminate.
            ({"sender": "jane@one.test", "folder": "Archive"}, set(), 0),
            # A sender nobody has: every not-safe message is unknown.
            ({"sender": "nobody@four.test"}, set(), 5),
        ],
    )
    def test_matches_and_counts(self, flags_db, filters, matches, indeterminate):
        page = flags_db.query_messages(**filters, limit=100)
        assert _ids(page) == matches
        assert page.total_matches == len(matches)
        assert page.indeterminate == indeterminate

    def test_cursor_walks_only_definite_matches_with_a_stable_count(self, flags_db):
        seen: list[str] = []
        page = flags_db.query_messages(participant="jane@one.test", limit=1)
        while True:
            assert page.indeterminate == 3
            assert page.total_matches == 4
            seen += [m.message_id for m in page.messages]
            if not page.has_more:
                break
            page = flags_db.query_messages(
                participant="jane@one.test", limit=1, cursor=page.next_cursor
            )
        assert sorted(seen) == ["a0", "b0", "b1", "bN"]

    def test_matched_addresses_come_from_definite_matches(self, flags_db):
        page = flags_db.query_messages(sender="jane", limit=100)
        assert page.address_matches["sender"].addresses == ["jane@one.test"]
        page = flags_db.query_messages(sender="carol", limit=100)
        assert page.address_matches["sender"].addresses == ["carol@three.test"]
        assert page.indeterminate == 5


class TestTool:
    def _call(self, fake_server, db, **kwargs):
        register_retrieval_tools(fake_server, db)
        return asyncio.run(fake_server.tools["query_messages"](**kwargs))

    def test_prose_states_the_sender_cause(self, fake_server, flags_db):
        out = self._call(fake_server, flags_db, sender="jane@one.test")
        assert out.structured_content["indeterminate"] == 5
        assert out.structured_content["total_matches"] == 1
        text = out.content[0].text
        assert "indeterminate: 5 " in text
        assert "sender ambiguous or not yet checked" in text
        # Only the causes of the filters given are named.
        assert "no stored size" not in text

    def test_prose_names_each_cause_the_filters_can_have(self, fake_server, flags_db):
        out = self._call(fake_server, flags_db, participant="jane", size_min=1)
        assert out.structured_content["indeterminate"] == 3
        # A name filter can also be undecided by its names (#1140).
        assert (
            "sender ambiguous or not yet checked; address list incomplete (an over-long or "
            "unparseable address), or not yet checked; display names not all indexed "
            "(reparse pending, or over the name budget); no stored size;"
        ) in out.content[0].text

    def test_a_size_bound_alone_names_only_its_cause(self, fake_server, flags_db):
        with closing(sqlite3.connect(flags_db.path)) as conn:
            conn.execute("UPDATE messages SET size_bytes = NULL WHERE message_id = 'c0'")
            conn.commit()
        out = self._call(fake_server, flags_db, size_min=1)
        assert out.structured_content["indeterminate"] == 1
        text = out.content[0].text
        assert "accept nor reject: no stored size;" in text
        assert "sender ambiguous or not yet checked" not in text

    def test_empty_page_is_not_a_known_miss(self, fake_server, flags_db):
        out = self._call(fake_server, flags_db, sender="nobody@four.test")
        text = out.content[0].text
        assert "No messages are known to match." in text
        assert "No messages match." not in text

    def test_no_marker_reaches_the_log(self, fake_server, flags_db, caplog):
        caplog.set_level(logging.DEBUG)
        self._call(fake_server, flags_db, sender=_MARKER, participant=_MARKER)
        assert _MARKER not in caplog.text


class TestScopeLabels:
    """``message_scope`` labels an unknown message context, never in
    scope, and a thread holding one is not wholly in scope."""

    def test_sender(self, flags_db):
        labels = flags_db.message_scope(["t-a", "t-n", "t-b"], from_addr="jane@one.test")
        assert labels.claimants == {claimant_of("a0")}
        assert labels.whole_threads == set()

    def test_participant(self, flags_db):
        labels = flags_db.message_scope(["t-a", "t-n", "t-b"], participant="jane@one.test")
        assert labels.claimants == {claimant_of(m) for m in ("a0", "b0", "b1", "bN")}
        assert labels.whole_threads == {"t-b"}


def test_search_emails_thread_filters_are_unchanged(flags_db):
    """Out of scope (#1153): the thread filters still match the thread's
    recorded senders and participants."""
    threads = [flags_db.get_thread(t) for t in ("t-a", "t-n", "t-b", "t-c")]
    kept = flags_db._apply_filters(threads, from_addr="jane@one.test")
    assert [r.thread_id for r in kept] == ["t-a", "t-n"]
    kept = flags_db._apply_filters(threads, participant="jane@one.test")
    assert [r.thread_id for r in kept] == ["t-a", "t-n", "t-b"]
