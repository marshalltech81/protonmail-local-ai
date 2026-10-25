"""The supervised-sync driver (#1759, procedure v5 section 2 and 1.8)."""

import os
import stat
import sys
import time
from pathlib import Path

from evidence_run import runner, syncdriver


def _stub(tmp_path: Path, mode: str, *, pidfile: Path | None = None) -> Path:
    path = tmp_path / f"stub-{mode}"
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        f"mode = {mode!r}\n"
        f"pidfile = {str(pidfile) if pidfile else None!r}\n"
        "if pidfile:\n"
        "    open(pidfile, 'w').write(str(os.getpid()))\n"
        "if mode == 'ok':\n"
        "    print('synthetic isync output')\n"
        "elif mode == 'fail':\n"
        "    print('synthetic failure'); sys.exit(3)\n"
        "elif mode == 'sleep':\n"
        "    time.sleep(60)\n"
        "elif mode == 'flood':\n"
        "    chunk = b'x' * 65536\n"
        "    for _ in range(64):\n"
        "        sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()\n"
        "    time.sleep(60)\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_clean_run_reports_ok_and_the_byte_count_never_the_output(tmp_path):
    result = syncdriver.run_bounded([str(_stub(tmp_path, "ok"))])
    assert result.token == "ok" and result.returncode == 0
    assert result.output_bytes == len(b"synthetic isync output\n")
    assert not hasattr(result, "output")
    assert "synthetic isync output" not in repr(result)


def test_a_failing_run_is_a_nonzero_exit_token(tmp_path):
    result = syncdriver.run_bounded([str(_stub(tmp_path, "fail"))])
    assert (result.token, result.returncode) == ("exit_nonzero", 3)


def test_a_run_past_its_timeout_is_killed(tmp_path):
    pidfile = tmp_path / "pid"
    started = time.monotonic()
    result = syncdriver.run_bounded([str(_stub(tmp_path, "sleep", pidfile=pidfile))], timeout=1.0)
    assert result.token == "timeout"
    assert time.monotonic() - started < 15
    time.sleep(0.3)
    assert not _alive(int(pidfile.read_text()))


def test_output_past_the_cap_kills_the_run(tmp_path):
    pidfile = tmp_path / "pid"
    cap = 1 << 20
    result = syncdriver.run_bounded(
        [str(_stub(tmp_path, "flood", pidfile=pidfile))], timeout=30, output_cap=cap
    )
    assert result.token == "output_cap"
    assert cap < result.output_bytes <= cap + (1 << 17)  # bounded work: at most one more read
    time.sleep(0.3)
    assert not _alive(int(pidfile.read_text()))


def test_the_production_bounds_are_the_procedures():
    assert (syncdriver.SYNC_TIMEOUT_SECONDS, syncdriver.OUTPUT_CAP_BYTES) == (600.0, 1 << 20)


def test_a_missing_binary_is_a_spawn_failure_token(tmp_path):
    result = syncdriver.run_bounded([str(tmp_path / "does-not-exist")])
    assert result.token == "spawn_failed" and result.returncode is None


def test_the_supervised_run_names_the_config_and_channel_and_counts_a_command(tmp_path):
    args = tmp_path / "args"
    stub = tmp_path / "mbsync-stub"
    stub.write_text(
        f"#!{sys.executable}\nimport sys\nopen({str(args)!r}, 'w').write(' '.join(sys.argv[1:]))\n"
    )
    stub.chmod(0o700)
    budget = runner.Budget()
    sync = syncdriver.SupervisedSync(
        tmp_path / "isyncrc", "protonmail", mbsync=str(stub), budget=budget
    )
    assert sync.sync_once().token == "ok"
    assert args.read_text() == f"-c {tmp_path / 'isyncrc'} protonmail"
    assert budget.commands == 1
