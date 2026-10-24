"""A NUL-bearing stored address has unknown domain, never a truncated one."""

from contextlib import closing

import pytest
from src.lib.predicates import Leaf, Where, compile_leaves, normalize_where
from src.lib.sqlite import Database

from tests.conftest import _insert_message, claimant_of
from tests.test_sqlite import _open_built_db_conn


@pytest.fixture
def nul_db(tmp_path):
    conn, path = _open_built_db_conn(tmp_path)
    for mid, address in [
        ("bad", "a@one.test\x00b@two.test"),
        ("good", "ok@one.test"),
        ("mixed", "a@one.test\x00b@two.test"),
    ]:
        _insert_message(
            conn,
            message_id=mid,
            thread_id=mid,
            sent_at="2024-01-01T00:00:00+00:00",
            from_=["placeholder@example.test"],
            to=["placeholder@example.test"],
            cc=["placeholder@example.test"],
        )
        conn.execute(
            "UPDATE message_participants SET address = ? WHERE claimant_id = ?",
            (address, claimant_of(mid)),
        )
    conn.execute(
        "INSERT INTO message_participants (claimant_id, role, address, name) VALUES (?, 'to', 'yes@one.test', NULL)",
        (claimant_of("mixed"),),
    )
    conn.commit()
    conn.close()
    return Database(str(path))


@pytest.mark.parametrize("role", ["from", "to", "cc", "recipient", "any"])
@pytest.mark.parametrize("domain", ["one.test", "two.test", "absent.test"])
def test_domain_leaf_keeps_unknowns_and_known_positives(nul_db, role, domain):
    sql, params = compile_leaves([Leaf("domain_is", (role, domain))])
    with closing(nul_db._connect()) as conn:
        truth = dict(conn.execute(f"SELECT message_id, ({sql}) FROM messages m", params))
    assert truth["bad"] is None
    if domain == "one.test":
        assert truth["good"] == 1
        assert truth["mixed"] == (1 if role in ("to", "recipient", "any") else None)


@pytest.mark.parametrize("group", ["sender_domain", "recipient_domain"])
def test_domain_groups_put_nul_addresses_in_unknown_group(nul_db, group):
    page = nul_db.aggregate_messages(group_by=group)
    values = {g.value for g in page.groups}
    assert values == {None, "one.test"}
    unknown = next(g for g in page.groups if g.value is None)
    assert unknown.messages >= 1


@pytest.mark.parametrize("negate", [False, True])
def test_nul_domain_does_not_become_a_match_under_negation(nul_db, negate):
    where = normalize_where(
        Where.model_validate(
            {"all": [{"leaf": "domain_is", "role": "from", "value": "one.test", "negate": negate}]}
        )
    )
    page = nul_db.query_messages(where=where)
    assert page.indeterminate == 2
    assert page.total_matches == (0 if negate else 1)
