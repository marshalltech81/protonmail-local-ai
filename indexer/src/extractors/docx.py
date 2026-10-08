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

The walk reads the opened XML directly, one child element at a time,
and keeps what python-docx's ``iter_inner_content()`` and
``Paragraph.text`` keep, in the same order (#1031). Those select a
container's paragraphs and tables, a paragraph's runs and hyperlinks,
and a run's text elements with XPath unions, which libxml2 merges in
time quadratic in the number of siblings: plainly timed, 1 MiB of a
paragraph alternating runs and hyperlinks took 4.2 s, and a 1,500-page
synthetic report 15 s. ``tests/test_docx_walk.py`` pins the text the
old walk returned on a catalogue of shapes. The walk is also bounded,
counted as it goes: blocks (paragraphs and tables, plus a cost per
section for its header and footer references), table rows and cells,
text elements and characters, each charged before the item is read.
When a budget runs out the text collected so far is returned, as the
PPTX extractor does: the first budget to run out logs a rate-limited
WARNING naming it and is counted in the attachments aggregate's
``extractor_caps`` (#903).

Legacy ``.doc`` (binary Word, an OLE2 compound file, not OOXML) cannot
be parsed by ``python-docx``. The dispatcher never hands this module an
OLE2 payload: one labelled ``.doc`` goes to the ``doc`` extractor
(#935), and any other is recorded ``unsupported`` (#694). A
``.doc``-labelled payload reaches it only when it is not OLE2 (an OOXML
file mislabelled as ``.doc``).

Word templates (``.dotx``) are read the same way (#937). A template is
a ``.docx`` whose main part declares the template content type, and
``docx.Document`` refuses it ("not a Word file"). So the package is
opened with ``Package.open`` and the main part used directly. The
template content type is registered once, at import, in python-docx's
``PartFactory.part_type_for`` (the extension point ``docx/__init__.py``
uses for the document type), so the factory builds a ``DocumentPart``
for it. The payload bytes are not changed. This route depends on
python-docx 1.2.0's ``Package`` / ``PartFactory`` API;
``test_python_docx_route_for_templates_still_holds`` fails if an
upgrade breaks it.

Before python-docx opens it, the package is checked from the ZIP central
directory (#967, #946), as the PPTX extractor does. python-docx parses
every XML part it relates whole with lxml, and builds a part for every
related member, checking each relationship against a list of the parts
it has already visited, so opening costs the number of related members
times the number of relationships. A package fails as
``DocxPackageBudgetError``, which the dispatcher records ``unsupported``
since the same bytes always repeat it (#1032), when its members expand
by more than
``_MAX_EXPANSION_BYTES`` past their compressed size, declare more than
``_MAX_DECLARED_BYTES`` together (#1033), number more than
``_MAX_MEMBERS``, or hold more than ``_MAX_RELS_BYTES`` of relationship
parts. The constants below say what was measured.

python-docx follows a package's part relationships recursively while it
opens it, so a long chain of related parts raises ``RecursionError``
there (#945). Only the open is guarded: the error becomes
``DocxRelationshipChainError``, recorded ``failed`` by type, since a
chain of crafted parts is a property of the file, not host pressure. A
``RecursionError`` anywhere else keeps its own type.

The pre-open budgets trust the member sizes the ZIP central directory
declares, and a member that understates its size is still decompressed
whole when python-docx reads it. So the whole extraction, budgets
included, runs in a child process under an address-space and a CPU
limit (``ooxml``, #1040): ``extract`` starts it, and ``extract_text``
is what runs in it. A ``MemoryError`` or ``RecursionError`` there is
the child's failure, recorded ``failed`` by type, not host pressure.
The child reports the budget that cut the text by name, and this module
logs it.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable

from docx.document import Document as DocxDocument
from docx.opc.constants import CONTENT_TYPE as CT
from docx.opc.part import PartFactory
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tc
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.package import Package
from docx.parts.document import DocumentPart
from docx.section import _Footer, _Header

from . import over_package_budget, warn_extractor_cap
from .ooxml import run_child

log = logging.getLogger("indexer.extractor.docx")

_W_P = qn("w:p")
_W_TBL = qn("w:tbl")
_W_TR = qn("w:tr")
_W_TC = qn("w:tc")
_W_R = qn("w:r")
_W_HYPERLINK = qn("w:hyperlink")
# The run children whose text python-docx's ``Run.text`` joins.
_RUN_TEXT_TAGS = tuple(qn(f"w:{tag}") for tag in ("br", "cr", "noBreakHyphen", "ptab", "t", "tab"))

# Walk budgets (#1031). The walk reads each element once, so it is linear
# in the XML, but the package budgets below still let a crafted part hold
# millions of elements, and a header part several sections define is
# read once per section. Plainly timed (M-series Mac), the walk reads an
# empty paragraph in about 0.3 microseconds, a short paragraph with text
# in about 2, a table cell in about 0.7 and a text element in about
# 0.3; a section's header and footer references take about 37. A
# synthetic 1,500-page report (60,000 formatted paragraphs and 150
# tables of 100 cells) uses about 75,000 blocks, 18,000 table cells,
# 270,000 text elements and 5.8 million characters, and reads in about
# half a second.
#
# Paragraphs and tables in the body, headers, footers and table cells.
# A section costs ``_SECTION_COST`` more, for its header and footer
# references. 500,000 short paragraphs read in about 1.2 s.
_MAX_BLOCKS = 500_000
_SECTION_COST = 20

# Table rows and cells, nested tables included: about 0.35 s.
_MAX_TABLE_CELLS = 500_000

# Runs, hyperlinks, and the text, tab and break elements in runs: about
# 0.6 s.
_MAX_TEXT_ELEMENTS = 2_000_000

# Characters read. Five times the dispatcher's default
# ``INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS`` (2,000,000), as in the PPTX
# and XLSX extractors, so the dispatcher's cap still decides the stored
# length unless an operator raises it past this.
_MAX_TEXT_CHARS = 10_000_000

# python-docx 1.2.0 has no constant for the Word template main part.
WML_TEMPLATE_MAIN = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"
)
PartFactory.part_type_for[WML_TEMPLATE_MAIN] = DocumentPart

# Main-part content types this module reads: a document and a template.
_WORD_MAIN_TYPES = frozenset({CT.WML_DOCUMENT_MAIN, WML_TEMPLATE_MAIN})


# Bytes the package's members may expand by past their compressed size
# (#946). Plainly timed, opening parses XML at about 7 bytes of memory per
# byte of text-heavy XML and about 23 per byte of element-dense XML (32 MiB
# peaked at 241 MiB and 784 MiB). Pictures are stored already compressed
# and barely expand; a synthetic 500-page formatted document with 2,000
# pictures expanded by 16 MiB. The same figure as the PPTX extractor's.
_MAX_EXPANSION_BYTES = 32 * 1024 * 1024

# Bytes all the package's members declare together, stored or compressed
# (#1033). A member stored uncompressed does not expand, so without this
# the payload's own stored XML (``INDEXER_ATTACHMENT_MAX_BYTES``) came on
# top of the expansion above: about 64 MiB of XML at the defaults, and
# more with a larger payload cap. Plainly timed, opening stored
# element-dense XML raised peak memory by 722 MiB for 32 MiB, 1,080 MiB
# for 48 MiB and 1,439 MiB for 64 MiB. Pictures count too: real-shaped
# synthetic documents near the default 32 MiB payload cap declared
# 29-35 MiB in all (ten 2.8 MiB photos; 500 pages with 2,000 pictures;
# 1,500 pages with 300 pictures), so this allows the default payload cap
# plus 16 MiB, the largest expansion measured above. The same figure as
# the PPTX extractor's.
_MAX_DECLARED_BYTES = 48 * 1024 * 1024

# Members in the package (#967). python-docx checks each part it reaches
# against a list of the parts already visited: plainly timed, 5,000
# related members open in 0.2 s, 10,000 in 0.8 s and 20,000 in 2.6 s.
# A document has one member per picture, header, footer and embedded
# object; the 2,000-picture document above has about 2,000.
_MAX_MEMBERS = 5_000

# Declared bytes of relationship parts (#967). Each relationship to a part
# already visited is checked against the whole visited list, so the open
# costs members times relationships: plainly timed, 5,000 members and
# 4 MiB of the smallest relationships (about 44 bytes each, 84,000 of
# them) took 2.7 s. A picture's relationship is about 130 bytes and an
# external hyperlink's (which skips the check) about 200; the
# 2,000-picture document above, with 10,000 hyperlinks, held 2.1 MiB.
_MAX_RELS_BYTES = 4 * 1024 * 1024


# Address space (``RLIMIT_AS``), CPU seconds (``RLIMIT_CPU``) and
# wall-clock seconds the extraction's child process may use (#1040),
# past the CPU limit so a CPU-bound child meets that first. Plainly
# measured in the indexer image, child peak RSS and time:
#
# * a one-paragraph document: 45 MB and 0.2 s, nearly all of it
#   starting the child and importing python-docx;
# * a synthetic 1,500-page report (60,000 formatted paragraphs and 150
#   tables, stopped at the text budget): 220 MB and 0.7 s;
# * 500 pages with 2,000 pictures (30 MB): 148 MB and 0.4 s;
# * 600,000 empty paragraphs (stopped at the block budget): 117 MB and
#   0.4 s; 1 MiB of a paragraph alternating runs and hyperlinks (#1031):
#   54 MB and 0.3 s;
# * 47 MiB of stored element-dense XML, inside the pre-open budgets:
#   1,137 MB and 0.9 s;
# * a 0.5 MB document whose ``word/document.xml`` declares a few KB but
#   decompresses to 512 MiB (#1040): 1,069 MB and 0.5 s.
#
# 1 GiB is about 4.6 times the largest benign peak; the last two fail
# under it, as ``XMLSyntaxError`` (lxml reports a failed allocation as
# one) and ``MemoryError``. The CPU limit is many times the slowest
# case, as in the XLSX extractor.
CHILD_MAX_ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024
CHILD_MAX_CPU_SECONDS = 30
CHILD_TIMEOUT_SECONDS = 45.0

# The walk budgets the child may report as having cut the text.
_CAP_NAMES = frozenset({"docx_blocks", "docx_table_cells", "docx_text_elements", "docx_text_chars"})


class DocxPackageBudgetError(Exception):
    """The package is over a budget checked before python-docx opens it:
    expansion, declared size, member count or relationship bytes. Fixed
    text."""

    def __init__(self) -> None:
        super().__init__("package over a DOCX pre-open budget")


class DocxRelationshipChainError(Exception):
    """The package relates its parts in a chain too long for python-docx
    to follow: it follows relationships recursively. Fixed text."""

    def __init__(self) -> None:
        super().__init__("package relationship chain too deep to open")


def _open_document(payload: bytes) -> DocxDocument:
    """Load a ``.docx`` or ``.dotx`` payload as a python-docx document.

    The same check ``docx.Document`` makes, widened to the template type.
    Any other main part (a workbook, a presentation) raises ``ValueError``
    with fixed text, as ``docx.Document`` would. A package over a
    pre-open budget raises ``DocxPackageBudgetError`` before python-docx
    reads any member.
    """
    if over_package_budget(
        payload,
        max_members=_MAX_MEMBERS,
        max_expansion_bytes=_MAX_EXPANSION_BYTES,
        max_rels_bytes=_MAX_RELS_BYTES,
        max_declared_bytes=_MAX_DECLARED_BYTES,
    ):
        raise DocxPackageBudgetError()
    try:
        package = Package.open(io.BytesIO(payload))
    except RecursionError:
        raise DocxRelationshipChainError() from None
    part = package.main_document_part
    if part.content_type not in _WORD_MAIN_TYPES or not isinstance(part, DocumentPart):
        raise ValueError("main part is not a Word document or template")
    return part.document


class _Budget:
    """The four walk budgets of one extraction. ``take`` charges one item
    before it is read; when that item does not fit, the budget it ran out
    of is recorded in ``cut`` and the walk stops."""

    def __init__(self) -> None:
        self.left = {
            "docx_blocks": _MAX_BLOCKS,
            "docx_table_cells": _MAX_TABLE_CELLS,
            "docx_text_elements": _MAX_TEXT_ELEMENTS,
            "docx_text_chars": _MAX_TEXT_CHARS,
        }
        self.cut: str | None = None

    def take(self, cap: str, units: int = 1) -> bool:
        if self.left[cap] < units:
            self.cut = cap
            return False
        self.left[cap] -= units
        return True

    def take_text(self, text: str) -> str:
        """Charge ``text``'s characters and return the part of it that
        fits. When not all of it fits, the budget is spent and recorded
        in ``cut``."""
        left = self.left["docx_text_chars"]
        if len(text) <= left:
            self.left["docx_text_chars"] = left - len(text)
            return text
        self.left["docx_text_chars"] = 0
        self.cut = "docx_text_chars"
        return text[:left]


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001
    max_ocr_pages: int = 20,  # noqa: ARG001
    ocr_timeout_seconds: float | None = None,  # noqa: ARG001
    max_pdf_pages: int | None = None,  # noqa: ARG001
    on_progress: Callable[[], None] | None = None,  # noqa: ARG001
) -> tuple[str, str]:
    """Extract text from a DOCX or DOTX payload in the child process
    (``ooxml``, #1040). Returns (text, "docx")."""
    text, caps = run_child(
        "docx",
        payload,
        max_address_space_bytes=CHILD_MAX_ADDRESS_SPACE_BYTES,
        max_cpu_seconds=CHILD_MAX_CPU_SECONDS,
        timeout_seconds=CHILD_TIMEOUT_SECONDS,
        caps=_CAP_NAMES,
        permanent={"DocxPackageBudgetError": DocxPackageBudgetError},
    )
    for cap in caps:
        warn_extractor_cap(log, cap, "document truncated at a walk budget")
    return text, "docx"


def extract_text(payload: bytes) -> tuple[str, list[str]]:
    """The document's text and the names of the walk budgets that cut
    it. Runs in the child process (``ooxml_child``)."""
    document = _open_document(payload)

    budget = _Budget()
    # Body paragraphs keep the document's natural paragraph structure —
    # the chunker keys off blank-line gaps between paragraphs.
    lines: list[str] = []
    if _block_lines(document.element.body, budget, lines):
        _header_footer_lines(document, budget, lines)
    return "\n\n".join(lines), [budget.cut] if budget.cut is not None else []


def _header_footer_lines(document: DocxDocument, budget: _Budget, lines: list[str]) -> bool:
    """Append the lines of every header and footer Word displays, each
    part once. False once a budget ran out.

    A section has a default, a first-page and an even-page header and
    footer. The first-page pair shows when the section's
    ``different_first_page_header_footer`` is set, the even-page pair
    when the document's ``odd_and_even_pages_header_footer`` is. A part a
    section does not define (``is_linked_to_previous``) is inherited from
    the nearest earlier section that defines it. python-docx resolves that
    by recursing through every earlier section, so it is tracked here
    instead: the latest definition of each kind waits in ``pending``
    until a section shows it, and is read once. Each section's settings
    and references are visited once, so the work is linear in the XML;
    each section costs ``_SECTION_COST`` blocks. A part several sections
    define is read again for each, every read charged.
    """
    even_pages = document.settings.odd_and_even_pages_header_footer
    pending: dict[str, _Header | _Footer] = {}
    for section in document.sections:
        if not budget.take("docx_blocks", _SECTION_COST):
            return False
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
            if (
                shown
                and kind in pending
                and not _block_lines(pending.pop(kind)._element, budget, lines)
            ):
                return False
    return True


def _block_lines(container: BaseOxmlElement, budget: _Budget, lines: list[str]) -> bool:
    """Append one line per non-empty paragraph and one per non-empty table
    row of a body, header or footer, in document order. False once a
    budget ran out.

    The blocks are the container's ``w:p`` and ``w:tbl`` children, read
    one at a time. python-docx's ``iter_inner_content()`` selects them
    with the XPath union ``./w:p | ./w:tbl``, which libxml2 merges in
    time quadratic in the number of blocks before returning the first
    (#1031).
    """
    for block in container.iterchildren(_W_P, _W_TBL):
        if not budget.take("docx_blocks"):
            return False
        if block.tag == _W_P:
            text = _paragraph_text(block, budget)
            if text:
                lines.append(text)
            if budget.cut is not None:
                return False
        elif not _table_lines(block, budget, lines):
            return False
    return True


def _table_lines(tbl: BaseOxmlElement, budget: _Budget, lines: list[str]) -> bool:
    """Serialize each row as space-joined cells so a header row like
    "Invoice #  Date  Amount" stays on one line and matches a search for
    any of those tokens. Empty cells are dropped from the row to avoid
    runs of double-spaces that would dilute FTS scoring. Each row costs
    one table-cell unit. False once a budget ran out.
    """
    for tr in tbl.iterchildren(_W_TR):
        if not budget.take("docx_table_cells"):
            return False
        pieces: list[str] = []
        complete = _row_pieces(tr, budget, pieces)
        if pieces:
            lines.append(" ".join(pieces))
        if not complete:
            return False
    return True


def _row_pieces(tr: BaseOxmlElement, budget: _Budget, pieces: list[str]) -> bool:
    """Append the text of each cell in ``tr``, nested tables included.
    Each cell, and each row of a nested table, costs one table-cell unit,
    and each block in a cell one block. False once a budget ran out.

    Everything under a top-level row is appended to one flat list and
    joined once, so text inside nested tables is copied once rather than
    once per level of nesting.
    """
    for tc in tr.iterchildren(_W_TC):
        if not budget.take("docx_table_cells"):
            return False
        # A vertically merged cell's continuation rows hold no content
        # of their own; ``row.cells`` would repeat the first row's.
        if isinstance(tc, CT_Tc) and tc.vMerge == "continue":
            continue
        for block in tc.iterchildren(_W_P, _W_TBL):
            if not budget.take("docx_blocks"):
                return False
            if block.tag == _W_P:
                text = _paragraph_text(block, budget)
                if text:
                    pieces.append(text)
                if budget.cut is not None:
                    return False
                continue
            for nested_tr in block.iterchildren(_W_TR):
                if not budget.take("docx_table_cells"):
                    return False
                if not _row_pieces(nested_tr, budget, pieces):
                    return False
    return True


def _paragraph_text(p: BaseOxmlElement, budget: _Budget) -> str:
    """The paragraph's text, stripped: what python-docx's
    ``Paragraph.text`` returns, read one element at a time.

    That property joins the text of the paragraph's ``w:r`` and
    ``w:hyperlink`` children, a hyperlink's being that of its ``w:r``
    children and a run's that of its ``w:br``, ``w:cr``,
    ``w:noBreakHyphen``, ``w:ptab``, ``w:t`` and ``w:tab`` children, each
    mapped by the element's own ``str()``. It selects both lists with
    XPath unions, quadratic as in ``_block_lines``. Each of those
    elements costs one text-element unit, and its characters text-char
    units. When a budget runs out, the text read so far is returned with
    ``budget.cut`` set.
    """
    pieces: list[str] = []
    for child in p.iterchildren(_W_R, _W_HYPERLINK):
        if not budget.take("docx_text_elements"):
            break
        runs = (child,) if child.tag == _W_R else child.iterchildren(_W_R)
        for run in runs:
            if run is not child and not budget.take("docx_text_elements"):
                break
            for item in run.iterchildren(*_RUN_TEXT_TAGS):
                if not budget.take("docx_text_elements"):
                    break
                pieces.append(budget.take_text(str(item)))
                if budget.cut is not None:
                    break
            if budget.cut is not None:
                break
        if budget.cut is not None:
            break
    return "".join(pieces).strip()
