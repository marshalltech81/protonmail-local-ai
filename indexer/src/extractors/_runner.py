"""Run an external extraction tool on one attachment payload (#935).

Shared by the ``doc`` extractor (catdoc), the ``ppt`` extractor, which
runs Apache POI in a Java process (#957), and the extractors that run
in the Python extractor child (``extractor_child.py``: the OOXML
formats and ``xls``) through ``run_child`` below. Every tool is
attacker-reachable parsing code, so the run is bounded and its output
is treated as data:

* The tool is started through ``_launcher.py`` (``python -I``), which
  lowers its own address-space (``RLIMIT_AS``) and CPU-time
  (``RLIMIT_CPU``) limits to the values the caller passes and then
  ``execve``s the tool, so every tool runs under both limits from its
  first instruction (#995). The caller sizes them from a plain
  measurement of the tool; there are no defaults. A process the tool
  starts inherits both.
* The tool runs in its own session, so it and every process it starts
  share one process group, and the whole group is killed with
  ``SIGKILL`` whenever the run ends: on a timeout, at the output cap,
  on an error and after a normal exit (#1291). The group is killed
  before the tool is reaped, so its id cannot have been reused. A
  process that leaves the group (``setsid``) escapes this; the tools
  run here do not.
* Each run gets a scratch directory the parent owns: a mode-700
  directory under ``/tmp`` (a tmpfs in Compose), removed with
  everything in it once the group is dead, so a killed tool leaks no
  files (#1291). The payload is written into it as a mode-600 file,
  passed as the last argument (not stdin: catdoc's catppt 0.95, the
  first ``.ppt`` candidate, segfaulted when it read stdin), and the
  directory is the tool's ``TMPDIR``.
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
  which the dispatcher records by type name as a ``failed`` row. The
  exit status rides on ``ToolExitError``; only the ``ppt`` extractor
  reads it, for its reader's encrypted-deck status (#983).

The extractor child speaks a framed protocol on stdout (#1291), parsed
here as it arrives. Every frame but the text is one ASCII line:

* ``P``: progress, passed to the caller's ``on_progress`` when read,
  so a long extraction can refresh the indexer's heartbeat;
* ``C <name>``: a budget that cut the text, one of the caller's
  ``caps``;
* ``N <name> <count>``: a count, one of the caller's ``counts``;
* ``E <type name>``: the extraction raised; nothing may follow;
* ``T <length>`` and then exactly ``length`` bytes of UTF-8 text;
  nothing may follow.

Output that breaks this (a line over ``_MAX_FRAME_LINE`` bytes, an
unknown frame or name, bytes after the last frame, a text shorter or
longer than its length, text that is not UTF-8, no ``E`` or ``T``
frame) or that the output cap cut raises ``ChildOutputError``: a
``failed`` row, never text. ``E`` raises the caller's ``permanent``
class for that name, else ``ChildError``, which the dispatcher records
``failed`` under the reported name.
"""

from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import subprocess  # nosec B404 — argument lists only, no shell
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

# Bytes read from a tool's stdout per call.
_READ_CHUNK = 64 * 1024

# Where each run's scratch directory is made: the hardened image's
# writable tmpfs.
_TMP_DIR = "/tmp"  # nosec B108 — tmpfs in Compose; the directory is mode 700

# The environment a tool runs with: nothing inherited from the indexer
# (its secrets path, provider settings), and a UTF-8 locale. The run
# adds ``TMPDIR``, its scratch directory.
_TOOL_ENV = {"LC_ALL": "C.UTF-8"}

# Sets the limits, then ``execve``s the tool.
_LAUNCHER = Path(__file__).with_name("_launcher.py")

# The Python extractor child.
_CHILD = Path(__file__).with_name("extractor_child.py")

# Seconds between checks whether the tool has exited, once its stdout
# has closed.
_EXIT_POLL_SECONDS = 0.001

# Longest protocol line the parent reads, newline excluded: a frame
# letter, a space and a name or number.
_MAX_FRAME_LINE = 128

# What a reported type name must look like: a Python identifier.
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
_COUNT = re.compile(r"[0-9]{1,18}")


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
    """The tool exited with a non-zero status, kept as ``returncode`` for
    a caller whose tool gives one status a fixed meaning (the ``ppt``
    reader's encrypted-deck status, #983). The message stays fixed."""

    def __init__(self, returncode: int | None = None) -> None:
        super().__init__("extraction tool exited with an error")
        self.returncode = returncode


class ChildOutputError(Exception):
    """The extractor child's output was cut at the byte cap or broke the
    protocol."""

    def __init__(self) -> None:
        super().__init__("extractor child output malformed or over its cap")


class ChildError(Exception):
    """The extraction raised in the child. ``type_name`` is the
    exception's type name, which the dispatcher records; the message is
    fixed, as the exception's own could quote the document."""

    def __init__(self, type_name: str) -> None:
        super().__init__("extraction raised in the extractor child")
        self.type_name = type_name


@dataclass(frozen=True)
class ToolOutput:
    """What a tool wrote to stdout, and whether the byte cap cut it."""

    data: bytes
    truncated: bool


@dataclass(frozen=True)
class ChildResult:
    """The extractor child's text, the budgets that cut it (each once,
    in the order reported) and its counts."""

    text: str
    caps: list[str]
    counts: dict[str, int]


def raw_output_cap(max_extracted_chars: int | None, *, ceiling: int) -> int:
    """The output byte cap of a raw tool (``doc``, ``ppt``) for the
    dispatcher's configured character cap (#1308): four bytes a character,
    UTF-8's worst case, so the character cap decides the stored length,
    never above ``ceiling``, which also applies when the character cap is
    disabled (``None``). The cap bounds the tool's raw output, before the
    dispatcher strips surrounding whitespace.

    The ceiling bounds the indexer's own memory, since the output is read
    into it: measured plainly in the indexer image through the
    dispatcher, an extraction holds about five bytes per output byte at
    its peak (the read chunks and their join, then the decoded text, four
    bytes a character once one character is outside the Basic
    Multilingual Plane), linear from 8 to 256 MiB of output.
    """
    if max_extracted_chars is None:
        return ceiling
    return min(4 * max_extracted_chars, ceiling)


def run_tool(
    argv: list[str],
    payload: bytes,
    *,
    timeout_seconds: float,
    max_output_bytes: int,
    max_address_space_bytes: int,
    max_cpu_seconds: int,
    suffix: str,
    on_output: Callable[[bytes], None] | None = None,
) -> ToolOutput:
    """Run ``argv`` plus the path of a file holding ``payload``, under
    ``max_address_space_bytes`` of address space and ``max_cpu_seconds``
    of CPU time. ``argv[0]`` is an absolute path: the launcher
    ``execve``s it with no ``PATH`` search.

    With ``on_output``, each piece of stdout read (within the cap) is
    passed to it as it arrives and the returned ``data`` is empty.

    Raises ``ToolTimeoutError``, ``ToolCrashError`` or ``ToolExitError``;
    a run the output cap cut is returned, not raised, since the bytes
    before the cut are the tool's own.
    """
    scratch = tempfile.mkdtemp(dir=_TMP_DIR)
    try:
        path = os.path.join(scratch, f"payload{suffix}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
        chunks: list[bytes] = []
        truncated = _run(
            launched,
            {**_TOOL_ENV, "TMPDIR": scratch},
            timeout_seconds,
            max_output_bytes,
            chunks.append if on_output is None else on_output,
        )
        return ToolOutput(b"".join(chunks), truncated=truncated)
    finally:
        # The tool's whole process group is dead by now (``_run``), so
        # nothing writes here while it is removed.
        shutil.rmtree(scratch)


def _run(
    argv: list[str],
    env: dict[str, str],
    timeout_seconds: float,
    max_output_bytes: int,
    sink: Callable[[bytes], None],
) -> bool:
    """Run ``argv`` in its own session, passing its stdout to ``sink``.
    Returns whether the output cap cut it."""
    deadline = time.monotonic() + timeout_seconds
    proc = subprocess.Popen(  # nosec B603 — fixed argument list, no shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        close_fds=True,
        start_new_session=True,
    )
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
                    sink(chunk[:room])
                    truncated = True
                    break
                sink(chunk)
                size += len(chunk)
        if not truncated:
            _wait_exited(proc.pid, deadline)
    finally:
        # Kill the group before reaping the tool: while the tool is
        # unreaped its id, which is the group's, cannot be reused.
        _kill_group(proc.pid)
        returncode = proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
    if truncated:
        return True
    if returncode < 0:
        raise ToolCrashError
    if returncode > 0:
        raise ToolExitError(returncode)
    return False


def _wait_exited(pid: int, deadline: float) -> None:
    """Wait until ``pid`` has exited, without reaping it."""
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    while os.waitid(os.P_PID, pid, flags) is None:
        if time.monotonic() >= deadline:
            raise ToolTimeoutError
        time.sleep(_EXIT_POLL_SECONDS)


def _kill_group(pgid: int) -> None:
    """``SIGKILL`` every process in the group ``pgid``."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError, PermissionError:
        # Nothing left to kill: macOS refuses (EPERM) a group whose only
        # member is the unreaped tool.
        pass


def run_child(
    module: str,
    payload: bytes,
    *,
    max_address_space_bytes: int,
    max_cpu_seconds: int,
    timeout_seconds: float,
    max_output_bytes: int,
    caps: frozenset[str],
    counts: frozenset[str] = frozenset(),
    permanent: Mapping[str, type[Exception]] | None = None,
    on_progress: Callable[[], None] | None = None,
) -> ChildResult:
    """Run ``module``'s extraction of ``payload`` in the extractor child
    under the caller's limits, and parse its frames (module docstring).
    The wall-clock timeout is past the CPU limit, so a CPU-bound child
    meets that first."""
    frames = _Frames(caps, counts, on_progress)
    output = run_tool(
        [sys.executable, "-I", str(_CHILD), module],
        payload,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        max_address_space_bytes=max_address_space_bytes,
        max_cpu_seconds=max_cpu_seconds,
        suffix=f".{module}",
        on_output=frames.feed,
    )
    if output.truncated:
        raise ChildOutputError
    error = frames.error()
    if error is not None:
        known = (permanent or {}).get(error)
        if known is not None:
            raise known()
        raise ChildError(error)
    return frames.result()


class _Frames:
    """The child's frames, parsed as its output arrives."""

    def __init__(
        self,
        caps: frozenset[str],
        counts: frozenset[str],
        on_progress: Callable[[], None] | None,
    ) -> None:
        self._caps_allowed = caps
        self._counts_allowed = counts
        self._on_progress = on_progress
        self._line = bytearray()
        self._caps: list[str] = []
        self._counts: dict[str, int] = {}
        self._error: str | None = None
        self._text_length: int | None = None
        self._text = bytearray()

    def feed(self, data: bytes) -> None:
        """Take the next bytes of output; raises ``ChildOutputError`` as
        soon as they break the protocol, which ends the run."""
        if self._error is not None:
            # Nothing may follow an error frame.
            raise ChildOutputError
        if self._text_length is not None:
            self._take_text(data)
            return
        self._line += data
        while self._error is None and self._text_length is None:
            end = self._line.find(b"\n")
            if end < 0:
                if len(self._line) > _MAX_FRAME_LINE:
                    raise ChildOutputError
                return
            if end > _MAX_FRAME_LINE:
                raise ChildOutputError
            line = bytes(self._line[:end])
            rest = bytes(self._line[end + 1 :])
            self._line.clear()
            self._frame(line)
            if self._text_length is not None:
                self._take_text(rest)
            elif self._error is not None:
                if rest:
                    raise ChildOutputError
            else:
                self._line += rest

    def _frame(self, line: bytes) -> None:
        try:
            kind, _, value = line.decode("ascii").partition(" ")
        except UnicodeDecodeError:
            raise ChildOutputError from None
        if kind == "P" and not value:
            if self._on_progress is not None:
                self._on_progress()
        elif kind == "C" and value in self._caps_allowed:
            if value not in self._caps:
                self._caps.append(value)
        elif kind == "N":
            name, _, count = value.partition(" ")
            if name not in self._counts_allowed or not _COUNT.fullmatch(count):
                raise ChildOutputError
            self._counts[name] = int(count)
        elif kind == "E" and _TYPE_NAME.fullmatch(value):
            self._error = value
        elif kind == "T" and _COUNT.fullmatch(value):
            self._text_length = int(value)
        else:
            raise ChildOutputError

    def _take_text(self, data: bytes) -> None:
        assert self._text_length is not None
        if len(self._text) + len(data) > self._text_length:
            raise ChildOutputError
        self._text += data

    def error(self) -> str | None:
        """The type name of an ``E`` frame, if the child sent one."""
        return self._error

    def result(self) -> ChildResult:
        """The text frame's result; raises ``ChildOutputError`` when the
        output ended without a whole one."""
        if self._text_length is None or len(self._text) != self._text_length:
            raise ChildOutputError
        try:
            text = self._text.decode("utf-8")
        except UnicodeDecodeError:
            raise ChildOutputError from None
        return ChildResult(text, list(self._caps), dict(self._counts))
