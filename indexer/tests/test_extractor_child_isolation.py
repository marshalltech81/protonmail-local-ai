"""Direct child protocol tests must not inherit another test's counters (#1673)."""

import subprocess
import sys
from pathlib import Path


def test_parent_degradation_cannot_leak_between_child_tests(tmp_path):
    case = tmp_path / "test_ordered_child.py"
    case.write_text(
        """
from src import extractors
from src.extractors import extractor_child, xls_child

def test_parent_leaves_a_parser_cap():
    extractors.note_parser_caps_message()

def test_child_protocol_starts_clean(monkeypatch):
    def boom(*args, **kwargs):
        raise MemoryError("SYNTHETIC_PRIVATE_CHILD_MARKER")
    monkeypatch.setattr(xls_child, "extract_text", boom)
    assert extractor_child.run("xls", b"x") == b"E MemoryError\\n"
""",
        encoding="utf-8",
    )
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tests.conftest",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            str(case),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
