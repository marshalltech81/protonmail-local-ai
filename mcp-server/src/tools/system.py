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
from .outputs import MailboxStatusOutput, QueueCounts, tool_result

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
    was already queued), and nothing is waiting in the queue. Dead
    messages are terminal and reported separately, not waited on.
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
    waiting = queue.pending + queue.retrying
    if waiting:
        reasons.append(
            f"{_messages(waiting)} waiting to be indexed "
            f"({queue.pending:,} pending, {queue.retrying:,} retrying)"
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
    lines = ["=== Mailbox Status ===", f"Current:        {'yes' if out.current else 'no'}"]
    lines += [f"  - {reason}" for reason in out.not_current_reasons]
    q = out.queue
    lines += [
        f"Last mail sync: {_when(out.last_sync_at, out.checked_at)}",
        f"Indexer seen:   {_when(out.indexer_last_seen_at, out.checked_at)}",
        f"Queue:          {q.pending:,} pending, {q.retrying:,} retrying, {q.dead:,} dead",
    ]
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
    @server.tool(output_schema=MailboxStatusOutput.model_json_schema())
    @timings.timed_tool("get_mailbox_status")
    async def get_mailbox_status() -> CallToolResult:
        """
        Report whether the local email index is current, and what it holds.
        Call this before answering questions about email content.

        This server answers only from the local index, which mbsync fills
        from Proton every few minutes; it never contacts Proton itself. The
        index is current when mail synced recently, the indexer is running,
        and no message is waiting to be indexed. When it is not current, the
        reasons are listed: say so before relying on the results, since
        recent mail may be missing. Mail that reached Proton after the last
        sync is never searchable yet.

        Returns:
            current and the reasons it is false, last sync time, indexer
            liveness, queue counts (pending, retrying, dead), total threads
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
