"""HTML attachment extractor.

Renders an attached ``.html`` document down to the plain text the
chunker expects. Reuses the same ``html2text`` configuration the email
body parser uses (``ignore_links``, ``ignore_images``, no body wrap)
so an HTML attachment and an HTML message body produce comparable text
for retrieval.
"""

from __future__ import annotations

import html2text

_h2t = html2text.HTML2Text()
_h2t.ignore_links = True
_h2t.ignore_images = True
_h2t.body_width = 0


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001
    max_ocr_pages: int = 20,  # noqa: ARG001
    ocr_timeout_seconds: float | None = None,  # noqa: ARG001
    max_pdf_pages: int | None = None,  # noqa: ARG001
) -> tuple[str, str]:
    """Decode HTML bytes and convert to plain text. Returns (text, "html")."""
    try:
        source = payload.decode("utf-8")
    except UnicodeDecodeError:
        source = payload.decode("utf-8", errors="replace")
    return _h2t.handle(source), "html"
