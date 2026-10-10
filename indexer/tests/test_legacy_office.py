"""Legacy binary Office attachments (#935): the shared subprocess runner,
``.doc`` through catdoc, ``.xls`` through xlrd in a child process, and
``.ppt`` through Apache POI in a Java process (#957).

Fixtures are synthetic and tool-generated (``fixtures/extractors/README.md``).
Tests that need the real catdoc binary skip locally when it is missing
(it has no Homebrew formula); CI installs it, and
``test_catdoc_is_installed_in_ci`` fails there if it is not, so they
cannot skip in CI.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import resource
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest
from src import extractors
from src.extractors import (
    STATUS_FAILED,
    STATUS_SUCCESS,
    extract,
)
from src.extractors._runner import (
    ToolCrashError,
    ToolExitError,
    ToolOutput,
    ToolTimeoutError,
    run_tool,
)

FIXTURES = Path(__file__).parent / "fixtures" / "extractors"
DOC_FIXTURE = FIXTURES / "legacy.doc"
XLS_FIXTURE = FIXTURES / "legacy.xls"

MARKER = "SYNTHETIC_PAYLOAD_MARKER"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_IN_CI = bool(os.environ.get("CI"))
requires_catdoc = pytest.mark.skipif(
    shutil.which("catdoc") is None and not _IN_CI,
    reason="catdoc is not installed (no Homebrew formula); these tests run in CI and the image",
)
linux_only = pytest.mark.skipif(
    sys.platform != "linux",
    reason=(
        "real resource limits and crashing signals are asserted on Linux only (the image and "
        "CI): macOS does not enforce RLIMIT_AS and writes a crash report for each crash"
    ),
)


# Generous limits for the runner's own tests: a stand-in tool is a
# Python script, which starts well under them.
_LIMITS = {"max_address_space_bytes": 1024 * 1024 * 1024, "max_cpu_seconds": 60}


def _fake_tool(tmp_path: Path, body: str) -> str:
    """An executable Python script standing in for a tool."""
    path = tmp_path / "fake_tool"
    path.write_text(f"#!{sys.executable}\nimport json, os, resource, signal, sys, time\n{body}\n")
    path.chmod(0o700)
    return str(path)


def _stub_exit_status(monkeypatch, returncode: int) -> None:
    """Make a tool's exit status read as ``returncode``: the real tool
    is still reaped, so no process is left behind."""
    real_wait = subprocess.Popen.wait

    def wait(self, timeout=None):
        real_wait(self, timeout)
        return returncode

    monkeypatch.setattr(subprocess.Popen, "wait", wait)


def _process_exists(pid: int) -> bool:
    """Whether ``pid`` is a live process (a zombie counts as gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform == "linux":
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    return True


def _peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


# ---------------------------------------------------------------------------
# The shared runner
# ---------------------------------------------------------------------------


class TestRunTool:
    def test_returns_stdout_and_passes_a_private_temp_file(self, tmp_path):
        tool = _fake_tool(
            tmp_path,
            "path = sys.argv[-1]\n"
            "print(oct(os.stat(path).st_mode & 0o777), path, open(path, 'rb').read().decode())",
        )
        output = run_tool(
            [tool],
            b"payload bytes",
            timeout_seconds=30,
            max_output_bytes=4096,
            **_LIMITS,
            suffix=".doc",
        )
        mode, path, content = output.data.decode().split(" ", 2)
        assert mode == "0o600"
        assert path.endswith(".doc")
        assert content.strip() == "payload bytes"
        assert output.truncated is False
        # Deleted after the run.
        assert not Path(path).exists()

    def test_temp_file_is_deleted_when_the_tool_fails(self, tmp_path):
        record = tmp_path / "seen"
        tool = _fake_tool(tmp_path, f"open({str(record)!r}, 'w').write(sys.argv[-1])\nsys.exit(2)")
        with pytest.raises(ToolExitError):
            run_tool(
                [tool], b"x", timeout_seconds=30, max_output_bytes=10, **_LIMITS, suffix=".doc"
            )
        assert not Path(record.read_text()).exists()

    def test_output_is_read_up_to_the_cap_and_the_tool_killed(self, tmp_path):
        """Never read whole and cut: the parent stops at the cap, and the
        tool, which would write 64 MB, is killed."""
        tool = _fake_tool(
            tmp_path,
            "for _ in range(1024):\n    sys.stdout.buffer.write(b'a' * 65536)\n    sys.stdout.flush()",
        )
        started = time.monotonic()
        output = run_tool(
            [tool], b"x", timeout_seconds=30, max_output_bytes=100_000, **_LIMITS, suffix=".x"
        )
        assert output.truncated is True
        assert output.data == b"a" * 100_000
        assert time.monotonic() - started < 20

    def test_timeout_kills_the_tool(self, tmp_path):
        tool = _fake_tool(tmp_path, "time.sleep(60)")
        started = time.monotonic()
        with pytest.raises(ToolTimeoutError):
            run_tool([tool], b"x", timeout_seconds=0.5, max_output_bytes=10, **_LIMITS, suffix=".x")
        assert time.monotonic() - started < 10

    def test_timeout_applies_while_the_tool_writes(self, tmp_path):
        """A tool that keeps writing a little, under the cap, still meets
        the deadline."""
        tool = _fake_tool(
            tmp_path,
            "while True:\n    sys.stdout.write('a')\n    sys.stdout.flush()\n    time.sleep(0.05)",
        )
        with pytest.raises(ToolTimeoutError):
            run_tool(
                [tool],
                b"x",
                timeout_seconds=0.5,
                max_output_bytes=1_000_000,
                **_LIMITS,
                suffix=".x",
            )

    def test_timeout_applies_after_the_tool_closes_stdout(self, tmp_path):
        tool = _fake_tool(tmp_path, "os.close(1)\ntime.sleep(60)")
        with pytest.raises(ToolTimeoutError):
            run_tool([tool], b"x", timeout_seconds=0.5, max_output_bytes=10, **_LIMITS, suffix=".x")

    def test_a_death_by_signal_is_a_fixed_error(self, tmp_path):
        # SIGKILL: macOS writes no crash report for it.
        tool = _fake_tool(tmp_path, "os.kill(os.getpid(), signal.SIGKILL)")
        with pytest.raises(ToolCrashError) as excinfo:
            run_tool([tool], b"x", timeout_seconds=30, max_output_bytes=10, **_LIMITS, suffix=".x")
        assert str(excinfo.value) == "extraction tool killed by a signal"

    @pytest.mark.parametrize("returncode", [-11, -24, -6])
    def test_any_signal_maps_to_the_crash_error(self, monkeypatch, tmp_path, returncode):
        """SIGSEGV, SIGXCPU and SIGABRT, by the status the parent sees:
        stubbed, so no tool crashes for real."""

        _stub_exit_status(monkeypatch, returncode)
        tool = _fake_tool(tmp_path, "pass")
        with pytest.raises(ToolCrashError):
            run_tool([tool], b"x", timeout_seconds=30, max_output_bytes=10, **_LIMITS, suffix=".x")

    @linux_only
    def test_a_real_segfault_is_a_fixed_error(self, tmp_path):
        tool = _fake_tool(tmp_path, "os.kill(os.getpid(), signal.SIGSEGV)")
        with pytest.raises(ToolCrashError):
            run_tool([tool], b"x", timeout_seconds=30, max_output_bytes=10, **_LIMITS, suffix=".x")

    def test_non_zero_exit_withholds_stderr_and_stdout(self, tmp_path, caplog):
        caplog.set_level("DEBUG")
        tool = _fake_tool(
            tmp_path,
            f"sys.stderr.write({MARKER!r})\nsys.stdout.write({MARKER!r})\nsys.exit(1)",
        )
        with pytest.raises(ToolExitError) as excinfo:
            run_tool(
                [tool], b"x", timeout_seconds=30, max_output_bytes=1000, **_LIMITS, suffix=".x"
            )
        assert MARKER not in str(excinfo.value)
        assert MARKER not in caplog.text

    def test_non_zero_exit_carries_its_status_and_fixed_text(self, tmp_path):
        """#983: the status is kept for a caller that gives one a meaning
        (only the ``ppt`` extractor does); the message stays fixed."""
        tool = _fake_tool(tmp_path, f"sys.stderr.write({MARKER!r})\nsys.exit(10)")
        with pytest.raises(ToolExitError) as excinfo:
            run_tool([tool], b"x", timeout_seconds=30, max_output_bytes=10, **_LIMITS, suffix=".x")
        assert excinfo.value.returncode == 10
        assert str(excinfo.value) == "extraction tool exited with an error"

    def test_tool_runs_under_the_limits_it_is_given(self, tmp_path):
        """The launcher sets both limits and then ``execve``s the tool,
        which reports its own: the limits are inherited, not applied to
        the launcher alone."""
        tool = _fake_tool(
            tmp_path,
            "print(json.dumps({'argv': sys.argv[1:-1], "
            "'cpu': resource.getrlimit(resource.RLIMIT_CPU), "
            "'as': resource.getrlimit(resource.RLIMIT_AS)}))",
        )
        output = run_tool(
            [tool, "-a", "b"],
            b"x",
            timeout_seconds=30,
            max_output_bytes=4096,
            max_address_space_bytes=768 * 1024 * 1024,
            max_cpu_seconds=7,
            suffix=".x",
        )
        seen = json.loads(output.data)
        assert seen["argv"] == ["-a", "b"]
        assert seen["cpu"] == [7, 8]
        if sys.platform == "linux":
            assert seen["as"] == [768 * 1024 * 1024] * 2

    @linux_only
    def test_a_real_cpu_limit_hit_is_a_crash_error(self, tmp_path):
        """SIGXCPU from the CPU limit, real only on Linux: on macOS it
        would write a crash report (the stubbed status test covers it)."""
        tool = _fake_tool(tmp_path, "while True:\n    pass")
        started = time.monotonic()
        with pytest.raises(ToolCrashError):
            run_tool(
                [tool],
                b"x",
                timeout_seconds=30,
                max_output_bytes=10,
                max_address_space_bytes=1024 * 1024 * 1024,
                max_cpu_seconds=1,
                suffix=".x",
            )
        assert time.monotonic() - started < 15

    @linux_only
    def test_a_real_address_space_hit_is_an_exit_error(self, tmp_path):
        tool = _fake_tool(tmp_path, "bytearray(512 * 1024 * 1024)")
        with pytest.raises(ToolExitError):
            run_tool(
                [tool],
                b"x",
                timeout_seconds=30,
                max_output_bytes=10,
                max_address_space_bytes=256 * 1024 * 1024,
                max_cpu_seconds=30,
                suffix=".x",
            )

    def test_payload_is_in_a_private_scratch_directory_that_is_the_tools_tmpdir(self, tmp_path):
        tool = _fake_tool(
            tmp_path,
            "path = sys.argv[-1]\n"
            "print(json.dumps({'dir': os.path.dirname(path), 'tmpdir': os.environ['TMPDIR'], "
            "'mode': oct(os.stat(os.path.dirname(path)).st_mode & 0o777)}))",
        )
        output = run_tool(
            [tool], b"x", timeout_seconds=30, max_output_bytes=4096, **_LIMITS, suffix=".x"
        )
        seen = json.loads(output.data)
        assert seen["dir"] == seen["tmpdir"]
        assert seen["mode"] == "0o700"
        assert not Path(seen["dir"]).exists()

    @pytest.mark.parametrize("end", ["timeout", "cap", "exit", "error"])
    def test_scratch_files_are_removed_however_the_run_ends(self, tmp_path, monkeypatch, end):
        """#1291: a file the tool (or a tool it starts) writes to its
        scratch directory is removed with it, also when the tool is
        killed."""
        from src.extractors import _runner

        scratch_root = tmp_path / "tmp"
        scratch_root.mkdir()
        monkeypatch.setattr(_runner, "_TMP_DIR", str(scratch_root))
        finish = {
            "timeout": "time.sleep(60)",
            "cap": "sys.stdout.write('a' * 100)",
            "exit": "pass",
            "error": "sys.exit(2)",
        }[end]
        tool = _fake_tool(
            tmp_path,
            "scratch = os.path.dirname(sys.argv[-1])\n"
            "open(os.path.join(scratch, 'left-behind'), 'w').write('x')\n"
            "os.makedirs(os.path.join(scratch, 'sub', 'dir'))\n" + finish,
        )
        try:
            run_tool([tool], b"x", timeout_seconds=2, max_output_bytes=10, **_LIMITS, suffix=".x")
        except ToolTimeoutError, ToolExitError:
            pass
        assert list(scratch_root.iterdir()) == []

    @pytest.mark.parametrize("end", ["timeout", "cap", "exit"])
    def test_processes_the_tool_starts_are_killed_with_it(self, tmp_path, end):
        """#1291: the tool runs in its own session and its whole process
        group is killed (``SIGKILL``) when the run ends: a grandchild
        does not outlive a timeout, the output cap or a normal exit."""
        pid_file = tmp_path / "grandchild.pid"
        finish = {
            "timeout": "time.sleep(60)",
            "cap": "sys.stdout.write('a' * 100)\nsys.stdout.flush()\ntime.sleep(60)",
            "exit": "pass",
        }[end]
        tool = _fake_tool(
            tmp_path,
            "import subprocess\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n" + finish,
        )
        started = time.monotonic()
        try:
            run_tool([tool], b"x", timeout_seconds=2, max_output_bytes=10, **_LIMITS, suffix=".x")
        except ToolTimeoutError:
            assert end == "timeout"
        assert time.monotonic() - started < 15
        grandchild = int(pid_file.read_text())
        deadline = time.monotonic() + 10
        while _process_exists(grandchild):
            assert time.monotonic() < deadline, "the grandchild outlived the run"
            time.sleep(0.05)

    def test_output_is_streamed_to_the_callback(self, tmp_path):
        tool = _fake_tool(tmp_path, "sys.stdout.write('abc')")
        seen: list[bytes] = []
        output = run_tool(
            [tool],
            b"x",
            timeout_seconds=30,
            max_output_bytes=2,
            **_LIMITS,
            suffix=".x",
            on_output=seen.append,
        )
        assert output == ToolOutput(b"", truncated=True)
        assert b"".join(seen) == b"ab"

    def test_tool_gets_no_inherited_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYNTHETIC_SECRET_ENV", MARKER)
        tool = _fake_tool(tmp_path, "print(sorted(os.environ))")
        output = run_tool(
            [tool], b"x", timeout_seconds=30, max_output_bytes=4096, **_LIMITS, suffix=".x"
        )
        assert b"SYNTHETIC_SECRET_ENV" not in output.data


def _run_tool_calls() -> dict[str, list[ast.Call]]:
    """Every call to ``run_tool`` or ``run_child`` (which calls
    ``run_tool``) in the indexer's source, by file."""
    src = Path(extractors.__file__).parents[1]
    calls: dict[str, list[ast.Call]] = {}
    for path in sorted(src.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in {"run_tool", "run_child"}:
                calls.setdefault(str(path.relative_to(src)), []).append(node)
    return calls


# External programs the indexer starts without ``run_tool``, so without
# its address-space and CPU limits, and why. Each has a timeout only.
_TOOLS_OUTSIDE_RUN_TOOL = {
    # Runs only in the extractor child, which ``image.py`` starts through
    # ``run_child``: each Tesseract inherits the child's limits and dies
    # with its process group (#1292).
    "extractors/image_child.py": "Tesseract through pytesseract, in the extractor child (#1292)",
    # pdf2image runs pdfinfo and pdftoppm, and pytesseract Tesseract,
    # under the OCR timeout.
    "extractors/pdf.py": "pdfinfo, pdftoppm and Tesseract through pdf2image and pytesseract (#1021)",
}


class TestEveryToolRunsUnderLimits:
    """#995: every external tool gets address-space and CPU limits
    through the one path, ``run_tool``, whose callers must each pass
    both; a program started any other way is listed above with its
    reason."""

    def test_every_run_tool_caller_passes_both_limits(self):
        calls = _run_tool_calls()
        # Guards the scan: it finds the callers (and nothing passes
        # limits through ``**kwargs``, which it could not check).
        assert set(calls) == {
            "extractors/_runner.py",
            "extractors/doc.py",
            "extractors/docx.py",
            "extractors/eml.py",
            "extractors/image.py",
            "extractors/ooxml.py",
            "extractors/ppt.py",
            "extractors/pptx.py",
            "extractors/xls.py",
            "extractors/xlsx.py",
        }
        for path, nodes in calls.items():
            for node in nodes:
                keywords = {k.arg for k in node.keywords}
                assert None not in keywords, path
                assert {"max_address_space_bytes", "max_cpu_seconds"} <= keywords, path

    def test_the_indexer_never_loads_the_image_child(self):
        """``image_child`` starts Tesseract; only the extractor child may
        import it (#1292). Importing the image extractor and running the
        dispatcher's imports loads neither it nor pytesseract."""
        code = (
            "import sys; import src.extractors, src.extractors.image; "
            "print(sorted(m for m in ('src.extractors.image_child', 'pytesseract') "
            "if m in sys.modules))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(extractors.__file__).parents[2],
            capture_output=True,
            text=True,
            check=True,
        )
        assert out.stdout.strip() == "[]"

    def test_no_other_module_starts_a_program(self):
        src = Path(extractors.__file__).parents[1]
        starters = re.compile(
            r"^\s*(import (subprocess|pytesseract|pdf2image)|"
            r"from (subprocess|pytesseract|pdf2image)( import|\.))|os\.(exec|spawn|posix_spawn|system|popen)",
            re.M,
        )
        found = {
            str(path.relative_to(src))
            for path in src.rglob("*.py")
            if starters.search(path.read_text())
        }
        allowed = {"extractors/_runner.py", "extractors/_launcher.py"}
        assert found - allowed == set(_TOOLS_OUTSIDE_RUN_TOOL)
        assert all(reason.strip() for reason in _TOOLS_OUTSIDE_RUN_TOOL.values())


# ---------------------------------------------------------------------------
# .doc through catdoc
# ---------------------------------------------------------------------------


def test_catdoc_is_installed_in_ci():
    """The catdoc tests skip only off CI: CI installs the package, so a
    missing binary there is a failure, not a skip."""
    if not _IN_CI:
        pytest.skip("only checked in CI")
    assert shutil.which("catdoc") is not None


class TestDocExtractor:
    @requires_catdoc
    def test_real_doc_extracts_its_text(self):
        result = extract(
            content_type="application/msword",
            filename="memo.doc",
            payload=DOC_FIXTURE.read_bytes(),
        )
        assert result.status == STATUS_SUCCESS
        assert result.extractor == "doc@2"
        assert result.text is not None
        assert "The COBALT-LANTERN ledger code is 4471." in result.text
        assert "Café crème at the Zürich office, naïve résumé." in result.text

    @requires_catdoc
    def test_real_doc_by_extension(self):
        result = extract(
            content_type="application/octet-stream",
            filename="memo.DOC",
            payload=DOC_FIXTURE.read_bytes(),
        )
        assert result.status == STATUS_SUCCESS
        assert "COBALT-LANTERN" in (result.text or "")

    @requires_catdoc
    def test_catdoc_failure_on_garbage_ole2_is_a_fixed_failed(self, caplog):
        caplog.set_level("DEBUG")
        payload = _OLE2_MAGIC + MARKER.encode() + bytes(2048)
        result = extract(content_type="application/msword", filename="a.doc", payload=payload)
        assert result.status in (STATUS_FAILED, "empty")
        if result.status == STATUS_FAILED:
            assert result.error in {"ToolExitError", "ToolCrashError"}
        assert MARKER not in caplog.text

    def test_missing_binary_is_failed(self, monkeypatch):
        from src.extractors import doc

        monkeypatch.setattr(doc.shutil, "which", lambda _name: None)
        result = extract(
            content_type="application/msword", filename="a.doc", payload=_OLE2_MAGIC + bytes(64)
        )
        assert (result.status, result.error) == (STATUS_FAILED, "ToolNotFoundError")

    # Only the sleep needs the short timeout to reach the timeout path.
    # The others start two Python processes, which a loaded run can take
    # longer than 0.5 s to start (#1336).
    @pytest.mark.parametrize(
        ("body", "error", "timeout"),
        [
            ("time.sleep(60)", "ToolTimeoutError", 0.5),
            (
                f"sys.stderr.write({MARKER!r})\nsys.stdout.write({MARKER!r})\nsys.exit(1)",
                "ToolExitError",
                30.0,
            ),
            ("os.kill(os.getpid(), signal.SIGKILL)", "ToolCrashError", 30.0),
        ],
    )
    def test_tool_failures_are_fixed_failed_rows(
        self, tmp_path, monkeypatch, caplog, body, error, timeout
    ):
        from src.extractors import doc

        caplog.set_level("DEBUG")
        tool = _fake_tool(tmp_path, body)
        monkeypatch.setattr(doc.shutil, "which", lambda _name: tool)
        monkeypatch.setattr(doc, "TOOL_TIMEOUT_SECONDS", timeout)
        result = extract(
            content_type="application/msword",
            filename=f"{MARKER}.doc",
            payload=_OLE2_MAGIC + MARKER.encode(),
        )
        assert (result.status, result.error) == (STATUS_FAILED, error)
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert error in warnings[0].getMessage()
        assert MARKER not in caplog.text

    def test_the_ppt_encrypted_status_means_nothing_to_catdoc(self, tmp_path, monkeypatch):
        """#983: only the ``ppt`` extractor reads that exit status; from
        catdoc it is an ordinary failure."""
        from src.extractors import doc, ppt

        tool = _fake_tool(tmp_path, f"sys.exit({ppt.ENCRYPTED_EXIT_STATUS})")
        monkeypatch.setattr(doc.shutil, "which", lambda _name: tool)
        result = extract(
            content_type="application/msword", filename="a.doc", payload=_OLE2_MAGIC + bytes(64)
        )
        assert (result.status, result.error) == (STATUS_FAILED, "ToolExitError")

    def test_catdoc_runs_under_both_limits(self, tmp_path, monkeypatch):
        """#995: a stand-in catdoc reports its own limits, so the launch
        path is proved to set them, not only to pass them."""
        from src.extractors import doc

        tool = _fake_tool(
            tmp_path,
            "print(json.dumps({'cpu': resource.getrlimit(resource.RLIMIT_CPU), "
            "'as': resource.getrlimit(resource.RLIMIT_AS), 'env': sorted(os.environ)}))",
        )
        monkeypatch.setattr(doc.shutil, "which", lambda _name: tool)
        monkeypatch.setenv("SYNTHETIC_SECRET_ENV", MARKER)
        text, _ = doc.extract(_OLE2_MAGIC)
        seen = json.loads(text)
        assert seen["cpu"] == [doc.CHILD_MAX_CPU_SECONDS, doc.CHILD_MAX_CPU_SECONDS + 1]
        if sys.platform == "linux":
            assert seen["as"] == [doc.CHILD_MAX_ADDRESS_SPACE_BYTES] * 2
        assert "SYNTHETIC_SECRET_ENV" not in seen["env"]
        # The timeout is past the CPU limit, so a CPU-bound run meets
        # the limit first.
        assert doc.TOOL_TIMEOUT_SECONDS > doc.CHILD_MAX_CPU_SECONDS + 1

    @pytest.mark.parametrize("returncode", [-11, -24, -9])
    def test_signal_statuses_are_crash_rows(self, tmp_path, monkeypatch, caplog, returncode):
        """SIGSEGV, SIGXCPU (the CPU limit) and SIGKILL (its hard limit),
        by the status the parent sees: stubbed, so nothing crashes for
        real on macOS."""

        from src.extractors import doc

        caplog.set_level("DEBUG")
        tool = _fake_tool(tmp_path, f"sys.stdout.write({MARKER!r})")
        monkeypatch.setattr(doc.shutil, "which", lambda _name: tool)
        _stub_exit_status(monkeypatch, returncode)
        result = extract(content_type="application/msword", filename="a.doc", payload=_OLE2_MAGIC)
        assert (result.status, result.error, result.text) == (STATUS_FAILED, "ToolCrashError", None)
        assert MARKER not in caplog.text

    @requires_catdoc
    @linux_only
    def test_real_catdoc_over_its_address_space_is_a_fixed_failed_row(self, monkeypatch, caplog):
        """Under 2 MiB catdoc cannot even map libc (measured in the
        image) and exits with an error: the limit reaches the real
        binary, and the hit is a fixed failed row."""
        from src.extractors import doc

        caplog.set_level("DEBUG")
        monkeypatch.setattr(doc, "CHILD_MAX_ADDRESS_SPACE_BYTES", 2 * 1024 * 1024)
        result = extract(
            content_type="application/msword",
            filename="memo.doc",
            payload=DOC_FIXTURE.read_bytes(),
        )
        assert (result.status, result.error, result.text) == (STATUS_FAILED, "ToolExitError", None)
        assert "COBALT" not in caplog.text

    @linux_only
    def test_real_cpu_limit_hit_is_a_fixed_failed_row(self, tmp_path, monkeypatch, caplog):
        from src.extractors import doc

        caplog.set_level("DEBUG")
        tool = _fake_tool(tmp_path, f"sys.stderr.write({MARKER!r})\nwhile True:\n    pass")
        monkeypatch.setattr(doc.shutil, "which", lambda _name: tool)
        monkeypatch.setattr(doc, "CHILD_MAX_CPU_SECONDS", 1)
        started = time.monotonic()
        result = extract(content_type="application/msword", filename="a.doc", payload=_OLE2_MAGIC)
        assert (result.status, result.error) == (STATUS_FAILED, "ToolCrashError")
        assert time.monotonic() - started < 15
        assert MARKER not in caplog.text

    def test_catdoc_gets_a_fixed_charset_and_no_wrapping(self, tmp_path, monkeypatch):
        from src.extractors import doc

        tool = _fake_tool(tmp_path, "print(' '.join(sys.argv[1:-1]))")
        monkeypatch.setattr(doc.shutil, "which", lambda _name: tool)
        text, name = doc.extract(_OLE2_MAGIC)
        assert (text.strip(), name) == ("-d utf-8 -w", "doc")

    @pytest.mark.parametrize("error", [MemoryError, RecursionError])
    def test_host_pressure_in_the_parent_propagates(self, monkeypatch, error):
        from src.extractors import doc

        def raise_(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(doc, "run_tool", raise_)
        monkeypatch.setattr(doc.shutil, "which", lambda _name: "/bin/true")
        with pytest.raises(error):
            extract(content_type="application/msword", filename="a.doc", payload=_OLE2_MAGIC)


# ---------------------------------------------------------------------------
# .xls through xlrd in a child process
# ---------------------------------------------------------------------------


def _sst_bomb() -> bytes:
    """The generated workbook with its shared-string table turned into
    the loop measured for #935: the declared count is 2^31 - 1 and the
    first string ("Item", 7 bytes) becomes an empty string whose
    phonetic size is -7, so xlrd re-reads it forever. Same length, so
    the OLE2 container is unchanged."""
    data = bytearray(XLS_FIXTURE.read_bytes())
    first = data.find(b"\x04\x00\x00Item")
    assert first > 0 and data.count(b"\x04\x00\x00Item") == 1
    data[first - 4 : first] = struct.pack("<i", 2**31 - 1)
    data[first : first + 7] = b"\x00\x00\x04" + struct.pack("<i", -7)
    return bytes(data)


def _self_referencing_directory() -> bytes:
    """The generated workbook with the root storage's first child made
    its own left sibling: a cycle in the OLE2 directory tree, which
    xlrd's ``_build_family_tree`` follows with no cycle check."""
    data = bytearray(XLS_FIXTURE.read_bytes())
    sector_size = 1 << struct.unpack_from("<H", data, 0x1E)[0]
    directory = 512 + struct.unpack_from("<i", data, 0x30)[0] * sector_size
    child = struct.unpack_from("<i", data, directory + 0x4C)[0]
    assert child > 0
    struct.pack_into("<i", data, directory + child * 128 + 0x44, child)
    return bytes(data)


def _xls(payload: bytes, **kwargs):
    return extract(
        content_type="application/vnd.ms-excel", filename="book.xls", payload=payload, **kwargs
    )


class TestXlsExtractor:
    def test_real_xls_extracts_both_sheets(self):
        result = _xls(XLS_FIXTURE.read_bytes())
        assert result.status == STATUS_SUCCESS
        assert result.extractor == "xls@1"
        assert result.text == (
            "[Sheet: Summary]\nItem\tNote\nCOBALT-LANTERN\tCafé crème, Zürich"
            "\n\n[Sheet: Détails]\nSecond-sheet code\tOBSIDIAN-HERON 8812"
        )

    def test_real_xls_by_extension(self):
        result = extract(
            content_type="application/octet-stream",
            filename="book.XLS",
            payload=XLS_FIXTURE.read_bytes(),
        )
        assert "OBSIDIAN-HERON 8812" in (result.text or "")

    @linux_only
    def test_sst_loop_meets_the_address_space_limit(self, monkeypatch):
        """The shared-string loop runs until the child's address space
        runs out: the child reports MemoryError, a bounded failed row. The parent's
        own memory does not grow with it."""
        from src.extractors import xls

        monkeypatch.setattr(xls, "CHILD_MAX_ADDRESS_SPACE_BYTES", 256 * 1024 * 1024)
        before = _peak_rss_bytes()
        started = time.monotonic()
        result = _xls(_sst_bomb())
        assert (result.status, result.error) == (STATUS_FAILED, "MemoryError")
        assert time.monotonic() - started < 30
        assert _peak_rss_bytes() - before < 64 * 1024 * 1024

    @linux_only
    def test_cpu_limit_kills_the_child(self, monkeypatch):
        """SIGXCPU, real only on Linux: on macOS it would write a crash
        report (the mapping is covered by the stubbed status test)."""
        from src.extractors import xls

        monkeypatch.setattr(xls, "CHILD_MAX_CPU_SECONDS", 1)
        started = time.monotonic()
        result = _xls(_sst_bomb())
        assert (result.status, result.error) == (STATUS_FAILED, "ToolCrashError")
        assert time.monotonic() - started < 15

    def test_wall_clock_timeout_kills_the_child(self, monkeypatch):
        from src.extractors import xls

        monkeypatch.setattr(xls, "XLS_TIMEOUT_SECONDS", 0.5)
        started = time.monotonic()
        result = _xls(_sst_bomb())
        assert (result.status, result.error) == (STATUS_FAILED, "ToolTimeoutError")
        assert time.monotonic() - started < 10

    def test_directory_cycle_is_a_failed_row_not_recursion(self):
        from src.extractors import xls_child

        payload = _self_referencing_directory()
        # The shape is the one claimed: in-process, xlrd recurses until
        # RecursionError ...
        with pytest.raises(RecursionError):
            xls_child.extract_text(payload)
        # ... which the dispatcher would treat as host pressure. In the
        # child it is a failed row reported by type.
        result = _xls(payload)
        assert (result.status, result.error) == (STATUS_FAILED, "RecursionError")

    def test_garbage_ole2_is_failed_without_quoting_it(self, caplog):
        caplog.set_level("DEBUG")
        result = _xls(_OLE2_MAGIC + MARKER.encode() + bytes(1024))
        assert (result.status, result.error) == (STATUS_FAILED, "CompDocError")
        assert MARKER not in caplog.text


class TestXlsChildWalk:
    """The child's budgets, run in-process so the work done is counted."""

    @staticmethod
    def _count(monkeypatch) -> dict[str, int]:
        import xlrd

        counts = {"sheets": 0, "rows": 0, "unloaded": 0}
        load, row_values, unload = (
            xlrd.book.Book.sheet_by_index,
            xlrd.sheet.Sheet.row_values,
            xlrd.book.Book.unload_sheet,
        )

        def counting_load(self, index):
            counts["sheets"] += 1
            return load(self, index)

        def counting_rows(self, rowx, *args):
            counts["rows"] += 1
            return row_values(self, rowx, *args)

        def counting_unload(self, index):
            counts["unloaded"] += 1
            return unload(self, index)

        monkeypatch.setattr(xlrd.book.Book, "sheet_by_index", counting_load)
        monkeypatch.setattr(xlrd.sheet.Sheet, "row_values", counting_rows)
        monkeypatch.setattr(xlrd.book.Book, "unload_sheet", counting_unload)
        return counts

    def test_every_sheet_is_unloaded(self, monkeypatch):
        from src.extractors import xls_child

        counts = self._count(monkeypatch)
        text, caps = xls_child.extract_text(XLS_FIXTURE.read_bytes())
        assert caps == []
        assert "OBSIDIAN-HERON" in text
        assert counts == {"sheets": 2, "rows": 3, "unloaded": 2}

    def test_sheet_budget_stops_before_loading(self, monkeypatch):
        from src.extractors import xls_child

        monkeypatch.setattr(xls_child, "_MAX_SHEETS", 1)
        counts = self._count(monkeypatch)
        text, caps = xls_child.extract_text(XLS_FIXTURE.read_bytes())
        assert caps == ["xls_sheets"]
        assert text == "[Sheet: Summary]\nItem\tNote\nCOBALT-LANTERN\tCafé crème, Zürich"
        assert counts["sheets"] == 1

    def test_cell_budget_stops_the_walk_and_the_next_load(self, monkeypatch):
        from src.extractors import xls_child

        # Two two-cell rows fit; the second sheet's row does not.
        monkeypatch.setattr(xls_child, "_MAX_EXPANDED_CELLS", 2 * (2 + xls_child._ROW_COST))
        counts = self._count(monkeypatch)
        text, caps = xls_child.extract_text(XLS_FIXTURE.read_bytes())
        assert caps == ["xls_expanded_cells"]
        assert "OBSIDIAN" not in text
        assert counts == {"sheets": 2, "rows": 3, "unloaded": 2}

    def test_cell_budget_spent_on_one_sheet_loads_no_more(self, monkeypatch):
        from src.extractors import xls_child

        monkeypatch.setattr(xls_child, "_MAX_EXPANDED_CELLS", 1)
        counts = self._count(monkeypatch)
        text, caps = xls_child.extract_text(XLS_FIXTURE.read_bytes())
        assert (text, caps) == ("", ["xls_expanded_cells"])
        assert counts == {"sheets": 1, "rows": 1, "unloaded": 1}

    def test_text_budget_cuts_a_value(self, monkeypatch):
        from src.extractors import xls_child

        header = "[Sheet: Summary]"
        monkeypatch.setattr(xls_child, "_MAX_TEXT_CHARS", len("Summary") + 11 + 3)
        counts = self._count(monkeypatch)
        text, caps = xls_child.extract_text(XLS_FIXTURE.read_bytes())
        assert (text, caps) == (f"{header}\nIt", ["xls_text_chars"])
        assert counts["rows"] == 1

    def test_cell_values_are_written_as_the_xlsx_extractor_writes_them(self):
        import xlrd
        from src.extractors import xls_child

        assert xls_child._cell_text(xlrd.XL_CELL_NUMBER, 4471.0, 0) == "4471"
        assert xls_child._cell_text(xlrd.XL_CELL_NUMBER, 2.5, 0) == "2.5"
        assert xls_child._cell_text(xlrd.XL_CELL_NUMBER, 1e20, 0) == "1e+20"
        assert xls_child._cell_text(xlrd.XL_CELL_BOOLEAN, 1, 0) == "TRUE"
        assert xls_child._cell_text(xlrd.XL_CELL_BOOLEAN, 0, 0) == "FALSE"
        assert xls_child._cell_text(xlrd.XL_CELL_DATE, 45000.0, 0) == "2023-03-15 00:00:00"
        # A date xlrd cannot convert keeps its number.
        assert xls_child._cell_text(xlrd.XL_CELL_DATE, 1e10, 0) == "10000000000"
        for ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK, xlrd.XL_CELL_ERROR):
            assert xls_child._cell_text(ctype, 7, 0) is None

    def test_separators_inside_a_value_become_spaces(self, monkeypatch):
        import xlrd
        from src.extractors import xls_child

        class Sheet:
            name = "S"
            nrows = 1

            def row_types(self, _rowx):
                return [xlrd.XL_CELL_TEXT, xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_TEXT, xlrd.XL_CELL_TEXT]

            def row_values(self, _rowx):
                return ["a\tb", "", "  ", "c\nd"]

        class Book:
            nsheets = 1
            datemode = 0

            def sheet_by_index(self, _index):
                return Sheet()

            def unload_sheet(self, _index):
                pass

        assert xls_child._walk(Book()) == ("[Sheet: S]\na b\t\t\tc d", [])


# ---------------------------------------------------------------------------
# .ppt through Apache POI in a Java process (#957)
# ---------------------------------------------------------------------------

PPT_FIXTURE = FIXTURES / "legacy.ppt"
PPT_LO_FIXTURE = FIXTURES / "legacy-lo.ppt"
# Password-protected, written by POI (#983, fixtures/extractors/README.md).
PPT_ENCRYPTED_FIXTURE = FIXTURES / "legacy-encrypted.ppt"
ENCRYPTED_DECK_MARKER = "SYNTHETIC-ENCRYPTED-DECK"
_PPT_MIME = "application/vnd.ms-powerpoint"

# The image's runtime, or the one CI exports from indexer/Dockerfile.
_PPT_HOME = Path(os.environ.get("INDEXER_TEST_PPT_HOME", "/opt/ppt"))
_HAS_PPT_RUNTIME = (_PPT_HOME / "jre" / "bin" / "java").is_file()
requires_ppt_runtime = pytest.mark.skipif(
    not _HAS_PPT_RUNTIME and not _IN_CI,
    reason=(
        "the .ppt Java runtime is built by indexer/Dockerfile for Linux; set "
        "INDEXER_TEST_PPT_HOME to an exported /opt/ppt (CI does) to run these"
    ),
)


@pytest.fixture
def real_ppt_home(monkeypatch):
    from src.extractors import ppt

    monkeypatch.setattr(ppt, "PPT_HOME", _PPT_HOME)


def _fake_ppt_home(tmp_path: Path, body: str) -> Path:
    """A ``PPT_HOME`` whose ``java`` is a Python script running ``body``."""
    home = tmp_path / "ppt"
    java = home / "jre" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.write_text(f"#!{sys.executable}\nimport json, os, resource, signal, sys, time\n{body}\n")
    java.chmod(0o700)
    return home


def _ppt(payload: bytes, **kwargs):
    return extract(content_type=_PPT_MIME, filename="deck.ppt", payload=payload, **kwargs)


def test_ppt_runtime_is_installed_in_ci():
    """The real-JVM tests skip only off CI: CI exports the runtime from
    indexer/Dockerfile, so a missing runtime there is a failure."""
    if not _IN_CI:
        pytest.skip("only checked in CI")
    assert _HAS_PPT_RUNTIME, f"no Java runtime under {_PPT_HOME}"


@requires_ppt_runtime
@pytest.mark.usefixtures("real_ppt_home")
class TestPptRealReader:
    def test_powerpoint_deck_yields_placeholders_text_box_and_non_ascii(self):
        """The deck PowerPoint 16 saved keeps all slide text in drawing
        records, which catppt never read (#958)."""
        result = _ppt(PPT_FIXTURE.read_bytes())
        assert (result.status, result.extractor) == (STATUS_SUCCESS, "ppt@2")
        text = result.text or ""
        assert "Synthetic legacy slide deck" in text
        assert "The AMBER-KESTREL project code is 5129." in text
        assert "The text box holds TEAL-MARMOT 3307." in text
        assert "Café crème at the Zürich office, naïve résumé." in text
        # Nothing the JVM or Log4j prints reaches the text.
        assert "warning" not in text.lower()
        assert "log4j" not in text.lower()

    def test_powerpoint_deck_by_extension(self):
        result = extract(
            content_type="application/octet-stream",
            filename="DECK.PPT",
            payload=PPT_FIXTURE.read_bytes(),
        )
        assert result.status == STATUS_SUCCESS
        assert "AMBER-KESTREL" in (result.text or "")

    def test_libreoffice_deck_yields_its_text(self):
        result = _ppt(PPT_LO_FIXTURE.read_bytes())
        assert result.status == STATUS_SUCCESS
        text = result.text or ""
        assert "Synthetic legacy slide deck" in text
        assert "The AMBER-KESTREL project code is 5129." in text
        assert "Café crème at the Zürich office, naïve résumé." in text

    def test_garbage_ole2_is_a_fixed_failed_row(self, caplog):
        caplog.set_level("DEBUG")
        result = _ppt(_OLE2_MAGIC + MARKER.encode() + bytes(2048))
        assert (result.status, result.error) == (STATUS_FAILED, "ToolExitError")
        assert MARKER not in caplog.text

    def test_encrypted_deck_is_a_fixed_unsupported_row(self, caplog):
        """#983: POI raises its encrypted-file exception for a
        password-protected deck; the reader exits with the reserved
        status and the row is ``unsupported``, served for good, instead
        of ``failed`` and re-run every 7 days."""
        from src.extractors import ENCRYPTED_PPT_ERROR, STATUS_UNSUPPORTED

        caplog.set_level("DEBUG")
        payload = PPT_ENCRYPTED_FIXTURE.read_bytes()
        assert payload.startswith(_OLE2_MAGIC)
        # The slide text is only in the encrypted streams.
        assert ENCRYPTED_DECK_MARKER.encode() not in payload
        extractors.drain_extractor_counts()
        result = _ppt(payload)
        assert (result.status, result.extractor, result.text, result.error) == (
            STATUS_UNSUPPORTED,
            "ppt@2",
            None,
            ENCRYPTED_PPT_ERROR,
        )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "recorded unsupported" in warnings[0].getMessage()
        assert ENCRYPTED_PPT_ERROR in warnings[0].getMessage()
        assert ENCRYPTED_DECK_MARKER not in caplog.text


class TestPptExtractor:
    def test_java_runs_through_the_launcher_with_limits_and_options(self, tmp_path, monkeypatch):
        """The launcher lowers its limits and caps glibc's malloc arenas,
        then ``execve``s Java with the fixed options, the jars and the
        payload file; nothing else is inherited."""
        import json

        from src.extractors import ppt

        home = _fake_ppt_home(
            tmp_path,
            "print(json.dumps({'argv': sys.argv, 'env': dict(os.environ), "
            "'cpu': resource.getrlimit(resource.RLIMIT_CPU), "
            "'as': resource.getrlimit(resource.RLIMIT_AS)}))",
        )
        monkeypatch.setattr(ppt, "PPT_HOME", home)
        monkeypatch.setenv("SYNTHETIC_SECRET_ENV", MARKER)
        text, name = ppt.extract(_OLE2_MAGIC)
        seen = json.loads(text)
        assert name == "ppt"
        assert seen["argv"][1:-1] == [*ppt._JVM_OPTIONS, "-cp", f"{home}/lib/*", "PptText"]
        assert seen["argv"][-1].endswith(".ppt")
        assert seen["cpu"] == [ppt.CHILD_MAX_CPU_SECONDS, ppt.CHILD_MAX_CPU_SECONDS + 1]
        if sys.platform == "linux":
            assert seen["as"] == [ppt.CHILD_MAX_ADDRESS_SPACE_BYTES] * 2
        assert seen["env"]["MALLOC_ARENA_MAX"] == "2"
        assert seen["env"]["LC_ALL"] == "C.UTF-8"
        assert "SYNTHETIC_SECRET_ENV" not in seen["env"]

    def test_jvm_output_goes_to_stderr_and_writes_no_files(self):
        """JVM messages on stdout would be indexed as the deck's text, and
        a crash report or core file would copy it to disk."""
        from src.extractors import ppt

        for option in (
            "-Xlog:disable",
            "-XX:+DisplayVMOutputToStderr",
            "-XX:+ErrorFileToStderr",
            "-XX:-CreateCoredumpOnCrash",
            "-XX:-UsePerfData",
            "-Xmx128m",
        ):
            assert option in ppt._JVM_OPTIONS

    def test_missing_runtime_is_failed(self, tmp_path, monkeypatch):
        from src.extractors import ppt

        monkeypatch.setattr(ppt, "PPT_HOME", tmp_path / "absent")
        result = _ppt(_OLE2_MAGIC + bytes(64))
        assert (result.status, result.error) == (STATUS_FAILED, "ToolNotFoundError")

    @pytest.mark.parametrize(
        ("body", "error"),
        [
            ("time.sleep(60)", "ToolTimeoutError"),
            (
                f"sys.stderr.write({MARKER!r})\nsys.stdout.write({MARKER!r})\nsys.exit(1)",
                "ToolExitError",
            ),
            ("os.kill(os.getpid(), signal.SIGKILL)", "ToolCrashError"),
        ],
    )
    def test_tool_failures_are_fixed_failed_rows(self, tmp_path, monkeypatch, caplog, body, error):
        from src.extractors import ppt

        caplog.set_level("DEBUG")
        monkeypatch.setattr(ppt, "PPT_HOME", _fake_ppt_home(tmp_path, body))
        monkeypatch.setattr(ppt, "PPT_TIMEOUT_SECONDS", 0.5)
        started = time.monotonic()
        result = extract(
            content_type=_PPT_MIME,
            filename=f"{MARKER}.ppt",
            payload=_OLE2_MAGIC + MARKER.encode(),
        )
        assert (result.status, result.error, result.text) == (STATUS_FAILED, error, None)
        assert time.monotonic() - started < 20
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert error in warnings[0].getMessage()
        assert MARKER not in caplog.text

    def test_encrypted_status_is_a_fixed_unsupported_row(self, tmp_path, monkeypatch, caplog):
        """#983: the reserved status alone makes the row ``unsupported``;
        what the reader wrote to stdout or stderr is still withheld."""
        from src.extractors import ENCRYPTED_PPT_ERROR, STATUS_UNSUPPORTED, ppt

        caplog.set_level("DEBUG")
        body = (
            f"sys.stderr.write({MARKER!r})\nsys.stdout.write({MARKER!r})\n"
            f"sys.exit({ppt.ENCRYPTED_EXIT_STATUS})"
        )
        monkeypatch.setattr(ppt, "PPT_HOME", _fake_ppt_home(tmp_path, body))
        result = extract(
            content_type=_PPT_MIME,
            filename=f"{MARKER}.ppt",
            payload=_OLE2_MAGIC + MARKER.encode(),
        )
        assert (result.status, result.extractor, result.text, result.error) == (
            STATUS_UNSUPPORTED,
            "ppt@2",
            None,
            ENCRYPTED_PPT_ERROR,
        )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "recorded unsupported" in warnings[0].getMessage()
        assert MARKER not in caplog.text

    @pytest.mark.parametrize("returncode", [1, 2, 3, 9, 11, 255])
    def test_other_exit_statuses_stay_failed(self, tmp_path, monkeypatch, returncode):
        """Any status but the reserved one (an uncaught exception, a JVM
        that cannot start, a failed allocation) can be anything, so it
        stays ``failed`` and is retried."""
        from src.extractors import ppt

        assert returncode != ppt.ENCRYPTED_EXIT_STATUS
        monkeypatch.setattr(ppt, "PPT_HOME", _fake_ppt_home(tmp_path, f"sys.exit({returncode})"))
        result = _ppt(_OLE2_MAGIC)
        assert (result.status, result.error) == (STATUS_FAILED, "ToolExitError")

    def test_reader_and_extractor_agree_on_the_encrypted_status(self):
        """``PptText.java`` exits with the status ``ppt.py`` reads, for
        POI's encrypted-file exception by exact class only."""
        from src.extractors import ppt

        source = (Path(__file__).parents[1] / "java" / "PptText.java").read_text()
        declared = re.findall(r"static final int ENCRYPTED_EXIT_STATUS = (\d+);", source)
        assert declared == [str(ppt.ENCRYPTED_EXIT_STATUS)]
        assert "e.getClass() != EncryptedPowerPointFileException.class" in source
        # Not 1 (an uncaught exception or a JVM start-up failure) and not
        # 3 (the JVM's ExitOnOutOfMemoryError status).
        assert ppt.ENCRYPTED_EXIT_STATUS not in {0, 1, 2, 3}

    @pytest.mark.parametrize("returncode", [-11, -24, -6])
    def test_signal_statuses_are_crash_rows(self, tmp_path, monkeypatch, returncode):
        """SIGSEGV, SIGXCPU (the CPU limit) and SIGABRT, by the status the
        parent sees: stubbed, so nothing crashes for real on macOS."""

        from src.extractors import ppt

        monkeypatch.setattr(ppt, "PPT_HOME", _fake_ppt_home(tmp_path, "pass"))
        _stub_exit_status(monkeypatch, returncode)
        result = _ppt(_OLE2_MAGIC)
        assert (result.status, result.error) == (STATUS_FAILED, "ToolCrashError")

    def test_output_is_cut_at_the_byte_cap_and_java_killed(self, tmp_path, monkeypatch, caplog):
        """Read up to the cap, never whole: a reader writing 16 MiB is
        killed before it finishes, and the cut is reported once."""
        from src.extractors import ppt

        caplog.set_level("DEBUG")
        extractors.drain_extractor_counts()
        finished = tmp_path / "finished"
        home = _fake_ppt_home(
            tmp_path,
            f"sys.stdout.write({MARKER!r})\n"
            "for _ in range(256):\n    sys.stdout.write('b' * 65536)\n"
            "sys.stdout.flush()\n"
            f"open({str(finished)!r}, 'w').close()",
        )
        monkeypatch.setattr(ppt, "PPT_HOME", home)
        monkeypatch.setattr(ppt, "_MAX_OUTPUT_BYTES", 100_000)
        started = time.monotonic()
        text, _ = ppt.extract(_OLE2_MAGIC)
        assert len(text) == 100_000
        assert text.startswith(MARKER)
        assert not finished.exists()
        assert time.monotonic() - started < 20
        lines = [r for r in caplog.records if "extractor cap" in r.getMessage()]
        assert [r.levelno for r in lines] == [logging.WARNING]
        assert "extractor cap ppt_output_bytes:" in lines[0].getMessage()
        assert extractors.drain_extractor_counts()["extractor_caps"] == 1
        assert MARKER not in caplog.text

    @pytest.mark.parametrize(
        "payload", [b"PK\x03\x04" + bytes(64), MARKER.encode(), b""], ids=["zip", "text", "empty"]
    )
    def test_non_ole2_ppt_is_unsupported_without_running_java(self, monkeypatch, payload):
        from src.extractors import NON_OLE2_PPT_ERROR, STATUS_UNSUPPORTED, ppt

        def must_not_run(*_args, **_kwargs):
            raise AssertionError("the reader ran on a non-OLE2 payload")

        monkeypatch.setattr(ppt, "run_tool", must_not_run)
        for content_type, filename in ((_PPT_MIME, "a.bin"), ("application/octet-stream", "a.ppt")):
            result = extract(content_type=content_type, filename=filename, payload=payload)
            assert (result.status, result.extractor, result.error) == (
                STATUS_UNSUPPORTED,
                None,
                NON_OLE2_PPT_ERROR,
            )

    def test_a_ppt_occurrence_runs_ppt_and_never_an_ooxml_refresh(self, tmp_path, monkeypatch):
        """A ``.ppt`` occurrence runs the ``ppt`` extractor. Since #928 an
        OOXML row is never refreshed from it (rows are per module), so
        the dispatcher has no OOXML-to-legacy re-route to take."""
        from src.extractors import ppt

        monkeypatch.setattr(ppt, "PPT_HOME", _fake_ppt_home(tmp_path, "print('slide words')"))
        result = extract(
            content_type=_PPT_MIME,
            filename="deck.ppt",
            payload=_OLE2_MAGIC + bytes(64),
        )
        assert (result.status, result.extractor, result.text) == (
            STATUS_SUCCESS,
            "ppt@2",
            "slide words",
        )

    @pytest.mark.parametrize(
        ("content_type", "filename"),
        [
            (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "a.bin",
            ),
            ("application/octet-stream", "deck.pptx"),
        ],
    )
    def test_ole2_labelled_pptx_stays_unsupported(self, monkeypatch, content_type, filename):
        """``.ppt`` bytes labelled ``.pptx`` select the PPTX extractor,
        which cannot read OLE2: recorded ``unsupported`` without running
        either reader (#936, #957)."""
        from src.extractors import LEGACY_OLE2_ERROR, STATUS_UNSUPPORTED, ppt

        def must_not_run(*_args, **_kwargs):
            raise AssertionError("the .ppt reader ran on a .pptx occurrence")

        monkeypatch.setattr(ppt, "run_tool", must_not_run)
        result = extract(
            content_type=content_type, filename=filename, payload=_OLE2_MAGIC + bytes(64)
        )
        assert (result.status, result.extractor, result.error) == (
            STATUS_UNSUPPORTED,
            None,
            LEGACY_OLE2_ERROR,
        )

    @pytest.mark.parametrize("error", [MemoryError, RecursionError])
    def test_host_pressure_in_the_parent_propagates(self, tmp_path, monkeypatch, error):
        from src.extractors import ppt

        def raise_(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(ppt, "PPT_HOME", _fake_ppt_home(tmp_path, "pass"))
        monkeypatch.setattr(ppt, "run_tool", raise_)
        with pytest.raises(error):
            _ppt(_OLE2_MAGIC)


@pytest.mark.parametrize("module", ["doc", "ppt"])
@pytest.mark.parametrize("truncated", [True, False])
def test_cut_raw_tool_output_is_incomplete_text(tmp_path, monkeypatch, module, truncated):
    """#1291: a raw tool's output the byte cap cut is kept, and the
    attachment's text is reported incomplete (#1242); uncut output is
    complete."""
    from src.extractors import doc, ppt

    body = f"sys.stdout.write('SYNTHETIC text ' + 'a' * {200 if truncated else 10})"
    if module == "doc":
        tool = _fake_tool(tmp_path, body)
        monkeypatch.setattr(doc.shutil, "which", lambda _name: tool)
        monkeypatch.setattr(doc, "_MAX_OUTPUT_BYTES", 100)
        mime = "application/msword"
    else:
        monkeypatch.setattr(ppt, "PPT_HOME", _fake_ppt_home(tmp_path, body))
        monkeypatch.setattr(ppt, "_MAX_OUTPUT_BYTES", 100)
        mime = _PPT_MIME
    result = extract(content_type=mime, filename=f"a.{module}", payload=_OLE2_MAGIC)
    assert result.status == STATUS_SUCCESS
    assert len(result.text or "") == (100 if truncated else 25)
    assert result.text_complete is not truncated


# ---------------------------------------------------------------------------
# The raw tools' output byte cap follows the configured text cap (#1308)
# ---------------------------------------------------------------------------

_FOUR_BYTE_CHAR = "\U0001f600"


class TestRawOutputCap:
    """The ``doc`` and ``ppt`` output byte cap is four bytes per character
    of the configured ``max_extracted_chars`` (UTF-8's worst case), never
    above the measured ceiling, which also applies when the character cap
    is disabled (``None``)."""

    @pytest.mark.parametrize(
        ("max_chars", "ceiling", "expected"),
        [
            (None, 1000, 1000),
            (1, 1000, 4),
            (249, 1000, 996),
            (250, 1000, 1000),
            (251, 1000, 1000),
            (10**15, 1000, 1000),
            (2_000_000, 40 * 1024 * 1024, 8_000_000),
        ],
        ids=["disabled", "one", "below", "exact", "above", "oversized", "default"],
    )
    def test_cap_is_four_bytes_a_character_up_to_the_ceiling(self, max_chars, ceiling, expected):
        from src.extractors._runner import raw_output_cap

        assert raw_output_cap(max_chars, ceiling=ceiling) == expected

    def test_both_ceilings_are_the_measured_value(self):
        """40 MiB: the other extractors' 10,000,000-character text budget
        at four bytes a character, which the parent's measured peak (about
        five bytes per output byte, ``_runner.raw_output_cap``) keeps near
        200 MiB."""
        from src.extractors import doc, ppt

        assert doc._MAX_OUTPUT_BYTES == ppt._MAX_OUTPUT_BYTES == 40 * 1024 * 1024

    def test_only_doc_and_ppt_take_the_configured_cap(self):
        """The dispatcher passes ``max_extracted_chars`` to the raw-tool
        extractors only; every other extractor's signature is unchanged."""
        import importlib
        import inspect

        modules = ("doc", "docx", "html", "image", "pdf", "ppt", "pptx", "text", "xls", "xlsx")
        takes = {
            name
            for name in modules
            if "max_extracted_chars"
            in inspect.signature(
                importlib.import_module(f"src.extractors.{name}").extract
            ).parameters
        }
        assert takes == {"doc", "ppt"}


def _raw_tool(tmp_path: Path, monkeypatch, module: str, body: str):
    """Install ``body`` as ``module``'s tool and spy on its runs, each
    recorded as ``(max_output_bytes, bytes returned)``. Returns the MIME
    type and the runs."""
    from src.extractors import doc, ppt

    if module == "doc":
        tool = _fake_tool(tmp_path, body)
        monkeypatch.setattr(doc.shutil, "which", lambda _name: tool)
        mime = "application/msword"
    else:
        monkeypatch.setattr(ppt, "PPT_HOME", _fake_ppt_home(tmp_path, body))
        mime = _PPT_MIME
    target = doc if module == "doc" else ppt
    real = target.run_tool
    runs: list[tuple[int, int]] = []

    def spy(*args, **kwargs):
        output = real(*args, **kwargs)
        runs.append((kwargs["max_output_bytes"], len(output.data)))
        return output

    monkeypatch.setattr(target, "run_tool", spy)
    return mime, runs


def _cap_lines(caplog) -> list[str]:
    """The cap named by each extractor-cap line."""
    return [
        r.getMessage().split(":")[0]
        for r in caplog.records
        if r.name.startswith("indexer.extractor") and "extractor cap" in r.getMessage()
    ]


@pytest.mark.parametrize("module", ["doc", "ppt"])
class TestRawOutputCapThroughTheDispatcher:
    """End to end: the configured cap reaches the tool's run, the bytes
    read stop at it, and a cut is reported and marks the text incomplete
    (#1242)."""

    @pytest.fixture(autouse=True)
    def _fresh(self, caplog):
        caplog.set_level("DEBUG")
        extractors.drain_extractor_counts()

    @staticmethod
    def _extract(mime, module, max_chars):
        return extract(
            content_type=mime,
            filename=f"a.{module}",
            payload=_OLE2_MAGIC,
            max_extracted_chars=max_chars,
        )

    def test_four_byte_text_at_the_exact_cap_is_whole(self, tmp_path, monkeypatch, caplog, module):
        """``n`` four-byte characters are ``4n`` bytes: exactly the cap,
        read whole, nothing cut."""
        n = 50
        body = (_FOUR_BYTE_CHAR * n).encode()
        mime, runs = _raw_tool(tmp_path, monkeypatch, module, f"sys.stdout.buffer.write({body!r})")
        result = self._extract(mime, module, n)
        assert runs == [(4 * n, 4 * n)]
        assert result.status == STATUS_SUCCESS
        assert result.text == _FOUR_BYTE_CHAR * n
        assert result.text_complete is True
        assert _cap_lines(caplog) == []
        assert extractors.drain_extractor_counts()["extractor_caps"] == 0

    def test_four_byte_text_one_byte_past_the_cap_is_cut(
        self, tmp_path, monkeypatch, caplog, module
    ):
        """``4n + 1`` bytes: the output is cut at ``4n``, the ``n`` whole
        characters before it are kept, and the cut is one WARNING naming
        the module's cap."""
        n = 50
        body = (_FOUR_BYTE_CHAR * n + MARKER).encode()
        mime, runs = _raw_tool(tmp_path, monkeypatch, module, f"sys.stdout.buffer.write({body!r})")
        result = self._extract(mime, module, n)
        assert runs == [(4 * n, 4 * n)]
        assert result.status == STATUS_SUCCESS
        assert result.text == _FOUR_BYTE_CHAR * n
        assert result.text_complete is False
        assert _cap_lines(caplog) == [f"extractor cap {module}_output_bytes"]
        assert extractors.drain_extractor_counts()["extractor_caps"] == 1
        assert MARKER not in caplog.text

    def test_ascii_text_at_the_cap_is_read_whole_and_cut_by_characters(
        self, tmp_path, monkeypatch, caplog, module
    ):
        """``4n`` one-byte characters fit the byte cap; the dispatcher's
        character cap then keeps ``n`` of them."""
        n = 50
        mime, runs = _raw_tool(
            tmp_path,
            monkeypatch,
            module,
            f"sys.stdout.write({MARKER!r} + 'a' * {4 * n - len(MARKER)})",
        )
        result = self._extract(mime, module, n)
        assert runs == [(4 * n, 4 * n)]
        assert result.text == (MARKER + "a" * (4 * n))[:n]
        assert result.text_complete is False
        assert _cap_lines(caplog) == ["extractor cap extracted_chars"]
        assert MARKER not in caplog.text

    def test_ascii_text_past_the_cap_is_cut_by_bytes_then_characters(
        self, tmp_path, monkeypatch, caplog, module
    ):
        n = 50
        mime, runs = _raw_tool(
            tmp_path, monkeypatch, module, f"sys.stdout.write({MARKER!r} + 'a' * {4 * n})"
        )
        result = self._extract(mime, module, n)
        assert runs == [(4 * n, 4 * n)]
        assert result.text == (MARKER + "a" * (4 * n))[:n]
        assert result.text_complete is False
        assert _cap_lines(caplog) == [
            f"extractor cap {module}_output_bytes",
            "extractor cap extracted_chars",
        ]
        assert MARKER not in caplog.text

    @pytest.mark.parametrize("max_chars", [None, 10**12], ids=["disabled", "oversized"])
    def test_disabled_or_oversized_setting_reads_up_to_the_ceiling(
        self, tmp_path, monkeypatch, caplog, module, max_chars
    ):
        """No character cap, or one past the ceiling: the ceiling bounds
        the bytes read, and a tool writing 16 MiB is killed at it."""
        from src.extractors import doc, ppt

        monkeypatch.setattr(doc if module == "doc" else ppt, "_MAX_OUTPUT_BYTES", 1000)
        finished = tmp_path / "finished"
        mime, runs = _raw_tool(
            tmp_path,
            monkeypatch,
            module,
            f"sys.stdout.write({MARKER!r})\n"
            "for _ in range(256):\n    sys.stdout.write('b' * 65536)\n"
            "sys.stdout.flush()\n"
            f"open({str(finished)!r}, 'w').close()",
        )
        result = self._extract(mime, module, max_chars)
        assert runs == [(1000, 1000)]
        assert not finished.exists()
        assert len(result.text or "") == 1000
        assert result.text_complete is False
        assert _cap_lines(caplog) == [f"extractor cap {module}_output_bytes"]
        assert extractors.drain_extractor_counts()["extractor_caps"] == 1
        assert MARKER not in caplog.text


def test_raw_tool_versions_are_bumped_for_the_derived_cap():
    """#1308: the same bytes can now yield different text, so rows the
    previous versions wrote are re-extracted once."""
    from src.extractors import EXTRACTOR_VERSIONS, is_stale_extractor

    assert (EXTRACTOR_VERSIONS["doc"], EXTRACTOR_VERSIONS["ppt"]) == (2, 2)
    assert is_stale_extractor("doc@1")
    assert is_stale_extractor("ppt@1")
    assert not is_stale_extractor("doc@2")
    assert not is_stale_extractor("ppt@2")
