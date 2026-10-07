"""#980: committed Office fixtures must carry synthetic author metadata.

Office applications write the creating user into a file's metadata:
the OLE2 SummaryInformation properties Author and Last Saved By for
``.doc`` / ``.xls`` / ``.ppt``, ``docProps/core.xml`` creator and
lastModifiedBy for OOXML, and the ``office:meta`` creator fields for
ODF. The repository is public, so this module walks every Office file
under the indexer and mcp-server test trees and requires each of those
fields to be empty or a name on ``_SYNTHETIC_AUTHORS``.

Fixtures built at test time live in ``tmp_path`` and are never
committed, so the walk does not see them. Other places a name can hide
(the PowerPoint Current User stream, Word's associated-strings table,
DocumentSummaryInformation) are not read here.

It lives in the indexer suite because CI has no repo-root pytest job; it
reads the mcp-server's test tree by path. Failure messages name the
file and the field, never the value, so a real name is not copied into
the CI log."""

import shutil
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
_META = "{urn:oasis:names:tc:opendocument:xmlns:meta:1.0}"
_OOXML_FIELDS = {"creator": f"{_DC}creator", "lastModifiedBy": f"{_CP}lastModifiedBy"}
_ODF_FIELDS = {"initial-creator": f"{_META}initial-creator", "creator": f"{_DC}creator"}


def _office_files(roots: tuple[Path, ...]) -> list[Path]:
    """Every file under ``roots`` whose extension is an Office format."""
    extensions = _OLE2 | _OOXML | _ODF_ZIP | _ODF_FLAT
    return sorted(p for root in roots for p in root.rglob("*") if p.suffix.lower() in extensions)


def _ole2_fields(path: Path) -> dict[str, str]:
    with olefile.OleFileIO(str(path)) as ole:
        meta = ole.get_metadata()
    # SummaryInformation strings are bytes in the property set's code page.
    # PowerPoint for Mac writes code page 10000 (Mac Roman).
    codepage = meta.codepage or 1252
    encoding = _CODEPAGES.get(codepage, f"cp{codepage}")
    fields = {}
    for name, value in (("Author", meta.author), ("Last Saved By", meta.last_saved_by)):
        if isinstance(value, bytes):
            value = value.decode(encoding, errors="replace")
        fields[name] = value or ""
    return fields


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
        core = _zip_member(path, "docProps/core.xml")
        return _xml_fields(core, _OOXML_FIELDS) if core is not None else {}
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


def _ooxml(path: Path, core: bytes | None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        if core is not None:
            archive.writestr("docProps/core.xml", core)
    return path


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
