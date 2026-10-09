"""``query_messages``' ``where``: Boolean evaluation (#1087).

The schema is #1088's (``{"all": [leaf | {"any": [leaf, ...]}]}``,
``negate`` on a leaf); this evaluates it with three values: ``all`` is
false if any clause is false, else unknown if any is unknown; ``any`` is
true if any leaf is true, else unknown if any is unknown; ``negate``
swaps true and false and keeps unknown. A message the expression leaves
unknown is ``indeterminate``. Each leaf reports its own value over the
messages the expression does not reject, ``negate`` echoed; a negated
leaf lists no addresses. All data is synthetic.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from itertools import product

import pytest
from src.lib import predicates
from src.lib.predicates import (
    MAX_WHERE_NODES,
    QUERY_DIGEST_FORMAT,
    InvalidFilterError,
    normalize_where,
)
from src.lib.sqlite import Database

from tests.conftest import _insert_message
from tests.test_evidence_scope import _finish_threads
from tests.test_retrieval import _error, _handlers, _text
from tests.test_sqlite import _open_built_db_conn
from tests.test_structured_output import _call, _tools
from tests.test_where import (
    MARKER,
    _corpus,
    _leaf,
    _probe_leaf_statement,
    _reject,
    _server,
    _where,
)

N = None  # unknown

# The three-valued tables, written out rather than derived from the
# code under test.
AND = {
    (1, 1): 1, (1, 0): 0, (1, N): N,
    (0, 1): 0, (0, 0): 0, (0, N): 0,
    (N, 1): N, (N, 0): 0, (N, N): N,
}  # fmt: skip
OR = {
    (1, 1): 1, (1, 0): 1, (1, N): 1,
    (0, 1): 1, (0, 0): 0, (0, N): N,
    (N, 1): 1, (N, 0): N, (N, N): N,
}  # fmt: skip
NOT = {1: 0, 0: 1, N: N}

VALUES = (1, 0, N)

# Leaf A is true, false or unknown of a message by its body; leaf B by
# its To addresses.
A = _leaf("body_words", "alpha")
B = _leaf("address_is", "x@a.test", role="to")


def _folder(a, b) -> str:
    return f"f-{a}-{b}"


@pytest.fixture
def truth_db(tmp_path) -> Database:
    """Nine messages, one per (A, B) value pair, each alone in its
    folder so a ``folder`` filter isolates it."""
    conn, path = _open_built_db_conn(tmp_path, "truth.db")
    for i, (a, b) in enumerate(product(VALUES, VALUES)):
        flags = {}
        if a is N:
            flags["body_complete"] = 0  # a parse cap: no word is ruled out
        if b is N:
            flags["to_addresses_complete"] = None  # not assessed yet
        _insert_message(
            conn,
            message_id=f"m-{a}-{b}",
            thread_id=f"t{i}",
            folder=_folder(a, b),
            sent_at=f"2024-01-01T00:{i:02d}:00+00:00",
            body=(
                "the quarterly review covers travel and equipment for the team "
                f"{'alpha' if a == 1 else 'omega'} with notes on budget and staffing plans"
            ),
            from_=["s@one.test"],
            to=["x@a.test" if b == 1 else "y@b.test"],
            completeness=flags,
        )
    _finish_threads(conn)
    conn.close()
    return Database(str(path))


def _value(db: Database, folder: str, *items: dict) -> int | None:
    """The expression's value on the one message in ``folder``: 1 when
    returned, ``None`` when counted indeterminate, 0 otherwise."""
    page = db.query_messages(folder=folder, where=normalize_where(_where(*items)), limit=5)
    assert page.total_matches + page.indeterminate <= 1
    if page.total_matches:
        return 1
    return N if page.indeterminate else 0


def _cases():
    for a, b in product(VALUES, VALUES):
        yield "all", a, b, AND[(a, b)]
        yield "any", a, b, OR[(a, b)]
    for a in VALUES:
        yield "negate", a, 1, NOT[a]


class TestTruthTable:
    @pytest.mark.parametrize(("op", "a", "b", "expected"), list(_cases()))
    def test_three_valued_operators(self, truth_db, op, a, b, expected):
        items = {
            "all": [A, B],
            "any": [{"any": [A, B]}],
            "negate": [{**A, "negate": True}],
        }[op]
        assert _value(truth_db, _folder(a, b), *items) == expected

    @pytest.mark.parametrize(("a", "b"), list(product(VALUES, VALUES)))
    def test_negate_inside_any(self, truth_db, a, b):
        items = [{"any": [{**A, "negate": True}, B]}]
        assert _value(truth_db, _folder(a, b), *items) == OR[(NOT[a], b)]

    @pytest.mark.parametrize(("a", "b"), list(product(VALUES, VALUES)))
    def test_all_of_a_leaf_and_a_negated_group_member(self, truth_db, a, b):
        # A AND (NOT B OR A): a clause and a group sharing a leaf.
        items = [A, {"any": [{**B, "negate": True}, A]}]
        assert _value(truth_db, _folder(a, b), *items) == AND[(a, OR[(NOT[b], a)])]

    def test_unknown_rows_are_indeterminate_across_the_whole_set(self, truth_db):
        page = truth_db.query_messages(where=normalize_where(_where({"any": [A, B]})), limit=50)
        assert page.total_matches == sum(v == 1 for v in OR.values())
        assert page.indeterminate == sum(v is N for v in OR.values())
        page = truth_db.query_messages(
            where=normalize_where(_where({**A, "negate": True})), limit=50
        )
        # NOT A: true where A is false (three B values), unknown where A
        # is unknown; never false for an unknown A.
        assert (page.total_matches, page.indeterminate) == (3, 3)


# ---------------------------------------------------------------------------
# leaf_results under any and negate
# ---------------------------------------------------------------------------


class TestLeafResults:
    def test_counts_are_each_leafs_own_value_over_p_with_negate_echoed(self, truth_db):
        page = truth_db.query_messages(
            where=normalize_where(_where({"any": [{**A, "negate": True, "id": "a"}, B]})),
            limit=50,
        )
        expected = {k: OR[(NOT[k[0]], k[1])] for k in product(VALUES, VALUES)}
        p = [k for k, v in expected.items() if v != 0]
        assert page.total_matches + page.indeterminate == len(p)
        a, b = page.leaf_results
        assert (a.path, a.id, a.leaf, a.negate) == ("where.all[0].any[0]", "a", "body_words", True)
        assert (b.path, b.id, b.leaf, b.negate) == (
            "where.all[0].any[1]",
            None,
            "address_is",
            False,
        )
        # A's own value, before negation, over P.
        assert (a.true, a.false, a.indeterminate) == (
            sum(k[0] == 1 for k in p),
            sum(k[0] == 0 for k in p),
            sum(k[0] is N for k in p),
        )
        assert (b.true, b.false, b.indeterminate) == (
            sum(k[1] == 1 for k in p),
            sum(k[1] == 0 for k in p),
            sum(k[1] is N for k in p),
        )
        for r in page.leaf_results:
            assert r.true + r.false + r.indeterminate == page.total_matches + page.indeterminate

    def test_a_negated_address_leaf_lists_no_addresses(self, truth_db):
        page = truth_db.query_messages(
            where=normalize_where(_where({**B, "negate": True})), limit=50
        )
        [b] = page.leaf_results
        assert b.negate is True
        assert (b.true, b.false, b.indeterminate) == (0, 3, 3)
        assert b.distinct is None and b.addresses is None

    def test_addresses_are_the_leafs_own_matches_on_returned_messages(self, tmp_path):
        # The From leaf is unknown on "amb" (ambiguous sender) though its
        # stored From holds the address; the body leaf returns it. A
        # group member lists only the addresses on returned messages it
        # is itself true of.
        conn, path = _open_built_db_conn(tmp_path, "amb.db")
        for i, (message_id, ambiguous, word) in enumerate(
            [("amb", 1, "alpha"), ("ok", 0, "omega"), ("other", 0, "omega")]
        ):
            _insert_message(
                conn,
                message_id=message_id,
                thread_id=f"t{i}",
                sent_at=f"2024-01-0{i + 1}T09:00:00+00:00",
                body=f"notes on the quarterly plan and the {word} review for the team",
                from_=["jane@one.test" if message_id != "other" else "sam@one.test"],
                to=["x@a.test"],
                sender_ambiguous=ambiguous,
            )
        _finish_threads(conn)
        conn.close()
        db = Database(str(path))
        page = db.query_messages(
            where=normalize_where(
                _where({"any": [_leaf("address_is", "jane@one.test"), A]}),
            ),
            limit=50,
        )
        assert sorted(m.message_id for m in page.messages) == ["amb", "ok"]
        jane, _ = page.leaf_results
        assert (jane.true, jane.false, jane.indeterminate) == (1, 0, 1)
        assert (jane.distinct, jane.addresses) == (1, ["jane@one.test"])
        # Only "amb" returned: the From leaf is true of no returned
        # message, so it lists no address though the rows hold one.
        page = db.query_messages(
            where=normalize_where(_where(A, {"any": [_leaf("address_contains", "jane"), A]})),
            limit=50,
        )
        assert [m.message_id for m in page.messages] == ["amb"]
        _, jane, _ = page.leaf_results
        assert (jane.true, jane.false, jane.indeterminate) == (0, 0, 1)
        assert (jane.distinct, jane.addresses) == (0, [])


# ---------------------------------------------------------------------------
# Normalization: any groups, negate, caps
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_any_leaves_carry_their_path_clause_and_negate(self):
        leaves = normalize_where(
            _where(
                _leaf("body_words", "a"),
                {"any": [_leaf("body_words", "b", negate=True, id="nb"), B]},
            )
        )
        assert [(w.path, w.id, w.clause, w.negate) for w in leaves] == [
            ("where.all[0]", None, 0, False),
            ("where.all[1].any[0]", "nb", 1, True),
            ("where.all[1].any[1]", None, 1, False),
        ]

    def test_an_empty_any_group_is_refused(self):
        assert _reject(_where(A, {"any": []})) == (
            "where.all[1]: an any group holds at least one leaf"
        )

    def test_values_and_ids_inside_a_group_name_their_path(self):
        assert _reject(_where({"any": [A, _leaf("address_is", "jane")]})) == (
            "where.all[0].any[1]: address_is takes a full address (name@example.com)"
        )
        assert _reject(_where({**A, "id": "x"}, {"any": [{**B, "id": "x"}]})) == (
            "where.all[1].any[0]: id repeats an earlier leaf's id"
        )

    @pytest.mark.parametrize(
        ("items", "reason"),
        [
            # Many empty groups fit the node cap (one node each) and are
            # refused as empty; many unary groups (two nodes each) break
            # the cap.
            ([{"any": []}] * MAX_WHERE_NODES, "where.all[0]: an any group holds at least one leaf"),
            (
                [{"any": [A]}] * (MAX_WHERE_NODES // 2 + 1),
                "where holds at most 16 nodes (each leaf and each any group counts one)",
            ),
            (
                [{"any": []}] * (MAX_WHERE_NODES + 1),
                # Refused by the argument model's list cap first.
                None,
            ),
        ],
    )
    def test_worst_cases_are_refused_before_any_sql(
        self, fake_server, truth_db, monkeypatch, items, reason
    ):
        connections = []
        words = []
        monkeypatch.setattr(truth_db, "_connect", lambda: connections.append(1))
        monkeypatch.setattr(predicates, "_text_terms", lambda t: words.append(t) or ["a"])
        if reason is None:
            with pytest.raises(ValueError):
                _where(*items)
        else:
            handler = _handlers(fake_server, truth_db)["query_messages"]
            assert _error(handler(where=_where(*items))) == f"Error: {reason}"
        assert connections == []
        assert words == []

    def test_a_unary_group_means_its_leaf(self, truth_db):
        for a, b in product(VALUES, VALUES):
            folder = _folder(a, b)
            assert _value(truth_db, folder, {"any": [A]}) == _value(truth_db, folder, A)


# ---------------------------------------------------------------------------
# The cursor digest: canonical form, format version
# ---------------------------------------------------------------------------


def _digest(*items: dict) -> str:
    return predicates.leaf_digest([], "effective", normalize_where(_where(*items)))


class TestDigest:
    def test_format_version_is_bumped(self):
        assert QUERY_DIGEST_FORMAT == 3

    def test_leaf_order_within_any_does_not_matter(self):
        c = _leaf("domain_is", "one.test", role="cc", id="c")
        assert _digest({"any": [A, B, c]}) == _digest({"any": [c, {**B, "id": "b"}, A]})
        # A one-leaf group is its leaf.
        assert _digest(A, {"any": [B]}) == _digest({"any": [A]}, B) == _digest(A, B)

    @pytest.mark.parametrize(
        ("one", "other"),
        [
            # Grouping, negation and the order of all's clauses count.
            ([A, B], [{"any": [A, B]}]),
            ([A], [{**A, "negate": True}]),
            ([{"any": [A, B]}], [{"any": [{**A, "negate": True}, B]}]),
            ([A, B], [B, A]),
            ([{"any": [A, B]}, A], [{"any": [A]}, {"any": [B, A]}]),
        ],
    )
    def test_the_digest_covers_grouping_and_negation(self, one, other):
        assert _digest(*one) != _digest(*other)

    def test_a_cursor_pages_under_the_reordered_group(self, truth_db):
        first = truth_db.query_messages(where=normalize_where(_where({"any": [A, B]})), limit=2)
        assert first.next_cursor
        rest = truth_db.query_messages(
            where=normalize_where(_where({"any": [B, A]})), limit=50, cursor=first.next_cursor
        )
        everything = truth_db.query_messages(
            where=normalize_where(_where({"any": [A, B]})), limit=50
        )
        assert [m.message_id for m in first.messages + rest.messages] == [
            m.message_id for m in everything.messages
        ]
        with pytest.raises(InvalidFilterError, match="issued for different filters"):
            truth_db.query_messages(
                where=normalize_where(_where({"any": [{**A, "negate": True}, B]})),
                limit=2,
                cursor=first.next_cursor,
            )

    def test_a_format_1_cursor_is_foreign(self, truth_db):
        where = normalize_where(_where(A))
        page = truth_db.query_messages(where=where, limit=1)
        cursor = page.next_cursor
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        # #1088's format: version 1, the where leaves as [name, value].
        old = hashlib.sha256(
            json.dumps([1, "effective", [], [[w.leaf.name, w.leaf.value] for w in where]]).encode()
        ).hexdigest()[:16]
        assert payload["q"] != old
        payload["q"] = old
        stale = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        with pytest.raises(InvalidFilterError, match="issued for different filters"):
            truth_db.query_messages(where=where, limit=1, cursor=stale)


# ---------------------------------------------------------------------------
# Bounded work
# ---------------------------------------------------------------------------


def test_boolean_leaf_counts_evaluate_each_leaf_a_bounded_number_of_times_per_row(
    tmp_path, monkeypatch
):
    # The same gate as #1088's: per-leaf evaluations in the leaf-count
    # statement, at two sizes with realistic text, under any and negate
    # (a materialized CTE; no flattening into the aggregates).
    items = [
        {
            "any": [
                _leaf("address_is", "b@two.test", role="to"),
                _leaf("body_words", "nonexistentword"),
            ]
        },
        _leaf("address_contains", "zzz", role="from", negate=True),
    ]
    shape = []
    for size in (40, 400):
        with monkeypatch.context() as patch:
            calls, page, connections, statements = _probe_leaf_statement(
                _corpus(tmp_path, size), patch, items
            )
        p = page.total_matches + page.indeterminate
        assert (page.total_matches, page.indeterminate) == (size // 2, size // 4)
        assert set(calls) == {"address_is", "body_words", "address_contains"}
        for name, n in calls.items():
            # Once per message scanned in the filter, once per message
            # of P in the projection.
            assert n <= size + p, name
        shape.append((connections, statements))
    assert shape[0] == shape[1]
    assert shape[0][0] == 1


def test_each_leaf_is_compiled_a_fixed_number_of_times(truth_db, monkeypatch):
    # The expression compiles every leaf once (one subquery per leaf),
    # the counts once more, and the matched addresses of an address
    # leaf sharing a group once more.
    compiled: list[str] = []
    for name in predicates.WHERE_LEAVES:
        kind = predicates.LEAVES[name]

        def spy(value, params, _original=kind.compile, _name=name):
            compiled.append(_name)
            return _original(value, params)

        monkeypatch.setitem(
            predicates.LEAVES,
            name,
            predicates.LeafKind(kind.name, kind.param, spy, kind.evaluability),
        )
    c = _leaf("domain_is", "one.test")
    truth_db.query_messages(
        where=normalize_where(_where({"any": [A, B]}, {**c, "negate": True})), limit=1
    )
    assert sorted(compiled) == sorted(["body_words"] * 2 + ["address_is"] * 3 + ["domain_is"] * 2)


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


class TestTool:
    def test_negate_is_echoed_and_validates_against_the_served_schema(self, truth_db):
        out = _call(
            _server(truth_db),
            "query_messages",
            where={"all": [{"any": [{**A, "negate": True, "id": "na"}, B]}]},
        )
        assert out["total_matches"] == 5  # OR(NOT A, B) true on five pairs
        assert out["indeterminate"] == 3
        na, b = out["leaf_results"]
        assert (na["path"], na["id"], na["negate"]) == ("where.all[0].any[0]", "na", True)
        assert (b["path"], b["negate"]) == ("where.all[0].any[1]", False)
        assert b["addresses"] == ["x@a.test"]
        assert na["addresses"] is None and na["distinct_addresses"] is None

    def test_prose_marks_negated_leaves_and_groups(self, fake_server, truth_db):
        handler = _handlers(fake_server, truth_db)["query_messages"]
        text = _text(
            asyncio.run(handler(where=_where({"any": [{**A, "negate": True}, B]}), limit=1))
        )
        assert (
            "Query: where.all[0].any[0] not body_words('alpha'), "
            "where.all[0].any[1] address_is(to, 'x@a.test')" in text
        )
        assert "where.all[0].any[0] (negated): true " in text

    def test_description_states_the_boolean_rules(self, truth_db):
        schema = _tools(_server(truth_db))["query_messages"].input_schema
        desc = " ".join(schema["properties"]["where"]["description"].split())
        assert "refused for now" not in desc
        assert "negate" in desc and "any" in desc
        assert "a negated leaf lists none" in desc

    def test_values_are_withheld_from_the_log(self, truth_db, caplog):
        with caplog.at_level(logging.DEBUG):
            _call(
                _server(truth_db),
                "query_messages",
                where={
                    "all": [
                        {
                            "any": [
                                _leaf("address_contains", MARKER.lower(), negate=True, id=MARKER),
                                _leaf("body_words", MARKER),
                            ]
                        }
                    ]
                },
            )
        assert MARKER not in caplog.text
        assert MARKER.lower() not in caplog.text
