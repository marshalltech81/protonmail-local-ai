"""The ``authority_class`` leaf answers unknown for a message whose
sender attribution is not known safe (#1161).

``messages.sender_ambiguous`` is 0 (one From header), 1 (a repeated
From, or a header scan cut short) or NULL (not assessed yet, #1144).
The leaf is:

- 0 for a message in ``AUTHORITY_EXCLUDED_FOLDERS`` (Spam), whatever
  the flag: Spam stays a decided "no" (owner, 2026-10-08);
- otherwise, with the flag 0, whether its From sender carries the
  requested class (no From participant, or another class, is 0;
  ``unclassified`` matches as a stored class);
- otherwise unknown, so ``query_messages`` leaves the message out of the
  matches and counts it as ``indeterminate``.

``search_emails`` keeps a thread with a message whose leaf is true, so
its selections are unchanged. All data is synthetic.
"""

import asyncio
import itertools
import logging
from contextlib import closing

import pytest
from src.lib.predicates import LEAVES, Evaluability, Leaf, compile_leaves
from src.lib.sqlite import Database
from src.tools.retrieval import register_retrieval_tools

from tests.conftest import _insert_message, set_authority
from tests.test_evidence_scope import _finish_threads
from tests.test_sqlite import _open_built_db_conn

JANE = "jane@one.test"  # counsel
CAROL = "carol@three.test"  # unclassified (no rule matched)
_SENDERS = {"jane": JANE, "carol": CAROL, "none": None}
_CLASS = {"jane": "counsel", "carol": "unclassified", "none": None}
_FLAGS = {"0": 0, "1": 1, "N": None}
_FOLDERS = ("INBOX", "Spam")
_REQUESTED = ("counsel", "unclassified", "vendor")
_MARKER = "Zq-authority-marker-1161"


def _mid(sender: str, flag: str, folder: str) -> str:
    return f"{sender}-{flag}-{folder}"


# One message per (sender, flag, folder), each in its own thread so a
# thread's selection is its message's.
_CASES = list(itertools.product(_SENDERS, _FLAGS, _FOLDERS))


@pytest.fixture
def authority_db(tmp_path) -> Database:
    conn, path = _open_built_db_conn(tmp_path, "authority-flags.db")
    for i, (sender, flag, folder) in enumerate(_CASES):
        mid = _mid(sender, flag, folder)
        address = _SENDERS[sender]
        _insert_message(
            conn,
            message_id=mid,
            thread_id=f"t-{mid}",
            folder=folder,
            sent_at=f"2024-01-{i + 1:02d}T09:00:00+00:00",
            from_=[address] if address else None,
            to=["bob@two.test"],
            sender_ambiguous=_FLAGS[flag],
        )
    _finish_threads(conn)
    conn.commit()
    conn.close()
    set_authority(path, JANE, "counsel", "address:jane@one.test")
    return Database(str(path))


def _expected(sender: str, flag: str, folder: str, requested: str) -> int | None:
    if folder == "Spam":
        return 0
    if _FLAGS[flag] != 0:
        return None
    return int(_CLASS[sender] == requested)


def _truth(db: Database, leaf: Leaf) -> dict[str, int | None]:
    """Each message's value of ``leaf``: 1, 0 or None (unknown)."""
    sql, params = compile_leaves([leaf])
    with closing(db._connect()) as conn:
        rows = conn.execute(
            f"SELECT m.message_id, ({sql}) FROM messages m",  # nosec B608
            params,
        ).fetchall()
    return {r[0]: (None if r[1] is None else int(bool(r[1]))) for r in rows}


@pytest.mark.parametrize("requested", _REQUESTED)
def test_truth_table(authority_db, requested):
    """The cross product of flag, Spam, the sender's class and the
    requested class."""
    expected = {_mid(*case): _expected(*case, requested) for case in _CASES}
    assert _truth(authority_db, Leaf("authority_class", requested)) == expected


def test_evaluability_is_declared():
    assert LEAVES["authority_class"].evaluability is Evaluability.UNKNOWN_WHEN_NULL


def _ids(page) -> set[str]:
    return {m.message_id for m in page.messages}


# Outside Spam, every message whose flag is 1 or NULL: 3 senders x 2.
_UNDECIDED = 6


class TestQueryMessages:
    @pytest.mark.parametrize(
        ("filters", "matches", "indeterminate"),
        [
            ({"authority_class": "counsel"}, {"jane-0-INBOX"}, _UNDECIDED),
            ({"authority_class": "unclassified"}, {"carol-0-INBOX"}, _UNDECIDED),
            ({"authority_class": "vendor"}, set(), _UNDECIDED),
            # Spam is a decided "no", whatever the flag.
            ({"authority_class": "counsel", "folder": "Spam"}, set(), 0),
            # An unknown leaf AND a false leaf is false.
            ({"authority_class": "counsel", "folder": "Archive"}, set(), 0),
            # Two leaves unknown on the same messages count them once.
            (
                {"authority_class": "counsel", "sender": JANE},
                {"jane-0-INBOX"},
                _UNDECIDED,
            ),
        ],
    )
    def test_matches_and_counts(self, authority_db, filters, matches, indeterminate):
        page = authority_db.query_messages(**filters, limit=100)
        assert _ids(page) == matches
        assert page.total_matches == len(matches)
        assert page.indeterminate == indeterminate


class TestTool:
    def _call(self, fake_server, db, **kwargs):
        register_retrieval_tools(fake_server, db)
        return asyncio.run(fake_server.tools["query_messages"](**kwargs))

    def test_prose_states_the_sender_cause(self, fake_server, authority_db):
        out = self._call(fake_server, authority_db, authority_class="counsel")
        assert out.structured_content["indeterminate"] == _UNDECIDED
        assert out.structured_content["total_matches"] == 1
        text = out.content[0].text
        assert f"indeterminate: {_UNDECIDED} " in text
        assert "accept nor reject: sender ambiguous or not yet checked;" in text
        assert "no stored size" not in text

    def test_empty_page_is_not_a_known_miss(self, fake_server, authority_db):
        out = self._call(fake_server, authority_db, authority_class="vendor")
        text = out.content[0].text
        assert "No messages are known to match." in text
        assert "No messages match." not in text

    def test_no_marker_reaches_the_log(self, fake_server, authority_db, caplog):
        caplog.set_level(logging.DEBUG)
        self._call(fake_server, authority_db, authority_class="counsel", subject=_MARKER)
        assert _MARKER not in caplog.text


@pytest.mark.parametrize("requested", _REQUESTED)
def test_search_emails_thread_selection_is_unchanged(authority_db, requested):
    """``_threads_with_message`` selects with ``AND <leaf>``, so an
    unknown leaf selects nothing, as the decided 0 did before: the
    threads kept are exactly those with a safe, non-Spam message whose
    sender carries the class."""
    threads = [f"t-{_mid(*case)}" for case in _CASES]
    found = authority_db._threads_with_message(threads, Leaf("authority_class", requested))
    expected = {
        f"t-{_mid(sender, flag, folder)}"
        for sender, flag, folder in _CASES
        if folder != "Spam" and _FLAGS[flag] == 0 and _CLASS[sender] == requested
    }
    assert found == expected
