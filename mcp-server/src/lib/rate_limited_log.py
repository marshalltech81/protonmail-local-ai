"""The MCP server's rate-limited logger for repeated warnings (#889).

A remote client can repeat a rejected request as fast as it likes; one
line each would evict the diagnostics that matter from Docker's bounded
log history.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable


class RateLimitedLog:
    """Rate-limited WARNINGs keyed by a fixed set of reasons.

    In each window of ``interval`` seconds the first occurrence of each
    key is logged as ``first_msg % key``; every occurrence is counted.
    When an occurrence arrives after the window ended, a window that had
    more than one occurrence of some key is reported as one
    ``summary_msg % (seconds, "<key>=<count> ...")`` line, the seconds
    being those the window actually covered, and a new window starts. A
    window with no later occurrence is never summarised; its first lines
    are already logged.

    Keys are fixed literals (reason enums), never request values, so
    state is one counter per key, bounded whatever the traffic; an
    unknown key raises with fixed text. The lock makes ``record`` safe
    from any thread; it never awaits, so it is safe on the event loop
    too. Logging happens outside the lock.
    """

    def __init__(
        self,
        logger: logging.Logger,
        keys: tuple[str, ...],
        interval: float,
        *,
        first_msg: str,
        summary_msg: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._keys = keys
        self._interval = interval
        self._first_msg = first_msg
        self._summary_msg = summary_msg
        self._clock = clock
        self._lock = threading.Lock()
        self._window_start: float | None = None
        self._counts = dict.fromkeys(keys, 0)

    def record(self, key: str) -> None:
        if key not in self._counts:
            raise ValueError("unknown rate-limit key")
        summary: tuple[int, dict[str, int]] | None = None
        with self._lock:
            now = self._clock()
            if self._window_start is None or now - self._window_start >= self._interval:
                if self._window_start is not None and any(n > 1 for n in self._counts.values()):
                    summary = (int(now - self._window_start), dict(self._counts))
                self._window_start = now
                self._counts = dict.fromkeys(self._keys, 0)
            self._counts[key] += 1
            first = self._counts[key] == 1
        if summary is not None:
            elapsed, counts = summary
            self._logger.warning(
                self._summary_msg,
                elapsed,
                " ".join(f"{name}={n}" for name, n in counts.items() if n),
            )
        if first:
            self._logger.warning(self._first_msg, key)


class ArgumentRejections(RateLimitedLog):
    """Rate-limited ``rejected invalid argument: <tool>.<field>``
    WARNINGs for the tools' argument checks (#1039).

    Both parts of the key are fixed literals: ``tool`` is named at the
    call site, and a field outside ``FIELDS`` (the names the
    ``InvalidFilterError`` raisers and the tools' own checks use) is
    counted as ``other`` rather than raising, so a new field can never
    change what the caller receives. ``fields`` replaces ``FIELDS`` for
    a caller with its own fixed set (the registered tools' parameter
    names, #1131); ``other`` is always added.
    """

    FIELDS = (
        "date_from",
        "date_to",
        "date_from/date_to",
        "size_min",
        "size_max",
        "size_min/size_max",
        "cursor",
        "text",
        "authority_class",
        "offset",
        "filter_type",
        "query",
        "other",
    )

    def __init__(
        self,
        logger: logging.Logger,
        tools: tuple[str, ...],
        interval: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        *,
        fields: tuple[str, ...] = FIELDS,
    ) -> None:
        self._fields = tuple(dict.fromkeys((*fields, "other")))
        super().__init__(
            logger,
            tuple(f"{tool}.{field}" for tool in tools for field in self._fields),
            interval,
            first_msg="rejected invalid argument: %s",
            summary_msg="rejected invalid arguments in the last %ds: %s",
            clock=clock,
        )

    def reject(self, tool: str, field: str) -> None:
        self.record(f"{tool}.{field if field in self._fields else 'other'}")
