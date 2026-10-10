"""PDF extraction pinned over a catalogue of synthetic shapes (#1293).

Each shape runs through the dispatcher with Poppler and Tesseract faked
at their libraries (``pdf2image``, ``pytesseract``), and everything the
extraction decides is compared with a pin: the stored result (status,
extractor, text, error, ``ocr_pages_skipped``, ``text_complete`` and the
cap columns), the counts it added to the attachments aggregate, the
progress callbacks and the work done (each render's pages and DPI, each
OCR call, each page count). The pins were recorded from the in-process
extractor on ``main`` before it moved to the extractor child
(``PDF_CATALOGUE_UPDATE=1`` rewrites them), so a difference is a change
in what the child path stores. PDFs are synthetic.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from src import extractors
from src.extractors import extract

PIN = Path(__file__).parent / "fixtures" / "pdf_catalogue_pin.json"
UPDATE = os.environ.get("PDF_CATALOGUE_UPDATE") == "1"
MARKER = "SYNTHETIC_PDF_CATALOGUE_MARKER"

DIGITAL = "Synthetic catalogue statement for the account holder, page {n}."
TINY = "p{n}"


def _pdf(layout: str, *, encrypt: tuple[str, str] | None = None) -> bytes:
    """One page per character:

    * ``d``: a digital text layer over the 40-character floor;
    * ``t``: a digital text layer under it (a stamped page number);
    * ``s``: no text layer (what pypdf sees on a scanned page);
    * ``b``: a content stream pypdf cannot decode (an unknown filter),
      so the page's text extraction raises;
    * ``L``: a 200-inch square page with no text layer, which lowers the
      OCR DPI for the whole document.

    ``encrypt`` is (user password, algorithm)."""
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
        side = 14_400 if kind == "L" else None
        page = writer.add_blank_page(width=side or 612, height=side or 792)
        if kind in "dtb":
            stream = ContentStream(None, writer)
            text = (DIGITAL if kind != "t" else TINY).format(n=n).encode()
            stream._data = b"BT /F1 12 Tf 72 720 Td (" + text + b") Tj ET"
            if kind == "b":
                stream[NameObject("/Filter")] = NameObject("/SyntheticUnknownDecode")
            page[NameObject("/Contents")] = writer._add_object(stream)
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
    writer.add_metadata({"/Title": MARKER})
    if encrypt is not None:
        user_password, algorithm = encrypt
        writer.encrypt(
            user_password=user_password,
            owner_password="synthetic-owner-password",  # pragma: allowlist secret
            algorithm=algorithm,
        )
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _deep_tree() -> bytes:
    from tests.test_extractors import _deep_page_tree_pdf

    return _deep_page_tree_pdf(MARKER)


# Shape id -> (payload, dispatcher keyword arguments, fake options). The
# fake options: ``ocr`` is what each Tesseract call returns (``text``:
# words, ``empty``: nothing, ``fail``: it raises), ``pdfinfo_fail`` makes
# the timed page count raise.
_SHAPES: dict[str, tuple[object, dict, dict]] = {
    "digital": ("dd", {}, {}),
    "digital-one-page": ("d", {}, {}),
    "scanned": ("sss", {}, {}),
    "mixed": ("ds", {}, {}),
    "mixed-runs": ("sdsds", {}, {}),
    "tiny-text-pages": ("tdt", {}, {}),
    "tiny-text-only": ("tt", {}, {}),
    "empty-pdf-no-text": ("s", {"ocr_timeout_seconds": None}, {"ocr": "empty"}),
    "scanned-ocr-reads-nothing": ("ss", {}, {"ocr": "empty"}),
    "mixed-ocr-reads-nothing": ("ds", {}, {"ocr": "empty"}),
    "ocr-off-digital": ("dd", {"ocr_enabled": False}, {}),
    "ocr-off-scanned": ("ss", {"ocr_enabled": False}, {}),
    "ocr-off-mixed": ("ds", {"ocr_enabled": False}, {}),
    "ocr-off-tiny": ("t", {"ocr_enabled": False}, {}),
    "ocr-off-broken-page": ("dbd", {"ocr_enabled": False}, {}),
    "broken-page-ocr-recovers": ("dbd", {}, {}),
    "broken-page-ocr-reads-nothing": ("dbd", {}, {"ocr": "empty"}),
    "broken-only": ("b", {}, {}),
    "ocr-fails-mixed": ("ds", {}, {"ocr": "fail"}),
    "ocr-fails-scanned": ("ss", {}, {"ocr": "fail"}),
    "ocr-fails-broken-mixed": ("dbd", {}, {"ocr": "fail"}),
    "pdfinfo-fails-scanned": ("s", {}, {"pdfinfo_fail": True}),
    "ocr-no-timeout": ("sds", {"ocr_timeout_seconds": None}, {}),
    "ocr-short-timeout": ("ss", {"ocr_timeout_seconds": 7}, {}),
    "over-ocr-cap": ("sssss", {"max_ocr_pages": 2}, {}),
    "over-ocr-cap-mixed": ("dsdsss", {"max_ocr_pages": 2}, {}),
    "at-ocr-cap": ("ss", {"max_ocr_pages": 2}, {}),
    "ocr-cap-off": ("sss", {"max_ocr_pages": 0}, {}),
    "over-ocr-cap-ocr-fails": ("sss", {"max_ocr_pages": 1}, {"ocr": "fail"}),
    "over-digital-cap": ("ddd", {"max_pdf_pages": 2}, {}),
    "at-digital-cap": ("dd", {"max_pdf_pages": 2}, {}),
    "digital-cap-zero": ("dd", {"max_pdf_pages": 0}, {}),
    "over-digital-cap-scanned": ("sss", {"max_pdf_pages": 1}, {}),
    "over-both-caps": ("sssd", {"max_pdf_pages": 3, "max_ocr_pages": 1}, {}),
    "over-digital-cap-ocr-off": ("ddd", {"max_pdf_pages": 1, "ocr_enabled": False}, {}),
    "large-page-lowers-dpi": ("sL", {}, {}),
    "large-page-ocr-fails": ("dL", {}, {"ocr": "fail"}),
    "extracted-chars-cut": ("dd", {"max_extracted_chars": 30}, {}),
    "owner-password-only-aes": ("d", {}, {"encrypt": ("", "AES-256")}),
    "owner-password-only-rc4": ("d", {}, {"encrypt": ("", "RC4-128")}),
    "open-password": ("d", {}, {"encrypt": ("synthetic-open", "AES-256")}),
    "deep-page-tree": (_deep_tree, {}, {}),
    "not-a-pdf": (b"%PDF-1.7 synthetic garbage " + MARKER.encode(), {}, {}),
    "truncated": ("truncated", {}, {}),
}


def _payload(spec: object, fake: dict) -> bytes:
    if isinstance(spec, bytes):
        return spec
    if callable(spec):
        return spec()
    assert isinstance(spec, str)
    if spec == "truncated":
        return _pdf("dd")[:400]
    return _pdf(spec, encrypt=fake.get("encrypt"))


def _fake_poppler_and_tesseract(monkeypatch, fake: dict) -> dict:
    """Record each page count, render and OCR call."""
    from PIL import Image

    work: dict = {"pdfinfo": [], "renders": [], "ocr_calls": 0}

    def pdfinfo(_payload, **kwargs):
        work["pdfinfo"].append(kwargs.get("timeout"))
        if fake.get("pdfinfo_fail"):
            raise RuntimeError(MARKER)
        return {"Pages": 99}

    def convert(_payload, **kwargs):
        first, last = kwargs["first_page"], kwargs["last_page"]
        timeout = kwargs.get("timeout")
        work["renders"].append(
            [first, last, kwargs["dpi"], None if timeout is None else round(timeout, 1)]
        )
        return [Image.new("RGB", (4, 4), color="white") for _ in range(first, last + 1)]

    def tesseract(_image, **kwargs):
        work["ocr_calls"] += 1
        mode = fake.get("ocr", "text")
        if mode == "fail":
            raise RuntimeError(MARKER)
        if mode == "empty":
            return "  \n"
        return f"OCR words of call {work['ocr_calls']} timeout {kwargs.get('timeout')}\n"

    monkeypatch.setattr("pdf2image.pdfinfo_from_bytes", pdfinfo)
    monkeypatch.setattr("pdf2image.convert_from_bytes", convert)
    monkeypatch.setattr("pytesseract.image_to_string", tesseract)
    return work


def _observe(shape: str, monkeypatch) -> dict:
    spec, kwargs, fake = _SHAPES[shape]
    payload = _payload(spec, fake)
    work = _fake_poppler_and_tesseract(monkeypatch, fake)
    kwargs = {"ocr_timeout_seconds": 60, **kwargs}
    extractors.drain_extractor_counts()
    progress: list[int] = []
    result = extract(
        content_type="application/pdf",
        filename=f"{MARKER}.pdf",
        payload=payload,
        on_progress=lambda: progress.append(1),
        **kwargs,
    )
    counts = {
        key: n
        for key, n in extractors.drain_extractor_counts().items()
        if n and key != "warnings_suppressed"
    }
    return {
        "result": {
            "status": result.status,
            "extractor": result.extractor,
            "text": result.text,
            "error": result.error,
            "ocr_pages_skipped": result.ocr_pages_skipped,
            "text_complete": result.text_complete,
            "ocr_pages_cap": result.ocr_pages_cap,
            "digital_pages_cap": result.digital_pages_cap,
            "extracted_chars_cap": result.extracted_chars_cap,
        },
        "counts": counts,
        "progress": len(progress),
        "work": work,
    }


def _pins() -> dict:
    return json.loads(PIN.read_text()) if PIN.exists() else {}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_pdf_shape_matches_the_in_process_pin(shape, monkeypatch, caplog):
    caplog.set_level("DEBUG")
    observed = _observe(shape, monkeypatch)
    if UPDATE:
        pins = _pins()
        pins[shape] = observed
        PIN.write_text(json.dumps(pins, indent=1, sort_keys=True) + "\n")
        return
    assert observed == _pins()[shape]
    assert MARKER not in caplog.text


def test_every_pin_has_a_shape():
    """A shape removed from the catalogue leaves no stale pin."""
    assert UPDATE or sorted(_pins()) == sorted(_SHAPES)
