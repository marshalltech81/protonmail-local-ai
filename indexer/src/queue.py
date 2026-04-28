"""
Durable indexing queue backed by the ``indexing_jobs`` SQLite table.

Before the indexing_jobs queue, the indexer processed files inline inside the
watchdog callback: any crash mid-embed or Ollama outage left the file
unindexed with no durable record of the failure, and a parser bug on a
single message was silently retried on every restart without ever
giving up. The queue makes both cases observable and bounded:

- Every discovered filepath (watchdog event, initial scan, future
  reconciler-driven reindex) is ``enqueue``d instead of processed
  inline. Enqueue is fast — a single SQLite write — so the watchdog
  callback thread no longer blocks on an Ollama round-trip.
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

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_BACKOFF_SECONDS = 30
# Cap the backoff so a long-dead row doesn't compute an impractical
# next_attempt_at that overflows or schedules decades in the future.
_MAX_BACKOFF_SECONDS = 6 * 60 * 60  # 6 hours


def load_config_from_env(env: dict[str, str] | os._Environ) -> dict[str, int]:
    """Parse queue config from a dict-like env mapping.

    Exposed as a helper so ``main.py`` and tests share the same parsing
    rather than each re-implementing int coercion with the same default.
    """
    return {
        "max_attempts": int(env.get("INDEXER_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)),
        "base_backoff_seconds": int(
            env.get("INDEXER_RETRY_BASE_SECONDS", DEFAULT_BASE_BACKOFF_SECONDS)
        ),
    }


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

    def mark_succeeded(self, filepath: str) -> None:
        """Remove the job row. Indexing ran through cleanly."""
        self.db.queue_delete(filepath)

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
