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

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("indexer.extractor")


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

    try:
        text, extractor_name = extractor_fn(
            payload,
            ocr_enabled=ocr_enabled,
            max_ocr_pages=max_ocr_pages,
        )
    except Exception as exc:  # noqa: BLE001 — we deliberately catch everything
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


_IMPORT_CACHE: dict[str, Callable[..., tuple[str, str]] | None] = {}


def _safe_import(module_name: str) -> Callable[..., tuple[str, str]] | None:
    """Lazy-import a per-format extractor, caching the result.

    Each extractor module exposes ``extract(payload, **opts) -> (text, name)``.
    A missing optional dependency (the module's ``import`` raising
    ``ImportError`` for one of *its* imports) is downgraded to ``None``
    here so callers see it as ``unsupported`` rather than a hard failure.
    Cached so repeated attachments of the same MIME type do not re-pay
    the import cost.

    The package root is ``src`` in both the indexer container
    (``python -m src.main``) and the test runner
    (``pythonpath = ["."]``), so a single canonical import path is
    sufficient. The actual ``ImportError`` is logged so a missing
    optional dependency surfaces with the offending module name rather
    than just "extractor X not importable".
    """
    if module_name in _IMPORT_CACHE:
        return _IMPORT_CACHE[module_name]
    try:
        module = __import__(f"src.extractors.{module_name}", fromlist=["extract"])
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
