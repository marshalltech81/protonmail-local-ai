"""Deterministic entity resolution (PLAN Phase 4 item 1).

One person entity per canonical address, with every display name seen
for that address as an alias; one organization entity per exact sender
domain, except common free-mail providers. Nothing merges two
addresses, however similar their names.
"""

import pytest
from src.entities import FREE_MAIL_DOMAINS, organization_domain

from tests.conftest import make_message, make_thread


def _vec() -> list[float]:
    return [1.0] + [0.0] * 4095


def _entities(db) -> dict[str, tuple[str, str, str | None]]:
    return {
        r["entity_id"]: (r["kind"], r["canonical_key"], r["organization_id"])
        for r in db._conn.execute("SELECT * FROM entities")
    }


def _aliases(db, entity_id: str) -> set[str]:
    return {
        r["alias"]
        for r in db._conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ?", (entity_id,)
        )
    }


class TestOrganizationDomain:
    def test_exact_domain_of_the_address(self):
        assert organization_domain("jane@mail.northwind.example") == "mail.northwind.example"

    def test_free_mail_has_no_organization(self):
        for domain in sorted(FREE_MAIL_DOMAINS)[:3]:
            assert organization_domain(f"someone@{domain}") is None

    @pytest.mark.parametrize("value", ["", "no-at-sign", "trailing@"])
    def test_no_domain(self, value):
        assert organization_domain(value) is None


class TestEntityPopulation:
    def test_person_and_organization_entities(self, db):
        msg = make_message(
            message_id="m1@example.com",
            from_addr='"Jane Roe" <Jane@Northwind.example>',
            to_addrs=["sam@gmail.com"],
        )
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _vec())

        assert _entities(db) == {
            "person:jane@northwind.example": (
                "person",
                "jane@northwind.example",
                "org:northwind.example",
            ),
            "org:northwind.example": ("organization", "northwind.example", None),
            # Free-mail address: a person, no organization.
            "person:sam@gmail.com": ("person", "sam@gmail.com", None),
        }
        assert _aliases(db, "person:jane@northwind.example") == {"Jane Roe"}
        assert _aliases(db, "person:sam@gmail.com") == set()

    def test_display_names_of_one_address_are_aliases_of_one_entity(self, db):
        first = make_message(
            message_id="m1@example.com",
            from_addr="Jane Roe <jane@northwind.example>",
            filepath="/maildir/INBOX/cur/m1",
        )
        second = make_message(
            message_id="m2@example.com",
            from_addr="J. Roe (Northwind) <JANE@northwind.example>",
            filepath="/maildir/INBOX/cur/m2",
        )
        db.upsert_thread(make_thread(messages=[first], thread_id="t1"), _vec())
        db.upsert_thread(make_thread(messages=[second], thread_id="t2"), _vec())

        people = [k for k, v in _entities(db).items() if v[1] == "jane@northwind.example"]
        assert people == ["person:jane@northwind.example"]
        assert _aliases(db, "person:jane@northwind.example") == {
            "Jane Roe",
            "J. Roe (Northwind)",
        }

    def test_same_name_on_different_addresses_is_never_merged(self, db):
        msg = make_message(
            message_id="m1@example.com",
            from_addr="Jane Roe <jane@northwind.example>",
            to_addrs=["Jane Roe <jane.roe@contoso.example>"],
        )
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _vec())

        entities = _entities(db)
        assert "person:jane@northwind.example" in entities
        assert "person:jane.roe@contoso.example" in entities
        assert _aliases(db, "person:jane@northwind.example") == {"Jane Roe"}
        assert _aliases(db, "person:jane.roe@contoso.example") == {"Jane Roe"}

    def test_subdomains_are_separate_organizations(self, db):
        msg = make_message(
            message_id="m1@example.com",
            from_addr="a@northwind.example",
            to_addrs=["b@mail.northwind.example"],
        )
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _vec())

        entities = _entities(db)
        assert entities["person:a@northwind.example"][2] == "org:northwind.example"
        assert entities["person:b@mail.northwind.example"][2] == "org:mail.northwind.example"

    def test_reprocessing_is_idempotent(self, db):
        msg = make_message(
            message_id="m1@example.com",
            from_addr="Jane Roe <jane@northwind.example>",
            to_addrs=["Sam Poe <sam@contoso.example>"],
        )
        thread = make_thread(messages=[msg], thread_id="t1")
        db.upsert_thread(thread, _vec())

        def snapshot():
            return (
                db._conn.execute("SELECT * FROM entities ORDER BY entity_id").fetchall(),
                db._conn.execute(
                    "SELECT * FROM entity_aliases ORDER BY entity_id, alias"
                ).fetchall(),
            )

        before = [list(map(tuple, rows)) for rows in snapshot()]
        db.upsert_thread(thread, _vec())
        after = [list(map(tuple, rows)) for rows in snapshot()]
        assert before == after

    def test_unparseable_participants_make_no_entity(self, db):
        msg = make_message(
            message_id="m1@example.com",
            from_addr="just a name",
            to_addrs=["also no address"],
        )
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _vec())
        assert _entities(db) == {}
