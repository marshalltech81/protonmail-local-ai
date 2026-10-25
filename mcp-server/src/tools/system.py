"""
System tools — Group 4.
Mailbox status: how current the local index is, and what it holds.
Claude should call get_mailbox_status before making claims about email content.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Literal

from fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from ..lib import timings
from ..lib.build_identity import git_commit
from ..lib.reranker import RerankerBackend
from .outputs import (
    CollectionAttempt,
    CollectionStatus,
    MailboxStatusOutput,
    QueueCounts,
    read_only,
    tool_result,
)

log = logging.getLogger("mcp.tools.system")

# A sync is stale after three missed intervals, but never sooner than
# five minutes: a sync of a busy mailbox can outlast a short interval.
SYNC_STALE_INTERVALS = 3
SYNC_STALE_FLOOR_SECS = 300
# The indexer reports every 30 s while it runs (``_IngestionStateRecorder``)
# and its own healthcheck allows 600 s between heartbeats.
INDEXER_STALE_SECS = 600
# Containers share the host clock, so a stamp written by mbsync or the
# indexer should never be ahead of ours; two minutes covers rounding and
# a host clock step. Anything further ahead (a clock rollback, a bad
# write) cannot vouch for a recent sync or heartbeat.
FUTURE_SKEW_TOLERANCE_SECS = 120


def _age(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _messages(n: int) -> str:
    return f"{n:,} message" + ("" if n == 1 else "s")


def _freshness_reasons(
    *,
    last_sync_at: datetime | None,
    sync_interval_secs: int | None,
    indexer_last_seen_at: datetime | None,
    now: datetime,
) -> list[str]:
    """Sync and heartbeat freshness failures shared by both status flags."""
    reasons = []
    if last_sync_at is None or sync_interval_secs is None:
        reasons.append("no successful mail sync has been recorded")
    else:
        age = (now - last_sync_at).total_seconds()
        if -age > FUTURE_SKEW_TOLERANCE_SECS:
            reasons.append(
                f"the last successful mail sync is timestamped {_age(-age)} in the future"
            )
        elif age > max(SYNC_STALE_INTERVALS * sync_interval_secs, SYNC_STALE_FLOOR_SECS):
            reasons.append(
                f"last successful mail sync was {_age(age)} ago "
                f"(mbsync syncs every {sync_interval_secs}s)"
            )
    if indexer_last_seen_at is None:
        reasons.append("the indexer has not reported")
    else:
        age = (now - indexer_last_seen_at).total_seconds()
        if -age > FUTURE_SKEW_TOLERANCE_SECS:
            reasons.append(f"the indexer last reported {_age(-age)} in the future")
        elif age > INDEXER_STALE_SECS:
            reasons.append(f"the indexer last reported {_age(age)} ago")
    return reasons


def not_current_reasons(
    *,
    last_sync_at: datetime | None,
    sync_interval_secs: int | None,
    indexer_last_seen_at: datetime | None,
    queue: QueueCounts,
    now: datetime,
) -> list[str]:
    """Freshness failures and waiting work; terminal and parked jobs are excluded."""
    reasons = _freshness_reasons(
        last_sync_at=last_sync_at,
        sync_interval_secs=sync_interval_secs,
        indexer_last_seen_at=indexer_last_seen_at,
        now=now,
    )
    waiting = queue.pending + queue.retrying + queue.deferred + queue.extraction_deferred
    if waiting:
        deferred = f", {queue.deferred:,} deferred"
        extraction = f", {queue.extraction_deferred:,} with attachment extraction deferred"
        reparse = f"; {queue.reparse:,} of them already indexed and being reparsed"
        reasons.append(
            f"{_messages(waiting)} waiting to be indexed "
            f"({queue.pending:,} pending, {queue.retrying:,} retrying"
            f"{deferred if queue.deferred else ''}"
            f"{extraction if queue.extraction_deferred else ''}"
            f"{reparse if queue.reparse else ''})"
        )
    return reasons


# Terminal outcomes that leave the collection a success, rather than a
# failure: ``succeeded`` and the degraded-but-successful
# ``succeeded_with_withheld_folders``.
_SUCCESS_OUTCOMES = frozenset({"succeeded", "succeeded_with_withheld_folders"})


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def collection_status(row: dict | None) -> CollectionStatus:
    """mbsync's latest collection outcome from the indexer's
    ``account_collection_state`` row (#1592); ``None`` (not recorded yet)
    and an unknown status read as an unknown outcome, never a success."""
    if row is None:
        return CollectionStatus(
            outcome="unknown",
            outcome_at=None,
            withheld_far_side_folders=None,
            failure_class=None,
            latest_attempt=None,
            last_success_at=None,
            failure_unrecovered=False,
            success_undelivered=False,
            unknown_reason="not_recorded",
            observed_at=None,
        )
    unrecovered = row["unrecovered_failure_attempt_id"] is not None
    if row["status"] != "known":
        return CollectionStatus(
            outcome="unknown",
            outcome_at=None,
            withheld_far_side_folders=None,
            failure_class=None,
            latest_attempt=None,
            last_success_at=None,
            failure_unrecovered=unrecovered,
            success_undelivered=False,
            unknown_reason=row["unknown_reason"],
            observed_at=_time(row["observed_at"]),
        )
    terminal = row["terminal_outcome"]
    return CollectionStatus(
        outcome=terminal or "unknown",
        outcome_at=_time(row["terminal_completed_at"]),
        withheld_far_side_folders=row["terminal_withheld_folders"],
        failure_class=row["terminal_failure_class"],
        latest_attempt=CollectionAttempt(
            outcome=row["attempt_outcome"],
            started_at=datetime.fromisoformat(row["attempt_started_at"]),
        ),
        last_success_at=_time(row["success_completed_at"]),
        failure_unrecovered=unrecovered,
        # A success terminal whose delivery the indexer has not yet
        # confirmed (the acknowledged sync does not yet name it, #1612
        # round 12): a watcher delayed or behind enough to read this
        # collection's publication (a rename event, or the periodic
        # heartbeat's direct read) before processing that sync's own
        # stamp rename reports ``success_delivered`` 0 here even though
        # no failure was ever observed in between, so this is checked
        # independently of ``failure_unrecovered`` rather than relying on
        # a failure having been seen.
        success_undelivered=(terminal in _SUCCESS_OUTCOMES and not row["success_delivered"]),
        unknown_reason=None if terminal else "no_completed_attempt",
        observed_at=_time(row["observed_at"]),
    )


def collection_reasons(collection: CollectionStatus) -> list[str]:
    """Why mbsync's collection keeps the index from being current: after a
    failed attempt, until a later success is delivered to the index."""
    if collection.outcome == "failed" and collection.failure_unrecovered:
        return [
            f"mbsync's latest collection attempt failed ({collection.failure_class}); the "
            "index is not current until a later successful sync is delivered to it"
        ]
    if collection.failure_unrecovered:
        return [
            "mbsync's last failed collection attempt has not yet been followed by a "
            "successful sync delivered to the index"
        ]
    if collection.success_undelivered:
        return [
            "mbsync's latest successful collection has not yet been confirmed delivered "
            "to the index"
        ]
    return []


def _mailbox_status(
    db, *, rerank_mode: Literal["none", "cohere"] = "none", rerank_candidates: int | None = None
) -> MailboxStatusOutput:
    stats = db.get_mailbox_status()
    state = stats["ingestion"] or {}
    last_sync_at = (
        datetime.fromisoformat(state["sync_completed_at"])
        if state.get("sync_completed_at")
        else None
    )
    indexer_seen = (
        datetime.fromisoformat(state["indexer_seen_at"]) if state.get("indexer_seen_at") else None
    )
    queue = QueueCounts(**stats["queue"])
    now = datetime.now(UTC)
    reasons = not_current_reasons(
        last_sync_at=last_sync_at,
        sync_interval_secs=state.get("sync_interval_secs"),
        indexer_last_seen_at=indexer_seen,
        queue=queue,
        now=now,
    )
    collection = collection_status(stats["collection"])
    collection_failures = collection_reasons(collection)
    reasons += collection_failures
    freshness_failures = _freshness_reasons(
        last_sync_at=last_sync_at,
        sync_interval_secs=state.get("sync_interval_secs"),
        indexer_last_seen_at=indexer_seen,
        now=now,
    )
    waiting = queue.pending + queue.retrying + queue.deferred + queue.extraction_deferred
    ingestion_caught_up = (
        not (freshness_failures or collection_failures) and waiting == queue.reparse
    )
    return MailboxStatusOutput(
        rerank_mode=rerank_mode,
        rerank_candidates=rerank_candidates,
        server_version=git_commit(),
        current=not reasons,
        ingestion_caught_up=ingestion_caught_up,
        not_current_reasons=reasons,
        collection=collection,
        last_sync_at=last_sync_at,
        sync_interval_secs=state.get("sync_interval_secs"),
        indexer_last_seen_at=indexer_seen,
        queue=queue,
        total_threads=stats["total_threads"],
        total_messages=stats["total_messages"],
        oldest_message=stats["oldest_message"],
        newest_message=stats["newest_message"],
        conflicting_message_ids=stats["conflicting_message_ids"],
        extra_claimant_files=stats["extra_claimant_files"],
        header_predicates=stats["header_predicates"],
        query_capabilities=stats["query_capabilities"],
        operator_identity=stats["operator_identity"],
        authority_rules=stats.get("authority_rules", {}),
        index_schema_version=stats["index_schema_version"],
        checked_at=now,
    )


def _when(value: datetime | None, now: datetime) -> str:
    if value is None:
        return "never"
    return f"{value.isoformat()} ({_age((now - value).total_seconds())} ago)"


def _render(out: MailboxStatusOutput) -> str:
    lines = [
        "=== Mailbox Status ===",
        (
            f"Reranking: cohere; rerank_candidates={out.rerank_candidates}; "
            "up to max(limit, rerank_candidates) candidate threads per reranking search"
            if out.rerank_mode == "cohere"
            else "Reranking: none"
        ),
        f"Server version: {out.server_version}",
        f"Index schema version: {out.index_schema_version}",
        f"Current:        {'yes' if out.current else 'no'}",
    ]
    lines += [f"  - {reason}" for reason in out.not_current_reasons]
    lines.append(
        f"Ingestion caught up: {'yes' if out.ingestion_caught_up else 'no'} (excludes reparses and terminal failures)"
    )
    rules = out.authority_rules
    lines.append(
        "Authority rules: unknown"
        if rules.configured is None
        else f"Authority rules: configured={rules.configured}, patterns={rules.rule_count}, "
        f"defined classes={', '.join(rules.classes or []) or 'none'}"
    )
    identity = out.operator_identity
    lines.append(
        f"Operator identity: {identity.state}; addresses={identity.address_count}; Sent conflicts={identity.sent_conflicts}"
    )
    h = out.header_predicates
    lines.append(
        f"Header predicates: {h.status}; {h.unassessed_messages} unassessed, "
        f"{h.incomplete_messages} incomplete messages"
    )
    lines.append("Query capabilities (conservative evidence coverage, including Trash):")
    for name, capability in out.query_capabilities.items():
        lines.append(
            f"  {name}: {capability.status}; "
            f"{capability.available_messages} available, "
            f"{capability.unassessed_messages} unassessed, "
            f"{capability.missing_messages} missing messages"
        )
        if capability.condition:
            lines.append(f"    {capability.condition}")
    q = out.queue
    lines += [
        f"Last mail sync: {_when(out.last_sync_at, out.checked_at)}",
        *_collection_lines(out),
        f"Indexer seen:   {_when(out.indexer_last_seen_at, out.checked_at)}",
        f"Queue:          {q.pending:,} pending, {q.retrying:,} retrying, "
        f"{q.deferred:,} deferred, {q.extraction_deferred:,} extraction deferred, "
        f"{q.parked_trashed:,} parked (trashed), {q.dead:,} dead",
    ]
    if q.reparse:
        one = q.reparse == 1
        lines.append(
            f"  {q.reparse:,} waiting message{'' if one else 's'} "
            f"{'is' if one else 'are'} already indexed and being reparsed after an "
            f"upgrade: search finds {'it' if one else 'them'}, but data the upgrade "
            "adds is unknown until a successful reparse. Sender, participant and "
            "authority filters count messages with unassessed metadata as indeterminate; "
            "queue membership alone does not establish that metadata is missing."
        )
    lines.append(
        "Reparse-added fields remain unknown until a successful reparse; "
        "a dead-lettered reparse needs make requeue-dead and a successful retry, "
        "even when the waiting reparse count is zero."
    )
    if q.deferred:
        one = q.deferred == 1
        lines.append(
            f"  {_messages(q.deferred)} {'is' if one else 'are'} deferred "
            f"without a failure of {'its' if one else 'their'} own (a file the indexer "
            "cannot read yet, an embedder outage or configuration error, or a job "
            f"waiting for a rename); the indexer retries {'it' if one else 'them'} "
            "without spending attempts."
        )
    if q.extraction_deferred:
        one = q.extraction_deferred == 1
        lines.append(
            f"  {_messages(q.extraction_deferred)} {'is' if one else 'are'} already indexed, "
            f"but {'its' if one else 'their'} attachment extraction reached the indexer's "
            "per-message budget: the remaining attachments are extracted on later passes; "
            "until then search has only text indexed for them earlier (flagged as retained "
            "indexed text), or none."
        )
    if q.parked_trashed:
        one = q.parked_trashed == 1
        lines.append(
            f"  {q.parked_trashed:,} trashed message{'' if one else 's'} "
            f"{'is' if one else 'are'} already indexed and wait{'s' if one else ''} for "
            f"the reaper to remove {'it' if one else 'them'} (or for "
            f"{'its file' if one else 'their files'} to be restored); "
            f"{'it does' if one else 'they do'} not make the index non-current."
        )
    if q.dead:
        lines.append(
            f"  {_messages(q.dead)} failed permanently and "
            f"{'is' if q.dead == 1 else 'are'} incompletely indexed: missing from "
            "search, or found only by keyword, until an operator requeues them "
            "(make requeue-dead)."
        )
    lines += [
        f"Total threads:  {out.total_threads:,}",
        f"Total messages: {out.total_messages:,}",
        f"Oldest message: {out.oldest_message or 'unknown'}",
        f"Newest message: {out.newest_message or 'unknown'}",
    ]
    lines += _conflict_lines(out)
    lines.append(f"Checked at:     {out.checked_at.isoformat()}")
    return "\n".join(lines)


_UNKNOWN_REASON_TEXT = {
    "not_recorded": "the indexer has not recorded it yet",
    "missing": "mbsync has not published its collection status",
    "legacy": "mbsync's collection status predates this version",
    "malformed": "mbsync's collection status could not be read",
    "unsupported": "mbsync's collection status has a format this version does not read",
    "account_mismatch": "mbsync's collection status names another account",
    "unreadable": "mbsync's collection status could not be opened",
    "no_completed_attempt": "no collection attempt has completed yet",
}


def _collection_lines(out: MailboxStatusOutput) -> list[str]:
    """mbsync's latest collection outcome (#1592), as counts and fixed
    text, and Bridge/Proton upstream currency, always unknown."""
    c = out.collection
    if c.outcome == "unknown":
        head = f"unknown ({_UNKNOWN_REASON_TEXT[c.unknown_reason or 'not_recorded']})"
    elif c.outcome == "failed":
        head = f"failed ({c.failure_class}), {_when(c.outcome_at, out.checked_at)}"
    else:
        head = f"succeeded, {_when(c.outcome_at, out.checked_at)}"
    lines = [f"Mail collection: {head}"]
    if c.outcome == "failed" and not c.failure_unrecovered:
        lines.append("  Last recorded outcome; recovered by a later delivered sync.")
    if c.withheld_far_side_folders:
        n = c.withheld_far_side_folders
        folders = f"{n:,} far-side folder{'' if n == 1 else 's'}"
        if c.outcome == "failed":
            # Another error failed the run too, so nothing is said of the rest.
            lines.append(f"  {folders} could not be opened in that failed collection.")
        else:
            lines.append(
                f"  {folders} could not be opened (most likely renamed or deleted in Proton); "
                "the rest synced and their local copies are kept."
            )
    if c.latest_attempt is not None and c.latest_attempt.outcome == "in_progress":
        lines.append(
            f"  Latest attempt started {_when(c.latest_attempt.started_at, out.checked_at)} with "
            "no outcome recorded yet; this does not show whether mbsync is still running."
        )
    lines.append("Upstream (Bridge/Proton) currency: unknown")
    return lines


def _conflict_lines(out: MailboxStatusOutput) -> list[str]:
    """Message-ID conflicts as counts and a fixed hint, never the IDs (#455)."""
    ids, extra = out.conflicting_message_ids, out.extra_claimant_files
    if not ids:
        return ["Message-ID conflicts: none"]
    return [
        f"Message-ID conflicts: {ids:,} Message-ID{'' if ids == 1 else 's'} "
        f"{'is' if ids == 1 else 'are'} claimed by more than one file "
        f"({extra:,} extra file{'' if extra == 1 else 's'})",
        "  get_message on such a Message-ID lists its claimant IDs; "
        "call get_message with a claimant ID to read one file.",
    ]


def register_system_tools(server, db, *, reranker: RerankerBackend | None = None):
    @server.tool(
        output_schema=MailboxStatusOutput.model_json_schema(),
        annotations=read_only("Get Mailbox Status"),
    )
    @timings.timed_tool("get_mailbox_status")
    async def get_mailbox_status() -> CallToolResult:
        """
        Report the server version, whether the local email index is current, and what it holds.
        Call this before answering questions about email content.
        Also call this when asked which version or build of this server is running.

        This server answers only from the local index, which mbsync fills
        from Proton once per sync interval (sync_interval_secs); it never
        contacts Proton itself. The index is current when mail synced
        recently, the indexer is running, no message is waiting to be
        indexed (pending, retrying or deferred) or to have its remaining
        attachments extracted (extraction deferred), and no failed mail
        collection is waiting for a later successful one. When it is not
        current, the reasons are listed: say so before relying on the
        results, since recent mail or attachment text may be missing.
        Mail that reached Proton after the last sync is not in the index
        yet, even when the index is current. ingestion_caught_up uses the
        same freshness and collection checks but excludes waiting reparses;
        both flags exclude terminal failures. Fields added by a reparse
        stay unknown until it succeeds; dead-lettered reparses need
        make requeue-dead and a successful retry even when queue.reparse is zero.
        collection reports mbsync's
        latest collection from the Bridge app: succeeded, succeeded with
        a count of far-side folders it could not open, failed (with a
        class) or unknown.
        upstream_currency, whether Bridge itself is in step with Proton,
        is always unknown.

        Returns server_version (the deployed source commit, with -dirty for
        local changes, or unknown when build identity is unavailable),
        current and the reasons it is false, the collection outcome and
        upstream currency, last sync time, indexer
        liveness, queue counts (pending, retrying, deferred, extraction
        deferred, parked trashed files that do not count against
        current, dead, and how many waiting messages are already
        indexed and being reparsed), total threads
        and messages, the date range, and how many Message-IDs more
        than one file claims (counts only).
        rerank_mode and rerank_candidates describe the active reranker:
        none/null when disabled, cohere and its configured candidate count
        when enabled. A reranking search can send up to
        max(limit, rerank_candidates) candidate threads to that provider.
        """
        log.info("tool=get_mailbox_status")
        try:
            # The status queries scan aggregates; run them in a worker
            # thread so they do not block the shared event loop.
            output = await asyncio.to_thread(
                _mailbox_status,
                db,
                rerank_mode="cohere" if reranker is not None else "none",
                rerank_candidates=reranker.candidates if reranker is not None else None,
            )
        except Exception as e:
            log.error("get_mailbox_status error: %s", type(e).__name__)
            raise ToolError(f"Mailbox status error: {type(e).__name__}") from e
        return tool_result(_render(output), output)


def get_mailbox_status() -> dict:
    """Standalone helper used by the Makefile ``status`` target.

    Opens the local SQLite index directly (in read-only URI mode, same as
    the running MCP server) and returns the same fields the tool does.
    On failure it reports the exception type alone, as the tool does: the
    message can quote stored values (#732).
    """
    import os

    from ..lib.sqlite import Database

    try:
        mode = os.environ.get("RERANK_MODE", "none").strip().lower()
        if mode not in ("none", "cohere"):
            raise ValueError("invalid rerank mode")
        output = _mailbox_status(
            Database(os.environ.get("SQLITE_PATH", "/data/mail.db")),
            rerank_mode="cohere" if mode == "cohere" else "none",
            rerank_candidates=int(os.environ.get("RERANK_CANDIDATES", "20").strip() or "20")
            if mode == "cohere"
            else None,
        )
    except Exception as e:
        return {"status": "error", "error": f"Mailbox status error: {type(e).__name__}"}
    return {"status": "ok", **output.model_dump(mode="json")}
