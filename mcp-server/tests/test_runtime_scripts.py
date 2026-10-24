"""The runtime-image scripts build their own synthetic SQLite databases
from ``scripts.reconcile_bench._SCHEMA`` and run only in a CI step
against the shipped image, so a schema change can break one unseen until
CI (#1677: ``runtime_bcc.py`` kept inserting columns #1612 removed, and
``main`` stayed red). Running them here, on the local SQLite, fails the
change that breaks them. The full-body script uses indexer dependencies
and runs in ``indexer/tests/test_runtime_full_body.py`` instead."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent


# The aggregate and list-metadata scripts assert wall-clock bounds, so they
# run in the workflow's serial pass; the Bcc script compares SQLite VM step
# counts and can share the parallel one.
_SERIAL = pytest.mark.serial


@pytest.mark.parametrize(
    "script",
    [
        "runtime_bcc",
        "runtime_clock_indexes",
        pytest.param("runtime_aggregate_samples", marks=_SERIAL),
        pytest.param("runtime_list_metadata", marks=_SERIAL),
    ],
)
def test_runtime_script_runs_against_the_current_schema(script):
    done = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(_TESTS / f"{script}.py")],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=_TESTS.parent,
        env={**os.environ, "PYTHONPATH": str(_TESTS.parent)},
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "OK" in done.stderr  # unittest's own verdict: tests actually ran
