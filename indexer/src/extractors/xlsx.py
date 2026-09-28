"""XLSX (Excel .xlsx) extractor.

Uses ``openpyxl`` in read-only mode to walk every cell of every sheet.
Each row becomes a single text line of tab-separated cell values; each
sheet is preceded by ``[Sheet: name]`` so a search can land on the
right sheet when several share columns. Formula cells return their
last-computed value (``data_only=True``) — for a forwarded-as-PDF /
forwarded-as-XLSX flow the user expects to see the same numbers.

Massive spreadsheets are bounded by the dispatcher's
``INDEXER_ATTACHMENT_MAX_BYTES`` cap, but byte size does not bound the
work: read-only ``iter_rows`` pads every row to the sheet's full width,
empty cells included, so a few KB of sparse cells can declare a
16,384 x 1,048,576 grid. ``_MAX_EXPANDED_CELLS`` bounds the cells
visited across the whole workbook.
"""

from __future__ import annotations

import io

import openpyxl

# Cells visited, empty padding included, across every sheet. Far above
# any workbook that fits the attachment byte cap with real data; a few
# tenths of a second at worst.
_MAX_EXPANDED_CELLS = 20_000_000


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

    parts: list[str] = []
    expanded_cells = 0
    for sheet in workbook.worksheets:
        sheet_lines = [f"[Sheet: {sheet.title}]"]
        for row in sheet.iter_rows(values_only=True):
            expanded_cells += max(len(row), 1)
            if expanded_cells > _MAX_EXPANDED_CELLS:
                workbook.close()
                raise ValueError(f"workbook exceeds the {_MAX_EXPANDED_CELLS}-cell budget")
            cells = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
            if cells:
                sheet_lines.append("\t".join(cells))
        # Skip sheets with only the header line — empty sheet, nothing
        # the LLM can do with the title alone.
        if len(sheet_lines) > 1:
            parts.append("\n".join(sheet_lines))

    workbook.close()
    return "\n\n".join(parts), "xlsx"
