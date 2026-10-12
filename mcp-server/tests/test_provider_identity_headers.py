"""#1316: header predicates match Proton identity headers as stored text.

Since #1079 every top-level header, ``X-Pm-Internal-Id`` and
``X-Pm-Gluon-Id`` included, is stored as ``message_headers`` evidence of
its own claimant. ``header_contains`` on such a header is a caseless
substring match of each stored field's decoded value (its raw value
only when decoding failed), not a validated logical lookup (#1313): it
counts claimants, so byte versions of one message count apart, it
matches a forged or duplicated field as readily as a genuine one, and
it matches every ID containing the value. All values here are
synthetic.
"""

import pytest
from src.lib.sqlite import Database

from tests.conftest import _insert_message, claimant_of
from tests.test_population_leaves import _query
from tests.test_sqlite import _open_built_db_conn

_PM_ID = "SYNTHPMID1316aaaa=="
_PM_OTHER = "SYNTHPMID1316bbbb=="
_PM_ENCODED = "SYNTHPMID1316cccc=="
_PM_UNDECODED = "SYNTHPMID1316dddd=="

# (message_id, variant, headers_complete, X-Pm-Internal-Id fields in order
# as (raw_value, decoded_value))
_FILES = [
    ("built@mail.example", "", 1, [(_PM_ID, _PM_ID)]),
    # The same message rebuilt with a new MIME boundary: other bytes.
    ("built@mail.example", "rebuilt", 1, [(_PM_ID, _PM_ID)]),
    # A forged field above the genuine one, in another file.
    ("forged@mail.example", "", 1, [(_PM_OTHER, _PM_OTHER), (_PM_ID, _PM_ID)]),
    ("other@mail.example", "", 1, [(_PM_OTHER, _PM_OTHER)]),
    # An encoded word: the decoded value differs from the raw text.
    ("encoded@mail.example", "", 1, [("=?utf-8?q?SYNTHPMID1316cccc=3D=3D?=", _PM_ENCODED)]),
    # Decoding failed: only the raw value is stored.
    ("undecoded@mail.example", "", 1, [(_PM_UNDECODED, None)]),
    # Header capture stopped before any identity field (headers_budget).
    ("capped@mail.example", "", 0, []),
    # Header capture stopped after a matching identity field was kept.
    ("capped-match@mail.example", "", 0, [(_PM_ID, _PM_ID)]),
    # Not yet re-parsed since migration 0013.
    ("pending@mail.example", "", None, []),
]


@pytest.fixture
def identity_db(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "identity.db")
    for i, (mid, variant, complete, values) in enumerate(_FILES):
        _insert_message(
            conn,
            message_id=mid,
            thread_id=f"t{i}",
            sent_at=f"2024-01-01T00:0{i}:00+00:00",
            variant=variant,
        )
        claimant = claimant_of(mid, variant)
        conn.execute(
            "UPDATE messages SET headers_complete = ? WHERE claimant_id = ?",
            (complete, claimant),
        )
        conn.executemany(
            "INSERT INTO message_headers VALUES (?, ?, ?, ?, ?, ?)",
            [
                (claimant, n, "X-Pm-Internal-Id", "x-pm-internal-id", raw, decoded)
                for n, (raw, decoded) in enumerate(values)
            ],
        )
    conn.commit()
    conn.close()
    return Database(str(path))


def _matched(page) -> set[str]:
    return {m.claimant_id for m in page.messages}


def test_identity_value_counts_files_not_logical_messages(identity_db):
    """One Proton ID held by two byte versions, by a forged field in a
    third file and by a kept field of a capped file matches four
    claimants: a stored match decides even when capture is incomplete.
    Files whose capture is incomplete or not yet assessed and that hold
    no matching field are indeterminate, not misses."""
    page = _query(
        identity_db, {"leaf": "header_contains", "name": "X-Pm-Internal-Id", "value": _PM_ID}
    )
    assert _matched(page) == {
        claimant_of("built@mail.example"),
        claimant_of("built@mail.example", "rebuilt"),
        claimant_of("forged@mail.example"),
        claimant_of("capped-match@mail.example"),
    }
    assert (page.total_matches, page.indeterminate) == (4, 2)


def test_identity_value_is_a_caseless_substring_not_an_equality(identity_db):
    """A shared prefix in another case matches every stored ID that
    contains it, so the predicate cannot stand in for an ID lookup; and
    a duplicated field matches on its first occurrence as well as its
    last."""
    page = _query(
        identity_db,
        {"leaf": "header_contains", "name": "x-pm-internal-id", "value": "synthpmid1316"},
    )
    assert (page.total_matches, page.indeterminate) == (7, 2)
    assert claimant_of("other@mail.example") in _matched(page)
    # The forged file's first field holds _PM_OTHER, its last _PM_ID.
    first_only = _query(
        identity_db, {"leaf": "header_contains", "name": "X-Pm-Internal-Id", "value": _PM_OTHER}
    )
    assert _matched(first_only) == {
        claimant_of("forged@mail.example"),
        claimant_of("other@mail.example"),
    }


def _contains(db, value):
    return _matched(
        _query(db, {"leaf": "header_contains", "name": "X-Pm-Internal-Id", "value": value})
    )


def test_identity_value_is_matched_decoded_first(identity_db):
    """A decoded value is matched in place of its raw text: the ID an
    encoded word decodes to matches, the encoded-word text does not; a
    field whose decoding failed is matched on its raw value."""
    encoded = claimant_of("encoded@mail.example")
    assert _contains(identity_db, _PM_ENCODED) == {encoded}
    assert encoded not in _contains(identity_db, "?q?SYNTHPMID1316cccc=3D")
    assert _contains(identity_db, _PM_UNDECODED) == {claimant_of("undecoded@mail.example")}
