"""
Attachment text extraction with per-MIME dispatch.

Public API: ``extract(content_type, filename, payload, ...)`` returns an
``ExtractionResult`` with one of the documented statuses. The dispatch
table maps a normalized content type (or, for ambiguous / missing
``Content-Type`` headers, a filename extension) to a per-format
extractor function. Per-format modules each expose a single
``extract(payload, **opts)`` callable that returns ``(text, extractor_name)``
on success or raises on failure.

The dispatcher itself is the only place that:

* enforces ``INDEXER_ATTACHMENT_MAX_BYTES`` (skip very large attachments
  to bound CPU and memory under a single email with a huge zip);
* honors ``INDEXER_OCR_ENABLED`` (a single switch turns off all OCR
  paths — image extraction and the scanned-PDF fallback — if the
  operator wants to disable Tesseract entirely);
* turns extractor exceptions into ``failed`` ``ExtractionResult`` rows
  rather than letting them bubble into the indexer queue and dead-
  letter the parent message.

Per-format extractors live in sibling modules and are intentionally
tiny — they exist so each MIME type's library can be lazy-imported
(see ``_safe_import``). A missing optional dependency for a rare
format does not break the indexer; it surfaces as
``status="unsupported"`` with an extractor-specific error message
operators can act on.
"""

from __future__ import annotations

import importlib
import logging
import os
import zipfile
from collections.abc import Callable
from dataclasses import dataclass

import defusedxml
from PIL import Image

# ``defuse_stdlib`` swaps the standard-library XML parsers (``xml.etree``,
# ``xml.sax``, ``xml.dom.*``, ``xml.parsers.expat``, ``xmlrpc.client``)
# for hardened equivalents that reject billion-laughs / external-entity /
# DTD-of-doom payloads. python-docx and openpyxl ultimately decode their
# zip members through libraries that may reach into stdlib XML; calling
# this once at module import time covers that path. The lxml-direct
# paths inside python-docx already disable entity resolution at parser
# construction; openpyxl uses ``defusedxml.ElementTree`` when available
# (which adding the dep here also enables).
defusedxml.defuse_stdlib()


# Process-wide cap for ALL PIL consumers — the standalone image
# extractor here AND any transitive PIL user (pypdf rendering embedded
# document images, openpyxl chart graphics, etc.). Set at module import
# time so the cap applies uniformly the first time any indexer code
# path reaches PIL.
#
# 30 Mpx is comfortably above any legitimate attachment image (a
# 300 DPI letter page is ~8 Mpx; a 600 DPI A3 is ~24 Mpx; a modern
# phone shot is ~12 Mpx) and well below the host-pressure threshold
# (decoding a 30 Mpx RGB image is ~90 MB of pixel buffer). PIL's
# default limit (~89 Mpx) is a HINT — it emits a
# ``DecompressionBombWarning`` and otherwise lets processing proceed —
# and was observed during a real indexer backfill to admit a 94 Mpx
# image embedded in a marketing PDF, contributing to OOM kills.
# Lowering the cap converts those into a hard rejection.
GLOBAL_MAX_IMAGE_PIXELS = 30_000_000
Image.MAX_IMAGE_PIXELS = GLOBAL_MAX_IMAGE_PIXELS

log = logging.getLogger("indexer.extractor")

# Cap the *uncompressed* size of any zip-based attachment (DOCX / XLSX).
# The dispatcher's ``max_bytes`` already bounds the on-disk payload, but
# a 1 MB workbook can decompress to multi-GB of XML (zip bomb). Reject
# anything whose declared uncompressed size exceeds this cap before
# python-docx / openpyxl get a chance to expand it. 200 MB covers any
# realistic spreadsheet while keeping memory bounded.
ZIP_MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome of one extraction attempt against an attachment payload.

    ``status`` is one of:

    * ``"success"`` — non-empty text extracted; ``text`` populated.
    * ``"empty"`` — extractor ran cleanly but the document had no text
      to extract (truly empty page, image of a blank surface, etc.).
    * ``"unsupported"`` — no extractor registered for this MIME type
      *or* the format's optional dependency is missing in this image.
    * ``"too_large"`` — payload exceeded ``max_bytes``.
    * ``"failed"`` — extractor raised; ``error`` captures a short repr
      of the exception. Indexer treats this as terminal for the
      attachment (won't keep retrying), but a future re-extraction
      sweep can re-run after a library upgrade.
    """

    status: str
    extractor: str | None
    text: str | None
    error: str | None


# Public statuses are exposed as constants so callers can compare without
# typo-prone string literals scattered across the codebase.
STATUS_SUCCESS = "success"
STATUS_EMPTY = "empty"
STATUS_UNSUPPORTED = "unsupported"
STATUS_TOO_LARGE = "too_large"
STATUS_FAILED = "failed"


# Maps normalized MIME -> per-format extractor module name (under
# ``indexer.extractors``). The module is imported lazily so a missing
# optional dependency doesn't break the indexer at startup.
_MIME_DISPATCH: dict[str, str] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "docx",  # legacy .doc handled best-effort by docx path
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xlsx",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/plain": "text",
    "text/csv": "text",
    "text/markdown": "text",
}

# Filename-extension fallback for cases where Content-Type is missing,
# is ``application/octet-stream``, or otherwise unhelpful — common for
# attachments forwarded from clients that strip MIME hints.
_EXT_DISPATCH: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "docx",
    ".xlsx": "xlsx",
    ".xls": "xlsx",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".txt": "text",
    ".csv": "text",
    ".md": "text",
    ".markdown": "text",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".tif": "image",
    ".tiff": "image",
    ".bmp": "image",
    ".webp": "image",
    ".gif": "image",
}

# Image MIME types are routed to the image extractor unless OCR is
# disabled (in which case we report ``unsupported`` so the cached row
# can be upgraded later if the operator flips the switch).
_IMAGE_MIME_PREFIX = "image/"


def extract(
    *,
    content_type: str,
    filename: str,
    payload: bytes,
    ocr_enabled: bool = True,
    max_bytes: int = 10_000_000,
    max_ocr_pages: int = 20,
    max_extracted_chars: int | None = None,
    ocr_timeout_seconds: float | None = None,
    max_pdf_pages: int | None = None,
) -> ExtractionResult:
    """Run text extraction for one attachment payload.

    Returns an ``ExtractionResult`` regardless of outcome — the
    dispatcher converts every exception inside a per-format extractor
    into a ``failed`` result so a single malformed attachment cannot
    dead-letter the parent message. The caller is expected to persist
    the result via ``Database.store_attachment_extraction``.

    ``max_extracted_chars`` (when supplied) caps the length of
    extracted text that is returned and persisted in the cache. A
    multi-hundred-page OCR'd PDF can otherwise produce megabytes of
    text and bloat the ``attachment_extractions`` table well past the
    payload's on-disk size. ``None`` means no cap.
    """
    if len(payload) > max_bytes:
        return ExtractionResult(
            status=STATUS_TOO_LARGE,
            extractor=None,
            text=None,
            error=f"payload {len(payload)} bytes exceeds cap {max_bytes}",
        )

    module_name, dispatch_via = _resolve_extractor(content_type, filename)

    # Image types are gated by ``ocr_enabled`` because the only sensible
    # extractor is Tesseract. Disabling OCR globally should cleanly
    # downgrade them to ``unsupported`` rather than failing per-call.
    if module_name == "image" and not ocr_enabled:
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=None,
            text=None,
            error="OCR disabled (INDEXER_OCR_ENABLED=false)",
        )

    if module_name is None:
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=None,
            text=None,
            error=f"no extractor for content_type={content_type!r} filename={filename!r}",
        )

    extractor_fn = _safe_import(module_name)
    if extractor_fn is None:
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=None,
            text=None,
            error=f"extractor module {module_name!r} not importable in this image",
        )

    # Zip-based formats (DOCX, XLSX) need a zip-bomb pre-check: the
    # ``max_bytes`` cap above only bounds the compressed payload; a
    # malicious workbook can declare 200× expansion in its central
    # directory. Reject before handing to lxml.
    if module_name in {"docx", "xlsx"}:
        zip_error = _validate_zip_payload(payload)
        if zip_error is not None:
            return ExtractionResult(
                status=STATUS_FAILED,
                extractor=module_name,
                text=None,
                error=zip_error,
            )

    try:
        text, extractor_name = extractor_fn(
            payload,
            ocr_enabled=ocr_enabled,
            max_ocr_pages=max_ocr_pages,
            ocr_timeout_seconds=ocr_timeout_seconds,
            max_pdf_pages=max_pdf_pages,
        )
    except MemoryError, RecursionError:
        # Resource-exhaustion errors are not "the extractor failed on
        # this payload" — they are "the runtime is in trouble". Letting
        # them bubble surfaces the host-level pressure (a zip-bomb
        # attachment that decompressed past the indexer container's
        # memory ceiling) instead of caching a misleading ``failed``
        # row that retries on every reappearance of the same payload.
        raise
    except Exception as exc:  # noqa: BLE001 — see comment below
        # Per-payload extractor errors (broken PDFs, malformed DOCX,
        # missing optional deps that slipped past _safe_import) become
        # ``failed`` rows so a single bad attachment cannot dead-letter
        # the parent message. ``MemoryError`` / ``RecursionError`` are
        # excluded above precisely because they are not per-payload.
        log.debug(
            "extractor %s failed on %s (dispatch_via=%s): %s",
            module_name,
            filename,
            dispatch_via,
            exc,
        )
        return ExtractionResult(
            status=STATUS_FAILED,
            extractor=module_name,
            text=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    if extractor_name == "pdf-ocr-disabled":
        return ExtractionResult(
            status=STATUS_UNSUPPORTED,
            extractor=None,
            text=None,
            error="OCR disabled (INDEXER_OCR_ENABLED=false)",
        )

    cleaned = (text or "").strip()
    if not cleaned:
        return ExtractionResult(
            status=STATUS_EMPTY,
            extractor=extractor_name,
            text=None,
            error=None,
        )
    if max_extracted_chars is not None and len(cleaned) > max_extracted_chars:
        log.info(
            "extractor %s output truncated from %d to %d chars (filename=%r)",
            extractor_name,
            len(cleaned),
            max_extracted_chars,
            filename,
        )
        cleaned = cleaned[:max_extracted_chars]
    return ExtractionResult(
        status=STATUS_SUCCESS,
        extractor=extractor_name,
        text=cleaned,
        error=None,
    )


def _resolve_extractor(content_type: str, filename: str) -> tuple[str | None, str]:
    """Return (module_name, dispatch_via) for an attachment.

    ``dispatch_via`` is just for diagnostics so a confusing dispatch
    can be traced back to "MIME header said X" vs. "filename extension
    was Y". Order:

    1. Direct MIME match against ``_MIME_DISPATCH``.
    2. ``image/*`` routes to the image extractor when OCR is enabled
       (gating happens upstream so the resolver itself can stay pure).
    3. Filename extension fallback against ``_EXT_DISPATCH``.
    """
    normalized_mime = (content_type or "").lower().split(";", 1)[0].strip()
    if normalized_mime in _MIME_DISPATCH:
        return _MIME_DISPATCH[normalized_mime], "mime"
    if normalized_mime.startswith(_IMAGE_MIME_PREFIX):
        return "image", "mime-image"
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _EXT_DISPATCH:
        return _EXT_DISPATCH[ext], "extension"
    return None, "none"


def _validate_zip_payload(payload: bytes) -> str | None:
    """Return an error string when ``payload`` looks like a zip bomb, else ``None``.

    Walks the central directory and rejects the archive when:

    * the file is not a valid zip (let the per-format extractor surface
      that as ``failed`` for a clearer error string),
    * any single member declares an uncompressed size above the cap, or
    * the sum of declared uncompressed sizes exceeds the cap.

    Reading ``ZipInfo.file_size`` does not decompress anything — it just
    parses the central directory header — so this check is cheap and
    runs before lxml gets involved.
    """
    try:
        import io

        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            total = 0
            for info in zf.infolist():
                if info.file_size > ZIP_MAX_UNCOMPRESSED_BYTES:
                    return (
                        f"zip member {info.filename!r} declares "
                        f"{info.file_size} uncompressed bytes (cap "
                        f"{ZIP_MAX_UNCOMPRESSED_BYTES})"
                    )
                total += info.file_size
                if total > ZIP_MAX_UNCOMPRESSED_BYTES:
                    return f"zip total uncompressed size exceeds cap {ZIP_MAX_UNCOMPRESSED_BYTES}"
    except zipfile.BadZipFile:
        # Not a zip — let the format-specific extractor produce a more
        # informative failure (e.g. python-docx's ``BadZipFile``).
        return None
    return None


_IMPORT_CACHE: dict[str, Callable[..., tuple[str, str]] | None] = {}


def _safe_import(module_name: str) -> Callable[..., tuple[str, str]] | None:
    """Lazy-import a per-format extractor, caching the result.

    Each extractor module exposes ``extract(payload, **opts) -> (text, name)``.
    A missing optional dependency (the module's ``import`` raising
    ``ImportError`` for one of *its* imports) is downgraded to ``None``
    here so callers see it as ``unsupported`` rather than a hard failure.
    Cached so repeated attachments of the same MIME type do not re-pay
    the import cost.

    Importing relative to the current package keeps this resilient to
    any future package rename. The actual ``ImportError`` is logged so
    a missing optional dependency surfaces with the offending module
    name rather than just "extractor X not importable".
    """
    if module_name in _IMPORT_CACHE:
        return _IMPORT_CACHE[module_name]
    try:
        module = importlib.import_module(f".{module_name}", package=__package__)
    except ImportError as exc:
        log.warning(
            "extractor %s unavailable (missing dependency %r): %s",
            module_name,
            exc.name or "<unknown>",
            exc,
        )
        _IMPORT_CACHE[module_name] = None
        return None
    fn = getattr(module, "extract", None)
    _IMPORT_CACHE[module_name] = fn
    return fn
