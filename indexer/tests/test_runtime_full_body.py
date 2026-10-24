"""Run the full-body CI script with the indexer's own dependencies (#1682)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.serial
def test_full_body_runtime_script_runs_all_source_shapes():
    # The CI image mounts these same sources at FULL_BODY_ROOT. Keep the
    # runtime-image check too: the host's SQLite can have a different planner.
    done = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(_ROOT / "mcp-server/tests/runtime_full_body.py")],
        cwd=_ROOT / "indexer",
        env={**os.environ, "FULL_BODY_ROOT": str(_ROOT), "PYTHONPATH": str(_ROOT / "indexer")},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    rows = [json.loads(line) for line in done.stdout.splitlines()]
    assert [(row["shape"], row["key_bytes"]) for row in rows] == [
        ("plain", 97),
        ("unicode", 97),
        ("ranges", 1032),
        ("plain", 4025),
    ]
    assert all(row["read_bytes"] > 0 for row in rows)
