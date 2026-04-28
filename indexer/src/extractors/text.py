"""Plain-text / CSV / Markdown extractor.

These formats need no parsing — decode the bytes and the result IS the
extractable text. The only nuance is charset detection: TXT attachments
in the wild often have no MIME charset hint, and emails routinely carry
documents in legacy single-byte encodings (cp1252, latin-1) that decode
into mojibake under a naive ``utf-8`` attempt. We try utf-8 first, then
fall back to ``utf-8`` with ``errors="replace"`` so ill-formed bytes do
not abort extraction — replacement characters are still searchable.
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
        # Best-effort fallback: replace ill-formed bytes rather than
        # raising. The text remains searchable; mojibake characters
        # become a Unicode replacement code point in the chunk.
        return payload.decode("utf-8", errors="replace"), "text"
