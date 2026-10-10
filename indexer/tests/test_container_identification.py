"""#1416: an OLE2 or ZIP payload is identified by its container directory,
in a child process, under any label and before the OCR gate, and runs the
one extractor its directory names; a container no extractor reads is
``unsupported`` with a fixed error, and a malformed one or a limit hit is
``failed``. The cache key stays the label's namespace (owner decision on
#1416); each cached row records how its payload was certified
(``attachment_extractions.identifier``), and a row with none, or with an
older identification version, is refreshed by the lookup and the startup
sweep, which share one eligibility, checked here over a catalogue."""

from __future__ import annotations

import hashlib
import io
import itertools
import logging
import struct
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import olefile
import pytest
from src import attachment_indexing, extractors, main
from src.attachment_indexing import (
    apply_attachment_writes,
    identification_due,
    identification_refreshes,
    prepare_attachment_writes,
)
from src.database import EMBEDDING_DIM, Database
from src.extractors import (
    AMBIGUOUS_CONTAINER_ERROR,
    CONTAINER_IDENTIFIER,
    ENCRYPTED_OFFICE_ERROR,
    EXTRACTOR_VERSIONS,
    LEGACY_OLE2_ERROR,
    OLE2_NOT_OFFICE_ERROR,
    PERMANENT_FAILURE_ERRORS,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_TOO_LARGE,
    STATUS_UNSUPPORTED,
    ZIP_NOT_OFFICE_ERROR,
    ExtractionResult,
    container,
    container_child,
    extract,
)
from src.extractors._runner import process_launches
from src.parser import Attachment
from src.queue import REASON_INITIAL_SCAN, REASON_REEXTRACT, IndexingQueue
from src.threader import Threader
from src.timings import TimingAggregator

from tests.conftest import make_message, make_mock_embedder, make_ole2, make_thread, make_zip

MARKER = "SYNTHETIC_1416_MARKER"
_UNIT_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
_FIXTURES = Path(__file__).parent / "fixtures" / "extractors"

_WORD = ("WordDocument", "1Table")
_EXCEL = ("Workbook",)
_POWERPOINT = ("PowerPoint Document", "Current User")
_ZIP_MAIN = {
    "docx": "word/document.xml",
    "xlsx": "xl/workbook.xml",
    "pptx": "ppt/presentation.xml",
}


def _ooxml(kind: str, *extra: str) -> bytes:
    return make_zip("[Content_Types].xml", "_rels/.rels", _ZIP_MAIN[kind], *extra)


# --------------------------------------------------------------------------
# The child's identification


class TestIdentification:
    @pytest.mark.parametrize(
        ("streams", "token"),
        [
            (_WORD, "doc"),
            (("worddocument",), "doc"),
            (_EXCEL, "xls"),
            (("Book",), "xls"),
            (_POWERPOINT, "ppt"),
            (("EncryptionInfo", "EncryptedPackage"), "ole2-encrypted"),
            (("EncryptionInfo",), "ole2-other"),
            (("__substg1.0_0037001F", "__properties_version1.0"), "ole2-other"),
            ((), "ole2-other"),
            (("WordDocument", "Workbook"), "ambiguous"),
            (("PowerPoint Document", "Book"), "ambiguous"),
        ],
    )
    def test_ole2_root_streams_decide(self, streams, token):
        assert container_child.extract_text(make_ole2(*streams)) == (token, [])

    @pytest.mark.parametrize(
        ("fixture", "token"),
        [
            ("legacy.doc", "doc"),
            ("legacy.xls", "xls"),
            ("legacy.ppt", "ppt"),
            ("legacy-lo.ppt", "ppt"),
            ("legacy-encrypted.ppt", "ppt"),
        ],
    )
    def test_tool_generated_files_are_identified(self, fixture, token):
        payload = (_FIXTURES / fixture).read_bytes()
        assert container_child.extract_text(payload) == (token, [])

    @pytest.mark.parametrize(
        ("names", "token"),
        [
            (("[Content_Types].xml", _ZIP_MAIN["docx"]), "docx"),
            (("[Content_Types].xml", _ZIP_MAIN["xlsx"]), "xlsx"),
            (("[Content_Types].xml", _ZIP_MAIN["pptx"]), "pptx"),
            (("[CONTENT_TYPES].XML", "Word/Document.XML"), "docx"),
            (("[Content_Types].xml", "word/document2.xml"), "ooxml"),
            (("[Content_Types].xml", _ZIP_MAIN["docx"], _ZIP_MAIN["xlsx"]), "ambiguous"),
            ((_ZIP_MAIN["docx"],), "zip-other"),
            (("mimetype", "content.xml"), "zip-other"),
            ((), "zip-other"),
        ],
    )
    def test_zip_member_names_decide(self, names, token):
        assert container_child.extract_text(make_zip(*names)) == (token, [])

    def test_no_member_is_opened(self, monkeypatch):
        """Only the central directory is read: no member is decompressed."""
        payload = make_zip(*_ooxml_names("docx"), contents=b"x" * 4096)
        opened = MagicMock(side_effect=AssertionError("a member was opened"))
        monkeypatch.setattr(zipfile.ZipFile, "open", opened)
        assert container_child.extract_text(payload) == ("docx", [])
        opened.assert_not_called()

    def test_ole2_bytes_are_never_read_as_a_file_name(self, monkeypatch):
        """olefile reads ``bytes`` shorter than 1,536 as a file name; the
        child passes a file object, whatever the length."""
        seen: list[type] = []
        real = container_child._BoundedOleFile.__init__

        def recording(self, filename=None, *args, **kwargs):
            seen.append(type(filename))
            real(self, filename, *args, **kwargs)

        monkeypatch.setattr(container_child._BoundedOleFile, "__init__", recording)
        with pytest.raises(Exception) as raised:
            container_child.extract_text(container.OLE2_SIGNATURE + MARKER.encode())
        assert not isinstance(raised.value, FileNotFoundError)
        assert seen == [io.BytesIO]


def _ooxml_names(kind: str) -> tuple[str, ...]:
    return ("[Content_Types].xml", _ZIP_MAIN[kind])


# --------------------------------------------------------------------------
# The work guard


_END, _FREE, _FAT_SECTOR = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD


def _v4_header(
    *, dir_sectors: int, fat_sectors: int, difat_sectors: int = 0, first_dir: int | None = None
) -> bytes:
    """A version-4 header: the FAT in sectors ``0..fat_sectors-1`` (up to
    109 listed in the header) and the directory after them."""
    listed = min(fat_sectors, 109)
    difat = list(range(listed)) + [_FREE] * (109 - listed)
    return (
        container.OLE2_SIGNATURE
        + b"\0" * 16
        + struct.pack("<HHHHH", 0x3E, 4, 0xFFFE, 12, 6)
        + b"\0" * 6
        + struct.pack(
            "<9I",
            dir_sectors,
            fat_sectors,
            fat_sectors if first_dir is None else first_dir,
            0,
            4096,
            _END,
            0,
            _END if not difat_sectors else 1,
            difat_sectors,
        )
        + struct.pack("<109I", *difat)
    ).ljust(4096, b"\0")


def _long_directory(dir_sectors: int, *, loop: bool = False) -> bytes:
    """A version-4 OLE2 file (4,096-byte sectors, 32 directory entries a
    sector) whose directory chain is ``dir_sectors`` long, or loops back
    to its start. The entries are empty: the guard counts the chain
    before any entry is read."""
    fat_sectors = 1
    while fat_sectors * 1024 < fat_sectors + dir_sectors:
        fat_sectors += 1
    fat = [_FREE] * (1024 * fat_sectors)
    fat[:fat_sectors] = [_FAT_SECTOR] * fat_sectors
    first = fat_sectors
    for i in range(dir_sectors):
        fat[first + i] = first + i + 1 if i < dir_sectors - 1 else (first if loop else _END)
    sectors = struct.pack(f"<{len(fat)}I", *fat) + bytes(4096 * dir_sectors)
    return _v4_header(dir_sectors=dir_sectors, fat_sectors=fat_sectors) + sectors


class TestWorkGuard:
    @pytest.fixture
    def spies(self, monkeypatch):
        """Count olefile's own directory load, its stream opens and its
        FAT sector loads, the work the guard must come before."""
        counts = {"loaddirectory": 0, "_open": 0, "loadfat_sect": 0}
        for name in counts:
            real = getattr(olefile.OleFileIO, name)

            def counting(self, *args, _real=real, _name=name, **kwargs):
                counts[_name] += 1
                return _real(self, *args, **kwargs)

            monkeypatch.setattr(olefile.OleFileIO, name, counting)
        return counts

    def test_the_hook_runs_on_a_valid_file(self, spies):
        assert container_child.extract_text(make_ole2(*_WORD)) == ("doc", [])
        assert spies["loaddirectory"] == 1
        assert spies["_open"] == 1
        assert spies["loadfat_sect"] == 1

    def test_an_oversized_directory_is_rejected_before_it_is_read(self, spies):
        """A chain one sector past the entry budget: the directory stream
        is never opened, the entry list never allocated, the tree never
        built (olefile's ``loaddirectory`` does all three)."""
        sectors = container_child._MAX_DIRECTORY_ENTRIES // 32 + 1
        payload = _long_directory(sectors)
        started = time.perf_counter()
        with pytest.raises(container_child.ContainerDirectoryBudgetError):
            container_child.extract_text(payload)
        assert time.perf_counter() - started < 5
        assert (spies["loaddirectory"], spies["_open"]) == (0, 0)

    def test_a_directory_at_the_budget_is_read(self, spies):
        sectors = container_child._MAX_DIRECTORY_ENTRIES // 32
        token, _ = container_child.extract_text(_long_directory(sectors))
        assert token == "ole2-other"
        assert spies["loaddirectory"] == 1

    def test_a_looping_directory_chain_is_rejected_before_it_is_read(self, spies):
        started = time.perf_counter()
        with pytest.raises(container_child.ContainerDirectoryBudgetError):
            container_child.extract_text(_long_directory(3, loop=True))
        assert time.perf_counter() - started < 5
        assert (spies["loaddirectory"], spies["_open"]) == (0, 0)

    def test_a_table_larger_than_the_file_is_rejected_before_it_is_read(self, spies):
        """A header declaring 2^31 FAT sectors through a DIFAT chain ran
        until the CPU limit (measured); the guard stops it before any
        table sector is read."""
        fat = struct.pack("<1024I", _FAT_SECTOR, _FAT_SECTOR, _END, *([_FREE] * 1021))
        payload = (
            _v4_header(
                dir_sectors=1,
                fat_sectors=0x7FFFFFFF,
                difat_sectors=0x7FFFFFFF // 1023,
                first_dir=1,
            )
            + fat
            + bytes(4096 * 2)
        )
        with pytest.raises(container_child.ContainerDirectoryBudgetError):
            container_child.extract_text(payload)
        assert spies["loadfat_sect"] == 0

    @pytest.mark.parametrize("members", [10, 11])
    def test_zip_entries_are_charged(self, monkeypatch, members):
        monkeypatch.setattr(container_child, "_MAX_DIRECTORY_ENTRIES", 10)
        names = (*_ooxml_names("xlsx"), *(f"m{i}" for i in range(members - 2)))
        payload = make_zip(*names)
        if members > 10:
            with pytest.raises(container_child.ContainerDirectoryBudgetError):
                container_child.extract_text(payload)
        else:
            assert container_child.extract_text(payload) == ("xlsx", [])

    def test_the_budget_is_above_every_ooxml_member_budget(self):
        """A package between an extractor's member budget and this one
        reaches that extractor, whose own budget records it
        ``unsupported`` instead of a ``failed`` limit hit."""
        from src.extractors import docx, pptx

        assert container_child._MAX_DIRECTORY_ENTRIES > max(docx._MAX_MEMBERS, pptx._MAX_MEMBERS)

    def test_a_degenerate_sibling_chain_is_recorded_failed(self, caplog):
        """olefile recurses once per sibling: a long chain raises
        ``RecursionError`` in the child, a ``failed`` row by type."""
        caplog.set_level("DEBUG")
        names = [f"{MARKER}{i}" for i in range(2_000)]
        payload = _sibling_chain(names)
        result = extract(content_type="application/msword", filename="a.doc", payload=payload)
        assert (result.status, result.error, result.extractor) == (
            STATUS_FAILED,
            "RecursionError",
            None,
        )
        assert result.identifier == CONTAINER_IDENTIFIER
        assert MARKER not in caplog.text


def _sibling_chain(names: list[str]) -> bytes:
    """A version-4 OLE2 file whose root's children form one right-sibling
    chain."""
    entries_per_sector = 32
    count = len(names) + 1
    dir_sectors = -(-count // entries_per_sector)
    fat = [_FREE] * 1024
    fat[0] = _FAT_SECTOR
    for i in range(dir_sectors):
        fat[1 + i] = 2 + i if i < dir_sectors - 1 else _END

    def entry(name: str, kind: int, right: int, child: int) -> bytes:
        encoded = (name + "\0").encode("utf-16-le")
        return (
            encoded.ljust(64, b"\0")
            + struct.pack("<HBB3I", len(encoded), kind, 1, _FREE, right, child)
            + b"\0" * 36
            + struct.pack("<IQ", _END, 0)
        )

    entries = [entry("Root Entry", 5, _FREE, 1)]
    for sid, name in enumerate(names, start=1):
        entries.append(entry(name, 2, sid + 1 if sid < len(names) else _FREE, _FREE))
    directory = b"".join(entries).ljust(dir_sectors * 4096, b"\0")
    return (
        _v4_header(dir_sectors=dir_sectors, fat_sectors=1) + struct.pack("<1024I", *fat) + directory
    )


# --------------------------------------------------------------------------
# Dispatch: every label, before the OCR gate


# Every label kind the dispatcher selects by: an image, each OOXML and
# legacy Office type, text, PDF, HTML, an attached email and none.
_LABELS = (
    ("image/png", "photo.png"),
    ("application/octet-stream", "scan.jpg"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.bin"),
    ("application/octet-stream", "book.xlsx"),
    ("application/octet-stream", "deck.pptx"),
    ("application/msword", "a.doc"),
    ("application/octet-stream", "book.xls"),
    ("application/vnd.ms-powerpoint", "deck.ppt"),
    ("text/plain", "notes.txt"),
    ("application/pdf", "a.pdf"),
    ("text/html", "a.html"),
    ("message/rfc822", "a.eml"),
    ("application/octet-stream", "a.bin"),
)
_OOXML_LABEL_MODULES = {
    "a.bin": None,
    "book.xlsx": "xlsx",
    "deck.pptx": "pptx",
    "a.doc": "docx",
    "book.xls": "xlsx",
}
_KINDS = {
    "doc": lambda: make_ole2(*_WORD, trailer=MARKER.encode()),
    "xls": lambda: make_ole2(*_EXCEL, trailer=MARKER.encode()),
    "ppt": lambda: make_ole2(*_POWERPOINT, trailer=MARKER.encode()),
    "docx": lambda: _ooxml("docx", f"{MARKER}.xml"),
    "xlsx": lambda: _ooxml("xlsx", f"{MARKER}.xml"),
    "pptx": lambda: _ooxml("pptx", f"{MARKER}.xml"),
}


@pytest.fixture
def stubbed_extractors(monkeypatch) -> list[str]:
    """Each Office extractor replaced by a stub recording its module, so
    dispatch is observed without catdoc, Java or a real document."""
    calls: list[str] = []

    def stub(module: str):
        def run(payload, **_opts):
            calls.append(module)
            return f"{module} words", module

        return run

    for module in ("doc", "xls", "ppt", "docx", "xlsx", "pptx"):
        monkeypatch.setitem(extractors._IMPORT_CACHE, module, stub(module))
    return calls


class TestDispatch:
    @pytest.mark.parametrize("ocr_enabled", [True, False], ids=["ocr-on", "ocr-off"])
    @pytest.mark.parametrize("kind", sorted(_KINDS))
    @pytest.mark.parametrize(("content_type", "filename"), _LABELS)
    def test_every_label_runs_the_extractor_the_directory_names(
        self, stubbed_extractors, caplog, content_type, filename, kind, ocr_enabled
    ):
        caplog.set_level("DEBUG")
        result = extract(
            content_type=content_type,
            filename=filename,
            payload=_KINDS[kind](),
            ocr_enabled=ocr_enabled,
        )
        assert stubbed_extractors == [kind]
        assert (result.status, result.extractor, result.text) == (
            STATUS_SUCCESS,
            f"{kind}@{EXTRACTOR_VERSIONS[kind]}",
            f"{kind} words",
        )
        assert result.identifier == CONTAINER_IDENTIFIER
        assert MARKER not in caplog.text

    @pytest.mark.parametrize(
        ("payload", "error"),
        [
            (make_ole2("Contents", trailer=MARKER.encode()), OLE2_NOT_OFFICE_ERROR),
            (make_ole2("EncryptionInfo", "EncryptedPackage"), ENCRYPTED_OFFICE_ERROR),
            (make_ole2("WordDocument", "PowerPoint Document"), AMBIGUOUS_CONTAINER_ERROR),
            (_ooxml("docx", _ZIP_MAIN["pptx"]), AMBIGUOUS_CONTAINER_ERROR),
            (make_zip(f"{MARKER}.txt"), ZIP_NOT_OFFICE_ERROR),
            (b"PK\x05\x06" + bytes(18), ZIP_NOT_OFFICE_ERROR),
        ],
        ids=["ole2-other", "encrypted", "ole2-ambiguous", "zip-ambiguous", "zip", "empty-zip"],
    )
    @pytest.mark.parametrize(("content_type", "filename"), _LABELS)
    def test_a_container_no_extractor_reads_is_unsupported(
        self, stubbed_extractors, caplog, content_type, filename, payload, error
    ):
        caplog.set_level("DEBUG")
        result = extract(
            content_type=content_type, filename=filename, payload=payload, ocr_enabled=False
        )
        assert stubbed_extractors == []
        assert (result.status, result.extractor, result.text, result.error) == (
            STATUS_UNSUPPORTED,
            None,
            None,
            error,
        )
        assert result.identifier == CONTAINER_IDENTIFIER
        assert error in PERMANENT_FAILURE_ERRORS
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert [r.getMessage() for r in warnings] == [
            f"extractor container declined (dispatch_via={_via(content_type, filename)}): "
            f"{error}; recorded unsupported, not retried"
        ]
        assert MARKER not in caplog.text

    @pytest.mark.parametrize(("content_type", "filename"), _LABELS)
    def test_an_unnamed_ooxml_kind_keeps_the_labels_ooxml_extractor(
        self, stubbed_extractors, content_type, filename
    ):
        """An OOXML package whose main part the directory does not name
        runs the label's OOXML extractor, a legacy label's counterpart
        included; under any other label it is a ZIP no extractor reads."""
        payload = make_zip("[Content_Types].xml", "word/document2.xml")
        result = extract(content_type=content_type, filename=filename, payload=payload)
        module = extractors._label_ooxml_module(
            extractors._resolve_extractor(content_type, filename)[0]
        )
        if module is None:
            assert (result.status, result.error) == (STATUS_UNSUPPORTED, ZIP_NOT_OFFICE_ERROR)
            assert stubbed_extractors == []
        else:
            assert stubbed_extractors == [module]
            assert result.status == STATUS_SUCCESS

    @pytest.mark.parametrize(
        ("payload", "error"),
        [
            (
                container.OLE2_SIGNATURE + MARKER.encode() + bytes(2048),
                {"NotOleFileError", "ValueError"},
            ),
            (b"PK\x03\x04" + MARKER.encode() + bytes(64), {"BadZipFile"}),
        ],
        ids=["ole2", "zip"],
    )
    def test_a_malformed_container_is_failed_by_type(
        self, stubbed_extractors, caplog, payload, error
    ):
        caplog.set_level("DEBUG")
        result = extract(content_type="image/png", filename=f"{MARKER}.png", payload=payload)
        # The parser's own exception type (olefile's depends on which
        # header field the garbage breaks first), never its message.
        assert (result.status, result.extractor) == (STATUS_FAILED, None)
        assert result.error in error
        assert result.identifier == CONTAINER_IDENTIFIER
        assert stubbed_extractors == []
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == [f"extractor container failed (dispatch_via=mime-image): {result.error}"]
        assert MARKER not in caplog.text

    def test_an_unknown_token_is_failed(self, monkeypatch, stubbed_extractors):
        """The child's output is checked against the fixed token set."""
        from src.extractors._runner import ChildResult

        monkeypatch.setattr(container, "run_child", lambda *_a, **_k: ChildResult(MARKER, [], {}))
        result = extract(content_type="image/png", filename="a.png", payload=make_ole2())
        assert (result.status, result.error) == (STATUS_FAILED, "ContainerTokenError")
        assert stubbed_extractors == []

    def test_other_payloads_keep_the_labels_dispatch(self, stubbed_extractors):
        """A payload with neither signature is dispatched by its label as
        before, and certified ''; a ``too_large`` one is not dispatched."""
        result = extract(content_type="text/plain", filename="a.txt", payload=b"plain words")
        assert (result.status, result.extractor, result.identifier) == (
            STATUS_SUCCESS,
            "text@3",
            "",
        )
        too_large = extract(
            content_type="image/png", filename="a.png", payload=make_ole2(*_WORD), max_bytes=10
        )
        assert (too_large.status, too_large.identifier) == (STATUS_TOO_LARGE, None)
        assert stubbed_extractors == []

    @pytest.mark.real_extractor_child
    def test_identification_is_one_process_launch(self, stubbed_extractors):
        """The identification child is a process launch, charged to the
        per-message extraction budget like any other (#1236)."""
        before = process_launches()
        result = extract(content_type="image/png", filename="a.png", payload=make_ole2("Contents"))
        assert result.error == OLE2_NOT_OFFICE_ERROR
        assert process_launches() - before == 1

    @pytest.mark.real_extractor_child
    def test_the_real_child_runs_under_its_limits(self, monkeypatch):
        calls: list[dict] = []
        real = extractors._runner.run_tool

        def spy(argv, payload, **kwargs):
            calls.append({"argv": argv, **kwargs})
            return real(argv, payload, **kwargs)

        monkeypatch.setattr(extractors._runner, "run_tool", spy)
        assert container.identify((_FIXTURES / "legacy.doc").read_bytes()) == "doc"
        [call] = calls
        assert call["argv"][-1] == "container"
        assert call["max_address_space_bytes"] == container.CHILD_MAX_ADDRESS_SPACE_BYTES
        assert call["max_cpu_seconds"] == container.CHILD_MAX_CPU_SECONDS
        assert call["timeout_seconds"] > container.CHILD_MAX_CPU_SECONDS + 1


def _via(content_type: str, filename: str) -> str:
    return extractors._resolve_extractor(content_type, filename)[1]


def test_the_cache_namespace_is_the_labels(stubbed_extractors):
    """The key stays the label's namespace (owner decision on #1416): a
    Word file under an image label is keyed ``image`` and stamped
    ``doc``; ``label_extraction_modules`` names namespaces, not what ran."""
    payload = make_ole2(*_WORD)
    assert extractors.extraction_module("image/png", "a.png", payload) == "image"
    assert extractors.label_extraction_modules("image/png", "a.png") == {"image", "pdf"}
    result = extract(content_type="image/png", filename="a.png", payload=payload)
    assert result.extractor == f"doc@{EXTRACTOR_VERSIONS['doc']}"


# --------------------------------------------------------------------------
# One eligibility for the lookup and the sweep


_STATUSES = (STATUS_SUCCESS, STATUS_EMPTY, STATUS_UNSUPPORTED, STATUS_FAILED, STATUS_TOO_LARGE)
_IDENTIFIERS = (None, "", "container@0", CONTAINER_IDENTIFIER, "container@2", "container@x")
_STAMPS = ("doc@2", "pdf-ocr@5", "image-ocr@6", None)


def _catalogue() -> list[dict]:
    return [
        {
            "name": f"r{i}",
            "extraction_status": status,
            "identifier": identifier,
            "extractor": stamp,
        }
        for i, (status, identifier, stamp) in enumerate(
            itertools.product(_STATUSES, _IDENTIFIERS, _STAMPS)
        )
    ]


@pytest.fixture(scope="module")
def catalogue_db(tmp_path_factory):
    """One message per catalogue row, each occurrence using its own row."""
    db = Database(tmp_path_factory.mktemp("cat") / "mail.db")
    rows = _catalogue()
    for row in rows:
        name = row["name"]
        msg = make_message(message_id=f"{name}@example.com", filepath=f"/maildir/cur/{name}")
        db.upsert_thread(make_thread(messages=[msg], thread_id=f"t-{name}"), _UNIT_VECTOR)
        db._conn.execute(
            "INSERT INTO attachments (attachment_occurrence_id, claimant_id, attachment_id, "
            "thread_id, filename, content_type, size_bytes, seen_at, extractor_module, "
            "text_complete) VALUES (?, ?, ?, ?, 'f.doc', 'application/msword', 1, "
            "'2026-01-01', 'doc', 1)",
            (f"occ-{name}", msg.claimant_id, f"h-{name}", f"t-{name}"),
        )
        db._conn.execute(
            "INSERT INTO attachment_extractions (attachment_id, extractor_module, "
            "extraction_status, extractor, extracted_text, extraction_error, extracted_at, "
            "text_complete, identifier) VALUES (?, 'doc', ?, ?, NULL, NULL, '2026-01-01', 1, ?)",
            (f"h-{name}", row["extraction_status"], row["extractor"], row["identifier"]),
        )
        db._conn.commit()
    yield db, rows
    db.close()


@pytest.mark.parametrize("ocr_enabled", [True, False])
def test_the_sweep_sql_and_the_lookup_select_the_same_rows(catalogue_db, ocr_enabled):
    db, rows = catalogue_db
    found = db.find_identification_refresh_attachment_filepaths(ocr_enabled=ocr_enabled)
    expected = {
        f"/maildir/cur/{row['name']}"
        for row in rows
        if identification_due(row, ocr_enabled=ocr_enabled)
    }
    assert found == expected
    # The lookup re-extracts exactly the due rows of a container payload,
    # and of a payload with neither signature only those it cannot
    # certify '' (a row with an identifier of an older version).
    for row in rows:
        due = identification_due(row, ocr_enabled=ocr_enabled)
        assert identification_refreshes(row, make_ole2(), ocr_enabled=ocr_enabled) is due
        plain = identification_refreshes(row, b"plain words", ocr_enabled=ocr_enabled)
        assert plain is (due and row["identifier"] is not None)


def test_the_catalogue_is_written_out_by_hand():
    """Guards the differential: the predicate's answer for each kind of
    row, written out rather than derived from it."""

    def due(status, identifier, stamp="doc@2", *, ocr=True):
        row = {"extraction_status": status, "identifier": identifier, "extractor": stamp}
        return identification_due(row, ocr_enabled=ocr)

    assert due(STATUS_SUCCESS, None)
    assert due(STATUS_UNSUPPORTED, None)
    assert due(STATUS_FAILED, "container@0")
    assert due(STATUS_FAILED, "container@x")
    assert not due(STATUS_SUCCESS, "")
    assert not due(STATUS_SUCCESS, CONTAINER_IDENTIFIER)
    # A newer version, after a rollback, is kept.
    assert not due(STATUS_SUCCESS, "container@2")
    # No dispatch reached a ``too_large`` row.
    assert not due(STATUS_TOO_LARGE, None)
    # OCR text is kept while OCR is off, whatever the module.
    assert not due(STATUS_SUCCESS, None, "pdf-ocr@5", ocr=False)
    assert not due(STATUS_SUCCESS, None, "image-ocr@6", ocr=False)
    assert due(STATUS_SUCCESS, None, "pdf-ocr@5", ocr=True)
    assert due(STATUS_SUCCESS, None, "doc@2", ocr=False)


# --------------------------------------------------------------------------
# The lookup


def _attachment(payload: bytes, filename: str, content_type: str) -> Attachment:
    return Attachment(
        filename=filename,
        content_type=content_type,
        size=len(payload),
        payload=payload,
        content_hash=hashlib.sha256(payload).hexdigest(),
    )


class TestLookup:
    @pytest.fixture
    def db(self, tmp_path):
        db = Database(tmp_path / "mail.db")
        msg = make_message(message_id="m@example.com")
        db.upsert_thread(make_thread(messages=[msg], thread_id="t-1"), _UNIT_VECTOR)
        yield db
        db.close()

    @staticmethod
    def _store(db, attachment, module, *, status, error=None, identifier, stamp="doc@2"):
        db.store_attachment_extraction(
            attachment_id=attachment.content_hash,
            extractor_module=module,
            extraction_status=status,
            extractor=stamp,
            extracted_text=f"{MARKER} cached" if status == STATUS_SUCCESS else None,
            extraction_error=error,
            text_complete=status in {STATUS_SUCCESS, STATUS_EMPTY},
            identifier=identifier,
        )

    @staticmethod
    def _run(db, attachment, monkeypatch, *, ocr_enabled=True, fresh=None):
        extractor = MagicMock(
            return_value=fresh
            or ExtractionResult(
                status=STATUS_SUCCESS,
                extractor="doc@2",
                text="fresh words",
                error=None,
                text_complete=True,
                identifier=CONTAINER_IDENTIFIER,
            )
        )
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        plan = prepare_attachment_writes(
            attachment=attachment,
            claimant_id=make_message(message_id="m@example.com").claimant_id,
            db=db,
            chunk_target_tokens=350,
            chunk_max_tokens=500,
            chunk_overlap_tokens=60,
            ocr_enabled=ocr_enabled,
            max_bytes=10_000_000,
            max_ocr_pages=20,
        )
        plan.embeddings_by_chunk_id = {c.chunk_id: _UNIT_VECTOR for c in plan.chunks}
        with db.transaction():
            apply_attachment_writes(
                plan=plan,
                claimant_id=make_message(message_id="m@example.com").claimant_id,
                thread_id="t-1",
                db=db,
            )
        return extractor.call_count, plan

    @staticmethod
    def _row(db, attachment, module):
        return db.get_attachment_extraction(attachment.content_hash, module)

    def test_a_plain_payloads_row_is_certified_and_kept(self, db, monkeypatch):
        attachment = _attachment(b"plain words", "a.txt", "text/plain")
        self._store(db, attachment, "text", status=STATUS_SUCCESS, identifier=None, stamp="text@3")
        calls, plan = self._run(db, attachment, monkeypatch)
        assert (calls, plan.cached) == (0, True)
        row = self._row(db, attachment, "text")
        assert (row["identifier"], row["extracted_text"]) == ("", f"{MARKER} cached")
        # Certified: served again with no write needed.
        assert self._run(db, attachment, monkeypatch)[0] == 0

    @pytest.mark.parametrize(
        ("status", "error"),
        [
            (STATUS_SUCCESS, None),
            (STATUS_UNSUPPORTED, LEGACY_OLE2_ERROR),
            (STATUS_UNSUPPORTED, extractors.BINARY_AS_TEXT_ERROR),
            (STATUS_UNSUPPORTED, extractors.OCR_DISABLED_ERROR),
            (STATUS_FAILED, "UnidentifiedImageError"),
        ],
    )
    def test_a_containers_uncertified_row_is_identified_once(self, db, monkeypatch, status, error):
        """Checked before an ``unsupported`` row is honoured: a row #694,
        #932 or the image extractor recorded for a container is extracted
        again, certified, and then served."""
        attachment = _attachment(make_ole2(*_WORD), "scan.png", "image/png")
        self._store(db, attachment, "image", status=status, error=error, identifier=None)
        calls, plan = self._run(db, attachment, monkeypatch, ocr_enabled=False)
        assert (calls, plan.cached, plan.status) == (1, False, STATUS_SUCCESS)
        row = self._row(db, attachment, "image")
        assert (row["identifier"], row["extractor"]) == (CONTAINER_IDENTIFIER, "doc@2")
        assert self._run(db, attachment, monkeypatch, ocr_enabled=False)[0] == 0

    @pytest.mark.parametrize(
        ("identifier", "refreshed"),
        [("container@0", True), (CONTAINER_IDENTIFIER, False), ("container@2", False)],
    )
    def test_an_older_version_is_refreshed_and_a_newer_kept(
        self, db, monkeypatch, identifier, refreshed
    ):
        attachment = _attachment(make_ole2(*_WORD), "a.doc", "application/msword")
        self._store(db, attachment, "doc", status=STATUS_SUCCESS, identifier=identifier)
        assert self._run(db, attachment, monkeypatch)[0] == int(refreshed)

    def test_a_too_large_row_is_left_to_its_own_refresh(self, db, monkeypatch):
        attachment = _attachment(make_ole2(*_WORD), "a.doc", "application/msword")
        db.store_attachment_extraction(
            attachment_id=attachment.content_hash,
            extractor_module="doc",
            extraction_status=STATUS_TOO_LARGE,
            extractor=None,
            extracted_text=None,
            extraction_error="cap",
        )
        monkeypatch.setattr(attachment_indexing, "too_large_fits", lambda *_a: False)
        assert self._run(db, attachment, monkeypatch)[0] == 0
        assert self._row(db, attachment, "doc")["identifier"] is None

    def test_ocr_text_is_kept_while_ocr_is_off(self, db, monkeypatch):
        attachment = _attachment(make_ole2(*_WORD), "a.pdf", "application/pdf")
        self._store(
            db, attachment, "pdf", status=STATUS_SUCCESS, identifier=None, stamp="pdf-ocr@5"
        )
        assert self._run(db, attachment, monkeypatch, ocr_enabled=False)[0] == 0
        assert self._run(db, attachment, monkeypatch, ocr_enabled=True)[0] == 1


# --------------------------------------------------------------------------
# The startup sweep and the bootstrap reparse, end to end


def _write_eml(path: Path, message_id: str, payload: bytes, ctype: str, filename: str) -> None:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "Files"
    msg["Message-ID"] = f"<{message_id}>"
    msg["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"
    msg.set_content("See the attached file.")
    maintype, _, subtype = ctype.partition("/")
    msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(msg))


class TestStartupSweep:
    """Rows written before identification (no ``identifier``): the sweep
    queues every message using one, and clears their assessments, before
    the drain replaces any of them; the drain certifies or re-identifies
    each row once, so the next sweep finds nothing."""

    WORD = make_ole2(*_WORD, trailer=MARKER.encode())
    PLAIN = f"{MARKER} plain attachment words".encode()

    def _drain(self, db, queue):
        return main._drain_queue_batched(
            db,
            make_mock_embedder(_UNIT_VECTOR),
            Threader(db),
            queue,
            batch_size=10,
            timing_aggregator=TimingAggregator(window=4),
            max_passes=1,
        )

    @pytest.fixture
    def mailbox(self, tmp_path, monkeypatch, stubbed_extractors):
        """Two messages sharing a Word file sent as an image (one row,
        recorded ``failed`` by the image extractor before #1416), one
        dead-lettered message sharing it too, and one carrying text."""
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", True)
        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
        paths = {}
        for name, payload, ctype, filename in (
            ("first", self.WORD, "image/png", f"{MARKER}.png"),
            ("second", self.WORD, "image/png", "copy.png"),
            ("dead", self.WORD, "image/png", "old.png"),
            ("plain", self.PLAIN, "text/plain", "notes.txt"),
        ):
            path = maildir / "INBOX" / "cur" / f"{name}.eml"
            _write_eml(path, f"{name}@example.com", payload, ctype, filename)
            queue.enqueue(str(path), REASON_INITIAL_SCAN)
            paths[name] = str(path)
        self._drain(db, queue)
        # As a v11 indexer left them: the image extractor's failure for the
        # Word file, and every row uncertified.
        db._conn.execute(
            "UPDATE attachment_extractions SET extraction_status = 'failed', "
            "extractor = 'image@6', extracted_text = NULL, "
            "extraction_error = 'UnidentifiedImageError', text_complete = 0 "
            "WHERE extractor_module = 'image'"
        )
        db._conn.execute("UPDATE attachment_extractions SET identifier = NULL")
        db._conn.execute(
            "UPDATE attachments SET text_complete = 0 WHERE extractor_module = 'image'"
        )
        db._conn.commit()
        queue.enqueue(paths["dead"], REASON_INITIAL_SCAN)
        queue.mark_dead_terminal(paths["dead"], stage="embed", error="x")
        stubbed_extractors.clear()
        yield db, queue, paths, stubbed_extractors
        db.close()

    @staticmethod
    def _queued(db) -> dict[str, str]:
        rows = db._conn.execute(
            "SELECT filepath, reason FROM indexing_jobs WHERE status = 'queued'"
        ).fetchall()
        return {r["filepath"]: r["reason"] for r in rows}

    @staticmethod
    def _words(db, filepath) -> str:
        rows = db._conn.execute(
            "SELECT c.text FROM message_chunks c JOIN message_thread_map m "
            "ON m.claimant_id = c.claimant_id WHERE m.filepath = ? "
            "AND c.attachment_id IS NOT NULL",
            (filepath,),
        ).fetchall()
        return " ".join(r["text"] for r in rows)

    def test_every_dependent_is_queued_and_cleared_before_the_row_is_replaced(
        self, mailbox, caplog
    ):
        db, queue, paths, calls = mailbox
        caplog.set_level(logging.INFO)
        assert main._requeue_stale_extractions(db, queue) == 3
        assert self._queued(db) == {
            paths["first"]: REASON_REEXTRACT,
            paths["second"]: REASON_REEXTRACT,
            paths["plain"]: REASON_REEXTRACT,
        }
        # Nothing was extracted yet: the shared row is as it was, and
        # every occurrence using it, the dead-lettered one's too, has its
        # assessment cleared.
        row = db._conn.execute(
            "SELECT extraction_status, identifier FROM attachment_extractions "
            "WHERE extractor_module = 'image'"
        ).fetchone()
        assert tuple(row) == (STATUS_FAILED, None)
        assert calls == []
        states = db._conn.execute(
            "SELECT text_complete FROM attachments WHERE extractor_module = 'image'"
        ).fetchall()
        assert [r[0] for r in states] == [None, None, None]
        [line] = [
            r for r in caplog.records if r.getMessage().startswith("container identification")
        ]
        assert line.levelno == logging.INFO
        assert line.getMessage() == (
            "container identification sweep (version 1): 4 message(s) with an attachment "
            "result cached before its OLE2 / ZIP container certification or under an older "
            "identification version; re-queued 3, already queued 0, skipped 1 dead-lettered "
            "(run make requeue-dead to refresh them)."
        )

        while self._queued(db):
            self._drain(db, queue)
        # The Word file was identified and read once for both messages;
        # the text row was certified without being extracted again.
        assert calls == ["doc"]
        rows = {
            r["extractor_module"]: (r["extraction_status"], r["extractor"], r["identifier"])
            for r in db._conn.execute(
                "SELECT extractor_module, extraction_status, extractor, identifier "
                "FROM attachment_extractions"
            )
        }
        assert rows == {
            "image": (STATUS_SUCCESS, f"doc@{EXTRACTOR_VERSIONS['doc']}", CONTAINER_IDENTIFIER),
            "text": (STATUS_SUCCESS, "text@3", ""),
        }
        assert "doc words" in self._words(db, paths["first"])
        assert "doc words" in self._words(db, paths["second"])
        assert MARKER in self._words(db, paths["plain"])

        # Once: the next start finds nothing; the dead-lettered message is
        # still skipped, as every arm skips it.
        caplog.clear()
        assert main._requeue_stale_extractions(db, queue) == 0
        assert self._queued(db) == {}
        assert MARKER not in caplog.text
        jobs = db._conn.execute("SELECT last_error FROM indexing_jobs").fetchall()
        assert all(MARKER not in (r["last_error"] or "") for r in jobs)

    def test_the_v12_reparse_reaches_the_same_rows(self, mailbox):
        """The migration's reparse (``REPARSE_ENQUEUE_SQL``) passes every
        occurrence through the lookup, which certifies or re-identifies
        it with no sweep at all."""
        from src.queue import REPARSE_ENQUEUE_SQL

        db, queue, paths, calls = mailbox
        db._conn.executescript(REPARSE_ENQUEUE_SQL)
        while self._queued(db):
            self._drain(db, queue)
        assert calls == ["doc"]
        identifiers = {
            r[0] for r in db._conn.execute("SELECT identifier FROM attachment_extractions")
        }
        assert identifiers == {CONTAINER_IDENTIFIER, ""}
        assert main._requeue_stale_extractions(db, queue) == 0

    def test_extraction_off_leaves_the_rows(self, mailbox, monkeypatch):
        db, queue, _paths, _calls = mailbox
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", False)
        assert main._requeue_stale_extractions(db, queue) == 0
        assert self._queued(db) == {}
