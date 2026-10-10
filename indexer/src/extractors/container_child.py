"""Container identification, run in the extractor child (#1416).

``container.identify`` runs it as
``python -I extractor_child.py container <payload file>``, through the
runner's launcher (``_launcher.py``), which has lowered the child's
address-space and CPU limits first. It reads only a container's
directory and names the one extractor that reads the payload, without
extracting anything:

* OLE2 compound file: olefile reads the header, the FAT and the
  directory; the root storage's stream names decide. ``WordDocument``
  is ``doc``, ``Workbook`` or ``Book`` (BIFF5) is ``xls``,
  ``PowerPoint Document`` is ``ppt``. ``EncryptionInfo`` with
  ``EncryptedPackage`` is an encrypted OOXML file, which no extractor
  reads. No stream stays nothing, more than one of the three stays
  ambiguous: neither is guessed.
* ZIP: zipfile reads the central directory; no member is opened or
  decompressed. An OOXML package has ``[Content_Types].xml``, and its
  main part's name decides: ``word/document.xml`` is ``docx``,
  ``xl/workbook.xml`` is ``xlsx``, ``ppt/presentation.xml`` is ``pptx``.
  A package with none of them is ``ooxml`` (the dispatcher keeps the
  label's OOXML extractor, if it selects one), more than one is
  ambiguous, and an archive with no ``[Content_Types].xml`` is not an
  OOXML package.

One guard bounds the work (``_charge``), over every dimension the read
costs:

* directory entries: at most ``_MAX_DIRECTORY_ENTRIES``, above the
  largest member budget an OOXML extractor applies, so that extractor's
  budget still decides a package between the two;
* bytes inspected: for OLE2, the allocation tables (FAT and DIFAT
  sectors) the header declares may not exceed the file's own sectors,
  and the directory is at most ``_MAX_DIRECTORY_ENTRIES`` entries of 128
  bytes; for ZIP, the central directory, which the payload cap bounds;
* decompression: none, no member or stream is opened;
* ambiguity: one pass over the root stream names or the member names,
  and a container naming more than one document kind is reported, not
  resolved.

For OLE2, olefile is hooked (``_BoundedOleFile``): ``loadfat`` checks
the declared table size before any table sector is read, and
``loaddirectory`` counts the directory stream's sector chain in the FAT
olefile already loaded, so an over-budget or looping chain is rejected
before the stream is read, the entry list allocated or the storage tree
built. For ZIP, the entries are counted once zipfile has read the
central directory. The child's address-space and CPU limits bound what
the guard does not see (zipfile's list for a central directory of the
payload's full size, measured in the image).

The result is one fixed token (``TOKENS``); an over-budget directory
raises ``ContainerDirectoryBudgetError``, and any other error (a
malformed container, olefile's ``RecursionError`` on a degenerate
sibling chain, a limit) is reported by type name: the parent records a
``failed`` row.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import olefile

from .container import (
    AMBIGUOUS,
    DOC,
    DOCX,
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

# OOXML main-part names (lowercased: OPC part names compare
# case-insensitively).
_OOXML_CONTENT_TYPES = "[content_types].xml"
_OOXML_MAIN_PARTS = {
    "word/document.xml": DOCX,
    "xl/workbook.xml": XLSX,
    "ppt/presentation.xml": PPTX,
}


class ContainerDirectoryBudgetError(Exception):
    """The container's directory is over the work guard (``_charge``)."""


def _charge(*, entries: int = 0, table_sectors: int = 0, file_sectors: int = 0) -> None:
    """The one work guard (#1416): the directory ``entries`` a read
    would hold, at most ``_MAX_DIRECTORY_ENTRIES``, and the allocation
    ``table_sectors`` an OLE2 header declares, at most the file's own
    ``file_sectors``."""
    if entries > _MAX_DIRECTORY_ENTRIES or table_sectors > file_sectors:
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
    return _identify_zip(payload), []


def _identify_ole2(payload: bytes) -> str:
    # A file object, never the bytes: olefile reads bytes shorter than
    # 1,536 as a file name.
    with _BoundedOleFile(io.BytesIO(payload)) as ole:
        names = {_ole2_name(kid) for kid in ole.root.kids}
    kinds = {_OLE2_STREAMS[name] for name in names if name in _OLE2_STREAMS}
    if len(kinds) > 1:
        return AMBIGUOUS
    if kinds:
        return kinds.pop()
    if _OLE2_ENCRYPTION_STREAMS <= names:
        return OLE2_ENCRYPTED
    return OLE2_OTHER


def _ole2_name(entry: Any) -> str:
    return str(entry.name).lower()


def _identify_zip(payload: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = archive.infolist()
    _charge(entries=len(members))
    names = {info.filename.lower() for info in members}
    if _OOXML_CONTENT_TYPES not in names:
        return ZIP_OTHER
    kinds = {kind for part, kind in _OOXML_MAIN_PARTS.items() if part in names}
    if len(kinds) > 1:
        return AMBIGUOUS
    if kinds:
        return kinds.pop()
    return OOXML_UNKNOWN
