"""The indexer's rate-limited logger for repeated lines (#889).

A crafted stream of mail, or a provider that keeps failing, can repeat
one line per attachment, page or request; one line each would evict
the diagnostics that matter from Docker's bounded log history.
"""

from __future__ import annotations

import logging
import time
from threading import Lock


class LineBudget:
    """At most ``limit`` lines per window of ``window_secs`` seconds,
    shared by every caller. A line past the budget is not logged but
    counted under its bucket, one of the fixed ``buckets``; the owner
    of each bucket reports its count in an existing summary line and
    resets it with ``drain``.

    State is a window start, a line count and one counter per fixed
    bucket, so it is bounded whatever the input. Bucket names are fixed
    literals; an unknown one is a programming error and raises with
    fixed text. A lock makes ``log`` and ``drain`` safe from any thread;
    logging happens outside it.
    """

    def __init__(self, *, limit: int, window_secs: float, buckets: tuple[str, ...]) -> None:
        self.limit = limit
        self.window_secs = window_secs
        self._lock = Lock()
        self._window_start: float | None = None
        self._in_window = 0
        self._suppressed = dict.fromkeys(buckets, 0)

    def log(
        self,
        logger: logging.Logger,
        bucket: str,
        msg: str,
        *args: object,
        level: int = logging.WARNING,
    ) -> bool:
        """Log ``msg % args`` at ``level`` unless this window's budget is
        spent; then count it under ``bucket``. ``args`` must be counts,
        module names, type names or fixed text. Returns whether the line
        was logged."""
        if bucket not in self._suppressed:
            raise ValueError("unknown rate-limit bucket")
        # Read through the module so a test's patched clock applies.
        now = time.monotonic()
        with self._lock:
            if self._window_start is None or now - self._window_start >= self.window_secs:
                self._window_start = now
                self._in_window = 0
            if self._in_window >= self.limit:
                self._suppressed[bucket] += 1
                return False
            self._in_window += 1
        logger.log(level, msg, *args)
        return True

    def drain(self, bucket: str) -> int:
        """Return the lines withheld under ``bucket`` since the last
        call, and reset its count."""
        if bucket not in self._suppressed:
            raise ValueError("unknown rate-limit bucket")
        with self._lock:
            n = self._suppressed[bucket]
            self._suppressed[bucket] = 0
        return n
