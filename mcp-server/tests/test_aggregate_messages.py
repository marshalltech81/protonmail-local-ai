"""``aggregate_messages``: server-side counts by one dimension (#823).

The groups are evaluated by the same leaves as ``query_messages`` (the
flat filters and ``where``), so each group's count is the
``total_matches`` of ``query_messages`` over the same filters plus the
group's own filter, and a message the filters leave undecided is
counted as indeterminate, never as a group's match. Every value is
synthetic.
"""

from __future__ import annotations

import asyncio
import base64
import calendar
import json
import logging
import sqlite3

import pytest
from fastmcp import FastMCP
from src.lib import predicates
from src.lib.predicates import WHERE_LEAVES, Where, normalize_where
from src.lib.sqlite import AGGREGATE_DIMENSIONS, Database
from src.tools.retrieval import register_retrieval_tools

from tests.conftest import _insert_message, set_authority
from tests.test_evidence_scope import _finish_threads
from tests.test_retrieval import _error, _handlers, _text
from tests.test_sqlite import _open_built_db_conn
from tests.test_structured_output import _call, _tools

MARKER = "SYNTHETIC_AGGREGATE_MARKER"

# (message_id, thread, folder, sent_at, From, subject, extra)
_MESSAGES = [
    ("m1", "t1", "INBOX", "2023-05-02T09:00:00+00:00", ["Ann <ann@one.test>"], "budget a", {}),
    ("m2", "t1", "INBOX", "2024-01-10T09:00:00+00:00", ["Ann B <ann@one.test>"], "budget b", {}),
    ("m3", "t2", "Archive", "2024-01-20T09:00:00+00:00", ["Bob <bob@one.test>"], "budget c", {}),
    (
        "m4",
        "t3",
        "INBOX",
        "2024-02-01T09:00:00+00:00",
        ["carol@two.test"],
        "budget d",
        {"has_attachments": True},
    ),
    ("m5", "t4", "Spam", "2024-02-03T09:00:00+00:00", ["carol@two.test"], "budget e", {}),
    ("m6", "t5", "Trash", "2024-03-01T09:00:00+00:00", ["dave@three.test"], "budget f", {}),
    # Sender ambiguous: no sender group.
    (
        "m7",
        "t6",
        "INBOX",
        "2024-03-05T09:00:00+00:00",
        ["eve@two.test"],
        "budget g",
        {"sender_ambiguous": 1},
    ),
    # Two From addresses: one group each.
    (
        "m8",
        "t1",
        "INBOX",
        "2024-04-01T09:00:00+00:00",
        ["ann@one.test", "bob@one.test"],
        "budget h",
        {"has_attachments": True},
    ),
    # Sender not checked yet.
    (
        "m9",
        "t7",
        "INBOX",
        "2024-04-02T09:00:00+00:00",
        ["frank@four.test"],
        "budget i",
        {"sender_ambiguous": None},
    ),
    # A subject filter cannot rule this one out: its subject is not
    # checked yet.
    (
        "m10",
        "t8",
        "Archive",
        "2024-04-03T09:00:00+00:00",
        ["ann@one.test"],
        "other",
        {"completeness": {"subject_complete": None}},
    ),
    ("m11", "t9", "INBOX", "2022-12-31T23:59:59+00:00", ["gus@five.test"], "other", {}),
]


@pytest.fixture
def agg_db(tmp_path) -> Database:
    conn, path = _open_built_db_conn(tmp_path, "aggregate.db")
    for mid, thread, folder, sent_at, from_, subject, extra in _MESSAGES:
        _insert_message(
            conn,
            message_id=mid,
            thread_id=thread,
            folder=folder,
            sent_at=sent_at,
            from_=from_,
            to=["me@self.test"],
            subject=subject,
            body=f"routine note {mid}",
            **extra,
        )
    _finish_threads(conn)
    conn.close()
    set_authority(path, "carol@two.test", "vendor", "rule-v")
    set_authority(path, "bob@one.test", "counsel", "rule-c")
    return Database(str(path))


def _all_groups(db: Database, group_by: str, **filters) -> tuple[list, object]:
    """Every group, paged three at a time, and the first page."""
    groups, cursor, first = [], None, None
    while True:
        page = db.aggregate_messages(group_by=group_by, limit=3, cursor=cursor, **filters)
        first = first or page
        groups += page.groups
        if not page.has_more:
            return groups, first
        cursor = page.next_cursor


def _where(*items: dict) -> list:
    return normalize_where(Where.model_validate({"all": list(items)})) if items else []


def _group_filters(group_by: str, value: str, filters: dict, where_items: list) -> tuple:
    """The ``query_messages`` filters selecting one group's messages."""
    filters = dict(filters)
    items = list(where_items)
    if group_by == "sender_address":
        items.append({"leaf": "address_is", "role": "from", "value": value})
    elif group_by == "sender_domain":
        items.append({"leaf": "domain_is", "role": "from", "value": value})
    elif group_by == "folder":
        filters["folder"] = value
    elif group_by == "year":
        filters["date_from"], filters["date_to"] = f"{value}-01-01", f"{value}-12-31"
    elif group_by == "month":
        year, month = (int(p) for p in value.split("-"))
        last = calendar.monthrange(year, month)[1]
        filters["date_from"], filters["date_to"] = f"{value}-01", f"{value}-{last:02d}"
    else:
        filters["authority_class"] = value
    return filters, _where(*items)


def _paged_messages(db: Database, filters: dict, where: list) -> tuple[list, object]:
    rows, cursor, first = [], None, None
    while True:
        page = db.query_messages(**filters, where=where, limit=100, cursor=cursor)
        first = first or page
        rows += page.messages
        if not page.has_more:
            return rows, first
        cursor = page.next_cursor


_FILTER_CASES = [
    ({}, []),
    ({"subject": "budget"}, []),
    (
        {"has_attachments": None},
        [
            {
                "any": [
                    {"leaf": "domain_is", "role": "from", "value": "one.test"},
                    {"leaf": "domain_is", "role": "from", "value": "two.test"},
                ]
            },
            {"leaf": "body_words", "value": "m3", "negate": True},
        ],
    ),
]


class TestEqualityWithQueryMessages:
    @pytest.mark.parametrize("group_by", AGGREGATE_DIMENSIONS)
    @pytest.mark.parametrize(("filters", "where_items"), _FILTER_CASES)
    def test_each_group_is_the_query_messages_answer_for_its_value(
        self, agg_db, group_by, filters, where_items
    ):
        filters = {k: v for k, v in filters.items() if v is not None}
        groups, page = _all_groups(agg_db, group_by, **filters, where=_where(*where_items))
        whole = agg_db.query_messages(**filters, where=_where(*where_items), limit=1)
        assert (page.total_matches, page.indeterminate) == (
            whole.total_matches,
            whole.indeterminate,
        )
        assert page.total_groups == len(groups)
        for g in groups:
            if g.value is None:
                continue
            gf, gw = _group_filters(group_by, g.value, filters, where_items)
            rows, q = _paged_messages(agg_db, gf, gw)
            assert g.messages == q.total_matches == len(rows), (group_by, g.value)
            assert g.threads == len({m.thread_id for m in rows})
            assert g.with_attachments == sum(m.has_attachments for m in rows)
            dates = sorted(m.effective_at for m in rows)
            assert (g.first_at, g.last_at) == ((dates[0], dates[-1]) if dates else (None, None))

    @pytest.mark.parametrize("group_by", ["folder", "year", "month"])
    @pytest.mark.parametrize(("filters", "where_items"), _FILTER_CASES)
    def test_single_valued_groups_partition_matches_and_indeterminate(
        self, agg_db, group_by, filters, where_items
    ):
        filters = {k: v for k, v in filters.items() if v is not None}
        groups, page = _all_groups(agg_db, group_by, **filters, where=_where(*where_items))
        assert all(g.value is not None for g in groups)
        assert sum(g.messages for g in groups) == page.total_matches
        assert sum(g.indeterminate for g in groups) == page.indeterminate
        for g in groups:
            gf, gw = _group_filters(group_by, g.value, filters, where_items)
            q = agg_db.query_messages(**gf, where=gw, limit=1)
            # These dimensions are decided for every message, so the
            # group's undecided messages are exactly the query's.
            assert g.indeterminate == q.indeterminate


class TestGroups:
    def test_sender_groups(self, agg_db):
        groups, page = _all_groups(agg_db, "sender_address")
        by = {g.value: g for g in groups}
        # m6 is in Trash; m8 counts for both of its From addresses.
        assert page.total_matches == 10
        assert {v: g.messages for v, g in by.items()} == {
            "ann@one.test": 4,
            "bob@one.test": 2,
            "carol@two.test": 2,
            "gus@five.test": 1,
            None: 2,
        }
        assert sum(g.messages for g in groups) == page.total_matches + 1
        # Count descending, then value; the no-value group last on ties.
        assert [g.value for g in groups] == [
            "ann@one.test",
            "bob@one.test",
            "carol@two.test",
            None,
            "gus@five.test",
        ]
        # The display name on the latest matched message that has one.
        assert by["ann@one.test"].display_name == "Ann B"
        assert by["carol@two.test"].display_name is None
        assert by[None].display_name is None
        assert by["ann@one.test"].threads == 2

    def test_the_display_name_comes_from_every_stored_name(self, tmp_path):
        # Review round 2: a From address written first without a name and
        # again with one keeps no name on its participant row; the name
        # is in message_participant_names (#1140).
        conn, path = _open_built_db_conn(tmp_path, "names.db")
        _insert_message(
            conn,
            message_id="m-old",
            thread_id="t1",
            sent_at="2024-01-01T09:00:00+00:00",
            from_=["Old Name <ann@one.test>"],
        )
        _insert_message(
            conn,
            message_id="m-new",
            thread_id="t2",
            sent_at="2024-02-01T09:00:00+00:00",
            from_=["ann@one.test", "Late Name <ann@one.test>"],
        )
        conn.close()
        [group] = Database(str(path)).aggregate_messages(group_by="sender_address").groups
        assert group.display_name == "Late Name"

    def test_ambiguous_or_unchecked_senders_are_in_the_no_value_group(self, agg_db):
        groups, _ = _all_groups(agg_db, "sender_domain")
        by = {g.value: g.messages for g in groups}
        # m7 (ambiguous) and m9 (not checked) have no sender domain.
        assert by == {"one.test": 5, "two.test": 2, "five.test": 1, None: 2}

    def test_authority_groups_leave_spam_and_unsafe_senders_without_a_class(self, agg_db):
        groups, _ = _all_groups(agg_db, "authority_class")
        by = {g.value: g.messages for g in groups}
        # m5 is in Spam; m7 and m9 have no safe sender.
        assert by == {"unclassified": 5, "counsel": 2, "vendor": 1, None: 3}

    def test_the_domain_follows_the_last_at_sign_as_domain_is_does(self, agg_db):
        with sqlite3.connect(agg_db.path) as conn:
            conn.execute(
                "UPDATE message_participants SET address = 'gus@x@five.test' "
                "WHERE address = 'gus@five.test'"
            )
        groups, _ = _all_groups(agg_db, "sender_domain")
        assert {g.value: g.messages for g in groups}["five.test"] == 1
        where = _where({"leaf": "domain_is", "role": "from", "value": "five.test"})
        assert agg_db.query_messages(where=where).total_matches == 1

    def test_a_sender_with_no_person_entity_has_no_class(self, agg_db):
        with sqlite3.connect(agg_db.path) as conn:
            conn.execute("DELETE FROM entities WHERE entity_id = 'person:gus@five.test'")
        groups, _ = _all_groups(agg_db, "authority_class")
        assert {g.value: g.messages for g in groups}[None] == 4
        # query_messages rules every class out for it, as the group says.
        assert (
            agg_db.query_messages(
                sender="gus@five.test", authority_class="unclassified"
            ).total_matches
            == 0
        )

    def test_undecided_messages_are_never_a_group_match(self, agg_db):
        groups, page = _all_groups(agg_db, "folder", subject="budget")
        by = {g.value: g for g in groups}
        assert page.indeterminate == 1
        # m10 (Archive) cannot be ruled out: indeterminate, not a match.
        assert (by["Archive"].messages, by["Archive"].indeterminate) == (1, 1)
        assert by["INBOX"].indeterminate == 0

    def test_a_group_of_only_undecided_messages_is_listed(self, agg_db):
        groups, _ = _all_groups(agg_db, "sender_address", subject="zzz", folder="Archive")
        assert [(g.value, g.messages, g.indeterminate) for g in groups] == [("ann@one.test", 0, 1)]
        assert (groups[0].first_at, groups[0].last_at, groups[0].threads) == (None, None, 0)
        assert groups[0].display_name is None

    def test_year_and_month_use_the_utc_effective_time(self, agg_db):
        years, _ = _all_groups(agg_db, "year")
        assert {g.value: g.messages for g in years} == {"2022": 1, "2023": 1, "2024": 8}
        months, _ = _all_groups(agg_db, "month", date_from="2024-01-01", date_to="2024-02-29")
        assert {g.value: g.messages for g in months} == {"2024-01": 2, "2024-02": 2}


@pytest.fixture
def from_db(tmp_path) -> Database:
    """Review round 3: messages whose stored From list is or is not
    complete, by ``from_addresses_complete`` (0 a known loss, NULL not
    reparsed yet)."""
    conn, path = _open_built_db_conn(tmp_path, "from.db")
    rows = [
        # (id, folder, From, from_addresses_complete, sender_ambiguous, subject_complete)
        ("c1", "INBOX", ["ann@one.test"], 1, 0, 1),  # complete
        ("i1", "INBOX", ["bob@two.test"], 0, 0, 1),  # incomplete
        ("n1", "INBOX", ["cy@two.test"], None, 0, 1),  # not reparsed
        ("m1", "INBOX", ["ann@one.test", "bob@two.test"], 0, 0, 1),  # two authors
        ("a1", "INBOX", ["dee@three.test"], 0, 1, 1),  # ambiguous: null group already
        ("s1", "Spam", ["eve@three.test"], 0, 0, 1),  # Spam: no class either way
        ("e1", "INBOX", [], 0, 0, 1),  # no stored From row
        ("u1", "INBOX", ["fay@four.test"], 0, 0, None),  # undecided under a subject filter
        ("x1", "Archive", ["gil@four.test"], 0, 0, 1),  # filtered out by folder
    ]
    for mid, folder, from_, from_complete, ambiguous, subject_complete in rows:
        _insert_message(
            conn,
            message_id=mid,
            thread_id=f"t-{mid}",
            folder=folder,
            sent_at="2024-01-10T09:00:00+00:00",
            subject="report" if subject_complete else "other",
            from_=from_,
            sender_ambiguous=ambiguous,
            completeness={
                "from_addresses_complete": from_complete,
                "subject_complete": subject_complete,
            },
        )
    conn.close()
    return Database(str(path))


class TestIncompleteFromLists:
    """``incomplete_from_messages``: messages the filters do not reject
    whose sender is safe but whose stored From list is not known to be
    complete, so further sender groups or memberships may be missing."""

    @pytest.mark.parametrize("group_by", ["sender_address", "sender_domain"])
    def test_sender_dimensions_count_each_message_once(self, from_db, group_by):
        page = from_db.aggregate_messages(group_by=group_by, folder="INBOX")
        # i1, n1 (NULL counts), m1 once despite two authors, e1 (no From
        # row: also in the null group), u1; not c1 (complete), a1
        # (ambiguous), x1 (filtered out).
        assert page.incomplete_from_messages == 5

    def test_authority_class_leaves_spam_out(self, from_db):
        assert from_db.aggregate_messages(group_by="sender_address").incomplete_from_messages == 7
        assert from_db.aggregate_messages(group_by="authority_class").incomplete_from_messages == 6

    def test_an_undecided_message_is_in_the_population(self, from_db):
        page = from_db.aggregate_messages(
            group_by="sender_address", folder="INBOX", subject="report"
        )
        # u1's subject is not checked yet: undecided, still counted.
        assert page.indeterminate == 1
        assert page.incomplete_from_messages == 5

    @pytest.mark.parametrize("group_by", ["folder", "year", "month"])
    def test_absent_for_dimensions_without_a_sender(self, from_db, group_by):
        assert from_db.aggregate_messages(group_by=group_by).incomplete_from_messages is None

    def test_the_count_is_the_same_on_every_page(self, from_db):
        counts, cursor = [], None
        while True:
            page = from_db.aggregate_messages(group_by="sender_address", limit=1, cursor=cursor)
            counts.append(page.incomplete_from_messages)
            if not page.has_more:
                break
            cursor = page.next_cursor
        assert len(counts) > 2 and set(counts) == {7}

    def test_known_groups_still_equal_query_messages(self, from_db):
        page = from_db.aggregate_messages(group_by="sender_address", limit=100)
        for g in page.groups:
            if g.value is not None:
                assert g.messages == from_db.query_messages(sender=g.value).total_matches

    def test_tool_output_prose_and_log(self, fake_server, from_db, caplog):
        handler = _handlers(fake_server, from_db)["aggregate_messages"]
        with caplog.at_level(logging.DEBUG):
            out = asyncio.run(handler(group_by="sender_address", subject=MARKER))
            folders = asyncio.run(handler(group_by="folder"))
        assert out.structured_content["incomplete_from_messages"] == 1  # u1, undecided
        assert "incomplete_from_messages" not in folders.structured_content
        text = _text(asyncio.run(handler(group_by="sender_domain")))
        assert (
            "7 messages have an incomplete From list; further sender groups or "
            "memberships may be missing." in text
        )
        assert "incomplete From list" not in _text(folders)
        lines = [r.getMessage() for r in caplog.records if r.name == "mcp.timings"]
        assert "'incomplete_from_messages': 1" in lines[0]
        assert "incomplete_from_messages" not in lines[1]
        assert MARKER not in caplog.text

    def test_served_schema_validates_with_and_without_the_field(self, from_db):
        out = _call(_server(from_db), "aggregate_messages", group_by="authority_class")
        assert out["incomplete_from_messages"] == 6
        out = _call(_server(from_db), "aggregate_messages", group_by="month")
        assert "incomplete_from_messages" not in out


class TestTrash:
    def test_trash_is_left_out_by_default_and_counted_when_named(self, agg_db):
        groups, page = _all_groups(agg_db, "folder")
        assert "Trash" not in {g.value for g in groups}
        groups, page = _all_groups(agg_db, "sender_address", folder="Trash")
        assert [(g.value, g.messages) for g in groups] == [("dave@three.test", 1)]


class TestPaging:
    def test_limit_is_clamped_to_the_group_cap(self, agg_db):
        assert len(agg_db.aggregate_messages(group_by="sender_address", limit=1).groups) == 1
        page = agg_db.aggregate_messages(group_by="sender_address", limit=100)
        assert len(page.groups) == page.total_groups == 5
        assert page.has_more is False and page.next_cursor is None

    def test_pages_cover_every_group_once_in_order(self, agg_db):
        whole = agg_db.aggregate_messages(group_by="sender_address", limit=100).groups
        paged, _ = _all_groups(agg_db, "sender_address")
        assert [g.value for g in paged] == [g.value for g in whole]

    def test_a_cursor_is_bound_to_the_filters_and_the_dimension(self, agg_db):
        page = agg_db.aggregate_messages(group_by="sender_address", limit=1)
        for other in (
            {"group_by": "sender_domain"},
            {"group_by": "sender_address", "folder": "INBOX"},
            {"group_by": "sender_address", "where": _where({"leaf": "body_words", "value": "x"})},
        ):
            with pytest.raises(ValueError, match="different filters"):
                agg_db.aggregate_messages(**other, cursor=page.next_cursor)

    @pytest.mark.parametrize(
        "cursor",
        [
            "not-base64!",
            base64.urlsafe_b64encode(b"[]").decode(),
            base64.urlsafe_b64encode(json.dumps({"v": 1, "q": "x", "o": -1}).encode()).decode(),
            base64.urlsafe_b64encode(json.dumps({"v": 2, "q": "x", "s": "a"}).encode()).decode(),
        ],
    )
    def test_a_malformed_cursor_is_refused(self, agg_db, cursor):
        with pytest.raises(ValueError, match="invalid cursor"):
            agg_db.aggregate_messages(group_by="folder", cursor=cursor)

    def test_a_query_messages_cursor_is_foreign(self, agg_db):
        cursor = agg_db.query_messages(limit=1).next_cursor
        with pytest.raises(ValueError, match="invalid cursor"):
            agg_db.aggregate_messages(group_by="folder", cursor=cursor)

    def test_a_page_past_the_last_group_still_reports_the_totals(self, agg_db):
        page = agg_db.aggregate_messages(group_by="folder", limit=1)
        data = json.loads(base64.urlsafe_b64decode(page.next_cursor + "=="))
        data["o"] = 50
        cursor = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        late = agg_db.aggregate_messages(group_by="folder", cursor=cursor)
        assert (late.groups, late.total_matches, late.total_groups) == ([], 10, 3)
        assert late.has_more is False


# ---------------------------------------------------------------------------
# Bounded work
# ---------------------------------------------------------------------------


def _work_corpus(tmp_path, size: int) -> Database:
    """``size`` messages from 7 senders: half match the probe leaves, a
    quarter are undecided (To not checked), a quarter are rejected."""
    conn, path = _open_built_db_conn(tmp_path, f"agg-work-{size}.db")
    for i in range(size):
        kind = i % 4
        _insert_message(
            conn,
            message_id=f"w{i}",
            thread_id=f"t{i % 11}",
            sent_at=f"2024-{1 + i % 12:02d}-01T{i // 60 % 24:02d}:{i % 60:02d}:00+00:00",
            # Realistic density (#1262): over 20 tokens per chunk.
            body=(
                f"the quarterly budget approved by the committee covers travel, "
                f"equipment and training for team{i} through the end of year "
                f"{2000 + i % 30}, reference item{i} batch{i % 7} and review{i % 13}"
            ),
            from_=[f"Sender {i % 7} <s{i % 7}@d{i % 3}.test>"],
            to=["b@two.test" if kind < 2 else "c@three.test"],
            completeness={"to_addresses_complete": None} if kind == 2 else {},
        )
    _finish_threads(conn)
    conn.close()
    return Database(str(path))


def _probe(db: Database, monkeypatch, group_by: str, items: list) -> tuple:
    """Each leaf's evaluations in the aggregate statement, the
    connections the call opened and its top-level statements."""
    statements: list[str] = []
    calls: dict[str, int] = {}
    connections: list[sqlite3.Connection] = []
    connect = db._connect

    def probe(tag, value):
        calls[tag] = calls.get(tag, 0) + 1
        return value

    def traced():
        conn = connect()
        connections.append(conn)
        conn.create_function("mcp_probe", 2, probe)
        conn.set_trace_callback(lambda s: None if s.startswith("-- ") else statements.append(s))
        return conn

    monkeypatch.setattr(db, "_connect", traced)
    for name in WHERE_LEAVES:
        kind = predicates.LEAVES[name]

        def spy(value, params, _original=kind.compile, _name=name):
            return f"mcp_probe('{_name}', ({_original(value, params)}))"

        monkeypatch.setitem(
            predicates.LEAVES,
            name,
            predicates.LeafKind(kind.name, kind.param, spy, kind.evaluability),
        )
    page = db.aggregate_messages(group_by=group_by, where=_where(*items), limit=2)
    ours = [s for s in statements if s.startswith("WITH e AS MATERIALIZED")]
    return calls, page, len(connections), (len(ours), len(statements))


@pytest.mark.parametrize("group_by", AGGREGATE_DIMENSIONS)
def test_one_statement_evaluates_each_leaf_a_bounded_number_of_times_per_row(
    tmp_path, monkeypatch, group_by
):
    items = [
        {
            "any": [
                {"leaf": "address_is", "role": "to", "value": "b@two.test"},
                {"leaf": "body_words", "value": "nonexistentword"},
            ]
        },
        {"leaf": "address_contains", "role": "from", "value": "zzz", "negate": True},
    ]
    shape = []
    for size in (40, 400):
        with monkeypatch.context() as patch:
            calls, page, connections, statements = _probe(
                _work_corpus(tmp_path, size), patch, group_by, items
            )
        p = page.total_matches + page.indeterminate
        assert (page.total_matches, page.indeterminate) == (size // 2, size // 4)
        assert set(calls) == {"address_is", "body_words", "address_contains"}
        for name, n in calls.items():
            # Once per message scanned in the filter, once per message
            # not rejected in the projection.
            assert n <= size + p, (name, n)
        shape.append((connections, statements))
    # One connection and one statement of ours (FTS5 runs its own for
    # body_words), the same at both sizes.
    assert shape[0] == shape[1]
    assert shape[0][0] == 1 and shape[0][1][0] == 1


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def _server(db) -> FastMCP:
    server = FastMCP("aggregate-test")
    register_retrieval_tools(server, db)
    return server


class TestTool:
    def test_output_validates_against_the_served_schema(self, agg_db):
        out = _call(_server(agg_db), "aggregate_messages", group_by="sender_address", limit=2)
        assert out["group_by"] == "sender_address"
        assert (out["total_matches"], out["indeterminate"], out["total_groups"]) == (10, 0, 5)
        assert out["returned"] == 2 and out["has_more"] is True and out["next_cursor"]
        assert out["groups"][0] == {
            "value": "ann@one.test",
            "messages": 4,
            "threads": 2,
            "first_at": "2023-05-02T09:00:00+00:00",
            "last_at": "2024-04-03T09:00:00+00:00",
            "with_attachments": 1,
            "indeterminate": 0,
            "display_name": "Ann B",
        }
        nxt = _call(
            _server(agg_db),
            "aggregate_messages",
            group_by="sender_address",
            limit=2,
            cursor=out["next_cursor"],
        )
        assert nxt["offset"] == 2
        assert [g["value"] for g in nxt["groups"]] == ["carol@two.test", None]

    def test_group_by_is_a_closed_required_enum(self, agg_db):
        tool = _tools(_server(agg_db))["aggregate_messages"]
        # The whole description reaches the client (#1011).
        for phrase in ("Trash", "indeterminate", "null", "Each page"):
            assert phrase in tool.description, phrase
        # Review round 1: paging sends addresses and names to the
        # calling model, so the description asks for disclosure first
        # and the smallest page, as query_messages' does.
        description = " ".join(tool.description.split())
        for phrase in (
            "tell the user how many groups (total_groups)",
            "which may be remote",
            "Prefer the smallest page",
        ):
            assert phrase in description, phrase
        schema = tool.input_schema
        assert schema["properties"]["group_by"]["enum"] == list(AGGREGATE_DIMENSIONS)
        assert "group_by" in schema["required"]
        assert set(schema["properties"]) >= {
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
            "where",
            "limit",
            "cursor",
        }

    def test_accepts_the_query_messages_filters(self, agg_db):
        qm = _tools(_server(agg_db))["query_messages"].input_schema["properties"]
        agg = _tools(_server(agg_db))["aggregate_messages"].input_schema["properties"]
        # Every filter, not the row projection.
        assert set(qm) - {"fields"} == set(agg) - {"group_by"}
        for name in set(qm) - {"fields", "limit"}:
            assert {k: v for k, v in agg[name].items() if k != "description"} == {
                k: v for k, v in qm[name].items() if k != "description"
            }, name

    def test_prose_states_counts_and_the_no_value_group(self, fake_server, agg_db):
        handler = _handlers(fake_server, agg_db)["aggregate_messages"]
        text = _text(asyncio.run(handler(group_by="sender_domain", subject="budget")))
        assert "group_by: sender_domain" in text
        assert "total_matches: 8" in text
        assert "indeterminate: 1" in text
        assert "groups: 3 (returned 1-3)" in text
        assert "one.test: 4 messages, 2 threads" in text
        assert "(no attributable sender): 2 messages" in text

    def test_an_empty_result_says_so(self, fake_server, agg_db):
        handler = _handlers(fake_server, agg_db)["aggregate_messages"]
        text = _text(asyncio.run(handler(group_by="folder", folder="Nowhere")))
        assert "total_matches: 0" in text
        assert "No messages match." in text

    def test_rejections_quote_the_value_to_the_caller_only(self, fake_server, agg_db, caplog):
        handler = _handlers(fake_server, agg_db)["aggregate_messages"]
        with caplog.at_level(logging.INFO):
            err = _error(handler(group_by="folder", date_from=f"{MARKER}-01"))
            assert MARKER in err
            err = _error(handler(group_by="folder", cursor=MARKER))
            assert "invalid cursor" in err
        assert MARKER not in caplog.text
        assert "aggregate_messages.date_from" in caplog.text
        assert "aggregate_messages.cursor" in caplog.text

    def test_logs_withhold_filter_values_and_mail(self, fake_server, tmp_path, caplog):
        conn, path = _open_built_db_conn(tmp_path, "marker.db")
        _insert_message(
            conn,
            message_id=f"{MARKER}-mid",
            thread_id=f"t-{MARKER}",
            folder=f"{MARKER}-folder",
            sent_at="2024-01-10T09:00:00+00:00",
            subject=f"about {MARKER}",
            from_=[f"{MARKER} Person <{MARKER.lower()}@example.test>"],
        )
        conn.close()
        handler = _handlers(fake_server, Database(str(path)))["aggregate_messages"]
        with caplog.at_level(logging.DEBUG):
            out = asyncio.run(
                handler(group_by="sender_address", subject=MARKER, folder=f"{MARKER}-folder")
            )
        assert MARKER in _text(out)
        assert MARKER not in caplog.text
        assert MARKER.lower() not in caplog.text
        assert "tool=aggregate_messages {'group_by': 'sender_address', 'limit': 25}" in caplog.text
        [line] = [r.getMessage() for r in caplog.records if r.name == "mcp.timings"]
        assert "outcome=ok" in line
        assert "'total_matches': 1" in line and "'groups': 1" in line

    def test_a_long_group_value_is_returned_whole(self, fake_server, tmp_path):
        # Review round 1: the value is the group's identity, used as an
        # exact address_is / folder follow-up, so it is never cut. The
        # parser stores addresses up to 998 characters.
        address = "a" * 700 + "@long.test"
        conn, path = _open_built_db_conn(tmp_path, "long.db")
        _insert_message(
            conn,
            message_id="m-long",
            thread_id="t-long",
            sent_at="2024-01-10T09:00:00+00:00",
            from_=[address],
        )
        conn.close()
        out = _call(_server(Database(str(path))), "aggregate_messages", group_by="sender_address")
        assert [g["value"] for g in out["groups"]] == [address]
        # Review round 2: the flat sender filter is the follow-up for an
        # address longer than where's 320-character address_is limit.
        assert Database(str(path)).query_messages(sender=address).total_matches == 1

    def test_a_database_error_is_returned_by_type(self, fake_server, agg_db, monkeypatch, caplog):
        def boom(**_):
            raise sqlite3.OperationalError(MARKER)

        monkeypatch.setattr(agg_db, "aggregate_messages", boom)
        handler = _handlers(fake_server, agg_db)["aggregate_messages"]
        with caplog.at_level(logging.INFO):
            assert _error(handler(group_by="folder")) == "Error: OperationalError"
        assert MARKER not in caplog.text
