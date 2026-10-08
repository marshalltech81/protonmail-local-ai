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

    def test_every_name_one_message_gives_an_address_is_an_alias(self, db):
        """#1140: a header writing one address under two names, and a
        later role repeating it under a third, gives three aliases."""
        msg = make_message(
            message_id="m1@example.com",
            from_addr="Jane Roe <jane@northwind.example>",
            to_addrs=["J. Roe <jane@northwind.example>", "Jane Roe <jane@northwind.example>"],
            cc_addrs=['"Roe, Jane" <jane@northwind.example>'],
        )
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _vec())
        assert _aliases(db, "person:jane@northwind.example") == {
            "Jane Roe",
            "J. Roe",
            "Roe, Jane",
        }

    def test_unparseable_participants_make_no_entity(self, db):
        msg = make_message(
            message_id="m1@example.com",
            from_addr="just a name",
            to_addrs=["also no address"],
        )
        db.upsert_thread(make_thread(messages=[msg], thread_id="t1"), _vec())
        assert _entities(db) == {}


def _tombstone(db, thread_id: str) -> None:
    for row in db.get_thread_messages(thread_id):
        db.add_pending_deletion(row["filepath"], row["claimant_id"], thread_id)


def _reap(db, thread_id: str, survivors: list, reaped: list) -> None:
    """Remove ``reaped`` from ``thread_id`` as the reconciler does:
    tombstone them, then rewrite the thread from ``survivors``."""
    for msg in reaped:
        db.add_pending_deletion(msg.filepath, msg.claimant_id, thread_id)
    removed = db.reap_thread_messages(
        make_thread(messages=survivors, thread_id=thread_id),
        _vec(),
        [m.claimant_id for m in reaped],
    )
    assert removed == [m.filepath for m in reaped]


def _participant_rows(db, claimant_id: str) -> int:
    return db._conn.execute(
        "SELECT COUNT(*) FROM message_participants WHERE claimant_id = ?", (claimant_id,)
    ).fetchone()[0]


class TestEntityPruningOnReap:
    """#464: reaping a message deletes the entities and aliases no
    surviving message mentions, in the reap's own transaction, and
    leaves everything a surviving message still mentions."""

    def test_entity_shared_with_a_survivor_stays(self, db):
        kept = make_message(
            message_id="k@example.com",
            from_addr="Jane Roe <jane@northwind.example>",
            to_addrs=["sam@gmail.com"],
            filepath="/maildir/INBOX/cur/k",
        )
        gone = make_message(
            message_id="g@example.com",
            from_addr="J. Roe <jane@northwind.example>",
            to_addrs=["Pat Lee <pat@contoso.example>"],
            filepath="/maildir/INBOX/cur/g",
        )
        db.upsert_thread(make_thread(messages=[kept, gone], thread_id="t1"), _vec())

        _reap(db, "t1", survivors=[kept], reaped=[gone])

        assert _participant_rows(db, gone.claimant_id) == 0
        assert _entities(db) == {
            "person:jane@northwind.example": (
                "person",
                "jane@northwind.example",
                "org:northwind.example",
            ),
            "org:northwind.example": ("organization", "northwind.example", None),
            "person:sam@gmail.com": ("person", "sam@gmail.com", None),
        }
        # The reaped message's display name goes; the survivor's stays.
        assert _aliases(db, "person:jane@northwind.example") == {"Jane Roe"}
        assert _aliases(db, "person:pat@contoso.example") == set()

    def test_a_later_name_is_pruned_only_when_no_survivor_carries_it(self, db):
        """#1140: an alias the reaped message wrote as an address's
        second name goes; one a survivor also wrote as a second name
        stays."""
        kept = make_message(
            message_id="k@example.com",
            from_addr="Jane Roe <jane@northwind.example>",
            to_addrs=["Sam <sam@contoso.example>", "Samuel <sam@contoso.example>"],
            filepath="/maildir/INBOX/cur/k",
        )
        gone = make_message(
            message_id="g@example.com",
            from_addr="Jane Roe <jane@northwind.example>",
            to_addrs=[
                "Sam <sam@contoso.example>",
                "Samuel <sam@contoso.example>",
                "S. Poe <sam@contoso.example>",
            ],
            filepath="/maildir/INBOX/cur/g",
        )
        db.upsert_thread(make_thread(messages=[kept, gone], thread_id="t1"), _vec())
        assert _aliases(db, "person:sam@contoso.example") == {"Sam", "Samuel", "S. Poe"}

        _reap(db, "t1", survivors=[kept], reaped=[gone])

        assert _aliases(db, "person:sam@contoso.example") == {"Sam", "Samuel"}

    def test_entity_mentioned_in_another_thread_stays(self, db):
        gone = make_message(
            message_id="g@example.com",
            from_addr="Jane Roe <jane@northwind.example>",
            filepath="/maildir/INBOX/cur/g",
        )
        other = make_message(
            message_id="o@example.com",
            from_addr="Sam Poe <sam@contoso.example>",
            to_addrs=["Jane Roe <jane@northwind.example>"],
            filepath="/maildir/INBOX/cur/o",
        )
        db.upsert_thread(make_thread(messages=[gone], thread_id="t1"), _vec())
        db.upsert_thread(make_thread(messages=[other], thread_id="t2"), _vec())
        # bob@example.com (the default recipient) is only in the reaped mail.
        before = {k: v for k, v in _entities(db).items() if "example.com" not in k}

        _tombstone(db, "t1")
        assert db.delete_thread_completely("t1")

        assert _entities(db) == before
        assert _aliases(db, "person:jane@northwind.example") == {"Jane Roe"}

    def test_entity_of_only_reaped_mail_is_removed_with_aliases(self, db):
        gone = make_message(
            message_id="g@example.com",
            from_addr="Jane Roe <jane@northwind.example>",
            to_addrs=["Sam Poe <sam@gmail.com>"],
            filepath="/maildir/INBOX/cur/g",
        )
        db.upsert_thread(make_thread(messages=[gone], thread_id="t1"), _vec())
        assert _entities(db)

        _tombstone(db, "t1")
        assert db.delete_thread_completely("t1")

        assert _entities(db) == {}
        assert db._conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0] == 0

    def test_organization_stays_while_another_person_belongs_to_it(self, db):
        kept = make_message(
            message_id="k@example.com",
            from_addr="a@northwind.example",
            to_addrs=["sam@gmail.com"],
            filepath="/maildir/INBOX/cur/k",
        )
        gone = make_message(
            message_id="g@example.com",
            from_addr="b@northwind.example",
            to_addrs=["sam@gmail.com"],
            filepath="/maildir/INBOX/cur/g",
        )
        db.upsert_thread(make_thread(messages=[kept, gone], thread_id="t1"), _vec())

        _reap(db, "t1", survivors=[kept], reaped=[gone])

        entities = _entities(db)
        assert "person:b@northwind.example" not in entities
        assert "person:a@northwind.example" in entities
        assert "org:northwind.example" in entities

    def test_failed_reap_removes_nothing(self, db, monkeypatch):
        """The prune runs inside the reap's transaction: a failure after
        it rolls the entity deletes back with everything else."""
        kept = make_message(
            message_id="k@example.com",
            from_addr="sam@gmail.com",
            filepath="/maildir/INBOX/cur/k",
        )
        gone = make_message(
            message_id="g@example.com",
            from_addr="Jane Roe <jane@northwind.example>",
            filepath="/maildir/INBOX/cur/g",
        )
        db.upsert_thread(make_thread(messages=[kept, gone], thread_id="t1"), _vec())
        before = (_entities(db), _aliases(db, "person:jane@northwind.example"))

        prune = db._prune_orphan_entities

        def prune_then_fail(cur, mentions):
            prune(cur, mentions)
            assert "person:jane@northwind.example" not in _entities(db)
            raise RuntimeError("synthetic failure after the prune")

        monkeypatch.setattr(db, "_prune_orphan_entities", prune_then_fail)
        with pytest.raises(RuntimeError):
            _reap(db, "t1", survivors=[kept], reaped=[gone])
        _tombstone(db, "t1")
        with pytest.raises(RuntimeError):
            db.delete_thread_completely("t1")

        assert (_entities(db), _aliases(db, "person:jane@northwind.example")) == before
        assert _participant_rows(db, gone.claimant_id) > 0

    def test_prune_touches_only_the_reaped_messages_entities(self, db):
        """The sweep runs per address of the reaped message, each an
        indexed lookup: the number of entity statements does not grow
        with the rest of the table, and none scans a table."""
        for i in range(40):
            msg = make_message(
                message_id=f"u{i}@example.com",
                from_addr=f"User {i} <u{i}@org{i}.example>",
                to_addrs=["sam@gmail.com"],
                filepath=f"/maildir/INBOX/cur/u{i}",
            )
            db.upsert_thread(make_thread(messages=[msg], thread_id=f"u{i}"), _vec())
        kept = make_message(
            message_id="k@example.com",
            from_addr="Sam Poe <sam@gmail.com>",
            filepath="/maildir/INBOX/cur/k",
        )
        gone = make_message(
            message_id="g@example.com",
            from_addr="Jane Roe <jane@northwind.example>",
            to_addrs=["Sam Poe <sam@gmail.com>"],
            filepath="/maildir/INBOX/cur/g",
        )
        db.upsert_thread(make_thread(messages=[kept, gone], thread_id="t1"), _vec())
        total = len(_entities(db))
        assert total > 80

        statements: list[str] = []
        db._conn.set_trace_callback(statements.append)
        try:
            _reap(db, "t1", survivors=[kept], reaped=[gone])
        finally:
            db._conn.set_trace_callback(None)

        entity_statements = [s for s in statements if "entit" in s or "message_participants" in s]
        # Two touched addresses: a handful of lookups each, not one per
        # entity in the table.
        assert 0 < len(entity_statements) <= 10
        for sql in entity_statements:
            plan = " ".join(
                str(r[3]) for r in db._conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
            )
            assert "SCAN" not in plan, (sql, plan)
        assert "person:jane@northwind.example" not in _entities(db)
        # Jane and her organization go; nothing else does.
        assert len(_entities(db)) == total - 2

    def test_prune_work_follows_entities_not_recipients(self, db):
        """Review round 1: a crafted header lists thousands of recipients
        but only ``MAX_ENTITY_PARTICIPANTS_PER_MESSAGE`` get entities.
        The prune issues statements only for addresses that own one, so
        the recipients past the cap cost no per-address work."""
        from src.database import MAX_ENTITY_PARTICIPANTS_PER_MESSAGE

        recipients = [f"R{i} <r{i}@gmail.com>" for i in range(5000)]
        kept = make_message(
            message_id="k@example.com", from_addr="sam@gmail.com", filepath="/maildir/k"
        )
        gone = make_message(
            message_id="g@example.com",
            from_addr="Jane Roe <jane@northwind.example>",
            to_addrs=recipients,
            filepath="/maildir/g",
        )
        db.upsert_thread(make_thread(messages=[kept, gone], thread_id="t1"), _vec())
        assert _participant_rows(db, gone.claimant_id) == 5001

        statements: list[str] = []
        db._conn.set_trace_callback(statements.append)
        try:
            _reap(db, "t1", survivors=[kept], reaped=[gone])
        finally:
            db._conn.set_trace_callback(None)

        entity_statements = [s for s in statements if "entit" in s or "message_participants" in s]
        # A few statements per entity-owning address (existence check,
        # organization lookup, delete, which the trace reports again for
        # the alias cascade) plus the organization sweep and the mention
        # read: not one per recipient (over 15,000 before the filter).
        assert len(entity_statements) <= 4 * MAX_ENTITY_PARTICIPANTS_PER_MESSAGE + 5
        assert set(_entities(db)) == {
            "person:sam@gmail.com",
            "person:bob@example.com",
            "org:example.com",
        }
