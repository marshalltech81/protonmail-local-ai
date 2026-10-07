"""The MCP server's shared rate-limited logger (#889).

The ``/mcp`` rejection log built on it is covered end to end in
``test_http_transport.py``; these tests cover the helper itself.
"""

from __future__ import annotations

import logging
import threading

import pytest
from src.lib.rate_limited_log import RateLimitedLog

MARKER = "SYNTHETIC_CLIENT_MARKER"
log = logging.getLogger("mcp.test.rate_limited")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _limiter(clock: _Clock) -> RateLimitedLog:
    return RateLimitedLog(
        log,
        ("alpha", "beta"),
        60.0,
        first_msg="synthetic first: key=%s",
        summary_msg="synthetic summary in the last %ds: %s",
        clock=clock,
    )


def _messages(caplog, prefix: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == log.name and r.getMessage().startswith(prefix)
    ]


def test_first_per_key_then_summary_after_the_window(caplog):
    clock = _Clock()
    limiter = _limiter(clock)
    with caplog.at_level(logging.INFO):
        for _ in range(4):
            limiter.record("alpha")
        limiter.record("beta")
        clock.now += 59.9
        limiter.record("alpha")
        assert _messages(caplog, "synthetic summary") == []
        clock.now += 0.1  # exactly one interval after the window began
        limiter.record("beta")
    assert _messages(caplog, "synthetic first") == [
        "synthetic first: key=alpha",
        "synthetic first: key=beta",
        "synthetic first: key=beta",
    ]
    assert _messages(caplog, "synthetic summary") == [
        "synthetic summary in the last 60s: alpha=5 beta=1"
    ]
    assert {r.levelno for r in caplog.records if r.name == log.name} == {logging.WARNING}


def test_a_window_without_repeats_is_not_summarised(caplog):
    clock = _Clock()
    limiter = _limiter(clock)
    with caplog.at_level(logging.INFO):
        limiter.record("alpha")
        limiter.record("beta")
        clock.now += 120
        limiter.record("alpha")
    assert _messages(caplog, "synthetic summary") == []
    assert len(_messages(caplog, "synthetic first")) == 3


def test_unknown_key_is_rejected_without_its_value(caplog):
    limiter = _limiter(_Clock())
    with caplog.at_level(logging.INFO), pytest.raises(ValueError) as info:
        limiter.record(MARKER)
    assert MARKER not in str(info.value)
    assert MARKER not in caplog.text
    # State stays the fixed keys.
    assert set(limiter._counts) == {"alpha", "beta"}


def test_counts_are_exact_across_threads(caplog):
    clock = _Clock()
    limiter = _limiter(clock)

    def hammer(key: str) -> None:
        for _ in range(250):
            limiter.record(key)

    with caplog.at_level(logging.INFO):
        workers = [
            threading.Thread(target=hammer, args=("alpha" if i % 2 else "beta",)) for i in range(8)
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        assert len(_messages(caplog, "synthetic first")) == 2
        clock.now += 61
        limiter.record("alpha")
    assert _messages(caplog, "synthetic summary") == [
        "synthetic summary in the last 61s: alpha=1000 beta=1000"
    ]
