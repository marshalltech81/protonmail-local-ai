"""
Tests for src/reconciler.py — tombstone detection, live on_moved handling,
reap behavior (full-thread and rebuild paths), grace window, mass-delete
brake, and embedding-service-failure backoff.

The reconciler is exercised end-to-end against a real SQLite database and
real .eml files in tmp_path. Embedding is stubbed with FakeEmbedder so the
tests do not require a live embedding service.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
from src.database import (
    EMBEDDING_DIM,
    Database,
)
from src.occurrence_scan import file_identity
from src.reconciler import Reconciler, ReconcilerConfig, load_config_from_env, sweep_paths
from src.threader import Threader

from tests.conftest import TEST_ACCOUNT, count_pending_deletions, lose_message

FAKE_EMBEDDING = [0.0] * EMBEDDING_DIM


def _handle_moved(reconciler: Reconciler, src: str, dest: str) -> None:
    """``Reconciler.handle_moved``, with the destination's identity
    verified as the real watcher caller (``main.py``'s ``on_moved``)
    does (#1612). Every call in this file is a genuine, unmodified
    rename, so ``dest``'s current stat is exactly what verification
    would compute."""
    reconciler.handle_moved(src, dest, verified_identity=file_identity(Path(dest)))


class FakeEmbedder:
    def __init__(self):
        self.calls = 0
        self.should_fail = False

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        if self.should_fail:
            raise RuntimeError("simulated embedder outage")
        return FAKE_EMBEDDING

    def embed_batch(self, texts: list[str], **_kw) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def _default_config(**overrides) -> ReconcilerConfig:
    base = {
        "enabled": True,
        "grace_days": 0,
        "sweep_interval_secs": 60,
        "max_batch_pct": 1.0,
        "force": False,
    }
    base.update(overrides)
    return ReconcilerConfig(**base)


def _write_eml(
    path: Path,
    message_id: str,
    subject: str = "Test message",
    body: str = "Hello",
    in_reply_to: str | None = None,
    date: datetime | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["Message-ID"] = f"<{message_id}>"
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = subject
    msg["Date"] = (date or datetime(2024, 1, 1, 12, 0, tzinfo=UTC)).strftime(
        "%a, %d %b %Y %H:%M:%S %z"
    )
    if in_reply_to:
        msg["In-Reply-To"] = f"<{in_reply_to}>"
    msg.set_content(body)
    path.write_bytes(bytes(msg))


def _index(path: Path, db: Database, threader: Threader) -> str:
    """Parse + thread + upsert a real .eml file. Returns the thread_id."""
    from src.parser import parse_email

    parsed = parse_email(path)
    assert parsed is not None
    thread = threader.assign_thread(parsed)
    db.upsert_thread(thread, FAKE_EMBEDDING)
    return thread.thread_id


@pytest.fixture
def maildir(tmp_path: Path) -> Path:
    d = tmp_path / "maildir" / "INBOX" / "cur"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def db(tmp_path: Path) -> Database:
    """The account's Maildir root is the test's ``maildir`` tree, which
    the reconciliation scan walks (#1567)."""
    (tmp_path / "maildir").mkdir(exist_ok=True)
    database = Database(
        tmp_path / "test.db", account=replace(TEST_ACCOUNT, maildir_root=tmp_path / "maildir")
    )
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def reconciler(db: Database, embedder: FakeEmbedder) -> Reconciler:
    return Reconciler(db, embedder, _default_config(), maildir_root=db.account.maildir_root)


# ---------------------------------------------------------------------------
# Sweep — startup detection
# ---------------------------------------------------------------------------


class TestSweep:
    def test_records_tombstone_when_file_has_t_flag(self, db, threader, reconciler, maildir):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "m1@example.com")
        _index(path, db, threader)

        # mbsync renames to add T flag
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)

        result = reconciler.sweep()
        assert result["tombstoned"] == 1
        assert db.has_pending_deletion(str(trashed))

    def test_updates_filepath_when_non_deletion_flag_changes(
        self, db, threader, reconciler, maildir
    ):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "m2@example.com")
        _index(path, db, threader)

        # mbsync renames to add R flag (replied)
        replied = maildir / "1700000000.M1.host:2,RS"
        path.rename(replied)

        result = reconciler.sweep()
        assert result["renamed"] == 1
        assert result["tombstoned"] == 0
        assert db.find_active_occurrence(str(replied)) is not None
        assert db.find_active_occurrence(str(path)) is None

    def test_clears_tombstone_when_t_flag_reversed(self, db, threader, reconciler, maildir):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "m3@example.com")
        _index(path, db, threader)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)
        reconciler.sweep()
        assert db.has_pending_deletion(str(trashed))

        # mbsync un-flags on a subsequent pull (T removed)
        restored = maildir / "1700000000.M1.host:2,S"
        trashed.rename(restored)

        # The scan follows the restoring rename, whose write clears the
        # tombstones (#1567).
        result = reconciler.sweep()
        assert result["renamed"] == 1
        assert db.has_pending_deletion(str(restored)) is False
        assert count_pending_deletions(db) == 0

    def test_idempotent_across_multiple_sweeps(self, db, threader, reconciler, maildir):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "m4@example.com")
        _index(path, db, threader)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)

        first = reconciler.sweep()
        second = reconciler.sweep()
        assert first["tombstoned"] == 1
        assert second["tombstoned"] == 0  # already recorded
        assert count_pending_deletions(db) == 1

    def test_marks_missing_file_as_tombstone(self, db, threader, reconciler, maildir):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "m5@example.com")
        _index(path, db, threader)
        path.unlink()

        result = reconciler.sweep()
        assert (result["retired"], result["tombstoned"]) == (1, 1)

    def test_restore_during_sweep_leaves_no_stale_tombstone(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """#301: the sweep's walk saw the trashed file, the watcher then
        restored it and cleared its tombstone; the sweep must not carry
        the occurrence back to the obsolete trashed path or tombstone the
        restored message."""
        import src.occurrence_scan as scan_module

        rec = Reconciler(db, embedder, _default_config(), maildir_root=db.account.maildir_root)
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "restored@example.com")
        thread_id = _index(path, db, threader)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)
        _handle_moved(rec, str(path), str(trashed))
        assert db.has_pending_deletion(str(trashed))

        real_walk = scan_module._walk

        def walk_then_restore(*args):
            files = real_walk(*args)
            trashed.rename(path)
            _handle_moved(rec, str(trashed), str(path))  # the watcher wins the lock here
            return files

        monkeypatch.setattr(scan_module, "_walk", walk_then_restore)
        assert rec.sweep()["tombstoned"] == 0
        monkeypatch.setattr(scan_module, "_walk", real_walk)

        assert db.find_active_occurrence(str(path)) is not None
        assert count_pending_deletions(db) == 0
        rec.sweep()
        assert rec.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None

    def test_move_during_sweep_does_not_tombstone_the_old_path(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """#301, missing-file branch: the watcher moved the file to another
        folder after the sweep's walk listed it in the old one; the sweep
        must not retire the moved occurrence or tombstone the message."""
        import src.occurrence_scan as scan_module

        rec = Reconciler(db, embedder, _default_config(), maildir_root=db.account.maildir_root)
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "moved@example.com")
        thread_id = _index(path, db, threader)
        archived = maildir.parent.parent / "Archive" / "cur" / path.name
        archived.parent.mkdir(parents=True)

        real_walk = scan_module._walk

        def walk_then_move(*args):
            files = real_walk(*args)
            path.rename(archived)
            _handle_moved(rec, str(path), str(archived))
            return files

        monkeypatch.setattr(scan_module, "_walk", walk_then_move)
        assert rec.sweep()["retired"] == 0
        monkeypatch.setattr(scan_module, "_walk", real_walk)

        assert count_pending_deletions(db) == 0
        assert rec.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None


# Byte-identical copies are occurrences of their own (#1566, #1567):
# removing one copy, the last copy, a trashed copy beside a live one, a
# copy found by the scan and an unmapped copy moved across folders are
# covered in ``tests/test_occurrence_reconciliation.py``.


class TestSweepPaths:
    def test_updates_filepath_on_flag_rename(self, db, threader, maildir):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "sp1@example.com")
        _index(path, db, threader)

        replied = maildir / "1700000000.M1.host:2,RS"
        path.rename(replied)

        result = sweep_paths(db, maildir_root=db.account.maildir_root)
        assert result["renamed"] == 1
        assert result["unreachable"] == 0
        assert db.find_active_occurrence(str(replied)) is not None
        assert db.find_active_occurrence(str(path)) is None

    def test_heals_read_state_changed_while_offline(self, db, threader, maildir):
        """A flag change mbsync made while the indexer was down reaches
        the message's state through the startup sweep."""
        path = maildir / "1700000000.M9.host:2,"
        _write_eml(path, "sp-state@example.com")
        _index(path, db, threader)
        starred = maildir / "1700000000.M9.host:2,FS"
        path.rename(starred)

        sweep_paths(db, maildir_root=db.account.maildir_root)

        row = db._conn.execute(
            "SELECT relative_path, seen, flagged, replied FROM message_occurrences"
        ).fetchone()
        assert (str(db.account.maildir_root / row[0]), *row[1:]) == (str(starred), 1, 1, 0)

    def test_lists_each_folder_once_however_many_files_were_renamed(
        self, db, threader, maildir, monkeypatch
    ):
        """The sweep runs before the startup walk; rescanning the whole
        folder for every stale path would be O(N^2) for N files renamed
        while the indexer was down (#246). Each directory is listed once
        per sweep, by its walk, and a rename is verified from the
        recorded file identity without reading the file (#1567)."""
        count = 6
        for i in range(count):
            path = maildir / f"17000000{i:02d}.M{i}.host:2,S"
            _write_eml(path, f"many{i}@example.com")
            _index(path, db, threader)
            path.rename(path.with_name(path.name.replace(":2,S", ":2,RS")))
        import os

        import src.occurrence_scan as scan_module

        listed: list[str] = []
        real_scandir = os.scandir

        def counting_scandir(path="."):
            listed.append(str(path))
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", counting_scandir)
        hashed: list[Path] = []
        monkeypatch.setattr(scan_module, "_sha256", lambda p: hashed.append(p) or "")

        result = sweep_paths(db, maildir_root=db.account.maildir_root)

        assert result == {
            "renamed": count,
            "retired": 0,
            "unreachable": 0,
            "tombstones_cleared": 0,
            "bound": 0,
            "unverified": 0,
            "stale": 0,
            "complete": True,
        }
        assert len(listed) == len(set(listed))
        assert str(maildir) in listed
        assert hashed == []

    def test_does_not_tombstone_missing_files(self, db, threader, maildir):
        """sweep_paths is the always-on variant; a missing file's
        occurrence is retired by a complete scan but nothing is recorded
        in pending_deletions. Reconciler.sweep() is still responsible for
        tombstoning when deletion reconciliation is enabled (mirror
        mode)."""
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "sp2@example.com")
        _index(path, db, threader)
        path.unlink()

        result = sweep_paths(db, maildir_root=db.account.maildir_root)
        assert result["retired"] == 1
        assert count_pending_deletions(db) == 0

    def test_does_not_tombstone_t_flagged_files(self, db, threader, maildir):
        """A trashed file is reachable at its new path, so sweep_paths
        updates the filepath and keeps the row alive. Tombstoning of
        T-flagged files belongs to the full Reconciler."""
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "sp3@example.com")
        _index(path, db, threader)

        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)

        result = sweep_paths(db, maildir_root=db.account.maildir_root)
        assert result["renamed"] == 1
        assert count_pending_deletions(db) == 0

    def test_idempotent(self, db, threader, maildir):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "sp4@example.com")
        _index(path, db, threader)
        replied = maildir / "1700000000.M1.host:2,RS"
        path.rename(replied)

        first = sweep_paths(db, maildir_root=db.account.maildir_root)
        second = sweep_paths(db, maildir_root=db.account.maildir_root)
        assert first["renamed"] == 1
        assert second["renamed"] == 0

    def _index_tombstoned(self, db, threader, maildir, subject: str) -> Path:
        """One indexed ``T``-flagged file carrying a tombstone, as a
        mirror-mode run leaves it; archive mode writes none itself."""
        path = maildir / "1700000000.M1.host:2,ST"
        _write_eml(path, "sp-restore@example.com", subject=subject)
        _index(path, db, threader)
        entry = db.find_active_occurrence(str(path))
        assert entry is not None
        lose_message(db, entry["claimant_id"])
        return path

    def test_clears_leftover_tombstone_on_restore_seen_at_startup(
        self, db, threader, maildir, caplog
    ):
        """#860: a restore (``T`` flag cleared) that
        mbsync applied while the indexer was down reaches the index only
        through this sweep, which runs in archive mode too, where no
        reconciler would clear the tombstone later. The sweep applies the
        same restore rule as ``handle_moved``: the tombstone is dropped
        with the rename, counted on the sweep's own line, and nothing is
        reaped or tombstoned."""
        marker = "SYNTHETIC_MARKER_860"
        path = self._index_tombstoned(db, threader, maildir, marker)
        live = maildir / "1700000000.M1.host:2,S"
        path.rename(live)

        with caplog.at_level(logging.INFO):
            result = sweep_paths(db, maildir_root=db.account.maildir_root)

        assert (result["renamed"], result["tombstones_cleared"]) == (1, 1)
        assert count_pending_deletions(db) == 0
        assert db.find_active_occurrence(str(live)) is not None
        assert db._conn.execute("SELECT count(*) FROM message_thread_map").fetchone()[0] == 1
        assert [
            (r.levelno, r.getMessage())
            for r in caplog.records
            if r.getMessage().startswith("startup rename sweep")
        ] == [
            (
                logging.INFO,
                "startup rename sweep: renamed=1 retired=0 unreachable=0 tombstones_cleared=1 "
                "bound=0 unverified=0 stale=0",
            )
        ]
        assert marker not in caplog.text

    def test_keeps_leftover_tombstone_while_file_stays_trashed(self, db, threader, maildir, caplog):
        """A flag rename that keeps ``T`` is no restore: the message keeps
        its tombstones, its occurrence follows the file to its new name,
        and the sweep counts none cleared."""
        marker = "SYNTHETIC_MARKER_860"
        path = self._index_tombstoned(db, threader, maildir, marker)
        starred = maildir / "1700000000.M1.host:2,FST"
        path.rename(starred)

        with caplog.at_level(logging.INFO):
            result = sweep_paths(db, maildir_root=db.account.maildir_root)

        assert (result["renamed"], result["tombstones_cleared"]) == (1, 0)
        assert db.has_pending_deletion(str(starred))
        assert not db.has_pending_deletion(str(path))
        assert count_pending_deletions(db) == 1
        assert marker not in caplog.text


# ---------------------------------------------------------------------------
# handle_moved — live watchdog detection
# ---------------------------------------------------------------------------


class TestHandleMoved:
    def test_records_tombstone_on_t_flag_rename(self, db, threader, reconciler, maildir):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "mv1@example.com")
        _index(path, db, threader)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)

        _handle_moved(reconciler, str(path), str(trashed))
        assert db.has_pending_deletion(str(trashed))

    def test_clears_tombstone_when_flag_removed(self, db, threader, reconciler, maildir):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "mv2@example.com")
        _index(path, db, threader)

        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)
        _handle_moved(reconciler, str(path), str(trashed))
        assert db.has_pending_deletion(str(trashed))

        restored = maildir / "1700000000.M1.host:2,S"
        trashed.rename(restored)
        _handle_moved(reconciler, str(trashed), str(restored))
        assert db.has_pending_deletion(str(trashed)) is False
        assert db.has_pending_deletion(str(restored)) is False

    def test_ignores_moves_for_unindexed_files(self, db, threader, reconciler, maildir):
        src = maildir / "unknown:2,S"
        dest = maildir / "unknown:2,ST"
        # Must not raise, must not record anything
        _handle_moved(reconciler, str(src), str(dest))
        assert count_pending_deletions(db) == 0


# ---------------------------------------------------------------------------
# Reap — grace window, full-reap, rebuild paths
# ---------------------------------------------------------------------------


class TestReap:
    def test_does_not_reap_inside_grace_window(self, db, threader, embedder, maildir):
        cfg = _default_config(grace_days=7)
        rec = Reconciler(db, embedder, cfg, maildir_root=db.account.maildir_root)

        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "g1@example.com")
        _index(path, db, threader)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)
        rec.sweep()

        result = rec.reap()
        assert result["threads_reaped"] == 0
        assert result["threads_rebuilt"] == 0
        assert db.get_thread(db.find_active_occurrence(str(trashed))["thread_id"]) is not None

    def test_aba_move_during_sweep_does_not_reap_the_live_message(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """#336: the sweep's walk saw the file at B while it was briefly
        away from A, and the watcher moved it back to A before the scan
        read the occurrences; with a zero grace window a stale retire or
        tombstone would let the next reap delete the live message."""
        import src.occurrence_scan as scan_module

        rec = Reconciler(db, embedder, _default_config(), maildir_root=db.account.maildir_root)
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "aba@example.com")
        thread_id = _index(path, db, threader)
        elsewhere = maildir.parent.parent / "Archive" / "cur" / path.name
        elsewhere.parent.mkdir(parents=True)

        real_walk = scan_module._walk

        def move_away_walk_move_back(*args):
            path.rename(elsewhere)
            _handle_moved(rec, str(path), str(elsewhere))
            files = real_walk(*args)
            elsewhere.rename(path)
            _handle_moved(rec, str(elsewhere), str(path))
            return files

        monkeypatch.setattr(scan_module, "_walk", move_away_walk_move_back)
        assert rec.sweep()["retired"] == 0
        monkeypatch.setattr(scan_module, "_walk", real_walk)

        assert rec.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None
        assert count_pending_deletions(db) == 0

    def test_the_reap_lists_each_directory_at_most_once(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """#336: each tombstone whose mapped file was gone made the live
        check list the folder again. Liveness is the occurrence state the
        sweep reconciled (#1567); the reap's on-disk restore check
        (review rounds 4 and 5 on #1612) lists each directory at most once
        per pass."""
        import os

        paths = []
        for i in range(20):
            p = maildir / f"1700000{i:04d}.M1.host:2,S"
            _write_eml(p, f"gone{i}@example.com", subject=f"Subject {i}")
            _index(p, db, threader)
            paths.append(p)
        for p in paths:
            entry = db.find_active_occurrence(str(p))
            lose_message(db, entry["claimant_id"])
            p.unlink()

        listed: list[object] = []
        real_iterdir = Path.iterdir
        real_scandir = os.scandir

        def counting_iterdir(self):
            listed.append(self)
            return real_iterdir(self)

        def counting_scandir(path="."):
            listed.append(path)
            return real_scandir(path)

        monkeypatch.setattr(Path, "iterdir", counting_iterdir)
        monkeypatch.setattr(os, "scandir", counting_scandir)
        rec = Reconciler(
            db, embedder, _default_config(force=True), maildir_root=db.account.maildir_root
        )
        assert rec.reap()["threads_reaped"] == 20
        # ``Path.iterdir`` may list through ``os.scandir``: at most one
        # listing (each hook once) per directory, and only INBOX's.
        directories = {str(p) for p in listed}
        assert len(directories) <= 2
        assert len(listed) <= 2 * len(directories)

    def test_reap_clears_an_orphan_tombstone_for_a_live_message(
        self, db, threader, embedder, maildir
    ):
        """A stale tombstone on a message with a live occurrence (left by
        an older release, #301): the reaper never takes it, and the next
        sweep clears it."""
        rec = Reconciler(db, embedder, _default_config(), maildir_root=db.account.maildir_root)
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "orphan@example.com")
        thread_id = _index(path, db, threader)
        entry = db.find_active_occurrence(str(path))
        db._conn.execute(
            "INSERT INTO pending_deletions (occurrence_id, claimant_id, marked_at) "
            "VALUES (?, ?, '2000-01-01T00:00:00+00:00')",
            (entry["id"], entry["claimant_id"]),
        )
        db._conn.commit()

        assert rec.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None
        assert rec.sweep()["cleared"] == 1
        assert count_pending_deletions(db) == 0

    def test_full_reap_when_last_message_tombstoned(self, db, threader, reconciler, maildir):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "full@example.com")
        thread_id = _index(path, db, threader)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)

        reconciler.sweep()
        result = reconciler.reap()

        assert result["threads_reaped"] == 1
        assert result["threads_rebuilt"] == 0
        assert db.get_thread(thread_id) is None
        assert db.count_total_messages() == 0
        assert count_pending_deletions(db) == 0

    def test_full_reap_removes_the_pending_job(self, db, threader, reconciler, maildir):
        """#244: a job still queued for the message (an embedder outage
        past the grace window) was left behind, and draining it later
        re-indexed the reaped message from the kept .eml."""
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "queued@example.com")
        _index(path, db, threader)
        queue = IndexingQueue(db)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)
        reconciler.sweep()  # moves the job onto the trashed path
        assert queue.has_pending_row(str(trashed))

        assert reconciler.reap()["threads_reaped"] == 1
        assert not queue.has_pending_row(str(trashed))
        assert queue.stats().get("queued", 0) == 0

    def test_partial_reap_removes_only_the_reaped_job(self, db, threader, reconciler, maildir):
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        orig = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig, "orig@example.com", subject="Budget")
        _index(orig, db, threader)
        reply = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply,
            "reply@example.com",
            subject="Re: Budget",
            in_reply_to="orig@example.com",
            date=datetime(2024, 2, 1, 12, 0, tzinfo=UTC),
        )
        _index(reply, db, threader)
        queue = IndexingQueue(db)
        queue.enqueue(str(orig), REASON_INITIAL_SCAN)
        queue.enqueue(str(reply), REASON_INITIAL_SCAN)
        trashed = maildir / "1700000000.M1.host:2,ST"
        orig.rename(trashed)
        reconciler.sweep()

        assert reconciler.reap()["threads_rebuilt"] == 1
        assert not queue.has_pending_row(str(trashed))
        assert queue.has_pending_row(str(reply))

    def _restore_after_snapshot(self, db, monkeypatch, trashed, live):
        """The watcher restores a message after ``reap()``
        read its tombstones but before the reap transaction runs."""
        snapshot = db.reapable_messages(datetime.now(UTC).isoformat())
        assert snapshot
        trashed.rename(live)
        # The watcher's write: the occurrence follows, the tombstones go.
        db.update_filepath(
            str(trashed), str(live), move_occurrence=True, verified_identity=file_identity(live)
        )
        monkeypatch.setattr(db, "reapable_messages", lambda _cutoff: snapshot)

    def test_full_reap_skips_a_message_restored_after_the_snapshot(
        self, db, threader, reconciler, maildir, monkeypatch
    ):
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "restored@example.com")
        thread_id = _index(path, db, threader)
        queue = IndexingQueue(db)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)
        reconciler.sweep()
        self._restore_after_snapshot(db, monkeypatch, trashed, path)

        assert reconciler.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None
        assert queue.has_pending_row(str(path))

    def test_reap_skips_a_message_re_trashed_after_the_snapshot(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """Restored and T-flagged again after the snapshot,
        the message has a fresh tombstone, so its grace period restarts;
        matching any tombstone by message ID deleted it at once."""
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        rec = Reconciler(
            db, embedder, _default_config(grace_days=7), maildir_root=db.account.maildir_root
        )
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "again@example.com")
        thread_id = _index(path, db, threader)
        queue = IndexingQueue(db)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)
        rec.sweep()
        with db.transaction():
            db._conn.execute("UPDATE pending_deletions SET marked_at = '2000-01-01T00:00:00+00:00'")
        snapshot = db.reapable_messages(datetime.now(UTC).isoformat())
        assert snapshot
        # Restored, then trashed again: a new tombstone, a new grace period.
        claimant = snapshot[0]["claimant_id"]
        db._conn.execute("DELETE FROM pending_deletions")
        db._conn.commit()
        lose_message(db, claimant)
        monkeypatch.setattr(db, "reapable_messages", lambda _cutoff: snapshot)

        assert rec.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None
        assert queue.has_pending_row(str(trashed))

    def test_reap_between_restore_steps_keeps_the_message(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """``handle_moved`` committed the rename, then
        cleared the tombstone in a separate step; a reap on the main thread
        in between still saw the eligible tombstone and deleted the
        restored message. The rename and the clear are one write."""
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        rec = Reconciler(db, embedder, _default_config(), maildir_root=db.account.maildir_root)
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "restoring@example.com")
        thread_id = _index(path, db, threader)
        queue = IndexingQueue(db)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)
        _handle_moved(rec, str(path), str(trashed))
        trashed.rename(path)

        real_update = db.update_filepath

        def update_then_reap(*args, **kwargs):
            result = real_update(*args, **kwargs)
            rec.reap()  # the main thread wins the lock right after the rename
            return result

        monkeypatch.setattr(db, "update_filepath", update_then_reap)
        _handle_moved(rec, str(trashed), str(path))

        assert db.get_thread(thread_id) is not None
        assert not db.has_pending_deletion(str(path))
        assert queue.has_pending_row(str(path))

    def test_partial_reap_skips_a_message_restored_after_the_snapshot(
        self, db, threader, reconciler, maildir, monkeypatch
    ):
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        orig = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig, "orig@example.com", subject="Budget")
        thread_id = _index(orig, db, threader)
        reply = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply,
            "reply@example.com",
            subject="Re: Budget",
            in_reply_to="orig@example.com",
            date=datetime(2024, 2, 1, 12, 0, tzinfo=UTC),
        )
        _index(reply, db, threader)
        queue = IndexingQueue(db)
        queue.enqueue(str(orig), REASON_INITIAL_SCAN)
        trashed = maildir / "1700000000.M1.host:2,ST"
        orig.rename(trashed)
        reconciler.sweep()
        self._restore_after_snapshot(db, monkeypatch, trashed, orig)

        assert reconciler.reap()["threads_rebuilt"] == 0
        assert {r["message_id"] for r in db.get_thread_messages(thread_id)} == {
            "orig@example.com",
            "reply@example.com",
        }
        assert queue.has_pending_row(str(orig))

    def test_rebuild_when_thread_has_survivors(self, db, threader, embedder, reconciler, maildir):
        # Two messages in one thread; tombstone the original, keep the reply.
        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(
            orig_path,
            "orig@example.com",
            subject="Budget discussion",
            body="Original message body.",
        )
        thread_id = _index(orig_path, db, threader)

        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "reply@example.com",
            subject="Re: Budget discussion",
            body="Reply body content.",
            in_reply_to="orig@example.com",
            date=datetime(2024, 2, 1, 12, 0, tzinfo=UTC),
        )
        _index(reply_path, db, threader)

        # mbsync flags the original as deleted; reply survives
        orig_trashed = maildir / "1700000000.M1.host:2,ST"
        orig_path.rename(orig_trashed)

        reconciler.sweep()
        embed_calls_before = embedder.calls
        result = reconciler.reap()

        assert result["threads_rebuilt"] == 1
        assert result["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None
        # Original message row gone, reply row stays
        assert db.find_active_occurrence(str(orig_trashed)) is None
        assert db.find_active_occurrence(str(reply_path)) is not None
        # body_text was rebuilt — the original body no longer appears
        body = db._conn.execute(
            "SELECT body_text FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()["body_text"]
        assert "Original message body." not in body
        assert "Reply body content." in body
        # Re-embedding happened
        assert embedder.calls > embed_calls_before

    def test_backs_off_when_embedder_fails(self, db, threader, embedder, reconciler, maildir):
        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "e1@example.com")
        thread_id = _index(orig_path, db, threader)

        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "e2@example.com",
            in_reply_to="e1@example.com",
            subject="Re: Test message",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply_path, db, threader)

        trashed = maildir / "1700000000.M1.host:2,ST"
        orig_path.rename(trashed)
        reconciler.sweep()

        embedder.should_fail = True
        result = reconciler.reap()
        assert result["threads_rebuilt"] == 0
        # Nothing was committed — thread and tombstone remain
        assert db.get_thread(thread_id) is not None
        assert db.has_pending_deletion(str(trashed))
        # On next pass with embedder healthy, reap succeeds
        embedder.should_fail = False
        result = reconciler.reap()
        assert result["threads_rebuilt"] == 1
        assert db.has_pending_deletion(str(trashed)) is False

    def test_embed_failure_log_omits_provider_response_body(
        self, db, threader, embedder, reconciler, maildir, caplog
    ):
        """Regression (#206): a provider status error can echo the input
        (here, the survivor's subject) in its body. The reaper logged the
        raw exception, bypassing ``scrub_embed_error``."""
        import httpx2
        from openai import BadRequestError

        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "e1@example.com")
        _index(orig_path, db, threader)
        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "e2@example.com",
            in_reply_to="e1@example.com",
            subject="Re: SYNTHETIC_PRIVATE_SUBJECT",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply_path, db, threader)
        orig_path.rename(maildir / "1700000000.M1.host:2,ST")
        reconciler.sweep()

        def echoing_embed(text):
            raise BadRequestError(
                message=f"invalid input: {text}",
                response=httpx2.Response(400, request=httpx2.Request("POST", "http://x")),
                body={"error": f"invalid input: {text}"},
            )

        embedder.embed = echoing_embed
        with caplog.at_level("WARNING"):
            assert reconciler.reap()["threads_rebuilt"] == 0

        assert "reaper: embedding failed" in caplog.text
        assert "SYNTHETIC_PRIVATE_SUBJECT" not in caplog.text
        assert "status=400" in caplog.text
        # The operator can tell which thread is stuck from the survivor's
        # mbsync path, without the thread ID (#257).
        assert str(reply_path) in caplog.text
        assert "e1@example.com" not in caplog.text

    def test_flag_rename_during_reap_does_not_revive_the_deleted_message(
        self, db, threader, embedder, reconciler, maildir, monkeypatch
    ):
        """Regression (#213): the watcher renamed a tombstoned file
        (``:2,ST`` -> ``:2,RST``) after ``reap`` snapshotted tombstones
        but before it picked survivors. Survivors were chosen by the
        snapshot's stale path, so the deleted message was rebuilt into
        the thread, then its records were removed by message ID — leaving
        its text in the thread with nothing left to clean it up."""
        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "gone@example.com", body="DELETED_BODY_MARKER")
        thread_id = _index(orig_path, db, threader)
        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "kept@example.com",
            in_reply_to="gone@example.com",
            subject="Re: Test message",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply_path, db, threader)
        trashed = maildir / "1700000000.M1.host:2,ST"
        orig_path.rename(trashed)
        reconciler.sweep()

        renamed = maildir / "1700000000.M1.host:2,RST"
        real_get = db.get_thread_messages

        def rename_then_read(tid):
            if trashed.exists():
                trashed.rename(renamed)
                _handle_moved(reconciler, str(trashed), str(renamed))
            return real_get(tid)

        monkeypatch.setattr(db, "get_thread_messages", rename_then_read)
        assert reconciler.reap()["threads_rebuilt"] == 1

        row = db._conn.execute(
            "SELECT message_ids, body_text FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        assert "gone@example.com" not in row["message_ids"]
        assert "DELETED_BODY_MARKER" not in row["body_text"]
        assert [r["message_id"] for r in real_get(thread_id)] == ["kept@example.com"]

    def test_skips_reap_when_survivor_unreadable(self, db, threader, embedder, reconciler, maildir):
        # If a survivor's file is transiently unreadable when the
        # reconciler reparses it (mbsync chmod race, perms regression),
        # the reaper must skip the pass cleanly — not crash.
        # ``parse_email`` propagates the ``OSError``, which the reaper
        # catches.
        import os

        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "u1@example.com")
        thread_id = _index(orig_path, db, threader)

        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "u2@example.com",
            in_reply_to="u1@example.com",
            subject="Re: Test message",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply_path, db, threader)

        trashed = maildir / "1700000000.M1.host:2,ST"
        orig_path.rename(trashed)
        reconciler.sweep()

        os.chmod(reply_path, 0o000)
        try:
            result = reconciler.reap()
        finally:
            os.chmod(reply_path, 0o644)

        assert result["threads_rebuilt"] == 0
        assert db.get_thread(thread_id) is not None
        assert db.has_pending_deletion(str(trashed))

    def test_blocked_threads_count_surfaces_in_reap_return(
        self, db, threader, embedder, reconciler, maildir, monkeypatch
    ):
        # Deterministic parse failures on a survivor retry forever under
        # the reaper's ``except Exception`` catch. ``reap()`` reports
        # ``blocked_threads``, which the periodic reconcile log line
        # carries, so a stuck thread shows as a count on every pass and
        # not only as per-thread WARN/ERROR lines.
        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "bk1@example.com")
        thread_id = _index(orig_path, db, threader)

        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "bk2@example.com",
            in_reply_to="bk1@example.com",
            subject="Re: Test message",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply_path, db, threader)

        trashed = maildir / "1700000000.M1.host:2,ST"
        orig_path.rename(trashed)
        reconciler.sweep()

        # Force a deterministic content-pathology failure on every
        # ``parse_email`` call so the reaper hits the broad
        # ``except Exception`` branch.
        def _raise_value_error(path, maildir_root=None):
            raise ValueError("malformed MIME boundary")

        monkeypatch.setattr("src.reconciler.parse_email", _raise_value_error)

        result_1 = reconciler.reap()
        assert result_1["threads_rebuilt"] == 0
        assert result_1["blocked_threads"] == 1
        assert reconciler._blocked_thread_attempts[thread_id] == 1

        # Counter must INCREMENT on each subsequent stuck pass so an
        # operator can see whether the failure is fresh or chronic.
        result_2 = reconciler.reap()
        assert result_2["blocked_threads"] == 1  # still one stuck thread
        assert reconciler._blocked_thread_attempts[thread_id] == 2

        # And a successful reap (failure resolved) must CLEAR the
        # counter so a one-time blip doesn't permanently linger.
        monkeypatch.undo()
        result_3 = reconciler.reap()
        assert result_3["threads_rebuilt"] == 1
        assert result_3["blocked_threads"] == 0
        assert thread_id not in reconciler._blocked_thread_attempts

    def test_escalation_warning_fires_once_at_threshold(
        self, db, threader, embedder, reconciler, maildir, monkeypatch, caplog
    ):
        # A thread blocked for ``_BLOCKED_ESCALATION_THRESHOLD``
        # consecutive passes earns a one-shot, higher-signal WARN so
        # operators can distinguish "embedder is cold-starting" from
        # "embedder is misconfigured and has been failing all day".
        # The latch ensures the message fires exactly once per stuck
        # episode rather than every pass.
        from src.reconciler import _BLOCKED_ESCALATION_THRESHOLD

        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "esc1@example.com")
        thread_id = _index(orig_path, db, threader)

        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "esc2@example.com",
            in_reply_to="esc1@example.com",
            subject="Re: Test message",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply_path, db, threader)

        trashed = maildir / "1700000000.M1.host:2,ST"
        orig_path.rename(trashed)
        reconciler.sweep()

        def _raise_value_error(path, maildir_root=None):
            raise ValueError("malformed MIME boundary")

        monkeypatch.setattr("src.reconciler.parse_email", _raise_value_error)

        # Below-threshold passes do not emit the escalation line.
        with caplog.at_level("WARNING", logger="indexer.reconciler"):
            for _ in range(_BLOCKED_ESCALATION_THRESHOLD - 1):
                reconciler.reap()
        assert not any("no longer a transient retry" in r.message for r in caplog.records)

        # The pass that crosses the threshold fires the escalation
        # exactly once, even though the underlying failure persists.
        caplog.clear()
        with caplog.at_level("WARNING", logger="indexer.reconciler"):
            reconciler.reap()
            reconciler.reap()
            reconciler.reap()
        escalation_lines = [r for r in caplog.records if "no longer a transient retry" in r.message]
        assert len(escalation_lines) == 1
        # The thread ID is the root Message-ID (sender domain); not logged
        # (#257). A survivor's mbsync path identifies the thread instead.
        assert thread_id not in escalation_lines[0].message
        assert str(reply_path) in escalation_lines[0].message
        assert thread_id in reconciler._escalated_threads

    def test_escalation_latch_resets_after_successful_reap(
        self, db, threader, embedder, reconciler, maildir, monkeypatch, caplog
    ):
        # Once a thread reaps cleanly the escalation latch clears, so
        # a future stuck episode on the same thread can re-emit the
        # warning instead of being silently swallowed by a stale set
        # entry.
        from src.reconciler import _BLOCKED_ESCALATION_THRESHOLD

        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "esc1@example.com")
        thread_id = _index(orig_path, db, threader)

        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "esc2@example.com",
            in_reply_to="esc1@example.com",
            subject="Re: Test message",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply_path, db, threader)

        trashed = maildir / "1700000000.M1.host:2,ST"
        orig_path.rename(trashed)
        reconciler.sweep()

        def _raise_value_error(path, maildir_root=None):
            raise ValueError("malformed MIME boundary")

        monkeypatch.setattr("src.reconciler.parse_email", _raise_value_error)
        for _ in range(_BLOCKED_ESCALATION_THRESHOLD):
            reconciler.reap()
        assert thread_id in reconciler._escalated_threads

        # Successful reap clears both the counter and the latch.
        monkeypatch.undo()
        result = reconciler.reap()
        assert result["threads_rebuilt"] == 1
        assert thread_id not in reconciler._blocked_thread_attempts
        assert thread_id not in reconciler._escalated_threads

    def test_skips_reap_when_survivor_oversized(
        self, db, threader, embedder, reconciler, maildir, monkeypatch
    ):
        # ``parse_email`` raises ``OversizedMessageError`` when the file
        # exceeds ``INDEXER_PARSE_MAX_BYTES``. That exception is not an
        # ``OSError`` subclass; the reap path catches it with
        # ``OSError`` as a foreseeable failure, so an oversized survivor
        # skips the pass and the thread stays, without stalling every
        # other thread's tombstones.
        from src.parser import OversizedMessageError

        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "ov1@example.com")
        thread_id = _index(orig_path, db, threader)

        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "ov2@example.com",
            in_reply_to="ov1@example.com",
            subject="Re: Test message",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply_path, db, threader)

        trashed = maildir / "1700000000.M1.host:2,ST"
        orig_path.rename(trashed)
        reconciler.sweep()

        def _raise_oversized(path, maildir_root=None):
            raise OversizedMessageError(path, size=99, cap=10)

        monkeypatch.setattr("src.reconciler.parse_email", _raise_oversized)

        result = reconciler.reap()

        assert result["threads_rebuilt"] == 0
        assert db.get_thread(thread_id) is not None
        assert db.has_pending_deletion(str(trashed))

    def test_skips_reap_on_content_pathology_in_survivor(
        self, db, threader, embedder, reconciler, maildir, monkeypatch
    ):
        # ``parse_email`` deliberately propagates content-pathology
        # exceptions (malformed MIME the email module cannot decompose,
        # html2text blowups, etc.) so the indexer worker can route them
        # through the queue's retry + dead-letter cascade. The reaper has
        # no equivalent: an uncaught one would crash ``reap()`` and stall
        # every other thread's tombstones, so the reaper logs and skips
        # the pass.
        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "cp1@example.com")
        thread_id = _index(orig_path, db, threader)

        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "cp2@example.com",
            in_reply_to="cp1@example.com",
            subject="Re: Test message",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply_path, db, threader)

        trashed = maildir / "1700000000.M1.host:2,ST"
        orig_path.rename(trashed)
        reconciler.sweep()

        def _raise_value_error(path, maildir_root=None):
            raise ValueError("malformed MIME boundary")

        monkeypatch.setattr("src.reconciler.parse_email", _raise_value_error)

        result = reconciler.reap()

        assert result["threads_rebuilt"] == 0
        assert db.get_thread(thread_id) is not None
        assert db.has_pending_deletion(str(trashed))

    @pytest.mark.parametrize(
        "exc", [ValueError("SYNTHETIC_REAP_MARKER"), OSError("SYNTHETIC_REAP_MARKER")]
    )
    def test_reap_parse_failure_log_keeps_mail_out(
        self, db, threader, reconciler, maildir, monkeypatch, caplog, exc
    ):
        """#257: a parser exception can quote the header or body it
        rejected, so the reaper logs its type, never its text or a
        traceback."""
        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "rp1@example.com")
        _index(orig_path, db, threader)
        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "rp2@example.com",
            in_reply_to="rp1@example.com",
            subject="Re: Test message",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply_path, db, threader)
        orig_path.rename(maildir / "1700000000.M1.host:2,ST")
        reconciler.sweep()

        def _raise(path, maildir_root=None):
            raise exc

        monkeypatch.setattr("src.reconciler.parse_email", _raise)
        with caplog.at_level(logging.DEBUG):
            assert reconciler.reap()["threads_rebuilt"] == 0
        assert "SYNTHETIC_REAP_MARKER" not in caplog.text
        # The thread ID is the root Message-ID, which carries the
        # sender's domain: reaper lines name the survivor file instead.
        assert "rp1@example.com" not in caplog.text
        assert type(exc).__name__ in caplog.text
        assert all(r.exc_info is None for r in caplog.records)

    def test_reap_keeps_the_eml_on_disk(self, db, threader, reconciler, maildir):
        """#721: the indexer never deletes Maildir files. Neither a
        whole-thread reap nor a partial rebuild removes a reaped
        message's .eml; mbsync owns the files (#728)."""
        alone = maildir / "1700000000.M1.host:2,S"
        _write_eml(alone, "alone@example.com", subject="Alone")
        _index(alone, db, threader)
        orig = maildir / "1700000001.M2.host:2,S"
        _write_eml(orig, "orig@example.com", subject="Budget")
        _index(orig, db, threader)
        reply = maildir / "1700000002.M3.host:2,S"
        _write_eml(
            reply,
            "reply@example.com",
            subject="Re: Budget",
            in_reply_to="orig@example.com",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply, db, threader)
        alone_trashed = maildir / "1700000000.M1.host:2,ST"
        alone.rename(alone_trashed)
        orig_trashed = maildir / "1700000001.M2.host:2,ST"
        orig.rename(orig_trashed)
        reconciler.sweep()

        result = reconciler.reap()

        assert result["threads_reaped"] == 1
        assert result["threads_rebuilt"] == 1
        assert db.find_active_occurrence(str(alone_trashed)) is None
        assert db.find_active_occurrence(str(orig_trashed)) is None
        assert alone_trashed.exists()
        assert orig_trashed.exists()
        assert reply.exists()

    def test_reap_chunkless_fallback_uses_survivor_subject(
        self, db, threader, embedder, reconciler, maildir
    ):
        # When a reap leaves only survivors whose bodies are all
        # chunk-less (blank body, only-quoted body that strips to empty),
        # the rebuilt thread vector falls back to embedding a subject.
        # That fallback MUST source from the oldest SURVIVING message's
        # subject — not from ``rebuilt_thread.subject`` (the threader's
        # normalized grouping key: lowercased, ``Re:``/``Fwd:`` stripped)
        # and not from ``display_subject`` (which references the
        # OLDEST-EVER message including reaped ones, embedding text
        # from content the user just deleted).
        original_subject = "Quarterly Review Schedule"
        reply_subject = f"Re: {original_subject}"

        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "ds1@example.com", subject=original_subject, body="")
        thread_id = _index(orig_path, db, threader)

        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "ds2@example.com",
            subject=reply_subject,
            body="",
            in_reply_to="ds1@example.com",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply_path, db, threader)

        # Reaping the original leaves the reply as the sole survivor —
        # blank body, no chunks, so the reap path takes the fallback
        # branch in ``_reap_thread``.
        trashed = maildir / "1700000000.M1.host:2,ST"
        orig_path.rename(trashed)
        reconciler.sweep()

        embedder.calls = 0
        # FakeEmbedder records call count but not args; intercept embed
        # to capture the actual text the reaper sent.
        embedded: list[str] = []
        original_embed = embedder.embed

        def _capture(text: str) -> list[float]:
            embedded.append(text)
            return original_embed(text)

        embedder.embed = _capture

        result = reconciler.reap()
        assert result["threads_rebuilt"] == 1
        assert db.get_thread(thread_id) is not None

        # The reaper must have embedded the SURVIVOR's original-case
        # subject (``Re: Quarterly Review Schedule``). Embedding from
        # the deleted message is wrong: that text is no longer part of
        # the index, and using it as the vector key for the rebuilt
        # thread is semantically odd. The normalized grouping key
        # (lowercased, prefix-stripped) would also be the regression.
        assert reply_subject in embedded, (
            f"reap fallback must embed the SURVIVOR's original-case "
            f"subject (Re-prefix intact); embedded: {embedded!r}"
        )
        normalized = original_subject.lower()
        assert all(text != normalized for text in embedded), (
            f"reap fallback must not embed the normalized grouping key "
            f"({normalized!r}); embedded: {embedded!r}"
        )

    def test_reap_chunkless_fallback_prefers_survivor_over_deleted_display_subject(
        self, db, threader, embedder, reconciler, maildir
    ):
        # ``display_subject`` is maintained as the OLDEST-ever message's
        # subject across the lifetime of a thread, including reaped
        # messages. After reaping the original, the survivor may have a
        # substantively different subject — but ``display_subject`` in
        # the DB still references the deleted message. The fallback
        # must prefer the survivor's subject so the rebuild's vector
        # key is text that still exists in the index. Embedding from
        # the deleted message's subject would tie the post-reap thread
        # vector to content the user explicitly removed.
        deleted_subject = "Confidential salary discussion"
        survivor_subject = "Re: Public Q1 results"

        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "del1@example.com", subject=deleted_subject, body="")
        thread_id = _index(orig_path, db, threader)

        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "del2@example.com",
            subject=survivor_subject,
            body="",
            in_reply_to="del1@example.com",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply_path, db, threader)

        # Sanity check: ``display_subject`` was set to the deleted
        # message's subject — so a fallback that reads display_subject
        # FIRST would expose the deleted text in the post-reap vector.
        assert db.get_thread_display_subject(thread_id) == deleted_subject

        trashed = maildir / "1700000000.M1.host:2,ST"
        orig_path.rename(trashed)
        reconciler.sweep()

        embedded: list[str] = []
        original_embed = embedder.embed

        def _capture(text: str) -> list[float]:
            embedded.append(text)
            return original_embed(text)

        embedder.embed = _capture

        result = reconciler.reap()
        assert result["threads_rebuilt"] == 1
        # The survivor's subject must be what the rebuild embeds.
        assert survivor_subject in embedded, (
            f"reap fallback must use survivor's subject, not the "
            f"deleted message's; embedded: {embedded!r}"
        )
        # The deleted subject text must NOT appear as an embed input.
        assert deleted_subject not in embedded, (
            f"reap fallback must not embed the deleted message's "
            f"subject text ({deleted_subject!r}); embedded: {embedded!r}"
        )

    def test_reap_chunkless_fallback_skips_blank_oldest_survivor(
        self, db, threader, embedder, reconciler, maildir
    ):
        # Several survivors, the oldest with an empty subject and a later
        # one with a subject. The fallback must take the later survivor's
        # subject rather than drop to the stored ``display_subject``,
        # which still holds the deleted message's subject.
        deleted_subject = "SYNTHETIC_DELETED_SUBJECT"
        live_subject = "SYNTHETIC_LIVE_SUBJECT"

        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "ms1@example.com", subject=deleted_subject, body="")
        thread_id = _index(orig_path, db, threader)

        blank_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            blank_path,
            "ms2@example.com",
            subject="",
            body="",
            in_reply_to="ms1@example.com",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(blank_path, db, threader)

        live_path = maildir / "1700000002.M3.host:2,S"
        _write_eml(
            live_path,
            "ms3@example.com",
            subject=live_subject,
            body="",
            in_reply_to="ms2@example.com",
            date=datetime(2024, 3, 1, tzinfo=UTC),
        )
        _index(live_path, db, threader)
        assert db.get_thread_display_subject(thread_id) == deleted_subject

        orig_path.rename(maildir / "1700000000.M1.host:2,ST")
        reconciler.sweep()

        embedded: list[str] = []
        original_embed = embedder.embed

        def _capture(text: str) -> list[float]:
            embedded.append(text)
            return original_embed(text)

        embedder.embed = _capture

        result = reconciler.reap()
        assert result["threads_rebuilt"] == 1
        assert embedded == [live_subject]
        assert all(deleted_subject not in text for text in embedded)

    def test_reap_chunkless_fallback_uses_sentinel_when_no_subject_anywhere(
        self, db, threader, embedder, reconciler, maildir
    ):
        # No survivor and no stored display subject: the sentinel is
        # embedded.
        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "ns1@example.com", subject="", body="")
        thread_id = _index(orig_path, db, threader)

        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "ns2@example.com",
            subject="",
            body="",
            in_reply_to="ns1@example.com",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply_path, db, threader)
        assert db.get_thread_display_subject(thread_id) is None

        orig_path.rename(maildir / "1700000000.M1.host:2,ST")
        reconciler.sweep()

        embedded: list[str] = []
        original_embed = embedder.embed

        def _capture(text: str) -> list[float]:
            embedded.append(text)
            return original_embed(text)

        embedder.embed = _capture

        result = reconciler.reap()
        assert result["threads_rebuilt"] == 1
        assert embedded == ["(empty thread)"]

    def test_reap_chunkless_fallback_ignores_stored_subject_when_survivors_blank(
        self, db, threader, embedder, reconciler, maildir
    ):
        # Every survivor's subject is empty, so a non-empty stored
        # ``display_subject`` can only come from the deleted message.
        # The fallback must go straight to the sentinel.
        deleted_subject = "SYNTHETIC_DELETED_SUBJECT"

        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "bs1@example.com", subject=deleted_subject, body="")
        thread_id = _index(orig_path, db, threader)

        for i, day in ((2, 1), (3, 2)):
            path = maildir / f"170000000{i - 1}.M{i}.host:2,S"
            _write_eml(
                path,
                f"bs{i}@example.com",
                subject="",
                body="",
                in_reply_to=f"bs{i - 1}@example.com",
                date=datetime(2024, 2, day, tzinfo=UTC),
            )
            _index(path, db, threader)
        assert db.get_thread_display_subject(thread_id) == deleted_subject

        orig_path.rename(maildir / "1700000000.M1.host:2,ST")
        reconciler.sweep()

        embedded: list[str] = []
        original_embed = embedder.embed

        def _capture(text: str) -> list[float]:
            embedded.append(text)
            return original_embed(text)

        embedder.embed = _capture

        result = reconciler.reap()
        assert result["threads_rebuilt"] == 1
        assert embedded == ["(empty thread)"]
        assert all(deleted_subject not in text for text in embedded)

    def test_reap_drops_chunks_for_reaped_messages_and_keeps_survivor_chunks(
        self, db, threader, embedder, reconciler, maildir
    ):
        """Chunk cascade: when the reaper rebuilds a thread, the reaped
        message's per-message chunks must be removed (across the chunks
        table, the FTS shadow tables, and the vec table), while the
        survivor's chunks must remain so the rebuilt thread vector can
        derive from them.
        """
        from src.chunker import MessageChunk

        # Index two messages with body content; manually write per-message
        # chunks for each so the reap-time mean-of-survivors path has
        # something to consume. ``_index`` here goes through the legacy
        # direct-upsert path (no chunker) so we add the chunks ourselves.
        orig_path = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig_path, "co1@example.com", subject="Chunked", body="orig")
        thread_id = _index(orig_path, db, threader)

        reply_path = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply_path,
            "co2@example.com",
            subject="Re: Chunked",
            body="reply",
            in_reply_to="co1@example.com",
            date=datetime(2024, 2, 1, 12, 0, tzinfo=UTC),
        )
        _index(reply_path, db, threader)
        from src.parser import parse_email

        orig_msg, reply_msg = parse_email(orig_path), parse_email(reply_path)
        assert orig_msg is not None and reply_msg is not None
        co1, co2 = orig_msg.claimant_id, reply_msg.claimant_id

        orig_chunk = MessageChunk(
            chunk_id="orig-chunk".ljust(64, "0"),
            chunk_index=0,
            text="original chunk text",
            char_start=0,
            char_end=20,
            token_est=5,
        )
        reply_chunk = MessageChunk(
            chunk_id="reply-chunk".ljust(64, "0"),
            chunk_index=0,
            text="reply chunk text",
            char_start=0,
            char_end=20,
            token_est=5,
        )
        db.replace_message_chunks(
            claimant_id=co1,
            thread_id=thread_id,
            chunks=[orig_chunk],
            embeddings_by_chunk_id={orig_chunk.chunk_id: [0.1] * EMBEDDING_DIM},
        )
        db.replace_message_chunks(
            claimant_id=co2,
            thread_id=thread_id,
            chunks=[reply_chunk],
            embeddings_by_chunk_id={reply_chunk.chunk_id: [0.2] * EMBEDDING_DIM},
        )

        # Tombstone the original; reply survives.
        orig_trashed = maildir / "1700000000.M1.host:2,ST"
        orig_path.rename(orig_trashed)
        reconciler.sweep()

        embed_calls_before = embedder.calls
        thread_reads: list[str] = []
        real_compute = Database._compute_thread_sums

        def counted(db_self, tid):
            thread_reads.append(tid)
            return real_compute(db_self, tid)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Database, "_compute_thread_sums", counted)
            result = reconciler.reap()
        assert result["threads_rebuilt"] == 1
        # The survivors' mean comes from the thread's chunk-vector sum,
        # with no thread-wide read (#1356): the survivor's vector alone.
        assert thread_reads == []
        stored = db._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0]
        assert struct.unpack(f"{EMBEDDING_DIM}f", stored)[:2] == pytest.approx(
            (EMBEDDING_DIM**-0.5,) * 2
        )
        assert (
            db._conn.execute(
                "SELECT count FROM thread_vector_sums WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
            == 1
        )

        # Reaped message's chunk is gone from all three indexes.
        assert db.get_chunk_ids_for_message(co1) == set()
        orig_vec = db._conn.execute(
            "SELECT COUNT(*) FROM message_chunks_vec WHERE chunk_id = ?", (orig_chunk.chunk_id,)
        ).fetchone()[0]
        assert orig_vec == 0

        # Survivor's chunk is preserved.
        assert db.get_chunk_ids_for_message(co2) == {reply_chunk.chunk_id}

        # Embedder was NOT called for the rebuilt thread vector — the
        # survivor had a chunk embedding to mean over, so the reap path
        # took the chunk-aware branch instead of the subject-fallback
        # embed.
        assert embedder.calls == embed_calls_before


# ---------------------------------------------------------------------------
# Mass-delete brake
# ---------------------------------------------------------------------------


class TestMassDeleteBrake:
    def _stage_batch(self, maildir, db, threader, count: int) -> list[Path]:
        paths = []
        for i in range(count):
            p = maildir / f"1700000{i:04d}.M1.host:2,S"
            _write_eml(p, f"mass{i}@example.com", subject=f"Subject {i}")
            _index(p, db, threader)
            paths.append(p)
        return paths

    def test_aborts_when_tombstones_exceed_threshold(self, db, threader, embedder, maildir):
        # With 30 messages the 10-message absolute floor is the binding
        # limit: max_allowed here is ``max(10, int(30 * 0.05)) = 10``, so
        # 20 tombstones trip the brake.
        paths = self._stage_batch(maildir, db, threader, 30)
        for p in paths[:20]:
            t = p.with_name(p.name + "T")
            p.rename(t)

        cfg = _default_config(grace_days=0, max_batch_pct=0.05)
        rec = Reconciler(db, embedder, cfg, maildir_root=db.account.maildir_root)
        rec.sweep()

        # Age all tombstones so they are past the grace window
        db._conn.execute("UPDATE pending_deletions SET marked_at = '2000-01-01T00:00:00+00:00'")
        db._conn.commit()

        result = rec.reap()
        assert result["aborted"] is True
        # Index was not touched
        assert db.count_total_messages() == 30
        assert count_pending_deletions(db) == 20

    def test_small_mailbox_floor_permits_routine_cleanup(self, db, threader, embedder, maildir):
        """Regression: without an absolute floor a 10-message mailbox
        would trip the 5% brake on a single deletion (10%). The brake
        exists to catch Bridge-outage-induced mass tombstoning, not to
        block routine cleanup on small mailboxes — so the first 10
        tombstones per pass are always allowed."""
        paths = self._stage_batch(maildir, db, threader, 10)
        for p in paths[:6]:
            t = p.with_name(p.name + "T")
            p.rename(t)

        cfg = _default_config(grace_days=0, max_batch_pct=0.05)
        rec = Reconciler(db, embedder, cfg, maildir_root=db.account.maildir_root)
        rec.sweep()
        db._conn.execute("UPDATE pending_deletions SET marked_at = '2000-01-01T00:00:00+00:00'")
        db._conn.commit()

        result = rec.reap()
        assert result["aborted"] is False
        assert db.count_total_messages() == 4  # 10 - 6 reaped

    def test_stale_live_tombstones_do_not_trip_the_brake(self, db, threader, embedder, maildir):
        """#336: tombstones whose message maps to a live,
        untrashed file are stale and cleared by the reaper, but they were
        counted against the brake first. Eleven orphans in a small mailbox
        aborted every pass, and no sweep revisits their dead paths, so the
        brake blocked legitimate deletions forever."""
        paths = self._stage_batch(maildir, db, threader, 12)
        for p in paths[:11]:
            entry = db.find_active_occurrence(str(p))
            db._conn.execute(
                "INSERT INTO pending_deletions (occurrence_id, claimant_id, marked_at) "
                "VALUES (?, ?, '2000-01-01T00:00:00+00:00')",
                (entry["id"], entry["claimant_id"]),
            )
        db._conn.commit()
        trashed = paths[11].with_name(paths[11].name + "T")
        paths[11].rename(trashed)

        cfg = _default_config(grace_days=0, max_batch_pct=0.05)
        rec = Reconciler(db, embedder, cfg, maildir_root=db.account.maildir_root)
        rec.sweep()

        result = rec.reap()
        assert result["aborted"] is False
        assert result["threads_reaped"] == 1
        assert db.count_total_messages() == 11
        assert count_pending_deletions(db) == 0

    def test_force_overrides_brake(self, db, threader, embedder, maildir):
        paths = self._stage_batch(maildir, db, threader, 30)
        for p in paths[:20]:
            t = p.with_name(p.name + "T")
            p.rename(t)

        cfg = _default_config(grace_days=0, max_batch_pct=0.05, force=True)
        rec = Reconciler(db, embedder, cfg, maildir_root=db.account.maildir_root)
        rec.sweep()
        db._conn.execute("UPDATE pending_deletions SET marked_at = '2000-01-01T00:00:00+00:00'")
        db._conn.commit()

        result = rec.reap()
        assert result["aborted"] is False
        assert db.count_total_messages() == 10  # 30 - 20 reaped


# ---------------------------------------------------------------------------
# load_config_from_env
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_defaults_to_mirror(self):
        """Mirror is the shipped default (owner decision 2026-10-01):
        with no variable set, upstream deletions are reconciled."""
        cfg = load_config_from_env({})
        assert cfg.enabled is True
        assert cfg.grace_days == 7
        assert cfg.sweep_interval_secs == 3600
        assert cfg.max_batch_pct == pytest.approx(0.05)
        assert cfg.force is False
        assert not hasattr(cfg, "unlink_on_reap")

    def test_enabled_parses_truthy_values(self):
        for val in ("1", "true", "TRUE", "yes", "on"):
            cfg = load_config_from_env({"INDEXER_DELETION_ENABLED": val})
            assert cfg.enabled is True, val

    def test_empty_value_keeps_mirror_default(self):
        cfg = load_config_from_env({"INDEXER_DELETION_ENABLED": "  "})
        assert cfg.enabled is True

    def test_archive_opt_out_parses_falsy_values(self):
        for val in ("0", "false", "FALSE", "no", "off", " false "):
            cfg = load_config_from_env({"INDEXER_DELETION_ENABLED": val})
            assert cfg.enabled is False, val

    def test_unknown_enabled_value_fails_closed(self):
        """A typo must not silently pick a retention mode."""
        with pytest.raises(ValueError, match="INDEXER_DELETION_ENABLED"):
            load_config_from_env({"INDEXER_DELETION_ENABLED": "archive-please"})

    def test_empty_values_yield_defaults(self):
        """Unset and empty both mean "use the documented default" (#481)."""
        cfg = load_config_from_env(
            {
                "INDEXER_DELETION_GRACE_DAYS": "",
                "INDEXER_DELETION_SWEEP_INTERVAL_SECS": " ",
                "INDEXER_DELETION_MAX_BATCH_PCT": "",
            }
        )
        assert cfg.grace_days == 7
        assert cfg.sweep_interval_secs == 3600
        assert cfg.max_batch_pct == pytest.approx(0.05)
        assert cfg.force is False

    def test_valid_values_are_returned(self):
        cfg = load_config_from_env(
            {
                "INDEXER_DELETION_GRACE_DAYS": "0",
                "INDEXER_DELETION_SWEEP_INTERVAL_SECS": "60",
                "INDEXER_DELETION_MAX_BATCH_PCT": "1",
            }
        )
        assert cfg.grace_days == 0
        assert cfg.sweep_interval_secs == 60
        assert cfg.max_batch_pct == 1.0
        cfg = load_config_from_env({"INDEXER_DELETION_MAX_BATCH_PCT": "0"})
        assert cfg.max_batch_pct == 0.0

    @pytest.mark.parametrize("raw", ["true", "YES", "1", "tru"])
    def test_the_removed_force_setting_never_disables_the_brake(self, raw):
        """Forcing is the one-run ``make force-deletion`` command (#1609):
        no environment value turns the brake off for the service, and the
        removed setting is not read (``scripts/validate-env.sh`` refuses
        it before startup)."""
        cfg = load_config_from_env({"INDEXER_DELETION_FORCE": raw})
        assert cfg.force is False

    @pytest.mark.parametrize(
        ("name", "raw", "message"),
        [
            ("INDEXER_DELETION_GRACE_DAYS", "not-a-number", "integer"),
            ("INDEXER_DELETION_GRACE_DAYS", "tru", "integer"),
            ("INDEXER_DELETION_GRACE_DAYS", "nan", "integer"),
            ("INDEXER_DELETION_GRACE_DAYS", "inf", "integer"),
            ("INDEXER_DELETION_GRACE_DAYS", "-5", ">= 0"),
            ("INDEXER_DELETION_SWEEP_INTERVAL_SECS", "-5", ">= 60"),
            ("INDEXER_DELETION_SWEEP_INTERVAL_SECS", "5", ">= 60"),
        ],
    )
    def test_invalid_integer_fails(self, name, raw, message):
        """No silent fallback to the default and no clamp to the minimum."""
        with pytest.raises(ValueError, match=f"{name}.*{message}"):
            load_config_from_env({name: raw})

    @pytest.mark.parametrize(
        "raw", ["tru", "nan", "NaN", "inf", "-inf", "-5", "-0.1", "2.5", "1.0001"]
    )
    def test_invalid_max_batch_pct_fails(self, raw):
        """NaN would pass both range checks and then raise inside every
        reap sweep, so reaping would silently never run (#481);
        out-of-range values are rejected, not clamped."""
        with pytest.raises(ValueError, match="INDEXER_DELETION_MAX_BATCH_PCT.*between 0 and 1"):
            load_config_from_env({"INDEXER_DELETION_MAX_BATCH_PCT": raw})


# ---------------------------------------------------------------------------
# Reaped-source records (PLAN Phase 4 item 4)
# ---------------------------------------------------------------------------


def _reaped_rows(db: Database) -> list[dict]:
    return [
        dict(r)
        for r in db._conn.execute(
            "SELECT claimant_id, message_id, thread_id, reaped_at FROM reaped_messages "
            "ORDER BY claimant_id"
        )
    ]


class TestReapedMessageRecords:
    """A reap leaves an identifier-only record per message, so a later
    lookup of a cited claimant ID or thread ID can say the source was
    reaped rather than that it never existed."""

    def test_full_reap_records_each_reaped_message(self, db, threader, reconciler, maildir):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "full@example.com")
        thread_id = _index(path, db, threader)
        claimant = db.get_thread_messages(thread_id)[0]["claimant_id"]
        path.rename(maildir / "1700000000.M1.host:2,ST")
        before = datetime.now(UTC).isoformat()

        reconciler.sweep()
        assert reconciler.reap()["threads_reaped"] == 1

        rows = _reaped_rows(db)
        assert [(r["claimant_id"], r["thread_id"]) for r in rows] == [(claimant, thread_id)]
        assert claimant.startswith(rows[0]["message_id"])
        assert before <= rows[0]["reaped_at"] <= datetime.now(UTC).isoformat()

    def test_partial_reap_records_only_the_reaped_message(self, db, threader, reconciler, maildir):
        orig = maildir / "1700000000.M1.host:2,S"
        _write_eml(orig, "orig@example.com", subject="Budget")
        thread_id = _index(orig, db, threader)
        reply = maildir / "1700000001.M2.host:2,S"
        _write_eml(
            reply,
            "reply@example.com",
            subject="Re: Budget",
            in_reply_to="orig@example.com",
            date=datetime(2024, 2, 1, 12, 0, tzinfo=UTC),
        )
        _index(reply, db, threader)
        by_path = {r["filepath"]: r["claimant_id"] for r in db.get_thread_messages(thread_id)}
        orig.rename(maildir / "1700000000.M1.host:2,ST")

        reconciler.sweep()
        assert reconciler.reap()["threads_rebuilt"] == 1

        rows = _reaped_rows(db)
        assert [(r["claimant_id"], r["thread_id"]) for r in rows] == [
            (by_path[str(orig)], thread_id)
        ]

    def test_skipped_reap_records_nothing(self, db, threader, reconciler, maildir, monkeypatch):
        """A reap the transaction re-check abandons (the message was
        restored after the snapshot) must not leave a removed record."""
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "restored@example.com")
        _index(path, db, threader)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)
        reconciler.sweep()
        snapshot = db.reapable_messages(datetime.now(UTC).isoformat())
        trashed.rename(path)
        db.update_filepath(
            str(trashed), str(path), move_occurrence=True, verified_identity=file_identity(path)
        )
        monkeypatch.setattr(db, "reapable_messages", lambda _cutoff: snapshot)

        assert reconciler.reap()["threads_reaped"] == 0
        assert _reaped_rows(db) == []

    def test_records_hold_identifiers_only(self, db):
        cols = {r["name"] for r in db._conn.execute("PRAGMA table_info(reaped_messages)")}
        assert cols == {"claimant_id", "message_id", "thread_id", "reaped_at"}


class TestPruneReapedMessages:
    def _seed(self, db, claimant_id: str, reaped_at: datetime) -> None:
        with db.transaction():
            db._conn.execute(
                "INSERT INTO reaped_messages (claimant_id, message_id, thread_id, reaped_at) "
                "VALUES (?, ?, ?, ?)",
                (claimant_id, claimant_id.split("#")[0], "t", reaped_at.isoformat()),
            )

    def test_prunes_records_past_the_retention_window(self, db):
        from src.database import REAPED_RECORD_RETENTION_DAYS

        now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
        window = timedelta(days=REAPED_RECORD_RETENTION_DAYS)
        self._seed(db, "old@example.com#00000001", now - window - timedelta(seconds=1))
        self._seed(db, "edge@example.com#00000002", now - window + timedelta(seconds=1))
        self._seed(db, "new@example.com#00000003", now)

        assert db.prune_reaped_messages(now=now) == 1

        assert [r["claimant_id"] for r in _reaped_rows(db)] == [
            "edge@example.com#00000002",
            "new@example.com#00000003",
        ]

    def test_retention_is_short(self):
        """Identifiers derive from the sender's Message-ID; the record
        exists to explain a recent citation, not to archive deletions."""
        from src.database import REAPED_RECORD_RETENTION_DAYS

        assert 0 < REAPED_RECORD_RETENTION_DAYS <= 30
