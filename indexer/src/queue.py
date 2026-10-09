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
- A worker loop in the main thread claims batches (``claim_batch``) and runs the
  existing parse → thread → embed → upsert pipeline against the
  returned path. Success deletes the row (``mark_succeeded``); failure
  records the error, increments ``attempts``, and schedules an
  exponential backoff. When ``attempts`` reaches
  ``max_attempts`` the row transitions to status ``dead`` — stays in
  the table for visibility, stops being claimed.
- Infrastructure failures (embedder down or misconfigured) are not
  the message's fault: ``defer`` postpones the row without touching
  ``attempts``, so no outage can dead-letter mail. Every failure
  records a ``last_error_class`` (see ``ERROR_CLASSES``), and
  ``requeue_dead`` is the operator path back out of ``dead``.

Idempotency:

- Re-enqueuing a path that is already ``queued`` or ``dead``
  resets attempts to 0 and schedules it immediately. A newly-arrived
  watchdog event represents fresh intent to index the file; prior
  dead-letter status should not permanently block that.
- ``mark_succeeded`` on a missing row is a no-op rather than an error,
  so the worker can safely invoke it after a retry cycle that succeeded
  through a different code path.

Single-worker invariant:

- Claiming does not transition the row to ``in_progress``; the
  worker holds "currently processing X" state in memory. If the worker
  crashes mid-process, the row stays ``queued`` and the next restart
  picks it up.
- The one message whose parse or extraction step is running carries
  one attempt while it runs (``begin_attempt``), refunded when the step
  returns or its outcome is recorded. A process that dies mid-step (OOM
  kill, a re-raised ``MemoryError``, the stall guard) leaves that
  message charged and no other: a message that keeps killing the
  worker reaches ``dead`` instead of looping forever, while an ordinary
  restart costs at most one attempt to one message.
- Scaling to multiple workers would require an explicit claim column
  (pid + heartbeat) and row-level locking; that's out of scope.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from .extractors import warn_rate_limited

# The per-message ``terminal:``, ``retry:`` and ``dead-letter:`` lines
# share the indexer's rate limit (#1320): crafted mail can trigger them
# once per delivered message. Withheld lines are counted in the queue
# heartbeat's ``suppressed_lines``; the rows record every transition.
log = logging.getLogger("indexer.queue")

STATUS_QUEUED = "queued"
STATUS_DEAD = "dead"

# Why a job last failed, persisted in ``indexing_jobs.last_error_class``
# so a dead row explains itself and ``requeue_dead`` can target a class.
#
# * retryable — may succeed on a later attempt; consumes the attempt
#   budget (``mark_failed``) and dead-letters once it is exhausted.
# * permanent_source_failure — this source can never be indexed under
#   the current config (oversized, no Message-ID, input the embedder
#   rejects); dead-lettered immediately (``mark_dead_terminal``).
# * operator_action_required — the pipeline itself is misconfigured
#   (embedder rejects credentials or model). Says nothing about the
#   message, so it is deferred, never dead-lettered (``defer``).
#
# Infrastructure outages are deferred with ``retryable`` and likewise
# never consume attempts: a transient outage must not turn into a
# permanent omission from the index.
ERROR_CLASS_RETRYABLE = "retryable"
ERROR_CLASS_PERMANENT = "permanent_source_failure"
ERROR_CLASS_OPERATOR = "operator_action_required"
ERROR_CLASSES = (ERROR_CLASS_RETRYABLE, ERROR_CLASS_PERMANENT, ERROR_CLASS_OPERATOR)

# Reasons are free-form tags logged for observability; enumerating them
# here keeps the set reviewable without forcing a CHECK constraint (new
# reasons can appear with behavior changes without a schema bump).
REASON_ON_CREATED = "on_created"
REASON_ON_MOVED = "on_moved"
REASON_INITIAL_SCAN = "initial_scan"
REASON_RECOVERY = "recovery"
REASON_RESCAN = "rescan"
REASON_REEXTRACT = "reextract"
# Re-read an already indexed message so a parser change that keeps chunk
# IDs fills its per-message rows (#1078). Queued by ``REPARSE_ENQUEUE_SQL``.
REASON_REPARSE = "reparse"

# Queue every indexed source file for a reparse, in one statement. A
# migration that adds parser-produced per-message data ends with this
# statement verbatim, and ``make reparse`` (``src/reparse.py``) runs the
# same text, so both share one contract (docs/architecture.md, "Reparse
# in place"). ``ON CONFLICT DO NOTHING`` leaves every existing row as it
# is: a pending or retrying job keeps its reason, attempts, error and due
# time (the worker parses it in full anyway), and a dead-lettered job
# stays dead until ``make requeue-dead``. ``WHERE true`` is SQLite's
# required disambiguation for an upsert on ``INSERT ... SELECT``. The
# timestamp has the shape ``datetime.isoformat()`` gives in UTC, so it
# sorts and parses with the rows Python writes.
REPARSE_ENQUEUE_SQL = """\
INSERT INTO indexing_jobs
    (filepath, reason, status, attempts, created_at, updated_at, next_attempt_at)
SELECT filepath, 'reparse', 'queued', 0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM indexed_files WHERE true
ON CONFLICT(filepath) DO NOTHING;"""

# Stages ``defer`` is called with. The queue heartbeat counts deferrals
# per stage, and tells permission deferrals and parked trashed files
# apart from retries (#874).
STAGE_PARSE = "parse"
STAGE_EMBED = "embed"
STAGE_TRASHED = "trashed"
DEFER_STAGES = (STAGE_PARSE, STAGE_EMBED, STAGE_TRASHED)
# A message whose attachment extraction reached the per-message budget
# (#1236) is continued at this stage: ``main._phase2c_commit_vectors``
# defers it, inside the transaction that commits the pass's results, with
# ``EXTRACTION_DEFERRED_ERROR``, due at once, so it takes its turn behind
# the rows already due. A claimed row carrying both is a continuation,
# which extracts only the occurrences still pending. Counted in the queue
# heartbeat's ``extraction_deferred`` bucket (from the rows) rather than in
# ``drain_deferrals``, and in the attachments aggregate once committed.
STAGE_EXTRACT = "extract"
EXTRACTION_DEFERRED_ERROR = (
    "attachment extraction deferred: per-message budget reached; continued on a later pass"
)
# ``last_error`` of a job deferred because mbsync has not opened the file
# to the indexer yet (``main._phase1_commit_thread``). Fixed text, and
# distinct from ``_stage_error``'s rendering of the same
# ``PermissionError`` (``PermissionError: [Errno 13] ...``), which a job
# past its deferral window writes on the normal retry path, so the
# heartbeat tells the two apart (Codex round 2 on #904).
PERMISSION_DEFERRED_ERROR = "PermissionError: deferred until mbsync opens the file"

# Written by ``begin_attempt`` while a message's step runs and cleared
# when it returns, so a row still carrying it after a restart was being
# processed when the indexer stopped.
INTERRUPTED_STAGE = "interrupted"
_INTERRUPTED_ERROR = (
    "indexer stopped while processing this message (crash, out-of-memory kill, or stall-guard exit)"
)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_BACKOFF_SECONDS = 30
# Cap the backoff so a long-dead row doesn't compute an impractical
# next_attempt_at that overflows or schedules decades in the future.
_MAX_BACKOFF_SECONDS = 6 * 60 * 60  # 6 hours


def load_config_from_env(env: dict[str, str] | os._Environ) -> dict[str, int]:
    """Parse queue config from a dict-like env mapping.

    Exposed as a helper so ``main.py`` and tests share the same parsing
    rather than each re-implementing int coercion with the same default.

    Unset or empty values yield the documented default. A non-integer
    or a value below the minimum raises ``ValueError`` naming the
    variable, so a typo stops startup instead of being silently
    replaced (#481) — the same rule as
    ``reconciler.load_config_from_env`` and ``main._int_env``. Both
    knobs require a minimum of 1:

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
        raise ValueError(f"{name}={raw!r} is not an integer") from None
    if minimum is not None and value < minimum:
        raise ValueError(f"{name}={raw!r} must be >= {minimum}")
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
        self._in_flight: tuple[str, float] | None = None
        # Guards ``_in_flight`` and its refund against the stall guard's thread.
        self._lock = threading.Lock()
        # ``defer`` calls per stage since the last heartbeat (#874).
        self._deferrals: Counter[str] = Counter()
        # A batch of one goes to a reparse row next, when both classes are
        # due (#1142). In memory only: a restart starting with a
        # foreground row costs the reparse one turn.
        self._reparse_turn = False

    # ----- writes --------------------------------------------------------

    def enqueue(self, filepath: str, reason: str, *, due_at: datetime | None = None) -> None:
        """Queue ``filepath`` for indexing, resetting any prior state.

        ``due_at`` (a past time) sets where the row sorts in the queue,
        which hands rows out by due time: the initial scan passes each
        message's own time so its backlog is indexed oldest first and
        ahead of mail queued as it arrives (#699). Default: now.

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
            due_iso=due_at.isoformat() if due_at is not None else None,
        )

    def enqueue_reparse(self) -> int:
        """Queue every indexed file for a reparse (``REPARSE_ENQUEUE_SQL``)
        and return how many jobs were added; files that already have a
        job keep it. ``make reparse`` calls this."""
        return self.db.queue_enqueue_reparse()

    def redate_untried(self, filepath: str, due_at: datetime) -> None:
        """Move a queued row that has never been tried to ``due_at``, as
        ``enqueue(due_at=)`` would have placed it (#699). A row with an
        attempt, an error or a parked stage keeps its due time, so a retry
        backoff or a trashed-file park is not cut short."""
        self.db.queue_redate_untried(filepath=filepath, due_iso=due_at.isoformat())

    def claim_batch(self, limit: int) -> list[sqlite3.Row]:
        """Return up to ``limit`` distinct due queued rows, foreground
        first with one slot kept for a reparse row while both are due
        (``Database.queue_fetch_due_batch``, #1142).

        Fetches N distinct rows under one lock — so the batched
        initial indexer's gather phase can pick up distinct messages
        without re-claiming the same row before marking it succeeded.
        Rows stay in 'queued' state; the caller must mark each one
        (succeeded / failed / skipped) by the end of the batch or they
        will be returned again on the next call. Claiming charges no
        attempt; only ``begin_attempt`` does.

        A batch of one has no second slot to keep, so the two classes
        take turns while both are due: after a foreground row the next
        claim prefers a reparse row, and the other way round.
        """
        rows = self.db.queue_fetch_due_batch(
            STATUS_QUEUED, _now_iso(), limit, reparse_turn=self._reparse_turn
        )
        if limit == 1 and rows:
            self._reparse_turn = rows[0]["reason"] != REASON_REPARSE
        return rows

    def begin_attempt(self, filepath: str) -> bool:
        """Charge one attempt to ``filepath`` before running one of its
        per-message steps; return False if it must not run.

        The charge is committed before the step starts, together with
        ``last_stage = 'interrupted'``, so a process that dies mid-step
        leaves the row counted and marked. ``end_attempt`` or any outcome
        method refunds it and clears the mark. A marked row whose
        attempts are exhausted got there only through such deaths, so it
        is dead-lettered here rather than run again; an unmarked row over
        the limit (``INDEXER_MAX_ATTEMPTS`` lowered) still runs, and
        ``mark_failed`` decides.
        """
        if not self.db.queue_charge_attempt(
            filepath=filepath,
            max_attempts=self.max_attempts,
            marker_stage=INTERRUPTED_STAGE,
            marker_error=_INTERRUPTED_ERROR,
            error_class=ERROR_CLASS_RETRYABLE,
            now_iso=_now_iso(),
        ):
            warn_rate_limited(
                log,
                "dead-letter: %s interrupted mid-processing on every attempt",
                filepath,
                level=logging.ERROR,
                attachment=False,
            )
            return False
        with self._lock:
            self._in_flight = (filepath, time.monotonic())
        return True

    def end_attempt(self, filepath: str) -> None:
        """Refund the ``begin_attempt`` charge: the step returned."""
        self._settle(filepath)

    def note_progress(self) -> None:
        """Restart the in-flight clock: the step finished one bounded unit
        of work (one attachment), so the stall guard's limit applies per
        unit rather than to a whole message."""
        with self._lock:
            if self._in_flight is not None:
                self._in_flight = (self._in_flight[0], time.monotonic())

    def mark_interrupted(self, filepaths: list[str]) -> None:
        """Durably mark several rows as mid-step without charging any.

        For batch-wide work (the bulk embed and vector commit) whose
        failure cannot be pinned on one message: if the process dies
        there, every marked row is replayed alone, where a charge is
        attributable. Each row's outcome method overwrites or deletes
        the mark.
        """
        self.db.queue_mark_interrupted(
            filepaths=filepaths, marker_stage=INTERRUPTED_STAGE, marker_error=_INTERRUPTED_ERROR
        )

    @contextmanager
    def holding_in_flight(self) -> Iterator[tuple[str, float] | None]:
        """Yield the in-flight message while blocking its refund.

        The stall guard decides and exits inside this block, so a step
        that returns at that moment cannot refund its charge (or begin
        the next message) before the process is gone.
        """
        with self._lock:
            yield self._in_flight

    def _settle(self, filepath: str) -> None:
        """Refund an open charge on ``filepath`` before its outcome is
        recorded, so outcome methods keep their own attempt accounting."""
        with self._lock:
            in_flight = self._in_flight
            if in_flight is None or in_flight[0] != filepath:
                return
            self.db.queue_refund_attempt(filepath=filepath, marker_stage=INTERRUPTED_STAGE)
            self._in_flight = None

    def mark_succeeded(self, filepath: str) -> None:
        """Remove the job row. Indexing ran through cleanly."""
        self._settle(filepath)
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
        self._settle(filepath)
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
        self._settle(filepath)
        self.db.queue_mark_dead(
            filepath=filepath,
            attempts=1,
            last_stage=stage,
            last_error=error,
            error_class=ERROR_CLASS_PERMANENT,
            now_iso=_now_iso(),
        )
        warn_rate_limited(
            log,
            "terminal: %s stage=%s error=%s",
            filepath,
            stage,
            _truncate_error(error),
            attachment=False,
        )

    def mark_failed(self, filepath: str, *, stage: str, error: str) -> None:
        """Record a failed attempt and schedule the next retry or dead-letter.

        ``stage`` is one of ``"parse" | "thread" | "embed" | "db_write"``
        — useful for operators to see which pipeline step broke. The
        value is stored verbatim so new stages can be introduced without
        migrating existing rows.
        """
        self._settle(filepath)
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
                error_class=ERROR_CLASS_RETRYABLE,
                now_iso=_now_iso(),
            )
            warn_rate_limited(
                log,
                "dead-letter: %s after %d attempts at stage=%s error=%s",
                filepath,
                new_attempts,
                stage,
                _truncate_error(error),
                level=logging.ERROR,
                attachment=False,
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
            error_class=ERROR_CLASS_RETRYABLE,
            now_iso=_now_iso(),
            next_attempt_iso=next_attempt.isoformat(),
        )
        warn_rate_limited(
            log,
            "retry: %s attempt %d/%d at stage=%s next_in=%ds error=%s",
            filepath,
            new_attempts,
            self.max_attempts,
            stage,
            backoff_seconds,
            _truncate_error(error),
            attachment=False,
        )

    def defer(
        self,
        filepath: str,
        *,
        stage: str,
        error: str,
        error_class: str,
        delay_seconds: float,
    ) -> None:
        """Postpone a job for an infrastructure failure without spending
        its attempt budget.

        Used when the failure says nothing about the message — the
        embedder is down, rate-limiting, or rejecting credentials — and
        to continue a message whose attachment extraction reached the
        per-message budget (``STAGE_EXTRACT``, #1236). The row stays
        ``queued`` with ``attempts`` unchanged and becomes due again
        after ``delay_seconds``, so no outage can dead-letter it.

        Inside ``db.transaction()`` the row's write commits or rolls back
        with the caller's (``Database.queue_mark_failed``); the caller
        then refunds any charge first (``end_attempt``), since the refund
        commits on its own.
        """
        self._settle(filepath)
        attempts = self.db.queue_get_attempts(filepath)
        if attempts is None:
            log.warning("defer: no queue row for %s; nothing to update", filepath)
            return
        next_attempt = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        self.db.queue_mark_failed(
            filepath=filepath,
            attempts=attempts,
            last_stage=stage,
            last_error=error,
            error_class=error_class,
            now_iso=_now_iso(),
            next_attempt_iso=next_attempt.isoformat(),
        )
        self._deferrals[stage] += 1

    def drain_deferrals(self) -> dict[str, int]:
        """``defer`` calls per stage since the last call, then reset."""
        counts, self._deferrals = self._deferrals, Counter()
        return {stage: counts[stage] for stage in DEFER_STAGES}

    def requeue_dead(self, error_class: str | None = None) -> int:
        """Return dead-lettered jobs to the queue with a fresh budget.

        The operator rescue path once the underlying cause is fixed.
        ``error_class`` limits the requeue to one failure class; ``None``
        requeues every dead row. Returns the number of rows requeued.
        """
        return self.db.queue_requeue_dead(error_class=error_class, now_iso=_now_iso())

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
        indexer health file / MCP ``get_mailbox_status`` so operators can
        see when work is backing up or files are giving up."""
        return self.db.queue_stats()

    def heartbeat_counts(self, now: datetime | None = None) -> dict[str, int]:
        """Counts for the queue heartbeat line (#874), in one query.

        ``pending`` rows have never failed; ``deferred_permission`` rows
        were last deferred because mbsync has not opened the file yet;
        ``parked_trashed`` rows are trashed files waiting to be reaped;
        ``retrying`` is every other queued row (failures, including a
        permission error past its deferral window, embedder deferrals,
        a row interrupted mid-step). ``extraction_deferred`` rows are
        messages continued because their attachment extraction reached
        the per-message budget (``STAGE_EXTRACT``, #1236). ``reparse``
        counts the queued rows
        whose reason is ``reparse`` outside ``parked_trashed`` (the ones
        that can drain), ``reparse_parked_trashed`` those parked as
        trashed (#1331) and ``reparse_dead`` the dead ones (#1078).
        ``oldest_due_age``
        is how long, in seconds, the longest-waiting due row has been due
        (0 when none is due): it grows while draining is stalled.
        """
        now = now or datetime.now(UTC)
        counts, oldest_due = self.db.queue_heartbeat_counts(
            now_iso=now.isoformat(),
            permission_stage=STAGE_PARSE,
            permission_deferred_error=PERMISSION_DEFERRED_ERROR,
            trashed_stage=STAGE_TRASHED,
            extract_stage=STAGE_EXTRACT,
            extraction_deferred_error=EXTRACTION_DEFERRED_ERROR,
            reparse_reason=REASON_REPARSE,
        )
        age = 0
        if oldest_due is not None:
            age = max(0, int((now - datetime.fromisoformat(oldest_due)).total_seconds()))
        return {
            "pending": counts.get("pending", 0),
            "retrying": counts.get("retrying", 0),
            "deferred_permission": counts.get("deferred_permission", 0),
            "parked_trashed": counts.get("parked_trashed", 0),
            "extraction_deferred": counts.get("extraction_deferred", 0),
            "dead": counts.get("dead", 0),
            "oldest_due_age": age,
            "reparse": counts["reparse"],
            "reparse_parked_trashed": counts["reparse_parked_trashed"],
            "reparse_dead": counts["reparse_dead"],
        }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _truncate_error(error: str, limit: int = 200) -> str:
    """Cap log output for stack traces. The full text is already in the
    ``last_error`` column for post-mortem; logs just need a hint."""
    if len(error) <= limit:
        return error
    return error[:limit] + "…"
