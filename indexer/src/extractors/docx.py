"""DOCX (Word .docx) extractor.

Uses ``python-docx`` to walk the body, headers and footers in document
order. Tables are serialized cell-by-cell separated by single spaces so
a row's cells read together for retrieval, while preserving paragraph
structure elsewhere so the downstream chunker has paragraph boundaries
to pack on. Header / footer text is included because invoices and
contracts routinely place key fields (vendor name, dates, totals)
there, often inside layout tables.

Each ``<w:tc>`` element is read once. python-docx's ``row.cells``
repeats a cell for every grid column it spans and every row it merges
down, so a tiny file declaring a huge span multiplied extraction work
(#228). Nested tables are walked recursively (#226); lxml rejects XML
nested deeper than 256 elements, which keeps the recursion shallow.

Legacy ``.doc`` (binary Word, not OOXML) cannot be parsed by
``python-docx``; the dispatcher routes those to this module too but
the call will raise ``BadZipFile`` and surface as ``failed`` —
acceptable until / unless a real ``.doc`` extractor (e.g. ``antiword``,
``catdoc``) is added.
"""

from __future__ import annotations

import io
from collections.abc import Iterable

import docx as _docx
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001
    max_ocr_pages: int = 20,  # noqa: ARG001
    ocr_timeout_seconds: float | None = None,  # noqa: ARG001
    max_pdf_pages: int | None = None,  # noqa: ARG001
) -> tuple[str, str]:
    """Extract text from a DOCX payload. Returns (text, "docx")."""
    document = _docx.Document(io.BytesIO(payload))

    parts: list[str] = []
    # Body paragraphs keep the document's natural paragraph structure —
    # the chunker keys off blank-line gaps between paragraphs.
    parts.extend(_block_lines(document.iter_inner_content()))
    # Headers + footers (per section). A header linked to the previous
    # section repeats that section's content, so it is skipped.
    for section in document.sections:
        for part in (section.header, section.footer):
            if not part.is_linked_to_previous:
                parts.extend(_block_lines(part.iter_inner_content()))

    return "\n\n".join(parts), "docx"


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
        cell_texts: list[str] = []
        for tc in row._tr.tc_lst:
            # A vertically merged cell's continuation rows hold no content
            # of their own; ``row.cells`` would repeat the first row's.
            if tc.vMerge == "continue":
                continue
            text = " ".join(_block_lines(_Cell(tc, table).iter_inner_content()))
            if text:
                cell_texts.append(text)
        if cell_texts:
            lines.append(" ".join(cell_texts))
    return lines
