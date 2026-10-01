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
        assert result.extractor == "text@2"
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
        assert result.extractor == "text@2"

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
        # Only the exception type is persisted (#257).
        assert result.error == "RuntimeError"
        assert result.extractor == "text@2"


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


class TestTextExtractorUnicode:
    """#234: UTF-16 text decoded as cp1252 (or, without a BOM, as UTF-8)
    came out NUL-interleaved, cached as a successful extraction, and
    no word in it could be searched."""

    WORDS = "COBALT invoice amount 1234 — résumé"

    @pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be", "utf-32", "utf-8-sig"])
    def test_bom_selects_the_decoder(self, encoding):
        import codecs

        from src.extractors.text import extract as text_extract

        payload = self.WORDS.encode(encoding)
        if encoding == "utf-16-be":
            payload = codecs.BOM_UTF16_BE + payload
        text, _ = text_extract(payload)
        assert text == self.WORDS

    @pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
    def test_bomless_utf16_is_recognised_by_its_nul_bytes(self, encoding):
        from src.extractors.text import extract as text_extract

        text, _ = text_extract(self.WORDS.encode(encoding))
        assert text == self.WORDS

    def test_short_bomless_utf16_is_recognised(self):
        """Review round 1: under ten code units, the "almost no NULs in the
        other byte" allowance rounded to zero and could never pass."""
        from src.extractors.text import extract as text_extract

        assert text_extract(b"H\x00i\x00")[0] == "Hi"
        assert text_extract(b"\x00H\x00i")[0] == "Hi"

    def test_utf8_with_a_stray_nul_stays_utf8(self):
        from src.extractors.text import extract as text_extract

        payload = "id\x00résumé line one\nline two".encode()
        text, _ = text_extract(payload)
        assert text == payload.decode("utf-8")

    def test_mime_utf16_attachment_extracts_searchable_text(self):
        """The issue's shape: Python's email package writes a BOM for
        ``charset=utf-16``, which the parser hands on as payload bytes."""
        import email
        from email.message import EmailMessage

        msg = EmailMessage()
        msg.set_content("body")
        msg.add_attachment(self.WORDS, subtype="plain", charset="utf-16", filename="report.txt")
        part = list(email.message_from_bytes(bytes(msg)).walk())[-1]
        result = extract(
            content_type=part.get_content_type(),
            filename="report.txt",
            payload=part.get_payload(decode=True),
        )
        assert result.status == STATUS_SUCCESS
        # The MIME writer appends a one-byte newline after the UTF-16
        # bytes, which decodes as one replacement character.
        assert result.text is not None and self.WORDS in result.text
        assert "\x00" not in result.text
        assert result.extractor == "text@2"


class TestStaleOcrRowsWhileOcrIsOff:
    """Review round 1: refreshing a stale OCR row with OCR off would
    overwrite its text with "OCR disabled" and clear its chunks."""

    def test_ocr_rows_are_not_stale_while_ocr_is_off(self, monkeypatch):
        from src import extractors

        monkeypatch.setattr(extractors, "EXTRACTOR_VERSIONS", {"image": 2, "pdf": 2})
        for name, module in (("image-ocr", "image"), ("pdf-ocr", "pdf")):
            assert extractors.stale_extractor_module(name) == module
            assert extractors.stale_extractor_module(name, ocr_enabled=False) is None
        # A digital PDF row needs no OCR to refresh.
        assert extractors.stale_extractor_module("pdf-digital", ocr_enabled=False) == "pdf"


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

    @staticmethod
    def _first_and_even_page_document():
        """Section 1 defines every header and footer type, with a table in
        its first-page header. Section 2 links all of them to section 1."""
        import docx
        from docx.enum.section import WD_SECTION
        from docx.shared import Inches

        document = docx.Document()
        document.settings.odd_and_even_pages_header_footer = True
        document.add_paragraph("BODY_MARK")
        first = document.sections[0]
        first.different_first_page_header_footer = True
        first.header.paragraphs[0].text = "DEFAULT_HEADER_MARK"
        first.footer.paragraphs[0].text = "DEFAULT_FOOTER_MARK"
        first_header = first.first_page_header
        first_header.paragraphs[0].text = "FIRST_HEADER_MARK"
        header_table = first_header.add_table(rows=1, cols=1, width=Inches(2))
        header_table.cell(0, 0).text = "FIRST_HEADER_TABLE_MARK"
        first.first_page_footer.paragraphs[0].text = "FIRST_FOOTER_MARK"
        first.even_page_header.paragraphs[0].text = "EVEN_HEADER_MARK"
        first.even_page_footer.paragraphs[0].text = "EVEN_FOOTER_MARK"
        second = document.add_section(WD_SECTION.NEW_PAGE)
        second.different_first_page_header_footer = True
        return document

    MARKS = (
        "DEFAULT_HEADER_MARK",
        "DEFAULT_FOOTER_MARK",
        "FIRST_HEADER_MARK",
        "FIRST_HEADER_TABLE_MARK",
        "FIRST_FOOTER_MARK",
        "EVEN_HEADER_MARK",
        "EVEN_FOOTER_MARK",
    )

    def test_first_page_and_even_page_headers_and_footers_are_read(self):
        """Regression (#299): only the default header and footer were read,
        so first-page and even-page text (and tables there) was dropped.
        Section 2 links every part to section 1, so each mark appears once."""
        from src.extractors.docx import extract as docx_extract

        text, _ = docx_extract(self._save(self._first_and_even_page_document()))
        for mark in self.MARKS:
            assert text.count(mark) == 1, mark

    def test_a_part_inherited_by_a_later_section_is_read_once(self):
        """Section 1 defines a first-page header but does not show it;
        section 2 turns the first page on and inherits it by linking."""
        import docx
        from docx.enum.section import WD_SECTION
        from src.extractors.docx import extract as docx_extract

        document = docx.Document()
        first = document.sections[0]
        first.first_page_header.paragraphs[0].text = "INHERITED_FIRST_MARK"
        first.different_first_page_header_footer = False
        second = document.add_section(WD_SECTION.NEW_PAGE)
        second.different_first_page_header_footer = True
        third = document.add_section(WD_SECTION.NEW_PAGE)
        third.different_first_page_header_footer = True
        text, _ = docx_extract(self._save(document))
        assert text.count("INHERITED_FIRST_MARK") == 1

    def test_parts_the_settings_switch_off_are_not_read(self):
        """A first-page or even-page part Word never displays (its setting
        is off in every section) is not indexed."""
        import docx
        from src.extractors.docx import extract as docx_extract

        document = docx.Document()
        section = document.sections[0]
        section.first_page_header.paragraphs[0].text = "HIDDEN_FIRST_MARK"
        section.even_page_footer.paragraphs[0].text = "HIDDEN_EVEN_MARK"
        section.different_first_page_header_footer = False
        document.settings.odd_and_even_pages_header_footer = False
        text, _ = docx_extract(self._save(document))
        assert "HIDDEN_FIRST_MARK" not in text
        assert "HIDDEN_EVEN_MARK" not in text

    def test_many_linked_sections_are_walked_linearly(self, monkeypatch):
        """python-docx resolves a linked part by recursing through every
        prior section, which is quadratic and can exceed the recursion
        limit. The extractor must read each defined part once instead."""
        import time

        import docx
        from docx.enum.section import WD_SECTION
        from docx.section import _BaseHeaderFooter
        from src.extractors.docx import extract as docx_extract

        document = docx.Document()
        document.settings.odd_and_even_pages_header_footer = True
        first = document.sections[0]
        first.different_first_page_header_footer = True
        first.first_page_header.paragraphs[0].text = "MANY_FIRST_MARK"
        first.even_page_footer.paragraphs[0].text = "MANY_EVEN_MARK"
        for _ in range(3000):
            document.add_section(WD_SECTION.NEW_PAGE).different_first_page_header_footer = True
        payload = self._save(document)

        resolutions = 0
        original = _BaseHeaderFooter._get_or_add_definition

        def counting(self):
            nonlocal resolutions
            resolutions += 1
            return original(self)

        monkeypatch.setattr(_BaseHeaderFooter, "_get_or_add_definition", counting)
        started = time.monotonic()
        text, _ = docx_extract(payload)
        assert time.monotonic() - started < 10.0
        # One resolution per defined part read (two here), none per
        # linked section.
        assert resolutions <= 6
        assert text.count("MANY_FIRST_MARK") == 1
        assert text.count("MANY_EVEN_MARK") == 1

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
        assert result.extractor == "docx@3"

    def test_docx_version_2_rows_are_stale(self):
        # docx@2 missed first-page and even-page headers/footers (#299).
        from src import extractors

        assert extractors.stale_extractor_module("docx@2") == "docx"
        assert extractors.stale_extractor_module("docx@3") is None

    def test_versions_are_keyed_by_dispatch_module(self, monkeypatch):
        """The image module records ``image-ocr`` and the PDF module
        ``pdf-ocr`` / ``pdf-digital``. A version keyed by module must both
        stamp those names and recognise them as stale."""
        from src import extractors

        monkeypatch.setattr(extractors, "EXTRACTOR_VERSIONS", {"image": 2, "pdf": 3})
        assert extractors._stamp_extractor("image", "image-ocr") == "image-ocr@2"
        assert extractors._stamp_extractor("pdf", "pdf-digital") == "pdf-digital@3"
        assert extractors._stamp_extractor("text", "text") == "text"
        assert extractors.stale_extractor_module("image-ocr") == "image"
        assert extractors.stale_extractor_module("image-ocr@2") is None
        assert extractors.stale_extractor_module("pdf-ocr@2") == "pdf"
        assert extractors.stale_extractor_module("text") is None
        assert extractors.stale_extractor_module(None) is None

    def test_module_override_ignores_the_occurrence_metadata(self):
        # Used to refresh a stale cache row from any occurrence of its bytes.
        import docx
        from src.extractors import STATUS_SUCCESS, extract

        document = docx.Document()
        document.add_paragraph("override text")
        result = extract(
            content_type="application/octet-stream",
            filename="blob.bin",
            payload=self._save(document),
            module_override="docx",
        )
        assert result.status == STATUS_SUCCESS
        assert result.extractor == "docx@3"
        assert "override text" in (result.text or "")


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
        # The budget's ValueError is recorded by type only (#257).
        assert result.error == "ValueError"


class TestPdfDigitalExtractor:
    def test_extracts_text_from_minimal_digital_pdf(self):
        """A small synthetic PDF with a real text layer must round-trip
        through the digital path without invoking OCR. ``pypdf`` itself
        is the canonical PDF builder available — synthesizing a valid
        PDF byte stream by hand is too brittle, so we use pypdf to
        write and pypdf to read.
        """

        from pathlib import Path as _P

        from src.extractors.pdf import _extract_digital_pages

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
        text = _extract_digital_pages(payload)[0]
        # The fixture's content stream contains a literal "Invoice
        # number 42". Assert the digital path round-trips it so a
        # regression in the pypdf pin or in ``_extract_digital_pages``
        # surfaces here rather than silently degrading retrieval.
        assert "Invoice number 42" in text

    def test_public_pdf_extract_accepts_long_digital_text_without_ocr(self, monkeypatch):
        from src.extractors import pdf

        monkeypatch.setattr(
            pdf,
            "_extract_digital_pages",
            lambda payload, **_: ["Invoice number 42 with enough digital text to clear threshold."],
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
        """When ``_extract_digital_pages`` returns near-empty text (a scanned
        PDF), the dispatcher must call into the OCR path. Mocked here to
        avoid requiring Tesseract + Poppler at test time.
        """
        from src.extractors import pdf

        monkeypatch.setattr(pdf, "_extract_digital_pages", lambda payload, **_: ["", ""])
        monkeypatch.setattr(
            pdf,
            "_extract_ocr",
            lambda payload, **_: {0: "OCR'd page 1", 1: "OCR'd page 2"},
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

        monkeypatch.setattr(pdf, "_extract_digital_pages", lambda payload, **_: [""])

        def fail_ocr(payload, **_):
            raise RuntimeError("poppler missing")

        monkeypatch.setattr(pdf, "_extract_ocr", fail_ocr)

        result = extract(
            content_type="application/pdf",
            filename="scan.pdf",
            payload=b"%PDF-1.7",
        )

        assert result.status == STATUS_FAILED
        assert result.extractor == "pdf@2"
        assert result.text is None
        assert result.error == "RuntimeError"

    def test_ocr_disabled_returns_digital_text_only(self, monkeypatch):
        from src.extractors import pdf

        monkeypatch.setattr(pdf, "_extract_digital_pages", lambda payload, **_: ["tiny"])
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

        monkeypatch.setattr(pdf, "_extract_digital_pages", lambda payload, **_: ["tiny"])
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
        monkeypatch.setattr(pdf, "_ocr_dpi", lambda payload, pages: 200)
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

        text = pdf._extract_ocr(b"%PDF-1.7", pages=[0])

        assert text == {0: "ocr text"}
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
        monkeypatch.setattr(pdf, "_ocr_dpi", lambda payload, pages: 200)
        real_temp_dir = _tempfile_mod.TemporaryDirectory
        monkeypatch.setattr(
            pdf.tempfile,
            "TemporaryDirectory",
            lambda **kwargs: real_temp_dir(dir=str(tmp_path)),
        )

        with pytest.raises(OSError):
            pdf._extract_ocr(b"%PDF-1.7", pages=[0])

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
        pdf._extract_ocr(self._blank_pdf(612, 792), pages=[0])
        assert captured["dpi"] == 200

    def test_ocr_lowers_dpi_for_oversized_pages(self, monkeypatch, tmp_path):
        """Regression (#211): a 435-byte PDF with a 200-inch square page
        asked Poppler for a 40,000 x 40,000 raster (~4.8 GB) at 200 dpi,
        written to the tmpfs before Pillow's size check ran. The DPI is
        lowered so the largest page fits the pixel budget."""
        from src.extractors import pdf

        captured = self._capture_render(monkeypatch, tmp_path)
        pdf._extract_ocr(self._blank_pdf(14_400, 14_400), pages=[0])
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
            pdf._extract_ocr(buf.getvalue(), pages=[0])
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
        pdf._extract_ocr(buf.getvalue(), pages=[0])
        dpi = captured["dpi"]
        width_px = math.ceil(0.01 * 1_000 / 72 * dpi)
        height_px = math.ceil(14_400 * 1_000 / 72 * dpi)
        assert width_px * height_px <= pdf._MAX_OCR_PAGE_PIXELS

    def test_ocr_render_has_a_deadline(self, monkeypatch, tmp_path):
        """Regression (#211): the OCR timeout reached only Tesseract, so a
        hung Poppler render blocked the worker forever."""
        from src.extractors import pdf

        captured = self._capture_render(monkeypatch, tmp_path)
        pdf._extract_ocr(self._blank_pdf(612, 792), pages=[0], ocr_timeout_seconds=45)
        assert 40 < captured["timeout"] <= 45

    def test_unreadable_page_sizes_fail_before_rendering(self, monkeypatch, tmp_path):
        """If the page sizes cannot be read the raster size is unknown, so
        the OCR fallback fails closed rather than rendering blind."""
        from src.extractors import pdf

        captured = self._capture_render(monkeypatch, tmp_path)
        with pytest.raises(Exception):  # noqa: B017 — any pypdf parse error
            pdf._extract_ocr(b"%PDF-1.7 not a real pdf", pages=[0])
        assert captured == {}


class TestPdfPageLevelOcr:
    """#292: the 40-character floor applied to the whole document, so a
    PDF with one digital page and one scanned page never OCR'd the
    scanned one. OCR now runs on each page whose own text layer is
    under the floor, at most ``max_ocr_pages`` of them per document.

    Poppler and Tesseract are faked: each render call and each OCR call
    is recorded, so the tests assert the work done, not only the text.
    """

    DIGITAL = "Quarterly statement for the synthetic account holder, page {n}."
    SCANNED = "SCANNED_PAGE_WORDS"

    @classmethod
    def _pdf(cls, layout: str) -> bytes:
        """One page per character: ``d`` has a digital text layer, ``s``
        has none (what pypdf sees on a scanned page)."""
        import io as _io

        from pypdf import PdfWriter
        from pypdf.generic import ContentStream, DictionaryObject, NameObject

        writer = PdfWriter()
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        for n, kind in enumerate(layout, start=1):
            page = writer.add_blank_page(width=612, height=792)
            if kind == "d":
                stream = ContentStream(None, writer)
                text = cls.DIGITAL.format(n=n).encode()
                stream._data = b"BT /F1 12 Tf 72 720 Td (" + text + b") Tj ET"
                # Indirect, as Poppler requires of a page content stream.
                page[NameObject("/Contents")] = writer._add_object(stream)
                page[NameObject("/Resources")] = DictionaryObject(
                    {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
                )
        buf = _io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    @staticmethod
    def _fake_ocr(monkeypatch, tmp_path, *, fail: bool = False) -> dict:
        """Record every render (first, last page) and OCR call."""
        import tempfile as _tempfile_mod

        from PIL import Image
        from src.extractors import pdf

        work: dict = {"renders": [], "timeouts": [], "ocr_calls": 0}

        def fake_convert(payload, **kwargs):
            first, last = kwargs["first_page"], kwargs["last_page"]
            work["renders"].append((first, last))
            work["timeouts"].append(kwargs.get("timeout"))
            return [Image.new("RGB", (4, 4), color="white") for _ in range(first, last + 1)]

        def fake_tesseract(image, **_):
            work["ocr_calls"] += 1
            if fail:
                raise RuntimeError("SYNTHETIC_OCR_MARKER")
            return f"{TestPdfPageLevelOcr.SCANNED} {work['ocr_calls']}"

        monkeypatch.setattr("pdf2image.convert_from_bytes", fake_convert)
        monkeypatch.setattr("pytesseract.image_to_string", fake_tesseract)
        real_temp_dir = _tempfile_mod.TemporaryDirectory
        monkeypatch.setattr(
            pdf.tempfile,
            "TemporaryDirectory",
            lambda **kwargs: real_temp_dir(dir=str(tmp_path)),
        )
        return work

    def _extract(self, layout: str, **kwargs) -> ExtractionResult:
        return extract(
            content_type="application/pdf",
            filename="statement.pdf",
            payload=self._pdf(layout),
            **kwargs,
        )

    def test_mixed_pdf_ocrs_its_scanned_page(self, monkeypatch, tmp_path):
        work = self._fake_ocr(monkeypatch, tmp_path)
        result = self._extract("ds")
        assert result.status == STATUS_SUCCESS
        assert result.extractor == "pdf-ocr@2"
        assert result.text is not None
        assert self.DIGITAL.format(n=1) in result.text
        assert self.SCANNED in result.text
        # Only page 2 is rendered and read; the digital page is not.
        assert work["renders"] == [(2, 2)]
        assert work["ocr_calls"] == 1

    def test_text_keeps_page_order_and_skips_digital_pages(self, monkeypatch, tmp_path):
        work = self._fake_ocr(monkeypatch, tmp_path)
        result = self._extract("dsdss")
        # One render per run of scanned pages, three pages read in all.
        assert work["renders"] == [(2, 2), (4, 5)]
        assert work["ocr_calls"] == 3
        text = result.text or ""
        positions = [
            text.index(self.DIGITAL.format(n=1)),
            text.index(f"{self.SCANNED} 1"),
            text.index(self.DIGITAL.format(n=3)),
            text.index(f"{self.SCANNED} 2"),
            text.index(f"{self.SCANNED} 3"),
        ]
        assert positions == sorted(positions)

    def test_page_cap_bounds_the_scanned_pages_per_document(self, monkeypatch, tmp_path):
        work = self._fake_ocr(monkeypatch, tmp_path)
        self._extract("d" + "s" * 30, max_ocr_pages=5)
        assert work["renders"] == [(2, 6)]
        assert work["ocr_calls"] == 5

    def test_page_cap_counts_pages_across_runs(self, monkeypatch, tmp_path):
        work = self._fake_ocr(monkeypatch, tmp_path)
        self._extract("sd" * 10, max_ocr_pages=3)
        assert work["renders"] == [(1, 1), (3, 3), (5, 5)]
        assert work["ocr_calls"] == 3

    def test_digital_pdf_renders_nothing(self, monkeypatch, tmp_path):
        work = self._fake_ocr(monkeypatch, tmp_path)
        result = self._extract("dd")
        assert result.extractor == "pdf-digital@2"
        assert work["renders"] == [] and work["ocr_calls"] == 0

    def test_scanned_pdf_still_ocrs_every_page_within_the_cap(self, monkeypatch, tmp_path):
        work = self._fake_ocr(monkeypatch, tmp_path)
        result = self._extract("sss")
        assert result.extractor == "pdf-ocr@2"
        assert work["renders"] == [(1, 3)]
        assert work["ocr_calls"] == 3

    def test_mixed_pdf_with_ocr_off_keeps_its_digital_text(self, monkeypatch, tmp_path):
        work = self._fake_ocr(monkeypatch, tmp_path)
        result = self._extract("ds", ocr_enabled=False)
        assert result.status == STATUS_SUCCESS
        assert result.extractor == "pdf-digital@2"
        assert work["renders"] == [] and work["ocr_calls"] == 0

    def test_ocr_failure_on_a_mixed_pdf_keeps_the_digital_text(self, monkeypatch, tmp_path, caplog):
        """Before #292 a mixed PDF was indexed from its text layer alone;
        a page OCR cannot read must not lose that text."""
        caplog.set_level("DEBUG")
        self._fake_ocr(monkeypatch, tmp_path, fail=True)
        result = self._extract("ds")
        assert result.status == STATUS_SUCCESS
        assert result.extractor == "pdf-digital@2"
        assert result.text == self.DIGITAL.format(n=1)
        assert "RuntimeError" in caplog.text
        assert "SYNTHETIC_OCR_MARKER" not in caplog.text

    @pytest.mark.parametrize("error", [MemoryError, RecursionError])
    def test_host_pressure_during_ocr_is_not_swallowed(self, monkeypatch, error):
        from src.extractors import pdf

        def raise_error(payload, **_):
            raise error()

        monkeypatch.setattr(pdf, "_extract_ocr", raise_error)
        with pytest.raises(error):
            self._extract("ds")

    def test_ocr_failure_on_a_scanned_pdf_is_still_failed(self, monkeypatch, tmp_path):
        self._fake_ocr(monkeypatch, tmp_path, fail=True)
        result = self._extract("ss")
        assert result.status == STATUS_FAILED
        assert result.error == "RuntimeError"

    def test_render_deadline_is_shared_by_every_run(self, monkeypatch, tmp_path):
        """The render timeout bounds the whole document, as when it was one
        Poppler call: each run gets what is left, and none starts once
        the budget is spent."""
        from src.extractors import pdf

        work = self._fake_ocr(monkeypatch, tmp_path)
        clock = {"now": 0.0}
        real_convert = __import__("pdf2image").convert_from_bytes

        def slow_convert(payload, **kwargs):
            clock["now"] += 20.0
            return real_convert(payload, **kwargs)

        monkeypatch.setattr("pdf2image.convert_from_bytes", slow_convert)
        monkeypatch.setattr(pdf.time, "monotonic", lambda: clock["now"])

        payload = self._pdf("sdsds")
        assert len(pdf._extract_ocr(payload, pages=[0, 2, 4], ocr_timeout_seconds=45)) == 3
        assert work["timeouts"] == [45, 25, 5]

        work["renders"].clear()
        clock["now"] = 0.0
        with pytest.raises(TimeoutError):
            pdf._extract_ocr(payload, pages=[0, 2, 4], ocr_timeout_seconds=30)
        assert work["renders"] == [(1, 1), (3, 3)]

    def test_ocr_time_does_not_count_against_the_render_budget(self, monkeypatch, tmp_path):
        """Review round 1: the render deadline was wall-clock, so a slow
        Tesseract page between two fast renders spent it and the next
        render was refused. Only Poppler time counts."""
        from src.extractors import pdf

        work = self._fake_ocr(monkeypatch, tmp_path)
        clock = {"now": 0.0}
        real_convert = __import__("pdf2image").convert_from_bytes
        real_tesseract = __import__("pytesseract").image_to_string

        def fast_convert(payload, **kwargs):
            clock["now"] += 1.0
            return real_convert(payload, **kwargs)

        def slow_tesseract(image, **kwargs):
            clock["now"] += 50.0
            return real_tesseract(image, **kwargs)

        monkeypatch.setattr("pdf2image.convert_from_bytes", fast_convert)
        monkeypatch.setattr("pytesseract.image_to_string", slow_tesseract)
        monkeypatch.setattr(pdf.time, "monotonic", lambda: clock["now"])

        texts = pdf._extract_ocr(self._pdf("sdsds"), pages=[0, 2, 4], ocr_timeout_seconds=45)
        assert len(texts) == 3
        assert work["renders"] == [(1, 1), (3, 3), (5, 5)]
        assert work["ocr_calls"] == 3
        assert work["timeouts"] == [45, 44, 43]

    def test_dpi_is_sized_from_the_pages_rendered(self, monkeypatch, tmp_path):
        """An oversized digital page that is never rendered must not lower
        the DPI of the scanned pages."""
        import io as _io

        from pypdf import PdfReader, PdfWriter
        from src.extractors import pdf

        mixed = PdfReader(_io.BytesIO(self._pdf("ds")))
        writer = PdfWriter()
        writer.add_page(mixed.pages[0])
        writer.pages[0].mediabox.upper_right = (14_400, 14_400)
        writer.add_page(mixed.pages[1])
        buf = _io.BytesIO()
        writer.write(buf)
        assert pdf._ocr_dpi(buf.getvalue(), [1]) == 200
        assert pdf._ocr_dpi(buf.getvalue(), [0, 1]) < 200


class TestPdfExtractorVersion:
    """#292 changes what the PDF extractor returns for the same bytes, so
    rows it wrote before the fix (unversioned) must be re-extracted."""

    @pytest.mark.parametrize("name", ["pdf-digital", "pdf-ocr", "pdf"])
    def test_pre_bump_pdf_rows_are_stale(self, name):
        from src.extractors import EXTRACTOR_VERSIONS, stale_extractor_module

        assert EXTRACTOR_VERSIONS["pdf"] == 2
        assert stale_extractor_module(name) == "pdf"

    @pytest.mark.parametrize("name", ["pdf-digital@2", "pdf-ocr@2", "pdf@2"])
    def test_current_pdf_rows_are_not_stale(self, name):
        from src.extractors import stale_extractor_module

        assert stale_extractor_module(name) is None


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


class TestMultipageTiff:
    """#231: a multipage TIFF was OCR'd from its first frame only and
    cached as a complete extraction."""

    COLORS = [(255, 255, 255), (0, 0, 0), (255, 0, 0)]

    def _frames(self, fmt: str, count: int) -> bytes:
        import io

        from PIL import Image

        frames = [Image.new("RGB", (32, 32), c) for c in self.COLORS[:count]]
        buf = io.BytesIO()
        frames[0].save(buf, format=fmt, save_all=True, append_images=frames[1:])
        return buf.getvalue()

    def _ocr_by_color(self, monkeypatch) -> list[str]:
        from src.extractors import image as image_module

        seen: list[str] = []

        def fake_ocr(img, **_kwargs):
            marker = f"PAGE_{self.COLORS.index(img.convert('RGB').getpixel((0, 0)))}"
            seen.append(marker)
            return marker

        monkeypatch.setattr(image_module.pytesseract, "image_to_string", fake_ocr)
        return seen

    def test_every_page_is_ocrd_in_order(self, monkeypatch):
        seen = self._ocr_by_color(monkeypatch)
        result = extract(
            content_type="image/tiff", filename="scan.tiff", payload=self._frames("TIFF", 3)
        )
        assert seen == ["PAGE_0", "PAGE_1", "PAGE_2"]
        assert result.status == STATUS_SUCCESS
        assert result.text is not None
        assert result.text.split() == ["PAGE_0", "PAGE_1", "PAGE_2"]
        assert result.extractor == "image-ocr@2"

    def test_pages_are_capped_by_max_ocr_pages(self, monkeypatch):
        seen = self._ocr_by_color(monkeypatch)
        extract(
            content_type="image/tiff",
            filename="scan.tiff",
            payload=self._frames("TIFF", 3),
            max_ocr_pages=2,
        )
        assert seen == ["PAGE_0", "PAGE_1"]

    def test_frames_past_the_cap_are_never_enumerated(self, monkeypatch):
        """Review round 1: ``n_frames`` walks every image directory before
        a cap applies, so a compact TIFF with thousands of them stalled
        the worker. Pages are reached by ``seek()`` up to the cap."""
        from PIL import TiffImagePlugin

        def walked(_self):
            raise AssertionError("n_frames walks the whole frame chain")

        monkeypatch.setattr(TiffImagePlugin.TiffImageFile, "n_frames", property(walked))
        seen = self._ocr_by_color(monkeypatch)
        result = extract(
            content_type="image/tiff",
            filename="scan.tiff",
            payload=self._frames("TIFF", 3),
            max_ocr_pages=2,
        )
        assert result.status == STATUS_SUCCESS
        assert seen == ["PAGE_0", "PAGE_1"]

    def test_a_later_page_over_the_pixel_cap_fails_before_decoding(self, monkeypatch):
        """Review round 1: ``Image.open`` checks only the first frame's
        size; a later oversized page must not be decoded."""
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (32, 32), (255, 255, 255)).save(
            buf, format="TIFF", save_all=True, append_images=[Image.new("RGB", (400, 400))]
        )
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10_000)
        seen = self._ocr_by_color(monkeypatch)
        result = extract(content_type="image/tiff", filename="scan.tiff", payload=buf.getvalue())
        assert result.status == STATUS_FAILED
        assert seen == ["PAGE_0"]

    def test_animated_gif_is_still_one_page(self, monkeypatch):
        # Animation frames are not pages; OCR'ing each would multiply the
        # work for no new text.
        seen = self._ocr_by_color(monkeypatch)
        extract(content_type="image/gif", filename="a.gif", payload=self._frames("GIF", 3))
        assert seen == ["PAGE_0"]


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


class TestMailContentStaysOutOfLogsAndErrors:
    """#257: attachment filenames, MIME types, zip member names and raw
    library exception text are sender-controlled. None of them may reach
    the logs or the ``error`` persisted to
    ``attachment_extractions.extraction_error``."""

    FILENAME = "SYNTHETIC_FILENAME_MARKER.pdf"

    @staticmethod
    def _assert_absent(marker, caplog, result):
        assert marker not in caplog.text
        assert marker not in (result.error or "")

    def _install_failing_extractor(self, monkeypatch, exc):
        def fake_safe_import(module_name):
            def boom(payload, **opts):
                raise exc

            return boom

        monkeypatch.setattr("src.extractors._safe_import", fake_safe_import)
        monkeypatch.setattr("src.extractors._IMPORT_CACHE", {})

    def test_extractor_exception_logs_and_persists_type_only(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        self._install_failing_extractor(monkeypatch, ValueError("SYNTHETIC_EXC_MARKER"))

        result = extract(content_type="application/pdf", filename=self.FILENAME, payload=b"x")

        assert result.status == STATUS_FAILED
        assert result.error == "ValueError"
        assert "ValueError" in caplog.text
        self._assert_absent("SYNTHETIC_EXC_MARKER", caplog, result)
        self._assert_absent("SYNTHETIC_FILENAME_MARKER", caplog, result)

    def test_no_extractor_error_omits_filename_and_mime(self, caplog):
        caplog.set_level("DEBUG")
        result = extract(
            content_type="application/x-SYNTHETIC_MIME_MARKER",
            filename="SYNTHETIC_FILENAME_MARKER.bin",
            payload=b"x",
        )

        assert result.status == STATUS_UNSUPPORTED
        assert result.error
        self._assert_absent("SYNTHETIC_FILENAME_MARKER", caplog, result)
        self._assert_absent("SYNTHETIC_MIME_MARKER", caplog, result)

    def test_truncation_log_omits_filename(self, caplog):
        caplog.set_level("DEBUG")
        result = extract(
            content_type="text/plain",
            filename="SYNTHETIC_FILENAME_MARKER.txt",
            payload=b"abcdefghij" * 20,
            max_extracted_chars=16,
        )

        assert result.status == STATUS_SUCCESS
        assert "truncated" in caplog.text
        self._assert_absent("SYNTHETIC_FILENAME_MARKER", caplog, result)

    def test_zip_member_name_is_not_quoted(self, monkeypatch, caplog):
        import io
        import zipfile

        caplog.set_level("DEBUG")
        monkeypatch.setattr("src.extractors.ZIP_MAX_UNCOMPRESSED_BYTES", 4)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("SYNTHETIC_MEMBER_MARKER", b"<root>" * 50)

        result = extract(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="SYNTHETIC_FILENAME_MARKER.xlsx",
            payload=buf.getvalue(),
        )

        assert result.status == STATUS_FAILED
        assert "uncompressed" in (result.error or "")
        assert "300" in (result.error or "")
        self._assert_absent("SYNTHETIC_MEMBER_MARKER", caplog, result)
        self._assert_absent("SYNTHETIC_FILENAME_MARKER", caplog, result)

    def test_pypdf_page_failure_logs_type_only(self, monkeypatch, caplog):
        from src.extractors import pdf

        caplog.set_level("DEBUG")

        class BadPage:
            def extract_text(self):
                raise ValueError("SYNTHETIC_PYPDF_MARKER")

        class FakeReader:
            def __init__(self, stream):
                self.pages = [BadPage()]

        monkeypatch.setattr(pdf.pypdf, "PdfReader", FakeReader)

        assert pdf._extract_digital_pages(b"%PDF-1.7") == [""]
        assert "ValueError" in caplog.text
        assert "SYNTHETIC_PYPDF_MARKER" not in caplog.text

    def test_ocr_fallback_failure_logs_and_persists_type_only(self, monkeypatch, caplog):
        from src.extractors import pdf

        caplog.set_level("DEBUG")
        monkeypatch.setattr(pdf, "_extract_digital_pages", lambda payload, **_: [""])

        def fail_ocr(payload, **_):
            raise RuntimeError("SYNTHETIC_OCR_MARKER")

        monkeypatch.setattr(pdf, "_extract_ocr", fail_ocr)

        result = extract(content_type="application/pdf", filename=self.FILENAME, payload=b"x")

        assert result.status == STATUS_FAILED
        assert result.error == "RuntimeError"
        assert "PDF OCR fallback failed" in caplog.text
        self._assert_absent("SYNTHETIC_OCR_MARKER", caplog, result)
        self._assert_absent("SYNTHETIC_FILENAME_MARKER", caplog, result)
