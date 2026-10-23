"""Cancelled deletion obligations clear their blocked reaper episode."""

import logging

import pytest

from tests.test_occurrence_reconciliation import MARKER, Box, _eml


@pytest.mark.parametrize("cancelled", [False, True])
def test_blocked_reap_episode_tracks_current_deletion_obligations(tmp_path, caplog, cancelled):
    box = Box(tmp_path, grace_days=30)
    try:
        path = box.put("INBOX/cur/blocked:2,S", _eml("blocked@example.test"))
        box.index(path)
        thread_id = box.db._conn.execute("SELECT thread_id FROM messages").fetchone()[0]
        path.unlink()
        box.reconciler.sweep()
        box.reconciler._record_blocked(thread_id, "(no active occurrence)")
        box.reconciler._escalated_threads.add(thread_id)
        if cancelled:
            box.put("INBOX/cur/blocked:2,S", _eml("blocked@example.test"))
            box.index(path)
            assert box.tombstones() == []
        caplog.set_level(logging.INFO)
        caplog.clear()
        box.reconciler.reap()
        assert (thread_id in box.reconciler._blocked_thread_attempts) is not cancelled
        assert (thread_id in box.reconciler._escalated_threads) is not cancelled
        if cancelled:
            assert "1 blocked thread(s) no longer have pending deletions" in caplog.text
        assert MARKER not in caplog.text
    finally:
        box.close()
