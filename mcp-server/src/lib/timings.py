"""
Per-call stage timings for the MCP query path (#287).

Each instrumented tool call logs one line on the ``mcp.timings`` logger
naming the tool, its outcome, the wall-clock milliseconds of every
stage that ran, a few counts, and configuration identifiers. A stage
that did not run is absent, not zero, so a keyword-mode search shows no
vector lanes.

A retrieval lane that fails and falls back (a vector lane error, the
FTS-to-LIKE fallback, an attachment lane, a rerank fallback) adds a
``degraded_<lane>`` count, so a call that returned ``outcome=ok`` with
lower-quality results says so on its own line (#877).

Content safety: stage names, count names and config keys are literals
in the code, durations are floats, counts are ints, and config values
are operator mode names (``rerank``/``inference``). Nothing the caller
sent or the mailbox holds can reach the line.

The active recorder lives in a ``ContextVar``. ``asyncio.to_thread``
copies the context into the worker thread, so ``Database`` methods
record their lanes with ``stage()`` without a new parameter. Outside a
timed tool call (direct ``Database`` use, the baseline harness) every
helper is a no-op. One call's stages run one after another, so the
recorder needs no lock.
"""

import functools
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

log = logging.getLogger("mcp.timings")


class QueryTimings:
    """Stage durations (ms) and counts for one tool call."""

    def __init__(self, tool: str, config: dict[str, str]) -> None:
        self.tool = tool
        self.config = config
        self.stages_ms: dict[str, float] = {}
        self.counts: dict[str, int] = {}

    def format(self, outcome: str, total_ms: float) -> str:
        stages = {name: round(ms, 1) for name, ms in self.stages_ms.items()}
        return (
            f"tool={self.tool} outcome={outcome} total_ms={total_ms:.1f} "
            f"stages_ms={stages} counts={self.counts} config={self.config}"
        )


_current: ContextVar[QueryTimings | None] = ContextVar("mcp_query_timings", default=None)


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Add the block's wall-clock time to stage ``name`` of the active call.

    A stage entered more than once in a call (the per-thread inference
    calls of ``extract_from_emails``) accumulates. The time is recorded
    even when the block raises, so a failed call shows where it spent
    its time.
    """
    timings = _current.get()
    if timings is None:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - start) * 1000
        timings.stages_ms[name] = timings.stages_ms.get(name, 0.0) + elapsed


def count(name: str, value: int) -> None:
    """Add ``value`` to count ``name`` of the active call, if any."""
    timings = _current.get()
    if timings is not None:
        timings.counts[name] = timings.counts.get(name, 0) + value


def rerank_mode(reranker: object | None) -> str:
    """Config identifier for the rerank layer: its mode, or ``none``."""
    if reranker is None:
        return "none"
    return str(getattr(reranker, "mode", "enabled"))


def timed_tool(
    tool: str, **config: str
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Time a tool handler and log its stages once the call ends.

    Apply below ``@server.tool()``: ``functools.wraps`` keeps the
    handler's name, signature and docstring, which FastMCP reads to
    build the tool's schema. The line is logged on success and on
    failure (``outcome=error``, the exception re-raised unchanged).
    """

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            timings = QueryTimings(tool, dict(config))
            token = _current.set(timings)
            start = time.perf_counter()
            outcome = "error"
            try:
                result = await fn(*args, **kwargs)
                outcome = "ok"
                return result
            finally:
                total_ms = (time.perf_counter() - start) * 1000
                _current.reset(token)
                log.info("%s", timings.format(outcome, total_ms))

        return wrapper

    return decorator
