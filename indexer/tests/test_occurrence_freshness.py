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

    def test_the_same_content_recreated_after_retirement_rebinds(self, tmp_path):
        """Round-8 P1 (occurrence_scan.py ~420): a complete scan retires
        an occurrence whose file vanished, and the same bytes come back
        later at the same path with a new mtime (mbsync sets it from
        IMAP's INTERNALDATE, so a redelivery or a restore gets a fresh
        one). The retired occurrence's stale ``indexed_files`` row must
        not make the watcher or the walk skip the path forever: the
        freshness check (not ``_hash_unindexed``, which still only hashes
        a trashed or dead-lettered file) is what unsticks this, since a
        metadata mismatch here means the walk's own gate no longer
        trusts the record."""
        box = Box(tmp_path)
        try:
            raw = _eml("returning@example.test")
            path = box.put("INBOX/cur/w4:2,S", raw)
            box.index(path)
            before_claimant = box.occurrences()[0]["claimant_id"]
            recorded = box.db.file_identity(str(path))
            path.unlink()
            scan_occurrences(box.db, box.root)
            assert box.db.find_active_occurrence(str(path)) is None
            # Still indexed (stale): retiring an occurrence never touches
            # indexed_files.
            assert box.db.is_indexed(str(path))

            path.write_bytes(raw)
            # A new mtime, deterministically different from the recorded
            # one (mbsync sets it from IMAP's INTERNALDATE on a
            # redelivery or restore, not from "now").
            os.utime(path, ns=(recorded["mtime_ns"] + 1, recorded["mtime_ns"] + 1))

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

            after = box.db.find_active_occurrence(str(path))
            assert after is not None
            assert after["claimant_id"] == before_claimant
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

    def test_a_repeatedly_renamed_unindexed_file_resets_its_queue_state_each_time(self, tmp_path):
        """A path that never makes it past Case 2 — never indexed, so
        never gets an active occurrence — gets a fresh ``attempts=0``
        queued row on every rename event, even though its bytes never
        change. This is intended (#1612/#1627 panel): ``Queue.enqueue``
        (``INSERT OR REPLACE``) always resets prior state, so the total
        number of retries such a path can accumulate is bounded only by
        how many times it is renamed, never by ``max_attempts`` — that
        only bounds failures between one admission and the next. A
        ``DO NOTHING`` admission here (as ``queue_enqueue_if_absent``
        uses for the content backstop) would instead leave a
        dead-lettered destination stuck dead when fresh bytes land on it
        (see the next test)."""
        box = Box(tmp_path)
        try:
            raw = _eml("flapping@example.test")
            path = box.put("INBOX/cur/g1:2,S", raw)
            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            handler = main.MaildirHandler(box.db, queue)

            queue.enqueue(str(path), REASON_ON_CREATED)
            queue.mark_failed(str(path), stage="parse", error="boom: transient")
            queue.mark_failed(str(path), stage="parse", error="boom: transient")
            attempts = box.db._conn.execute(
                "SELECT attempts FROM indexing_jobs WHERE filepath = ?", (str(path),)
            ).fetchone()[0]
            assert attempts == 2  # one short of max_attempts=3; not yet dead

            # Renamed away, same bytes: Case 2 (no active occurrence, not
            # indexed at the source) resets the destination's row.
            dest = box.root / "INBOX/cur/g1:2,RS"
            path.rename(dest)
            with patch.object(main, "MAILDIR_PATH", box.root):
                handler.on_moved(_moved(path, dest))
            row = dict(
                box.db._conn.execute(
                    "SELECT status, attempts FROM indexing_jobs WHERE filepath = ?", (str(dest),)
                ).fetchone()
            )
            assert row == {"status": "queued", "attempts": 0}

            # Burn attempts again at the new name, then rename back to
            # the original name: a second rename event resets a second
            # time. Nothing requires exhausting max_attempts in between.
            queue.mark_failed(str(dest), stage="parse", error="boom: transient")
            queue.mark_failed(str(dest), stage="parse", error="boom: transient")
            dest.rename(path)
            with patch.object(main, "MAILDIR_PATH", box.root):
                handler.on_moved(_moved(dest, path))
            row = dict(
                box.db._conn.execute(
                    "SELECT status, attempts FROM indexing_jobs WHERE filepath = ?", (str(path),)
                ).fetchone()
            )
            assert row == {"status": "queued", "attempts": 0}
        finally:
            box.close()

    def test_new_bytes_onto_a_dead_lettered_path_are_retried_from_a_clean_state(self, tmp_path):
        """A destination whose queue row is already ``dead`` (a prior
        attempt under that path name failed permanently) is not left
        dead when a rename delivers new content onto it: Case 2's
        ``queue.enqueue`` resets it to a fresh ``queued`` row, so the
        replacement is retried instead of silently staying unindexed
        behind a stale dead letter (#1612/#1627 panel). Contrast
        ``TestStaleQueueAdmissionPreservesState``, where the content
        backstop's ``queue_enqueue_if_absent`` must NOT disturb an
        existing dead letter — the two admission paths have opposite
        requirements by design."""
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/h1:2,S", _eml("willdie@example.test", "too big"))
            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            handler = main.MaildirHandler(box.db, queue)
            queue.enqueue(str(path), REASON_ON_CREATED)
            queue.mark_dead_terminal(str(path), stage="parse", error="oversized: too large")
            assert box.db.queue_get_status(str(path)) == "dead"

            # New content replaces the file under the same path name,
            # delivered as a rename from a temp file (mbsync's own
            # delivery pattern): the destination is never re-read here,
            # only its current on-disk bytes matter.
            path.write_bytes(_eml("replacement@example.test", "normal size now"))
            tmp_src = box.root / "INBOX" / "tmp" / "incoming"
            tmp_src.parent.mkdir(parents=True, exist_ok=True)
            tmp_src.write_bytes(b"placeholder")
            with patch.object(main, "MAILDIR_PATH", box.root):
                handler.on_moved(_moved(tmp_src, path))

            assert box.db.queue_get_status(str(path)) == "queued"
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

    def test_a_same_size_same_mtime_replacement_is_hashed_before_being_carried_forward(
        self, tmp_path, caplog
    ):
        """Fresh evidence beyond the metadata-mismatch case above (round
        12): the replacement can also match the source's recorded size
        *and* mtime (mbsync sets mtime from IMAP's INTERNALDATE, so two
        different messages can legitimately share one — the same
        reasoning ``occurrence_scan``'s content backstop and
        ``_move_occurrence``'s #1612 sibling fix (d8f11362) both rest
        on). A metadata-only check cannot tell the replacement apart
        from the old claimant's bytes, so the content hash must be
        checked before the occurrence is carried forward."""
        box = Box(tmp_path)
        try:
            old_raw = _eml("before@example.test")
            path = box.put("INBOX/cur/r3:2,S", old_raw)
            box.index(path)
            stat_before = path.stat()

            new_raw = _eml("after@example.test", body="a completely different payload")
            pad = len(old_raw) - len(new_raw)
            new_raw = (
                new_raw[:-2] + b" " * pad + new_raw[-2:]
                if pad > 0
                else new_raw[: pad - 2] + new_raw[-2:]
            )
            assert len(new_raw) == len(old_raw) and new_raw != old_raw
            path.write_bytes(new_raw)
            os.utime(path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))

            dest = box.root / "INBOX/cur/r3:2,RS"
            path.rename(dest)

            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            handler = main.MaildirHandler(box.db, queue)
            with patch.object(main, "MAILDIR_PATH", box.root):
                handler.on_moved(_moved(path, dest))

            # Not carried forward: the occurrence stays at the old,
            # now-nonexistent path rather than being trusted onto dest
            # on a size/mtime match alone.
            assert box.db.find_active_occurrence(str(path)) is not None
            assert box.db.find_active_occurrence(str(dest)) is None
            # Treated as new mail instead.
            assert queue.has_pending_row(str(dest))
            assert "did not match" in caplog.text
            assert MARKER not in caplog.text
        finally:
            box.close()

    def test_a_mismatch_onto_an_already_trashed_destination_retires_the_source(
        self, tmp_path, caplog
    ):
        """#1612 round 13: when mirror retention is enabled and the
        rename that exposes the mismatch also adds the ``T`` flag, Case
        2 skips the destination entirely (``_is_reaped_or_deleted``), so
        nothing else ever revisits this rename — the periodic
        reconciliation scan only retires an occurrence whose uniq
        resolves to no file at all, never one whose uniq still resolves
        to *some* file, however mismatched. Without an explicit retire
        here, the stale occurrence would stay active at a path that no
        longer exists, keeping the old message searchable forever."""
        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/r5:2,S", _eml("before@example.test"))
            box.index(path)
            # Content replaced out of band, then renamed in the same
            # folder with the T flag added — the trashed-rename case the
            # finding describes.
            path.write_bytes(_eml("after@example.test", "a completely different message"))
            dest = box.root / "INBOX/cur/r5:2,ST"
            path.rename(dest)

            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            # Mirror mode: only it has a trashed-destination skip.
            handler = main.MaildirHandler(box.db, queue, reconciler=box.reconciler)
            with patch.object(main, "MAILDIR_PATH", box.root):
                handler.on_moved(_moved(path, dest))

            # Retired, not left dangling: no active occurrence anywhere.
            assert box.active() == []
            # Case 2's trashed skip is unchanged: the mismatched
            # replacement is still not indexed as new mail.
            assert not queue.has_pending_row(str(dest))
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


class TestWatcherFallsThroughOnAnUnboundRename:
    """main.py's on_moved Case 1 (~1313): with no active occurrence at
    the source, ``take_case_1`` is set from ``Database.is_indexed``
    alone — a retired path can still carry a stale ``indexed_files``
    row. When that path is reused for different bytes and renamed,
    ``_move_occurrence``'s own metadata check correctly refuses to bind
    or move anything (round 9's guard), but both Case 1 branches used
    to return anyway instead of falling through to Case 2: the
    replacement went unindexed until the periodic Maildir rescan
    (#1612 round 10 review)."""

    def _retired_stale_path(self, box: Box, relative: str, message_id: str):
        """Index a message, then retire its occurrence by deleting the
        file and running a complete scan: ``find_active_occurrence`` is
        ``None`` afterward, but ``is_indexed`` stays ``True`` (stale) —
        ``indexed_files`` rows are never touched by retirement."""
        path = box.put(relative, _eml(message_id))
        box.index(path)
        path.unlink()
        scan_occurrences(box.db, box.root)
        assert box.db.find_active_occurrence(str(path)) is None
        assert box.db.is_indexed(str(path))
        return path

    def test_archive_mode_falls_through_to_case_2(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = self._retired_stale_path(box, "INBOX/cur/f1:2,S", "retiredstale1@example.test")
            dest = box.root / "INBOX/cur/f1:2,RS"
            dest.write_bytes(_eml("different1@example.test", "a completely different message"))

            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            # No reconciler: the archive-mode Case 1 branch.
            handler = main.MaildirHandler(box.db, queue)
            with patch.object(main, "MAILDIR_PATH", box.root):
                handler.on_moved(_moved(path, dest))

            assert box.db.find_active_occurrence(str(dest)) is None
            assert queue.has_pending_row(str(dest))
        finally:
            box.close()

    def test_mirror_mode_falls_through_to_case_2(self, tmp_path):
        box = Box(tmp_path)
        try:
            path = self._retired_stale_path(box, "INBOX/cur/f2:2,S", "retiredstale2@example.test")
            dest = box.root / "INBOX/cur/f2:2,RS"
            dest.write_bytes(_eml("different2@example.test", "a completely different message"))

            queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
            # ``box.reconciler``: the mirror-mode Case 1 branch
            # (``Reconciler.handle_moved``).
            handler = main.MaildirHandler(box.db, queue, reconciler=box.reconciler)
            with patch.object(main, "MAILDIR_PATH", box.root):
                handler.on_moved(_moved(path, dest))

            assert box.db.find_active_occurrence(str(dest)) is None
            assert queue.has_pending_row(str(dest))
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
            # The second file waits for a later pass's fresh budget —
            # counted, not left silent (#1612 round 10 review).
            assert first.backstop_deferred == 1

            second = scan_occurrences(box.db, box.root, hash_cache=cache)
            assert second.hashed == 1
            assert second.backstop_deferred == 0

            third = scan_occurrences(box.db, box.root, hash_cache=cache)
            assert third.hashed == 0
            assert third.backstop_deferred == 0
        finally:
            box.close()

    def test_a_single_oversized_file_is_not_hashed_over_the_byte_budget(
        self, tmp_path, monkeypatch
    ):
        """Bound the work per input (AGENTS.md): a file whose own size
        exceeds the whole per-pass byte budget must not be hashed
        either. Checking only bytes already consumed
        (``budget["bytes"] >= cap``) lets the very first, oversized
        file through untouched, since 0 is never >= a positive cap
        (#1612 round 8 review) — the check must be against the
        prospective total. Every pass hits the same return for this
        file (its own size always exceeds the whole budget), so it is
        never backstop-verified; this must still be counted, not left
        silent (#1612 round 10 review)."""
        import src.occurrence_scan as scan_module

        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/b8:2,S", _eml("oversized@example.test"))
            box.index(path)
            # Just under the file's own size: it alone would blow the
            # budget, whatever else has (or has not) been hashed yet.
            monkeypatch.setattr(scan_module, "_BACKSTOP_MAX_BYTES", path.stat().st_size - 1)
            counts = scan_occurrences(box.db, box.root, hash_cache={})
            assert counts.hashed == 0
            assert counts.backstop_deferred == 1
            assert box.db.find_active_occurrence(str(path)) is not None
        finally:
            box.close()

    def test_the_byte_budget_bounds_accumulated_held_paths_too(self, tmp_path, monkeypatch):
        """Two files, neither alone over budget, together are: the
        prospective-total check defers the second to a later pass
        exactly as the files-count budget already does."""
        import src.occurrence_scan as scan_module

        box = Box(tmp_path)
        try:
            a = box.put("INBOX/cur/b9:2,S", _eml("accuma@example.test"))
            b = box.put("INBOX/cur/b10:2,S", _eml("accumb@example.test"))
            box.index(a, b)
            size = a.stat().st_size
            # Room for one of the two same-sized files per pass, not both.
            monkeypatch.setattr(scan_module, "_BACKSTOP_MAX_BYTES", size + size // 2)
            cache: dict = {}
            first = scan_occurrences(box.db, box.root, hash_cache=cache)
            assert first.hashed == 1
            assert first.backstop_deferred == 1
            second = scan_occurrences(box.db, box.root, hash_cache=cache)
            assert second.hashed == 1
            assert second.backstop_deferred == 0
            third = scan_occurrences(box.db, box.root, hash_cache=cache)
            assert third.hashed == 0
            assert third.backstop_deferred == 0
            assert box.db.find_active_occurrence(str(a)) is not None
            assert box.db.find_active_occurrence(str(b)) is not None
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

    def test_a_file_restored_after_hashing_is_not_retired_on_stale_evidence(
        self, tmp_path, monkeypatch
    ):
        """Finding (#1612 round 8 review): if the path changes again
        after ``_sha256`` returns but before
        ``retire_mismatched_occurrence``'s transaction, the occurrence
        must not be retired on now-stale evidence.
        ``retire_mismatched_occurrence`` rechecks the identity captured
        before hashing inside its own transaction, the same pattern
        ``move_verified_occurrence`` and ``bind_found_copy`` already use
        for their own TOCTOU window (module docstring, final paragraph)."""
        import src.occurrence_scan as scan_module

        box = Box(tmp_path)
        try:
            path = box.put("INBOX/cur/b11:2,S", _eml("racerrrr@example.test", "AAAA"))
            box.index(path)
            original = path.read_bytes()
            before_stat = path.stat()

            intruder = _eml("intruder@example.test", "BBBB")
            assert len(intruder) == len(original)
            path.write_bytes(intruder)
            os.utime(path, ns=(before_stat.st_mtime_ns, before_stat.st_mtime_ns))

            real_sha256 = scan_module._sha256

            def _sha256_then_restore(p):
                digest = real_sha256(p)
                if p == path:
                    # The race: the path is back to its own claimant's
                    # bytes — via a fresh write, a different inode, as a
                    # real restore-by-rename would be — by the time
                    # retire_mismatched_occurrence's transaction runs.
                    tmp = path.with_name(path.name + ".racetmp")
                    tmp.write_bytes(original)
                    os.utime(tmp, ns=(before_stat.st_mtime_ns, before_stat.st_mtime_ns))
                    os.replace(tmp, path)
                return digest

            monkeypatch.setattr(scan_module, "_sha256", _sha256_then_restore)

            counts = scan_occurrences(box.db, box.root, hash_cache={})
            assert (counts.stale, counts.unreachable) == (0, 1)
            assert box.db.find_active_occurrence(str(path)) is not None
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
