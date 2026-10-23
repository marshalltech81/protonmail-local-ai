"""#1567: a message is reaped only after it loses its last live occurrence.

A live occurrence is an active one that is not ``T``-flagged (PLAN.md
decision 47). The reconciler's sweep reconciles the occurrences with the
Maildir (``occurrence_scan``): missed renames are followed once the
bytes are verified, byte-identical copies are bound, and an occurrence
whose file a complete scan did not find is retired; a partial scan
retires nothing. Tombstones are keyed by occurrence and written only
while the message has no live occurrence, so the earliest mark is when
it lost its last one, across every account. The brakes count messages.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from src import main
from src.database import EMBEDDING_DIM, SCHEMA_VERSION, Database
from src.occurrence_scan import file_identity, scan_occurrences
from src.queue import REASON_INITIAL_SCAN, IndexingQueue
from src.reconciler import Reconciler, ReconcilerConfig, sweep_paths
from src.threader import Threader
from src.timings import TimingAggregator

from tests.conftest import TEST_ACCOUNT, make_mock_embedder
from tests.test_database import _schema_shape, _v14_from_fresh

MARKER = "SYNTHETIC_RECONCILE_MARKER"
_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIM - 1)


def _config(**overrides) -> ReconcilerConfig:
    base = {
        "enabled": True,
        "grace_days": 0,
        "sweep_interval_secs": 3600,
        "max_batch_pct": 1.0,
        "force": False,
    }
    base.update(overrides)
    return ReconcilerConfig(**base)


def _eml(message_id: str, body: str = "body") -> bytes:
    return (
        "From: alice@example.com\r\n"
        "To: bob@example.com\r\n"
        f"Subject: {MARKER} {message_id}\r\n"
        f"Message-ID: <{message_id}>\r\n"
        "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n{body} {MARKER}\r\n"
    ).encode()


def _put(root: Path, relative: str, raw: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


class Box:
    """A Maildir root, a database bound to it and a reconciler."""

    def __init__(self, tmp_path: Path, **config):
        self.root = tmp_path / "maildir"
        self.root.mkdir()
        self.db = Database(
            tmp_path / "mail.db", account=replace(TEST_ACCOUNT, maildir_root=self.root)
        )
        self.reconciler = Reconciler(
            self.db, make_mock_embedder(_VECTOR), _config(**config), maildir_root=self.root
        )

    def put(self, relative: str, raw: bytes) -> Path:
        return _put(self.root, relative, raw)

    def handle_moved(self, src: Path | str, dest: Path | str) -> None:
        """``Reconciler.handle_moved``, with the destination's identity
        verified as the real watcher caller (``main.py``'s ``on_moved``)
        does (#1612). Every call in this file is a genuine, unmodified
        rename, so ``dest``'s current stat is exactly what verification
        would compute; a test of the mismatch guard itself calls
        ``self.reconciler.handle_moved`` directly instead."""
        self.reconciler.handle_moved(
            str(src), str(dest), verified_identity=file_identity(Path(dest))
        )

    def index(self, *paths: Path) -> None:
        queue = IndexingQueue(self.db, max_attempts=3, base_backoff_seconds=0)
        for path in paths:
            queue.enqueue(str(path), REASON_INITIAL_SCAN)
        with patch.object(main, "MAILDIR_PATH", self.root):
            main._drain_queue_batched(
                self.db,
                make_mock_embedder(_VECTOR),
                Threader(self.db),
                queue,
                batch_size=8,
                timing_aggregator=TimingAggregator(window=4),
            )

    def occurrences(self) -> list[dict]:
        return [
            dict(r) for r in self.db._conn.execute("SELECT * FROM message_occurrences ORDER BY id")
        ]

    def active(self) -> list[tuple[str, int]]:
        return [
            (o["relative_path"], o["trashed"]) for o in self.occurrences() if o["state"] == "active"
        ]

    def messages(self) -> int:
        return self.db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def tombstones(self) -> list[dict]:
        return [
            dict(r)
            for r in self.db._conn.execute("SELECT * FROM pending_deletions ORDER BY occurrence_id")
        ]

    def age_tombstones(self, days: int) -> None:
        then = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        self.db._conn.execute("UPDATE pending_deletions SET marked_at = ?", (then,))
        self.db._conn.commit()

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    try:
        yield b
    finally:
        b.close()


class TestLastLiveLoss:
    def test_removing_one_of_two_copies_retires_its_occurrence_and_keeps_the_message(
        self, box, caplog
    ):
        caplog.set_level(logging.INFO)
        raw = _eml("two@example.test")
        inbox = box.put("INBOX/cur/a1:2,S", raw)
        archive = box.put("Archive/cur/a2:2,S", raw)
        box.index(inbox, archive)
        archive.unlink()

        counts = box.reconciler.sweep()
        assert (counts["retired"], counts["tombstoned"], counts["complete"]) == (1, 0, True)
        assert box.active() == [("INBOX/cur/a1:2,S", 0)]
        retired = [o for o in box.occurrences() if o["state"] == "retired"]
        assert [o["relative_path"] for o in retired] == ["Archive/cur/a2:2,S"]
        assert box.tombstones() == []
        assert box.reconciler.reap()["threads_reaped"] == 0
        assert box.messages() == 1
        assert MARKER not in caplog.text

    def test_removing_the_last_copy_waits_out_the_grace_window(self, tmp_path):
        box = Box(tmp_path, grace_days=7)
        try:
            path = box.put("INBOX/cur/b1:2,S", _eml("last@example.test"))
            box.index(path)
            path.unlink()
            counts = box.reconciler.sweep()
            assert (counts["retired"], counts["tombstoned"]) == (1, 1)
            # Within the grace window: kept.
            assert box.reconciler.reap()["threads_reaped"] == 0
            assert box.messages() == 1
            box.age_tombstones(8)
            assert box.reconciler.reap()["threads_reaped"] == 1
            assert box.messages() == 0
            assert box.occurrences() == [] and box.tombstones() == []
            # The file's rows are unmarked, so it is re-indexed if it returns.
            assert box.db._conn.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0] == 0
        finally:
            box.close()

    def test_a_trashed_inbox_copy_beside_a_trash_copy_is_not_a_loss(self, box):
        raw = _eml("trash@example.test")
        inbox = box.put("INBOX/cur/c1:2,S", raw)
        box.index(inbox)
        trash = box.put("Trash/cur/c2:2,S", raw)
        trashed = box.root / "INBOX/cur/c1:2,ST"
        inbox.rename(trashed)

        counts = box.reconciler.sweep()
        # The rename is followed (bytes verified by the recorded identity)
        # and the Trash copy bound by its recorded hash once indexed.
        box.index(trash)
        counts = box.reconciler.sweep()
        assert sorted(box.active()) == [("INBOX/cur/c1:2,ST", 1), ("Trash/cur/c2:2,S", 0)]
        assert box.tombstones() == []
        assert counts["tombstoned"] == 0
        assert box.reconciler.reap()["threads_reaped"] == 0
        # Trashing the Trash copy too loses the last live occurrence.
        trash.rename(box.root / "Trash/cur/c2:2,ST")
        counts = box.reconciler.sweep()
        assert counts["tombstoned"] == 1
        assert len(box.tombstones()) == 2
        assert box.reconciler.reap()["threads_reaped"] == 1

    def test_a_restore_during_grace_clears_the_tombstones(self, tmp_path):
        box = Box(tmp_path, grace_days=7)
        try:
            path = box.put("INBOX/cur/d1:2,S", _eml("restore@example.test"))
            box.index(path)
            trashed = box.root / "INBOX/cur/d1:2,ST"
            path.rename(trashed)
            assert box.reconciler.sweep()["tombstoned"] == 1
            assert box.db.has_pending_deletion(str(trashed))
            trashed.rename(path)
            counts = box.reconciler.sweep()
            # The followed rename clears the tombstones; counted as cleared.
            assert (counts["renamed"], counts["cleared"]) == (1, 1)
            assert box.tombstones() == []
            box.age_tombstones(30)
            assert box.reconciler.reap()["threads_reaped"] == 0
            assert box.messages() == 1
        finally:
            box.close()

    def test_a_restore_seen_by_the_watcher_clears_the_tombstones(self, box):
        path = box.put("INBOX/cur/e1:2,S", _eml("watch@example.test"))
        box.index(path)
        trashed = box.root / "INBOX/cur/e1:2,ST"
        path.rename(trashed)
        box.handle_moved(str(path), str(trashed))
        assert len(box.tombstones()) == 1
        trashed.rename(path)
        box.handle_moved(str(trashed), str(path))
        assert box.tombstones() == []
        assert box.reconciler.reap()["threads_reaped"] == 0

    def test_the_grace_window_runs_from_the_latest_loss(self, tmp_path):
        """A message that lost its last copy, got one back and lost it
        again is marked from the second loss."""
        box = Box(tmp_path, grace_days=7)
        try:
            path = box.put("INBOX/cur/f1:2,S", _eml("again@example.test"))
            box.index(path)
            trashed = box.root / "INBOX/cur/f1:2,ST"
            path.rename(trashed)
            box.reconciler.sweep()
            box.age_tombstones(30)
            trashed.rename(path)
            box.reconciler.sweep()
            path.rename(trashed)
            box.reconciler.sweep()
            (tomb,) = box.tombstones()
            assert datetime.fromisoformat(tomb["marked_at"]) > datetime.now(UTC) - timedelta(
                hours=1
            )
            assert box.reconciler.reap()["threads_reaped"] == 0
        finally:
            box.close()

    def test_a_replaced_file_counts_as_a_loss_at_the_next_sweep(self, box):
        """New bytes at a path retire the old occurrence at the parse; the
        sweep then tombstones the message that held it."""
        path = box.put("INBOX/cur/g1:2,S", _eml("old@example.test"))
        box.index(path)
        path.write_bytes(_eml("new@example.test"))
        box.index(path)
        assert box.reconciler.sweep()["tombstoned"] == 1
        assert box.reconciler.reap()["threads_reaped"] == 1
        assert box.messages() == 1


class TestScanCompleteness:
    def test_a_partial_scan_retires_nothing(self, box, caplog):
        caplog.set_level(logging.INFO)
        raw = _eml("partial@example.test")
        inbox = box.put("INBOX/cur/h1:2,S", raw)
        other = box.put("Archive/cur/h2:2,S", _eml("other@example.test"))
        box.index(inbox, other)
        inbox.unlink()
        locked = box.root / "Locked" / "cur"
        locked.mkdir(parents=True)
        os.chmod(locked, 0)
        try:
            counts = box.reconciler.sweep()
        finally:
            os.chmod(locked, 0o700)
        assert counts["complete"] is False
        assert (counts["retired"], counts["tombstoned"], counts["unreachable"]) == (0, 0, 1)
        assert ("INBOX/cur/h1:2,S", 0) in box.active()
        assert box.reconciler.reap()["threads_reaped"] == 0
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("scan was partial (1 unreadable" in r.getMessage() for r in warnings)
        assert MARKER not in caplog.text
        # Readable again: the next scan is complete and retires it.
        assert box.reconciler.sweep()["retired"] == 1

    def test_a_missing_root_retires_nothing(self, box):
        path = box.put("INBOX/cur/i1:2,S", _eml("root@example.test"))
        box.index(path)
        shutil.move(box.root, box.root.with_name("elsewhere"))
        counts = box.reconciler.sweep()
        assert counts["complete"] is False and counts["retired"] == 0
        assert box.active() == [("INBOX/cur/i1:2,S", 0)]
        assert box.tombstones() == []

    def test_the_rename_sweep_retires_but_never_tombstones(self, box):
        """``sweep_paths`` runs in archive mode too: a vanished file's
        occurrence is retired, the message is kept and not tombstoned."""
        path = box.put("INBOX/cur/j1:2,S", _eml("archive@example.test"))
        box.index(path)
        path.unlink()
        counts = sweep_paths(box.db, maildir_root=box.root)
        assert (counts["retired"], counts["complete"]) == (1, True)
        assert box.active() == []
        assert box.tombstones() == []
        assert box.messages() == 1

    def test_another_accounts_occurrences_are_left_alone(self, box):
        path = box.put("INBOX/cur/k1:2,S", _eml("acct@example.test"))
        box.index(path)
        other = _second_account(box.db)
        claimant = box.occurrences()[0]["claimant_id"]
        _add_occurrence(box.db, other, claimant, "INBOX/cur/elsewhere:2,S")
        counts = box.reconciler.sweep()
        assert counts["retired"] == 0
        assert len([o for o in box.occurrences() if o["state"] == "active"]) == 2


class TestMissedRenames:
    def test_a_missed_flag_rename_is_followed_once_the_bytes_match(self, box):
        path = box.put("INBOX/cur/l1:2,S", _eml("flag@example.test"))
        box.index(path)
        (before,) = box.occurrences()
        renamed = box.root / "INBOX/cur/l1:2,FS"
        path.rename(renamed)
        counts = sweep_paths(box.db, maildir_root=box.root)
        assert counts["renamed"] == 1
        (after,) = box.occurrences()
        assert after["id"] == before["id"]
        assert (after["relative_path"], after["flagged"]) == ("INBOX/cur/l1:2,FS", 1)
        assert box.db.is_indexed(str(renamed)) and not box.db.is_indexed(str(path))

    def test_a_same_uniq_file_with_other_bytes_is_not_followed_but_is_retired(self, box):
        """#1612 round 15: ``_verify`` reads the candidate's bytes and
        proves they are not this occurrence's, so the missed rename is
        not followed (the replacement is not bound to the old
        claimant) but the vanished claimant IS retired directly —
        ``retire_vanished_occurrence`` would refuse, since the uniq
        still resolves to this (wrong) file."""
        path = box.put("INBOX/cur/m1:2,S", _eml("same@example.test"))
        box.index(path)
        path.unlink()
        box.put("INBOX/cur/m1:2,FS", _eml("different@example.test"))
        counts = box.reconciler.sweep()
        assert (counts["renamed"], counts["retired"], counts["unverified"]) == (0, 1, 0)
        assert box.active() == []

    def test_a_same_uniq_file_unreadable_at_verification_is_left_unverified(self, box, monkeypatch):
        """Companion to the proven-mismatch case above: when the
        candidate cannot be read at all (inconclusive, not proven), the
        occurrence must stay active, not be retired on an unproven
        result (#1612 round 15)."""
        import src.occurrence_scan as scan_module

        path = box.put("INBOX/cur/m2:2,S", _eml("same2@example.test"))
        box.index(path)
        path.unlink()
        candidate = box.put("INBOX/cur/m2:2,FS", _eml("other@example.test"))

        real_sha256 = scan_module._sha256

        def _raise_for_candidate(p, identity=None):
            if p == candidate:
                raise OSError("synthetic: candidate unreadable at verification time")
            return real_sha256(p, identity)

        monkeypatch.setattr(scan_module, "_sha256", _raise_for_candidate)
        counts = box.reconciler.sweep()
        assert (counts["renamed"], counts["retired"], counts["unverified"]) == (0, 0, 1)
        assert box.active() == [("INBOX/cur/m2:2,S", 0)]

    def test_a_restored_original_is_not_retired_as_a_scan_mismatch(self, box, monkeypatch):
        """#1612 round 16: between ``_walk()`` recording the original
        path absent and ``retire_mismatched_rename``'s own transaction,
        the exact original file can come back (restored) rather than
        staying gone. The same-uniq candidate this scan already proved
        mismatched must not retire the restored claimant; a later pass
        re-evaluates it instead."""
        original = _eml("same3@example.test")
        path = box.put("INBOX/cur/m3:2,S", original)
        box.index(path)
        path.unlink()
        box.put("INBOX/cur/m3:2,FS", _eml("different3@example.test"))

        real_retire = box.db.retire_mismatched_rename

        def _restore_then_retire(occurrence_id, filepath, **kwargs):
            # Simulates a restore landing between the scan's absence
            # check and this call's own transaction.
            path.write_bytes(original)
            return real_retire(occurrence_id, filepath, **kwargs)

        monkeypatch.setattr(box.db, "retire_mismatched_rename", _restore_then_retire)
        counts = box.reconciler.sweep()
        assert (counts["renamed"], counts["retired"], counts["unreachable"]) == (0, 0, 1)
        assert box.active() == [("INBOX/cur/m3:2,S", 0)]

    def test_an_unmapped_copy_moved_across_folders_keeps_the_message(self, tmp_path):
        """#1141: the copy no occurrence held moves; the watcher binds its
        destination, so removing the other copy reaps nothing."""
        box = Box(tmp_path)
        try:
            raw = _eml("moved@example.test")
            inbox = box.put("INBOX/cur/n1:2,S", raw)
            box.index(inbox)
            # A copy indexed under the same claimant but never bound (an
            # index from before v14 that the seed bound once).
            copy = box.put("Archive/cur/n2:2,S", raw)
            box.index(copy)
            box.db._conn.execute(
                "DELETE FROM message_occurrences WHERE relative_path = 'Archive/cur/n2:2,S'"
            )
            box.db._conn.commit()
            dest = box.root / "Clients" / "cur" / "n2:2,S"
            dest.parent.mkdir(parents=True)
            copy.rename(dest)
            box.handle_moved(str(copy), str(dest))
            assert ("Clients/cur/n2:2,S", 0) in box.active()
            inbox.unlink()
            box.reconciler.sweep()
            assert box.reconciler.reap()["threads_reaped"] == 0
            assert box.messages() == 1
        finally:
            box.close()

    def test_a_reused_path_with_other_bytes_is_not_bound_on_rename(self, box):
        """A retired path whose stale ``indexed_files`` record names an
        indexed message, reused by a different file whose creation was
        missed: a flag rename binds nothing, so the parse binds the new
        file to its own claimant."""
        old = box.put("INBOX/cur/r1:2,S", _eml("old@example.test"))
        box.index(old)
        box.db._conn.execute(
            "UPDATE message_occurrences SET state = 'retired', retired_at = '2026-01-01' "
            "WHERE relative_path = 'INBOX/cur/r1:2,S'"
        )
        box.db._conn.commit()
        old.write_bytes(_eml("new@example.test", body="a different, longer body"))
        dest = box.root / "INBOX/cur/r1:2,FS"
        old.rename(dest)
        box.handle_moved(str(old), str(dest))
        assert box.active() == []

    def test_a_reused_path_with_the_same_size_and_mtime_is_hashed_before_binding(self, box):
        """Fresh evidence beyond the case above: the replacement can also
        match the stale ``indexed_files`` record's size *and* mtime
        (mbsync sets mtime from IMAP's INTERNALDATE, so two different
        messages can legitimately share one — the same reasoning
        ``occurrence_scan``'s content backstop was built on). A
        metadata-only check cannot tell the replacement apart from the
        old claimant's bytes, so the content hash must be checked before
        binding; this must not be bound to the old claimant."""
        old_raw = _eml("old@example.test")
        old = box.put("INBOX/cur/r1:2,S", old_raw)
        box.index(old)
        box.db._conn.execute(
            "UPDATE message_occurrences SET state = 'retired', retired_at = '2026-01-01' "
            "WHERE relative_path = 'INBOX/cur/r1:2,S'"
        )
        box.db._conn.commit()
        stat_before = old.stat()
        new_raw = _eml("new@example.test", body="a completely different payload")
        pad = len(old_raw) - len(new_raw)
        new_raw = (
            new_raw[:-2] + b" " * pad + new_raw[-2:]
            if pad > 0
            else new_raw[: pad - 2] + new_raw[-2:]
        )
        assert len(new_raw) == len(old_raw) and new_raw != old_raw
        old.write_bytes(new_raw)
        os.utime(old, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
        dest = box.root / "INBOX/cur/r1:2,FS"
        old.rename(dest)
        box.handle_moved(str(old), str(dest))
        assert box.active() == []

    def test_a_reused_path_replaced_again_right_after_hashing_is_not_bound(self, box):
        """#1612 round 15: the destination's hash matched the retired
        path's stale ``indexed_files`` record (a genuine rename, no
        mismatch), but it is replaced again — same size and mtime, a
        new inode — in the window between that hash check succeeding
        and the bind. ``_move_occurrence``'s unheld-rename branch must
        recheck the identity it captured before hashing, the same
        TOCTOU pattern ``move_verified_occurrence`` and
        ``bind_found_copy`` already use for their own write, or it
        binds the replacement to the wrong (old) claimant."""
        import src.database as database_module

        old_raw = _eml("old@example.test")
        old = box.put("INBOX/cur/r2:2,S", old_raw)
        box.index(old)
        box.db._conn.execute(
            "UPDATE message_occurrences SET state = 'retired', retired_at = '2026-01-01' "
            "WHERE relative_path = 'INBOX/cur/r2:2,S'"
        )
        box.db._conn.commit()
        dest = box.root / "INBOX/cur/r2:2,FS"
        old.rename(dest)
        stat_before = dest.stat()

        real_sha256 = database_module._sha256

        def _hash_then_replace(filepath, identity=None):
            digest = real_sha256(filepath, identity)
            if filepath == str(dest):
                # The race: a different message, same size as the
                # genuine rename's, replaces dest right after its
                # hash matched -- by the time _move_occurrence binds.
                replacement = _eml("intruder@example.test", body="a different payload entirely")
                pad = len(old_raw) - len(replacement)
                replacement = (
                    replacement[:-2] + b" " * pad + replacement[-2:]
                    if pad > 0
                    else replacement[: pad - 2] + replacement[-2:]
                )
                assert len(replacement) == len(old_raw)
                tmp = dest.with_name(dest.name + ".racetmp")
                tmp.write_bytes(replacement)
                os.utime(tmp, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
                os.replace(tmp, dest)
            return digest

        with patch.object(database_module, "_sha256", _hash_then_replace):
            box.handle_moved(str(old), str(dest))
        assert box.active() == []

    def test_an_unmapped_copy_moved_in_archive_mode_keeps_its_occurrence(self, box):
        raw = _eml("archived@example.test")
        inbox = box.put("INBOX/cur/o1:2,S", raw)
        copy = box.put("Archive/cur/o2:2,S", raw)
        box.index(inbox, copy)
        box.db._conn.execute(
            "DELETE FROM message_occurrences WHERE relative_path = 'Archive/cur/o2:2,S'"
        )
        box.db._conn.commit()
        dest = box.root / "Clients" / "cur" / "o2:2,S"
        dest.parent.mkdir(parents=True)
        copy.rename(dest)
        handler = main.MaildirHandler(box.db, IndexingQueue(box.db, max_attempts=3))
        handler.on_moved(_moved(copy, dest))
        assert ("Clients/cur/o2:2,S", 0) in box.active()


class TestFoundCopies:
    def test_a_trashed_copy_never_indexed_is_hashed_once_and_bound(self, box, caplog):
        caplog.set_level(logging.INFO)
        raw = _eml("found@example.test")
        inbox = box.put("INBOX/cur/p1:2,S", raw)
        box.index(inbox)
        box.put("Trash/cur/p2:2,ST", raw)
        stranger = box.put("Trash/cur/p3:2,ST", _eml("unknown@example.test"))
        first = box.reconciler.sweep()
        # 3 hashes: the two Trash copies (``_hash_unindexed``) plus the
        # content backstop's one hash of the INBOX copy's held path
        # (#1612), since nothing has verified its identity yet.
        assert (first["bound"], first["hashed"]) == (1, 3)
        bound = [o for o in box.occurrences() if o["provenance"] == "scan"]
        assert [(o["relative_path"], o["trashed"]) for o in bound] == [("Trash/cur/p2:2,ST", 1)]
        # The unmatched file, and the already-verified held path, are not
        # read again in this process.
        second = box.reconciler.sweep()
        assert (second["bound"], second["hashed"]) == (0, 0)
        assert stranger.exists()
        assert MARKER not in caplog.text

    def test_a_dead_lettered_copy_is_bound(self, box):
        raw = _eml("dead@example.test")
        inbox = box.put("INBOX/cur/q1:2,S", raw)
        box.index(inbox)
        copy = box.put("Archive/cur/q2:2,S", raw)
        box.db.queue_enqueue(filepath=str(copy), reason="x", status="dead", now_iso="2024")
        assert box.reconciler.sweep()["bound"] == 1

    def test_new_mail_is_left_to_the_walk(self, box):
        box.put("INBOX/cur/r1:2,S", _eml("new@example.test"))
        counts = box.reconciler.sweep()
        assert (counts["bound"], counts["hashed"]) == (0, 0)


class TestInterleavings:
    def test_a_rename_the_watcher_records_first_wins(self, box):
        path = box.put("INBOX/cur/s1:2,S", _eml("race@example.test"))
        box.index(path)
        (o,) = box.occurrences()
        dest = box.root / "INBOX/cur/s1:2,RS"
        path.rename(dest)
        box.handle_moved(str(path), str(dest))
        # The scan read the occurrence before the watcher moved it.
        identity = file_identity(dest)
        assert identity is not None
        assert (
            box.db.move_verified_occurrence(
                o["id"], o["relative_path"], str(dest), identity=identity
            )
            is False
        )
        assert box.db.retire_vanished_occurrence(o["id"], o["relative_path"]) is False
        assert box.active() == [("INBOX/cur/s1:2,RS", 0)]

    def test_move_verified_occurrence_reports_a_rename_rows_failure(self, box, monkeypatch):
        """#1612 round 15: ``move_verified_occurrence``'s own
        preconditions (the occurrence still active at its recorded
        path, the identity still matching, no other active occurrence
        at the destination) can pass while ``_move_occurrence``'s own
        in-transaction recheck of that same identity still refuses --
        a race too narrow to reproduce with real file I/O in a
        single-threaded test, simulated here the same way other races
        in this suite are. Before the fix, ``move_verified_occurrence``
        called ``_rename_rows`` and returned ``True`` unconditionally,
        regardless of whether anything was actually moved."""
        path = box.put("INBOX/cur/v1:2,S", _eml("raced@example.test"))
        box.index(path)
        (o,) = box.occurrences()
        dest = box.root / "INBOX/cur/v1:2,RS"
        path.rename(dest)
        identity = file_identity(dest)
        assert identity is not None

        # _move_occurrence's own in-transaction recheck found nothing to
        # move or bind (its genuine failure mode, #1612 round 15).
        monkeypatch.setattr(box.db, "_move_occurrence", lambda *a, **kw: (None, False))

        assert (
            box.db.move_verified_occurrence(
                o["id"], o["relative_path"], str(dest), identity=identity
            )
            is False
        )
        # Nothing actually moved: the occurrence is still where it was.
        assert box.active() == [("INBOX/cur/v1:2,S", 0)]

    def test_a_file_back_before_the_retire_is_kept(self, box):
        path = box.put("INBOX/cur/t1:2,S", _eml("back@example.test"))
        box.index(path)
        (o,) = box.occurrences()
        # Renamed after the scan's walk, not yet reported by the watcher.
        path.rename(box.root / "INBOX/cur/t1:2,FS")
        assert box.db.retire_vanished_occurrence(o["id"], o["relative_path"]) is False
        assert box.active() == [("INBOX/cur/t1:2,S", 0)]

    def test_a_late_watcher_event_after_the_scan_moved_it_changes_nothing(self, box):
        path = box.put("INBOX/cur/u1:2,S", _eml("late@example.test"))
        box.index(path)
        dest = box.root / "INBOX/cur/u1:2,ST"
        path.rename(dest)
        box.reconciler.sweep()
        assert box.active() == [("INBOX/cur/u1:2,ST", 1)]
        tombs = box.tombstones()
        box.handle_moved(str(path), str(dest))
        assert box.active() == [("INBOX/cur/u1:2,ST", 1)]
        assert box.tombstones() == tombs

    def test_a_restore_after_the_reaper_read_its_candidates_is_kept(self, box, monkeypatch):
        path = box.put("INBOX/cur/v1:2,S", _eml("snap@example.test"))
        box.index(path)
        trashed = box.root / "INBOX/cur/v1:2,ST"
        path.rename(trashed)
        box.reconciler.sweep()
        real = box.db.reapable_messages

        def restore_then_read(cutoff):
            rows = real(cutoff)
            trashed.rename(path)
            box.handle_moved(str(trashed), str(path))
            return rows

        monkeypatch.setattr(box.db, "reapable_messages", restore_then_read)
        assert box.reconciler.reap()["threads_reaped"] == 0
        assert box.messages() == 1


class TestBrakes:
    def test_the_brake_counts_messages_not_occurrences(self, tmp_path, caplog):
        box = Box(tmp_path, max_batch_pct=0.0)
        try:
            raw = _eml("many@example.test")
            paths = [box.put(f"F{i}/cur/w{i}:2,S", raw) for i in range(12)]
            box.index(*paths)
            for p in paths:
                p.rename(p.with_name(p.name + "T"))
            box.reconciler.sweep()
            assert len(box.tombstones()) == 12
            # One message, twelve occurrences: under the floor of 10.
            assert box.reconciler.reap()["threads_reaped"] == 1
        finally:
            box.close()

    def test_the_global_brake_trips_on_distinct_messages(self, tmp_path, caplog):
        caplog.set_level(logging.ERROR)
        box = Box(tmp_path, max_batch_pct=0.0)
        try:
            paths = [box.put(f"INBOX/cur/x{i}:2,S", _eml(f"x{i}@example.test")) for i in range(11)]
            box.index(*paths)
            for p in paths:
                p.rename(p.with_name(p.name + "T"))
            box.reconciler.sweep()
            result = box.reconciler.reap()
            assert result["aborted"] is True and result["tombstones_pending"] == 11
            assert "11 messages past grace window" in caplog.text
            assert box.messages() == 11
        finally:
            box.close()

    def test_the_account_brake_is_not_computed_with_one_active_account(self, box, monkeypatch):
        path = box.put("INBOX/cur/y1:2,S", _eml("one@example.test"))
        box.index(path)
        path.rename(path.with_name(path.name + "T"))
        box.reconciler.sweep()
        calls = []
        monkeypatch.setattr(box.db, "accounts_of", lambda ids: calls.append(ids) or {})
        assert box.reconciler.reap()["threads_reaped"] == 1
        assert calls == []

    def test_the_account_brake_holds_back_a_mass_deletion_in_one_account(self, tmp_path, caplog):
        caplog.set_level(logging.ERROR)
        box = Box(tmp_path, max_batch_pct=0.5)
        try:
            # Account A (the configured one): 40 live messages and 1 lost.
            paths = [box.put(f"INBOX/cur/z{i}:2,S", _eml(f"z{i}@example.test")) for i in range(41)]
            box.index(*paths)
            paths[0].unlink()
            # Account B: 12 messages, 11 lost (over its floor of 10 and half).
            other = _second_account(box.db)
            b_claimants = []
            for i in range(12):
                b = box.put(f"B/cur/b{i}:2,S", _eml(f"b{i}@example.test"))
                box.index(b)
                (row,) = [o for o in box.occurrences() if o["relative_path"] == f"B/cur/b{i}:2,S"]
                box.db._conn.execute(
                    "UPDATE message_occurrences SET account_id = ?, trashed = ? WHERE id = ?",
                    (other, int(i < 11), row["id"]),
                )
                b_claimants.append(row["claimant_id"])
                # Account B's files live under its own root, not this one.
                b.unlink()
            box.db._conn.commit()
            box.reconciler.sweep()
            result = box.reconciler.reap()
            assert result["aborted"] is False
            assert f"reaper held back account {other}: 11 messages" in caplog.text
            remaining = {r[0] for r in box.db._conn.execute("SELECT claimant_id FROM messages")}
            # Account A's lost message was reaped; account B's were kept.
            assert len(remaining) == 53 - 1
            assert set(b_claimants) <= remaining
        finally:
            box.close()


class TestMigrationV17:
    def _v14(self, tmp_path) -> tuple[Path, Path, list[str]]:
        root = tmp_path / "maildir"
        raw = _eml("one@example.test")
        paths = [
            _put(root, "INBOX/cur/m1:2,ST", raw),
            _put(root, "INBOX/cur/m2:2,S", _eml("two@example.test")),
            _put(root, "INBOX/cur/m3:2,S", _eml("three@example.test")),
        ]
        account = replace(TEST_ACCOUNT, maildir_root=root)
        db = Database(tmp_path / "mail.db", account=account)
        box_db = db
        queue = IndexingQueue(box_db, max_attempts=3, base_backoff_seconds=0)
        for p in paths:
            queue.enqueue(str(p), REASON_INITIAL_SCAN)
        with patch.object(main, "MAILDIR_PATH", root):
            main._drain_queue_batched(
                db,
                make_mock_embedder(_VECTOR),
                Threader(db),
                queue,
                batch_size=8,
                timing_aggregator=TimingAggregator(window=4),
            )
        claimants = [
            r[0]
            for r in db._conn.execute(
                "SELECT o.claimant_id FROM message_occurrences o ORDER BY o.relative_path"
            )
        ]
        chunk_ids = sorted(r[0] for r in db._conn.execute("SELECT chunk_id FROM message_chunks"))
        _v14_from_fresh(db)
        # v14 tombstones: two on the first message, one on the second.
        rows = [
            (str(paths[0]), claimants[0], "2024-01-01T00:00:00+00:00"),
            (str(root / "INBOX/cur/gone:2,S"), claimants[0], "2024-02-01T00:00:00+00:00"),
            (str(paths[1]), claimants[1], "2024-03-01T00:00:00+00:00"),
        ]
        for filepath, claimant, marked in rows:
            db._conn.execute(
                "INSERT INTO pending_deletions VALUES (?, ?, (SELECT thread_id FROM messages "
                "WHERE claimant_id = ?), ?)",
                (filepath, claimant, claimant, marked),
            )
        # The third message lost its occurrence: its path holds other bytes.
        db._conn.execute("DELETE FROM message_occurrences WHERE claimant_id = ?", (claimants[2],))
        paths[2].write_bytes(_eml("replaced@example.test"))
        db._conn.commit()
        db.close()
        return tmp_path / "mail.db", root, [*claimants, *chunk_ids]

    def test_v14_migrates_to_the_fresh_shape(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        path, root, ids = self._v14(tmp_path)
        account = replace(TEST_ACCOUNT, maildir_root=root)
        db = Database(path, account=account)
        fresh = Database(tmp_path / "fresh.db", account=account)
        try:
            assert SCHEMA_VERSION == 18
            assert _schema_shape(db._conn) == _schema_shape(fresh._conn)
            claimants = ids[:3]
            tombs = {
                r["claimant_id"]: r["marked_at"]
                for r in db._conn.execute("SELECT * FROM pending_deletions")
            }
            # One row per message, its latest mark.
            assert tombs == {
                claimants[0]: "2024-02-01T00:00:00+00:00",
                claimants[1]: "2024-03-01T00:00:00+00:00",
            }
            (retired,) = [
                dict(r)
                for r in db._conn.execute(
                    "SELECT * FROM message_occurrences WHERE claimant_id = ?", (claimants[2],)
                )
            ]
            assert (retired["state"], retired["provenance"]) == ("retired", "migration")
            assert retired["relative_path"] == "INBOX/cur/m3:2,S"
            assert retired["first_observed_at"] is None and retired["last_observed_at"] is None
            # IDs unchanged; v17 itself queues nothing (no parser-produced
            # data), but v15's attachment-inventory backfill reparses
            # every indexed file in the same migration run.
            chunk_ids = sorted(
                r[0] for r in db._conn.execute("SELECT chunk_id FROM message_chunks")
            )
            assert chunk_ids == ids[3:]
            jobs = db._conn.execute("SELECT reason FROM indexing_jobs").fetchall()
            assert [r[0] for r in jobs] == ["reparse"] * 3
            occurrence = db._conn.execute(
                "SELECT provenance FROM message_occurrences WHERE claimant_id = ?",
                (claimants[0],),
            ).fetchone()
            assert occurrence[0] == "parse"
        finally:
            db.close()
            fresh.close()
        assert "applied migrations: [15, 16, 17, 18]" in caplog.text
        assert "Migration v17 carried 2 tombstone(s)" in caplog.text
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("found 1 message(s) with no verified file" in m for m in warnings)
        assert MARKER not in caplog.text

    def test_the_sweep_after_the_migration_settles_the_tombstones(self, tmp_path):
        path, root, ids = self._v14(tmp_path)
        account = replace(TEST_ACCOUNT, maildir_root=root)
        db = Database(path, account=account)
        try:
            rec = Reconciler(db, make_mock_embedder(_VECTOR), _config(), maildir_root=root)
            counts = rec.sweep()
            # The second message is live: its carried tombstone goes. The
            # third has only a retired occurrence: it is marked now.
            assert counts["cleared"] == 1 and counts["tombstoned"] == 1
            marked = {r["claimant_id"] for r in db._conn.execute("SELECT * FROM pending_deletions")}
            assert marked == {ids[0], ids[2]}
        finally:
            db.close()


def _second_account(db: Database) -> int:
    cur = db._conn.execute(
        "INSERT INTO accounts (scope_uuid, provider, created_at) VALUES (?, ?, ?)",
        ("00000000-0000-4000-8000-000000000002", "proton-bridge", "2024-01-01T00:00:00+00:00"),
    )
    db._conn.commit()
    return int(cur.lastrowid or 0)


def _add_occurrence(db: Database, account_id: int, claimant_id: str, relative: str) -> None:
    db._conn.execute(
        "INSERT INTO message_occurrences (account_id, folder, uniq, claimant_id, relative_path, "
        "seen, flagged, replied, trashed, state, provenance) "
        "VALUES (?, 'INBOX', ?, ?, ?, 1, 0, 0, 0, 'active', 'parse')",
        (account_id, relative.rsplit("/", 1)[1].split(":2,")[0], claimant_id, relative),
    )
    db._conn.commit()


class _Event:
    def __init__(self, src: Path, dest: Path):
        self.src_path = str(src)
        self.dest_path = str(dest)
        self.is_directory = False


def _moved(src: Path, dest: Path) -> _Event:
    return _Event(src, dest)


def test_scan_of_a_root_outside_the_account_changes_nothing(box, tmp_path):
    path = box.put("INBOX/cur/aa:2,S", _eml("outside@example.test"))
    box.index(path)
    outside = tmp_path / "other"
    outside.mkdir()
    counts = scan_occurrences(box.db, outside)
    assert counts.complete is False and counts.retired == 0
    assert box.active() == [("INBOX/cur/aa:2,S", 0)]


def test_a_scan_of_part_of_the_root_leaves_the_rest_alone(box):
    inbox = box.put("INBOX/cur/ab:2,S", _eml("scoped@example.test"))
    archive = box.put("Archive/cur/ac:2,S", _eml("other@example.test"))
    box.index(inbox, archive)
    archive.unlink()
    counts = scan_occurrences(box.db, box.root / "INBOX")
    assert counts.complete is True and counts.retired == 0
    assert ("Archive/cur/ac:2,S", 0) in box.active()


def test_files_outside_the_account_root_make_the_scan_partial(box, tmp_path):
    outside = tmp_path / "other"
    _put(outside, "INBOX/cur/ad:2,S", _eml("elsewhere@example.test"))
    assert scan_occurrences(box.db, outside).complete is False


def test_an_unreadable_trashed_copy_is_left_unbound(box):
    raw = _eml("locked@example.test")
    box.index(box.put("INBOX/cur/ae:2,S", raw))
    locked = box.put("Trash/cur/af:2,ST", raw)
    os.chmod(locked, 0)
    try:
        counts = box.reconciler.sweep()
    finally:
        os.chmod(locked, 0o600)
    # 2: the locked Trash copy's attempted hash plus the content
    # backstop's one hash of the INBOX copy's held path (#1612).
    assert (counts["bound"], counts["hashed"]) == (0, 2)


def test_a_survivor_with_no_file_holds_its_thread_back(tmp_path, caplog):
    """A survivor whose only occurrence was retired (it is losing its last
    copy too, still within the grace window) cannot be re-parsed: the
    thread is blocked, visibly, and nothing is removed."""
    caplog.set_level(logging.WARNING)
    box = Box(tmp_path, grace_days=7)
    try:
        first = box.put("INBOX/cur/ag:2,S", _eml("root@example.test"))
        reply = box.put(
            "INBOX/cur/ah:2,S",
            _eml("reply@example.test").replace(
                b"Subject:", b"In-Reply-To: <root@example.test>\r\nSubject:", 1
            ),
        )
        box.index(first, reply)
        first.rename(first.with_name("ag:2,ST"))
        box.reconciler.sweep()
        box.age_tombstones(30)
        reply.unlink()
        box.reconciler.sweep()
        result = box.reconciler.reap()
        assert (result["threads_reaped"], result["threads_rebuilt"]) == (0, 0)
        assert result["blocked_threads"] == 1
        assert box.messages() == 2
        assert "a survivor has no file to re-read" in caplog.text
        assert MARKER not in caplog.text
    finally:
        box.close()


class TestReviewRound1:
    """Review round 1 on #1612."""

    def test_a_reap_keeps_a_reused_path_of_another_message(self, box):
        """Identity namespace: a retired locator of the reaped message
        that now holds another message's file keeps that file's
        ``indexed_files`` and queue rows."""
        a = box.put("INBOX/cur/p1:2,S", _eml("moved@example.test"))
        box.index(a)
        moved = box.root / "Archive/cur/p1:2,S"
        moved.parent.mkdir(parents=True)
        a.rename(moved)
        box.db.update_filepath(
            str(a), str(moved), move_occurrence=True, verified_identity=file_identity(moved)
        )
        b = box.put("INBOX/cur/p1:2,S", _eml("reuser@example.test"))
        box.index(b)
        queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
        queue.enqueue(str(b), REASON_INITIAL_SCAN)
        moved.unlink()
        assert box.reconciler.sweep()["tombstoned"] == 1
        assert box.reconciler.reap()["threads_reaped"] == 1
        assert box.messages() == 1
        assert box.db.is_indexed(str(b))
        assert box.db.queue_get_status(str(b)) == "queued"

    def test_a_found_copy_replaced_after_hashing_is_not_bound(self, box, monkeypatch):
        """A T-flagged copy hashed by the scan and replaced before the
        write is refused inside the write transaction."""
        raw = _eml("raced@example.test")
        box.index(box.put("INBOX/cur/r1:2,S", raw))
        copy = box.put("Trash/cur/r2:2,ST", raw)
        real = scan_occurrences.__globals__["_sha256"]

        def hash_then_replace(path, identity=None):
            digest = real(path, identity)
            if path == copy:
                swap = copy.with_name("swap")
                swap.write_bytes(_eml("other@example.test"))
                os.replace(swap, copy)
            return digest

        monkeypatch.setitem(scan_occurrences.__globals__, "_sha256", hash_then_replace)
        counts = scan_occurrences(box.db, box.root, hash_cache={})
        assert counts.bound == 0
        assert box.active() == [("INBOX/cur/r1:2,S", 0)]

    def test_a_missed_rename_replaced_after_hashing_is_not_followed(self, box, monkeypatch):
        old = box.put("INBOX/cur/q1:2,S", _eml("renamed@example.test"))
        box.index(old)
        new = box.root / "INBOX/cur/q1:2,FS"
        old.rename(new)
        os.utime(new, ns=(1, 1))  # the recorded identity no longer matches: hash it
        real = scan_occurrences.__globals__["_sha256"]

        def hash_then_replace(path, identity=None):
            digest = real(path, identity)
            swap = path.with_name("swap")
            swap.write_bytes(_eml("intrude@example.test"))
            os.replace(swap, path)
            return digest

        monkeypatch.setitem(scan_occurrences.__globals__, "_sha256", hash_then_replace)
        counts = scan_occurrences(box.db, box.root)
        assert (counts.renamed, counts.unverified) == (0, 1)
        assert box.active() == [("INBOX/cur/q1:2,S", 0)]

    def test_the_v17_warning_counts_only_messages_with_no_active_copy(self, tmp_path, caplog):
        """A v14 seed row retired by a later move is history, not a
        message without a verified file."""
        root = tmp_path / "maildir"
        account = replace(TEST_ACCOUNT, maildir_root=root)
        box = Box(tmp_path)
        try:
            raw = _eml("seeded@example.test")
            path = box.put("INBOX/cur/s1:2,S", raw)
            box.index(path)
            db = box.db
            # The seed row, retired by a move to Archive, beside the new row.
            db._conn.execute("UPDATE message_occurrences SET provenance = 'migration'")
            db._conn.commit()
            moved = box.root / "Archive/cur/s1:2,S"
            moved.parent.mkdir(parents=True)
            path.rename(moved)
            db.update_filepath(
                str(path), str(moved), move_occurrence=True, verified_identity=file_identity(moved)
            )
            _v14_from_fresh(db)
            db.close()
            caplog.set_level(logging.INFO)
            Database(tmp_path / "mail.db", account=account).close()
        finally:
            box.db.close()
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("no verified file" in m for m in warnings)


class TestReviewRound2:
    """Review round 2 on #1612."""

    def test_a_replaced_file_with_the_same_size_and_mtime_is_hashed_again(self, box):
        """The hash cache is keyed by the file's inode too: a T-flagged
        copy replaced by another file with the same size and mtime is not
        bound with the old file's digest."""
        raw = _eml("cached@example.test")
        box.index(box.put("INBOX/cur/h1:2,S", raw))
        other = _eml("cachex@example.test")
        assert len(other) == len(raw)
        cache: dict = {}
        # Hashed once while it is another message's copy-to-be; then
        # replaced by bytes of the same size and mtime.
        copy = box.put("Trash/cur/h2:2,ST", other)
        os.utime(copy, ns=(5, 5))
        assert scan_occurrences(box.db, box.root, hash_cache=cache).bound == 0
        swap = copy.with_name("swap")
        swap.write_bytes(raw)
        os.utime(swap, ns=(5, 5))
        os.replace(swap, copy)
        counts = scan_occurrences(box.db, box.root, hash_cache=cache)
        # The replacement holds the indexed message's bytes: hashed again
        # and bound, not skipped (or misbound) by the stale digest.
        assert (counts.hashed, counts.bound) == (1, 1)
        assert ("Trash/cur/h2:2,ST", 1) in box.active()


class TestReviewRound4:
    """Review round 4 on #1612."""

    def test_a_copy_restored_after_the_sweep_is_not_reaped(self, box, caplog):
        """The reap re-checks the disk inside its transaction: a T flag
        cleared upstream after the sweep keeps the message."""
        caplog.set_level(logging.INFO)
        path = box.put("INBOX/cur/w1:2,S", _eml("restored@example.test"))
        box.index(path)
        trashed = path.with_name("w1:2,ST")
        path.rename(trashed)
        assert box.reconciler.sweep()["tombstoned"] == 1
        trashed.rename(path)  # restored upstream; no sweep or watcher yet
        assert box.reconciler.reap()["threads_reaped"] == 0
        assert box.messages() == 1
        assert "file is back on disk since the sweep" in caplog.text
        # The next sweep follows the rename back and clears the loss.
        box.reconciler.sweep()
        assert box.active() == [("INBOX/cur/w1:2,S", 0)]
        assert box.tombstones() == []
        assert MARKER not in caplog.text

    def test_a_vanished_file_back_before_the_reap_is_kept(self, box):
        path = box.put("INBOX/cur/w2:2,S", _eml("back@example.test"))
        box.index(path)
        raw = path.read_bytes()
        path.unlink()
        assert box.reconciler.sweep()["tombstoned"] == 1
        path.write_bytes(raw)
        assert box.reconciler.reap()["threads_reaped"] == 0
        assert box.messages() == 1

    def test_a_restored_message_in_a_partial_reap_is_kept(self, box):
        first = box.put("INBOX/cur/w3:2,S", _eml("root3@example.test"))
        reply = box.put(
            "INBOX/cur/w4:2,S",
            _eml("reply3@example.test").replace(
                b"Subject:", b"In-Reply-To: <root3@example.test>\r\nSubject:", 1
            ),
        )
        box.index(first, reply)
        trashed = first.with_name("w3:2,ST")
        first.rename(trashed)
        assert box.reconciler.sweep()["tombstoned"] == 1
        trashed.rename(first)
        result = box.reconciler.reap()
        assert (result["threads_reaped"], result["threads_rebuilt"]) == (0, 0)
        assert box.messages() == 2


class TestReviewRound5:
    """Review round 5 on #1612."""

    def test_a_restore_that_changes_other_flags_is_not_reaped(self, box):
        path = box.put("INBOX/cur/w5:2,S", _eml("reflagged@example.test"))
        box.index(path)
        trashed = path.with_name("w5:2,ST")
        path.rename(trashed)
        assert box.reconciler.sweep()["tombstoned"] == 1
        # Restored upstream with another flag change; no sweep or watcher yet.
        trashed.rename(path.with_name("w5:2,RS"))
        assert box.reconciler.reap()["threads_reaped"] == 0
        assert box.messages() == 1
