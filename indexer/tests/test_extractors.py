"""Tests for src/extractors/.

Covers the dispatch (MIME → module → extension fallback → unsupported),
the simple text + html extractors end-to-end, the PDF digital path
against a tiny synthesized PDF, and the safety properties the
dispatcher itself enforces (size cap, OCR gate, exception → ``failed``).

OCR-dependent paths (Tesseract, Poppler) are tested via mocks rather
than against a live binary so the test suite stays runnable on a
laptop without the Docker image's apt packages installed.
"""

from __future__ import annotations

import pytest
from src.extractors import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_TOO_LARGE,
    STATUS_UNSUPPORTED,
    ExtractionResult,
    extract,
)


class TestDispatchByMime:
    def test_text_plain_routes_to_text_extractor(self):
        result = extract(
            content_type="text/plain",
            filename="note.txt",
            payload=b"Hello there",
        )
        assert result.status == STATUS_SUCCESS
        assert result.extractor == "text"
        assert result.text == "Hello there"

    def test_text_csv_uses_text_extractor(self):
        result = extract(
            content_type="text/csv",
            filename="data.csv",
            payload=b"a,b,c\n1,2,3",
        )
        assert result.status == STATUS_SUCCESS
        assert "1,2,3" in result.text

    def test_text_html_renders_via_html_extractor(self):
        result = extract(
            content_type="text/html",
            filename="page.html",
            payload=b"<html><body><h1>Title</h1><p>Body text.</p></body></html>",
        )
        assert result.status == STATUS_SUCCESS
        assert result.extractor == "html"
        assert "Title" in result.text
        assert "Body text" in result.text

    def test_unknown_mime_with_known_extension_falls_back(self):
        # ``application/octet-stream`` is the catch-all clients use when
        # MIME detection fails. Filename extension routing must rescue
        # these.
        result = extract(
            content_type="application/octet-stream",
            filename="reading.txt",
            payload=b"text content",
        )
        assert result.status == STATUS_SUCCESS
        assert result.extractor == "text"

    def test_no_dispatch_match_returns_unsupported(self):
        result = extract(
            content_type="application/x-unknown",
            filename="mystery.bin",
            payload=b"\x00\x01",
        )
        assert result.status == STATUS_UNSUPPORTED
        assert result.text is None


class TestSafetyGates:
    def test_payload_over_max_bytes_returns_too_large(self):
        result = extract(
            content_type="text/plain",
            filename="huge.txt",
            payload=b"x" * 200,
            max_bytes=100,
        )
        assert result.status == STATUS_TOO_LARGE
        assert result.text is None
        assert "200" in (result.error or "")

    def test_image_dispatch_blocked_when_ocr_disabled(self):
        result = extract(
            content_type="image/png",
            filename="screenshot.png",
            payload=b"\x89PNG\r\n\x1a\n",
            ocr_enabled=False,
        )
        # Without OCR the image extractor has nothing useful to do —
        # downgrade to unsupported so a future OCR-enabled re-run can
        # upgrade the cached row.
        assert result.status == STATUS_UNSUPPORTED
        assert "OCR disabled" in (result.error or "")

    def test_zip_bomb_oversize_member_is_rejected(self, monkeypatch):
        """A zip whose central directory declares a member above the
        uncompressed cap surfaces as ``failed`` before the per-format
        extractor runs — defense against quadratic-blowup XML in DOCX /
        XLSX payloads. The cap is monkeypatched low here so the test
        doesn't need to forge a multi-MB zip; the production cap
        (``ZIP_MAX_UNCOMPRESSED_BYTES``) is 200 MiB.
        """
        import io
        import zipfile

        # Lower the cap so any nontrivial zip member trips it. The test
        # doesn't actually need a multi-MB payload to verify the gate.
        monkeypatch.setattr("src.extractors.ZIP_MAX_UNCOMPRESSED_BYTES", 4)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("payload.xml", b"<root>" * 50)

        result = extract(
            content_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            filename="bomb.xlsx",
            payload=buf.getvalue(),
        )
        assert result.status == STATUS_FAILED
        assert "uncompressed" in (result.error or "")

    def test_memory_error_propagates_not_cached_as_failed(self, monkeypatch):
        """``MemoryError`` is host-level pressure, not a per-payload
        extractor bug — bubble it so the runtime sees it instead of
        caching a misleading ``failed`` row that would retry on every
        reappearance of the same content_hash.
        """

        def fake_safe_import(module_name):
            def boom(payload, **opts):
                raise MemoryError("simulated OOM")

            return boom

        monkeypatch.setattr("src.extractors._safe_import", fake_safe_import)
        monkeypatch.setattr("src.extractors._IMPORT_CACHE", {})

        try:
            extract(
                content_type="text/plain",
                filename="big.txt",
                payload=b"hi",
            )
        except MemoryError:
            return
        raise AssertionError("expected MemoryError to propagate, got an ExtractionResult")

    def test_extractor_exception_becomes_failed_not_raised(self, monkeypatch):
        # Dispatcher must convert per-format exceptions into a ``failed``
        # ExtractionResult so a single malformed attachment cannot
        # dead-letter the parent message's indexing job.
        from src.extractors import _safe_import as real_safe_import

        def fake_safe_import(module_name):
            if module_name == "text":

                def boom(payload, **opts):
                    raise RuntimeError("simulated extractor crash")

                return boom
            return real_safe_import(module_name)

        monkeypatch.setattr("src.extractors._safe_import", fake_safe_import)
        # Bust the import cache so the fake gets installed.
        monkeypatch.setattr("src.extractors._IMPORT_CACHE", {})

        result = extract(
            content_type="text/plain",
            filename="boom.txt",
            payload=b"hi",
        )
        assert result.status == STATUS_FAILED
        assert "simulated extractor crash" in result.error
        assert result.extractor == "text"


class TestEmptyExtraction:
    def test_extractor_returning_empty_text_reports_empty(self):
        # ``text`` extractor on whitespace-only payload yields a
        # whitespace-stripped empty string → status="empty", not
        # "success" with a blank text field.
        result = extract(
            content_type="text/plain",
            filename="blank.txt",
            payload=b"   \n\t  ",
        )
        assert result.status == STATUS_EMPTY
        assert result.text is None


class TestExtractionResultShape:
    def test_dataclass_is_frozen(self):
        r = ExtractionResult(status=STATUS_SUCCESS, extractor="x", text="y", error=None)
        with pytest.raises(Exception):
            r.status = STATUS_EMPTY  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Per-format extractors — small synthetic fixtures rather than file
# fixtures so the tests stay readable and the test suite stays self-
# contained. OCR-dependent paths use mocks for ``pytesseract`` and
# ``pdf2image`` so the tests run without the Docker image's apt
# packages installed on the host.
# ---------------------------------------------------------------------------


class TestTextExtractorFallback:
    def test_invalid_utf8_falls_back_to_replace_decode(self):
        """``text`` extractor must not raise on ill-formed bytes — the
        chunker can still index a payload with replacement characters,
        but a hard decode failure would dead-letter the parent message.
        """
        from src.extractors.text import extract as text_extract

        # ``\xff\xfe`` is an invalid UTF-8 start byte sequence in
        # context. The replacement-decode fallback yields valid Unicode.
        payload = b"before" + b"\xff\xfe" + b"after"
        text, name = text_extract(payload)
        assert name == "text"
        assert "before" in text
        assert "after" in text


class TestHtmlExtractorFallback:
    def test_invalid_utf8_html_falls_back_to_replace_decode(self):
        from src.extractors.html import extract as html_extract

        payload = b"<html><body>" + b"\xff\xfe" + b"text</body></html>"
        text, name = html_extract(payload)
        assert name == "html"
        assert "text" in text

    def test_unclosed_style_does_not_blank_the_next_document(self):
        """Regression (#216): a shared converter carried an unclosed
        ``<style>`` into the next attachment, which came out empty."""
        from src.extractors.html import extract as html_extract

        html_extract(b"<style>unfinished")
        text, _ = html_extract(b"<p>Next document text</p>")
        assert "Next document text" in text


class TestDocxExtractor:
    def test_extracts_paragraphs_and_tables(self):
        import io

        import docx
        from src.extractors.docx import extract as docx_extract

        document = docx.Document()
        document.add_paragraph("Invoice number 12345")
        document.add_paragraph("Due date: 2024-04-30")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Vendor"
        table.cell(0, 1).text = "Amount"
        table.cell(1, 0).text = "Acme Corp"
        table.cell(1, 1).text = "$500"
        buf = io.BytesIO()
        document.save(buf)

        text, name = docx_extract(buf.getvalue())
        assert name == "docx"
        assert "Invoice number 12345" in text
        assert "Due date: 2024-04-30" in text
        assert "Vendor Amount" in text
        assert "Acme Corp $500" in text

    @staticmethod
    def _save(document) -> bytes:
        import io

        buf = io.BytesIO()
        document.save(buf)
        return buf.getvalue()

    def test_huge_grid_span_is_read_once(self):
        """Regression (#228): python-docx's ``row.cells`` repeats a cell
        once per spanned grid column, so a tiny DOCX declaring a
        million-column span made the extractor copy its text a million
        times before any character cap applied."""
        import time

        import docx
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from src.extractors.docx import extract as docx_extract

        document = docx.Document()
        cell = document.add_table(rows=1, cols=1).cell(0, 0)
        cell.text = "SYNTH_CELL"
        span = OxmlElement("w:gridSpan")
        span.set(qn("w:val"), "1000000")
        cell._tc.get_or_add_tcPr().append(span)

        started = time.monotonic()
        text, _ = docx_extract(self._save(document))
        assert time.monotonic() - started < 2.0
        assert text.count("SYNTH_CELL") == 1

    def test_merged_cells_are_not_repeated(self):
        import docx
        from src.extractors.docx import extract as docx_extract

        document = docx.Document()
        table = document.add_table(rows=3, cols=3)
        table.cell(0, 0).merge(table.cell(0, 2)).text = "ACROSS"
        table.cell(1, 0).merge(table.cell(2, 0)).text = "DOWN"
        text, _ = docx_extract(self._save(document))
        assert text.count("ACROSS") == 1
        assert text.count("DOWN") == 1

    def test_nested_and_header_footer_tables_are_read(self):
        """Regression (#226): ``cell.text`` skips nested tables, and only
        header/footer paragraphs were read, so this document extracted
        as empty."""
        import docx
        from docx.shared import Inches
        from src.extractors.docx import extract as docx_extract

        document = docx.Document()
        outer = document.add_table(rows=1, cols=1)
        outer.cell(0, 0).add_table(rows=1, cols=1).cell(0, 0).text = "NESTED_MARK"
        section = document.sections[0]
        section.header.add_table(rows=1, cols=1, width=Inches(2)).cell(0, 0).text = "HEADER_MARK"
        section.footer.add_table(rows=1, cols=1, width=Inches(2)).cell(0, 0).text = "FOOTER_MARK"
        text, _ = docx_extract(self._save(document))
        assert "NESTED_MARK" in text
        assert "HEADER_MARK" in text
        assert "FOOTER_MARK" in text

    def test_deep_nesting_stays_within_the_recursion_limit(self):
        # RecursionError escapes the dispatcher as host pressure. lxml
        # rejects XML deeper than 256 elements, which caps table nesting
        # near 80 levels, so the recursive walk stays far below the limit.
        import docx
        from src.extractors.docx import extract as docx_extract

        document = docx.Document()
        cell = document.add_table(rows=1, cols=1).cell(0, 0)
        for _ in range(60):
            cell = cell.add_table(rows=1, cols=1).cell(0, 0)
        cell.text = "DEEPEST"
        text, _ = docx_extract(self._save(document))
        assert "DEEPEST" in text

    def test_body_content_keeps_document_order(self):
        import docx
        from src.extractors.docx import extract as docx_extract

        document = docx.Document()
        document.add_paragraph("FIRST")
        document.add_table(rows=1, cols=1).cell(0, 0).text = "SECOND"
        document.add_paragraph("THIRD")
        text, _ = docx_extract(self._save(document))
        assert text.index("FIRST") < text.index("SECOND") < text.index("THIRD")

    def test_dispatcher_stamps_the_current_extractor_version(self):
        # The cache stores this name; bumping the version is what makes
        # rows written by the old walker re-extract.
        import docx
        from src.extractors import extract

        document = docx.Document()
        document.add_paragraph("versioned")
        result = extract(
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="v.docx",
            payload=self._save(document),
        )
        assert result.extractor == "docx@2"

    def test_versions_are_keyed_by_dispatch_module(self, monkeypatch):
        """The image module records ``image-ocr`` and the PDF module
        ``pdf-ocr`` / ``pdf-digital``. A version keyed by module must both
        stamp those names and recognise them as stale."""
        from src import extractors

        monkeypatch.setattr(extractors, "EXTRACTOR_VERSIONS", {"image": 2, "pdf": 3})
        assert extractors._stamp_extractor("image", "image-ocr") == "image-ocr@2"
        assert extractors._stamp_extractor("pdf", "pdf-digital") == "pdf-digital@3"
        assert extractors._stamp_extractor("text", "text") == "text"
        assert extractors.is_stale_extractor("image-ocr")
        assert not extractors.is_stale_extractor("image-ocr@2")
        assert extractors.needs_reextraction("image-ocr", "image/tiff", "scan.tif")
        assert extractors.needs_reextraction("pdf-ocr@2", "application/pdf", "a.pdf")
        assert not extractors.needs_reextraction("pdf-ocr@2", "image/tiff", "scan.tif")


class TestXlsxExtractor:
    def test_serializes_each_sheet_with_header_marker(self):
        import io

        import openpyxl
        from src.extractors.xlsx import extract as xlsx_extract

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Q1"
        ws.append(["Item", "Price"])
        ws.append(["Widget", 25])
        ws.append(["Gadget", 75])
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()

        text, name = xlsx_extract(buf.getvalue())
        assert name == "xlsx"
        assert "[Sheet: Q1]" in text
        assert "Item\tPrice" in text
        assert "Widget\t25" in text

    def test_sparse_sheet_at_worksheet_bounds_fails_promptly(self):
        """Regression (#202): read-only ``iter_rows`` pads every row out
        to the sheet's full width, so a 5 KB workbook with cells at A1
        and XFD1048576 asked for ~17 billion cell visits and stalled the
        indexing worker. A cell budget turns it into a ``failed``
        attachment instead."""
        import io
        import time

        import openpyxl
        from src.extractors import STATUS_FAILED, extract

        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = "first"
        ws["XFD1048576"] = "last"
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()

        started = time.monotonic()
        result = extract(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="sparse.xlsx",
            payload=buf.getvalue(),
        )
        assert time.monotonic() - started < 5.0
        assert result.status == STATUS_FAILED
        assert "cell budget" in (result.error or "")


class TestPdfDigitalExtractor:
    def test_extracts_text_from_minimal_digital_pdf(self):
        """A small synthetic PDF with a real text layer must round-trip
        through the digital path without invoking OCR. ``pypdf`` itself
        is the canonical PDF builder available — synthesizing a valid
        PDF byte stream by hand is too brittle, so we use pypdf to
        write and pypdf to read.
        """

        from pathlib import Path as _P

        from src.extractors.pdf import _extract_digital

        fixture = _P(__file__).parent / "fixtures" / "extractors" / "digital.pdf"
        if not fixture.exists():
            # Generate the fixture on demand the first time the test
            # runs. Subsequent runs reuse it for determinism.
            fixture.parent.mkdir(parents=True, exist_ok=True)
            from pypdf import PdfWriter
            from pypdf.generic import (
                ContentStream,
                DictionaryObject,
                NameObject,
            )

            writer = PdfWriter()
            page = writer.add_blank_page(width=612, height=792)
            # Minimal text-layer content stream: BT / Tf / Td / Tj / ET.
            stream = ContentStream(None, writer)
            stream._data = b"BT /F1 12 Tf 72 720 Td (Invoice number 42) Tj ET"
            page[NameObject("/Contents")] = stream
            # Register a basic Type 1 font so the Tf reference resolves.
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            resources = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            page[NameObject("/Resources")] = resources
            with fixture.open("wb") as f:
                writer.write(f)

        payload = fixture.read_bytes()
        text = _extract_digital(payload)
        # The fixture's content stream contains a literal "Invoice
        # number 42". Assert the digital path round-trips it so a
        # regression in the pypdf pin or in ``_extract_digital``
        # surfaces here rather than silently degrading retrieval.
        assert "Invoice number 42" in text

    def test_public_pdf_extract_accepts_long_digital_text_without_ocr(self, monkeypatch):
        from src.extractors import pdf

        monkeypatch.setattr(
            pdf,
            "_extract_digital",
            lambda payload, **_: "Invoice number 42 with enough digital text to clear threshold.",
        )
        monkeypatch.setattr(
            pdf,
            "_extract_ocr",
            lambda *a, **kw: pytest.fail("OCR should not run for digital PDFs"),
        )

        text, name = pdf.extract(b"%PDF-1.7", ocr_enabled=True)

        assert "Invoice number 42" in text
        assert name == "pdf-digital"

    def test_ocr_fallback_invoked_when_digital_too_short(self, monkeypatch):
        """When ``_extract_digital`` returns near-empty text (a scanned
        PDF), the dispatcher must call into the OCR path. Mocked here to
        avoid requiring Tesseract + Poppler at test time.
        """
        from src.extractors import pdf

        monkeypatch.setattr(pdf, "_extract_digital", lambda payload, **_: "")
        monkeypatch.setattr(
            pdf,
            "_extract_ocr",
            lambda payload, **_: "OCR'd page 1\n\nOCR'd page 2",
        )

        text, name = pdf.extract(b"%PDF-1.7 (mocked)", ocr_enabled=True, max_ocr_pages=5)
        assert name == "pdf-ocr"
        assert "OCR'd page 1" in text

    def test_ocr_fallback_failure_is_reported_as_failed(self, monkeypatch):
        """If a scanned PDF needs OCR but Poppler/Tesseract fails, the
        dispatcher should cache a failed extraction with the error
        instead of treating near-empty digital text as a successful
        no-text result.
        """
        from src.extractors import pdf

        monkeypatch.setattr(pdf, "_extract_digital", lambda payload, **_: "")

        def fail_ocr(payload, **_):
            raise RuntimeError("poppler missing")

        monkeypatch.setattr(pdf, "_extract_ocr", fail_ocr)

        result = extract(
            content_type="application/pdf",
            filename="scan.pdf",
            payload=b"%PDF-1.7",
        )

        assert result.status == STATUS_FAILED
        assert result.extractor == "pdf"
        assert result.text is None
        assert "poppler missing" in (result.error or "")

    def test_ocr_disabled_returns_digital_text_only(self, monkeypatch):
        from src.extractors import pdf

        monkeypatch.setattr(pdf, "_extract_digital", lambda payload, **_: "tiny")
        # OCR must NOT be called when ocr_enabled=False, even if digital
        # text is too short to satisfy ``_MIN_DIGITAL_CHARS``. The sentinel
        # extractor name lets the dispatcher cache an OCR-disabled row that
        # will be re-run when OCR is enabled later.
        ocr_called = []
        monkeypatch.setattr(
            pdf,
            "_extract_ocr",
            lambda *a, **kw: ocr_called.append(True) or "",
        )

        text, name = pdf.extract(b"%PDF-1.7", ocr_enabled=False, max_ocr_pages=5)
        assert text == "tiny"
        assert name == "pdf-ocr-disabled"
        assert ocr_called == []

    def test_ocr_disabled_scanned_pdf_dispatches_as_unsupported(self, monkeypatch):
        from src.extractors import pdf

        monkeypatch.setattr(pdf, "_extract_digital", lambda payload, **_: "tiny")
        monkeypatch.setattr(
            pdf,
            "_extract_ocr",
            lambda *a, **kw: pytest.fail("OCR should not run when disabled"),
        )

        result = extract(
            content_type="application/pdf",
            filename="scan.pdf",
            payload=b"%PDF-1.7",
            ocr_enabled=False,
        )

        assert result.status == STATUS_UNSUPPORTED
        assert result.text is None
        assert result.extractor is None
        assert "OCR disabled" in (result.error or "")

    def test_ocr_temp_dir_is_cleaned_after_success(self, monkeypatch, tmp_path):
        """``_extract_ocr`` must remove its per-call temp dir on success.

        Repro for the production ``ENOSPC`` cascade: ``pdf2image``
        writes one PPM per rendered page (~6 MB at 200 dpi), and the
        previous implementation passed ``output_folder="/tmp"``
        directly without cleanup, so every OCR call permanently leaked
        files into the indexer container's bounded tmpfs.
        """
        import os
        import tempfile as _tempfile_mod

        from PIL import Image
        from src.extractors import pdf

        captured: dict[str, str] = {}

        def fake_convert(payload, **kwargs):
            output_folder = str(kwargs["output_folder"])
            captured["output_folder"] = output_folder
            assert os.path.isdir(output_folder), output_folder
            # Simulate pdftoppm's spill so the test fails if cleanup
            # only removes an empty dir.
            with open(os.path.join(output_folder, "page-001.ppm"), "wb") as f:
                f.write(b"P6\n1 1\n255\n\x00\x00\x00")
            return [Image.new("RGB", (4, 4), color="white")]

        monkeypatch.setattr("pdf2image.convert_from_bytes", fake_convert)
        monkeypatch.setattr(pdf, "_ocr_dpi", lambda payload, max_ocr_pages: 200)
        monkeypatch.setattr(
            "pytesseract.image_to_string",
            lambda image, **_: "ocr text",
        )
        # Reroute ``dir="/tmp"`` to a host-side tmp_path so the test
        # works on dev machines where ``/tmp`` is the host's real /tmp
        # (and the test would otherwise leave probe files behind on
        # failure). Capture the real constructor before patching to
        # avoid re-entry.
        real_temp_dir = _tempfile_mod.TemporaryDirectory
        monkeypatch.setattr(
            pdf.tempfile,
            "TemporaryDirectory",
            lambda **kwargs: real_temp_dir(dir=str(tmp_path)),
        )

        text = pdf._extract_ocr(b"%PDF-1.7", max_ocr_pages=1)

        assert text == "ocr text"
        assert captured["output_folder"].startswith(str(tmp_path))
        assert not os.path.exists(captured["output_folder"]), (
            "TemporaryDirectory must be removed on success"
        )

    def test_ocr_temp_dir_is_cleaned_after_exception(self, monkeypatch, tmp_path):
        """Same cleanup contract on the exception path — an OCR failure
        in the middle of rendering must not leak the temp dir, otherwise
        a single bad PDF can fill tmpfs and break every subsequent OCR
        call until the container restarts.
        """
        import os
        import tempfile as _tempfile_mod

        from src.extractors import pdf

        captured: dict[str, str] = {}

        def fake_convert(payload, **kwargs):
            output_folder = str(kwargs["output_folder"])
            captured["output_folder"] = output_folder
            with open(os.path.join(output_folder, "page-001.ppm"), "wb") as f:
                f.write(b"P6\n1 1\n255\n\x00\x00\x00")
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("pdf2image.convert_from_bytes", fake_convert)
        monkeypatch.setattr(pdf, "_ocr_dpi", lambda payload, max_ocr_pages: 200)
        real_temp_dir = _tempfile_mod.TemporaryDirectory
        monkeypatch.setattr(
            pdf.tempfile,
            "TemporaryDirectory",
            lambda **kwargs: real_temp_dir(dir=str(tmp_path)),
        )

        with pytest.raises(OSError):
            pdf._extract_ocr(b"%PDF-1.7", max_ocr_pages=1)

        assert "output_folder" in captured
        assert not os.path.exists(captured["output_folder"]), (
            "TemporaryDirectory must be removed even when convert raises"
        )

    @staticmethod
    def _blank_pdf(width: float, height: float) -> bytes:
        import io as _io

        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=width, height=height)
        buf = _io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    def _capture_render(self, monkeypatch, tmp_path) -> dict:
        import tempfile as _tempfile_mod

        from PIL import Image
        from src.extractors import pdf

        captured: dict = {}

        def fake_convert(payload, **kwargs):
            captured.update(kwargs)
            return [Image.new("RGB", (4, 4), color="white")]

        monkeypatch.setattr("pdf2image.convert_from_bytes", fake_convert)
        monkeypatch.setattr("pytesseract.image_to_string", lambda image, **_: "ocr text")
        real_temp_dir = _tempfile_mod.TemporaryDirectory
        monkeypatch.setattr(
            pdf.tempfile,
            "TemporaryDirectory",
            lambda **kwargs: real_temp_dir(dir=str(tmp_path)),
        )
        return captured

    def test_ocr_renders_ordinary_pages_at_full_dpi(self, monkeypatch, tmp_path):
        from src.extractors import pdf

        captured = self._capture_render(monkeypatch, tmp_path)
        pdf._extract_ocr(self._blank_pdf(612, 792), max_ocr_pages=5)
        assert captured["dpi"] == 200

    def test_ocr_lowers_dpi_for_oversized_pages(self, monkeypatch, tmp_path):
        """Regression (#211): a 435-byte PDF with a 200-inch square page
        asked Poppler for a 40,000 x 40,000 raster (~4.8 GB) at 200 dpi,
        written to the tmpfs before Pillow's size check ran. The DPI is
        lowered so the largest page fits the pixel budget."""
        from src.extractors import pdf

        captured = self._capture_render(monkeypatch, tmp_path)
        pdf._extract_ocr(self._blank_pdf(14_400, 14_400), max_ocr_pages=5)
        side = 14_400 / 72 * captured["dpi"]
        assert 1 <= captured["dpi"] < 200
        assert side * side <= pdf._MAX_OCR_PAGE_PIXELS

    def test_page_too_large_even_at_one_dpi_fails_before_rendering(self, monkeypatch, tmp_path):
        """UserUnit scales a page up to 75,000x, past any usable DPI."""
        import io as _io

        from pypdf import PdfWriter
        from pypdf.generic import FloatObject, NameObject
        from src.extractors import pdf

        writer = PdfWriter()
        page = writer.add_blank_page(width=14_400, height=14_400)
        page[NameObject("/UserUnit")] = FloatObject(75_000)
        buf = _io.BytesIO()
        writer.write(buf)

        captured = self._capture_render(monkeypatch, tmp_path)
        with pytest.raises(ValueError, match="too large"):
            pdf._extract_ocr(buf.getvalue(), max_ocr_pages=5)
        assert captured == {}

    def test_ocr_budget_counts_rounded_pixel_sides(self, monkeypatch, tmp_path):
        """Review round 1: Poppler rounds each side up to a whole pixel, so
        a sliver page (0.14 in wide, 200,000 in tall via UserUnit) has a
        small area but rasterizes to 6 x 40M pixels at 200 dpi. The
        budget must hold for the rounded sides."""
        import io as _io
        import math

        from pypdf import PdfWriter
        from pypdf.generic import FloatObject, NameObject
        from src.extractors import pdf

        writer = PdfWriter()
        page = writer.add_blank_page(width=0.01, height=14_400)
        page[NameObject("/UserUnit")] = FloatObject(1_000)
        buf = _io.BytesIO()
        writer.write(buf)

        captured = self._capture_render(monkeypatch, tmp_path)
        pdf._extract_ocr(buf.getvalue(), max_ocr_pages=5)
        dpi = captured["dpi"]
        width_px = math.ceil(0.01 * 1_000 / 72 * dpi)
        height_px = math.ceil(14_400 * 1_000 / 72 * dpi)
        assert width_px * height_px <= pdf._MAX_OCR_PAGE_PIXELS

    def test_ocr_render_has_a_deadline(self, monkeypatch, tmp_path):
        """Regression (#211): the OCR timeout reached only Tesseract, so a
        hung Poppler render blocked the worker forever."""
        from src.extractors import pdf

        captured = self._capture_render(monkeypatch, tmp_path)
        pdf._extract_ocr(self._blank_pdf(612, 792), max_ocr_pages=5, ocr_timeout_seconds=45)
        assert captured["timeout"] == 45

    def test_unreadable_page_sizes_fail_before_rendering(self, monkeypatch, tmp_path):
        """If the page sizes cannot be read the raster size is unknown, so
        the OCR fallback fails closed rather than rendering blind."""
        from src.extractors import pdf

        captured = self._capture_render(monkeypatch, tmp_path)
        with pytest.raises(Exception):  # noqa: B017 — any pypdf parse error
            pdf._extract_ocr(b"%PDF-1.7 not a real pdf", max_ocr_pages=5)
        assert captured == {}


class TestImageExtractor:
    def test_invokes_pytesseract_with_oriented_image(self, monkeypatch):
        import io

        from PIL import Image
        from src.extractors import image as image_module

        captured = {}

        def fake_image_to_string(img, **kwargs):
            captured["called"] = True
            captured["mode"] = img.mode
            captured["kwargs"] = kwargs
            return "RECEIPT TOTAL $42.00\n"

        monkeypatch.setattr(image_module.pytesseract, "image_to_string", fake_image_to_string)

        # Build a tiny in-memory image so the extractor has real bytes
        # to load through PIL.
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color="white").save(buf, format="PNG")
        text, name = image_module.extract(buf.getvalue())

        assert captured["called"] is True
        assert "RECEIPT TOTAL" in text
        assert name == "image-ocr"

    def test_exif_orientation_rotates_image_before_ocr(self, monkeypatch):
        """An image with EXIF Orientation=6 (rotated 90° CW for display)
        must reach pytesseract post-rotation. ``ImageOps.exif_transpose``
        is the production mechanism — verify it actually fires by
        building a wide source image and asserting the OCR'd image
        comes through with the rotated dimensions."""
        import io

        from PIL import Image
        from src.extractors import image as image_module

        # Wide source image (40x10). Orientation=6 means "rotate 90° CW
        # for display", so post-transpose the image becomes 10×40.
        src = Image.new("RGB", (40, 10), color="white")
        exif = src.getexif()
        # 0x0112 is the standard Orientation tag. Pillow's
        # ``Image.Exif`` accepts integer keys directly, avoiding a new
        # piexif dependency just for the test.
        exif[0x0112] = 6
        buf = io.BytesIO()
        src.save(buf, format="JPEG", exif=exif.tobytes())

        captured: dict[str, tuple[int, int]] = {}

        def fake_image_to_string(img, **kwargs):
            captured["size"] = img.size
            return "ok"

        monkeypatch.setattr(image_module.pytesseract, "image_to_string", fake_image_to_string)

        image_module.extract(buf.getvalue())

        # Post-rotation the image is 10 wide × 40 tall — assert we did
        # not OCR the unrotated 40×10 source.
        assert captured["size"] == (10, 40)

    def test_decompression_bomb_warning_promoted_to_error(self, monkeypatch):
        """A canvas in the warning band (1×–2× the cap) must surface
        as a raised ``DecompressionBombWarning`` (promoted to error
        inside the ``warnings.catch_warnings()`` scope) so the
        dispatcher records it as ``failed`` rather than OOM'ing the
        worker. PIL raises ``DecompressionBombError`` past 2× directly,
        so we size the test image into the warning band only."""
        import io

        import pytest
        from PIL import Image
        from src.extractors import image as image_module

        # 50×50 = 2500 pixels. Cap at 1500 puts the image at 1.67× the
        # cap — inside the warning band (Error fires only past 2×). The
        # cap lives on ``Image.MAX_IMAGE_PIXELS`` since the global
        # extractors-module assignment in
        # ``indexer.extractors.__init__``; monkeypatching there scopes
        # the test override and lets pytest restore the global on
        # teardown.
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1500)

        buf = io.BytesIO()
        Image.new("RGB", (50, 50), color="white").save(buf, format="PNG")

        with pytest.raises(Image.DecompressionBombWarning):
            image_module.extract(buf.getvalue())

    def test_decompression_bomb_error_propagates(self, monkeypatch):
        """A canvas past 2× the cap must surface PIL's
        ``DecompressionBombError`` directly. The dispatcher's exception
        handler converts both this and the warning-band raise into a
        ``failed`` ExtractionResult."""
        import io

        import pytest
        from PIL import Image
        from src.extractors import image as image_module

        # 50×50 = 2500 pixels. Cap at 100 → 25× the cap → Error.
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)

        buf = io.BytesIO()
        Image.new("RGB", (50, 50), color="white").save(buf, format="PNG")

        with pytest.raises(Image.DecompressionBombError):
            image_module.extract(buf.getvalue())

    def test_extract_does_not_mutate_global_max_image_pixels(self, monkeypatch):
        """``extract()`` must not permanently change ``Image.MAX_IMAGE_PIXELS``.

        Earlier versions wrapped the body in a per-call save/restore
        because the cap was set inside ``extract()``. The cap is now
        global (set at ``indexer.extractors.__init__`` import time),
        so this test exists to catch regressions where someone
        re-introduces a per-call assignment that doesn't restore
        cleanly — that would silently bleed into pypdf's PIL usage and
        defeat the whole point of having a uniform cap.
        """
        import io

        from PIL import Image
        from src.extractors import image as image_module

        before = Image.MAX_IMAGE_PIXELS
        monkeypatch.setattr(image_module.pytesseract, "image_to_string", lambda img, **_: "ok")

        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color="white").save(buf, format="PNG")
        image_module.extract(buf.getvalue())

        assert Image.MAX_IMAGE_PIXELS == before


class TestGlobalImagePixelCap:
    """Process-wide PIL cap installed by ``indexer.extractors`` at import.

    The cap protects every PIL consumer in the indexer process, not
    just the standalone image extractor. pypdf renders embedded
    document images through PIL; openpyxl decodes chart graphics
    through PIL; both inherit the limit set here.
    """

    def test_global_cap_is_set_when_extractors_package_is_imported(self):
        from PIL import Image
        from src.extractors import GLOBAL_MAX_IMAGE_PIXELS

        # The package's ``__init__.py`` performs the assignment at
        # import time. Importing ``GLOBAL_MAX_IMAGE_PIXELS`` here also
        # triggers / confirms the import-time side effect.
        assert Image.MAX_IMAGE_PIXELS == GLOBAL_MAX_IMAGE_PIXELS

    def test_global_cap_is_meaningfully_below_pil_default(self):
        # PIL's default is roughly 89,478,485 pixels. The eval session
        # observed a 94 Mpx image admitted under that default. The
        # global cap must be materially below the default — anything
        # above ~50 Mpx defeats the purpose since the OOM-contributing
        # 94 Mpx image was already in that band.
        from src.extractors import GLOBAL_MAX_IMAGE_PIXELS

        assert GLOBAL_MAX_IMAGE_PIXELS < 50_000_000


class TestDispatcherTextCap:
    def test_max_extracted_chars_truncates_success_text(self):
        result = extract(
            content_type="text/plain",
            filename="long.txt",
            payload=b"abcdefghijklmnopqrstuvwxyz" * 100,
            max_extracted_chars=64,
        )
        assert result.status == STATUS_SUCCESS
        assert result.text is not None
        assert len(result.text) == 64

    def test_max_extracted_chars_none_does_not_truncate(self):
        payload = b"abcdefghij" * 50
        result = extract(
            content_type="text/plain",
            filename="long.txt",
            payload=payload,
            max_extracted_chars=None,
        )
        assert result.status == STATUS_SUCCESS
        assert result.text is not None
        assert len(result.text) == len(payload)
