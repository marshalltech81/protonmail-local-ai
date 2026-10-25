"""The evidence-run driver (#1767, procedure v5 section 4 and 7): G1 to G8
composed from frozen inputs, every stop rule, in-memory-only state, the
frozen-list refusal, G7's order and baselines, the budgets and the canary.

Everything runs against the in-memory ``SimulatedWorld`` on a virtual
clock: no account, no network, no real waiting."""

import builtins
import inspect
import json
import os
import re
import shutil

import pytest
from evidence_run import driver, export, frozen, runner
from evidence_run import probe as probe_mod
from evidence_run.runner import Limits, Refused
from evidence_run.simulated import DEFAULT_H11, STEP_EFFECTS, Scenario, SimulatedWorld

SERVICE_RULES = [{"name": "welcome", "sender_domain": "service.example"}]


def _frozen_dir(tmp_path, *, edit=None):
    directory = tmp_path / "frozen"
    shutil.copytree(frozen.FROZEN_DIR, directory)
    if edit:
        edit(directory)
    path = directory / "service_messages.json"
    data = json.loads(path.read_text())
    data["rules"], data["frozen"] = SERVICE_RULES, True
    data["frozen_hash"] = frozen.service_hash(SERVICE_RULES)
    path.write_text(json.dumps(data))
    return directory


@pytest.fixture
def inputs(tmp_path):
    return frozen.load(_frozen_dir(tmp_path))


def _run(inputs, scenario=None, *, world=None, limits=None, groups=driver.GROUPS, **kwargs):
    clock = driver.VirtualClock()
    world = world or SimulatedWorld(scenario)
    drv = driver.Driver(
        world,
        inputs,
        clock=clock,
        sleep=clock.sleep,
        limits=limits,
        isolation_checked=True,
        tls_checked=True,
        **kwargs,
    )
    return drv, drv.run(groups)


def _tweak_fixtures(**plan):
    def edit(directory):
        path = directory / "fixtures.json"
        data = json.loads(path.read_text())
        for group, values in plan.items():
            data["groups"][group].update(values)
        path.write_text(json.dumps(data))

    return edit


# ---- the whole run ------------------------------------------------------


def test_the_default_dry_run_completes_in_order_with_nothing_failed(inputs):
    drv, result = _run(inputs)
    assert result.stop is None
    export.validate_export(result.export)
    assert result.states["P1"] == result.states["P2"] == "observed"
    assert result.states["P4"] == result.states["P5"] == result.states["P9"] == "observed"
    assert result.states["P7"] == result.states["P8"] == result.states["P11"] == "observed"
    assert result.states["P10"] == result.states["P12"] == "observed"
    # No reviewed trigger for event rebuild (e) or primary-address change (i):
    # unobservable, counted, not a pass and not a failure.
    assert result.states["P6"] == result.states["P3"] == "unobservable"
    assert result.export["counts"]["steps_unobservable"] == 2
    assert result.export["booleans"]["all_predictions_held"] is False
    assert set(result.export["timings"]) == {f"g{n}_seconds" for n in range(1, 9)}


def test_with_every_step_triggerable_every_prediction_is_observed(inputs):
    drv, result = _run(inputs, Scenario(unavailable_steps=frozenset()))
    assert result.stop is None
    assert set(result.states.values()) == {"observed"}
    assert result.export["booleans"]["all_predictions_held"] is True


def test_groups_must_run_in_order(inputs):
    with pytest.raises(runner.ConfigError):
        _run(inputs, groups=("g2", "g1"))


def test_the_simulated_h11_defaults_equal_the_frozen_expectations(inputs):
    assert DEFAULT_H11 == inputs.h11_expect


def test_every_stop_token_raised_in_the_driver_is_in_its_vocabulary():
    raised = set(re.findall(r'StopRun\("([a-z_]+)"\)', inspect.getsource(driver)))
    assert raised <= driver.STOP_TOKENS
    assert {"world_error", "probe_failed", "identifier_in_output"} <= driver.STOP_TOKENS


# ---- the frozen list ----------------------------------------------------


def test_an_account_stage_run_is_refused_until_the_service_list_is_frozen():
    world = SimulatedWorld()
    with pytest.raises(Refused) as refused:
        driver.Driver(world, frozen.load())  # the committed placeholder
    assert refused.value.token == "service_list_not_frozen"
    assert (world.sends, world.syncs) == (0, 0)  # nothing was touched


def test_a_dry_run_needs_no_frozen_list_because_it_touches_no_account():
    clock = driver.VirtualClock()
    drv = driver.Driver(
        SimulatedWorld(),
        frozen.load(),
        clock=clock,
        sleep=clock.sleep,
        account_stage=False,
        isolation_checked=True,
        tls_checked=True,
    )
    assert drv.run().stop is None


# ---- stop rules ---------------------------------------------------------


def _stopped(result, token, group):
    assert result.stop == (token, group)
    assert result.export["stop"] == {"reason": token, "group": group}
    assert result.export["booleans"]["stopped_early"] is True
    export.validate_export(result.export)


def test_the_first_failed_prediction_stops_the_run_and_later_groups_never_run(inputs):
    drv, result = _run(inputs, Scenario(proton_id_changes_on=frozenset({"move"})))
    _stopped(result, "prediction_failed", "g3")
    assert result.states["P3"] == "failed"
    assert result.states["P4"] == "predicted"  # G4 never started
    assert "g4_seconds" not in result.export["timings"]


def test_a_message_not_on_the_service_list_is_an_unexplained_stop(inputs):
    _, result = _run(inputs, Scenario(foreign_after_sends=1))
    _stopped(result, "unexplained_message", "g1")


def test_an_expected_service_message_is_counted_and_does_not_stop(inputs):
    _, result = _run(inputs, Scenario(service_after_sends=1, unavailable_steps=frozenset()))
    assert result.stop is None
    assert result.export["counts"]["service_messages"] == 1


def test_a_test_message_from_a_domain_that_is_not_ours_is_a_foreign_party_stop(inputs):
    _, result = _run(inputs, Scenario(foreign_party_after_sends=2))
    _stopped(result, "foreign_party", "g1")


def test_an_account_other_than_a_b_or_c_stops_the_run(inputs):
    _, result = _run(inputs, Scenario(extra_account_after_sends=3))
    _stopped(result, "foreign_party", "g1")


@pytest.mark.parametrize("warning", ["rate_limit", "abuse", "terms"])
def test_a_provider_warning_stops_the_run(inputs, warning):
    _, result = _run(inputs, Scenario(warning_after_sends=5, warning_token=warning))
    _stopped(result, "provider_warning", "g1")


def test_a_refused_submission_is_treated_as_a_provider_warning(inputs):
    _, result = _run(inputs, Scenario(send_result="refused"))
    _stopped(result, "provider_warning", "g1")


def test_a_failed_isolation_test_stops_the_run_before_the_group(inputs):
    drv, result = _run(inputs, Scenario(isolation_fails_on_call=3))
    _stopped(result, "isolation_failed", "g3")
    assert result.export["booleans"]["isolation_passed"] is False


def test_an_exhausted_group_clock_stops_the_run(tmp_path):
    inputs = frozen.load(
        _frozen_dir(tmp_path, edit=_tweak_fixtures(g1={"batches": 0, "operations": 1}))
    )
    _, result = _run(inputs)
    _stopped(result, "clock_exhausted", "g1")


def test_a_message_that_never_arrives_is_unobservable_and_stops_the_run(inputs):
    never = frozenset({"SYNTH-r1g1-7"})
    drv, result = _run(inputs, Scenario(never_deliver=never))
    _stopped(result, "convergence_timeout", "g1")
    assert result.states["P1"] == "unobservable"


def test_the_attempt_ceiling_counts_every_submission_and_stops(inputs):
    drv, result = _run(inputs, limits=Limits(max_attempts=10))
    _stopped(result, "attempt_budget_exhausted", "g1")
    assert result.export["counts"]["submission_attempts"] == 10


def test_a_failing_connection_retries_twice_then_stops(inputs):
    drv, result = _run(inputs, Scenario(send_result="connection_failed"))
    _stopped(result, "retry_limit", "g1")
    assert result.export["counts"]["retries"] == 2
    assert result.export["counts"]["submission_attempts"] == 3  # first try and two retries


def test_an_ambiguous_outcome_is_counted_and_never_resent(inputs):
    drv, result = _run(inputs, Scenario(ambiguous_sends=3, unavailable_steps=frozenset()))
    assert result.stop is None
    counts = result.export["counts"]
    assert counts["ambiguous_outcomes"] == 3 and counts["retries"] == 0


def test_g6_stops_when_its_unique_bytes_exceed_the_cap(inputs):
    _, result = _run(inputs, limits=Limits(g6_unique_bytes=1024 * 1024))
    _stopped(result, "unique_bytes_exhausted", "g6")


def test_g6_unique_bytes_are_counted_apart_from_transfer_bytes(inputs):
    _, result = _run(inputs)
    counts = result.export["counts"]
    assert counts["unique_bytes"] == 200 * 262144  # the 52,428,800 of the plan
    assert counts["transfer_bytes"] > counts["unique_bytes"]  # headers and passes on top


def test_a_world_that_needs_a_restart_makes_the_run_stop_and_ask(inputs):
    _, result = _run(inputs, Scenario(restart_after_sends=4))
    _stopped(result, "state_not_spannable", "g1")


def test_a_replaced_probe_is_lost_state_and_stops_the_run(inputs):
    class Swapper(SimulatedWorld):
        drv = None

        def send(self, route, raw):
            if self.sends == 2 and self.drv is not None:
                self.drv.probe = probe_mod.Probe()  # a restart that loses every digest
            return super().send(route, raw)

    world = Swapper()
    clock = driver.VirtualClock()
    drv = driver.Driver(world, inputs, clock=clock, sleep=clock.sleep, isolation_checked=True)
    world.drv = drv
    _stopped(drv.run(), "probe_state_lost", "g1")


def test_a_declined_operator_step_stops_the_run(inputs):
    class Declines(SimulatedWorld):
        def operator(self, step):
            return "declined" if step == "c1" else super().operator(step)

    _, result = _run(inputs, world=Declines())
    _stopped(result, "operator_declined", "g7")


def test_a_world_error_is_a_fixed_stop(inputs):
    from evidence_run.world import WorldError

    class Breaks(SimulatedWorld):
        def sync(self):
            raise WorldError("any")

    _, result = _run(inputs, world=Breaks())
    _stopped(result, "world_error", "g1")


def test_an_export_that_would_carry_an_identifier_is_replaced_by_the_stop_alone(
    inputs, monkeypatch
):
    canary = "SYNTHCANARY1767"
    monkeypatch.setattr(driver.Driver, "_extra_counts", lambda self: {canary: 1})
    _, result = _run(inputs)
    _stopped(result, "identifier_in_output", "g8")
    assert canary not in json.dumps(result.export)
    assert set(result.export["booleans"]) == {"stopped_early"}


# ---- each prediction can fail, and fails closed -------------------------


@pytest.mark.parametrize(
    "scenario, prediction, group",
    [
        (Scenario(dup_internal_in_control=True), "P1", "g1"),
        (Scenario(forged_internal_survives=True), "P2", "g2"),
        (Scenario(forged_gluon_survives=True), "P2", "g2"),
        (Scenario(draft_replace_changes_id=False), "P4", "g4"),
        (Scenario(draft_send_keeps_id=False), "P4", "g4"),
        (Scenario(append_distinct_ids=False), "P5", "g5"),
        (Scenario(h11={**DEFAULT_H11, "a": "refused"}), "P5", "g5"),
        (Scenario(header_read_sets_flags=1), "P9", "g6"),
        (Scenario(membership_ok=False), "P9", "g6"),
        (Scenario(race_neither=1), "P10", "g6"),
        (Scenario(race_id_changed=True), "P10", "g6"),
        (Scenario(file_drift=True), "P11", "g3"),
        (Scenario(fresh_drift=True), "P11", "g3"),
        (Scenario(step_effects={**STEP_EFFECTS, "b": set()}), "P6", "g7"),
        (Scenario(step_changes_proton_id=frozenset({"a"})), "P6", "g7"),
        (Scenario(step_effects={**STEP_EFFECTS, "g": set()}), "P7", "g7"),
        (Scenario(uid_reuse=True), "P3", "g8"),
        (Scenario(flag_scope_ok=False), "P3", "g8"),
    ],
)
def test_a_prediction_that_does_not_hold_is_failed_and_stops(inputs, scenario, prediction, group):
    scenario.unavailable_steps = frozenset()
    _, result = _run(inputs, scenario)
    _stopped(result, "prediction_failed", group)
    assert result.states[prediction] == "failed"


def test_a_route_that_does_not_carry_the_fields_is_unobservable_for_p2_not_a_pass(inputs):
    scenario = Scenario(route_preserves={"B": True, "C": False, "A2": None})
    _, result = _run(inputs, scenario)
    assert result.stop is None
    booleans = result.export["booleans"]
    assert booleans["route_b_preserved"] is True and booleans["route_c_preserved"] is False
    assert "route_a2_preserved" not in booleans  # cannot be checked: no boolean
    assert result.states["P2"] == "observed"  # judged on the one route that carries them


def test_with_no_route_carrying_the_fields_p2_is_unobservable(inputs):
    scenario = Scenario(route_preserves={"B": False, "C": None, "A2": False})
    _, result = _run(inputs, scenario)
    assert result.stop is None
    assert result.states["P2"] == "unobservable"


def test_the_c_route_check_goes_through_the_world_before_any_forged_send(inputs):
    calls = []

    class Spy(SimulatedWorld):
        def route_preserves(self, route, raw):
            calls.append(("check", route, b"X-Pm-Internal-Id: SYNTHFORGED-" in raw))
            return super().route_preserves(route, raw)

        def send(self, route, raw):
            calls.append(("send", route, b"SYNTHFORGED" in raw))
            return super().send(route, raw)

    _run(inputs, world=Spy(), groups=("g1", "g2"))
    g2 = [c for c in calls if c[0] == "check" or (c[0] == "send" and c[2])]
    assert [c[:2] for c in g2[:3]] == [("check", "B"), ("check", "C"), ("check", "A2")]
    assert all(c[2] for c in g2 if c[0] == "check")  # the check message carries forged fields


# ---- G7: order and baselines --------------------------------------------


def test_g7_runs_the_steps_in_the_fixed_order_with_a_baseline_before_each(inputs):
    events = []

    class Log(SimulatedWorld):
        def served(self, folder=None):
            events.append("served")
            return super().served(folder)

        def operator(self, step):
            events.append(f"op:{step}")
            return super().operator(step)

    world = Log()
    drv, result = _run(inputs, world=world, groups=driver.GROUPS[:7])
    assert world.operator_log == ["a", "b", "c1", "c2", "d", "e", "f", "g", "h", "i"]
    # The snapshot taken for each step's baseline comes right before it.
    for index, event in enumerate(events):
        if event.startswith("op:"):
            assert events[index - 1] == "served"
    assert [s for s in drv.snapshots if s.endswith(".base")] == [
        f"g7.{step}.base" for step in ["a", "b", "c1", "c2", "d", "e", "f", "g", "h", "i"]
    ]


def test_each_g7_step_is_judged_against_the_state_the_previous_step_left(inputs):
    # Step d changes the Gluon ID and UID; the next step (e) expects its own
    # change from d's result, so a step that changed nothing relative to its
    # own baseline holds even though it differs from the start of G7.
    drv, result = _run(inputs, Scenario(unavailable_steps=frozenset()), groups=driver.GROUPS[:7])
    assert result.stop is None and result.states["P6"] == "observed"


def test_an_unavailable_g7_step_is_unobservable_and_counted(inputs):
    drv, result = _run(inputs, Scenario(unavailable_steps=frozenset({"e", "h", "i"})))
    assert result.states["P8"] == "unobservable"
    assert result.export["counts"]["steps_unobservable"] == 3
    assert result.export["counts"]["steps_observed"] == 7


# ---- state stays in memory ----------------------------------------------


def test_nothing_is_written_to_disk_but_the_allowlisted_export(tmp_path, monkeypatch):
    out = tmp_path / "export.json"
    opened = []
    real_open, real_os_open = builtins.open, os.open

    def spy_open(file, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax+"):
            opened.append(("open", os.fspath(file)))
        return real_open(file, mode, *a, **k)

    def spy_os_open(path, flags, *a, **k):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            opened.append(("os.open", os.fspath(path)))
        return real_os_open(path, flags, *a, **k)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    assert driver.main(["--dry-run", "--export", str(out)]) == 0
    monkeypatch.undo()
    assert {path for _, path in opened} == {str(out)}
    assert (out.stat().st_mode & 0o777) == 0o600
    export.validate_export(json.loads(out.read_text()))
    assert [p.name for p in tmp_path.iterdir()] == ["export.json"]


def test_the_probe_state_is_never_part_of_the_export_or_the_result(inputs):
    drv, result = _run(inputs)
    text = json.dumps(result.export)
    assert "digest" not in text and "key" not in text.replace("keys", "")
    assert not hasattr(result, "probe")


# ---- the canary ---------------------------------------------------------


def test_no_identifier_address_subject_or_token_reaches_the_export(inputs):
    _, result = _run(inputs, Scenario(service_after_sends=1, unavailable_steps=frozenset()))
    text = json.dumps(result.export)
    for marker in (
        "SYNTHPM",
        "SYNTHGLUON",
        "SYNTHFORGED",
        "SYNTH-r1",
        "sender.example",
        "mail.example",
        "service.example",
        "Recovered Messages",
        "INBOX",
        "@",
    ):
        assert marker not in text, marker


# ---- the command line ---------------------------------------------------


def test_the_command_line_refuses_live_without_an_approval(tmp_path, capsys):
    assert driver.main(["--live"]) == 2
    assert "refused: service_list_not_frozen" in capsys.readouterr().err


def test_the_command_line_refuses_live_without_a_token_even_with_a_frozen_list(tmp_path, capsys):
    directory = _frozen_dir(tmp_path)
    assert driver.main(["--live", "--frozen-dir", str(directory)]) == 2
    assert "refused: not_authorized" in capsys.readouterr().err


def test_the_command_line_wants_exactly_one_of_dry_run_and_live(capsys):
    assert driver.main([]) == 2
    assert "refused: choose_dry_run_or_live" in capsys.readouterr().err
    assert driver.main(["--dry-run", "--live"]) == 2


def test_the_hashes_the_owner_records_are_printed(capsys, inputs):
    assert driver.main(["--print-bundle-hash"]) == 0
    assert capsys.readouterr().out.strip() == frozen.load().bundle_hash
    assert driver.main(["--service-hash"]) == 0
    assert capsys.readouterr().out.strip() == frozen.service_hash(frozen.load().service_rules)


def test_a_dry_run_that_stops_exits_nonzero_and_exports_only_the_stop(tmp_path, monkeypatch):
    # (The simulated world has no failing default; the CLI exit code follows
    # the result.) Run the driver directly with a failing scenario instead.
    from evidence_run import simulated

    monkeypatch.setattr(
        simulated, "SimulatedWorld", lambda: SimulatedWorld(Scenario(foreign_after_sends=1))
    )
    out = tmp_path / "export.json"
    assert driver.main(["--dry-run", "--export", str(out)]) == 1
    assert json.loads(out.read_text())["stop"]["reason"] == "unexplained_message"


# ---- P11: retained files and fresh downloads ----------------------------


def test_p11_compares_the_retained_files_and_a_fresh_download_after_g3_and_g7(inputs):
    fresh_calls = []

    class Spy(SimulatedWorld):
        def fresh_files(self, folder):
            fresh_calls.append(folder)
            return super().fresh_files(folder)

    drv, result = _run(inputs, Scenario(unavailable_steps=frozenset()), world=Spy())
    assert result.states["P11"] == "observed"
    assert "INBOX" in fresh_calls and len(fresh_calls) >= 2  # after G3 and after G7
    # Each fresh download is one more supervised isync run, counted.
    assert result.export["counts"]["isync_commands"] >= len(fresh_calls)


def test_a_fresh_download_that_cannot_be_made_leaves_p11_unobservable_not_a_pass(inputs):
    drv, result = _run(inputs, Scenario(fresh_available=False))
    assert result.stop is None
    assert result.states["P11"] == "unobservable"


@pytest.mark.parametrize("missing", ["retained", "fresh", "all"])
def test_p11_requires_every_eligible_file(inputs, missing):
    from dataclasses import replace

    from evidence_run import fixtures

    class Missing(SimulatedWorld):
        def served(self, folder=None):
            items = super().served(folder)
            if missing == "all":
                return []
            if missing == "retained" and items:
                items[0] = replace(items[0], file_bytes=None)
            return items

        def fresh_files(self, folder):
            files = super().fresh_files(folder)
            if missing == "fresh" and files:
                files.pop(next(iter(files)))
            return files

    world = Missing()
    for n in (1, 2):
        world.send("B", fixtures.control_message("r1g3", n))
    world.sync()
    drv, _ = _run(inputs, world=world, groups=())
    for n in (1, 2):
        drv._register(fixtures.control_message("r1g3", n))
    assert drv._p11(("INBOX",)) == "unobservable"


@pytest.mark.parametrize("count", [0, 1])
def test_g7_missing_baseline_is_unobservable(inputs, count, monkeypatch):
    original = driver.Driver._snapshot

    def snapshot(self, label, tokens, folders):
        snaps = original(self, label, tokens, folders)
        return dict(list(snaps.items())[:count]) if label.endswith(".base") else snaps

    monkeypatch.setattr(driver.Driver, "_snapshot", snapshot)
    _, result = _run(inputs, groups=("g7",))
    assert result.export["counts"].get("steps_observed", 0) == 0
    assert result.export["counts"]["steps_unobservable"] == len(inputs.g7_steps)


def test_g8_checks_all_uid_history(inputs):
    drv, _ = _run(inputs, groups=())
    drv._continuity = {
        "a": [("INBOX", 1, 1), ("INBOX", 2, 1)],
        "b": [("INBOX", 1, 1), ("INBOX", 3, 1)],
    }
    result = drv.run(("g8",))
    assert result.stop == ("prediction_failed", "g8")


@pytest.mark.parametrize("missing", [False, True])
def test_g8_requires_spam_arrival_and_stable_identity(inputs, missing):
    class Spam(SimulatedWorld):
        def served(self, folder=None):
            return [] if missing and folder == "Spam" else super().served(folder)

    _, result = _run(
        inputs, world=Spam(Scenario(proton_id_changes_on=frozenset({"spam"}))), groups=("g8",)
    )
    assert result.states["P3"] == ("unobservable" if missing else "failed")


@pytest.mark.parametrize(
    "group,scenario,allowed",
    [
        ("g3", Scenario(proton_id_changes_on=frozenset({"move"})), {"move"}),
        ("g4", Scenario(draft_replace_changes_id=False), {"draft_edit", "draft_replace"}),
        ("g5", Scenario(append_distinct_ids=False), set()),
    ],
)
def test_failure_stops_before_later_mutations(inputs, group, scenario, allowed):
    world = SimulatedWorld(scenario)
    _, result = _run(inputs, world=world, groups=(group,))
    assert result.stop == ("prediction_failed", group)
    assert {op for op, _ in world.mutate_log} <= allowed
    if group == "g4":
        assert len(world.msgs) == 1
    if group == "g5":
        assert not result.export["counts"].get("h11_matched", 0)


def test_g1_uses_the_frozen_control_count(tmp_path):
    inputs = frozen.load(_frozen_dir(tmp_path, edit=_tweak_fixtures(g1={"control": 1})))
    world = SimulatedWorld()
    _, result = _run(inputs, world=world, groups=("g1",))
    assert result.stop is None
    assert world.sends == 1 + inputs.fixtures["groups"]["g1"]["incremental"]


def test_untriggered_expected_h11_case_is_unobservable(inputs):
    scenario = Scenario(h11={**DEFAULT_H11, "d": "no_trigger"})
    _, result = _run(inputs, scenario, groups=("g5",))
    assert result.stop is None
    assert result.states["P5"] == "unobservable"


def test_unavailable_initial_isolation_never_exports_a_pass(inputs):
    from evidence_run.world import WorldError

    class Unavailable(SimulatedWorld):
        def isolation_ok(self):
            raise WorldError("isolation_not_configured")

    clock = driver.VirtualClock()
    drv = driver.Driver(Unavailable(), inputs, clock=clock, sleep=clock.sleep)
    result = drv.run(("g1",))
    assert result.stop == ("world_error", "g1")
    assert result.export["booleans"]["isolation_passed"] is False


def test_unavailable_later_isolation_does_not_reuse_an_earlier_pass(inputs):
    from evidence_run.world import WorldError

    class LaterUnavailable(SimulatedWorld):
        def isolation_ok(self):
            if self.isolation_calls:
                raise WorldError("isolation_not_configured")
            return super().isolation_ok()

    _, result = _run(inputs, world=LaterUnavailable(), groups=("g1", "g2"))
    assert result.stop == ("world_error", "g2")
    assert result.export["booleans"]["isolation_passed"] is False


@pytest.mark.parametrize("cold_stop", [False, True])
def test_g6_checks_stop_signals_after_each_header_pass(inputs, cold_stop):
    class WarningAfterPass(SimulatedWorld):
        def __init__(self):
            super().__init__(Scenario(cold_provable=True))
            self.warned = False
            self.passes = []
            self.races = 0

        def header_pass(self, *, cold):
            self.passes.append(cold)
            result = super().header_pass(cold=cold)
            self.warned = cold == cold_stop
            return result

        def warnings(self):
            return ["synthetic_warning"] if self.warned else []

        def race(self, repeats):
            self.races += 1
            return super().race(repeats)

    world = WarningAfterPass()
    _, result = _run(inputs, world=world, groups=("g6",))
    _stopped(result, "provider_warning", "g6")
    assert world.passes == ([False, True] if cold_stop else [False])
    assert world.races == 0


@pytest.mark.parametrize("outcomes", [(), ("old",), ("unknown",) * 20])
def test_g6_missing_or_unrecognized_race_observations_are_unobservable(inputs, outcomes):
    from evidence_run.world import RaceResult

    class IncompleteRace(SimulatedWorld):
        def race(self, repeats):
            return RaceResult(outcomes, False)

    _, result = _run(inputs, world=IncompleteRace(), groups=("g6",))
    assert result.stop is None
    assert result.states["P10"] == "unobservable"
