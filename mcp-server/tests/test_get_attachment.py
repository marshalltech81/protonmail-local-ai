"""``get_attachment``: one attachment occurrence's whole stored text, paged (#796).

The reader streams the stored text through a read-only ``blobopen`` in
fixed blocks: it decodes only up to the page, counts the rest without
keeping it, and never selects the column, so a row of any length (the
extraction cap can be off) costs one block plus one page of memory.
Pages are cut by the helper ``get_message`` uses. All data is synthetic.
"""

import asyncio
import io
import logging
import sqlite3
import time
import tracemalloc

import pytest
import src.lib.sqlite as sqlite_mod
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from src.lib.sqlite import OCR_DISABLED_ERRORS, TEXT_READ_BLOCK_BYTES, Database
from src.tools.retrieval import (
    _MESSAGE_BODY_PAGE_CHARS,
    _PAGE_WINDOW_CHARS,
    _body_page,
    register_retrieval_tools,
)

from tests.conftest import (
    FakeMCPServer,
    _insert_attachment,
    _insert_extraction,
    _insert_message,
    claimant_of,
)
from tests.test_sqlite import _open_built_db_conn

MARKER = "zq-synthetic-get-attachment-marker"
_PAGE = _MESSAGE_BODY_PAGE_CHARS

# Texts whose page cuts and block splits are worth checking: NUL,
# two-, three- and four-byte characters, combining marks and
# zero-width-joined sequences straddling the 20,000th character, a run
# of marks longer than the cut backoff, and lengths at the page edges.
_ZWJ_PAIR = "\U0001f469‍\U0001f4bb"
_CATALOGUE = {
    "ascii": "".join(f"line {i:06d} of the synthetic text\n" for i in range(2500)),
    "nul": ("a\x00b" * 9000) + "\x00" * 10 + ("c\x00" * 9000),
    "multibyte": ("é€\U0001f600x" * 13_000),
    "combining_on_cut": "a" * 19_999 + "é" + "b" * 100,
    "zwj_on_cut": "a" * 19_999 + _ZWJ_PAIR + "b" * 100,
    "zwj_before_cut": "a" * 19_998 + _ZWJ_PAIR + "b" * 100,
    "marks_run": "a" + "́" * 50_000,
    "astral_on_cut": "a" * 19_999 + "\U0001f600" * 3 + "\x00" + "z" * 20_000,
    "exact_page": "q" * _PAGE,
    "page_plus_one": "q" * (_PAGE + 1),
    "two_pages": "é" * (2 * _PAGE),
    "single": "\x00",
}


def _occ(n: int) -> str:
    return f"{claimant_of('carrier@example.com')}:occ-{n}"


def _db(tmp_path, texts: dict[int, tuple[str, str | None, str | None]], folder="INBOX"):
    """One message carrying an occurrence per ``texts`` entry:
    ``n -> (status, extracted_text, extraction_error)``; a ``None``
    status leaves the payload without an extraction row."""
    conn, path = _open_built_db_conn(tmp_path, "get-attachment.db")
    _insert_message(
        conn,
        message_id="carrier@example.com",
        thread_id="t1",
        sent_at="2024-02-01T09:00:00+00:00",
        folder=folder,
        has_attachments=True,
    )
    for n, (status, text, error) in texts.items():
        _insert_attachment(
            conn,
            message_id="carrier@example.com",
            thread_id="t1",
            attachment_id=f"p-{n}",
            filename=f"file-{n}.txt",
            content_type="text/plain",
            occurrence_id=_occ(n),
            extractor_module="text",
        )
        if status is not None:
            _insert_extraction(
                conn,
                attachment_id=f"p-{n}",
                status=status,
                extracted_text=text,
                error=error,
                extractor="text",
                extractor_module="text",
            )
    conn.commit()
    conn.close()
    return Database(str(path))


def _handler(db):
    server = FakeMCPServer()
    register_retrieval_tools(server, db)
    return server.tools["get_attachment"]


def _pages(handler, occurrence: str) -> list[dict]:
    pages, offset = [], 0
    while offset is not None:
        out = asyncio.run(handler(attachment_occurrence_id=occurrence, offset=offset))
        pages.append(out.structured_content | {"_prose": out.content[0].text})
        offset = out.structured_content["next_offset"]
        assert len(pages) < 1000
    return pages


class _CountingBlob:
    """A blob proxy that records the size of every read request and the
    bytes each returned."""

    def __init__(self, blob) -> None:
        self._blob = blob
        self.requests: list[int] = []
        self.returned = 0

    def read(self, n: int = -1) -> bytes:
        self.requests.append(n)
        data = self._blob.read(n)
        self.returned += len(data)
        return data


class _Decoded:
    """Bytes the reader passed to the UTF-8 decoder, over a test."""

    total = 0


def _counting_decoder_factory():
    base = sqlite_mod._UTF8_DECODER

    class Counting(base):  # type: ignore[valid-type,misc]
        def decode(self, data, final=False):
            _Decoded.total += len(data)
            return super().decode(data, final)

    return Counting


@pytest.fixture
def instrumented(monkeypatch):
    """Record every blob read, the bytes decoded, and every SQL
    statement the database runs."""
    blobs: list[_CountingBlob] = []
    statements: list[str] = []
    original_reader = sqlite_mod._read_text_window
    original_connect = Database._connect

    def reader(blob, offset, chars):
        counting = _CountingBlob(blob)
        blobs.append(counting)
        return original_reader(counting, offset, chars)

    def connect(self):
        conn = original_connect(self)
        conn.set_trace_callback(statements.append)
        return conn

    _Decoded.total = 0
    monkeypatch.setattr(sqlite_mod, "_read_text_window", reader)
    monkeypatch.setattr(sqlite_mod, "_UTF8_DECODER", _counting_decoder_factory())
    monkeypatch.setattr(Database, "_connect", connect)
    return blobs, statements


class TestReader:
    """``_read_text_window`` against the exact answer, ``text[offset:
    offset + chars]`` and ``len(text)``, over the catalogue, with blocks
    small enough that characters straddle them."""

    @pytest.mark.parametrize("name", sorted(_CATALOGUE))
    @pytest.mark.parametrize("block", [1, 3, 7, TEXT_READ_BLOCK_BYTES])
    def test_window_and_total_are_exact(self, monkeypatch, name, block):
        monkeypatch.setattr(sqlite_mod, "TEXT_READ_BLOCK_BYTES", block)
        text = _CATALOGUE[name]
        data = text.encode()
        for offset in sorted({0, 1, 2, _PAGE - 1, _PAGE, len(text) - 1, len(text), len(text) + 5}):
            if block < 7 and len(data) > 100_000:
                continue
            window, total = sqlite_mod._read_text_window(
                io.BytesIO(data), offset, _PAGE_WINDOW_CHARS
            )
            assert total == len(text)
            assert window == text[offset : offset + _PAGE_WINDOW_CHARS]

    def test_decoding_stops_at_the_window_and_the_rest_is_only_counted(self, monkeypatch):
        """Bytes decoded are at most the window's end plus one block,
        whatever follows; every read asks for one block."""
        monkeypatch.setattr(sqlite_mod, "_UTF8_DECODER", _counting_decoder_factory())
        _Decoded.total = 0
        text = "\U0001f600" * 300_000
        blob = _CountingBlob(io.BytesIO(text.encode()))
        window, total = sqlite_mod._read_text_window(blob, 1000, _PAGE_WINDOW_CHARS)
        assert window == text[1000 : 1000 + _PAGE_WINDOW_CHARS] and total == len(text)
        assert _Decoded.total <= 4 * 1000 + TEXT_READ_BLOCK_BYTES + 4 * _PAGE_WINDOW_CHARS
        assert set(blob.requests) == {TEXT_READ_BLOCK_BYTES}
        assert blob.returned == len(text.encode())

    def test_invalid_utf8_while_decoding_fails_closed(self):
        with pytest.raises(UnicodeDecodeError):
            sqlite_mod._read_text_window(io.BytesIO(b"ab\xff"), 0, 10)


class TestBoundedRead:
    """The whole row is never loaded: a stored text longer than the
    indexer's default 2,000,000-character cap stands for
    ``INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS=0``."""

    _TEXT = ("é\x00\U0001f600" + "x" * 97) * 50_000  # 5,000,000 characters

    @pytest.fixture
    def big(self, tmp_path):
        return _db(tmp_path, {0: ("success", self._TEXT, None)})

    @pytest.mark.parametrize("offset", [0, 1_234_567, 4_990_000, 5_000_000])
    def test_bytes_decoded_reads_and_sql_are_bounded(self, big, instrumented, offset):
        blobs, statements = instrumented
        started = time.perf_counter()
        found = big.get_attachment_text(_occ(0), offset, _PAGE_WINDOW_CHARS)
        assert time.perf_counter() - started < 10
        assert found is not None
        assert found.window == self._TEXT[offset : offset + _PAGE_WINDOW_CHARS]
        assert found.total_chars == len(self._TEXT)
        [blob] = blobs
        offset_bytes = len(self._TEXT[:offset].encode())
        assert _Decoded.total <= offset_bytes + TEXT_READ_BLOCK_BYTES + 4 * _PAGE_WINDOW_CHARS
        assert set(blob.requests) == {TEXT_READ_BLOCK_BYTES}
        # The counting pass reads each remaining byte once.
        assert blob.returned == len(self._TEXT.encode())
        # No statement selects the column; ``typeof`` reads its type only.
        assert statements
        for sql in statements:
            assert "extracted_text" not in sql.replace("typeof(e.extracted_text)", "")

    def test_memory_is_one_block_and_one_page(self, big):
        """The counting pass keeps nothing: Python allocations peak far
        below the 5 MB stored text."""
        stored = len(self._TEXT.encode())
        tracemalloc.start()
        try:
            found = big.get_attachment_text(_occ(0), 0, _PAGE_WINDOW_CHARS)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert found is not None and found.total_chars == len(self._TEXT)
        assert stored > 5_000_000
        assert peak < 1_500_000


class TestPaging:
    @pytest.mark.parametrize("name", sorted(_CATALOGUE))
    def test_pages_reconstruct_exactly_and_cut_where_get_message_does(self, tmp_path, name):
        text = _CATALOGUE[name]
        handler = _handler(_db(tmp_path, {0: ("success", text, None)}))
        pages = _pages(handler, _occ(0))
        assert "".join(p["text"] for p in pages) == text
        # The same cuts as get_message's _body_page.
        expected, offset = [], 0
        while offset is not None:
            page, offset = _body_page(text, offset)
            expected.append(page)
        assert [p["text"] for p in pages] == expected
        offset = 0
        for p in pages:
            assert p["text_offset"] == offset
            assert p["text_total_chars"] == len(text)
            assert len(p["text"]) <= _PAGE
            assert p["truncated"] is None
            assert p["unavailable_reason"] is None
            offset += len(p["text"])

    def test_prose_states_the_range_and_the_next_call(self, tmp_path):
        text = "x" * 20_000 + "TAIL" + "y" * 29_996
        out = asyncio.run(
            _handler(_db(tmp_path, {0: ("success", text, None)}))(attachment_occurrence_id=_occ(0))
        )
        prose = out.content[0].text
        assert "TAIL" not in prose
        assert "characters 1-20,000 of 50,000" in prose
        assert "30,000 more characters: call get_attachment with offset=20000" in prose
        assert out.structured_content["next_offset"] == 20_000
        assert "file-0.txt" in prose

    def test_offset_at_the_end_returns_an_empty_page(self, tmp_path):
        handler = _handler(_db(tmp_path, {0: ("success", "abc", None)}))
        out = asyncio.run(handler(attachment_occurrence_id=_occ(0), offset=3))
        assert out.structured_content["text"] == ""
        assert out.structured_content["next_offset"] is None
        assert "No text past offset 3; the text has 3 characters." in out.content[0].text

    @pytest.mark.parametrize("offset", [-1, 4, 10**9])
    def test_invalid_offset_is_rejected_and_only_the_field_logged(self, tmp_path, caplog, offset):
        handler = _handler(_db(tmp_path, {0: ("success", "abc", None)}))
        with caplog.at_level(logging.DEBUG), pytest.raises(ToolError, match="Error: offset"):
            asyncio.run(handler(attachment_occurrence_id=_occ(0), offset=offset))
        rejected = [r.getMessage() for r in caplog.records if "rejected" in r.getMessage()]
        assert rejected == ["rejected invalid argument: get_attachment.offset"]


class TestStatuses:
    _ROWS = {
        0: ("success", f"text with {MARKER}", None),
        1: ("empty", None, None),
        2: ("failed", None, f"stored error {MARKER}"),
        3: ("unsupported", None, "no extractor for this content type or filename extension"),
        4: ("unsupported", None, OCR_DISABLED_ERRORS[0]),
        5: ("unsupported", None, OCR_DISABLED_ERRORS[1]),
        6: ("too_large", None, "too large"),
        7: (None, None, None),
        8: ("success", None, None),
        9: ("success", "", None),
    }

    @pytest.fixture
    def handler(self, tmp_path):
        return _handler(_db(tmp_path, self._ROWS))

    @pytest.mark.parametrize(
        ("n", "text", "reason"),
        [
            (0, f"text with {MARKER}", None),
            (1, "", None),
            (2, None, "extraction failed"),
            (
                3,
                None,
                "no extractor could read this file: its type has no extractor, the "
                "extractor is missing from this image, or it declined the file (for "
                "example an encrypted PDF or a work cap)",
            ),
            (4, None, "the file needs OCR, which is off (INDEXER_OCR_ENABLED=false)"),
            (5, None, "the file needs OCR, which is off (INDEXER_OCR_ENABLED=false)"),
            (
                6,
                None,
                "the file is over the indexer's attachment size limit, so it was not extracted",
            ),
            (
                7,
                None,
                "no extraction is recorded for this attachment yet (not run yet, or "
                "attachment extraction is off)",
            ),
            (8, None, "the extraction succeeded but stored no text"),
            (9, "", None),
        ],
    )
    def test_each_status_returns_its_text_or_a_fixed_reason(self, handler, n, text, reason):
        out = asyncio.run(handler(attachment_occurrence_id=_occ(n)))
        data = out.structured_content
        assert data["text"] == text
        assert data["unavailable_reason"] == reason
        assert data["next_offset"] is None
        assert data["truncated"] is None
        assert data["attachment"]["attachment_occurrence_id"] == _occ(n)
        assert data["attachment"]["extraction_status"] == self._ROWS[n][0]
        if reason:
            assert reason in out.content[0].text
            assert data["text_total_chars"] == 0
        # A failed row's stored error never reaches the caller.
        assert f"stored error {MARKER}" not in str(data) + out.content[0].text

    def test_no_text_rejects_a_positive_offset(self, handler):
        with pytest.raises(ToolError, match=r"past the end of the text \(0 characters\)"):
            asyncio.run(handler(attachment_occurrence_id=_occ(2), offset=1))

    def test_unavailable_text_is_counted_on_the_timing_line(self, handler, caplog):
        caplog.set_level(logging.INFO)
        asyncio.run(handler(attachment_occurrence_id=_occ(7)))
        [line] = [r.getMessage() for r in caplog.records if r.name == "mcp.timings"]
        assert "tool=get_attachment outcome=ok" in line
        assert "'attachments': 1" in line
        assert "'text_unavailable_none': 1" in line

    def test_trash_is_read(self, tmp_path):
        handler = _handler(_db(tmp_path, {0: ("success", "kept", None)}, folder="Trash"))
        out = asyncio.run(handler(attachment_occurrence_id=_occ(0)))
        assert out.structured_content["text"] == "kept"
        assert out.structured_content["attachment"]["folder"] == "Trash"


class TestPrivacyAndErrors:
    def test_unknown_occurrence_is_an_error_with_a_fixed_log(self, tmp_path, caplog):
        handler = _handler(_db(tmp_path, {}))
        caplog.set_level(logging.DEBUG)
        with pytest.raises(ToolError, match="Attachment occurrence not found"):
            asyncio.run(handler(attachment_occurrence_id=MARKER))
        assert "get_attachment failed: not_found" in caplog.text
        assert MARKER not in caplog.text

    def test_repeated_unknown_occurrences_log_one_warning(self, tmp_path, caplog):
        """Codex round 1: a client can send unknown IDs as fast as it
        likes, so the not-found WARNING is rate-limited; every call
        still errors and gets its own timing line."""
        handler = _handler(_db(tmp_path, {}))
        caplog.set_level(logging.DEBUG)
        for n in range(5):
            with pytest.raises(ToolError, match="Attachment occurrence not found"):
                asyncio.run(handler(attachment_occurrence_id=f"{MARKER}-{n}"))
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings.count("get_attachment failed: not_found") == 1
        timing = [r.getMessage() for r in caplog.records if r.name == "mcp.timings"]
        assert len(timing) == 5
        assert all("outcome=error" in line for line in timing)
        assert MARKER not in caplog.text

    def test_the_not_found_summary_counts_the_suppressed_lines(self, caplog):
        """The limiter's summary after its window, with the count."""
        from src.tools.retrieval import _not_found_log

        now = [0.0]
        limiter = _not_found_log(clock=lambda: now[0])
        caplog.set_level(logging.INFO)
        for _ in range(4):
            limiter.record("not_found")
        now[0] = 61.0
        limiter.record("not_found")
        messages = [r.getMessage() for r in caplog.records]
        assert messages == [
            "get_attachment failed: not_found",
            "get_attachment failed in the last 61s: not_found=4",
            "get_attachment failed: not_found",
        ]

    def test_mail_values_never_reach_the_log(self, tmp_path, caplog):
        db = _db(tmp_path, {0: ("success", f"{MARKER} " * 30_000, None)})
        handler = _handler(db)
        caplog.set_level(logging.DEBUG)
        _pages(handler, _occ(0))
        assert "'offset': 20000" in caplog.text
        assert MARKER not in caplog.text
        assert _occ(0) not in caplog.text

    def test_a_database_error_is_reported_by_type(self, tmp_path, monkeypatch, caplog):
        db = _db(tmp_path, {0: ("success", "x", None)})

        def boom(*_a):
            raise sqlite3.OperationalError(MARKER)

        monkeypatch.setattr(db, "get_attachment_text", boom)
        with pytest.raises(ToolError, match="Error: OperationalError"):
            asyncio.run(_handler(db)(attachment_occurrence_id=_occ(0)))
        assert "get_attachment error: OperationalError" in caplog.text
        assert MARKER not in caplog.text

    def test_a_real_client_gets_schema_valid_output_and_the_guidance(self, tmp_path):
        """Through FastMCP: the output is checked against the published
        schema, and the guidance is in the description a client gets."""
        db = _db(tmp_path, {0: ("success", "x" * 25_000, None), 1: ("failed", None, None)})
        server = FastMCP("get-attachment-test")
        register_retrieval_tools(server, db)

        async def run():
            async with Client(server) as client:
                tool = next(t for t in await client.list_tools() if t.name == "get_attachment")
                ok = await client.call_tool_mcp(
                    "get_attachment", {"attachment_occurrence_id": _occ(0), "offset": 20_000}
                )
                failed = await client.call_tool_mcp(
                    "get_attachment", {"attachment_occurrence_id": _occ(1)}
                )
                return tool, ok, failed

        tool, ok, failed = asyncio.run(run())
        assert not ok.is_error and not failed.is_error
        assert ok.structured_content["text"] == "x" * 5_000
        assert failed.structured_content["text"] is None
        description = tool.description or ""
        for phrase in ("query_attachments", "may be remote", "20,000 characters", "truncated"):
            assert phrase in description
