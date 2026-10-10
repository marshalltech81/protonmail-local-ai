"""The extractor child's framed protocol (#1291).

Every extractor that runs in the extractor child (the OOXML formats,
``xls`` and ``image``) reports its result through the same frames, parsed by
``_runner.run_child`` (``src/extractors/_runner.py`` documents them).
Output that breaks the protocol, or that the runner cut at its byte
cap, is a ``failed`` row and never text; progress frames reach the
dispatcher's ``on_progress`` while the child runs. Here the runner is
stubbed, except where a test starts a real process. Payloads are
synthetic.
"""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

import pytest
from src import extractors
from src.extractors import (
    DOCX_PACKAGE_BUDGET_ERROR,
    OOXML_MODULES,
    PPTX_PACKAGE_BUDGET_ERROR,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
    XLSX_EAGER_BUDGET_ERROR,
    _runner,
    extract,
    extractor_child,
)
from src.extractors._runner import (
    ChildOutputError,
    ToolCrashError,
    ToolExitError,
    ToolOutput,
    ToolTimeoutError,
    run_tool,
)

from tests.conftest import make_ole2
from tests.test_ooxml_child import _payload as _ooxml_payload

MARKER = "SYNTHETIC_CHILD_MARKER"

_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "image": "image/png",
}
# Every module the dispatcher runs in the extractor child.
_MODULES = tuple(_MIME)
# The modules whose caps are all logged as extractor caps (the image
# extractor's frame caps are logged and counted as OCR-capped images,
# ``tests/test_image_child.py``).
_EXTRACTOR_CAP_MODULES = tuple(m for m in _MODULES if m != "image")
# The options each module's extractor passes the child.
_OPTIONS = {"image": ["20", "0", shutil.which("tesseract") or "tesseract"]}


def _png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


def _payload(module: str) -> bytes:
    if module == "image":
        return _png()
    # A container the dispatcher's identification (#1416) names as a
    # workbook, so the ``xls`` child is the one that runs.
    return make_ole2("Workbook") if module == "xls" else _ooxml_payload(module)


def _extract(module: str, **kwargs):
    return extract(
        content_type=_MIME[module], filename=f"a.{module}", payload=_payload(module), **kwargs
    )


def _module(module: str):
    return getattr(__import__("src.extractors", fromlist=[module]), module)


def _cap_names(module: str) -> list[str]:
    mod = _module(module)
    names = getattr(mod, "_CAP_NAMES", None) or getattr(mod, "_CAPS", None)
    return sorted(names or mod._CAP_MESSAGES)


def stub_child_output(monkeypatch, data: bytes, *, truncated: bool = False) -> list[dict]:
    """Make the runner hand ``data`` to the protocol parser as the
    child's stdout; returns the arguments of each run."""
    calls: list[dict] = []
    real = _runner.run_tool

    def fake(argv, payload, *, on_output, **kwargs):
        # Container identification (#1416) runs before the extractor's
        # child; it is left to run, and only the extractor's run is stubbed.
        child = str(_runner._CHILD)
        if child in argv and argv[argv.index(child) + 1] == "container":
            return real(argv, payload, on_output=on_output, **kwargs)
        calls.append({"argv": argv, **kwargs})
        on_output(data)
        return ToolOutput(b"", truncated=truncated)

    monkeypatch.setattr(_runner, "run_tool", fake)
    return calls


def _fake_tool(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake_child"
    path.write_text(f"#!{sys.executable}\nimport os, sys, time\n{body}\n")
    path.chmod(0o700)
    return str(path)


_LIMITS = {"max_address_space_bytes": 1024 * 1024 * 1024, "max_cpu_seconds": 60}


class TestMalformedOutput:
    @pytest.mark.parametrize("module", _MODULES)
    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(b"", id="empty"),
            pytest.param(b"T 4", id="no-newline"),
            pytest.param(b"\n" + MARKER.encode(), id="old-header-format"),
            pytest.param(b"T 9\nshort", id="text-shorter-than-its-length"),
            pytest.param(b"T 2\nlonger", id="text-longer-than-its-length"),
            pytest.param(b"T\n", id="text-without-length"),
            pytest.param(b"T -1\n", id="negative-length"),
            pytest.param(b"T 1e3\n", id="length-not-digits"),
            pytest.param(b"T 4\r\ntext", id="length-with-carriage-return"),
            pytest.param(b"T 2\n\xff\xfe", id="text-not-utf8"),
            pytest.param(b"C unknown_cap\nT 0\n", id="unknown-cap"),
            pytest.param(b"C docx_blocks\nC xls_sheets\nT 0\n", id="other-format-cap"),
            pytest.param(b"N pages 3\nT 0\n", id="count-not-allowed"),
            pytest.param(b"P now\nT 0\n", id="progress-with-a-value"),
            pytest.param(b"X\nT 0\n", id="unknown-frame"),
            pytest.param(b"P\nP\n", id="no-result-frame"),
            pytest.param(b"E ValueError\nT 0\n", id="frame-after-error"),
            pytest.param(b"E ValueError\ntext", id="text-after-error"),
            pytest.param(b"E \n", id="empty-type"),
            pytest.param(b"E 1Error\n", id="type-not-identifier"),
            pytest.param(b"E Value Error\n", id="type-with-space"),
            pytest.param(b"E " + b"E" * 101 + b"\n", id="type-too-long"),
            pytest.param("E Érreur\n".encode(), id="type-not-ascii"),
            pytest.param(b"P" * 200, id="line-over-the-frame-limit"),
            pytest.param(b"C " + b"x" * 200 + b"\nT 0\n", id="long-line-with-newline"),
        ],
    )
    def test_malformed_output_is_failed_not_cached_as_text(self, module, data, monkeypatch):
        stub_child_output(monkeypatch, data)
        result = _extract(module)
        assert (result.status, result.error, result.text) == (
            STATUS_FAILED,
            "ChildOutputError",
            None,
        )
        assert result.text_complete is False

    @pytest.mark.parametrize("module", _MODULES)
    def test_truncated_output_is_failed_not_cached_as_text(self, module, monkeypatch):
        """Frames the runner cut at its cap are rejected, even when what
        was read is a whole, well-formed result."""
        body = f"{MARKER} text".encode()
        stub_child_output(monkeypatch, b"T %d\n" % len(body) + body, truncated=True)
        result = _extract(module)
        assert (result.status, result.error, result.text) == (
            STATUS_FAILED,
            "ChildOutputError",
            None,
        )

    def test_a_bad_frame_ends_the_parse_as_it_arrives(self):
        """The parser raises on the first bad frame rather than reading on,
        which ends the run (the runner kills the child)."""
        frames = _runner._Frames(frozenset(), frozenset(), None)
        with pytest.raises(ChildOutputError):
            frames.feed(b"X\n")

    def test_bytes_after_an_error_frame_in_a_later_read_are_refused(self):
        frames = _runner._Frames(frozenset(), frozenset(), None)
        frames.feed(b"E ValueError\n")
        with pytest.raises(ChildOutputError):
            frames.feed(b"T 0\n")

    def test_frames_split_across_reads_are_parsed(self):
        seen: list[int] = []
        frames = _runner._Frames(frozenset({"a"}), frozenset({"n"}), lambda: seen.append(1))
        for byte in b"P\nC a\nN n 12\nT 5\nZ\xc3\xbcri":
            frames.feed(bytes([byte]))
        assert frames.result() == _runner.ChildResult("Züri", ["a"], {"n": 12})
        assert seen == [1]


class TestFrames:
    @pytest.mark.parametrize(
        ("module", "cap"), [(m, c) for m in _EXTRACTOR_CAP_MODULES for c in _cap_names(m)]
    )
    def test_each_cap_name_is_accepted_logged_and_counted(self, module, cap, monkeypatch, caplog):
        extractors.drain_extractor_counts()
        caplog.set_level("DEBUG")
        body = f"{MARKER} text".encode()
        stub_child_output(
            monkeypatch, f"C {cap}\nC {cap}\n".encode() + b"T %d\n" % len(body) + body
        )
        result = _extract(module)
        assert result.status == STATUS_SUCCESS
        assert result.text == f"{MARKER} text"
        assert result.text_complete is False
        lines = [r for r in caplog.records if "extractor cap" in r.getMessage()]
        # Reported twice, logged and counted once.
        assert [r.levelno for r in lines] == [logging.WARNING]
        assert lines[0].getMessage().startswith(f"extractor cap {cap}: ")
        assert extractors.drain_extractor_counts()["extractor_caps"] == 1
        assert MARKER not in caplog.text

    @pytest.mark.parametrize("module", _MODULES)
    def test_progress_frames_reach_the_dispatchers_callback(self, module, monkeypatch):
        seen: list[int] = []
        stub_child_output(monkeypatch, b"P\nP\nP\nT 4\ntext")
        result = _extract(module, on_progress=lambda: seen.append(1))
        assert (result.status, result.text, result.text_complete) == (STATUS_SUCCESS, "text", True)
        assert len(seen) == 3

    def test_counts_are_returned_when_allowed(self, monkeypatch):
        stub_child_output(monkeypatch, b"N pages 7\nN pages 9\nT 0\n")
        result = _runner.run_child(
            "docx",
            b"x",
            max_address_space_bytes=1,
            max_cpu_seconds=1,
            timeout_seconds=1,
            max_output_bytes=1024,
            caps=frozenset(),
            counts=frozenset({"pages"}),
        )
        assert result == _runner.ChildResult("", [], {"pages": 9})

    @pytest.mark.parametrize(
        ("module", "type_name", "error"),
        [
            ("docx", "DocxPackageBudgetError", DOCX_PACKAGE_BUDGET_ERROR),
            ("pptx", "PptxPackageBudgetError", PPTX_PACKAGE_BUDGET_ERROR),
            ("xlsx", "XlsxEagerPartBudgetError", XLSX_EAGER_BUDGET_ERROR),
        ],
    )
    def test_a_permanent_rejection_is_unsupported(self, module, type_name, error, monkeypatch):
        stub_child_output(monkeypatch, f"E {type_name}\n".encode())
        result = _extract(module)
        assert (result.status, result.error) == (STATUS_UNSUPPORTED, error)

    @pytest.mark.parametrize(
        ("module", "type_name"),
        [
            # Another format's permanent rejection is only a type name here.
            ("docx", "PptxPackageBudgetError"),
            ("pptx", "XlsxEagerPartBudgetError"),
            ("xlsx", "DocxPackageBudgetError"),
            ("xls", "XlsxEagerPartBudgetError"),
            ("docx", "MemoryError"),
            ("xlsx", "RecursionError"),
            ("xls", "CompDocError"),
            ("xls", "MemoryError"),
            ("image", "DecompressionBombError"),
            ("image", "TesseractError"),
            ("image", "MemoryError"),
        ],
    )
    def test_any_other_type_is_failed_by_that_name(self, module, type_name, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        stub_child_output(monkeypatch, f"E {type_name}\n".encode())
        result = _extract(module)
        assert (result.status, result.error) == (STATUS_FAILED, type_name)
        via = "mime-image" if module == "image" else "mime"
        assert f"extractor {module} failed (dispatch_via={via}): {type_name}" in caplog.text

    @pytest.mark.parametrize("module", _MODULES)
    @pytest.mark.parametrize("error", [ToolTimeoutError, ToolCrashError, ToolExitError])
    def test_runner_failures_are_failed_rows_rate_limited(self, module, error, monkeypatch, caplog):
        """A timeout, a signal (a crash or the CPU limit) and a non-zero
        exit (the address-space limit when the child cannot report it):
        ``failed`` by type, each a WARNING within the rate limit, the rest
        counted as suppressed."""

        def fail(*_a, **_k):
            raise error

        monkeypatch.setattr(_runner, "run_tool", fail)
        extractors.drain_extractor_counts()
        caplog.set_level("DEBUG")
        attempts = extractors._WARNINGS_PER_WINDOW + 5
        for _ in range(attempts):
            result = _extract(module)
            assert (result.status, result.error) == (STATUS_FAILED, error.__name__)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == extractors._WARNINGS_PER_WINDOW
        assert all(error.__name__ in r.getMessage() for r in warnings)
        assert extractors.drain_extractor_counts()["warnings_suppressed"] == 5
        assert MARKER not in caplog.text

    @pytest.mark.parametrize("module", _MODULES)
    def test_each_module_runs_the_child_with_its_own_limits(self, module, monkeypatch):
        calls = stub_child_output(monkeypatch, b"T 0\n")
        _extract(module)
        mod = _module(module)
        if module == "image":
            output_cap = mod._MAX_OUTPUT_BYTES
            timeout = mod.child_timeout_seconds(20, None)
            cpu = mod.child_cpu_seconds(None)
        else:
            output_cap = (
                mod._MAX_OUTPUT_BYTES if module == "xls" else _module("ooxml")._MAX_OUTPUT_BYTES
            )
            timeout = mod.XLS_TIMEOUT_SECONDS if module == "xls" else mod.CHILD_TIMEOUT_SECONDS
            cpu = mod.CHILD_MAX_CPU_SECONDS
        child = [sys.executable, "-I", str(_runner._CHILD), module, *_OPTIONS.get(module, [])]
        assert calls == [
            {
                "argv": child,
                "timeout_seconds": timeout,
                "max_output_bytes": output_cap,
                "max_address_space_bytes": mod.CHILD_MAX_ADDRESS_SPACE_BYTES,
                "max_cpu_seconds": cpu,
                "suffix": f".{module}",
            }
        ]


class TestRealProcess:
    def test_progress_reaches_the_callback_while_the_child_runs(self, tmp_path):
        """The child waits for the file the callback writes before it
        sends its text: the frame was read and passed on while the child
        was still running, not at its exit."""
        flag = tmp_path / "progress-seen"
        tool = _fake_tool(
            tmp_path,
            "sys.stdout.buffer.write(b'P\\n')\n"
            "sys.stdout.buffer.flush()\n"
            "deadline = time.monotonic() + 20\n"
            f"while not os.path.exists({str(flag)!r}):\n"
            "    if time.monotonic() > deadline:\n"
            "        sys.exit(5)\n"
            "    time.sleep(0.01)\n"
            "sys.stdout.buffer.write(b'T 2\\nok')",
        )
        frames = _runner._Frames(frozenset(), frozenset(), flag.touch)
        output = run_tool(
            [tool],
            b"x",
            timeout_seconds=30,
            max_output_bytes=1024,
            **_LIMITS,
            suffix=".x",
            on_output=frames.feed,
        )
        assert output == ToolOutput(b"", truncated=False)
        assert frames.result().text == "ok"
        assert flag.exists()

    @pytest.mark.real_extractor_child
    def test_xls_reports_an_xlrd_error_by_type(self, caplog):
        """The real child reports the walk's exception by type name, with
        no text from the workbook."""
        caplog.set_level("DEBUG")
        result = extract(
            content_type=_MIME["xls"],
            filename="a.xls",
            # A container identification names a workbook (#1416), whose
            # empty Workbook stream xlrd rejects.
            payload=make_ole2("Workbook", trailer=MARKER.encode() + bytes(1024)),
        )
        assert (result.status, result.error) == (STATUS_FAILED, "XLRDError")
        assert MARKER not in caplog.text


class TestChildSide:
    @pytest.mark.parametrize("module", _MODULES)
    @pytest.mark.parametrize("error", [MemoryError, RecursionError, ValueError])
    def test_any_exception_is_reported_by_type_only(self, module, error, monkeypatch):
        """Host-pressure types too: in the child they are its own limit."""
        mod = _module(extractor_child.MODULES[module])

        def boom(_payload, *_options, **_kwargs):
            raise error(MARKER)

        monkeypatch.setattr(mod, "extract_text", boom)
        assert extractor_child.run(module, b"x", _OPTIONS.get(module, ())) == (
            f"E {error.__name__}\n".encode()
        )

    def test_result_frames(self):
        assert extractor_child.result_frames("Zürich", ["a", "b"]) == (
            b"C a\nC b\nT 7\n" + "Zürich".encode()
        )
        assert extractor_child.result_frames("", []) == b"T 0\n"
        assert extractor_child.result_frames("", ["a"], {"text_lost": 1, "extractor_caps": 2}) == (
            b"C a\nN text_lost 1\nN extractor_caps 2\nT 0\n"
        )
        # A lone surrogate cannot be UTF-8: replaced, never a broken body.
        assert extractor_child.result_frames("\ud800", []) == b"T 1\n?"

    def test_the_child_runs_every_module_the_dispatcher_sends_it(self):
        """The modules whose extractors call ``run_child`` and the child's
        list agree, and each listed module has the extraction entry."""
        assert set(extractor_child.MODULES) == OOXML_MODULES | {"xls", "eml", "image", "container"}
        for name in extractor_child.MODULES.values():
            assert callable(_module(name).extract_text)

    @pytest.mark.parametrize("module", _MODULES)
    def test_frames_round_trip_through_the_parser(self, module, monkeypatch):
        """What the child writes for a real extraction parses back to the
        same text and caps."""
        if module == "image":
            from src.extractors import image_child

            monkeypatch.setattr(
                image_child.pytesseract, "image_to_string", lambda *_a, **_k: f"{MARKER} Café"
            )
            payload = _png()
        elif module == "xls":
            payload = (
                Path(__file__).parent / "fixtures" / "extractors" / "legacy.xls"
            ).read_bytes()
        else:
            payload = _ooxml_payload(module, f"{MARKER} Café")
        options = _OPTIONS.get(module, ())
        mod = _module(extractor_child.MODULES[module])
        text, caps = mod.extract_text(payload, *options)
        # A real child starts with zero counters.
        extractors.drain_counters()
        frames = _runner._Frames(
            frozenset(_cap_names(module)), extractors.CHILD_DEGRADATION_KEYS, None
        )
        frames.feed(extractor_child.run(module, payload, options))
        assert frames.result() == _runner.ChildResult(text, caps, {})
