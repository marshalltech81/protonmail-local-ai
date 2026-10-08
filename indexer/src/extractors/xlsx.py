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
work, along three dimensions:

* XML nodes parsed: openpyxl builds every element of a row, and every
  attribute, before any code here sees the row, then collapses cells
  that repeat a coordinate, so a few KB of workbook could cost
  gigabytes and seconds while it was charged one cell (#432). It parses
  each worksheet when the workbook loads as well as during the walk.
  Before openpyxl opens the workbook, ``_bound_worksheets`` streams
  every worksheet it will parse through expat, charges each element
  and each attribute one node, and cuts the worksheet before the row
  that crosses ``_MAX_SHEET_NODES`` across the workbook or
  ``_MAX_ROW_NODES`` in one row, or before a start tag that runs past
  ``_MAX_TAG_BYTES``, since expat builds a tag's attributes before they
  can be charged. A cut ends that worksheet; a worksheet reached with
  no budget left is read as empty.
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

When a budget runs out the text collected so far is returned, as the
dispatcher's own ``max_extracted_chars`` truncation would: a hostile
workbook is truncated, and a long legitimate one still yields its
first rows rather than nothing. Each budget that cut a workbook logs a
rate-limited WARNING naming it, once per extraction, and is counted in
the attachments aggregate's ``extractor_caps`` (#903).

Parts openpyxl loads whole rather than streams (the shared-string
table, ``[Content_Types].xml``, the workbook, styles and the rest; see
``_MAX_EAGER_BYTES``) are a fourth dimension, bounded before any of
them is read (#428): ``_check_eager_parts`` finds each the way openpyxl
does and charges its declared size, and a workbook over a cap fails
with ``XlsxEagerPartBudgetError``, which the dispatcher records as
``unsupported`` (#931). No text is kept then: a part loaded
whole cannot be cut the way a worksheet is. The bytes of a worksheet,
as opposed to its nodes, are bounded by the dispatcher's zip cap.

All of this trusts the member sizes the ZIP central directory
declares, and a member that understates its size is still decompressed
whole when it is read. So the whole extraction, budgets included, runs
in a child process under an address-space and a CPU limit
(``ooxml``, #1040): ``extract`` starts it, and ``extract_text`` is what
runs in it. The child reports the budgets that cut the text by name,
and this module logs them.
"""

from __future__ import annotations

import io
import logging
import shutil
import zipfile
from collections.abc import Callable
from typing import IO
from xml.parsers import expat

import openpyxl
from defusedxml import EntitiesForbidden, ExternalReferenceForbidden
from openpyxl.drawing.spreadsheet_drawing import SpreadsheetDrawing
from openpyxl.packaging.manifest import Manifest
from openpyxl.packaging.relationship import RelationshipList, get_dependents, get_rels_path
from openpyxl.packaging.workbook import WorkbookPackage
from openpyxl.reader.excel import ExcelReader
from openpyxl.xml.constants import (
    ARC_CONTENT_TYPES,
    ARC_CORE,
    ARC_CUSTOM,
    ARC_STYLE,
    ARC_THEME,
    ARC_WORKBOOK,
    IMAGE_NS,
    SHARED_STRINGS,
    XLSM,
    XLSX,
    XLTM,
    XLTX,
)
from openpyxl.xml.functions import fromstring

from . import warn_extractor_cap
from .ooxml import run_child

log = logging.getLogger("indexer.extractor.xlsx")

# Address space (``RLIMIT_AS``), CPU seconds (``RLIMIT_CPU``) and
# wall-clock seconds the extraction's child process may use (#1040),
# past the CPU limit so a CPU-bound child meets that first. Plainly
# measured in the indexer image, child peak RSS and time:
#
# * a one-cell workbook: 44 MB and 0.08 s, nearly all of it starting
#   the child and importing openpyxl (0.2 s before the image shipped
#   compiled bytecode, #1230; the cases below were measured then);
# * 1,000,000 cells, half of them distinct strings (5 MB, stopped at
#   the cell budget): 91 MB and 5.4 s;
# * 119 rows of 60,000 duplicate cells (stopped at the node budget):
#   115 MB and 3.9 s;
# * a shared-string table of empty strings and a stylesheet of empty
#   fonts, together just under the eager-part budget: 508 MB and
#   10.3 s;
# * a 0.5 MB workbook whose stylesheet declares a few KB but
#   decompresses to 512 MiB (#1040): 1,068 MB and 0.5 s.
#
# 1 GiB is about twice the eager-part worst case, and the last case
# fails under it as ``MemoryError``. 30 CPU seconds is about three times
# the slowest case.
CHILD_MAX_ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024
CHILD_MAX_CPU_SECONDS = 30
CHILD_TIMEOUT_SECONDS = 45.0

# The budgets the child may report as having cut the text, and what
# each logs.
_CAP_MESSAGES = {
    "xlsx_sheet_nodes": "xlsx worksheet XML cut before openpyxl parses it",
    "xlsx_row_nodes": "xlsx worksheet XML cut before openpyxl parses it",
    "xlsx_tag_bytes": "xlsx worksheet XML cut before openpyxl parses it",
    "xlsx_expanded_cells": "xlsx walk stopped at the cell budget",
    "xlsx_text_chars": "xlsx walk stopped at the text budget",
}

# XML nodes (elements plus their attributes) in the worksheet parts
# openpyxl will parse, across the workbook, and in any one row (#432).
# Plainly timed, openpyxl spends one to two microseconds on a node,
# whatever its kind (a cell, a row, an attribute, an element it does
# not know), counting the parse at load and the walk, so the worst
# shapes stop in about 5 to 10 s and a few hundred MB; the pre-pass adds
# about a fifth to a normal sheet. A dense sheet has four or five nodes
# a cell, so about a million cells fit, past what the dispatcher's
# default 2,000,000-character cap keeps. The row budget allows each of
# Excel's 16,384 columns eight nodes and bounds what one row costs in
# memory, since openpyxl builds a row whole.
_MAX_SHEET_NODES = 5_000_000
_MAX_ROW_NODES = 131_072

# Parts openpyxl reads whole rather than streams (#428): the manifest,
# shared strings, workbook and the relationships it resolves, styles,
# theme, core and custom properties, each worksheet's relationships,
# and each chartsheet with its drawings, charts and images. A part over
# ``_MAX_EAGER_PART_BYTES``, or reads that together cross
# ``_MAX_EAGER_BYTES`` or ``_MAX_EAGER_READS``, fail the workbook before
# openpyxl opens it. Plainly timed, the costliest shapes (empty shared
# strings, empty fonts in the styles) cost about 0.4 s and 20 to 35 MB
# per MB of part, so two of them filling the aggregate take about 7 s
# and 300 MB; a legitimate shared-string table costs about a fifth of
# that. Each sheet costs about 0.1 ms besides its bytes, and a
# chartsheet about six reads, so a workbook naming thousands of sheets
# stops at the read budget in about half a second. External links,
# which openpyxl would also read whole, are not loaded at all
# (``keep_links=False``): their cached values are not extracted.
_MAX_EAGER_PART_BYTES = 8 * 1024 * 1024
_MAX_EAGER_BYTES = 16 * 1024 * 1024
_MAX_EAGER_READS = 4096

# Bytes of worksheet XML fed to the pre-pass parser per call.
_SCAN_CHUNK = 64 * 1024

# Bytes the pre-pass feeds expat with no event: expat holds a start tag
# whole and builds every attribute before the tag's handler can charge
# them, so a tag with millions of attributes would cost gigabytes first.
# Text and end tags are events, and a cell Excel writes is far smaller.
_MAX_TAG_BYTES = 1024 * 1024

# Encodings in which an ASCII end tag can be appended to a cut prefix.
_ASCII_COMPATIBLE = frozenset({"utf-8", "utf8", "us-ascii", "ascii", "iso-8859-1", "latin-1"})

# What a worksheet past the node budget is replaced with: a worksheet
# with no rows.
_EMPTY_WORKSHEET = b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'

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

# A sheet header's characters besides its title: ``[Sheet: ]`` and the
# blank line before it.
_HEADER_OVERHEAD = len("[Sheet: ]") + 2

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
    on_progress: Callable[[], None] | None = None,
) -> tuple[str, str]:
    """Extract text from an XLSX payload in the child process (``ooxml``,
    #1040). Returns (text, "xlsx")."""
    text, caps = run_child(
        "xlsx",
        payload,
        max_address_space_bytes=CHILD_MAX_ADDRESS_SPACE_BYTES,
        max_cpu_seconds=CHILD_MAX_CPU_SECONDS,
        timeout_seconds=CHILD_TIMEOUT_SECONDS,
        caps=frozenset(_CAP_MESSAGES),
        permanent={"XlsxEagerPartBudgetError": XlsxEagerPartBudgetError},
        on_progress=on_progress,
    )
    for cap in caps:
        warn_extractor_cap(log, cap, _CAP_MESSAGES[cap])
    return text, "xlsx"


def extract_text(payload: bytes) -> tuple[str, list[str]]:
    """The workbook's text and the names of the budgets that cut it.
    Runs in the child process (``extractor_child``)."""
    caps: list[str] = []
    _check_eager_parts(payload)
    workbook = openpyxl.load_workbook(
        _bound_worksheets(payload, caps),
        read_only=True,
        data_only=True,
        keep_links=False,
    )
    try:
        return _serialize(workbook, caps), caps
    finally:
        workbook.close()


class XlsxEagerPartBudgetError(Exception):
    """A part openpyxl loads whole is over its cap, or the parts it loads
    whole are over the workbook's budget (#428). Fixed text: the
    dispatcher records it as ``unsupported`` with its own fixed text,
    since the same bytes always trip the same budget (#931)."""

    def __init__(self) -> None:
        super().__init__("xlsx part openpyxl loads whole is over budget")


class _EagerBudget:
    """Charges each read openpyxl makes of a part it loads whole, by the
    part's declared uncompressed size, before anything reads it.

    zipfile never returns more than a member's declared size (it stops
    there and checks the CRC), so the declared size bounds what openpyxl
    parses. It decompresses the member's whole stream first, though, so
    a member that understates its size is expanded anyway: the child
    process's address-space limit bounds that (#1040). A name stored
    twice is read from its last entry, which is what ``getinfo``
    returns. A part read several times is charged each time.
    """

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        self.names = set(archive.namelist())
        self.bytes_left = _MAX_EAGER_BYTES
        self.reads_left = _MAX_EAGER_READS

    def count(self) -> None:
        """Charge one member read with no byte charge."""
        self.reads_left -= 1
        if self.reads_left < 0:
            raise XlsxEagerPartBudgetError

    def charge(self, name: str) -> bool:
        """Charge one read of ``name``; ``False`` when it is absent, so
        openpyxl's own read of it fails or is skipped."""
        if name not in self.names:
            return False
        self.count()
        size = self.archive.getinfo(name).file_size
        self.bytes_left -= size
        if size > _MAX_EAGER_PART_BYTES or self.bytes_left < 0:
            raise XlsxEagerPartBudgetError
        return True

    def read_rels(self, part: str) -> RelationshipList | None:
        """Charge and read the relationships of ``part``, as openpyxl's
        ``get_dependents`` resolves them, or ``None`` when it has none."""
        rels_path = get_rels_path(part)
        if not self.charge(rels_path):
            return None
        return get_dependents(self.archive, rels_path)


def _check_eager_parts(payload: bytes) -> None:
    """Raise ``XlsxEagerPartBudgetError`` when the parts
    ``load_workbook(read_only=True, keep_links=False)`` reads whole cross
    ``_MAX_EAGER_PART_BYTES`` each or ``_MAX_EAGER_BYTES`` /
    ``_MAX_EAGER_READS`` together (#428).

    Mirrors openpyxl 3.1.5's ``ExcelReader.read`` and finds each part the
    way it does, through the manifest and relationships, and charges a
    part before reading it. A part openpyxl would fail on (missing,
    malformed) stops the walk and is left for openpyxl to report.
    Worksheets are streamed and bounded by ``_bound_worksheets``; each
    is charged one read here, and its relationships, read whole, bytes.
    ``_bound_worksheets`` reads the manifest, workbook and workbook
    relationships once more; the budgets were sized from timings that
    include it.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile:
        return  # openpyxl reports it
    with archive:
        budget = _EagerBudget(archive)
        # read_manifest.
        if not budget.charge(ARC_CONTENT_TYPES):
            return
        manifest = Manifest.from_tree(fromstring(archive.read(ARC_CONTENT_TYPES)))
        # read_strings: found by content type, under any name.
        strings = manifest.find(SHARED_STRINGS)
        if strings is not None:
            budget.charge(strings.PartName[1:])
        # read_properties, read_custom, read_theme, apply_stylesheet.
        for name in (ARC_CORE, ARC_CUSTOM, ARC_THEME, ARC_STYLE):
            budget.charge(name)
        # read_workbook. External links are not read (keep_links=False).
        workbook_part = _workbook_part_name(manifest)
        if workbook_part is None or not budget.charge(workbook_part):
            return
        package = WorkbookPackage.from_tree(fromstring(archive.read(workbook_part)))
        workbook_rels = budget.read_rels(workbook_part)
        if workbook_rels is None:
            return
        rels_by_id = workbook_rels.to_dict()
        # read_worksheets, as ``WorkbookParser.find_sheets`` yields them.
        for sheet in package.sheets:
            if not sheet.id:
                continue
            rel = rels_by_id.get(sheet.id)
            if rel is None:
                return
            if rel.target not in budget.names:
                continue
            if "chartsheet" in rel.Type:
                _charge_chartsheet(budget, rel.target)
            else:
                budget.count()
                budget.read_rels(rel.target)


def _workbook_part_name(manifest: Manifest) -> str | None:
    """The workbook part's name, as openpyxl's ``_find_workbook_part``
    finds it, or ``None`` when it finds none."""
    for content_type in (XLTM, XLTX, XLSM, XLSX):
        part = manifest.find(content_type)
        if part:
            return part.PartName[1:]
    defaults = {default.ContentType for default in manifest.Default}
    if defaults & {XLTM, XLTX, XLSM, XLSX}:
        return ARC_WORKBOOK
    return None


def _charge_chartsheet(budget: _EagerBudget, target: str) -> None:
    """Charge what ``ExcelReader.read_chartsheet`` reads: the
    chartsheet, its relationships, and for each drawing they name what
    ``find_images`` reads, once per reference."""
    budget.charge(target)
    rels = budget.read_rels(target)
    if rels is None:
        return  # openpyxl fails on the missing relationships
    for drawing_rel in rels.find(SpreadsheetDrawing._rel_type):
        drawing_path = drawing_rel.target
        if not budget.charge(drawing_path):
            return
        try:
            drawing = SpreadsheetDrawing.from_tree(fromstring(budget.archive.read(drawing_path)))
        except TypeError:
            continue  # find_images reads nothing more from it
        deps = budget.read_rels(drawing_path)
        if deps is None:
            return
        # ``find_images`` reads a chart and its relationships per chart
        # reference, and an image per picture reference.
        for chart_rel in drawing._chart_rels:
            chart_path = deps.get(chart_rel.id).target
            if budget.charge(chart_path):
                budget.read_rels(chart_path)
        for blip in drawing._blip_rels:
            dep = deps.get(blip.embed)
            if dep.Type == IMAGE_NS:
                budget.charge(dep.target)


def _bound_worksheets(payload: bytes, reported: list[str]) -> io.BytesIO:
    """Return the workbook with every worksheet cut before the row that
    crosses a node budget, or the workbook unchanged when none does.
    Appends the budgets that cut a worksheet to ``reported``, once each.

    The worksheets are found the way openpyxl finds them, through its
    public ``ExcelReader``, and scanned in the order it walks them, one
    budget across them all. A worksheet named twice is charged twice,
    as openpyxl parses it twice.
    """
    reader = ExcelReader(io.BytesIO(payload), read_only=True, data_only=True, keep_links=False)
    # Per cut worksheet: how many of its leading bytes to keep, and what
    # follows them.
    cuts: dict[str, tuple[int, bytes]] = {}
    # The budgets that cut a worksheet, reported once each below.
    caps: set[str] = set()
    try:
        reader.read_manifest()
        reader.read_workbook()
        left = _MAX_SHEET_NODES
        # As ``ExcelReader.read_worksheets`` selects them.
        for _sheet, rel in reader.parser.find_sheets():
            target = rel.target
            if target not in reader.valid_files or "chartsheet" in rel.Type:
                continue
            with reader.archive.open(target) as member:
                if target in cuts:
                    # Charge what openpyxl will parse: the cut worksheet.
                    keep, tail = cuts[target]
                    cut_member = io.BytesIO()
                    _copy_prefix(member, cut_member, keep)
                    cut_member.write(tail)
                    cut_member.seek(0)
                    left, cut = _scan_worksheet(cut_member, left, caps)
                else:
                    left, cut = _scan_worksheet(member, left, caps)
            if cut is not None:
                # A cut of the cut worksheet falls inside its kept bytes.
                cuts[target] = cut
    finally:
        reader.archive.close()
    reported.extend(sorted(caps))
    if not cuts:
        return io.BytesIO(payload)
    # Rewrite the archive with the cut worksheets. A name stored twice
    # is read from its last entry, by openpyxl as here, so one copy is
    # kept. Members are stored uncompressed: the dispatcher caps their
    # total size, and recompressing would cost more than it saves.
    rebuilt = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(payload)) as original,
        zipfile.ZipFile(rebuilt, "w") as out,
    ):
        for name in dict.fromkeys(original.namelist()):
            with out.open(name, "w") as member, original.open(name) as data:
                if name in cuts:
                    keep, tail = cuts[name]
                    _copy_prefix(data, member, keep)
                    member.write(tail)
                else:
                    shutil.copyfileobj(data, member)
    rebuilt.seek(0)
    return rebuilt


class _Cut(Exception):
    """Raised by the scan at the node that crosses a budget."""


class _WorksheetScan:
    """Expat handlers that charge each element, and each of its
    attributes, one node.

    A depth-3 element (a ``<row>`` inside ``<sheetData>``, or any other
    grandchild of the root) and everything inside it is one unit: the
    scan cuts at the start of the unit holding the node that crosses a
    budget, so openpyxl sees whole rows, or at the start of a shallower
    element that crosses it.
    """

    def __init__(self, left: int) -> None:
        self.parser = expat.ParserCreate()
        self.parser.StartElementHandler = self._start
        self.parser.EndElementHandler = self._end
        self.parser.CharacterDataHandler = self._text
        self.parser.XmlDeclHandler = self._declaration
        # As defusedxml configures the parser openpyxl reads with.
        self.parser.EntityDeclHandler = self._forbid_entity
        self.parser.UnparsedEntityDeclHandler = self._forbid_entity
        self.parser.ExternalEntityRefHandler = self._forbid_external
        self.left = left
        self.encoding: str | None = None
        self.depth = 0
        self.outer: list[str] = []  # names of the open elements above depth 3
        self.unit_start = 0  # byte offset of the open unit
        self.unit_depth = 1
        self.last_event = 0  # byte offset of the latest event
        self.unit_nodes = 0
        self.left_before_unit = left
        self.cut_at: int | None = None
        self.cap: str | None = None  # the budget the cut was for

    def _start(self, name: str, attributes: dict[str, str]) -> None:
        self.depth += 1
        self.last_event = self.parser.CurrentByteIndex
        if self.depth <= 3:
            self.unit_start = self.last_event
            self.unit_depth = self.depth
            self.unit_nodes = 0
            self.left_before_unit = self.left
        if self.depth <= 2:
            self.outer.append(name)
        cost = 1 + len(attributes)
        self.unit_nodes += cost
        self.left -= cost
        if self.left < 0 or self.unit_nodes > _MAX_ROW_NODES:
            self.cap = "xlsx_row_nodes" if self.unit_nodes > _MAX_ROW_NODES else "xlsx_sheet_nodes"
            self.cut()
            raise _Cut

    def cut(self) -> None:
        """Cut at the start of the latest unit: the open one, or when
        none is open the one before, a row more than it needs."""
        self.cut_at = self.unit_start

    def _text(self, _data: str) -> None:
        self.last_event = self.parser.CurrentByteIndex

    def _end(self, _name: str) -> None:
        self.last_event = self.parser.CurrentByteIndex
        if self.depth <= 2:
            self.outer.pop()
        self.depth -= 1

    def _declaration(self, _version: str, encoding: str | None, _standalone: int) -> None:
        self.encoding = encoding

    @staticmethod
    def _forbid_entity(*_args: object) -> None:
        raise EntitiesForbidden(None, None, None, None, None, None)

    @staticmethod
    def _forbid_external(
        _context: str, _base: str | None, _system_id: str | None, _public_id: str | None
    ) -> int:
        raise ExternalReferenceForbidden(None, None, None, None)


def _copy_prefix(source: IO[bytes], target: IO[bytes], size: int) -> None:
    """Copy the first ``size`` bytes of ``source`` to ``target``."""
    while size > 0 and (chunk := source.read(min(size, _SCAN_CHUNK))):
        target.write(chunk)
        size -= len(chunk)


def _scan_worksheet(
    source: IO[bytes], left: int, caps: set[str]
) -> tuple[int, tuple[int, bytes] | None]:
    """Charge one worksheet's nodes against ``left``; adds the budget a
    cut was for to ``caps``.

    Returns the budget left and, when the worksheet crosses a budget,
    its cut: the bytes to keep and the end tags of the elements still
    open there, or no bytes and a worksheet with no rows when the root
    itself crosses or an ASCII end tag cannot be appended in the
    worksheet's encoding.
    """
    scan = _WorksheetScan(left)
    head = b""
    fed = 0
    try:
        while chunk := source.read(_SCAN_CHUNK):
            head = head or chunk[:4]
            scan.parser.Parse(chunk, False)
            fed += len(chunk)
            if fed - scan.last_event > _MAX_TAG_BYTES:
                scan.cap = "xlsx_tag_bytes"
                scan.cut()
                break
        else:
            scan.parser.Parse(b"", True)
    except _Cut:
        pass
    except expat.ExpatError:
        # openpyxl's parser is expat too and stops at the same error,
        # so it parses no more than was charged.
        pass
    if scan.cut_at is None:
        return scan.left, None
    if scan.cap is not None:
        caps.add(scan.cap)
    encoding = (scan.encoding or "utf-8").lower()
    ascii_compatible = (
        encoding in _ASCII_COMPATIBLE
        and not head.startswith((b"\xff\xfe", b"\xfe\xff"))
        and b"\x00" not in head
    )
    if scan.unit_depth == 1 or not ascii_compatible:
        return scan.left_before_unit, (0, _EMPTY_WORKSHEET)
    closers = "".join(f"</{name}>" for name in reversed(scan.outer[: scan.unit_depth - 1]))
    return scan.left_before_unit, (scan.cut_at, closers.encode(encoding))


def _serialize(workbook: openpyxl.Workbook, reported: list[str]) -> str:
    """The workbook's text; appends the budget that ended the walk, if
    one did, to ``reported``."""
    parts: list[str] = []
    expanded_cells = 0
    chars_left = _MAX_TEXT_CHARS
    # Whether the text budget cut or left unread a value (#903). A budget
    # the last value spends exactly cuts nothing, so once it is spent the
    # walk reads on, keeping nothing and charging the cell budget as
    # before, until it meets a value (a cut) or the end (review round 1
    # on #917).
    cut = False
    for sheet in workbook.worksheets:
        # Read-only mode trusts the dimension record and stops at it.
        sheet.reset_dimensions()
        # Charge the header, and the blank line before it, before
        # copying the title (#435). A header that leaves no budget for a
        # value keeps nothing of its sheet, so its title of any length
        # is not copied; its rows are only looked at for a value.
        if chars_left > 0:
            chars_left -= len(sheet.title) + _HEADER_OVERHEAD
        sheet_lines = [f"[Sheet: {sheet.title}]"] if chars_left > 0 else []
        for row in sheet.iter_rows(values_only=True):
            expanded_cells += len(row) + _ROW_COST
            if expanded_cells > _MAX_EXPANDED_CELLS:
                break
            cells: list[str] = []
            blanks = 0  # empty cells since the last value
            for value in row:
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
                    cut = True
                    break
                raw = str(value)
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
        # Skip sheets with only the header line — empty sheet, nothing
        # the LLM can do with the title alone.
        if len(sheet_lines) > 1:
            parts.append("\n".join(sheet_lines))
        if cut or expanded_cells > _MAX_EXPANDED_CELLS:
            break
    # A budget that ended the walk cut the text (#903).
    if expanded_cells > _MAX_EXPANDED_CELLS:
        reported.append("xlsx_expanded_cells")
    elif cut:
        reported.append("xlsx_text_chars")
    return "\n\n".join(parts)
