"""#1612: a stale ``indexed_files`` or occurrence record must not hide a
changed file from parsing forever.

Two round-8 Codex P1 findings on ``occurrence_scan.py`` (a ``held``-path
early return, and ``_hash_unindexed``'s refusal to hash a live
non-dead-lettered file) both came from the same root cause:
``Database.is_indexed()`` and the watcher's rename handling trusted a
path-existence or occurrence record without ever checking whether its
bytes still matched. This file is the regression catalogue for the
panel-approved fix (owner go-ahead, 2026-10-13):

- ``Database.is_indexed_and_fresh`` (size/mtime against ``indexed_files``)
  for the Maildir walk and the watcher's new-mail gate.
- ``occurrence_scan.verify_rename_bytes`` for the watcher's rename
  source, re-checked inside the same transaction as the move.
- The content backstop in ``occurrence_scan.scan_occurrences`` for the
  metadata-collision blind spot neither of the above can see.
- The ``ON CONFLICT DO NOTHING`` queue admission (``STALE_ENQUEUE_SQL``)
  that lets a stale detection request a reparse without resetting a
  pending retry's or dead letter's state.

All fixtures are synthetic (never derived from real mail).
"""

from __future__ import annotations

import os
from dataclasses import replace
from unittest.mock import patch

from src import main
from src.database import EMBEDDING_DIM, Database
from src.occurrence_scan import scan_occurrences
from src.queue import REASON_ON_CREATED, REASON_STALE_CONTENT, IndexingQueue
from src.reconciler import sweep_paths
from src.threader import Threader
from src.timings import TimingAggregator

from tests.conftest import TEST_ACCOUNT, make_mock_embedder
from tests.test_occurrence_reconciliation import MARKER, Box, _eml, _moved, _put

_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIM - 1)


# ---------------------------------------------------------------------------
# Database.is_indexed_and_fresh — the walk's and the watcher's new-mail
# gate's freshness check.
# ---------------------------------------------------------------------------


class TestIsIndexedAndFresh:
    def test_a_path_never_indexed_is_not_fresh(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = tmp_path / "maildir" / "nope"
            assert box.db.is_indexed_and_fresh(str(path)) is False
        finally:
            box.close()

    def test_an_indexed_unchanged_file_is_fresh(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/a1:2,S", _eml("fresh@example.test"))
            box.index(path)
            assert box.db.is_indexed_and_fresh(str(path)) is True
        finally:
            box.close()

    def test_a_replaced_file_with_different_metadata_is_not_fresh(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/a2:2,S", _eml("replaced@example.test"))
            box.index(path)
            assert box.db.is_indexed_and_fresh(str(path)) is True
            # An out-of-band overwrite: no rename event, no re-parse — the
            # scenario a stale indexed_files record used to hide forever.
            path.write_bytes(_eml("different@example.test", "much longer body now"))
            assert box.db.is_indexed_and_fresh(str(path)) is False
            # is_indexed (pure existence) is unaffected: it stays True.
            assert box.db.is_indexed(str(path)) is True
        finally:
            box.close()


class TestWalkRequeuesStaleContent:
    """main.py's Maildir walk (~3340): a stale indexed_files record falls
    through to the normal dead/pending gates instead of being skipped
    forever (design point 1)."""

    def test_a_changed_file_is_enqueued_again(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/w1:2,S", _eml("walked@example.test"))
            box.index(path)
            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            assert not queue.has_pending_row(str(path))

            path.write_bytes(_eml("walked2@example.test", "new body entirely, longer"))

            enqueued = main._enqueue_unindexed_messages(box.db, queue, box.root, "initial_scan")
            assert enqueued == 1
            assert queue.has_pending_row(str(path))
        finally:
            box.close()

    def test_an_unchanged_file_is_not_requeued(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/w2:2,S", _eml("stable@example.test"))
            box.index(path)
            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)

            enqueued = main._enqueue_unindexed_messages(box.db, queue, box.root, "rescan")
            assert enqueued == 0
            assert not queue.has_pending_row(str(path))
        finally:
            box.close()

    def test_a_changed_file_already_dead_lettered_stays_dead(self, tmp_path):
        """The walk's existing dead-letter gate still applies to a stale
        path: changed bytes do not resurrect a row the operator has not
        asked to requeue (``make requeue-dead``)."""
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/w3:2,S", _eml("deadish@example.test"))
            box.index(path)
            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            queue.enqueue(str(path), REASON_ON_CREATED)
            queue.mark_dead_terminal(str(path), stage="parse", error="oversized: too large")

            path.write_bytes(_eml("deadish2@example.test", "changed now, still dead"))

            enqueued = main._enqueue_unindexed_messages(box.db, queue, box.root, "rescan")
            assert enqueued == 0
            assert queue.is_dead(str(path))
        finally:
            box.close()


class TestWatcherDestinationAdmission:
    """main.py's on_moved Case 2 (~1334): the same freshness check gates
    whether a rename destination is treated as already indexed."""

    def test_new_mail_reusing_a_stale_path_is_enqueued(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/d1:2,S", _eml("first@example.test"))
            box.index(path)
            # The path's indexed_files record is now stale: different
            # bytes land there without any observed rename event.
            path.unlink()
            path.write_bytes(_eml("second@example.test", "a different message entirely"))

            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            handler = main.MaildirHandler(box.db, queue)
            tmp_src = box.root / "INBOX" / "tmp" / "incoming"
            tmp_src.parent.mkdir(parents=True)
            tmp_src.write_bytes(b"placeholder")
            with patch.object(main, "MAILDIR_PATH", box.root):
                handler.on_moved(_moved(tmp_src, path))
            assert queue.has_pending_row(str(path))
        finally:
            box.close()


class TestWatcherRenameVerification:
    """main.py's on_moved Case 1 (~1281): an active occurrence's claimant
    is verified against the destination's bytes before being carried
    forward, whether the source has an indexed_files record or not
    (design point 2)."""

    def test_a_genuine_rename_is_trusted_and_carries_the_occurrence(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/r1:2,S", _eml("genuine@example.test"))
            box.index(path)
            dest = box.root / "INBOX/cur/r1:2,RS"
            path.rename(dest)
            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            handler = main.MaildirHandler(box.db, queue)
            with patch.object(main, "MAILDIR_PATH", box.root):
                handler.on_moved(_moved(path, dest))
            assert box.db.find_active_occurrence(str(dest)) is not None
            assert not queue.has_pending_row(str(dest))
        finally:
            box.close()

    def test_a_mismatch_with_an_indexed_files_record_is_not_carried_forward(self, tmp_path, caplog):
        """The metadata-based check: the source has an indexed_files
        record, but the race (something else wrote different content at
        the rename's destination between the OS rename and this event)
        means the destination's bytes no longer match it."""
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/r2:2,S", _eml("before@example.test"))
            box.index(path)
            dest = box.root / "INBOX/cur/r2:2,RS"
            path.rename(dest)
            # Simulates the race: the renamed-to file is replaced before
            # the watcher's event is processed.
            dest.write_bytes(_eml("after@example.test", "a completely different message"))

            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            handler = main.MaildirHandler(box.db, queue)
            with patch.object(main, "MAILDIR_PATH", box.root):
                handler.on_moved(_moved(path, dest))

            # Not carried forward: the occurrence stays at the old,
            # now-nonexistent path rather than being trusted onto dest.
            assert box.db.find_active_occurrence(str(path)) is not None
            assert box.db.find_active_occurrence(str(dest)) is None
            # Treated as new mail instead.
            assert queue.has_pending_row(str(dest))
            assert "did not match" in caplog.text
            assert MARKER not in caplog.text
        finally:
            box.close()

    def test_a_mismatch_with_no_indexed_files_record_is_checked_by_claimant_hash(self, tmp_path):
        """A scan-bound occurrence (``bind_found_copy`` never writes
        ``indexed_files``) is verified by hashing the destination and
        checking it against the claimant ID's embedded hash prefix."""
        box = Box(tmp_path)
        try:
            raw = _eml("scanbound@example.test")
            original = box.put("INBOX/cur/s1:2,S", raw)
            box.index(original)
            copy = box.put("Trash/cur/s2:2,ST", raw)
            counts = scan_occurrences(box.db, box.root, hash_cache={})
            assert counts.bound == 1
            (scan_row,) = [
                o for o in box.occurrences() if o["provenance"] == "scan" and o["state"] == "active"
            ]
            assert scan_row["relative_path"] == "Trash/cur/s2:2,ST"
            # No indexed_files row for the scan-bound copy.
            assert box.db.file_identity(str(copy)) is None

            dest = box.root / "Trash/cur/s2:2,T"
            copy.unlink()
            dest.write_bytes(_eml("other@example.test", "unrelated content"))

            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            handler = main.MaildirHandler(box.db, queue)
            with patch.object(main, "MAILDIR_PATH", box.root):
                handler.on_moved(_moved(copy, dest))

            assert box.db.find_active_occurrence(str(dest)) is None
            assert queue.has_pending_row(str(dest))
        finally:
            box.close()

    def test_a_mismatch_does_not_carry_the_indexed_files_record_either(self, tmp_path):
        """The latent twin of the occurrence-level bug: a stale
        indexed_files record must not be carried under the destination's
        name either, just because the occurrence check failed for a
        different reason."""
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/r3:2,S", _eml("ok@example.test"))
            box.index(path)
            dest = box.root / "INBOX/cur/r3:2,RS"
            path.rename(dest)
            dest.write_bytes(_eml("other@example.test", "replaced before the event"))

            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            handler = main.MaildirHandler(box.db, queue)
            with patch.object(main, "MAILDIR_PATH", box.root):
                handler.on_moved(_moved(path, dest))

            assert box.db.file_identity(str(dest)) is None
        finally:
            box.close()

    def test_a_destination_that_vanishes_before_verification_is_not_trusted(self, tmp_path):
        """The rename lands, but the file is gone again (another race)
        before the watcher's event is processed: nothing to stat, so
        nothing is verified."""
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/r4:2,S", _eml("vanishing@example.test"))
            box.index(path)
            dest = box.root / "INBOX/cur/r4:2,RS"
            path.rename(dest)
            dest.unlink()

            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            handler = main.MaildirHandler(box.db, queue)
            with patch.object(main, "MAILDIR_PATH", box.root):
                handler.on_moved(_moved(path, dest))

            assert box.db.find_active_occurrence(str(dest)) is None
            assert box.db.find_active_occurrence(str(path)) is not None
        finally:
            box.close()


class TestContentBackstop:
    """occurrence_scan.scan_occurrences's content backstop: a metadata
    match alone is never trusted for a held path (design point 3)."""

    def test_equal_metadata_replacement_is_caught_only_by_hashing(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/b1:2,S", _eml("aaaaaaaa@example.test", "AAAA"))
            box.index(path)
            before_stat = path.stat()

            replacement = _eml("bbbbbbbb@example.test", "BBBB")
            assert len(replacement) == len(path.read_bytes())
            path.write_bytes(replacement)
            os.utime(path, ns=(before_stat.st_mtime_ns, before_stat.st_mtime_ns))
            assert path.stat().st_size == before_stat.st_size
            assert path.stat().st_mtime_ns == before_stat.st_mtime_ns

            counts = scan_occurrences(box.db, box.root, hash_cache={})
            assert counts.stale == 1
            assert box.db.find_active_occurrence(str(path)) is None
            assert box.db.queue_get_status(str(path)) == "queued"
        finally:
            box.close()

    def test_an_unchanged_held_path_is_hashed_at_most_once_per_identity(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/b2:2,S", _eml("steady@example.test"))
            box.index(path)
            cache: dict = {}
            first = scan_occurrences(box.db, box.root, hash_cache=cache)
            assert first.stale == 0
            assert first.hashed == 1
            second = scan_occurrences(box.db, box.root, hash_cache=cache)
            assert second.hashed == 0
        finally:
            box.close()

    def test_with_no_hash_cache_the_backstop_is_skipped(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/b3:2,S", _eml("ccccccccc@example.test", "AAAA"))
            box.index(path)
            before_stat = path.stat()
            replacement = _eml("ddddddddd@example.test", "BBBB")
            assert len(replacement) == len(path.read_bytes())
            path.write_bytes(replacement)
            os.utime(path, ns=(before_stat.st_mtime_ns, before_stat.st_mtime_ns))

            counts = scan_occurrences(box.db, box.root)
            assert counts.stale == 0
            assert counts.hashed == 0
            assert box.db.find_active_occurrence(str(path)) is not None
        finally:
            box.close()

    def test_an_unreadable_held_path_is_left_unverified_not_retired(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/b4:2,S", _eml("locked@example.test"))
            box.index(path)
            os.chmod(path, 0)
            try:
                counts = scan_occurrences(box.db, box.root, hash_cache={})
            finally:
                os.chmod(path, 0o600)
            assert (counts.stale, counts.hashed, counts.unverified) == (0, 0, 1)
            assert box.db.find_active_occurrence(str(path)) is not None
        finally:
            box.close()

    def test_the_per_pass_budget_bounds_how_many_held_paths_are_hashed(self, tmp_path, monkeypatch):
        """Bound the work per input (AGENTS.md): a scan pass hashes at
        most ``_BACKSTOP_MAX_FILES`` previously-unverified held paths,
        however many it holds, and the rest wait for a later pass rather
        than making one pass unboundedly expensive."""
        import src.occurrence_scan as scan_module

        monkeypatch.setattr(scan_module, "_BACKSTOP_MAX_FILES", 1)
        box = Box(tmp_path)
        try:
            a = box.put("INBOX/cur/b5:2,S", _eml("budgeta@example.test"))
            b = box.put("INBOX/cur/b6:2,S", _eml("budgetb@example.test"))
            box.index(a, b)
            cache: dict = {}

            first = scan_occurrences(box.db, box.root, hash_cache=cache)
            assert first.hashed == 1

            second = scan_occurrences(box.db, box.root, hash_cache=cache)
            assert second.hashed == 1

            third = scan_occurrences(box.db, box.root, hash_cache=cache)
            assert third.hashed == 0
        finally:
            box.close()

    def test_a_concurrent_retire_leaves_the_path_unreachable_not_stale(self, tmp_path, monkeypatch):
        """The occurrence moved or retired (by the watcher, concurrently)
        between active_occurrences() reading it and the backstop's
        write: not this scan's call to make any more."""
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/b7:2,S", _eml("eeeeeeeee@example.test", "AAAA"))
            box.index(path)
            before_stat = path.stat()
            replacement = _eml("fffffffff@example.test", "BBBB")
            assert len(replacement) == len(path.read_bytes())
            path.write_bytes(replacement)
            os.utime(path, ns=(before_stat.st_mtime_ns, before_stat.st_mtime_ns))

            monkeypatch.setattr(box.db, "retire_mismatched_occurrence", lambda *a, **kw: False)
            counts = scan_occurrences(box.db, box.root, hash_cache={})
            assert (counts.stale, counts.unreachable) == (0, 1)
        finally:
            box.close()


class TestStaleQueueAdmissionPreservesState:
    """Database.queue_enqueue_if_absent (STALE_ENQUEUE_SQL): a repeat
    stale-content detection must not reset a pending retry's or dead
    letter's state (design point 4 and 8)."""

    def test_a_pending_retry_keeps_its_attempts_and_error(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/q1:2,S", _eml("retrying@example.test"))
            queue = IndexingQueue(box.db, max_attempts=5, base_backoff_seconds=30)
            queue.enqueue(str(path), REASON_ON_CREATED)
            queue.mark_failed(str(path), stage="parse", error="boom: transient")
            before = dict(
                box.db._conn.execute(
                    "SELECT status, attempts, last_error FROM indexing_jobs WHERE filepath = ?",
                    (str(path),),
                ).fetchone()
            )
            assert before["attempts"] == 1
            assert before["status"] == "queued"

            inserted = box.db.queue_enqueue_if_absent(str(path), REASON_STALE_CONTENT)

            assert inserted is False
            after = dict(
                box.db._conn.execute(
                    "SELECT status, attempts, last_error FROM indexing_jobs WHERE filepath = ?",
                    (str(path),),
                ).fetchone()
            )
            assert after == before
        finally:
            box.close()

    def test_a_dead_letter_stays_dead(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/q2:2,S", _eml("deadend@example.test"))
            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            queue.enqueue(str(path), REASON_ON_CREATED)
            queue.mark_dead_terminal(str(path), stage="parse", error="oversized: too large")

            inserted = box.db.queue_enqueue_if_absent(str(path), REASON_STALE_CONTENT)

            assert inserted is False
            assert box.db.queue_get_status(str(path)) == "dead"
        finally:
            box.close()

    def test_an_absent_path_is_inserted_queued(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/q3:2,S", _eml("fresh2@example.test"))
            inserted = box.db.queue_enqueue_if_absent(str(path), REASON_STALE_CONTENT)
            assert inserted is True
            assert box.db.queue_get_status(str(path)) == "queued"
        finally:
            box.close()


class TestArchiveModeHashCache:
    """reconciler.sweep_paths: archive mode's call sites now always pass
    a hash cache (design point 5), so a dead-lettered byte-identical copy
    can bind there too, not only in mirror mode."""

    def test_a_dead_lettered_copy_binds_with_a_cache(self, tmp_path):
        root = tmp_path / "maildir"
        raw = _eml("archived@example.test")
        original = _put(root, "INBOX/cur/arc1:2,S", raw)
        db = Database(tmp_path / "mail.db", account=replace(TEST_ACCOUNT, maildir_root=root))
        try:
            queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
            queue.enqueue(str(original), REASON_ON_CREATED)
            with patch.object(main, "MAILDIR_PATH", root):
                main._drain_queue_batched(
                    db,
                    make_mock_embedder(_VECTOR),
                    Threader(db),
                    queue,
                    batch_size=8,
                    timing_aggregator=TimingAggregator(window=4),
                )
            copy = _put(root, "Trash/cur/arc2:2,ST", raw)
            queue.enqueue(str(copy), REASON_ON_CREATED)
            queue.mark_dead_terminal(str(copy), stage="parse", error="oversized: too large")

            result = sweep_paths(db, maildir_root=root, hash_cache={})
            assert result["bound"] == 1
        finally:
            db.close()

    def test_without_a_cache_the_dead_lettered_copy_is_left_unbound(self, tmp_path):
        """The bug this fixes: archive mode's call sites used to pass no
        cache at all, so a dead-lettered T-flagged copy could never bind
        there (round-8 P1 on #1612)."""
        root = tmp_path / "maildir"
        raw = _eml("archived2@example.test")
        original = _put(root, "INBOX/cur/arc3:2,S", raw)
        db = Database(tmp_path / "mail.db", account=replace(TEST_ACCOUNT, maildir_root=root))
        try:
            queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
            queue.enqueue(str(original), REASON_ON_CREATED)
            with patch.object(main, "MAILDIR_PATH", root):
                main._drain_queue_batched(
                    db,
                    make_mock_embedder(_VECTOR),
                    Threader(db),
                    queue,
                    batch_size=8,
                    timing_aggregator=TimingAggregator(window=4),
                )
            copy = _put(root, "Trash/cur/arc4:2,ST", raw)
            queue.enqueue(str(copy), REASON_ON_CREATED)
            queue.mark_dead_terminal(str(copy), stage="parse", error="oversized: too large")

            result = sweep_paths(db, maildir_root=root)
            assert result["bound"] == 0
        finally:
            db.close()


class TestUnchangedBytesReparseKeepsVectors:
    """A reparse the freshness check triggers is still a reparse in
    place: unchanged content keeps the same deterministic chunk IDs and
    vectors (design point 7 and 8)."""

    def test_a_freshness_triggered_reparse_keeps_the_same_chunk_vectors(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/v1:2,S", _eml("vectors@example.test", "stable body text"))
            box.index(path)
            before = [
                dict(r)
                for r in box.db._conn.execute(
                    "SELECT chunk_id, (SELECT embedding FROM message_chunks_vec v "
                    "WHERE v.chunk_id = c.chunk_id) AS embedding FROM message_chunks c "
                    "ORDER BY chunk_id"
                )
            ]
            assert before

            # A stale metadata record without a real content change: the
            # file is untouched, but a corrupted/garbage indexed_files
            # row makes is_indexed_and_fresh say "stale" anyway. Re-
            # parsing the unchanged bytes must still produce the same
            # chunk IDs and vectors.
            box.db._conn.execute(
                "UPDATE indexed_files SET mtime_ns = mtime_ns + 1 WHERE filepath = ?",
                (str(path),),
            )
            box.db._conn.commit()
            assert box.db.is_indexed_and_fresh(str(path)) is False

            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            enqueued = main._enqueue_unindexed_messages(box.db, queue, box.root, "rescan")
            assert enqueued == 1
            with patch.object(main, "MAILDIR_PATH", box.root):
                main._drain_queue_batched(
                    box.db,
                    make_mock_embedder(_VECTOR),
                    Threader(box.db),
                    queue,
                    batch_size=8,
                    timing_aggregator=TimingAggregator(window=4),
                )

            after = [
                dict(r)
                for r in box.db._conn.execute(
                    "SELECT chunk_id, (SELECT embedding FROM message_chunks_vec v "
                    "WHERE v.chunk_id = c.chunk_id) AS embedding FROM message_chunks c "
                    "ORDER BY chunk_id"
                )
            ]
            assert after == before
        finally:
            box.close()
