"""Child process of the legacy ``.xls`` extractor (#935).

Run by ``xls.extract`` as
``python -I xls_child.py <address space> <cpu seconds> <payload file>``; it
imports only the standard library and xlrd, so it never loads the
indexer (``-I`` also keeps this directory off ``sys.path``).

xlrd's ``open_workbook`` does work the payload cap does not bound
before any caller code runs: its shared-string loop trusts a declared
count and a signed skip length, so a few bytes can produce millions of
strings, and its OLE2 directory walk recurses with no cycle check. The
child therefore lowers its own address-space and CPU limits before it
imports xlrd, to the values the parent passes (``xls.py``
``CHILD_MAX_ADDRESS_SPACE_BYTES`` and ``CHILD_MAX_CPU_SECONDS``), and
the parent adds a wall-clock timeout.
A limit hit or any error ends the child with no text; the parent
records a ``failed`` row.

Within those limits the walk follows the xlsx extractor's budgets,
counted while reading:

* sheets: at most ``_MAX_SHEETS`` are loaded. xlrd loads one sheet at a
  time (``on_demand``) and each is unloaded after its walk, so at most
  one sheet's cells are held: BIFF8 caps a sheet at 65,536 rows by 256
  columns, and ``ragged_rows`` pads a row only to its own last cell.
* cells visited: ``_MAX_EXPANDED_CELLS`` across the workbook, padding
  included, plus ``_ROW_COST`` per row. The budget is checked before a
  sheet is loaded, so the cells built are at most the budget plus one
  sheet.
* characters copied: ``_MAX_TEXT_CHARS`` from cell values, sheet
  titles and separators, which also bounds the returned length.

Each budget that ended the walk is reported back by name for the parent
to log through ``warn_extractor_cap``.

Output on stdout: one line of comma-separated cap names, then the text
as UTF-8. Nothing is written to stderr on purpose: an error's message
can quote the workbook, so errors end the child with exit status 3 and
no text, and xlrd's own diagnostics go to a discarding log file.
"""

from __future__ import annotations

import resource
import sys
from typing import Any

# The xlsx extractor's budgets (see ``xlsx.py``), with the sheet count
# bounded as well: xlrd charges a sheet's fixed cost on load, before
# its rows are walked.
_MAX_SHEETS = 1024
_MAX_EXPANDED_CELLS = 5_000_000
_ROW_COST = 64
_MAX_TEXT_CHARS = 10_000_000
_HEADER_OVERHEAD = len("[Sheet: ]") + 2
_CELL_SEPARATORS = str.maketrans("\t\r\n", "   ")

# Exit status for any error in the walk (type and text withheld).
_EXIT_ERROR = 3


class _DiscardLog:
    """xlrd's ``logfile``: its diagnostics can quote the workbook."""

    def write(self, _text: str) -> int:
        return 0


def extract_text(payload: bytes) -> tuple[str, list[str]]:
    """The workbook's text and the names of the budgets that cut it."""
    import xlrd

    book = xlrd.open_workbook(
        file_contents=payload,
        on_demand=True,
        ragged_rows=True,
        formatting_info=False,
        logfile=_DiscardLog(),
    )
    try:
        return _walk(book)
    finally:
        book.release_resources()


def _walk(book: Any) -> tuple[str, list[str]]:
    parts: list[str] = []
    caps: list[str] = []
    expanded_cells = 0
    chars_left = _MAX_TEXT_CHARS
    cut = False
    for index in range(book.nsheets):
        if index >= _MAX_SHEETS:
            caps.append("xls_sheets")
            break
        sheet = book.sheet_by_index(index)
        try:
            if chars_left > 0:
                chars_left -= len(sheet.name) + _HEADER_OVERHEAD
            sheet_lines = [f"[Sheet: {sheet.name}]"] if chars_left > 0 else []
            for rowx in range(sheet.nrows):
                types = sheet.row_types(rowx)
                values = sheet.row_values(rowx)
                expanded_cells += len(values) + _ROW_COST
                if expanded_cells > _MAX_EXPANDED_CELLS:
                    break
                cells: list[str] = []
                blanks = 0
                for ctype, value in zip(types, values, strict=True):
                    raw = _cell_text(ctype, value, book.datemode)
                    if raw is None:
                        blanks += 1
                        continue
                    room = chars_left - (blanks + 1)
                    if room <= 0:
                        cut = True
                        break
                    text = raw[:room]
                    chars_left -= len(text)
                    cut = len(raw) > room
                    text = text.translate(_CELL_SEPARATORS).strip()
                    if text:
                        chars_left -= blanks + 1
                        cells.extend([""] * blanks)
                        cells.append(text)
                        blanks = 0
                    else:
                        blanks += 1
                    if cut:
                        break
                if cells:
                    sheet_lines.append("\t".join(cells))
                if cut:
                    break
            if len(sheet_lines) > 1:
                parts.append("\n".join(sheet_lines))
        finally:
            book.unload_sheet(index)
        if cut or expanded_cells > _MAX_EXPANDED_CELLS:
            break
    if expanded_cells > _MAX_EXPANDED_CELLS:
        caps.append("xls_expanded_cells")
    elif cut:
        caps.append("xls_text_chars")
    return "\n\n".join(parts), caps


def _cell_text(ctype: int, value: object, datemode: int) -> str | None:
    """A cell's text as the xlsx extractor would write it, or ``None``
    for an empty, blank or error cell."""
    import xlrd

    if ctype == xlrd.XL_CELL_TEXT:
        return str(value)
    if ctype == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if value else "FALSE"
    if ctype == xlrd.XL_CELL_DATE and isinstance(value, float):
        try:
            return str(xlrd.xldate.xldate_as_datetime(value, datemode))
        except xlrd.xldate.XLDateError, ValueError, OverflowError:
            return _number_text(value)
    if ctype == xlrd.XL_CELL_NUMBER and isinstance(value, float):
        return _number_text(value)
    return None


def _number_text(value: float) -> str:
    """``4471.0`` -> ``4471``; other floats as Python writes them."""
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def encode_output(text: str, caps: list[str]) -> bytes:
    """The child's stdout: the cap names on one line, then the text."""
    return (",".join(caps) + "\n").encode("ascii") + text.encode("utf-8", errors="replace")


def _limit_resources(address_space: int, cpu_seconds: int) -> None:  # pragma: no cover — child only
    """Lower this process's limits; raises when Linux refuses one.

    macOS refuses to lower ``RLIMIT_AS`` from its unlimited default
    (``ValueError``), and the image runs on Linux only, so there the
    address-space limit is skipped for local test runs; CI and the
    image apply it."""
    try:
        resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    except ValueError:
        if sys.platform == "linux":
            raise
    # Past the soft limit the kernel sends SIGXCPU; past the hard one,
    # SIGKILL.
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))


def main(argv: list[str]) -> int:  # pragma: no cover — runs only in the child
    _limit_resources(int(argv[1]), int(argv[2]))
    try:
        with open(argv[3], "rb") as handle:
            payload = handle.read()
        text, caps = extract_text(payload)
    except BaseException:  # noqa: BLE001 — any error: no text, type withheld
        return _EXIT_ERROR
    sys.stdout.buffer.write(encode_output(text, caps))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover — runs only in the child
    sys.exit(main(sys.argv))
