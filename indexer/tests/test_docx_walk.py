"""The DOCX extractor's walk over an opened document (#1031).

``TestDocxShapeCatalogue`` pins the text the walk returns for a
catalogue of WordprocessingML shapes. It was written against the walk
that read blocks through python-docx's ``iter_inner_content()`` and text
through ``Paragraph.text`` (main at 787193c9), before that walk was
replaced by direct child iteration, and must keep passing unchanged:
the rewrite keeps the same elements in the same order.

All text is synthetic.
"""

from __future__ import annotations

import io
import logging

import pytest

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _t(text: str) -> str:
    return f'<w:t xml:space="preserve">{text}</w:t>'


def _r(*inner: str) -> str:
    return "<w:r>" + "".join(inner) + "</w:r>"


def _p(*inner: str) -> str:
    """A paragraph; a plain string argument becomes one run of text."""
    body = "".join(i if i.startswith("<") else _r(_t(i)) for i in inner)
    return "<w:p>" + body + "</w:p>"


def _tc(*blocks: str, props: str = "") -> str:
    # A cell must end with a paragraph; the shapes add their own.
    return f"<w:tc><w:tcPr>{props}</w:tcPr>" + "".join(blocks) + "</w:tc>"


def _tr(*cells: str) -> str:
    return "<w:tr>" + "".join(cells) + "</w:tr>"


def _tbl(*rows: str) -> str:
    return "<w:tbl><w:tblPr/><w:tblGrid/>" + "".join(rows) + "</w:tbl>"


def _fill(element, fragment: str) -> None:
    """Replace ``element``'s block content with ``fragment``, keeping a
    trailing ``w:sectPr`` (the body's) in place."""
    from docx.oxml import parse_xml
    from docx.oxml.ns import qn

    for child in list(element):
        if child.tag != qn("w:sectPr"):
            element.remove(child)
    wrapper = parse_xml(f'<w:root xmlns:w="{_W}" xmlns:r="{_R}">{fragment}</w:root>')
    sect_pr = element.find(qn("w:sectPr"))
    for child in list(wrapper):
        if sect_pr is not None:
            sect_pr.addprevious(child)
        else:
            element.append(child)


def docx_payload(body: str, *, header: str | None = None, footer: str | None = None) -> bytes:
    """A synthetic ``.docx`` whose body (and optional default header and
    footer) hold exactly the given WordprocessingML fragments."""
    import docx

    document = docx.Document()
    _fill(document.element.body, body)
    section = document.sections[0]
    if header is not None:
        section.header.is_linked_to_previous = False
        _fill(section.header._element, header)
    if footer is not None:
        section.footer.is_linked_to_previous = False
        _fill(section.footer._element, footer)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


_RUN_CONTENT = _p(
    _r(
        _t("a"),
        "<w:tab/>",
        _t("b"),
        "<w:br/>",
        _t("c"),
        '<w:br w:type="page"/>',
        _t("d"),
        "<w:cr/>",
        _t("e"),
        "<w:noBreakHyphen/>",
        _t("f"),
        '<w:ptab w:relativeTo="margin" w:alignment="left" w:leader="none"/>',
        _t("g"),
        "<w:softHyphen/>",
        '<w:sym w:font="Symbol" w:char="F04A"/>',
        "<w:lastRenderedPageBreak/>",
        _t("h"),
        '<w:br w:type="column"/>',
        _t("i"),
        "<w:delText>j</w:delText>",
        "<w:instrText>k</w:instrText>",
        "<w:t/>",
        _t("l"),
    )
)

_HYPERLINKS = _p(
    "one ",
    '<w:hyperlink w:anchor="a1">' + _r(_t("link")) + _r(_t(" text")) + "</w:hyperlink>",
    " two",
    '<w:hyperlink w:anchor="a2">' + _r(_t(" tab"), "<w:tab/>", _t("end")) + "</w:hyperlink>",
    '<w:hyperlink w:anchor="a3"/>',
)

# Paragraph children other than runs and hyperlinks are not read.
_PARAGRAPH_WRAPPERS = _p(
    "kept",
    '<w:ins w:id="1" w:author="A" w:date="2026-01-01T00:00:00Z">' + _r(_t("inserted")) + "</w:ins>",
    '<w:del w:id="2" w:author="A" w:date="2026-01-01T00:00:00Z">'
    + "<w:r><w:delText>deleted</w:delText></w:r></w:del>",
    '<w:smartTag w:uri="u" w:element="e">' + _r(_t("smart")) + "</w:smartTag>",
    '<w:fldSimple w:instr="PAGE">' + _r(_t("field")) + "</w:fldSimple>",
    "<w:sdt><w:sdtContent>" + _r(_t("inline control")) + "</w:sdtContent></w:sdt>",
    '<w:customXml w:element="x">' + _r(_t("custom")) + "</w:customXml>",
    " end",
)

# Body children other than paragraphs and tables are not read.
_BODY_WRAPPERS = (
    "<w:sdt><w:sdtContent>"
    + _p("in control")
    + "</w:sdtContent></w:sdt>"
    + _p("outside")
    + '<w:customXml w:element="x">'
    + _p("custom")
    + "</w:customXml>"
    + "<!-- a comment -->"
    + "<?synthetic-pi data?>"
    + '<w:bookmarkStart w:id="0" w:name="b"/>'
    + _p("after")
    + '<w:bookmarkEnd w:id="0"/>'
)

_MERGED = _tbl(
    _tr(
        _tc(_p("span"), props='<w:gridSpan w:val="3"/>'),
        _tc(_p("top"), props='<w:vMerge w:val="restart"/>'),
    ),
    _tr(
        _tc(_p("left")),
        _tc(_p("mid")),
        _tc(_p("right")),
        _tc(_p("continued"), props='<w:vMerge w:val="continue"/>'),
    ),
    _tr(
        _tc(_p("a")),
        _tc(_p("b")),
        _tc(_p("c")),
        _tc(_p("implicit continue"), props="<w:vMerge/>"),
    ),
)

_NESTED = _tbl(
    _tr(
        _tc(
            _p("outer one"),
            _tbl(_tr(_tc(_p("inner one")), _tc(_p("inner two"))), _tr(_tc(_p("inner three")))),
            _p("after inner"),
        ),
        _tc(_p("outer two")),
    ),
    _tr(_tc(_p("")), _tc(_p("   "))),
    _tr(_tc(_p("last row"))),
)


def _deep(levels: int) -> str:
    fragment = _p(f"level {levels}")
    for level in range(levels - 1, -1, -1):
        fragment = _tbl(_tr(_tc(_p(f"level {level}"), fragment)))
    return fragment


# Row and cell children other than rows and cells are not read.
_TABLE_WRAPPERS = _tbl(
    _tr(
        _tc(_p("cell"), "<w:sdt><w:sdtContent>" + _p("cell control") + "</w:sdtContent></w:sdt>"),
        "<w:sdt><w:sdtContent>" + _tc(_p("control cell")) + "</w:sdtContent></w:sdt>",
        '<w:customXml w:element="x">' + _tc(_p("custom cell")) + "</w:customXml>",
        _tc(_p("last cell")),
    ),
    "<w:sdt><w:sdtContent>" + _tr(_tc(_p("control row"))) + "</w:sdtContent></w:sdt>",
    _tr(_tc(_p("plain row"))),
)

CATALOGUE: list[tuple[str, str, str | None, str | None, str]] = [
    ("empty", "", None, None, ""),
    ("empty-paragraph-element", "<w:p/>", None, None, ""),
    ("plain-paragraphs", _p("alpha") + _p("beta"), None, None, "alpha\n\nbeta"),
    (
        "whitespace-and-empty-paragraphs",
        _p("  gamma  ") + _p("") + _p("   ") + "<w:p/>" + _p("été – delta"),
        None,
        None,
        "gamma\n\nété – delta",
    ),
    ("several-runs", _p("one", " two", " three"), None, None, "one two three"),
    ("run-content", _RUN_CONTENT, None, None, "a\tb\ncd\ne-f\tghil"),
    ("run-only-breaks", _p(_r("<w:br/>", "<w:tab/>")), None, None, ""),
    ("hyperlinks", _HYPERLINKS, None, None, "one link text two tab\tend"),
    ("paragraph-wrappers", _PARAGRAPH_WRAPPERS, None, None, "kept end"),
    (
        "paragraph-properties",
        '<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
        + _r("<w:rPr><w:b/></w:rPr>", _t("bold"))
        + "</w:p>",
        None,
        None,
        "bold",
    ),
    ("body-wrappers", _BODY_WRAPPERS, None, None, "outside\n\nafter"),
    (
        "simple-table",
        _tbl(_tr(_tc(_p("a")), _tc(_p("b"))), _tr(_tc(_p("c")), _tc(_p("")))),
        None,
        None,
        "a b\n\nc",
    ),
    (
        "multi-paragraph-cells",
        _tbl(_tr(_tc(_p("first"), _p(""), _p(" second ")), _tc(_p("third")))),
        None,
        None,
        "first second third",
    ),
    ("empty-table", "<w:tbl><w:tblPr/><w:tblGrid/></w:tbl>", None, None, ""),
    ("empty-rows", _tbl(_tr(), _tr(_tc(_p(""))), _tr(_tc())), None, None, ""),
    ("merged-cells", _MERGED, None, None, "span top\n\nleft mid right\n\na b c"),
    (
        "nested-table",
        _NESTED,
        None,
        None,
        "outer one inner one inner two inner three after inner outer two\n\nlast row",
    ),
    (
        "deep-nesting",
        _deep(6),
        None,
        None,
        "level 0 level 1 level 2 level 3 level 4 level 5 level 6",
    ),
    (
        "alternating-paragraphs-and-tables",
        _p("p0")
        + _tbl(_tr(_tc(_p("t0"))))
        + _p("p1")
        + _tbl(_tr(_tc(_p("t1"))), _tr(_tc(_p("t1b"))))
        + _p("p2"),
        None,
        None,
        "p0\n\nt0\n\np1\n\nt1\n\nt1b\n\np2",
    ),
    ("table-wrappers", _TABLE_WRAPPERS, None, None, "cell last cell\n\nplain row"),
    (
        "header-and-footer",
        _p("body"),
        _p("head") + _tbl(_tr(_tc(_p("head cell")), _tc(_p("head cell 2")))) + _p("head end"),
        _p("foot"),
        "body\n\nhead\n\nhead cell head cell 2\n\nhead end\n\nfoot",
    ),
    (
        "header-wrappers",
        _p("body"),
        "<w:sdt><w:sdtContent>" + _p("header control") + "</w:sdtContent></w:sdt>" + _p("head"),
        _p(_r(_t("page ")), '<w:fldSimple w:instr="PAGE">' + _r(_t("1")) + "</w:fldSimple>"),
        "body\n\nhead\n\npage",
    ),
    ("empty-header", _p("body"), "", _p(""), "body"),
]


class TestDocxShapeCatalogue:
    @pytest.mark.parametrize(
        ("body", "header", "footer", "expected"),
        [case[1:] for case in CATALOGUE],
        ids=[case[0] for case in CATALOGUE],
    )
    def test_walk_returns_the_pinned_text(self, body, header, footer, expected):
        from src.extractors.docx import extract

        text, name = extract(docx_payload(body, header=header, footer=footer))
        assert name == "docx"
        assert text == expected


def _header_shared_by_sections(sections: int, header_paragraphs: int, marker: str) -> bytes:
    """Every section defines its default header as the same header part,
    which holds ``header_paragraphs`` short paragraphs, ``marker`` first."""
    import docx
    from docx.oxml import parse_xml
    from docx.oxml.ns import qn

    document = docx.Document()
    section = document.sections[0]
    section.header.is_linked_to_previous = False
    paragraphs = [_p(marker)] + [_p(f"h{i}") for i in range(1, header_paragraphs)]
    _fill(section.header._element, "".join(paragraphs))
    rid = section._sectPr.find(qn("w:headerReference")).get(qn("r:id"))
    unit = (
        f'<w:p xmlns:w="{_W}" xmlns:r="{_R}"><w:pPr><w:sectPr>'
        f'<w:headerReference w:type="default" r:id="{rid}"/></w:sectPr></w:pPr></w:p>'
    )
    body_sect_pr = document.element.body.find(qn("w:sectPr"))
    for _ in range(sections - 1):
        body_sect_pr.addprevious(parse_xml(unit))
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _record_budgets(monkeypatch) -> list:
    """Keep each extraction's budget so a test can read what it spent."""
    from src.extractors import docx

    made: list = []

    class Recording(docx._Budget):
        def __init__(self) -> None:
            super().__init__()
            made.append(self)

    monkeypatch.setattr(docx, "_Budget", Recording)
    return made


def _spent(budget) -> dict[str, int]:
    from src.extractors import docx

    limits = {
        "docx_blocks": docx._MAX_BLOCKS,
        "docx_table_cells": docx._MAX_TABLE_CELLS,
        "docx_text_elements": docx._MAX_TEXT_ELEMENTS,
        "docx_text_chars": docx._MAX_TEXT_CHARS,
    }
    return {cap: limits[cap] - left for cap, left in budget.left.items()}


def _record_xpath(monkeypatch) -> list[str]:
    """Record every XPath query python-docx's elements run."""
    from docx.oxml.xmlchemy import BaseOxmlElement

    queries: list[str] = []
    original = BaseOxmlElement.xpath

    def recording(self, xpath_str):
        queries.append(xpath_str)
        return original(self, xpath_str)

    monkeypatch.setattr(BaseOxmlElement, "xpath", recording)
    return queries


def _count_paragraph_reads(monkeypatch) -> list[int]:
    from src.extractors import docx

    calls = [0]
    original = docx._paragraph_text

    def counting(*args):
        calls[0] += 1
        return original(*args)

    monkeypatch.setattr(docx, "_paragraph_text", counting)
    return calls


def _cap_lines(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "extractor cap" in r.getMessage()]


# Shapes whose siblings python-docx selected with an XPath union: blocks
# in a body and in a cell, runs and hyperlinks in a paragraph, and text
# elements in a run. ``n`` repeats the unit.
_SIBLING_SHAPES = {
    "body-blocks": lambda n: (_p("x") + _tbl(_tr(_tc(_p("y"))))) * n,
    "cell-blocks": lambda n: _tbl(_tr(_tc((_p("x") + _tbl(_tr(_tc(_p("y"))))) * n + _p("z")))),
    "runs-and-hyperlinks": lambda n: _p(
        *[_r(_t("x")) + '<w:hyperlink w:anchor="a">' + _r(_t("y")) + "</w:hyperlink>"] * n
    ),
    "run-text-elements": lambda n: _p(_r(*[_t("x"), "<w:tab/>"] * n)),
}


class TestDocxWalkIsLinear:
    """#1031: the walk reads each sibling once, with no XPath union over
    them, so its work grows with the XML and nothing else."""

    @pytest.mark.parametrize("shape", sorted(_SIBLING_SHAPES))
    def test_no_xpath_query_over_the_siblings(self, shape, monkeypatch):
        """The queries the extraction runs (opening the package and
        listing sections) are the same for 10 units as for 5,000, and
        none is a union over block, run or run-content siblings."""
        from src.extractors.docx import extract

        small = docx_payload(_SIBLING_SHAPES[shape](10))
        large = docx_payload(_SIBLING_SHAPES[shape](5_000))
        runs: list[list[str]] = []
        for payload in (small, large):
            with monkeypatch.context() as m:
                queries = _record_xpath(m)
                extract(payload)
                runs.append(queries)
        assert runs[0] == runs[1]
        for query in runs[1]:
            assert not {"w:p", "w:tbl", "w:r", "w:hyperlink", "w:t"} & {
                step.strip().removeprefix("./") for step in query.split("|")
            }

    @pytest.mark.parametrize(
        ("shape", "per_unit"),
        [
            # A paragraph, and a table with its row, cell and paragraph.
            ("body-blocks", {"docx_blocks": 3, "docx_table_cells": 2}),
            ("cell-blocks", {"docx_blocks": 3, "docx_table_cells": 2}),
            # A run and its text; a hyperlink, its run and its text.
            ("runs-and-hyperlinks", {"docx_text_elements": 5}),
            ("run-text-elements", {"docx_text_elements": 2}),
        ],
    )
    def test_work_charged_is_linear(self, shape, per_unit, monkeypatch):
        from src.extractors.docx import extract

        units = 1_000
        budgets = _record_budgets(monkeypatch)
        extract(docx_payload(_SIBLING_SHAPES[shape](units)))
        spent = _spent(budgets[0])
        for cap, cost in per_unit.items():
            # Plus the fixed part of each shape: the section, the outer
            # table, row and cell, the paragraph and run around the units.
            assert units * cost <= spent[cap] <= units * cost + 30

    def test_alternating_paragraphs_and_tables_at_one_mib(self, monkeypatch, caplog):
        """The issue's shape: 1 MiB of body XML alternating empty
        paragraphs and tables took 11 s through ``iter_inner_content()``."""
        import time

        from src.extractors import docx

        unit = "<w:p/><w:tbl/>"
        units = 1024 * 1024 // len(unit) + 1
        payload = docx_payload(unit * units)
        budgets = _record_budgets(monkeypatch)
        reads = _count_paragraph_reads(monkeypatch)
        started = time.perf_counter()
        text, _ = docx.extract(payload)
        assert time.perf_counter() - started < 10
        assert text == ""
        assert reads[0] == units
        assert _spent(budgets[0])["docx_blocks"] == 2 * units + docx._SECTION_COST
        assert not _cap_lines(caplog)

    def test_a_long_report_is_read_in_full(self, caplog):
        """A synthetic 1,500-page formatted report (60,000 paragraphs and
        150 tables of 100 cells) is far inside every budget."""
        import time

        from src import extractors
        from src.extractors.docx import extract

        extractors.drain_extractor_counts()
        caplog.set_level("DEBUG")
        paragraph = (
            '<w:p><w:pPr><w:spacing w:after="120"/></w:pPr>'
            '<w:r><w:rPr><w:sz w:val="22"/></w:rPr>'
            + _t("Synthetic report paragraph {i} with ordinary sentence text. ")
            + "</w:r><w:r><w:rPr><w:b/></w:rPr>"
            + _t("Bold tail.")
            + "</w:r></w:p>"
        )
        parts = []
        for i in range(60_000):
            parts.append(paragraph.replace("{i}", str(i)))
            if i % 400 == 399:
                rows = [_tr(*[_tc(_p(f"r{r}c{c} t{i}")) for c in range(5)]) for r in range(20)]
                parts.append(_tbl(*rows))
        payload = docx_payload("".join(parts))
        started = time.perf_counter()
        text, _ = extract(payload)
        assert time.perf_counter() - started < 15
        assert "Synthetic report paragraph 0 with" in text
        assert "r19c4 t59999" in text
        assert not _cap_lines(caplog)
        assert extractors.drain_extractor_counts()["extractor_caps"] == 0


class TestDocxWalkBudgets:
    """#1031: what a crafted document can make the walk do is bounded by
    the walk budgets, and the text read before a budget ran out is kept."""

    _MARKER = "SYNTHETIC_DOCX_WALK_MARKER"

    def test_dense_empty_paragraphs_stop_at_the_block_budget(self, monkeypatch, caplog):
        """The issue's other shape: element-dense XML of empty
        paragraphs, past the block budget."""
        import time

        from src import extractors
        from src.extractors import docx

        extractors.drain_extractor_counts()
        caplog.set_level("DEBUG")
        payload = docx_payload("<w:p/>" * (docx._MAX_BLOCKS + 100_000) + _p(self._MARKER))
        reads = _count_paragraph_reads(monkeypatch)
        started = time.perf_counter()
        text, _ = docx.extract(payload)
        assert time.perf_counter() - started < 10
        assert text == ""
        assert reads[0] == docx._MAX_BLOCKS
        lines = _cap_lines(caplog)
        assert [r.levelname for r in lines] == ["WARNING"]
        assert (
            "extractor cap docx_blocks: document truncated after 0 lines" in lines[0].getMessage()
        )
        assert extractors.drain_extractor_counts()["extractor_caps"] == 1
        assert self._MARKER not in caplog.text

    def test_a_header_part_every_section_shows_is_charged_per_read(self, monkeypatch, caplog):
        """A header part many sections define is read again for each:
        600 sections showing one part of 1,000 paragraphs would read
        600,000 paragraphs from a payload of a few kilobytes."""
        import time

        from src import extractors
        from src.extractors import docx

        extractors.drain_extractor_counts()
        caplog.set_level("DEBUG")
        payload = _header_shared_by_sections(600, 1_000, self._MARKER)
        budgets = _record_budgets(monkeypatch)
        reads = _count_paragraph_reads(monkeypatch)
        started = time.perf_counter()
        text, _ = docx.extract(payload)
        assert time.perf_counter() - started < 15
        # The body's 599 paragraphs, then each section's cost and a whole
        # header read while they fit: the block budget is the bound.
        per_section = docx._SECTION_COST + 1_000
        whole = (docx._MAX_BLOCKS - 599) // per_section
        assert whole < 600
        assert text.count("h999") == whole
        assert 599 + whole * 1_000 <= reads[0] <= 599 + (whole + 1) * 1_000
        assert _spent(budgets[0])["docx_blocks"] > docx._MAX_BLOCKS - per_section
        lines = _cap_lines(caplog)
        assert [r.levelname for r in lines] == ["WARNING"]
        assert "extractor cap docx_blocks" in lines[0].getMessage()
        assert extractors.drain_extractor_counts()["extractor_caps"] == 1
        assert self._MARKER not in caplog.text

    @pytest.mark.parametrize(
        ("constant", "setting", "body", "reported"),
        [
            # The section costs ``_SECTION_COST`` blocks after the body.
            ("_MAX_BLOCKS", "2+section", _p("a") + _p("b"), False),
            ("_MAX_BLOCKS", "2+section", _p("a") + _p("b") + _p("c"), True),
            ("_MAX_BLOCKS", "2", _p("a") + _p("b"), True),
            # A row and each cell cost one.
            ("_MAX_TABLE_CELLS", "3", _tbl(_tr(_tc(_p("a")), _tc(_p("b")))), False),
            ("_MAX_TABLE_CELLS", "3", _tbl(_tr(_tc(_p("a")), _tc(_p("b"))), _tr()), True),
            # A run and its text element cost one each.
            ("_MAX_TEXT_ELEMENTS", "2", _p("a"), False),
            ("_MAX_TEXT_ELEMENTS", "2", _p("a") + _p("b"), True),
            ("_MAX_TEXT_CHARS", "6", _p("abcdef"), False),
            ("_MAX_TEXT_CHARS", "6", _p("abcdef") + _p("g"), True),
        ],
    )
    def test_budget_spent_exactly_reports_only_a_real_cut(
        self, constant, setting, body, reported, monkeypatch, caplog
    ):
        from src import extractors
        from src.extractors import docx

        extractors.drain_extractor_counts()
        value = int(setting.split("+")[0]) + (docx._SECTION_COST if "+" in setting else 0)
        monkeypatch.setattr(docx, constant, value)
        caplog.set_level("DEBUG")
        docx.extract(docx_payload(body))
        assert bool(_cap_lines(caplog)) is reported
        assert extractors.drain_extractor_counts()["extractor_caps"] == int(reported)

    def test_a_cut_inside_a_nested_table_keeps_the_row_read_so_far(self, monkeypatch):
        from src.extractors import docx

        inner = _tbl(_tr(_tc(_p("inner one")), _tc(_p("inner two"))))
        body = _tbl(_tr(_tc(_p("outer"), inner), _tc(_p("next"))), _tr(_tc(_p("second row"))))
        # The outer row and cell, the inner row and first cell; the second
        # inner cell is refused.
        monkeypatch.setattr(docx, "_MAX_TABLE_CELLS", 4)
        text, _ = docx.extract(docx_payload(body))
        assert text == "outer inner one"

    def test_a_cut_inside_a_hyperlink_keeps_the_text_read_so_far(self, monkeypatch):
        from src.extractors import docx

        body = _p(
            "before ",
            '<w:hyperlink w:anchor="a">' + _r(_t("link")) + _r(_t(" more")) + "</w:hyperlink>",
        )
        # The run and its text, the hyperlink, its first run and its
        # text; the second run is refused.
        monkeypatch.setattr(docx, "_MAX_TEXT_ELEMENTS", 5)
        text, _ = docx.extract(docx_payload(body))
        assert text == "before link"

    @pytest.mark.parametrize(
        ("constant", "value", "body", "expected"),
        [
            # The table costs a block, as does each paragraph in its cell;
            # the third paragraph is refused.
            ("_MAX_BLOCKS", 3, _tbl(_tr(_tc(_p("a"), _p("b"), _p("c")))), "a b"),
            # The cell's second paragraph keeps the character that fits.
            ("_MAX_TEXT_CHARS", 3, _tbl(_tr(_tc(_p("ab"), _p("cd")))), "ab c"),
            # The outer row and cell, the nested first row and its cell;
            # the nested second row is refused.
            (
                "_MAX_TABLE_CELLS",
                4,
                _tbl(_tr(_tc(_p("outer"), _tbl(_tr(_tc(_p("one"))), _tr(_tc(_p("two"))))))),
                "outer one",
            ),
            # The run and its first text element; the second is refused.
            ("_MAX_TEXT_ELEMENTS", 2, _p(_r(_t("a"), _t("b"))), "a"),
        ],
        ids=["block-in-cell", "chars-in-cell", "nested-row", "element-in-run"],
    )
    def test_a_cut_anywhere_keeps_the_text_read_before_it(
        self, constant, value, body, expected, monkeypatch, caplog
    ):
        from src.extractors import docx

        monkeypatch.setattr(docx, constant, value)
        caplog.set_level("DEBUG")
        text, _ = docx.extract(docx_payload(body))
        assert text == expected
        assert len(_cap_lines(caplog)) == 1
