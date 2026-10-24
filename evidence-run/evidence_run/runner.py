"""Budgets, clocks and stop rules (procedure v5 sections 2, 3 and 7).

Everything here is deterministic given an injected clock, so the limits
are tested without waiting. A ``StopRun`` ends the run at the first failed
prediction, the first unexplained message or the first exhausted budget;
the owner's first-failure rule has no override in code.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from . import export


class StopRun(Exception):
    """The run stops. ``token`` is a fixed reason; nothing else is carried."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


class ConfigError(Exception):
    """The run is configured in a way the procedure does not allow."""


@dataclass(frozen=True)
class Limits:
    max_attempts: int = 600  # submissions, retries and ambiguous outcomes included
    max_retries: int = 2  # per operation
    max_rate: float = 10.0  # operations per second, all clients together
    convergence_seconds: float = 120.0  # one batch's wait for delivery
    plain_fixture_bytes: int = 64 * 1024
    # G6's unique payload is 200 x 256 KiB = 52,428,800 bytes; the cap leaves
    # room for the fixtures' own headers and nothing more.
    g6_unique_bytes: int = 54 * 1024 * 1024
    group_clock_max: float = 3600.0  # a group sized beyond this is refused
    sync_timeout: float = 600.0
    sync_output_cap: int = 1 << 20


class Operation:
    def __init__(self, budget: Budget, name: str) -> None:
        self._budget = budget
        self.name = name
        self.retries = 0

    def attempt(self) -> None:
        self._budget.attempt()

    def retry(self) -> None:
        if self.retries >= self._budget.limits.max_retries:
            raise StopRun("retry_limit")
        self.retries += 1
        self._budget.attempt(retry=True)


class Budget:
    def __init__(
        self,
        limits: Limits | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.limits = limits or Limits()
        self._clock, self._sleep = clock, sleep
        self.attempts = self.retries = self.ambiguous = self.commands = 0
        self.unique_bytes = self.transfer_bytes = 0
        self._stamps: list[float] = []

    def _pace(self) -> None:
        """Hold consecutive operations at least 1/max_rate seconds apart, so
        no one-second window holds more than max_rate of them."""
        if self._stamps:
            wait = self._stamps[-1] + 1.0 / self.limits.max_rate - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._stamps.append(self._clock())

    def attempt(self, *, retry: bool = False, ambiguous: bool = False) -> None:
        """Count one submission attempt: a first try, a retry, or one whose
        outcome is unknown all spend the same ceiling."""
        if self.attempts >= self.limits.max_attempts:
            raise StopRun("attempt_budget_exhausted")
        self._pace()
        self.attempts += 1
        self.retries += int(retry)
        self.ambiguous += int(ambiguous)

    def operation(self, name: str) -> Operation:
        return Operation(self, name)

    def command(self) -> None:
        """Count one supervised command (an isync run); not a submission."""
        self.commands += 1

    def add_unique(self, size: int) -> None:
        if self.unique_bytes + size > self.limits.g6_unique_bytes:
            raise StopRun("unique_bytes_exhausted")
        self.unique_bytes += size

    def add_transfer(self, size: int) -> None:
        self.transfer_bytes += size

    @property
    def max_in_any_second(self) -> int:
        best, eps = 0, 1e-6
        for i, start in enumerate(self._stamps):
            best = max(best, sum(1 for t in self._stamps[i:] if t < start + 1.0 - eps))
        return best

    def counts(self) -> dict[str, int]:
        return {
            "submission_attempts": self.attempts,
            "retries": self.retries,
            "ambiguous_outcomes": self.ambiguous,
            "isync_commands": self.commands,
            "unique_bytes": self.unique_bytes,
            "transfer_bytes": self.transfer_bytes,
        }


def group_clock_seconds(
    *, batches: int, convergence: float = 120.0, operations: int = 0, rate: float = 10.0
) -> float:
    """A group's clock from its worst-case waits: every batch may wait out
    one convergence period, and the operations are paced at ``rate``."""
    return batches * convergence + operations / rate


class GroupClock:
    def __init__(self, name: str, seconds: float, clock: Callable[[], float]) -> None:
        self.name, self.seconds, self._clock = name, seconds, clock
        self._start = clock()

    def check(self) -> None:
        if self._clock() - self._start > self.seconds:
            raise StopRun("clock_exhausted")


def start_group(
    name: str,
    *,
    batches: int,
    operations: int,
    limits: Limits | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> GroupClock:
    limits = limits or Limits()
    seconds = group_clock_seconds(
        batches=batches,
        convergence=limits.convergence_seconds,
        operations=operations,
        rate=limits.max_rate,
    )
    if seconds > limits.group_clock_max:
        raise ConfigError("a group's worst-case waits exceed the group clock ceiling")
    return GroupClock(name, seconds, clock)


@dataclass(frozen=True)
class MessageSummary:
    """What the classifier may look at: the correlation token the message
    carries, if any, and the sender's domain. Never a body or a subject."""

    token: str | None
    sender_domain: str | None


class Correlation:
    def __init__(self) -> None:
        self._tokens: set[str] = set()

    def register(self, token: str) -> None:
        self._tokens.add(token)

    def knows(self, token: str | None) -> bool:
        return token is not None and token in self._tokens


class ServiceRules:
    """The expected Proton service messages, frozen before the first
    account action (procedure 1B.1)."""

    def __init__(self) -> None:
        self._rules: dict[str, Callable[[MessageSummary], bool]] = {}
        self.frozen = False
        self._counts: Counter[str] = Counter()

    def add(self, name: str, predicate: Callable[[MessageSummary], bool]) -> None:
        if self.frozen:
            raise ConfigError("the service message list is frozen")
        self._rules[name] = predicate

    def freeze(self) -> None:
        self.frozen = True

    def match(self, summary: MessageSummary) -> str | None:
        for name, predicate in self._rules.items():
            if predicate(summary):
                self._counts[name] += 1
                return name
        return None

    def counts(self) -> dict[str, int]:
        return dict(self._counts)


class Run:
    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.correlation = Correlation()
        self.service_rules = ServiceRules()

    def record(self, prediction: str, state: str) -> None:
        """Record a prediction's state; the first ``failed`` stops the run."""
        if state not in export.PREDICTION_STATES:
            raise ValueError("a prediction is predicted, observed, failed or unobservable")
        self.states[prediction] = state
        if state == "failed":
            raise StopRun("prediction_failed")

    def classify(self, summary: MessageSummary) -> str:
        """``test``, ``service``; anything else stops the run."""
        if self.correlation.knows(summary.token):
            return "test"
        if not self.service_rules.frozen:
            raise ConfigError("the service message list is not frozen")
        if self.service_rules.match(summary) is not None:
            return "service"
        raise StopRun("unexplained_message")

    def service_counts(self) -> dict[str, int]:
        return self.service_rules.counts()
