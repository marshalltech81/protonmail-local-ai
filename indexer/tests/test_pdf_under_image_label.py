"""#1415: a PDF under an image label runs the PDF extractor.

The dispatcher routes an ``image``-module payload starting ``%PDF-`` to
``pdf`` before the OCR gate, the cache key follows the same routine, and
the "OCR disabled" result of an image is stamped (``image@5`` from
#1415, the current ``image`` version since), so a row
recorded with no stamp, which may hold such a PDF, is re-run once whatever
the OCR setting: by the cache check and by the startup sweep, under both
legacy keys (``''`` from migration 0001 and ``image``). Synthetic payloads
only.
"""

from __future__ import annotations

import hashlib
import io
import logging

import pytest
from src import attachment_indexing, extractors, main
from src.attachment_indexing import (
    NO_EXTRACTOR_MODULE,
    _unsupported_still_holds,
    prepare_attachment_writes,
    reprocess_reruns_extraction,
)
from src.extractors import (
    EXTRACTOR_VERSIONS,
    OCR_DISABLED_ERROR,
    SCANNED_PDF_OCR_DISABLED_ERROR,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
    extract,
    extraction_module,
    label_extraction_modules,
)
from src.parser import Attachment
from src.queue import REASON_INITIAL_SCAN, REASON_REEXTRACT

from tests.test_extraction_budget import Pipeline

MARKER = "SYNTHETIC_1415_MARKER"
# The stamp the current ``image`` version writes (``image@5`` when #1415
# landed; #1401 moved it on).
_STAMP = f"image@{EXTRACTOR_VERSIONS['image']}"

# Image labels: by MIME type, by a generic ``image/*`` type and by
# extension under a generic MIME type.
_IMAGE_LABELS = (
    ("image/png", "scan.png"),
    ("image/jpeg", "scan.jpg"),
    ("image/heic", "scan.heic"),
    ("image/x-synthetic", "scan.bin"),
    ("application/octet-stream", "scan.tiff"),
)

_PNG = b"\x89PNG\r\n\x1a\n" + MARKER.encode() + bytes(64)


def _digital_pdf(text: str = f"{MARKER} digital text layer for the synthetic account") -> bytes:
    """A one-page PDF with a digital text layer."""
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
    page = writer.add_blank_page(width=612, height=792)
    stream = ContentStream(None, writer)
    stream._data = b"BT /F1 12 Tf 72 720 Td (" + text.encode() + b") Tj ET"
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _blank_pdf() -> bytes:
    """A one-page PDF with no text layer (what pypdf sees on a scan)."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _record_modules(monkeypatch) -> list[str]:
    """Record the module of each per-format extractor the dispatcher runs."""
    calls: list[str] = []
    real_import = extractors._safe_import

    def recording_import(module_name):
        fn = real_import(module_name)
        assert fn is not None

        def wrapped(payload, **opts):
            calls.append(module_name)
            return fn(payload, **opts)

        return wrapped

    monkeypatch.setattr(extractors, "_safe_import", recording_import)
    return calls


class TestRouting:
    @pytest.mark.parametrize("ocr_enabled", [True, False])
    @pytest.mark.parametrize(("content_type", "filename"), _IMAGE_LABELS)
    def test_a_pdf_under_an_image_label_runs_the_pdf_extractor(
        self, monkeypatch, content_type, filename, ocr_enabled
    ):
        calls = _record_modules(monkeypatch)
        payload = _digital_pdf()
        result = extract(
            content_type=content_type, filename=filename, payload=payload, ocr_enabled=ocr_enabled
        )
        assert result.status == STATUS_SUCCESS
        assert (result.extractor or "").startswith("pdf")
        assert MARKER in (result.text or "")
        # The PDF extractor ran once; the image extractor never did.
        assert calls == ["pdf"]
        # The cache key and every module the label can select agree.
        assert extraction_module(content_type, filename, payload) == "pdf"
        assert label_extraction_modules(content_type, filename) == {"image", "pdf"}

    @pytest.mark.parametrize(("content_type", "filename"), _IMAGE_LABELS)
    def test_a_scanned_pdf_under_an_image_label_with_ocr_off_is_the_pdf_row(
        self, monkeypatch, content_type, filename
    ):
        calls = _record_modules(monkeypatch)
        result = extract(
            content_type=content_type, filename=filename, payload=_blank_pdf(), ocr_enabled=False
        )
        assert (result.status, result.error, result.extractor) == (
            STATUS_UNSUPPORTED,
            SCANNED_PDF_OCR_DISABLED_ERROR,
            None,
        )
        assert calls == ["pdf"]

    @pytest.mark.parametrize(("content_type", "filename"), _IMAGE_LABELS)
    def test_image_bytes_keep_the_image_module_and_a_stamped_ocr_disabled_result(
        self, monkeypatch, content_type, filename
    ):
        calls = _record_modules(monkeypatch)
        result = extract(
            content_type=content_type, filename=filename, payload=_PNG, ocr_enabled=False
        )
        assert (result.status, result.error, result.extractor) == (
            STATUS_UNSUPPORTED,
            OCR_DISABLED_ERROR,
            _STAMP,
        )
        assert calls == []
        assert extraction_module(content_type, filename, _PNG) == "image"

    def test_other_labels_are_not_rerouted(self):
        pdf = _digital_pdf()
        assert extraction_module("application/pdf", "a.pdf", pdf) == "pdf"
        assert label_extraction_modules("application/pdf", "a.pdf") == {"pdf"}
        # A PDF under a text label stays the text module's binary-as-text row.
        assert extraction_module("text/plain", "a.txt", pdf) == "text"
        assert label_extraction_modules("text/plain", "a.txt") == {"text"}


class TestLegacyPredicate:
    @pytest.mark.parametrize(
        ("error", "extractor", "on", "off"),
        [
            # The unstamped image row never holds (#1415).
            (OCR_DISABLED_ERROR, None, False, False),
            # A stamped one holds while OCR is off.
            (OCR_DISABLED_ERROR, _STAMP, False, True),
            # The scanned-PDF row is unaffected.
            (SCANNED_PDF_OCR_DISABLED_ERROR, None, False, True),
        ],
    )
    @pytest.mark.parametrize("module", ["image", NO_EXTRACTOR_MODULE])
    def test_unsupported_still_holds(self, error, extractor, on, off, module):
        assert _unsupported_still_holds(error, module, True, extractor) is on
        assert _unsupported_still_holds(error, module, False, extractor) is off

    @pytest.mark.parametrize(("content_type", "filename"), _IMAGE_LABELS)
    @pytest.mark.parametrize("module", ["image", NO_EXTRACTOR_MODULE])
    def test_reprocess_reruns_an_unstamped_row(self, content_type, filename, module):
        assert reprocess_reruns_extraction(OCR_DISABLED_ERROR, module, content_type, filename, None)


def _attachment(payload: bytes, content_type: str = "image/png", filename: str = "scan.png"):
    return Attachment(
        filename=filename,
        content_type=content_type,
        size=len(payload),
        payload=payload,
        content_hash=hashlib.sha256(payload).hexdigest(),
    )


def _prepare(db, attachment, *, ocr_enabled):
    return prepare_attachment_writes(
        db=db,
        attachment=attachment,
        claimant_id="message@example.com",
        chunk_target_tokens=350,
        chunk_max_tokens=500,
        chunk_overlap_tokens=60,
        ocr_enabled=ocr_enabled,
        max_bytes=10_000_000,
        max_ocr_pages=20,
    )


class TestCacheCheck:
    @pytest.fixture
    def db(self, tmp_path):
        from src.database import EMBEDDING_DIM, Database

        from tests.conftest import make_message, make_thread

        db = Database(tmp_path / "mail.db")
        db.upsert_thread(
            make_thread(
                messages=[make_message(message_id="message@example.com")], thread_id="thread-1"
            ),
            [0.0] * EMBEDDING_DIM,
        )
        yield db
        db.close()

    def _counting(self, monkeypatch) -> list[bytes]:
        calls: list[bytes] = []

        def counting(**kwargs):
            calls.append(kwargs["payload"])
            return extract(**kwargs)

        monkeypatch.setattr(attachment_indexing, "extract_attachment", counting)
        return calls

    @pytest.mark.parametrize("old", [None, "image@5"], ids=["unstamped", "historical-image@5"])
    def test_an_unstamped_image_row_is_re_extracted_and_stamped(self, db, monkeypatch, old):
        """With OCR off: the unstamped row does not short-circuit, the fresh
        result is stamped with the current ``image`` version and persisted,
        and from then on it does. A row #1415 stamped ``image@5`` is older
        than the current version (#1401), so it is refreshed once the same
        way, then reused."""
        calls = self._counting(monkeypatch)
        attachment = _attachment(_PNG)
        db.store_attachment_extraction(
            attachment_id=attachment.content_hash,
            extractor_module="image",
            extraction_status=STATUS_UNSUPPORTED,
            extractor=old,
            extracted_text=None,
            extraction_error=OCR_DISABLED_ERROR,
        )
        plan = _prepare(db, attachment, ocr_enabled=False)
        assert (plan.status, plan.cached, len(calls)) == (STATUS_UNSUPPORTED, False, 1)
        assert plan.extraction_to_persist is not None
        assert plan.extraction_to_persist.extractor == _STAMP
        db.store_attachment_extraction(
            attachment_id=attachment.content_hash,
            extractor_module="image",
            extraction_status=plan.extraction_to_persist.status,
            extractor=plan.extraction_to_persist.extractor,
            extracted_text=None,
            extraction_error=plan.extraction_to_persist.error,
            text_complete=False,
        )
        again = _prepare(db, attachment, ocr_enabled=False)
        assert (again.status, again.cached, len(calls)) == (STATUS_UNSUPPORTED, True, 1)

    def test_an_image_labelled_pdf_shares_the_pdf_row(self, db, monkeypatch):
        """The image-labelled occurrence is keyed ``pdf``, so it is served the
        row a PDF-labelled occurrence of the same bytes wrote."""
        calls = self._counting(monkeypatch)
        payload = _digital_pdf()
        as_pdf = _attachment(payload, "application/pdf", "a.pdf")
        as_image = _attachment(payload)
        assert attachment_indexing.extraction_cache_module(as_image) == "pdf"
        first = _prepare(db, as_pdf, ocr_enabled=False)
        assert first.extraction_to_persist is not None
        db.store_attachment_extraction(
            attachment_id=as_pdf.content_hash,
            extractor_module="pdf",
            extraction_status=first.extraction_to_persist.status,
            extractor=first.extraction_to_persist.extractor,
            extracted_text=first.extraction_to_persist.text,
            extraction_error=None,
            text_complete=first.extraction_to_persist.text_complete,
        )
        second = _prepare(db, as_image, ocr_enabled=False)
        assert (second.status, second.cached) == (STATUS_SUCCESS, True)
        assert len(calls) == 1


class _RealExtract:
    """``extract`` itself, recording each payload it is called on."""

    def __init__(self):
        self.calls: list[bytes] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs["payload"])
        return extract(**kwargs)


def _jobs(p) -> dict[str, tuple]:
    return {
        r["filepath"]: (r["reason"], r["status"])
        for r in p.db._conn.execute("SELECT filepath, reason, status FROM indexing_jobs")
    }


def _rows(p) -> list[tuple]:
    return sorted(
        tuple(r)
        for r in p.db._conn.execute(
            "SELECT a.filename, a.extractor_module, e.extractor, e.extraction_status, "
            "e.extraction_error FROM attachments a JOIN attachment_extractions e "
            "ON e.attachment_id = a.attachment_id AND e.extractor_module = a.extractor_module"
        )
    )


def _unstamp(p, filename: str, *, module: str) -> None:
    """Rewrite one occurrence's row as a release before #1415 left it: no
    stamp, keyed ``module`` (``''`` is migration 0001's key)."""
    aid = p.db._conn.execute(
        "SELECT attachment_id FROM attachments WHERE filename = ?", (filename,)
    ).fetchone()[0]
    p.db._conn.execute(
        "UPDATE attachment_extractions SET extractor = NULL, extractor_module = ?, "
        "extraction_status = 'unsupported', extraction_error = ?, extracted_text = NULL "
        "WHERE attachment_id = ?",
        (module, OCR_DISABLED_ERROR, aid),
    )
    p.db._conn.execute(
        "UPDATE attachments SET extractor_module = ? WHERE attachment_id = ?", (module, aid)
    )
    p.db._conn.commit()


class TestSweep:
    """The startup sweep with OCR off re-queues the unstamped rows once,
    under both legacy keys, and nothing else of the "OCR disabled" arm."""

    def _pipeline(self, tmp_path, monkeypatch, *, ocr: bool) -> tuple[Pipeline, dict[str, str]]:
        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", ocr)
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
        p = Pipeline(tmp_path, monkeypatch, _RealExtract(), launches=50)
        pdf = _digital_pdf()
        paths = {
            # An image-labelled PDF recorded "OCR disabled" with no stamp,
            # keyed ``image`` (after #928) and ``''`` (migration 0001).
            "legacy_image": p.add("legacy_image", [(pdf, "image/png", f"{MARKER}-1.png")]),
            "legacy_blank": p.add("legacy_blank", [(pdf + b"\n", "image/jpeg", f"{MARKER}-2.jpg")]),
            # A genuine image, unstamped too.
            "legacy_png": p.add("legacy_png", [(_PNG, "image/png", f"{MARKER}-3.png")]),
            # Untouched while OCR is off: a scanned PDF and a stamped image row.
            "scanned": p.add("scanned", [(_blank_pdf(), "application/pdf", f"{MARKER}-4.pdf")]),
            "stamped": p.add("stamped", [(_PNG + b"x", "image/png", f"{MARKER}-5.png")]),
            # Qualifying, but dead-lettered or already pending.
            "dead": p.add("dead", [(pdf + b"\n\n", "image/png", f"{MARKER}-6.png")]),
            "pending": p.add("pending", [(pdf + b"\n\n\n", "image/png", f"{MARKER}-7.png")]),
        }
        while _jobs(p):
            p.drain()
        _unstamp(p, f"{MARKER}-1.png", module="image")
        _unstamp(p, f"{MARKER}-2.jpg", module=NO_EXTRACTOR_MODULE)
        _unstamp(p, f"{MARKER}-3.png", module="image")
        _unstamp(p, f"{MARKER}-6.png", module="image")
        _unstamp(p, f"{MARKER}-7.png", module="image")
        p.queue.enqueue(paths["dead"], REASON_INITIAL_SCAN)
        p.queue.mark_dead_terminal(paths["dead"], stage="parse", error="x")
        p.queue.enqueue(paths["pending"], REASON_INITIAL_SCAN)
        p.extractor.calls.clear()
        return p, paths

    def test_ocr_off_requeues_the_unstamped_rows_once(self, tmp_path, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        p, paths = self._pipeline(tmp_path, monkeypatch, ocr=False)
        before = _rows(p)
        assert (f"{MARKER}-4.pdf", "pdf", None, "unsupported", SCANNED_PDF_OCR_DISABLED_ERROR) in (
            before
        )
        assert (f"{MARKER}-5.png", "image", _STAMP, "unsupported", OCR_DISABLED_ERROR) in before

        caplog.clear()
        assert main._requeue_stale_extractions(p.db, p.queue) == 3
        assert _jobs(p) == {
            paths["legacy_image"]: (REASON_REEXTRACT, "queued"),
            paths["legacy_blank"]: (REASON_REEXTRACT, "queued"),
            paths["legacy_png"]: (REASON_REEXTRACT, "queued"),
            paths["dead"]: (REASON_INITIAL_SCAN, "dead"),
            paths["pending"]: (REASON_INITIAL_SCAN, "queued"),
        }
        lines = [r for r in caplog.records if r.getMessage().startswith("re-queued ")]
        assert len(lines) == 1
        # Five candidates: three queued, one pending, one dead-lettered.
        assert lines[0].levelno == logging.WARNING
        assert lines[0].getMessage().startswith("re-queued 3 of 5 message(s) (0 for a missing")
        assert "; 1 already pending, skipped 1 dead-lettered " in lines[0].getMessage()

        while any(status == "queued" for _r, status in _jobs(p).values()):
            p.drain()
        # Each unstamped row was re-extracted once, and the pending message too.
        assert len(p.extractor.calls) == 4
        after = _rows(p)
        assert (f"{MARKER}-1.png", "pdf", "pdf-digital@5", "success", None) in after
        assert (f"{MARKER}-2.jpg", "pdf", "pdf-digital@5", "success", None) in after
        assert (f"{MARKER}-3.png", "image", _STAMP, "unsupported", OCR_DISABLED_ERROR) in after
        assert (f"{MARKER}-7.png", "pdf", "pdf-digital@5", "success", None) in after
        # Untouched: the scanned PDF, the stamped image and the dead letter.
        for kept in (f"{MARKER}-4.pdf", f"{MARKER}-5.png", f"{MARKER}-6.png"):
            assert [r for r in after if r[0] == kept] == [r for r in before if r[0] == kept]

        # Once only: a second sweep finds only the dead-lettered message.
        caplog.clear()
        assert main._requeue_stale_extractions(p.db, p.queue) == 0
        lines = [r for r in caplog.records if r.getMessage().startswith("re-queued ")]
        assert [r.getMessage()[:28] for r in lines] == ["re-queued 0 of 1 message(s) "]

        assert MARKER not in caplog.text
        errors = [r[0] for r in p.db._conn.execute("SELECT last_error FROM indexing_jobs")]
        assert all(MARKER not in (e or "") for e in errors)

    def test_ocr_on_requeues_every_ocr_disabled_row(self, tmp_path, monkeypatch):
        p, paths = self._pipeline(tmp_path, monkeypatch, ocr=False)
        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", True)
        # The three unstamped rows, the scanned PDF and the stamped image.
        assert main._requeue_stale_extractions(p.db, p.queue) == 5

    def test_extraction_off_leaves_the_rows(self, tmp_path, monkeypatch):
        p, paths = self._pipeline(tmp_path, monkeypatch, ocr=False)
        before = _rows(p)
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", False)
        assert main._requeue_stale_extractions(p.db, p.queue) == 0
        assert _rows(p) == before
        assert _jobs(p) == {
            paths["dead"]: (REASON_INITIAL_SCAN, "dead"),
            paths["pending"]: (REASON_INITIAL_SCAN, "queued"),
        }
