"""Ends the indexer when one unit of work — a message's parse, or one
attachment's extraction — runs far past any legitimate duration.

The worker is a single synchronous thread, so a step that never returns
(a parser or extractor stuck on hostile input) blocks all ingestion, and
the health check alone cannot help: Compose does not restart an
unhealthy container. The guard runs on a daemon thread, watches the
queue's in-flight message, and exits the process once it has gone
longer than the limit without progress (``IndexingQueue.note_progress``,
called per attachment). Compose's restart policy brings the indexer
back, and the attempt ``begin_attempt`` charged stays counted, so a
message that stalls the worker every time reaches ``dead``.

``os._exit`` rather than ``sys.exit``: the stuck step owns the main
thread, so nothing short of ending the process stops it. SQLite rolls
back any open transaction on the next open.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from .queue import IndexingQueue

log = logging.getLogger("indexer.stall_guard")


class StallGuard:
    def __init__(
        self,
        queue: IndexingQueue,
        *,
        limit_seconds: float,
        interval_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        exit_fn: Callable[[int], object] = os._exit,
    ) -> None:
        self._queue = queue
        self._limit = limit_seconds
        self._interval = interval_seconds
        self._clock = clock
        self._exit = exit_fn

    def check(self) -> bool:
        """Exit the process if stalled; True when it fired.

        Decides and exits while holding the queue's in-flight lock, so a
        step returning at that moment cannot refund its charge or charge
        the next message first.
        """
        with self._queue.holding_in_flight() as in_flight:
            filepath = self._overdue(in_flight)
            if filepath is None:
                return False
            log.error(
                "stall guard: %s has been processing for over %ds; exiting so the "
                "container restarts (the attempt stays counted)",
                filepath,
                self._limit,
            )
            self._exit(1)
            return True

    def _overdue(self, in_flight: tuple[str, float] | None) -> str | None:
        if in_flight is None:
            return None
        filepath, started = in_flight
        if self._clock() - started > self._limit:
            return filepath
        return None

    def start(self) -> None:
        threading.Thread(target=self._run, name="stall-guard", daemon=True).start()

    def _run(self) -> None:
        # ``check`` returns only if ``exit_fn`` did (a test double).
        while not self.check():
            time.sleep(self._interval)
