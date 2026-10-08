"""The predicate layer behind ``query_messages``, ``message_scope`` and
``search_emails``' post-fusion filters (#1084).

``TestPinnedFilterSemantics`` was written and run against ``main``
before the predicate module existed, so every filter's result at each
of the three call sites is pinned before the rewrite.
"""

import json
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from src.lib.predicates import (
    DATE_BASES,
    LEAVES,
    Evaluability,
    Leaf,
    compile_leaves,
    leaf_digest,
    message_scope_leaves,
    query_messages_leaves,
    search_emails_leaves,
)
from src.lib.security import _LOGGABLE_TOOL_PARAMS, log_tool_call
from src.lib.sqlite import Database, InvalidFilterError, MessageRecord, _record_clock

from tests.conftest import _insert_message, claimant_of, set_authority
from tests.test_evidence_scope import _finish_threads
from tests.test_sqlite import _open_built_db_conn

# A synthetic value no fixture row contains: the tests assert it never
# reaches the log through any adapter.
_MARKER = "Zq-private-marker-7731"


@pytest.fixture
def mixed_db(tmp_path) -> Database:
    """Three threads whose messages differ in every filtered field.

    ``t-mix`` holds ``ma`` (alice, January, INBOX, read) and ``mb``
    (bob, March, Archive, flagged, with an attachment), so one message
    satisfies a sender filter and another a date filter. ``t-solo`` is
    ``mc`` (carol, sent in February but delivered on the 20th), ``t-trash``
    is ``md`` (alice, in Trash) and ``t-spam`` is ``me`` (alice, in Spam).
    alice carries the ``counsel`` class.
    """
    conn, path = _open_built_db_conn(tmp_path, "predicates.db")
    _insert_message(
        conn,
        message_id="ma",
        thread_id="t-mix",
        sent_at="2024-01-10T09:00:00+00:00",
        subject="Budget Plan",
        from_=["Alice Example <alice@one.example>"],
        to=["Bob <bob@two.example>"],
        body="the budget plan is approved",
        seen=True,
    )
    _insert_message(
        conn,
        message_id="mb",
        thread_id="t-mix",
        sent_at="2024-03-10T09:00:00+00:00",
        subject="Re: Budget Plan",
        folder="Archive",
        from_=["bob@two.example"],
        to=["alice@one.example"],
        cc=["Carol <carol@three.example>"],
        has_attachments=True,
        body="attached totals",
        attachment_text="spreadsheet totals",
        flagged=True,
    )
    _insert_message(
        conn,
        message_id="mc",
        thread_id="t-solo",
        sent_at="2024-02-10T09:00:00+00:00",
        occurred_at="2024-02-20T09:00:00+00:00",
        subject="Straße notice",
        from_=["carol@three.example"],
        to=["alice@one.example"],
        body="notice about the street",
    )
    _insert_message(
        conn,
        message_id="md",
        thread_id="t-trash",
        sent_at="2024-04-01T09:00:00+00:00",
        subject="old",
        folder="Trash",
        from_=["alice@one.example"],
        to=["bob@two.example"],
        body="old news",
    )
    _insert_message(
        conn,
        message_id="me",
        thread_id="t-spam",
        sent_at="2024-05-01T09:00:00+00:00",
        subject="offer",
        folder="Spam",
        from_=["alice@one.example"],
        to=["bob@two.example"],
        body="offer",
    )
    _finish_threads(conn)
    conn.execute("UPDATE threads SET has_attachments = 1 WHERE thread_id = 't-mix'")
    conn.commit()
    conn.close()
    set_authority(path, "alice@one.example", "counsel", "address:alice@one.example")
    return Database(str(path))


def _ids(page) -> list[str]:
    return [m.message_id for m in page.messages]


def _scope(db: Database, **filters) -> set[str]:
    labels = db.message_scope(["t-mix", "t-solo", "t-trash"], **filters)
    return {c.split("#", 1)[0] for c in labels.claimants}


def _threads(db: Database) -> list:
    return [db.get_thread(t) for t in ("t-mix", "t-solo", "t-trash")]


def _filtered(db: Database, **filters) -> list[str]:
    return [r.thread_id for r in db._apply_filters(_threads(db), **filters)]


class TestPinnedFilterSemantics:
    """What each call site returns for each filter today (pinned on
    ``main`` before #1084)."""

    @pytest.mark.parametrize(
        ("filters", "expected"),
        [
            ({}, ["me", "mb", "mc", "ma"]),
            ({"sender": "alice@one.example"}, ["me", "ma"]),
            ({"sender": "ALICE@ONE.EXAMPLE"}, ["me", "ma"]),
            ({"sender": " alice@one.example "}, ["me", "ma"]),
            ({"sender": "Alice"}, ["me", "ma"]),
            ({"sender": "@one.example"}, ["me", "ma"]),
            ({"sender": "  "}, ["me", "mb", "mc", "ma"]),
            ({"recipient": "alice@one.example"}, ["mb", "mc"]),
            ({"recipient": "carol@three.example"}, ["mb"]),
            ({"participant": "carol@three.example"}, ["mb", "mc"]),
            ({"participant": "CAROL"}, ["mb", "mc"]),
            ({"subject": "budget plan"}, ["mb", "ma"]),
            ({"subject": "STRASSE"}, ["mc"]),
            ({"text": "approved budget"}, ["ma"]),
            ({"text": "totals"}, ["mb"]),
            ({"text": "spreadsheet"}, []),
            ({"folder": "Trash"}, ["md"]),
            ({"folder": "Archive"}, ["mb"]),
            ({"date_from": "2024-02-15"}, ["me", "mb", "mc"]),
            ({"date_to": "2024-02-15"}, ["ma"]),
            ({"date_from": "2024-02-20T09:00:00+00:00"}, ["me", "mb", "mc"]),
            ({"has_attachments": True}, ["mb"]),
            ({"has_attachments": False}, ["me", "mc", "ma"]),
            ({"seen": True}, ["ma"]),
            ({"seen": False}, ["me", "mb", "mc"]),
            ({"flagged": True}, ["mb"]),
            ({"authority_class": "counsel"}, ["ma"]),
            ({"authority_class": "counsel", "folder": "Trash"}, ["md"]),
            ({"authority_class": "unclassified"}, ["mb", "mc"]),
            ({"authority_class": " "}, ["me", "mb", "mc", "ma"]),
            ({"sender": "alice@one.example", "date_from": "2024-03-01"}, ["me"]),
            (
                {"sender": "alice@one.example", "date_from": "2024-03-01", "date_to": "2024-03-31"},
                [],
            ),
        ],
    )
    def test_query_messages(self, mixed_db, filters, expected):
        page = mixed_db.query_messages(**filters)
        assert _ids(page) == expected
        assert page.total_matches == len(expected)

    def test_query_messages_reports_matched_addresses(self, mixed_db):
        page = mixed_db.query_messages(sender="alice", recipient="bob@two.example")
        assert page.address_matches["sender"].addresses == ["alice@one.example"]
        assert page.address_matches["sender"].distinct == 1
        assert page.address_matches["recipient"].addresses == ["bob@two.example"]
        assert set(page.address_matches) == {"sender", "recipient"}

    @pytest.mark.parametrize(
        ("filters", "field"),
        [
            ({"text": "..."}, "text"),
            ({"text": " ".join(f"w{i}" for i in range(17))}, "text"),
            ({"authority_class": "boss"}, "authority_class"),
            ({"date_from": "not-a-date"}, "date_from"),
            ({"date_from": "2024-02-02", "date_to": "2024-02-01"}, "date_from/date_to"),
        ],
    )
    def test_query_messages_rejections(self, mixed_db, filters, field):
        with pytest.raises(InvalidFilterError) as info:
            mixed_db.query_messages(**filters)
        assert info.value.field_name == field

    def test_cursor_walks_and_is_bound_to_its_filters(self, mixed_db):
        first = mixed_db.query_messages(sender="alice@one.example", limit=1)
        assert _ids(first) == ["me"] and first.has_more
        second = mixed_db.query_messages(
            sender="alice@one.example", limit=1, cursor=first.next_cursor
        )
        assert _ids(second) == ["ma"] and second.offset == 1 and not second.has_more
        # Padding is stripped before the digest; case is not, so another
        # spelling of the same address is a foreign cursor.
        assert _ids(
            mixed_db.query_messages(sender=" alice@one.example ", cursor=first.next_cursor)
        ) == ["ma"]
        with pytest.raises(InvalidFilterError, match="issued for different filters"):
            mixed_db.query_messages(sender="ALICE@one.example", cursor=first.next_cursor)
        with pytest.raises(InvalidFilterError, match="issued for different filters"):
            mixed_db.query_messages(sender="bob@two.example", cursor=first.next_cursor)
        with pytest.raises(InvalidFilterError, match="issued for different filters"):
            mixed_db.query_messages(cursor=first.next_cursor)
        for cursor in ("not-a-cursor", "e30", "eyJ2IjogOX0"):
            with pytest.raises(InvalidFilterError, match="invalid cursor"):
                mixed_db.query_messages(cursor=cursor)

    @pytest.mark.parametrize(
        ("filters", "expected"),
        [
            ({}, {"ma", "mb", "mc"}),
            ({"from_addr": "alice@one.example"}, {"ma"}),
            ({"from_addr": "Alice"}, {"ma"}),
            ({"from_addr": "@two.example"}, {"mb"}),
            ({"participant": "alice@one.example"}, {"ma", "mb", "mc"}),
            ({"participant": "Carol"}, {"mb", "mc"}),
            ({"folders": ["Trash"]}, {"md"}),
            ({"folders": ["INBOX", "Archive"]}, {"ma", "mb", "mc"}),
            ({"date_from": "2024-02-15"}, {"mb", "mc"}),
            ({"date_to": "2024-02-15"}, {"ma"}),
            ({"from_addr": "alice@one.example", "date_from": "2024-03-01"}, set()),
        ],
    )
    def test_message_scope(self, mixed_db, filters, expected):
        assert _scope(mixed_db, **filters) == expected

    def test_message_scope_whole_threads(self, mixed_db):
        labels = mixed_db.message_scope(["t-mix", "t-solo", "t-trash"], participant="alice")
        assert labels.whole_threads == {"t-mix", "t-solo"}
        labels = mixed_db.message_scope(["t-mix"], from_addr="alice@one.example")
        assert labels.whole_threads == set()

    def test_message_scope_rejections(self, mixed_db):
        with pytest.raises(InvalidFilterError):
            mixed_db.message_scope(["t-mix"], date_from="bad")

    @pytest.mark.parametrize(
        ("filters", "expected"),
        [
            ({}, ["t-mix", "t-solo", "t-trash"]),
            ({"from_addr": "alice@one.example"}, ["t-mix", "t-trash"]),
            ({"from_addr": "ALICE@ONE.EXAMPLE"}, ["t-mix", "t-trash"]),
            ({"from_addr": "Alice"}, ["t-mix", "t-trash"]),
            ({"from_addr": "@one.example"}, ["t-mix", "t-trash"]),
            ({"from_addr": "bob@two.example"}, ["t-mix"]),
            ({"from_addr": "carol"}, ["t-solo"]),
            ({"participant": "carol@three.example"}, ["t-mix", "t-solo"]),
            ({"participant": "Bob"}, ["t-mix", "t-trash"]),
            ({"date_from": "2024-03-01"}, ["t-mix", "t-trash"]),
            ({"date_to": "2024-02-15"}, ["t-mix"]),
            ({"has_attachments": True}, ["t-mix"]),
            ({"has_attachments": False}, ["t-solo", "t-trash"]),
            ({"folders": ["Archive"]}, ["t-mix"]),
            ({"folders": ["INBOX"]}, ["t-mix", "t-solo"]),
            ({"authority_class": "counsel"}, ["t-mix", "t-trash"]),
            ({"authority_class": "unclassified"}, ["t-mix", "t-solo"]),
            # The thread-level quantifier: alice's message is from
            # January and bob's from March, yet the thread matches both
            # filters; ``query_messages`` finds no such message.
            (
                {
                    "from_addr": "alice@one.example",
                    "date_from": "2024-03-01",
                    "date_to": "2024-03-31",
                },
                ["t-mix"],
            ),
        ],
    )
    def test_apply_filters(self, mixed_db, filters, expected):
        assert _filtered(mixed_db, **filters) == expected

    def test_apply_filters_rejections(self, mixed_db):
        with pytest.raises(InvalidFilterError, match="date_to"):
            mixed_db._apply_filters(_threads(mixed_db), date_to="bad")
        with pytest.raises(InvalidFilterError, match="date_from must not be after"):
            mixed_db._apply_filters(
                _threads(mixed_db), date_from="2024-02-02", date_to="2024-02-01"
            )
        # The class is validated by the search methods before the filter
        # runs; the filter itself only finds no such sender.
        assert mixed_db._apply_filters(_threads(mixed_db), authority_class="boss") == []

    def test_apply_filters_evaluates_senders_on_the_thread_row(self, mixed_db):
        """``search_emails`` matches ``from_addr`` against the thread's
        recorded senders, not the per-message rows: a message whose From
        is not the thread's primary author on any message does not count."""
        thread = mixed_db.get_thread("t-mix")
        thread.senders = ["only@four.example"]
        assert mixed_db._apply_filters([thread], from_addr="alice@one.example") == []
        assert mixed_db._apply_filters([thread], from_addr="only@four.example") == [thread]

    def test_no_filter_value_reaches_the_log(self, mixed_db, caplog):
        with caplog.at_level(logging.DEBUG):
            mixed_db.query_messages(
                sender=_MARKER,
                recipient=_MARKER,
                participant=_MARKER,
                subject=_MARKER,
                text=_MARKER,
            )
            mixed_db.message_scope(["t-mix"], from_addr=_MARKER, participant=_MARKER)
            mixed_db._apply_filters(_threads(mixed_db), from_addr=_MARKER, participant=_MARKER)
            with pytest.raises(InvalidFilterError):
                mixed_db.query_messages(folder=_MARKER, date_from=_MARKER)
        assert _MARKER not in caplog.text


def test_fixture_thread_row_records_each_messages_primary_author(mixed_db):
    with sqlite3.connect(mixed_db.path) as conn:
        senders = json.loads(
            conn.execute("SELECT senders FROM threads WHERE thread_id = 't-mix'").fetchone()[0]
        )
    assert senders == ["Alice Example <alice@one.example>", "bob@two.example"]
    assert claimant_of("ma").startswith("ma#")


_DOCS = Path(__file__).resolve().parents[2] / "docs" / "mcp-tools.md"

# One sample value per declared parameter shape. A leaf declaring a new
# shape must add its sample here, so its compiler gets exercised.
_SAMPLES: dict[str, object] = {
    "address": "jane@example.test",
    "text": "budget",
    "words": ("budget", "plan"),
    "folders": ("INBOX", "Archive"),
    "instant": "2024-01-01T00:00:00+00:00",
    "bool": True,
    "class": "counsel",
    "bytes": 1024,
    "basis": "occurred",
}

# Leaves deliberately left out of docs/mcp-tools.md, each with its
# reason. Empty: every leaf is in the "Filter predicates" table.
_UNDOCUMENTED: dict[str, str] = {}


def _filter_predicates_section() -> str:
    return _DOCS.read_text().split("## Filter predicates", 1)[1].split("\n## ", 1)[0]


def _every_adapter_leaf_name() -> set[str]:
    """The leaf names the three adapters can build, with every parameter
    given."""
    names = {
        leaf.name
        for leaf in query_messages_leaves(
            sender="a@example.test",
            recipient="b@example.test",
            participant="c@example.test",
            subject="s",
            text="word",
            folder="INBOX",
            date_from="2024-01-01",
            date_to="2024-12-31",
            has_attachments=True,
            authority_class="counsel",
            seen=True,
            flagged=False,
        )
    }
    names |= {leaf.name for leaf in query_messages_leaves(**dict.fromkeys(_QUERY_PARAMS))}
    for basis in DATE_BASES:
        names |= {
            leaf.name
            for leaf in query_messages_leaves(
                **{
                    **dict.fromkeys(_QUERY_PARAMS),
                    "date_from": "2024-01-01",
                    "date_to": "2024-12-31",
                    "replied": True,
                    "size_min": 1,
                    "size_max": 2,
                    "date_basis": basis,
                }
            )
        }
    names |= {
        leaf.name
        for leaf in message_scope_leaves(
            from_addr="a@example.test",
            participant="c@example.test",
            folders=["INBOX"],
            date_from="2024-01-01",
            date_to="2024-12-31",
        )
    }
    names |= {
        leaf.name
        for leaf in search_emails_leaves(
            folders=["INBOX"],
            from_addr="a@example.test",
            date_from="2024-01-01",
            date_to="2024-12-31",
            has_attachments=False,
            participant="c@example.test",
            authority_class="counsel",
        )
    }
    return names


_QUERY_PARAMS = (
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
    "size_min",
    "size_max",
    "date_basis",
)


class TestLeafRegistry:
    """Every registered leaf has a compiler, an evaluability rule and a
    docs entry, and some adapter builds it (the capability report of
    #1093 reads the same registry)."""

    def test_registry_is_keyed_by_leaf_name(self):
        assert all(name == kind.name for name, kind in LEAVES.items())
        assert len(LEAVES) == 21

    @pytest.mark.parametrize("name", sorted(LEAVES))
    def test_leaf_compiles_with_a_rule_and_a_docs_entry(self, name, mixed_db):
        kind = LEAVES[name]
        assert kind.param in _SAMPLES, f"{name}: no sample for parameter shape {kind.param!r}"
        params: list = []
        sql = kind.compile(_SAMPLES[kind.param], params)
        assert sql.startswith(("m.", "instr(mcp_casefold(m.", "NULLIF(m."))
        assert sql.count("?") == len(params)
        # The fragment runs as written against the schema.
        with closing(mixed_db._connect()) as conn:
            conn.execute(f"SELECT COUNT(*) FROM messages m WHERE {sql}", params).fetchone()
        assert isinstance(kind.evaluability, Evaluability)
        if name in _UNDOCUMENTED:
            assert _UNDOCUMENTED[name]
        else:
            assert f"| `{name}` |" in _filter_predicates_section(), f"{name}: not documented"

    def test_every_leaf_is_built_by_an_adapter(self):
        assert _every_adapter_leaf_name() == set(LEAVES)

    def test_docs_table_names_no_unregistered_leaf(self):
        documented = set(re.findall(r"^\| `([a-z_]+)` \|", _filter_predicates_section(), re.M))
        assert documented == set(LEAVES) - set(_UNDOCUMENTED)

    def test_thread_tests_cover_the_search_emails_leaves_decided_in_memory(self):
        in_memory = {name for name, kind in LEAVES.items() if kind.thread_test is not None}
        assert in_memory == {
            "sender",
            "participant",
            "effective_from",
            "effective_to",
            "has_attachments",
        }


class TestCompilerAndDigest:
    def test_no_leaves_compile_to_true(self):
        assert compile_leaves([]) == ("1", [])

    def test_leaves_conjoin_in_order(self):
        sql, params = compile_leaves(
            [Leaf("folder", ("INBOX",)), Leaf("seen", False), Leaf("effective_from", "2024-01-01")]
        )
        assert sql == "m.folder IN (?) AND m.seen = ? AND m.effective_at >= ?"
        assert params == ["INBOX", 0, "2024-01-01"]

    def test_query_messages_sql_is_the_old_where_clause(self):
        sql, params = compile_leaves(
            query_messages_leaves(
                sender="jane@example.test",
                recipient=None,
                participant=None,
                subject="Plan",
                text="budget plan",
                folder=None,
                date_from="2024-01-01",
                date_to=None,
                has_attachments=True,
                authority_class="counsel",
                seen=None,
                flagged=True,
            )
        )
        assert sql == " AND ".join(
            [
                "m.claimant_id IN (SELECT claimant_id FROM message_participants "
                "WHERE address = ? AND role IN (?))",
                "instr(mcp_casefold(m.subject), ?) > 0",
                "m.claimant_id IN (SELECT c.claimant_id FROM message_chunks_fts f "
                "JOIN message_chunks c ON c.fts_rowid = f.rowid "
                "WHERE message_chunks_fts MATCH ? AND c.attachment_id IS NULL)",
                "m.claimant_id IN (SELECT c.claimant_id FROM message_chunks_fts f "
                "JOIN message_chunks c ON c.fts_rowid = f.rowid "
                "WHERE message_chunks_fts MATCH ? AND c.attachment_id IS NULL)",
                "m.folder NOT IN (?)",
                "m.effective_at >= ?",
                "m.has_attachments = ?",
                "m.flagged = ?",
                "m.claimant_id IN (SELECT p.claimant_id FROM entities e "
                "JOIN message_participants p ON p.address = e.canonical_key AND p.role = 'from' "
                "JOIN messages am ON am.claimant_id = p.claimant_id "
                "WHERE e.kind = 'person' AND e.authority_class = ? "
                "AND am.sender_ambiguous = 0 AND am.folder NOT IN (?))",
            ]
        )
        assert params == [
            "jane@example.test",
            "from",
            "plan",
            '"budget"',
            '"plan"',
            "Trash",
            "2024-01-01T00:00:00+00:00",
            1,
            1,
            "counsel",
            "Spam",
        ]

    def test_digest_binds_the_leaf_list_in_order(self):
        a = [Leaf("sender", "jane@example.test"), Leaf("not_in_folders", ("Trash",))]
        assert leaf_digest(a) == leaf_digest(list(a))
        assert len(leaf_digest(a)) == 16
        assert leaf_digest(a) != leaf_digest(list(reversed(a)))
        assert leaf_digest(a) != leaf_digest([Leaf("sender", "Jane@example.test"), a[1]])
        assert leaf_digest(a) != leaf_digest([Leaf("participant", "jane@example.test"), a[1]])

    def test_adapters_ignore_blank_filters_and_keep_the_default_scope(self):
        assert query_messages_leaves(**dict.fromkeys(_QUERY_PARAMS)) == [
            Leaf("not_in_folders", ("Trash",))
        ]
        blank = {
            name: "  "
            for name in _QUERY_PARAMS
            if name
            not in (
                "has_attachments",
                "seen",
                "flagged",
                "replied",
                "size_min",
                "size_max",
                "date_from",
                "date_to",
            )
        }
        assert query_messages_leaves(**{**dict.fromkeys(_QUERY_PARAMS), **blank}) == [
            Leaf("not_in_folders", ("Trash",))
        ]
        assert message_scope_leaves(
            from_addr=None, participant=None, folders=None, date_from=None, date_to=None
        ) == [Leaf("not_in_folders", ("Trash",))]
        assert (
            search_emails_leaves(
                folders=None,
                from_addr=None,
                date_from=None,
                date_to=None,
                has_attachments=None,
                participant=None,
                authority_class=None,
            )
            == []
        )

    def test_search_emails_leaves_keep_the_old_filter_order(self):
        leaves = search_emails_leaves(
            folders=["INBOX"],
            from_addr="a@example.test",
            date_from="2024-01-01",
            date_to="2024-12-31",
            has_attachments=True,
            participant="c@example.test",
            authority_class="counsel",
        )
        assert [leaf.name for leaf in leaves] == [
            "sender",
            "participant",
            "effective_from",
            "effective_to",
            "has_attachments",
            "folder",
            "authority_class",
        ]

    def test_apply_filters_falls_back_to_sql_for_a_leaf_without_a_thread_test(
        self, mixed_db, monkeypatch
    ):
        """A leaf ``search_emails`` does not take today still has the
        wrapper's meaning: some message of the thread satisfies it."""
        monkeypatch.setattr("src.lib.sqlite.search_emails_leaves", lambda **_: [Leaf("seen", True)])
        assert _filtered(mixed_db) == ["t-mix"]


class TestLogAllowlist:
    """No leaf value reaches the log: the ``log_tool_call`` allowlist is
    the one from before the module, and a marker passed through every
    tool parameter that becomes a leaf is withheld."""

    def test_allowlist_is_pinned(self):
        # #1085 added replied (bool) and size_min / size_max (ints);
        # each passes only its own check. date_basis is not served.
        assert set(_LOGGABLE_TOOL_PARAMS) == {
            "mode",
            "style",
            "filter_type",
            "limit",
            "max_threads",
            "offset",
            "has_attachments",
            "seen",
            "flagged",
            "include_scores",
            "extracted_only",
            "include_attachments_metadata",
            "date_from",
            "date_to",
            "source",
            "scope",
            "max_chunks_per_thread",
            "max_chars_per_chunk",
            "dedupe_attachments",
            "authority_class",
            "fields",
            "replied",
            "size_min",
            "size_max",
        }

    def test_valid_1085_values_are_logged_and_invalid_ones_withheld(self, caplog):
        logger = logging.getLogger("test-1085-tool-log")
        with caplog.at_level(logging.INFO, logger="test-1085-tool-log"):
            log_tool_call(
                logger,
                "query_messages",
                {"replied": True, "size_min": 1000, "size_max": 2000},
            )
            log_tool_call(
                logger,
                "query_messages",
                {"replied": 1, "size_min": True, "size_max": _MARKER},
            )
        first, second = caplog.messages
        assert "'replied': True" in first and "'size_min': 1000" in first
        assert "'size_max': 2000" in first
        assert first.endswith("withheld=[]")
        assert second.endswith("withheld=['replied', 'size_max', 'size_min']")
        assert _MARKER not in caplog.text

    def test_marker_through_each_tools_filters_is_withheld(self, caplog):
        logger = logging.getLogger("test-1084-tool-log")
        calls = {
            "query_messages": {**dict.fromkeys(_QUERY_PARAMS, _MARKER), "cursor": _MARKER},
            "search_emails": {
                "query": _MARKER,
                "folders": [_MARKER],
                "from_addr": _MARKER,
                "participant": _MARKER,
                "date_from": _MARKER,
                "date_to": _MARKER,
                "has_attachments": _MARKER,
                "authority_class": _MARKER,
            },
            "ask_mailbox": {
                "folders": [_MARKER],
                "from_addr": _MARKER,
                "participant": _MARKER,
                "date_from": _MARKER,
                "date_to": _MARKER,
            },
        }
        with caplog.at_level(logging.DEBUG, logger="test-1084-tool-log"):
            for tool, params in calls.items():
                log_tool_call(logger, tool, params)
        assert len(caplog.records) == 3
        assert _MARKER not in caplog.text
        for params in calls.values():
            for name in params:
                assert f"'{name}'" in caplog.text


@pytest.fixture
def clocks_db(tmp_path) -> Database:
    """Four messages of one thread whose clocks, sizes and replied flags
    differ (#1085). ``c2`` is sent on February 10 and delivered on the
    20th, either side of a February 15 bound. ``c3`` is sent after
    ``c4`` but delivered before ``c4``'s send time, so the sent and
    effective orders differ; ``c4`` has no delivery time and ``c3`` no
    stored size. Only ``c1`` is replied to.

    Effective order: c4, c3, c2, c1. Sent order: c3, c4, c2, c1.
    Occurred order (c4 has none): c3, c2, c1.
    """
    conn, path = _open_built_db_conn(tmp_path, "clocks.db")
    rows = [
        ("c1", "2024-01-10T09:00:00+00:00", "2024-01-12T09:00:00+00:00", 100, True),
        ("c2", "2024-02-10T09:00:00+00:00", "2024-02-20T09:00:00+00:00", 5000, False),
        ("c3", "2024-03-20T09:00:00+00:00", "2024-03-05T09:00:00+00:00", None, False),
        ("c4", "2024-03-10T09:00:00+00:00", None, 300, False),
    ]
    for message_id, sent_at, occurred_at, size_bytes, replied in rows:
        _insert_message(
            conn,
            message_id=message_id,
            thread_id="t-clocks",
            sent_at=sent_at,
            occurred_at=occurred_at,
            from_=["alice@one.example"],
            to=["bob@two.example"],
            size_bytes=size_bytes,
            replied=replied,
        )
    _finish_threads(conn)
    conn.commit()
    conn.close()
    return Database(str(path))


class TestClockSizeAndRepliedLeaves:
    """``replied``, ``size_min`` / ``size_max`` and ``date_basis`` on
    ``query_messages`` (#1085). A NULL size or a NULL clock under the
    chosen basis is neither a match nor a miss: the row is left out
    (its ``indeterminate`` count arrives with #1086)."""

    @pytest.mark.parametrize(
        ("filters", "expected"),
        [
            ({}, ["c4", "c3", "c2", "c1"]),
            ({"date_basis": "effective"}, ["c4", "c3", "c2", "c1"]),
            ({"date_basis": " effective "}, ["c4", "c3", "c2", "c1"]),
            ({"date_basis": ""}, ["c4", "c3", "c2", "c1"]),
            ({"date_basis": "sent"}, ["c3", "c4", "c2", "c1"]),
            ({"date_basis": "occurred"}, ["c3", "c2", "c1"]),
            # c2: sent before the bound, delivered after it.
            ({"date_from": "2024-02-15"}, ["c4", "c3", "c2"]),
            ({"date_from": "2024-02-15", "date_basis": "sent"}, ["c3", "c4"]),
            ({"date_from": "2024-02-15", "date_basis": "occurred"}, ["c3", "c2"]),
            ({"date_to": "2024-02-15"}, ["c1"]),
            ({"date_to": "2024-02-15", "date_basis": "sent"}, ["c2", "c1"]),
            ({"date_to": "2024-02-15", "date_basis": "occurred"}, ["c1"]),
            (
                {
                    "date_from": "2024-02-10T09:00:00+00:00",
                    "date_to": "2024-02-10T09:00:00+00:00",
                    "date_basis": "sent",
                },
                ["c2"],
            ),
            ({"replied": True}, ["c1"]),
            ({"replied": False}, ["c4", "c3", "c2"]),
            # Inclusive bounds; c3 has no stored size and never matches a bound.
            ({"size_min": 100}, ["c4", "c2", "c1"]),
            ({"size_min": 0}, ["c4", "c2", "c1"]),
            ({"size_min": 101}, ["c4", "c2"]),
            ({"size_max": 300}, ["c4", "c1"]),
            ({"size_max": 99}, []),
            ({"size_min": 300, "size_max": 300}, ["c4"]),
            ({"size_min": 100, "size_max": 5000}, ["c4", "c2", "c1"]),
            ({"size_min": 200, "replied": True}, []),
            ({"size_max": 400, "date_basis": "occurred"}, ["c1"]),
        ],
    )
    def test_query_messages(self, clocks_db, filters, expected):
        page = clocks_db.query_messages(**filters)
        assert _ids(page) == expected
        assert page.total_matches == len(expected)

    @pytest.mark.parametrize(
        ("filters", "matches", "indeterminate"),
        [
            # Every leaf decided for every row: nothing is indeterminate
            # (and no extra count query runs; see the spy test below).
            ({}, 4, 0),
            ({"replied": True}, 1, 0),
            ({"date_basis": "sent", "date_from": "2024-02-15"}, 2, 0),
            # c3 has no stored size: unknown under a size bound.
            ({"size_min": 100}, 3, 1),
            ({"size_max": 99}, 0, 1),
            # Kleene AND: a false leaf decides the row even when another
            # is unknown (c3 is not replied, so it is false, not unknown).
            ({"size_min": 100, "replied": True}, 1, 0),
            ({"size_min": 100, "replied": False}, 2, 1),
            # c4 has no delivery time: unknown under the occurred basis,
            # with or without a bound.
            ({"date_basis": "occurred"}, 3, 1),
            ({"date_basis": "occurred", "date_from": "2024-02-15"}, 2, 1),
            ({"date_basis": "occurred", "date_to": "2024-01-01"}, 0, 1),
            ({"date_basis": "occurred", "replied": True}, 1, 0),
            # Two unknowns on two rows (c3's size, c4's clock) count once each.
            ({"date_basis": "occurred", "size_min": 100}, 2, 2),
            ({"date_basis": "occurred", "size_min": 100, "replied": False}, 1, 2),
        ],
    )
    def test_indeterminate_counts_rows_neither_accepted_nor_rejected(
        self, clocks_db, filters, matches, indeterminate
    ):
        page = clocks_db.query_messages(**filters)
        assert (page.total_matches, page.indeterminate) == (matches, indeterminate)
        # The count is read on the same snapshot and is independent of paging.
        first = clocks_db.query_messages(**filters, limit=1)
        assert first.indeterminate == indeterminate
        if first.has_more:
            rest = clocks_db.query_messages(**filters, limit=1, cursor=first.next_cursor)
            assert rest.indeterminate == indeterminate

    def test_indeterminate_count_runs_only_for_a_leaf_that_can_be_unknown(
        self, clocks_db, monkeypatch
    ):
        statements: list[str] = []
        real_connect = clocks_db._connect

        def spying_connect():
            conn = real_connect()
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(clocks_db, "_connect", spying_connect)
        clocks_db.query_messages(replied=True)
        assert not [s for s in statements if "IS NULL" in s]
        statements.clear()
        clocks_db.query_messages(size_min=100)
        assert len([s for s in statements if ") IS NULL" in s]) == 1

    @pytest.mark.parametrize(
        ("filters", "field"),
        [
            ({"date_basis": "internal"}, "date_basis"),
            ({"date_basis": "bogus"}, "date_basis"),
            ({"size_min": -1}, "size_min"),
            ({"size_max": -1}, "size_max"),
            ({"size_min": True}, "size_min"),
            ({"size_max": "12"}, "size_max"),
            # Beyond SQLite's INTEGER: sqlite3 would raise OverflowError at
            # the bind (Codex round 1 on #1125).
            ({"size_min": 2**63}, "size_min"),
            ({"size_max": 2**63}, "size_max"),
            ({"size_min": 400, "size_max": 300}, "size_min/size_max"),
        ],
    )
    def test_rejections(self, clocks_db, filters, field):
        with pytest.raises(InvalidFilterError) as info:
            clocks_db.query_messages(**filters)
        assert info.value.field_name == field
        if field in ("size_min", "size_max"):
            assert str(info.value) == f"{field} must be an integer from 0 to 9223372036854775807"

    def test_internal_basis_is_a_fixed_text_error_naming_1092(self, clocks_db):
        with pytest.raises(InvalidFilterError) as info:
            clocks_db.query_messages(date_basis="internal")
        assert str(info.value) == (
            "date_basis 'internal' is unavailable until #1092 (the index stores no "
            "server arrival time); use effective, sent or occurred"
        )

    def test_cursor_walks_the_basis_order_and_is_bound_to_the_basis(self, clocks_db):
        first = clocks_db.query_messages(date_basis="sent", limit=1)
        assert _ids(first) == ["c3"] and first.has_more
        second = clocks_db.query_messages(date_basis="sent", limit=1, cursor=first.next_cursor)
        assert _ids(second) == ["c4"] and second.offset == 1
        third = clocks_db.query_messages(date_basis="sent", limit=2, cursor=second.next_cursor)
        assert _ids(third) == ["c2", "c1"] and not third.has_more
        # The same leaf list under another basis is another keyset: the
        # cursor is foreign there, never read against the other clock.
        for other in ({}, {"date_basis": "effective"}, {"date_basis": "occurred"}):
            with pytest.raises(InvalidFilterError, match="issued for different filters"):
                clocks_db.query_messages(**other, cursor=first.next_cursor)
        with pytest.raises(InvalidFilterError, match="issued for different filters"):
            clocks_db.query_messages(date_basis="sent", replied=False, cursor=first.next_cursor)
        default = clocks_db.query_messages(limit=1)
        assert _ids(default) == ["c4"]
        with pytest.raises(InvalidFilterError, match="issued for different filters"):
            clocks_db.query_messages(date_basis="sent", cursor=default.next_cursor)
        assert _ids(clocks_db.query_messages(limit=1, cursor=default.next_cursor)) == ["c3"]

    def test_cursor_under_occurred_skips_undated_rows(self, clocks_db):
        first = clocks_db.query_messages(date_basis="occurred", limit=1)
        assert _ids(first) == ["c3"]
        rest = clocks_db.query_messages(date_basis="occurred", cursor=first.next_cursor)
        assert _ids(rest) == ["c2", "c1"] and not rest.has_more

    def test_cursor_binds_size_and_replied(self, clocks_db):
        first = clocks_db.query_messages(size_min=100, limit=1)
        assert _ids(first) == ["c4"]
        assert _ids(clocks_db.query_messages(size_min=100, cursor=first.next_cursor)) == [
            "c2",
            "c1",
        ]
        for other in ({"size_min": 101}, {"size_max": 100}, {"size_min": 100, "replied": False}):
            with pytest.raises(InvalidFilterError, match="issued for different filters"):
                clocks_db.query_messages(**other, cursor=first.next_cursor)

    def test_leaves_compile_and_declare_their_evaluability(self):
        sql, params = compile_leaves(
            [
                Leaf("dated", "occurred"),
                Leaf("sent_from", "2024-01-01T00:00:00+00:00"),
                Leaf("occurred_to", "2024-12-31T23:59:59.999999+00:00"),
                Leaf("replied", True),
                Leaf("size_min", 1),
                Leaf("size_max", 2),
            ]
        )
        assert sql == (
            "NULLIF(m.occurred_at IS NOT NULL, 0) AND m.sent_at >= ? AND m.occurred_at <= ? "
            "AND m.replied = ? AND m.size_bytes >= ? AND m.size_bytes <= ?"
        )
        assert params == ["2024-01-01T00:00:00+00:00", "2024-12-31T23:59:59.999999+00:00", 1, 1, 2]
        unknown_when_null = {"size_min", "size_max", "occurred_from", "occurred_to", "dated"}
        for name, kind in LEAVES.items():
            expected = (
                Evaluability.UNKNOWN_WHEN_NULL
                if name in unknown_when_null
                else Evaluability.DECIDED
            )
            assert kind.evaluability is expected, name

    def test_adapter_builds_the_basis_leaves(self):
        base = dict.fromkeys(_QUERY_PARAMS)
        assert query_messages_leaves(
            **{**base, "date_from": "2024-01-01", "date_basis": "sent"}
        ) == [
            Leaf("not_in_folders", ("Trash",)),
            Leaf("sent_from", "2024-01-01T00:00:00+00:00"),
        ]
        assert query_messages_leaves(
            **{**base, "date_to": "2024-01-01", "date_basis": "occurred"}
        ) == [
            Leaf("not_in_folders", ("Trash",)),
            Leaf("dated", "occurred"),
            Leaf("occurred_to", "2024-01-01T23:59:59.999999+00:00"),
        ]
        assert query_messages_leaves(
            **{**base, "replied": False, "size_min": 10, "size_max": 20}
        ) == [
            Leaf("not_in_folders", ("Trash",)),
            Leaf("replied", False),
            Leaf("size_min", 10),
            Leaf("size_max", 20),
        ]

    def test_digest_binds_the_basis(self):
        leaves = [Leaf("not_in_folders", ("Trash",))]
        assert leaf_digest(leaves) == leaf_digest(leaves, "effective")
        assert len({leaf_digest(leaves, basis) for basis in DATE_BASES}) == len(DATE_BASES)

    def test_cursor_position_needs_the_basis_clock(self):
        record = MessageRecord(
            message_id="c",
            claimant_id="c#0",
            thread_id="t",
            subject="s",
            sent_at="2024-01-10T09:00:00+00:00",
            folder="INBOX",
            has_attachments=False,
        )
        assert _record_clock(record, DATE_BASES["sent"]) == "2024-01-10T09:00:00+00:00"
        assert _record_clock(record, DATE_BASES["effective"]) == "2024-01-10T09:00:00+00:00"
        # Unreachable through query_messages (the ``dated`` leaf keeps
        # such rows off the page); refused rather than encoded as null.
        with pytest.raises(ValueError, match="without a occurred time"):
            _record_clock(record, DATE_BASES["occurred"])

    def test_no_filter_value_reaches_the_log(self, clocks_db, caplog):
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(InvalidFilterError):
                clocks_db.query_messages(date_basis=_MARKER)
            with pytest.raises(InvalidFilterError):
                clocks_db.query_messages(size_min=_MARKER)
        assert _MARKER not in caplog.text


@pytest.mark.parametrize("value", [[], 3, True, 1.5, {"basis": "sent"}])
def test_normalize_date_basis_rejects_a_non_string_with_fixed_text(value):
    """A raw non-string from the wire is an ``InvalidFilterError`` with
    fixed text, not an ``AttributeError`` from ``.strip()`` (#1085,
    Codex round 5)."""
    from src.lib.predicates import normalize_date_basis

    with pytest.raises(InvalidFilterError) as info:
        normalize_date_basis(value)
    assert info.value.field_name == "date_basis"
    assert str(info.value) == "date_basis must be a string"


def test_normalize_date_basis_keeps_none_as_the_default_for_database_callers():
    """``Database`` callers pass ``None`` for "not given"; only the
    tool rejects an explicit null (Codex round 6)."""
    from src.lib.predicates import DEFAULT_DATE_BASIS, normalize_date_basis

    assert normalize_date_basis(None) == DEFAULT_DATE_BASIS == "effective"
