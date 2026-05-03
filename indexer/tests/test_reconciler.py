"""
Tests for src/reconciler.py — tombstone detection, live on_moved handling,
reap behavior (full-thread and rebuild paths), grace window, mass-delete
brake, and Ollama-failure backoff.

The reconciler is exercised end-to-end against a real SQLite database and
real .eml files in tmp_path. Embedding is stubbed with FakeEmbedder so the
tests do not require Ollama.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest
from src.database import (
    EMBEDDING_DIM,  # noqa: F401  -- via reuse
    Database,
)
from src.reconciler import Reconciler, ReconcilerConfig, load_config_from_env, sweep_paths
from src.threader import Threader

FAKE_EMBEDDING = [0.0] * EMBEDDING_DIM


class FakeEmbedder:
    def __init__(self):
        self.calls = 0
        self.should_fail = False

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        if self.should_fail:
            raise RuntimeError("simulated Ollama outage")
        return FAKE_EMBEDDING


def _default_config(**overrides) -> ReconcilerConfig:
    base = {
        "enabled": True,
        "grace_days": 0,
        "sweep_interval_secs": 60,
        "max_batch_pct": 1.0,
        "force": False,
        "unlink_on_reap": False,
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
def reconciler(db: Database, embedder: FakeEmbedder, threader: Threader) -> Reconciler:
    return Reconciler(db, embedder, threader, _default_config())


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
        assert db.count_pending_deletions() == 1

    def test_marks_missing_file_as_tombstone(self, db, threader, reconciler, maildir):
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "m5@example.com")
        _index(path, db, threader)
        path.unlink()

        result = reconciler.sweep()
        assert result["missing"] == 1


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

    def test_does_not_tombstone_missing_files(self, db, threader, maildir):
        """sweep_paths is the always-on variant; a missing file must be
        counted as unreachable but NOT recorded in pending_deletions.
        The opt-in Reconciler.sweep() is still responsible for
        tombstoning when the operator has enabled deletion
        reconciliation."""
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "sp2@example.com")
        _index(path, db, threader)
        path.unlink()

        result = sweep_paths(db)
        assert result["unreachable"] == 1
        assert db.count_pending_deletions() == 0

    def test_does_not_tombstone_t_flagged_files(self, db, threader, maildir):
        """A trashed file is reachable at its new path, so sweep_paths
        updates the filepath and keeps the row alive. Tombstoning of
        T-flagged files stays opt-in via the full Reconciler."""
        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "sp3@example.com")
        _index(path, db, threader)

        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)

        result = sweep_paths(db)
        assert result["renamed"] == 1
        assert db.count_pending_deletions() == 0

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
        assert db.count_pending_deletions() == 0


# ---------------------------------------------------------------------------
# Reap — grace window, full-reap, rebuild paths
# ---------------------------------------------------------------------------


class TestReap:
    def test_does_not_reap_inside_grace_window(self, db, threader, embedder, maildir):
        cfg = _default_config(grace_days=7)
        rec = Reconciler(db, embedder, threader, cfg)

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
        assert db.count_pending_deletions() == 0

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

    def test_unlinks_files_when_unlink_on_reap_enabled(self, db, threader, embedder, maildir):
        cfg = _default_config(unlink_on_reap=True)
        rec = Reconciler(db, embedder, threader, cfg)

        path = maildir / "1700000000.M1.host:2,S"
        _write_eml(path, "u1@example.com")
        _index(path, db, threader)
        trashed = maildir / "1700000000.M1.host:2,ST"
        path.rename(trashed)
        rec.sweep()
        assert trashed.exists()
        rec.reap()
        assert trashed.exists() is False

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
            message_id="co1@example.com",
            thread_id=thread_id,
            chunks=[orig_chunk],
            embeddings_by_chunk_id={orig_chunk.chunk_id: [0.1] * EMBEDDING_DIM},
        )
        db.replace_message_chunks(
            message_id="co2@example.com",
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
        assert db.get_chunk_ids_for_message("co1@example.com") == set()
        orig_vec = db._conn.execute(
            "SELECT COUNT(*) FROM message_chunks_vec WHERE chunk_id = ?", (orig_chunk.chunk_id,)
        ).fetchone()[0]
        assert orig_vec == 0

        # Survivor's chunk is preserved.
        assert db.get_chunk_ids_for_message("co2@example.com") == {reply_chunk.chunk_id}

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
        rec = Reconciler(db, embedder, threader, cfg)
        rec.sweep()

        # Age all tombstones so they are past the grace window
        db._conn.execute("UPDATE pending_deletions SET marked_at = '2000-01-01T00:00:00+00:00'")
        db._conn.commit()

        result = rec.reap()
        assert result["aborted"] is True
        # Index was not touched
        assert db.count_total_messages() == 30
        assert db.count_pending_deletions() == 20

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
        rec = Reconciler(db, embedder, threader, cfg)
        rec.sweep()
        db._conn.execute("UPDATE pending_deletions SET marked_at = '2000-01-01T00:00:00+00:00'")
        db._conn.commit()

        result = rec.reap()
        assert result["aborted"] is False
        assert db.count_total_messages() == 4  # 10 - 6 reaped

    def test_force_overrides_brake(self, db, threader, embedder, maildir):
        paths = self._stage_batch(maildir, db, threader, 30)
        for p in paths[:20]:
            t = p.with_name(p.name + "T")
            p.rename(t)

        cfg = _default_config(grace_days=0, max_batch_pct=0.05, force=True)
        rec = Reconciler(db, embedder, threader, cfg)
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
    def test_defaults_disabled(self):
        cfg = load_config_from_env({})
        assert cfg.enabled is False
        assert cfg.grace_days == 7
        assert cfg.sweep_interval_secs == 3600
        assert cfg.max_batch_pct == pytest.approx(0.05)
        assert cfg.force is False
        assert cfg.unlink_on_reap is False

    def test_enabled_parses_truthy_values(self):
        for val in ("1", "true", "TRUE", "yes", "on"):
            cfg = load_config_from_env({"INDEXER_DELETION_ENABLED": val})
            assert cfg.enabled is True, val

    def test_invalid_numeric_falls_back_to_default(self):
        cfg = load_config_from_env({"INDEXER_DELETION_GRACE_DAYS": "not-a-number"})
        assert cfg.grace_days == 7

    def test_max_batch_pct_clamped_to_unit_range(self):
        cfg = load_config_from_env({"INDEXER_DELETION_MAX_BATCH_PCT": "2.5"})
        assert cfg.max_batch_pct == 1.0
        cfg = load_config_from_env({"INDEXER_DELETION_MAX_BATCH_PCT": "-0.1"})
        assert cfg.max_batch_pct == 0.0

    def test_sweep_interval_has_minimum(self):
        cfg = load_config_from_env({"INDEXER_DELETION_SWEEP_INTERVAL_SECS": "5"})
        assert cfg.sweep_interval_secs == 60
