"""Ends the indexer when one message's step runs far past any legitimate
duration.

The worker is a single synchronous thread, so a step that never returns
(a parser or extractor stuck on hostile input) blocks all ingestion, and
the health check alone cannot help: Compose does not restart an
unhealthy container. The guard runs on a daemon thread, watches the
queue's in-flight message, and exits the process once that message has
run longer than the limit. Compose's restart policy brings the indexer
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

    def stalled(self) -> str | None:
        """The in-flight message's path if it has run past the limit."""
        in_flight = self._queue.in_flight()
        if in_flight is None:
            return None
        filepath, started = in_flight
        if self._clock() - started > self._limit:
            return filepath
        return None

    def check(self) -> bool:
        """Exit the process if stalled; True when it fired."""
        filepath = self.stalled()
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

    def start(self) -> None:
        threading.Thread(target=self._run, name="stall-guard", daemon=True).start()

    def _run(self) -> None:
        # ``check`` returns only if ``exit_fn`` did (a test double).
        while not self.check():
            time.sleep(self._interval)
