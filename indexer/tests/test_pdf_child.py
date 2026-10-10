"""The PDF extractor in the extractor child (#1293).

pypdf's parse and walk, Poppler and Tesseract run in the child under the
limits ``pdf.py`` passes; progress, caps, counts, the OCR fallback's
failure and the extractor name cross the pipe as fixed tokens, and the
parent logs and records them as the in-process extractor did. The
stored results are pinned against ``main`` by ``tests/test_pdf_catalogue.py``.
Most tests here stub the child's output or run the child in this
process (``tests/conftest.py``); those marked ``real_extractor_child``
start the real process, and the ones that read ``/proc`` or need the
address-space limit run on Linux only (the image and CI). PDFs are
synthetic.
"""

from __future__ import annotations

import io
import logging
import math
import os
import shutil
import sys
import threading
import time
import zlib
from pathlib import Path

import pytest
from src import extractors
from src.extractors import (
    ENCRYPTED_PDF_ERROR,
    PDF_LIMIT_ERROR,
    SCANNED_PDF_OCR_DISABLED_ERROR,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
    _runner,
    extract,
    extractor_child,
    pdf,
    pdf_child,
)

from tests.test_extractor_child import stub_child_output
from tests.test_image_child import _limit
from tests.test_pdf_catalogue import _pdf

MARKER = "SYNTHETIC_PDF_CHILD_MARKER"
real_child = pytest.mark.real_extractor_child
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="reads /proc; limits are Linux")
requires_ocr_tools = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("tesseract", "pdftoppm", "pdfinfo")),
    reason="Tesseract or Poppler is not installed",
)


def _extract(payload: bytes, **kwargs):
    return extract(
        content_type="application/pdf", filename=f"{MARKER}.pdf", payload=payload, **kwargs
    )


@pytest.fixture(autouse=True)
def _fresh_counts():
    extractors.drain_extractor_counts()


@pytest.fixture(autouse=True)
def _restore_path(monkeypatch):
    """``pdf_child.extract_text`` sets ``PATH`` to its argument, which in
    this process is the test's own: undo it after each test."""
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))


# ---------------------------------------------------------------------------
# The child's arguments: one table, checked before any work
# ---------------------------------------------------------------------------

# Each argument at its bottom and top, below and above them, and of the
# wrong type, from ``pdf.OPTIONS``; the other arguments stay valid.
_VALID = ["1", "20", "60.0", "500", "/usr/bin"]


def _with(index: int, value: str) -> list[str]:
    values = list(_VALID)
    values[index] = value
    return values


def _option_cases() -> list[tuple[str, list[str], bool]]:
    cases: list[tuple[str, list[str], bool]] = []
    for index, (name, kind, low, high) in enumerate(pdf.OPTIONS):
        if kind == "path":
            cases += [
                (f"{name}-min", _with(index, "/"), True),
                (f"{name}-max", _with(index, "/" * int(high)), True),
                (f"{name}-below-min", _with(index, ""), False),
                (f"{name}-above-max", _with(index, "/" * (int(high) + 1)), False),
                (f"{name}-nul", _with(index, "/usr\0/bin"), False),
            ]
            continue
        top = repr(float(high)) if kind == "seconds" else str(int(high))
        above = repr(float(high) * 2) if kind == "seconds" else str(int(high) + 1)
        cases += [
            (f"{name}-min", _with(index, str(int(low))), True),
            (f"{name}-max", _with(index, top), True),
            (f"{name}-below-min", _with(index, "-1"), False),
            (f"{name}-above-max", _with(index, above), False),
            (f"{name}-wrong-type", _with(index, "x"), False),
        ]
        if kind in ("bool", "int", "int-or-none"):
            cases += [(f"{name}-float", _with(index, "1.5"), False)]
            cases += [(f"{name}-non-ascii-digit", _with(index, "١"), False)]
        if kind == "int-or-none":
            cases += [(f"{name}-none", _with(index, "none"), True)]
        else:
            cases += [(f"{name}-none-not-allowed", _with(index, "none"), False)]
        if kind == "seconds":
            for bad in ("nan", "inf", "-inf"):
                cases += [(f"{name}-{bad}", _with(index, bad), False)]
    cases += [
        ("too-few", _VALID[:-1], False),
        ("too-many", [*_VALID, "x"], False),
        ("none-at-all", [], False),
    ]
    return cases


class TestOptions:
    @pytest.mark.parametrize(
        ("values", "valid"),
        [pytest.param(values, valid, id=case) for case, values, valid in _option_cases()],
    )
    def test_every_argument_is_checked_against_the_table(self, values, valid):
        if valid:
            parsed = pdf_child.parse_options(values)
            assert list(parsed) == [name for name, *_ in pdf.OPTIONS]
        else:
            with pytest.raises(pdf_child.PdfChildOptionsError) as info:
                pdf_child.parse_options(values)
            assert str(info.value) == "pdf child argument out of range"

    def test_the_table_is_walked_before_any_work(self, monkeypatch):
        """A bad argument fails the run before pypdf reads anything."""
        monkeypatch.setattr(
            pdf_child, "_extract_digital_pages", lambda *a, **k: pytest.fail("work ran")
        )
        with pytest.raises(pdf_child.PdfChildOptionsError):
            pdf_child.extract_text(b"%PDF-1.7", *_with(1, "-1"))

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            # In-process meanings: no OCR cap at or below 0, no digital cap
            # for None, a digital cap below 0 stops at once as 0 does, no
            # timeout at or below 0 or for None.
            (
                {"ocr_enabled": True, "max_ocr_pages": 20, "ocr_timeout_seconds": 60},
                ["1", "20", "60.0", "500"],
            ),
            (
                {"ocr_enabled": False, "max_ocr_pages": -3, "ocr_timeout_seconds": None},
                ["0", "0", "0.0", "500"],
            ),
            ({"max_ocr_pages": 0, "ocr_timeout_seconds": -5, "max_pdf_pages": None}, None),
            ({"max_pdf_pages": -1}, ["1", "20", "0.0", "0"]),
            ({"max_pdf_pages": 0}, ["1", "20", "0.0", "0"]),
            (
                {"max_ocr_pages": 10**30, "max_pdf_pages": 10**30, "ocr_timeout_seconds": 1e300},
                None,
            ),
            ({"ocr_timeout_seconds": 7.25}, ["1", "20", "7.25", "500"]),
        ],
    )
    def test_the_parents_settings_always_pass_the_table(self, kwargs, expected):
        settings = {
            "ocr_enabled": True,
            "max_ocr_pages": 20,
            "ocr_timeout_seconds": None,
            "max_pdf_pages": 500,
            **kwargs,
        }
        values = pdf.child_options(**settings, path="/usr/bin")
        parsed = pdf_child.parse_options(values)
        if expected is not None:
            assert values[:4] == expected
        assert parsed["path"] == "/usr/bin"
        assert parsed["max_pdf_pages"] is None or isinstance(parsed["max_pdf_pages"], int)


# ---------------------------------------------------------------------------
# Limits and the arguments the parent passes
# ---------------------------------------------------------------------------


class TestLimits:
    def test_cpu_covers_the_digital_walk_and_a_four_thread_tesseract(self):
        assert pdf.child_cpu_seconds(500, 60) == math.ceil(500 * pdf._DIGITAL_PAGE_SECONDS) + 30
        assert pdf.child_cpu_seconds(10, 60) == 4 * 60 + 30
        assert pdf.child_cpu_seconds(10, None) == pdf.child_cpu_seconds(10, 60)
        # With the digital cap off, the default's walk is budgeted.
        assert pdf.child_cpu_seconds(None, 60) == pdf.child_cpu_seconds(500, 60)

    def test_the_wall_clock_covers_the_walk_the_render_and_every_page(self):
        walk = 30 + 500 * pdf._DIGITAL_PAGE_SECONDS
        assert pdf.child_timeout_seconds(
            ocr_enabled=True, max_ocr_pages=20, ocr_timeout_seconds=60, max_pdf_pages=500
        ) == walk + 2 * 60 + 20 * (60 + 10)
        assert (
            pdf.child_timeout_seconds(
                ocr_enabled=False, max_ocr_pages=20, ocr_timeout_seconds=60, max_pdf_pages=500
            )
            == walk
        )
        # With no OCR timeout a page is bounded by the CPU limit of a
        # single-threaded Tesseract, as for images.
        assert pdf.child_timeout_seconds(
            ocr_enabled=True, max_ocr_pages=2, ocr_timeout_seconds=None, max_pdf_pages=500
        ) == walk + 2 * 270 + 2 * (270 + 10)
        # With either cap off, its default is budgeted.
        assert pdf.child_timeout_seconds(
            ocr_enabled=True, max_ocr_pages=0, ocr_timeout_seconds=60, max_pdf_pages=None
        ) == pdf.child_timeout_seconds(
            ocr_enabled=True, max_ocr_pages=20, ocr_timeout_seconds=60, max_pdf_pages=500
        )

    def test_the_cpu_limit_lets_a_tesseract_timeout_fire_first(self):
        for timeout in (1, 60, 600):
            assert pdf.child_cpu_seconds(500, timeout) > 4 * timeout

    @pytest.mark.parametrize(
        ("kwargs", "options"),
        [
            ({}, ["1", "20", "60.0", "500"]),
            (
                {"ocr_enabled": False, "max_ocr_pages": 3, "ocr_timeout_seconds": None},
                ["0", "3", "0.0", "500"],
            ),
            ({"max_pdf_pages": None}, ["1", "20", "60.0", "none"]),
        ],
    )
    def test_the_settings_and_limits_reach_the_child(self, kwargs, options, monkeypatch):
        calls = stub_child_output(monkeypatch, b"T 0 pdf-digital\n")
        monkeypatch.setenv("PATH", "/opt/x/bin:/usr/bin")
        settings = {
            "ocr_enabled": True,
            "max_ocr_pages": 20,
            "ocr_timeout_seconds": 60,
            "max_pdf_pages": 500,
            **kwargs,
        }
        _extract(b"%PDF-1.7", **settings)
        [call] = calls
        assert call["argv"] == [
            sys.executable,
            "-I",
            str(_runner._CHILD),
            "pdf",
            *options,
            "/opt/x/bin:/usr/bin",
        ]
        assert call["max_address_space_bytes"] == pdf.CHILD_MAX_ADDRESS_SPACE_BYTES
        assert call["max_cpu_seconds"] == pdf.child_cpu_seconds(
            settings["max_pdf_pages"], settings["ocr_timeout_seconds"]
        )
        assert call["timeout_seconds"] == pdf.child_timeout_seconds(**settings)

    def test_the_child_finds_its_tools_through_the_path_it_is_given(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        seen: list[str | None] = []
        monkeypatch.setattr(
            pdf_child, "extract", lambda *a, **k: seen.append(os.environ.get("PATH")) or ("", "x")
        )
        pdf_child.extract_text(b"%PDF-1.7", *_with(4, "/opt/x/bin:/usr/bin"))
        assert seen == ["/opt/x/bin:/usr/bin"]


# ---------------------------------------------------------------------------
# The frames, as the parent applies them
# ---------------------------------------------------------------------------


def _text_frame(text: str, name: str) -> bytes:
    body = text.encode()
    return b"T %d %s\n" % (len(body), name.encode()) + body


class TestFrames:
    @pytest.mark.parametrize(
        ("name", "status", "extractor"),
        [
            ("pdf-digital", STATUS_SUCCESS, "pdf-digital@6"),
            ("pdf-ocr", STATUS_SUCCESS, "pdf-ocr@6"),
            ("pdf-ocr-disabled", STATUS_UNSUPPORTED, None),
        ],
    )
    def test_the_extractor_name_crosses_with_the_text(self, name, status, extractor, monkeypatch):
        stub_child_output(monkeypatch, _text_frame("words", name))
        result = _extract(b"%PDF-1.7")
        assert (result.status, result.extractor) == (status, extractor)
        if status == STATUS_UNSUPPORTED:
            assert result.error == SCANNED_PDF_OCR_DISABLED_ERROR

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(b"T 5\nwords", id="no-name"),
            pytest.param(b"T 5 image-ocr\nwords", id="another-extractors-name"),
            pytest.param(b"T 5 pdf-ocr extra\nwords", id="trailing-token"),
            pytest.param(b"T 5 \nwords", id="empty-name"),
            pytest.param(b"C image_text_chars\nT 0 pdf-digital\n", id="another-extractors-cap"),
            pytest.param(b"N pages 1\nT 0 pdf-digital\n", id="unknown-count"),
            pytest.param(b"R not a type\nT 0 pdf-digital\n", id="bad-recovered-type"),
            pytest.param(b"R RuntimeError\n", id="no-result"),
        ],
    )
    def test_output_that_breaks_the_protocol_is_failed(self, data, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        stub_child_output(monkeypatch, data)
        result = _extract(b"%PDF-1.7")
        assert (result.status, result.error) == (STATUS_FAILED, "ChildOutputError")
        # Nothing the broken output carried was applied.
        assert extractors.drain_extractor_counts()["extractor_caps"] == 0
        assert "OCR fallback failed" not in caplog.text

    def test_output_the_byte_cap_cut_is_failed_and_applies_nothing(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        stub_child_output(
            monkeypatch,
            b"N pdf_pages_failed 2\n" + _text_frame("words", "pdf-digital"),
            truncated=True,
        )
        result = _extract(b"%PDF-1.7")
        assert (result.status, result.error) == (STATUS_FAILED, "ChildOutputError")
        assert extractors.drain_extractor_counts()["pdf_pages_failed"] == 0

    def test_the_digital_page_cap_is_logged_counted_and_recorded(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        stub_child_output(
            monkeypatch, b"C pdf_digital_pages\n" + _text_frame(f"{MARKER} text", "pdf-digital")
        )
        result = _extract(b"%PDF-1.7", max_pdf_pages=7)
        assert (result.status, result.digital_pages_cap, result.text_complete) == (
            STATUS_SUCCESS,
            7,
            False,
        )
        [line] = [r for r in caplog.records if "extractor cap" in r.getMessage()]
        assert (line.levelno, line.getMessage()) == (
            logging.WARNING,
            "extractor cap pdf_digital_pages: pdf-digital stopped at 7 pages",
        )
        assert extractors.drain_extractor_counts()["extractor_caps"] == 1
        assert MARKER not in caplog.text

    def test_the_ocr_page_cap_is_logged_and_recorded_with_its_skipped_pages(
        self, monkeypatch, caplog
    ):
        caplog.set_level("DEBUG")
        stub_child_output(
            monkeypatch,
            b"C pdf_ocr_pages\nN ocr_capped_pdfs 1\nN ocr_pages_skipped 4\n"
            b"N result_ocr_pages_skipped 4\nN text_lost 1\n" + _text_frame("words", "pdf-ocr"),
        )
        result = _extract(b"%PDF-1.7", max_ocr_pages=3)
        assert (result.ocr_pages_skipped, result.ocr_pages_cap, result.text_complete) == (
            4,
            3,
            False,
        )
        [line] = [r for r in caplog.records if "OCR capped" in r.getMessage()]
        assert (line.levelno, line.getMessage()) == (
            logging.WARNING,
            "pdf OCR capped at 3 of 7 scanned pages",
        )
        counts = extractors.drain_extractor_counts()
        assert (counts["ocr_capped_pdfs"], counts["ocr_pages_skipped"]) == (1, 4)

    def test_the_text_budget_is_an_extractor_cap(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        stub_child_output(monkeypatch, b"C pdf_text_chars\n" + _text_frame("text", "pdf-digital"))
        result = _extract(b"%PDF-1.7")
        assert (result.status, result.text, result.text_complete) == (STATUS_SUCCESS, "text", False)
        [line] = [r for r in caplog.records if "extractor cap" in r.getMessage()]
        assert line.getMessage() == "extractor cap pdf_text_chars: pdf text cut at 10000000 chars"
        # Not a configured limit: nothing recorded.
        assert (result.digital_pages_cap, result.ocr_pages_cap) == (0, 0)

    def test_a_lowered_ocr_dpi_is_an_extractor_cap(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        stub_child_output(monkeypatch, b"N pdf_ocr_dpi 15\n" + _text_frame("words", "pdf-ocr"))
        result = _extract(b"%PDF-1.7")
        assert result.text_complete is False
        lines = [r.getMessage() for r in caplog.records if r.name == "indexer.extractor.pdf"]
        assert lines == ["extractor cap pdf_ocr_dpi: pdf OCR rendered at 15 dpi, not 200"]
        assert extractors.drain_extractor_counts()["extractor_caps"] == 1

    def test_an_ocr_fallback_failure_is_logged_by_type_name(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        stub_child_output(
            monkeypatch,
            b"N text_lost 1\nR TesseractError\n" + _text_frame("digital words", "pdf-digital"),
        )
        result = _extract(b"%PDF-1.7")
        assert (result.status, result.extractor, result.text_complete) == (
            STATUS_SUCCESS,
            "pdf-digital@6",
            False,
        )
        [line] = [r for r in caplog.records if "OCR fallback failed" in r.getMessage()]
        assert (line.levelno, line.getMessage()) == (
            logging.WARNING,
            "PDF OCR fallback failed: TesseractError",
        )

    def test_what_the_child_reported_before_it_failed_is_applied(self, monkeypatch, caplog):
        """The OCR cap and page failures recorded before the OCR raised,
        as the in-process extractor applied them before it raised."""
        caplog.set_level("DEBUG")
        stub_child_output(
            monkeypatch,
            b"C pdf_ocr_pages\nN ocr_capped_pdfs 1\nN ocr_pages_skipped 2\nN pdf_pages_failed 1\n"
            b"N result_ocr_pages_skipped 2\n"
            b"R RuntimeError\nE RuntimeError\n",
        )
        result = _extract(b"%PDF-1.7", max_ocr_pages=1)
        assert (result.status, result.error, result.ocr_pages_skipped) == (
            STATUS_FAILED,
            "RuntimeError",
            None,
        )
        counts = extractors.drain_extractor_counts()
        assert (counts["ocr_capped_pdfs"], counts["ocr_pages_skipped"]) == (1, 2)
        assert counts["pdf_pages_failed"] == 1
        messages = [r.getMessage() for r in caplog.records]
        assert "pdf OCR capped at 1 of 3 scanned pages" in messages
        assert "PDF OCR fallback failed: RuntimeError" in messages

    @pytest.mark.parametrize(
        ("type_name", "status", "error"),
        [
            ("FileNotDecryptedError", STATUS_UNSUPPORTED, ENCRYPTED_PDF_ERROR),
            ("LimitReachedError", STATUS_UNSUPPORTED, PDF_LIMIT_ERROR),
            # A subclass by another name stays failed, as in process.
            ("WrongPasswordError", STATUS_FAILED, "WrongPasswordError"),
            ("PdfReadError", STATUS_FAILED, "PdfReadError"),
            # The child's own limits are per payload, not host pressure.
            ("MemoryError", STATUS_FAILED, "MemoryError"),
            ("RecursionError", STATUS_FAILED, "RecursionError"),
        ],
    )
    def test_an_error_is_recorded_by_type_name(self, type_name, status, error, monkeypatch):
        stub_child_output(monkeypatch, f"E {type_name}\n".encode())
        result = _extract(b"%PDF-1.7")
        assert (result.status, result.error) == (status, error)


# ---------------------------------------------------------------------------
# The child side, in process
# ---------------------------------------------------------------------------


class TestChildSide:
    def test_the_text_is_stripped_then_cut_at_the_budget(self, monkeypatch):
        monkeypatch.setattr(pdf_child, "_MAX_TEXT_CHARS", 10)
        monkeypatch.setattr(
            pdf_child,
            "extract",
            lambda *a, **k: ("   " + "x" * 8 + "  \n  y" + " " * 50, "pdf-ocr"),
        )
        extractors.reset_attempt()
        text, caps, name = pdf_child.extract_text(b"%PDF-1.7", *_VALID)
        assert (text, caps, name) == ("x" * 8 + "  ", [], "pdf-ocr")
        assert extractors.child_reports() == (["pdf_text_chars"], [])

    def test_text_within_the_budget_is_only_stripped(self, monkeypatch):
        monkeypatch.setattr(pdf_child, "extract", lambda *a, **k: ("  a\n\nb  ", "pdf-digital"))
        extractors.reset_attempt()
        assert pdf_child.extract_text(b"%PDF-1.7", *_VALID) == ("a\n\nb", [], "pdf-digital")
        assert extractors.child_reports() == ([], [])

    def test_each_page_image_is_closed_once_it_is_ocrd(self, monkeypatch):
        """At most one decoded page is held, whatever the page cap."""
        from PIL import Image

        images: list[Image.Image] = []
        closed: set[int] = set()
        open_at_ocr: list[int] = []

        def convert(_payload, **kwargs):
            batch = []
            for _ in range(kwargs["first_page"], kwargs["last_page"] + 1):
                image = Image.new("RGB", (4, 4), "white")
                index = len(images)
                original = image.close
                image.close = lambda original=original, index=index: (  # type: ignore[method-assign]
                    closed.add(index),
                    original(),
                )[1]
                images.append(image)
                batch.append(image)
            return batch

        def ocr(_image, **_kwargs):
            open_at_ocr.append(len(images) - len(closed))
            return "text"

        monkeypatch.setattr("pdf2image.convert_from_bytes", convert)
        monkeypatch.setattr("pdf2image.pdfinfo_from_bytes", lambda *_a, **_k: {})
        monkeypatch.setattr("pytesseract.image_to_string", ocr)
        texts = pdf_child._extract_ocr(_pdf("ssss"), pages=[0, 1, 2, 3])
        assert texts == dict.fromkeys(range(4), "text")
        # Before each OCR every earlier page is closed; all are after.
        assert open_at_ocr == [4, 3, 2, 1]
        assert closed == {0, 1, 2, 3}

    def test_an_image_is_closed_when_its_ocr_raises(self, monkeypatch):
        from PIL import Image

        closed: list[int] = []

        def convert(_payload, **_kwargs):
            image = Image.new("RGB", (4, 4), "white")
            original = image.close
            image.close = lambda: (closed.append(1), original())[1]  # type: ignore[method-assign]
            return [image]

        def ocr(_image, **_kwargs):
            raise RuntimeError(MARKER)

        monkeypatch.setattr("pdf2image.convert_from_bytes", convert)
        monkeypatch.setattr("pdf2image.pdfinfo_from_bytes", lambda *_a, **_k: {})
        monkeypatch.setattr("pytesseract.image_to_string", ocr)
        with pytest.raises(RuntimeError):
            pdf_child._extract_ocr(_pdf("s"), pages=[0])
        assert closed == [1]

    def test_rendered_pages_go_to_the_scratch_directory(self, monkeypatch, tmp_path):
        from PIL import Image

        folders: list[str] = []

        def convert(_payload, **kwargs):
            folders.append(kwargs["output_folder"])
            return [Image.new("RGB", (4, 4), "white")]

        monkeypatch.setattr("pdf2image.convert_from_bytes", convert)
        monkeypatch.setattr("pdf2image.pdfinfo_from_bytes", lambda *_a, **_k: {})
        monkeypatch.setattr("pytesseract.image_to_string", lambda *_a, **_k: "text")
        monkeypatch.setattr(pdf_child.tempfile, "tempdir", str(tmp_path))
        pdf_child._extract_ocr(_pdf("s"), pages=[0])
        [folder] = folders
        assert Path(folder).parent == tmp_path
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# The text ceiling (owner exception, 2026-10-10 on #1293)
# ---------------------------------------------------------------------------

_CEILING = 10_000_000
_DEFAULT_CHARS = 2_000_000
# A stripped text whose whitespace run of 10,000,000 less the setting
# (and 10 more) ends exactly at the child's cut.
_WHITESPACE_RUN = "a" * (_DEFAULT_CHARS - 10) + " " * (_CEILING - _DEFAULT_CHARS + 10) + "b" * 50
_OVER_CEILING = "x" * (_CEILING + 2_000_000)


def _row(result) -> dict:
    from src.extractors import CAP_DIGITAL_PAGES, CAP_EXTRACTED_CHARS, CAP_OCR_PAGES

    return {
        "extraction_status": result.status,
        "extractor": result.extractor,
        "ocr_pages_skipped": result.ocr_pages_skipped,
        CAP_OCR_PAGES: result.ocr_pages_cap,
        CAP_DIGITAL_PAGES: result.digital_pages_cap,
        CAP_EXTRACTED_CHARS: result.extracted_chars_cap,
    }


def _refreshed_at(result, max_extracted_chars: int) -> bool:
    from src.attachment_indexing import cap_raised

    return cap_raised(
        _row(result),
        ocr_enabled=True,
        max_ocr_pages=20,
        max_pdf_pages=500,
        max_extracted_chars=max_extracted_chars,
    )


class TestTextCeiling:
    """The child cuts the stripped text at 10,000,000 characters with no
    ``pdf`` version bump (owner exception, 2026-10-10 on #1293, as for
    images on #1325). Stored results change only with the character cap
    off or above 10,000,000 (a longer text is stored cut and incomplete,
    and the hardcoded cut records no configured cap, so raising a
    setting does not refresh it), and for one crafted shape, a whitespace
    run ending at the cut (its trailing whitespace and the configured-cap
    record differ). The in-process extractor stored the expected values
    noted beside each case."""

    @pytest.mark.parametrize(
        ("text", "setting", "stored", "cap", "refresh_setting", "refreshed"),
        [
            # Cap off: the in-process extractor stored all 12,000,000.
            pytest.param(_OVER_CEILING, None, _OVER_CEILING[:_CEILING], 0, 0, False, id="off"),
            # Above the ceiling: in process, all 12,000,000 (under 15M).
            pytest.param(
                _OVER_CEILING,
                15_000_000,
                _OVER_CEILING[:_CEILING],
                0,
                20_000_000,
                False,
                id="above-ceiling",
            ),
            # Default: as in process, cut at the setting and refreshable.
            pytest.param(
                _OVER_CEILING,
                _DEFAULT_CHARS,
                _OVER_CEILING[:_DEFAULT_CHARS],
                _DEFAULT_CHARS,
                3_000_000,
                True,
                id="default",
            ),
            # The whitespace shape: in process, 1,999,990 a's then 10
            # spaces, cap 2,000,000 and refreshable; through the child the
            # run is stripped and no configured cap is recorded.
            pytest.param(
                _WHITESPACE_RUN,
                _DEFAULT_CHARS,
                "a" * (_DEFAULT_CHARS - 10),
                0,
                3_000_000,
                False,
                id="whitespace-run-at-the-cut",
            ),
        ],
    )
    def test_the_stored_result(
        self, text, setting, stored, cap, refresh_setting, refreshed, monkeypatch, caplog
    ):
        caplog.set_level("DEBUG")
        monkeypatch.setattr(pdf_child, "extract", lambda *_a, **_k: (text, "pdf-digital"))
        result = _extract(b"%PDF-1.7", max_extracted_chars=setting)
        assert (result.status, result.extractor) == (STATUS_SUCCESS, "pdf-digital@6")
        assert result.text == stored
        assert result.text_complete is False
        assert result.extracted_chars_cap == cap
        assert _refreshed_at(result, refresh_setting) is refreshed
        # The hardcoded cut is reported as its own extractor cap.
        assert "extractor cap pdf_text_chars" in caplog.text


# ---------------------------------------------------------------------------
# The OCR phase's address-space limit (#1450)
# ---------------------------------------------------------------------------

MiB = 1024 * 1024


class _Limits:
    """A fake ``resource`` limit for ``RLIMIT_AS``."""

    def __init__(self, hard):
        self.value = (hard, hard)
        self.set: list[tuple[int, int]] = []

    def getrlimit(self, _which):
        return self.value

    def setrlimit(self, _which, value):
        self.set.append(value)
        self.value = value


def _fake_limits(monkeypatch, *, hard, mapped):
    limits = _Limits(hard)
    monkeypatch.setattr(pdf_child.resource, "getrlimit", limits.getrlimit)
    monkeypatch.setattr(pdf_child.resource, "setrlimit", limits.setrlimit)
    monkeypatch.setattr(pdf_child, "_mapped_bytes", lambda: mapped)
    return limits


class TestOcrPhaseLimit:
    @pytest.mark.parametrize(
        ("mapped", "expected"),
        [
            # The tools' need is the floor.
            (100 * MiB, 768 * MiB),
            (512 * MiB, 768 * MiB),
            # Above it, what the child maps plus its headroom.
            (700 * MiB, 956 * MiB),
            (1024 * MiB, 1280 * MiB),
        ],
    )
    def test_the_limit_is_the_larger_of_the_child_and_the_tools_need(
        self, mapped, expected, monkeypatch
    ):
        limits = _fake_limits(monkeypatch, hard=2048 * MiB, mapped=mapped)
        pdf_child._limit_for_ocr()
        assert limits.set == [(expected, expected)]

    def test_a_child_over_the_budget_starts_no_tool(self, monkeypatch):
        limits = _fake_limits(monkeypatch, hard=2048 * MiB, mapped=1025 * MiB)
        with pytest.raises(pdf_child.PdfOcrMemoryBudgetError) as info:
            pdf_child._limit_for_ocr()
        assert str(info.value) == "pdf child over the OCR-phase address-space budget"
        assert limits.set == []

    def test_a_lower_limit_is_kept(self, monkeypatch):
        limits = _fake_limits(monkeypatch, hard=512 * MiB, mapped=100 * MiB)
        pdf_child._limit_for_ocr()
        assert limits.set == [(512 * MiB, 512 * MiB)]

    def test_with_no_limit_nothing_changes(self, monkeypatch):
        """In a test that runs the child in process, and on macOS."""
        import resource

        limits = _fake_limits(monkeypatch, hard=resource.RLIM_INFINITY, mapped=0)
        monkeypatch.setattr(pdf_child, "_mapped_bytes", lambda: pytest.fail("read"))
        pdf_child._limit_for_ocr()
        assert limits.set == []

    def test_the_mapped_size_is_this_processes(self):
        if not Path("/proc/self/statm").exists():
            pytest.skip("no /proc")
        assert 10 * MiB < pdf_child._mapped_bytes() < 64 * 1024 * MiB

    def test_the_limit_is_set_after_the_parse_and_before_any_tool(self, monkeypatch):
        from PIL import Image

        events: list[str] = []
        real_dpi = pdf_child._ocr_dpi
        monkeypatch.setattr(pdf_child, "_ocr_dpi", lambda *a: events.append("dpi") or real_dpi(*a))
        monkeypatch.setattr(pdf_child, "_limit_for_ocr", lambda: events.append("limit"))
        monkeypatch.setattr(
            "pdf2image.pdfinfo_from_bytes", lambda *_a, **_k: events.append("pdfinfo") or {}
        )
        monkeypatch.setattr(
            "pdf2image.convert_from_bytes",
            lambda *_a, **_k: events.append("render") or [Image.new("RGB", (4, 4))],
        )
        monkeypatch.setattr(
            "pytesseract.image_to_string", lambda *_a, **_k: events.append("ocr") or "t"
        )
        pdf_child._extract_ocr(_pdf("s"), pages=[0], ocr_timeout_seconds=60)
        assert events == ["dpi", "limit", "pdfinfo", "render", "ocr"]

    @pytest.mark.parametrize(
        ("layout", "status", "extractor", "error"),
        [
            # A mixed PDF keeps its digital text, as on any OCR failure.
            ("ds", STATUS_SUCCESS, "pdf-digital@6", None),
            ("ss", STATUS_FAILED, "pdf@6", "PdfOcrMemoryBudgetError"),
        ],
    )
    def test_over_the_budget_the_ocr_fallback_fails_without_a_tool(
        self, layout, status, extractor, error, monkeypatch, caplog
    ):
        caplog.set_level("DEBUG")
        _fake_limits(monkeypatch, hard=2048 * MiB, mapped=1100 * MiB)
        for target in (
            "pdf2image.pdfinfo_from_bytes",
            "pdf2image.convert_from_bytes",
            "pytesseract.image_to_string",
        ):
            monkeypatch.setattr(target, lambda *_a, **_k: pytest.fail("a tool ran"))
        result = _extract(_pdf(layout), ocr_timeout_seconds=60)
        assert (result.status, result.extractor, result.error) == (status, extractor, error)
        assert result.text_complete is False
        assert "PDF OCR fallback failed: PdfOcrMemoryBudgetError" in caplog.text


class TestRenderCalls:
    def test_a_long_run_is_rendered_a_few_pages_at_a_time(self, monkeypatch):
        """At most ``_PAGES_PER_RENDER`` pages per Poppler call, each in
        its own scratch directory, removed before the next call (#1450)."""
        from PIL import Image

        calls: list[tuple[int, int, str, bool]] = []

        def convert(_payload, **kwargs):
            earlier_left = any(Path(folder).exists() for *_r, folder, _e in calls)
            calls.append(
                (kwargs["first_page"], kwargs["last_page"], kwargs["output_folder"], earlier_left)
            )
            return [
                Image.new("RGB", (4, 4))
                for _ in range(kwargs["first_page"], kwargs["last_page"] + 1)
            ]

        monkeypatch.setattr("pdf2image.convert_from_bytes", convert)
        monkeypatch.setattr("pdf2image.pdfinfo_from_bytes", lambda *_a, **_k: {})
        monkeypatch.setattr("pytesseract.image_to_string", lambda *_a, **_k: "t")
        texts = pdf_child._extract_ocr(_pdf("s" * 12), pages=list(range(12)))
        assert texts == dict.fromkeys(range(12), "t")
        assert [(first, last) for first, last, *_ in calls] == [(1, 5), (6, 10), (11, 12)]
        assert len({folder for _f, _l, folder, _e in calls}) == 3
        assert not any(left for *_rest, left in calls)
        assert not any(Path(folder).exists() for _f, _l, folder, _e in calls)


# ---------------------------------------------------------------------------
# The real child process
# ---------------------------------------------------------------------------


def _in_process(monkeypatch) -> None:
    """Run the extractor child in this process, as ``tests/conftest.py``
    does for unmarked tests."""

    real = _runner.run_tool

    def run_tool(argv, payload, *, on_output=None, **kwargs):
        child = str(_runner._CHILD)
        if child not in argv:
            return real(argv, payload, on_output=on_output, **kwargs)
        module, *options = argv[argv.index(child) + 1 :]
        before = extractors.drain_counters()
        attempt = dict(vars(extractors._attempt))
        try:
            output = extractor_child.run(
                module, payload, options, lambda: on_output(extractor_child.PROGRESS_FRAME)
            )
        finally:
            extractors.add_counters(before)
            vars(extractors._attempt).clear()
            vars(extractors._attempt).update(attempt)
        on_output(output)
        return _runner.ToolOutput(b"", truncated=False)

    monkeypatch.setattr(_runner, "run_tool", run_tool)


def _observe(payload: bytes, **kwargs) -> tuple[object, dict, int]:
    extractors.drain_extractor_counts()
    progress: list[int] = []
    result = _extract(payload, on_progress=lambda: progress.append(1), **kwargs)
    counts = {k: n for k, n in extractors.drain_extractor_counts().items() if n}
    return result, counts, len(progress)


@real_child
class TestRealChild:
    @pytest.mark.parametrize(
        ("layout", "kwargs"),
        [
            ("dd", {}),
            ("dbd", {"ocr_enabled": False}),
            ("ddd", {"max_pdf_pages": 2, "ocr_enabled": False}),
            ("t", {"ocr_enabled": False}),
        ],
    )
    def test_without_ocr_the_child_stores_what_the_in_process_run_stores(
        self, layout, kwargs, monkeypatch
    ):
        payload = _pdf(layout)
        child = _observe(payload, **kwargs)
        _in_process(monkeypatch)
        assert _observe(payload, **kwargs) == child

    @requires_ocr_tools
    @pytest.mark.parametrize(
        ("layout", "kwargs"),
        [("ds", {}), ("sds", {"max_ocr_pages": 1}), ("sL", {})],
    )
    def test_with_real_poppler_and_tesseract_the_child_stores_what_the_in_process_run_stores(
        self, layout, kwargs, monkeypatch
    ):
        payload = _pdf(layout)
        child = _observe(payload, **kwargs)
        _in_process(monkeypatch)
        assert _observe(payload, **kwargs) == child

    @requires_ocr_tools
    def test_the_childs_poppler_and_tesseract_launches_are_counted(self):
        """#1236: the child reports the processes it started: the timed
        page count, then per run pdf2image's page count, Poppler's version
        check and the render, and one Tesseract per page."""
        before = _runner.process_launches()
        result = _extract(_pdf("ss"), ocr_timeout_seconds=60)
        assert result.status in (STATUS_SUCCESS, STATUS_EMPTY)
        assert _runner.process_launches() - before == 1 + 1 + 3 + 2

    def test_a_page_tree_bomb_is_unsupported_without_reading_a_page(self):
        """A 2 KB PDF whose page tree declares 2^20 pages by sharing
        nodes: pypdf refuses it while listing pages (``LimitReachedError``),
        recorded ``unsupported``, and no page is read."""
        payload = _page_tree_bomb(20)
        assert len(payload) < 4096
        start = time.monotonic()
        result, _counts, progress = _observe(payload)
        assert (result.status, result.error) == (STATUS_UNSUPPORTED, PDF_LIMIT_ERROR)
        assert progress == 0
        assert time.monotonic() - start < 30

    def test_a_flat_huge_page_count_stops_at_the_digital_cap(self):
        """20,000 pages: the walk reads exactly the cap's pages."""
        result, _counts, progress = _observe(_flat_pages(20_000), max_pdf_pages=50)
        assert (result.status, result.digital_pages_cap) == (STATUS_SUCCESS, 50)
        assert progress == 50

    def test_a_deeply_nested_object_fails_the_pdf(self):
        result, _counts, progress = _observe(_nested_catalog(50_000))
        assert (result.status, result.error) == (STATUS_FAILED, "PdfReadError")
        assert progress == 0

    def test_a_walk_past_the_wall_clock_is_killed_with_its_pages_counted(self, monkeypatch):
        """Pages that each cost about a second of pypdf: the child is
        killed at its wall clock, having read some pages and not all, and
        the PDF is ``failed``."""
        monkeypatch.setattr(pdf, "child_timeout_seconds", lambda **_k: 4.0)
        payload = _heavy_pages(200, mb=1.0)
        start = time.monotonic()
        result, _counts, progress = _observe(payload, ocr_enabled=False)
        assert (result.status, result.error) == (STATUS_FAILED, "ToolTimeoutError")
        assert 0 < progress < 200
        assert time.monotonic() - start < 30


@real_child
@linux_only
class TestLimitsInTheImage:
    """Linux only: the address-space limit is not applied on macOS
    (``_launcher``), and the process checks read ``/proc``."""

    def test_the_address_space_limit_fails_a_content_stream_bomb(self, monkeypatch):
        """One page whose 8 MiB content stream (about 20 KB compressed)
        pypdf's walk expands to about 250 MiB: under a 200 MiB limit the
        child fails before the page is read (``MemoryError``, or
        ``ToolExitError`` when the child cannot report it), and under the
        real limit the same page is read."""
        payload = _heavy_pages(1, mb=8.0, op=b"1.1 2.2 m 3.3 4.4 l S\n")
        assert len(payload) < 64 * 1024
        limit = pdf.CHILD_MAX_ADDRESS_SPACE_BYTES
        monkeypatch.setattr(pdf, "CHILD_MAX_ADDRESS_SPACE_BYTES", 200 * 1024 * 1024)
        start = time.monotonic()
        result, _counts, progress = _observe(payload, ocr_enabled=False)
        assert result.status == STATUS_FAILED
        assert result.error in ("MemoryError", "ToolExitError")
        assert progress == 0
        assert time.monotonic() - start < 60
        # The page has no text, so with OCR off it is the OCR-disabled
        # sentinel once read.
        monkeypatch.setattr(pdf, "CHILD_MAX_ADDRESS_SPACE_BYTES", limit)
        result, _counts, progress = _observe(payload, ocr_enabled=False)
        assert (result.status, result.error, progress) == (
            STATUS_UNSUPPORTED,
            SCANNED_PDF_OCR_DISABLED_ERROR,
            1,
        )

    @requires_ocr_tools
    def test_poppler_and_tesseract_inherit_the_limits_and_die_with_the_child(
        self, tmp_path, monkeypatch
    ):
        scratch_root = tmp_path / "scratch"
        scratch_root.mkdir()
        monkeypatch.setattr(_runner, "_TMP_DIR", str(scratch_root))
        monkeypatch.setattr(pdf, "child_timeout_seconds", lambda **_k: 4.0)
        payload = _scanned_text_pages(6)
        seen: dict[int, tuple[str, str]] = {}
        done = threading.Event()

        def watch():
            while not done.is_set():
                for pid, found in _tools_under(scratch_root).items():
                    seen.setdefault(pid, found)
                time.sleep(0.01)

        watcher = threading.Thread(target=watch)
        watcher.start()
        try:
            result, _counts, _progress = _observe(payload, ocr_timeout_seconds=60)
        finally:
            done.set()
            watcher.join()
        assert (result.status, result.error) == (STATUS_FAILED, "ToolTimeoutError")
        tools = {name for name, _limits in seen.values()}
        assert "tesseract" in tools and tools & {"pdftoppm", "pdfinfo"}
        # The OCR phase's limit (#1450): this scan-only PDF leaves the
        # child mapping far less than the tools' floor less the headroom.
        address_space = pdf._OCR_TOOL_ADDRESS_SPACE_BYTES
        cpu = pdf.child_cpu_seconds(500, 60)
        for _name, limits in seen.values():
            assert _limit(limits, "Max address space") == (address_space, address_space)
            assert _limit(limits, "Max cpu time") == (cpu, cpu + 1)
        assert _tools_under(scratch_root) == {}
        assert list(scratch_root.iterdir()) == []

    @requires_ocr_tools
    def test_a_heavy_digital_page_then_scans_run_under_the_ocr_phase_limit(self):
        """#1450: a 16 MiB page of path operators (pypdf's walk peaks
        around 430 MiB) then scanned pages, end to end. The child's limit
        is 2 GiB for the parse and the OCR phase's once OCR starts; each
        Poppler and Tesseract process runs under the OCR phase's."""
        payload = _mixed_pdf(16.0, 2)
        seen: dict[int, tuple[str, str]] = {}
        child_limits: set[tuple[int, int]] = set()
        done = threading.Event()

        def watch():
            while not done.is_set():
                for pid, found in _tools_under(Path(_runner._TMP_DIR)).items():
                    seen.setdefault(pid, found)
                for limits in _pdf_children():
                    child_limits.add(_limit(limits, "Max address space"))
                time.sleep(0.005)

        watcher = threading.Thread(target=watch)
        watcher.start()
        try:
            result, _counts, progress = _observe(payload, ocr_timeout_seconds=60)
        finally:
            done.set()
            watcher.join()
        assert (result.status, result.extractor) == (STATUS_SUCCESS, "pdf-ocr@6")
        assert "synthetic" in (result.text or "").lower()
        assert progress == 3 + 2
        tools = {name for name, _limits in seen.values()}
        assert {"tesseract", "pdftoppm", "pdfinfo"} <= tools
        ocr_phase = pdf._OCR_TOOL_ADDRESS_SPACE_BYTES
        for _name, limits in seen.values():
            assert _limit(limits, "Max address space") == (ocr_phase, ocr_phase)
        parse = pdf.CHILD_MAX_ADDRESS_SPACE_BYTES
        assert child_limits <= {(parse, parse), (ocr_phase, ocr_phase)}
        assert (ocr_phase, ocr_phase) in child_limits


def _tools_under(root: Path) -> dict[int, tuple[str, str]]:
    """Poppler and Tesseract processes whose arguments name a file under
    ``root``, with their ``/proc`` limits."""
    found: dict[int, tuple[str, str]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            limits = (entry / "limits").read_text()
        except OSError:
            continue
        name = os.path.basename(argv[0]).decode(errors="replace") if argv else ""
        if name in ("tesseract", "pdftoppm", "pdfinfo") and any(
            str(root).encode() in a for a in argv
        ):
            found[int(entry.name)] = (name, limits)
    return found


# ---------------------------------------------------------------------------
# Synthetic hostile and heavy PDFs
# ---------------------------------------------------------------------------


def _build(objects: list[bytes]) -> bytes:
    """A PDF of ``objects`` (object 1 is the catalog) with a valid xref."""
    out = io.BytesIO()
    out.write(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R /Info << /Title (%s) >> >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, MARKER.encode(), xref)
    )
    return out.getvalue()


_FONT = b"<< /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>"


def _page_tree_bomb(depth: int) -> bytes:
    """One page under ``depth`` levels of ``/Pages`` nodes, each listing
    its child twice: 2 ** ``depth`` pages declared in ``depth`` + 3
    objects."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>")
    kid = 3
    for level in range(1, depth + 1):
        objects.append(b"<< /Type /Pages /Kids [%d 0 R %d 0 R] /Count %d >>" % (kid, kid, 2**level))
        kid = len(objects)
    objects[1] = b"<< /Type /Pages /Kids [%d 0 R] /Count %d >>" % (kid, 2**depth)
    return _build(objects)


def _flat_pages(count: int) -> bytes:
    """``count`` pages sharing one short digital content stream."""
    text = f"Synthetic flat page {MARKER} long enough to pass the floor".encode()
    content = b"BT /F1 12 Tf 72 720 Td (" + text + b") Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    kids = []
    for _ in range(count):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 3 0 R "
            b"/Resources " + _FONT + b" >>"
        )
        kids.append(b"%d 0 R" % len(objects))
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), count)
    return _build(objects)


def _nested_catalog(depth: int) -> bytes:
    """A one-page PDF whose catalog holds an array nested ``depth`` deep."""
    return _build(
        [
            b"<< /Type /Catalog /Pages 2 0 R /X " + b"[" * depth + b"]" * depth + b" >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
        ]
    )


def _heavy_pages(count: int, *, mb: float, op: bytes = b"(ab) Tj\n") -> bytes:
    """``count`` pages sharing one Flate content stream of ``mb`` MiB of
    ``op`` (a few KB compressed)."""
    raw = b"BT /F1 12 Tf\n" + op * int(mb * 2**20 // len(op)) + b"ET\n"
    data = zlib.compress(raw, 9)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream",
    ]
    kids = []
    for _ in range(count):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 3 0 R "
            b"/Resources " + _FONT + b" >>"
        )
        kids.append(b"%d 0 R" % len(objects))
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), count)
    return _build(objects)


def _scanned_text_pages(count: int) -> bytes:
    """``count`` 15.8-inch pages, each a 3160 x 3160 JPEG of text lines
    (just under the 10,000,000-pixel page budget at 200 dpi)."""
    from PIL import Image, ImageDraw

    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids = []
    for n in range(count):
        img = Image.new("RGB", (3160, 3160), "white")
        draw = ImageDraw.Draw(img)
        for y in range(40, 3100, 52):
            draw.text(
                (60, y),
                f"Synthetic scanned statement line {n} {y} total",
                fill="black",
                font_size=32,
            )
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        jpeg = buf.getvalue()
        page = len(objects) + 1
        content = b"q 1137 0 0 1137 0 0 cm /Im0 Do Q"
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1137 1137] /Contents %d 0 R "
            b"/Resources << /XObject << /Im0 %d 0 R >> >> >>" % (page + 1, page + 2)
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        objects.append(
            b"<< /Type /XObject /Subtype /Image /Width 3160 /Height 3160 /ColorSpace /DeviceRGB "
            b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n"
            % len(jpeg)
            + jpeg
            + b"\nendstream"
        )
        kids.append(b"%d 0 R" % page)
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), count)
    return _build(objects)


def _pdf_children() -> list[str]:
    """The ``/proc`` limits of every PDF extractor child running."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            limits = (entry / "limits").read_text()
        except OSError:
            continue
        if any(a.endswith(b"extractor_child.py") for a in argv) and b"pdf" in argv:
            found.append(limits)
    return found


def _mixed_pdf(mb: float, scans: int) -> bytes:
    """A first page of ``mb`` MiB of path operators with a line of text,
    then ``scans`` scanned letter pages of text at 200 dpi."""
    import random

    from PIL import Image, ImageDraw

    rnd = random.Random(1)
    ops = [b"BT /F1 12 Tf 72 720 Td (Synthetic heavy page with enough text to count) Tj ET\n"]
    size = 0
    while size < mb * 2**20:
        op = b"%d.%d %d.%d m %d.%d %d.%d l S\n" % tuple(rnd.randrange(600) for _ in range(8))
        ops.append(op)
        size += len(op)
    heavy = zlib.compress(b"".join(ops), 6)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources " + _FONT + b" >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(heavy) + heavy + b"\nendstream",
    ]
    kids = [b"3 0 R"]
    for n in range(scans):
        img = Image.new("RGB", (1700, 2200), "white")
        draw = ImageDraw.Draw(img)
        for y in range(60, 2100, 48):
            draw.text(
                (80, y), f"Synthetic scanned statement line {n} {y}", fill="black", font_size=30
            )
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        jpeg = buf.getvalue()
        page = len(objects) + 1
        content = b"q 612 0 0 792 0 0 cm /Im0 Do Q"
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
            b"/Resources << /XObject << /Im0 %d 0 R >> >> >>" % (page + 1, page + 2)
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        objects.append(
            b"<< /Type /XObject /Subtype /Image /Width 1700 /Height 2200 /ColorSpace /DeviceRGB "
            b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n"
            % len(jpeg)
            + jpeg
            + b"\nendstream"
        )
        kids.append(b"%d 0 R" % page)
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), len(kids))
    return _build(objects)
