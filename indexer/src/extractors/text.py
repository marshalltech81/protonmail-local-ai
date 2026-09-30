"""Plain-text / CSV / Markdown extractor.

These formats need no parsing — decode the bytes and the result IS the
extractable text. The only nuance is charset detection: TXT attachments
in the wild often have no MIME charset hint and may arrive in legacy
single-byte encodings (cp1252, latin-1). A byte-order mark picks its
Unicode decoder, and BOM-less UTF-16 is recognised by its NUL bytes;
otherwise we try utf-8, then cp1252 (covers most Western-European
single-byte payloads), then finally utf-8 with ``errors="replace"`` so
ill-formed bytes never abort extraction — replacement characters are
still searchable.
"""

from __future__ import annotations

import codecs

# UTF-32 first: its little-endian BOM begins with UTF-16's.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
    (codecs.BOM_UTF8, "utf-8-sig"),
)


def _bomless_utf16(payload: bytes) -> str | None:
    """``utf-16-le`` / ``utf-16-be`` when NULs fill one byte of most code
    units and not the other, as they do for UTF-16 text in Latin
    scripts. UTF-8 text never looks like that, and without this check
    such UTF-16 decodes as valid UTF-8 with a NUL between letters."""
    units = len(payload) // 2
    if units == 0:
        return None
    odd_nuls = payload[1::2].count(0)
    even_nuls = payload[0::2].count(0)
    if odd_nuls > units // 2 and even_nuls <= units // 10:
        return "utf-16-le"
    if even_nuls > units // 2 and odd_nuls <= units // 10:
        return "utf-16-be"
    return None


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001 — accepted for dispatcher uniformity
    max_ocr_pages: int = 20,  # noqa: ARG001 — accepted for dispatcher uniformity
    ocr_timeout_seconds: float | None = None,  # noqa: ARG001
    max_pdf_pages: int | None = None,  # noqa: ARG001
) -> tuple[str, str]:
    """Decode bytes as text. Returns (text, "text")."""
    for bom, encoding in _BOMS:
        if payload.startswith(bom):
            return payload.decode(encoding, errors="replace"), "text"
    utf16 = _bomless_utf16(payload)
    if utf16 is not None:
        return payload.decode(utf16, errors="replace"), "text"
    try:
        return payload.decode("utf-8"), "text"
    except UnicodeDecodeError:
        pass
    try:
        # cp1252 is a superset of latin-1 (it fills in printable bytes
        # 0x80–0x9F that latin-1 leaves as control chars) and matches what
        # most Windows-origin invoices / receipts encode as. cp1252 has
        # five undefined byte values (0x81, 0x8D, 0x8F, 0x90, 0x9D) which
        # raise ``UnicodeDecodeError``; the replacement fallback below
        # handles those payloads without aborting extraction.
        return payload.decode("cp1252"), "text"
    except UnicodeDecodeError:
        return payload.decode("utf-8", errors="replace"), "text"
