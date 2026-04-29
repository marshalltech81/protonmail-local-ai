"""PDF extractor with OCR fallback.

Two paths share one entry point:

1. **Digital PDFs** (most generated invoices, statements, contracts):
   ``pypdf`` walks the page tree and pulls out the embedded text layer.
   Cheap — typical page is a few ms — and exact (no OCR error rate).

2. **Scanned PDFs** (photos of paper, faxes, signed PDFs flattened to
   image): the digital path returns empty or near-empty text. We fall
   through to OCR by rendering each page to a PIL image via
   ``pdf2image`` (which calls out to Poppler's ``pdftoppm``) and
   passing the image to Tesseract.

The OCR fallback is gated by ``ocr_enabled`` and bounded by
``max_ocr_pages`` so a 500-page scanned book attachment does not
monopolise CPU. Pages beyond the cap are silently skipped — the
indexer logs the truncation via the dispatcher's ``failed`` /
``empty`` accounting and the operator can lift the cap and reprocess
the cached extraction if needed.
"""

from __future__ import annotations

import io
import logging

import pypdf

log = logging.getLogger("indexer.extractor.pdf")

# Minimum extracted-character count below which we treat the digital
# path as "nothing usable" and fall through to OCR. A handful of stray
# whitespace/header tokens from a scanned PDF sometimes do come out of
# pypdf — without this floor we'd accept that as "success" and never
# OCR the actual page contents.
_MIN_DIGITAL_CHARS = 40


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,
    max_ocr_pages: int = 20,
    ocr_timeout_seconds: float | None = None,
    max_pdf_pages: int | None = None,
) -> tuple[str, str]:
    """Extract text from a PDF payload, falling back to OCR if needed.

    ``max_pdf_pages`` bounds the digital pypdf walk. The OCR cap above
    only bounds the rendered-image path; a 5 MB text-only PDF can
    legitimately carry thousands of pages, and at ~ms each that adds
    up to a meaningful queue stall.

    ``ocr_timeout_seconds`` is forwarded into the OCR fallback for the
    same reason as ``image.extract`` — see that module's docstring.
    """
    digital_text = _extract_digital(payload, max_pdf_pages=max_pdf_pages)
    if len(digital_text.strip()) >= _MIN_DIGITAL_CHARS:
        return digital_text, "pdf-digital"

    if not ocr_enabled:
        # Return whatever the digital path produced (may be empty);
        # caller will see "empty" status and persist the row so a
        # future OCR-enabled re-run picks it up.
        return digital_text, "pdf-digital"

    try:
        ocr_text = _extract_ocr(
            payload,
            max_ocr_pages=max_ocr_pages,
            ocr_timeout_seconds=ocr_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        # At this point the digital text layer was below the usable
        # threshold, so swallowing OCR failures would cache the
        # attachment as empty / partial and make the job look
        # successful. Let the dispatcher record a failed extraction
        # with the OCR error so operators can fix Poppler/Tesseract and
        # re-run extraction.
        log.warning("PDF OCR fallback failed: %s", exc)
        raise

    # Concatenate digital + OCR — digital is fast and may have caught a
    # few lines (cover page, embedded title) even when most of the doc
    # is scanned. Strip + dedup'ish via simple newline join is
    # sufficient for retrieval; the chunker normalises whitespace.
    if digital_text and ocr_text:
        return digital_text + "\n\n" + ocr_text, "pdf-ocr"
    if ocr_text:
        return ocr_text, "pdf-ocr"
    return digital_text, "pdf-digital"


def _extract_digital(payload: bytes, *, max_pdf_pages: int | None = None) -> str:
    """Pull the embedded text layer out of a PDF, page by page.

    ``max_pdf_pages`` (when set) caps page iteration so a pathological
    PDF with thousands of mostly-blank pages cannot stall the worker.
    """
    reader = pypdf.PdfReader(io.BytesIO(payload))
    pages: list[str] = []
    for index, page in enumerate(reader.pages):
        if max_pdf_pages is not None and index >= max_pdf_pages:
            log.info(
                "pdf-digital truncated at %d pages (max_pdf_pages cap)",
                max_pdf_pages,
            )
            break
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            # Per-page failures (broken cross-ref tables, cipher
            # entries pypdf chokes on) shouldn't abort the whole doc.
            log.debug("pypdf page extract failed: %s", exc)
            continue
        if text.strip():
            pages.append(text.strip())
    return "\n\n".join(pages)


def _extract_ocr(
    payload: bytes,
    *,
    max_ocr_pages: int,
    ocr_timeout_seconds: float | None = None,
) -> str:
    """Render each page to an image and OCR via Tesseract.

    Uses ``pdf2image`` (Poppler) for rendering and ``pytesseract`` for
    OCR. Both are imported lazily so a missing system dep surfaces here
    rather than at indexer start.

    ``output_folder=/tmp`` is set explicitly because the indexer
    container runs with ``read_only: true`` and ``/tmp`` mounted as
    tmpfs — pdf2image's default falls back to ``/var/tmp``, which is
    not writable on the hardened image.
    """
    import pytesseract
    from pdf2image import convert_from_bytes

    convert_kwargs: dict[str, object] = {
        "dpi": 200,
        "first_page": 1,
        # ``output_folder`` directs pdftoppm's spill to the tmpfs
        # mount; without it pdf2image picks an arbitrary tempdir which
        # may be read-only on hardened images.
        "output_folder": "/tmp",  # nosec B108 — tmpfs in the indexer container
    }
    if max_ocr_pages > 0:
        convert_kwargs["last_page"] = max_ocr_pages
    images = convert_from_bytes(payload, **convert_kwargs)  # type: ignore[arg-type]

    tesseract_kwargs: dict[str, float] = {}
    if ocr_timeout_seconds is not None and ocr_timeout_seconds > 0:
        # Apply the timeout per page rather than to the whole document
        # so a slow page does not eat the entire budget for the rest.
        tesseract_kwargs["timeout"] = float(ocr_timeout_seconds)

    pages: list[str] = []
    for image in images:
        text = pytesseract.image_to_string(image, **tesseract_kwargs)
        if text and text.strip():
            pages.append(text.strip())
    return "\n\n".join(pages)
