"""
Deletion reconciliation.

mbsync is configured ``Sync Pull`` + ``Expunge None``, which means a message
deleted on ProtonMail is never physically removed from the local Maildir.
Instead, mbsync renames the file to add the IMAP ``\\Deleted`` / Maildir ``T``
flag. Without reconciliation the local index keeps the message forever.

The reconciler provides a two-phase path. It runs by default (mirror
mode); ``INDEXER_DELETION_ENABLED=false`` opts out (archive mode):

1. **Tombstone**: a startup sweep plus live ``on_moved`` detection record every
   ``T``-flagged file into the ``pending_deletions`` table. No primary data is
   mutated — if mbsync un-flags the file on a later pull, the tombstone is
   cleared without data loss.

2. **Reap**: after a configurable grace window the reaper deletes the
   message's ``message_thread_map`` / ``indexed_files`` rows, and either
   rebuilds the parent thread from the surviving messages on disk or removes
   the thread entirely when no messages remain. The same transaction
   writes an identifier-only ``reaped_messages`` record per message so
   mcp-server can report a cited source as reaped; records
   expire after ``REAPED_RECORD_RETENTION_DAYS``.

A mass-delete brake caps how many messages the reaper may touch in one pass,
so a transient Bridge outage (vault rebuild, folder rename, auth glitch) that
causes mbsync to mark a large batch as ``T`` cannot silently wipe the index.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .chunker import mean_vector
from .database import Database
from .embedder import EmbeddingBackend, scrub_embed_error
from .maildir import is_trashed, resolve_current_path
from .parser import OversizedMessageError, _derive_folder, parse_email
from .threader import Thread, Threader

log = logging.getLogger("indexer.reconciler")

# Absolute lower bound on how many tombstones the reaper will process in
# one pass even when ``max_batch_pct`` is the binding constraint. With a
# 5% default and a 10-message mailbox, a single deletion is 10% and
# would incorrectly trip the mass-delete brake. See ``reap()``.
_REAP_ABSOLUTE_FLOOR = 10

# Consecutive blocked passes before a thread crosses from "transient
# blip" into "stuck state" and earns a one-shot escalation log line.
# Routine cold-start / single-batch retries clear well within this
# window; threads still blocked after this many sweeps almost always
# indicate a misconfigured embedder, persistent parser bug, or stuck
# survivor file that needs operator attention rather than another
# silent retry.
_BLOCKED_ESCALATION_THRESHOLD = 3

# Directory listings the pre-reap live-copy check may spend in one reap
# pass (#1102). Each tombstoned message with identical copies has them
# resolved through a listing of its own, since a shared one can predate
# mbsync restoring a copy; a check reserves ``_LISTINGS_PER_CANDIDATE``
# per candidate before it runs and is charged what it used, so a pass
# never exceeds the budget. Messages past it are left for the next pass;
# the checked ones are reaped, so every pass makes progress. A listing
# of a large folder costs a few ms.
_LIVE_COPY_RECHECK_LISTINGS = 256
# ``resolve_current_path`` lists at most the file's directory and its
# ``new`` / ``cur`` sibling.
_LISTINGS_PER_CANDIDATE = 3


def _is_live(filepath: str | None, listings: dict[Path, dict[str, Path]]) -> bool:
    """True when the message file at ``filepath`` (or its flag-renamed
    successor) exists and is not trashed. ``listings`` is the caller's
    per-pass directory cache for ``resolve_current_path``."""
    if filepath is None:
        return False
    current = resolve_current_path(Path(filepath), listings)
    return current is not None and not is_trashed(current)


def _remap_to_identical_copies(
    db: Database,
    moves: list[tuple[Any, str, bool]],
    listings: dict[Path, dict[str, Path]],
    maildir_root: Path | None,
) -> dict[str, Path]:
    """Remap messages to a byte-identical copy still on disk, and return
    ``claimant_id -> copy`` for those remapped (#1102).

    ``moves`` holds ``(map row, path it maps to now, live_only)``: a row
    whose file is gone takes any copy, a row whose file is ``T``-flagged
    only a live one (moving it to another trashed copy changes nothing).

    Byte-identical files share one claimant ID and the mapping holds only
    one of their paths, while ``indexed_files`` holds every path. One
    lookup covers all of ``moves``. A live copy is preferred to a trashed
    one, so a trashed copy that sorts first cannot get the message reaped
    while a live copy remains. The remap moves the locator, folder and
    S/F/R state; it never tombstones or clears anything, so each caller
    applies its own trash rule to the copy.

    When the pass's directory cache shows no copy, or the database
    refuses a remap because the copy vanished after it was resolved (the
    watcher renamed it meanwhile), the copies are resolved once more
    through a fresh listing shared by the whole retry phase, and the
    remap retried once: the pass's cache can be stale for that folder.
    """
    if not moves:
        return {}
    copies = db.find_identical_copies([row["claimant_id"] for row, _, _ in moves])
    remapped: dict[str, Path] = {}
    # The retry phase's own listing: fresh, built on first use, shared
    # by every message so a folder is listed once more per pass at most.
    fresh: dict[Path, dict[str, Path]] = {}
    for row, from_path, live_only in moves:
        candidates = copies.get(row["claimant_id"], [])
        for cache in (listings, fresh):
            copy = _pick_copy(candidates, cache, live_only=live_only)
            if copy is None:
                # The pass's listing can predate a copy coming back.
                continue
            dest_folder = _derive_folder(copy, maildir_root)
            same_folder = dest_folder == _derive_folder(Path(from_path), maildir_root)
            if db.remap_to_identical_copy(
                from_path, str(copy), folder=None if same_folder else dest_folder
            ):
                remapped[row["claimant_id"]] = copy
                break
    return remapped


def _pick_copy(
    candidates: list[str], listings: dict[Path, dict[str, Path]], *, live_only: bool
) -> Path | None:
    """The current path of the first live candidate, else (unless
    ``live_only``) of the first trashed one; ``None`` when none fits."""
    found = [
        current
        for path in candidates
        if (current := resolve_current_path(Path(path), listings)) is not None
    ]
    live = next((p for p in found if not is_trashed(p)), None)
    if live is not None or live_only:
        return live
    return found[0] if found else None


@dataclass(frozen=True)
class ReconcilerConfig:
    enabled: bool
    grace_days: int
    sweep_interval_secs: int
    max_batch_pct: float
    force: bool


class Reconciler:
    """Owns tombstone + reap behavior for the indexer."""

    def __init__(
        self,
        db: Database,
        embedder: EmbeddingBackend,
        config: ReconcilerConfig,
        maildir_root: Path | None = None,
    ):
        self.db = db
        self.embedder = embedder
        self.config = config
        # Passed through to parse_email when re-reading survivors so nested
        # folder paths (``Clients/ABC``) are preserved during thread rebuild.
        self.maildir_root = maildir_root
        # Threads currently stuck in the reap path because a survivor
        # cannot be parsed (corrupt MIME header, runtime bug in the
        # parser, oversized survivor file). Counter increments each
        # pass the thread fails to reap, resets when the thread
        # successfully reaps. Surfaced through ``reap()``'s return
        # dict and via the per-pass WARN at each call site (plus the
        # one-shot escalation WARN when the counter crosses
        # ``_BLOCKED_ESCALATION_THRESHOLD``) — operator-visible only
        # through the indexer's own logs. NOT plumbed into the
        # mcp-server's ``get_mailbox_status``: that surface runs in a
        # separate process and reads SQLite stats, with no IPC back
        # to this counter. In-memory only — resets on indexer restart,
        # which is the right shape for a counter that signals
        # "something's blocked right now" rather than long-term retry
        # accounting.
        self._blocked_thread_attempts: dict[str, int] = {}
        # Threads for which the one-shot escalation log has already
        # fired. Kept separate from ``_blocked_thread_attempts`` because
        # the per-pass WARN at each call site already fires every
        # sweep; this latch ensures the higher-severity "this is now
        # a stuck state" message is emitted exactly once per stuck
        # episode rather than spamming on every retry. Clears together
        # with the attempt counter when the thread reaps successfully.
        self._escalated_threads: set[str] = set()
        # The live-copy check's per-pass budget use and what it left
        # (``_check_live_copies``); reset at the start of each ``reap()``.
        self._recheck_listings = 0
        self._left_unchecked = 0
        self._reaped_unchecked = 0

    # -----------------------------------------------------------------
    # Tombstone detection
    # -----------------------------------------------------------------

    def sweep(self) -> dict:
        """Walk every indexed file, update filepaths after flag renames, and
        record tombstones for ``T``-flagged files. Returns a small summary
        dict for logging/tests.

        A message whose mapped file is gone or ``T``-flagged is first
        matched against the other indexed paths with the same bytes
        (#1102): byte-identical files share one claimant ID, and the
        mapping holds only one of their paths. When such a copy still
        exists (for a trashed file, a live one) the message is remapped to
        it and the usual trash rule applies to the copy; only a message
        with no such copy is tombstoned.
        """
        counts = {"tombstoned": 0, "cleared": 0, "renamed": 0, "missing": 0, "remapped": 0}

        listings: dict[Path, dict[str, Path]] = {}
        gone = []
        trashed: list[tuple[Any, Path]] = []
        for row in self.db.iter_message_map():
            stored = Path(row["filepath"])
            current = resolve_current_path(stored, listings)

            if current is None:
                # Settled after the walk, with one lookup for all of them.
                gone.append(row)
                continue

            if str(current) != row["filepath"]:
                # mbsync renamed the file for a non-deletion flag change
                # (e.g. S → SR). Keep the stored path aligned.
                self.db.update_filepath(row["filepath"], str(current))
                counts["renamed"] += 1

            if is_trashed(current):
                # Settled after the walk too: a live copy keeps it.
                trashed.append((row, current))
                continue
            self._apply_trash_rule(row, current, counts)

        remapped = _remap_to_identical_copies(
            self.db,
            [(row, row["filepath"], False) for row in gone]
            + [(row, str(current), True) for row, current in trashed],
            listings,
            self.maildir_root,
        )
        counts["remapped"] = len(remapped)
        for row, current in trashed:
            self._apply_trash_rule(row, remapped.get(row["claimant_id"], current), counts)
        for row in gone:
            copy = remapped.get(row["claimant_id"])
            if copy is not None:
                self._apply_trash_rule(row, copy, counts)
                continue
            # File fully gone — under Expunge None this is unexpected, but
            # treat it as a tombstone so the index can heal. The reaper
            # will still wait out the grace window before acting.
            if self.db.add_pending_deletion(row["filepath"], row["claimant_id"], row["thread_id"]):
                counts["missing"] += 1

        if any(counts.values()):
            log.info(
                "reconciler sweep: tombstoned=%d cleared=%d renamed=%d missing=%d remapped=%d",
                counts["tombstoned"],
                counts["cleared"],
                counts["renamed"],
                counts["missing"],
                counts["remapped"],
            )
        return counts

    def _apply_trash_rule(self, row, current: Path, counts: dict[str, int]) -> None:
        """Tombstone ``current`` when it is ``T``-flagged; clear its
        tombstone when mbsync reversed the flag within the grace window."""
        current_filepath = str(current)
        if is_trashed(current):
            if self.db.add_pending_deletion(current_filepath, row["claimant_id"], row["thread_id"]):
                counts["tombstoned"] += 1
        elif self.db.has_pending_deletion(current_filepath):
            # mbsync reversed the T flag before the grace window expired —
            # the message is alive again, clear the tombstone.
            self.db.clear_pending_deletion(current_filepath)
            counts["cleared"] += 1

    def handle_moved(self, src_path: str, dest_path: str, *, folder: str | None = None) -> None:
        """Live tombstone detection from watchdog ``on_moved`` events.

        ``folder`` is forwarded to ``update_filepath`` for a rename that
        crosses Maildir folders.
        """
        entry = self._find_entry_for_path(src_path) or self._find_entry_for_path(dest_path)
        if entry is None:
            return
        if entry["filepath"] != dest_path:
            # A restore clears the tombstone in the same write as the
            # rename: the reaper, on the main thread, must never see the
            # live path with an eligible tombstone still attached.
            self.db.update_filepath(
                entry["filepath"],
                dest_path,
                folder=folder,
                clear_tombstone=not is_trashed(dest_path),
            )
        if is_trashed(dest_path):
            self.db.add_pending_deletion(dest_path, entry["claimant_id"], entry["thread_id"])
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

        # A tombstone claims the message was trashed or went missing when
        # it was written. Check the file the message maps to now: if it
        # exists outside the trash, the claim is stale (a move away and
        # back during a sweep, or a tombstone left under a dead path by
        # the #301 race), so clear it rather than reap. This runs before
        # the brake so stale rows, which no sweep revisits, cannot hold it
        # shut, and shares one listings cache so each folder is listed
        # once per pass rather than once per tombstone.
        listings: dict[Path, dict[str, Path]] = {}
        eligible = []
        for tomb in tombstones:
            if _is_live(tomb["mapped_filepath"], listings):
                self.db.clear_pending_deletion(tomb["filepath"])
            else:
                eligible.append(tomb)
        tombstones = eligible
        if not tombstones:
            return {"threads_reaped": 0, "threads_rebuilt": 0, "aborted": False}

        total_messages = self._total_messages()
        if total_messages > 0 and not self.config.force:
            # Small mailboxes need an absolute floor on the brake. With
            # the default ``max_batch_pct=0.05`` a 10-message mailbox
            # would trip the brake on a single deletion (10%), which is
            # the expected steady-state cadence of a small mailbox. The
            # brake's purpose is to catch a Bridge-outage-induced mass
            # tombstoning event, not to block routine single-message
            # cleanup — so allow at least ``_REAP_ABSOLUTE_FLOOR``
            # tombstones per pass before gating.
            max_allowed = max(
                _REAP_ABSOLUTE_FLOOR,
                int(total_messages * self.config.max_batch_pct),
            )
            if len(tombstones) > max_allowed:
                log.error(
                    "reaper aborted: %d tombstones past grace window exceed "
                    "mass-delete threshold (max allowed %d = max(%d, %.1f%% "
                    "of %d total messages); set INDEXER_DELETION_FORCE=true "
                    "to override)",
                    len(tombstones),
                    max_allowed,
                    _REAP_ABSOLUTE_FLOOR,
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

        # Other paths holding a reaped message's bytes are unmarked with
        # it (#1102); one lookup for the whole pass, since
        # ``indexed_files`` has no ``content_hash`` index.
        copies = self.db.find_identical_copies([t["claimant_id"] for t in tombstones])

        threads_reaped = 0
        threads_rebuilt = 0
        # The live-copy check's budget and what it left, for this pass.
        self._recheck_listings = 0
        self._left_unchecked = 0
        self._reaped_unchecked = 0

        for thread_id, tombs in grouped.items():
            reaped, rebuilt = self._reap_thread(thread_id, tombs, cutoff, copies)
            threads_reaped += int(reaped)
            threads_rebuilt += int(rebuilt)

        if self._left_unchecked or self._reaped_unchecked:
            log.warning(
                "reaper: the live-copy re-check budget (%d directory listings) is "
                "spent; %d message(s) left for the next pass, %d whose own file is "
                "gone reaped unchecked",
                _LIVE_COPY_RECHECK_LISTINGS,
                self._left_unchecked,
                self._reaped_unchecked,
            )

        if threads_reaped or threads_rebuilt:
            log.info(
                "reconciler reap: threads_reaped=%d threads_rebuilt=%d",
                threads_reaped,
                threads_rebuilt,
            )
        blocked_count = len(self._blocked_thread_attempts)
        if blocked_count:
            log.warning(
                "reconciler reap: %d thread(s) blocked from reaping "
                "(corrupt survivor / embed outage / parser regression); "
                "see prior reaper lines for the affected survivor files",
                blocked_count,
            )
        return {
            "threads_reaped": threads_reaped,
            "threads_rebuilt": threads_rebuilt,
            "aborted": False,
            # Number of threads that failed to reap on this pass and
            # remain in a stuck state. Surfaced so a stale deletion
            # cleanup is visible in operator health checks instead of
            # buried in per-pass WARN/ERROR log lines.
            "blocked_threads": blocked_count,
        }

    def _record_blocked(self, thread_id: str, survivor_path: str) -> int:
        """Bump the blocked-attempts counter for ``thread_id`` and return it.

        Crossing ``_BLOCKED_ESCALATION_THRESHOLD`` for the first time
        emits a one-shot WARN distinct from the per-pass log lines at
        each call site, so operators can grep for the escalation
        signal without scanning every routine retry. The latch in
        ``_escalated_threads`` prevents the message from firing again
        until the thread reaps successfully.
        """
        attempts = self._blocked_thread_attempts.get(thread_id, 0) + 1
        self._blocked_thread_attempts[thread_id] = attempts
        if attempts >= _BLOCKED_ESCALATION_THRESHOLD and thread_id not in self._escalated_threads:
            log.warning(
                "reconciler: the thread of survivor %s has been blocked from "
                "reaping for %d consecutive passes; this is no longer a "
                "transient retry. Check embedder availability, parser errors, "
                "or that survivor file. Sweeps will continue, but this message "
                "will not repeat until the thread reaps cleanly.",
                # The survivor's mbsync-generated path, never the thread ID
                # (the root Message-ID) (#257).
                survivor_path,
                attempts,
            )
            self._escalated_threads.add(thread_id)
        return attempts

    def _clear_blocked(self, thread_id: str) -> None:
        """Drop the blocked-attempts entry for a thread that reaped cleanly."""
        self._blocked_thread_attempts.pop(thread_id, None)
        self._escalated_threads.discard(thread_id)

    def _reap_thread(
        self,
        thread_id: str,
        tombs: list,
        cutoff: str,
        copies: Mapping[str, list[str]] | None = None,
    ) -> tuple[bool, bool]:
        """Reap one thread. Returns (fully_reaped, rebuilt).

        ``copies`` maps a claimant to the other indexed paths holding its
        bytes. They are checked on disk first (``_check_live_copies``):
        a message with a live copy is kept, one left unchecked by the
        pass's budget stays a survivor for the next pass, and only the
        rest are reaped, with their copies unmarked in the reap
        transaction.

        ``tombs`` is a snapshot; the database re-checks inside the reap
        transaction that each message is still tombstoned at or before
        ``cutoff``, since the watcher may restore (or restore and trash
        again) a message meanwhile."""
        # Survivors are chosen by claimant ID, which removal also uses:
        # the watcher can rename a tombstoned file (a flag change) after
        # ``tombs`` was read, and a stale snapshot path would let the
        # deleted message be rebuilt into the thread as a survivor.
        copies = copies or {}
        checked = len(tombs)
        tombs, kept, left = self._check_live_copies(tombs, copies)
        if kept:
            # Its reap is cancelled, so the count it held no longer applies.
            self._clear_blocked(thread_id)
        if not tombs:
            return False, False
        dead_ids = {t["claimant_id"] for t in tombs}
        copy_paths = [p for t in tombs for p in copies.get(t["claimant_id"], [])]
        all_rows = self.db.get_thread_messages(thread_id)
        survivor_rows = [r for r in all_rows if r["claimant_id"] not in dead_ids]

        if not survivor_rows:
            # Whole thread gone. Drop everything; the .eml files stay on
            # disk because the indexer never deletes Maildir files.
            if not self.db.delete_thread_completely(
                thread_id, grace_cutoff=cutoff, copy_paths=copy_paths
            ):
                log.info(
                    "reaper: a thread changed since its tombstones were read; retrying next pass",
                )
                return False, False
            log.info("reaped a thread (%d messages)", len(tombs))
            self._clear_blocked(thread_id)
            return True, False

        # Thread has survivors — parse them from disk and rebuild.
        survivors: list = []
        for row in survivor_rows:
            try:
                msg = parse_email(Path(row["filepath"]), maildir_root=self.maildir_root)
            except (OSError, OversizedMessageError) as e:
                # Foreseeable parse failures the parser is documented to
                # raise: ``OSError`` covers the mbsync chmod / rename /
                # perms races; ``OversizedMessageError`` fires when the
                # survivor exceeds ``INDEXER_PARSE_MAX_BYTES``. Skip the
                # pass; a later sweep retries (the underlying condition
                # is typically transient or operator-actionable).
                attempts = self._record_blocked(thread_id, row["filepath"])
                log.warning(
                    "reaper: could not read survivor %s "
                    "(%s); skipping this reap pass (blocked attempts=%d)",
                    row["filepath"],
                    # Type only: the message can quote mail (#257).
                    type(e).__name__,
                    attempts,
                )
                return False, False
            except Exception as e:
                # Content-pathology errors (malformed MIME the email
                # module cannot decompose, html2text blowups, etc.) the
                # parser deliberately propagates instead of swallowing.
                # The indexer's main worker routes those through the
                # queue's retry + dead-letter cascade; the reaper has
                # no equivalent, so skipping the pass with a loud log
                # is the closest equivalent — better than crashing
                # ``reap()`` and stalling every other thread's
                # tombstones behind a single corrupt survivor. The
                # blocked-attempts counter on the Reconciler instance
                # surfaces these stuck threads to operators via
                # ``reap()``'s return dict so deterministic failures
                # (which retry forever under this catch) are not just
                # buried log lines.
                attempts = self._record_blocked(thread_id, row["filepath"])
                log.error(
                    "reaper: parse_email raised %s on survivor %s; "
                    "skipping this reap pass (blocked attempts=%d)",
                    type(e).__name__,
                    row["filepath"],
                    attempts,
                )
                return False, False
            if msg is None:
                # Survivor unparseable; skip it from the rebuild but do not
                # delete the DB row. A later sweep can pick it up again.
                attempts = self._record_blocked(thread_id, row["filepath"])
                log.warning(
                    "reaper: could not re-parse survivor %s; "
                    "skipping this reap pass (blocked attempts=%d)",
                    row["filepath"],
                    attempts,
                )
                return False, False
            self.db.keep_persisted_fallback_date(msg)
            survivors.append(msg)

        # Note: ``survivors`` is always non-empty here. ``survivor_rows``
        # was checked non-empty above; every parse-failure path in the
        # loop body early-returns, so we cannot exit the loop with an
        # empty list.
        survivors.sort(key=lambda m: m.effective_date)
        existing = self.db.get_thread(thread_id)
        subject = existing.subject if existing else survivors[0].subject
        folder = existing.folder if existing else survivors[0].folder
        rebuilt_thread = Thread(
            thread_id=thread_id,
            subject=subject,
            participants=Threader._participants(survivors),
            messages=survivors,
            folder=folder,
            date_first=survivors[0].effective_date,
            date_last=survivors[-1].effective_date,
        )

        try:
            # Recompute the thread vector as the mean of the survivors'
            # chunk embeddings. Reading chunks for survivor claimant IDs
            # excludes the reaped messages even though their chunk rows
            # are still on disk at this point — the reap transaction
            # below tears them down atomically. Falls back to embedding
            # the subject line in the rare case that no survivor has
            # any indexed chunks (e.g. all bodies empty).
            survivor_claimant_ids = [r["claimant_id"] for r in survivor_rows]
            survivor_chunks = self.db.get_chunk_embeddings_for_messages(survivor_claimant_ids)
            if survivor_chunks:
                embedding = mean_vector(survivor_chunks)
            else:
                # Fallback: the oldest SURVIVOR with a non-empty
                # original-case subject (``Re:``/``Fwd:`` intact, which
                # differs from ``rebuilt_thread.subject`` only by case /
                # prefix-stripping, both noise for retrieval); the
                # sentinel ``(empty thread)`` when no survivor has one.
                # The stored ``display_subject`` is deliberately not
                # consulted: it can still hold a reaped message's
                # subject, and when every survivor's subject is empty a
                # non-empty stored value can only have come from one.
                fallback = next(
                    (s for m in survivors if (s := (m.subject or "").strip())),
                    "(empty thread)",
                )
                embedding = self.embedder.embed(fallback)
        except Exception as e:
            # Embedding service unavailable or embedding failed — leave state untouched
            # and retry on the next sweep rather than committing partial work.
            attempts = self._record_blocked(thread_id, survivor_rows[0]["filepath"])
            log.warning(
                "reaper: embedding failed for the thread of survivor %s (%s); "
                "will retry next pass (blocked attempts=%d)",
                survivor_rows[0]["filepath"],
                # A provider status error can echo the input (a subject).
                scrub_embed_error(e),
                attempts,
            )
            return False, False

        # Atomic reap: rewrite the thread row with survivors + tear down
        # each reaped message's message_thread_map / indexed_files /
        # pending_deletions rows inside a single BEGIN IMMEDIATE, so a
        # crash mid-reap cannot leave the thread row and the map
        # disagreeing about which messages belong.
        removed_filepaths = self.db.reap_thread_messages(
            rebuilt_thread,
            embedding,
            [tomb["claimant_id"] for tomb in tombs],
            grace_cutoff=cutoff,
            copy_paths=copy_paths,
        )
        if removed_filepaths is None:
            log.info(
                "reaper: a message in a thread was restored since its tombstones "
                "were read; retrying next pass",
            )
            return False, False
        log.info(
            "rebuilt a thread: removed %d message(s), %d survive",
            len(tombs),
            len(survivors),
        )
        if left:
            log.info(
                "reaper: reaped %d of %d tombstoned message(s) in a thread; "
                "%d left for the next pass",
                len(tombs),
                checked,
                left,
            )
        self._clear_blocked(thread_id)
        return False, True

    def _check_live_copies(
        self, tombs: list, copies: Mapping[str, list[str]]
    ) -> tuple[list, int, int]:
        """Check each tombstoned message's identical copies on disk and
        return ``(to_reap, kept, left)`` (#1102).

        The sweep found no live copy, but mbsync can restore one (drop its
        ``T`` flag) before the reap, and that copy is not the mapped path,
        so the watcher clears nothing for it. A message with a live copy
        is remapped to it, its tombstone cleared, and it stays a survivor;
        ``kept`` counts those. A remap the database refuses (the mapping
        moved meanwhile) keeps the message too, for the next pass.

        Each message's copies are resolved through a listing of its own,
        charged to the pass's budget (``_LIVE_COPY_RECHECK_LISTINGS``).
        Past the budget a message is left unchecked: it stays tombstoned
        and a survivor of this pass's rebuild, whole, and the next pass
        checks it (``left`` counts those). A message whose own file is gone
        cannot be re-parsed as a survivor, so it is checked first and,
        past the budget, reaped unchecked; its copies are unmarked with
        it, so one that is live again is re-indexed by the next walk.
        """
        to_reap: list = []
        kept = 0
        left = 0
        # Gone files first: they are the ones that cannot wait.
        ordered = sorted(tombs, key=lambda t: Path(t["mapped_filepath"] or "").exists())
        for tomb in ordered:
            candidates = copies.get(tomb["claimant_id"], [])
            if not candidates:
                to_reap.append(tomb)
                continue
            reserve = _LISTINGS_PER_CANDIDATE * len(candidates)
            if self._recheck_listings + reserve > _LIVE_COPY_RECHECK_LISTINGS:
                if Path(tomb["mapped_filepath"] or "").exists():
                    left += 1
                    self._left_unchecked += 1
                else:
                    to_reap.append(tomb)
                    self._reaped_unchecked += 1
                continue
            fresh: dict[Path, dict[str, Path]] = {}
            copy = _pick_copy(candidates, fresh, live_only=True)
            self._recheck_listings += len(fresh)
            if copy is None:
                to_reap.append(tomb)
                continue
            kept += 1
            # Set: ``copies`` only holds claimants that are mapped.
            mapped = tomb["mapped_filepath"]
            dest_folder = _derive_folder(copy, self.maildir_root)
            same_folder = dest_folder == _derive_folder(Path(mapped), self.maildir_root)
            if self.db.remap_to_identical_copy(
                mapped, str(copy), folder=None if same_folder else dest_folder
            ) and self.db.has_pending_deletion(str(copy)):
                self.db.clear_pending_deletion(str(copy))
        if kept:
            log.info(
                "reaper: kept %d message(s) with a live identical copy restored since the sweep",
                kept,
            )
        return to_reap, kept, left

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _total_messages(self) -> int:
        return self.db.count_total_messages()


def sweep_paths(db: Database, *, maildir_root: Path | None = None) -> dict:
    """Walk every indexed file and update the stored filepath when mbsync
    has renamed it in place (e.g. a flag-only rename such as ``S`` →
    ``SR``, or a ``new`` → ``cur`` promotion). Intended to be safe to
    run on every indexer startup — unlike ``Reconciler.sweep()`` it does
    NOT tombstone missing files, so it is safe under archive mode too,
    while still healing path drift that accumulated while the indexer
    was offline. A tombstone follows its file through the rename, except
    that a rename which dropped the ``T`` flag restores the message, so
    the tombstone is cleared with it (the rule ``Reconciler.handle_moved``
    applies live): in archive mode no reconciler would clear it later, and
    a tombstone a mirror-mode run left would otherwise report the restored
    message as pending deletion for ever (#860).

    A message whose file is gone is remapped to a byte-identical copy
    still on disk, as ``Reconciler.sweep`` does (#1102), so archive mode
    does not keep the gone path, folder and flags for ever; a remap to a
    live copy clears the message's tombstone like a restore.
    ``maildir_root`` names the Maildir root for the copy's folder.

    Returns a summary dict so the caller can log how much drift there
    was (useful when diagnosing "new mail shows up in search late" on
    mailboxes where the indexer restarts often).
    """
    renamed = 0
    tombstones_cleared = 0

    listings: dict[Path, dict[str, Path]] = {}
    gone = []
    for row in db.iter_message_map():
        stored = Path(row["filepath"])
        current = resolve_current_path(stored, listings)

        if current is None:
            # File is no longer at any of the expected Maildir paths:
            # settled after the walk, with one copy lookup for all.
            gone.append(row)
            continue

        if str(current) != row["filepath"]:
            restored = not is_trashed(current)
            if restored and db.has_pending_deletion(row["filepath"]):
                tombstones_cleared += 1
            db.update_filepath(row["filepath"], str(current), clear_tombstone=restored)
            renamed += 1

    remapped = _remap_to_identical_copies(
        db, [(row, row["filepath"], False) for row in gone], listings, maildir_root
    )
    for copy in remapped.values():
        if not is_trashed(copy) and db.has_pending_deletion(str(copy)):
            db.clear_pending_deletion(str(copy))
            tombstones_cleared += 1
    # A full sweep (with reconciliation enabled) would tombstone the
    # rest; this lightweight variant just counts the miss so the
    # operator can see the signal in logs.
    unreachable = len(gone) - len(remapped)

    if renamed or unreachable or remapped:
        log.info(
            "startup rename sweep: renamed=%d unreachable=%d tombstones_cleared=%d remapped=%d",
            renamed,
            unreachable,
            tombstones_cleared,
            len(remapped),
        )
    return {
        "renamed": renamed,
        "unreachable": unreachable,
        "tombstones_cleared": tombstones_cleared,
        "remapped": len(remapped),
    }


def load_config_from_env(env: Mapping[str, str]) -> ReconcilerConfig:
    """Parse reconciler knobs from environment variables.

    ``INDEXER_DELETION_ENABLED`` selects the retention mode. Unset or
    empty means mirror (``enabled`` is ``True``): mail deleted upstream
    is tombstoned and reaped from the local index after the grace
    window. ``false`` selects archive: the index is append-only and
    keeps upstream-deleted mail. Any other value raises ``ValueError``
    so a typo fails startup instead of silently picking a mode.

    The other knobs follow the same rule (#481): unset or empty yields
    the default, and an unrecognised boolean, a non-integer, an integer
    below its minimum, or a batch fraction that is not a finite number
    in [0, 1] raises ``ValueError`` naming the variable.
    """

    _truthy = {"1", "true", "yes", "on"}
    _falsy = {"0", "false", "no", "off"}

    def _mode(name: str, default: bool) -> bool:
        raw = (env.get(name) or "").strip().lower()
        if not raw:
            return default
        if raw in _truthy:
            return True
        if raw in _falsy:
            return False
        raise ValueError(
            f"{name}={raw!r} is not recognized; use true (mirror: reap mail "
            "deleted upstream) or false (archive: keep it locally)"
        )

    def _bool(name: str, default: bool) -> bool:
        raw = (env.get(name) or "").strip().lower()
        if not raw:
            return default
        if raw in _truthy:
            return True
        if raw in _falsy:
            return False
        raise ValueError(f"{name}={raw!r} is not recognized; use true or false")

    def _int(name: str, default: int, minimum: int = 0) -> int:
        raw = (env.get(name) or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(f"{name}={raw!r} is not an integer") from None
        if value < minimum:
            raise ValueError(f"{name}={raw!r} must be >= {minimum}")
        return value

    def _pct(name: str, default: float) -> float:
        raw = (env.get(name) or "").strip()
        if not raw:
            return default
        message = f"{name}={raw!r} must be a number between 0 and 1"
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(message) from None
        # NaN fails every comparison, so test the accepted range rather
        # than the rejected one: a NaN brake used to make every reap
        # sweep raise and reaping silently never ran. This also rejects
        # infinity.
        if not (0.0 <= value <= 1.0):
            raise ValueError(message)
        return value

    return ReconcilerConfig(
        enabled=_mode("INDEXER_DELETION_ENABLED", True),
        grace_days=_int("INDEXER_DELETION_GRACE_DAYS", 7, minimum=0),
        sweep_interval_secs=_int("INDEXER_DELETION_SWEEP_INTERVAL_SECS", 3600, minimum=60),
        max_batch_pct=_pct("INDEXER_DELETION_MAX_BATCH_PCT", 0.05),
        force=_bool("INDEXER_DELETION_FORCE", False),
    )
