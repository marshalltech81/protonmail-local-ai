"""Run an external extraction tool on one attachment payload (#935).

Shared by the ``doc`` extractor (catdoc), the ``xls`` extractor, which
runs xlrd in a child Python process, and the ``ppt`` extractor, which
runs Apache POI in a Java process (#957). Every
tool is attacker-reachable parsing code, so the run is bounded and its
output is treated as data:

* The tool is started through ``_launcher.py`` (``python -I``), which
  lowers its own address-space (``RLIMIT_AS``) and CPU-time
  (``RLIMIT_CPU``) limits to the values the caller passes and then
  ``execve``s the tool, so every tool runs under both limits from its
  first instruction (#995). The caller sizes them from a plain
  measurement of the tool; there are no defaults.

* The payload is written to a mode-600 temporary file under ``/tmp``
  (a tmpfs in Compose), passed as the last argument and deleted
  afterwards. Not stdin: catdoc's catppt 0.95, the first ``.ppt``
  candidate, segfaulted when it read stdin.
* The tool runs with an argument list and no shell, a minimal
  environment, no stdin, and its stderr discarded: stderr can quote the
  document, and nothing a tool prints may reach a log or ``last_error``.
* A wall-clock timeout kills the tool. Its stdout is read incrementally
  up to ``max_output_bytes``; past it the tool is killed and the bytes
  read so far are returned with ``truncated=True``. Output is never read
  whole and cut afterwards.
* A timeout, a death by signal (a crash, or the CPU limit) and a
  non-zero exit (a tool that fails an allocation under the
  address-space limit exits with an error) raise fixed-text exceptions,
  which the dispatcher records by type name as a ``failed`` row.
"""

from __future__ import annotations

import os
import selectors
import subprocess  # nosec B404 — argument lists only, no shell
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Bytes read from a tool's stdout per call.
_READ_CHUNK = 64 * 1024

# Where the payload file is written: the hardened image's writable tmpfs.
_TMP_DIR = "/tmp"  # nosec B108 — tmpfs in Compose; the file is mode 600

# The environment a tool runs with: nothing inherited from the indexer
# (its secrets path, provider settings), and a UTF-8 locale.
_TOOL_ENV = {"LC_ALL": "C.UTF-8"}

# Sets the limits, then ``execve``s the tool.
_LAUNCHER = Path(__file__).with_name("_launcher.py")


class ToolNotFoundError(Exception):
    """The tool's binary is not installed in this image."""

    def __init__(self) -> None:
        super().__init__("extraction tool not installed")


class ToolTimeoutError(Exception):
    """The tool ran past its wall-clock timeout and was killed."""

    def __init__(self) -> None:
        super().__init__("extraction tool timed out")


class ToolCrashError(Exception):
    """The tool died from a signal: a crash, or a resource limit."""

    def __init__(self) -> None:
        super().__init__("extraction tool killed by a signal")


class ToolExitError(Exception):
    """The tool exited with a non-zero status."""

    def __init__(self) -> None:
        super().__init__("extraction tool exited with an error")


@dataclass(frozen=True)
class ToolOutput:
    """What a tool wrote to stdout, and whether the byte cap cut it."""

    data: bytes
    truncated: bool


def run_tool(
    argv: list[str],
    payload: bytes,
    *,
    timeout_seconds: float,
    max_output_bytes: int,
    max_address_space_bytes: int,
    max_cpu_seconds: int,
    suffix: str,
) -> ToolOutput:
    """Run ``argv`` plus the path of a temporary file holding ``payload``,
    under ``max_address_space_bytes`` of address space and
    ``max_cpu_seconds`` of CPU time. ``argv[0]`` is an absolute path:
    the launcher ``execve``s it with no ``PATH`` search.

    Raises ``ToolTimeoutError``, ``ToolCrashError`` or ``ToolExitError``;
    a run the output cap cut is returned, not raised, since the bytes
    before the cut are the tool's own.
    """
    fd, path = tempfile.mkstemp(dir=_TMP_DIR, suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        launched = [
            sys.executable,
            "-I",
            str(_LAUNCHER),
            str(max_address_space_bytes),
            str(max_cpu_seconds),
            *argv,
            path,
        ]
        return _run(launched, timeout_seconds, max_output_bytes)
    finally:
        os.unlink(path)


def _run(argv: list[str], timeout_seconds: float, max_output_bytes: int) -> ToolOutput:
    deadline = time.monotonic() + timeout_seconds
    proc = subprocess.Popen(  # nosec B603 — fixed argument list, no shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_TOOL_ENV,
        close_fds=True,
    )
    chunks: list[bytes] = []
    size = 0
    truncated = False
    try:
        assert proc.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ToolTimeoutError
                if not selector.select(remaining):
                    continue
                chunk = os.read(proc.stdout.fileno(), _READ_CHUNK)
                if not chunk:
                    break
                room = max_output_bytes - size
                if len(chunk) > room:
                    chunks.append(chunk[:room])
                    truncated = True
                    break
                chunks.append(chunk)
                size += len(chunk)
        if truncated:
            return ToolOutput(b"".join(chunks), truncated=True)
        try:
            returncode = proc.wait(max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            raise ToolTimeoutError from None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
    if returncode < 0:
        raise ToolCrashError
    if returncode > 0:
        raise ToolExitError
    return ToolOutput(b"".join(chunks), truncated=False)
