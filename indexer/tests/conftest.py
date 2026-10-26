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
from src.account import AccountConfig
from src.database import Database
from src.parser import Message
from src.threader import Thread, Threader

# The account every test database is bound to (#1596). Its Maildir root
# is ``/`` so every absolute path a test indexes lies inside it; tests of
# the root itself build their own ``AccountConfig``.
TEST_ACCOUNT = AccountConfig(
    scope_uuid="00000000-0000-4000-8000-000000000001",
    provider="proton-bridge",
    maildir_root=Path("/"),
)


def make_mock_embedder(vector: list[float] | None = None) -> MagicMock:
    """MagicMock that satisfies the EmbeddingBackend Protocol.

    Production hot paths call ``embed_batch(texts)``, not
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
    ``contents`` (stored), with a fixed timestamp, so the same arguments
    give the same bytes in every run."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name in member_names:
            archive.writestr(zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0)), contents)
    return buffer.getvalue()


# OPC metadata of a synthetic OOXML package (#1416): its content types
# and package relationships, which identification reads (owner decision
# on #1416, 2026-10-10).
OPC_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
OPC_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
WORD_MAIN_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
WORKBOOK_MAIN_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
PRESENTATION_MAIN_CT = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
)
OOXML_MAIN_PARTS = {
    "docx": ("word/document.xml", WORD_MAIN_CT),
    "xlsx": ("xl/workbook.xml", WORKBOOK_MAIN_CT),
    "pptx": ("ppt/presentation.xml", PRESENTATION_MAIN_CT),
}


def make_package(members: dict[str, bytes]) -> bytes:
    """A synthetic ZIP of ``members`` (name to contents, stored), with a
    fixed timestamp."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, contents in members.items():
            archive.writestr(zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0)), contents)
    return buffer.getvalue()


def ooxml_content_types(
    overrides: dict[str, str] | None = None, defaults: dict[str, str] | None = None
) -> bytes:
    """``[Content_Types].xml`` with ``defaults`` (extension to content
    type; ``rels`` and ``xml`` by default) and ``overrides`` (part name to
    content type)."""
    if defaults is None:
        defaults = {
            "rels": "application/vnd.openxmlformats-package.relationships+xml",
            "xml": "application/xml",
        }
    lines = [f'<Types xmlns="{OPC_CONTENT_TYPES_NS}">']
    lines += [f'<Default Extension="{e}" ContentType="{t}"/>' for e, t in defaults.items()]
    lines += [f'<Override PartName="{n}" ContentType="{t}"/>' for n, t in (overrides or {}).items()]
    lines.append("</Types>")
    return "".join(lines).encode()


def ooxml_rels(
    *targets: str, rel_type: str = OFFICE_DOCUMENT_REL, mode: str | None = None
) -> bytes:
    """``_rels/.rels`` with one relationship of ``rel_type`` per target."""
    attr = "" if mode is None else f' TargetMode="{mode}"'
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="{rel_type}" Target="{t}"{attr}/>'
        for i, t in enumerate(targets, 1)
    )
    return f'<Relationships xmlns="{OPC_RELATIONSHIPS_NS}">{rels}</Relationships>'.encode()


def make_ooxml(kind: str, *extra: str, contents: bytes = b"") -> bytes:
    """A minimal OOXML package of ``kind`` (``docx``, ``xlsx`` or
    ``pptx``) at its conventional main part, with its content types and
    root relationship, plus ``extra`` members holding ``contents``."""
    part, content_type = OOXML_MAIN_PARTS[kind]
    members = {
        "[Content_Types].xml": ooxml_content_types({f"/{part}": content_type}),
        "_rels/.rels": ooxml_rels(f"/{part}"),
        part: b"<root/>",
    }
    members.update(dict.fromkeys(extra, contents))
    return make_package(members)


def count_pending_deletions(db: Database) -> int:
    """Number of tombstones in ``pending_deletions``."""
    return int(db._conn.execute("SELECT COUNT(*) FROM pending_deletions").fetchone()[0])


def message_path_sql(db: Database, alias: str = "m") -> str:
    """SQL for the file of the message row ``alias``: its representative
    occurrence's path under ``db``'s account root (an active one, live
    first), as ``messages.filepath`` held before #1568 dropped it."""
    root = str(db.account.maildir_root).rstrip("/") + "/"
    assert "'" not in root
    return (
        f"('{root}' || (SELECT o.relative_path FROM message_occurrences o "
        f"WHERE o.claimant_id = {alias}.claimant_id "
        "ORDER BY o.state = 'active' DESC, o.trashed, o.id LIMIT 1))"
    )


def lose_message(db: Database, claimant_id: str, marked_at: str | None = None) -> None:
    """Make a message lose its last live occurrence, as mbsync ``T``-flagging
    every copy would (#1567): its active occurrences are marked trashed and
    each of its occurrences is tombstoned, at ``marked_at`` (now when not
    given). Tests that need a reapable message without renaming files."""
    db._conn.execute(
        "UPDATE message_occurrences SET trashed = 1 WHERE claimant_id = ? AND state = 'active'",
        (claimant_id,),
    )
    db._conn.execute(
        "INSERT OR REPLACE INTO pending_deletions (occurrence_id, claimant_id, marked_at) "
        "SELECT id, claimant_id, ? FROM message_occurrences WHERE claimant_id = ?",
        (marked_at or datetime.now(UTC).isoformat(), claimant_id),
    )
    db._conn.commit()


@pytest.fixture(autouse=True)
def _account_env(monkeypatch):
    """``main()`` and the maintenance commands read the account from the
    environment (#1596): give them ``TEST_ACCOUNT``'s scope, and the
    default provider whatever the shell exports."""
    monkeypatch.setenv("ACCOUNT_SCOPE", TEST_ACCOUNT.scope_uuid)
    monkeypatch.delenv("SOURCE_MODE", raising=False)


@pytest.fixture(autouse=True)
def _reset_extractor_warning_budget(monkeypatch):
    """The shared rate-limited line budget (``warn_rate_limited``,
    attachment and other lines) is process-wide; give each test a fresh
    window so earlier tests cannot spend its budget."""
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
    # Direct extractor_child.run tests simulate a fresh process; counters
    # left by an earlier parser/extractor test must not become its N frames.
    extractors.drain_counters()
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
    """Run the extractor child (#1040, #1291, #1292, #1293, #1294) in
    this process for the OOXML, image, PDF and HTML conversions (the
    ``html`` extractor and message bodies) and container identification
    (#1416), through the same frames and parsing (progress frames as each
    page is read), so a test can patch a walk's budgets, stub Poppler or
    Tesseract or count calls. The ``xls`` extractor starts
    the real child, and so does ``eml``. A test marked ``real_extractor_child``
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
    in_process = OOXML_MODULES | {"image", "html", "pdf", "container"}

    def run_tool(argv, payload, *, on_output=None, **kwargs):
        child = str(_runner._CHILD)
        if child not in argv or argv[argv.index(child) + 1] not in in_process:
            return real(argv, payload, on_output=on_output, **kwargs)
        assert on_output is not None
        module, *options = argv[argv.index(child) + 1 :]
        # A real child starts with zero counters (#1314): set the test's
        # aside so the child sends only what this extraction counted. It
        # also has its own attempt state, which the dispatcher's must
        # not see changed: the parent applies only what crosses.
        before = extractors.drain_counters()
        attempt = dict(vars(extractors._attempt))
        try:
            output = extractor_child.run(
                module,
                payload,
                options,
                lambda: on_output(extractor_child.PROGRESS_FRAME),
            )
        finally:
            extractors.add_counters(before)
            vars(extractors._attempt).clear()
            vars(extractors._attempt).update(attempt)
        on_output(output)
        return _runner.ToolOutput(b"", truncated=False)

    monkeypatch.setattr(_runner, "run_tool", run_tool)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db", account=TEST_ACCOUNT)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def threader(db: Database) -> Threader:
    return Threader(db)


@pytest.fixture
def header_work(monkeypatch):
    from tests.work_counts import HeaderWork

    work = HeaderWork()
    work.install(monkeypatch)
    return work


@pytest.fixture
def reply_work(monkeypatch):
    from src import quoting

    from tests.work_counts import CountedReplyPattern

    lengths = []
    monkeypatch.setattr(
        quoting,
        "_REPLY_HEADER_PATTERNS",
        tuple(CountedReplyPattern(pattern, lengths) for pattern in quoting._REPLY_HEADER_PATTERNS),
    )
    return lengths


def drop_literal_schema(conn):
    """Undo v26 and v25 for historical migration fixtures, before rebuilding parents."""
    conn.execute("DROP TABLE IF EXISTS account_address_scope")
    conn.execute("DROP TRIGGER IF EXISTS literal_window_delete")
    for table in (
        "literal_long_fts",
        "literal_short_fts",
        "literal_windows",
        "literal_attachment_spans",
        "literal_attachment_state",
        "literal_state",
        "literal_scrub",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
