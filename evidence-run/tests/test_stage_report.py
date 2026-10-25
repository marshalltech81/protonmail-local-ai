"""The account-free stage report (#1767, procedure v5 1A): the file the live
guard requires. It is built from the isolation matrix's verdict and the TLS
pin check, as an allowlisted export, never from anything else."""

import json
import shutil

import pytest
from evidence_run import frozen, runner, stage_report


def _isolation(tmp_path, verdict="pass"):
    path = tmp_path / "isolation.json"
    path.write_text(
        json.dumps(
            {
                "verdict": verdict,
                "violations": 0,
                "control_failures": 0,
                "incomplete": 0,
                "cells": 12,
            }
        )
    )
    return path


def test_a_passing_matrix_and_a_checked_pin_make_a_passing_report(tmp_path):
    report = stage_report.build(json.loads(_isolation(tmp_path).read_text()), tls_pin_checked=True)
    assert report["booleans"] == {"isolation_passed": True, "tls_pin_checked": True}
    assert not report["stop"]


@pytest.mark.parametrize(
    "verdict, pin, passed",
    [("fail", True, False), ("pass", False, True), ("fail", False, False)],
)
def test_a_failed_matrix_or_an_unchecked_pin_is_recorded_as_false(tmp_path, verdict, pin, passed):
    matrix = json.loads(_isolation(tmp_path, verdict).read_text())
    report = stage_report.build(matrix, tls_pin_checked=pin)
    assert report["booleans"] == {"isolation_passed": passed, "tls_pin_checked": pin}


@pytest.mark.parametrize("matrix", [{}, {"verdict": "maybe"}, {"verdict": 1}, []])
def test_a_matrix_report_that_is_not_the_matrix_output_is_refused(matrix):
    with pytest.raises(runner.Refused) as refused:
        stage_report.build(matrix, tls_pin_checked=True)
    assert refused.value.token == "isolation_report_invalid"


def test_the_command_writes_a_mode_600_report_the_live_guard_accepts(tmp_path):
    directory = tmp_path / "frozen"
    shutil.copytree(frozen.FROZEN_DIR, directory)
    rules = [{"name": "welcome", "sender_domain": "service.example"}]
    path = directory / "service_messages.json"
    data = json.loads(path.read_text())
    data["rules"], data["frozen"], data["frozen_hash"] = rules, True, frozen.service_hash(rules)
    path.write_text(json.dumps(data))
    inputs = frozen.load(directory)

    out = tmp_path / "stage_report.json"
    code = stage_report.main(
        ["--isolation-report", str(_isolation(tmp_path)), "--tls-pin-checked", "--out", str(out)]
    )
    assert code == 0 and (out.stat().st_mode & 0o777) == 0o600

    token = tmp_path / "approval"
    token.write_text(inputs.bundle_hash)
    token.chmod(0o600)
    frozen.authorize_live(token, out, inputs)


def test_a_failed_matrix_gives_a_report_the_live_guard_refuses(tmp_path):
    directory = tmp_path / "frozen"
    shutil.copytree(frozen.FROZEN_DIR, directory)
    rules = [{"name": "welcome", "sender_domain": "service.example"}]
    path = directory / "service_messages.json"
    data = json.loads(path.read_text())
    data["rules"], data["frozen"], data["frozen_hash"] = rules, True, frozen.service_hash(rules)
    path.write_text(json.dumps(data))
    inputs = frozen.load(directory)
    out = tmp_path / "stage_report.json"
    assert (
        stage_report.main(
            [
                "--isolation-report",
                str(_isolation(tmp_path, "fail")),
                "--tls-pin-checked",
                "--out",
                str(out),
            ]
        )
        == 1  # not a passing report: the exit code says so
    )
    token = tmp_path / "approval"
    token.write_text(inputs.bundle_hash)
    token.chmod(0o600)
    with pytest.raises(runner.Refused) as refused:
        frozen.authorize_live(token, out, inputs)
    assert refused.value.token == "stage_report_invalid"


def test_the_command_refuses_a_missing_or_unreadable_isolation_report(tmp_path, capsys):
    assert (
        stage_report.main(
            ["--isolation-report", str(tmp_path / "none.json"), "--out", str(tmp_path / "o.json")]
        )
        == 2
    )
    assert "refused: isolation_report_invalid" in capsys.readouterr().err
