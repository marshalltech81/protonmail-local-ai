"""
Shared fixtures for indexer tests.
"""

import io
import struct
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from src.database import Database
from src.parser import Message
from src.threader import Thread, Threader


def make_mock_embedder(vector: list[float] | None = None) -> MagicMock:
    """MagicMock that satisfies the EmbeddingBackend Protocol.

    Production hot paths now call ``embed_batch(texts)`` instead of
    ``embed(text)`` per chunk. A bare ``MagicMock()`` would auto-create
    ``embed_batch`` as a child Mock that returns another Mock — which
    iterates as length 0, silently producing empty embedding dicts and
    unrelated downstream test failures.

    This helper wires ``embed_batch`` to delegate to ``embed`` per input
    so tests that set ``.embed.return_value`` or ``.embed.side_effect``
    keep working unchanged. Tests that need to inspect batched calls
    can override ``embed_batch.side_effect`` directly.
    """
    m = MagicMock()
    if vector is not None:
        m.embed.return_value = vector
    m.embed_batch.side_effect = lambda texts, **_kw: [m.embed(t) for t in texts]
    return m


def make_message(
    message_id: str = "msg1@example.com",
    subject: str = "Hello world",
    from_addr: str = "alice@example.com",
    to_addrs: list[str] | None = None,
    cc_addrs: list[str] | None = None,
    body_text: str = "This is the message body.",
    folder: str = "INBOX",
    filepath: str = "/maildir/INBOX/cur/msg1",
    date: datetime | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    has_attachments: bool = False,
    occurred_at: datetime | None = None,
) -> Message:
    return Message(
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references or [],
        subject=subject,
        from_addr=from_addr,
        to_addrs=to_addrs or ["bob@example.com"],
        cc_addrs=cc_addrs or [],
        date=date or datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        body_text=body_text,
        folder=folder,
        filepath=filepath,
        has_attachments=has_attachments,
        occurred_at=occurred_at,
    )


def make_thread(
    messages: list[Message] | None = None,
    thread_id: str | None = None,
    subject: str = "hello world",
    folder: str = "INBOX",
) -> Thread:
    msgs = messages or [make_message()]
    tid = thread_id or msgs[0].message_id
    return Thread(
        thread_id=tid,
        subject=subject,
        participants=[msgs[0].from_addr] + msgs[0].to_addrs,
        messages=msgs,
        folder=folder,
        date_first=msgs[0].effective_date,
        date_last=msgs[-1].effective_date,
    )


_OLE2_END = 0xFFFFFFFE
_OLE2_FREE = 0xFFFFFFFF
_OLE2_NOSTREAM = 0xFFFFFFFF


def _ole2_entry(name: str, kind: int, *, left: int, right: int, child: int) -> bytes:
    encoded = (name + "\0").encode("utf-16-le")
    return (
        encoded.ljust(64, b"\0")
        + struct.pack("<HBB3I", len(encoded), kind, 1, left, right, child)
        + b"\0" * 36
        + struct.pack("<IQ", _OLE2_END, 0)
    )


def make_ole2(*stream_names: str, trailer: bytes = b"", storages: tuple[str, ...] = ()) -> bytes:
    """A synthetic, valid OLE2 compound file (version 3, 512-byte
    sectors) whose root storage holds empty streams named
    ``stream_names`` and empty storages named ``storages``, followed by
    ``trailer``. Identification (#1416) reads only these names; the bytes
    carry no document."""
    entries_per_sector = 4
    children = [(name, 2) for name in stream_names] + [(name, 1) for name in storages]
    count = len(children) + 1
    dir_sectors = -(-count // entries_per_sector)
    fat = [0xFFFFFFFD] + [i + 2 if i < dir_sectors - 1 else _OLE2_END for i in range(dir_sectors)]
    fat += [_OLE2_FREE] * (128 - len(fat))

    # A balanced red-black tree over the root's children (all black).
    links: dict[int, tuple[int, int]] = {}

    def subtree(lo: int, hi: int) -> int:
        if lo > hi:
            return _OLE2_NOSTREAM
        mid = (lo + hi) // 2
        links[mid] = (subtree(lo, mid - 1), subtree(mid + 1, hi))
        return mid

    root_child = subtree(1, count - 1)
    entries = [
        _ole2_entry("Root Entry", 5, left=_OLE2_NOSTREAM, right=_OLE2_NOSTREAM, child=root_child)
    ]
    for sid, (name, kind) in enumerate(children, start=1):
        left, right = links[sid]
        entries.append(_ole2_entry(name, kind, left=left, right=right, child=_OLE2_NOSTREAM))
    directory = b"".join(entries).ljust(dir_sectors * 512, b"\0")
    header = (
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        + b"\0" * 16
        + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6)
        + b"\0" * 6
        + struct.pack("<9I", 0, 1, 1, 0, 4096, _OLE2_END, 0, _OLE2_END, 0)
        + struct.pack("<109I", 0, *([_OLE2_FREE] * 108))
    )
    return header + struct.pack("<128I", *fat) + directory + trailer


def make_zip(*member_names: str, contents: bytes = b"") -> bytes:
    """A synthetic ZIP whose members are ``member_names``, each holding
    ``contents`` (stored)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name in member_names:
            archive.writestr(name, contents)
    return buffer.getvalue()


def make_ooxml_names(kind: str) -> tuple[str, ...]:
    """The member names identification (#1416) reads for an OOXML
    package of ``kind`` (``docx``, ``xlsx`` or ``pptx``)."""
    main = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}
    return ("[Content_Types].xml", "_rels/.rels", main[kind])


def count_pending_deletions(db: Database) -> int:
    """Number of tombstones in ``pending_deletions``."""
    return int(db._conn.execute("SELECT COUNT(*) FROM pending_deletions").fetchone()[0])


@pytest.fixture(autouse=True)
def _reset_extractor_warning_budget(monkeypatch):
    """The failed-extraction WARNING rate limit is process-wide; give each
    test a fresh window so earlier tests cannot spend its budget."""
    from src import extractors
    from src.rate_limited_log import LineBudget

    monkeypatch.setattr(
        extractors,
        "_LINE_BUDGET",
        LineBudget(
            limit=extractors._WARNINGS_PER_WINDOW,
            window_secs=extractors._WARNING_WINDOW_SECS,
            buckets=(extractors._ATTACHMENT_LINES, extractors._OTHER_LINES),
        ),
    )
    # The attachments-line debounce is process-wide too.
    main = sys.modules.get("src.main")
    if main is not None:
        monkeypatch.setattr(main, "_last_outcomes_log", None)
        # So are the recurring steps' failure streaks (#873).
        monkeypatch.setattr(
            main, "_streaks", {name: main._FailureStreak(name) for name in main.RECOVERY_COMPONENTS}
        )
        # And the queue heartbeat's interval (#874).
        monkeypatch.setattr(main, "_last_queue_heartbeat", None)
        # And the reparse progress counts (#1078).
        monkeypatch.setattr(main, "_reparse_progress", main._ReparseProgress())
        # And the WAL checkpoint's busy streak (#875).
        monkeypatch.setattr(main, "_wal_busy_passes", 0)
        # And whether the heartbeat saw extraction deferrals (#1236).
        monkeypatch.setattr(main, "_extraction_deferrals_seen", False)
        # And the thread vector sums backfill and check progress (#1356).
        monkeypatch.setattr(main, "_vector_sums_backfill_done", False)
        monkeypatch.setattr(main, "_vector_sums_check_cursor", None)


@pytest.fixture(autouse=True)
def _ooxml_child_in_process(request, monkeypatch):
    """Run the extractor child (#1040, #1291, #1292) in this process for
    the OOXML and image extractors and container identification (#1416), through the same frames and parsing
    (progress frames as each page is read), so a test can patch a walk's
    budgets, stub Tesseract or count calls. The ``xls`` extractor starts
    the real child, as before. A test marked ``real_extractor_child``
    starts the real child process for every module
    (``tests/test_ooxml_child.py``, ``tests/test_image_child.py``)."""
    if request.node.get_closest_marker("real_extractor_child"):
        return
    import pytesseract
    from src import extractors
    from src.extractors import OOXML_MODULES, _runner, extractor_child

    # The image child sets pytesseract's command for its process; here
    # that is the test process, so restore it after each test.
    monkeypatch.setattr(
        pytesseract.pytesseract, "tesseract_cmd", pytesseract.pytesseract.tesseract_cmd
    )
    real = _runner.run_tool
    in_process = OOXML_MODULES | {"image", "container"}

    def run_tool(argv, payload, *, on_output=None, **kwargs):
        child = str(_runner._CHILD)
        if child not in argv or argv[argv.index(child) + 1] not in in_process:
            return real(argv, payload, on_output=on_output, **kwargs)
        assert on_output is not None
        module, *options = argv[argv.index(child) + 1 :]
        # A real child starts with zero counters (#1314): set the test's
        # aside so the child sends only what this extraction counted.
        before = extractors.drain_counters()
        try:
            output = extractor_child.run(
                module,
                payload,
                options,
                lambda: on_output(extractor_child.PROGRESS_FRAME),
            )
        finally:
            extractors.add_counters(before)
        on_output(output)
        return _runner.ToolOutput(b"", truncated=False)

    monkeypatch.setattr(_runner, "run_tool", run_tool)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def threader(db: Database) -> Threader:
    return Threader(db)
