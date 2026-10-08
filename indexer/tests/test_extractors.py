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

import logging
from collections.abc import Callable

import pytest
from src.extractors import (
    DOCX_PACKAGE_BUDGET_ERROR,
    PPTX_PACKAGE_BUDGET_ERROR,
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
        assert result.extractor == "text@3"
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
        assert result.extractor == "text@3"

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

    def test_default_max_bytes_matches_indexer_attachment_default(self):
        # A caller that omits max_bytes gets the same cap as the indexer's
        # INDEXER_ATTACHMENT_MAX_BYTES default (#780).
        import inspect

        from src import main

        default = inspect.signature(extract).parameters["max_bytes"].default
        assert default == main._DEFAULT_ATTACHMENT_MAX_BYTES == 32 * 1024 * 1024

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
        assert result.extractor == "text@3"


class TestFailedOutcomesAreLogged:
    """#871: a ``failed`` extraction drops the attachment out of search,
    so it logs a WARNING naming the extractor module and the exception
    type; never the filename, the member names or the exception text."""

    @pytest.fixture(autouse=True)
    def _fresh_counts(self):
        # The warning window is reset per test by ``conftest``.
        from src import extractors

        extractors.drain_extractor_counts()

    def test_failed_warnings_are_rate_limited(self, monkeypatch, caplog):
        """Review round 1 (security): many distinct malformed attachments
        each logged a WARNING. The first ``_WARNINGS_PER_WINDOW`` per
        window are logged; the rest are counted for the aggregate. The
        extraction results are unchanged."""
        from src import extractors, rate_limited_log

        caplog.set_level("INFO")
        clock = {"now": 1000.0}
        monkeypatch.setattr(rate_limited_log.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(extractors._LINE_BUDGET, "limit", 2)

        def boom(payload, **opts):
            raise ValueError("SYNTHETIC_EXC_MARKER")

        monkeypatch.setattr(extractors, "_safe_import", lambda module_name: boom)

        def failures(n):
            return [
                extract(content_type="text/plain", filename="a.txt", payload=b"%d" % i)
                for i in range(n)
            ]

        def warnings():
            return [r for r in caplog.records if r.name == "indexer.extractor"]

        results = failures(5)
        assert {r.status for r in results} == {STATUS_FAILED}
        assert {r.error for r in results} == {"ValueError"}
        assert len(warnings()) == 2
        assert extractors.drain_extractor_counts()["warnings_suppressed"] == 3

        # Within the window the budget stays spent.
        clock["now"] += extractors._WARNING_WINDOW_SECS - 1
        failures(1)
        assert len(warnings()) == 2
        # A new window logs again.
        clock["now"] += 2
        failures(3)
        assert len(warnings()) == 4
        assert extractors.drain_extractor_counts()["warnings_suppressed"] == 2
        assert "SYNTHETIC_EXC_MARKER" not in caplog.text

    def test_extractor_exception_logs_a_warning(self, monkeypatch, caplog):
        caplog.set_level("INFO")

        def fake_safe_import(module_name):
            def boom(payload, **opts):
                raise ValueError("SYNTHETIC_EXC_MARKER")

            return boom

        monkeypatch.setattr("src.extractors._safe_import", fake_safe_import)
        monkeypatch.setattr("src.extractors._IMPORT_CACHE", {})

        result = extract(
            content_type="text/plain",
            filename="SYNTHETIC_FILENAME_MARKER.txt",
            payload=b"SYNTHETIC_TEXT_MARKER",
        )

        assert result == ExtractionResult(
            status=STATUS_FAILED, extractor="text@3", text=None, error="ValueError"
        )
        [record] = [r for r in caplog.records if r.name == "indexer.extractor"]
        assert record.levelname == "WARNING"
        assert record.getMessage() == "extractor text failed (dispatch_via=mime): ValueError"
        for marker in (
            "SYNTHETIC_EXC_MARKER",
            "SYNTHETIC_FILENAME_MARKER",
            "SYNTHETIC_TEXT_MARKER",
        ):
            assert marker not in caplog.text

    def test_zip_budget_failure_logs_a_warning(self, monkeypatch, caplog):
        import io
        import zipfile

        caplog.set_level("INFO")
        monkeypatch.setattr("src.extractors.ZIP_MAX_UNCOMPRESSED_BYTES", 4)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("SYNTHETIC_MEMBER_MARKER", b"<root>" * 50)

        result = extract(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="SYNTHETIC_FILENAME_MARKER.xlsx",
            payload=buf.getvalue(),
        )

        assert result == ExtractionResult(
            status=STATUS_FAILED,
            extractor="xlsx@6",
            text=None,
            error="zip member declares 300 uncompressed bytes (cap 4)",
        )
        [record] = [r for r in caplog.records if r.name == "indexer.extractor"]
        assert record.levelname == "WARNING"
        assert record.getMessage() == (
            "extractor xlsx failed (dispatch_via=mime): zip uncompressed-size cap exceeded"
        )
        for marker in ("SYNTHETIC_MEMBER_MARKER", "SYNTHETIC_FILENAME_MARKER"):
            assert marker not in caplog.text

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"content_type": "text/plain", "filename": "a.txt", "payload": b"words"},
            {"content_type": "text/plain", "filename": "a.txt", "payload": b"   "},
            {"content_type": "application/x-unknown", "filename": "a.bin", "payload": b"x"},
            {"content_type": "text/plain", "filename": "a.txt", "payload": b"xx", "max_bytes": 1},
            {
                "content_type": "image/png",
                "filename": "a.png",
                "payload": b"x",
                "ocr_enabled": False,
            },
        ],
    )
    def test_other_outcomes_log_no_failure_warning(self, caplog, kwargs):
        caplog.set_level("INFO")
        assert extract(**kwargs).status != STATUS_FAILED
        assert "failed" not in caplog.text


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
        assert result.extractor == "text@3"


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

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            # Valid multi-byte UTF-8 decodes unchanged.
            (b"<p>caf\xc3\xa9 na\xc3\xafve</p>", "café naïve\n"),
            # Each invalid byte, and a truncated sequence, becomes one
            # U+FFFD while the valid text around it is kept.
            (
                b"<html><body><p>caf\xc3\xa9 \xff\xfe text \xe2\x82</p></body></html>",
                "café �� text �\n",
            ),
        ],
        ids=["valid-utf8", "malformed-utf8"],
    )
    def test_decoded_text_is_pinned(self, payload, expected):
        """#844: the extractor's text for valid and malformed UTF-8 is
        fixed, so simplifying the decode needs no EXTRACTOR_VERSIONS bump."""
        from src.extractors.html import extract as html_extract

        assert html_extract(payload) == (expected, "html")

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
        assert result.extractor == "docx@7"

    def test_docx_version_2_rows_are_stale(self):
        # docx@2 missed first-page and even-page headers/footers (#299).
        from src import extractors

        assert extractors.stale_extractor_module("docx@2") == "docx"
        assert extractors.stale_extractor_module("docx@7") is None

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


def _xlsx_bytes(rows: list[list[object]]) -> bytes:
    """A synthetic one-sheet workbook. openpyxl writes string cells as
    inline strings; see ``_shared_string_xlsx`` for shared ones."""
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def _shared_string_xlsx(shared: str, refs: int, *, tail: str | None = None) -> bytes:
    """A synthetic workbook whose cells A1..A<refs> all reference one
    shared string, stored once in ``xl/sharedStrings.xml``, followed by
    an optional inline-string ``tail`` row. openpyxl never writes
    shared strings, so its output is rewritten into that shape."""
    import io
    import zipfile
    from xml.sax.saxutils import escape

    rows = [f'<row r="{r}"><c r="A{r}" t="s"><v>0</v></c></row>' for r in range(1, refs + 1)]
    if tail is not None:
        rows.append(
            f'<row r="{refs + 1}"><c r="A{refs + 1}" t="inlineStr"><is><t>{escape(tail)}</t>'
            "</is></c></row>"
        )
    sheet_data = "<sheetData>" + "".join(rows) + "</sheetData>"
    sst = (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{refs}" uniqueCount="1"><si><t xml:space="preserve">{escape(shared)}</t></si></sst>'
    )
    base = zipfile.ZipFile(io.BytesIO(_xlsx_bytes([["placeholder"]])))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as rebuilt:
        for info in base.infolist():
            data = base.read(info).decode()
            if info.filename == "xl/worksheets/sheet1.xml":
                start, end = data.index("<sheetData>"), data.index("</sheetData>")
                data = data[:start] + sheet_data + data[end + len("</sheetData>") :]
                data = data.replace(
                    '<dimension ref="A1:A1"/>', f'<dimension ref="A1:A{refs + 1}"/>'
                )
            elif info.filename == "[Content_Types].xml":
                data = data.replace(
                    "</Types>",
                    '<Override PartName="/xl/sharedStrings.xml" ContentType="application/'
                    'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>',
                )
            elif info.filename == "xl/_rels/workbook.xml.rels":
                data = data.replace(
                    "</Relationships>",
                    '<Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    'relationships/sharedStrings" Target="sharedStrings.xml" Id="rIdSst"/>'
                    "</Relationships>",
                )
            rebuilt.writestr(info.filename, data)
        rebuilt.writestr("xl/sharedStrings.xml", sst)
    return out.getvalue()


def _rewrite_sheet_xml(payload: bytes, edit) -> bytes:
    """Apply ``edit`` to the first worksheet's XML, to give a synthetic
    workbook metadata openpyxl would not write itself."""
    import io
    import zipfile

    base = zipfile.ZipFile(io.BytesIO(payload))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as rebuilt:
        for info in base.infolist():
            data = base.read(info)
            if info.filename == "xl/worksheets/sheet1.xml":
                data = edit(data.decode()).encode()
            rebuilt.writestr(info.filename, data)
    return out.getvalue()


def _rows_until_cell_budget(*, first_row_cells: int) -> int:
    """Rows yielded before the cell budget runs out: one row of
    ``first_row_cells`` cells, then empty rows."""
    from src.extractors import xlsx

    spent = xlsx._ROW_COST + first_row_cells
    return 1 + (xlsx._MAX_EXPANDED_CELLS - spent) // xlsx._ROW_COST + 1


def _count_parsed_rows(monkeypatch) -> list[int]:
    """Count the worksheet rows openpyxl's read-only parser hands back."""
    from openpyxl.worksheet._read_only import ReadOnlyWorksheet

    calls = [0]
    original = ReadOnlyWorksheet._get_row

    def counting(self, *args, **kwargs):
        calls[0] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ReadOnlyWorksheet, "_get_row", counting)
    return calls


class TestXlsxSharedStringBudget:
    """#294: one long shared string referenced by many cells expands a
    small workbook into an unbounded string. The text budget stops the
    traversal, not just the returned length."""

    def test_repeated_shared_string_stops_at_the_text_budget(self, monkeypatch):
        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_TEXT_CHARS", 100_000)
        payload = _shared_string_xlsx("S" * 32_767, 256)
        assert len(payload) < 20_000
        rows = _count_parsed_rows(monkeypatch)

        text, _ = xlsx.extract(payload)

        assert len(text) <= 100_000
        assert text.startswith("[Sheet: Sheet]\nSSS")
        # 256 rows would expand to ~8.4M characters; the walk stops on
        # the fourth row, where the budget runs out.
        assert rows[0] == 4

    def test_whitespace_shared_string_is_charged_before_stripping(self, monkeypatch):
        """Stripping scans the whole value; charging only what survives
        the strip would let blank cells cost unbounded work."""
        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_TEXT_CHARS", 100_000)
        payload = _shared_string_xlsx(" " * 32_767, 256, tail="tail")
        rows = _count_parsed_rows(monkeypatch)

        text, _ = xlsx.extract(payload)

        assert "tail" not in text
        # Three full blank values and a fourth sliced to the budget's last
        # characters, where the walk stops because that value was cut
        # (review round 1 on #917; a fifth row was parsed before): 100,000
        # characters scanned in all.
        assert rows[0] == 4

    @pytest.mark.parametrize(
        ("room", "expected"),
        [(4, "abc"), (6, "abcde"), (7, "abcdef"), (8, "abcdef"), (1, None)],
    )
    def test_value_crossing_the_budget_keeps_its_prefix(self, monkeypatch, room, expected):
        """The separator is reserved before slicing, so a value that
        crosses the limit is emitted up to it rather than dropped."""
        from src.extractors import xlsx

        header = "[Sheet: Sheet]"
        monkeypatch.setattr(xlsx, "_MAX_TEXT_CHARS", len(header) + 2 + room)
        text, _ = xlsx.extract(_xlsx_bytes([["abcdef"], ["next"]]))
        # A sheet with nothing but its header line is skipped.
        assert text == ("" if expected is None else f"{header}\n{expected}")
        assert len(text) <= xlsx._MAX_TEXT_CHARS

    def test_default_budget_bounds_a_large_expansion(self):
        import time

        from src.extractors import xlsx

        # ~65M characters if every reference were expanded.
        payload = _shared_string_xlsx("S" * 32_767, 2_000)
        started = time.perf_counter()
        text, _ = xlsx.extract(payload)
        assert time.perf_counter() - started < 5.0
        assert len(text) <= xlsx._MAX_TEXT_CHARS

    def test_workbook_under_the_budget_is_unchanged(self):
        from src.extractors import xlsx

        payload = _shared_string_xlsx("S" * 32_767, 3, tail="tail")
        text, _ = xlsx.extract(payload)
        assert text == "[Sheet: Sheet]\n" + "\n".join(["S" * 32_767] * 3 + ["tail"])

    def test_dispatcher_stamps_the_xlsx_version(self):
        from src import extractors

        result = extract(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="book.xlsx",
            payload=_xlsx_bytes([["versioned"]]),
        )
        assert result.extractor == "xlsx@6"
        assert extractors.stale_extractor_module("xlsx") == "xlsx"
        assert extractors.stale_extractor_module("xlsx@5") == "xlsx"
        assert extractors.stale_extractor_module("xlsx@6") is None


def _titled_xlsx(sheets: list[tuple[str, list[list[object]]]]) -> bytes:
    """A synthetic workbook with one sheet per ``(title, rows)`` pair.
    openpyxl refuses titles it considers invalid, so each sheet is
    written under a placeholder name and ``xl/workbook.xml`` is
    rewritten to carry the real title."""
    import io
    import zipfile
    from xml.sax.saxutils import quoteattr

    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for index, (_, rows) in enumerate(sheets):
        ws = wb.create_sheet(f"placeholder{index}")
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    base = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as rebuilt:
        for info in base.infolist():
            data = base.read(info)
            if info.filename == "xl/workbook.xml":
                text = data.decode()
                for index, (title, _) in enumerate(sheets):
                    text = text.replace(f'name="placeholder{index}"', f"name={quoteattr(title)}")
                data = text.encode()
            rebuilt.writestr(info.filename, data)
    return out.getvalue()


class TestXlsxSheetTitleBudget:
    """#435: the ``[Sheet: name]`` header was built from the full title
    before the text budget was charged, so a title larger than the
    budget left was copied whole only to be dropped. The returned text
    was already bounded; the copy was bounded only by lxml's 10 MB
    limit on one XML attribute, which openpyxl happens to inherit."""

    # The header's fixed characters plus the blank line charged with it.
    _OVERHEAD = len("[Sheet: ]") + 2

    @pytest.mark.parametrize(
        ("title_len", "kept"),
        [(10, True), (11, True), (12, False), (13, False), (500, False)],
    )
    def test_title_at_the_budget_boundary(self, monkeypatch, title_len, kept):
        """Pins the output on both sides of the boundary: a header that
        leaves no budget for a value drops its sheet, as before."""
        from src.extractors import xlsx

        first = "[Sheet: first]\nfirst"
        # The first sheet leaves exactly 12 + overhead; a value then
        # needs one character for itself and one for its newline.
        monkeypatch.setattr(xlsx, "_MAX_TEXT_CHARS", len(first) + 2 + 12 + self._OVERHEAD + 1)
        title = "T" * title_len
        text, _ = xlsx.extract(_titled_xlsx([("first", [["first"]]), (title, [["v"]])]))

        assert text == (f"{first}\n\n[Sheet: {title}]\nv" if kept else first)
        assert len(text) <= xlsx._MAX_TEXT_CHARS

    def test_title_past_the_budget_is_not_copied(self, monkeypatch):
        import io
        import time
        import tracemalloc

        import openpyxl
        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_TEXT_CHARS", 1_000)
        title_len = 5_000_000
        payload = _titled_xlsx([("first", [["first"]]), ("T" * title_len, [["v"]])])
        assert len(payload) < 20_000
        # Loading parses the title (#428); only the walk is measured.
        workbook = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
        try:
            tracemalloc.start()
            started = time.perf_counter()
            text = xlsx._serialize(workbook)
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        finally:
            workbook.close()

        assert text == "[Sheet: first]\nfirst"
        assert elapsed < 5.0
        # Copying the title would allocate at least its length.
        assert peak < title_len // 10

    def test_ordinary_titles_are_unchanged(self):
        from src.extractors import xlsx

        payload = _titled_xlsx([("Budget 2026", [["a", 1]]), ("Notes", [["b"]])])
        text, _ = xlsx.extract(payload)
        assert text == "[Sheet: Budget 2026]\na\t1\n\n[Sheet: Notes]\nb"


class TestXlsxColumnPositions:
    """#296: dropping empty cells shifted later values left, so a value
    in one column read as belonging to another."""

    _ROWS: list[list[object]] = [
        ["Project", "Approved", "Paid"],
        ["alpha", 500, None],
        ["beta", None, 500],
        [None, None, "gamma"],
        ["  ", "delta", None],
    ]

    def test_each_value_stays_under_its_column(self):
        from src.extractors import xlsx

        text, _ = xlsx.extract(_xlsx_bytes(self._ROWS))

        assert text.split("\n")[1:] == [
            "Project\tApproved\tPaid",
            "alpha\t500",
            "beta\t\t500",
            "\t\tgamma",
            "\tdelta",
        ]
        header = self._ROWS[0]
        for source, line in zip(self._ROWS[1:], text.split("\n")[2:], strict=True):
            for column, value in enumerate(line.split("\t")):
                expected = source[column]
                assert value == ("" if expected is None else str(expected).strip()), header[column]

    def test_tabs_and_line_breaks_inside_a_value_do_not_shift_columns(self):
        from src.extractors import xlsx

        text, _ = xlsx.extract(_xlsx_bytes([["a\tb", "c\nd", "e\r\nf", "last"]]))
        assert text.split("\n")[1:] == ["a b\tc d\te  f\tlast"]

    def test_positions_survive_attachment_chunking(self):
        from src.chunker import chunk_message
        from src.extractors import xlsx

        text, _ = xlsx.extract(_xlsx_bytes(self._ROWS))
        chunks = chunk_message(message_pk="m::a", body_text=text)
        joined = "\n".join(c.text for c in chunks)
        assert "beta\t\t500" in joined
        assert "\t\tgamma" in joined

    def test_empty_cells_are_charged_to_the_text_budget(self, monkeypatch):
        """Empty cells now emit a tab each, so they count against the
        budget that bounds the returned length."""
        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_TEXT_CHARS", 1_000)
        text, _ = xlsx.extract(_xlsx_bytes([["x"] + [None] * 5_000 + ["y"]]))
        assert len(text) <= 1_000
        assert "y" not in text


class TestXlsxStaleDimensions:
    """#305: read-only iteration trusted the worksheet's declared
    dimension, so cells outside a stale, undersized one were dropped
    and the extraction still reported success."""

    @staticmethod
    def _declare(payload: bytes, ref: str) -> bytes:
        def edit(xml: str) -> str:
            start = xml.index("<dimension ref=")
            end = xml.index("/>", start) + 2
            return xml[:start] + f'<dimension ref="{ref}"/>' + xml[end:]

        return _rewrite_sheet_xml(payload, edit)

    _ROWS: list[list[object]] = [
        ["Invoice"],
        ["PAYMENTZX829", 1250],
        [],
        [None, None, None, "late"],
    ]

    def test_undersized_dimension_keeps_later_rows_and_columns(self):
        from src.extractors import xlsx

        text, _ = xlsx.extract(self._declare(_xlsx_bytes(self._ROWS), "A1:A1"))
        assert text.split("\n")[1:] == ["Invoice", "PAYMENTZX829\t1250", "\t\t\tlate"]

    def test_dispatcher_reports_the_recovered_cells(self):
        from src.extractors import extract

        result = extract(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="stale.xlsx",
            payload=self._declare(_xlsx_bytes(self._ROWS), "A1:A1"),
        )
        assert result.status == STATUS_SUCCESS
        assert "PAYMENTZX829\t1250" in (result.text or "")
        assert "late" in (result.text or "")

    def test_offset_dimension_keeps_cells_before_it(self):
        from src.extractors import xlsx

        text, _ = xlsx.extract(self._declare(_xlsx_bytes(self._ROWS), "B2:B2"))
        assert text.split("\n")[1:] == ["Invoice", "PAYMENTZX829\t1250", "\t\t\tlate"]

    def test_correct_and_oversized_dimensions_read_the_same(self):
        from src.extractors import xlsx

        payload = _xlsx_bytes(self._ROWS)
        expected, _ = xlsx.extract(payload)
        oversized, _ = xlsx.extract(self._declare(payload, "A1:Z500"))
        assert oversized == expected

    def test_rows_spanning_every_column_stop_at_the_text_budget(self, monkeypatch):
        """Without the declared width, a row is padded to its own last
        cell, so many rows reaching column XFD still expand; the empty
        fields between their values are charged to the text budget."""
        import io

        import openpyxl
        from src.extractors import xlsx

        wb = openpyxl.Workbook()
        ws = wb.active
        for r in range(1, 2_001):
            ws.cell(row=r, column=1, value="a")
            ws.cell(row=r, column=16_384, value="z")
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        monkeypatch.setattr(xlsx, "_MAX_TEXT_CHARS", 100_000)
        rows = _count_parsed_rows(monkeypatch)

        text, _ = xlsx.extract(self._declare(buf.getvalue(), "A1:A1"))

        assert len(text) <= 100_000
        # Each full row costs 16,385 characters: six fit, the seventh
        # stops the walk.
        assert rows[0] == 7
        assert text.split("\n")[1] == "a" + "\t" * 16_383 + "z"


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

    def test_sparse_sheet_at_worksheet_bounds_is_truncated_promptly(self, monkeypatch):
        """Regression (#202): a 5 KB workbook with cells at A1 and
        XFD1048576 declares a 16,384 x 1,048,576 grid, and padding every
        row to that width stalled the indexing worker. With the declared
        dimension ignored (#305) only the parsed rows are padded, but the
        million rows between them each cost a row charge, so the cell
        budget stops the walk and the text before the gap is kept."""
        import io
        import time

        import openpyxl
        from src.extractors import extract

        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = "first"
        ws["XFD1048576"] = "last"
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        payload = buf.getvalue()
        yielded = self._count_yielded_rows(monkeypatch)

        started = time.monotonic()
        result = extract(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="sparse.xlsx",
            payload=payload,
        )
        assert time.monotonic() - started < 1.0
        assert result.status == STATUS_SUCCESS
        assert result.text == "[Sheet: Sheet]\nfirst"
        assert yielded[0] == _rows_until_cell_budget(first_row_cells=1)

    def test_sheets_after_the_cell_budget_are_not_walked(self, monkeypatch):
        import io

        import openpyxl
        from src.extractors import xlsx

        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = "first"
        ws["A1048576"] = "last"
        wb.create_sheet("Later")["A1"] = "second"
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        yielded = self._count_yielded_rows(monkeypatch)

        text, _ = xlsx.extract(buf.getvalue())

        assert text == "[Sheet: Sheet]\nfirst"
        assert yielded[0] == _rows_until_cell_budget(first_row_cells=1)

    @staticmethod
    def _count_yielded_rows(monkeypatch) -> list[int]:
        """Count every row read-only iteration yields, missing ones too."""
        from openpyxl.worksheet._read_only import ReadOnlyWorksheet

        yielded = [0]
        original = ReadOnlyWorksheet._cells_by_row

        def counting(self, *args, **kwargs):
            for row in original(self, *args, **kwargs):
                yielded[0] += 1
                yield row

        monkeypatch.setattr(ReadOnlyWorksheet, "_cells_by_row", counting)
        return yielded

    def test_rows_past_the_cell_budget_are_truncated_promptly(self, monkeypatch):
        """Rows missing between two parsed rows still cost a visit each,
        and the row number is the producer's claim: one far past the
        sheet's last row asks for unbounded visits. The cell budget,
        which charges a missing row at its measured cost of several
        cells, stops the walk and the text before the gap is kept."""
        import time

        from src.extractors import extract

        payload = _rewrite_sheet_xml(
            _xlsx_bytes([["first"], ["far"]]),
            lambda xml: xml.replace('<row r="2"', '<row r="900000000"').replace(
                'r="A2"', 'r="A900000000"'
            ),
        )
        yielded = self._count_yielded_rows(monkeypatch)

        started = time.monotonic()
        result = extract(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="far.xlsx",
            payload=payload,
        )
        assert time.monotonic() - started < 1.0
        assert result.status == STATUS_SUCCESS
        assert result.text == "[Sheet: Sheet]\nfirst"
        assert yielded[0] == _rows_until_cell_budget(first_row_cells=1)

    def test_present_but_empty_cells_stop_promptly(self, monkeypatch):
        """Styled cells with no value widen their rows without reaching
        the text budget; the cell budget stops the walk. No value was
        seen, so the attachment is ``empty``."""
        import io
        import time

        import openpyxl
        from openpyxl.styles import Font
        from src.extractors import extract, xlsx

        wb = openpyxl.Workbook()
        ws = wb.active
        for r in range(1, 1_001):
            ws.cell(row=r, column=16_384).font = Font(bold=True)
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        rows = _count_parsed_rows(monkeypatch)

        started = time.monotonic()
        result = extract(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="styled.xlsx",
            payload=buf.getvalue(),
        )
        assert time.monotonic() - started < 1.0
        assert result.status == STATUS_EMPTY
        assert rows[0] == xlsx._MAX_EXPANDED_CELLS // (16_384 + xlsx._ROW_COST) + 1

    def test_physical_rows_are_truncated_promptly(self, monkeypatch):
        """Each parsed row costs openpyxl about a microsecond however few
        cells it holds, so millions of one-cell rows (a 160 MB member
        that deflates to a few hundred KB) took seconds while charged
        one cell each. A row charge stops the walk, and the rows read
        before it are kept."""
        import time

        from src.extractors import extract, xlsx

        count = 200_000
        rows_xml = "".join(
            f'<row r="{r}"><c r="A{r}"><v>1</v></c></row>' for r in range(1, count + 1)
        )

        def edit(xml: str) -> str:
            start, end = xml.index("<sheetData>"), xml.index("</sheetData>")
            return xml[:start] + "<sheetData>" + rows_xml + xml[end:]

        payload = _rewrite_sheet_xml(_xlsx_bytes([["placeholder"]]), edit)
        parsed = _count_parsed_rows(monkeypatch)

        started = time.monotonic()
        result = extract(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename="rows.xlsx",
            payload=payload,
        )
        # Generous wall-clock ceiling: about 0.3 s locally, but shared CI
        # runners have taken just over 1 s. The row count below is what
        # proves the charge stops the walk.
        assert time.monotonic() - started < 10.0
        assert result.status == STATUS_SUCCESS
        assert parsed[0] == xlsx._MAX_EXPANDED_CELLS // (1 + xlsx._ROW_COST) + 1
        assert parsed[0] < count
        # Every row inside the budget is kept; the one that crossed it
        # is not.
        assert (result.text or "").split("\n")[1:] == ["1"] * (parsed[0] - 1)


# Distinct column letters for a wide synthetic row.
_COLUMNS = [chr(ord("A") + i) for i in range(26)]


def _rows_before_sheet_end(rows_xml: str):
    """An ``_rewrite_sheet_xml`` edit that appends ``rows_xml`` to the
    sheet data."""

    def edit(xml: str) -> str:
        end = xml.index("</sheetData>")
        return xml[:end] + rows_xml + xml[end:]

    return edit


def _count_calls(monkeypatch, owner, name: str) -> list[int]:
    """Count calls to ``owner.name`` without changing what it does."""
    calls = [0]
    original = getattr(owner, name)

    def counting(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, counting)
    return calls


class TestXlsxRawNodeBudget:
    """#432: openpyxl builds every XML node of a row, and collapses
    duplicate coordinates, before the cell budget sees the row, so a
    small workbook of repeated ``<c r="A1"/>`` nodes cost gigabytes and
    seconds while it was charged one cell. A streaming pre-pass charges
    the nodes of each worksheet and cuts it before the row that crosses
    the workbook or the row budget."""

    _XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    @staticmethod
    def _parsed_cells(monkeypatch) -> list[int]:
        """Count the ``<c>`` nodes openpyxl's worksheet parser builds
        into cells."""
        from openpyxl.worksheet._reader import WorkSheetParser

        return _count_calls(monkeypatch, WorkSheetParser, "parse_cell")

    @staticmethod
    def _scanned_nodes(monkeypatch) -> list[int]:
        """Count the elements the pre-pass visits."""
        from src.extractors import xlsx

        return _count_calls(monkeypatch, xlsx._WorksheetScan, "_start")

    def test_one_row_of_a_million_duplicate_cells_is_cut(self, monkeypatch):
        import time
        import tracemalloc

        from src.extractors import xlsx

        payload = _rewrite_sheet_xml(
            _xlsx_bytes([["first"]]),
            _rows_before_sheet_end('<row r="2">' + '<c r="A2"/>' * 1_000_000 + "</row>"),
        )
        assert len(payload) < 100_000
        parsed = self._parsed_cells(monkeypatch)
        scanned = self._scanned_nodes(monkeypatch)

        # Before the pre-pass: about 2 s and 700 MB. Generous for CI.
        started = time.monotonic()
        result = extract(content_type=self._XLSX, filename="dup.xlsx", payload=payload)
        assert time.monotonic() - started < 10.0

        assert result.status == STATUS_SUCCESS
        assert result.text == "[Sheet: Sheet]\nfirst"
        # openpyxl built the one cell before the cut, and the pre-pass
        # stopped once the row crossed the row budget.
        assert parsed[0] == 1
        assert scanned[0] < xlsx._MAX_ROW_NODES // 2 + 100

        tracemalloc.start()
        try:
            xlsx.extract(payload)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert peak < 32 * 1024 * 1024

    @pytest.mark.parametrize(
        ("row", "nodes", "width", "yields_each"),
        [
            # A row of repeated cells: the row and its ``r`` attribute,
            # then each cell, its ``r`` and its value.
            pytest.param(
                lambda r: f'<row r="{r}">' + f'<c r="A{r}"><v>{r}</v></c>' * 200 + "</row>",
                2 + 200 * 3,
                1,
                True,
                id="duplicate-cells",
            ),
            # One-value rows that all claim the same row number: openpyxl
            # parses each but yields only the first, so the cell budget
            # never charged them.
            pytest.param(
                lambda r: f'<row r="2"><c r="A2"><v>{r}</v></c></row>',
                2 + 3,
                1,
                False,
                id="duplicate-rows",
            ),
            # Wide rows of distinct cells, each with a value.
            pytest.param(
                lambda r: (
                    f'<row r="{r}">'
                    + "".join(f'<c r="{col}{r}"><v>{r}</v></c>' for col in _COLUMNS)
                    + "</row>"
                ),
                2 + len(_COLUMNS) * 3,
                len(_COLUMNS),
                True,
                id="wide-distinct-rows",
            ),
        ],
    )
    def test_rows_past_the_workbook_budget_are_cut(
        self, monkeypatch, row, nodes, width, yields_each
    ):
        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_SHEET_NODES", 20_000)
        count = 2 * xlsx._MAX_SHEET_NODES // nodes
        payload = _rewrite_sheet_xml(
            _xlsx_bytes([["first"]]),
            _rows_before_sheet_end("".join(row(r) for r in range(2, count + 2))),
        )
        parsed = self._parsed_cells(monkeypatch)
        scanned = self._scanned_nodes(monkeypatch)

        text, _ = xlsx.extract(payload)

        # Whole rows are parsed up to the budget, and none after it.
        cells_per_row = nodes // 3
        kept, partial = divmod(parsed[0] - 1, cells_per_row)
        assert partial == 0
        assert 0 < kept < count
        assert (kept + 1) * nodes > xlsx._MAX_SHEET_NODES - 100
        assert kept * nodes <= xlsx._MAX_SHEET_NODES
        assert scanned[0] <= xlsx._MAX_SHEET_NODES
        lines = text.split("\n")
        assert lines[:2] == ["[Sheet: Sheet]", "first"]
        # Rows claiming one row number are parsed but read once.
        expected = ["\t".join([str(r)] * width) for r in range(2, (kept if yields_each else 1) + 2)]
        assert lines[2:] == expected

    def test_a_start_tag_past_the_tag_budget_is_cut_before_expat_builds_it(self, monkeypatch):
        """Review round 1: expat hands a start tag's attributes over as
        one dict, built before any budget is checked, so a cell with
        hundreds of thousands of attributes cost hundreds of MB. The
        scan stops feeding a tag that has run past ``_MAX_TAG_BYTES``
        with no event, and cuts before its row."""
        import tracemalloc

        from src.extractors import xlsx

        attributes = "".join(f' a{i}="1"' for i in range(400_000))
        payload = _rewrite_sheet_xml(
            _xlsx_bytes([["first"]]),
            _rows_before_sheet_end(f'<row r="2"><c r="A2"{attributes}/></row>'),
        )
        scanned = self._scanned_nodes(monkeypatch)

        tracemalloc.start()
        try:
            text, _ = xlsx.extract(payload)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        assert text == "[Sheet: Sheet]\nfirst"
        # The tag's start event never fired: only the elements before it.
        assert scanned[0] < 50
        assert peak < 32 * 1024 * 1024

    def test_long_text_does_not_count_against_the_tag_budget(self):
        from src.extractors import xlsx

        long_value = "x" * (2 * xlsx._MAX_TAG_BYTES)
        payload = _rewrite_sheet_xml(
            _xlsx_bytes([["first"]]),
            _rows_before_sheet_end(
                f'<row r="2"><c r="A2" t="inlineStr"><is><t>{long_value}</t></is></c></row>'
                '<row r="3"><c r="A3" t="inlineStr"><is><t>third</t></is></c></row>'
            ),
        )

        text, _ = xlsx.extract(payload)

        assert xlsx._bound_worksheets(payload).getvalue() == payload
        assert text == f"[Sheet: Sheet]\nfirst\n{long_value}\nthird"

    def test_a_row_past_the_row_budget_ends_the_worksheet(self, monkeypatch):
        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_ROW_NODES", 1_000)
        wide = '<row r="3">' + '<c r="A3"><v>1</v></c>' * 400 + "</row>"
        payload = _rewrite_sheet_xml(
            _xlsx_bytes([["first"], ["second"]]),
            _rows_before_sheet_end(wide + '<row r="4"><c r="A4"><v>4</v></c></row>'),
        )
        parsed = self._parsed_cells(monkeypatch)

        text, _ = xlsx.extract(payload)

        assert text == "[Sheet: Sheet]\nfirst\nsecond"
        assert parsed[0] == 2

    def test_elements_outside_the_rows_are_charged(self, monkeypatch):
        """openpyxl keeps elements it does not know in memory, so they
        are charged like any other, and a cut after the sheet data
        keeps every row."""
        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_SHEET_NODES", 10_000)

        def edit(xml: str) -> str:
            end = xml.index("</worksheet>")
            return xml[:end] + "<junk/>" * 100_000 + xml[end:]

        payload = _rewrite_sheet_xml(_xlsx_bytes([["first"], ["second"]]), edit)
        scanned = self._scanned_nodes(monkeypatch)

        text, _ = xlsx.extract(payload)

        assert text == "[Sheet: Sheet]\nfirst\nsecond"
        assert scanned[0] <= xlsx._MAX_SHEET_NODES + 1

    def test_sheets_after_the_budget_are_emptied(self, monkeypatch):
        from src.extractors import xlsx

        payload = _titled_xlsx([("one", [["first"]] * 50), ("two", [["second"]])])
        cut = xlsx._bound_worksheets(payload)
        monkeypatch.setattr(xlsx, "_MAX_SHEET_NODES", 300)

        text, _ = xlsx.extract(payload)

        assert cut.getvalue() == payload
        assert text.startswith("[Sheet: one]\nfirst")
        assert "two" not in text
        assert "second" not in text

    @pytest.mark.parametrize(("budget", "both_kept"), [(1_000, True), (400, False)])
    def test_a_worksheet_named_twice_is_charged_twice(self, monkeypatch, budget, both_kept):
        """openpyxl parses a worksheet once for each sheet that names
        it, so both are charged, and the shorter cut serves both: here
        the second, or an empty worksheet when the first cut left no
        budget for the second."""
        import io
        import zipfile

        from src.extractors import xlsx

        base = _xlsx_bytes([["first"]] + [[r] for r in range(2, 101)])
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(base)) as original, zipfile.ZipFile(out, "w") as rebuilt:
            for info in original.infolist():
                data = original.read(info).decode()
                if info.filename == "xl/workbook.xml":
                    data = data.replace(
                        "</sheets>",
                        '<sheet xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
                        'relationships" name="Again" sheetId="2" state="visible" r:id="rId9"/>'
                        "</sheets>",
                    )
                elif info.filename == "xl/_rels/workbook.xml.rels":
                    data = data.replace(
                        "</Relationships>",
                        '<Relationship Type="http://schemas.openxmlformats.org/officeDocument/'
                        '2006/relationships/worksheet" Target="/xl/worksheets/sheet1.xml" '
                        'Id="rId9"/></Relationships>',
                    )
                rebuilt.writestr(info.filename, data)
        payload = out.getvalue()
        whole, _ = xlsx.extract(payload)
        assert whole.count("\n100") == 2
        monkeypatch.setattr(xlsx, "_MAX_SHEET_NODES", budget)

        text, _ = xlsx.extract(payload)

        if not both_kept:
            assert text == ""
            return
        first, again = text.split("\n\n")
        assert first.removeprefix("[Sheet: Sheet]") == again.removeprefix("[Sheet: Again]")
        assert 1 < len(first.split("\n")) < 100

    @pytest.mark.parametrize(
        "document",
        [
            pytest.param(
                lambda body: (
                    '<?xml version="1.0" encoding="UTF-8"?>\n<x:worksheet '
                    'xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    f"<x:sheetData>{body.replace('<', '<x:').replace('<x:/', '</x:')}"
                    "</x:sheetData></x:worksheet>"
                ),
                id="prefixed",
            ),
            pytest.param(
                lambda body: (
                    "\ufeff"
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    f"<sheetData>{body}</sheetData></worksheet>"
                ),
                id="byte-order-mark",
            ),
            pytest.param(
                lambda body: (
                    '<?xml version="1.0" encoding="ISO-8859-1"?>'
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    f"<sheetData>{body}</sheetData></worksheet>"
                ),
                id="latin-1",
            ),
        ],
    )
    def test_a_cut_closes_the_open_elements(self, monkeypatch, document):
        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_SHEET_NODES", 300)
        body = "".join(
            f'<row r="{r}"><c r="A{r}" t="inlineStr"><is><t>v{r}</t></is></c></row>'
            for r in range(1, 101)
        )
        payload = _rewrite_sheet_xml(_xlsx_bytes([["first"]]), lambda _: document(body))

        text, _ = xlsx.extract(payload)

        lines = text.split("\n")
        assert lines[0] == "[Sheet: Sheet]"
        assert 1 < len(lines) < 100
        assert lines[1:] == [f"v{r}" for r in range(1, len(lines))]

    def test_a_cut_in_an_encoding_without_ascii_end_tags_empties_the_sheet(self, monkeypatch):
        import io
        import zipfile

        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_SHEET_NODES", 300)
        body = "".join(f'<row r="{r}"><c r="A{r}"><v>{r}</v></c></row>' for r in range(1, 101))
        document = (
            '<?xml version="1.0" encoding="UTF-16"?><worksheet '
            'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{body}</sheetData></worksheet>"
        ).encode("utf-16")
        base = _xlsx_bytes([["first"]])
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(base)) as original, zipfile.ZipFile(out, "w") as rebuilt:
            for info in original.infolist():
                data = original.read(info)
                if info.filename == "xl/worksheets/sheet1.xml":
                    data = document
                rebuilt.writestr(info.filename, data)

        text, _ = xlsx.extract(out.getvalue())

        assert text == ""

    def test_malformed_xml_is_left_to_openpyxl(self):
        import io

        from src.extractors import xlsx

        caps: set[str] = set()
        left, cut = xlsx._scan_worksheet(
            io.BytesIO(b"<worksheet><sheetData><row></sheetData>"), 50, caps
        )

        assert cut is None
        assert left == 50 - 3
        assert caps == set()

    def test_entity_declarations_are_refused(self):
        payload = _rewrite_sheet_xml(
            _xlsx_bytes([["first"]]),
            lambda xml: '<!DOCTYPE worksheet [<!ENTITY e "boom">]>' + xml,
        )

        result = extract(content_type=self._XLSX, filename="entity.xlsx", payload=payload)

        assert result.status == STATUS_FAILED
        assert result.error == "EntitiesForbidden"

    @pytest.mark.parametrize(
        "rows",
        [
            [["first"]],
            [["Item", "Price"], ["Widget", 25], ["Gadget", 75.5]],
            [[r * c for c in range(1, 30)] for r in range(1, 300)],
            [[None, "gap", None, "x" * 5_000]],
        ],
        ids=["one-cell", "mixed", "dense", "sparse-long"],
    )
    def test_workbooks_under_the_budget_are_not_rewritten(self, rows):
        from src.extractors import xlsx

        payload = _xlsx_bytes(rows)

        assert xlsx._bound_worksheets(payload).getvalue() == payload

    def test_chartsheets_are_not_scanned(self, monkeypatch):
        """openpyxl reads a chartsheet whole, not as a worksheet, so the
        pre-pass leaves it to the dispatcher's caps (#428)."""
        import io

        import openpyxl
        from openpyxl.chart import BarChart, Reference
        from src.extractors import xlsx

        wb = openpyxl.Workbook()
        ws = wb.active
        for value in (1, 2, 3):
            ws.append([value])
        chart = BarChart()
        chart.add_data(Reference(ws, min_col=1, min_row=1, max_row=3))
        wb.create_chartsheet("Chart").add_chart(chart)
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        scans = _count_calls(monkeypatch, xlsx, "_scan_worksheet")

        text, _ = xlsx.extract(buf.getvalue())

        assert text == "[Sheet: Sheet]\n1\n2\n3"
        assert scans[0] == 1


_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _eager_parts(strings_name: str = "xl/sharedStrings.xml") -> dict[str, bytes]:
    """The members of a synthetic workbook holding every part openpyxl
    loads whole: a shared-string table stored as ``strings_name``,
    styles, theme, core and custom properties, a worksheet with
    relationships, an external link, and a chartsheet whose drawing
    holds a chart and a picture."""
    import io
    import zipfile

    import openpyxl
    from openpyxl.chart import BarChart, Reference
    from openpyxl.packaging.custom import StringProperty
    from PIL import Image

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["placeholder"])
    ws["A1"].hyperlink = "https://example.com/"
    wb.custom_doc_props.append(StringProperty(name="k", value="v"))
    chart = BarChart()
    chart.add_data(Reference(ws, min_col=1, min_row=1, max_row=1))
    wb.create_chartsheet("Chart").add_chart(chart)
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as base:
        parts = {name: base.read(name) for name in base.namelist()}

    def edit(name: str, old: str, new: str) -> None:
        text = parts[name].decode()
        assert old in text, (name, old)
        parts[name] = text.replace(old, new, 1).encode()

    # A shared string referenced from a second row, found by its
    # manifest content type.
    edit(
        "xl/worksheets/sheet1.xml",
        "</sheetData>",
        '<row r="2"><c r="A2" t="s"><v>0</v></c></row></sheetData>',
    )
    parts[strings_name] = (
        f'<sst xmlns="{_MAIN_NS}" count="1" uniqueCount="1"><si><t>shared text</t></si></sst>'
    ).encode()
    edit(
        "[Content_Types].xml",
        "</Types>",
        f'<Override PartName="/{strings_name}" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.sharedStrings+xml"/></Types>',
    )
    # An external link, which openpyxl reads whole unless keep_links is off.
    parts["xl/externalLinks/externalLink1.xml"] = (
        f'<externalLink xmlns="{_MAIN_NS}"><externalBook xmlns:r="{_REL_NS}" r:id="rId1">'
        '<sheetNames><sheetName val="S"/></sheetNames></externalBook></externalLink>'
    ).encode()
    parts["xl/externalLinks/_rels/externalLink1.xml.rels"] = (
        f'<Relationships xmlns="{_PKG_REL_NS}"><Relationship Id="rId1" '
        f'Type="{_REL_NS}/externalLinkPath" Target="other.xlsx" TargetMode="External"/>'
        "</Relationships>"
    ).encode()
    edit(
        "xl/_rels/workbook.xml.rels",
        "</Relationships>",
        f'<Relationship Type="{_REL_NS}/externalLink" '
        'Target="externalLinks/externalLink1.xml" Id="rIdLink"/></Relationships>',
    )
    edit(
        "xl/workbook.xml",
        "<definedNames/>",
        f'<externalReferences><externalReference xmlns:r="{_REL_NS}" r:id="rIdLink"/>'
        "</externalReferences><definedNames/>",
    )
    # A picture in the chartsheet's drawing.
    image = io.BytesIO()
    Image.new("RGB", (1, 1)).save(image, "PNG")
    parts["xl/media/image1.png"] = image.getvalue()
    edit(
        "xl/drawings/_rels/drawing1.xml.rels",
        "</Relationships>",
        f'<Relationship Type="{_REL_NS}/image" Target="/xl/media/image1.png" Id="rIdImg"/>'
        "</Relationships>",
    )
    edit("xl/drawings/drawing1.xml", "</wsDr>", _picture_anchor() + "</wsDr>")
    return parts


def _picture_anchor() -> str:
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    return (
        "<oneCellAnchor><from><col>1</col><colOff>0</colOff><row>1</row><rowOff>0</rowOff>"
        '</from><ext cx="9525" cy="9525"/><pic><nvPicPr><cNvPr id="2" name="P"/><cNvPicPr/>'
        f'</nvPicPr><blipFill><a:blip xmlns:a="{a}" xmlns:r="{_REL_NS}" r:embed="rIdImg"/>'
        f'</blipFill><spPr><a:prstGeom xmlns:a="{a}" prst="rect"/></spPr></pic><clientData/>'
        "</oneCellAnchor>"
    )


def _zip_parts(parts: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return out.getvalue()


def _padded(data: bytes, size: int) -> bytes:
    """``data`` padded to ``size`` bytes with whitespace, which XML allows
    after the root element."""
    assert len(data) <= size
    return data + b" " * (size - len(data))


def _add_sheet_refs(parts: dict[str, bytes], count: int, rel_id: str) -> None:
    """Name the part behind ``rel_id`` from ``count`` more sheets."""
    sheets = "".join(
        f'<sheet xmlns:r="{_REL_NS}" name="Extra{i}" sheetId="{i + 10}" r:id="{rel_id}"/>'
        for i in range(count)
    )
    workbook = parts["xl/workbook.xml"].decode()
    parts["xl/workbook.xml"] = workbook.replace("</sheets>", sheets + "</sheets>", 1).encode()


def _chartsheet_rel_id(parts: dict[str, bytes]) -> str:
    import re

    rels = parts["xl/_rels/workbook.xml.rels"].decode()
    match = re.search(r'Target="/xl/chartsheets/sheet1.xml" Id="(\w+)"', rels)
    assert match is not None
    return match.group(1)


class _MemberReads:
    """Every zip member opened, and the bytes read from each, while
    installed."""

    def __init__(self, monkeypatch) -> None:
        import zipfile

        self.opened: list[str] = []
        self.bytes: dict[str, int] = {}
        original_open = zipfile.ZipFile.open
        original_read = zipfile.ZipExtFile.read
        reads = self

        def counting_open(archive, name, *args, **kwargs):
            reads.opened.append(name if isinstance(name, str) else name.filename)
            return original_open(archive, name, *args, **kwargs)

        def counting_read(member, *args, **kwargs):
            data = original_read(member, *args, **kwargs)
            reads.bytes[member.name] = reads.bytes.get(member.name, 0) + len(data)
            return data

        monkeypatch.setattr(zipfile.ZipFile, "open", counting_open)
        monkeypatch.setattr(zipfile.ZipExtFile, "read", counting_read)

    @property
    def total(self) -> int:
        return sum(self.bytes.values())


class TestXlsxEagerPartBudget:
    """#428: openpyxl loads some parts whole rather than streaming them
    (the shared-string table under any name, the manifest, the workbook,
    styles, and the rest), so a small, highly compressible workbook cost
    seconds and hundreds of MB before any budget applied. Each such part
    is charged its declared size, against a per-part cap and one budget
    across the workbook, before openpyxl opens it."""

    _XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    @staticmethod
    def _openpyxl_calls(monkeypatch) -> tuple[list[int], list[int]]:
        """Count workbook loads and pre-pass readers."""
        import openpyxl
        from src.extractors import xlsx

        return (
            _count_calls(monkeypatch, openpyxl, "load_workbook"),
            _count_calls(monkeypatch, xlsx, "ExcelReader"),
        )

    def _assert_rejected(self, payload: bytes, monkeypatch) -> _MemberReads:
        from src.extractors import XLSX_EAGER_BUDGET_ERROR as xlsx_budget_error
        from src.extractors import xlsx

        loads, readers = self._openpyxl_calls(monkeypatch)
        reads = _MemberReads(monkeypatch)
        result = extract(content_type=self._XLSX, filename="book.xlsx", payload=payload)
        # #931: the budget is fixed, so the same bytes always trip it.
        assert result.status == STATUS_UNSUPPORTED
        assert result.error == xlsx_budget_error == "workbook exceeds the eager-part budget"
        assert result.extractor == "xlsx@6"
        assert result.text is None
        # Rejected before openpyxl, or the pre-pass, opened the workbook,
        # and with no more read than the budget allows.
        assert loads[0] == 0
        assert readers[0] == 0
        assert reads.total <= xlsx._MAX_EAGER_BYTES
        return reads

    @pytest.mark.parametrize("strings_name", ["xl/sharedStrings.xml", "xl/custom/table.bin"])
    def test_every_part_openpyxl_reads_whole_is_charged(self, monkeypatch, strings_name):
        """The fixture extracts, and every read openpyxl makes of a member
        while it loads the workbook, worksheets aside, was charged first."""
        import io
        from collections import Counter

        import openpyxl
        from src.extractors import xlsx

        payload = _zip_parts(_eager_parts(strings_name))
        text, _ = xlsx.extract(payload)
        assert text == "[Sheet: Sheet]\nplaceholder\nshared text"

        charged: Counter[str] = Counter()
        original = xlsx._EagerBudget.charge

        def recording(budget, name):
            present = original(budget, name)
            if present:
                charged[name] += 1
            return present

        monkeypatch.setattr(xlsx._EagerBudget, "charge", recording)
        xlsx._check_eager_parts(payload)

        reads = _MemberReads(monkeypatch)
        openpyxl.load_workbook(
            io.BytesIO(payload), read_only=True, data_only=True, keep_links=False
        ).close()
        # Read attempts of absent members fail before reading anything.
        loaded = Counter(
            name
            for name in reads.opened
            if name in reads.bytes and not name.startswith("xl/worksheets/sheet")
        )
        assert strings_name in loaded
        assert "xl/media/image1.png" in loaded
        assert "xl/charts/chart1.xml" in loaded
        assert not loaded - charged
        # External links are not loaded at all.
        assert not any("externalLink" in name for name in reads.opened)

    @pytest.mark.parametrize(
        "name",
        [
            "[Content_Types].xml",
            "xl/sharedStrings.xml",
            "xl/custom/table.bin",
            "xl/styles.xml",
            "xl/theme/theme1.xml",
            "docProps/core.xml",
            "docProps/custom.xml",
            "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels",
            "xl/worksheets/_rels/sheet1.xml.rels",
            "xl/chartsheets/sheet1.xml",
            "xl/chartsheets/_rels/sheet1.xml.rels",
            "xl/drawings/drawing1.xml",
            "xl/drawings/_rels/drawing1.xml.rels",
            "xl/charts/chart1.xml",
            "xl/media/image1.png",
        ],
    )
    def test_a_part_over_its_cap_fails_before_openpyxl_reads_it(self, monkeypatch, name):
        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_EAGER_PART_BYTES", 64 * 1024)
        parts = _eager_parts(
            "xl/custom/table.bin" if name == "xl/custom/table.bin" else "xl/sharedStrings.xml"
        )
        parts[name] = _padded(parts[name], 64 * 1024 + 1)

        reads = self._assert_rejected(_zip_parts(parts), monkeypatch)

        assert name not in reads.bytes

    def test_an_external_link_over_the_cap_is_never_read(self, monkeypatch):
        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_EAGER_PART_BYTES", 64 * 1024)
        parts = _eager_parts()
        name = "xl/externalLinks/externalLink1.xml"
        parts[name] = _padded(parts[name], 1024 * 1024)
        payload = _zip_parts(parts)
        reads = _MemberReads(monkeypatch)

        text, _ = xlsx.extract(payload)

        assert text == "[Sheet: Sheet]\nplaceholder\nshared text"
        assert not any("externalLink" in opened for opened in reads.opened)

    @pytest.mark.parametrize("distinct", [True, False], ids=["distinct", "repeated"])
    def test_sub_cap_chartsheets_over_the_aggregate_fail(self, monkeypatch, distinct):
        """Each chartsheet is under the per-part cap; together they cross
        the workbook budget, whether they are separate parts or one part
        named by many sheets."""
        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_EAGER_PART_BYTES", 64 * 1024)
        monkeypatch.setattr(xlsx, "_MAX_EAGER_BYTES", 1024 * 1024)
        parts = _eager_parts()
        chartsheet = _padded(parts["xl/chartsheets/sheet1.xml"], 60 * 1024)
        parts["xl/chartsheets/sheet1.xml"] = chartsheet
        count = 40  # 40 x 60 KiB > 1 MiB
        if distinct:
            rels = parts["xl/_rels/workbook.xml.rels"].decode()
            for i in range(2, count + 2):
                parts[f"xl/chartsheets/sheet{i}.xml"] = chartsheet
                parts[f"xl/chartsheets/_rels/sheet{i}.xml.rels"] = parts[
                    "xl/chartsheets/_rels/sheet1.xml.rels"
                ]
                rels = rels.replace(
                    "</Relationships>",
                    f'<Relationship Type="{_REL_NS}/chartsheet" '
                    f'Target="/xl/chartsheets/sheet{i}.xml" Id="rIdC{i}"/></Relationships>',
                )
                _add_sheet_refs(parts, 1, f"rIdC{i}")
            parts["xl/_rels/workbook.xml.rels"] = rels.encode()
        else:
            _add_sheet_refs(parts, count, _chartsheet_rel_id(parts))

        reads = self._assert_rejected(_zip_parts(parts), monkeypatch)

        # Charged by declared size: no chartsheet itself is read.
        assert not any(name.startswith("xl/chartsheets/sheet") for name in reads.bytes)

    def test_one_chart_referenced_by_many_anchors_is_charged_per_reference(self, monkeypatch):
        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_EAGER_PART_BYTES", 64 * 1024)
        monkeypatch.setattr(xlsx, "_MAX_EAGER_BYTES", 1024 * 1024)
        parts = _eager_parts()
        parts["xl/charts/chart1.xml"] = _padded(parts["xl/charts/chart1.xml"], 60 * 1024)
        drawing = parts["xl/drawings/drawing1.xml"].decode()
        start, end = drawing.index("<absoluteAnchor>"), drawing.index("</absoluteAnchor>")
        anchor = drawing[start : end + len("</absoluteAnchor>")]
        parts["xl/drawings/drawing1.xml"] = drawing.replace(anchor, anchor * 40, 1).encode()

        reads = self._assert_rejected(_zip_parts(parts), monkeypatch)

        assert "xl/charts/chart1.xml" not in reads.bytes

    def test_sheets_over_the_read_budget_fail(self, monkeypatch):
        from src.extractors import xlsx

        parts = _eager_parts()
        _add_sheet_refs(parts, xlsx._MAX_EAGER_READS, "rId1")

        self._assert_rejected(_zip_parts(parts), monkeypatch)

    def test_parts_exactly_at_the_aggregate_still_extract(self, monkeypatch):
        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_EAGER_PART_BYTES", 64 * 1024)
        monkeypatch.setattr(xlsx, "_MAX_EAGER_BYTES", 64 * 1024)
        parts = _eager_parts()
        budget_left: list[int] = []
        original = xlsx._EagerBudget.charge

        def recording(budget, name):
            present = original(budget, name)
            budget_left.append(budget.bytes_left)
            return present

        monkeypatch.setattr(xlsx._EagerBudget, "charge", recording)
        xlsx._check_eager_parts(_zip_parts(parts))
        monkeypatch.setattr(xlsx._EagerBudget, "charge", original)
        # Spend what is left on the shared strings, charged once.
        strings = parts["xl/sharedStrings.xml"]
        parts["xl/sharedStrings.xml"] = _padded(strings, len(strings) + budget_left[-1])

        text, _ = xlsx.extract(_zip_parts(parts))
        assert text == "[Sheet: Sheet]\nplaceholder\nshared text"

        parts["xl/sharedStrings.xml"] += b" "
        self._assert_rejected(_zip_parts(parts), monkeypatch)

    def test_a_large_legitimate_shared_string_table_extracts(self):
        """Real caps: about 7.6 MiB of distinct shared strings."""
        import time

        from src.extractors import xlsx

        parts = _eager_parts()
        strings = "".join(f"<si><t>Item {i:07d} description</t></si>" for i in range(200_000))
        parts["xl/sharedStrings.xml"] = (f'<sst xmlns="{_MAIN_NS}">' + strings + "</sst>").encode()
        assert len(parts["xl/sharedStrings.xml"]) < xlsx._MAX_EAGER_PART_BYTES

        started = time.monotonic()
        text, _ = xlsx.extract(_zip_parts(parts))
        assert time.monotonic() - started < 30.0
        assert text == "[Sheet: Sheet]\nplaceholder\nItem 0000000 description"

    def test_worst_case_parts_under_the_cap_fail_fast_together(self, monkeypatch):
        """Real caps: the costliest shapes measured for #428 (empty shared
        strings, empty fonts in the styles, defined names, manifest
        defaults), each under the per-part cap, cross the aggregate;
        they fail in milliseconds rather than seconds, reading only the
        manifest."""
        import re
        import time
        import tracemalloc

        size = 7 * 1024 * 1024
        parts = _eager_parts()
        parts["xl/sharedStrings.xml"] = (
            f'<sst xmlns="{_MAIN_NS}">' + "<si/>" * (size // 5) + "</sst>"
        ).encode()
        styles = parts["xl/styles.xml"].decode()
        fonts = "<fonts>" + "<font><b/></font>" * (size // 17) + "</fonts>"
        parts["xl/styles.xml"] = re.sub(r"<fonts.*?</fonts>", fonts, styles, count=1).encode()
        workbook = parts["xl/workbook.xml"].decode()
        names = '<definedName name="nm">Sheet!$A$1</definedName>' * (size // 48)
        parts["xl/workbook.xml"] = workbook.replace(
            "<definedNames/>", f"<definedNames>{names}</definedNames>"
        ).encode()
        manifest = parts["[Content_Types].xml"].decode()
        defaults = '<Default Extension="e1234" ContentType="application/x"/>' * (512 * 1024 // 55)
        parts["[Content_Types].xml"] = manifest.replace("</Types>", defaults + "</Types>").encode()
        payload = _zip_parts(parts)
        assert len(payload) < 200_000

        tracemalloc.start()
        started = time.monotonic()
        try:
            reads = self._assert_rejected(payload, monkeypatch)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        # On main: about 7.5 s and 330 MB. Generous for CI.
        assert time.monotonic() - started < 5.0
        assert peak < 64 * 1024 * 1024
        assert set(reads.bytes) == {"[Content_Types].xml"}

    @staticmethod
    def _malformed(shape: str) -> bytes:
        """The fixture workbook broken in one of the ways the walk stops
        at and leaves to openpyxl."""
        parts = _eager_parts()

        def edit(name: str, old: str, new: str) -> None:
            text = parts[name].decode()
            assert old in text, (shape, old)
            parts[name] = text.replace(old, new, 1).encode()

        chart_rel = _chartsheet_rel_id(parts)
        workbook_override = (
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        )
        if shape == "not-a-zip":
            return b"PK\x03\x04 not a zip"
        if shape == "no-manifest":
            del parts["[Content_Types].xml"]
        elif shape == "no-workbook-type":
            edit("[Content_Types].xml", workbook_override, "")
        elif shape == "workbook-type-as-default":
            edit("[Content_Types].xml", workbook_override, "")
            edit(
                "[Content_Types].xml",
                'ContentType="application/xml"',
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet.main+xml"',
            )
        elif shape == "no-workbook":
            del parts["xl/workbook.xml"]
        elif shape == "no-workbook-rels":
            del parts["xl/_rels/workbook.xml.rels"]
        elif shape == "sheet-without-id":
            edit("xl/workbook.xml", f'r:id="{chart_rel}"', "")
        elif shape == "sheet-unknown-id":
            edit("xl/workbook.xml", f'r:id="{chart_rel}"', 'r:id="rIdMissing"')
        elif shape == "sheet-target-missing":
            del parts["xl/chartsheets/sheet1.xml"]
        elif shape == "chartsheet-without-rels":
            del parts["xl/chartsheets/_rels/sheet1.xml.rels"]
        elif shape == "drawing-missing":
            del parts["xl/drawings/drawing1.xml"]
        elif shape == "drawing-unreadable":
            parts["xl/drawings/drawing1.xml"] = (
                b'<wsDr xmlns="http://schemas.openxmlformats.org/drawingml/2006/'
                b'spreadsheetDrawing"><absoluteAnchor><pos/></absoluteAnchor></wsDr>'
            )
        elif shape == "drawing-without-rels":
            del parts["xl/drawings/_rels/drawing1.xml.rels"]
        elif shape == "chart-missing":
            del parts["xl/charts/chart1.xml"]
        elif shape == "picture-not-an-image":
            edit("xl/drawings/_rels/drawing1.xml.rels", f"{_REL_NS}/image", f"{_REL_NS}/oleObject")
        else:
            raise AssertionError(shape)
        return _zip_parts(parts)

    @pytest.mark.parametrize(
        "shape",
        [
            "not-a-zip",
            "no-manifest",
            "no-workbook-type",
            "workbook-type-as-default",
            "no-workbook",
            "no-workbook-rels",
            "sheet-without-id",
            "sheet-unknown-id",
            "sheet-target-missing",
            "chartsheet-without-rels",
            "drawing-missing",
            "drawing-unreadable",
            "drawing-without-rels",
            "chart-missing",
            "picture-not-an-image",
        ],
    )
    def test_the_walk_leaves_a_malformed_workbook_to_openpyxl(self, shape):
        """Under budget, the walk never changes whether a workbook loads:
        where it stops, openpyxl's own load succeeds, or fails as the
        walk does or later."""
        import io
        import warnings

        import openpyxl
        from src.extractors import xlsx

        payload = self._malformed(shape)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                openpyxl.load_workbook(
                    io.BytesIO(payload), read_only=True, data_only=True, keep_links=False
                ).close()
                expected: type[BaseException] | None = None
            except Exception as exc:
                expected = type(exc)
            try:
                xlsx._check_eager_parts(payload)
            except Exception as exc:
                assert not isinstance(exc, xlsx.XlsxEagerPartBudgetError)
                assert type(exc) is expected
            result = extract(content_type=self._XLSX, filename="book.xlsx", payload=payload)
        assert (result.status == STATUS_FAILED) == (expected is not None)

    def test_a_part_understating_its_size_is_read_no_further(self, monkeypatch):
        """The charge is the central directory's declared size; zipfile
        stops reading a member there, so a part declaring less than it
        holds fails rather than costing more than it was charged."""
        import struct

        parts = _eager_parts()
        parts["xl/sharedStrings.xml"] = (
            f'<sst xmlns="{_MAIN_NS}">' + "<si/>" * 1_000_000 + "</sst>"
        ).encode()
        payload = bytearray(_zip_parts(parts))
        entry = payload.find(b"PK\x01\x02", 0)
        while payload[entry + 46 : entry + 46 + 20] != b"xl/sharedStrings.xml":
            entry = payload.find(b"PK\x01\x02", entry + 4)
        struct.pack_into("<I", payload, entry + 24, 1024)  # uncompressed size
        reads = _MemberReads(monkeypatch)

        result = extract(content_type=self._XLSX, filename="book.xlsx", payload=bytes(payload))

        assert result.status == STATUS_FAILED
        assert reads.bytes.get("xl/sharedStrings.xml", 0) <= 1024

    def test_a_shared_string_table_twenty_mb_of_empty_items_fails_fast(self, monkeypatch):
        """The #428 measurement: 20 MB of ``<si/>`` took 7.4 s and 495 MB."""
        import time

        parts = _eager_parts()
        parts["xl/sharedStrings.xml"] = (
            f'<sst xmlns="{_MAIN_NS}">' + "<si/>" * 4_000_000 + "</sst>"
        ).encode()

        started = time.monotonic()
        reads = self._assert_rejected(_zip_parts(parts), monkeypatch)
        assert time.monotonic() - started < 5.0
        assert "xl/sharedStrings.xml" not in reads.bytes


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
        assert result.extractor == "pdf@5"
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
        monkeypatch.setattr(
            "pdf2image.pdfinfo_from_bytes",
            lambda payload, **kwargs: captured.setdefault("pdfinfo_timeout", kwargs.get("timeout")),
        )
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
        # #781: the page count pdf2image takes first is bounded too.
        assert captured["pdfinfo_timeout"] == 45

    def test_no_page_count_call_without_a_deadline(self, monkeypatch, tmp_path):
        from src.extractors import pdf

        captured = self._capture_render(monkeypatch, tmp_path)
        pdf._extract_ocr(self._blank_pdf(612, 792), pages=[0])
        assert "pdfinfo_timeout" not in captured

    def test_stalling_page_count_is_bounded_by_the_ocr_timeout(self, tmp_path, monkeypatch):
        """#781: pdf2image runs Poppler's ``pdfinfo`` for the page count
        before rendering and does not pass it the timeout. A ``pdfinfo``
        that stalls on a crafted PDF must stop at the OCR timeout, the
        way a hung render does. Runs the real pdf2image against a fake
        Poppler whose ``pdfinfo`` sleeps.

        The work done is read from the processes pdf2image starts, not
        from a marker the fake writes: under CPU load the timeout can
        kill the fake before its shell writes anything (#1148)."""
        import os
        import signal
        import subprocess
        import tempfile
        import time

        import pdf2image
        import pdf2image.pdf2image
        from pdf2image.exceptions import PDFPopplerTimeoutError
        from src.extractors import pdf

        bindir = tmp_path / "bin"
        bindir.mkdir()
        scripts = {
            "pdfinfo": "#!/bin/sh\nexec sleep 30\n",
            "pdftoppm": "#!/bin/sh\necho 'pdftoppm version 24.02.0' >&2\nexit 1\n",
        }
        for name, body in scripts.items():
            (bindir / name).write_text(body)
            (bindir / name).chmod(0o700)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr("pytesseract.image_to_string", lambda image, **_: "ocr text")
        real_temp_dir = tempfile.TemporaryDirectory
        monkeypatch.setattr(
            pdf.tempfile,
            "TemporaryDirectory",
            lambda **kwargs: real_temp_dir(dir=str(tmp_path)),
        )
        timeouts: list[object] = []
        real_pdfinfo = pdf2image.pdfinfo_from_bytes

        def spy_pdfinfo(payload, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            return real_pdfinfo(payload, **kwargs)

        monkeypatch.setattr("pdf2image.pdfinfo_from_bytes", spy_pdfinfo)
        procs: list[subprocess.Popen[bytes]] = []
        real_popen = pdf2image.pdf2image.Popen

        def recording_popen(command, **kwargs):
            proc = real_popen(command, **kwargs)
            procs.append(proc)
            return proc

        monkeypatch.setattr(pdf2image.pdf2image, "Popen", recording_popen)

        started = time.monotonic()
        with pytest.raises(PDFPopplerTimeoutError):
            pdf._extract_ocr(self._blank_pdf(612, 792), pages=[0], ocr_timeout_seconds=1)
        # Generous: the fake sleeps 30 s, and the kill below is the proof.
        assert time.monotonic() - started < 20
        # The page count ran once, under the OCR timeout, and was killed
        # rather than left to finish its stall.
        assert timeouts == [1]
        pdfinfo_runs = [p for p in procs if os.path.basename(p.args[0]) == "pdfinfo"]
        assert len(pdfinfo_runs) == 1
        assert pdfinfo_runs[0].returncode == -signal.SIGKILL

    def test_unreadable_page_sizes_fail_before_rendering(self, monkeypatch, tmp_path):
        """If the page sizes cannot be read the raster size is unknown, so
        the OCR fallback fails closed rather than rendering blind."""
        from src.extractors import pdf

        captured = self._capture_render(monkeypatch, tmp_path)
        with pytest.raises(Exception):  # noqa: B017 — any pypdf parse error
            pdf._extract_ocr(b"%PDF-1.7 not a real pdf", pages=[0])
        assert captured == {}


class TestPdfDigitalPageErrors:
    """#707: the per-page ``except Exception`` in ``_extract_digital_pages``
    also caught ``MemoryError`` and ``RecursionError``, so host pressure
    became a skipped page and the document could be cached as a success.
    Both must reach the dispatcher, which re-raises them; any other
    per-page error still skips that page only."""

    PAGE_TEXT = "Synthetic page {n} text that is long enough to count as digital."

    def _fake_reader(self, monkeypatch, error):
        """Three pages, the middle one raising ``error``; returns the list
        of page indexes whose ``extract_text`` ran."""
        from src.extractors import pdf

        calls: list[int] = []
        page_text = self.PAGE_TEXT

        class Page:
            def __init__(self, index):
                self.index = index

            def extract_text(self):
                calls.append(self.index)
                if self.index == 1:
                    raise error("SYNTHETIC_PAGE_MARKER")
                return page_text.format(n=self.index)

        class FakeReader:
            def __init__(self, stream):
                self.pages = [Page(0), Page(1), Page(2)]

        monkeypatch.setattr(pdf.pypdf, "PdfReader", FakeReader)
        return calls

    @pytest.mark.parametrize("error", [MemoryError, RecursionError])
    def test_host_pressure_on_a_page_propagates_from_the_extractor(self, monkeypatch, error):
        from src.extractors import pdf

        calls = self._fake_reader(monkeypatch, error)
        progress: list[None] = []
        with pytest.raises(error):
            pdf._extract_digital_pages(b"%PDF-1.7", on_progress=lambda: progress.append(None))
        # Extraction stops at the failing page: the page after it is not read.
        assert calls == [0, 1]
        assert len(progress) == 1

    @pytest.mark.parametrize("error", [MemoryError, RecursionError])
    def test_host_pressure_on_a_page_propagates_from_the_dispatcher(self, monkeypatch, error):
        calls = self._fake_reader(monkeypatch, error)
        with pytest.raises(error):
            extract(
                content_type="application/pdf",
                filename="synthetic.pdf",
                payload=b"%PDF-1.7",
                ocr_enabled=False,
            )
        assert calls == [0, 1]

    def test_ordinary_page_error_skips_only_that_page(self, monkeypatch):
        calls = self._fake_reader(monkeypatch, ValueError)
        result = extract(
            content_type="application/pdf",
            filename="synthetic.pdf",
            payload=b"%PDF-1.7",
            ocr_enabled=False,
        )
        assert calls == [0, 1, 2]
        assert result.status == STATUS_SUCCESS
        assert result.text == (self.PAGE_TEXT.format(n=0) + "\n\n" + self.PAGE_TEXT.format(n=2))


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

        work: dict = {"renders": [], "timeouts": [], "pdfinfo_timeouts": [], "ocr_calls": 0}

        def fake_pdfinfo(payload, **kwargs):
            work["pdfinfo_timeouts"].append(kwargs.get("timeout"))
            return {"Pages": 99}

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
        monkeypatch.setattr("pdf2image.pdfinfo_from_bytes", fake_pdfinfo)
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
        assert result.extractor == "pdf-ocr@5"
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

    def test_page_cap_logs_a_warning_with_the_counts(self, monkeypatch, tmp_path, caplog):
        """#871: pages past the cap are never read, so the cap says so at
        WARNING with counts only. The result is the same as before."""
        caplog.set_level("INFO")
        self._fake_ocr(monkeypatch, tmp_path)
        result = extract(
            content_type="application/pdf",
            filename="SYNTHETIC_FILENAME_MARKER.pdf",
            payload=self._pdf("d" + "s" * 30),
            max_ocr_pages=5,
        )
        assert result.status == STATUS_SUCCESS
        assert result.extractor == "pdf-ocr@5"
        assert result.text is not None
        assert [f"{self.SCANNED} {n}" in result.text for n in range(1, 7)] == [True] * 5 + [False]
        [record] = [r for r in caplog.records if "OCR capped" in r.getMessage()]
        assert record.levelname == "WARNING"
        assert record.getMessage() == "pdf OCR capped at 5 of 30 scanned pages"
        assert "SYNTHETIC_FILENAME_MARKER" not in caplog.text
        assert self.SCANNED not in caplog.text

    @pytest.mark.parametrize(
        "layout, cap, ocr, fail, status, skipped, ocr_calls",
        [
            # Capped: the pages past the cap are recorded on the result.
            ("d" + "s" * 30, 5, True, False, STATUS_SUCCESS, 25, 5),
            # Every scanned page read, or none to read: known, zero.
            ("sss", 20, True, False, STATUS_SUCCESS, 0, 3),
            ("dd", 1, True, False, STATUS_SUCCESS, 0, 0),
            # OCR off with enough digital text: the cap never applied.
            ("dd", 1, False, False, STATUS_SUCCESS, 0, 0),
            # Capped, then OCR failed: the digital text is kept, and the
            # capped pages are as unread as before.
            ("dsss", 1, True, True, STATUS_SUCCESS, 2, 1),
            # OCR off on a scanned PDF: ``unsupported``, nothing known.
            ("sss", 1, False, False, STATUS_UNSUPPORTED, None, 0),
            # OCR failed with no digital text: ``failed``, nothing known.
            ("sss", 1, True, True, STATUS_FAILED, None, 1),
        ],
    )
    def test_result_records_the_pages_the_cap_skipped(
        self, monkeypatch, tmp_path, layout, cap, ocr, fail, status, skipped, ocr_calls
    ):
        """#891: the count the cap left unread travels on the result, so
        the extraction cache can keep it. The text is unchanged."""
        work = self._fake_ocr(monkeypatch, tmp_path, fail=fail)
        result = self._extract(layout, max_ocr_pages=cap, ocr_enabled=ocr)
        assert result.status == status
        assert result.ocr_pages_skipped == skipped
        assert work["ocr_calls"] == ocr_calls

    def test_the_count_does_not_carry_into_the_next_extraction(self, monkeypatch, tmp_path):
        self._fake_ocr(monkeypatch, tmp_path)
        assert self._extract("d" + "s" * 30, max_ocr_pages=5).ocr_pages_skipped == 25
        assert self._extract("dss", max_ocr_pages=5).ocr_pages_skipped == 0
        text = extract(content_type="text/plain", filename="a.txt", payload=b"plain words")
        assert (text.status, text.ocr_pages_skipped) == (STATUS_SUCCESS, None)

    @staticmethod
    def _fail_pypdf_pages(monkeypatch, failing: set[int]) -> None:
        """Make pypdf raise on the pages at these 0-based indexes: the
        digital walk reads pages in order, one ``extract_text`` each."""
        from pypdf import PageObject

        real = PageObject.extract_text
        calls = {"n": 0}

        def extract_text(self, *args, **kwargs):
            index = calls["n"]
            calls["n"] += 1
            if index in failing:
                raise ValueError("SYNTHETIC_PYPDF_MARKER")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(PageObject, "extract_text", extract_text)

    @staticmethod
    def _unrecovered_line(caplog):
        """The attachments line for one committed successful PDF plus the
        extractor counts, and its level."""
        from src import attachment_indexing, main
        from src.extractors import STATUS_SUCCESS

        attachment_indexing.attachment_outcomes.record(STATUS_SUCCESS, None, cached=False)
        main._log_attachment_outcomes(force=True)
        [record] = [r for r in caplog.records if r.getMessage().startswith("attachments n=")]
        return record

    @pytest.mark.parametrize(
        "layout, failing, ocr, cap, ocr_text, extractor, unrecovered",
        [
            # OCR off: a mixed PDF returns its digital text before OCR
            # could read the failed page.
            ("dd", {1}, False, 20, True, "pdf-digital@5", 1),
            # OCR on: the failed page is OCR'd and its text recovered.
            ("dd", {1}, True, 20, True, "pdf-ocr@5", 0),
            # OCR on but it reads no text on the failed page.
            ("dd", {1}, True, 20, False, "pdf-digital@5", 1),
            # The OCR cap leaves the failed page unread.
            ("dsss", {3}, True, 1, True, "pdf-ocr@5", 1),
        ],
    )
    def test_pdf_pages_unrecovered(
        self,
        monkeypatch,
        tmp_path,
        caplog,
        layout,
        failing,
        ocr,
        cap,
        ocr_text,
        extractor,
        unrecovered,
    ):
        """Review round 4 on #884: ``pdf_pages_failed`` counts every page
        pypdf could not read, ``pdf_pages_unrecovered`` those whose text
        no OCR recovered. Only the latter makes the attachments line a
        WARNING. Extraction results are unchanged."""
        from src import attachment_indexing, extractors

        caplog.set_level("INFO")
        self._fake_ocr(monkeypatch, tmp_path)
        if not ocr_text:
            monkeypatch.setattr("pytesseract.image_to_string", lambda image, **_: "  ")
        self._fail_pypdf_pages(monkeypatch, failing)
        attachment_indexing.attachment_outcomes.drain()
        result = self._extract(layout, ocr_enabled=ocr, max_ocr_pages=cap)
        assert (result.status, result.extractor) == (STATUS_SUCCESS, extractor)
        assert result.text is not None
        assert self.DIGITAL.format(n=1) in result.text
        assert (self.SCANNED in result.text) is (ocr and ocr_text)
        counts = extractors.drain_extractor_counts()
        assert counts["pdf_pages_failed"] == len(failing)
        assert counts["pdf_pages_unrecovered"] == unrecovered
        # Put the counts back for the aggregate line.
        for _ in range(counts["pdf_pages_failed"]):
            extractors.note_pdf_page_failed()
        extractors.note_pdf_pages_unrecovered(counts["pdf_pages_unrecovered"])
        for _ in range(counts["ocr_capped_pdfs"]):
            extractors.note_ocr_capped(0)
        record = self._unrecovered_line(caplog)
        degraded = unrecovered or counts["ocr_capped_pdfs"]
        assert record.levelname == ("WARNING" if degraded else "INFO")
        assert f"pdf_pages_unrecovered={unrecovered}" in record.getMessage()
        assert "SYNTHETIC_PYPDF_MARKER" not in caplog.text

    def test_page_cap_warnings_are_rate_limited_and_counted(self, monkeypatch, tmp_path, caplog):
        """Review round 2 on #884: one message can carry many capped
        PDFs, and a WARNING each could flood the log. The cap line shares
        the extractor warning limit; every capped PDF and skipped page is
        counted for the attachments aggregate, whether its line was
        logged or suppressed. Results are unchanged."""
        from src import extractors

        caplog.set_level("INFO")
        self._fake_ocr(monkeypatch, tmp_path)
        monkeypatch.setattr(extractors._LINE_BUDGET, "limit", 2)
        extractors.drain_extractor_counts()
        results = [self._extract("d" + "s" * 30, max_ocr_pages=5) for _ in range(5)]
        assert {(r.status, r.extractor) for r in results} == {(STATUS_SUCCESS, "pdf-ocr@5")}
        lines = [r for r in caplog.records if "OCR capped" in r.getMessage()]
        assert [r.levelname for r in lines] == ["WARNING", "WARNING"]
        assert extractors.drain_extractor_counts() == {
            "pdf_pages_failed": 0,
            "pdf_pages_unrecovered": 0,
            "ocr_capped_pdfs": 5,
            "ocr_pages_skipped": 5 * 25,
            "ocr_capped_images": 0,
            "extractor_caps": 0,
            "parser_caps_messages": 0,
            "parser_recipients_merged_messages": 0,
            "parser_sender_ambiguous_messages": 0,
            "warnings_suppressed": 3,
        }

    @pytest.mark.parametrize("layout, cap", [("sss", 3), ("sss", 20), ("s" * 30, 0), ("dd", 1)])
    def test_no_cap_warning_when_every_scanned_page_is_read(
        self, monkeypatch, tmp_path, caplog, layout, cap
    ):
        caplog.set_level("INFO")
        self._fake_ocr(monkeypatch, tmp_path)
        from src import extractors

        extractors.drain_extractor_counts()
        self._extract(layout, max_ocr_pages=cap)
        assert "OCR capped" not in caplog.text
        assert extractors.drain_extractor_counts()["ocr_capped_pdfs"] == 0

    def test_page_cap_counts_pages_across_runs(self, monkeypatch, tmp_path):
        work = self._fake_ocr(monkeypatch, tmp_path)
        self._extract("sd" * 10, max_ocr_pages=3)
        assert work["renders"] == [(1, 1), (3, 3), (5, 5)]
        assert work["ocr_calls"] == 3

    def test_digital_pdf_renders_nothing(self, monkeypatch, tmp_path):
        work = self._fake_ocr(monkeypatch, tmp_path)
        result = self._extract("dd")
        assert result.extractor == "pdf-digital@5"
        assert work["renders"] == [] and work["ocr_calls"] == 0

    def test_scanned_pdf_still_ocrs_every_page_within_the_cap(self, monkeypatch, tmp_path):
        work = self._fake_ocr(monkeypatch, tmp_path)
        result = self._extract("sss")
        assert result.extractor == "pdf-ocr@5"
        assert work["renders"] == [(1, 3)]
        assert work["ocr_calls"] == 3

    def test_mixed_pdf_with_ocr_off_keeps_its_digital_text(self, monkeypatch, tmp_path):
        work = self._fake_ocr(monkeypatch, tmp_path)
        result = self._extract("ds", ocr_enabled=False)
        assert result.status == STATUS_SUCCESS
        assert result.extractor == "pdf-digital@5"
        assert work["renders"] == [] and work["ocr_calls"] == 0

    def test_ocr_failure_on_a_mixed_pdf_keeps_the_digital_text(self, monkeypatch, tmp_path, caplog):
        """Before #292 a mixed PDF was indexed from its text layer alone;
        a page OCR cannot read must not lose that text."""
        caplog.set_level("DEBUG")
        self._fake_ocr(monkeypatch, tmp_path, fail=True)
        result = self._extract("ds")
        assert result.status == STATUS_SUCCESS
        assert result.extractor == "pdf-digital@5"
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

    def test_page_count_time_counts_against_the_render_budget(self, monkeypatch, tmp_path):
        """#781: the page count is Poppler time, so it spends the same
        budget as the renders; it runs once per document."""
        from src.extractors import pdf

        work = self._fake_ocr(monkeypatch, tmp_path)
        clock = {"now": 0.0}
        real_pdfinfo = __import__("pdf2image").pdfinfo_from_bytes

        def slow_pdfinfo(payload, **kwargs):
            clock["now"] += 10.0
            return real_pdfinfo(payload, **kwargs)

        monkeypatch.setattr("pdf2image.pdfinfo_from_bytes", slow_pdfinfo)
        monkeypatch.setattr(pdf.time, "monotonic", lambda: clock["now"])

        pdf._extract_ocr(self._pdf("sdsds"), pages=[0, 2, 4], ocr_timeout_seconds=45)
        assert work["pdfinfo_timeouts"] == [45]
        # Each render's timeout also holds back the page-count time, for
        # the unbounded ``pdfinfo`` pdf2image runs inside it (#867 review).
        assert work["timeouts"] == [25, 25, 25]

    def _slow_page_count(self, monkeypatch, tmp_path, seconds, *, render_seconds=0.0):
        from src.extractors import pdf

        work = self._fake_ocr(monkeypatch, tmp_path)
        clock = {"now": 0.0}
        real_pdfinfo = __import__("pdf2image").pdfinfo_from_bytes
        real_convert = __import__("pdf2image").convert_from_bytes

        def slow_pdfinfo(payload, **kwargs):
            clock["now"] += seconds
            return real_pdfinfo(payload, **kwargs)

        def slow_convert(payload, **kwargs):
            clock["now"] += seconds + render_seconds
            return real_convert(payload, **kwargs)

        monkeypatch.setattr("pdf2image.pdfinfo_from_bytes", slow_pdfinfo)
        monkeypatch.setattr("pdf2image.convert_from_bytes", slow_convert)
        monkeypatch.setattr(pdf.time, "monotonic", lambda: clock["now"])
        return work

    def test_page_count_over_half_the_budget_is_an_ocr_timeout(self, monkeypatch, tmp_path):
        """#867 review round 1: pdf2image repeats the page count, unbounded,
        inside the render, so a page count taking most of the budget
        would roughly double the deadline. Over half the budget, OCR stops
        as on a timeout and nothing is rendered."""
        from src.extractors import pdf

        work = self._slow_page_count(monkeypatch, tmp_path, 27.0)  # 60% of 45
        with pytest.raises(TimeoutError):
            pdf._extract_ocr(self._pdf("ss"), pages=[0, 1], ocr_timeout_seconds=45)
        assert work["pdfinfo_timeouts"] == [45]
        assert work["renders"] == [] and work["ocr_calls"] == 0

    @pytest.mark.parametrize(
        ("layout", "status", "extractor"),
        [("ds", STATUS_SUCCESS, "pdf-digital@5"), ("ss", STATUS_FAILED, "pdf@5")],
    )
    def test_slow_page_count_degrades_like_a_timeout(
        self, monkeypatch, tmp_path, layout, status, extractor
    ):
        work = self._slow_page_count(monkeypatch, tmp_path, 27.0)
        result = self._extract(layout, ocr_timeout_seconds=45)
        assert (result.status, result.extractor) == (status, extractor)
        if status == STATUS_FAILED:
            assert result.error == "TimeoutError"
        assert len(work["pdfinfo_timeouts"]) == 1
        assert work["renders"] == [] and work["ocr_calls"] == 0

    def test_page_count_reserve_stops_a_run_that_cannot_fit(self, monkeypatch, tmp_path):
        """Poppler time stays within the budget: the inner page count is
        charged up front, so no run starts without room for it."""
        from src.extractors import pdf

        # 10 s page count, 10 s render: 10 + (10 + 10) + (10 + 10) = 50 > 45.
        work = self._slow_page_count(monkeypatch, tmp_path, 10.0, render_seconds=10.0)
        with pytest.raises(TimeoutError):
            pdf._extract_ocr(self._pdf("sdsds"), pages=[0, 2, 4], ocr_timeout_seconds=45)
        assert work["timeouts"] == [25, 5]
        assert work["renders"] == [(1, 1), (3, 3)]

    @pytest.mark.parametrize(
        ("layout", "status", "extractor"),
        [("ds", STATUS_SUCCESS, "pdf-digital@5"), ("ss", STATUS_FAILED, "pdf@5")],
    )
    def test_page_count_timeout_degrades_like_a_render_timeout(
        self, monkeypatch, tmp_path, layout, status, extractor
    ):
        """A mixed PDF keeps its digital text; a scanned one is a failed
        extraction recorded by type, as for any OCR failure."""
        from pdf2image.exceptions import PDFPopplerTimeoutError

        work = self._fake_ocr(monkeypatch, tmp_path)

        def stalled_pdfinfo(payload, **kwargs):
            raise PDFPopplerTimeoutError("SYNTHETIC_PDFINFO_MARKER")

        monkeypatch.setattr("pdf2image.pdfinfo_from_bytes", stalled_pdfinfo)
        result = self._extract(layout, ocr_timeout_seconds=30)
        assert (result.status, result.extractor) == (status, extractor)
        if status == STATUS_FAILED:
            assert result.error == "PDFPopplerTimeoutError"
        assert work["renders"] == [] and work["ocr_calls"] == 0

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

    def test_progress_is_reported_per_page_read(self, monkeypatch, tmp_path):
        """#485: a scanned PDF can OCR for ~20 minutes, past the
        healthcheck's 600 s heartbeat limit. Every digital page walked
        and every page OCR'd reports progress, in step with the work."""
        work = self._fake_ocr(monkeypatch, tmp_path)
        events: list[str] = []
        real_tesseract = __import__("pytesseract").image_to_string

        def tesseract(image, **kwargs):
            events.append("ocr")
            return real_tesseract(image, **kwargs)

        monkeypatch.setattr("pytesseract.image_to_string", tesseract)
        result = self._extract("dsdss", on_progress=lambda: events.append("progress"))
        assert result.status == STATUS_SUCCESS
        assert work["ocr_calls"] == 3
        # Five digital pages walked, then three pages OCR'd, each OCR
        # followed by a heartbeat before the next page starts.
        assert events == ["progress"] * 5 + ["ocr", "progress"] * 3

    def test_progress_callback_is_optional(self, monkeypatch, tmp_path):
        from src.extractors import pdf

        work = self._fake_ocr(monkeypatch, tmp_path)
        text, extractor = pdf.extract(self._pdf("ds"))
        assert extractor == "pdf-ocr"
        assert self.SCANNED in text
        assert work["ocr_calls"] == 1


class TestPdfExtractorVersion:
    """#292 changed what the PDF extractor returns for the same bytes,
    #691 makes AES-encrypted PDFs that need no open password extract
    instead of failing, #707 stops caching a success with pages
    dropped by host pressure, and #931 records a PDF that needs an open
    password or exceeds pypdf's limits ``unsupported``, so rows written
    before any of them must re-extract."""

    @pytest.mark.parametrize(
        "name",
        [
            "pdf-digital",
            "pdf-ocr",
            "pdf",
            "pdf-digital@2",
            "pdf-ocr@2",
            "pdf@2",
            "pdf-digital@3",
            "pdf-ocr@3",
            "pdf@3",
            "pdf-digital@4",
            "pdf-ocr@4",
            "pdf@4",
        ],
    )
    def test_pre_bump_pdf_rows_are_stale(self, name):
        from src.extractors import EXTRACTOR_VERSIONS, stale_extractor_module

        assert EXTRACTOR_VERSIONS["pdf"] == 5
        assert stale_extractor_module(name) == "pdf"

    @pytest.mark.parametrize("name", ["pdf-digital@5", "pdf-ocr@5", "pdf@5"])
    def test_current_pdf_rows_are_not_stale(self, name):
        from src.extractors import stale_extractor_module

        assert stale_extractor_module(name) is None


def _encrypted_pdf(text: str, *, user_password: str, algorithm: str) -> bytes:
    """A one-page digital PDF encrypted with pypdf. An empty
    ``user_password`` is the owner-password-only shape (print/copy
    restrictions, no open password)."""
    import io

    from pypdf import PdfWriter
    from pypdf.generic import ContentStream, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    stream = ContentStream(None, writer)
    stream._data = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    page[NameObject("/Contents")] = stream
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    writer.encrypt(
        user_password=user_password,
        owner_password="synthetic-owner-password",  # pragma: allowlist secret
        algorithm=algorithm,
    )
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _deep_page_tree_pdf(marker: str = "SYNTHETIC_DEEP_TREE_MARKER") -> bytes:
    """A one-page PDF whose page sits under more nested ``/Pages`` nodes
    than pypdf's ``page_tree_maximum_depth`` allows, so reading its pages
    raises ``LimitReachedError`` (#931). ``marker`` is the title, so a
    test can check it never reaches a log."""
    import io

    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Title": marker})
    pages = writer.root_object["/Pages"]
    kid = pages["/Kids"][0]
    # pypdf 6's limit is 100 levels.
    for _ in range(150):
        node = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Pages"),
                NameObject("/Kids"): ArrayObject([kid]),
                NameObject("/Count"): NumberObject(1),
            }
        )
        kid = writer._add_object(node)
    pages[NameObject("/Kids")] = ArrayObject([kid])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestPermanentFailuresAreUnsupported:
    """#931: an extractor exception the same bytes always repeat is
    recorded ``unsupported`` with a fixed text per type, stamped with the
    extractor so a later version bump still refreshes it. Matched by
    exact class: anything else, a subclass included, stays ``failed``."""

    def test_pdf_over_a_pypdf_limit_is_unsupported(self, monkeypatch, caplog):
        import io

        import pypdf
        from src.extractors import PDF_LIMIT_ERROR, pdf

        caplog.set_level("DEBUG")
        monkeypatch.setattr(pdf, "_extract_ocr", lambda *a, **kw: pytest.fail("OCR must not run"))
        payload = _deep_page_tree_pdf()
        with pytest.raises(pypdf.errors.LimitReachedError):
            list(pypdf.PdfReader(io.BytesIO(payload)).pages)

        result = extract(
            content_type="application/pdf",
            filename="SYNTHETIC_FILENAME_MARKER.pdf",
            payload=payload,
        )

        assert result == ExtractionResult(
            status=STATUS_UNSUPPORTED, extractor="pdf@5", text=None, error=PDF_LIMIT_ERROR
        )
        assert PDF_LIMIT_ERROR == "PDF structure exceeds pypdf limits"
        [record] = [r for r in caplog.records if r.name == "indexer.extractor"]
        assert record.levelname == "WARNING"
        assert record.getMessage() == (
            "extractor pdf declined (dispatch_via=mime): PDF structure exceeds pypdf "
            "limits; recorded unsupported, not retried"
        )
        for marker in ("SYNTHETIC_DEEP_TREE_MARKER", "SYNTHETIC_FILENAME_MARKER"):
            assert marker not in caplog.text

    @pytest.mark.parametrize("module", ["pptx", "docx"])
    def test_package_over_a_pre_open_budget_is_unsupported(self, module, monkeypatch, caplog):
        """#1032: the PPTX and DOCX pre-open package budgets are decided
        from the ZIP central directory alone, so the same bytes trip them
        on every run; a ``failed`` row would only re-run the check every
        7 days. Recorded ``unsupported`` with fixed text, before the
        package is opened, with the same WARNING as the other permanent
        failures; the marker stays out of the log and the fixed text."""
        from src import extractors

        if module == "pptx":
            from src.extractors import pptx as extractor_module

            payload = _deck(_boxes("SYNTHETIC_BUDGET_MARKER"))
            content_type, error = _PPTX_MIME, PPTX_PACKAGE_BUDGET_ERROR
        else:
            from src.extractors import docx as extractor_module

            payload = _docx_bytes("SYNTHETIC_BUDGET_MARKER")
            content_type, error = _DOCX_MIME, DOCX_PACKAGE_BUDGET_ERROR
        monkeypatch.setattr(extractor_module, "_MAX_MEMBERS", 1)
        opened = _count_calls(monkeypatch, extractor_module.Package, "open")
        caplog.set_level("DEBUG")

        result = extract(
            content_type=content_type,
            filename=f"SYNTHETIC_FILENAME_MARKER.{module}",
            payload=payload,
        )

        version = extractors.EXTRACTOR_VERSIONS[module]
        assert result == ExtractionResult(
            status=STATUS_UNSUPPORTED, extractor=f"{module}@{version}", text=None, error=error
        )
        assert opened[0] == 0
        [record] = [r for r in caplog.records if r.name == "indexer.extractor"]
        assert record.levelname == "WARNING"
        assert record.getMessage() == (
            f"extractor {module} declined (dispatch_via=mime): {error}; "
            "recorded unsupported, not retried"
        )
        for marker in ("SYNTHETIC_BUDGET_MARKER", "SYNTHETIC_FILENAME_MARKER"):
            assert marker not in caplog.text
            assert marker not in error

    def test_package_budget_fixed_texts(self):
        assert PPTX_PACKAGE_BUDGET_ERROR == "presentation exceeds a pre-open package budget"
        assert DOCX_PACKAGE_BUDGET_ERROR == "document exceeds a pre-open package budget"

    def test_pptx_version_bumped_so_failed_budget_rows_refresh(self):
        """#1032: the ``failed`` rows ``pptx@2`` wrote for a deck over a
        budget are stale, so the startup sweep re-runs them once and they
        are recorded ``unsupported``. The PPTX walk is budgeted (#936), so
        the re-run is bounded. ``docx`` was bumped only once its walk was
        budgeted too: see
        ``TestDocxPackageBudget.test_cached_rows_are_stale_once_the_walk_is_budgeted``."""
        from src.extractors import EXTRACTOR_VERSIONS, stale_extractor_module

        assert EXTRACTOR_VERSIONS["pptx"] == 3
        assert stale_extractor_module("pptx@2") == "pptx"
        assert stale_extractor_module("pptx@3") is None

    def test_a_page_level_pypdf_limit_keeps_the_other_pages(self, monkeypatch):
        """Review round 1: a limit hit inside one page's text extraction
        (``/ToUnicode`` size, for example) is a per-page failure, as
        before: the page is counted in ``pdf_pages_failed`` (and OCR'd when
        OCR is on) and the other pages' text is kept, rather than the
        whole document being recorded ``unsupported``."""
        from pypdf.errors import LimitReachedError
        from src import extractors
        from src.extractors import pdf

        good_text = "digital words " * 20

        class LimitPage:
            def extract_text(self):
                raise LimitReachedError("SYNTHETIC_PYPDF_MARKER")

        class GoodPage:
            def extract_text(self):
                return good_text

        class FakeReader:
            def __init__(self, stream):
                self.pages = [LimitPage(), GoodPage()]

        monkeypatch.setattr(pdf.pypdf, "PdfReader", FakeReader)
        extractors.drain_extractor_counts()

        result = extract(
            content_type="application/pdf", filename="a.pdf", payload=b"%PDF-1.7", ocr_enabled=False
        )

        assert (result.status, result.extractor) == (STATUS_SUCCESS, "pdf-digital@5")
        assert result.text == good_text.strip()
        assert extractors.drain_extractor_counts()["pdf_pages_failed"] == 1

    @pytest.mark.parametrize(
        ("module_name", "exc_path"),
        [
            ("pdf", "pypdf.errors.PdfReadError"),
            # A subclass of an allowlisted type is not matched.
            ("pdf", "pypdf.errors.WrongPasswordError"),
            # An allowlisted type from another extractor is not matched.
            ("xlsx", "pypdf.errors.FileNotDecryptedError"),
            ("pdf", "src.extractors.xlsx.XlsxEagerPartBudgetError"),
            # The legacy extractors' subprocess errors (#935, #957): a timeout or
            # a crash can be transient, so they stay ``failed`` for any
            # module.
            *(
                (module, f"src.extractors._runner.{name}")
                for module in ("doc", "xls", "ppt", "pdf", "xlsx")
                for name in (
                    "ToolNotFoundError",
                    "ToolTimeoutError",
                    "ToolCrashError",
                    "ToolExitError",
                )
            ),
        ],
    )
    def test_other_exceptions_stay_failed(self, monkeypatch, module_name, exc_path):
        import importlib

        module_path, _, name = exc_path.rpartition(".")
        exc_type = getattr(importlib.import_module(module_path), name)

        def boom(payload, **opts):
            raise exc_type()

        monkeypatch.setattr("src.extractors._safe_import", lambda module: boom)
        filename = f"a.{module_name}"
        # The XLSX path runs the zip pre-check first; give it a zip. A
        # legacy label keeps its legacy extractor only for an OLE2 payload.
        payload = b"x"
        if module_name in {"doc", "xls", "ppt"}:
            payload = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(64)
        if module_name == "xlsx":
            import io
            import zipfile

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as archive:
                archive.writestr("a", b"a")
            payload = buf.getvalue()

        result = extract(
            content_type="application/octet-stream", filename=filename, payload=payload
        )

        assert result.status == STATUS_FAILED
        assert result.error == name
        assert (result.extractor or "").startswith(f"{module_name}@")


class TestEncryptedPdf:
    """#691: pypdf needs ``cryptography`` for AES. An owner-password-only
    PDF opens with the empty user password and extracts like any other;
    one that needs a real open password is recorded as a failure by type,
    and no password is ever guessed."""

    # Long enough to clear the digital-text floor, so OCR never runs.
    MARKER = "SYNTHETIC_OWNER_ONLY_MARKER with enough digital text to clear the floor"

    @pytest.mark.parametrize("algorithm", ["AES-128", "AES-256", "RC4-128"])
    def test_owner_password_only_pdf_extracts(self, algorithm, monkeypatch):
        from src.extractors import pdf

        monkeypatch.setattr(pdf, "_extract_ocr", lambda *a, **kw: pytest.fail("OCR must not run"))
        payload = _encrypted_pdf(self.MARKER, user_password="", algorithm=algorithm)

        result = extract(content_type="application/pdf", filename="statement.pdf", payload=payload)

        assert result.status == STATUS_SUCCESS
        assert result.extractor == "pdf-digital@5"
        assert result.text == self.MARKER

    @pytest.mark.parametrize("algorithm", ["AES-128", "AES-256"])
    def test_pdf_needing_an_open_password_is_unsupported(self, algorithm, monkeypatch, caplog):
        """#931: no password is ever tried, so the same bytes always fail
        the same way: recorded ``unsupported`` with fixed text, not a
        ``failed`` row re-run every 7 days."""
        from src.extractors import ENCRYPTED_PDF_ERROR, pdf

        caplog.set_level("DEBUG")
        monkeypatch.setattr(pdf, "_extract_ocr", lambda *a, **kw: pytest.fail("OCR must not run"))
        payload = _encrypted_pdf(
            "SYNTHETIC_USER_PW_MARKER with enough digital text to clear the floor",
            user_password="SYNTHETIC_USER_PASSWORD",  # pragma: allowlist secret
            algorithm=algorithm,
        )

        result = extract(content_type="application/pdf", filename="locked.pdf", payload=payload)

        assert result.status == STATUS_UNSUPPORTED
        assert result.extractor == "pdf@5"
        assert result.error == ENCRYPTED_PDF_ERROR == "encrypted PDF (open password required)"
        assert result.text is None
        for marker in ("SYNTHETIC_USER_PW_MARKER", "SYNTHETIC_USER_PASSWORD", "synthetic-owner"):
            assert marker not in caplog.text
            assert marker not in (result.error or "")

    def test_only_the_empty_user_password_is_tried(self, monkeypatch):
        """No password guessing: the reader is opened with no password
        (pypdf then tries the empty one) and never ``decrypt``ed."""
        import pypdf
        from src.extractors import pdf

        opened: list[dict] = []

        class RecordingReader(pypdf.PdfReader):
            def __init__(self, stream, *args, **kwargs):
                opened.append(dict(kwargs, args=args))
                super().__init__(stream, *args, **kwargs)

            def decrypt(self, password):
                pytest.fail("the extractor must not try passwords")

        monkeypatch.setattr(pdf.pypdf, "PdfReader", RecordingReader)
        payload = _encrypted_pdf(self.MARKER, user_password="SYNTHETIC_PW", algorithm="AES-256")

        result = extract(content_type="application/pdf", filename="locked.pdf", payload=payload)

        assert result.status == STATUS_UNSUPPORTED
        assert opened == [{"args": ()}]


class TestCffFontPdf:
    """#691: pypdf reads the built-in encoding of an embedded CFF
    (``/FontFile3`` ``/Type1C``) font only with fontTools, which is held
    back until py-pdf/pypdf#4156 bounds its cost. Without it the codes
    fall through to StandardEncoding and the text comes out garbled, as
    a ``success`` row no retrieval test can tell from real text. The
    fixture pins that gap: its page's only text is set in such a font,
    whose encoding places each letter at its ROT13 code (recipe in
    ``fixtures/extractors/README.md``)."""

    # The sentence the fixture carries; ``cff-src/generate-cff-pdf.py``
    # holds the same string.
    SENTENCE = "Synthetic CFF marker: the quick brown fox jumps over the lazy dog."

    @staticmethod
    def _fixture() -> bytes:
        from pathlib import Path

        return (Path(__file__).parent / "fixtures" / "extractors" / "cff-font.pdf").read_bytes()

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "CFF built-in encodings need fontTools, held back under #691; a flip means "
            "fontTools landed and EXTRACTOR_VERSIONS['pdf'] needs a bump so cached rows re-extract"
        ),
    )
    def test_cff_font_text_extracts_as_written(self, monkeypatch):
        """Today the extractor returns the ROT13 of the sentence as
        ``success``; with fontTools it returns the sentence itself. Only
        the text is under the xfail: the module version is pinned in
        ``test_extracts_as_success_from_the_digital_path`` so that the
        bump fontTools brings cannot keep this test failing (review
        round 1 on #1114)."""
        from src.extractors import pdf

        monkeypatch.setattr(pdf, "_extract_ocr", lambda *a, **kw: pytest.fail("OCR must not run"))

        result = extract(
            content_type="application/pdf", filename="cff.pdf", payload=self._fixture()
        )

        assert result.status == STATUS_SUCCESS
        assert result.text == self.SENTENCE

    def test_extracts_as_success_from_the_digital_path(self, monkeypatch):
        """Whatever the text, the fixture is a ``success`` row from the
        digital path at the current module version: the garble is
        invisible to a status check, which is why the xfail above
        exists. Passes before and after fontTools; the version moves
        with the bump."""
        from src.extractors import pdf

        monkeypatch.setattr(pdf, "_extract_ocr", lambda *a, **kw: pytest.fail("OCR must not run"))

        result = extract(
            content_type="application/pdf", filename="cff.pdf", payload=self._fixture()
        )

        assert result.status == STATUS_SUCCESS
        assert result.extractor == "pdf-digital@5"
        assert result.text is not None and len(result.text) == len(self.SENTENCE)

    def test_fixture_resolves_text_through_the_cff_encoding_alone(self):
        """The gap stays pinned only while nothing but the font program
        maps the codes: no ``/Encoding``, no ``/ToUnicode``, a Type1C
        ``/FontFile3``. The file carries no document information
        dictionary or XMP, so no author or producer name is committed."""
        import io

        import pypdf

        payload = self._fixture()
        reader = pypdf.PdfReader(io.BytesIO(payload))

        assert len(payload) < 100_000
        assert len(reader.pages) == 1
        fonts = reader.pages[0]["/Resources"]["/Font"]
        assert list(fonts.keys()) == ["/F1"]
        font = fonts["/F1"].get_object()
        assert font["/Subtype"] == "/Type1"
        assert "/Encoding" not in font
        assert "/ToUnicode" not in font
        font_file = font["/FontDescriptor"]["/FontFile3"].get_object()
        assert font_file["/Subtype"] == "/Type1C"
        assert reader.metadata is None
        assert reader.xmp_metadata is None


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
        assert result.extractor == "image-ocr@3"

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

    def test_progress_is_reported_per_page(self, monkeypatch):
        """#485: each OCR'd page refreshes the heartbeat."""
        seen = self._ocr_by_color(monkeypatch)
        events: list[str] = []
        from src.extractors import image as image_module

        real_ocr = image_module.pytesseract.image_to_string

        def ocr(img, **kwargs):
            events.append("ocr")
            return real_ocr(img, **kwargs)

        monkeypatch.setattr(image_module.pytesseract, "image_to_string", ocr)
        result = extract(
            content_type="image/tiff",
            filename="scan.tiff",
            payload=self._frames("TIFF", 3),
            on_progress=lambda: events.append("progress"),
        )
        assert result.status == STATUS_SUCCESS
        assert seen == ["PAGE_0", "PAGE_1", "PAGE_2"]
        assert events == ["ocr", "progress"] * 3

    def test_progress_callback_is_optional(self, monkeypatch):
        from src.extractors import image as image_module

        seen = self._ocr_by_color(monkeypatch)
        text, extractor = image_module.extract(self._frames("TIFF", 2))
        assert extractor == "image-ocr"
        assert text.split() == ["PAGE_0", "PAGE_1"]
        assert seen == ["PAGE_0", "PAGE_1"]


class TestMultipageTiffOcrCap:
    """#885: OCR of a multipage TIFF stopped at ``max_ocr_pages`` without
    a word, so the later frames silently dropped out of search. A single
    probe seek past the cap finds whether a frame was left unread; the
    capped image is logged (rate limited, counts only) and counted for
    the attachments aggregate. The extraction result is unchanged."""

    COLORS = TestMultipageTiff.COLORS

    @pytest.fixture(autouse=True)
    def _fresh_counts(self):
        from src import extractors

        extractors.drain_extractor_counts()

    def _frames(self, count: int) -> bytes:
        import io

        from PIL import Image

        frames = [Image.new("RGB", (32, 32), c) for c in self.COLORS[:count]]
        buf = io.BytesIO()
        frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
        return buf.getvalue()

    def _corrupt_third_frame(self) -> bytes:
        """Three frames, the second frame's next-IFD offset pointing past
        the end of the file: seeking to the third frame raises a
        non-``EOFError`` (Pillow: ``TypeError``)."""
        import struct

        data = bytearray(self._frames(3))
        assert data[:2] == b"II"
        offset = struct.unpack_from("<I", data, 4)[0]
        ifds = []
        while offset:
            ifds.append(offset)
            entries = struct.unpack_from("<H", data, offset)[0]
            offset = struct.unpack_from("<I", data, offset + 2 + 12 * entries)[0]
        entries = struct.unpack_from("<H", data, ifds[1])[0]
        struct.pack_into("<I", data, ifds[1] + 2 + 12 * entries, len(data) + 1000)
        return bytes(data)

    def _ocr(self, monkeypatch) -> list[str]:
        from src.extractors import image as image_module

        seen: list[str] = []

        def fake_ocr(img, **_kwargs):
            marker = f"PAGE_{self.COLORS.index(img.convert('RGB').getpixel((0, 0)))}"
            seen.append(marker)
            return marker

        monkeypatch.setattr(image_module.pytesseract, "image_to_string", fake_ocr)
        return seen

    @staticmethod
    def _count_seeks(monkeypatch) -> list[int]:
        from PIL import TiffImagePlugin

        seeks: list[int] = []
        real_seek = TiffImagePlugin.TiffImageFile.seek

        def seek(self, frame):
            seeks.append(frame)
            return real_seek(self, frame)

        monkeypatch.setattr(TiffImagePlugin.TiffImageFile, "seek", seek)
        return seeks

    @staticmethod
    def _cap_lines(caplog) -> list:
        return [r for r in caplog.records if "image OCR capped" in r.getMessage()]

    def _extract(self, payload: bytes, max_ocr_pages: int) -> ExtractionResult:
        return extract(
            content_type="image/tiff",
            filename="SYNTHETIC_FILENAME_MARKER.tiff",
            payload=payload,
            max_ocr_pages=max_ocr_pages,
        )

    def test_a_frame_past_the_cap_is_logged_and_counted(self, monkeypatch, caplog):
        from src import extractors

        caplog.set_level("INFO")
        seen = self._ocr(monkeypatch)
        seeks = self._count_seeks(monkeypatch)
        result = self._extract(self._frames(3), max_ocr_pages=2)
        assert (result.status, result.extractor) == (STATUS_SUCCESS, "image-ocr@3")
        assert result.text is not None
        assert result.text.split() == ["PAGE_0", "PAGE_1"]
        assert seen == ["PAGE_0", "PAGE_1"]
        # One seek to the second frame, one probe past the cap; the probed
        # frame is never OCR'd and the frame chain is not walked further.
        assert seeks == [1, 2]
        [line] = self._cap_lines(caplog)
        assert line.levelname == "WARNING"
        assert line.getMessage() == "image OCR capped at 2 of at least 3 frames"
        counts = extractors.drain_extractor_counts()
        assert counts["ocr_capped_images"] == 1
        assert counts["ocr_capped_pdfs"] == 0
        assert counts["ocr_pages_skipped"] == 0
        assert "SYNTHETIC_FILENAME_MARKER" not in caplog.text
        assert "PAGE_" not in caplog.text

    @pytest.mark.parametrize(
        "frames, cap, expected_seeks",
        [
            (2, 2, [1, 2]),  # the probe finds no further frame
            (3, 3, [1, 2, 3]),
            (3, 0, [1, 2, 3]),  # no cap: no probe, the loop ends at EOF
            (3, 20, [1, 2, 3]),
            (1, 1, [1]),
        ],
    )
    def test_no_cap_line_when_every_frame_is_read(
        self, monkeypatch, caplog, frames, cap, expected_seeks
    ):
        from src import extractors

        caplog.set_level("INFO")
        seen = self._ocr(monkeypatch)
        seeks = self._count_seeks(monkeypatch)
        result = self._extract(self._frames(frames), max_ocr_pages=cap)
        assert result.status == STATUS_SUCCESS
        assert seen == [f"PAGE_{i}" for i in range(frames)]
        assert seeks == expected_seeks
        assert self._cap_lines(caplog) == []
        assert extractors.drain_extractor_counts()["ocr_capped_images"] == 0

    @pytest.mark.filterwarnings("ignore:Corrupt EXIF data:UserWarning")
    def test_an_unreadable_probe_frame_leaves_the_result_unchanged(self, monkeypatch, caplog):
        """The probe seek can raise on a corrupt file; the frames already
        read are the result, as without the probe."""
        from src import extractors

        caplog.set_level("INFO")
        self._ocr(monkeypatch)
        intact = self._extract(self._frames(3), max_ocr_pages=2)
        extractors.drain_extractor_counts()
        caplog.clear()
        seen = self._ocr(monkeypatch)
        seeks = self._count_seeks(monkeypatch)
        result = self._extract(self._corrupt_third_frame(), max_ocr_pages=2)
        assert result == intact
        assert seen == ["PAGE_0", "PAGE_1"]
        assert seeks == [1, 2]
        [line] = self._cap_lines(caplog)
        assert line.levelname == "WARNING"
        assert line.getMessage() == (
            "image OCR capped at 2 frames; the next frame could not be read (TypeError)"
        )
        assert extractors.drain_extractor_counts()["ocr_capped_images"] == 1
        assert "SYNTHETIC_FILENAME_MARKER" not in caplog.text

    @pytest.mark.parametrize("exc", [MemoryError, RecursionError])
    def test_host_pressure_in_the_probe_is_not_swallowed(self, monkeypatch, exc):
        """The dispatcher re-raises these as host pressure; the probe
        must not turn them into a capped success."""
        from PIL import TiffImagePlugin

        self._ocr(monkeypatch)
        real_seek = TiffImagePlugin.TiffImageFile.seek

        def seek(self, frame):
            if frame == 2:
                raise exc
            return real_seek(self, frame)

        monkeypatch.setattr(TiffImagePlugin.TiffImageFile, "seek", seek)
        with pytest.raises(exc):
            self._extract(self._frames(3), max_ocr_pages=2)

    def test_cap_lines_are_rate_limited_and_every_image_counted(self, monkeypatch, caplog):
        from src import extractors

        caplog.set_level("INFO")
        self._ocr(monkeypatch)
        monkeypatch.setattr(extractors._LINE_BUDGET, "limit", 2)
        results = [self._extract(self._frames(3), max_ocr_pages=1) for _ in range(5)]
        assert {(r.status, r.text) for r in results} == {(STATUS_SUCCESS, "PAGE_0")}
        assert [r.levelname for r in self._cap_lines(caplog)] == ["WARNING", "WARNING"]
        counts = extractors.drain_extractor_counts()
        assert counts["ocr_capped_images"] == 5
        assert counts["warnings_suppressed"] == 3

    def test_a_capped_image_makes_the_attachments_line_a_warning(self, monkeypatch, caplog):
        from src import attachment_indexing, main

        caplog.set_level("INFO")
        self._ocr(monkeypatch)
        attachment_indexing.attachment_outcomes.drain()
        self._extract(self._frames(3), max_ocr_pages=2)
        attachment_indexing.attachment_outcomes.record(STATUS_SUCCESS, None, cached=False)
        main._log_attachment_outcomes(force=True)
        [line] = [r for r in caplog.records if r.getMessage().startswith("attachments n=")]
        assert line.levelname == "WARNING"
        assert "ocr_capped_images=1" in line.getMessage()


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

    def test_pypdf_page_failures_are_counted(self, monkeypatch):
        """#871: each page pypdf cannot read is counted for the INFO
        attachments aggregate; the pages returned are unchanged."""
        from src import extractors
        from src.extractors import pdf

        class BadPage:
            def extract_text(self):
                raise ValueError("SYNTHETIC_PYPDF_MARKER")

        class GoodPage:
            def extract_text(self):
                return "  digital words  "

        class FakeReader:
            def __init__(self, stream):
                self.pages = [BadPage(), GoodPage(), BadPage()]

        monkeypatch.setattr(pdf.pypdf, "PdfReader", FakeReader)
        extractors.drain_extractor_counts()
        assert pdf._extract_digital_pages(b"%PDF-1.7") == ["", "digital words", ""]
        assert extractors.drain_extractor_counts() == {
            "pdf_pages_failed": 2,
            "pdf_pages_unrecovered": 0,
            "ocr_capped_pdfs": 0,
            "ocr_pages_skipped": 0,
            "ocr_capped_images": 0,
            "extractor_caps": 0,
            "parser_caps_messages": 0,
            "parser_recipients_merged_messages": 0,
            "parser_sender_ambiguous_messages": 0,
            "warnings_suppressed": 0,
        }
        assert extractors.drain_extractor_counts()["pdf_pages_failed"] == 0

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


@pytest.fixture
def library_logger_levels():
    """Start the pypdf and PIL loggers at NOTSET and restore their levels
    afterwards, so a test can show the library logs before the guard
    runs. ``setLevel`` (not attribute assignment) clears the logging
    module's per-logger level cache.

    ``src.main`` is imported first: its import runs the guard, so a
    first import inside the test would silence the loggers again before
    the unguarded run (#869)."""
    import importlib

    importlib.import_module("src.main")
    loggers = [logging.getLogger(name) for name in ("pypdf", "PIL")]
    saved = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.NOTSET)
    yield
    for logger, level in zip(loggers, saved, strict=True):
        logger.setLevel(level)


def _pdf_with_marker_font() -> bytes:
    """A one-page digital PDF whose font names a marker base font and a
    marker encoding pypdf does not implement, so pypdf's ``_cmap`` logger
    reports the encoding name it read from the document."""
    import io

    from pypdf import PdfWriter
    from pypdf.generic import ContentStream, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    stream = ContentStream(None, writer)
    stream._data = b"BT /F1 12 Tf 72 720 Td (Invoice number 42 with enough digital text) Tj ET"
    page[NameObject("/Contents")] = stream
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/MARKER690+Helvetica"),
            NameObject("/Encoding"): NameObject("/MARKER690Encoding"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _xlsx_with_out_of_range_date() -> bytes:
    """A workbook whose date-formatted cell holds a serial value outside
    the date range, so openpyxl warns with the value it read."""
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = 987654321690.0
    ws["A1"].number_format = "yyyy-mm-dd"
    ws["A2"] = "hello"
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def _tiff_with_bad_tag_count() -> bytes:
    """A TIFF whose ImageWidth tag has two entries: Pillow warns with the
    tag number and the count read from the file, then fails to open it."""
    import struct

    entries = [(256, 3, 2, 0), (257, 3, 1, 10), (258, 3, 1, 8), (262, 3, 1, 1)]
    ifd = struct.pack("<H", len(entries))
    for tag, typ, count, value in entries:
        ifd += struct.pack("<HHII", tag, typ, count, value)
    data = b"II*\x00" + struct.pack("<I", 8) + ifd + b"\x00\x00\x00\x00"
    return data.ljust(300, b"\x00")


class TestDocumentLibraryOutputIsSilenced:
    """#690: pypdf, Pillow and openpyxl log or warn with values read from
    the document (font dictionaries, encoding names, cell values, TIFF
    tags). The indexer's logging setup silences them; the dispatcher's
    own fixed-text logging and ``failed`` status still report outcomes.

    Each test first runs the extraction without the guard to show the
    library does emit the document value, then with it."""

    XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def test_logging_setup_raises_library_logger_levels(self):
        import importlib

        # The module-level setup runs when ``src.main`` is imported.
        importlib.import_module("src.main")
        assert logging.getLogger("pypdf").level == logging.CRITICAL
        assert logging.getLogger("PIL").level == logging.CRITICAL

    def test_pypdf_document_values_stay_out_of_the_log(self, caplog, library_logger_levels):
        import warnings

        from src import main

        payload = _pdf_with_marker_font()
        caplog.set_level("DEBUG")
        before = extract(content_type="application/pdf", filename="a.pdf", payload=payload)
        assert "MARKER690" in caplog.text
        assert "pypdf._cmap" in caplog.text

        caplog.clear()
        with warnings.catch_warnings():
            main.quiet_document_libraries()
            after = extract(content_type="application/pdf", filename="a.pdf", payload=payload)

        assert after == before
        assert after.status == STATUS_SUCCESS
        assert after.text == "Invoice number 42 with enough digital text"
        assert "MARKER690" not in caplog.text
        assert "pypdf" not in caplog.text
        assert "Advanced encoding" not in caplog.text

    def test_openpyxl_cell_values_stay_out_of_warnings(self):
        import warnings

        from src import main

        payload = _xlsx_with_out_of_range_date()
        with warnings.catch_warnings(record=True) as control:
            warnings.simplefilter("always")
            before = extract(content_type=self.XLSX, filename="a.xlsx", payload=payload)
        assert any("987654321690" in str(w.message) for w in control)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            main.quiet_document_libraries()
            after = extract(content_type=self.XLSX, filename="a.xlsx", payload=payload)

        assert after == before
        assert after.status == STATUS_SUCCESS
        assert not [w for w in caught if "987654321690" in str(w.message)]
        assert not [w for w in caught if "openpyxl" in w.filename]

    def test_pillow_tag_values_stay_out_and_failure_still_surfaces(
        self, caplog, library_logger_levels
    ):
        import warnings

        from src import main

        payload = _tiff_with_bad_tag_count()
        caplog.set_level("DEBUG")
        with warnings.catch_warnings(record=True) as control:
            warnings.simplefilter("always")
            before = extract(content_type="image/tiff", filename="a.tiff", payload=payload)
        assert any("Metadata Warning" in str(w.message) for w in control)

        caplog.clear()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            main.quiet_document_libraries()
            after = extract(content_type="image/tiff", filename="a.tiff", payload=payload)

        assert after == before
        # The failure is still reported, by the dispatcher's own
        # fixed-text log line and status, not by the silenced library.
        assert after.status == STATUS_FAILED
        assert after.error == "UnidentifiedImageError"
        assert "UnidentifiedImageError" in caplog.text
        assert not [w for w in caught if "Metadata Warning" in str(w.message)]
        assert not [w for w in caught if "/PIL/" in w.filename]

    def test_decompression_bomb_still_fails_under_the_guard(self, monkeypatch):
        """The image extractor promotes ``DecompressionBombWarning`` to an
        error in its own ``catch_warnings`` scope; the guard's Pillow
        ``ignore`` filter must not swallow it."""
        import io
        import warnings

        from PIL import Image
        from src import main

        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1500)
        buf = io.BytesIO()
        Image.new("RGB", (50, 50), color="white").save(buf, format="PNG")

        with warnings.catch_warnings():
            main.quiet_document_libraries()
            result = extract(content_type="image/png", filename="a.png", payload=buf.getvalue())

        assert result.status == STATUS_FAILED
        assert result.error == "DecompressionBombWarning"


def _heic(size: tuple[int, int] = (64, 48), color: str = "red") -> bytes:
    """A synthetic HEIC, encoded with the libheif bundled in pillow-heif."""
    import io

    import pillow_heif
    from PIL import Image

    buf = io.BytesIO()
    pillow_heif.from_pillow(Image.new("RGB", size, color)).save(buf, quality=90)
    return buf.getvalue()


def _png(size: tuple[int, int] = (64, 48), color: str = "red") -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class TestHeicImages:
    """#691: iPhone HEIC/HEIF photos open through pillow-heif's Pillow
    plugin and go through exactly the guards every other image does: the
    dispatcher's byte cap, the process-wide pixel cap and decompression-bomb
    handling, and the OCR page and time bounds."""

    @pytest.mark.parametrize(
        ("content_type", "filename"),
        [
            ("image/heic", "IMG_0001.HEIC"),
            ("image/heif", "photo.heif"),
            ("application/octet-stream", "IMG_0001.heic"),
            ("application/octet-stream", "photo.HEIF"),
            ("", "photo.heic"),
            # Review round 1: ``.hif`` (Canon / Fujifilm cameras) is a
            # still HEIF that pillow-heif registers too.
            ("application/octet-stream", "IMG_0001.HIF"),
            ("", "photo.hif"),
        ],
    )
    def test_heic_routes_to_the_image_extractor(self, content_type, filename):
        from src.extractors import resolved_extractor_module

        assert resolved_extractor_module(content_type, filename) == "image"

    @pytest.mark.parametrize("filename", ["burst.heics", "burst.heifs"])
    def test_heif_sequence_extensions_are_not_routed(self, filename):
        """``.heics`` / ``.heifs`` are image sequences (track-based, often
        with no still primary image). They are not routed by extension; a
        sequence sent with an ``image/`` MIME type still reaches the image
        extractor, which reads at most its primary image."""
        from src.extractors import resolved_extractor_module

        assert resolved_extractor_module("application/octet-stream", filename) is None
        assert resolved_extractor_module("image/heic-sequence", filename) == "image"

    def test_extension_dispatch_covers_every_still_heif_extension_pillow_registers(self):
        from PIL import Image
        from src.extractors import (
            _EXT_DISPATCH,
            image,  # noqa: F401 - registers the opener
        )

        heif = {ext for ext, fmt in Image.registered_extensions().items() if fmt == "HEIF"}
        sequences = {".heics", ".heifs"}
        assert sequences <= heif
        assert {ext for ext in heif - sequences if _EXT_DISPATCH.get(ext) != "image"} == set()

    def test_heic_photo_is_decoded_and_ocrd(self, monkeypatch):
        from src.extractors import image as image_module

        seen: list[tuple[tuple[int, int], tuple[int, int, int]]] = []

        def fake_ocr(img, **kwargs):
            rgb = img.convert("RGB")
            seen.append((rgb.size, rgb.getpixel((32, 24))))
            assert kwargs == {"timeout": 7.0}
            return "SYNTHETIC_HEIC_TEXT"

        monkeypatch.setattr(image_module.pytesseract, "image_to_string", fake_ocr)

        result = extract(
            content_type="application/octet-stream",
            filename="IMG_0001.HEIC",
            payload=_heic(),
            ocr_timeout_seconds=7,
        )

        assert result.status == STATUS_SUCCESS
        assert result.extractor == "image-ocr@3"
        assert result.text == "SYNTHETIC_HEIC_TEXT"
        # One page OCR'd, decoded at its real size and (lossy) colour.
        assert len(seen) == 1
        size, (r, g, b) = seen[0]
        assert size == (64, 48)
        assert r > 200 and g < 60 and b < 60

    @pytest.mark.parametrize(
        ("make", "content_type"), [(_png, "image/png"), (_heic, "image/heic")], ids=["png", "heic"]
    )
    def test_oversized_payload_is_skipped_before_decoding(self, make, content_type, monkeypatch):
        from src.extractors import image as image_module

        monkeypatch.setattr(
            image_module.pytesseract,
            "image_to_string",
            lambda *a, **kw: pytest.fail("an oversized image must not be OCR'd"),
        )
        payload = make()

        result = extract(
            content_type=content_type,
            filename="photo",
            payload=payload,
            max_bytes=len(payload) - 1,
        )

        assert result.status == STATUS_TOO_LARGE
        assert result.extractor is None

    @pytest.mark.parametrize(
        ("cap", "error"),
        [(1500, "DecompressionBombWarning"), (100, "DecompressionBombError")],
    )
    def test_pixel_bomb_heic_fails_like_a_png_without_decoding(self, cap, error, monkeypatch):
        """50x50 = 2500 pixels: 1.67x a 1500 cap is the warning band the
        extractor promotes to an error, 25x a 100 cap is Pillow's hard
        error. Both reject from the header size, before any HEVC decode."""
        from PIL import Image
        from pillow_heif.as_plugin import HeifImageFile
        from src.extractors import image as image_module

        payloads = {"png": _png((50, 50)), "heic": _heic((50, 50))}
        decodes: list[int] = []
        real_load = HeifImageFile.load

        def counting_load(self):
            decodes.append(1)
            return real_load(self)

        monkeypatch.setattr(HeifImageFile, "load", counting_load)
        monkeypatch.setattr(
            image_module.pytesseract,
            "image_to_string",
            lambda *a, **kw: pytest.fail("a pixel bomb must not be OCR'd"),
        )
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", cap)

        results = {
            kind: extract(content_type=f"image/{kind}", filename=f"a.{kind}", payload=payload)
            for kind, payload in payloads.items()
        }

        assert results["heic"] == results["png"]
        assert results["heic"].status == STATUS_FAILED
        assert results["heic"].error == error
        assert results["heic"].extractor == "image@3"
        assert decodes == []

    def test_only_the_primary_heif_image_is_ocrd(self, monkeypatch):
        """A HEIF holding several top-level images is not a TIFF: like an
        animated GIF, only the primary image is read, so the OCR work per
        file stays one page."""
        import io

        import pillow_heif
        from PIL import Image
        from src.extractors import image as image_module

        heif = pillow_heif.from_pillow(Image.new("RGB", (32, 32), "red"))
        for color in ("green", "blue"):
            heif.add_from_pillow(Image.new("RGB", (32, 32), color))
        buf = io.BytesIO()
        heif.save(buf)
        calls: list[int] = []

        def fake_ocr(img, **kwargs):
            calls.append(1)
            return "page"

        monkeypatch.setattr(image_module.pytesseract, "image_to_string", fake_ocr)

        result = extract(content_type="image/heif", filename="burst.heif", payload=buf.getvalue())

        assert result.status == STATUS_SUCCESS
        assert calls == [1]

    def test_auxiliary_heif_images_are_not_decoded(self):
        """Only the HEIF opener is registered, with thumbnails, depth and
        auxiliary images off, so a photo's extra images cost no decode."""
        import pillow_heif
        from src.extractors import image  # noqa: F401 - registers the opener

        assert pillow_heif.options.THUMBNAILS is False
        assert pillow_heif.options.DEPTH_IMAGES is False
        assert pillow_heif.options.AUX_IMAGES is False

    def test_image_version_3_rows_are_current(self):
        from src.extractors import EXTRACTOR_VERSIONS, stale_extractor_module

        assert EXTRACTOR_VERSIONS["image"] == 3
        assert stale_extractor_module("image@2") == "image"
        assert stale_extractor_module("image-ocr@2") == "image"
        assert stale_extractor_module("image-ocr@3") is None


def _docx_bytes(text: str) -> bytes:
    import io

    import docx

    document = docx.Document()
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Legacy binary Office labels, and the OOXML extractor each falls back to
# for a payload that is not OLE2 (#694, #935).
_LEGACY_LABELS = (
    ("application/msword", "a.bin", "docx"),
    ("application/octet-stream", "a.doc", "docx"),
    ("application/vnd.ms-excel", "a.bin", "xlsx"),
    ("application/octet-stream", "a.xls", "xlsx"),
)


def _count_extractor_calls(monkeypatch) -> list[str]:
    """Wrap ``_safe_import`` so each per-format extractor call is recorded
    by module name before the real extractor runs."""
    from src import extractors

    calls: list[str] = []
    real_import = extractors._safe_import

    def counting_import(module_name):
        fn = real_import(module_name)
        assert fn is not None

        def wrapped(payload, **opts):
            calls.append(module_name)
            return fn(payload, **opts)

        return wrapped

    monkeypatch.setattr(extractors, "_safe_import", counting_import)
    return calls


class TestLegacyOfficeLabels:
    """#694, #935: ``application/msword`` / ``.doc`` and
    ``application/vnd.ms-excel`` / ``.xls`` route a genuine legacy binary
    (an OLE2 compound file) to the ``doc`` / ``xls`` extractor, and any
    other payload to the OOXML extractor, as a best effort for OOXML files
    mislabelled as a legacy type."""

    def test_zip_payload_with_a_legacy_label_still_reaches_the_ooxml_extractor(self, monkeypatch):
        calls = _count_extractor_calls(monkeypatch)
        cases = {
            "docx": _docx_bytes("SYNTHETIC_DOC_TEXT"),
            "xlsx": _xlsx_bytes([["SYNTHETIC_DOC_TEXT"]]),
        }
        for content_type, filename, module in _LEGACY_LABELS:
            result = extract(content_type=content_type, filename=filename, payload=cases[module])
            assert result.status == STATUS_SUCCESS, (content_type, filename)
            assert result.extractor is not None
            assert result.extractor.startswith(f"{module}@"), (content_type, filename)
            assert "SYNTHETIC_DOC_TEXT" in (result.text or "")
        assert calls == [module for _, _, module in _LEGACY_LABELS]

    def test_other_payloads_with_a_legacy_label_still_fail_in_the_extractor(
        self, monkeypatch, caplog
    ):
        """Neither ZIP nor OLE2: today's behaviour, the extractor runs and
        its exception is recorded as ``failed`` by type, with a WARNING."""
        caplog.set_level("INFO")
        calls = _count_extractor_calls(monkeypatch)
        payload = b"SYNTHETIC_PAYLOAD_MARKER not a zip and not OLE2"
        for content_type, filename, module in _LEGACY_LABELS:
            result = extract(content_type=content_type, filename=filename, payload=payload)
            assert result.status == STATUS_FAILED, (content_type, filename)
            assert result.extractor is not None
            assert result.extractor.startswith(f"{module}@")
            assert result.error == "BadZipFile"
        assert calls == [module for _, _, module in _LEGACY_LABELS]
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == len(_LEGACY_LABELS)
        assert "SYNTHETIC_PAYLOAD_MARKER" not in caplog.text

    def test_ole2_payload_with_a_legacy_label_reaches_the_legacy_extractor(self, monkeypatch):
        """#935: a genuine ``.doc`` / ``.xls`` goes to the legacy extractor
        (#694 recorded it ``unsupported``)."""
        from src import extractors

        calls: list[str] = []

        def stub(module_name):
            def run(payload, **_opts):
                calls.append(module_name)
                return "legacy words", module_name

            return run

        monkeypatch.setattr(extractors, "_safe_import", stub)
        payload = _OLE2_MAGIC + bytes(512)
        for content_type, filename, _ in _LEGACY_LABELS:
            result = extract(content_type=content_type, filename=filename, payload=payload)
            assert result.status == STATUS_SUCCESS, (content_type, filename)
            assert result.extractor == f"{calls[-1]}@1"
        assert calls == ["doc", "doc", "xls", "xls"]

    def test_ole2_check_reads_only_the_signature(self, monkeypatch):
        """A payload shorter than the signature, or one that only starts
        like it, keeps today's path."""
        calls = _count_extractor_calls(monkeypatch)
        for payload in (_OLE2_MAGIC[:4], b"\xd0\xcf\x11\xe0\x00\x00\x00\x00junk"):
            result = extract(content_type="application/msword", filename="a.doc", payload=payload)
            assert result.status == STATUS_FAILED
        assert calls == ["docx", "docx"]

    def test_ole2_payload_without_a_legacy_label_stays_unsupported(self, monkeypatch):
        """An OLE2 payload bound for either OOXML extractor with no legacy
        label is ``unsupported`` (#694): under an OOXML label (for example
        a password-protected OOXML package, which is also OLE2; the MIME
        type wins over a ``.doc`` name). #935 keeps this."""
        from src.extractors import LEGACY_OLE2_ERROR

        calls = _count_extractor_calls(monkeypatch)
        payload = _OLE2_MAGIC + bytes(64)
        for content_type, filename in (
            (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "a.doc",
            ),
            ("application/octet-stream", "a.docx"),
            ("application/octet-stream", "a.xlsx"),
        ):
            result = extract(content_type=content_type, filename=filename, payload=payload)
            assert (result.status, result.error) == (STATUS_UNSUPPORTED, LEGACY_OLE2_ERROR), (
                content_type,
                filename,
            )
        assert calls == []

    def test_ole2_payload_labelled_as_text_is_a_binary_payload(self, monkeypatch):
        """#932: an OLE2 payload labelled ``.txt`` is not decoded either; the
        text guard records it with its own fixed reason."""
        from src.extractors import BINARY_AS_TEXT_ERROR

        calls = _count_extractor_calls(monkeypatch)
        result = extract(
            content_type="text/plain", filename="a.txt", payload=_OLE2_MAGIC + b"words"
        )
        assert (result.status, result.error) == (STATUS_UNSUPPORTED, BINARY_AS_TEXT_ERROR)
        assert calls == []

    def test_docx_and_xlsx_rows_from_before_the_ole2_check_are_stale(self):
        """The recorded outcome changed for OLE2 payloads (``failed`` became
        ``unsupported``), so rows the previous versions wrote re-extract."""
        from src.extractors import EXTRACTOR_VERSIONS, stale_extractor_module

        assert EXTRACTOR_VERSIONS["docx"] >= 4
        assert EXTRACTOR_VERSIONS["xlsx"] >= 5
        assert stale_extractor_module("docx@3") == "docx"
        assert stale_extractor_module("xlsx@4") == "xlsx"


# Fixed binary signatures the text guard rejects (#932).
_BINARY_SIGNATURES = {
    "pdf": b"%PDF-",
    "zip": b"PK\x03\x04",
    "zip-empty": b"PK\x05\x06",
    "ole2": _OLE2_MAGIC,
    "png": b"\x89PNG\r\n\x1a\n",
    "jpeg": b"\xff\xd8\xff",
    "gif87a": b"GIF87a",
    "gif89a": b"GIF89a",
}

# Occurrences that select the text extractor: a ``text/plain`` label, and a
# ``.txt`` name with no Content-Type.
_TEXT_LABELS = (
    ("text/plain", "SYNTHETIC_FILENAME_MARKER.pdf"),
    ("", "SYNTHETIC_FILENAME_MARKER.txt"),
)

# Genuine text in each encoding the text extractor decodes today.
_GENUINE_TEXT = "SYNTHETIC invoice 1234, résumé café"
_GENUINE_TEXT_PAYLOADS = {
    "utf-8": _GENUINE_TEXT.encode("utf-8"),
    "utf-8-bom": _GENUINE_TEXT.encode("utf-8-sig"),
    "utf-16-bom": _GENUINE_TEXT.encode("utf-16"),
    "utf-16-le-bomless": _GENUINE_TEXT.encode("utf-16-le"),
    "utf-16-be-bomless": _GENUINE_TEXT.encode("utf-16-be"),
    "cp1252": _GENUINE_TEXT.encode("cp1252"),
}


class TestBinaryPayloadLabelledAsText:
    """#932: a binary payload labelled as text was decoded with replacement
    characters and cached ``success``. A payload bound for the text
    extractor that starts with a fixed binary signature is recorded
    ``unsupported`` instead, decided by the bytes alone."""

    @pytest.mark.parametrize("encoding", sorted(_GENUINE_TEXT_PAYLOADS))
    @pytest.mark.parametrize(("content_type", "filename"), _TEXT_LABELS)
    def test_genuine_text_keeps_todays_path(self, encoding, content_type, filename, monkeypatch):
        calls = _count_extractor_calls(monkeypatch)
        result = extract(
            content_type=content_type,
            filename=filename,
            payload=_GENUINE_TEXT_PAYLOADS[encoding],
        )
        assert result.status == STATUS_SUCCESS
        assert result.text == _GENUINE_TEXT
        assert result.extractor is not None and result.extractor.startswith("text@")
        assert calls == ["text"]

    @pytest.mark.parametrize("signature", sorted(_BINARY_SIGNATURES))
    @pytest.mark.parametrize(("content_type", "filename"), _TEXT_LABELS)
    def test_binary_signature_is_unsupported_without_decoding(
        self, signature, content_type, filename, monkeypatch, caplog
    ):
        from src.extractors import BINARY_AS_TEXT_ERROR

        caplog.set_level("DEBUG")
        calls = _count_extractor_calls(monkeypatch)
        payload = _BINARY_SIGNATURES[signature] + b"SYNTHETIC_PAYLOAD_MARKER" + bytes(64)
        result = extract(content_type=content_type, filename=filename, payload=payload)
        assert result == ExtractionResult(
            status=STATUS_UNSUPPORTED, extractor=None, text=None, error=BINARY_AS_TEXT_ERROR
        )
        assert calls == []
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        for marker in ("SYNTHETIC_PAYLOAD_MARKER", "SYNTHETIC_FILENAME_MARKER"):
            assert marker not in caplog.text
            assert marker not in BINARY_AS_TEXT_ERROR

    @pytest.mark.parametrize("signature", sorted(_BINARY_SIGNATURES))
    def test_partial_signature_keeps_todays_path(self, signature, monkeypatch):
        """Only the whole fixed prefix matches: a payload shorter than the
        signature, or one that diverges at its last byte, is decoded."""
        calls = _count_extractor_calls(monkeypatch)
        magic = _BINARY_SIGNATURES[signature]
        for payload in (magic[:-1], magic[:-1] + b"\x00text"):
            result = extract(content_type="text/plain", filename="a.txt", payload=payload)
            assert result.status in {STATUS_SUCCESS, STATUS_EMPTY}, (signature, payload)
        assert calls == ["text", "text"]

    def test_signature_after_the_first_byte_is_not_matched(self, monkeypatch):
        calls = _count_extractor_calls(monkeypatch)
        result = extract(
            content_type="text/plain", filename="a.txt", payload=b"see %PDF-1.7 in the text"
        )
        assert result.status == STATUS_SUCCESS
        assert calls == ["text"]

    def test_signature_check_is_bounded_on_a_large_payload(self, monkeypatch):
        """The guard is a fixed-prefix check: a payload at the default size
        cap is rejected without reading past its signature, and genuine
        text of the same size still reaches the extractor once."""
        import time

        from src.extractors import BINARY_AS_TEXT_ERROR, DEFAULT_MAX_BYTES

        calls = _count_extractor_calls(monkeypatch)
        tail = b"A" * (DEFAULT_MAX_BYTES - 16)
        start = time.perf_counter()
        for magic in _BINARY_SIGNATURES.values():
            result = extract(content_type="text/plain", filename="a.txt", payload=magic + tail)
            assert (result.status, result.error) == (STATUS_UNSUPPORTED, BINARY_AS_TEXT_ERROR)
        assert time.perf_counter() - start < 2.0
        assert calls == []

    def test_other_extractors_still_read_their_formats(self, monkeypatch):
        """The guard covers only the text extractor: a PNG labelled as an
        image still reaches the image extractor."""
        calls = _count_extractor_calls(monkeypatch)
        extract(content_type="image/png", filename="a.png", payload=b"\x89PNG\r\n\x1a\n")
        assert calls == ["image"]

    def test_text_rows_from_before_the_guard_are_stale(self):
        """``success`` rows the previous text version wrote for binary
        payloads re-run once."""
        from src.extractors import EXTRACTOR_VERSIONS, stale_extractor_module

        assert EXTRACTOR_VERSIONS["text"] == 3
        assert stale_extractor_module("text@2") == "text"
        assert stale_extractor_module("text@3") is None


_DOTX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.template"
_DOTX_MAIN_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"


def _rich_docx_bytes() -> bytes:
    """A synthetic ``.docx`` with a body fact, a table cell and non-ASCII text."""
    import io

    import docx

    document = docx.Document()
    document.add_paragraph("SYNTHETIC_DOTX_FACT renewal due 2031-04-01")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "SYNTHETIC_DOTX_CELL"
    table.cell(0, 1).text = "Grüße, café, 東京"
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _dotx_bytes(docx_payload: bytes) -> bytes:
    """A synthetic Word template: the ``.docx`` with its main part declared
    as the template content type, as Word saves a ``.dotx``. python-docx
    has no API to save a template, so the fixture (test code only) edits
    the generated package's ``[Content_Types].xml``."""
    import io
    import zipfile

    source = zipfile.ZipFile(io.BytesIO(docx_payload))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "[Content_Types].xml":
                assert b"document.main+xml" in data
                data = data.replace(b"document.main+xml", b"template.main+xml")
            archive.writestr(info, data)
    return out.getvalue()


class TestWordTemplates:
    """#937: a Word template (``.dotx``) is read by the DOCX extractor. Its
    main part is loaded as a python-docx ``DocumentPart`` through
    ``PartFactory.part_type_for``; the payload bytes are not changed."""

    def test_python_docx_route_for_templates_still_holds(self):
        """Fails loudly if a python-docx upgrade changes the route: the
        stock ``docx.Document`` still refuses a template, and the
        registration ``extractors.docx`` makes at import loads the
        template's main part as a ``DocumentPart``."""
        import io

        import docx
        from docx.opc.part import PartFactory
        from docx.package import Package
        from docx.parts.document import DocumentPart
        from src.extractors import docx as docx_extractor

        payload = _dotx_bytes(_rich_docx_bytes())
        with pytest.raises(ValueError, match="not a Word file"):
            docx.Document(io.BytesIO(payload))
        assert docx_extractor.WML_TEMPLATE_MAIN == _DOTX_MAIN_CT
        assert PartFactory.part_type_for[_DOTX_MAIN_CT] is DocumentPart
        part = Package.open(io.BytesIO(payload)).main_document_part
        assert isinstance(part, DocumentPart)
        assert part.content_type == _DOTX_MAIN_CT

    def test_dotx_is_extracted_by_mime_and_by_extension(self, monkeypatch, caplog):
        from src.extractors import EXTRACTOR_VERSIONS

        caplog.set_level("DEBUG")
        calls = _count_extractor_calls(monkeypatch)
        payload = _dotx_bytes(_rich_docx_bytes())
        for content_type, filename in (
            (_DOTX_MIME, "a.bin"),
            ("application/octet-stream", "SYNTHETIC_FILENAME_MARKER.dotx"),
        ):
            result = extract(content_type=content_type, filename=filename, payload=payload)
            assert result.status == STATUS_SUCCESS
            assert result.extractor == f"docx@{EXTRACTOR_VERSIONS['docx']}"
            assert result.text is not None
            assert "SYNTHETIC_DOTX_FACT renewal due 2031-04-01" in result.text
            assert "SYNTHETIC_DOTX_CELL Grüße, café, 東京" in result.text
        assert calls == ["docx", "docx"]
        for marker in ("SYNTHETIC_DOTX_FACT", "SYNTHETIC_DOTX_CELL", "SYNTHETIC_FILENAME_MARKER"):
            assert marker not in caplog.text

    def test_dotx_yields_the_same_text_as_the_docx_it_came_from(self):
        docx_payload = _rich_docx_bytes()
        as_docx = extract(
            content_type="application/octet-stream", filename="a.docx", payload=docx_payload
        )
        as_dotx = extract(
            content_type=_DOTX_MIME, filename="a.dotx", payload=_dotx_bytes(docx_payload)
        )
        assert as_docx.status == as_dotx.status == STATUS_SUCCESS
        assert as_docx.text == as_dotx.text

    def test_dotx_goes_through_the_zip_guard(self, monkeypatch):
        calls = _count_extractor_calls(monkeypatch)
        monkeypatch.setattr("src.extractors.ZIP_MAX_UNCOMPRESSED_BYTES", 4)
        result = extract(
            content_type=_DOTX_MIME, filename="a.dotx", payload=_dotx_bytes(_rich_docx_bytes())
        )
        assert result.status == STATUS_FAILED
        assert result.error is not None and "uncompressed" in result.error
        assert calls == []

    def test_ole2_payload_labelled_dotx_keeps_the_ole2_guard(self, monkeypatch):
        """#694's guard runs before the extractor for a ``.dotx`` label too."""
        from src.extractors import LEGACY_OLE2_ERROR

        calls = _count_extractor_calls(monkeypatch)
        for content_type, filename in (
            (_DOTX_MIME, "a.bin"),
            ("application/octet-stream", "a.dotx"),
        ):
            result = extract(
                content_type=content_type, filename=filename, payload=_OLE2_MAGIC + bytes(64)
            )
            assert (result.status, result.error) == (STATUS_UNSUPPORTED, LEGACY_OLE2_ERROR)
        assert calls == []

    def test_non_word_package_still_fails(self, caplog):
        """Only the document and template main parts are read: another OOXML
        package (a workbook) labelled ``.dotx`` fails with a fixed error."""
        import io

        import openpyxl

        caplog.set_level("DEBUG")
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        assert sheet is not None
        sheet["A1"] = "SYNTHETIC_SHEET_MARKER"
        buf = io.BytesIO()
        workbook.save(buf)
        result = extract(content_type=_DOTX_MIME, filename="a.dotx", payload=buf.getvalue())
        assert (result.status, result.error) == (STATUS_FAILED, "ValueError")
        assert "SYNTHETIC_SHEET_MARKER" not in caplog.text

    def test_docx_rows_from_before_dotx_support_are_stale(self):
        """A template labelled ``.docx`` failed before; it now extracts, so
        the docx rows the previous version wrote re-extract."""
        from src.extractors import EXTRACTOR_VERSIONS, stale_extractor_module

        assert EXTRACTOR_VERSIONS["docx"] >= 5
        assert stale_extractor_module("docx@4") == "docx"


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_CHAIN_MARKER = "SYNTHETIC_CHAIN_MARKER"


def _chained_docx(links: int) -> bytes:
    """A synthetic ``.docx`` whose main part relates to ``/c/p0.xml``, each
    ``/c/pN.xml`` relating to the next, ``links`` relationships long."""
    import io
    import zipfile

    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rIdChain" Type="http://example.invalid/chain" Target="/c/p{0}.xml"/>'
        "</Relationships>"
    )
    source = zipfile.ZipFile(io.BytesIO(_docx_bytes(_CHAIN_MARKER)))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "word/_rels/document.xml.rels":
                data = data.replace(
                    b"</Relationships>",
                    b'<Relationship Id="rIdChain" Type="http://example.invalid/chain"'
                    b' Target="/c/p0.xml"/></Relationships>',
                )
            archive.writestr(info, data)
        for i in range(links + 1):
            archive.writestr(f"c/p{i}.xml", b"<x/>")
        for i in range(links):
            archive.writestr(f"c/_rels/p{i}.xml.rels", rels.format(i + 1))
    return out.getvalue()


class TestDocxRelationshipChain:
    """#945: python-docx walks a package's part relationships recursively
    while it opens it, so a long chain of related parts raised
    ``RecursionError``, which the dispatcher re-raises as host pressure.
    It is a property of the file: a ``failed`` row with a fixed type."""

    def test_long_relationship_chain_fails_by_type_not_recursion(self, monkeypatch, caplog):
        import time

        from docx.opc.pkgreader import PackageReader
        from src.extractors import EXTRACTOR_VERSIONS
        from src.extractors import docx as docx_extractor

        links = 2000
        payload = _chained_docx(links)
        assert len(payload) < 1_000_000

        # Work done: each part the loader visits reads its relationships
        # once, and the document walk never starts.
        rels_read: list[int] = []
        srels_for = PackageReader._srels_for

        def counting_srels_for(phys_reader, source_uri):
            rels_read.append(1)
            return srels_for(phys_reader, source_uri)

        monkeypatch.setattr(PackageReader, "_srels_for", staticmethod(counting_srels_for))
        walked: list[int] = []
        block_lines = docx_extractor._block_lines

        def counting_block_lines(blocks):
            walked.append(1)
            return block_lines(blocks)

        monkeypatch.setattr(docx_extractor, "_block_lines", counting_block_lines)

        caplog.set_level("DEBUG")
        started = time.perf_counter()
        result = extract(content_type=_DOCX_MIME, filename="a.docx", payload=payload)
        assert time.perf_counter() - started < 5.0
        assert (result.status, result.error) == (STATUS_FAILED, "DocxRelationshipChainError")
        assert result.extractor == f"docx@{EXTRACTOR_VERSIONS['docx']}"
        assert result.text is None
        assert 0 < len(rels_read) < links
        assert walked == []
        assert _CHAIN_MARKER not in caplog.text

    def test_short_relationship_chain_still_extracts(self):
        """The same shape under the recursion limit opens and is read."""
        result = extract(content_type=_DOCX_MIME, filename="a.docx", payload=_chained_docx(20))
        assert result.status == STATUS_SUCCESS
        assert result.text == _CHAIN_MARKER

    def test_recursion_error_after_the_open_still_escapes(self, monkeypatch):
        """Only the package open is guarded: a ``RecursionError`` raised
        while the opened document is walked stays host pressure."""
        from src.extractors import docx as docx_extractor

        def boom(*_args):
            raise RecursionError

        monkeypatch.setattr(docx_extractor, "_block_lines", boom)
        with pytest.raises(RecursionError):
            extract(content_type=_DOCX_MIME, filename="a.docx", payload=_docx_bytes(_CHAIN_MARKER))


_DOCX_BUDGET_MARKER = "SYNTHETIC_DOCX_BUDGET_MARKER"


def _media_docx_bytes(images: int) -> bytes:
    """A synthetic ``.docx`` with real-shaped package members: a body
    fact, a table, a header, an external hyperlink and ``images`` distinct
    PNG pictures (stored compressed, so they barely expand)."""
    import io

    import docx
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.shared import Inches

    document = docx.Document()
    document.add_paragraph("SYNTHETIC_MEDIA_FACT renewal due 2031-04-01")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "SYNTHETIC_MEDIA_CELL"
    table.cell(0, 1).text = "Grüße, café, 東京"
    document.sections[0].header.paragraphs[0].text = "SYNTHETIC_MEDIA_HEADER"
    document.part.relate_to("https://example.invalid/ref", RT.HYPERLINK, is_external=True)
    for i in range(images):
        document.add_picture(io.BytesIO(_png((8 + i, 8), "red")), width=Inches(1))
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


_MEDIA_DOCX_TEXT = (
    "SYNTHETIC_MEDIA_FACT renewal due 2031-04-01\n\n"
    "SYNTHETIC_MEDIA_CELL Grüße, café, 東京\n\n"
    "SYNTHETIC_MEDIA_HEADER"
)


class TestDocxPackageBudget:
    """#967, #946: python-docx builds a part for every related member,
    checking each relationship against a list of the parts it has visited,
    and parses every XML part whole, before any extractor work runs. The
    package is checked from the ZIP central directory first."""

    @pytest.mark.parametrize(
        ("content_type", "filename", "to_payload"),
        [
            (_DOCX_MIME, "a.docx", lambda payload: payload),
            (_DOTX_MIME, "a.dotx", _dotx_bytes),
        ],
        ids=["docx", "dotx"],
    )
    def test_ordinary_documents_with_media_extract_as_before(
        self, content_type, filename, to_payload, monkeypatch
    ):
        """Pinned before the package budgets: a document and a template
        with pictures, a table, a header and a hyperlink open once and
        read the same text."""
        from src.extractors import docx as docx_extractor

        payload = to_payload(_media_docx_bytes(40))
        opened = _count_calls(monkeypatch, docx_extractor.Package, "open")
        result = extract(content_type=content_type, filename=filename, payload=payload)
        assert (result.status, result.text) == (STATUS_SUCCESS, _MEDIA_DOCX_TEXT)
        assert opened[0] == 1

    @staticmethod
    def _over(budget: str) -> list[tuple[str, bytes, int]]:
        """Members that put a small synthetic document just over the
        default ``budget``, as (name, data, compression)."""
        import zipfile

        from src.extractors import docx as docx_extractor

        if budget == "_MAX_MEMBERS":
            # Tiny stored members add nothing to the expansion sum.
            count = docx_extractor._MAX_MEMBERS
            return [(f"c/p{i}.xml", b"", zipfile.ZIP_STORED) for i in range(count)]
        if budget == "_MAX_RELS_BYTES":
            # One wide relationship part; its expansion stays far under
            # the expansion budget.
            size = docx_extractor._MAX_RELS_BYTES + 1
            return [("c/_rels/p.xml.rels", b" " * size, zipfile.ZIP_DEFLATED)]
        # Highly compressible padding: a small payload, a large parse.
        size = docx_extractor._MAX_EXPANSION_BYTES + 1
        return [("c/pad.xml", b" " * size, zipfile.ZIP_DEFLATED)]

    @pytest.mark.parametrize("budget", ["_MAX_MEMBERS", "_MAX_RELS_BYTES", "_MAX_EXPANSION_BYTES"])
    @pytest.mark.parametrize(
        ("content_type", "filename", "to_payload"),
        [
            (_DOCX_MIME, f"{_DOCX_BUDGET_MARKER}.docx", lambda payload: payload),
            (_DOTX_MIME, f"{_DOCX_BUDGET_MARKER}.dotx", _dotx_bytes),
        ],
        ids=["docx", "dotx"],
    )
    def test_package_over_a_default_budget_fails_before_python_docx_opens(
        self, budget, content_type, filename, to_payload, monkeypatch, caplog
    ):
        """Each budget at its default: the package is recorded
        ``unsupported`` with fixed text (#1032), python-docx never opens it
        (no member is read, no part is parsed), no cap WARNING is logged,
        and the marker stays out of the log."""
        import time
        import zipfile

        from src import extractors
        from src.extractors import EXTRACTOR_VERSIONS
        from src.extractors import docx as docx_extractor

        base = to_payload(_docx_bytes(_DOCX_BUDGET_MARKER))
        payload = _with_members(base, self._over(budget))
        assert len(payload) < 2_000_000
        opened = _count_calls(monkeypatch, docx_extractor.Package, "open")
        reads = _count_calls(monkeypatch, zipfile.ZipFile, "read")
        extractors.drain_extractor_counts()

        caplog.set_level("DEBUG")
        started = time.perf_counter()
        result = extract(content_type=content_type, filename=filename, payload=payload)
        assert time.perf_counter() - started < 5.0
        assert (result.status, result.error) == (STATUS_UNSUPPORTED, DOCX_PACKAGE_BUDGET_ERROR)
        assert result.extractor == f"docx@{EXTRACTOR_VERSIONS['docx']}"
        assert result.text is None
        assert opened[0] == 0
        assert reads[0] == 0
        assert "extractor docx declined" in caplog.text
        assert "extractor cap" not in caplog.text
        assert extractors.drain_extractor_counts()["extractor_caps"] == 0
        assert _DOCX_BUDGET_MARKER not in caplog.text
        assert _DOCX_BUDGET_MARKER not in str(docx_extractor.DocxPackageBudgetError())

    @pytest.mark.parametrize(
        ("content_type", "filename", "to_payload"),
        [
            (_DOCX_MIME, f"{_DOCX_BUDGET_MARKER}.docx", lambda payload: payload),
            (_DOTX_MIME, f"{_DOCX_BUDGET_MARKER}.dotx", _dotx_bytes),
        ],
        ids=["docx", "dotx"],
    )
    def test_stored_element_dense_xml_over_the_declared_budget_fails_before_opening(
        self, content_type, filename, to_payload, monkeypatch, caplog
    ):
        """#1033: XML stored uncompressed does not expand, so it passed the
        expansion budget. The worst case at the defaults: the main part
        stored as element-dense XML almost filling the payload cap, plus
        deflated element-dense XML just under the expansion budget. Every
        other budget passes, and the declared total fails the document
        before python-docx opens it or reads a member."""
        import io
        import time
        import zipfile

        from src import extractors
        from src.extractors import DEFAULT_MAX_BYTES
        from src.extractors import docx as docx_extractor

        payload = _dense_stored_package(
            to_payload(_docx_bytes(_DOCX_BUDGET_MARKER)),
            "word/document.xml",
            b"</w:body>",
            b"<w:p/>",
            pad_name="word/pad.xml",
            expansion_budget=docx_extractor._MAX_EXPANSION_BYTES,
        )
        infos = zipfile.ZipFile(io.BytesIO(payload)).infolist()
        assert len(payload) <= DEFAULT_MAX_BYTES
        assert len(infos) <= docx_extractor._MAX_MEMBERS
        assert sum(max(i.file_size - i.compress_size, 0) for i in infos) <= (
            docx_extractor._MAX_EXPANSION_BYTES
        )
        assert sum(i.file_size for i in infos) > docx_extractor._MAX_DECLARED_BYTES
        opened = _count_calls(monkeypatch, docx_extractor.Package, "open")
        reads = _count_calls(monkeypatch, zipfile.ZipFile, "read")
        extractors.drain_extractor_counts()

        caplog.set_level("DEBUG")
        started = time.perf_counter()
        result = extract(content_type=content_type, filename=filename, payload=payload)
        assert time.perf_counter() - started < 5.0
        assert (result.status, result.error) == (STATUS_UNSUPPORTED, DOCX_PACKAGE_BUDGET_ERROR)
        assert result.text is None
        assert opened[0] == 0
        assert reads[0] == 0
        warnings = [r for r in caplog.records if "extractor docx declined" in r.getMessage()]
        assert [r.levelname for r in warnings] == ["WARNING"]
        assert extractors.drain_extractor_counts()["extractor_caps"] == 0
        assert _DOCX_BUDGET_MARKER not in caplog.text

    def test_document_with_large_stored_media_still_extracts(self, monkeypatch):
        """#1033: media count against the declared budget too, so a
        real-shaped document near the default payload cap still opens:
        its pictures, together about 30 MiB, stored uncompressed."""
        import io
        import zipfile

        from src.extractors import DEFAULT_MAX_BYTES
        from src.extractors import docx as docx_extractor

        payload = _with_large_media(_media_docx_bytes(3), "word/media/", 30 * 1024 * 1024)
        infos = zipfile.ZipFile(io.BytesIO(payload)).infolist()
        assert len(payload) <= DEFAULT_MAX_BYTES
        assert sum(i.file_size for i in infos) > 30 * 1024 * 1024
        opened = _count_calls(monkeypatch, docx_extractor.Package, "open")
        result = extract(content_type=_DOCX_MIME, filename="a.docx", payload=payload)
        assert (result.status, result.text) == (STATUS_SUCCESS, _MEDIA_DOCX_TEXT)
        assert opened[0] == 1

    def test_package_budgets_spent_exactly_open_the_document(self, monkeypatch):
        import io
        import zipfile

        from src.extractors import docx as docx_extractor

        payload = _media_docx_bytes(3)
        infos = zipfile.ZipFile(io.BytesIO(payload)).infolist()
        monkeypatch.setattr(docx_extractor, "_MAX_MEMBERS", len(infos))
        monkeypatch.setattr(
            docx_extractor,
            "_MAX_RELS_BYTES",
            sum(i.file_size for i in infos if i.filename.endswith(".rels")),
        )
        monkeypatch.setattr(
            docx_extractor,
            "_MAX_EXPANSION_BYTES",
            sum(max(i.file_size - i.compress_size, 0) for i in infos),
        )
        monkeypatch.setattr(docx_extractor, "_MAX_DECLARED_BYTES", sum(i.file_size for i in infos))
        assert docx_extractor.extract(payload) == (_MEDIA_DOCX_TEXT, "docx")

    @pytest.mark.parametrize(
        "budget",
        ["_MAX_MEMBERS", "_MAX_RELS_BYTES", "_MAX_EXPANSION_BYTES", "_MAX_DECLARED_BYTES"],
    )
    def test_one_unit_over_a_budget_fails(self, budget, monkeypatch):
        import io
        import zipfile

        from src.extractors import docx as docx_extractor

        payload = _media_docx_bytes(3)
        infos = zipfile.ZipFile(io.BytesIO(payload)).infolist()
        spent = {
            "_MAX_MEMBERS": len(infos),
            "_MAX_RELS_BYTES": sum(i.file_size for i in infos if i.filename.endswith(".rels")),
            "_MAX_EXPANSION_BYTES": sum(max(i.file_size - i.compress_size, 0) for i in infos),
            "_MAX_DECLARED_BYTES": sum(i.file_size for i in infos),
        }
        monkeypatch.setattr(docx_extractor, budget, spent[budget] - 1)
        with pytest.raises(docx_extractor.DocxPackageBudgetError):
            docx_extractor.extract(payload)

    @pytest.mark.parametrize("layout", ["stored", "deflated", "mixed"])
    def test_declared_budget_decides_the_same_whatever_the_compression(self, layout):
        """#1033: the same members get the same declared-total decision
        whether they are stored, deflated or a mix: at the budget exactly
        they pass, one byte under it they fail."""
        import io
        import zipfile

        from src.extractors import over_package_budget

        members = [(f"c/p{i}.xml", b"<w:p/>" * (1000 + i)) for i in range(6)]
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            for i, (name, data) in enumerate(members):
                stored = layout == "stored" or (layout == "mixed" and i % 2 == 0)
                compression = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
                archive.writestr(name, data, compress_type=compression)
        payload = out.getvalue()
        total = sum(len(data) for _, data in members)

        def over(max_declared: int) -> bool:
            return over_package_budget(
                payload,
                max_members=100,
                max_expansion_bytes=10 * total,
                max_rels_bytes=0,
                max_declared_bytes=max_declared,
            )

        assert (over(total), over(total - 1)) == (False, True)

    def test_payload_that_is_not_a_zip_is_left_to_python_docx(self, monkeypatch):
        from src.extractors import docx as docx_extractor

        opened = _count_calls(monkeypatch, docx_extractor.Package, "open")
        with pytest.raises(Exception) as excinfo:
            docx_extractor.extract(b"not a zip " + _DOCX_BUDGET_MARKER.encode())
        assert not isinstance(excinfo.value, docx_extractor.DocxPackageBudgetError)
        assert opened[0] == 1

    def test_related_parts_under_the_member_budget_open_quickly(self):
        """The member budget bounds python-docx's quadratic part walk
        (#967): every member just under it is a related part, and the
        document still opens and reads in a generous time."""
        import time

        from src.extractors import docx as docx_extractor

        parts = docx_extractor._MAX_MEMBERS - 20
        payload = _related_parts_docx(parts)
        started = time.perf_counter()
        assert docx_extractor.extract(payload) == (_DOCX_BUDGET_MARKER, "docx")
        assert time.perf_counter() - started < 5.0

    def test_cached_rows_are_stale_once_the_walk_is_budgeted(self):
        """#1036 shipped the package budgets with no ``docx`` bump, and the
        #1032 permanent-failure mapping kept it, because a bump would have
        re-run every cached document through the unbudgeted walk after
        the open. #1031 budgets that walk and bumps ``docx`` to 7 (6 is
        taken by the reverted #1068), so the ``docx@5`` rows, the
        ``failed`` package-budget rows among them, re-extract once."""
        from src.extractors import EXTRACTOR_VERSIONS, stale_extractor_module

        assert EXTRACTOR_VERSIONS["docx"] == 7
        assert stale_extractor_module("docx@5") == "docx"
        assert stale_extractor_module("docx@6") == "docx"
        assert stale_extractor_module("docx@7") is None

    def test_long_chain_still_fails_as_a_chain_under_the_budgets(self):
        """#968's behaviour holds: a chain under the package budgets still
        fails as a relationship chain, not as a package budget."""
        from src.extractors import docx as docx_extractor

        with pytest.raises(docx_extractor.DocxRelationshipChainError):
            docx_extractor.extract(_chained_docx(2000))


def _with_members(payload: bytes, members: list[tuple[str, bytes, int]]) -> bytes:
    """``payload`` with ``members`` appended, as (name, data, compression)."""
    import io
    import zipfile

    source = zipfile.ZipFile(io.BytesIO(payload))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for info in source.infolist():
            archive.writestr(info, source.read(info.filename))
        for name, data, compression in members:
            archive.writestr(name, data, compress_type=compression)
    return out.getvalue()


def _dense_stored_package(
    payload: bytes,
    main_name: str,
    close_tag: bytes,
    unit: bytes,
    *,
    pad_name: str,
    expansion_budget: int,
) -> bytes:
    """``payload`` with ``main_name`` padded with ``unit`` elements before
    ``close_tag`` and stored uncompressed, almost filling the default
    payload cap, plus a deflated ``pad_name`` member of the same elements
    expanding just under ``expansion_budget`` (#1033's worst case)."""
    import io
    import zipfile

    from src.extractors import DEFAULT_MAX_BYTES

    margin = 1024 * 1024
    source = zipfile.ZipFile(io.BytesIO(payload))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == main_name:
                head, tail = data.split(close_tag, 1)
                count = (DEFAULT_MAX_BYTES - margin - len(data)) // len(unit)
                data = head + unit * count + close_tag + tail
                archive.writestr(info.filename, data, compress_type=zipfile.ZIP_STORED)
            else:
                archive.writestr(info, data)
        pad = unit * ((expansion_budget - margin) // len(unit))
        archive.writestr(pad_name, pad, compress_type=zipfile.ZIP_DEFLATED)
    return out.getvalue()


def _with_large_media(payload: bytes, prefix: str, total: int) -> bytes:
    """``payload`` with each member under ``prefix`` (its pictures or
    media) grown by incompressible bytes to ``total`` bytes together, all
    stored uncompressed, as already-compressed media are."""
    import io
    import random
    import zipfile

    source = zipfile.ZipFile(io.BytesIO(payload))
    media = [info for info in source.infolist() if info.filename.startswith(prefix)]
    each = total // len(media)
    noise = random.Random(1033).randbytes(each)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for info in source.infolist():
            data = source.read(info.filename)
            if info in media:
                data = data + noise[: each - len(data)]
                archive.writestr(info.filename, data, compress_type=zipfile.ZIP_STORED)
            else:
                archive.writestr(info, data)
    return out.getvalue()


def _related_parts_docx(parts: int) -> bytes:
    """A synthetic ``.docx`` whose main part relates to ``parts`` empty
    members, the shape that makes python-docx's part walk quadratic."""
    import io
    import zipfile

    rels = "".join(
        f'<Relationship Id="rX{i}" Type="http://example.invalid/part" Target="/c/p{i}.bin"/>'
        for i in range(parts)
    ).encode()
    source = zipfile.ZipFile(io.BytesIO(_docx_bytes(_DOCX_BUDGET_MARKER)))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "word/_rels/document.xml.rels":
                data = data.replace(b"</Relationships>", rels + b"</Relationships>")
            elif info.filename == "[Content_Types].xml":
                data = data.replace(
                    b"</Types>",
                    b'<Default Extension="bin" ContentType="application/octet-stream"/></Types>',
                )
            archive.writestr(info, data)
        for i in range(parts):
            archive.writestr(f"c/p{i}.bin", b"", compress_type=zipfile.ZIP_STORED)
    return out.getvalue()


# #936: PowerPoint ``.pptx``. Every deck is built here with python-pptx
# itself; crafted shapes python-pptx cannot write are made by editing the
# saved XML.

_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_PPSX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.slideshow"
_POTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.template"
_PPTX_MARKER = "SYNTHETIC_PPTX_MARKER"


def _deck(*slides) -> bytes:
    """A deck with one blank slide per item; each item is a callable that
    adds shapes to its slide."""
    import io

    from pptx import Presentation

    presentation = Presentation()
    for build in slides:
        build(presentation.slides.add_slide(presentation.slide_layouts[6]))
    buf = io.BytesIO()
    presentation.save(buf)
    return buf.getvalue()


def _box(shapes, text: str):
    from pptx.util import Inches

    box = shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    box.text_frame.text = text
    return box


def _boxes(*texts: str):
    """A slide builder adding one text box per text."""

    def build(slide):
        for text in texts:
            _box(slide.shapes, text)

    return build


def _rewrite_deck(payload: bytes, edits: dict[str, Callable[[bytes], bytes]], extra=()) -> bytes:
    """Re-zip ``payload`` with ``edits[name](xml) -> xml`` applied to
    those members and ``extra`` (name, bytes) members added."""
    import io
    import zipfile

    source = zipfile.ZipFile(io.BytesIO(payload))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename in edits:
                data = edits[info.filename](data)
            archive.writestr(info.filename, data)
        for name, data in extra:
            archive.writestr(name, data)
    return out.getvalue()


def _count_pptx_work(monkeypatch) -> dict[str, int]:
    """Count the slides python-pptx resolves and the non-placeholder
    shapes it builds, independently of the extractor's own budget."""
    from pptx.parts.presentation import PresentationPart
    from pptx.shapes import shapetree

    counts = {"slides": 0, "shapes": 0}
    real_slide = PresentationPart.related_slide
    real_factory = shapetree.BaseShapeFactory

    def related_slide(self, rId):
        counts["slides"] += 1
        return real_slide(self, rId)

    def factory(shape_elm, parent):
        counts["shapes"] += 1
        return real_factory(shape_elm, parent)

    monkeypatch.setattr(PresentationPart, "related_slide", related_slide)
    monkeypatch.setattr(shapetree, "BaseShapeFactory", factory)
    return counts


class TestPptxExtractor:
    """#936: text from slide shapes (text frames, tables, groups) and the
    speaker notes, in slide order; no images and no OCR."""

    @staticmethod
    def _full_slide(slide):
        from pptx.util import Inches

        _box(slide.shapes, "Quarterly review")
        table = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
        for (row, col), text in {
            (0, 0): "Region",
            (0, 1): "Revenue",
            (1, 0): "North",
            (1, 1): "TABLEFACT 42",
        }.items():
            table.cell(row, col).text = text
        group = slide.shapes.add_group_shape()
        _box(group.shapes, "GROUPFACT inside a group")
        slide.notes_slide.notes_text_frame.text = "NOTESFACT said aloud"

    def test_reads_text_tables_groups_and_notes_in_slide_order(self):
        from src.extractors.pptx import extract as pptx_extract

        text, name = pptx_extract(_deck(self._full_slide, _boxes("Second slide")))
        assert name == "pptx"
        assert text == (
            "Quarterly review\n\nRegion Revenue\n\nNorth TABLEFACT 42\n\n"
            "GROUPFACT inside a group\n\nNOTESFACT said aloud\n\nSecond slide"
        )

    @pytest.mark.parametrize(
        ("content_type", "filename"),
        [(_PPTX_MIME, "deck.bin"), ("application/octet-stream", "Deck.PPTX"), ("", "deck.pptx")],
    )
    def test_dispatches_by_mime_and_by_extension(self, content_type, filename, monkeypatch):
        calls = _count_extractor_calls(monkeypatch)
        result = extract(
            content_type=content_type,
            filename=filename,
            payload=_deck(_boxes("ATTACHMENTFACT only in the deck")),
        )
        assert result == ExtractionResult(
            status=STATUS_SUCCESS,
            extractor="pptx@3",
            text="ATTACHMENTFACT only in the deck",
            error=None,
        )
        assert calls == ["pptx"]

    def test_non_ascii_text_and_line_breaks(self):
        from src.extractors.pptx import extract as pptx_extract

        text, _ = pptx_extract(_deck(_boxes("Résumé — 東京 Ελληνικά 🙂", "first\vsecond")))
        assert text == "Résumé — 東京 Ελληνικά 🙂\n\nfirst\nsecond"

    def test_merged_table_cell_is_read_once(self):
        from pptx.util import Inches
        from src.extractors.pptx import extract as pptx_extract

        def build(slide):
            table = slide.shapes.add_table(1, 3, Inches(1), Inches(1), Inches(4), Inches(1)).table
            table.cell(0, 0).text = "merged"
            table.cell(0, 2).text = "last"
            table.cell(0, 0).merge(table.cell(0, 1))
            # A spanned cell is hidden by the merge; text left in it is
            # not shown, so it is not read.
            table.cell(0, 1).text_frame.text = "hidden"

        text, _ = pptx_extract(_deck(build))
        assert text == "merged last"

    def test_picture_is_not_read(self, monkeypatch):
        import io

        from PIL import Image
        from pptx.util import Inches
        from src.extractors import image
        from src.extractors.pptx import extract as pptx_extract

        def no_ocr(*args, **kwargs):
            raise AssertionError("pictures are not OCR'd")

        monkeypatch.setattr(image, "extract", no_ocr)
        png = io.BytesIO()
        Image.new("RGB", (8, 8), color="white").save(png, "PNG")

        def build(slide):
            png.seek(0)
            slide.shapes.add_picture(png, Inches(1), Inches(1))
            _box(slide.shapes, "caption")

        assert pptx_extract(_deck(build)) == ("caption", "pptx")

    def test_shape_without_a_text_body_is_skipped(self):
        from src.extractors.pptx import extract as pptx_extract

        def build(slide):
            bare = _box(slide.shapes, "removed")
            bare._sp.remove(bare._sp.txBody)
            _box(slide.shapes, "kept")

        assert pptx_extract(_deck(build)) == ("kept", "pptx")

    def test_notes_page_shapes_count_against_the_shape_budget(self, monkeypatch):
        """A notes page has a slide image and a body placeholder: with the
        slide's own box that is three shapes, so a budget of two stops on
        the notes page and the next slide is not read."""
        from src.extractors import pptx

        def with_notes(slide):
            _box(slide.shapes, "on the slide")
            slide.notes_slide.notes_text_frame.text = "NOTESFACT"

        payload = _deck(with_notes, _boxes("next slide"))
        monkeypatch.setattr(pptx, "_MAX_SHAPES", 2)
        counts = _count_pptx_work(monkeypatch)
        assert pptx.extract(payload) == ("on the slide", "pptx")
        assert counts == {"slides": 1, "shapes": 1}

    def test_table_rows_are_listed_once(self, monkeypatch):
        """python-pptx's row collection has no ``__iter__``: iterating it
        indexes it, and each index lists every row again, so a table of
        20,000 empty rows took about two minutes. The rows are listed once."""
        import re
        import time

        from pptx.oxml.table import CT_Table
        from pptx.util import Inches
        from src.extractors import pptx

        def build(slide):
            table = slide.shapes.add_table(1, 1, Inches(1), Inches(1), Inches(4), Inches(1))
            table.table.cell(0, 0).text = "header"
            _box(slide.shapes, "after the table")

        def many_rows(xml: bytes) -> bytes:
            row = re.search(rb"<a:tr [^>]*>.*?</a:tr>", xml, re.S)
            assert row is not None
            return xml.replace(row.group(0), row.group(0) + b'<a:tr h="1"/>' * 20_000)

        payload = _rewrite_deck(_deck(build), {"ppt/slides/slide1.xml": many_rows})
        listings = [0]
        real = CT_Table.tr_lst

        def counting(self):
            listings[0] += 1
            return real.fget(self)

        monkeypatch.setattr(CT_Table, "tr_lst", property(counting))
        started = time.perf_counter()
        assert pptx.extract(payload) == ("header\n\nafter the table", "pptx")
        assert time.perf_counter() - started < 5.0
        assert listings[0] == 1

    def test_text_budget_can_end_in_the_notes(self, monkeypatch):
        from src.extractors import pptx

        def with_notes(slide):
            _box(slide.shapes, "abc")
            slide.notes_slide.notes_text_frame.text = "defgh"

        payload = _deck(with_notes, _boxes("next slide"))
        element = pptx._ELEMENT_COST
        monkeypatch.setattr(pptx, "_MAX_TEXT_CHARS", (2 * element + 3) + (2 * element + 2))
        counts = _count_pptx_work(monkeypatch)
        assert pptx.extract(payload) == ("abc\n\nde", "pptx")
        assert counts["slides"] == 1

    def test_row_refused_before_its_first_cell_adds_no_line(self, monkeypatch):
        from pptx.util import Inches
        from src.extractors import pptx

        def build(slide):
            table = slide.shapes.add_table(2, 1, Inches(1), Inches(1), Inches(4), Inches(1))
            table.table.cell(0, 0).text = "first"
            table.table.cell(1, 0).text = "second"
            _box(slide.shapes, "after the table")

        payload = _deck(build)
        monkeypatch.setattr(pptx, "_MAX_TABLE_CELLS", 2 + 1)
        assert pptx.extract(payload) == ("first", "pptx")

    def test_text_budget_can_end_inside_a_table_cell(self, monkeypatch, caplog):
        from pptx.util import Inches
        from src import extractors
        from src.extractors import pptx

        def build(slide):
            table = slide.shapes.add_table(1, 3, Inches(1), Inches(1), Inches(4), Inches(1))
            for col, text in enumerate(("abc", "defgh", "ijk")):
                table.table.cell(0, col).text = text

        payload = _deck(build)
        element = pptx._ELEMENT_COST
        monkeypatch.setattr(pptx, "_MAX_TEXT_CHARS", (2 * element + 3) + (2 * element + 2))
        caplog.set_level("DEBUG")
        extractors.drain_extractor_counts()
        assert pptx.extract(payload) == ("abc de", "pptx")
        assert "extractor cap pptx_text_chars" in caplog.text
        assert extractors.drain_extractor_counts()["extractor_caps"] == 1

    def test_deck_without_text_is_empty(self):
        result = extract(content_type=_PPTX_MIME, filename="a.pptx", payload=_deck(_boxes()))
        assert (result.status, result.extractor, result.text) == (STATUS_EMPTY, "pptx@3", None)

    def test_repeated_slide_entries_read_the_slide_once(self, monkeypatch):
        """A slide list naming one slide many times costs a slide-list
        entry each, not a walk of the slide each."""
        import re

        from src.extractors.pptx import extract as pptx_extract

        def repeat(xml: bytes) -> bytes:
            entry = re.search(rb"<p:sldId [^>]*/>", xml)
            assert entry is not None
            return xml.replace(entry.group(0), entry.group(0) * 1000)

        payload = _rewrite_deck(_deck(_boxes("once")), {"ppt/presentation.xml": repeat})
        counts = _count_pptx_work(monkeypatch)
        assert pptx_extract(payload) == ("once", "pptx")
        assert counts == {"slides": 1000, "shapes": 1}

    def test_deeply_nested_groups_are_walked_without_recursion(self):
        """Groups nest as deep as lxml parses (256 elements); the walk
        uses an explicit stack, so it passes under a recursion limit a
        recursive walk of the same nesting would exceed."""
        import inspect
        import sys

        from src.extractors.pptx import extract as pptx_extract

        depth = 200

        def build(slide):
            shapes = slide.shapes
            for _ in range(depth):
                shapes = shapes.add_group_shape().shapes
            _box(shapes, "DEEPFACT")

        payload = _deck(build)
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(len(inspect.stack()) + 100)
        try:
            text, _ = pptx_extract(payload)
        finally:
            sys.setrecursionlimit(limit)
        assert text == "DEEPFACT"

    def test_group_nesting_past_the_xml_depth_limit_fails_by_type(self, caplog):
        """A crafted group deeper than lxml parses fails as a parse error,
        recorded by type, never as ``RecursionError``."""
        group = (
            '<p:grpSp><p:nvGrpSpPr><p:cNvPr id="5" name="g"/><p:cNvGrpSpPr/><p:nvPr/>'
            "</p:nvGrpSpPr><p:grpSpPr/>"
        )

        def nest(xml: bytes) -> bytes:
            inner = group * 300 + _PPTX_MARKER + "</p:grpSp>" * 300
            return xml.replace(b"</p:spTree>", inner.encode() + b"</p:spTree>")

        payload = _rewrite_deck(_deck(_boxes("x")), {"ppt/slides/slide1.xml": nest})
        caplog.set_level("DEBUG")
        result = extract(content_type=_PPTX_MIME, filename="a.pptx", payload=payload)
        assert (result.status, result.error) == (STATUS_FAILED, "XMLSyntaxError")
        assert _PPTX_MARKER not in caplog.text

    def test_long_relationship_chain_fails_by_type_not_recursion(self, caplog):
        """python-pptx follows relationships recursively while it opens a
        package, so a chain of related parts raised ``RecursionError``,
        which the dispatcher re-raises as host pressure. It is a property
        of the file: a ``failed`` row with a fixed type."""
        import time

        rel = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://example.invalid/chain" Target="/c/p{0}.xml"/>'
            "</Relationships>"
        )
        links = 2000
        extra = [(f"c/p{i}.xml", b"<x/>") for i in range(links + 1)]
        extra += [(f"c/_rels/p{i}.xml.rels", rel.format(i + 1).encode()) for i in range(links)]

        def link(xml: bytes) -> bytes:
            return xml.replace(
                b"</Relationships>",
                b'<Relationship Id="rIdChain" Type="http://example.invalid/chain"'
                b' Target="/c/p0.xml"/></Relationships>',
            )

        payload = _rewrite_deck(
            _deck(_boxes(_PPTX_MARKER)), {"ppt/_rels/presentation.xml.rels": link}, extra
        )
        caplog.set_level("DEBUG")
        started = time.perf_counter()
        result = extract(content_type=_PPTX_MIME, filename="a.pptx", payload=payload)
        assert time.perf_counter() - started < 5.0
        assert (result.status, result.error) == (STATUS_FAILED, "PptxRelationshipChainError")
        assert result.extractor == "pptx@3"
        assert _PPTX_MARKER not in caplog.text

    def test_malformed_slide_fails_by_type_without_its_text(self, caplog):
        """A run without its ``a:t`` raises inside python-pptx; the error
        recorded and logged is the type alone."""

        def break_run(xml: bytes) -> bytes:
            return xml.replace(
                b"</p:spTree>",
                (
                    '<p:sp><p:nvSpPr><p:cNvPr id="9" name="t"/><p:cNvSpPr txBox="1"/><p:nvPr/>'
                    "</p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:p><a:r><a:rPr lang="
                    f'"{_PPTX_MARKER}"/></a:r></a:p></p:txBody></p:sp></p:spTree>'
                ).encode(),
            )

        payload = _rewrite_deck(_deck(_boxes("x")), {"ppt/slides/slide1.xml": break_run})
        caplog.set_level("DEBUG")
        result = extract(content_type=_PPTX_MIME, filename=f"{_PPTX_MARKER}.pptx", payload=payload)
        assert result.status == STATUS_FAILED
        assert result.error == "InvalidXmlError"
        assert "InvalidXmlError" in caplog.text
        assert _PPTX_MARKER not in caplog.text
        assert _PPTX_MARKER not in (result.error or "")

    def test_layout_placeholders_and_notes_placeholder_are_read(self):
        """Review round 1: text typed into a layout's title and content
        placeholders (``SlidePlaceholder``) and the notes placeholder
        (``NotesSlidePlaceholder``), both ``Shape`` subclasses."""
        import io

        from pptx import Presentation
        from pptx.shapes.autoshape import Shape
        from pptx.shapes.placeholder import NotesSlidePlaceholder, SlidePlaceholder
        from src.extractors.pptx import extract as pptx_extract

        assert issubclass(SlidePlaceholder, Shape)
        assert issubclass(NotesSlidePlaceholder, Shape)
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = "TITLEFACT"
        slide.placeholders[1].text_frame.text = "BODYFACT"
        slide.notes_slide.notes_text_frame.text = "NOTESFACT"
        buf = io.BytesIO()
        presentation.save(buf)

        assert pptx_extract(buf.getvalue()) == ("TITLEFACT\n\nBODYFACT\n\nNOTESFACT", "pptx")

    def test_expansion_budget_fails_before_python_pptx_opens(self, monkeypatch, caplog):
        """Review round 1: python-pptx parses every XML part whole when it
        opens, about 15 bytes of memory per byte of XML. A deck whose
        members expand by more than ``_MAX_EXPANSION_BYTES`` past their
        compressed size fails before python-pptx sees it."""
        from src.extractors import pptx

        def pad(xml: bytes) -> bytes:
            return xml.replace(b"</p:spTree>", b"<!--" + b" " * 300_000 + b"--></p:spTree>")

        payload = _rewrite_deck(_deck(_boxes(_PPTX_MARKER)), {"ppt/slides/slide1.xml": pad})
        monkeypatch.setattr(pptx, "_MAX_EXPANSION_BYTES", 250_000)
        opened = _count_calls(monkeypatch, pptx.Package, "open")
        caplog.set_level("DEBUG")
        result = extract(content_type=_PPTX_MIME, filename="a.pptx", payload=payload)
        assert (result.status, result.error) == (STATUS_UNSUPPORTED, PPTX_PACKAGE_BUDGET_ERROR)
        assert opened[0] == 0
        assert _PPTX_MARKER not in caplog.text

    @pytest.mark.parametrize(
        ("budget", "setting", "extra"),
        [
            # Many tiny stored members: they add nothing to the expansion
            # sum, but python-pptx builds a part for each one related.
            ("_MAX_MEMBERS", 50, [(f"c/p{i}.xml", b"") for i in range(60)]),
            # A wide relationship list, even to parts that do not exist:
            # python-pptx walks every relationship while it opens.
            ("_MAX_RELS_BYTES", 4096, [("c/_rels/p.xml.rels", b" " * 5000)]),
        ],
        ids=["members", "rels-bytes"],
    )
    def test_package_count_budgets_fail_before_python_pptx_opens(
        self, budget, setting, extra, monkeypatch, caplog
    ):
        """Review round 2: python-pptx materializes every related part and
        relationship before any walk budget applies. Members and the bytes
        of ``.rels`` members (the only relationship parts python-pptx
        reads, found by name) are counted from the central directory."""
        from src.extractors import pptx

        payload = _rewrite_deck(_deck(_boxes(_PPTX_MARKER)), {}, extra)
        monkeypatch.setattr(pptx, budget, setting)
        opened = _count_calls(monkeypatch, pptx.Package, "open")
        caplog.set_level("DEBUG")
        result = extract(content_type=_PPTX_MIME, filename="a.pptx", payload=payload)
        assert (result.status, result.error) == (STATUS_UNSUPPORTED, PPTX_PACKAGE_BUDGET_ERROR)
        assert opened[0] == 0
        assert _PPTX_MARKER not in caplog.text

    def test_package_budgets_spent_exactly_open_the_deck(self, monkeypatch):
        import io
        import zipfile

        from src.extractors import pptx

        payload = _deck(_boxes("exact"))
        infos = zipfile.ZipFile(io.BytesIO(payload)).infolist()
        monkeypatch.setattr(pptx, "_MAX_MEMBERS", len(infos))
        monkeypatch.setattr(
            pptx, "_MAX_RELS_BYTES", sum(i.file_size for i in infos if i.filename.endswith(".rels"))
        )
        monkeypatch.setattr(
            pptx,
            "_MAX_EXPANSION_BYTES",
            sum(max(i.file_size - i.compress_size, 0) for i in infos),
        )
        monkeypatch.setattr(pptx, "_MAX_DECLARED_BYTES", sum(i.file_size for i in infos))
        assert pptx.extract(payload) == ("exact", "pptx")

    def test_one_byte_over_the_declared_budget_fails(self, monkeypatch):
        import io
        import zipfile

        from src.extractors import pptx

        payload = _deck(_boxes("exact"))
        infos = zipfile.ZipFile(io.BytesIO(payload)).infolist()
        monkeypatch.setattr(pptx, "_MAX_DECLARED_BYTES", sum(i.file_size for i in infos) - 1)
        with pytest.raises(pptx.PptxPackageBudgetError):
            pptx.extract(payload)

    def test_stored_element_dense_xml_over_the_declared_budget_fails_before_opening(
        self, monkeypatch, caplog
    ):
        """#1033: XML stored uncompressed does not expand, so it passed the
        expansion budget. The worst case at the defaults: a slide stored
        as element-dense XML almost filling the payload cap, plus deflated
        element-dense XML just under the expansion budget. Every other
        budget passes, and the declared total fails the deck before
        python-pptx opens it or reads a member."""
        import io
        import time
        import zipfile

        from src import extractors
        from src.extractors import DEFAULT_MAX_BYTES, pptx

        payload = _dense_stored_package(
            _deck(_boxes(_PPTX_MARKER)),
            "ppt/slides/slide1.xml",
            b"</p:spTree>",
            b"<a:p/>",
            pad_name="ppt/pad.xml",
            expansion_budget=pptx._MAX_EXPANSION_BYTES,
        )
        infos = zipfile.ZipFile(io.BytesIO(payload)).infolist()
        assert len(payload) <= DEFAULT_MAX_BYTES
        assert len(infos) <= pptx._MAX_MEMBERS
        assert sum(max(i.file_size - i.compress_size, 0) for i in infos) <= (
            pptx._MAX_EXPANSION_BYTES
        )
        assert sum(i.file_size for i in infos) > pptx._MAX_DECLARED_BYTES
        opened = _count_calls(monkeypatch, pptx.Package, "open")
        reads = _count_calls(monkeypatch, zipfile.ZipFile, "read")
        extractors.drain_extractor_counts()

        caplog.set_level("DEBUG")
        started = time.perf_counter()
        result = extract(content_type=_PPTX_MIME, filename="a.pptx", payload=payload)
        assert time.perf_counter() - started < 5.0
        assert (result.status, result.error) == (STATUS_UNSUPPORTED, PPTX_PACKAGE_BUDGET_ERROR)
        assert result.text is None
        assert opened[0] == 0
        assert reads[0] == 0
        warnings = [r for r in caplog.records if "extractor pptx declined" in r.getMessage()]
        assert [r.levelname for r in warnings] == ["WARNING"]
        assert extractors.drain_extractor_counts()["extractor_caps"] == 0
        assert _PPTX_MARKER not in caplog.text

    def test_deck_with_large_stored_media_still_extracts(self, monkeypatch):
        """#1033: media count against the declared budget too, so a
        real-shaped deck near the default payload cap still opens: its
        pictures, together about 30 MiB, stored uncompressed."""
        import io
        import zipfile

        from src.extractors import DEFAULT_MAX_BYTES, pptx

        def pictures(slide):
            from pptx.util import Inches

            _box(slide.shapes, "SYNTHETIC_DECK_MEDIA_FACT")
            for i in range(3):
                slide.shapes.add_picture(io.BytesIO(_png((8 + i, 8), "red")), Inches(1), Inches(2))

        payload = _with_large_media(_deck(pictures), "ppt/media/", 30 * 1024 * 1024)
        infos = zipfile.ZipFile(io.BytesIO(payload)).infolist()
        assert len(payload) <= DEFAULT_MAX_BYTES
        assert sum(i.file_size for i in infos) > 30 * 1024 * 1024
        opened = _count_calls(monkeypatch, pptx.Package, "open")
        result = extract(content_type=_PPTX_MIME, filename="a.pptx", payload=payload)
        assert (result.status, result.text) == (STATUS_SUCCESS, "SYNTHETIC_DECK_MEDIA_FACT")
        assert opened[0] == 1

    def test_payload_that_is_not_a_zip_is_left_to_python_pptx(self, monkeypatch):
        from src.extractors import pptx

        opened = _count_calls(monkeypatch, pptx.Package, "open")
        result = extract(
            content_type=_PPTX_MIME,
            filename="a.pptx",
            payload=b"not a zip " + _PPTX_MARKER.encode(),
        )
        assert result.status == STATUS_FAILED
        assert result.error != PPTX_PACKAGE_BUDGET_ERROR
        assert opened[0] == 1

    def test_ordinary_deck_is_under_the_expansion_budget(self, monkeypatch):
        """Stored media (already compressed) costs nothing; the default
        budget is far above an ordinary deck's XML."""
        from src.extractors import pptx

        payload = _deck(TestPptxExtractor._full_slide, _boxes("b"))
        opened = _count_calls(monkeypatch, pptx.Package, "open")
        assert pptx.extract(payload)[0].startswith("Quarterly review")
        assert opened[0] == 1

    def test_zip_guard_runs_before_python_pptx(self, monkeypatch, caplog):
        calls = _count_extractor_calls(monkeypatch)
        monkeypatch.setattr("src.extractors.ZIP_MAX_UNCOMPRESSED_BYTES", 64)
        caplog.set_level("DEBUG")
        result = extract(
            content_type=_PPTX_MIME, filename="a.pptx", payload=_deck(_boxes(_PPTX_MARKER))
        )
        assert result.status == STATUS_FAILED
        assert "uncompressed" in (result.error or "")
        assert calls == []
        assert _PPTX_MARKER not in caplog.text

    def test_ole2_payload_labelled_pptx_is_unsupported(self, monkeypatch):
        """An encrypted ``.pptx`` is an OLE2 compound file, which
        python-pptx cannot open: recorded ``unsupported`` like an
        encrypted ``.docx`` (#694), without running the extractor."""
        from src.extractors import LEGACY_OLE2_ERROR

        calls = _count_extractor_calls(monkeypatch)
        for content_type, filename in (
            (_PPTX_MIME, "a.bin"),
            ("application/octet-stream", "a.pptx"),
        ):
            result = extract(
                content_type=content_type, filename=filename, payload=_OLE2_MAGIC + bytes(64)
            )
            assert (result.status, result.error) == (STATUS_UNSUPPORTED, LEGACY_OLE2_ERROR)
        assert calls == []


_PPTM_MIME = "application/vnd.ms-powerpoint.presentation.macroEnabled.12"
_PPSM_MIME = "application/vnd.ms-powerpoint.slideshow.macroEnabled.12"
_POTM_MIME = "application/vnd.ms-powerpoint.template.macroEnabled.12"
_PRESENTATION_MAIN = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
)
_SLIDESHOW_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml"
_TEMPLATE_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml"
_MACRO_MAIN = "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"
_SLIDESHOW_MACRO_MAIN = "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml"
_TEMPLATE_MACRO_MAIN = "application/vnd.ms-powerpoint.template.macroEnabled.main+xml"
_VBA_MARKER = "SYNTHETIC_MACRO_MARKER"

# (MIME type, extension, main-part content type) of each PresentationML
# variant read since #947 (``.ppsm`` / ``.potm`` since #1042).
_PPTX_VARIANTS = {
    "ppsx": (_PPSX_MIME, ".ppsx", _SLIDESHOW_MAIN),
    "potx": (_POTX_MIME, ".potx", _TEMPLATE_MAIN),
    "pptm": (_PPTM_MIME, ".pptm", _MACRO_MAIN),
    "ppsm": (_PPSM_MIME, ".ppsm", _SLIDESHOW_MACRO_MAIN),
    "potm": (_POTM_MIME, ".potm", _TEMPLATE_MACRO_MAIN),
}
# The variants whose main part is macro-enabled: saved with a
# ``vbaProject.bin`` part.
_MACRO_VARIANTS = {"pptm", "ppsm", "potm"}

# Each variant labelled by its MIME type alone and by its extension alone.
_PPTX_VARIANT_LABELS = [
    pytest.param(mime, "a.bin", main, id=f"{name}-mime")
    for name, (mime, _, main) in _PPTX_VARIANTS.items()
] + [
    pytest.param("application/octet-stream", f"a{ext}", main, id=f"{name}-ext")
    for name, (_, ext, main) in _PPTX_VARIANTS.items()
]


def _retyped_deck(main_type: str, payload: bytes | None = None) -> bytes:
    """A deck whose main part declares another PresentationML type, as
    PowerPoint saves a slideshow (``.ppsx``), a template (``.potx``) or a
    macro-enabled deck (``.pptm``). python-pptx saves only the
    presentation type, so the fixture (test code only) edits the saved
    package's ``[Content_Types].xml``."""

    def retype(xml: bytes) -> bytes:
        assert _PRESENTATION_MAIN.encode() in xml
        return xml.replace(_PRESENTATION_MAIN.encode(), main_type.encode())

    if payload is None:
        payload = _deck(_boxes("SLIDESHOWFACT"))
    return _rewrite_deck(payload, {"[Content_Types].xml": retype})


def _macro_deck(text: str, main_type: str = _MACRO_MAIN) -> bytes:
    """A ``.pptm`` (or, by ``main_type``, a ``.ppsm`` / ``.potm``): a deck
    with a ``vbaProject.bin`` part related from the presentation, as
    PowerPoint saves macros, holding a synthetic marker instead of a VBA
    project."""
    import io

    from pptx import Presentation
    from pptx.opc.package import Part
    from pptx.opc.packuri import PackURI

    presentation = Presentation()
    _boxes(text)(presentation.slides.add_slide(presentation.slide_layouts[6]))
    part = presentation.part
    vba = Part(
        PackURI("/ppt/vbaProject.bin"),
        "application/vnd.ms-office.vbaProject",
        part.package,
        _VBA_MARKER.encode(),
    )
    part.relate_to(vba, "http://schemas.microsoft.com/office/2006/relationships/vbaProject")
    buf = io.BytesIO()
    presentation.save(buf)
    return _retyped_deck(main_type, buf.getvalue())


class TestPowerPointVariants:
    """#947: slideshows (``.ppsx``), templates (``.potx``) and macro-enabled
    decks (``.pptm``) are read by the PPTX extractor. python-pptx's own
    part factory loads each one's main part as a ``PresentationPart``;
    the extractor opens the package with ``Package.open`` and accepts
    those main-part types. #1042: macro-enabled slideshows (``.ppsm``)
    and templates (``.potm``) too; python-pptx has no mapping for their
    main parts, so the extractor registers them at import. The payload
    bytes are not changed, and a macro part is never read."""

    def test_python_pptx_route_for_variants_still_holds(self):
        """Fails loudly if a python-pptx upgrade changes the route: the
        stock ``pptx.Presentation`` still refuses a slideshow and a
        template, plain or macro-enabled; python-pptx's own map
        (``pptx/__init__.py``) still covers the three #947 types and still
        lacks the two macro-enabled ones (so the registration
        ``extractors.pptx`` makes at import is still needed, and the test
        says so when an upgrade makes it redundant); and ``PartFactory``
        maps all five to ``PresentationPart``, which ``Package.open`` then
        loads."""
        import io

        import pptx as python_pptx
        from pptx import Presentation
        from pptx.opc.constants import CONTENT_TYPE as CT
        from pptx.opc.package import PartFactory
        from pptx.package import Package
        from pptx.parts.presentation import PresentationPart
        from src.extractors import pptx

        assert {_SLIDESHOW_MAIN, _TEMPLATE_MAIN, _MACRO_MAIN} == {
            CT.PML_SLIDESHOW_MAIN,
            CT.PML_TEMPLATE_MAIN,
            CT.PML_PRES_MACRO_MAIN,
        }
        assert not hasattr(CT, "PML_SLIDESHOW_MACRO_MAIN")
        assert not hasattr(CT, "PML_TEMPLATE_MACRO_MAIN")
        assert pptx.PML_SLIDESHOW_MACRO_MAIN == _SLIDESHOW_MACRO_MAIN
        assert pptx.PML_TEMPLATE_MACRO_MAIN == _TEMPLATE_MACRO_MAIN
        assert pptx._PRESENTATION_MAIN_TYPES == {
            CT.PML_PRESENTATION_MAIN,
            CT.PML_SLIDESHOW_MAIN,
            CT.PML_TEMPLATE_MAIN,
            CT.PML_PRES_MACRO_MAIN,
            _SLIDESHOW_MACRO_MAIN,
            _TEMPLATE_MACRO_MAIN,
        }
        native = python_pptx.content_type_to_part_class_map
        for main_type in (_SLIDESHOW_MAIN, _TEMPLATE_MAIN, _MACRO_MAIN):
            assert native[main_type] is PresentationPart
        for main_type in (_SLIDESHOW_MACRO_MAIN, _TEMPLATE_MACRO_MAIN):
            assert main_type not in native
        for main_type in (
            _SLIDESHOW_MAIN,
            _TEMPLATE_MAIN,
            _SLIDESHOW_MACRO_MAIN,
            _TEMPLATE_MACRO_MAIN,
        ):
            with pytest.raises(ValueError, match="not a PowerPoint file"):
                Presentation(io.BytesIO(_retyped_deck(main_type)))
        for _, _, main_type in _PPTX_VARIANTS.values():
            assert PartFactory.part_type_for[main_type] is PresentationPart
            part = Package.open(io.BytesIO(_retyped_deck(main_type))).main_document_part
            assert isinstance(part, PresentationPart)
            assert part.content_type == main_type

    @pytest.mark.parametrize(("content_type", "filename", "main_type"), _PPTX_VARIANT_LABELS)
    def test_each_variant_is_extracted_by_mime_and_by_extension(
        self, content_type, filename, main_type, monkeypatch, caplog
    ):
        calls = _count_extractor_calls(monkeypatch)
        payload = _retyped_deck(main_type, _deck(_boxes("VARIANTFACT renewal due 2031-04-01")))
        caplog.set_level("DEBUG")
        result = extract(content_type=content_type, filename=filename, payload=payload)
        assert (result.status, result.extractor) == (STATUS_SUCCESS, "pptx@3")
        assert result.text == "VARIANTFACT renewal due 2031-04-01"
        assert calls == ["pptx"]
        assert "VARIANTFACT" not in caplog.text

    @pytest.mark.parametrize(
        ("label", "main_type"),
        [
            pytest.param((mime, "a.bin"), main, id=f"{name}-mime")
            for name, (mime, _, main) in _PPTX_VARIANTS.items()
            if name in _MACRO_VARIANTS
        ]
        + [
            pytest.param(("", f"a{ext}"), main, id=f"{name}-ext")
            for name, (_, ext, main) in _PPTX_VARIANTS.items()
            if name in _MACRO_VARIANTS
        ],
    )
    def test_macro_part_is_never_read(self, label, main_type, monkeypatch, caplog):
        """A ``.pptm``, ``.ppsm`` or ``.potm`` yields its slide text.
        python-pptx loads the macro part as an opaque ``Part`` when it
        opens the package; nothing reads its bytes, so no macro text
        reaches the index or the logs."""
        import io
        import zipfile

        from pptx.opc.package import Part

        payload = _macro_deck("MACRODECKFACT", main_type)
        archive = zipfile.ZipFile(io.BytesIO(payload))
        assert archive.read("ppt/vbaProject.bin") == _VBA_MARKER.encode()
        assert main_type.encode() in archive.read("[Content_Types].xml")

        read: list[str] = []
        real_blob = Part.blob

        def recording_blob(part):
            read.append(str(part.partname))
            return real_blob.fget(part)

        monkeypatch.setattr(Part, "blob", property(recording_blob))
        caplog.set_level("DEBUG")
        content_type, filename = label
        result = extract(content_type=content_type, filename=filename, payload=payload)
        assert (result.status, result.extractor) == (STATUS_SUCCESS, "pptx@3")
        assert result.text == "MACRODECKFACT"
        assert "/ppt/vbaProject.bin" not in read
        assert _VBA_MARKER not in caplog.text

    @pytest.mark.parametrize(("content_type", "filename", "main_type"), _PPTX_VARIANT_LABELS)
    def test_ole2_payload_under_each_label_is_unsupported(
        self, content_type, filename, main_type, monkeypatch
    ):
        from src.extractors import LEGACY_OLE2_ERROR

        calls = _count_extractor_calls(monkeypatch)
        result = extract(
            content_type=content_type, filename=filename, payload=_OLE2_MAGIC + bytes(64)
        )
        assert (result.status, result.error) == (STATUS_UNSUPPORTED, LEGACY_OLE2_ERROR)
        assert calls == []

    @pytest.mark.parametrize(("content_type", "filename", "main_type"), _PPTX_VARIANT_LABELS)
    def test_variants_go_through_the_pre_open_budgets(
        self, content_type, filename, main_type, monkeypatch, caplog
    ):
        from src.extractors import pptx

        payload = _retyped_deck(main_type, _deck(_boxes(_PPTX_MARKER)))
        monkeypatch.setattr(pptx, "_MAX_MEMBERS", 5)
        opened = _count_calls(monkeypatch, pptx.Package, "open")
        caplog.set_level("DEBUG")
        result = extract(content_type=content_type, filename=filename, payload=payload)
        assert (result.status, result.error) == (STATUS_UNSUPPORTED, PPTX_PACKAGE_BUDGET_ERROR)
        assert opened[0] == 0
        assert _PPTX_MARKER not in caplog.text

    def test_variants_go_through_the_walk_budgets(self, monkeypatch, caplog):
        from src.extractors import pptx

        payload = _retyped_deck(_SLIDESHOW_MAIN, _deck(_boxes("one"), _boxes(_PPTX_MARKER)))
        monkeypatch.setattr(pptx, "_MAX_SLIDES", 1)
        caplog.set_level("DEBUG")
        result = extract(content_type=_PPSX_MIME, filename="a.ppsx", payload=payload)
        assert (result.status, result.text) == (STATUS_SUCCESS, "one")
        assert "extractor cap pptx_slides" in caplog.text
        assert _PPTX_MARKER not in caplog.text

    @pytest.mark.parametrize(("content_type", "filename", "main_type"), _PPTX_VARIANT_LABELS)
    def test_package_whose_main_part_is_not_a_presentation_fails_by_type(
        self, content_type, filename, main_type, caplog
    ):
        """A Word document under a PowerPoint label: python-pptx loads its
        main part as a generic part, which the extractor refuses with
        fixed text; nothing of the document is logged or recorded."""
        caplog.set_level("DEBUG")
        result = extract(
            content_type=content_type,
            filename=filename.replace("a.", f"{_PPTX_MARKER}."),
            payload=_docx_bytes(_PPTX_MARKER),
        )
        assert (result.status, result.error) == (STATUS_FAILED, "ValueError")
        assert "ValueError" in caplog.text
        assert _PPTX_MARKER not in caplog.text

    def test_variant_labelled_pptx_is_extracted(self):
        """The outcome that changed for a ``.pptx`` label, and why the
        PPTX version was bumped: a slideshow or template sent as ``.pptx``
        failed under ``pptx@1`` (``pptx.Presentation`` refused it). A
        macro-enabled slideshow or template sent as ``.pptx`` failed under
        ``pptx@2`` (its main part loaded as a generic part, #1042); the
        #1032 bump to ``pptx@3`` refreshes those rows."""
        for main_type in (
            _SLIDESHOW_MAIN,
            _TEMPLATE_MAIN,
            _SLIDESHOW_MACRO_MAIN,
            _TEMPLATE_MACRO_MAIN,
        ):
            result = extract(
                content_type=_PPTX_MIME, filename="a.pptx", payload=_retyped_deck(main_type)
            )
            assert (result.status, result.text) == (STATUS_SUCCESS, "SLIDESHOWFACT")

    def test_mime_dispatch_keys_are_lowercase(self):
        """The label is lowercased before the lookup, so a key with an
        upper-case letter (``macroEnabled``) would never match."""
        from src.extractors import _MIME_DISPATCH

        assert [key for key in _MIME_DISPATCH if key != key.lower()] == []

    def test_pptx_versions_1_and_2_rows_are_stale(self):
        """``pptx@1`` refused slideshows and templates (#947); ``pptx@2``
        recorded an over-budget package ``failed`` (#1032)."""
        from src.extractors import EXTRACTOR_VERSIONS, stale_extractor_module

        assert EXTRACTOR_VERSIONS["pptx"] == 3
        assert stale_extractor_module("pptx@1") == "pptx"
        assert stale_extractor_module("pptx@2") == "pptx"
        assert stale_extractor_module("pptx@3") is None


# #903: every truncation or skip cap inside an extractor is reported the
# same way: a fixed cap name in a rate-limited WARNING, and one
# ``extractor_caps`` count in the attachments aggregate per cap per
# extraction attempt.

_CAP_MARKER = "SYNTHETIC_CAP_MARKER"


def _cap_extracted_chars(monkeypatch):
    payload = (_CAP_MARKER + " ") * 20
    result = extract(
        content_type="text/plain",
        filename=f"{_CAP_MARKER}.txt",
        payload=payload.encode(),
        max_extracted_chars=16,
    )
    assert result.status == STATUS_SUCCESS
    assert result.text == payload[:16]


def _cap_pdf_digital_pages(monkeypatch):
    from src.extractors import pdf

    read: list[int] = []

    class Page:
        def __init__(self, index):
            self.index = index

        def extract_text(self):
            read.append(self.index)
            return f"{_CAP_MARKER} digital text of page {self.index} " * 2

    class FakeReader:
        def __init__(self, stream):
            self.pages = [Page(i) for i in range(5)]

    monkeypatch.setattr(pdf.pypdf, "PdfReader", FakeReader)
    text, name = pdf.extract(b"%PDF-1.7", ocr_enabled=False, max_pdf_pages=2)
    assert name == "pdf-digital"
    assert text == "\n\n".join(
        (f"{_CAP_MARKER} digital text of page {i} " * 2).strip() for i in range(2)
    )
    # The walk stops at the cap: the pages past it are never read.
    assert read == [0, 1]


def _blank_square_pdf(side: float) -> bytes:
    import io as _io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=side, height=side)
    buf = _io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _fake_ocr_render(monkeypatch, *, pdfinfo_error: Exception | None = None) -> list[int]:
    """Stub Poppler and Tesseract for ``pdf._extract_ocr``; returns the
    DPI of each render."""
    from PIL import Image

    renders: list[int] = []

    def pdfinfo(payload, **kwargs):
        if pdfinfo_error is not None:
            raise pdfinfo_error
        return {}

    def convert(payload, **kwargs):
        renders.append(kwargs["dpi"])
        pages = kwargs["last_page"] - kwargs["first_page"] + 1
        return [Image.new("RGB", (4, 4), color="white")] * pages

    monkeypatch.setattr("pdf2image.pdfinfo_from_bytes", pdfinfo)
    monkeypatch.setattr("pdf2image.convert_from_bytes", convert)
    monkeypatch.setattr("pytesseract.image_to_string", lambda image, **_: "ocr text")
    return renders


def _cap_pdf_ocr_dpi(monkeypatch):
    from src.extractors import pdf

    renders = _fake_ocr_render(monkeypatch)
    texts = pdf._extract_ocr(_blank_square_pdf(14_400), pages=[0], ocr_timeout_seconds=60)
    assert texts == {0: "ocr text"}
    # A 200-inch square page fits the pixel budget only at 15 dpi, and
    # was rendered once at it.
    assert renders == [15]


def _cap_xlsx_sheet_nodes(monkeypatch):
    from src.extractors import xlsx

    # Both worksheets cross the node budget: the first is cut, the second
    # emptied. One extraction, so one report.
    payload = _titled_xlsx(
        [("one", [[f"{_CAP_MARKER} first"]] * 50), ("two", [[f"{_CAP_MARKER} second"]])]
    )
    monkeypatch.setattr(xlsx, "_MAX_SHEET_NODES", 300)
    scans = _count_calls(monkeypatch, xlsx, "_scan_worksheet")
    text, _ = xlsx.extract(payload)
    lines = text.split("\n")
    assert lines[0] == "[Sheet: one]"
    assert 1 < len(lines) < 51
    assert set(lines[1:]) == {f"{_CAP_MARKER} first"}
    assert scans[0] == 2


def _cap_xlsx_row_nodes(monkeypatch):
    from src.extractors import xlsx

    monkeypatch.setattr(xlsx, "_MAX_ROW_NODES", 1_000)
    wide = '<row r="3">' + '<c r="A3"><v>1</v></c>' * 400 + "</row>"
    payload = _rewrite_sheet_xml(
        _xlsx_bytes([[f"{_CAP_MARKER} first"], ["second"]]),
        _rows_before_sheet_end(wide + '<row r="4"><c r="A4"><v>4</v></c></row>'),
    )
    rows = _count_parsed_rows(monkeypatch)
    text, _ = xlsx.extract(payload)
    assert text == f"[Sheet: Sheet]\n{_CAP_MARKER} first\nsecond"
    # Only the two rows before the cut reach openpyxl.
    assert rows[0] <= 3


def _cap_xlsx_tag_bytes(monkeypatch):
    from src.extractors import xlsx

    monkeypatch.setattr(xlsx, "_SCAN_CHUNK", 256)
    monkeypatch.setattr(xlsx, "_MAX_TAG_BYTES", 1_024)
    attributes = "".join(f' a{i}="1"' for i in range(1_000))
    payload = _rewrite_sheet_xml(
        _xlsx_bytes([[f"{_CAP_MARKER} first"]]),
        _rows_before_sheet_end(f'<row r="2"><c r="A2"{attributes}/></row>'),
    )
    rows = _count_parsed_rows(monkeypatch)
    text, _ = xlsx.extract(payload)
    assert text == f"[Sheet: Sheet]\n{_CAP_MARKER} first"
    assert rows[0] <= 2


def _cap_xlsx_expanded_cells(monkeypatch):
    from src.extractors import xlsx

    # Each one-cell row costs ``1 + _ROW_COST``: two fit, the third does not.
    monkeypatch.setattr(xlsx, "_MAX_EXPANDED_CELLS", 2 * (1 + xlsx._ROW_COST))
    rows = _count_parsed_rows(monkeypatch)
    text, _ = xlsx.extract(_xlsx_bytes([[f"{_CAP_MARKER} {r}"] for r in range(10)]))
    assert text == f"[Sheet: Sheet]\n{_CAP_MARKER} 0\n{_CAP_MARKER} 1"
    assert rows[0] <= 4


def _cap_xlsx_text_chars(monkeypatch):
    from src.extractors import xlsx

    header = "[Sheet: Sheet]"
    monkeypatch.setattr(xlsx, "_MAX_TEXT_CHARS", len(header) + 2 + 6)
    rows = _count_parsed_rows(monkeypatch)
    text, _ = xlsx.extract(_xlsx_bytes([[f"{_CAP_MARKER} value"]] + [["next"]] * 20))
    assert text == f"{header}\n{_CAP_MARKER[:5]}"
    assert rows[0] == 1


def _cap_pptx_slides(monkeypatch):
    from src.extractors import pptx

    monkeypatch.setattr(pptx, "_MAX_SLIDES", 2)
    payload = _deck(*[_boxes(f"{_CAP_MARKER} {i}") for i in range(5)])
    counts = _count_pptx_work(monkeypatch)
    text, _ = pptx.extract(payload)
    assert text == f"{_CAP_MARKER} 0\n\n{_CAP_MARKER} 1"
    # The third slide is resolved, refused, and nothing after it.
    assert counts == {"slides": 3, "shapes": 2}


def _cap_pptx_shapes(monkeypatch):
    """Group members count as shapes: the group, then the box in it."""
    from src.extractors import pptx

    def build(slide):
        _box(slide.shapes, f"{_CAP_MARKER} 0")
        _box(slide.shapes.add_group_shape().shapes, f"{_CAP_MARKER} 1")
        for i in range(2, 10):
            _box(slide.shapes, f"{_CAP_MARKER} {i}")

    monkeypatch.setattr(pptx, "_MAX_SHAPES", 3)
    payload = _deck(build)
    counts = _count_pptx_work(monkeypatch)
    text, _ = pptx.extract(payload)
    assert text == f"{_CAP_MARKER} 0\n\n{_CAP_MARKER} 1"
    assert counts == {"slides": 1, "shapes": 4}


def _cap_pptx_table_cells(monkeypatch):
    """A row costs one unit and each of its cells one: the second row
    keeps its first cell."""
    from pptx.util import Inches
    from src.extractors import pptx

    def build(slide):
        table = slide.shapes.add_table(3, 2, Inches(1), Inches(1), Inches(4), Inches(1)).table
        for row in range(3):
            for col in range(2):
                table.cell(row, col).text = f"{_CAP_MARKER}{row}{col}"

    monkeypatch.setattr(pptx, "_MAX_TABLE_CELLS", 3 + 2)
    payload = _deck(build)
    read = _count_calls(monkeypatch, pptx, "_text_lines")
    text, _ = pptx.extract(payload)
    assert text == f"{_CAP_MARKER}00 {_CAP_MARKER}01\n\n{_CAP_MARKER}10"
    assert read[0] == 3


def _cap_pptx_text_chars(monkeypatch):
    """A paragraph costs ``_ELEMENT_COST``, and each element in it that
    plus its characters: the second box keeps the five characters that
    fit."""
    from src.extractors import pptx

    first = f"{_CAP_MARKER} 0"
    element = pptx._ELEMENT_COST
    monkeypatch.setattr(pptx, "_MAX_TEXT_CHARS", (2 * element + len(first)) + (2 * element + 5))
    payload = _deck(_boxes(*[f"{_CAP_MARKER} {i}" for i in range(10)]))
    counts = _count_pptx_work(monkeypatch)
    text, _ = pptx.extract(payload)
    assert text == f"{first}\n\n{_CAP_MARKER[:5]}"
    assert counts == {"slides": 1, "shapes": 2}


def _cap_docx_blocks(monkeypatch):
    """A paragraph costs one block: the third is refused unread, and so
    is the header after it."""
    from src.extractors import docx

    from tests.test_docx_walk import _p, docx_payload

    monkeypatch.setattr(docx, "_MAX_BLOCKS", 2)
    payload = docx_payload(
        "".join(_p(f"{_CAP_MARKER} {i}") for i in range(10)), header=_p(f"{_CAP_MARKER} h")
    )
    read = _count_calls(monkeypatch, docx, "_paragraph_text")
    text, _ = docx.extract(payload)
    assert text == f"{_CAP_MARKER} 0\n\n{_CAP_MARKER} 1"
    assert read[0] == 2


def _cap_docx_table_cells(monkeypatch):
    """A row costs one unit and each of its cells one: the second row
    keeps its first cell."""
    from src.extractors import docx

    from tests.test_docx_walk import _p, _tbl, _tc, _tr, docx_payload

    rows = [_tr(*[_tc(_p(f"{_CAP_MARKER}{r}{c}")) for c in range(2)]) for r in range(3)]
    monkeypatch.setattr(docx, "_MAX_TABLE_CELLS", 3 + 2)
    payload = docx_payload(_tbl(*rows))
    read = _count_calls(monkeypatch, docx, "_paragraph_text")
    text, _ = docx.extract(payload)
    assert text == f"{_CAP_MARKER}00 {_CAP_MARKER}01\n\n{_CAP_MARKER}10"
    assert read[0] == 3


def _cap_docx_text_elements(monkeypatch):
    """A run costs one element and its text element one: the second
    paragraph keeps its first run."""
    from src.extractors import docx

    from tests.test_docx_walk import _p, docx_payload

    monkeypatch.setattr(docx, "_MAX_TEXT_ELEMENTS", 2 + 2 + 2)
    payload = docx_payload(
        "".join(_p(f"{_CAP_MARKER} {i}a", f" {_CAP_MARKER} {i}b") for i in range(5))
    )
    read = _count_calls(monkeypatch, docx, "_paragraph_text")
    text, _ = docx.extract(payload)
    assert text == f"{_CAP_MARKER} 0a {_CAP_MARKER} 0b\n\n{_CAP_MARKER} 1a"
    assert read[0] == 2


def _cap_docx_text_chars(monkeypatch):
    """The second paragraph keeps the five characters that fit."""
    from src.extractors import docx

    from tests.test_docx_walk import _p, docx_payload

    first = f"{_CAP_MARKER} 0"
    monkeypatch.setattr(docx, "_MAX_TEXT_CHARS", len(first) + 5)
    payload = docx_payload("".join(_p(f"{_CAP_MARKER} {i}") for i in range(10)))
    read = _count_calls(monkeypatch, docx, "_paragraph_text")
    text, _ = docx.extract(payload)
    assert text == f"{first}\n\n{_CAP_MARKER[:5]}"
    assert read[0] == 2


def _cap_doc_output_bytes(monkeypatch):
    """A tool writing past the byte cap: the bytes before it are kept,
    and no more are read."""
    import sys
    import tempfile

    from src.extractors import doc

    tool = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    tool.write(
        f"#!{sys.executable}\nimport sys\n"
        f"sys.stdout.write({_CAP_MARKER!r})\n"
        "for _ in range(256):\n    sys.stdout.write('b' * 65536)\n"
    )
    tool.close()
    import os

    os.chmod(tool.name, 0o700)
    monkeypatch.setattr(doc.shutil, "which", lambda _name: tool.name)
    monkeypatch.setattr(doc, "_MAX_OUTPUT_BYTES", 1000)
    try:
        text, _ = doc.extract(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    finally:
        os.unlink(tool.name)
    assert len(text) == 1000
    assert text.startswith(_CAP_MARKER)


def _run_xls_child_in_process(monkeypatch, **budgets):
    """Run the xls child's walk in this process (its budgets patched) and
    hand its output to the real parent."""
    from pathlib import Path

    from src.extractors import xls, xls_child
    from src.extractors._runner import ToolOutput

    for name, value in budgets.items():
        monkeypatch.setattr(xls_child, name, value)

    def run_tool(_argv, payload, **_kwargs):
        text, caps = xls_child.extract_text(payload)
        return ToolOutput(xls_child.encode_output(text, caps), truncated=False)

    monkeypatch.setattr(xls, "run_tool", run_tool)
    fixture = Path(__file__).parent / "fixtures" / "extractors" / "legacy.xls"
    text, _ = xls.extract(fixture.read_bytes())
    return text


def _cap_xls_sheets(monkeypatch):
    text = _run_xls_child_in_process(monkeypatch, _MAX_SHEETS=1)
    assert text.startswith("[Sheet: Summary]")
    assert "OBSIDIAN" not in text


def _cap_xls_expanded_cells(monkeypatch):
    text = _run_xls_child_in_process(monkeypatch, _MAX_EXPANDED_CELLS=2 * (2 + 64))
    assert "COBALT-LANTERN" in text
    assert "OBSIDIAN" not in text


def _cap_xls_text_chars(monkeypatch):
    text = _run_xls_child_in_process(monkeypatch, _MAX_TEXT_CHARS=len("Summary") + 11 + 3)
    assert text == "[Sheet: Summary]\nIt"


def _cap_ppt_output_bytes(monkeypatch):
    """The runner returned a cut output: the bytes before the cut are
    kept. That the runner stops reading at the cap and kills the reader
    is asserted in ``test_legacy_office``."""
    import tempfile
    from pathlib import Path

    from src.extractors import ppt
    from src.extractors._runner import ToolOutput

    calls = []

    def run_tool(_argv, _payload, *, max_output_bytes, **_kwargs):
        calls.append(max_output_bytes)
        data = (_CAP_MARKER + "b" * max_output_bytes)[:max_output_bytes]
        return ToolOutput(data.encode(), truncated=True)

    home = Path(tempfile.mkdtemp())
    (home / "jre" / "bin").mkdir(parents=True)
    (home / "jre" / "bin" / "java").touch()
    monkeypatch.setattr(ppt, "PPT_HOME", home)
    monkeypatch.setattr(ppt, "run_tool", run_tool)
    monkeypatch.setattr(ppt, "_MAX_OUTPUT_BYTES", 1000)
    text, _ = ppt.extract(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    assert calls == [1000]
    assert len(text) == 1000
    assert text.startswith(_CAP_MARKER)


# Each reported cap, and an extraction that crosses it with its output
# pinned (what the code returned before #903) and the work it did.
_CAP_TRIGGERS = {
    "extracted_chars": _cap_extracted_chars,
    "pdf_digital_pages": _cap_pdf_digital_pages,
    "pdf_ocr_dpi": _cap_pdf_ocr_dpi,
    "xlsx_sheet_nodes": _cap_xlsx_sheet_nodes,
    "xlsx_row_nodes": _cap_xlsx_row_nodes,
    "xlsx_tag_bytes": _cap_xlsx_tag_bytes,
    "xlsx_expanded_cells": _cap_xlsx_expanded_cells,
    "xlsx_text_chars": _cap_xlsx_text_chars,
    "pptx_slides": _cap_pptx_slides,
    "pptx_shapes": _cap_pptx_shapes,
    "pptx_table_cells": _cap_pptx_table_cells,
    "pptx_text_chars": _cap_pptx_text_chars,
    "docx_blocks": _cap_docx_blocks,
    "docx_table_cells": _cap_docx_table_cells,
    "docx_text_elements": _cap_docx_text_elements,
    "docx_text_chars": _cap_docx_text_chars,
    "doc_output_bytes": _cap_doc_output_bytes,
    "ppt_output_bytes": _cap_ppt_output_bytes,
    "xls_sheets": _cap_xls_sheets,
    "xls_expanded_cells": _cap_xls_expanded_cells,
    "xls_text_chars": _cap_xls_text_chars,
}

# Every cap constant in the extractor modules (``module:NAME``) and every
# configured cap the dispatcher takes (``module:parameter``), with the
# cap name it is reported under ...
_REPORTED_CAPS = {
    "src.extractors:max_extracted_chars": "extracted_chars",
    "src.extractors:max_pdf_pages": "pdf_digital_pages",
    "src.extractors.pdf:_MAX_OCR_PAGE_PIXELS": "pdf_ocr_dpi",
    "src.extractors.xlsx:_MAX_SHEET_NODES": "xlsx_sheet_nodes",
    "src.extractors.xlsx:_MAX_ROW_NODES": "xlsx_row_nodes",
    "src.extractors.xlsx:_MAX_TAG_BYTES": "xlsx_tag_bytes",
    "src.extractors.xlsx:_MAX_EXPANDED_CELLS": "xlsx_expanded_cells",
    "src.extractors.xlsx:_MAX_TEXT_CHARS": "xlsx_text_chars",
    "src.extractors.pptx:_MAX_SLIDES": "pptx_slides",
    "src.extractors.pptx:_MAX_SHAPES": "pptx_shapes",
    "src.extractors.pptx:_MAX_TABLE_CELLS": "pptx_table_cells",
    "src.extractors.pptx:_MAX_TEXT_CHARS": "pptx_text_chars",
    "src.extractors.docx:_MAX_BLOCKS": "docx_blocks",
    "src.extractors.docx:_MAX_TABLE_CELLS": "docx_table_cells",
    "src.extractors.docx:_MAX_TEXT_ELEMENTS": "docx_text_elements",
    "src.extractors.docx:_MAX_TEXT_CHARS": "docx_text_chars",
    "src.extractors.doc:_MAX_OUTPUT_BYTES": "doc_output_bytes",
    "src.extractors.ppt:_MAX_OUTPUT_BYTES": "ppt_output_bytes",
    "src.extractors.xls_child:_MAX_SHEETS": "xls_sheets",
    "src.extractors.xls_child:_MAX_EXPANDED_CELLS": "xls_expanded_cells",
    "src.extractors.xls_child:_MAX_TEXT_CHARS": "xls_text_chars",
}
# ... or the reason it is not reported as an extractor cap.
_WORKBOOK_FAILS = (
    "fails the workbook (XlsxEagerPartBudgetError): an unsupported row (#931), "
    "counted as unsupported="
)
_DECK_FAILS = (
    "fails the deck (PptxPackageBudgetError): an unsupported row (#1032), counted as unsupported="
)
_DOCUMENT_FAILS = (
    "fails the document (DocxPackageBudgetError): an unsupported row (#1032), "
    "counted as unsupported="
)
_UNREPORTED_CAPS = {
    "src.extractors:max_bytes": (
        "skips the whole attachment as too_large, counted as too_large= in the aggregate"
    ),
    "src.extractors:DEFAULT_MAX_BYTES": "the default of max_bytes",
    "src.extractors:ZIP_MAX_UNCOMPRESSED_BYTES": (
        "fails the document: a failed row with its rate-limited WARNING, counted as failed="
    ),
    "src.extractors:GLOBAL_MAX_IMAGE_PIXELS": (
        "an image over it raises: a failed row with its rate-limited WARNING, counted as failed="
    ),
    "src.extractors:max_ocr_pages": (
        "PDF: reported by #884 as ocr_capped_pdfs= / ocr_pages_skipped=; "
        "multipage TIFF: reported by #885 (#916) as ocr_capped_images="
    ),
    "src.extractors:ocr_timeout_seconds": (
        "a timeout raises: a failed row with its rate-limited WARNING, counted as failed="
    ),
    "src.extractors.xlsx:_MAX_EAGER_PART_BYTES": _WORKBOOK_FAILS,
    "src.extractors.xlsx:_MAX_EAGER_BYTES": _WORKBOOK_FAILS,
    "src.extractors.xlsx:_MAX_EAGER_READS": _WORKBOOK_FAILS,
    "src.extractors.docx:_MAX_EXPANSION_BYTES": _DOCUMENT_FAILS,
    "src.extractors.docx:_MAX_MEMBERS": _DOCUMENT_FAILS,
    "src.extractors.docx:_MAX_RELS_BYTES": _DOCUMENT_FAILS,
    "src.extractors.docx:_MAX_DECLARED_BYTES": _DOCUMENT_FAILS,
    "src.extractors.pptx:_MAX_EXPANSION_BYTES": _DECK_FAILS,
    "src.extractors.pptx:_MAX_MEMBERS": _DECK_FAILS,
    "src.extractors.pptx:_MAX_RELS_BYTES": _DECK_FAILS,
    "src.extractors.pptx:_MAX_DECLARED_BYTES": _DECK_FAILS,
    "src.extractors.xls:_MAX_OUTPUT_BYTES": (
        "child output past it cannot come from a working child: XlsOutputError, a failed row "
        "with its rate-limited WARNING, counted as failed="
    ),
    "src.extractors.xls:CHILD_MAX_ADDRESS_SPACE_BYTES": (
        "the child fails (ToolExitError): a failed row with its rate-limited WARNING, "
        "counted as failed="
    ),
    "src.extractors.xls:CHILD_MAX_CPU_SECONDS": (
        "the child is killed (ToolCrashError): a failed row with its rate-limited WARNING, "
        "counted as failed="
    ),
    "src.extractors.doc:CHILD_MAX_ADDRESS_SPACE_BYTES": (
        "catdoc fails (ToolExitError): a failed row with its rate-limited WARNING, "
        "counted as failed="
    ),
    "src.extractors.doc:CHILD_MAX_CPU_SECONDS": (
        "catdoc is killed (ToolCrashError): a failed row with its rate-limited WARNING, "
        "counted as failed="
    ),
    "src.extractors.ppt:CHILD_MAX_ADDRESS_SPACE_BYTES": (
        "the JVM fails (ToolExitError): a failed row with its rate-limited WARNING, "
        "counted as failed="
    ),
    "src.extractors.ppt:CHILD_MAX_CPU_SECONDS": (
        "the JVM is killed (ToolCrashError): a failed row with its rate-limited WARNING, "
        "counted as failed="
    ),
}

_EXTRACTOR_MODULES = (
    "src.extractors",
    "src.extractors._launcher",
    "src.extractors._runner",
    "src.extractors.doc",
    "src.extractors.docx",
    "src.extractors.html",
    "src.extractors.image",
    "src.extractors.pdf",
    "src.extractors.ppt",
    "src.extractors.pptx",
    "src.extractors.text",
    "src.extractors.xls",
    "src.extractors.xls_child",
    "src.extractors.xlsx",
)


def _extractor_caps_in_code() -> set[str]:
    """Every module-level ``*MAX*`` constant the extractor modules define,
    and every ``max_*`` / ``*_seconds`` parameter of the dispatcher."""
    import importlib
    import inspect
    import pkgutil
    import re

    from src import extractors

    # A new extractor module must be added to the list above.
    submodules = {f"src.extractors.{m.name}" for m in pkgutil.iter_modules(extractors.__path__)}
    assert submodules | {"src.extractors"} == set(_EXTRACTOR_MODULES)

    found: set[str] = set()
    for name in _EXTRACTOR_MODULES:
        source = inspect.getsource(importlib.import_module(name))
        for constant in re.findall(r"^([A-Z_][A-Z0-9_]*MAX[A-Z0-9_]*)\s*[:=]", source, re.M):
            found.add(f"{name}:{constant}")
    for parameter in inspect.signature(extractors.extract).parameters:
        if parameter.startswith("max_") or parameter.endswith("_seconds"):
            found.add(f"src.extractors:{parameter}")
    return found


class TestExtractorCapsAreReported:
    """#903: a cap that cuts what an extractor returns logs a rate-limited
    WARNING naming the cap and counts once in ``extractor_caps`` per cap
    per extraction attempt. The extraction's output is unchanged."""

    @pytest.fixture(autouse=True)
    def _fresh_counts(self):
        from src import extractors

        extractors.drain_extractor_counts()

    def test_every_cap_is_reported_or_excluded_with_a_reason(self):
        found = _extractor_caps_in_code()
        assert not set(_REPORTED_CAPS) & set(_UNREPORTED_CAPS)
        assert found == set(_REPORTED_CAPS) | set(_UNREPORTED_CAPS)
        assert all(reason.strip() for reason in _UNREPORTED_CAPS.values())
        # Each reported cap has a case that crosses it below.
        assert set(_REPORTED_CAPS.values()) == set(_CAP_TRIGGERS)

    def test_the_discovery_finds_cap_constants(self):
        """Guards the completeness test: a broken pattern would find
        nothing and pass vacuously."""
        found = _extractor_caps_in_code()
        assert "src.extractors.xlsx:_MAX_TEXT_CHARS" in found
        assert "src.extractors:GLOBAL_MAX_IMAGE_PIXELS" in found
        assert "src.extractors:max_extracted_chars" in found
        assert len(found) == len(_REPORTED_CAPS) + len(_UNREPORTED_CAPS)

    @pytest.mark.parametrize("cap", sorted(_CAP_TRIGGERS))
    def test_cap_logs_a_warning_and_is_counted(self, cap, monkeypatch, caplog):
        from src import extractors

        caplog.set_level("DEBUG")
        _CAP_TRIGGERS[cap](monkeypatch)

        lines = [
            r
            for r in caplog.records
            if r.name.startswith("indexer.extractor") and "extractor cap" in r.getMessage()
        ]
        assert [r.levelname for r in lines] == ["WARNING"]
        assert f"extractor cap {cap}:" in lines[0].getMessage()
        counts = extractors.drain_extractor_counts()
        assert counts["extractor_caps"] == 1
        assert counts["warnings_suppressed"] == 0
        assert _CAP_MARKER not in caplog.text

    def test_cap_lines_share_the_warning_rate_limit(self, monkeypatch, caplog):
        """A sender can attach many capped files: past the window's budget
        the line is withheld, and still counted."""
        from src import extractors

        caplog.set_level("DEBUG")
        monkeypatch.setattr(extractors._LINE_BUDGET, "limit", 2)
        for _ in range(5):
            _cap_extracted_chars(monkeypatch)

        lines = [r for r in caplog.records if "extractor cap" in r.getMessage()]
        assert len(lines) == 2
        counts = extractors.drain_extractor_counts()
        assert counts["extractor_caps"] == 5
        assert counts["warnings_suppressed"] == 3

    @pytest.mark.parametrize(
        "run",
        [
            lambda: extract(
                content_type="text/plain",
                filename="a.txt",
                payload=b"short text",
                max_extracted_chars=64,
            ),
            lambda: extract(
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename="a.xlsx",
                payload=_xlsx_bytes([["a", "b"], ["c", "d"]]),
            ),
            lambda: extract(
                content_type=_PPTX_MIME,
                filename="a.pptx",
                payload=_deck(TestPptxExtractor._full_slide, _boxes("b")),
            ),
        ],
        ids=["text", "xlsx", "pptx"],
    )
    def test_an_extraction_under_every_cap_reports_none(self, run, caplog):
        from src import extractors

        caplog.set_level("DEBUG")
        assert run().status == STATUS_SUCCESS
        assert "extractor cap" not in caplog.text
        assert extractors.drain_extractor_counts()["extractor_caps"] == 0

    def test_pdf_with_exactly_the_page_cap_reports_none(self, monkeypatch, caplog):
        from src import extractors
        from src.extractors import pdf

        class Page:
            def extract_text(self):
                return "digital text long enough to pass the floor easily"

        class FakeReader:
            def __init__(self, stream):
                self.pages = [Page(), Page()]

        caplog.set_level("DEBUG")
        monkeypatch.setattr(pdf.pypdf, "PdfReader", FakeReader)
        pdf.extract(b"%PDF-1.7", ocr_enabled=False, max_pdf_pages=2)
        assert "extractor cap" not in caplog.text
        assert extractors.drain_extractor_counts()["extractor_caps"] == 0

    def test_ordinary_page_renders_at_full_dpi_and_reports_none(self, monkeypatch, caplog):
        from src import extractors
        from src.extractors import pdf

        caplog.set_level("DEBUG")
        renders = _fake_ocr_render(monkeypatch)
        pdf._extract_ocr(_blank_square_pdf(612), pages=[0])
        assert renders == [pdf._OCR_DPI]
        assert "extractor cap" not in caplog.text
        assert extractors.drain_extractor_counts()["extractor_caps"] == 0

    def test_lowered_dpi_is_not_reported_when_no_render_ran(self, monkeypatch, caplog):
        """Review round 3: the DPI cap was counted when the DPI was chosen,
        so a Poppler failure before any render still reported a
        reduced-resolution OCR. It is reported once a render at it ran."""
        from src import extractors
        from src.extractors import pdf

        caplog.set_level("DEBUG")
        renders = _fake_ocr_render(monkeypatch, pdfinfo_error=TimeoutError("pdfinfo"))
        with pytest.raises(TimeoutError):
            pdf._extract_ocr(_blank_square_pdf(14_400), pages=[0], ocr_timeout_seconds=60)
        assert renders == []
        assert "extractor cap" not in caplog.text
        assert extractors.drain_extractor_counts()["extractor_caps"] == 0

    def test_lowered_dpi_is_reported_once_across_render_runs(self, monkeypatch, caplog):
        """Two runs of non-consecutive pages are two renders of one OCR
        pass: one report."""
        import io as _io

        from pypdf import PdfWriter
        from src import extractors
        from src.extractors import pdf

        writer = PdfWriter()
        for _ in range(3):
            writer.add_blank_page(width=14_400, height=14_400)
        buf = _io.BytesIO()
        writer.write(buf)
        caplog.set_level("DEBUG")
        renders = _fake_ocr_render(monkeypatch)
        pdf._extract_ocr(buf.getvalue(), pages=[0, 2])
        assert renders == [15, 15]
        assert caplog.text.count("extractor cap pdf_ocr_dpi") == 1
        assert extractors.drain_extractor_counts()["extractor_caps"] == 1

    @pytest.mark.parametrize(
        ("sheets", "reported"),
        [
            # The last value spends the budget exactly: nothing is cut.
            ([("Sheet", [["abcdef"]])], False),
            # So does an empty sheet after it, whose header was never due.
            ([("Sheet", [["abcdef"]]), ("two", [])], False),
            # A row after the exactly spent budget is cut.
            ([("Sheet", [["abcdef"], ["next"]])], True),
            # As is a later sheet with a value.
            ([("Sheet", [["abcdef"]]), ("two", [["next"]])], True),
        ],
        ids=["last-value", "empty-sheet-after", "row-after", "sheet-after"],
    )
    def test_text_budget_spent_exactly_reports_only_a_real_cut(
        self, sheets, reported, monkeypatch, caplog
    ):
        """Review round 1: a budget spent exactly by the workbook's last
        value reported ``xlsx_text_chars`` although nothing was cut. The
        text returned is the same either way."""
        from src import extractors
        from src.extractors import xlsx

        caplog.set_level("DEBUG")
        # The header ``[Sheet: Sheet]`` and the blank line before it, then
        # ``abcdef`` and its newline.
        monkeypatch.setattr(xlsx, "_MAX_TEXT_CHARS", len("[Sheet: Sheet]") + 2 + 7)
        text, _ = xlsx.extract(_titled_xlsx(sheets))
        assert text == "[Sheet: Sheet]\nabcdef"
        assert ("extractor cap xlsx_text_chars" in caplog.text) is reported
        assert extractors.drain_extractor_counts()["extractor_caps"] == int(reported)

    @pytest.mark.parametrize(
        ("cap", "setting", "slides", "reported"),
        [
            ("pptx_slides", 2, [["a"], ["b"]], False),
            ("pptx_slides", 2, [["a"], ["b"], ["c"]], True),
            ("pptx_shapes", 2, [["a", "b"]], False),
            ("pptx_shapes", 2, [["a"], ["b", "c"]], True),
            ("pptx_text_chars", 2 * 8 + 6, [["abcdef"]], False),
            ("pptx_text_chars", 2 * 8 + 6, [["abcdef"], ["g"]], True),
        ],
    )
    def test_pptx_budget_spent_exactly_reports_only_a_real_cut(
        self, cap, setting, slides, reported, monkeypatch, caplog
    ):
        """As for the XLSX text budget (review round 1 on #903): a budget
        the deck's last item spends exactly cut nothing."""
        from src import extractors
        from src.extractors import pptx

        constant = {
            "pptx_slides": "_MAX_SLIDES",
            "pptx_shapes": "_MAX_SHAPES",
            "pptx_text_chars": "_MAX_TEXT_CHARS",
        }[cap]
        assert pptx._ELEMENT_COST == 8
        monkeypatch.setattr(pptx, constant, setting)
        caplog.set_level("DEBUG")
        pptx.extract(_deck(*[_boxes(*texts) for texts in slides]))
        assert ("extractor cap" in caplog.text) is reported
        assert extractors.drain_extractor_counts()["extractor_caps"] == int(reported)

    @pytest.mark.parametrize(("rows", "reported"), [(1, False), (2, True)])
    def test_pptx_cell_budget_spent_exactly_reports_only_a_real_cut(
        self, rows, reported, monkeypatch, caplog
    ):
        from pptx.util import Inches
        from src import extractors
        from src.extractors import pptx

        def build(slide):
            table = slide.shapes.add_table(rows, 2, Inches(1), Inches(1), Inches(4), Inches(1))
            for row in range(rows):
                table.table.cell(row, 0).text = "x"

        monkeypatch.setattr(pptx, "_MAX_TABLE_CELLS", 3)
        caplog.set_level("DEBUG")
        assert pptx.extract(_deck(build))[0] == "x"
        assert ("extractor cap pptx_table_cells" in caplog.text) is reported
        assert extractors.drain_extractor_counts()["extractor_caps"] == int(reported)

    def test_spent_text_budget_stops_at_the_first_unread_value(self, monkeypatch):
        """After an exactly spent budget the walk reads on only to find a
        value it cannot keep, then stops: the rows after it are not
        parsed."""
        from src.extractors import xlsx

        monkeypatch.setattr(xlsx, "_MAX_TEXT_CHARS", len("[Sheet: Sheet]") + 2 + 7)
        rows = _count_parsed_rows(monkeypatch)
        text, _ = xlsx.extract(_xlsx_bytes([["abcdef"], ["next"]] + [["more"]] * 50))
        assert text == "[Sheet: Sheet]\nabcdef"
        assert rows[0] == 2
