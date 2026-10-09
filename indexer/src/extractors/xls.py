"""Legacy Excel ``.xls`` extractor (#935).

A legacy ``.xls`` is an OLE2 compound file holding a BIFF workbook,
which openpyxl cannot read. The dispatcher routes an OLE2 payload
labelled ``.xls`` / ``application/vnd.ms-excel`` here; a
``.xls``-labelled payload that is not OLE2 (an OOXML file mislabelled
as ``.xls``) still goes to ``xlsx``.

The workbook is read by xlrd in the extractor child
(``extractor_child.py``, running the walk in ``xls_child.py``),
started through ``_runner.run_child``. xlrd's work on opening a
workbook is not bounded by the payload size, so the runner's launcher
caps the child's address space and CPU time before it starts, and the
runner adds a wall-clock timeout; see ``xls_child`` for the per-sheet
budgets, which match the xlsx extractor's. The child costs a launcher
and a child Python start-up, the extractors package and an xlrd import
per workbook, measured plainly in the image on the fixture workbook:
about 0.06 s and 39 MiB of address space (0.02 s and 21 MiB while the
child imported xlrd alone, before #1291).

A limit hit, a timeout or a crash raises a fixed-text error from
``_runner``; an xlrd error (or the child's ``MemoryError`` or
``RecursionError``) is reported by type name as ``ChildError``. Each is
recorded by type name as a ``failed`` row. Output past
``_MAX_OUTPUT_BYTES`` cannot come from a working child, whose text
budget is smaller, so it fails the workbook too
(``ChildOutputError``). The budgets the child reports as having cut
the text are logged here through ``warn_extractor_cap``, and the
degradation it recorded in the child is re-applied here
(``apply_child_degradation``, #1314).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from . import CHILD_DEGRADATION_KEYS, apply_child_degradation, warn_extractor_cap
from ._runner import run_child

log = logging.getLogger("indexer.extractor.xls")

# Address space the child may map (``RLIMIT_AS``), and CPU seconds it
# may use (``RLIMIT_CPU``). Plainly measured in the indexer image
# (CPython 3.14, xlrd 2.0.2, child limits as below):
#
# * a 20 MB workbook of 1,000,000 cells, half of them distinct strings:
#   1.2 s and 145 MB peak RSS;
# * a sheet whose 65,536 rows each hold a cell in the last of 256
#   columns, padded to the full sheet: 0.8 s and 188 MB, stopped at the
#   cell budget;
# * 1,100 one-cell sheets: 0.15 s, stopped at the sheet budget;
# * a 6 KB workbook whose shared-string table declares 2^31 - 1 strings
#   and re-reads one forever (a negative phonetic size): the address
#   space runs out after 17.5 s of CPU and 466 MB RSS, and the child
#   fails (it reports MemoryError since #1291; at 1 GiB it took 35 s).
#
# 512 MiB is about 2.5 times the largest benign peak with the 18 MiB
# of address space the extractors package adds (#1291); the CPU limit
# ends a loop that allocates nothing.
CHILD_MAX_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
CHILD_MAX_CPU_SECONDS = 30

# Wall-clock seconds the child may run, past its CPU limit so a
# CPU-bound child meets that limit first.
XLS_TIMEOUT_SECONDS = 45.0

# Bytes of the child's output read: its text budget (10,000,000
# characters) at UTF-8's worst case of four bytes each, plus the
# frames.
_MAX_OUTPUT_BYTES = 40 * 1024 * 1024 + 1024

# The cap names the child may report, and what each logs.
_CAP_MESSAGES = {
    "xls_sheets": "xls walk stopped at the sheet budget",
    "xls_expanded_cells": "xls walk stopped at the cell budget",
    "xls_text_chars": "xls walk stopped at the text budget",
}


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001
    max_ocr_pages: int = 20,  # noqa: ARG001
    ocr_timeout_seconds: float | None = None,  # noqa: ARG001
    max_pdf_pages: int | None = None,  # noqa: ARG001
    on_progress: Callable[[], None] | None = None,
) -> tuple[str, str]:
    """Extract text from a legacy ``.xls`` payload. Returns (text, "xls")."""
    result = run_child(
        "xls",
        payload,
        max_address_space_bytes=CHILD_MAX_ADDRESS_SPACE_BYTES,
        max_cpu_seconds=CHILD_MAX_CPU_SECONDS,
        timeout_seconds=XLS_TIMEOUT_SECONDS,
        max_output_bytes=_MAX_OUTPUT_BYTES,
        caps=frozenset(_CAP_MESSAGES),
        counts=CHILD_DEGRADATION_KEYS,
        on_progress=on_progress,
    )
    apply_child_degradation(log, "xls", result.counts)
    for cap in result.caps:
        warn_extractor_cap(log, cap, _CAP_MESSAGES[cap])
    return result.text, "xls"
