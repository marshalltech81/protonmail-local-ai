"""DOCX (Word .docx) extractor.

Uses ``python-docx`` to walk the body, headers and footers in document
order. Headers and footers cover the default, first-page and even-page
parts each section displays (#299). Tables are serialized cell-by-cell separated by single spaces so
a row's cells read together for retrieval, while preserving paragraph
structure elsewhere so the downstream chunker has paragraph boundaries
to pack on. Header / footer text is included because invoices and
contracts routinely place key fields (vendor name, dates, totals)
there, often inside layout tables.

Each ``<w:tc>`` element is read once. python-docx's ``row.cells``
repeats a cell for every grid column it spans and every row it merges
down, so a tiny file declaring a huge span multiplied extraction work
(#228). Nested tables are walked recursively (#226), appending into one
flat list per top-level row so their text is copied once; lxml rejects
XML nested deeper than 256 elements, which keeps the recursion shallow.

Legacy ``.doc`` (binary Word, an OLE2 compound file, not OOXML) cannot
be parsed by ``python-docx``. The dispatcher sends ``.doc``-labelled
payloads here only when they are not OLE2 (an OOXML file mislabelled
as ``.doc``); a genuine one is recorded ``unsupported`` before this
module runs (#694). Word templates (``.dotx``) are not routed here:
``docx.Document`` refuses a package whose main part is the template
type.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable

import docx as _docx
from docx.document import Document as DocxDocument
from docx.oxml.table import CT_Row
from docx.section import _Footer, _Header
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001
    max_ocr_pages: int = 20,  # noqa: ARG001
    ocr_timeout_seconds: float | None = None,  # noqa: ARG001
    max_pdf_pages: int | None = None,  # noqa: ARG001
    on_progress: Callable[[], None] | None = None,  # noqa: ARG001
) -> tuple[str, str]:
    """Extract text from a DOCX payload. Returns (text, "docx")."""
    document = _docx.Document(io.BytesIO(payload))

    parts: list[str] = []
    # Body paragraphs keep the document's natural paragraph structure —
    # the chunker keys off blank-line gaps between paragraphs.
    parts.extend(_block_lines(document.iter_inner_content()))
    parts.extend(_header_footer_lines(document))
    return "\n\n".join(parts), "docx"


def _header_footer_lines(document: DocxDocument) -> list[str]:
    """Lines of every header and footer Word displays, each part once.

    A section has a default, a first-page and an even-page header and
    footer. The first-page pair shows when the section's
    ``different_first_page_header_footer`` is set, the even-page pair
    when the document's ``odd_and_even_pages_header_footer`` is. A part a
    section does not define (``is_linked_to_previous``) is inherited from
    the nearest earlier section that defines it. python-docx resolves that
    by recursing through every earlier section, so it is tracked here
    instead: the latest definition of each kind waits in ``pending``
    until a section shows it, and is read once. Each section's settings
    and references are visited once, so the work is linear in the XML.
    """
    even_pages = document.settings.odd_and_even_pages_header_footer
    pending: dict[str, _Header | _Footer] = {}
    lines: list[str] = []
    for section in document.sections:
        first_page = section.different_first_page_header_footer
        for kind, part, shown in (
            ("header", section.header, True),
            ("footer", section.footer, True),
            ("first_page_header", section.first_page_header, first_page),
            ("first_page_footer", section.first_page_footer, first_page),
            ("even_page_header", section.even_page_header, even_pages),
            ("even_page_footer", section.even_page_footer, even_pages),
        ):
            if not part.is_linked_to_previous:
                pending[kind] = part
            if shown and kind in pending:
                lines.extend(_block_lines(pending.pop(kind).iter_inner_content()))
    return lines


def _block_lines(blocks: Iterable[Paragraph | Table]) -> list[str]:
    """One line per non-empty paragraph and one per non-empty table row."""
    lines: list[str] = []
    for block in blocks:
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if text:
                lines.append(text)
        else:
            lines.extend(_table_lines(block))
    return lines


def _table_lines(table: Table) -> list[str]:
    """Serialize each row as space-joined cells so a header row like
    "Invoice #  Date  Amount" stays on one line and matches a search for
    any of those tokens. Empty cells are dropped from the row to avoid
    runs of double-spaces that would dilute FTS scoring.
    """
    lines: list[str] = []
    for row in table.rows:
        pieces: list[str] = []
        _row_pieces(row._tr, table, pieces)
        if pieces:
            lines.append(" ".join(pieces))
    return lines


def _row_pieces(tr: CT_Row, table: Table, pieces: list[str]) -> None:
    """Append the text of each cell in ``tr``, nested tables included.

    Everything under a top-level row is appended to one flat list and
    joined once, so text inside nested tables is copied once rather than
    once per level of nesting.
    """
    for tc in tr.tc_lst:
        # A vertically merged cell's continuation rows hold no content
        # of their own; ``row.cells`` would repeat the first row's.
        if tc.vMerge == "continue":
            continue
        for block in _Cell(tc, table).iter_inner_content():
            if isinstance(block, Paragraph):
                text = block.text.strip()
                if text:
                    pieces.append(text)
            else:
                for nested_row in block.rows:
                    _row_pieces(nested_row._tr, block, pieces)
