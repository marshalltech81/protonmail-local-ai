"""The supervised-sync driver (procedure v5 section 2, 1.8).

The run triggers every isync run itself and bounds each: a wall-clock
timeout and an output cap, after which the whole process group is killed.
The output is only counted, never kept or returned (it can name folders),
so a result is a token and numbers. The isync configuration is the
repository's own render (``render_isync_config.sh``).
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

SYNC_TIMEOUT_SECONDS = 600.0
OUTPUT_CAP_BYTES = 1 << 20
_READ = 65536


@dataclass(frozen=True)
class SyncResult:
    token: str  # ok | exit_nonzero | timeout | output_cap | spawn_failed
    returncode: int | None
    output_bytes: int


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError, PermissionError:
        proc.kill()
    proc.wait()


def run_bounded(
    argv: list[str],
    *,
    timeout: float = SYNC_TIMEOUT_SECONDS,
    output_cap: int = OUTPUT_CAP_BYTES,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> SyncResult:
    try:
        proc = subprocess.Popen(  # noqa: S603 - the argv is built by this module's caller
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
    except OSError:
        return SyncResult("spawn_failed", None, 0)
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    total = 0
    token: str | None = None
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                token = "timeout"
                break
            if not selector.select(min(remaining, 0.2)):
                continue
            chunk = os.read(proc.stdout.fileno(), _READ)
            if not chunk:
                break  # end of output
            total += len(chunk)  # counted, then dropped
            if total > output_cap:
                token = "output_cap"
                break
    if token is None:
        try:
            proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
        except subprocess.TimeoutExpired:
            token = "timeout"
    if token is not None:
        _kill_group(proc)
        proc.stdout.close()
        return SyncResult(token, None, total)
    proc.stdout.close()
    code = proc.returncode
    return SyncResult("ok" if code == 0 else "exit_nonzero", code, total)


class _Counts(Protocol):
    def command(self) -> None: ...


class SupervisedSync:
    def __init__(
        self,
        config_path: Path | str,
        channel: str,
        *,
        mbsync: str = "mbsync",
        budget: _Counts | None = None,
        timeout: float = SYNC_TIMEOUT_SECONDS,
        output_cap: int = OUTPUT_CAP_BYTES,
    ) -> None:
        self._argv = [mbsync, "-c", str(config_path), channel]
        self._budget, self._timeout, self._cap = budget, timeout, output_cap

    def sync_once(self) -> SyncResult:
        if self._budget is not None:
            self._budget.command()
        return run_bounded(self._argv, timeout=self._timeout, output_cap=self._cap)
