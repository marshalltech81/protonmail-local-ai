"""HTML attachment extractor.

Renders an attached ``.html`` document down to the plain text the
chunker expects. Reuses the same ``html2text`` configuration the email
body parser uses (``ignore_links``, ``ignore_images``, no body wrap)
so an HTML attachment and an HTML message body produce comparable text
for retrieval.
"""

from __future__ import annotations

from collections.abc import Callable

import html2text


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001
    max_ocr_pages: int = 20,  # noqa: ARG001
    ocr_timeout_seconds: float | None = None,  # noqa: ARG001
    max_pdf_pages: int | None = None,  # noqa: ARG001
    on_progress: Callable[[], None] | None = None,  # noqa: ARG001
) -> tuple[str, str]:
    """Decode HTML bytes and convert to plain text. Returns (text, "html")."""
    try:
        source = payload.decode("utf-8")
    except UnicodeDecodeError:
        source = payload.decode("utf-8", errors="replace")
    # A fresh converter per document: ``HTML2Text`` keeps parser state
    # between calls, so a shared one let an unclosed ``<style>`` blank
    # the next attachment.
    h2t = html2text.HTML2Text()
    h2t.ignore_links = True
    h2t.ignore_images = True
    h2t.body_width = 0
    return h2t.handle(source), "html"
