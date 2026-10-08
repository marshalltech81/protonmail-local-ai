"""The explicit address-mode and ``body_words`` leaves (#1088, PR 1).

``address_is``, ``address_contains``, ``display_name_contains``,
``address_or_name_contains`` and ``domain_is`` each take a role and a
value; ``body_words`` takes the words of today's ``text``. Each answers
1, 0 or NULL (unknown) under the same completeness rules as the leaves
it shares data with: a stored match decides, finding nothing is false
only when the role's addresses are complete (#1086), when the names it
reads are complete (#1140, for the leaves that read display names), and
on the From role only when the sender is not ambiguous (#1153).

The flat ``sender`` / ``recipient`` / ``participant`` / ``text``
leaves compile to these, so their SQL, and so every flat result,
``address_matches`` and ``indeterminate``, is unchanged: pinned below
against a verbatim copy of the compilers they replaced. All data is
synthetic.
"""

import pytest
from src.lib.predicates import (
    LEAVES,
    ROLE_SETS,
    Evaluability,
    Leaf,
    address_match_mode,
    canonical_addr,
    compile_leaves,
    inferred_address_leaf,
)
from src.lib.sqlite import Database

from tests.conftest import _insert_message
from tests.test_evidence_scope import _finish_threads
from tests.test_sender_leaf_indeterminate import _truth
from tests.test_sqlite import _open_built_db_conn

JANE = "Jane Roe <jane@one.test>"
SAM = "Sam Sub <sam@mail.one.test>"
BOB = "bob@two.test"
CAROL = "Carol Vale <carol@three.test>"

# message_id: (From, To, Cc, sender_ambiguous, participant_names_complete,
#              completeness overrides, body)
_ROWS: dict[str, tuple] = {
    "jfrom": (JANE, [BOB], [], 0, 1, {}, "budget approved"),
    "jcc": (BOB, [CAROL], [JANE], 0, 1, {}, "lunch soon"),
    "subto": (BOB, [SAM], [], 0, 1, {}, "lunch soon"),
    "none": (BOB, [CAROL], [], 0, 1, {}, "lunch soon"),
    # One role's addresses incomplete (0 a known loss, NULL unassessed).
    "from0": (BOB, [CAROL], [], 0, 1, {"from_addresses_complete": 0}, "lunch soon"),
    "toN": (BOB, [CAROL], [], 0, 1, {"to_addresses_complete": None}, "lunch soon"),
    "cc0": (BOB, [CAROL], [], 0, 1, {"cc_addresses_complete": 0}, "lunch soon"),
    # Display names incomplete (#1140).
    "names0": (BOB, [CAROL], [], 0, 0, {}, "lunch soon"),
    "namesN": (BOB, [CAROL], [], 0, None, {}, "lunch soon"),
    # The sender cannot be told (#1153): repeated From, or unassessed.
    "amb": (JANE, [CAROL], [], 1, 1, {}, "budget approved"),
    "ambN": (JANE, [CAROL], [], None, 1, {}, "budget approved"),
    # The indexed body incomplete (#1086).
    "body0": (BOB, [CAROL], [], 0, 1, {"body_complete": 0}, "lunch soon"),
    "bodyN": (BOB, [CAROL], [], 0, 1, {"body_complete": None}, "lunch soon"),
}


@pytest.fixture
def db(tmp_path) -> Database:
    conn, path = _open_built_db_conn(tmp_path, "explicit.db")
    for i, (message_id, row) in enumerate(_ROWS.items()):
        from_, to, cc, ambiguous, names, flags, body = row
        _insert_message(
            conn,
            message_id=message_id,
            thread_id=f"t-{message_id}",
            sent_at=f"2024-01-{i + 1:02d}T09:00:00+00:00",
            body=body,
            from_=[from_],
            to=to,
            cc=cc,
            sender_ambiguous=ambiguous,
            participant_names_complete=names,
            completeness=flags,
        )
    _finish_threads(conn)
    conn.close()
    return Database(str(path))


def _table(default: int, **values: int | None) -> dict[str, int | None]:
    assert set(values) <= set(_ROWS)
    return {name: values.get(name, default) for name in _ROWS}


# The From side unknown: its addresses incomplete, or the sender
# ambiguous or unassessed.
_FROM_UNKNOWN = {"from0": None, "amb": None, "ambN": None}
_NAMES_UNKNOWN = {"names0": None, "namesN": None}

# The expected value of each leaf per message: 1, 0 or None (unknown).
_TRUTH: dict[tuple[str, tuple], dict[str, int | None]] = {
    # Canonical equality; reads no display name, so the names flag does
    # not matter.
    ("address_is", ("from", "jane@one.test")): _table(0, jfrom=1, **_FROM_UNKNOWN),
    ("address_is", ("cc", "Jane@One.test")): _table(0, jcc=1, cc0=None),
    ("address_is", ("to", "sam@mail.one.test")): _table(0, subto=1, toN=None),
    ("address_is", ("visible_recipient", "sam@mail.one.test")): _table(
        0, subto=1, toN=None, cc0=None
    ),
    # The address only: "roe" is in Jane's name, not her address.
    ("address_contains", ("from", "roe")): _table(0, **_FROM_UNKNOWN),
    ("address_contains", ("visible_participant", "one.test")): _table(
        0, jfrom=1, jcc=1, subto=1, toN=None, cc0=None, **_FROM_UNKNOWN
    ),
    # The display name only: unknown when a name may be missing.
    ("display_name_contains", ("from", "roe")): _table(
        0, jfrom=1, **_FROM_UNKNOWN, **_NAMES_UNKNOWN
    ),
    ("display_name_contains", ("from", "one.test")): _table(0, **_FROM_UNKNOWN, **_NAMES_UNKNOWN),
    ("display_name_contains", ("cc", "ROE")): _table(0, jcc=1, cc0=None, **_NAMES_UNKNOWN),
    # Today's inferred substring mode: the address or a display name.
    ("address_or_name_contains", ("from", "roe")): _table(
        0, jfrom=1, **_FROM_UNKNOWN, **_NAMES_UNKNOWN
    ),
    ("address_or_name_contains", ("from", "one.test")): _table(
        0, jfrom=1, **_FROM_UNKNOWN, **_NAMES_UNKNOWN
    ),
    # The exact domain: a subdomain is not a match, unlike a substring.
    ("domain_is", ("visible_participant", "one.test")): _table(
        0, jfrom=1, jcc=1, toN=None, cc0=None, **_FROM_UNKNOWN
    ),
    ("domain_is", ("to", "mail.one.test")): _table(0, subto=1, toN=None),
    ("domain_is", ("to", "ONE.test")): _table(0, toN=None),
}


def test_every_row_is_in_every_truth_table():
    for truth in _TRUTH.values():
        assert set(truth) == set(_ROWS)


@pytest.mark.parametrize(("name", "value"), sorted(_TRUTH, key=repr))
def test_truth_table(db, name, value):
    assert _truth(db, Leaf(name, value)) == _TRUTH[(name, value)]


def test_body_words_truth_table(db):
    assert _truth(db, Leaf("body_words", ("budget",))) == _table(
        0, jfrom=1, amb=1, ambN=1, body0=None, bodyN=None
    )
    # Every word, in any order, and stemmed.
    assert _truth(db, Leaf("body_words", ("approve", "budget"))) == _table(
        0, jfrom=1, amb=1, ambN=1, body0=None, bodyN=None
    )


def test_domain_is_differs_from_a_substring_on_a_subdomain(db):
    domain = _truth(db, Leaf("domain_is", ("to", "one.test")))
    substring = _truth(db, Leaf("address_contains", ("to", "one.test")))
    assert domain["subto"] == 0
    assert substring["subto"] == 1


@pytest.mark.parametrize(
    "name",
    [
        "address_is",
        "address_contains",
        "display_name_contains",
        "address_or_name_contains",
        "domain_is",
        "body_words",
    ],
)
def test_evaluability_is_declared(name):
    assert LEAVES[name].evaluability is Evaluability.UNKNOWN_WHEN_NULL


def test_roles_are_the_decided_visible_sets():
    assert ROLE_SETS == {
        "from": ("from",),
        "to": ("to",),
        "cc": ("cc",),
        "visible_recipient": ("to", "cc"),
        "visible_participant": ("from", "to", "cc"),
    }


# ---------------------------------------------------------------------------
# The flat leaves compile to the explicit ones, unchanged
# ---------------------------------------------------------------------------

# A catalogue of value shapes the flat address filters take.
_VALUES = [
    "jane@one.test",
    "Jane@One.TEST",
    "Jane Roe <jane@one.test>",
    "  jane@one.test  ",
    "@one.test",
    "one.test",
    "roe",
    "ROE",
    "Straße",
    "a@",
    "(((((x)))))",
    "jane@one.test, bob@two.test",
]


@pytest.mark.parametrize(
    ("flat", "role"),
    [
        ("sender", "from"),
        ("recipient", "visible_recipient"),
        ("participant", "visible_participant"),
    ],
)
@pytest.mark.parametrize("value", _VALUES)
def test_flat_address_leaf_is_its_inferred_explicit_leaf(flat, role, value):
    explicit = inferred_address_leaf(Leaf(flat, value))
    expected = "address_is" if address_match_mode(value) == "exact" else "address_or_name_contains"
    assert explicit == Leaf(expected, (role, value))
    assert compile_leaves([Leaf(flat, value)]) == compile_leaves([explicit])


@pytest.mark.parametrize("flat", ["sender", "recipient", "participant"])
@pytest.mark.parametrize("value", _VALUES)
def test_flat_address_sql_is_unchanged(flat, value):
    assert compile_leaves([Leaf(flat, value)]) == _legacy_compile(flat, value)


@pytest.mark.parametrize("terms", [("budget",), ("budget", "plan"), ('a"b',)])
def test_flat_text_is_body_words(terms):
    assert compile_leaves([Leaf("text", terms)]) == compile_leaves([Leaf("body_words", terms)])
    assert compile_leaves([Leaf("text", terms)]) == _legacy_text(terms)


@pytest.mark.parametrize(
    "leaf",
    [
        Leaf("sender", "jane@one.test"),
        Leaf("sender", "roe"),
        Leaf("recipient", "@one.test"),
        Leaf("participant", "jane@one.test"),
        Leaf("participant", "one.test"),
        Leaf("text", ("budget",)),
    ],
)
def test_flat_truth_equals_explicit_truth(db, leaf):
    explicit = (
        Leaf("body_words", leaf.value) if leaf.name == "text" else inferred_address_leaf(leaf)
    )
    assert _truth(db, leaf) == _truth(db, explicit)


# Verbatim copy of the compilers on main before #1088 (``predicates.py``
# at 01d5e79b), the ground truth for "unchanged byte for byte".
_LEGACY_ROLES = {
    "sender": ("from",),
    "recipient": ("to", "cc"),
    "participant": ("from", "to", "cc"),
}
_LEGACY_COMPLETE = {
    "from": "from_addresses_complete",
    "to": "to_addresses_complete",
    "cc": "cc_addresses_complete",
}


def _legacy_participant_clause(value: str, roles: tuple[str, ...], params: list) -> str:
    role_sql = ",".join(["?"] * len(roles))
    complete = [f"m.{_LEGACY_COMPLETE[role]} = 1" for role in roles]
    if address_match_mode(value) == "exact":
        params.extend([canonical_addr(value), *roles])
        match = (
            "m.claimant_id IN (SELECT claimant_id FROM message_participants "
            f"WHERE address = ? AND role IN ({role_sql}))"
        )
    else:
        match = (
            "m.claimant_id IN (SELECT p.claimant_id FROM message_participants p "
            f"WHERE {_legacy_substring_rows(value, roles, params)})"
        )
        complete.append("m.participant_names_complete = 1")
    return f"CASE WHEN {match} THEN 1 WHEN {' AND '.join(complete)} THEN 0 ELSE NULL END"


def _legacy_substring_rows(value: str, roles: tuple[str, ...], params: list) -> str:
    role_sql = ",".join(["?"] * len(roles))
    params.extend([*roles, value.strip().lower(), value.strip().casefold()])
    return (
        f"p.role IN ({role_sql}) "
        "AND (instr(p.address, ?) > 0 OR EXISTS (SELECT 1 FROM message_participant_names n "
        "WHERE n.claimant_id = p.claimant_id AND n.role = p.role AND n.address = p.address "
        "AND instr(mcp_casefold(n.name), ?) > 0))"
    )


def _legacy_sender(value: str, params: list) -> str:
    return (
        "CASE WHEN m.sender_ambiguous = 0 THEN "
        f"{_legacy_participant_clause(value, _LEGACY_ROLES['sender'], params)} ELSE NULL END"
    )


def _legacy_recipient(value: str, params: list) -> str:
    return _legacy_participant_clause(value, _LEGACY_ROLES["recipient"], params)


def _legacy_compile(flat: str, value: str) -> tuple[str, list]:
    params: list = []
    if flat == "sender":
        sql = _legacy_sender(value, params)
    elif flat == "recipient":
        sql = _legacy_recipient(value, params)
    else:
        recipient = _legacy_recipient(value, params)
        sql = f"({recipient} OR {_legacy_sender(value, params)})"
    return sql, params


def _legacy_text(terms: tuple[str, ...]) -> tuple[str, list]:
    params = ['"' + term.replace('"', '""') + '"' for term in terms]
    match = " AND ".join(
        "m.claimant_id IN (SELECT c.claimant_id FROM message_chunks_fts f "
        "JOIN message_chunks c ON c.fts_rowid = f.rowid "
        "WHERE message_chunks_fts MATCH ? AND c.attachment_id IS NULL)"
        for _ in terms
    )
    return f"CASE WHEN ({match}) THEN 1 WHEN m.body_complete = 1 THEN 0 ELSE NULL END", params
