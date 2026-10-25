"""The frozen inputs (#1767, procedure v5 1B.1): committed data files read
by the driver, the service-message list that must be frozen before any
account-stage group, and the approval hash that binds the live run to them."""

import json
import shutil
import stat

import pytest
from evidence_run import frozen, runner


def _copy(tmp_path):
    target = tmp_path / "frozen"
    shutil.copytree(frozen.FROZEN_DIR, target)
    return target


def _freeze(directory, rules=None):
    path = directory / "service_messages.json"
    data = json.loads(path.read_text())
    if rules is not None:
        data["rules"] = rules
    data["frozen"] = True
    data["frozen_hash"] = frozen.service_hash(data["rules"])
    path.write_text(json.dumps(data))


def test_the_committed_inputs_load_and_hold_p1_to_p12_and_g1_to_g8():
    inputs = frozen.load()
    assert sorted(inputs.predictions, key=lambda p: int(p[1:])) == [f"P{n}" for n in range(1, 13)]
    assert set(inputs.fixtures["groups"]) == {f"g{n}" for n in range(1, 9)}
    assert all(p["expected"] == "predicted" for p in inputs.predictions.values())


def test_every_prediction_names_the_row_it_comes_from():
    for prediction in frozen.load().predictions.values():
        assert prediction["basis"].strip() and prediction["text"].strip()


def test_g7_runs_in_the_procedures_fixed_order_with_one_prediction_each():
    steps = frozen.load().g7_steps
    assert [s["id"] for s in steps] == ["a", "b", "c1", "c2", "d", "e", "f", "g", "h", "i"]
    # Disruptive steps are last: re-add, then mode, then primary address.
    assert [s["id"] for s in steps][-3:] == ["g", "h", "i"]
    for step in steps:
        assert step["prediction"] in {"P3", "P6", "P7", "P8"}
        assert set(step["expect"]) == {"proton_id", "gluon_id", "uid", "uidvalidity", "bytes"}
        assert set(step["expect"].values()) <= {"same", "changed", "any"}


def test_the_h11_expectations_cover_all_six_subcases_and_the_recovery_moves():
    expect = frozen.load().h11_expect
    assert set(expect) == {"a", "a2", "b", "c1", "c2", "d", "recovery_copy", "recovery_move"}
    assert expect["b"] == "no_trigger"  # unresolved without a reviewed trigger


def test_every_group_clock_is_sized_from_its_worst_case_and_under_the_ceiling():
    inputs = frozen.load()
    clocks = inputs.group_clocks()
    assert set(clocks) == {f"g{n}" for n in range(1, 9)}
    for seconds in clocks.values():
        assert 0 < seconds <= runner.Limits().group_clock_max


def test_the_committed_service_message_list_is_a_placeholder_and_is_not_frozen():
    inputs = frozen.load()
    assert inputs.service_frozen is False
    with pytest.raises(runner.Refused) as refused:
        inputs.require_service_frozen()
    assert refused.value.token == "service_list_not_frozen"


def test_a_frozen_list_with_its_recorded_hash_is_accepted(tmp_path):
    directory = _copy(tmp_path)
    _freeze(directory, rules=[{"name": "welcome", "sender_domain": "service.example"}])
    frozen.load(directory).require_service_frozen()


def test_a_frozen_flag_without_the_matching_hash_is_refused(tmp_path):
    directory = _copy(tmp_path)
    path = directory / "service_messages.json"
    data = json.loads(path.read_text())
    data["frozen"], data["frozen_hash"] = True, "0" * 64
    path.write_text(json.dumps(data))
    with pytest.raises(runner.Refused) as refused:
        frozen.load(directory).require_service_frozen()
    assert refused.value.token == "service_list_not_frozen"


def test_editing_a_rule_after_freezing_breaks_the_hash(tmp_path):
    directory = _copy(tmp_path)
    _freeze(directory)
    path = directory / "service_messages.json"
    data = json.loads(path.read_text())
    data["rules"].append({"name": "late", "sender_domain": "late.example"})
    path.write_text(json.dumps(data))
    with pytest.raises(runner.Refused):
        frozen.load(directory).require_service_frozen()


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d["predictions"].pop(),
        lambda d: d["predictions"][0].update(id="P13"),
        lambda d: d["predictions"][0].update(expected="observed"),
        lambda d: d["g7_steps"][0]["expect"].update(uid="sometimes"),
        lambda d: d["g7_steps"].reverse(),
    ],
)
def test_malformed_predictions_are_refused_with_a_fixed_token(tmp_path, edit):
    directory = _copy(tmp_path)
    path = directory / "predictions.json"
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data))
    with pytest.raises(runner.Refused) as refused:
        frozen.load(directory)
    assert refused.value.token == "frozen_inputs_invalid"


def test_the_bundle_hash_changes_with_any_frozen_file(tmp_path):
    base = frozen.load().bundle_hash
    directory = _copy(tmp_path)
    path = directory / "fixtures.json"
    data = json.loads(path.read_text())
    data["groups"]["g1"]["control"] += 1
    path.write_text(json.dumps(data))
    assert frozen.load(directory).bundle_hash != base
    assert frozen.load().bundle_hash == base  # and is stable for the committed files


def test_an_approval_token_is_accepted_only_when_it_binds_to_the_frozen_bundle(tmp_path):
    directory = _copy(tmp_path)
    _freeze(directory)
    inputs = frozen.load(directory)
    report = tmp_path / "stage_report.json"
    report.write_text(
        json.dumps(
            {
                "booleans": {"isolation_passed": True, "tls_pin_checked": True},
                "counts": {},
            }
        )
    )
    token = tmp_path / "approval"
    token.write_text(inputs.bundle_hash + "\n")
    token.chmod(0o600)
    frozen.authorize_live(token, report, inputs)

    token.write_text("0" * 64)
    with pytest.raises(runner.Refused) as refused:
        frozen.authorize_live(token, report, inputs)
    assert refused.value.token == "not_authorized"


def test_live_is_refused_without_a_token_a_loose_token_or_a_missing_report(tmp_path):
    directory = _copy(tmp_path)
    _freeze(directory)
    inputs = frozen.load(directory)
    report = tmp_path / "stage_report.json"
    report.write_text(json.dumps({"booleans": {"isolation_passed": True, "tls_pin_checked": True}}))
    token = tmp_path / "approval"

    with pytest.raises(runner.Refused) as refused:  # absent by default
        frozen.authorize_live(token, report, inputs)
    assert refused.value.token == "not_authorized"

    token.write_text(inputs.bundle_hash)
    token.chmod(0o644)  # readable by others: refused
    with pytest.raises(runner.Refused) as refused:
        frozen.authorize_live(token, report, inputs)
    assert refused.value.token == "not_authorized"

    token.chmod(0o600)
    report.unlink()
    with pytest.raises(runner.Refused) as refused:
        frozen.authorize_live(token, report, inputs)
    assert refused.value.token == "stage_report_missing"


@pytest.mark.parametrize(
    "report",
    [
        {"booleans": {"isolation_passed": False, "tls_pin_checked": True}},
        {"booleans": {"isolation_passed": True, "tls_pin_checked": False}},
        {"booleans": {"isolation_passed": True}},
        {"booleans": {"isolation_passed": True, "tls_pin_checked": True}, "subject": "x"},
        {
            "booleans": {"isolation_passed": True, "tls_pin_checked": True},
            "stop": {"reason": "isolation_failed", "group": "start"},
        },
    ],
)
def test_a_stage_report_that_does_not_pass_is_refused(tmp_path, report):
    directory = _copy(tmp_path)
    _freeze(directory)
    inputs = frozen.load(directory)
    path = tmp_path / "stage_report.json"
    path.write_text(json.dumps(report))
    token = tmp_path / "approval"
    token.write_text(inputs.bundle_hash)
    token.chmod(0o600)
    with pytest.raises(runner.Refused) as refused:
        frozen.authorize_live(token, path, inputs)
    assert refused.value.token == "stage_report_invalid"


def test_live_is_refused_while_the_service_list_is_not_frozen(tmp_path):
    inputs = frozen.load()  # the committed placeholder, not frozen
    report = tmp_path / "stage_report.json"
    report.write_text(json.dumps({"booleans": {"isolation_passed": True, "tls_pin_checked": True}}))
    token = tmp_path / "approval"
    token.write_text(inputs.bundle_hash)
    token.chmod(stat.S_IRUSR | stat.S_IWUSR)
    with pytest.raises(runner.Refused) as refused:
        frozen.authorize_live(token, report, inputs)
    assert refused.value.token == "service_list_not_frozen"
