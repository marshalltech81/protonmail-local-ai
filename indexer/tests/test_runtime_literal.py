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
    assert [(r["shape"], r["messages"]) for r in rows[:-1]] == [
        (s, n) for s in ("prose", "repeated", "unicode", "misaligned") for n in (40, 400)
    ]
    for small, large in zip(rows[:-1:2], rows[1:-1:2], strict=True):
        assert large["verified_windows"] == 10 * small["verified_windows"]
        assert large["transfer_bytes"] == 10 * small["transfer_bytes"]
        assert small["match_operands"] == large["match_operands"] == 1

    assert rows[-1]["shape"] == "capped" and rows[-1]["body_complete"] == 0
    assert rows[-1]["membership_bytes"] <= 512 * 1024 * 1024
