"""Positive-only observed Bcc semantics; absence never proves no recipient."""

import sqlite3

import pytest
from src.lib.predicates import Where, normalize_where
from src.lib.sqlite import Database
from src.tools.outputs import message_headers

from tests.conftest import _insert_message, claimant_of
from tests.test_direction import identity
from tests.test_evidence_scope import _finish_threads
from tests.test_sqlite import _open_built_db_conn


def where(mode, role, value, negate=False):
    return normalize_where(
        Where.model_validate(
            {"all": [{"leaf": mode, "role": role, "value": value, "negate": negate}]}
        )
    )


@pytest.fixture
def bcc_db(tmp_path):
    conn, path = _open_built_db_conn(tmp_path, "bcc.db")
    for mid, folder, to, bcc in [
        ("sent", "Sent", [], True),
        ("received", "INBOX", [], False),
        ("visible", "INBOX", ["Visible <target@example.test>"], False),
        ("own-only", "Sent", ["own@example.test"], False),
    ]:
        _insert_message(
            conn,
            message_id=mid,
            thread_id=mid,
            folder=folder,
            from_=["own@example.test"],
            to=to,
            sent_at="2024-01-01T00:00:00+00:00",
            completeness={"bcc_addresses_complete": 1},
        )
        if bcc:
            conn.execute(
                "INSERT INTO message_participants VALUES (?, 'bcc', 'target@example.test', 'HiddenMarker')",
                (claimant_of(mid),),
            )
            conn.execute(
                "INSERT INTO message_participant_names VALUES (?, 'bcc', 'target@example.test', 'HiddenMarker')",
                (claimant_of(mid),),
            )
    _finish_threads(conn)
    identity(conn, ["own@example.test"])
    conn.close()
    return Database(str(path))


@pytest.mark.parametrize(
    ("mode", "value"),
    [
        ("address_is", "target@example.test"),
        ("address_contains", "target"),
        ("display_name_contains", "HiddenMarker"),
        ("address_or_name_contains", "HiddenMarker"),
        ("domain_is", "example.test"),
    ],
)
@pytest.mark.parametrize("role", ["bcc", "recipient", "any"])
def test_new_roles_positive_only_even_on_sent(bcc_db, mode, value, role):
    page = bcc_db.query_messages(where=where(mode, role, value), limit=100)
    assert "sent" in {m.message_id for m in page.messages}
    assert page.indeterminate >= 1 or (role == "any" and mode == "domain_is")
    missing = bcc_db.query_messages(
        where=where(mode, role, "absent@other.test" if mode == "address_is" else "other.test"),
        limit=100,
    )
    assert missing.total_matches == 0 and missing.indeterminate == 4
    negated = bcc_db.query_messages(
        where=where(mode, role, "absent@other.test" if mode == "address_is" else "other.test", True)
    )
    assert negated.total_matches == 0 and negated.indeterminate == 4


def test_shared_outputs_direction_visible_roles_and_contacts(bcc_db):
    records = {m.message_id: m for m in bcc_db.query_messages(limit=100).messages}
    msg = records["sent"]
    assert msg.direction == "outbound"
    assert message_headers(msg).model_dump()["bcc_count"] == 1
    assert message_headers(msg).model_dump()["bcc"][0]["address"] == "target@example.test"
    assert bcc_db.get_message_view(msg.claimant_id).record.bcc_count == 1
    assert bcc_db.query_messages(recipient="target@example.test").total_matches == 1
    assert bcc_db.query_messages(participant="HiddenMarker").total_matches == 0
    assert bcc_db.find_contact("HiddenMarker") == []
    contacts = bcc_db.find_contact("target@example.test")
    assert len(contacts) == 1
    assert contacts[0]["thread_count"] == 1
    assert "HiddenMarker" not in str(contacts)
    assert bcc_db.query_messages(direction="self").total_matches == 0


def test_population_readers_share_new_unknowns_and_coverage(bcc_db):
    expr = where("address_is", "bcc", "target@example.test")
    qm = bcc_db.query_messages(where=expr)
    qt = bcc_db.query_threads(where=expr)
    ag = bcc_db.aggregate_messages(group_by="direction", where=expr)
    assert qm.total_matches == qt.total_matching_messages == ag.total_matches == 1
    assert qm.indeterminate == qt.indeterminate_messages == ag.indeterminate == 3
    coverage = bcc_db.get_mailbox_status()["query_capabilities"]["address_is"]
    assert coverage["available_messages"] == 0
    assert coverage["missing_messages"] == 4
    assert coverage["status"] == "supported when present"


def test_unassessed_capture_differs_from_assessed_no_proof(bcc_db):
    with sqlite3.connect(bcc_db.path) as conn:
        conn.execute("UPDATE messages SET bcc_addresses_complete=NULL WHERE message_id='received'")
    entry = bcc_db.get_mailbox_status()["query_capabilities"]["address_is"]
    assert entry["unassessed_messages"] == 1
    assert entry["missing_messages"] == 3
    from src.tools.retrieval import _where_cause_keys

    assert "bcc_provenance" in _where_cause_keys(where("address_is", "bcc", "absent@example.test"))


def test_any_role_does_not_report_ambiguous_from_as_a_matching_witness(bcc_db):
    with sqlite3.connect(bcc_db.path) as conn:
        conn.execute(
            "UPDATE messages SET sender_ambiguous=1, subject='only-this' WHERE message_id='sent'"
        )
    page = bcc_db.query_messages(
        subject="only-this", where=where("address_contains", "any", "@example.test")
    )
    assert page.total_matches == 1
    assert page.leaf_results[0].addresses == ["target@example.test"]


def test_bcc_projection_is_bounded_and_disclosed(bcc_db, fake_server, caplog):
    import asyncio

    from src.tools.retrieval import register_retrieval_tools

    with sqlite3.connect(bcc_db.path) as conn:
        conn.executemany(
            "INSERT INTO message_participants VALUES (?,'bcc',?,NULL)",
            [(claimant_of("sent"), f"a{i:03}@example.test") for i in range(25)],
        )
    register_retrieval_tools(fake_server, bcc_db)
    result = asyncio.run(
        fake_server.tools["query_messages"](folder="Sent", fields=["bcc", "bcc_count"])
    )
    msg = next(
        m for m in result.structured_content["messages"] if m["claimant_id"] == claimant_of("sent")
    )
    assert msg["bcc_count"] == 26 and len(msg["bcc"]) == 10
    assert set(msg) == {"claimant_id", "thread_id", "bcc", "bcc_count"}
    assert "Bcc (observed)" in result.content[0].text
    assert "HiddenMarker" not in caplog.text


def test_pre_bcc_direction_cursor_cannot_resume(bcc_db, monkeypatch):
    import hashlib
    import json

    from src.lib import sqlite as readers

    def old_digest(conn, digest):
        row = conn.execute(
            "SELECT state,address_digest FROM operator_identity WHERE id=1"
        ).fetchone()
        return hashlib.sha256(json.dumps(["direction-v1", digest, list(row)]).encode()).hexdigest()[
            :16
        ]

    with monkeypatch.context() as patch:
        patch.setattr(readers, "_identity_digest", old_digest)
        cursor = bcc_db.query_messages(limit=1).next_cursor
    from src.lib.predicates import InvalidFilterError

    with pytest.raises(InvalidFilterError, match="cursor"):
        bcc_db.query_messages(limit=1, cursor=cursor)
