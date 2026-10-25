"""Execute the production literal writer/reader work gate with synthetic inputs."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.serial
def test_literal_runtime_gate_counts_every_verification_once():
    result = subprocess.run(
        [sys.executable, str(ROOT / "mcp-server/tests/runtime_literal.py")],
        cwd=ROOT,
        env={**os.environ, "LITERAL_ROOT": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [(r["shape"], r["messages"]) for r in rows[:-2]] == [
        (s, n) for s in ("prose", "repeated", "unicode", "misaligned") for n in (40, 400)
    ]
    for small, large in zip(rows[:-2:2], rows[1:-2:2], strict=True):
        assert large["verified_windows"] == 10 * small["verified_windows"]
        assert large["transfer_bytes"] == 10 * small["transfer_bytes"]
        assert small["match_operands"] == large["match_operands"] == 1

    assert rows[-2]["shape"] == "capped" and rows[-2]["body_complete"] == 0
    assert rows[-2]["membership_bytes"] <= 16 * 1024 * 1024
    assert rows[-2]["constructed_windows"] == rows[-2]["windows"] <= 32
    assert rows[-2]["added_database_bytes"] + rows[-2]["wal_bytes"] <= 32 * 1024 * 1024
    assert rows[-1] == {"shape": "storage_limited", "windows": 0, "body_complete": 0}
