"""Independent unstripped converted-HTML evidence, outside retrieval (#1362)."""

from . import html
from .html_child import _document_sizes

CAP_TEXT = "full_body_html_chars"


def extract_text(payload: bytes, *lengths: str) -> tuple[str, list[str]]:
    sizes = _document_sizes(lengths, len(payload))
    texts: list[str] = []
    left = html._MAX_TEXT_CHARS
    offset = 0
    for size in sizes:
        source = payload[offset : offset + size].decode("utf-8", errors="replace")
        offset += size
        # Whitespace counts in this independent evidence budget. Legacy
        # html_child stripping, limits and outputs remain unchanged.
        text = html.html_to_text(source)
        if len(text) > left:
            texts.append(f"{left}:{text[:left]}")
            return "".join(texts), [CAP_TEXT]
        texts.append(f"{len(text)}:{text}")
        left -= len(text)
    return "".join(texts), []
