"""HTML to text, run in the extractor child (#1294).

``html.convert`` runs it as
``python -I extractor_child.py html <length> [<length> ...] <payload file>``,
through the runner's launcher (``_launcher.py``), which has already
lowered the address-space and CPU limits ``html.py`` passes. The payload
file holds the documents one after another; each ``<length>`` is one
document's size in bytes, in order.

Each document is decoded as UTF-8 with replacement, converted with
``html.html_to_text`` (a fresh converter each) and stripped, as the
dispatcher and the parser strip every text. The texts are returned
in one text, each prefixed by its length in characters and a colon,
which ``html.split_texts`` reads back. They share one budget,
``html._MAX_TEXT_CHARS``: the text that crosses it is cut there, the
documents after it are not converted, and the cap ``html_text_chars``
is reported.

An error (lengths that do not match the payload, anything ``html2text``
raises, the child's own ``MemoryError`` or ``RecursionError`` at its
limit) is reported by type name only (``extractor_child``), since its
message can quote the document.
"""

from __future__ import annotations

from . import html

# The child's arguments, all checked here before any work: from one to
# ``_MAX_DOCUMENTS`` document lengths, each a byte count written in ASCII
# digits (0 and up), together exactly the payload's size. The parent
# sends one for an attachment and one per HTML body part, at most
# ``parser.MAX_BODY_TEXT_PARTS`` (200).
_MAX_DOCUMENTS = 200


def extract_text(payload: bytes, *lengths: str) -> tuple[str, list[str]]:
    """Convert the documents in ``payload`` (``lengths``: their sizes in
    bytes). Returns the framed texts and the names of the caps that cut
    them."""
    sizes = _document_sizes(lengths, len(payload))
    framed: list[str] = []
    left = html._MAX_TEXT_CHARS
    offset = 0
    for size in sizes:
        source = payload[offset : offset + size].decode("utf-8", errors="replace")
        offset += size
        # Stripped before the budget charges it, as both callers strip:
        # whitespace must not cut a text or starve the ones after it.
        text = html.html_to_text(source).strip()
        if len(text) > left:
            framed.append(f"{left}:{text[:left]}")
            return "".join(framed), [html.CAP_TEXT]
        framed.append(f"{len(text)}:{text}")
        left -= len(text)
    return "".join(framed), []


def _document_sizes(lengths: tuple[str, ...], payload_size: int) -> list[int]:
    """The document sizes ``lengths`` give, checked against the argument
    rules above; raises ``ValueError`` (fixed text) on any breach."""
    if not 1 <= len(lengths) <= _MAX_DOCUMENTS:
        raise ValueError("expected 1 to 200 document lengths")
    if not all(n.isascii() and n.isdigit() for n in lengths):
        raise ValueError("document lengths must be ASCII decimal numbers")
    sizes = [int(n) for n in lengths]
    if sum(sizes) != payload_size:
        raise ValueError("document lengths must add up to the payload")
    return sizes
