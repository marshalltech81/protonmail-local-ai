"""Outage deferrals retain their identity after an earlier failure (#1176)."""

from types import SimpleNamespace

from src import main

from tests.test_main import _connection_error, _make_queue


def test_outage_after_a_real_failure_preserves_attempts_and_marks_deferral(db):
    path = "/maildir/INBOX/cur/synthetic"
    queue = _make_queue(db)
    queue.enqueue(path, "synthetic")
    queue.mark_failed(path, stage="parse", error="ValueError")
    before = db._conn.execute("SELECT attempts FROM indexing_jobs").fetchone()[0]
    main._pause_embedding(
        [SimpleNamespace(row={"filepath": path})], queue, None, _connection_error()
    )
    row = db._conn.execute("SELECT attempts,last_stage FROM indexing_jobs").fetchone()
    assert row[0] == before
    assert row[1] == "embed_deferred"
    counts = queue.heartbeat_counts()
    assert counts["retrying"] == 0
    assert counts["deferred_embed"] == 1
