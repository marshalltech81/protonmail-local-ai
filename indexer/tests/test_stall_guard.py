"""Tests for src/stall_guard.py — ends a worker stuck on one message (#235)."""

import threading

from src.database import Database
from src.queue import REASON_INITIAL_SCAN, IndexingQueue
from src.stall_guard import StallGuard


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _running_queue(tmp_path, clock: _Clock) -> IndexingQueue:
    db = Database(tmp_path / "q.db")
    queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
    queue.enqueue("/m/stuck", REASON_INITIAL_SCAN)
    queue.begin_attempt("/m/stuck")
    # Rebase the recorded start onto the fake clock.
    queue._in_flight = ("/m/stuck", clock.now)
    return queue


def test_idle_worker_is_never_stalled(tmp_path):
    db = Database(tmp_path / "q.db")
    guard = StallGuard(IndexingQueue(db), limit_seconds=60, clock=_Clock())
    assert guard.stalled() is None


def test_step_within_the_limit_is_not_stalled(tmp_path):
    clock = _Clock()
    guard = StallGuard(_running_queue(tmp_path, clock), limit_seconds=60, clock=clock)
    clock.now += 60
    assert guard.stalled() is None


def test_step_past_the_limit_is_stalled(tmp_path):
    clock = _Clock()
    guard = StallGuard(_running_queue(tmp_path, clock), limit_seconds=60, clock=clock)
    clock.now += 61
    assert guard.stalled() == "/m/stuck"


def test_check_exits_the_process_only_when_stalled(tmp_path):
    clock = _Clock()
    exits: list[int] = []
    guard = StallGuard(
        _running_queue(tmp_path, clock), limit_seconds=60, clock=clock, exit_fn=exits.append
    )
    guard.check()
    assert exits == []
    clock.now += 61
    guard.check()
    assert exits == [1]


def test_thread_exits_a_stalled_worker(tmp_path):
    """End to end on a real thread: the guard polls and calls exit_fn
    while the main thread is still 'stuck'."""
    clock = _Clock()
    exited = threading.Event()
    guard = StallGuard(
        _running_queue(tmp_path, clock),
        limit_seconds=60,
        interval_seconds=0.01,
        clock=clock,
        exit_fn=lambda code: exited.set(),
    )
    clock.now += 61
    guard.start()
    assert exited.wait(timeout=5)


def test_refund_waits_while_the_guard_is_exiting(tmp_path):
    """Review round 1: a step that returns just as the guard fires must
    not refund its charge (or charge the next message) before the exit —
    the guard decides and exits while holding the in-flight lock."""
    clock = _Clock()
    queue = _running_queue(tmp_path, clock)
    clock.now += 61
    finished = threading.Event()
    observed: dict = {}

    def exit_fn(code):
        refund = threading.Thread(target=lambda: (queue.end_attempt("/m/stuck"), finished.set()))
        refund.start()
        observed["refund_blocked"] = not finished.wait(timeout=0.2)
        observed["attempts"] = queue.db.queue_get_attempts("/m/stuck")

    StallGuard(queue, limit_seconds=60, clock=clock, exit_fn=exit_fn).check()

    assert observed == {"refund_blocked": True, "attempts": 1}
    assert finished.wait(timeout=5)
