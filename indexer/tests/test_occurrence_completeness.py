"""PR #1612 lifecycle catalogue; all mail and identities are synthetic."""

import logging
import os
from unittest.mock import patch

import pytest
from src import main
from src import occurrence_scan as scan
from src.queue import IndexingQueue

from tests.test_occurrence_reconciliation import MARKER, Box, _eml, _moved


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("trashed", [False, True])
@pytest.mark.parametrize("other_copy", [False, True])
def test_mismatch_retirement_respects_retention_and_last_live_copy(
    tmp_path, caplog, mirror, trashed, other_copy
):
    box = Box(tmp_path)
    try:
        raw = _eml("before@example.test")
        old = box.put("INBOX/cur/one:2,S", raw)
        paths = [old]
        if other_copy:
            paths.append(box.put("Archive/cur/two:2,S", raw))
        box.index(*paths)
        before = old.stat()
        old.write_bytes(raw.replace(b"before", b"after!"))
        os.utime(old, ns=(before.st_atime_ns, before.st_mtime_ns))
        dest = old.with_name("one:2,ST" if trashed else "one:2,FS")
        old.rename(dest)
        queue = IndexingQueue(box.db, max_attempts=3, base_backoff_seconds=0)
        handler = main.MaildirHandler(box.db, queue, reconciler=box.reconciler if mirror else None)
        with patch.object(main, "MAILDIR_PATH", box.root):
            handler.on_moved(_moved(old, dest))
        assert box.db.find_active_occurrence(str(old)) is None
        assert box.db.find_active_occurrence(str(dest)) is None
        assert len(box.active()) == int(other_copy)
        assert len(box.tombstones()) == int(mirror and not other_copy)
        assert queue.has_pending_row(str(dest)) == (not mirror or not trashed)
        assert MARKER not in caplog.text
    finally:
        box.close()


def test_size_mismatch_retires_without_reading_replacement(tmp_path, monkeypatch):
    box = Box(tmp_path)
    try:
        old = box.put("INBOX/cur/one:2,S", _eml("before@example.test"))
        box.index(old)
        dest = old.with_name("one:2,FS")
        old.rename(dest)
        dest.write_bytes(b"replacement" * 100_000)
        with patch.object(scan, "_sha256", side_effect=AssertionError("unexpected read")):
            counts = scan.scan_occurrences(box.db, box.root, hash_cache={})
        assert counts.hashed == 0
        assert counts.retired == 1
    finally:
        box.close()


def test_unheld_copies_share_the_scan_hash_budget(tmp_path, monkeypatch):
    box = Box(tmp_path)
    try:
        raw = _eml("copies@example.test")
        seed = box.put("INBOX/cur/seed:2,S", raw)
        box.index(seed)
        for i in range(6):
            box.put(f"INBOX/cur/copy-{i}:2,T", raw)
        monkeypatch.setattr(scan, "_BACKSTOP_MAX_FILES", 2)
        cache = {}
        for _ in range(4):
            with patch.object(scan, "_sha256", wraps=scan._sha256) as hashing:
                counts = scan.scan_occurrences(box.db, box.root, hash_cache=cache)
            assert hashing.call_count <= 2
            assert counts.hashed <= 2
        assert len(box.active()) == 7
    finally:
        box.close()


def test_same_inode_rewrite_invalidates_a_warm_hash_cache(tmp_path):
    box = Box(tmp_path)
    try:
        raw = _eml("before@example.test")
        path = box.put("INBOX/cur/one:2,S", raw)
        box.index(path)
        cache = {}
        assert scan.scan_occurrences(box.db, box.root, hash_cache=cache).hashed == 1
        before = path.stat()
        path.write_bytes(raw.replace(b"before", b"after!"))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.stat().st_ino == before.st_ino
        assert scan.scan_occurrences(box.db, box.root, hash_cache=cache).stale == 1
    finally:
        box.close()


def test_same_inode_restore_invalidates_mismatch_evidence(tmp_path, monkeypatch):
    box = Box(tmp_path)
    try:
        raw = _eml("before@example.test")
        path = box.put("INBOX/cur/one:2,S", raw)
        box.index(path)
        before = path.stat()
        path.write_bytes(raw.replace(b"before", b"after!"))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        hashing = scan._sha256

        def hash_then_restore(candidate, identity=None):
            digest = hashing(candidate, identity)
            path.write_bytes(raw)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert path.stat().st_ino == before.st_ino
            return digest

        monkeypatch.setattr(scan, "_sha256", hash_then_restore)
        counts = scan.scan_occurrences(box.db, box.root, hash_cache={})
        assert counts.stale == 0
        assert box.db.find_active_occurrence(str(path)) is not None
        assert box.db.queue_get_status(str(path)) is None
    finally:
        box.close()


@pytest.mark.parametrize("matching", [False, True])
def test_reap_verifies_a_former_folder_even_without_its_indexed_marker(tmp_path, matching):
    box = Box(tmp_path)
    try:
        raw = _eml("before@example.test")
        old = box.put("INBOX/cur/one:2,S", raw)
        box.index(old)
        dest = box.root / "Archive/cur/two:2,S"
        dest.parent.mkdir(parents=True)
        old.rename(dest)
        box.handle_moved(old, dest)
        assert not box.db.is_indexed(str(old))
        dest.unlink()
        box.reconciler.sweep()
        old.write_bytes(raw if matching else raw.replace(b"before", b"after!"))
        box.reconciler.reap()
        assert box.messages() == int(matching)
    finally:
        box.close()


def test_reap_checks_live_same_uniq_copy_beside_a_trashed_file(tmp_path):
    box = Box(tmp_path)
    try:
        raw = _eml("restore@example.test")
        trashed = box.put("INBOX/cur/one:2,T", raw)
        box.index(trashed)
        box.reconciler.sweep()
        box.put("INBOX/cur/one:2,FS", raw)
        box.reconciler.reap()
        assert box.messages() == 1
    finally:
        box.close()


def test_failed_hashes_spend_budget_without_starving_later_files(tmp_path, monkeypatch, caplog):
    box = Box(tmp_path)
    try:
        raw = _eml("before@example.test")
        first = box.put("INBOX/cur/a:2,S", raw)
        later = box.put("INBOX/cur/b:2,S", raw)
        box.index(first, later)
        stat_before = later.stat()
        later.write_bytes(raw.replace(b"before", b"after!"))
        os.utime(later, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
        monkeypatch.setattr(scan, "_BACKSTOP_MAX_FILES", 1)
        hashing = scan._sha256
        calls = []

        def fail_first(path, identity=None):
            calls.append(path)
            if path == first:
                raise OSError("synthetic unreadable file")
            return hashing(path, identity)

        monkeypatch.setattr(scan, "_sha256", fail_first)
        caplog.set_level(logging.INFO)
        results = [box.reconciler.sweep(), box.reconciler.sweep()]
        assert calls == [first, later]
        assert sum(r["stale"] for r in results) == 1
        assert all(r["hashed"] == 1 for r in results)
        assert "hash_deferred=1" in caplog.text
        assert MARKER not in caplog.text
    finally:
        box.close()


@pytest.mark.parametrize("flags", ["S", "FS", "RS"])
def test_source_restored_during_mismatch_check_is_kept(tmp_path, monkeypatch, flags):
    box = Box(tmp_path)
    try:
        raw = _eml("before@example.test")
        old = box.put("INBOX/cur/one:2,S", raw)
        box.index(old)
        dest = old.with_name("one:2,T")
        old.rename(dest)
        dest.write_bytes(raw.replace(b"before", b"after!"))
        retire = box.db.retire_mismatched_rename

        def restore_then_retire(*args, **kwargs):
            old.with_name(f"one:2,{flags}").write_bytes(raw)
            return retire(*args, **kwargs)

        monkeypatch.setattr(box.db, "retire_mismatched_rename", restore_then_retire)
        counts = scan.scan_occurrences(box.db, box.root, hash_cache={})
        assert counts.retired == 0
        assert box.db.find_active_occurrence(str(old)) is not None
    finally:
        box.close()


def test_held_size_mismatch_retires_an_oversized_replacement_without_hashing(tmp_path):
    box = Box(tmp_path)
    try:
        path = box.put("INBOX/cur/one:2,S", _eml("before@example.test"))
        box.index(path)
        with path.open("wb") as handle:
            handle.truncate(scan.MAX_HASH_BYTES + 1)
        with patch.object(scan, "_sha256", side_effect=AssertionError("unexpected read")):
            counts = scan.scan_occurrences(box.db, box.root, hash_cache={})
        assert counts.hashed == 0
        assert counts.stale == 1
        assert box.db.find_active_occurrence(str(path)) is None
        assert box.db.queue_get_status(str(path)) == "queued"
    finally:
        box.close()


def test_rename_proofs_get_budget_before_startup_backstop(tmp_path, monkeypatch):
    box = Box(tmp_path)
    try:
        paths = [
            box.put(f"INBOX/cur/aaa-{i}:2,S", _eml(f"held-{i}@example.test")) for i in range(2)
        ]
        old = box.put("INBOX/cur/zzz:2,S", _eml("rename@example.test"))
        box.index(*paths, old)
        new = old.with_name("zzz:2,FS")
        old.rename(new)
        monkeypatch.setattr(scan, "_BACKSTOP_MAX_FILES", 2)
        counts = scan.scan_occurrences(box.db, box.root, hash_cache={})
        assert counts.hashed <= 2
        assert counts.renamed == 1
        assert box.db.find_active_occurrence(str(new)) is not None
    finally:
        box.close()


@pytest.mark.parametrize("renamed", [False, True])
def test_partial_scan_still_handles_a_proven_mismatch_in_a_readable_folder(
    tmp_path, monkeypatch, renamed
):
    box = Box(tmp_path)
    try:
        raw = _eml("before@example.test")
        old = box.put("INBOX/cur/one:2,S", raw)
        box.index(old)
        new = old.with_name("one:2,FS") if renamed else old
        if renamed:
            old.rename(new)
        new.write_bytes(raw.replace(b"before", b"after!"))
        (box.root / "Unreadable").mkdir()
        walk = scan.os.scandir

        def unreadable(path):
            if str(path).endswith("Unreadable"):
                raise PermissionError("SYNTHETIC_PRIVATE_MARKER")
            return walk(path)

        monkeypatch.setattr(scan.os, "scandir", unreadable)
        counts = scan.scan_occurrences(box.db, box.root, hash_cache={})
        assert not counts.complete
        assert counts.stale + counts.retired == 1
        assert box.db.find_active_occurrence(str(old)) is None
    finally:
        box.close()


def test_reap_distinguishes_hash_deferral_from_a_verified_restore(tmp_path, monkeypatch, caplog):
    from src import file_evidence

    box = Box(tmp_path)
    try:
        raw = _eml("before@example.test")
        path = box.put("INBOX/cur/one:2,S", raw)
        box.index(path)
        path.unlink()
        box.reconciler.sweep()
        path.write_bytes(raw.replace(b"before", b"after!"))
        monkeypatch.setattr(file_evidence, "MAX_HASH_FILES", 0)
        caplog.set_level(logging.INFO)
        assert box.reconciler.reap()["threads_reaped"] == 0
        assert "restore verification incomplete" in caplog.text
        assert "hash verification deferred: 1 candidate(s)" in caplog.text
        assert "file is back on disk" not in caplog.text
        assert MARKER not in caplog.text
    finally:
        box.close()


def test_failed_restore_hashes_cannot_starve_a_later_independent_thread(tmp_path, monkeypatch):
    import time

    from src import file_evidence

    box = Box(tmp_path)
    try:
        raws = [_eml(f"before-{i}@example.test") for i in range(2)]
        paths = [box.put(f"INBOX/cur/{i}:2,S", raw) for i, raw in enumerate(raws)]
        box.index(*paths)
        for path in paths:
            path.unlink()
        box.reconciler.sweep()
        for path, raw in zip(paths, raws, strict=True):
            path.write_bytes(raw.replace(b"before", b"after!"))
        monkeypatch.setattr(file_evidence, "MAX_HASH_FILES", 1)
        digest = file_evidence.read_digest
        reads = []

        def unreadable_first(path, identity):
            reads.append(path)
            if path == paths[0]:
                raise PermissionError("SYNTHETIC_PRIVATE_MARKER")
            return digest(path, identity)

        monkeypatch.setattr(file_evidence, "read_digest", unreadable_first)
        started = time.monotonic()
        assert box.reconciler.reap()["threads_reaped"] == 0
        assert box.reconciler.reap()["threads_reaped"] == 1
        assert reads == paths
        assert len(box.tombstones()) == 1
        assert time.monotonic() - started < 5
    finally:
        box.close()


def test_failed_read_spends_partial_byte_budget_without_pinning_admission(tmp_path, monkeypatch):
    box = Box(tmp_path)
    try:
        raw = _eml("copies@example.test")
        seed = box.put("INBOX/cur/seed:2,S", raw)
        box.index(seed)
        first = box.put("INBOX/cur/a:2,T", raw)
        second = box.put("INBOX/cur/b:2,T", raw)
        monkeypatch.setattr(scan, "_BACKSTOP_MAX_BYTES", len(raw) * 3 // 2)
        original = scan._sha256
        reads = []

        def digest(path, identity=None):
            reads.append(path)
            if path == first:
                raise PermissionError("SYNTHETIC_PRIVATE_MARKER")
            return original(path, identity)

        monkeypatch.setattr(scan, "_sha256", digest)
        cache = {}
        for _ in range(4):
            counts = scan.scan_occurrences(box.db, box.root, hash_cache=cache)
            assert counts.hashed <= 1
        assert reads[0] == first
        assert second in reads
        assert len(reads) <= 4
        assert box.db.find_active_occurrence(str(second)) is not None
    finally:
        box.close()


def test_unreadable_rename_cannot_starve_the_held_path_backstop(tmp_path, monkeypatch):
    box = Box(tmp_path)
    try:
        old = box.put("INBOX/cur/a:2,S", _eml("rename@example.test"))
        raw = _eml("before@example.test")
        held = box.put("INBOX/cur/z:2,S", raw)
        box.index(old, held)
        renamed = old.with_name("a:2,FS")
        old.rename(renamed)
        held.write_bytes(raw.replace(b"before", b"after!"))
        monkeypatch.setattr(scan, "_BACKSTOP_MAX_FILES", 1)
        original = scan._sha256
        reads = []

        def digest(path, identity=None):
            reads.append(path)
            if path == renamed:
                raise PermissionError("SYNTHETIC_PRIVATE_MARKER")
            return original(path, identity)

        monkeypatch.setattr(scan, "_sha256", digest)
        cache = {}
        assert scan.scan_occurrences(box.db, box.root, hash_cache=cache).stale == 0
        assert scan.scan_occurrences(box.db, box.root, hash_cache=cache).stale == 1
        assert reads == [renamed, held]
    finally:
        box.close()


def test_oversized_deferred_copy_cannot_pin_admissible_copy_cursor(tmp_path, monkeypatch):
    box = Box(tmp_path)
    try:
        raw = _eml("copies@example.test")
        seed = box.put("INBOX/cur/seed:2,S", raw)
        box.index(seed)
        box.put("INBOX/cur/a-large:2,T", b"x" * (len(raw) * 2 + 1))
        failed = box.put("INBOX/cur/b-failed:2,T", raw)
        good = box.put("INBOX/cur/c-good:2,T", raw)
        monkeypatch.setattr(scan, "_BACKSTOP_MAX_FILES", 1)
        monkeypatch.setattr(scan, "_BACKSTOP_MAX_BYTES", len(raw) * 2)
        original = scan._sha256
        reads = []

        def digest(path, identity=None):
            reads.append(path)
            if path == failed:
                raise PermissionError("SYNTHETIC_PRIVATE_MARKER")
            return original(path, identity)

        monkeypatch.setattr(scan, "_sha256", digest)
        cache = {}
        for _ in range(4):
            assert scan.scan_occurrences(box.db, box.root, hash_cache=cache).hashed <= 1
        assert good in reads
        assert box.db.find_active_occurrence(str(good)) is not None
        assert all(path.name != "a-large:2,T" for path in reads)
    finally:
        box.close()
