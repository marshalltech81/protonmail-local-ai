"""
System tools — Group 4.
Mailbox status: how current the local index is, and what it holds.
Claude should call get_mailbox_status before making claims about email content.
"""

import asyncio
import logging
from datetime import UTC, datetime

from fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from ..lib import timings
from ..lib.build_identity import git_commit
from .outputs import MailboxStatusOutput, QueueCounts, read_only, tool_result

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


def not_current_reasons(
    *,
    last_sync_at: datetime | None,
    sync_interval_secs: int | None,
    indexer_last_seen_at: datetime | None,
    queue: QueueCounts,
    now: datetime,
) -> list[str]:
    """Why the index is not current; empty when it is.

    Current means: mbsync completed a sync recently, the indexer has
    reported recently (it read that sync's stamp after the sync's mail
    was already queued), and nothing is waiting in the queue (pending,
    retrying, deferred, or continued for deferred attachment extraction,
    #1236). Dead messages are terminal and parked trashed
    files are already indexed (#1165): both are reported separately, not
    waited on.
    """
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


def _mailbox_status(db) -> MailboxStatusOutput:
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
    return MailboxStatusOutput(
        server_version=git_commit(),
        current=not reasons,
        not_current_reasons=reasons,
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
        checked_at=now,
    )


def _when(value: datetime | None, now: datetime) -> str:
    if value is None:
        return "never"
    return f"{value.isoformat()} ({_age((now - value).total_seconds())} ago)"


def _render(out: MailboxStatusOutput) -> str:
    lines = [
        "=== Mailbox Status ===",
        f"Server version: {out.server_version}",
        f"Current:        {'yes' if out.current else 'no'}",
    ]
    lines += [f"  - {reason}" for reason in out.not_current_reasons]
    q = out.queue
    lines += [
        f"Last mail sync: {_when(out.last_sync_at, out.checked_at)}",
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
            "adds is missing until the reparse finishes."
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


def register_system_tools(server, db):
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
        from Proton every few minutes; it never contacts Proton itself. The
        index is current when mail synced recently, the indexer is running,
        and no message is waiting to be indexed (pending, retrying or
        deferred). When it is not current, the
        reasons are listed: say so before relying on the results, since
        recent mail may be missing. Mail that reached Proton after the last
        sync is never searchable yet.

        Returns server_version (the deployed source commit, with -dirty for
        local changes, or unknown when build identity is unavailable),
        current and the reasons it is false, last sync time, indexer
        liveness, queue counts (pending, retrying, deferred, parked
        trashed files that do not count against current, dead, and
        how many waiting messages are already indexed and being
        reparsed), total threads
        and messages, the date range, and how many Message-IDs more
        than one file claims (counts only).
        """
        log.info("tool=get_mailbox_status")
        try:
            # The status queries scan aggregates; run them in a worker
            # thread so they do not block the shared event loop.
            output = await asyncio.to_thread(_mailbox_status, db)
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
        output = _mailbox_status(Database(os.environ.get("SQLITE_PATH", "/data/mail.db")))
    except Exception as e:
        return {"status": "error", "error": f"Mailbox status error: {type(e).__name__}"}
    return {"status": "ok", **output.model_dump(mode="json")}
