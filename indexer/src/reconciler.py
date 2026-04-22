"""
Deletion reconciliation.

mbsync is configured ``Sync Pull`` + ``Expunge None``, which means a message
deleted on ProtonMail is never physically removed from the local Maildir.
Instead, mbsync renames the file to add the IMAP ``\\Deleted`` / Maildir ``T``
flag. Without reconciliation the local index keeps the message forever.

The reconciler provides a two-phase, opt-in path:

1. **Tombstone**: a startup sweep plus live ``on_moved`` detection record every
   ``T``-flagged file into the ``pending_deletions`` table. No primary data is
   mutated — if mbsync un-flags the file on a later pull, the tombstone is
   cleared without data loss.

2. **Reap**: after a configurable grace window the reaper deletes the
   message's ``message_thread_map`` / ``indexed_files`` rows, and either
   rebuilds the parent thread from the surviving messages on disk or removes
   the thread entirely when no messages remain.

A mass-delete brake caps how many messages the reaper may touch in one pass,
so a transient Bridge outage (vault rebuild, folder rename, auth glitch) that
causes mbsync to mark a large batch as ``T`` cannot silently wipe the index.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .database import Database
from .embedder import Embedder
from .maildir import is_trashed, resolve_current_path
from .parser import parse_email
from .threader import Thread, Threader

log = logging.getLogger("indexer.reconciler")


@dataclass(frozen=True)
class ReconcilerConfig:
    enabled: bool
    grace_days: int
    sweep_interval_secs: int
    max_batch_pct: float
    force: bool
    unlink_on_reap: bool


class Reconciler:
    """Owns tombstone + reap behavior for the indexer."""

    def __init__(
        self,
        db: Database,
        embedder: Embedder,
        threader: Threader,
        config: ReconcilerConfig,
        maildir_root: Path | None = None,
    ):
        self.db = db
        self.embedder = embedder
        self.threader = threader
        self.config = config
        # Passed through to parse_email when re-reading survivors so nested
        # folder paths (``Clients/ABC``) are preserved during thread rebuild.
        self.maildir_root = maildir_root

    # -----------------------------------------------------------------
    # Tombstone detection
    # -----------------------------------------------------------------

    def sweep(self) -> dict:
        """Walk every indexed file, update filepaths after flag renames, and
        record tombstones for ``T``-flagged files. Returns a small summary
        dict for logging/tests.
        """
        tombstoned = 0
        cleared = 0
        renamed = 0
        missing = 0

        for row in self.db.iter_message_map():
            stored = Path(row["filepath"])
            current = resolve_current_path(stored)

            if current is None:
                # File fully gone — under Expunge None this is unexpected, but
                # treat it as a tombstone so the index can heal. The reaper
                # will still wait out the grace window before acting.
                if self.db.add_pending_deletion(
                    row["filepath"], row["message_id"], row["thread_id"]
                ):
                    missing += 1
                continue

            if str(current) != row["filepath"]:
                # mbsync renamed the file for a non-deletion flag change
                # (e.g. S → SR). Keep the stored path aligned.
                self.db.update_filepath(row["filepath"], str(current))
                renamed += 1

            current_filepath = str(current)
            if is_trashed(current):
                if self.db.add_pending_deletion(
                    current_filepath, row["message_id"], row["thread_id"]
                ):
                    tombstoned += 1
            elif self.db.has_pending_deletion(current_filepath):
                # mbsync reversed the T flag before the grace window expired —
                # the message is alive again, clear the tombstone.
                self.db.clear_pending_deletion(current_filepath)
                cleared += 1

        if tombstoned or cleared or renamed or missing:
            log.info(
                "reconciler sweep: tombstoned=%d cleared=%d renamed=%d missing=%d",
                tombstoned,
                cleared,
                renamed,
                missing,
            )
        return {
            "tombstoned": tombstoned,
            "cleared": cleared,
            "renamed": renamed,
            "missing": missing,
        }

    def handle_moved(self, src_path: str, dest_path: str) -> None:
        """Live tombstone detection from watchdog ``on_moved`` events."""
        entry = self._find_entry_for_path(src_path) or self._find_entry_for_path(dest_path)
        if entry is None:
            return
        if entry["filepath"] != dest_path:
            self.db.update_filepath(entry["filepath"], dest_path)
        if is_trashed(dest_path):
            self.db.add_pending_deletion(dest_path, entry["message_id"], entry["thread_id"])
            log.info("tombstoned via on_moved: %s", dest_path)
        elif self.db.has_pending_deletion(dest_path):
            self.db.clear_pending_deletion(dest_path)
            log.info("cleared tombstone via on_moved: %s", dest_path)

    def _find_entry_for_path(self, path: str):
        return self.db.find_message_entry_by_filepath(path)

    # -----------------------------------------------------------------
    # Reap
    # -----------------------------------------------------------------

    def reap(self) -> dict:
        """Process tombstones older than the grace window.

        Groups tombstones by thread so each thread is rebuilt at most once per
        pass. Skips the batch entirely when the mass-delete brake trips,
        unless ``INDEXER_DELETION_FORCE=true`` is set.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=self.config.grace_days)).isoformat()
        tombstones = self.db.list_pending_deletions_older_than(cutoff)
        if not tombstones:
            return {"threads_reaped": 0, "threads_rebuilt": 0, "aborted": False}

        total_messages = self._total_messages()
        if total_messages > 0 and not self.config.force:
            pct = len(tombstones) / total_messages
            if pct > self.config.max_batch_pct:
                log.error(
                    "reaper aborted: %d tombstones past grace window exceed "
                    "mass-delete threshold %.1f%% of %d total messages "
                    "(set INDEXER_DELETION_FORCE=true to override)",
                    len(tombstones),
                    self.config.max_batch_pct * 100,
                    total_messages,
                )
                return {
                    "threads_reaped": 0,
                    "threads_rebuilt": 0,
                    "aborted": True,
                    "tombstones_pending": len(tombstones),
                }

        grouped: dict[str, list] = {}
        for tomb in tombstones:
            grouped.setdefault(tomb["thread_id"], []).append(tomb)

        threads_reaped = 0
        threads_rebuilt = 0

        for thread_id, tombs in grouped.items():
            reaped, rebuilt = self._reap_thread(thread_id, tombs)
            threads_reaped += int(reaped)
            threads_rebuilt += int(rebuilt)

        if threads_reaped or threads_rebuilt:
            log.info(
                "reconciler reap: threads_reaped=%d threads_rebuilt=%d",
                threads_reaped,
                threads_rebuilt,
            )
        return {
            "threads_reaped": threads_reaped,
            "threads_rebuilt": threads_rebuilt,
            "aborted": False,
        }

    def _reap_thread(self, thread_id: str, tombs: list) -> tuple[bool, bool]:
        """Reap one thread. Returns (fully_reaped, rebuilt)."""
        dead_filepaths = {t["filepath"] for t in tombs}
        all_rows = self.db.get_thread_messages(thread_id)
        survivor_rows = [r for r in all_rows if r["filepath"] not in dead_filepaths]

        if not survivor_rows:
            # Whole thread gone. Drop everything, then optionally unlink files.
            self.db.delete_thread_completely(thread_id)
            if self.config.unlink_on_reap:
                for fp in dead_filepaths:
                    self._safe_unlink(fp)
            log.info("reaped thread %s (%d messages)", thread_id, len(tombs))
            return True, False

        # Thread has survivors — parse them from disk and rebuild.
        survivors: list = []
        for row in survivor_rows:
            msg = parse_email(Path(row["filepath"]), maildir_root=self.maildir_root)
            if msg is None:
                # Survivor unparseable; skip it from the rebuild but do not
                # delete the DB row. A later sweep can pick it up again.
                log.warning(
                    "reaper: could not re-parse survivor %s in thread %s; skipping this reap pass",
                    row["filepath"],
                    thread_id,
                )
                return False, False
            survivors.append(msg)

        if not survivors:
            return False, False

        survivors.sort(key=lambda m: m.date)
        existing = self.db.get_thread(thread_id)
        subject = existing.subject if existing else survivors[0].subject
        folder = existing.folder if existing else survivors[0].folder
        rebuilt_thread = Thread(
            thread_id=thread_id,
            subject=subject,
            participants=Threader._participants(survivors),
            messages=survivors,
            folder=folder,
            date_first=survivors[0].date,
            date_last=survivors[-1].date,
        )

        try:
            embedding = self.embedder.embed(rebuilt_thread.text_for_embedding())
        except Exception as e:
            # Ollama unavailable or embedding failed — leave state untouched
            # and retry on the next sweep rather than committing partial work.
            log.warning(
                "reaper: embedding failed for thread %s (%s); will retry next pass",
                thread_id,
                e,
            )
            return False, False

        # Atomic reap: rewrite the thread row with survivors + tear down
        # each reaped message's message_thread_map / indexed_files /
        # pending_deletions rows inside a single BEGIN IMMEDIATE. Prior
        # code used rebuild_thread + a per-message remove_message loop —
        # separate transactions, so a crash mid-reap could leave the
        # thread row and the map disagreeing about which messages belong.
        removed_filepaths = self.db.reap_thread_messages(
            rebuilt_thread,
            embedding,
            [tomb["message_id"] for tomb in tombs],
        )
        if self.config.unlink_on_reap:
            for fp in removed_filepaths:
                self._safe_unlink(fp)
        log.info(
            "rebuilt thread %s: removed %d message(s), %d survive",
            thread_id,
            len(tombs),
            len(survivors),
        )
        return False, True

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _total_messages(self) -> int:
        return self.db.count_total_messages()

    def _safe_unlink(self, path: str) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            log.warning("reaper: failed to unlink %s: %s", path, e)


def load_config_from_env(env: dict[str, str]) -> ReconcilerConfig:
    """Parse reconciler knobs from environment variables.

    All settings are opt-in: with defaults, ``enabled`` is ``False`` and no
    tombstoning or reaping occurs.
    """

    def _bool(name: str, default: bool) -> bool:
        raw = env.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def _int(name: str, default: int, minimum: int = 0) -> int:
        raw = env.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            value = int(raw)
        except ValueError:
            log.warning("invalid %s=%r; falling back to %d", name, raw, default)
            return default
        return max(minimum, value)

    def _pct(name: str, default: float) -> float:
        raw = env.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            value = float(raw)
        except ValueError:
            log.warning("invalid %s=%r; falling back to %.2f", name, raw, default)
            return default
        if value < 0:
            return 0.0
        if value > 1:
            return 1.0
        return value

    return ReconcilerConfig(
        enabled=_bool("INDEXER_DELETION_ENABLED", False),
        grace_days=_int("INDEXER_DELETION_GRACE_DAYS", 7, minimum=0),
        sweep_interval_secs=_int("INDEXER_DELETION_SWEEP_INTERVAL_SECS", 3600, minimum=60),
        max_batch_pct=_pct("INDEXER_DELETION_MAX_BATCH_PCT", 0.05),
        force=_bool("INDEXER_DELETION_FORCE", False),
        unlink_on_reap=_bool("INDEXER_UNLINK_ON_REAP", False),
    )
