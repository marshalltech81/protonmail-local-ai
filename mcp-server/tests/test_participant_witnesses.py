"""Flat participant witnesses must obey the same From safety as the predicate."""

import logging

import pytest
from src.lib.predicates import normalize_where
from src.lib.sqlite import Database

from tests.conftest import _insert_message
from tests.test_sqlite import _open_built_db_conn
from tests.test_where import _where


@pytest.mark.parametrize("ambiguous", [None, 0, 1])
@pytest.mark.parametrize("role", ["to", "cc"])
@pytest.mark.parametrize("needle", ["synthetic.test", "SYNTHETIC_PRIVATE_MARKER"])
def test_flat_participant_reports_only_safe_role_witnesses(
    tmp_path, caplog, ambiguous, role, needle
):
    conn, path = _open_built_db_conn(tmp_path, "witnesses.db")
    _insert_message(
        conn,
        message_id="m",
        thread_id="t",
        sent_at="2024-01-01T00:00:00+00:00",
        from_=["SYNTHETIC_PRIVATE_MARKER <unsafe@synthetic.test>"],
        **{role: ["SYNTHETIC_PRIVATE_MARKER <recipient@synthetic.test>"]},
        sender_ambiguous=ambiguous,
    )
    conn.commit()
    conn.close()
    db = Database(str(path))
    for items in (
        [],
        [
            {
                "any": [
                    {"leaf": "subject_contains", "value": "absent"},
                    {"leaf": "body_words", "value": "absent", "negate": True},
                ]
            }
        ],
    ):
        with caplog.at_level(logging.INFO):
            page = db.query_messages(
                participant=needle, where=normalize_where(_where(*items)) if items else ()
            )
        assert (page.total_matches, page.indeterminate) == (1, 0)
        match = page.address_matches["participant"]
        expected = ["recipient@synthetic.test"]
        if ambiguous == 0:
            expected.append("unsafe@synthetic.test")
        assert match.addresses == expected
        assert match.distinct == len(expected)
    missing = db.query_messages(participant="absent")
    assert (missing.total_matches, missing.indeterminate) == (0, int(ambiguous != 0))
    exact = db.query_messages(participant="recipient@synthetic.test")
    assert exact.address_matches["participant"].addresses == ["recipient@synthetic.test"]
    assert "SYNTHETIC_PRIVATE_MARKER" not in caplog.text
