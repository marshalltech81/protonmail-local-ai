"""A plan or run id the generator cannot build is refused before any account
activity; a failure that still occurs mid-run stops with a record."""

from copy import deepcopy
from dataclasses import replace

import pytest
from evidence_run import driver, export, fixtures, frozen, runner
from evidence_run.simulated import SimulatedWorld
from test_driver import _frozen_dir, _run


@pytest.fixture
def inputs(tmp_path):
    return frozen.load(_frozen_dir(tmp_path))


def _edited(inputs, **groups):
    plan = deepcopy(inputs.fixtures)
    for group, values in groups.items():
        plan["groups"][group].update(values)
    return replace(inputs, fixtures=plan)


def _refused(inputs, **kwargs):
    world = SimulatedWorld()
    with pytest.raises(runner.Refused) as refused:
        driver.Driver(world, inputs, **kwargs)
    assert refused.value.token == "frozen_inputs_invalid"
    assert world.sends == 0 and not world.msgs


def _smallest(build, *args, **kwargs):
    for size in range(1, 5000):
        try:
            build(*args, size=size, **kwargs)
            return size
        except fixtures.FixtureError:
            pass
    raise AssertionError("no size builds")


@pytest.mark.parametrize("size", [0, 1, 65_537])
def test_invalid_later_fixture_size_is_refused_before_any_account_activity(inputs, size):
    _refused(_edited(inputs, g2={"size": size}))


@pytest.mark.parametrize("group", ["g1", "g3", "g4", "g5", "g7"])
def test_every_group_size_is_checked_against_the_generator(inputs, group):
    _refused(_edited(inputs, **{group: {"size": 65_537}}))


def test_g6_message_bytes_are_checked_against_the_g6_cap(inputs):
    _refused(_edited(inputs, g6={"message_bytes": fixtures.G6_MESSAGE_BYTES + 1}))
    _refused(_edited(inputs, g6={"message_bytes": 0}))


@pytest.mark.parametrize(
    ("run_id", "accepted"),
    [("r" * 14, True), ("r" * 15, False), ("r" * 16, False), ("bad-run", False)],
)
def test_run_id_must_stay_buildable_once_the_group_name_is_added(inputs, run_id, accepted):
    if accepted:
        driver.Driver(SimulatedWorld(), inputs, run_id=run_id)
    else:
        _refused(inputs, run_id=run_id)


def test_sizes_are_checked_at_every_reachable_number_width(inputs):
    # G1 numbers run 1..25: a size that holds a one-digit number but not a
    # two-digit one, and one that leaves a one-byte filler, are both refused.
    one_digit = _smallest(fixtures.control_message, "r1g1", 1)
    two_digit = _smallest(fixtures.control_message, "r1g1", 10)
    assert two_digit > one_digit
    _refused(_edited(inputs, g1={"size": one_digit}))
    _refused(_edited(inputs, g1={"size": two_digit + 1}))
    driver.Driver(SimulatedWorld(), _edited(inputs, g1={"size": two_digit}))


def test_g3_multipart_threshold_decides_which_layout_is_checked(inputs):
    multipart = _smallest(fixtures.control_message, "r1g3", 1, multipart=True)
    plain = _smallest(fixtures.control_message, "r1g3", 10, multipart=False)
    assert multipart > plain
    driver.Driver(SimulatedWorld(), _edited(inputs, g3={"size": multipart + 8, "multipart": 3}))
    _refused(_edited(inputs, g3={"size": multipart - 1, "multipart": 3}))
    # No multipart message at all: the smaller plain layout is enough.
    driver.Driver(SimulatedWorld(), _edited(inputs, g3={"size": plain + 8, "multipart": 0}))


def test_a_kind_the_plan_never_reaches_is_not_checked(inputs):
    duplicated = _smallest(fixtures.forged_message, "r1g2", 1, "duplicated")
    assert _smallest(fixtures.forged_message, "r1g2", 1, "conflicting") > duplicated
    # per_route 2 reaches forged and duplicated only; 3 reaches conflicting.
    driver.Driver(SimulatedWorld(), _edited(inputs, g2={"size": duplicated, "per_route": 2}))
    _refused(_edited(inputs, g2={"size": duplicated, "per_route": 3}))


def test_preflight_work_does_not_grow_with_counts(inputs, monkeypatch):
    calls = []
    original = fixtures._build

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(fixtures, "_build", counting)
    driver.Driver(SimulatedWorld(), inputs)
    baseline = len(calls)
    calls.clear()
    huge = _edited(inputs, g4={"drafts": 10**9}, g6={"messages": 10**9})
    driver.Driver(SimulatedWorld(), huge)
    assert baseline <= len(calls) <= baseline + 20


def test_every_message_a_run_builds_belongs_to_a_class_the_preflight_checked(inputs, monkeypatch):
    seen: list[tuple] = []
    original = fixtures._build

    def recording(run, n, *, size, multipart, extra, cap):
        key = (run, len(str(n)), size, multipart, tuple(e.replace(f"-{n}", "-N") for e in extra))
        seen.append((key, cap))
        return original(run, n, size=size, multipart=multipart, extra=extra, cap=cap)

    monkeypatch.setattr(fixtures, "_build", recording)
    clock = driver.VirtualClock()
    drv = driver.Driver(
        SimulatedWorld(),
        inputs,
        clock=clock,
        sleep=clock.sleep,
        isolation_checked=True,
        tls_checked=True,
    )
    checked = set(seen)
    seen.clear()
    drv.run()
    built = set(seen)
    assert built and built <= checked
    assert {key[0] for key, _ in built} == {f"r1g{n}" for n in range(1, 9)}


def test_an_unbuildable_message_mid_run_stops_with_a_record(inputs, monkeypatch):
    original = fixtures.control_message

    def late(run, n, **kwargs):
        if run.endswith("g4"):
            raise fixtures.FixtureError("late")
        return original(run, n, **kwargs)

    drv = driver.Driver(SimulatedWorld(), inputs, isolation_checked=True, tls_checked=True)
    monkeypatch.setattr(fixtures, "control_message", late)
    result = drv.run()
    assert result.stop == ("fixture_unbuildable", "g4")
    assert "late" not in str(result.export)
    assert result.export["booleans"]["stopped_early"] is True


def test_the_fixture_stop_token_is_in_both_allowlists():
    assert "fixture_unbuildable" in driver.STOP_TOKENS
    assert driver.STOP_TOKENS <= export.STOP_TOKENS


def test_a_valid_plan_is_unchanged_by_the_preflight(inputs):
    _, result = _run(inputs)
    assert result.stop is None
