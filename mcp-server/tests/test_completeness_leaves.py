"""Leaves answer unknown when the stored content they read is not
known to be complete (#1086).

The indexer records per message whether its stored subject, From / To /
Cc addresses, attachment list and body are complete
(``messages.*_complete``: 1 complete, 0 a known loss, NULL not
assessed). A stored match still decides a leaf; finding nothing decides
it false only when the relevant flag is 1, and is unknown otherwise, so
``query_messages`` leaves the message out of the matches and counts it
as ``indeterminate``. The existing gates (``sender_ambiguous``,
``participant_names_complete``) still apply. All data is synthetic.
"""

import asyncio
import logging
import re

import pytest
from src.lib.predicates import (
    LEAVES,
    Evaluability,
    Leaf,
    compile_leaves,
    query_messages_leaves,
)
from src.lib.sqlite import Database
from src.tools.retrieval import _filter_uses, _indeterminate_causes, register_retrieval_tools

from tests.conftest import _insert_message, set_authority
from tests.test_evidence_scope import _finish_threads
from tests.test_sender_leaf_indeterminate import _truth
from tests.test_sqlite import _open_built_db_conn

JANE = "Jane Roe <jane@one.test>"
BOB = "bob@two.test"
CAROL = "carol@three.test"
DAN = "dan@four.test"
_MARKER = "Zq-completeness-marker-1086"

_ALL = (
    "subject_complete",
    "from_addresses_complete",
    "to_addresses_complete",
    "cc_addresses_complete",
    "attachments_manifest_complete",
    "body_complete",
)

# message_id: (subject, body, From, To, has_attachments, folder,
#              sender_ambiguous, completeness overrides)
_ROWS: dict[str, tuple] = {
    # Everything matches and everything is complete.
    "full": ("Budget plan", "budget approved", JANE, BOB, True, "INBOX", 0, {}),
    # A stored match decides the leaf whatever the flag.
    "s0": ("Budget plan", "budget approved", JANE, BOB, True, "INBOX", 0, dict.fromkeys(_ALL, 0)),
    "sN": (
        "Budget plan",
        "budget approved",
        JANE,
        BOB,
        True,
        "INBOX",
        0,
        dict.fromkeys(_ALL, None),
    ),
    # Nothing matches: false when complete, unknown otherwise.
    "x1": ("Lunch", "lunch soon", CAROL, DAN, False, "INBOX", 0, {}),
    "x0": ("Lunch", "lunch soon", CAROL, DAN, False, "INBOX", 0, dict.fromkeys(_ALL, 0)),
    "xN": ("Lunch", "lunch soon", CAROL, DAN, False, "INBOX", 0, dict.fromkeys(_ALL, None)),
    # One role incomplete: only leaves reading that role are unknown.
    "from0": ("Lunch", "lunch soon", CAROL, DAN, False, "INBOX", 0, {"from_addresses_complete": 0}),
    "to0": ("Lunch", "lunch soon", CAROL, DAN, False, "INBOX", 0, {"to_addresses_complete": 0}),
    "ccN": ("Lunch", "lunch soon", CAROL, DAN, False, "INBOX", 0, {"cc_addresses_complete": None}),
    # A retained From match with an incomplete From list is a match.
    "jfrom0": ("Lunch", "lunch soon", JANE, DAN, False, "INBOX", 0, {"from_addresses_complete": 0}),
    # The sender gate still applies: an ambiguous sender is unknown.
    "amb": ("Lunch", "lunch soon", JANE, DAN, False, "INBOX", 1, {}),
    # A stored attachment decides the attachment leaf either way.
    "att0": (
        "Lunch",
        "lunch soon",
        CAROL,
        DAN,
        True,
        "INBOX",
        0,
        {"attachments_manifest_complete": 0},
    ),
    # Spam stays a decided "no" for authority, complete or not.
    "spam0": ("Lunch", "lunch soon", CAROL, DAN, False, "Spam", 0, {"from_addresses_complete": 0}),
}


@pytest.fixture
def db(tmp_path) -> Database:
    conn, path = _open_built_db_conn(tmp_path, "completeness.db")
    for i, (message_id, row) in enumerate(_ROWS.items()):
        subject, body, from_, to, has_attachments, folder, ambiguous, flags = row
        _insert_message(
            conn,
            message_id=message_id,
            thread_id=f"t-{message_id}",
            sent_at=f"2024-01-{i + 1:02d}T09:00:00+00:00",
            subject=subject,
            body=body,
            from_=[from_],
            to=[to],
            has_attachments=has_attachments,
            folder=folder,
            sender_ambiguous=ambiguous,
            completeness=flags,
        )
    _finish_threads(conn)
    conn.close()
    set_authority(path, "jane@one.test", "counsel", "synthetic")
    return Database(str(path))


_UNKNOWN_FOR_ALL = {"s0": 1, "sN": 1, "x1": 0, "x0": None, "xN": None}
_DECIDED_BY_OTHER_FLAGS = {k: 0 for k in ("from0", "to0", "ccN", "jfrom0", "amb", "att0", "spam0")}

# The expected value of each leaf per message: 1, 0 or None (unknown).
_TRUTH: dict[tuple[str, object], dict[str, int | None]] = {
    ("subject", "budget"): {"full": 1, **_UNKNOWN_FOR_ALL, **_DECIDED_BY_OTHER_FLAGS},
    ("text", ("budget",)): {"full": 1, **_UNKNOWN_FOR_ALL, **_DECIDED_BY_OTHER_FLAGS},
    ("has_attachments", True): {
        "full": 1,
        **_UNKNOWN_FOR_ALL,
        **_DECIDED_BY_OTHER_FLAGS,
        "att0": 1,
    },
    ("has_attachments", False): {
        "full": 0,
        "s0": 0,
        "sN": 0,
        "x1": 1,
        "x0": None,
        "xN": None,
        **{k: 1 for k in _DECIDED_BY_OTHER_FLAGS},
        "att0": 0,
    },
    ("sender", "jane@one.test"): {
        "full": 1,
        **_UNKNOWN_FOR_ALL,
        **_DECIDED_BY_OTHER_FLAGS,
        "from0": None,
        "spam0": None,
        "jfrom0": 1,
        "amb": None,
    },
    ("recipient", "bob@two.test"): {
        "full": 1,
        **_UNKNOWN_FOR_ALL,
        **_DECIDED_BY_OTHER_FLAGS,
        "to0": None,
        "ccN": None,
    },
    ("participant", "jane@one.test"): {
        "full": 1,
        **_UNKNOWN_FOR_ALL,
        **_DECIDED_BY_OTHER_FLAGS,
        "from0": None,
        "to0": None,
        "ccN": None,
        "spam0": None,
        "jfrom0": 1,
        "amb": None,
    },
    ("authority_class", "counsel"): {
        "full": 1,
        **_UNKNOWN_FOR_ALL,
        **_DECIDED_BY_OTHER_FLAGS,
        "from0": None,
        "jfrom0": 1,
        "amb": None,
    },
}
# A name or fragment reads the same flags (plus the names flag, 1 here).
_TRUTH[("sender", "jane")] = _TRUTH[("sender", "jane@one.test")]
_TRUTH[("recipient", "bob")] = _TRUTH[("recipient", "bob@two.test")]
_TRUTH[("participant", "roe")] = _TRUTH[("participant", "jane@one.test")]


def test_every_row_is_in_every_truth_table():
    for truth in _TRUTH.values():
        assert set(truth) == set(_ROWS)


@pytest.mark.parametrize(("name", "value"), sorted(_TRUTH, key=repr))
def test_truth_table(db, name, value):
    assert _truth(db, Leaf(name, value)) == _TRUTH[(name, value)]


@pytest.mark.parametrize(
    "name",
    ["subject", "text", "has_attachments", "sender", "recipient", "participant", "authority_class"],
)
def test_evaluability_is_declared(name):
    assert LEAVES[name].evaluability is Evaluability.UNKNOWN_WHEN_NULL


def test_a_message_complete_in_every_flag_is_decided_by_every_leaf(db):
    """With every flag 1, no leaf reads unknown: the gates are the only
    other sources, and they are open here."""
    for (name, value), truth in _TRUTH.items():
        assert truth["full"] is not None
        assert truth["x1"] is not None, (name, value)


# query_messages parameter for each leaf value above.
_PARAMS = {
    ("subject", "budget"): {"subject": "budget"},
    ("text", ("budget",)): {"text": "budget"},
    ("has_attachments", True): {"has_attachments": True},
    ("has_attachments", False): {"has_attachments": False},
    ("sender", "jane@one.test"): {"sender": "jane@one.test"},
    ("sender", "jane"): {"sender": "jane"},
    ("recipient", "bob@two.test"): {"recipient": "bob@two.test"},
    ("recipient", "bob"): {"recipient": "bob"},
    ("participant", "jane@one.test"): {"participant": "jane@one.test"},
    ("participant", "roe"): {"participant": "roe"},
    ("authority_class", "counsel"): {"authority_class": "counsel"},
}


def test_every_truth_table_has_a_query():
    assert set(_PARAMS) == set(_TRUTH)


@pytest.mark.parametrize("key", sorted(_PARAMS, key=repr))
def test_query_messages_counts_unknowns_before_paging(db, key):
    """``indeterminate`` is the whole predicate's NULL count, taken over
    every message, not the page; a one-row page reports the same."""
    truth = _TRUTH[key]
    page = db.query_messages(**_PARAMS[key], limit=1)
    assert page.total_matches == sum(1 for v in truth.values() if v == 1)
    assert page.indeterminate == sum(1 for v in truth.values() if v is None)
    assert {m.message_id for m in page.messages} <= {k for k, v in truth.items() if v == 1}


def test_an_unknown_leaf_and_a_false_leaf_is_false(db):
    page = db.query_messages(subject="budget", folder="Archive", limit=10)
    assert (page.total_matches, page.indeterminate) == (0, 0)


def test_scope_labels_keep_an_unknown_message_out_of_scope(db):
    labels = db.message_scope([f"t-{m}" for m in _ROWS], from_addr="jane@one.test")
    assert {c.split("#")[0] for c in labels.claimants} == {"full", "s0", "sN", "jfrom0"}


def test_search_emails_thread_filters_are_unchanged(db):
    """``search_emails`` decides sender and participant on the thread
    row, as before; ``authority_class`` reads a message through the
    leaf, so an incomplete From list without a classified address does
    not select its thread."""
    threads = [db.get_thread(f"t-{m}") for m in _ROWS]
    kept = db._apply_filters(threads, from_addr="jane@one.test")
    assert {r.thread_id for r in kept} == {"t-full", "t-s0", "t-sN", "t-jfrom0", "t-amb"}
    kept = db._apply_filters(threads, authority_class="counsel")
    assert {r.thread_id for r in kept} == {"t-full", "t-s0", "t-sN", "t-jfrom0"}


# Every query_messages parameter, with a value that builds its leaves.
_TOOL_ARGS = {
    "sender": "jane",
    "recipient": "bob",
    "participant": "jane",
    "subject": "budget",
    "text": "budget",
    "folder": "INBOX",
    "date_from": "2024-01-01",
    "date_to": "2024-12-31",
    "has_attachments": True,
    "authority_class": "counsel",
    "seen": True,
    "flagged": True,
    "replied": True,
    "size_min": 1,
    "size_max": 10,
}


@pytest.mark.parametrize("param", sorted(_TOOL_ARGS))
def test_every_filter_that_can_be_unknown_names_a_cause(param):
    """The prose names a cause for every filter whose leaves can be
    unknown, judged from the leaves' declared evaluability, not from the
    cause list itself."""
    leaves = query_messages_leaves(
        **{
            k: None
            for k in (
                "sender",
                "recipient",
                "participant",
                "subject",
                "text",
                "folder",
                "date_from",
                "date_to",
                "has_attachments",
                "authority_class",
                "seen",
                "flagged",
                "replied",
            )
        }
        | {param: _TOOL_ARGS[param]}
    )
    can_be_unknown = any(
        LEAVES[leaf.name].evaluability is Evaluability.UNKNOWN_WHEN_NULL for leaf in leaves
    )
    causes = _indeterminate_causes(_filter_uses({param: _TOOL_ARGS[param]}))
    assert bool(causes) == can_be_unknown


class TestTool:
    def _call(self, fake_server, db, **kwargs):
        register_retrieval_tools(fake_server, db)
        return asyncio.run(fake_server.tools["query_messages"](**kwargs))

    @pytest.mark.parametrize(
        ("kwargs", "cause"),
        [
            ({"subject": "budget"}, "subject cut to the stored length, or not yet checked"),
            (
                {"text": "budget"},
                "body not fully indexed (a parse cap, indexing not finished, or reparse pending)",
            ),
            (
                {"has_attachments": True},
                "attachment list incomplete (a parse cap), or not yet checked",
            ),
            (
                {"recipient": "bob@two.test"},
                "address list incomplete (an over-long or unparseable address), or not yet checked",
            ),
        ],
    )
    def test_prose_names_the_cause(self, fake_server, db, kwargs, cause):
        out = self._call(fake_server, db, **kwargs)
        assert out.structured_content["indeterminate"] > 0
        text = out.content[0].text
        assert f"indeterminate: {out.structured_content['indeterminate']} " in text
        assert cause in text
        # A cause the given filter cannot have is not named.
        assert "no stored size" not in text

    def test_the_timing_line_says_why(self, fake_server, db, caplog):
        with caplog.at_level("INFO", logger="mcp.timings"):
            self._call(fake_server, db, text="budget", subject="budget")
        [line] = [r.getMessage() for r in caplog.records if r.name == "mcp.timings"]
        counts = re.search(r"counts=(\{[^}]*\})", line).group(1)
        assert "'indeterminate': 2" in counts
        assert "'indeterminate_cause_body': 1" in counts
        assert "'indeterminate_cause_subject': 1" in counts
        assert "indeterminate_cause_address_list" not in counts

    def test_no_cause_on_the_timing_line_when_every_message_is_decided(
        self, fake_server, db, caplog
    ):
        with caplog.at_level("INFO", logger="mcp.timings"):
            self._call(fake_server, db, subject="budget", folder="Archive")
        [line] = [r.getMessage() for r in caplog.records if r.name == "mcp.timings"]
        assert "'indeterminate': 0" in line
        assert "indeterminate_cause" not in line

    def test_no_marker_reaches_the_log(self, fake_server, db, caplog):
        caplog.set_level(logging.DEBUG)
        self._call(fake_server, db, subject=_MARKER, text=_MARKER, recipient=_MARKER)
        assert _MARKER not in caplog.text


def test_compiled_sql_binds_every_value(db):
    """The new CASE wrappers add no inlined values: every value is a
    bound parameter."""
    for name, value in _TRUTH:
        sql, params = compile_leaves([Leaf(name, value)])
        assert "jane" not in sql and "budget" not in sql and "bob" not in sql
        assert params
