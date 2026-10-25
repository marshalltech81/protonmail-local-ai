"""The frozen inputs of the run (procedure v5 1B.1) and the live-run guard.

Everything the run must not be able to change after the fact is data in
``frozen/``: the predictions P1 to P12 (and each G7 step's expectation),
the fixture plan with each group's batches and operations, and the list of
expected Proton service messages. The service list is committed as a
placeholder; the driver refuses every account-stage group until it is
marked frozen and carries the hash of its rules, so a rule edited after
freezing is refused.

The owner's approval is a mode-600 file, absent by default and never
committed, holding the hash of the whole bundle. It binds the approval to
exactly these inputs, so changing a prediction or a fixture count after
approving invalidates it.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import export, runner

FROZEN_DIR = Path(__file__).resolve().parent / "frozen"
GROUPS = tuple(f"g{n}" for n in range(1, 9))
_DIMENSIONS = ("proton_id", "gluon_id", "uid", "uidvalidity", "bytes")
_EXPECTATIONS = frozenset({"same", "changed", "any"})
_G7_ORDER = ["a", "b", "c1", "c2", "d", "e", "f", "g", "h", "i"]


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def service_hash(rules: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical(rules)).hexdigest()


def _invalid() -> runner.Refused:
    return runner.Refused("frozen_inputs_invalid")


@dataclass(frozen=True)
class FrozenInputs:
    predictions: dict[str, dict[str, Any]]
    g7_steps: list[dict[str, Any]]
    h11_expect: dict[str, str]
    fixtures: dict[str, Any]
    service_rules: list[dict[str, Any]]
    service_frozen: bool
    service_recorded_hash: str | None
    bundle_hash: str

    def group_clocks(self, limits: runner.Limits | None = None) -> dict[str, float]:
        """Each group's clock from its worst-case waits (batches and
        operations), refused above the ceiling."""
        limits = limits or runner.Limits()
        clocks: dict[str, float] = {}
        for name in GROUPS:
            plan = self.fixtures["groups"][name]
            seconds = runner.group_clock_seconds(
                batches=plan["batches"],
                convergence=limits.convergence_seconds,
                operations=plan["operations"],
                rate=limits.max_rate,
            )
            if seconds > limits.group_clock_max:
                raise runner.ConfigError("a group's worst-case waits exceed the clock ceiling")
            clocks[name] = seconds
        return clocks

    def require_service_frozen(self) -> None:
        """Every group touches an account, so none runs until the service
        message list is frozen with the hash of its rules."""
        if not self.service_frozen or self.service_recorded_hash != service_hash(
            self.service_rules
        ):
            raise runner.Refused("service_list_not_frozen")


def _read(directory: Path, name: str) -> dict[str, Any]:
    try:
        data = json.loads((directory / name).read_text(encoding="utf-8"))
    except OSError, ValueError:
        raise _invalid() from None
    if not isinstance(data, dict) or data.get("version") != 1:
        raise _invalid()
    return data


def _validate(predictions: dict[str, Any], fixtures: dict[str, Any]) -> None:
    rows = predictions.get("predictions")
    if not isinstance(rows, list) or [r.get("id") for r in rows] != [f"P{n}" for n in range(1, 13)]:
        raise _invalid()
    for row in rows:
        if row.get("expected") != "predicted" or not row.get("text") or not row.get("basis"):
            raise _invalid()
    steps = predictions.get("g7_steps")
    if not isinstance(steps, list) or [s.get("id") for s in steps] != _G7_ORDER:
        raise _invalid()
    for step in steps:
        expect = step.get("expect")
        if (
            step.get("prediction") not in {"P3", "P6", "P7", "P8"}
            or not isinstance(expect, dict)
            or set(expect) != set(_DIMENSIONS)
            or not set(expect.values()) <= _EXPECTATIONS
        ):
            raise _invalid()
    h11 = predictions.get("h11_expect")
    if not isinstance(h11, dict) or not all(isinstance(v, str) for v in h11.values()):
        raise _invalid()
    groups = fixtures.get("groups")
    if not isinstance(groups, dict) or set(groups) != set(GROUPS):
        raise _invalid()
    for plan in groups.values():
        if not (isinstance(plan.get("batches"), int) and isinstance(plan.get("operations"), int)):
            raise _invalid()


def load(directory: Path | str = FROZEN_DIR) -> FrozenInputs:
    directory = Path(directory)
    predictions = _read(directory, "predictions.json")
    fixtures = _read(directory, "fixtures.json")
    service = _read(directory, "service_messages.json")
    _validate(predictions, fixtures)
    rules = service.get("rules")
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        raise _invalid()
    bundle = hashlib.sha256(
        _canonical({"predictions": predictions, "fixtures": fixtures, "service": service})
    ).hexdigest()
    return FrozenInputs(
        predictions={r["id"]: r for r in predictions["predictions"]},
        g7_steps=predictions["g7_steps"],
        h11_expect=predictions["h11_expect"],
        fixtures=fixtures,
        service_rules=rules,
        service_frozen=service.get("frozen") is True,
        service_recorded_hash=service.get("frozen_hash"),
        bundle_hash=bundle,
    )


def _token_ok(path: Path, inputs: FrozenInputs) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if (
        not stat.S_ISREG(info.st_mode)  # a symlink is refused too
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
    ):
        return False
    try:
        return path.read_text(encoding="utf-8").strip() == inputs.bundle_hash
    except OSError, UnicodeError:
        return False


def authorize_live(
    token_path: Path | str | None, report_path: Path | str | None, inputs: FrozenInputs
) -> None:
    """Live groups run only with the frozen service list, the owner's
    approval token for exactly these inputs, and a passing account-free
    stage report. Anything else is a refusal with a fixed token."""
    inputs.require_service_frozen()
    if token_path is None or not _token_ok(Path(token_path), inputs):
        raise runner.Refused("not_authorized")
    if report_path is None or not Path(report_path).is_file():
        raise runner.Refused("stage_report_missing")
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        export.validate_export(report)
        flags = report.get("booleans", {})
        ok = (
            flags.get("isolation_passed") is True
            and flags.get("tls_pin_checked") is True
            and not report.get("stop")
        )
    except OSError, ValueError, export.ExportError:
        ok = False
    if not ok:
        raise runner.Refused("stage_report_invalid")
