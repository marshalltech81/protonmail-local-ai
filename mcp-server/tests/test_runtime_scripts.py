"""The runtime-image scripts build their own synthetic SQLite databases
from ``scripts.reconcile_bench._SCHEMA`` and run only in a CI step
against the shipped image, so a schema change can break one unseen until
CI (#1677: ``runtime_bcc.py`` kept inserting columns #1612 removed, and
``main`` stayed red). Running them here, on the local SQLite, fails the
change that breaks them. ``runtime_clock_indexes.py`` and
``runtime_full_body.py`` need the image's mounts and are not run here."""

import subprocess
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent


@pytest.mark.parametrize(
    "script", ["runtime_bcc", "runtime_aggregate_samples", "runtime_list_metadata"]
)
def test_runtime_script_runs_against_the_current_schema(script):
    done = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(_TESTS / f"{script}.py")],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=_TESTS.parent,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "OK" in done.stderr  # unittest's own verdict: tests actually ran
