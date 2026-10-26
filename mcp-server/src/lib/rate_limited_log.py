"""The MCP server's rate-limited logger for repeated warnings (#889).

A remote client can repeat a rejected request as fast as it likes; one
line each would evict the diagnostics that matter from Docker's bounded
log history.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from collections.abc import Callable


def _schedule(delay: float, callback: Callable[[], None]) -> Callable[[], None]:
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()
    return timer.cancel


_LIMITERS: weakref.WeakSet[RateLimitedLog] = weakref.WeakSet()
_REGISTRY_LOCK = threading.Lock()


def flush_all() -> None:
    """Drain registered limiters, including tool-closure instances, on shutdown."""
    with _REGISTRY_LOCK:
        limiters = tuple(_LIMITERS)
    for limiter in limiters:
        limiter.flush()


class RateLimitedLog:
    """First WARNING per fixed key/window; count every occurrence.

    A repeated key lazily arms one cancellable daemon timer. Its summary
    includes the first occurrence and fires even if requests stop.
    ``flush`` atomically drains and cancels; recovery retains the warning budget.
    Logging stays outside the lock. Stale callbacks cannot drain a later
    window. Keys and state are bounded by the caller's fixed reason set.
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
        scheduler: Callable[[float, Callable[[], None]], Callable[[], None]] = _schedule,
    ) -> None:
        self._logger = logger
        self._keys = keys
        self._interval = interval
        self._first_msg = first_msg
        self._summary_msg = summary_msg
        self._clock = clock
        self._scheduler = scheduler
        self._lock = threading.Lock()
        self._window_start: float | None = None
        self._counts = dict.fromkeys(keys, 0)
        self._cancel: Callable[[], None] | None = None
        self._generation = 0
        self._episode_active = False
        self._recoveries = 0
        with _REGISTRY_LOCK:
            _LIMITERS.add(self)

    def _drain_locked(self, now: float) -> tuple[int, dict[str, int], int] | None:
        summary = None
        if self._window_start is not None and (
            any(n > 1 for n in self._counts.values()) or self._recoveries > 1
        ):
            summary = (int(now - self._window_start), dict(self._counts), self._recoveries)
        if self._cancel is not None:
            self._cancel()
            self._cancel = None
        self._generation += 1
        self._window_start = None
        self._counts = dict.fromkeys(self._keys, 0)
        self._recoveries = 0
        return summary

    def _emit(self, summary: tuple[int, dict[str, int], int] | None) -> None:
        if summary is not None:
            elapsed, counts, recoveries = summary
            self._logger.warning(
                self._summary_msg,
                elapsed,
                "occurrences "
                + " ".join(f"{name}={n}" for name, n in counts.items() if n)
                + (f" recoveries={recoveries}" if recoveries else ""),
            )

    def _arm_locked(self, now: float) -> None:
        assert self._window_start is not None
        generation = self._generation
        reference = weakref.ref(self)

        def expire() -> None:
            limiter = reference()
            if limiter is not None:
                limiter._expire(generation)

        self._cancel = self._scheduler(max(0.0, self._window_start + self._interval - now), expire)

    def _expire(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation or self._window_start is None:
                return
            now = self._clock()
            if now < self._window_start + self._interval:
                self._arm_locked(now)
                return
            summary = self._drain_locked(now)
        self._emit(summary)

    def record(self, key: str) -> None:
        if key not in self._counts:
            raise ValueError("unknown rate-limit key")
        summary = None
        with self._lock:
            now = self._clock()
            if self._window_start is not None and now - self._window_start >= self._interval:
                summary = self._drain_locked(now)
            if self._window_start is None:
                self._window_start = now
            self._episode_active = True
            self._counts[key] += 1
            first = self._counts[key] == 1
            if not first and self._cancel is None:
                self._arm_locked(now)
        self._emit(summary)
        if first:
            self._logger.warning(self._first_msg, key)

    def flush(self) -> None:
        """Drain pending occurrence counts and cancel the timer atomically."""
        with self._lock:
            summary = self._drain_locked(self._clock())
        self._emit(summary)

    def recover(self) -> None:
        """Report an actual episode end without resetting its warning-rate budget.

        One recovery line per window; repeated transitions are aggregated.
        Timer expiry drains counts, but only a success ends an episode.
        """
        with self._lock:
            if not self._episode_active:
                return
            self._episode_active = False
            now = self._clock()
            summary = None
            if self._window_start is not None and now - self._window_start >= self._interval:
                summary = self._drain_locked(now)
            if self._window_start is None:
                self._window_start = now
            self._recoveries += 1
            first = self._recoveries == 1
            if not first and self._cancel is None:
                self._arm_locked(now)
        self._emit(summary)
        if first:
            self._logger.info("Warning episode recovered: %s", self._first_msg)


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
        "date_basis",
        "size_min",
        "size_max",
        "size_min/size_max",
        "cursor",
        "text",
        "authority_class",
        "offset",
        "expected_text_sha256",
        "filter_type",
        "query",
        "where",
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
