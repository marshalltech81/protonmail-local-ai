"""Budgets, clocks and stop rules of the runner (#1759, procedure v5
section 2, 3 and 7)."""

import pytest
from evidence_run import runner
from evidence_run.runner import Budget, Limits, StopRun


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_the_limits_are_the_procedures():
    limits = Limits()
    assert limits.max_attempts == 600 and limits.max_retries == 2
    assert limits.max_rate == 10.0 and limits.convergence_seconds == 120.0
    assert limits.plain_fixture_bytes == 64 * 1024
    assert limits.g6_unique_bytes >= 52_428_800


def test_the_attempt_ceiling_counts_retries_and_ambiguous_outcomes_and_stops():
    clock = Clock()
    budget = Budget(Limits(max_attempts=5), clock=clock, sleep=clock.sleep)
    budget.attempt()  # 1: a first try
    budget.attempt(retry=True)  # 2: a retry counts
    budget.attempt(ambiguous=True)  # 3: an ambiguous outcome counts, and is tallied
    budget.attempt()
    budget.attempt()
    assert budget.attempts == 5 and budget.ambiguous == 1 and budget.retries == 1
    with pytest.raises(StopRun) as stop:
        budget.attempt()
    assert stop.value.token == "attempt_budget_exhausted"


def test_one_operation_may_retry_at_most_twice():
    clock = Clock()
    budget = Budget(clock=clock, sleep=clock.sleep)
    op = budget.operation("send-1")
    op.attempt()
    op.retry()
    op.retry()
    with pytest.raises(StopRun) as stop:
        op.retry()
    assert stop.value.token == "retry_limit"


def test_the_rate_is_held_to_ten_operations_per_second():
    clock = Clock()
    budget = Budget(clock=clock, sleep=clock.sleep)
    for _ in range(30):
        budget.attempt()
    # 30 operations at 10 per second cannot finish in under 2.9 seconds.
    assert clock.now >= 2.9 - 1e-9
    assert budget.max_in_any_second <= 10


def test_unique_bytes_are_capped_and_transfers_are_only_counted():
    clock = Clock()
    budget = Budget(Limits(g6_unique_bytes=1000), clock=clock, sleep=clock.sleep)
    budget.add_unique(900)
    budget.add_transfer(10_000_000)  # repeated transfers and copies are not capped here
    assert budget.transfer_bytes == 10_000_000 and budget.unique_bytes == 900
    with pytest.raises(StopRun) as stop:
        budget.add_unique(200)
    assert stop.value.token == "unique_bytes_exhausted"


def test_commands_are_counted_apart_from_submissions():
    budget = Budget()
    budget.command()
    budget.command()
    assert (budget.commands, budget.attempts) == (2, 0)


def test_a_group_clock_is_sized_from_worst_case_waits_not_per_message():
    # 200 messages polled one after another could wait 120 s each:
    assert runner.group_clock_seconds(batches=200, operations=200) == pytest.approx(200 * 120 + 20)
    # which is far past the group ceiling, so that shape is refused:
    with pytest.raises(runner.ConfigError):
        runner.start_group("g6-serial", batches=200, operations=200, limits=Limits())
    # batched polling (4 batches) is sized and accepted:
    clock = Clock()
    group = runner.start_group("g6", batches=4, operations=200, limits=Limits(), clock=clock)
    assert group.seconds == pytest.approx(4 * 120 + 20)


def test_group_clock_exhaustion_is_a_stop_recorded_unobservable():
    clock = Clock()
    group = runner.start_group("g1", batches=1, operations=0, limits=Limits(), clock=clock)
    group.check()
    clock.now = group.seconds + 1
    with pytest.raises(StopRun) as stop:
        group.check()
    assert stop.value.token == "clock_exhausted"


def test_the_first_failed_prediction_stops_the_run():
    run = runner.Run()
    run.record("P1", "predicted")
    run.record("P1", "observed")
    run.record("P2", "unobservable")
    with pytest.raises(StopRun) as stop:
        run.record("P3", "failed")
    assert stop.value.token == "prediction_failed"
    assert run.states == {"P1": "observed", "P2": "unobservable", "P3": "failed"}


def test_a_prediction_state_must_be_one_of_the_four():
    with pytest.raises(ValueError):
        runner.Run().record("P1", "held")


def test_a_message_with_a_registered_token_is_a_test_message():
    run = runner.Run()
    run.correlation.register("SYNTH-r-1")
    assert (
        run.classify(runner.MessageSummary(token="SYNTH-r-1", sender_domain="mail.example"))
        == "test"
    )


def test_a_frozen_service_message_is_recognised_and_counted_apart():
    run = runner.Run()
    run.service_rules.add(
        "welcome", lambda m: m.sender_domain == "proton.example" and m.token is None
    )
    run.service_rules.freeze()
    summary = runner.MessageSummary(token=None, sender_domain="proton.example")
    assert run.classify(summary) == "service"
    assert run.service_counts() == {"welcome": 1}


def test_an_unexplained_message_stops_the_run():
    run = runner.Run()
    run.service_rules.freeze()
    with pytest.raises(StopRun) as stop:
        run.classify(runner.MessageSummary(token=None, sender_domain="other.example"))
    assert stop.value.token == "unexplained_message"
    with pytest.raises(StopRun):
        run.classify(runner.MessageSummary(token="SYNTH-never-sent", sender_domain="mail.example"))


def test_the_service_rules_are_frozen_before_the_first_account_action():
    rules = runner.ServiceRules()
    rules.add("a", lambda m: True)
    rules.freeze()
    with pytest.raises(runner.ConfigError):
        rules.add("b", lambda m: True)
    with pytest.raises(runner.ConfigError):
        runner.Run().classify(runner.MessageSummary(token=None, sender_domain="x"))  # not frozen
