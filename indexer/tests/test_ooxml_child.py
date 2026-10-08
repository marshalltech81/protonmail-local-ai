"""OOXML extraction in a child process (#1040).

The DOCX, PPTX and XLSX pre-open budgets trust the member sizes the ZIP
central directory declares, and ``zipfile`` decompresses a member's
whole stream before it cuts the result to the declared size. So the
whole extraction runs in a child process under an address-space and a
CPU limit (``src/extractors/ooxml.py``), and its result crosses the
process boundary in the runner's framed protocol (``_runner``).

Tests marked ``real_extractor_child`` start the real child; every other
test in the suite runs it in process (``tests/conftest.py``). Payloads
are synthetic.
"""

from __future__ import annotations

import io
import json
import logging
import resource
import struct
import subprocess
import sys
import time
import zipfile
import zlib
from pathlib import Path

import pytest
from src import extractors
from src.extractors import (
    DOCX_PACKAGE_BUDGET_ERROR,
    PPTX_PACKAGE_BUDGET_ERROR,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
    XLSX_EAGER_BUDGET_ERROR,
    _runner,
    extract,
    extractor_child,
    ooxml,
    over_package_budget,
)
from src.extractors._runner import (
    ToolExitError,
    run_tool,
)

from tests.test_extractors import (
    _boxes,
    _chained_docx,
    _deck,
    _docx_bytes,
    _with_members,
    _xlsx_bytes,
)

MARKER = "SYNTHETIC_OOXML_MARKER"
MiB = 1024 * 1024

_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
# A member each format's library reads whole when it opens the package
# (openpyxl streams a worksheet in small reads, but reads the
# stylesheet whole).
_MAIN_MEMBER = {
    "docx": "word/document.xml",
    "pptx": "ppt/slides/slide1.xml",
    "xlsx": "xl/styles.xml",
}
_FORMATS = tuple(_MIME)

real_child = pytest.mark.real_extractor_child
linux_only = pytest.mark.skipif(
    sys.platform != "linux",
    reason=(
        "real resource limits and crashing signals are asserted on Linux only (the image and "
        "CI): macOS does not enforce RLIMIT_AS and writes a crash report for each crash"
    ),
)


def _payload(module: str, text: str = MARKER) -> bytes:
    if module == "docx":
        return _docx_bytes(text)
    if module == "pptx":
        return _deck(_boxes(text))
    return _xlsx_bytes([[text]])


def _extract(module: str, payload: bytes):
    return extract(content_type=_MIME[module], filename=f"a.{module}", payload=payload)


def _module(module: str):
    return getattr(__import__("src.extractors", fromlist=[module]), module)


def _peak_rss_bytes(who: int) -> int:
    peak = resource.getrusage(who).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def _understated(payload: bytes, name: str, padding: int) -> bytes:
    """``payload`` with member ``name``'s stream carrying ``padding``
    spaces after its data, while its local header and central directory
    entry still declare the data's own size and CRC: the shape of #1040."""
    with zipfile.ZipFile(io.BytesIO(payload)) as source:
        members = [(info, source.read(info.filename)) for info in source.infolist()]
    out = io.BytesIO()
    real = b""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for info, data in members:
            if info.filename == name:
                real = data
                data += b" " * padding
            archive.writestr(info.filename, data, compresslevel=9)
    buf = bytearray(out.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(buf))) as archive:
        offset = archive.getinfo(name).header_offset
        position = archive.start_dir
    # Local header: CRC at +14, uncompressed size at +22; central
    # directory entry: CRC at +16, uncompressed size at +24.
    struct.pack_into("<I", buf, offset + 14, zlib.crc32(real))
    struct.pack_into("<I", buf, offset + 22, len(real))
    while buf[position : position + 4] == b"PK\x01\x02":
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", buf, position + 28)
        if bytes(buf[position + 46 : position + 46 + name_len]).decode() == name:
            struct.pack_into("<I", buf, position + 16, zlib.crc32(real))
            struct.pack_into("<I", buf, position + 24, len(real))
        position += 46 + name_len + extra_len + comment_len
    return bytes(buf)


# Run by ``test_the_expansion_happens_in_the_child`` as a fresh process:
# one extraction with room in the child's address space, reporting its
# own peak memory and its children's.
_FRESH_PROCESS = """
import json, resource, sys
sys.path.insert(0, ".")
import importlib
from src.extractors import _runner, extract
module, path, mime = sys.argv[1:]
importlib.import_module("src.extractors." + module).CHILD_MAX_ADDRESS_SPACE_BYTES = 4 << 30
calls = []
real = _runner.run_tool
def spy(*args, **kwargs):
    calls.append(1)
    return real(*args, **kwargs)
_runner.run_tool = spy
result = extract(content_type=mime, filename="a." + module, payload=open(path, "rb").read())
if sys.platform == "darwin":
    scale = 1
    self_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
else:
    # Linux carries ru_maxrss across execve, so a process forked from a
    # large pytest would report pytest's size: read this image's own peak.
    scale = 1024
    with open("/proc/self/status") as status:
        line = next(line for line in status if line.startswith("VmHWM:"))
    self_peak = int(line.split()[1]) * 1024
print(json.dumps({
    "status": result.status,
    "marker": "SYNTHETIC_OOXML_MARKER" in (result.text or ""),
    "run_tool_calls": len(calls),
    "self_peak": self_peak,
    "children_peak": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale,
}))
"""


@pytest.fixture(scope="module")
def understated_512m():
    """Per format: a package whose main member declares its own few KB
    but whose stream decompresses to 512 MiB more."""
    return {
        module: _understated(_payload(module), _MAIN_MEMBER[module], 512 * MiB)
        for module in _FORMATS
    }


@pytest.fixture
def run_tool_calls(monkeypatch):
    """Record each ``run_tool`` call the OOXML extractors make, and run
    the real one."""
    calls: list[tuple[list[str], dict[str, object]]] = []

    def spy(argv, payload, *, on_output, **kwargs):
        calls.append((argv, kwargs))
        return run_tool(argv, payload, on_output=on_output, **kwargs)

    monkeypatch.setattr(_runner, "run_tool", spy)
    return calls


# ---------------------------------------------------------------------------
# The crafted member is expanded in the child, not in the indexer
# ---------------------------------------------------------------------------


class TestUnderstatedMember:
    @pytest.mark.parametrize("module", _FORMATS)
    def test_the_shape_passes_every_budget_that_reads_the_central_directory(
        self, module, understated_512m
    ):
        """The member declares its own size, so the dispatcher's ZIP guard
        and the pre-open budgets pass it; a read returns only the declared
        bytes, with a matching CRC."""
        payload = understated_512m[module]
        assert len(payload) < 2 * MiB
        assert extractors._validate_zip_payload(payload) is None
        assert not over_package_budget(
            payload,
            max_members=5_000,
            max_expansion_bytes=32 * MiB,
            max_rels_bytes=4 * MiB,
            max_declared_bytes=48 * MiB,
        )
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            assert len(archive.read(_MAIN_MEMBER[module])) < 64 * 1024

    @real_child
    @pytest.mark.parametrize("module", _FORMATS)
    def test_the_expansion_happens_in_the_child(self, module, understated_512m, tmp_path):
        """With room for it, the child expands the member and extracts the
        text, and the indexer's own memory does not grow by it. Run in a
        fresh process, so its peak and its children's peak are this
        extraction's alone. (macOS does not enforce the address-space
        limit; the next test asserts it on Linux.)"""
        payload = tmp_path / f"payload.{module}"
        payload.write_bytes(understated_512m[module])
        completed = subprocess.run(
            [sys.executable, "-c", _FRESH_PROCESS, module, str(payload), _MIME[module]],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            check=True,
            timeout=120,
        )
        seen = json.loads(completed.stdout)
        assert seen["status"] == STATUS_SUCCESS
        assert seen["marker"] is True
        assert seen["run_tool_calls"] == 1
        # The work happened: the child held the 512 MiB ...
        assert seen["children_peak"] > 512 * MiB
        # ... and the indexer did not (on main it peaked over 1 GiB).
        assert seen["self_peak"] < 384 * MiB

    @real_child
    @linux_only
    @pytest.mark.parametrize("module", _FORMATS)
    def test_the_address_space_limit_fails_it(self, module, understated_512m, caplog):
        """Under the format's own limit the child runs out of address
        space: a ``failed`` row by type, with a WARNING, and the indexer's
        memory does not grow."""
        caplog.set_level("DEBUG")
        before = _peak_rss_bytes(resource.RUSAGE_SELF)
        started = time.monotonic()
        result = _extract(module, understated_512m[module])
        assert time.monotonic() - started < 30
        assert (result.status, result.error) == (STATUS_FAILED, "MemoryError")
        assert _peak_rss_bytes(resource.RUSAGE_SELF) - before < 64 * MiB
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert [r.getMessage() for r in warnings] == [
            f"extractor {module} failed (dispatch_via=mime): MemoryError"
        ]
        assert MARKER not in caplog.text


# ---------------------------------------------------------------------------
# The real child
# ---------------------------------------------------------------------------


@real_child
class TestRealChild:
    @pytest.mark.parametrize("module", _FORMATS)
    def test_text_crosses_the_boundary_unchanged(self, module, run_tool_calls):
        """The real child returns what the in-process run returns, through
        the launcher, with the format's limits."""
        payload = _payload(module, f"{MARKER} Café crème, Zürich")
        expected = extractor_child.run(module, payload)
        result = _extract(module, payload)
        assert result.status == STATUS_SUCCESS
        assert result.text == expected.split(b"\n", 1)[1].decode().strip()
        assert "Café crème" in result.text
        mod = _module(module)
        assert run_tool_calls == [
            (
                [
                    sys.executable,
                    "-I",
                    str(Path(ooxml.__file__).with_name("extractor_child.py")),
                    module,
                ],
                {
                    "timeout_seconds": mod.CHILD_TIMEOUT_SECONDS,
                    "max_output_bytes": ooxml._MAX_OUTPUT_BYTES,
                    "max_address_space_bytes": mod.CHILD_MAX_ADDRESS_SPACE_BYTES,
                    "max_cpu_seconds": mod.CHILD_MAX_CPU_SECONDS,
                    "suffix": f".{module}",
                },
            )
        ]

    def test_a_cap_in_the_child_is_logged_and_counted_in_the_parent(self, caplog):
        """600,000 empty paragraphs stop at the real block budget in the
        child; the parent logs the cap once and counts it."""
        from tests.test_docx_walk import _p, docx_payload

        extractors.drain_extractor_counts()
        caplog.set_level("DEBUG")
        payload = docx_payload("<w:p/>" * 600_000 + _p(MARKER))
        result = _extract("docx", payload)
        assert result.status == "empty"
        lines = [r for r in caplog.records if "extractor cap" in r.getMessage()]
        assert [(r.levelno, r.getMessage()) for r in lines] == [
            (logging.WARNING, "extractor cap docx_blocks: document truncated at a walk budget")
        ]
        assert extractors.drain_extractor_counts()["extractor_caps"] == 1
        assert MARKER not in caplog.text

    @pytest.mark.parametrize(
        ("module", "members", "error"),
        [
            # More members than each pre-open budget allows.
            ("docx", 5_001, DOCX_PACKAGE_BUDGET_ERROR),
            ("pptx", 20_001, PPTX_PACKAGE_BUDGET_ERROR),
        ],
    )
    def test_a_package_budget_is_unsupported_across_the_boundary(self, module, members, error):
        payload = _with_members(
            _payload(module), [(f"extra/{i}.bin", b"", zipfile.ZIP_STORED) for i in range(members)]
        )
        result = _extract(module, payload)
        assert (result.status, result.error) == (STATUS_UNSUPPORTED, error)
        assert result.extractor == f"{module}@{extractors.EXTRACTOR_VERSIONS[module]}"

    def test_the_eager_part_budget_is_unsupported_across_the_boundary(self):
        payload = _with_members(
            _xlsx_bytes([[MARKER]]),
            [("docProps/custom.xml", b" " * (9 * MiB), zipfile.ZIP_DEFLATED)],
        )
        result = _extract("xlsx", payload)
        assert (result.status, result.error) == (STATUS_UNSUPPORTED, XLSX_EAGER_BUDGET_ERROR)

    def test_a_relationship_chain_keeps_its_type_across_the_boundary(self):
        result = _extract("docx", _chained_docx(2000))
        assert (result.status, result.error) == (STATUS_FAILED, "DocxRelationshipChainError")

    @pytest.mark.parametrize("module", _FORMATS)
    def test_a_library_error_keeps_its_type_and_withholds_its_text(self, module, caplog):
        caplog.set_level("DEBUG")
        payload = b"PK\x03\x04" + MARKER.encode() + bytes(64)
        result = _extract(module, payload)
        assert (result.status, result.error) == (STATUS_FAILED, "BadZipFile")
        assert MARKER not in caplog.text

    def test_the_wall_clock_timeout_kills_the_child(self, monkeypatch):
        from src.extractors import docx

        monkeypatch.setattr(docx, "CHILD_TIMEOUT_SECONDS", 0.01)
        result = _extract("docx", _payload("docx"))
        assert (result.status, result.error) == (STATUS_FAILED, "ToolTimeoutError")

    @linux_only
    def test_the_cpu_limit_kills_the_child(self, monkeypatch):
        """SIGXCPU, real only on Linux; 119 rows of 60,000 duplicate cells
        take seconds of CPU before the node budget stops them."""
        from src.extractors import xlsx

        rows = "".join(f'<row r="{r}">' + '<c r="A1"/>' * 60_000 + "</row>" for r in range(2, 121))
        payload = _xlsx_bytes([[MARKER]])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            parts = {i.filename: archive.read(i.filename) for i in archive.infolist()}
        sheet = parts["xl/worksheets/sheet1.xml"]
        parts["xl/worksheets/sheet1.xml"] = sheet.replace(
            b"</sheetData>", rows.encode() + b"</sheetData>"
        )
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in parts.items():
                archive.writestr(name, data)
        monkeypatch.setattr(xlsx, "CHILD_MAX_CPU_SECONDS", 1)
        started = time.monotonic()
        result = _extract("xlsx", out.getvalue())
        assert (result.status, result.error) == (STATUS_FAILED, "ToolCrashError")
        assert time.monotonic() - started < 15

    def test_an_unknown_format_is_refused_by_the_child(self):
        child = str(Path(ooxml.__file__).with_name("extractor_child.py"))
        with pytest.raises(ToolExitError) as raised:
            run_tool(
                [sys.executable, "-I", child, "pdf"],
                b"%PDF-1.7",
                timeout_seconds=30,
                max_output_bytes=1024,
                max_address_space_bytes=1024 * MiB,
                max_cpu_seconds=30,
                suffix=".pdf",
            )
        assert raised.value.returncode == 2
