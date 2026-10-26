"""A recent heartbeat is not evidence of a complete Maildir walk."""

import sqlite3
from contextlib import closing

import pytest
from src.tools.system import _mailbox_status, _render


def test_no_walk_is_unknown(empty_db):
    out = _mailbox_status(empty_db)
    assert out.maildir_walk is None
    assert "Maildir walk: unknown" in _render(out)


@pytest.mark.parametrize(
    "unreadable,attempt,expected",
    [
        (0, "0123456789ab-1", True),
        (1, "0123456789ab-1", False),
        (0, "0123456789ab-2", False),
        (0, None, None),
    ],
)
def test_enumeration_requires_a_readable_walk_of_the_latest_sync(
    empty_db,
    unreadable,
    attempt,
    expected,
):
    with closing(sqlite3.connect(empty_db.path)) as conn:
        conn.execute(
            "INSERT INTO ingestion_walks "
            "(account_id, root, started_at, completed_at, sync_completed_at, "
            "sync_interval_secs, sync_attempt_id, sync_generation, files_seen, "
            "unreadable_directories, enqueued) "
            "VALUES (1, '/maildir', ?, ?, ?, 60, '0123456789ab-1', 1, 3, ?, 2)",
            ("2026-10-25T10:01:00Z", "2026-10-25T10:02:00Z", "2026-10-25T10:00:00Z", unreadable),
        )
        if attempt is not None:
            conn.execute(
                "INSERT INTO account_collection_state "
                "(account_id, status, success_attempt_id, observed_at, attempt_id, "
                "attempt_started_at, attempt_outcome, run_deadline_secs, sync_interval_secs, "
                "success_completed_at, success_delivered) "
                "VALUES (1, 'known', ?, ?, ?, ?, 'succeeded', 300, 60, ?, 1)",
                (
                    attempt,
                    "2026-10-25T10:02:00Z",
                    attempt,
                    "2026-10-25T10:00:00Z",
                    "2026-10-25T10:00:00Z",
                ),
            )
        conn.commit()
    out = _mailbox_status(empty_db)
    walk = out.maildir_walk
    assert walk is not None
    assert walk.enumerated_latest_sync is expected
    assert walk.unreadable_directories == unreadable
    assert walk.files_seen == 3 and walk.enqueued == 2
    rendered = _render(out)
    assert "Maildir walk:" in rendered
    assert "/maildir" not in rendered
    assert "root" not in walk.model_dump()
