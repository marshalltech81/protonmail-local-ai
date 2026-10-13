"""
Deletion reconciliation.

mbsync is configured ``Sync Pull`` + ``Expunge None``, which means a message
deleted on ProtonMail is never physically removed from the local Maildir.
Instead, mbsync renames the file to add the IMAP ``\\Deleted`` / Maildir ``T``
flag. Without reconciliation the local index keeps the message forever.

Each Maildir copy of a message is its own occurrence (#1566). A message
is live while it has a live occurrence: an active one that is not
``T``-flagged (PLAN.md decision 47). The reconciler provides a two-phase
path over that. It runs by default (mirror mode);
``INDEXER_DELETION_ENABLED=false`` opts out (archive mode):

1. **Tombstone**: a sweep (at startup and on the reconcile interval)
   first reconciles the occurrences with the Maildir
   (``occurrence_scan.scan_occurrences``: missed renames followed, found
   copies bound, and occurrences whose file a complete scan did not find
   retired; a partial scan retires nothing; ``sweep_paths`` runs the same
   scan, in archive mode too, before every Maildir walk). It then records a tombstone
   in ``pending_deletions`` for each occurrence of every message with no
   live occurrence left, and removes the tombstones of every message
   that has one again. Live ``on_moved`` detection does the same for the
   message whose file it saw renamed. No primary data is mutated: if
   mbsync un-flags a file on a later pull, the tombstones are cleared
   without data loss. Losing the last live occurrence is claimant-wide,
   across every account.

2. **Reap**: once a message's earliest tombstone is past the grace
   window the reaper deletes its ``message_thread_map`` row (its
   ``messages`` row, occurrences and tombstones cascade) and unmarks every
   path holding its bytes, and either rebuilds the parent thread from the
   surviving messages on disk or removes the thread entirely when no
   messages remain. The same transaction writes an identifier-only
   ``reaped_messages`` record per message so mcp-server can report a cited
   source as reaped; records expire after ``REAPED_RECORD_RETENTION_DAYS``.

A mass-delete brake skips a whole reap pass, reaping nothing, when more
messages are past the grace window than a fraction of the mailbox allows,
so a transient Bridge outage (vault rebuild, folder rename, auth glitch) that
causes mbsync to mark a large batch as ``T`` cannot silently wipe the index.
With more than one active account, a per-account brake also holds back the
messages of an account whose own count passes that fraction of its
messages (#1567).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .database import Database
from .embedder import EmbeddingBackend, scrub_embed_error
from .maildir import is_trashed
from .occurrence_scan import ScanCounts, scan_occurrences
from .parser import OversizedMessageError, parse_email
from .threader import Thread, Threader

log = logging.getLogger("indexer.reconciler")

# Floor of the mass-delete brake's threshold: a pass with more
# past-grace messages than ``max(this, max_batch_pct * total)`` reaps
# nothing. Without it, with a 5% default and a 10-message mailbox, a
# single deletion is 10% and would trip the brake. See ``reap()``.
_REAP_ABSOLUTE_FLOOR = 10

# Consecutive blocked passes before a thread crosses from "transient
# blip" into "stuck state" and earns a one-shot escalation log line.
# Routine cold-start / single-batch retries clear well within this
# window; threads still blocked after this many sweeps almost always
# indicate a misconfigured embedder, persistent parser bug, or stuck
# survivor file that needs operator attention rather than another
# silent retry.
_BLOCKED_ESCALATION_THRESHOLD = 3


def _log_scan(prefix: str, counts: ScanCounts) -> None:
    """The scan's WARNING when it was partial: it retired nothing."""
    if not counts.complete:
        log.warning(
            "%s: the Maildir scan was partial (%d unreadable director(ies)); "
            "no occurrence was retired, %d left unreachable",
            prefix,
            counts.unreadable,
            counts.unreachable,
        )


@dataclass(frozen=True)
class ReconcilerConfig:
    enabled: bool
    grace_days: int
    sweep_interval_secs: int
    max_batch_pct: float
    # True only in the one-run ``make force-deletion`` command
    # (``force_deletion.py``, #1609); no setting turns the brake off.
    force: bool


class Reconciler:
    """Owns tombstone + reap behavior for the indexer."""

    def __init__(
        self,
        db: Database,
        embedder: EmbeddingBackend,
        config: ReconcilerConfig,
        maildir_root: Path,
    ):
        self.db = db
        self.embedder = embedder
        self.config = config
        # The Maildir root the scan walks, and against which survivors are
        # re-parsed so nested folder paths (``Clients/ABC``) are preserved
        # during thread rebuild.
        self.maildir_root = maildir_root
        # File hashes of unindexed ``T``-flagged or dead-lettered files
        # the scan read, by path, size, mtime and inode, so each file is
        # read once per process (``occurrence_scan``).
        self._hash_cache: dict[tuple[str, int, int, int], str] = {}
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

    # -----------------------------------------------------------------
    # Tombstone detection
    # -----------------------------------------------------------------

    def sweep(self) -> dict:
        """Reconcile the occurrences with the Maildir, retiring the ones a
        complete scan did not find, then tombstone every message left with
        no live occurrence and clear the tombstones of every message that
        has one. Returns a small summary dict for logging/tests:
        ``renamed`` / ``retired`` / ``bound`` occurrences, ``tombstoned``
        messages newly marked, ``cleared`` messages unmarked,
        ``unreachable`` / ``unverified`` occurrences left as they were, and
        whether the scan was ``complete``."""
        before = self.db.count_tombstoned_messages()
        scan = scan_occurrences(self.db, self.maildir_root, hash_cache=self._hash_cache)
        self.db.clear_live_tombstones()
        # Counted across the scan too: a restoring rename it follows
        # clears the message's tombstones in the same write.
        cleared = max(0, before - self.db.count_tombstoned_messages())
        tombstoned = self.db.mark_lost_messages()
        counts = {
            "tombstoned": tombstoned,
            "cleared": cleared,
            "renamed": scan.renamed,
            "retired": scan.retired,
            "bound": scan.bound,
            "unreachable": scan.unreachable,
            "unverified": scan.unverified,
            "hashed": scan.hashed,
            "complete": scan.complete,
        }
        if any(v for k, v in counts.items() if k != "complete"):
            log.info(
                "reconciler sweep: tombstoned=%d cleared=%d renamed=%d retired=%d bound=%d "
                "unreachable=%d unverified=%d hashed=%d",
                tombstoned,
                cleared,
                scan.renamed,
                scan.retired,
                scan.bound,
                scan.unreachable,
                scan.unverified,
                scan.hashed,
            )
        _log_scan("reconciler sweep", scan)
        return counts

    def handle_moved(self, src_path: str, dest_path: str) -> None:
        """Live tombstone detection from watchdog ``on_moved`` events.

        The rename carries the file's occurrence (``update_filepath``),
        whose transaction clears the message's tombstones when it leaves
        it live (a restore). A message left with no live occurrence (the
        rename added ``T``) is tombstoned at once.
        """
        claimant_id = self.db.update_filepath(src_path, dest_path, move_occurrence=True)
        if claimant_id is None:
            entry = self.db.find_active_occurrence(dest_path)
            if entry is None:
                return
            claimant_id = entry["claimant_id"]
        if is_trashed(dest_path) and self.db.mark_lost_messages(claimant_id):
            log.info("tombstoned via on_moved: %s", dest_path)

    # -----------------------------------------------------------------
    # Reap
    # -----------------------------------------------------------------

    def reap(self) -> dict:
        """Remove the messages whose loss of their last live occurrence is
        older than the grace window.

        Groups them by thread so each thread is rebuilt at most once per
        pass. Skips the batch entirely when the mass-delete brake trips,
        unless ``config.force`` is set (only by ``make force-deletion``);
        with more than one active account, holds back the messages of an
        account whose own brake trips.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=self.config.grace_days)).isoformat()
        candidates = self.db.reapable_messages(cutoff)
        if not candidates:
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
            # messages per pass before gating. Counted per message
            # (claimant), however many occurrences each lost.
            max_allowed = max(
                _REAP_ABSOLUTE_FLOOR,
                int(total_messages * self.config.max_batch_pct),
            )
            if len(candidates) > max_allowed:
                log.error(
                    "reaper aborted: %d messages past grace window exceed "
                    "mass-delete threshold (max allowed %d = max(%d, %.1f%% "
                    "of %d total messages); run make force-deletion for one "
                    "pass past it)",
                    len(candidates),
                    max_allowed,
                    _REAP_ABSOLUTE_FLOOR,
                    self.config.max_batch_pct * 100,
                    total_messages,
                )
                return {
                    "threads_reaped": 0,
                    "threads_rebuilt": 0,
                    "aborted": True,
                    "tombstones_pending": len(candidates),
                }
            held = self._account_brake(candidates)
            candidates = [c for c in candidates if c["claimant_id"] not in held]

        grouped: dict[str, list] = {}
        for candidate in candidates:
            grouped.setdefault(candidate["thread_id"], []).append(candidate)

        # Other paths holding a reaped message's bytes are unmarked with
        # it (#1102); one lookup for the whole pass, since
        # ``indexed_files`` has no ``content_hash`` index.
        copies = self.db.paths_with_content([c["claimant_id"] for c in candidates])

        threads_reaped = 0
        threads_rebuilt = 0
        # Directory listings for the reap's restore check, shared by the
        # pass so each directory is listed at most once (#336).
        listings: dict[Path, dict[str, Path]] = {}
        for thread_id, dead in grouped.items():
            reaped, rebuilt = self._reap_thread(thread_id, dead, cutoff, copies, listings)
            threads_reaped += int(reaped)
            threads_rebuilt += int(rebuilt)

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
            # remain in a stuck state. Reported on the reconcile pass's
            # ``maintenance pass=reconcile`` line, so a stale deletion
            # cleanup shows in one line per pass, not only in per-thread
            # WARN/ERROR lines.
            "blocked_threads": blocked_count,
        }

    def _account_brake(self, candidates: list) -> set[str]:
        """The messages the per-account brake holds back this pass
        (#1567): computed only while more than one account has an active
        occurrence. A message belongs to every account holding an
        occurrence of it; an account whose past-grace messages outnumber
        ``max(_REAP_ABSOLUTE_FLOOR, max_batch_pct * its messages)`` keeps
        all of them, logged at ERROR with the counts, so a mass deletion
        in a small account is caught although the global count stays
        under the global brake. The global brake is unchanged."""
        if self.db.active_account_count() < 2:
            return set()
        per_account: dict[int, set[str]] = {}
        accounts = self.db.accounts_of([c["claimant_id"] for c in candidates])
        for claimant_id, account_ids in accounts.items():
            for account_id in account_ids:
                per_account.setdefault(account_id, set()).add(claimant_id)
        totals = self.db.messages_per_account()
        held: set[str] = set()
        for account_id, claimant_ids in sorted(per_account.items()):
            total = totals.get(account_id, 0)
            max_allowed = max(_REAP_ABSOLUTE_FLOOR, int(total * self.config.max_batch_pct))
            if len(claimant_ids) > max_allowed:
                log.error(
                    "reaper held back account %d: %d messages past grace window exceed "
                    "its mass-delete threshold (max allowed %d = max(%d, %.1f%% of %d "
                    "messages); run make force-deletion for one pass past it)",
                    account_id,
                    len(claimant_ids),
                    max_allowed,
                    _REAP_ABSOLUTE_FLOOR,
                    self.config.max_batch_pct * 100,
                    total,
                )
                held |= claimant_ids
        return held

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
        dead: list,
        cutoff: str,
        copies: Mapping[str, list[str]] | None = None,
        listings: dict[Path, dict[str, Path]] | None = None,
    ) -> tuple[bool, bool]:
        """Reap one thread. Returns (fully_reaped, rebuilt).

        ``dead`` is a snapshot of the thread's reapable messages; the
        database re-checks inside the reap transaction that each still has
        no live occurrence and a tombstone at or before ``cutoff``, since
        the watcher may restore (or restore and trash again) a message, or
        a sweep bind a copy, meanwhile, and that no file of it is back on
        disk outside the trash (``listings``, the pass's directory cache).
        ``copies`` maps a message to the other indexed paths holding its
        bytes, unmarked with it."""
        copies = copies or {}
        dead_ids = {d["claimant_id"] for d in dead}
        copy_paths = [p for d in dead for p in copies.get(d["claimant_id"], [])]
        all_rows = self.db.get_thread_messages(thread_id)
        survivor_rows = [r for r in all_rows if r["claimant_id"] not in dead_ids]

        if not survivor_rows:
            # Whole thread gone. Drop everything; the .eml files stay on
            # disk because the indexer never deletes Maildir files.
            if not self.db.delete_thread_completely(
                thread_id, grace_cutoff=cutoff, copy_paths=copy_paths, listings=listings
            ):
                log.info(
                    "reaper: a thread changed since its tombstones were read; retrying next pass",
                )
                return False, False
            log.info("reaped a thread (%d messages)", len(dead))
            self._clear_blocked(thread_id)
            return True, False

        # Thread has survivors — parse them from disk and rebuild.
        survivors: list = []
        for row in survivor_rows:
            if row["filepath"] is None:
                # No active occurrence: its file is gone (it is losing its
                # last copy too, within the grace window). It cannot be
                # re-parsed, so the thread waits for it.
                attempts = self._record_blocked(thread_id, "(no active occurrence)")
                log.warning(
                    "reaper: a survivor has no file to re-read; "
                    "skipping this reap pass (blocked attempts=%d)",
                    attempts,
                )
                return False, False
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
                # module cannot decompose, a body / attachment walker
                # error not caught locally, etc.) the
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
            self.db.keep_persisted_first_indexed_at(msg)
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
            # The thread vector is the mean of the survivors' chunk
            # vectors: ``None`` here, and the reap transaction below
            # derives it from the thread's chunk-vector sum once the
            # reaped messages' chunks are subtracted (#1356), so no chunk
            # vector is read for it. This only checks, by chunk ID, that
            # a chunk survives. Falls back to embedding the subject line
            # in the rare case that none does (e.g. all bodies empty).
            embedding: list[float] | None = None
            if not self.db.thread_has_chunks_outside(thread_id, sorted(dead_ids)):
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
        # each reaped message's rows inside a single BEGIN IMMEDIATE, so a
        # crash mid-reap cannot leave the thread row and the map
        # disagreeing about which messages belong.
        removed = self.db.reap_thread_messages(
            rebuilt_thread,
            embedding,
            sorted(dead_ids),
            grace_cutoff=cutoff,
            copy_paths=copy_paths,
            listings=listings,
        )
        if removed is None:
            log.info(
                "reaper: a thread changed since its tombstones were read; retrying next pass",
            )
            return False, False
        log.info(
            "rebuilt a thread: removed %d message(s), %d survive",
            len(removed),
            len(survivors),
        )
        self._clear_blocked(thread_id)
        return False, True

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _total_messages(self) -> int:
        return self.db.count_total_messages()


def sweep_paths(db: Database, *, maildir_root: Path) -> dict[str, Any]:
    """Reconcile the occurrences with the Maildir
    (``occurrence_scan.scan_occurrences``) without tombstoning any message:
    follow the renames mbsync made while the watcher missed them (a flag
    change such as ``S`` → ``SR``, a ``new`` → ``cur`` promotion), once the
    file's bytes are verified, bind byte-identical copies the index
    records, and retire an occurrence whose file a complete scan did not
    find (a partial scan retires nothing). Safe on every indexer startup
    and before every periodic walk, in archive mode too: retiring removes
    a locator, never a message, and only ``Reconciler.sweep()`` tombstones.
    A rename that leaves a tombstoned message live (it dropped the ``T``
    flag) clears its tombstones in the same write, the rule
    ``Reconciler.handle_moved`` applies live (#860), and every other
    message with a live occurrence has its stale tombstones cleared too.

    Recording the renames before the walk keeps it from re-indexing them
    as new mail. Returns a summary dict so the caller can log how much
    drift there was (useful when diagnosing "new mail shows up in search
    late" on mailboxes where the indexer restarts often).
    """
    before = db.count_tombstoned_messages()
    scan = scan_occurrences(db, maildir_root)
    db.clear_live_tombstones()
    # Counted across the scan too: a restoring rename it follows clears
    # the message's tombstones in the same write.
    tombstones_cleared = max(0, before - db.count_tombstoned_messages())
    if (
        scan.renamed
        or scan.retired
        or scan.unreachable
        or scan.bound
        or scan.unverified
        or tombstones_cleared
    ):
        log.info(
            "startup rename sweep: renamed=%d retired=%d unreachable=%d tombstones_cleared=%d "
            "bound=%d unverified=%d",
            scan.renamed,
            scan.retired,
            scan.unreachable,
            tombstones_cleared,
            scan.bound,
            scan.unverified,
        )
    _log_scan("startup rename sweep", scan)
    return {
        "renamed": scan.renamed,
        "retired": scan.retired,
        "unreachable": scan.unreachable,
        "tombstones_cleared": tombstones_cleared,
        "bound": scan.bound,
        "unverified": scan.unverified,
        "complete": scan.complete,
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
    the default, and a non-integer, an integer below its minimum, or a
    batch fraction that is not a finite number in [0, 1] raises
    ``ValueError`` naming the variable.

    ``force`` is always ``False`` here: forcing the brake is the one-run
    ``make force-deletion`` command (#1609), never a setting.
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
        force=False,
    )
