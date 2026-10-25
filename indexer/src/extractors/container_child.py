"""Container identification, run in the extractor child (#1416).

``container.identify`` runs it as
``python -I extractor_child.py container <payload file>``, through the
runner's launcher (``_launcher.py``), which has lowered the child's
address-space and CPU limits first. It reads only a container's
directory and names the one extractor that reads the payload, without
extracting anything:

* OLE2 compound file: olefile reads the header, the FAT and the
  directory; the root storage's stream names decide (a storage's name
  never does). ``WordDocument``
  is ``doc``, ``Workbook`` or ``Book`` (BIFF5) is ``xls``,
  ``PowerPoint Document`` is ``ppt``. ``EncryptionInfo`` with
  ``EncryptedPackage`` is an encrypted OOXML file, which no extractor
  reads. No stream stays nothing, and more than one of the four kinds
  (the encrypted pair counting as one) stays ambiguous: neither is
  guessed.
* ZIP: zipfile reads the central directory. An archive with no
  ``[Content_Types].xml`` (under any case) is not an OOXML package. For
  a package, the two OPC metadata members, ``[Content_Types].xml`` and
  ``_rels/.rels``, are read and parsed with defusedxml (no DTD, no
  entities), and the extractors' own rules decide (owner decision on
  #1416, 2026-10-10): ``docx`` or ``pptx`` when the single root
  ``officeDocument`` relationship targets a member whose content type
  (its Override, else the Default for its extension) is a Word or
  PowerPoint main type that extractor reads, ``xlsx`` when openpyxl's
  workbook lookup in the content types finds a member. No other member
  is opened. A package naming none is ``ooxml`` (the dispatcher keeps
  the label's OOXML extractor, if it selects one), more than one is
  ambiguous, and missing or malformed metadata names none.

One guard bounds the work (``_charge``), over every dimension the read
costs:

* directory entries: at most ``_MAX_DIRECTORY_ENTRIES``, above the
  largest member budget an OOXML extractor applies, so that extractor's
  budget still decides a package between the two;
* bytes inspected: for OLE2, the allocation tables (FAT and DIFAT
  sectors) the header declares may not exceed the file's own sectors,
  and the directory is at most ``_MAX_DIRECTORY_ENTRIES`` entries of 128
  bytes; for ZIP, the central directory, which the payload cap bounds;
* decompression: only the two metadata members, at most
  ``_MAX_METADATA_BYTES`` compressed (their declared compressed sizes,
  each charged just before it is read) and ``_MAX_METADATA_BYTES``
  decompressed together, counted as each chunk comes out of zipfile, so
  a member that understates its size is bounded too;
* XML: at most ``_MAX_METADATA_ELEMENTS`` elements across both members,
  charged as the parser starts each, with no text kept; a DTD or entity
  is refused;
* ambiguity: one pass over the root stream names or the metadata
  entries, and a container naming more than one document kind is
  reported, not resolved.

For OLE2, olefile is hooked (``_BoundedOleFile``): ``loadfat`` checks
the declared table size before any table sector is read, and
``loaddirectory`` counts the directory stream's sector chain in the FAT
olefile already loaded, so an over-budget or looping chain is rejected
before the stream is read, the entry list allocated or the storage tree
built. For ZIP, the entries are counted once zipfile has read the
central directory. The child's address-space and CPU limits bound what
the guard does not see (zipfile's list for a central directory of the
payload's full size, and the attributes of one element, which expat
reads whole before the parser reports the element; both measured in
the image). expat running out of memory is reported as ``MemoryError``,
never as malformed metadata.

The result is one fixed token (``TOKENS``); an over-budget directory
raises ``ContainerDirectoryBudgetError``, and any other error (a
malformed container, olefile's ``RecursionError`` on a degenerate
sibling chain, a limit) is reported by type name: the parent records a
``failed`` row.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
import zlib
from typing import Any
from xml.parsers.expat import errors as expat_errors

import olefile
from defusedxml.ElementTree import DefusedXMLParser

from . import has_container_prefix
from .container import (
    AMBIGUOUS,
    DOC,
    DOCX,
    NOT_CONTAINER,
    OLE2_ENCRYPTED,
    OLE2_OTHER,
    OLE2_SIGNATURE,
    OOXML_UNKNOWN,
    PPT,
    PPTX,
    XLS,
    XLSX,
    ZIP_OTHER,
)

# Directory entries read at most: an OLE2 directory entry is 128 bytes,
# a ZIP central-directory entry one member. Two and a half times the
# largest member budget an OOXML extractor applies (``pptx``'s 20,000),
# so a package between the two reaches that extractor, whose own budget
# records it ``unsupported``; only one past this is a ``failed`` limit hit.
_MAX_DIRECTORY_ENTRIES = 50_000
_OLE2_ENTRY_BYTES = 128

# Root streams that name the legacy format (lowercased: OLE2 names
# compare case-insensitively).
_OLE2_STREAMS = {
    "worddocument": DOC,
    "workbook": XLS,
    "book": XLS,
    "powerpoint document": PPT,
}
_OLE2_ENCRYPTION_STREAMS = frozenset({"encryptioninfo", "encryptedpackage"})

# An OOXML package's content-types member, under any case: an archive
# without one is not a package.
_OOXML_CONTENT_TYPES = "[content_types].xml"

# The two OPC metadata members identification reads (owner decision on
# #1416, 2026-10-10), by the exact names the extractors' libraries read
# (python-docx, python-pptx, openpyxl): the content types and the
# package relationships.
_CONTENT_TYPES_MEMBER = "[Content_Types].xml"
_PACKAGE_RELS_MEMBER = "_rels/.rels"
_CONTENT_TYPES_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
_RELATIONSHIPS_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_OFFICE_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)

# The main-part content types each OOXML extractor reads
# (``docx._WORD_MAIN_TYPES``, ``pptx._PRESENTATION_MAIN_TYPES``) and the
# workbook types openpyxl looks for, in its order
# (``xlsx._workbook_part_name``); written out so the child does not import
# the libraries, and checked against them by
# ``test_the_main_types_are_the_extractors``.
_WORD_MAIN_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
    }
)
_PRESENTATION_MAIN_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
        "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml",
        "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml",
        "application/vnd.ms-powerpoint.template.macroEnabled.main+xml",
    }
)
_WORKBOOK_TYPES = (
    "application/vnd.ms-excel.template.macroEnabled.main+xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
    "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
)
# The workbook openpyxl reads when only a Default names a workbook type.
_DEFAULT_WORKBOOK = "xl/workbook.xml"

# What reading the two metadata members may cost together (owner decision
# on #1416): compressed bytes (their declared compressed sizes, past
# which zipfile reads nothing), bytes decompressed (counted as they come
# out, so a member that understates its size is still bounded), and XML
# elements parsed. A package at the 50,000-entry directory budget lists
# at most one content-types entry per member: about 6.6 MB and 50,000
# elements with Office's part names, inside both caps.
_MAX_METADATA_BYTES = 8 * 1024 * 1024
_MAX_METADATA_ELEMENTS = 100_000
# expat's error code for a failed allocation.
_EXPAT_NO_MEMORY = expat_errors.codes[expat_errors.XML_ERROR_NO_MEMORY]

# Bytes taken from a member per read: zipfile decompresses at most about
# this much per call.
_METADATA_CHUNK = 64 * 1024


class ContainerDirectoryBudgetError(Exception):
    """The container's directory is over the work guard (``_charge``)."""


def _charge(
    *,
    entries: int = 0,
    table_sectors: int = 0,
    file_sectors: int = 0,
    metadata_compressed: int = 0,
    metadata_bytes: int = 0,
    metadata_elements: int = 0,
) -> None:
    """The one work guard (#1416): the directory ``entries`` a read
    would hold, at most ``_MAX_DIRECTORY_ENTRIES``; the allocation
    ``table_sectors`` an OLE2 header declares, at most the file's own
    ``file_sectors``; and for an OOXML package's metadata members, the
    compressed bytes and decompressed bytes read and XML elements parsed
    so far, at most ``_MAX_METADATA_BYTES``, ``_MAX_METADATA_BYTES`` and
    ``_MAX_METADATA_ELEMENTS``."""
    if (
        entries > _MAX_DIRECTORY_ENTRIES
        or table_sectors > file_sectors
        or metadata_compressed > _MAX_METADATA_BYTES
        or metadata_bytes > _MAX_METADATA_BYTES
        or metadata_elements > _MAX_METADATA_ELEMENTS
    ):
        raise ContainerDirectoryBudgetError


class _BoundedOleFile(olefile.OleFileIO):
    """olefile with its table and directory loads bounded (``_charge``)
    before they read."""

    def loadfat(self, header: bytes) -> None:
        # Every FAT and DIFAT sector is a sector of the file: a header
        # declaring more would have olefile read and expand sectors past
        # the payload's size (measured: a 32 MiB file declaring 2^31 FAT
        # sectors ran until the CPU limit).
        _charge(
            table_sectors=self.num_fat_sectors + self.num_difat_sectors,
            file_sectors=self.nb_sect + 1,
        )
        super().loadfat(header)

    def loaddirectory(self, sect: int) -> None:
        start = sect
        sectors = 0
        fat = self.fat
        while sect != olefile.ENDOFCHAIN:
            sectors += 1
            # A chain that loops never reaches ENDOFCHAIN, so it ends here.
            _charge(entries=sectors * self.sectorsize // _OLE2_ENTRY_BYTES)
            if not 0 <= sect < len(fat):
                # olefile reports an out-of-range index itself.
                break
            sect = fat[sect]
        super().loaddirectory(start)


def extract_text(payload: bytes) -> tuple[str, list[str]]:
    """The identification token for ``payload`` and no cap names. Runs in
    the child process (``extractor_child``)."""
    if payload.startswith(OLE2_SIGNATURE):
        return _identify_ole2(payload), []
    if not has_container_prefix(payload) and not zipfile.is_zipfile(io.BytesIO(payload)):
        return NOT_CONTAINER, []
    return _identify_zip(payload), []


def _identify_ole2(payload: bytes) -> str:
    # A file object, never the bytes: olefile reads bytes shorter than
    # 1,536 as a file name.
    with _BoundedOleFile(io.BytesIO(payload)) as ole:
        # Streams only: a storage named like a format's stream names
        # nothing (#1444).
        names = {_ole2_name(kid) for kid in ole.root.kids if kid.entry_type == olefile.STGTY_STREAM}
    kinds = {_OLE2_STREAMS[name] for name in names if name in _OLE2_STREAMS}
    # An encrypted package is a kind of its own: beside a legacy stream it
    # makes the container ambiguous, so a decoy stream is never read
    # instead of it (#1444).
    if _OLE2_ENCRYPTION_STREAMS <= names:
        kinds.add(OLE2_ENCRYPTED)
    if len(kinds) > 1:
        return AMBIGUOUS
    if kinds:
        return kinds.pop()
    return OLE2_OTHER


def _ole2_name(entry: Any) -> str:
    return str(entry.name).lower()


def _identify_zip(payload: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = archive.infolist()
        _charge(entries=len(members))
        if _OOXML_CONTENT_TYPES not in {info.filename.lower() for info in members}:
            return ZIP_OTHER
        budget = _MetadataBudget()
        content_types = _read_metadata(archive, _CONTENT_TYPES_MEMBER, budget)
        rels = _read_metadata(archive, _PACKAGE_RELS_MEMBER, budget)
        names = {info.filename for info in members}
    kinds = _ooxml_kinds(content_types, rels, names)
    if len(kinds) > 1:
        return AMBIGUOUS
    if kinds:
        return kinds.pop()
    return OOXML_UNKNOWN


class _MetadataBudget:
    """The metadata reads' running totals, charged to ``_charge``."""

    def __init__(self) -> None:
        self.compressed = 0
        self.decompressed = 0
        self.elements = 0

    def charge(self, *, compressed: int = 0, decompressed: int = 0, elements: int = 0) -> None:
        self.compressed += compressed
        self.decompressed += decompressed
        self.elements += elements
        _charge(
            metadata_compressed=self.compressed,
            metadata_bytes=self.decompressed,
            metadata_elements=self.elements,
        )


# A metadata member's root element: its tag and the tag and attributes of
# each of its children, the only level OPC metadata has.
_Metadata = tuple[str, list[tuple[str, dict[str, str]]]]


class _MetadataTarget:
    """An XML parser target keeping the root's tag and its children's tags
    and attributes, and charging every element to the budget as it
    starts. No text is kept."""

    def __init__(self, budget: _MetadataBudget) -> None:
        self._budget = budget
        self._depth = 0
        self.root = ""
        self.children: list[tuple[str, dict[str, str]]] = []

    def start(self, tag: str, attrib: dict[str, str]) -> None:
        self._budget.charge(elements=1)
        if self._depth == 0:
            self.root = tag
        elif self._depth == 1:
            self.children.append((tag, dict(attrib)))
        self._depth += 1

    def end(self, tag: str) -> None:
        self._depth -= 1

    def close(self) -> _Metadata:
        return self.root, self.children


def _read_metadata(
    archive: zipfile.ZipFile, name: str, budget: _MetadataBudget
) -> _Metadata | None:
    """The metadata member ``name`` parsed, or ``None`` when it is absent,
    encrypted or malformed (a library reading it would fail too). Parsed
    with defusedxml, refusing a DTD and entities, as it is decompressed,
    chunk by chunk under ``budget``, which raises
    ``ContainerDirectoryBudgetError`` past its caps."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.flag_bits & 0x1:
        # Encrypted: zipfile would need a password.
        return None
    budget.charge(compressed=info.compress_size)
    target = _MetadataTarget(budget)
    parser = DefusedXMLParser(target=target, forbid_dtd=True)
    try:
        with archive.open(info) as stream:
            while chunk := stream.read(_METADATA_CHUNK):
                budget.charge(decompressed=len(chunk))
                parser.feed(chunk)
        return parser.close()
    except SyntaxError as exc:
        # XML that does not parse (``ParseError``), unless expat ran out
        # of memory: that is the child's address-space limit, reported as
        # such, never read as malformed metadata.
        if getattr(exc, "code", None) == _EXPAT_NO_MEMORY:
            raise MemoryError from None
        return None
    except zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, ValueError:
        # Malformed: a corrupt or unsupported member (zipfile, zlib), or a
        # DTD or entity defusedxml refuses (a ``ValueError``).
        return None


def _ooxml_kinds(
    content_types: _Metadata | None, rels: _Metadata | None, names: set[str]
) -> set[str]:
    """The OOXML extractors that would read the package, by their own
    rules (owner decision on #1416, 2026-10-10): ``docx`` and ``pptx``
    when the single root ``officeDocument`` relationship targets a member
    whose content type is one of their main types, ``xlsx`` when
    openpyxl's workbook lookup in the content types finds a member."""
    if content_types is None:
        return set()
    kinds: set[str] = set()
    if _workbook_part(content_types) in names:
        kinds.add(XLSX)
    main = _main_part(rels)
    if main is not None and main[1:] in names:
        if _content_type(content_types, main, untyped_ok=True) in _WORD_MAIN_TYPES:
            kinds.add(DOCX)
        if _content_type(content_types, main, untyped_ok=False) in _PRESENTATION_MAIN_TYPES:
            kinds.add(PPTX)
    return kinds


def _workbook_part(content_types: _Metadata) -> str | None:
    """The workbook member openpyxl reads (``_find_workbook_part``): the
    first Override of each workbook type in turn, else the conventional
    workbook when a Default carries one of them."""
    _, children = content_types
    overrides = [a for tag, a in children if tag == _CONTENT_TYPES_NS + "Override"]
    for workbook_type in _WORKBOOK_TYPES:
        for attrib in overrides:
            if attrib.get("ContentType") == workbook_type:
                return attrib.get("PartName", "")[1:]
    defaults = {a.get("ContentType") for tag, a in children if tag == _CONTENT_TYPES_NS + "Default"}
    if defaults & set(_WORKBOOK_TYPES):
        return _DEFAULT_WORKBOOK
    return None


def _main_part(rels: _Metadata | None) -> str | None:
    """The part name (``/word/document.xml``) the package's single root
    ``officeDocument`` relationship targets, resolved as python-docx and
    python-pptx resolve it, or ``None`` when there is none, more than one
    or an external one."""
    if rels is None:
        return None
    root, children = rels
    if root != _RELATIONSHIPS_NS + "Relationships":
        return None
    matching = [
        a
        for tag, a in children
        if tag == _RELATIONSHIPS_NS + "Relationship" and a.get("Type") == _OFFICE_DOCUMENT_REL
    ]
    if len(matching) != 1:
        return None
    [attrib] = matching
    target = attrib.get("Target")
    if target is None or attrib.get("TargetMode") == "External":
        return None
    return posixpath.normpath(posixpath.join("/", target))


def _content_type(content_types: _Metadata, part: str, *, untyped_ok: bool) -> str | None:
    """The content type of ``part`` (an Override for its name, else the
    Default for its extension), or ``None``. Both keys compare
    case-insensitively, as python-docx and python-pptx compare them; a
    later entry for the same key replaces an earlier one. Both libraries
    reject the content types when an entry has no name; python-docx
    accepts one with no content type (``untyped_ok``), python-pptx does
    not."""
    root, children = content_types
    if root != _CONTENT_TYPES_NS + "Types":
        return None
    overrides: dict[str, str] = {}
    defaults: dict[str, str] = {}
    for tag, attrib in children:
        if tag == _CONTENT_TYPES_NS + "Override":
            name, value = attrib.get("PartName"), attrib.get("ContentType")
            store = overrides
        elif tag == _CONTENT_TYPES_NS + "Default":
            name, value = attrib.get("Extension"), attrib.get("ContentType")
            store = defaults
        else:
            continue
        if name is None or (value is None and not untyped_ok):
            return None
        if value is not None:
            store[name.lower()] = value
    raw_extension = posixpath.splitext(part)[1]
    extension = raw_extension[1:] if raw_extension.startswith(".") else raw_extension
    if part.lower() in overrides:
        return overrides[part.lower()]
    return defaults.get(extension.lower())
