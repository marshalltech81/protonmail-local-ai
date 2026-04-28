"""Plain-text / CSV / Markdown extractor.

These formats need no parsing — decode the bytes and the result IS the
extractable text. The only nuance is charset detection: TXT attachments
in the wild often have no MIME charset hint and may arrive in legacy
single-byte encodings (cp1252, latin-1). We try utf-8 first, then
cp1252 (covers most Western-European single-byte payloads), then
finally utf-8 with ``errors="replace"`` so ill-formed bytes never abort
extraction — replacement characters are still searchable.
"""

from __future__ import annotations


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001 — accepted for dispatcher uniformity
    max_ocr_pages: int = 20,  # noqa: ARG001 — accepted for dispatcher uniformity
) -> tuple[str, str]:
    """Decode bytes as text. Returns (text, "text")."""
    try:
        return payload.decode("utf-8"), "text"
    except UnicodeDecodeError:
        pass
    try:
        # cp1252 is a superset of latin-1 (it fills in printable bytes
        # 0x80–0x9F that latin-1 leaves as control chars) and matches what
        # most Windows-origin invoices / receipts encode as. It always
        # succeeds because every byte maps to some code point — but if a
        # rare unmapped byte appears we still want the replacement fallback
        # rather than a hard raise.
        return payload.decode("cp1252"), "text"
    except UnicodeDecodeError:
        return payload.decode("utf-8", errors="replace"), "text"
