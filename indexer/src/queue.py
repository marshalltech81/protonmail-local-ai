"""
Durable indexing queue backed by the ``indexing_jobs`` SQLite table.

Before the indexing_jobs queue, the indexer processed files inline inside the
watchdog callback: any crash mid-embed or embedding service outage left the file
unindexed with no durable record of the failure, and a parser bug on a
single message was silently retried on every restart without ever
giving up. The queue makes both cases observable and bounded:

- Every discovered filepath (watchdog event, initial scan, future
  reconciler-driven reindex) is ``enqueue``d instead of processed
  inline. Enqueue is fast — a single SQLite write — so the watchdog
  callback thread no longer blocks on an embedding service round-trip.
- A worker loop in the main thread calls ``claim_next`` and runs the
  existing parse → thread → embed → upsert pipeline against the
  returned path. Success deletes the row (``mark_succeeded``); failure
  records the error, increments ``attempts``, and schedules an
  exponential backoff. When ``attempts`` reaches
  ``max_attempts`` the row transitions to status ``dead`` — stays in
  the table for visibility, stops being claimed.

Idempotency:

- Re-enqueuing a path that is already ``queued`` / ``failed`` / ``dead``
  resets attempts to 0 and schedules it immediately. A newly-arrived
  watchdog event represents fresh intent to index the file; prior
  dead-letter status should not permanently block that.
- ``mark_succeeded`` on a missing row is a no-op rather than an error,
  so the worker can safely invoke it after a retry cycle that succeeded
  through a different code path.

Single-worker invariant:

- ``claim_next`` does not transition the row to ``in_progress``; the
  worker holds "currently processing X" state in memory. If the worker
  crashes mid-process, the row stays ``queued`` and the next restart
  picks it up — its ``attempts`` counter is unchanged, which is the
  correct semantics (a crash is not an attempt that exercised the
  parse/embed/write pipeline).
- Scaling to multiple workers would require an explicit claim column
  (pid + heartbeat) and row-level locking; that's out of scope.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta

log = logging.getLogger("indexer.queue")

STATUS_QUEUED = "queued"
STATUS_DEAD = "dead"

# Reasons are free-form tags logged for observability; enumerating them
# here keeps the set reviewable without forcing a CHECK constraint (new
# reasons can appear with behavior changes without a schema bump).
REASON_ON_CREATED = "on_created"
REASON_ON_MOVED = "on_moved"
REASON_INITIAL_SCAN = "initial_scan"
REASON_RECOVERY = "recovery"

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_BACKOFF_SECONDS = 30
# Cap the backoff so a long-dead row doesn't compute an impractical
# next_attempt_at that overflows or schedules decades in the future.
_MAX_BACKOFF_SECONDS = 6 * 60 * 60  # 6 hours


def load_config_from_env(env: dict[str, str] | os._Environ) -> dict[str, int]:
    """Parse queue config from a dict-like env mapping.

    Exposed as a helper so ``main.py`` and tests share the same parsing
    rather than each re-implementing int coercion with the same default.

    Malformed or out-of-range values fall back to the documented default
    with a warning rather than raising at startup — matches the lenient
    parser shape used by ``reconciler.load_config_from_env`` and
    ``main._int_env`` so a typo in any one knob doesn't crash the
    indexer. Both knobs are clamped to a minimum of 1:

    - ``max_attempts <= 0`` would dead-letter every row on the first
      failure (the >= check in ``mark_failed`` matches immediately),
      neutralizing the retry contract.
    - ``base_backoff_seconds <= 0`` schedules ``next_attempt_at`` in the
      past (or at "now"), causing immediate retry churn that consumes
      the attempt budget in a tight loop until dead-letter.
    """
    return {
        "max_attempts": _int_env(env, "INDEXER_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, minimum=1),
        "base_backoff_seconds": _int_env(
            env,
            "INDEXER_RETRY_BASE_SECONDS",
            DEFAULT_BASE_BACKOFF_SECONDS,
            minimum=1,
        ),
    }


def _int_env(
    env: dict[str, str] | os._Environ,
    name: str,
    default: int,
    *,
    minimum: int | None = None,
) -> int:
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("invalid %s=%r; falling back to %d", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        log.warning(
            "invalid %s=%r (must be >= %d); falling back to %d",
            name,
            value,
            minimum,
            default,
        )
        return default
    return value


class IndexingQueue:
    """Thin wrapper around ``indexing_jobs`` with retry / backoff logic.

    All methods are safe to call on the same ``Database`` concurrently
    with other writers; the underlying ``Database._synchronized`` lock
    wraps every public method that touches SQLite, so queue writes
    serialize cleanly with ``upsert_thread`` / reconciler writes.
    """

    def __init__(
        self,
        db,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_backoff_seconds: int = DEFAULT_BASE_BACKOFF_SECONDS,
    ):
        self.db = db
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds

    # ----- writes --------------------------------------------------------

    def enqueue(self, filepath: str, reason: str) -> None:
        """Queue ``filepath`` for indexing, resetting any prior state.

        A newly-observed event (watchdog rename, initial-scan discovery)
        is fresh intent — even if a previous attempt marked the row
        ``dead``, the user or mbsync just re-surfaced the file, and we
        should retry from a clean state. ``INSERT OR REPLACE`` is the
        minimum write that expresses that semantics without a manual
        SELECT / UPDATE / INSERT dance.
        """
        now_iso = _now_iso()
        self.db.queue_enqueue(
            filepath=filepath,
            reason=reason,
            status=STATUS_QUEUED,
            now_iso=now_iso,
        )

    def claim_next(self) -> sqlite3.Row | None:
        """Return the oldest due ``queued`` job, or ``None`` when the
        queue is empty or every queued row is still in backoff.

        The row is returned unchanged — the caller holds the
        "currently processing" state in memory. On worker crash the row
        stays claimable and attempts_count is untouched, which is the
        correct behavior: a crash does not count as an attempt that
        exercised the parse/embed/write pipeline.
        """
        return self.db.queue_claim_next(STATUS_QUEUED, _now_iso())

    def claim_batch(self, limit: int) -> list[sqlite3.Row]:
        """Return up to ``limit`` distinct oldest-due queued rows.

        Like ``claim_next`` but fetches a snapshot of N rows in one
        query — so the batched initial indexer's gather phase can pick
        up distinct messages without re-claiming the same row before
        marking it succeeded. Rows stay in 'queued' state; the caller
        must mark each one (succeeded / failed / skipped) by the end of
        the batch or they will be returned again on the next call.
        """
        return self.db.queue_fetch_due_batch(STATUS_QUEUED, _now_iso(), limit)

    def mark_succeeded(self, filepath: str) -> None:
        """Remove the job row. Indexing ran through cleanly."""
        self.db.queue_delete(filepath)

    def mark_skipped(self, filepath: str, *, reason: str) -> None:
        """Drop a queue row that cannot be retried AND whose path is gone.

        Distinct from ``mark_succeeded`` (the file was NOT indexed) and
        from ``mark_failed`` (no retry, no dead-letter, no attempts
        bump). Used for terminal non-error conditions like
        ``FileNotFoundError`` at the parse stage — the file moved
        between enqueue and read (typically mbsync renaming to add an
        IMAP flag suffix), so the path is permanently invalid and the
        renamed file will be re-enqueued via a fresh ``IN_MOVED_TO``
        event. Burning retry budget on the original path is wasted
        work and clutters the dead-letter set with non-bug entries.

        For terminal failures where the file IS still present on disk
        (e.g. oversized), use ``mark_dead_terminal`` instead so the
        initial scan's ``is_dead`` gate prevents re-enqueue on every
        container restart.

        Logs at INFO because this is normal Maildir lifecycle
        behavior, not an indexer fault.
        """
        self.db.queue_delete(filepath)
        log.info("skipped: %s reason=%s", filepath, reason)

    def mark_dead_terminal(self, filepath: str, *, stage: str, error: str) -> None:
        """Move a row directly to ``dead`` for a non-retryable failure.

        Distinct from ``mark_failed`` (which schedules a retry until
        ``max_attempts``) and ``mark_skipped`` (which deletes the row).
        Used when the file is still present on disk and the failure is
        terminal under current config — e.g. oversized files. Deleting
        the row would let ``initial_index`` re-enqueue the same file on
        every container restart (the standard walk has no other way to
        know the file is unindexable); the ``is_dead`` gate skips
        dead-lettered rows so a persistent oversized file is recorded
        once and ignored thereafter. The row is also visible in
        ``queue.stats()['dead']`` for operator observability.
        """
        self.db.queue_mark_dead(
            filepath=filepath,
            attempts=1,
            last_stage=stage,
            last_error=error,
            now_iso=_now_iso(),
        )
        log.warning(
            "terminal: %s stage=%s error=%s",
            filepath,
            stage,
            _truncate_error(error),
        )

    def mark_failed(self, filepath: str, *, stage: str, error: str) -> None:
        """Record a failed attempt and schedule the next retry or dead-letter.

        ``stage`` is one of ``"parse" | "thread" | "embed" | "db_write"``
        — useful for operators to see which pipeline step broke. The
        value is stored verbatim so new stages can be introduced without
        migrating existing rows.
        """
        attempts_before = self.db.queue_get_attempts(filepath)
        if attempts_before is None:
            # Worker crashed after claim but before we could record a
            # failure, and somebody already cleaned the row up. Nothing
            # durable to update. Log and move on.
            log.warning("mark_failed: no queue row for %s; nothing to update", filepath)
            return
        new_attempts = attempts_before + 1
        if new_attempts >= self.max_attempts:
            self.db.queue_mark_dead(
                filepath=filepath,
                attempts=new_attempts,
                last_stage=stage,
                last_error=error,
                now_iso=_now_iso(),
            )
            log.error(
                "dead-letter: %s after %d attempts at stage=%s error=%s",
                filepath,
                new_attempts,
                stage,
                _truncate_error(error),
            )
            return
        backoff_seconds = min(
            self.base_backoff_seconds * (2**attempts_before),
            _MAX_BACKOFF_SECONDS,
        )
        next_attempt = datetime.now(UTC) + timedelta(seconds=backoff_seconds)
        self.db.queue_mark_failed(
            filepath=filepath,
            attempts=new_attempts,
            last_stage=stage,
            last_error=error,
            now_iso=_now_iso(),
            next_attempt_iso=next_attempt.isoformat(),
        )
        log.warning(
            "retry: %s attempt %d/%d at stage=%s next_in=%ds error=%s",
            filepath,
            new_attempts,
            self.max_attempts,
            stage,
            backoff_seconds,
            _truncate_error(error),
        )

    # ----- reads ---------------------------------------------------------

    def is_dead(self, filepath: str) -> bool:
        """True when ``filepath`` has a row at status=``dead``.

        Used by the initial scan to skip dead-lettered files instead
        of clobbering them via ``enqueue``'s ``INSERT OR REPLACE``.
        Without this skip, a container restart re-enqueues every
        previously-failed file, resets its retry counter to zero, and
        starts the 5-attempt backoff cascade over again — wasting
        ~30 minutes of embedding service load per file with no change in
        upstream behavior. The watchdog rename / on-created events
        keep going through ``enqueue`` (and DO reset dead state)
        because those signal real change in the underlying file.
        """
        return self.db.queue_get_status(filepath) == STATUS_DEAD

    def has_pending_row(self, filepath: str) -> bool:
        """True when ``filepath`` has a row in 'queued' state.

        ``mark_failed`` keeps a row at status=``queued`` with a
        future ``next_attempt_at`` (the retry cascade is in-band
        with the queue, not a separate state), so 'queued' covers
        both fresh enqueues and the active retry tail. Used by the
        recovery sweep to avoid clobbering a row that the normal
        retry path is already handling.
        """
        return self.db.queue_get_status(filepath) == STATUS_QUEUED

    def stats(self) -> dict[str, int]:
        """Return ``{'queued': n, 'dead': n}`` — surfaced through the
        indexer health file / MCP ``get_index_status`` so operators can
        see when work is backing up or files are giving up."""
        return self.db.queue_stats()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _truncate_error(error: str, limit: int = 200) -> str:
    """Cap log output for stack traces. The full text is already in the
    ``last_error`` column for post-mortem; logs just need a hint."""
    if len(error) <= limit:
        return error
    return error[:limit] + "…"
