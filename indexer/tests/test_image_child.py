"""The image extractor in the extractor child (#1292).

PIL's decode and Tesseract run in the child under the limits
``image.py`` passes; progress crosses the pipe as ``P`` frames and the
OCR caps as ``C`` frames, which the parent logs and counts as before.
Most tests here stub the child's output or run the child in this
process (``tests/conftest.py``); those marked ``real_extractor_child``
start the real process, and the ones that read ``/proc`` or run
Tesseract under the real limits run on Linux only (the image and CI).
Images are synthetic.
"""

from __future__ import annotations

import io
import logging
import shutil
import sys
import threading
import time
import warnings
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from src import extractors
from src.extractors import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SUCCESS,
    _runner,
    extract,
    image,
    image_child,
)

from tests.test_extractor_child import stub_child_output

MARKER = "SYNTHETIC_IMAGE_MARKER"
real_child = pytest.mark.real_extractor_child
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="reads /proc; limits are Linux")
# The Tesseract the indexer finds on its ``PATH``, passed to the child.
TESSERACT = shutil.which("tesseract")
requires_tesseract = pytest.mark.skipif(TESSERACT is None, reason="Tesseract is not installed")


def _tiff(count: int, size: tuple[int, int] = (16, 16)) -> bytes:
    frames = [Image.new("L", size, 255) for _ in range(count)]
    buf = io.BytesIO()
    frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
    return buf.getvalue()


def _png(size: tuple[int, int], mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, "white").save(buf, format="PNG")
    return buf.getvalue()


def _text_page(size: tuple[int, int]) -> bytes:
    img = Image.new("L", size, 255)
    draw = ImageDraw.Draw(img)
    line = "Synthetic invoice 12345 total amount due 42.00 " * 8
    for y in range(20, size[1] - 40, 40):
        draw.text((20, y), line, fill=0, font_size=28)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _extract(payload: bytes, **kwargs):
    return extract(content_type="image/tiff", filename=f"{MARKER}.tiff", payload=payload, **kwargs)


class TestLimits:
    def test_cpu_limit_lets_the_ocr_timeout_fire_first(self):
        """Tesseract runs up to four threads, so a page can use four
        times its timeout in CPU; with no timeout the default's applies."""
        assert image.child_cpu_seconds(60) == 4 * 60 + 30
        assert image.child_cpu_seconds(120) == 4 * 120 + 30
        assert image.child_cpu_seconds(None) == image.child_cpu_seconds(60)

    def test_wall_clock_covers_every_page_at_its_timeout(self):
        assert image.child_timeout_seconds(20, 60) == 20 * (60 + 10) + 30
        assert image.child_timeout_seconds(1, 60) == 70 + 30
        # With no OCR timeout a page is bounded by the CPU limit of a
        # single-threaded Tesseract.
        assert image.child_timeout_seconds(20, None) == 20 * (270 + 10) + 30

    @pytest.mark.parametrize(
        ("pages", "timeout", "options"),
        [(20, 60, ["20", "60"]), (3, None, ["3", "0"]), (5, 0, ["5", "0"]), (2, 7.5, ["2", "7.5"])],
    )
    def test_the_page_cap_and_timeout_reach_the_child(self, pages, timeout, options, monkeypatch):
        calls = stub_child_output(monkeypatch, b"T 0\n")
        monkeypatch.setattr(image.shutil, "which", lambda _name: "/opt/x/bin/tesseract")
        _extract(_tiff(1), max_ocr_pages=pages, ocr_timeout_seconds=timeout)
        [call] = calls
        assert call["argv"] == [
            sys.executable,
            "-I",
            str(_runner._CHILD),
            "image",
            *options,
            "/opt/x/bin/tesseract",
        ]
        effective = timeout or None
        assert call["max_cpu_seconds"] == image.child_cpu_seconds(effective)
        assert call["timeout_seconds"] == image.child_timeout_seconds(pages, effective)
        assert call["max_address_space_bytes"] == image.CHILD_MAX_ADDRESS_SPACE_BYTES


class TestTesseractPath:
    """The child runs with no ``PATH``: the parent passes the Tesseract
    its own ``PATH`` finds, so OCR works wherever it did in process."""

    def test_with_no_tesseract_the_bare_name_is_passed(self, monkeypatch):
        calls = stub_child_output(monkeypatch, b"T 0\n")
        monkeypatch.setattr(image.shutil, "which", lambda _name: None)
        _extract(_tiff(1))
        assert calls[0]["argv"][-1] == "tesseract"

    def test_the_child_runs_the_tesseract_it_is_given(self, monkeypatch):
        from src.extractors import image_child

        monkeypatch.setattr(image_child.pytesseract.pytesseract, "tesseract_cmd", "tesseract")
        seen: list[str] = []
        monkeypatch.setattr(
            image_child.pytesseract,
            "image_to_string",
            lambda *_a, **_k: seen.append(image_child.pytesseract.pytesseract.tesseract_cmd) or "",
        )
        image_child.extract_text(_tiff(1), "20", "0", "/opt/x/bin/tesseract")
        assert seen == ["/opt/x/bin/tesseract"]


class TestCapFrames:
    @pytest.fixture(autouse=True)
    def _fresh_counts(self):
        extractors.drain_extractor_counts()

    @pytest.mark.parametrize(
        ("cap", "message"),
        [
            ("ocr_frames", "image OCR capped at 2 of at least 3 frames"),
            (
                "ocr_frames_unreadable",
                "image OCR capped at 2 frames; the next frame could not be read",
            ),
        ],
    )
    def test_a_frame_cap_is_logged_and_counted_as_a_capped_image(
        self, cap, message, monkeypatch, caplog
    ):
        caplog.set_level("DEBUG")
        body = f"{MARKER} text".encode()
        stub_child_output(monkeypatch, f"C {cap}\n".encode() + b"T %d\n" % len(body) + body)
        result = _extract(_tiff(1), max_ocr_pages=2)
        assert (result.status, result.text) == (STATUS_SUCCESS, f"{MARKER} text")
        assert result.text_complete is False
        [line] = [r for r in caplog.records if "image OCR capped" in r.getMessage()]
        assert (line.levelno, line.getMessage()) == (logging.WARNING, message)
        counts = extractors.drain_extractor_counts()
        assert (counts["ocr_capped_images"], counts["extractor_caps"]) == (1, 0)
        assert MARKER not in caplog.text

    def test_the_text_budget_is_an_extractor_cap(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        stub_child_output(monkeypatch, b"C image_text_chars\nT 4\ntext")
        result = _extract(_tiff(1))
        assert (result.status, result.text, result.text_complete) == (STATUS_SUCCESS, "text", False)
        [line] = [r for r in caplog.records if "extractor cap" in r.getMessage()]
        assert line.levelno == logging.WARNING
        assert line.getMessage() == (
            "extractor cap image_text_chars: image OCR text cut at 10000000 chars"
        )
        counts = extractors.drain_extractor_counts()
        assert (counts["ocr_capped_images"], counts["extractor_caps"]) == (0, 1)

    @pytest.mark.parametrize("cap", ["xls_sheets", "docx_blocks", "pdf_ocr_dpi"])
    def test_another_extractors_cap_is_malformed_output(self, cap, monkeypatch):
        stub_child_output(monkeypatch, f"C {cap}\nT 0\n".encode())
        result = _extract(_tiff(1))
        assert (result.status, result.error) == (STATUS_FAILED, "ChildOutputError")

    def test_a_count_outside_the_degradation_keys_is_malformed_output(self, monkeypatch):
        stub_child_output(monkeypatch, b"N pages 1\nT 0\n")
        result = _extract(_tiff(1))
        assert (result.status, result.error) == (STATUS_FAILED, "ChildOutputError")

    def test_degradation_recorded_in_the_child_is_re_applied(self, monkeypatch, caplog):
        """What the decode or OCR records through the package helpers in
        the child crosses as ``N`` frames and is re-applied here (#1314)."""
        caplog.set_level("DEBUG")
        stub_child_output(monkeypatch, b"N text_lost 1\nN ocr_capped_images 1\nT 4\ntext")
        result = _extract(_tiff(1))
        assert (result.status, result.text, result.text_complete) == (STATUS_SUCCESS, "text", False)
        assert extractors.drain_extractor_counts()["ocr_capped_images"] == 1
        [line] = [r for r in caplog.records if "degraded in the child" in r.getMessage()]
        assert line.levelno == logging.WARNING
        assert line.getMessage() == (
            "extractor image degraded in the child: ocr_capped_images=1 text_lost=1"
        )
        assert MARKER not in caplog.text


def _main_text(pages: list[str], max_extracted_chars: int | None) -> str | None:
    """What main's in-process path stored for these OCR pages: the
    extractor joined them, the dispatcher stripped and cut the result."""
    cleaned = "\n\n".join(pages).strip()
    if not cleaned:
        return None
    return cleaned if max_extracted_chars is None else cleaned[:max_extracted_chars]


def _stub_pages(monkeypatch, pages: list[str]) -> list[int]:
    """Make Tesseract return ``pages`` in order; returns its call log."""
    from src.extractors import image_child

    calls: list[int] = []

    def ocr(*_a, **_k):
        calls.append(1)
        return pages[len(calls) - 1]

    monkeypatch.setattr(image_child.pytesseract, "image_to_string", ocr)
    return calls


# OCR page outputs: whitespace runs at either end and inside, pages that
# are only whitespace, empty pages between separators, and outputs over
# the child's budget (patched to 40 characters below), with and without
# whitespace that stripping removes.
_BUDGET = 40
_SHAPES = {
    "plain": ["abc"],
    "leading-whitespace": ["   \n\n abc"],
    "trailing-whitespace": ["abc \n\n  "],
    "interior-runs": ["a   b", "\n\n\n", "c"],
    "whitespace-only-pages": [" ", "\n", "x", "  "],
    "all-whitespace": ["  ", "\n"],
    "empty-pages-between-separators": ["a", "", "b"],
    "raw-over-stripped-under": [" " * 100 + "abc", "def" + " " * 100],
    "whitespace-page-after-text-over-raw": ["abc", " " * 100, "def"],
    "exactly-the-budget": ["x" * _BUDGET],
    "over-in-one-page": ["   " + "x" * 50],
    "over-across-pages": ["x" * 30, "y" * 30, "z" * 30],
    "over-after-whitespace-pages": [" " * 50, "x" * 30, " " * 50, "y" * 30],
}


class TestTextBudgetAfterStrip:
    """Owner decision 2026-10-08 on #1325: the child strips the joined
    OCR text, as the dispatcher does, before its 10,000,000-character
    budget applies, so any image whose stripped text is within the
    budget stores what main stored, whatever
    ``INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS`` is; past it the text is
    the stripped text cut at the budget, reported as a cap."""

    @pytest.fixture(autouse=True)
    def _fresh_counts(self):
        extractors.drain_extractor_counts()

    @pytest.mark.parametrize(
        "cap", [None, 10, _BUDGET, 2_000_000], ids=["off", "low", "at", "default"]
    )
    @pytest.mark.parametrize("shape", sorted(_SHAPES))
    def test_text_is_strip_then_cut(self, shape, cap, monkeypatch):
        from src.extractors import image_child

        pages = _SHAPES[shape]
        monkeypatch.setattr(image_child, "_MAX_TEXT_CHARS", _BUDGET)
        calls = _stub_pages(monkeypatch, pages)
        result = _extract(_tiff(len(pages)), max_ocr_pages=0, max_extracted_chars=cap)

        stripped = "\n\n".join(pages).strip()
        # The child cuts the stripped text at its budget; the dispatcher
        # strips the result again and applies its own cap.
        reference = stripped[:_BUDGET].strip() or None
        if reference is not None and cap is not None:
            reference = reference[:cap]
        assert result.text == reference
        over = len(stripped) > _BUDGET
        if not over:
            assert result.text == _main_text(pages, cap)
        assert result.text_complete is (not over and (cap is None or len(stripped) <= cap))
        counts = extractors.drain_extractor_counts()
        assert counts["extractor_caps"] == int(over) + int(
            cap is not None and len(stripped[:_BUDGET].strip()) > cap
        )
        # OCR stops after the page whose stripped prefix passed the
        # budget; otherwise every page is read.
        prefix = [
            i for i in range(1, len(pages) + 1) if len("\n\n".join(pages[:i]).strip()) > _BUDGET
        ]
        assert len(calls) == (prefix[0] if prefix else len(pages))

    def test_whitespace_past_the_real_budget_is_not_a_cut(self, monkeypatch, caplog):
        """12,000,000 characters of OCR output whose stripped text is
        short: identical to main, no cap, with the real budget."""
        caplog.set_level("DEBUG")
        pages = [" " * 6_000_000 + "a", "b" + " " * 6_000_000]
        _stub_pages(monkeypatch, pages)
        result = _extract(_tiff(2), max_ocr_pages=0, max_extracted_chars=None)
        assert result.text == _main_text(pages, None) == "a\n\nb"
        assert result.text_complete is True
        assert "extractor cap" not in caplog.text

    def test_text_past_the_real_budget_is_cut_and_reported(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        pages = [" " + "x" * 6_000_000, "y" * 6_000_000, "z"]
        calls = _stub_pages(monkeypatch, pages)
        result = _extract(_tiff(3), max_ocr_pages=0, max_extracted_chars=None)
        assert result.text == ("x" * 6_000_000 + "\n\n" + "y" * 6_000_000)[:10_000_000]
        assert result.text_complete is False
        assert len(calls) == 2
        [line] = [r for r in caplog.records if "extractor cap image_text_chars" in r.getMessage()]
        assert line.levelno == logging.WARNING


class TestProgressFramesInProcess:
    def test_a_progress_frame_follows_each_page(self, monkeypatch):
        """The in-process child writes each frame as the page finishes, as
        the real child flushes it (conftest), so each OCR is followed by
        the parent's callback, not batched at the end."""
        from src.extractors import image_child

        events: list[str] = []
        monkeypatch.setattr(
            image_child.pytesseract,
            "image_to_string",
            lambda *_a, **_k: events.append("ocr") or "page",
        )
        result = _extract(_tiff(3), on_progress=lambda: events.append("progress"))
        assert result.status == STATUS_SUCCESS
        assert events == ["ocr", "progress"] * 3


class TestModesPillowCannotSaveAsPng:
    """``pytesseract`` saves a frame as PNG before running Tesseract, and
    Pillow cannot write some modes as PNG (a CMYK JPEG failed with
    ``OSError``, #1400). The child converts those frames to RGB, per
    frame, and leaves every mode PNG can hold as it was."""

    # ``La`` is left out: Pillow cannot convert it to RGB either, and no
    # decoder produces it (only ``convert("La")`` does).
    MODES = (
        "1", "L", "P", "PA", "RGB", "RGBA", "RGBa", "RGBX", "LA", "CMYK",
        "YCbCr", "LAB", "HSV", "I", "F", "I;16", "I;16L", "I;16B", "I;16N",
    )  # fmt: skip

    @staticmethod
    def _saves_as_png(mode: str) -> bool:
        """Whether pytesseract can hand a frame of this mode to Tesseract:
        Pillow writes it as PNG, or pytesseract pastes its alpha channel
        onto white first (``RGBA``, ``LA``, ``PA``). Not pytesseract's own
        save, which takes ``LAB``'s "A" band for alpha and pastes through
        it, garbling the colours instead of failing."""
        if mode in {"RGBA", "LA", "PA"}:
            return True
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            try:
                Image.new(mode, (4, 4)).save(io.BytesIO(), format="PNG")
            except OSError:
                return False
        return True

    @staticmethod
    def _ocr_through_pytesseract_save(monkeypatch) -> list[str]:
        """Stub Tesseract but keep pytesseract's real PNG save; returns the
        mode of each frame it was given."""
        from src.extractors import image_child

        modes: list[str] = []

        def fake(frame, **_kwargs):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                with image_child.pytesseract.pytesseract.save(frame):
                    pass
            modes.append(frame.mode)
            return "text"

        monkeypatch.setattr(image_child.pytesseract, "image_to_string", fake)
        return modes

    def test_the_mode_list_covers_what_pillow_cannot_save(self):
        """Some of these modes must fail the PNG save, or the test below
        proves nothing; a new Pillow that saves more shows up here."""
        failing = {m for m in self.MODES if not self._saves_as_png(m)}
        assert {"CMYK", "YCbCr", "HSV", "F", "RGBa", "RGBX", "LAB"} <= failing

    @pytest.mark.parametrize("mode", MODES)
    def test_the_prepared_frame_always_saves_and_is_unchanged_when_it_already_did(self, mode):
        from src.extractors import image_child

        frame = Image.new(mode, (4, 4))
        prepared = image_child._png_ready(frame)
        if self._saves_as_png(mode):
            assert prepared is frame
        else:
            assert prepared.mode == "RGB"
            assert self._saves_as_png(prepared.mode)

    @pytest.mark.parametrize("fmt", ["JPEG", "TIFF"])
    def test_a_cmyk_image_is_ocrd(self, monkeypatch, fmt):
        modes = self._ocr_through_pytesseract_save(monkeypatch)
        buf = io.BytesIO()
        Image.new("CMYK", (16, 16)).save(buf, format=fmt)
        text, caps = image_child.extract_text(buf.getvalue(), "20", "0")
        assert (text, caps, modes) == ("text", [], ["RGB"])

    def test_each_frame_of_a_multipage_tiff_is_converted(self, monkeypatch):
        modes = self._ocr_through_pytesseract_save(monkeypatch)
        frames = [
            Image.new("CMYK", (16, 16)),
            Image.new("L", (16, 16)),
            Image.new("CMYK", (16, 16)),
        ]
        buf = io.BytesIO()
        frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
        text, caps = image_child.extract_text(buf.getvalue(), "20", "0")
        assert (text, caps, modes) == ("text\n\ntext\n\ntext", [], ["RGB", "L", "RGB"])

    @pytest.mark.parametrize("mode", ["RGB", "L", "P", "1"])
    def test_common_modes_reach_ocr_as_they_were(self, monkeypatch, mode):
        modes = self._ocr_through_pytesseract_save(monkeypatch)
        image_child.extract_text(_png((16, 16), mode), "20", "0")
        assert modes == [mode]

    @requires_tesseract
    def test_the_text_of_a_cmyk_image_is_read(self):
        img = Image.new("CMYK", (700, 120), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((20, 30), "SYNTHETICCMYK", fill=(0, 0, 0, 255), font_size=48)
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        text, caps = image_child.extract_text(buf.getvalue(), "20", "0", TESSERACT)
        assert "SYNTHETICCMYK" in text.replace(" ", "")
        assert caps == []


@real_child
class TestRealChild:
    def test_a_decompression_bomb_is_failed_before_any_page_is_read(self, caplog):
        """Over twice the pixel cap (set by the package import in the
        child too): rejected from the header, no page OCR'd."""
        caplog.set_level("DEBUG")
        pages: list[int] = []
        side = int((2 * extractors.GLOBAL_MAX_IMAGE_PIXELS) ** 0.5) + 100
        result = extract(
            content_type="image/png",
            filename=f"{MARKER}.png",
            payload=_png((side, side), "L"),
            on_progress=lambda: pages.append(1),
        )
        assert (result.status, result.error, result.text) == (
            STATUS_FAILED,
            "DecompressionBombError",
            None,
        )
        assert result.extractor == "image@4"
        assert pages == []
        assert MARKER not in caplog.text

    def test_the_warning_band_is_failed_before_any_page_is_read(self):
        pages: list[int] = []
        side = int((1.5 * extractors.GLOBAL_MAX_IMAGE_PIXELS) ** 0.5)
        result = extract(
            content_type="image/png",
            filename="a.png",
            payload=_png((side, side), "L"),
            on_progress=lambda: pages.append(1),
        )
        assert (result.status, result.error) == (STATUS_FAILED, "DecompressionBombWarning")
        assert pages == []

    @requires_tesseract
    def test_pages_are_ocrd_with_progress_and_the_cap_reported(self, caplog):
        caplog.set_level("DEBUG")
        extractors.drain_extractor_counts()
        pages: list[int] = []
        result = _extract(_tiff(4), max_ocr_pages=2, on_progress=lambda: pages.append(1))
        assert result.status in (STATUS_SUCCESS, STATUS_EMPTY)
        assert result.extractor == "image-ocr@4"
        assert result.text_complete is False
        assert len(pages) == 2
        assert extractors.drain_extractor_counts()["ocr_capped_images"] == 1
        assert "image OCR capped at 2 of at least 3 frames" in caplog.text

    @requires_tesseract
    def test_the_childs_tesseract_launches_are_counted(self):
        """#1236 (Codex round 1 on #1355): the child reports the
        Tesseract processes it started, one per OCR'd frame, and the
        parent adds them to its own launch count with the child's."""
        before = _runner.process_launches()
        result = _extract(_tiff(3), max_ocr_pages=3)
        assert result.status in (STATUS_SUCCESS, STATUS_EMPTY)
        # The child itself, then one Tesseract per frame read.
        assert _runner.process_launches() - before == 1 + 3

    @requires_tesseract
    def test_text_is_the_same_as_in_process(self, monkeypatch):
        """The same Tesseract on the same page: the child's text is the
        in-process extraction's, byte for byte."""
        from src.extractors import image_child

        payload = _text_page((1200, 300))
        in_process, _ = image_child.extract_text(payload, "20", "0")
        result = extract(content_type="image/png", filename="a.png", payload=payload)
        assert result.status == STATUS_SUCCESS
        assert result.text == in_process.strip()
        assert "invoice" in result.text.lower()


@real_child
@linux_only
@requires_tesseract
class TestLimitsInTheImage:
    """Run on Linux only: the address-space limit is not applied on macOS
    (``_launcher``), and the process checks read ``/proc``."""

    def test_the_address_space_limit_fails_a_decode_bomb(self, monkeypatch):
        """A 30,000,000-pixel RGBA PNG inside the pixel cap decodes to
        about 420 MiB: under a 160 MiB limit the child's decode fails
        (MemoryError), a ``failed`` row, with no page OCR'd."""
        monkeypatch.setattr(image, "CHILD_MAX_ADDRESS_SPACE_BYTES", 160 * 1024 * 1024)
        pages: list[int] = []
        start = time.monotonic()
        result = extract(
            content_type="image/png",
            filename="a.png",
            payload=_png((6000, 5000), "RGBA"),
            on_progress=lambda: pages.append(1),
        )
        assert (result.status, result.error) == (STATUS_FAILED, "MemoryError")
        assert pages == []
        assert time.monotonic() - start < 30

    def test_tesseract_inherits_the_limits_and_dies_with_the_child(self, tmp_path, monkeypatch):
        """Tesseract, started by pytesseract in the child, runs under the
        child's address-space and CPU limits; when the run ends at its
        wall clock, it is killed with the child's process group and its
        scratch (pytesseract's temporary files) is removed."""
        scratch_root = tmp_path / "scratch"
        scratch_root.mkdir()
        monkeypatch.setattr(_runner, "_TMP_DIR", str(scratch_root))
        monkeypatch.setattr(image, "child_timeout_seconds", lambda *_a: 3.0)
        seen: dict[int, str] = {}
        done = threading.Event()

        def watch():
            while not done.is_set():
                for pid, limits in _tesseracts_under(scratch_root).items():
                    seen.setdefault(pid, limits)
                time.sleep(0.01)

        watcher = threading.Thread(target=watch)
        watcher.start()
        try:
            result = extract(
                content_type="image/png",
                filename="a.png",
                payload=_text_page((6000, 5000)),
                ocr_timeout_seconds=60,
            )
        finally:
            done.set()
            watcher.join()
        assert (result.status, result.error) == (STATUS_FAILED, "ToolTimeoutError")
        assert seen, "Tesseract never started"
        for limits in seen.values():
            address_space = image.CHILD_MAX_ADDRESS_SPACE_BYTES
            assert _limit(limits, "Max address space") == (address_space, address_space)
            cpu = image.child_cpu_seconds(60)
            assert _limit(limits, "Max cpu time") == (cpu, cpu + 1)
        assert _tesseracts_under(scratch_root) == {}
        assert list(scratch_root.iterdir()) == []


def _tesseracts_under(root: Path) -> dict[int, str]:
    """Tesseract processes whose arguments name a file under ``root``,
    with their ``/proc`` limits."""
    found: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            limits = (entry / "limits").read_text()
        except OSError:
            continue
        if argv and argv[0].endswith(b"tesseract") and any(str(root).encode() in a for a in argv):
            found[int(entry.name)] = limits
    return found


def _limit(limits: str, name: str) -> tuple[int, int]:
    [line] = [line for line in limits.splitlines() if line.startswith(name)]
    soft, hard = line[len(name) :].split()[:2]
    return int(soft), int(hard)
