"""OOXML identification by the package's own metadata (owner decision
on #1416, 2026-10-10, PR #1444 review round 7).

The identification child reads ``[Content_Types].xml`` and
``_rels/.rels`` and names the OOXML extractors that would read the
package, by their own rules: Word and PowerPoint by the single root
``officeDocument`` relationship and its part's content type, Excel by
openpyxl's workbook lookup in the content types. The catalogue below
crosses renamed and conventional main parts, relationship and
content-type variants and malformed metadata; each shape's token is
checked against what the extractors' libraries themselves select
(python-docx, python-pptx, openpyxl), so the class cannot drift one
shape at a time.
"""

from __future__ import annotations

import io
import time
import zipfile

import pytest
from src.extractors import container_child

from tests.conftest import (
    OFFICE_DOCUMENT_REL,
    OPC_RELATIONSHIPS_NS,
    PRESENTATION_MAIN_CT,
    WORD_MAIN_CT,
    WORKBOOK_MAIN_CT,
    make_package,
    ooxml_content_types,
    ooxml_rels,
)

_WORD_BODY = (
    b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    b"<w:body/></w:document>"
)
_PRESENTATION_BODY = (
    b'<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'
)
_WORKBOOK_BODY = b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
_DOTX = "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"
_DOCM = "application/vnd.ms-word.document.macroEnabled.main+xml"
_POTX = "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml"
_PPSM = "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml"
_XLSM = "application/vnd.ms-excel.sheet.macroEnabled.main+xml"
_STRICT_REL = "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument"
_CORE_REL = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"


def _pkg(
    part: str,
    body: bytes,
    content_type: str | None,
    *,
    target: str | None = None,
    rels: bytes | None = None,
    content_types: bytes | None = None,
    extra: dict[str, bytes] | None = None,
) -> bytes:
    """A package with ``part`` (an Override of ``content_type`` unless
    ``None``) and a root relationship to ``target`` (``/part`` unless
    given); ``rels`` / ``content_types`` replace the generated members,
    ``b""`` meaning absent."""
    overrides = {} if content_type is None else {f"/{part}": content_type}
    members = {
        "[Content_Types].xml": ooxml_content_types(overrides)
        if content_types is None
        else content_types,
        "_rels/.rels": ooxml_rels(target or f"/{part}") if rels is None else rels,
        part: body,
    }
    members.update(extra or {})
    return make_package(
        {name: data for name, data in members.items() if data != b"" or name == part}
    )


def _rels_xml(*relationships: str) -> bytes:
    return f'<Relationships xmlns="{OPC_RELATIONSHIPS_NS}">{"".join(relationships)}</Relationships>'.encode()


def _encrypted_member(payload: bytes, name: str) -> bytes:
    """``payload`` with ``name`` flagged encrypted (general-purpose bit 0)
    in its local header and the central directory: zipfile would need a
    password to read it."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = {info.filename: archive.read(info) for info in archive.infolist()}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for member, data in members.items():
            info = zipfile.ZipInfo(member, date_time=(2024, 1, 1, 0, 0, 0))
            if member == name:
                info.flag_bits |= 0x1
            archive.writestr(info, data)
    return buffer.getvalue()


_DEFAULT_XML_AS_WORD = ooxml_content_types(
    defaults={
        "rels": "application/vnd.openxmlformats-package.relationships+xml",
        "xml": WORD_MAIN_CT,
    }
)
_DEFAULT_XML_AS_WORKBOOK = ooxml_content_types(
    defaults={
        "rels": "application/vnd.openxmlformats-package.relationships+xml",
        "xml": WORKBOOK_MAIN_CT,
    }
)

SHAPES: dict[str, bytes] = {
    # Conventional main parts.
    "docx": _pkg("word/document.xml", _WORD_BODY, WORD_MAIN_CT),
    "xlsx": _pkg("xl/workbook.xml", _WORKBOOK_BODY, WORKBOOK_MAIN_CT),
    "pptx": _pkg("ppt/presentation.xml", _PRESENTATION_BODY, PRESENTATION_MAIN_CT),
    # Renamed main parts, which the conventional names missed.
    "docx-renamed": _pkg("word/document2.xml", _WORD_BODY, WORD_MAIN_CT),
    "docx-renamed-relative-target": _pkg(
        "word/document2.xml", _WORD_BODY, WORD_MAIN_CT, target="word/document2.xml"
    ),
    "docx-renamed-dot-segments": _pkg(
        "word/document2.xml", _WORD_BODY, WORD_MAIN_CT, target="/word/./x/../document2.xml"
    ),
    "docx-elsewhere": _pkg("main/body.xml", _WORD_BODY, WORD_MAIN_CT),
    "xlsx-renamed": _pkg("xl/wb2.xml", _WORKBOOK_BODY, WORKBOOK_MAIN_CT),
    "pptx-renamed": _pkg("ppt/pres2.xml", _PRESENTATION_BODY, PRESENTATION_MAIN_CT),
    # Other main types the extractors read, and one they do not.
    "dotx": _pkg("word/document.xml", _WORD_BODY, _DOTX),
    "docm": _pkg("word/document.xml", _WORD_BODY, _DOCM),
    "potx": _pkg("ppt/presentation.xml", _PRESENTATION_BODY, _POTX),
    "ppsm": _pkg("ppt/presentation.xml", _PRESENTATION_BODY, _PPSM),
    "xlsm": _pkg("xl/workbook.xml", _WORKBOOK_BODY, _XLSM),
    # A main part typed by a Default for its extension.
    "docx-default-type": _pkg("word/doc.xml", _WORD_BODY, None, content_types=_DEFAULT_XML_AS_WORD),
    "xlsx-default-type": _pkg(
        "xl/workbook.xml", _WORKBOOK_BODY, None, content_types=_DEFAULT_XML_AS_WORKBOOK
    ),
    "xlsx-default-type-no-workbook": _pkg(
        "xl/other.xml", _WORKBOOK_BODY, None, content_types=_DEFAULT_XML_AS_WORKBOOK
    ),
    # Part-name case: both libraries compare part names and extensions
    # case-insensitively.
    "docx-target-case": _pkg(
        "WORD/Document2.xml",
        _WORD_BODY,
        None,
        content_types=ooxml_content_types({"/word/document2.xml": WORD_MAIN_CT}),
    ),
    "pptx-target-case": _pkg(
        "PPT/Pres2.xml",
        _PRESENTATION_BODY,
        None,
        content_types=ooxml_content_types({"/ppt/pres2.xml": PRESENTATION_MAIN_CT}),
    ),
    "pptx-override-case": _pkg(
        "ppt/pres2.xml",
        _PRESENTATION_BODY,
        None,
        content_types=ooxml_content_types({"/PPT/Pres2.XML": PRESENTATION_MAIN_CT}),
    ),
    # Relationship variants.
    "docx-no-rels": _pkg("word/document.xml", _WORD_BODY, WORD_MAIN_CT, rels=b""),
    "xlsx-no-rels": _pkg("xl/workbook.xml", _WORKBOOK_BODY, WORKBOOK_MAIN_CT, rels=b""),
    "docx-two-roots": _pkg(
        "word/document.xml",
        _WORD_BODY,
        WORD_MAIN_CT,
        rels=ooxml_rels("/word/document.xml", "/word/document.xml"),
    ),
    "docx-external-root": _pkg(
        "word/document.xml",
        _WORD_BODY,
        WORD_MAIN_CT,
        rels=ooxml_rels("/word/document.xml", mode="External"),
    ),
    "docx-target-missing": _pkg(
        "word/document.xml", _WORD_BODY, WORD_MAIN_CT, target="/word/absent.xml"
    ),
    "docx-strict-relationship": _pkg(
        "word/document.xml",
        _WORD_BODY,
        WORD_MAIN_CT,
        rels=ooxml_rels("/word/document.xml", rel_type=_STRICT_REL),
    ),
    "docx-other-relationship-only": _pkg(
        "word/document.xml",
        _WORD_BODY,
        WORD_MAIN_CT,
        rels=ooxml_rels("/docProps/core.xml", rel_type=_CORE_REL),
    ),
    "docx-root-beside-others": _pkg(
        "word/document.xml",
        _WORD_BODY,
        WORD_MAIN_CT,
        rels=_rels_xml(
            f'<Relationship Id="rId1" Type="{_CORE_REL}" Target="/docProps/core.xml"/>',
            f'<Relationship Id="rId2" Type="{OFFICE_DOCUMENT_REL}" Target="/word/document.xml"/>',
        ),
        extra={"docProps/core.xml": b"<x/>"},
    ),
    "docx-root-without-target": _pkg(
        "word/document.xml",
        _WORD_BODY,
        WORD_MAIN_CT,
        rels=_rels_xml(f'<Relationship Id="rId1" Type="{OFFICE_DOCUMENT_REL}"/>'),
    ),
    "docx-root-beside-untyped": _pkg(
        "word/document.xml",
        _WORD_BODY,
        WORD_MAIN_CT,
        rels=_rels_xml(
            '<Relationship Id="rId1" Target="/docProps/core.xml"/>',
            f'<Relationship Id="rId2" Type="{OFFICE_DOCUMENT_REL}" Target="/word/document.xml"/>',
        ),
        extra={"docProps/core.xml": b"<x/>"},
    ),
    "docx-relationships-unnamespaced": _pkg(
        "word/document.xml",
        _WORD_BODY,
        WORD_MAIN_CT,
        rels=(
            b'<Relationships><Relationship Id="rId1" Type="'
            + OFFICE_DOCUMENT_REL.encode()
            + b'" Target="/word/document.xml"/></Relationships>'
        ),
    ),
    "docx-malformed-rels": _pkg(
        "word/document.xml", _WORD_BODY, WORD_MAIN_CT, rels=b"<Relationships"
    ),
    # Content-type variants.
    "docx-no-content-types-member": _pkg(
        "word/document.xml", _WORD_BODY, WORD_MAIN_CT, content_types=b""
    ),
    "docx-content-types-lowercase-member": make_package(
        {
            "[content_types].xml": ooxml_content_types({"/word/document.xml": WORD_MAIN_CT}),
            "_rels/.rels": ooxml_rels("/word/document.xml"),
            "word/document.xml": _WORD_BODY,
        }
    ),
    "docx-malformed-content-types": _pkg(
        "word/document.xml", _WORD_BODY, WORD_MAIN_CT, content_types=b"<Types"
    ),
    "xlsx-malformed-content-types": _pkg(
        "xl/workbook.xml", _WORKBOOK_BODY, WORKBOOK_MAIN_CT, content_types=b"\x00\x01"
    ),
    "docx-content-types-unnamespaced": _pkg(
        "word/document.xml",
        _WORD_BODY,
        WORD_MAIN_CT,
        content_types=(
            b'<Types><Override PartName="/word/document.xml" ContentType="'
            + WORD_MAIN_CT.encode()
            + b'"/></Types>'
        ),
    ),
    "docx-override-without-content-type": _pkg(
        "word/document.xml",
        _WORD_BODY,
        None,
        content_types=ooxml_content_types({"/word/document.xml": WORD_MAIN_CT}).replace(
            b"</Types>", b'<Override PartName="/x.xml"/></Types>'
        ),
    ),
    "pptx-override-without-content-type": _pkg(
        "ppt/presentation.xml",
        _PRESENTATION_BODY,
        None,
        content_types=ooxml_content_types({"/ppt/presentation.xml": PRESENTATION_MAIN_CT}).replace(
            b"</Types>", b'<Override PartName="/x.xml"/></Types>'
        ),
    ),
    "docx-override-without-part-name": _pkg(
        "word/document.xml",
        _WORD_BODY,
        None,
        content_types=ooxml_content_types({"/word/document.xml": WORD_MAIN_CT}).replace(
            b"</Types>", b'<Override ContentType="application/xml"/></Types>'
        ),
    ),
    "pptx-default-without-extension": _pkg(
        "ppt/presentation.xml",
        _PRESENTATION_BODY,
        None,
        content_types=ooxml_content_types({"/ppt/presentation.xml": PRESENTATION_MAIN_CT}).replace(
            b"</Types>", b'<Default ContentType="application/xml"/></Types>'
        ),
    ),
    "docx-foreign-element-in-types": _pkg(
        "word/document.xml",
        _WORD_BODY,
        None,
        content_types=ooxml_content_types({"/word/document.xml": WORD_MAIN_CT}).replace(
            b"</Types>", b"<Other/></Types>"
        ),
    ),
    "docx-encrypted-content-types": _encrypted_member(
        _pkg("word/document.xml", _WORD_BODY, WORD_MAIN_CT), "[Content_Types].xml"
    ),
    "xlsx-override-missing-member": _pkg(
        "xl/other.xml",
        _WORKBOOK_BODY,
        None,
        content_types=ooxml_content_types({"/xl/wb2.xml": WORKBOOK_MAIN_CT}),
    ),
    # Two kinds in one package.
    "docx-with-workbook": _pkg(
        "word/document.xml",
        _WORD_BODY,
        None,
        content_types=ooxml_content_types(
            {"/word/document.xml": WORD_MAIN_CT, "/xl/workbook.xml": WORKBOOK_MAIN_CT}
        ),
        extra={"xl/workbook.xml": _WORKBOOK_BODY},
    ),
    "pptx-with-workbook-default": _pkg(
        "ppt/presentation.xml",
        _PRESENTATION_BODY,
        PRESENTATION_MAIN_CT,
        content_types=ooxml_content_types(
            {"/ppt/presentation.xml": PRESENTATION_MAIN_CT},
            defaults={"bin": WORKBOOK_MAIN_CT},
        ),
        extra={"xl/workbook.xml": _WORKBOOK_BODY},
    ),
    # Not an OOXML package.
    "zip-no-content-types": make_package({"word/document.xml": _WORD_BODY}),
}


def _library_kinds(payload: bytes) -> set[str]:
    """The OOXML extractors whose library opens the package's main part:
    the ground truth the child's answer is checked against."""
    from openpyxl.packaging.manifest import Manifest
    from openpyxl.xml.functions import fromstring
    from src.extractors import docx, pptx, xlsx

    kinds: set[str] = set()
    for kind, opener in (("docx", docx._open_document), ("pptx", pptx._open_presentation)):
        try:
            opener(payload)
        except Exception:  # noqa: BLE001 - any failure: the library does not read it
            continue
        kinds.add(kind)
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            manifest = Manifest.from_tree(fromstring(archive.read("[Content_Types].xml")))
            part = xlsx._workbook_part_name(manifest)
            if part is not None and part in archive.namelist():
                kinds.add("xlsx")
    except Exception:  # noqa: BLE001 - as above
        pass
    return kinds


def _expected(payload: bytes) -> str:
    kinds = _library_kinds(payload)
    if len(kinds) > 1:
        return "ambiguous"
    if kinds:
        return kinds.pop()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = {name.lower() for name in archive.namelist()}
    return "ooxml" if "[content_types].xml" in names else "zip-other"


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_identification_names_the_extractor_the_libraries_select(shape):
    payload = SHAPES[shape]
    assert container_child.extract_text(payload) == (_expected(payload), [])


# The catalogue's answers written out, so the differential is not only
# checked against itself; the renamed shapes are what the conventional
# names got wrong (they gave ``ooxml``).
_HAND = {
    "docx": "docx",
    "xlsx": "xlsx",
    "pptx": "pptx",
    "docx-renamed": "docx",
    "docx-elsewhere": "docx",
    "xlsx-renamed": "xlsx",
    "pptx-renamed": "pptx",
    "docm": "ooxml",
    "docx-default-type": "docx",
    "docx-target-case": "docx",
    "pptx-target-case": "pptx",
    "docx-no-rels": "ooxml",
    "xlsx-no-rels": "xlsx",
    "docx-two-roots": "ooxml",
    "docx-external-root": "ooxml",
    "docx-strict-relationship": "ooxml",
    "docx-malformed-rels": "ooxml",
    "docx-no-content-types-member": "zip-other",
    "docx-content-types-lowercase-member": "ooxml",
    "docx-with-workbook": "ambiguous",
    "zip-no-content-types": "zip-other",
}


@pytest.mark.parametrize("shape", sorted(_HAND))
def test_the_catalogue_is_written_out_by_hand(shape):
    assert _expected(SHAPES[shape]) == _HAND[shape]


def test_the_main_types_are_the_extractors():
    """The child's copies of the extractors' rules, against the
    extractors and their libraries."""
    from docx.opc.constants import RELATIONSHIP_TYPE as DOCX_RT
    from openpyxl.xml.constants import XLSM, XLSX, XLTM, XLTX
    from pptx.opc.constants import RELATIONSHIP_TYPE as PPTX_RT
    from src.extractors import docx, pptx

    assert container_child._WORD_MAIN_TYPES == docx._WORD_MAIN_TYPES
    assert container_child._PRESENTATION_MAIN_TYPES == pptx._PRESENTATION_MAIN_TYPES
    assert container_child._WORKBOOK_TYPES == (XLTM, XLTX, XLSM, XLSX)
    assert container_child._OFFICE_DOCUMENT_REL == DOCX_RT.OFFICE_DOCUMENT
    assert container_child._OFFICE_DOCUMENT_REL == PPTX_RT.OFFICE_DOCUMENT
    assert container_child._OFFICE_DOCUMENT_REL == OFFICE_DOCUMENT_REL


# --------------------------------------------------------------------------
# DTDs and entities are refused, never expanded


_LAUGHS = (
    b'<?xml version="1.0"?><!DOCTYPE Types [<!ENTITY a "aaaaaaaaaa">'
    + b"".join(
        f'<!ENTITY {c} "{("&" + p + ";") * 10}">'.encode()
        for p, c in zip("abcdefgh", "bcdefghi", strict=True)
    )
    + b']><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">&i;</Types>'
)


@pytest.mark.parametrize("member", ["[Content_Types].xml", "_rels/.rels"])
def test_a_dtd_in_the_metadata_is_refused_unexpanded(member):
    """A DTD (an entity expansion here) makes the member unreadable, so no
    kind is named from it; nothing is expanded."""
    payload = SHAPES["docx"]
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members[member] = _LAUGHS
    started = time.monotonic()
    assert container_child.extract_text(make_package(members)) == ("ooxml", [])
    assert time.monotonic() - started < 2


# --------------------------------------------------------------------------
# The work guard over the metadata reads


def _deflated(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(
                zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED
            )
    return buffer.getvalue()


class _Spy:
    """Counts what the metadata reads take: bytes out of zipfile and
    elements given to the target."""

    def __init__(self, monkeypatch):
        self.bytes = 0
        self.elements = 0
        real_read = zipfile.ZipExtFile.read
        real_start = container_child._MetadataTarget.start
        spy = self

        def read(stream, n=-1):
            data = real_read(stream, n)
            spy.bytes += len(data)
            return data

        def start(target, tag, attrib):
            spy.elements += 1
            return real_start(target, tag, attrib)

        monkeypatch.setattr(zipfile.ZipExtFile, "read", read)
        monkeypatch.setattr(container_child._MetadataTarget, "start", start)


def test_decompressed_metadata_is_bounded_as_it_is_read(monkeypatch):
    """A content-types member that inflates past the cap (a deflate bomb
    of whitespace) is stopped once the cap is passed, not after the
    whole member is inflated."""
    spy = _Spy(monkeypatch)
    bomb = b"<Types>" + b" " * (64 * 1024 * 1024) + b"</Types>"
    payload = _deflated({"[Content_Types].xml": bomb, "_rels/.rels": ooxml_rels("/a.xml")})
    assert len(payload) < 1024 * 1024
    with pytest.raises(container_child.ContainerDirectoryBudgetError):
        container_child.extract_text(payload)
    limit = container_child._MAX_METADATA_BYTES
    assert limit < spy.bytes <= limit + container_child._METADATA_CHUNK


def test_a_member_that_understates_its_size_is_bounded(monkeypatch):
    """zipfile stops at the declared size, so a member whose header
    understates what it inflates to costs no more than it declares."""
    spy = _Spy(monkeypatch)
    payload = bytearray(
        _deflated({"[Content_Types].xml": b"<Types>" + b" " * (32 * 1024 * 1024) + b"</Types>"})
    )
    # Rewrite the declared uncompressed size, in the local header and the
    # central directory, to 100 bytes.
    real = (32 * 1024 * 1024 + 15).to_bytes(4, "little")
    assert payload.count(real) == 2
    payload = bytes(payload).replace(real, (100).to_bytes(4, "little"))
    started = time.monotonic()
    assert container_child.extract_text(payload) == ("ooxml", [])
    assert time.monotonic() - started < 2
    assert spy.bytes <= 100


def test_compressed_metadata_is_charged_before_it_is_read(monkeypatch):
    """A member whose compressed size is over the cap is refused before a
    byte of it is read."""
    monkeypatch.setattr(container_child, "_MAX_METADATA_BYTES", 1000)
    spy = _Spy(monkeypatch)
    payload = make_package(
        {"[Content_Types].xml": ooxml_content_types() + b" " * 2000, "_rels/.rels": b""}
    )
    with pytest.raises(container_child.ContainerDirectoryBudgetError):
        container_child.extract_text(payload)
    assert spy.bytes == 0


def test_the_bytes_of_both_members_are_counted_together(monkeypatch):
    content_types = ooxml_content_types({"/word/document.xml": WORD_MAIN_CT})
    rels = ooxml_rels("/word/document.xml")
    payload = make_package(
        {"[Content_Types].xml": content_types, "_rels/.rels": rels, "word/document.xml": b"x"}
    )
    monkeypatch.setattr(container_child, "_MAX_METADATA_BYTES", len(content_types) + len(rels))
    assert container_child.extract_text(payload) == ("docx", [])
    monkeypatch.setattr(container_child, "_MAX_METADATA_BYTES", len(content_types) + len(rels) - 1)
    with pytest.raises(container_child.ContainerDirectoryBudgetError):
        container_child.extract_text(payload)


def test_xml_elements_are_charged_as_they_are_parsed(monkeypatch):
    """Elements are counted as the parser starts them, across both
    members, and the parse stops one past the cap; the text between them
    is never kept."""
    monkeypatch.setattr(container_child, "_MAX_METADATA_ELEMENTS", 50)
    spy = _Spy(monkeypatch)
    many = {f"/p{i}.xml": "application/xml" for i in range(10_000)}
    payload = make_package({"[Content_Types].xml": ooxml_content_types(many)})
    with pytest.raises(container_child.ContainerDirectoryBudgetError):
        container_child.extract_text(payload)
    assert spy.elements == 51


def test_deep_nesting_is_charged_like_any_element(monkeypatch):
    spy = _Spy(monkeypatch)
    deep = b"<a>" * 200_000 + b"</a>" * 200_000
    payload = make_package({"[Content_Types].xml": deep})
    with pytest.raises(container_child.ContainerDirectoryBudgetError):
        container_child.extract_text(payload)
    assert spy.elements == container_child._MAX_METADATA_ELEMENTS + 1


def test_a_package_at_the_directory_budget_is_read():
    """A benign package listing a content type for every member up to
    the directory budget fits the metadata caps."""
    entries = container_child._MAX_DIRECTORY_ENTRIES - 3
    overrides = {
        f"/ppt/slides/slide{i}.xml": (
            "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
        )
        for i in range(entries)
    }
    overrides["/ppt/presentation.xml"] = PRESENTATION_MAIN_CT
    members = {
        "[Content_Types].xml": ooxml_content_types(overrides),
        "_rels/.rels": ooxml_rels("/ppt/presentation.xml"),
        "ppt/presentation.xml": _PRESENTATION_BODY,
    }
    members.update(dict.fromkeys((f"ppt/slides/slide{i}.xml" for i in range(entries)), b""))
    started = time.monotonic()
    assert container_child.extract_text(_deflated(members)) == ("pptx", [])
    assert time.monotonic() - started < 10


def test_expat_out_of_memory_is_a_limit_not_malformed_metadata(monkeypatch):
    """expat failing an allocation (the child's address-space limit) is
    reported as ``MemoryError``, so the row is ``failed`` by the limit,
    never read as a package with malformed metadata."""
    import xml.etree.ElementTree as ET
    from xml.parsers.expat import errors

    def out_of_memory(self, data):
        error = ET.ParseError("out of memory")
        error.code = errors.codes[errors.XML_ERROR_NO_MEMORY]
        raise error

    monkeypatch.setattr(container_child.DefusedXMLParser, "feed", out_of_memory)
    with pytest.raises(MemoryError):
        container_child.extract_text(SHAPES["docx"])


def test_a_metadata_bomb_is_failed_by_the_real_child():
    """Through the real child process under its limits: a deflate bomb in
    the content types fails identification by type name, in bounded
    time."""
    from src.extractors import container
    from src.extractors._runner import ChildError

    bomb = b"<Types>" + b" " * (64 * 1024 * 1024) + b"</Types>"
    payload = _deflated({"[Content_Types].xml": bomb, "_rels/.rels": ooxml_rels("/a.xml")})
    started = time.monotonic()
    with pytest.raises(ChildError) as raised:
        container.identify(payload)
    assert time.monotonic() - started < 10
    assert raised.value.type_name == "ContainerDirectoryBudgetError"
