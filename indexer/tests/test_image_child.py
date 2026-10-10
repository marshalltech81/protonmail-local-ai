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
    IMAGE_PIXEL_CEILING_ERROR,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
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


def _jpeg(
    size: tuple[int, int],
    *,
    mode: str = "RGB",
    orientation: int | None = None,
    progressive: bool = False,
) -> bytes:
    buf = io.BytesIO()
    kwargs: dict = {"progressive": progressive}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif.tobytes()
    Image.new(mode, size, "white" if mode == "RGB" else None).save(buf, format="JPEG", **kwargs)
    return buf.getvalue()


def _header_only(kind: str, size: tuple[int, int]) -> bytes:
    """A small PNG or JPEG whose header declares ``size``: Pillow reads
    the size from the header (PNG ``IHDR``, JPEG ``SOF0``) before any
    pixel, so a refusal from the header needs no large payload."""
    import struct
    import zlib

    width, height = size
    if kind == "png":

        def chunk(kind: bytes, body: bytes) -> bytes:
            crc = struct.pack(">I", zlib.crc32(kind + body))
            return struct.pack(">I", len(body)) + kind + body + crc

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b""))
            + chunk(b"IEND", b"")
        )
    data = bytearray(_jpeg((16, 16), mode="L"))
    sof = data.index(b"\xff\xc0")
    data[sof + 5 : sof + 9] = struct.pack(">HH", height, width)
    return bytes(data)


def _mpo(size: tuple[int, int], pictures: int = 2) -> bytes:
    frames = [Image.new("RGB", size, "white") for _ in range(pictures)]
    buf = io.BytesIO()
    frames[0].save(buf, format="MPO", save_all=True, append_images=frames[1:])
    return buf.getvalue()


def _frames(payload: bytes, max_pages: str = "20") -> tuple[str, list[str], list[tuple]]:
    """Run the child's extraction in process with Tesseract stubbed;
    returns the text, the caps and each OCR'd frame's size and mode."""
    seen: list[tuple] = []

    def ocr(frame, **_kwargs):
        seen.append((frame.size, frame.mode))
        return "text"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(image_child.pytesseract, "image_to_string", ocr)
        text, caps = image_child.extract_text(payload, max_pages, "0")
    return text, caps, seen


# A small ceiling for the tests: 100 x 100 pixels.
_CEILING = 10_000


class TestPixelCeiling:
    """#1401: the image child's own pixel ceiling. At or under it, full
    resolution as before; over it, a JPEG or MPO is decoded at the
    smallest ``draft`` factor up to ``MAX_DRAFT_FACTOR`` that fits, and
    anything else is ``unsupported`` under a permanent error. The global
    cap is the cap of every other PIL consumer and is left as it was."""

    @pytest.fixture(autouse=True)
    def _ceiling(self, monkeypatch):
        monkeypatch.setattr(image_child, "CHILD_MAX_IMAGE_PIXELS", _CEILING)
        extractors.drain_extractor_counts()
        extractors.reset_attempt()

    @pytest.mark.parametrize(
        ("payload", "size"),
        [
            (_jpeg((100, 100)), (100, 100)),
            (_jpeg((10_000, 1)), (10_000, 1)),
            (_png((100, 100)), (100, 100)),
            (_tiff(1, (50, 200)), (50, 200)),
        ],
        ids=["jpeg-at", "jpeg-thin-at", "png-at", "tiff-at"],
    )
    def test_at_the_ceiling_the_image_is_read_at_full_resolution(self, payload, size):
        text, caps, seen = _frames(payload)
        assert (text, caps, [s for s, _ in seen]) == ("text", [], [size])
        assert extractors.child_degradation() == {}

    @pytest.mark.parametrize(
        ("size", "max_factor", "factor", "decoded"),
        [
            ((101, 100), 2, 2, (51, 50)),
            ((200, 200), 2, 2, (100, 100)),
            ((201, 200), 8, 4, (51, 50)),
            ((800, 800), 8, 8, (100, 100)),
            ((19_999, 2), 8, 2, (10_000, 1)),
        ],
    )
    def test_a_jpeg_over_it_is_drafted_at_the_smallest_factor_that_fits(
        self, size, max_factor, factor, decoded, monkeypatch
    ):
        monkeypatch.setattr(image_child, "MAX_DRAFT_FACTOR", max_factor)
        from PIL import ImageFile

        decodes: list[tuple[int, int]] = []
        real_load = ImageFile.ImageFile.load

        def counting_load(self):
            decodes.append(self.size)
            return real_load(self)

        monkeypatch.setattr(ImageFile.ImageFile, "load", counting_load)
        text, caps, seen = _frames(_jpeg(size))
        assert (text, caps, [s for s, _ in seen]) == ("text", [], [decoded])
        # Nothing was decoded at full size.
        assert size not in decodes
        assert extractors.child_degradation() == {extractors.CHILD_IMAGE_SCALE_FACTOR: factor}

    @pytest.mark.parametrize(
        ("orientation", "progressive", "mode", "decoded", "ocr_mode"),
        [
            (6, False, "RGB", (50, 100), "RGB"),
            (8, True, "RGB", (50, 100), "RGB"),
            (None, True, "L", (100, 50), "L"),
            (None, False, "CMYK", (100, 50), "RGB"),
        ],
        ids=["rotated", "rotated-progressive", "progressive-gray", "cmyk"],
    )
    def test_the_draft_comes_before_rotation_and_conversion(
        self, orientation, progressive, mode, decoded, ocr_mode
    ):
        payload = _jpeg((200, 100), mode=mode, orientation=orientation, progressive=progressive)
        text, caps, seen = _frames(payload)
        assert (text, caps, seen) == ("text", [], [(decoded, ocr_mode)])
        assert extractors.child_degradation()[extractors.CHILD_IMAGE_SCALE_FACTOR] == 2

    @pytest.mark.parametrize(
        "payload",
        [
            _jpeg((201, 200)),
            _png((101, 100)),
            _png((101, 100), "RGBA"),
            _tiff(1, (101, 100)),
        ],
        ids=["jpeg-past-the-factor", "png", "png-rgba", "tiff"],
    )
    def test_anything_else_over_it_is_refused_before_any_decode(self, payload, monkeypatch):
        monkeypatch.setattr(
            image_child.pytesseract, "image_to_string", lambda *_a, **_k: pytest.fail("OCR'd")
        )
        loads: list[int] = []
        monkeypatch.setattr(image_child.ImageOps, "exif_transpose", lambda *_a: loads.append(1))
        with pytest.raises(image.ImagePixelCeilingError):
            image_child.extract_text(payload, "20", "0")
        assert loads == []

    def test_a_heic_over_it_is_refused(self, monkeypatch):
        from tests.test_extractors import _heic

        with pytest.raises(image.ImagePixelCeilingError):
            image_child.extract_text(_heic((101, 100)), "20", "0")

    @pytest.mark.parametrize(
        "side",
        # The header limit is the ceiling times the largest factor
        # squared (2 x 2): Pillow's promoted warning above it, its hard
        # error above twice it.
        [int((1.5 * 4 * _CEILING) ** 0.5), int((3 * 4 * _CEILING) ** 0.5)],
        ids=["bomb-warning", "bomb-error"],
    )
    def test_pillows_bomb_exceptions_become_the_ceiling_error_without_their_text(
        self, side, caplog
    ):
        caplog.set_level("DEBUG")
        for payload in (_jpeg((side, side)), _png((side, side), "L")):
            with pytest.raises(image.ImagePixelCeilingError) as raised:
                image_child.extract_text(payload, "20", "0")
            assert raised.value.__cause__ is None
            assert raised.value.__suppress_context__
            assert "pixels" not in str(raised.value)

    def test_a_later_tiff_frame_over_it_is_refused(self, monkeypatch):
        frames = [Image.new("L", (50, 50), 255), Image.new("L", (300, 300), 255)]
        buf = io.BytesIO()
        frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
        monkeypatch.setattr(image_child.pytesseract, "image_to_string", lambda *_a, **_k: "p")
        with pytest.raises(image.ImagePixelCeilingError):
            image_child.extract_text(buf.getvalue(), "20", "0")

    def test_a_jpeg_too_thin_to_scale_is_refused(self, monkeypatch):
        """One pixel high: ``draft`` keeps the full size, which the
        returned-size check catches."""
        monkeypatch.setattr(
            image_child.pytesseract, "image_to_string", lambda *_a, **_k: pytest.fail("OCR'd")
        )
        with pytest.raises(image.ImagePixelCeilingError):
            image_child.extract_text(_jpeg((20_001, 1)), "20", "0")

    @pytest.mark.parametrize("returned", ["none", "wrong-size"])
    def test_a_draft_that_does_not_scale_is_refused(self, returned, monkeypatch):
        from PIL import JpegImagePlugin

        def draft(self, mode, size):
            if returned == "none":
                return None
            return self.mode, (0, 0, self.size[0], self.size[1])

        monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", draft)
        monkeypatch.setattr(
            image_child.pytesseract, "image_to_string", lambda *_a, **_k: pytest.fail("OCR'd")
        )
        with pytest.raises(image.ImagePixelCeilingError):
            image_child.extract_text(_jpeg((150, 150)), "20", "0")

    @pytest.mark.parametrize("payload", [_jpeg((150, 150)), _png((101, 100)), _png((900, 900))])
    def test_pillows_global_limit_is_restored(self, payload, monkeypatch):
        monkeypatch.setattr(image_child.pytesseract, "image_to_string", lambda *_a, **_k: "p")
        before = Image.MAX_IMAGE_PIXELS
        try:
            image_child.extract_text(payload, "20", "0")
        except image.ImagePixelCeilingError:
            pass
        assert Image.MAX_IMAGE_PIXELS == before == extractors.GLOBAL_MAX_IMAGE_PIXELS


def test_the_child_ceiling_is_not_the_global_cap():
    """The child's ceiling is its own constant; the global cap of every
    other PIL consumer is unchanged (#1401)."""
    assert extractors.GLOBAL_MAX_IMAGE_PIXELS == 30_000_000
    assert image.CHILD_MAX_IMAGE_PIXELS == image_child.CHILD_MAX_IMAGE_PIXELS
    assert image.MAX_DRAFT_FACTOR == image_child.MAX_DRAFT_FACTOR
    assert image.MAX_DRAFT_FACTOR in (1, 2, 4, 8)


class TestMultiPictureJpeg:
    """#1401: an MPO is read from its primary picture only; when its
    header lists more than one, ``mpo_frames`` reports the omission."""

    def test_only_the_primary_picture_is_read_and_the_rest_reported(self):
        text, caps, seen = _frames(_mpo((40, 30), pictures=3))
        assert (text, caps, seen) == ("text", [image.CAP_MPO_FRAMES], [((40, 30), "RGB")])

    def test_a_plain_jpeg_reports_nothing(self):
        assert _frames(_jpeg((40, 30)))[1] == []

    def test_an_mpo_over_the_ceiling_is_drafted_and_reported(self, monkeypatch):
        monkeypatch.setattr(image_child, "CHILD_MAX_IMAGE_PIXELS", _CEILING)
        extractors.reset_attempt()
        text, caps, seen = _frames(_mpo((200, 200)))
        assert (text, caps, seen) == ("text", [image.CAP_MPO_FRAMES], [((100, 100), "RGB")])
        assert extractors.child_degradation()[extractors.CHILD_IMAGE_SCALE_FACTOR] == 2

    def test_the_mpo_cap_is_logged_as_an_extractor_cap(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        extractors.drain_extractor_counts()
        monkeypatch.setattr(image_child.pytesseract, "image_to_string", lambda *_a, **_k: MARKER)
        result = extract(
            content_type="image/jpeg", filename=f"{MARKER}.jpg", payload=_mpo((40, 30))
        )
        assert (result.status, result.text, result.text_complete) == (STATUS_SUCCESS, MARKER, False)
        [line] = [r for r in caplog.records if "extractor cap mpo_frames" in r.getMessage()]
        assert (line.levelno, line.getMessage()) == (
            logging.WARNING,
            "extractor cap mpo_frames: image OCR read the primary picture of a "
            "multi-picture file only",
        )
        assert extractors.drain_extractor_counts()["extractor_caps"] == 1
        assert MARKER not in caplog.text


class TestScaleDownIsVisible:
    """#1401: a scale-down crosses the protocol as an allowlisted count,
    is an extractor cap in the parent (a rate-limited WARNING with the
    factor, ``text_complete`` false), and the text it read is logged at
    INFO with its count."""

    @pytest.fixture(autouse=True)
    def _ceiling(self, monkeypatch):
        monkeypatch.setattr(image_child, "CHILD_MAX_IMAGE_PIXELS", _CEILING)
        extractors.drain_extractor_counts()

    def test_the_factor_is_logged_counted_and_marks_the_text_incomplete(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        monkeypatch.setattr(image_child.pytesseract, "image_to_string", lambda *_a, **_k: MARKER)
        result = extract(
            content_type="image/jpeg", filename=f"{MARKER}.jpg", payload=_jpeg((150, 150))
        )
        assert (result.status, result.text, result.text_complete) == (STATUS_SUCCESS, MARKER, False)
        assert result.extractor == "image-ocr@6"
        lines = [
            (r.levelno, r.getMessage()) for r in caplog.records if r.name.startswith("indexer")
        ]
        assert (
            logging.WARNING,
            "extractor cap image_pixel_ceiling: image decoded at 1/2 scale (lossy) to fit the pixel ceiling",
        ) in lines
        assert (logging.INFO, f"image OCR at 1/2 scale read {len(MARKER)} chars") in lines
        assert not [line for line in lines if "degraded in the child" in line[1]]
        assert extractors.drain_extractor_counts()["extractor_caps"] == 1
        assert MARKER not in caplog.text
        # The ceiling is hardcoded, not a setting (#1418): it records no
        # configured cap value, and a change to it is a version bump.
        assert (result.ocr_pages_cap, result.digital_pages_cap, result.extracted_chars_cap) == (
            0,
            None,
            0,
        )

    def test_a_scale_down_that_read_nothing_logs_no_outcome_line(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        monkeypatch.setattr(image_child.pytesseract, "image_to_string", lambda *_a, **_k: " ")
        result = extract(content_type="image/jpeg", filename="a.jpg", payload=_jpeg((150, 150)))
        assert (result.status, result.text_complete) == (STATUS_EMPTY, False)
        assert "1/2 scale (lossy) to fit" in caplog.text
        assert "scale read" not in caplog.text

    def test_the_factor_frame_is_accepted_from_the_child(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        stub_child_output(monkeypatch, b"N image_scale_factor 4\nT 4\ntext")
        result = _extract(_tiff(1))
        assert (result.status, result.text, result.text_complete) == (STATUS_SUCCESS, "text", False)
        assert "image decoded at 1/4 scale (lossy) to fit the pixel ceiling" in caplog.text
        assert "image OCR at 1/4 scale read 4 chars" in caplog.text

    @pytest.mark.parametrize(
        "payload",
        [_png((101, 100)), _jpeg((201, 200))],
        ids=["png", "jpeg-past-the-factor"],
    )
    def test_an_image_over_the_ceiling_is_unsupported_for_good(self, payload, caplog):
        caplog.set_level("DEBUG")
        result = extract(content_type="image/png", filename=f"{MARKER}.png", payload=payload)
        assert (result.status, result.error, result.text) == (
            STATUS_UNSUPPORTED,
            IMAGE_PIXEL_CEILING_ERROR,
            None,
        )
        assert result.extractor == "image@6"
        assert IMAGE_PIXEL_CEILING_ERROR in extractors.PERMANENT_FAILURE_ERRORS
        assert "decompression bomb" not in caplog.text
        assert MARKER not in caplog.text


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
    @pytest.mark.parametrize(
        ("kind", "pixels"),
        [
            # Over twice the header limit: Pillow's hard error.
            ("png", 2 * 4 * image.CHILD_MAX_IMAGE_PIXELS + 10_000),
            ("jpeg", 2 * 4 * image.CHILD_MAX_IMAGE_PIXELS + 10_000),
            # Between the header limit and twice it: the promoted warning.
            ("jpeg", int(1.5 * 4 * image.CHILD_MAX_IMAGE_PIXELS)),
            # Over the ceiling, inside the header limit: the child's own
            # check (not a JPEG, so no scale-down).
            ("png", int(1.5 * image.CHILD_MAX_IMAGE_PIXELS)),
        ],
        ids=["bomb-error", "jpeg-bomb-error", "jpeg-bomb-warning", "png-over-ceiling"],
    )
    def test_an_image_past_the_ceiling_is_unsupported_before_any_page_is_read(
        self, kind, pixels, caplog
    ):
        """In the real child, with its real ceiling (#1401): refused from
        the header, no page OCR'd, recorded ``unsupported`` for good."""
        caplog.set_level("DEBUG")
        assert image.MAX_DRAFT_FACTOR == 2
        pages: list[int] = []
        side = int(pixels**0.5) + 1
        # Only the header carries the size: the payload stays small.
        payload = _header_only(kind, (side, side))
        result = extract(
            content_type=f"image/{kind}",
            filename=f"{MARKER}.{kind}",
            payload=payload,
            on_progress=lambda: pages.append(1),
        )
        assert (result.status, result.error, result.text) == (
            STATUS_UNSUPPORTED,
            IMAGE_PIXEL_CEILING_ERROR,
            None,
        )
        assert result.extractor == "image@6"
        assert pages == []
        assert MARKER not in caplog.text
        assert "decompression bomb" not in caplog.text

    @requires_tesseract
    def test_a_jpeg_over_the_ceiling_is_read_at_half_scale(self, caplog):
        """A real JPEG over the real ceiling, through the real child and
        Tesseract: decoded at 1/2 scale, its text read and the scale-down
        reported."""
        caplog.set_level("DEBUG")
        extractors.drain_extractor_counts()
        width = 8000
        height = image.CHILD_MAX_IMAGE_PIXELS // width + 200
        img = Image.new("L", (width, height), 255)
        ImageDraw.Draw(img).text((200, 200), "SYNTHETICHALFSCALE 4242", fill=0, font_size=160)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        result = extract(content_type="image/jpeg", filename="a.jpg", payload=buf.getvalue())
        assert result.status == STATUS_SUCCESS
        assert "SYNTHETICHALFSCALE" in (result.text or "").replace(" ", "")
        assert result.text_complete is False
        assert "image decoded at 1/2 scale (lossy) to fit the pixel ceiling" in caplog.text
        assert extractors.drain_extractor_counts()["extractor_caps"] == 1

    @requires_tesseract
    def test_pages_are_ocrd_with_progress_and_the_cap_reported(self, caplog):
        caplog.set_level("DEBUG")
        extractors.drain_extractor_counts()
        pages: list[int] = []
        result = _extract(_tiff(4), max_ocr_pages=2, on_progress=lambda: pages.append(1))
        assert result.status in (STATUS_SUCCESS, STATUS_EMPTY)
        assert result.extractor == "image-ocr@6"
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
