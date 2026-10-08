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
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
from src.database import (
    EMBEDDING_DIM,  # noqa: F401  -- via reuse
    Database,
)
from src.reconciler import Reconciler, ReconcilerConfig, load_config_from_env, sweep_paths
from src.threader import Threader

from tests.conftest import count_pending_deletions

FAKE_EMBEDDING = [0.0] * EMBEDDING_DIM


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
def reconciler(db: Database, embedder: FakeEmbedder) -> Reconciler:
    return Reconciler(db, embedder, _default_config())


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
        assert db.find_message_entry_by_filepath(str(replied)) is not None
        assert db.find_message_entry_by_filepath(str(path)) is None

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

        result = reconciler.sweep()
        assert result["cleared"] == 1
        assert db.has_pending_deletion(str(restored)) is False

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
        assert result["missing"] == 1

    def test_restore_during_sweep_leaves_no_stale_tombstone(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """#301: the sweep resolved the trashed path, the watcher then
        restored the file and cleared its tombstone, and the sweep
        recorded a new tombstone under the obsolete trashed path. Later
        sweeps look only at the live path, so the reaper deleted the
        restored message."""
        import src.reconciler as reconciler_module

        rec = Reconciler(db, embedder, _default_config())
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "restored@example.com")
        thread_id = _index(path, db, threader)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)
        rec.handle_moved(str(path), str(trashed))
        assert db.has_pending_deletion(str(trashed))

        real_resolve = reconciler_module.resolve_current_path

        def resolve_then_restore(stored, listings=None):
            current = real_resolve(stored, listings)
            trashed.rename(path)
            rec.handle_moved(str(trashed), str(path))  # the watcher wins the lock here
            return current

        monkeypatch.setattr(reconciler_module, "resolve_current_path", resolve_then_restore)
        assert rec.sweep()["tombstoned"] == 0
        monkeypatch.setattr(reconciler_module, "resolve_current_path", real_resolve)

        assert db.find_message_entry_by_filepath(str(path)) is not None
        assert count_pending_deletions(db) == 0
        rec.sweep()
        assert rec.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None

    def test_move_during_sweep_does_not_tombstone_the_old_path(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """#301, missing-file branch: the watcher moved the file to another
        folder while the sweep resolved the stale path, which no longer
        exists; the sweep tombstoned the old path as missing and the
        reaper deleted the moved message."""
        import src.reconciler as reconciler_module

        rec = Reconciler(db, embedder, _default_config())
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "moved@example.com")
        thread_id = _index(path, db, threader)
        archived = maildir.parent.parent / "Archive" / "cur" / path.name
        archived.parent.mkdir(parents=True)

        real_resolve = reconciler_module.resolve_current_path

        def move_then_resolve(stored, listings=None):
            path.rename(archived)
            rec.handle_moved(str(path), str(archived), folder="Archive")
            return real_resolve(stored, listings)

        monkeypatch.setattr(reconciler_module, "resolve_current_path", move_then_resolve)
        assert rec.sweep()["missing"] == 0
        monkeypatch.setattr(reconciler_module, "resolve_current_path", real_resolve)

        assert count_pending_deletions(db) == 0
        assert rec.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None


# ---------------------------------------------------------------------------
# Byte-identical copies (#1102)
# ---------------------------------------------------------------------------

_COPY_MARKER = "ZQXCOPYMARKER1102"


def _index_copies(
    db, threader, first: Path, second: Path, message_id: str, subject: str = "Subject"
) -> str:
    """Index ``first`` and a byte-identical copy at ``second``. Both claim
    one claimant ID, so the mapping keeps only ``second``, the latest."""
    _write_eml(first, message_id, subject=f"{subject} {_COPY_MARKER}", body=_COPY_MARKER)
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_bytes(first.read_bytes())
    thread_id = _index(first, db, threader)
    assert _index(second, db, threader) == thread_id
    assert db.find_message_entry_by_filepath(str(second)) is not None
    assert db.is_indexed(str(first))
    return thread_id


def _message_row(db, claimant_id: str):
    return db._conn.execute(
        "SELECT filepath, folder, replied FROM messages WHERE claimant_id = ?", (claimant_id,)
    ).fetchone()


def _restart(db, embedder, maildir_root: Path) -> int:
    """What a restart does: the rename sweep, the Maildir walk (returns
    how many files it queued), then the reconciler sweep and reap."""
    from src import main
    from src.queue import REASON_INITIAL_SCAN, IndexingQueue

    sweep_paths(db)
    queued = main._enqueue_unindexed_messages(
        db, IndexingQueue(db), maildir_root, REASON_INITIAL_SCAN, skip_trashed=True
    )
    rec = Reconciler(db, embedder, _default_config())
    rec.sweep()
    rec.reap()
    return queued


class TestByteIdenticalCopies:
    def test_removing_the_mapped_copy_keeps_the_message_through_the_other(
        self, db, threader, embedder, maildir, caplog
    ):
        """#1102: the sweep only saw the mapped path, tombstoned the
        claimant as missing and the reaper removed it, while the other
        copy stayed marked indexed, so a restart never re-queued it."""
        kept = maildir / "1700000000.M1.host:2,S"
        mapped = maildir / "1700000001.M2.host:2,S"
        thread_id = _index_copies(db, threader, kept, mapped, "copy@example.com")
        claimant = db.find_message_entry_by_filepath(str(mapped))["claimant_id"]
        mapped.unlink()

        rec = Reconciler(db, embedder, _default_config())
        with caplog.at_level(logging.INFO, logger="indexer.reconciler"):
            summary = rec.sweep()
            reaped = rec.reap()

        # A restart queues nothing (the copy is still marked indexed).
        assert _restart(db, embedder, maildir.parent.parent) == 0
        assert db.get_thread(thread_id) is not None
        assert reaped["threads_reaped"] == 0
        assert summary["remapped"] == 1
        assert summary["missing"] == 0
        assert count_pending_deletions(db) == 0
        assert db.find_message_entry_by_filepath(str(kept))["claimant_id"] == claimant
        assert _message_row(db, claimant)["filepath"] == str(kept)
        assert db.is_indexed(str(kept))
        assert not db.is_indexed(str(mapped))
        fts = db._conn.execute(
            "SELECT 1 FROM threads_fts WHERE threads_fts MATCH ?", (_COPY_MARKER,)
        ).fetchall()
        assert fts
        line = next(r for r in caplog.records if "remapped=1" in r.getMessage())
        assert line.levelno == logging.INFO
        assert _COPY_MARKER not in caplog.text
        assert "copy@example.com" not in caplog.text

    def test_removing_both_copies_still_reaps(self, db, threader, embedder, maildir):
        kept = maildir / "1700000000.M1.host:2,S"
        mapped = maildir / "1700000001.M2.host:2,S"
        thread_id = _index_copies(db, threader, kept, mapped, "both@example.com")
        kept.unlink()
        mapped.unlink()

        rec = Reconciler(db, embedder, _default_config())
        summary = rec.sweep()
        reaped = rec.reap()

        assert summary["missing"] == 1
        assert summary.get("remapped", 0) == 0
        assert reaped["threads_reaped"] == 1
        assert db.get_thread(thread_id) is None
        assert db.count_total_messages() == 0

    def test_a_copy_in_another_folder_carries_its_folder(self, db, threader, embedder, maildir):
        kept = maildir.parent.parent / "Archive" / "cur" / "1700000000.M1.host:2,RS"
        mapped = maildir / "1700000001.M2.host:2,S"
        _index_copies(db, threader, kept, mapped, "folder@example.com")
        claimant = db.find_message_entry_by_filepath(str(mapped))["claimant_id"]
        assert _message_row(db, claimant)["folder"] == "INBOX"
        mapped.unlink()

        assert Reconciler(db, embedder, _default_config()).sweep()["remapped"] == 1

        row = _message_row(db, claimant)
        assert row["filepath"] == str(kept)
        assert row["folder"] == "Archive"
        assert row["replied"] == 1

    def test_a_trashed_copy_is_remapped_then_tombstoned_as_today(
        self, db, threader, embedder, maildir
    ):
        """The guard only changes which path the claimant maps to; the
        trash rule then applies to that path unchanged."""
        kept = maildir / "1700000000.M1.host:2,ST"
        mapped = maildir / "1700000001.M2.host:2,S"
        _index_copies(db, threader, kept, mapped, "trashedcopy@example.com")
        mapped.unlink()

        summary = Reconciler(db, embedder, _default_config()).sweep()

        assert summary["remapped"] == 1
        assert summary["tombstoned"] == 1
        assert db.has_pending_deletion(str(kept))

    def test_a_tombstone_written_before_the_remap_is_cleared(self, db, threader, embedder, maildir):
        """A missing-file tombstone written before the guard existed (or
        earlier in the grace window) moves with the remap and is cleared."""
        kept = maildir / "1700000000.M1.host:2,S"
        mapped = maildir / "1700000001.M2.host:2,S"
        thread_id = _index_copies(db, threader, kept, mapped, "earlier@example.com")
        entry = db.find_message_entry_by_filepath(str(mapped))
        db.add_pending_deletion(str(mapped), entry["claimant_id"], entry["thread_id"])
        mapped.unlink()

        rec = Reconciler(db, embedder, _default_config())
        summary = rec.sweep()

        assert summary["remapped"] == 1
        assert summary["cleared"] == 1
        assert count_pending_deletions(db) == 0
        assert rec.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None

    def test_one_candidate_query_per_sweep(self, db, threader, embedder, maildir):
        for n in range(3):
            mapped = maildir / f"170000001{n}.M{n}.host:2,S"
            _index_copies(
                db,
                threader,
                maildir / f"170000000{n}.K{n}.host:2,S",
                mapped,
                f"many{n}@example.com",
            )
            mapped.unlink()
        statements: list[str] = []
        db._conn.set_trace_callback(statements.append)
        try:
            summary = Reconciler(db, embedder, _default_config()).sweep()
        finally:
            db._conn.set_trace_callback(None)

        assert summary["remapped"] == 3
        lookups = [s for s in statements if "FROM indexed_files" in s and "content_hash IN" in s]
        assert len(lookups) == 1, lookups
        # Codex round 1 on #1134: a join on ``content_hash`` (no index)
        # scanned ``indexed_files`` once per missing claimant. The lookup
        # must read ``indexed_files`` once, whatever the count: its plan
        # touches no other table, so nothing drives a per-row rescan.
        plan = [r["detail"] for r in db._conn.execute("EXPLAIN QUERY PLAN " + lookups[0])]
        tables = [d for d in plan if d.startswith(("SCAN", "SEARCH"))]
        assert [d for d in tables if "indexed_files" in d], plan
        assert all("indexed_files" in d or "json_each" in d for d in tables), plan

    def test_no_candidate_query_when_nothing_is_missing(self, db, threader, embedder, maildir):
        kept = maildir / "1700000000.M1.host:2,S"
        mapped = maildir / "1700000001.M2.host:2,S"
        _index_copies(db, threader, kept, mapped, "present@example.com")
        statements: list[str] = []
        db._conn.set_trace_callback(statements.append)
        try:
            summary = Reconciler(db, embedder, _default_config()).sweep()
        finally:
            db._conn.set_trace_callback(None)

        assert summary["remapped"] == 0
        assert not [s for s in statements if "content_hash IN" in s]

    def test_a_live_copy_is_preferred_over_a_trashed_one(self, db, threader, embedder, maildir):
        """Codex round 1 on #1134: the first candidate in path order was
        taken even when trashed, so the message was tombstoned and later
        reaped although a live copy remained."""
        trashed = maildir / "1700000000.A1.host:2,ST"
        live = maildir / "1700000002.Z1.host:2,S"
        mapped = maildir / "1700000001.M2.host:2,S"
        _index_copies(db, threader, trashed, mapped, "prefer@example.com")
        live.write_bytes(mapped.read_bytes())
        _index(live, db, threader)
        _index(mapped, db, threader)  # the mapping is ``mapped`` again
        claimant = db.find_message_entry_by_filepath(str(mapped))["claimant_id"]
        mapped.unlink()

        summary = Reconciler(db, embedder, _default_config()).sweep()

        assert summary["remapped"] == 1
        assert summary["tombstoned"] == 0
        assert _message_row(db, claimant)["filepath"] == str(live)
        assert count_pending_deletions(db) == 0

    def test_remap_onto_a_tombstoned_copy_keeps_one_tombstone(
        self, db, threader, embedder, maildir
    ):
        """Codex round 1 on #1134: both paths held a tombstone, and moving
        the old one onto the copy hit the primary key and aborted the
        whole sweep on every pass."""
        kept = maildir / "1700000000.M1.host:2,ST"
        mapped = maildir / "1700000001.M2.host:2,S"
        _write_eml(kept, "twotombs@example.com")
        _index(kept, db, threader)
        entry = db.find_message_entry_by_filepath(str(kept))
        assert db.add_pending_deletion(str(kept), entry["claimant_id"], entry["thread_id"])
        mapped.write_bytes(kept.read_bytes())
        _index(mapped, db, threader)
        assert db.add_pending_deletion(str(mapped), entry["claimant_id"], entry["thread_id"])
        mapped.unlink()

        summary = Reconciler(db, embedder, _default_config()).sweep()

        assert summary["remapped"] == 1
        assert count_pending_deletions(db) == 1
        assert db.has_pending_deletion(str(kept))
        assert _message_row(db, entry["claimant_id"])["filepath"] == str(kept)

    def test_remap_keeps_the_copys_own_job(self, db, threader, embedder, maildir):
        """Codex round 1 on #1134: the rename's ``UPDATE OR REPLACE``
        moved the gone path's dead job onto the copy, replacing the copy's
        runnable one."""
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        kept = maildir / "1700000000.M1.host:2,S"
        mapped = maildir / "1700000001.M2.host:2,S"
        _index_copies(db, threader, kept, mapped, "jobs@example.com")
        queue = IndexingQueue(db)
        queue.enqueue(str(kept), REASON_INITIAL_SCAN)
        queue.enqueue(str(mapped), REASON_INITIAL_SCAN)
        queue.mark_dead_terminal(str(mapped), stage="embed", error="fixed text")
        mapped.unlink()

        assert Reconciler(db, embedder, _default_config()).sweep()["remapped"] == 1

        assert queue.has_pending_row(str(kept))
        assert not queue.is_dead(str(kept))
        assert not queue.has_pending_row(str(mapped))
        assert not queue.is_dead(str(mapped))

    def test_reaping_with_every_copy_gone_unmarks_every_copy(self, db, threader, embedder, maildir):
        """Codex round 1 on #1134: the reap unmarked only the mapped path,
        so a copy that came back after a transient outage stayed marked
        indexed with no mapping left to repair, and was never re-queued."""
        from src import main
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        kept = maildir / "1700000000.M1.host:2,S"
        mapped = maildir / "1700000001.M2.host:2,S"
        _index_copies(db, threader, kept, mapped, "outage@example.com")
        saved = kept.read_bytes()
        kept.unlink()
        mapped.unlink()
        rec = Reconciler(db, embedder, _default_config())
        rec.sweep()
        assert rec.reap()["threads_reaped"] == 1

        assert not db.is_indexed(str(kept))
        assert not db.is_indexed(str(mapped))
        kept.write_bytes(saved)
        queued = main._enqueue_unindexed_messages(
            db, IndexingQueue(db), maildir.parent.parent, REASON_INITIAL_SCAN
        )
        assert queued == 1

    def test_partial_reap_unmarks_every_copy_of_the_reaped_message(
        self, db, threader, embedder, maildir
    ):
        kept = maildir / "1700000000.M1.host:2,S"
        mapped = maildir / "1700000001.M2.host:2,S"
        _write_eml(kept, "root1102@example.com", subject="Root")
        _index(kept, db, threader)
        mapped.write_bytes(kept.read_bytes())
        _index(mapped, db, threader)
        reply = maildir / "1700000002.M3.host:2,S"
        _write_eml(
            reply,
            "reply1102@example.com",
            subject="Re: Root",
            in_reply_to="root1102@example.com",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply, db, threader)
        kept.unlink()
        mapped.unlink()
        rec = Reconciler(db, embedder, _default_config())
        rec.sweep()

        assert rec.reap()["threads_rebuilt"] == 1
        assert not db.is_indexed(str(kept))
        assert not db.is_indexed(str(mapped))
        assert db.is_indexed(str(reply))

    def test_remap_keeps_the_runnable_job_when_the_copys_is_dead(
        self, db, threader, embedder, maildir
    ):
        """Codex round 2 on #1134: the gone path's runnable job was always
        dropped in favour of the copy's, even when the copy's was dead."""
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        kept = maildir / "1700000000.M1.host:2,S"
        mapped = maildir / "1700000001.M2.host:2,S"
        _index_copies(db, threader, kept, mapped, "deadcopy@example.com")
        queue = IndexingQueue(db)
        queue.enqueue(str(kept), REASON_INITIAL_SCAN)
        queue.mark_dead_terminal(str(kept), stage="embed", error="fixed text")
        queue.enqueue(str(mapped), REASON_INITIAL_SCAN)
        mapped.unlink()

        assert Reconciler(db, embedder, _default_config()).sweep()["remapped"] == 1

        assert queue.has_pending_row(str(kept))
        assert not queue.is_dead(str(kept))
        assert not queue.has_pending_row(str(mapped))

    def test_reap_unmarks_copies_with_one_scan_per_pass(self, db, threader, embedder, maildir):
        """Codex round 2 on #1134: the unmark scanned ``indexed_files``
        (no ``content_hash`` index) once per reaped thread. The pass looks
        the copies up once; each reap transaction then unmarks by path."""
        paths = []
        for n in range(3):
            kept = maildir / f"170000000{n}.K{n}.host:2,S"
            mapped = maildir / f"170000001{n}.M{n}.host:2,S"
            _index_copies(
                db, threader, kept, mapped, f"reapmany{n}@example.com", subject=f"Topic {n}"
            )
            kept.unlink()
            mapped.unlink()
            paths += [kept, mapped]
        rec = Reconciler(db, embedder, _default_config())
        rec.sweep()
        statements: list[str] = []
        db._conn.set_trace_callback(statements.append)
        try:
            assert rec.reap()["threads_reaped"] == 3
        finally:
            db._conn.set_trace_callback(None)

        scans = [s for s in statements if "indexed_files" in s and "content_hash" in s]
        assert len(scans) == 1, scans
        assert not any(db.is_indexed(str(p)) for p in paths)

    def test_a_copy_renamed_before_the_remap_is_followed(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """Codex round 2 on #1134: the watcher renamed the chosen copy
        between the sweep resolving it and the remap, which then mapped
        the message to a path that no longer existed."""
        import src.reconciler as reconciler_module

        kept = maildir / "1700000000.M1.host:2,S"
        mapped = maildir / "1700000001.M2.host:2,S"
        _index_copies(db, threader, kept, mapped, "raced@example.com")
        claimant = db.find_message_entry_by_filepath(str(mapped))["claimant_id"]
        mapped.unlink()
        replied = maildir / "1700000000.M1.host:2,RS"
        real_resolve = reconciler_module.resolve_current_path
        raced: list[bool] = []

        def resolve_then_rename(stored, listings=None):
            current = real_resolve(stored, listings)
            if current == kept and not raced:
                raced.append(True)
                kept.rename(replied)
                db.update_filepath(str(kept), str(replied))  # the watcher's write
            return current

        monkeypatch.setattr(reconciler_module, "resolve_current_path", resolve_then_rename)
        summary = sweep_paths(db)

        assert raced
        assert summary["remapped"] == 1
        assert _message_row(db, claimant)["filepath"] == str(replied)
        assert db.find_message_entry_by_filepath(str(replied))["claimant_id"] == claimant

    def test_a_copy_restored_after_the_cached_listing_is_found_on_retry(
        self, db, threader, maildir, monkeypatch
    ):
        """Codex round 6 on #1134: a copy absent when the sweep listed its
        folder, and back (flag-renamed) before the copy lookup, was missed
        through the stale listing and no cache-free attempt followed. The
        retry uses one fresh listing for the whole retry phase."""
        import src.reconciler as reconciler_module

        away = maildir.parent.parent / "away"
        away.mkdir()
        restore = []
        claimants = []
        for n in range(2):
            kept = maildir / f"170000000{n}.K{n}.host:2,S"
            mapped = maildir / f"170000001{n}.M{n}.host:2,S"
            _index_copies(db, threader, kept, mapped, f"retry{n}@example.com", subject=f"R{n}")
            claimants.append(db.find_message_entry_by_filepath(str(mapped))["claimant_id"])
            mapped.unlink()
            kept.rename(away / kept.name)
            restore.append((away / kept.name, maildir / f"170000000{n}.K{n}.host:2,RS"))

        real_resolve = reconciler_module.resolve_current_path

        def resolve_then_restore(stored, listings=None):
            current = real_resolve(stored, listings)
            while restore:  # mbsync puts the copies back, flag-renamed
                src, dest = restore.pop()
                src.rename(dest)
            return current

        listed: list[Path] = []
        real_iterdir = Path.iterdir

        def counting_iterdir(self):
            if self == maildir:
                listed.append(self)
            return real_iterdir(self)

        monkeypatch.setattr(reconciler_module, "resolve_current_path", resolve_then_restore)
        monkeypatch.setattr(Path, "iterdir", counting_iterdir)
        summary = sweep_paths(db)

        assert summary["remapped"] == 2
        for n, claimant in enumerate(claimants):
            row = _message_row(db, claimant)
            assert row["filepath"] == str(maildir / f"170000000{n}.K{n}.host:2,RS")
        # Once for the sweep's cached listing, once for the shared retry.
        assert len(listed) == 2

    def test_remap_is_refused_when_the_mapping_moved(self, db, threader, maildir):
        kept = maildir / "1700000000.M1.host:2,S"
        mapped = maildir / "1700000001.M2.host:2,S"
        _index_copies(db, threader, kept, mapped, "movedmap@example.com")

        assert not db.remap_to_identical_copy("/gone/elsewhere", str(kept))
        assert not db.remap_to_identical_copy(str(mapped), str(maildir / "absent"))
        assert db.find_message_entry_by_filepath(str(mapped)) is not None

    def test_a_trashed_mapped_copy_moves_to_a_live_copy(self, db, threader, embedder, maildir):
        """Codex round 3 on #1134: a mapped path still on disk but
        ``T``-flagged skipped the copy lookup, so it was tombstoned and
        reaped while a byte-identical live copy remained."""
        kept = maildir / "1700000000.M1.host:2,S"
        mapped = maildir / "1700000001.M2.host:2,S"
        thread_id = _index_copies(db, threader, kept, mapped, "trashmapped@example.com")
        claimant = db.find_message_entry_by_filepath(str(mapped))["claimant_id"]
        trashed = maildir / "1700000001.M2.host:2,ST"
        mapped.rename(trashed)
        rec = Reconciler(db, embedder, _default_config())
        rec.handle_moved(str(mapped), str(trashed))  # the live tombstone
        assert db.has_pending_deletion(str(trashed))

        summary = rec.sweep()

        assert summary["remapped"] == 1
        assert summary["tombstoned"] == 0
        assert count_pending_deletions(db) == 0
        assert _message_row(db, claimant)["filepath"] == str(kept)
        assert rec.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None

    def test_every_copy_trashed_still_tombstones(self, db, threader, embedder, maildir):
        kept = maildir / "1700000000.M1.host:2,ST"
        mapped = maildir / "1700000001.M2.host:2,S"
        thread_id = _index_copies(db, threader, kept, mapped, "alltrashed@example.com")
        trashed = maildir / "1700000001.M2.host:2,ST"
        mapped.rename(trashed)
        rec = Reconciler(db, embedder, _default_config())

        summary = rec.sweep()

        assert summary["remapped"] == 0
        assert summary["tombstoned"] == 1
        assert db.has_pending_deletion(str(trashed))
        assert rec.reap()["threads_reaped"] == 1
        assert db.get_thread(thread_id) is None

    def test_a_copy_restored_between_sweep_and_reap_keeps_the_message(
        self, db, threader, embedder, maildir, caplog
    ):
        """Codex round 4 on #1134: a copy whose T flag was removed after
        the sweep is not the mapped path, so the watcher cleared nothing
        and the reaper removed the message although the copy was live."""
        kept = maildir / "1700000000.M1.host:2,ST"
        mapped = maildir / "1700000001.M2.host:2,S"
        thread_id = _index_copies(db, threader, kept, mapped, "restored1102@example.com")
        claimant = db.find_message_entry_by_filepath(str(mapped))["claimant_id"]
        trashed = maildir / "1700000001.M2.host:2,ST"
        mapped.rename(trashed)
        rec = Reconciler(db, embedder, _default_config())
        assert rec.sweep()["tombstoned"] == 1
        restored = maildir / "1700000000.M1.host:2,S"
        kept.rename(restored)  # mbsync restores the unmapped copy

        with caplog.at_level(logging.INFO, logger="indexer.reconciler"):
            result = rec.reap()

        assert result["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None
        assert _message_row(db, claimant)["filepath"] == str(restored)
        assert count_pending_deletions(db) == 0
        line = next(r for r in caplog.records if "live identical copy" in r.getMessage())
        assert line.levelno == logging.INFO
        assert "1 message(s)" in line.getMessage()
        assert _COPY_MARKER not in caplog.text
        assert rec.reap()["threads_reaped"] == 0

    def test_a_copy_restored_before_a_partial_reap_keeps_the_message(
        self, db, threader, embedder, maildir
    ):
        kept = maildir / "1700000000.M1.host:2,ST"
        mapped = maildir / "1700000001.M2.host:2,S"
        _write_eml(kept, "proot1102@example.com", subject="Root")
        _index(kept, db, threader)
        mapped.write_bytes(kept.read_bytes())
        _index(mapped, db, threader)
        claimant = db.find_message_entry_by_filepath(str(mapped))["claimant_id"]
        reply = maildir / "1700000002.M3.host:2,S"
        _write_eml(
            reply,
            "preply1102@example.com",
            subject="Re: Root",
            in_reply_to="proot1102@example.com",
            date=datetime(2024, 2, 1, tzinfo=UTC),
        )
        _index(reply, db, threader)
        trashed = maildir / "1700000001.M2.host:2,ST"
        mapped.rename(trashed)
        rec = Reconciler(db, embedder, _default_config())
        rec.sweep()
        restored = maildir / "1700000000.M1.host:2,S"
        kept.rename(restored)

        result = rec.reap()

        assert result["threads_rebuilt"] == 0
        assert _message_row(db, claimant)["filepath"] == str(restored)
        assert count_pending_deletions(db) == 0

    def _thread_of_restorable_copies(self, db, threader, maildir) -> tuple[str, list[Path]]:
        """Three messages of one thread, each with two trashed identical
        copies, tombstoned by a sweep; returns the thread and the unmapped
        copies' live (restored) names, not yet on disk."""
        restored = []
        parent = None
        for n in range(3):
            kept = maildir / f"170000000{n}.K{n}.host:2,ST"
            mapped = maildir / f"170000001{n}.M{n}.host:2,S"
            _write_eml(
                kept,
                f"chain{n}@example.com",
                subject="Re: Chain" if parent else "Chain",
                in_reply_to=parent,
                date=datetime(2024, 1, 1 + n, tzinfo=UTC),
            )
            thread_id = _index(kept, db, threader)
            mapped.write_bytes(kept.read_bytes())
            _index(mapped, db, threader)
            mapped.rename(maildir / f"170000001{n}.M{n}.host:2,ST")
            restored.append(maildir / f"170000000{n}.K{n}.host:2,S")
            parent = f"chain{n}@example.com"
        return thread_id, restored

    def test_live_copy_checks_stay_within_the_pass_budget(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """Codex rounds 5 and 8 on #1134: each message's copies are
        resolved with a listing of its own, and every listing counts
        against one budget per reap pass, so a pass lists at most
        ``_LIVE_COPY_RECHECK_LISTINGS`` directories for these checks."""
        import src.reconciler as reconciler_module

        thread_id, restored = self._thread_of_restorable_copies(db, threader, maildir)
        rec = Reconciler(db, embedder, _default_config())
        assert rec.sweep()["tombstoned"] == 3
        for path in restored:
            path.with_name(path.name + "T").rename(path)
        monkeypatch.setattr(reconciler_module, "_LIVE_COPY_RECHECK_LISTINGS", 3)
        listed = self._count_listings(monkeypatch, maildir)

        result = rec.reap()

        assert len(listed) <= 3
        # A check reserves its copies' distinct directories (``cur`` and its
        # ``new`` sibling: 2) and is charged what it listed (1 here): two
        # fit in 3, the third waits for the next pass.
        assert len(listed) == 2
        assert result["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None
        assert count_pending_deletions(db) == 1

    def test_a_kept_thread_is_no_longer_counted_as_blocked(self, db, threader, embedder, maildir):
        """Codex round 5 on #1134: a thread an earlier pass recorded as
        blocked kept its entry after a live copy cancelled the reap."""
        thread_id, restored = self._thread_of_restorable_copies(db, threader, maildir)
        rec = Reconciler(db, embedder, _default_config())
        rec.sweep()
        for path in restored:
            path.with_name(path.name + "T").rename(path)
        rec._blocked_thread_attempts[thread_id] = 2

        result = rec.reap()

        assert result["threads_reaped"] == 0
        assert result["blocked_threads"] == 0

    def test_a_copy_restored_after_the_threads_listing_is_kept(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """Codex round 7 on #1134: the per-thread listing, taken for an
        earlier message, still showed a later message's copy as trashed
        after mbsync restored it, so the thread was reaped although that
        copy was live. A miss is resolved again with a fresh listing."""
        thread_id, restored = self._thread_of_restorable_copies(db, threader, maildir)
        rec = Reconciler(db, embedder, _default_config())
        assert rec.sweep()["tombstoned"] == 3
        first = restored[0]
        first.with_name(first.name + "T").rename(first)
        real_iterdir = Path.iterdir
        pending = list(restored[1:])

        def iterdir_then_restore(self):
            listing = list(real_iterdir(self))
            if self == maildir:
                while pending:  # restored once the folder was listed
                    path = pending.pop()
                    path.with_name(path.name + "T").rename(path)
            return iter(listing)

        monkeypatch.setattr(Path, "iterdir", iterdir_then_restore)
        result = rec.reap()

        assert result["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None
        mapped = {r["filepath"] for r in db.iter_message_map()}
        assert mapped == {str(p) for p in restored}
        assert count_pending_deletions(db) == 0

    @staticmethod
    def _stored_message_ids(db, thread_id: str) -> list[str]:
        import json

        row = db._conn.execute(
            "SELECT message_ids FROM threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return json.loads(row["message_ids"])

    @staticmethod
    def _count_listings(monkeypatch, directory: Path) -> list[Path]:
        listed: list[Path] = []
        real_iterdir = Path.iterdir

        def counting_iterdir(self):
            if self == directory:
                listed.append(self)
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", counting_iterdir)
        return listed

    def test_a_thread_past_the_budget_makes_progress_every_pass(
        self, db, threader, embedder, maildir, monkeypatch, caplog
    ):
        """Codex round 8 on #1134: a thread needing more re-checks than the
        budget was deferred whole, and every pass repeated the same checks,
        so it was never reaped. Now the checked messages are reaped and the
        unchecked ones stay, whole, in the rebuilt thread for the next pass."""
        import src.reconciler as reconciler_module

        thread_id, _restored = self._thread_of_restorable_copies(db, threader, maildir)
        rec = Reconciler(db, embedder, _default_config())
        rec.sweep()
        for n in range(3):  # every copy gone for good
            (maildir / f"170000000{n}.K{n}.host:2,ST").unlink()
        monkeypatch.setattr(reconciler_module, "_LIVE_COPY_RECHECK_LISTINGS", 2)
        listed = self._count_listings(monkeypatch, maildir)

        with caplog.at_level(logging.INFO, logger="indexer.reconciler"):
            first = rec.reap()

        assert len(listed) == 1  # one check fits the budget of 2
        assert first["threads_rebuilt"] == 1
        assert db.count_total_messages() == 2
        assert count_pending_deletions(db) == 2
        thread = db.get_thread(thread_id)
        assert thread is not None
        assert set(self._stored_message_ids(db, thread_id)) == {
            r["claimant_id"] for r in db.get_thread_messages(thread_id)
        }
        warning = next(r for r in caplog.records if "re-check budget" in r.getMessage())
        assert warning.levelno == logging.WARNING
        assert "2 message(s) left for the next pass" in warning.getMessage()
        partial = next(
            r
            for r in caplog.records
            if "left for the next pass" in r.getMessage() and r.levelno == logging.INFO
        )
        assert "reaped 1 of 3" in partial.getMessage()
        assert _COPY_MARKER not in caplog.text
        assert str(maildir) not in caplog.text

        listed.clear()
        assert rec.reap()["threads_rebuilt"] == 1
        assert len(listed) == 1
        assert db.count_total_messages() == 1
        listed.clear()
        assert rec.reap()["threads_reaped"] == 1
        assert len(listed) == 1
        assert db.get_thread(thread_id) is None
        assert count_pending_deletions(db) == 0

    def test_unchecked_messages_stay_whole_in_the_rebuilt_thread(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """The panel's consistency requirement: a message left unchecked is
        a survivor of the rebuild, so its text, chunks and the thread
        vector are computed with it."""
        import src.reconciler as reconciler_module

        thread_id, _restored = self._thread_of_restorable_copies(db, threader, maildir)
        rec = Reconciler(db, embedder, _default_config())
        rec.sweep()
        for n in range(3):
            (maildir / f"170000000{n}.K{n}.host:2,ST").unlink()
        monkeypatch.setattr(reconciler_module, "_LIVE_COPY_RECHECK_LISTINGS", 2)
        survivors_seen: list[list[str]] = []
        real = db.get_chunk_embeddings_for_messages

        def spy(claimant_ids):
            survivors_seen.append(sorted(claimant_ids))
            return real(claimant_ids)

        monkeypatch.setattr(db, "get_chunk_embeddings_for_messages", spy)
        rec.reap()

        remaining = sorted(r["claimant_id"] for r in db.get_thread_messages(thread_id))
        assert survivors_seen == [remaining]
        assert len(remaining) == 2
        assert sorted(self._stored_message_ids(db, thread_id)) == remaining

    def test_restores_around_the_checks(self, db, threader, embedder, maildir, monkeypatch):
        """A copy restored before its message is checked keeps the message;
        one restored after its check is unmarked with the reaped message,
        so the next Maildir walk indexes it again."""
        from src import main
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        thread_id, restored = self._thread_of_restorable_copies(db, threader, maildir)
        rec = Reconciler(db, embedder, _default_config())
        rec.sweep()
        for n in range(3):
            (maildir / f"170000000{n}.K{n}.host:2,ST").unlink()
        saved = {}
        for n, path in enumerate(restored):
            mapped = maildir / f"170000001{n}.M{n}.host:2,ST"
            saved[path] = mapped.read_bytes()
        real_iterdir = Path.iterdir
        listings: list[int] = []

        def iterdir_with_restores(self):
            listing = list(real_iterdir(self))
            if self == maildir:
                listings.append(1)
                if len(listings) == 1:
                    # During the first message's check: its own copy comes
                    # back too late, the second message's before its check.
                    restored[0].write_bytes(saved[restored[0]])
                    restored[1].write_bytes(saved[restored[1]])
            return iter(listing)

        monkeypatch.setattr(Path, "iterdir", iterdir_with_restores)
        rec.reap()
        monkeypatch.setattr(Path, "iterdir", real_iterdir)

        mapped = {r["filepath"] for r in db.iter_message_map()}
        assert str(restored[1]) in mapped  # kept through its restored copy
        assert str(restored[0]) not in mapped  # reaped: restored after its check
        assert not db.is_indexed(str(restored[0]))
        queued = main._enqueue_unindexed_messages(
            db, IndexingQueue(db), maildir.parent.parent, REASON_INITIAL_SCAN, skip_trashed=True
        )
        assert queued == 1
        assert db.get_thread(thread_id) is not None

    def test_a_gone_file_message_past_the_budget_waits_visibly(
        self, db, threader, embedder, maildir, monkeypatch, caplog
    ):
        """Owner decision, round 9 on #1134: a message whose own file is
        gone is never reaped unchecked. Past the budget it stays
        tombstoned; it cannot be rebuilt as a survivor, so its thread waits
        on the blocked-thread count and WARNING until the budget reaches
        it. Gone files are checked first."""
        import src.reconciler as reconciler_module

        thread_id, _restored = self._thread_of_restorable_copies(db, threader, maildir)
        rec = Reconciler(db, embedder, _default_config())
        for n in range(3):
            (maildir / f"170000000{n}.K{n}.host:2,ST").unlink()
            (maildir / f"170000001{n}.M{n}.host:2,ST").unlink()
        rec.sweep()
        monkeypatch.setattr(reconciler_module, "_LIVE_COPY_RECHECK_LISTINGS", 2)

        with caplog.at_level(logging.INFO, logger="indexer.reconciler"):
            result = rec.reap()

        assert result["threads_reaped"] == 0
        assert result["blocked_threads"] == 1
        assert count_pending_deletions(db) == 3
        assert db.get_thread(thread_id) is not None
        assert "unchecked" not in caplog.text
        assert any(
            r.levelno == logging.WARNING and "blocked from reaping" in r.getMessage()
            for r in caplog.records
        )

        monkeypatch.setattr(reconciler_module, "_LIVE_COPY_RECHECK_LISTINGS", 256)
        assert rec.reap()["threads_reaped"] == 1

    def _add_copy_markers(self, db, claimant_id: str, paths: list[Path]) -> None:
        """Record ``paths`` in ``indexed_files`` as further copies of
        ``claimant_id`` (same bytes), without files on disk."""
        content_hash = db._conn.execute(
            "SELECT content_hash FROM messages WHERE claimant_id = ?", (claimant_id,)
        ).fetchone()["content_hash"]
        db._conn.executemany(
            "INSERT INTO indexed_files (filepath, indexed_at, size, mtime_ns, content_hash) "
            "VALUES (?, datetime('now'), NULL, NULL, ?)",
            [(str(p), content_hash) for p in paths],
        )
        db._conn.commit()

    def test_a_message_with_many_copies_in_one_folder_is_checked(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """Codex round 9 on #1134: 3 listings reserved per copy made a
        message with 86 or more copies cost more than the whole budget,
        so it was never checked. It reserves its distinct folders."""
        kept = maildir / "1700000000.K0.host:2,S"
        mapped = maildir / "1700000001.M0.host:2,S"
        thread_id = _index_copies(db, threader, kept, mapped, "many1102@example.com")
        claimant = db.find_message_entry_by_filepath(str(mapped))["claimant_id"]
        self._add_copy_markers(
            db, claimant, [maildir / f"17000001{n:02d}.C{n}.host:2,S" for n in range(90)]
        )
        kept.unlink()
        trashed = maildir / "1700000001.M0.host:2,ST"
        mapped.rename(trashed)
        rec = Reconciler(db, embedder, _default_config())
        assert rec.sweep()["tombstoned"] == 1
        listed = self._count_listings(monkeypatch, maildir)

        result = rec.reap()

        assert result["threads_reaped"] == 1
        assert db.get_thread(thread_id) is None
        assert len(listed) == 1  # one folder, listed once for 91 copies

    def test_an_oversized_check_runs_first_in_its_pass_and_logs(
        self, db, threader, embedder, maildir, monkeypatch, caplog
    ):
        """A message whose folders alone exceed the budget is checked as
        the pass's first check (its cost is bounded by folders, which mail
        cannot grow), logged at INFO with counts; the next one waits."""
        import src.reconciler as reconciler_module

        thread_id, _restored = self._thread_of_restorable_copies(db, threader, maildir)
        rec = Reconciler(db, embedder, _default_config())
        rec.sweep()
        for n in range(3):
            (maildir / f"170000000{n}.K{n}.host:2,ST").unlink()
        monkeypatch.setattr(reconciler_module, "_LIVE_COPY_RECHECK_LISTINGS", 1)
        listed = self._count_listings(monkeypatch, maildir)

        with caplog.at_level(logging.INFO, logger="indexer.reconciler"):
            result = rec.reap()

        assert result["threads_rebuilt"] == 1
        assert len(listed) == 1
        assert count_pending_deletions(db) == 2
        over = [r for r in caplog.records if "over the budget" in r.getMessage()]
        assert [(r.levelno, r.getMessage()) for r in over] == [
            (
                logging.INFO,
                "reaper: a live-copy check needs 2 directory listing(s), over the "
                "budget of 1; run as the pass's first check",
            )
        ]
        assert str(maildir) not in caplog.text
        assert db.get_thread(thread_id) is not None

    def test_a_refused_remap_is_a_retry_not_a_kept_copy(
        self, db, threader, embedder, maildir, monkeypatch, caplog
    ):
        """Codex round 9 on #1134: ``kept`` was counted, and the blocked
        state cleared, before the remap's result was known."""
        kept = maildir / "1700000000.M1.host:2,ST"
        mapped = maildir / "1700000001.M2.host:2,S"
        thread_id = _index_copies(db, threader, kept, mapped, "refused1102@example.com")
        trashed = maildir / "1700000001.M2.host:2,ST"
        mapped.rename(trashed)
        rec = Reconciler(db, embedder, _default_config())
        rec.sweep()
        kept.rename(maildir / "1700000000.M1.host:2,S")  # restored after the sweep
        rec._blocked_thread_attempts[thread_id] = 2
        monkeypatch.setattr(db, "remap_to_identical_copy", lambda *_a, **_kw: False)

        with caplog.at_level(logging.INFO, logger="indexer.reconciler"):
            result = rec.reap()

        assert result["threads_reaped"] == 0
        assert rec._blocked_thread_attempts.get(thread_id) == 2
        assert count_pending_deletions(db) == 1
        assert "live identical copy restored" not in caplog.text
        retry = [r for r in caplog.records if "refused" in r.getMessage()]
        assert [(r.levelno, r.getMessage()) for r in retry] == [
            (
                logging.INFO,
                "reaper: 1 remap(s) to a live identical copy refused (the mapping "
                "or the copy moved); retrying next pass",
            )
        ]

    def _tombstone_backdated(self, db, path: Path, days: int) -> None:
        entry = db.find_message_entry_by_filepath(str(path))
        assert db.add_pending_deletion(str(path), entry["claimant_id"], entry["thread_id"])
        marked = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        db._conn.execute(
            "UPDATE pending_deletions SET marked_at = ? WHERE filepath = ?", (marked, str(path))
        )
        db._conn.commit()

    def _marked_at(self, db, path: Path) -> datetime:
        row = db._conn.execute(
            "SELECT marked_at FROM pending_deletions WHERE filepath = ?", (str(path),)
        ).fetchone()
        return datetime.fromisoformat(row["marked_at"])

    def test_remap_onto_an_older_tombstone_restarts_the_grace(
        self, db, threader, embedder, maildir
    ):
        """Codex round 7 on #1134: the copy's own old tombstone survived
        the remap (``add_pending_deletion`` ignores an existing row), so
        the message was reaped a grace period after the copy was trashed,
        not after the live path it was mapped to disappeared."""
        kept = maildir / "1700000000.M1.host:2,ST"
        mapped = maildir / "1700000001.M2.host:2,S"
        _write_eml(kept, "grace1102@example.com")
        thread_id = _index(kept, db, threader)
        self._tombstone_backdated(db, kept, days=10)
        mapped.write_bytes(kept.read_bytes())
        _index(mapped, db, threader)  # live and mapped, no tombstone
        mapped.unlink()
        before = datetime.now(UTC)
        rec = Reconciler(db, embedder, _default_config(grace_days=7))

        assert rec.sweep()["remapped"] == 1

        assert self._marked_at(db, kept) >= before
        assert rec.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None

    def test_merging_two_tombstones_keeps_the_later_mark(self, db, threader, embedder, maildir):
        """Codex round 7 on #1134: of two tombstones the copy's was kept
        whatever its age."""
        kept = maildir / "1700000000.M1.host:2,ST"
        mapped = maildir / "1700000001.M2.host:2,S"
        _write_eml(kept, "merge1102@example.com")
        thread_id = _index(kept, db, threader)
        self._tombstone_backdated(db, kept, days=10)
        mapped.write_bytes(kept.read_bytes())
        _index(mapped, db, threader)
        self._tombstone_backdated(db, mapped, days=1)
        later = self._marked_at(db, mapped)
        mapped.unlink()
        rec = Reconciler(db, embedder, _default_config(grace_days=7))

        assert rec.sweep()["remapped"] == 1

        assert count_pending_deletions(db) == 1
        assert self._marked_at(db, kept) == later
        assert rec.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None

    def test_reap_drops_a_copys_dead_job(self, db, threader, embedder, maildir):
        """Codex round 4 on #1134: the reap unmarked a copy's path but
        kept its dead job, so the walk skipped the copy when it came back
        until an operator requeued dead jobs."""
        from src import main
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        kept = maildir / "1700000000.M1.host:2,S"
        mapped = maildir / "1700000001.M2.host:2,S"
        _index_copies(db, threader, kept, mapped, "deadjob1102@example.com")
        queue = IndexingQueue(db)
        queue.enqueue(str(kept), REASON_INITIAL_SCAN)
        queue.mark_dead_terminal(str(kept), stage="embed", error="fixed text")
        saved = kept.read_bytes()
        kept.unlink()
        mapped.unlink()
        rec = Reconciler(db, embedder, _default_config())
        rec.sweep()
        assert rec.reap()["threads_reaped"] == 1

        assert not queue.is_dead(str(kept))
        assert not queue.has_pending_row(str(kept))
        kept.write_bytes(saved)
        assert (
            main._enqueue_unindexed_messages(db, queue, maildir.parent.parent, REASON_INITIAL_SCAN)
            == 1
        )

    def test_archive_mode_rename_sweep_remaps_to_the_copy(self, db, threader, maildir, caplog):
        """Codex round 1 on #1134: archive mode has no reconciler, so the
        message kept the gone path, folder and flags for ever."""
        root = maildir.parent.parent
        kept = root / "Archive" / "cur" / "1700000000.M1.host:2,RS"
        mapped = maildir / "1700000001.M2.host:2,S"
        _index_copies(db, threader, kept, mapped, "archive@example.com")
        claimant = db.find_message_entry_by_filepath(str(mapped))["claimant_id"]
        mapped.unlink()

        with caplog.at_level(logging.INFO, logger="indexer.reconciler"):
            summary = sweep_paths(db, maildir_root=root)

        assert summary["remapped"] == 1
        assert summary["unreachable"] == 0
        row = _message_row(db, claimant)
        assert row["filepath"] == str(kept)
        assert row["folder"] == "Archive"
        assert row["replied"] == 1
        line = next(r for r in caplog.records if "remapped=1" in r.getMessage())
        assert line.levelno == logging.INFO
        assert _COPY_MARKER not in caplog.text
        assert count_pending_deletions(db) == 0


# ---------------------------------------------------------------------------
# sweep_paths — always-on startup rename sweep
# ---------------------------------------------------------------------------


class TestSweepPaths:
    def test_updates_filepath_on_flag_rename(self, db, threader, maildir):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "sp1@example.com")
        _index(path, db, threader)

        replied = maildir / "1700000000.M1.host:2,RS"
        path.rename(replied)

        result = sweep_paths(db)
        assert result["renamed"] == 1
        assert result["unreachable"] == 0
        assert db.find_message_entry_by_filepath(str(replied)) is not None
        assert db.find_message_entry_by_filepath(str(path)) is None

    def test_heals_read_state_changed_while_offline(self, db, threader, maildir):
        """A flag change mbsync made while the indexer was down reaches
        the message's state through the startup sweep."""
        path = maildir / "1700000000.M9.host:2,"
        _write_eml(path, "sp-state@example.com")
        _index(path, db, threader)
        starred = maildir / "1700000000.M9.host:2,FS"
        path.rename(starred)

        sweep_paths(db)

        row = db._conn.execute("SELECT filepath, seen, flagged, replied FROM messages").fetchone()
        assert tuple(row) == (str(starred), 1, 1, 0)

    def test_lists_each_folder_once_however_many_files_were_renamed(
        self, db, threader, maildir, monkeypatch
    ):
        """Review (PR #246): the sweep now runs before the startup walk,
        and it rescanned the whole folder for every stale path — O(N^2)
        for N files renamed while the indexer was down. Each directory
        is now listed at most once per sweep."""
        count = 6
        for i in range(count):
            path = maildir / f"17000000{i:02d}.M{i}.host:2,S"
            _write_eml(path, f"many{i}@example.com")
            _index(path, db, threader)
            path.rename(path.with_name(path.name.replace(":2,S", ":2,RS")))
        listed: list[Path] = []
        real_iterdir = Path.iterdir

        def counting_iterdir(self):
            listed.append(self)
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", counting_iterdir)

        result = sweep_paths(db)

        assert result == {
            "renamed": count,
            "unreachable": 0,
            "tombstones_cleared": 0,
            "remapped": 0,
        }
        assert len(listed) == len(set(listed)) == 1

    def test_does_not_tombstone_missing_files(self, db, threader, maildir):
        """sweep_paths is the always-on variant; a missing file must be
        counted as unreachable but NOT recorded in pending_deletions.
        Reconciler.sweep() is still responsible for tombstoning when
        deletion reconciliation is enabled (mirror mode)."""
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "sp2@example.com")
        _index(path, db, threader)
        path.unlink()

        result = sweep_paths(db)
        assert result["unreachable"] == 1
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

        result = sweep_paths(db)
        assert result["renamed"] == 1
        assert count_pending_deletions(db) == 0

    def test_idempotent(self, db, threader, maildir):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "sp4@example.com")
        _index(path, db, threader)
        replied = maildir / "1700000000.M1.host:2,RS"
        path.rename(replied)

        first = sweep_paths(db)
        second = sweep_paths(db)
        assert first["renamed"] == 1
        assert second["renamed"] == 0

    def _index_tombstoned(self, db, threader, maildir, subject: str) -> Path:
        """One indexed ``T``-flagged file carrying a tombstone, as a
        mirror-mode run leaves it; archive mode writes none itself."""
        path = maildir / "1700000000.M1.host:2,ST"
        _write_eml(path, "sp-restore@example.com", subject=subject)
        thread_id = _index(path, db, threader)
        entry = db.find_message_entry_by_filepath(str(path))
        assert entry is not None
        assert db.add_pending_deletion(str(path), entry["claimant_id"], thread_id)
        return path

    def test_clears_leftover_tombstone_on_restore_seen_at_startup(
        self, db, threader, maildir, caplog
    ):
        """Review round 1 on #860: a restore (``T`` flag cleared) that
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
            result = sweep_paths(db)

        assert result == {"renamed": 1, "unreachable": 0, "tombstones_cleared": 1, "remapped": 0}
        assert count_pending_deletions(db) == 0
        assert db.find_message_entry_by_filepath(str(live)) is not None
        assert db._conn.execute("SELECT count(*) FROM message_thread_map").fetchone()[0] == 1
        assert [
            (r.levelno, r.getMessage())
            for r in caplog.records
            if r.getMessage().startswith("startup rename sweep")
        ] == [
            (
                logging.INFO,
                "startup rename sweep: renamed=1 unreachable=0 tombstones_cleared=1 remapped=0",
            )
        ]
        assert marker not in caplog.text

    def test_keeps_leftover_tombstone_while_file_stays_trashed(self, db, threader, maildir, caplog):
        """A flag rename that keeps ``T`` is no restore: the tombstone
        follows the file to its new name and the sweep counts none
        cleared."""
        marker = "SYNTHETIC_MARKER_860"
        path = self._index_tombstoned(db, threader, maildir, marker)
        starred = maildir / "1700000000.M1.host:2,FST"
        path.rename(starred)

        with caplog.at_level(logging.INFO):
            result = sweep_paths(db)

        assert result == {"renamed": 1, "unreachable": 0, "tombstones_cleared": 0, "remapped": 0}
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

        reconciler.handle_moved(str(path), str(trashed))
        assert db.has_pending_deletion(str(trashed))

    def test_clears_tombstone_when_flag_removed(self, db, threader, reconciler, maildir):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "mv2@example.com")
        _index(path, db, threader)

        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)
        reconciler.handle_moved(str(path), str(trashed))
        assert db.has_pending_deletion(str(trashed))

        restored = maildir / "1700000000.M1.host:2,S"
        trashed.rename(restored)
        reconciler.handle_moved(str(trashed), str(restored))
        assert db.has_pending_deletion(str(trashed)) is False
        assert db.has_pending_deletion(str(restored)) is False

    def test_ignores_moves_for_unindexed_files(self, db, threader, reconciler, maildir):
        src = maildir / "unknown:2,S"
        dest = maildir / "unknown:2,ST"
        # Must not raise, must not record anything
        reconciler.handle_moved(str(src), str(dest))
        assert count_pending_deletions(db) == 0


# ---------------------------------------------------------------------------
# Reap — grace window, full-reap, rebuild paths
# ---------------------------------------------------------------------------


class TestReap:
    def test_does_not_reap_inside_grace_window(self, db, threader, embedder, maildir):
        cfg = _default_config(grace_days=7)
        rec = Reconciler(db, embedder, cfg)

        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "g1@example.com")
        _index(path, db, threader)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)
        rec.sweep()

        result = rec.reap()
        assert result["threads_reaped"] == 0
        assert result["threads_rebuilt"] == 0
        assert (
            db.get_thread(db.find_message_entry_by_filepath(str(trashed))["thread_id"]) is not None
        )

    def test_aba_move_during_sweep_does_not_reap_the_live_message(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """#336 review round 1: the sweep resolved path A as missing while
        the file was briefly at B, and the watcher moved it back to A
        before the tombstone was written. The map holds A again, so the
        tombstone passes the path check; with a zero grace window the
        next reap deleted the live message."""
        import src.reconciler as reconciler_module

        rec = Reconciler(db, embedder, _default_config())
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "aba@example.com")
        thread_id = _index(path, db, threader)
        elsewhere = maildir.parent.parent / "Archive" / "cur" / path.name
        elsewhere.parent.mkdir(parents=True)

        real_resolve = reconciler_module.resolve_current_path

        def move_away_resolve_move_back(stored, listings=None):
            path.rename(elsewhere)
            rec.handle_moved(str(path), str(elsewhere), folder="Archive")
            current = real_resolve(stored, listings)
            elsewhere.rename(path)
            rec.handle_moved(str(elsewhere), str(path), folder="INBOX")
            return current

        monkeypatch.setattr(reconciler_module, "resolve_current_path", move_away_resolve_move_back)
        rec.sweep()
        monkeypatch.setattr(reconciler_module, "resolve_current_path", real_resolve)

        assert rec.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None
        assert count_pending_deletions(db) == 0

    def test_live_checks_list_each_directory_once(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """#336 review round 2: each tombstone whose mapped file is gone
        made the live check list the folder again, so a batch of missing
        files in one large folder cost a folder scan per tombstone. The
        reaper shares one listings cache across the pass, as the sweep
        does."""
        paths = []
        for i in range(20):
            p = maildir / f"1700000{i:04d}.M1.host:2,S"
            _write_eml(p, f"gone{i}@example.com", subject=f"Subject {i}")
            _index(p, db, threader)
            paths.append(p)
        for p in paths:
            entry = db.find_message_entry_by_filepath(str(p))
            db.add_pending_deletion(str(p), entry["claimant_id"], entry["thread_id"])
            p.unlink()

        listed: list[Path] = []
        real_iterdir = Path.iterdir

        def counting_iterdir(self):
            listed.append(self)
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", counting_iterdir)
        rec = Reconciler(db, embedder, _default_config(force=True))
        assert rec.reap()["threads_reaped"] == 20
        assert listed
        assert len(listed) == len(set(listed))

    def test_reap_clears_an_orphan_tombstone_for_a_live_message(
        self, db, threader, embedder, maildir
    ):
        """A tombstone left under a dead path by the #301 race before its
        fix: the message now maps to a live, untrashed file, so the
        reaper clears the tombstone instead of deleting the message."""
        rec = Reconciler(db, embedder, _default_config())
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "orphan@example.com")
        thread_id = _index(path, db, threader)
        entry = db.find_message_entry_by_filepath(str(path))
        db._conn.execute(
            "INSERT INTO pending_deletions (filepath, claimant_id, thread_id, marked_at) "
            "VALUES (?, ?, ?, '2000-01-01T00:00:00+00:00')",
            (str(maildir / "1700000000.M1.host:2,ST"), entry["claimant_id"], thread_id),
        )
        db._conn.commit()

        assert rec.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None
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
        """Review round 3: the watcher restores a message after ``reap()``
        read its tombstones but before the reap transaction runs."""
        snapshot = db.list_pending_deletions_older_than(datetime.now(UTC).isoformat())
        trashed.rename(live)
        db.update_filepath(str(trashed), str(live))
        db.clear_pending_deletion(str(live))
        monkeypatch.setattr(db, "list_pending_deletions_older_than", lambda _cutoff: snapshot)

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
        """Review round 4: restored and T-flagged again after the snapshot,
        the message has a fresh tombstone, so its grace period restarts;
        matching any tombstone by message ID deleted it at once."""
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        rec = Reconciler(db, embedder, _default_config(grace_days=7))
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
        snapshot = db.list_pending_deletions_older_than(datetime.now(UTC).isoformat())
        # Restored, then trashed again: a new tombstone, a new grace period.
        db.clear_pending_deletion(str(trashed))
        db.add_pending_deletion(str(trashed), "again@example.com", thread_id)
        monkeypatch.setattr(db, "list_pending_deletions_older_than", lambda _cutoff: snapshot)

        assert rec.reap()["threads_reaped"] == 0
        assert db.get_thread(thread_id) is not None
        assert queue.has_pending_row(str(trashed))

    def test_reap_between_restore_steps_keeps_the_message(
        self, db, threader, embedder, maildir, monkeypatch
    ):
        """Review round 5: ``handle_moved`` committed the rename, then
        cleared the tombstone in a separate step; a reap on the main thread
        in between still saw the eligible tombstone and deleted the
        restored message. The rename and the clear are now one write."""
        from src.queue import REASON_INITIAL_SCAN, IndexingQueue

        rec = Reconciler(db, embedder, _default_config())
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "restoring@example.com")
        thread_id = _index(path, db, threader)
        queue = IndexingQueue(db)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)
        rec.handle_moved(str(path), str(trashed))
        trashed.rename(path)

        real_update = db.update_filepath

        def update_then_reap(*args, **kwargs):
            result = real_update(*args, **kwargs)
            rec.reap()  # the main thread wins the lock right after the rename
            return result

        monkeypatch.setattr(db, "update_filepath", update_then_reap)
        rec.handle_moved(str(trashed), str(path))

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
        assert db.find_message_entry_by_filepath(str(orig_trashed)) is None
        assert db.find_message_entry_by_filepath(str(reply_path)) is not None
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
                reconciler.handle_moved(str(trashed), str(renamed))
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
        # the reaper must skip the pass cleanly — not crash. Equivalent
        # to the prior None-return behavior, now exercised through the
        # OSError path that ``parse_email`` propagates.
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
        # Deterministic parse failures on a survivor will retry forever
        # under the broadened ``except Exception`` catch. Without a
        # visibility surface, the only operator signal was a stuck
        # tombstone count + scattered WARN/ERROR log lines per pass.
        # ``reap()`` now reports ``blocked_threads`` so a stale
        # deletion-cleanup state is visible to ``get_mailbox_status``
        # consumers instead of buried in logs.
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
        # ``OSError`` subclass, so the reap path must catch it
        # explicitly — otherwise a single oversized survivor crashes
        # the whole ``reap()`` call and stalls every other thread's
        # tombstones behind it.
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
        # no equivalent — and the previous narrow ``OSError`` catch let
        # those exceptions crash ``reap()`` entirely, stalling every
        # other thread's tombstones. The reaper must log + skip the pass.
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
        assert db.find_message_entry_by_filepath(str(alone_trashed)) is None
        assert db.find_message_entry_by_filepath(str(orig_trashed)) is None
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
        result = reconciler.reap()
        assert result["threads_rebuilt"] == 1

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
        # Stage past the 10-message absolute floor so the 5% percentage
        # gate is the binding constraint — max_allowed here is
        # ``max(10, int(30 * 0.05)) = 10``; 20 tombstones trips the brake.
        paths = self._stage_batch(maildir, db, threader, 30)
        for p in paths[:20]:
            t = p.with_name(p.name + "T")
            p.rename(t)

        cfg = _default_config(grace_days=0, max_batch_pct=0.05)
        rec = Reconciler(db, embedder, cfg)
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
        rec = Reconciler(db, embedder, cfg)
        rec.sweep()
        db._conn.execute("UPDATE pending_deletions SET marked_at = '2000-01-01T00:00:00+00:00'")
        db._conn.commit()

        result = rec.reap()
        assert result["aborted"] is False
        assert db.count_total_messages() == 4  # 10 - 6 reaped

    def test_stale_live_tombstones_do_not_trip_the_brake(self, db, threader, embedder, maildir):
        """#336 review round 2: tombstones whose message maps to a live,
        untrashed file are stale and cleared by the reaper, but they were
        counted against the brake first. Eleven orphans in a small mailbox
        aborted every pass, and no sweep revisits their dead paths, so the
        brake blocked legitimate deletions forever."""
        paths = self._stage_batch(maildir, db, threader, 12)
        for p in paths[:11]:
            entry = db.find_message_entry_by_filepath(str(p))
            db._conn.execute(
                "INSERT INTO pending_deletions (filepath, claimant_id, thread_id, marked_at) "
                "VALUES (?, ?, ?, '2000-01-01T00:00:00+00:00')",
                (str(p) + "T", entry["claimant_id"], entry["thread_id"]),
            )
        db._conn.commit()
        trashed = paths[11].with_name(paths[11].name + "T")
        paths[11].rename(trashed)

        cfg = _default_config(grace_days=0, max_batch_pct=0.05)
        rec = Reconciler(db, embedder, cfg)
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
        rec = Reconciler(db, embedder, cfg)
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
                "INDEXER_DELETION_FORCE": "",
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
                "INDEXER_DELETION_FORCE": "YES",
            }
        )
        assert cfg.grace_days == 0
        assert cfg.sweep_interval_secs == 60
        assert cfg.max_batch_pct == 1.0
        assert cfg.force is True
        cfg = load_config_from_env(
            {
                "INDEXER_DELETION_MAX_BATCH_PCT": "0",
                "INDEXER_DELETION_FORCE": "off",
            }
        )
        assert cfg.max_batch_pct == 0.0
        assert cfg.force is False

    @pytest.mark.parametrize("name", ["INDEXER_DELETION_FORCE"])
    @pytest.mark.parametrize("raw", ["tru", "-5", "nan", "inf"])
    def test_unrecognized_boolean_fails(self, name, raw):
        """A typo used to read as false without a word (#481)."""
        with pytest.raises(ValueError, match=f"{name}.*not recognized"):
            load_config_from_env({name: raw})

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
        """NaN used to pass both range checks and then raise inside every
        reap sweep, so reaping silently never ran (#481); out-of-range
        values used to be clamped."""
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
        snapshot = db.list_pending_deletions_older_than(datetime.now(UTC).isoformat())
        trashed.rename(path)
        db.update_filepath(str(trashed), str(path))
        db.clear_pending_deletion(str(path))
        monkeypatch.setattr(db, "list_pending_deletions_older_than", lambda _cutoff: snapshot)

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
