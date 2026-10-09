"""``query_messages``' ``where`` parameter (#1088, PR 2: the contract).

``where: {"all": [leaf | {"any": [leaf, ...]}]}`` is one closed leaf
model: ``leaf`` from the six explicit leaves, ``role`` (required on the
five address leaves, refused on ``body_words``), ``value``, ``negate``
and an optional ``id``. The ``any`` groups and ``negate: true`` are in
the schema but refused with fixed text until #1087; ``all`` of leaves
runs as a conjunction with the flat parameters. A node cap, per-leaf
value limits and normalization reject through the rate-limited
per-field path; the cursor digest is versioned and covers the
expression; each leaf reports in its own result list. All data is
synthetic.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging

import pytest
from fastmcp import FastMCP
from pydantic import ValidationError
from src.lib import predicates
from src.lib.argument_validation import ArgumentValidationLog
from src.lib.predicates import (
    MAX_WHERE_NODES,
    MAX_WHERE_VALUE_CHARS,
    WHERE_ADDRESS_LEAVES,
    WHERE_LEAVES,
    WHERE_ROLES,
    InvalidFilterError,
    Leaf,
    Where,
    WhereLeaf,
    normalize_where,
)
from src.lib.sqlite import Database
from src.tools.retrieval import register_retrieval_tools

from tests.conftest import _insert_message
from tests.test_evidence_scope import _finish_threads
from tests.test_explicit_leaves import db as explicit_db  # noqa: F401 (fixture)
from tests.test_retrieval import _error, _handlers, _text
from tests.test_sqlite import _open_built_db_conn
from tests.test_structured_output import _call, _tools, _wire

MARKER = "SYNTHETIC_WHERE_MARKER"


def _where(*items: dict) -> Where:
    return Where.model_validate({"all": list(items)})


def _leaf(leaf: str, value: str, role: str | None = "from", **extra) -> dict:
    item = {"leaf": leaf, "value": value, **extra}
    if leaf in WHERE_ADDRESS_LEAVES and role is not None:
        item["role"] = role
    return item


def _server(db, *, middleware: bool = False) -> FastMCP:
    server = FastMCP("where-test")
    if middleware:
        server.add_middleware(ArgumentValidationLog(server))
    register_retrieval_tools(server, db)
    return server


# ---------------------------------------------------------------------------
# The closed leaf model
# ---------------------------------------------------------------------------


class TestModel:
    @pytest.mark.parametrize("leaf", WHERE_ADDRESS_LEAVES)
    def test_role_is_required_on_an_address_leaf(self, leaf):
        for item in ({"leaf": leaf, "value": "x"}, {"leaf": leaf, "role": None, "value": "x"}):
            with pytest.raises(ValidationError, match="role is required"):
                _where(item)

    @pytest.mark.parametrize("role", [*WHERE_ROLES, None])
    def test_role_is_refused_on_body_words_null_included(self, role):
        with pytest.raises(ValidationError, match="role is not accepted on body_words"):
            _where({"leaf": "body_words", "role": role, "value": "budget"})
        assert _where({"leaf": "body_words", "value": "budget"}).all[0].role is None

    @pytest.mark.parametrize(
        "leaf",
        # The flat filters' inferred names and the internal adapter
        # leaves are not part of ``where``.
        ["sender", "recipient", "participant", "text", "subject", "folder", "authority_class"],
    )
    def test_only_the_six_explicit_leaves(self, leaf):
        with pytest.raises(ValidationError):
            _where({"leaf": leaf, "role": "from", "value": "x"})

    @pytest.mark.parametrize("role", ["visible_participant", "recipient", "any", "bcc", "From"])
    def test_only_the_visible_roles(self, role):
        with pytest.raises(ValidationError):
            _where({"leaf": "address_is", "role": role, "value": "a@example.test"})

    @pytest.mark.parametrize(
        "where",
        [
            {"all": [{"leaf": "body_words", "value": "x", "extra": 1}]},
            {"all": [{"any": [], "extra": 1}]},
            {"all": [], "any": []},
            {"all": [{"any": [{"any": []}]}]},  # no nesting below an any group
        ],
    )
    def test_closed_models(self, where):
        with pytest.raises(ValidationError):
            Where.model_validate(where)

    def test_leaf_list_and_roles_are_the_decided_ones(self):
        assert WHERE_LEAVES == (
            "address_is",
            "address_contains",
            "display_name_contains",
            "address_or_name_contains",
            "domain_is",
            "body_words",
        )
        assert WHERE_ROLES == ("from", "to", "cc", "visible_recipient")
        assert set(WHERE_LEAVES) <= set(predicates.LEAVES)
        assert set(WHERE_ROLES) <= set(predicates.ROLE_SETS)


# ---------------------------------------------------------------------------
# normalize_where: cap, any / negate, values, ids
# ---------------------------------------------------------------------------


def _reject(where: Where) -> str:
    with pytest.raises(InvalidFilterError) as exc:
        normalize_where(where)
    assert exc.value.field_name == "where"
    return str(exc.value)


class TestNormalize:
    def test_leaves_keep_their_path_and_id(self):
        leaves = normalize_where(
            _where(
                _leaf("address_is", "Jane <Jane@Example.test>", id="j"),
                _leaf("body_words", "Budget approved"),
            )
        )
        assert leaves == [
            WhereLeaf("where.all[0]", "j", Leaf("address_is", ("from", "jane@example.test"))),
            WhereLeaf("where.all[1]", None, Leaf("body_words", ("budget", "approved"))),
        ]

    def test_node_cap_counts_leaves_and_any_groups(self, monkeypatch):
        assert len(normalize_where(_where(*[_leaf("body_words", "a")] * MAX_WHERE_NODES))) == 16
        calls = []
        monkeypatch.setattr(predicates, "_text_terms", lambda t: calls.append(t) or ["a"])
        over = [_leaf("body_words", "a")] * (MAX_WHERE_NODES - 1) + [
            {"any": [_leaf("body_words", "a")]}
        ]
        text = _reject(_where(*over))
        assert text == "where holds at most 16 nodes (each leaf and each any group counts one)"
        # Refused before any value is read.
        assert calls == []

    def test_an_empty_all_is_refused(self):
        assert _reject(_where()) == "where.all must hold at least one leaf"

    def test_any_groups_are_refused_until_1087(self):
        text = _reject(_where(_leaf("body_words", "a"), {"any": [_leaf("body_words", "b")]}))
        assert text == (
            "where.all[1]: any groups are not evaluated yet (#1087); list leaves directly in all"
        )

    def test_negate_true_is_refused_and_false_accepted(self):
        text = _reject(_where(_leaf("body_words", "a", negate=True)))
        assert text == "where.all[0]: negate is not evaluated yet (#1087)"
        assert normalize_where(_where(_leaf("body_words", "a", negate=False)))

    @pytest.mark.parametrize("leaf", WHERE_LEAVES)
    def test_blank_values_are_refused(self, leaf):
        assert _reject(_where(_leaf(leaf, "  \t"))) == f"where.all[0]: {leaf} value is empty"

    @pytest.mark.parametrize("leaf", WHERE_LEAVES)
    def test_value_limit_per_leaf(self, leaf):
        limit = MAX_WHERE_VALUE_CHARS[leaf]
        text = _reject(_where(_leaf(leaf, MARKER + "x" * limit)))
        assert text == f"where.all[0]: {leaf} value is longer than {limit} characters"
        assert MARKER not in text

    def test_address_is_takes_a_full_address(self):
        for value in ("jane", "@example.test", "example.test", "Jane Roe"):
            assert _reject(_where(_leaf("address_is", value))) == (
                "where.all[0]: address_is takes a full address (name@example.com)"
            )

    @pytest.mark.parametrize(
        ("value", "domain"),
        [(" Example.TEST ", "example.test"), ("@mail.example.test", "mail.example.test")],
    )
    def test_domain_is_normalization(self, value, domain):
        [leaf] = normalize_where(_where(_leaf("domain_is", value, role="to")))
        assert leaf.leaf == Leaf("domain_is", ("to", domain))

    @pytest.mark.parametrize(
        "value", ["jane@example.test", "exa mple.test", "example..test", ".test", "test.", "@"]
    )
    def test_domain_is_validation(self, value):
        assert _reject(_where(_leaf("domain_is", value))) == (
            "where.all[0]: domain_is takes a domain (example.com)"
        )

    def test_substring_values_are_stripped(self):
        leaves = normalize_where(
            _where(
                _leaf("address_contains", " Jane "),
                _leaf("display_name_contains", " Roe ", role="cc"),
                _leaf("address_or_name_contains", " Ja ", role="visible_recipient"),
            )
        )
        assert [w.leaf for w in leaves] == [
            Leaf("address_contains", ("from", "Jane")),
            Leaf("display_name_contains", ("cc", "Roe")),
            Leaf("address_or_name_contains", ("visible_recipient", "Ja")),
        ]

    def test_body_words_word_rules(self):
        assert _reject(_where(_leaf("body_words", "?!"))) == (
            "where.all[0]: body_words value holds no word"
        )
        words = " ".join(f"w{i}" for i in range(17))
        assert _reject(_where(_leaf("body_words", words))) == (
            "where.all[0]: body_words holds at most 16 words"
        )

    def test_ids(self):
        assert _reject(
            _where(_leaf("body_words", "a", id="x"), _leaf("body_words", "b", id="x"))
        ) == ("where.all[1]: id repeats an earlier leaf's id")
        assert _reject(_where(_leaf("body_words", "a", id=" "))) == "where.all[0]: id is empty"
        assert _reject(_where(_leaf("body_words", "a", id="i" * 65))) == (
            "where.all[0]: id is longer than 64 characters"
        )
        assert len(normalize_where(_where(_leaf("body_words", "a"), _leaf("body_words", "b"))))


# ---------------------------------------------------------------------------
# Database.query_messages with where
# ---------------------------------------------------------------------------


def _claimants(page) -> list[str]:
    return [m.message_id for m in page.messages]


class TestDatabase:
    def test_explicit_leaf_equals_its_flat_filter(self, messages_db):
        flat = messages_db.query_messages(sender="jane@example.com", limit=50)
        explicit = messages_db.query_messages(
            where=normalize_where(_where(_leaf("address_is", "jane@example.com"))), limit=50
        )
        assert _claimants(explicit) == _claimants(flat)
        assert explicit.total_matches == flat.total_matches == 3
        text = messages_db.query_messages(text="budget", limit=50)
        words = messages_db.query_messages(
            where=normalize_where(_where(_leaf("body_words", "budget"))), limit=50
        )
        assert _claimants(words) == _claimants(text)

    def test_where_is_anded_with_the_flat_filters(self, messages_db):
        page = messages_db.query_messages(
            folder="INBOX",
            where=normalize_where(
                _where(_leaf("domain_is", "example.com"), _leaf("body_words", "gracias"))
            ),
            limit=50,
        )
        assert _claimants(page) == ["m5"]
        page = messages_db.query_messages(
            folder="Archive",
            where=normalize_where(_where(_leaf("domain_is", "example.com"))),
            limit=50,
        )
        assert _claimants(page) == ["m3"]

    def test_address_matches_stay_flat_only(self, messages_db):
        page = messages_db.query_messages(
            recipient="@other.org",
            where=normalize_where(_where(_leaf("address_is", "jane@example.com"))),
            limit=50,
        )
        assert list(page.address_matches) == ["recipient"]

    def test_leaf_results(self, messages_db):
        page = messages_db.query_messages(
            where=normalize_where(
                _where(
                    _leaf("domain_is", "other.org", role="visible_recipient", id="dom"),
                    _leaf("body_words", "lunch"),
                )
            ),
            limit=50,
        )
        assert _claimants(page) == ["m3"]
        dom, words = page.leaf_results
        assert (dom.path, dom.id, dom.leaf) == ("where.all[0]", "dom", "domain_is")
        assert (dom.true, dom.false, dom.indeterminate) == (1, 0, 0)
        assert (dom.distinct, dom.addresses) == (1, ["carol@other.org"])
        assert (words.path, words.id, words.leaf) == ("where.all[1]", None, "body_words")
        assert (words.true, words.false, words.indeterminate) == (1, 0, 0)
        assert words.distinct is None and words.addresses is None

    def test_counts_cover_the_messages_the_expression_does_not_reject(
        self,
        explicit_db,  # noqa: F811
    ):
        # Owner decision 2026-10-08 (Option B): the counts cover P, the
        # matches plus the indeterminate messages. "toN" (To addresses
        # not assessed, body complete) is left undecided by the address
        # leaf alone; the body leaf is true of it.
        page = explicit_db.query_messages(
            where=normalize_where(
                _where(
                    _leaf("address_is", "sam@mail.one.test", role="to", id="to"),
                    _leaf("body_words", "lunch", id="body"),
                )
            ),
            limit=50,
        )
        assert _claimants(page) == ["subto"]
        assert (page.total_matches, page.indeterminate) == (1, 1)
        to, body = page.leaf_results
        assert (to.true, to.false, to.indeterminate) == (1, 0, 1)
        assert (body.true, body.false, body.indeterminate) == (2, 0, 0)
        for r in page.leaf_results:
            assert r.true + r.false + r.indeterminate == page.total_matches + page.indeterminate
        # Matched addresses come from the returned messages only.
        assert (to.distinct, to.addresses) == (1, ["sam@mail.one.test"])

    @pytest.mark.parametrize(
        "items",
        [
            [_leaf("display_name_contains", "roe")],
            [_leaf("address_or_name_contains", "one.test", role="visible_recipient")],
            [_leaf("domain_is", "one.test"), _leaf("body_words", "budget")],
            [_leaf("address_contains", "o", role="cc"), _leaf("body_words", "lunch soon")],
        ],
    )
    def test_each_leaf_counts_sum_to_matches_plus_indeterminate(
        self,
        explicit_db,  # noqa: F811
        items,
    ):
        page = explicit_db.query_messages(where=normalize_where(_where(*items)), limit=1)
        for r in page.leaf_results:
            assert r.true + r.false + r.indeterminate == page.total_matches + page.indeterminate

    def test_no_where_no_leaf_results(self, messages_db):
        assert messages_db.query_messages(sender="jane@example.com").leaf_results == []

    def test_undecided_leaf_counts_indeterminate(self, explicit_db):  # noqa: F811
        # "toN" has To addresses not yet assessed: a To leaf that finds
        # nothing there is unknown, not false.
        page = explicit_db.query_messages(
            where=normalize_where(_where(_leaf("address_is", "sam@mail.one.test", role="to"))),
            limit=50,
        )
        assert _claimants(page) == ["subto"]
        assert page.indeterminate == 1

    def test_cursor_is_bound_to_the_where_expression(self, messages_db):
        where = normalize_where(_where(_leaf("address_is", "jane@example.com")))
        first = messages_db.query_messages(where=where, limit=2)
        assert first.next_cursor
        rest = messages_db.query_messages(where=where, limit=2, cursor=first.next_cursor)
        assert _claimants(first) + _claimants(rest) == ["m5", "m3", "m1"]
        for other in (
            # Another value, another leaf, and the same set through the
            # flat parameter (provenance).
            {"where": normalize_where(_where(_leaf("address_is", "bob@example.com")))},
            {"where": normalize_where(_where(_leaf("address_contains", "jane@example.com")))},
            {"sender": "jane@example.com"},
        ):
            with pytest.raises(InvalidFilterError, match="issued for different filters"):
                messages_db.query_messages(**other, limit=2, cursor=first.next_cursor)

    def test_a_pre_upgrade_cursor_is_foreign(self, messages_db):
        # The digest before the format version: date basis and leaves.
        leaves = predicates.query_messages_leaves(
            sender="jane@example.com",
            recipient=None,
            participant=None,
            subject=None,
            text=None,
            folder=None,
            date_from=None,
            date_to=None,
            has_attachments=None,
            authority_class=None,
            seen=None,
            flagged=None,
        )
        old = hashlib.sha256(
            json.dumps(["effective", [[leaf.name, leaf.value] for leaf in leaves]]).encode()
        ).hexdigest()[:16]
        page = messages_db.query_messages(sender="jane@example.com", limit=2)
        cursor = page.next_cursor
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        assert payload["q"] != old
        payload["q"] = old
        stale = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        with pytest.raises(InvalidFilterError, match="issued for different filters"):
            messages_db.query_messages(sender="jane@example.com", limit=2, cursor=stale)

    def test_bounded_work_one_count_statement_and_one_per_address_leaf(
        self, messages_db, monkeypatch
    ):
        statements: list[str] = []
        connect = messages_db._connect

        def traced():
            conn = connect()
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(messages_db, "_connect", traced)
        compiled: list[str] = []
        for name in WHERE_LEAVES:
            kind = predicates.LEAVES[name]
            original = kind.compile

            def spy(value, params, _original=original, _name=name):
                compiled.append(_name)
                return _original(value, params)

            monkeypatch.setitem(
                predicates.LEAVES,
                name,
                predicates.LeafKind(kind.name, kind.param, spy, kind.evaluability),
            )
        items = [
            _leaf("address_contains", "e", role="visible_recipient"),
            _leaf("domain_is", "example.com"),
            _leaf("body_words", "budget"),
        ]
        messages_db.query_messages(where=normalize_where(_where(*items)), limit=1)
        counts = [s for s in statements if "TOTAL(" in s]
        addresses = [s for s in statements if "GROUP BY p.address" in s]
        assert len(counts) == 1
        assert counts[0].count(" AS l") == len(items)
        assert len(addresses) == 2  # one per address leaf
        # Each leaf compiled twice: once into the expression the count,
        # the indeterminate count and the page share, once into the leaf
        # counts.
        assert sorted(compiled) == sorted([w["leaf"] for w in items] * 2)


# ---------------------------------------------------------------------------
# The tool, through the wire
# ---------------------------------------------------------------------------


class TestTool:
    def test_schema_serves_the_closed_model(self, messages_db):
        tool = _tools(_server(messages_db))["query_messages"]
        schema = tool.input_schema
        # FastMCP serves the models inline.
        where = schema["properties"]["where"]["anyOf"][0]
        assert where["additionalProperties"] is False
        leaf, group = where["properties"]["all"]["items"]["anyOf"]
        assert group["additionalProperties"] is False
        assert group["properties"]["any"]["items"] == leaf
        assert leaf["additionalProperties"] is False
        assert leaf["properties"]["leaf"]["enum"] == list(WHERE_LEAVES)
        assert leaf["properties"]["role"]["anyOf"][0]["enum"] == list(WHERE_ROLES)
        assert "description" not in leaf
        desc = " ".join(schema["properties"]["where"]["description"].split())
        assert (
            "``role`` is required on the five address leaves and refused, null included, "
            "on ``body_words``." in desc
        )

    def test_leaf_results_and_flat_address_matches(self, messages_db):
        out = _call(
            _server(messages_db),
            "query_messages",
            recipient="@other.org",
            where={"all": [_leaf("address_is", "Jane@Example.com", id="jane")]},
        )
        assert out["total_matches"] == 2  # m3 and m5: from jane, to an other.org address
        assert [m["filter"] for m in out["address_matches"]] == ["recipient"]
        assert out["leaf_results"] == [
            {
                "path": "where.all[0]",
                "id": "jane",
                "leaf": "address_is",
                "true": 2,
                "false": 0,
                "indeterminate": 0,
                "distinct_addresses": 1,
                "addresses": ["jane@example.com"],
            }
        ]
        assert _call(_server(messages_db), "query_messages")["leaf_results"] == []

    def test_prose_names_each_leaf(self, fake_server, messages_db):
        handler = _handlers(fake_server, messages_db)["query_messages"]
        text = _text(
            asyncio.run(
                handler(
                    where=_where(
                        _leaf("domain_is", "other.org", role="visible_recipient", id="d"),
                        _leaf("body_words", "lunch friday"),
                    )
                )
            )
        )
        assert (
            "Query: where.all[0] domain_is(visible_recipient, 'other.org'), "
            "where.all[1] body_words('lunch', 'friday')" in text
        )
        assert "where.all[0] (id 'd'): true 1, false 0, indeterminate 0; 1 distinct address" in text
        assert "where.all[1]: true 1, false 0, indeterminate 0" in text

    def test_where_leaf_names_its_indeterminate_cause(self, fake_server, explicit_db):  # noqa: F811
        handler = _handlers(fake_server, explicit_db)["query_messages"]
        text = _text(asyncio.run(handler(where=_where(_leaf("body_words", "budget")))))
        assert "indeterminate: 2" in text
        assert "body not fully indexed" in text
        assert "address list incomplete" not in text

    @pytest.mark.parametrize(
        ("item", "causes"),
        [
            (
                _leaf("display_name_contains", "roe"),
                ["sender ambiguous", "address list incomplete", "display names not all"],
            ),
            (_leaf("address_is", "nobody@one.test", role="to"), ["address list incomplete"]),
        ],
    )
    def test_address_leaf_names_its_indeterminate_causes(
        self,
        fake_server,
        explicit_db,  # noqa: F811
        caplog,
        item,
        causes,
    ):
        handler = _handlers(fake_server, explicit_db)["query_messages"]
        with caplog.at_level(logging.INFO):
            text = _text(asyncio.run(handler(where=_where(item))))
        line = next(x for x in text.splitlines() if x.startswith("indeterminate: "))
        everything = ["sender ambiguous", "address list incomplete", "display names not all"]
        for cause in everything:
            assert (cause in line) == (cause in causes)
        assert "body not fully indexed" not in line

    @pytest.mark.parametrize(
        ("item", "reason"),
        [
            ({"any": [_leaf("body_words", "a")]}, "any groups are not evaluated yet (#1087)"),
            (_leaf("body_words", "a", negate=True), "negate is not evaluated yet (#1087)"),
            (_leaf("address_is", MARKER), "address_is takes a full address"),
            (_leaf("body_words", MARKER * 100), "value is longer than"),
        ],
    )
    def test_rejections_are_fixed_text_and_logged_by_field(self, messages_db, caplog, item, reason):
        server = _server(messages_db)
        with caplog.at_level(logging.INFO):
            for _ in range(3):
                result = _wire(server, "query_messages", {"where": {"all": [item]}})
                assert result.is_error
                assert reason in result.content[0].text
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert [w for w in warnings if "rejected" in w] == [
            "rejected invalid argument: query_messages.where"
        ]
        assert MARKER not in caplog.text

    def test_model_rejection_is_logged_by_field(self, messages_db, caplog):
        server = _server(messages_db, middleware=True)
        with caplog.at_level(logging.INFO):
            result = _wire(
                server,
                "query_messages",
                {"where": {"all": [{"leaf": "address_is", "value": MARKER}]}},
            )
        assert result.is_error
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert "rejected invalid argument: query_messages.where" in warnings
        assert MARKER not in caplog.text

    def test_where_values_are_withheld_from_the_log(self, messages_db, caplog):
        with caplog.at_level(logging.DEBUG):
            _call(
                _server(messages_db),
                "query_messages",
                where={"all": [_leaf("address_contains", MARKER.lower(), id=MARKER)]},
            )
        assert MARKER not in caplog.text
        assert MARKER.lower() not in caplog.text
        assert "withheld=['where']" in caplog.text


def _corpus(tmp_path, size: int) -> Database:
    """``size`` synthetic messages: half match both probe leaves, a
    quarter leave the address leaf undecided (To not assessed), a
    quarter are rejected (another recipient, To complete)."""
    conn, path = _open_built_db_conn(tmp_path, f"work-{size}.db")
    for i in range(size):
        kind = i % 4
        _insert_message(
            conn,
            message_id=f"w{i}",
            thread_id=f"t{i}",
            sent_at=f"2024-01-01T{i // 60 % 24:02d}:{i % 60:02d}:00+00:00",
            # Realistic density (#1262): over 20 tokens per chunk, with
            # words that vary per message so the FTS index grows.
            body=(
                f"the quarterly budget approved by the committee covers travel, "
                f"equipment and training for team{i} through the end of year "
                f"{2000 + i % 30}, reference item{i} batch{i % 7} and review{i % 13}"
            ),
            from_=["a@one.test"],
            to=["b@two.test" if kind < 2 else "c@three.test"],
            completeness={"to_addresses_complete": None} if kind == 2 else {},
        )
    _finish_threads(conn)
    conn.close()
    return Database(str(path))


def _probe_leaf_statement(db, monkeypatch, items) -> tuple[dict[str, int], object]:
    """Each leaf's evaluations inside the leaf-count statement, counted
    by wrapping its compiled SQL in a non-deterministic SQL function."""
    statements: list[str] = []
    calls: dict[tuple[int, str], int] = {}
    connect = db._connect

    def probe(tag, value):
        key = (len(statements) - 1, tag)
        calls[key] = calls.get(key, 0) + 1
        return value

    def traced():
        conn = connect()
        conn.create_function("mcp_probe", 2, probe)
        # FTS5 runs its own statements ("-- ..."); count against the
        # top-level statement that started them.
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
    page = db.query_messages(where=normalize_where(_where(*items)), limit=1)
    [index] = [i for i, s in enumerate(statements) if "TOTAL(" in s]
    return {tag: n for (i, tag), n in calls.items() if i == index}, page


@pytest.mark.parametrize("size", [40, 400])
def test_leaf_counts_evaluate_each_leaf_a_bounded_number_of_times_per_row(
    tmp_path, monkeypatch, size
):
    # Owner, 2026-10-08: measure the per-row leaf evaluation of the
    # leaf-count statement (SQLite may flatten the projection into the
    # aggregates) at two sizes; it must be linear in rows × leaves.
    db = _corpus(tmp_path, size)
    items = [_leaf("address_is", "b@two.test", role="to"), _leaf("body_words", "budget")]
    calls, page = _probe_leaf_statement(db, monkeypatch, items)
    p = page.total_matches + page.indeterminate
    assert (page.total_matches, page.indeterminate) == (size // 2, size // 4)
    # The filter reads each leaf at most once per message scanned, and
    # the projection once per message of P.
    assert calls["address_is"] <= size + p
    assert calls["body_words"] <= size + p


def test_database_is_unchanged_without_where(messages_db: Database):
    # Flat calls keep their results; only the digest's format changed.
    page = messages_db.query_messages(sender="jane@example.com", limit=50)
    assert _claimants(page) == ["m5", "m3", "m1"]
    assert page.leaf_results == []


def test_a_where_value_sqlite_cannot_encode_is_answered_by_type(fake_server, messages_db, caplog):
    # Codex round 1: an unpaired surrogate reaches the FTS tokenizer
    # while where is normalized; the handler's error boundary answers
    # and logs it by type, as it does for the flat text filter.
    handler = _handlers(fake_server, messages_db)["query_messages"]
    with caplog.at_level(logging.INFO):
        text = _error(handler(where=_where(_leaf("body_words", f"{MARKER} \ud800"))))
    assert text == "Error: UnicodeEncodeError"
    assert "query_messages error: UnicodeEncodeError" in caplog.text
    assert MARKER not in caplog.text


def test_the_handler_refuses_negate(fake_server, messages_db):
    handler = _handlers(fake_server, messages_db)["query_messages"]
    assert "negate is not evaluated" in _error(
        handler(where=_where(_leaf("body_words", "a", negate=True)))
    )
