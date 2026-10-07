"""#980: committed Office fixtures must carry synthetic author metadata.

Office applications write the creating user and organisation into a
file's metadata: the OLE2 SummaryInformation properties Author and Last
Saved By and DocumentSummaryInformation Company and Manager for
``.doc`` / ``.xls`` / ``.ppt``, ``docProps/core.xml`` creator and
lastModifiedBy and ``docProps/app.xml`` Company and Manager for OOXML,
and the ``office:meta`` creator fields for ODF. The repository is
public, so this module walks every Office file under the indexer and
mcp-server test trees and requires each of those fields to be empty or
a name on ``_SYNTHETIC_AUTHORS``.

Fixtures built at test time live in ``tmp_path`` and are never
committed, so the walk does not see them. Names kept in binary records
are not read, because reading them would mean hand-parsing the record
(#1010, option 3): the PowerPoint Current User stream, Word's
associated-strings table and its revision and comment author tables,
and OOXML comment and revision authors. Check those by hand before
committing a fixture.

It lives in the indexer suite because CI has no repo-root pytest job; it
reads the mcp-server's test tree by path. Failure messages name the
file and the field, never the value, so a real name is not copied into
the CI log."""

import shutil
import struct
import zipfile
from pathlib import Path

import olefile
import pytest
from defusedxml import ElementTree

_REPO = Path(__file__).resolve().parents[2]
_TEST_ROOTS = (_REPO / "indexer" / "tests", _REPO / "mcp-server" / "tests")
_FIXTURES = Path(__file__).parent / "fixtures" / "extractors"

# Synthetic names a fixture may carry. Never add a real person's name.
_SYNTHETIC_AUTHORS = frozenset({"Synthetic Author"})

_OLE2 = frozenset({".doc", ".dot", ".xls", ".xlt", ".ppt", ".pot", ".pps"})
_OOXML = frozenset(
    {
        ".docx",
        ".docm",
        ".dotx",
        ".dotm",
        ".xlsx",
        ".xlsm",
        ".xltx",
        ".xltm",
        ".pptx",
        ".pptm",
        ".potx",
        ".potm",
        ".ppsx",
        ".ppsm",
    }
)
_ODF_ZIP = frozenset({".odt", ".ods", ".odp"})
_ODF_FLAT = frozenset({".fodt", ".fods", ".fodp"})

_CODEPAGES = {1200: "utf-16-le", 10000: "mac_roman", 65001: "utf-8"}
_DC = "{http://purl.org/dc/elements/1.1/}"
_CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
_EP = "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}"
_META = "{urn:oasis:names:tc:opendocument:xmlns:meta:1.0}"
_OOXML_FIELDS = {"creator": f"{_DC}creator", "lastModifiedBy": f"{_CP}lastModifiedBy"}
_OOXML_APP_FIELDS = {"Company": f"{_EP}Company", "Manager": f"{_EP}Manager"}
_ODF_FIELDS = {"initial-creator": f"{_META}initial-creator", "creator": f"{_DC}creator"}


def _office_files(roots: tuple[Path, ...]) -> list[Path]:
    """Every file under ``roots`` whose extension is an Office format."""
    extensions = _OLE2 | _OOXML | _ODF_ZIP | _ODF_FLAT
    return sorted(p for root in roots for p in root.rglob("*") if p.suffix.lower() in extensions)


def _decode(value: object, codepage: int | None) -> str:
    """A property-set string, decoded from bytes in its set's code page."""
    if not isinstance(value, bytes):
        return str(value or "")
    # olefile reads the code page as a signed 16-bit value, so UTF-8
    # (65001) arrives as -535. PowerPoint for Mac writes 10000 (Mac Roman).
    codepage = (codepage or 1252) & 0xFFFF
    return value.decode(_CODEPAGES.get(codepage, f"cp{codepage}"), errors="replace")


def _ole2_fields(path: Path) -> dict[str, str]:
    with olefile.OleFileIO(str(path)) as ole:
        meta = ole.get_metadata()
    # Strings are bytes in their own property set's code page.
    return {
        "Author": _decode(meta.author, meta.codepage),
        "Last Saved By": _decode(meta.last_saved_by, meta.codepage),
        "Company": _decode(meta.company, meta.codepage_doc),
        "Manager": _decode(meta.manager, meta.codepage_doc),
    }


def _xml_fields(xml: bytes, fields: dict[str, str]) -> dict[str, str]:
    root = ElementTree.fromstring(xml)
    found: dict[str, str] = {}
    for name, tag in fields.items():
        # A field may repeat; join them so every value is checked.
        found[name] = "\n".join(el.text or "" for el in root.iter(tag))
    return found


def _zip_member(path: Path, member: str) -> bytes | None:
    with zipfile.ZipFile(path) as archive:
        if member not in archive.namelist():
            return None
        return archive.read(member)


def _author_fields(path: Path) -> dict[str, str]:
    """The author fields of an Office file, by field name ("" when absent)."""
    suffix = path.suffix.lower()
    if suffix in _OLE2:
        return _ole2_fields(path)
    if suffix in _OOXML:
        fields: dict[str, str] = {}
        core = _zip_member(path, "docProps/core.xml")
        if core is not None:
            fields |= _xml_fields(core, _OOXML_FIELDS)
        app = _zip_member(path, "docProps/app.xml")
        if app is not None:
            fields |= _xml_fields(app, _OOXML_APP_FIELDS)
        return fields
    if suffix in _ODF_ZIP:
        meta = _zip_member(path, "meta.xml")
        return _xml_fields(meta, _ODF_FIELDS) if meta is not None else {}
    return _xml_fields(path.read_bytes(), _ODF_FIELDS)


def _violations(path: Path) -> list[str]:
    """Names of the fields holding something other than empty or a synthetic name."""
    bad = []
    for field, value in _author_fields(path).items():
        for line in value.split("\n"):
            name = line.strip(" \x00")
            if name and name not in _SYNTHETIC_AUTHORS:
                bad.append(field)
                break
    return bad


def _assert_synthetic(path: Path) -> None:
    bad = _violations(path)
    assert bad == [], (
        f"{path.name}: {', '.join(bad)} is not empty or a synthetic name; regenerate "
        "or scrub it (see indexer/tests/fixtures/extractors/README.md)"
    )


def test_walk_finds_the_committed_legacy_fixtures():
    found = _office_files(_TEST_ROOTS)
    assert all(root.is_dir() for root in _TEST_ROOTS)
    for name in ("legacy.doc", "legacy.xls", "legacy.ppt", "legacy-lo.ppt"):
        assert _FIXTURES / name in found
    assert _FIXTURES / "legacy-src" / "legacy-xls.fods" in found


@pytest.mark.parametrize(
    "path",
    _office_files(_TEST_ROOTS),
    ids=lambda p: str(p.relative_to(_REPO)),
)
def test_committed_office_fixture_has_synthetic_authors(path: Path):
    _assert_synthetic(path)


def test_ole2_fields_read_the_powerpoint_deck():
    # legacy.ppt is the one fixture with names set, so the reader is
    # pinned against real values rather than only empty ones.
    fields = _author_fields(_FIXTURES / "legacy.ppt")
    assert fields["Author"] == "Synthetic Author"
    assert fields["Last Saved By"].strip() == "Synthetic Author"


def test_check_rejects_an_ole2_author_off_the_allowlist(tmp_path: Path):
    deck = tmp_path / "copy.ppt"
    shutil.copyfile(_FIXTURES / "legacy.ppt", deck)
    # Same length as "Synthetic Author", so the stream size does not move.
    with olefile.OleFileIO(str(deck), write_mode=True) as ole:
        data = ole.openstream("\x05SummaryInformation").read()
        assert data.count(b"Synthetic Author") == 2
        ole.write_stream(
            "\x05SummaryInformation", data.replace(b"Synthetic Author", b"Fictional Writer")
        )

    assert _office_files((tmp_path,)) == [deck]
    assert _violations(deck) == ["Author", "Last Saved By"]


def _swap_property_ids(stream: bytes, first: int, second: int) -> bytes:
    """``stream`` with properties ``first`` and ``second`` trading IDs.

    Rewrites the ID column of the first section's ID/offset table only,
    so the stream keeps its size. This edits a committed synthetic
    fixture in a test; the check itself does not parse records."""
    data = bytearray(stream)
    # Header (28 bytes), then the first section's FMTID (16) and offset (4).
    (section,) = struct.unpack_from("<I", data, 44)
    (count,) = struct.unpack_from("<I", data, section + 4)
    for entry in range(section + 8, section + 8 + 8 * count, 8):
        (pid,) = struct.unpack_from("<I", data, entry)
        if pid in (first, second):
            struct.pack_into("<I", data, entry, second if pid == first else first)
    return bytes(data)


@pytest.mark.parametrize(("pid", "field"), [(14, "Manager"), (15, "Company")])
def test_check_rejects_an_ole2_company_or_manager_off_the_allowlist(
    tmp_path: Path, pid: int, field: str
):
    deck = tmp_path / "copy.ppt"
    shutil.copyfile(_FIXTURES / "legacy.ppt", deck)
    stream = "\x05DocumentSummaryInformation"
    with olefile.OleFileIO(str(deck)) as ole:
        before = ole.getproperties(stream)
    # The deck's only non-empty DocumentSummaryInformation string is its
    # presentation format (property 3); give that value the field's ID.
    assert before[3] == b"Widescreen"
    assert not before.get(14) and not before.get(15)
    with olefile.OleFileIO(str(deck), write_mode=True) as ole:
        data = ole.openstream(stream).read()
        ole.write_stream(stream, _swap_property_ids(data, 3, pid))

    assert _author_fields(deck)[field] == "Widescreen"
    assert _violations(deck) == [field]


def test_decode_reads_the_utf8_code_page_olefile_reports_as_signed():
    # LibreOffice writes code page 65001; olefile returns it as -535.
    with olefile.OleFileIO(str(_FIXTURES / "legacy.doc")) as ole:
        assert ole.get_metadata().codepage_doc == -535
    assert _decode("Café".encode(), -535) == "Café"
    assert _decode("Café".encode("mac_roman"), 10000) == "Café"
    assert _decode(None, None) == ""


def test_ole2_fields_read_company_and_manager_as_empty_on_the_committed_fixtures():
    for name in ("legacy.doc", "legacy.xls", "legacy.ppt", "legacy-lo.ppt"):
        fields = _author_fields(_FIXTURES / name)
        assert (fields["Company"], fields["Manager"]) == ("", ""), name


def _ooxml(path: Path, core: bytes | None, app: bytes | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        if core is not None:
            archive.writestr("docProps/core.xml", core)
        if app is not None:
            archive.writestr("docProps/app.xml", app)
    return path


def _app(company: str, manager: str) -> bytes:
    return (
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
        'extended-properties"><Application>Synthetic</Application>'
        f"<Company>{company}</Company><Manager>{manager}</Manager></Properties>"
    ).encode()


def test_check_rejects_an_ooxml_company_or_manager_off_the_allowlist(tmp_path: Path):
    core = _core("Synthetic Author", "")
    company = _ooxml(tmp_path / "company.docx", core, _app("Fictional Holdings", ""))
    manager = _ooxml(tmp_path / "manager.xlsx", core, _app("", "Fictional Boss"))
    good = _ooxml(tmp_path / "good.pptx", core, _app("Synthetic Author", ""))
    app_only = _ooxml(tmp_path / "app-only.docx", None, _app("Fictional Holdings", ""))

    assert _violations(company) == ["Company"]
    assert _violations(manager) == ["Manager"]
    assert _violations(good) == []
    assert _violations(app_only) == ["Company"]


def test_failure_message_does_not_quote_the_company(tmp_path: Path):
    bad = _ooxml(tmp_path / "bad.docx", None, _app("MARKER-1010-COMPANY", ""))
    with pytest.raises(AssertionError) as excinfo:
        _assert_synthetic(bad)
    assert "Company" in str(excinfo.value)
    assert "MARKER-1010-COMPANY" not in str(excinfo.value)


def _core(creator: str, last: str) -> bytes:
    return (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
        'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:creator>{creator}</dc:creator><cp:lastModifiedBy>{last}</cp:lastModifiedBy>"
        "</cp:coreProperties>"
    ).encode()


def test_check_rejects_an_ooxml_author_off_the_allowlist(tmp_path: Path):
    bad = _ooxml(tmp_path / "bad.docx", _core("Fictional Writer", "Synthetic Author"))
    good = _ooxml(tmp_path / "good.xlsx", _core("Synthetic Author", ""))
    bare = _ooxml(tmp_path / "bare.pptx", None)

    assert _office_files((tmp_path,)) == [bad, bare, good]
    assert _violations(bad) == ["creator"]
    assert _violations(good) == []
    assert _author_fields(bare) == {}


def test_check_rejects_an_odf_author_off_the_allowlist(tmp_path: Path):
    meta = (
        b'<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
        b' xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0"'
        b' xmlns:dc="http://purl.org/dc/elements/1.1/"><office:meta>'
        b"<meta:initial-creator>Synthetic Author</meta:initial-creator>"
        b"<dc:creator>Fictional Writer</dc:creator></office:meta></office:document-meta>"
    )
    flat = tmp_path / "bad.fodt"
    flat.write_bytes(meta)
    packed = tmp_path / "bad.odt"
    with zipfile.ZipFile(packed, "w") as archive:
        archive.writestr("meta.xml", meta)

    assert _violations(flat) == ["creator"]
    assert _violations(packed) == ["creator"]


def test_failure_message_does_not_quote_the_name(tmp_path: Path):
    bad = _ooxml(tmp_path / "bad.docx", _core("MARKER-980-NAME", ""))
    with pytest.raises(AssertionError) as excinfo:
        _assert_synthetic(bad)
    assert "creator" in str(excinfo.value)
    assert "MARKER-980-NAME" not in str(excinfo.value)
