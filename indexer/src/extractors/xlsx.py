"""XLSX (Excel .xlsx) extractor.

Uses ``openpyxl`` in read-only mode to walk every cell of every sheet.
Each row becomes a single text line of tab-separated cell values; each
cell keeps its column position, so an empty cell is an empty field
between tabs (#296) and tabs or line breaks inside a value become
spaces. Trailing empty cells and wholly empty rows are dropped. Each
sheet is preceded by ``[Sheet: name]`` so a search can land on the
right sheet when several share columns. Formula cells return their
last-computed value (``data_only=True``) — for a forwarded-as-PDF /
forwarded-as-XLSX flow the user expects to see the same numbers.

Massive spreadsheets are bounded by the dispatcher's
``INDEXER_ATTACHMENT_MAX_BYTES`` cap, but byte size does not bound the
work, along two dimensions:

* cells visited: the declared worksheet dimension is ignored, since a
  stale one would silently hide cells outside it (#305), so each parsed
  row is padded to its own last cell and every missing row between two
  parsed ones still costs a visit; the row and column numbers are the
  producer's claim. Each row, parsed or missing, also costs a fixed
  charge. ``_MAX_EXPANDED_CELLS`` bounds the cells visited plus those
  row charges across the whole workbook.
* characters copied: a shared string is stored once and referenced by
  any number of cells, so a few KB of workbook can expand into
  gigabytes of text (#294). ``_MAX_TEXT_CHARS`` bounds the characters
  read from cell values across the whole workbook.

When either budget runs out the walk stops and the text collected so
far is returned, as the dispatcher's own ``max_extracted_chars``
truncation would: a hostile workbook is truncated, and a long
legitimate one still yields its first rows rather than nothing.

Known limitation (#428): these budgets apply during the walk. Parts
openpyxl loads whole before it (the shared-string table,
``[Content_Types].xml``, ``xl/workbook.xml``) are bounded only by the
dispatcher's per-member zip cap, so a small, highly compressible
attachment can still cost seconds and hundreds of MB.
"""

from __future__ import annotations

import io

import openpyxl

# Cells visited, empty padding included, across every sheet, plus
# ``_ROW_COST`` per row. Plainly timed, openpyxl parses a row in about a
# microsecond however few cells it holds (about three with one value),
# against about 16 ns to pad a cell, so a row costs 64 cells; a missing
# row between two parsed ones, which cannot be told apart from a
# parsed row with no cells, is charged the same. The worst cases (one-
# cell rows, rows with no cells, missing rows, rows of styled empty
# cells) then stop in about a quarter of a second at most; a sheet
# longer than about 75,000 rows is truncated there.
_MAX_EXPANDED_CELLS = 5_000_000
_ROW_COST = 64

# Characters read from cell values and sheet titles across the
# workbook, plus each separator emitted, so it also bounds the returned
# length. Charged before stripping so blank values cost their full
# length; trailing empty cells emit nothing and cost only the cell
# budget. Five times the default ``INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS``
# (2,000,000), so the dispatcher's cap still decides the stored length
# unless an operator raises it past this.
_MAX_TEXT_CHARS = 10_000_000

# A tab or line break inside a value would read as a column or row
# boundary.
_CELL_SEPARATORS = str.maketrans("\t\r\n", "   ")


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
        # Read-only mode trusts the dimension record and stops at it.
        sheet.reset_dimensions()
        header = f"[Sheet: {sheet.title}]"
        chars_left -= len(header) + 2  # and the blank line before it
        sheet_lines = [header]
        for row in sheet.iter_rows(values_only=True):
            expanded_cells += len(row) + _ROW_COST
            if expanded_cells > _MAX_EXPANDED_CELLS:
                break
            cells: list[str] = []
            blanks = 0  # empty cells since the last value
            for value in row:
                if chars_left <= 0:
                    break
                if value is None:
                    blanks += 1
                    continue
                # The empty cells before a value become empty fields, so
                # their tabs are charged with it, as is its own tab or
                # newline; trailing empty cells are dropped and cost only
                # the cell budget. Reserve those separators, then slice
                # before stripping so no value costs more work than the
                # budget has left and one crossing it keeps its prefix.
                room = chars_left - (blanks + 1)
                if room <= 0:
                    chars_left = 0
                    break
                text = str(value)[:room]
                chars_left -= len(text)
                text = text.translate(_CELL_SEPARATORS).strip()
                if not text:
                    blanks += 1
                    continue
                chars_left -= blanks + 1
                cells.extend([""] * blanks)
                cells.append(text)
                blanks = 0
            if cells:
                sheet_lines.append("\t".join(cells))
            if chars_left <= 0:
                break
        # Skip sheets with only the header line — empty sheet, nothing
        # the LLM can do with the title alone.
        if len(sheet_lines) > 1:
            parts.append("\n".join(sheet_lines))
        if chars_left <= 0 or expanded_cells > _MAX_EXPANDED_CELLS:
            break
    return "\n\n".join(parts)
