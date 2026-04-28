"""XLSX (Excel .xlsx) extractor.

Uses ``openpyxl`` in read-only mode to walk every cell of every sheet.
Each row becomes a single text line of tab-separated cell values; each
sheet is preceded by ``[Sheet: name]`` so a search can land on the
right sheet when several share columns. Formula cells return their
last-computed value (``data_only=True``) — for a forwarded-as-PDF /
forwarded-as-XLSX flow the user expects to see the same numbers.

Massive spreadsheets are bounded by the dispatcher's
``INDEXER_ATTACHMENT_MAX_BYTES`` cap, so this extractor itself does
not need a row limit.
"""

from __future__ import annotations

import io

import openpyxl


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001
    max_ocr_pages: int = 20,  # noqa: ARG001
) -> tuple[str, str]:
    """Extract text from an XLSX payload. Returns (text, "xlsx")."""
    workbook = openpyxl.load_workbook(
        io.BytesIO(payload),
        read_only=True,
        data_only=True,
    )

    parts: list[str] = []
    for sheet in workbook.worksheets:
        sheet_lines = [f"[Sheet: {sheet.title}]"]
        for row in sheet.iter_rows(values_only=True):
            cells = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
            if cells:
                sheet_lines.append("\t".join(cells))
        # Skip sheets with only the header line — empty sheet, nothing
        # the LLM can do with the title alone.
        if len(sheet_lines) > 1:
            parts.append("\n".join(sheet_lines))

    workbook.close()
    return "\n\n".join(parts), "xlsx"
