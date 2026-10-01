"""XLSX (Excel .xlsx) extractor.

Uses ``openpyxl`` in read-only mode to walk every cell of every sheet.
Each row becomes a single text line of tab-separated cell values; each
sheet is preceded by ``[Sheet: name]`` so a search can land on the
right sheet when several share columns. Formula cells return their
last-computed value (``data_only=True``) — for a forwarded-as-PDF /
forwarded-as-XLSX flow the user expects to see the same numbers.

Massive spreadsheets are bounded by the dispatcher's
``INDEXER_ATTACHMENT_MAX_BYTES`` cap, but byte size does not bound the
work, along two dimensions:

* cells visited: read-only ``iter_rows`` pads every row to the sheet's
  full width, empty cells included, so a few KB of sparse cells can
  declare a 16,384 x 1,048,576 grid. ``_MAX_EXPANDED_CELLS`` bounds
  the cells visited across the whole workbook and fails the extraction.
* characters copied: a shared string is stored once and referenced by
  any number of cells, so a few KB of workbook can expand into
  gigabytes of text (#294). ``_MAX_TEXT_CHARS`` bounds the characters
  read from cell values across the whole workbook; when it runs out the
  walk stops and the text collected so far is returned, as the
  dispatcher's own ``max_extracted_chars`` truncation would.
"""

from __future__ import annotations

import io

import openpyxl

# Cells visited, empty padding included, across every sheet. Far above
# any workbook that fits the attachment byte cap with real data; a few
# tenths of a second at worst.
_MAX_EXPANDED_CELLS = 20_000_000

# Characters read from cell values and sheet titles across the
# workbook, plus a separator for each, so it also bounds the returned
# length. Charged before stripping so blank values cost their full
# length. Five times the default ``INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS``
# (2,000,000), so the dispatcher's cap still decides the stored length
# unless an operator raises it past this.
_MAX_TEXT_CHARS = 10_000_000


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001
    max_ocr_pages: int = 20,  # noqa: ARG001
    ocr_timeout_seconds: float | None = None,  # noqa: ARG001
    max_pdf_pages: int | None = None,  # noqa: ARG001
) -> tuple[str, str]:
    """Extract text from an XLSX payload. Returns (text, "xlsx")."""
    workbook = openpyxl.load_workbook(
        io.BytesIO(payload),
        read_only=True,
        data_only=True,
    )
    try:
        return _serialize(workbook), "xlsx"
    finally:
        workbook.close()


def _serialize(workbook: openpyxl.Workbook) -> str:
    parts: list[str] = []
    expanded_cells = 0
    chars_left = _MAX_TEXT_CHARS
    for sheet in workbook.worksheets:
        header = f"[Sheet: {sheet.title}]"
        chars_left -= len(header) + 2  # and the blank line before it
        sheet_lines = [header]
        for row in sheet.iter_rows(values_only=True):
            expanded_cells += max(len(row), 1)
            if expanded_cells > _MAX_EXPANDED_CELLS:
                raise ValueError(f"workbook exceeds the {_MAX_EXPANDED_CELLS}-cell budget")
            cells: list[str] = []
            for value in row:
                if value is None or chars_left <= 0:
                    continue
                # Slice before stripping so no value costs more work
                # than the budget has left.
                text = str(value)[:chars_left]
                chars_left -= len(text) + 1  # and its tab or newline
                text = text.strip()
                if text:
                    cells.append(text)
            if cells:
                sheet_lines.append("\t".join(cells))
            if chars_left <= 0:
                break
        # Skip sheets with only the header line — empty sheet, nothing
        # the LLM can do with the title alone.
        if len(sheet_lines) > 1:
            parts.append("\n".join(sheet_lines))
        if chars_left <= 0:
            break
    return "\n\n".join(parts)
