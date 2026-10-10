"""#1418: a cached attachment result records the configured limits that
cut it (``ocr_pages_cap``, ``digital_pages_cap``, ``extracted_chars_cap``:
the limit when it cut, 0 when not, NULL when unknown), and a result a
limit cut is extracted again once the operator raises that limit: at
cache lookup (``_cache_hit_short_circuits``) and by the startup sweep,
which re-queues every message using the row before the drain replaces
it. A row cached before the record that lost text is extracted once
(the bootstrap arm). The lookup and the sweep share one eligibility,
checked here over a catalogue of rows."""

from __future__ import annotations

import hashlib
import inspect
import itertools
import logging
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from src import attachment_indexing, extractors, main
from src.attachment_indexing import (
    _cache_hit_short_circuits,
    apply_attachment_writes,
    cap_bootstrap_due,
    cap_raised,
    prepare_attachment_writes,
    record_committed_outcomes,
)
from src.database import EMBEDDING_DIM, CompletenessClearing, Database
from src.extractors import (
    CAP_COLUMNS,
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_TOO_LARGE,
    STATUS_UNSUPPORTED,
    extract,
)
from src.parser import Attachment
from src.queue import REASON_INITIAL_SCAN, REASON_REEXTRACT, IndexingQueue
from src.threader import Threader
from src.timings import TimingAggregator

from tests.conftest import make_message, make_mock_embedder, make_ole2, make_thread

MARKER = "SYNTHETIC_1418_MARKER"
_UNIT_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIM - 1)


def _attachment(payload: bytes, filename: str = "note.txt", ctype: str = "text/plain"):
    return Attachment(
        filename=filename,
        content_type=ctype,
        size=len(payload),
        payload=payload,
        content_hash=hashlib.sha256(payload).hexdigest(),
    )


def _caps(result) -> tuple:
    return tuple(getattr(result, cap) for cap in CAP_COLUMNS)


# --------------------------------------------------------------------------
# What each extractor records


class TestExtractorsRecordTheLimitThatCut:
    @pytest.fixture(autouse=True)
    def _fresh_counts(self):
        extractors.drain_extractor_counts()
        yield
        extractors.drain_extractor_counts()

    @pytest.mark.parametrize(
        "limit, expected",
        [(10, (None, None, 10)), (10_000, (None, None, 0)), (None, (None, None, 0))],
        ids=["cut", "not-cut", "no-limit"],
    )
    def test_the_dispatcher_records_its_character_cap(self, limit, expected):
        result = extract(
            content_type="text/plain",
            filename="a.txt",
            payload=f"{MARKER} words".encode(),
            max_extracted_chars=limit,
        )
        assert result.status == STATUS_SUCCESS
        assert _caps(result) == expected

    def test_an_empty_result_records_its_caps(self):
        result = extract(content_type="text/plain", filename="a.txt", payload=b"   ")
        assert result.status == STATUS_EMPTY
        assert _caps(result) == (None, None, 0)

    @staticmethod
    def _pdf(monkeypatch, pages: list[str]):
        """Stub the PDF's page reads: ``pages`` is each page's digital
        text; OCR reads text from every page it is given."""
        from src.extractors import pdf

        monkeypatch.setattr(pdf, "_extract_digital_pages", lambda payload, **_: list(pages))
        monkeypatch.setattr(
            pdf, "_extract_ocr", lambda payload, pages, **_: dict.fromkeys(pages, "ocr text")
        )

    def test_the_pdf_ocr_page_cap_is_recorded(self, monkeypatch):
        self._pdf(monkeypatch, ["", "", ""])
        result = extract(
            content_type="application/pdf", filename="a.pdf", payload=b"%PDF-1.7", max_ocr_pages=2
        )
        assert (result.status, result.extractor) == (STATUS_SUCCESS, "pdf-ocr@6")
        assert (result.ocr_pages_skipped, _caps(result)) == (1, (2, 0, 0))

    def test_a_pdf_inside_its_caps_records_zero(self, monkeypatch):
        self._pdf(monkeypatch, ["x" * 50, ""])
        result = extract(
            content_type="application/pdf",
            filename="a.pdf",
            payload=b"%PDF-1.7",
            max_ocr_pages=2,
            max_pdf_pages=5,
            max_extracted_chars=1_000,
        )
        assert _caps(result) == (0, 0, 0)

    def test_the_real_digital_walk_records_its_page_cap(self):
        """A three-page digital PDF read with a two-page limit."""
        import io

        import pypdf
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = pypdf.PdfWriter()
        for i in range(3):
            page = writer.add_blank_page(width=200, height=200)
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            stream = DecodedStreamObject()
            text = f"page {i} " + "digital words " * 5
            stream.set_data(f"BT /F1 8 Tf 10 100 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
        buf = io.BytesIO()
        writer.write(buf)
        payload = buf.getvalue()
        cut = extract(
            content_type="application/pdf", filename="a.pdf", payload=payload, max_pdf_pages=2
        )
        whole = extract(
            content_type="application/pdf", filename="a.pdf", payload=payload, max_pdf_pages=3
        )
        assert (cut.status, cut.extractor) == (STATUS_SUCCESS, "pdf-digital@6")
        assert _caps(cut) == (0, 2, 0)
        assert _caps(whole) == (0, 0, 0)
        assert "page 2" in (whole.text or "") and "page 2" not in (cut.text or "")

    def test_a_multipage_image_records_its_frame_cap(self, monkeypatch):
        import io

        from PIL import Image
        from src.extractors import image_child

        monkeypatch.setattr(image_child.pytesseract, "image_to_string", lambda *_a, **_k: "words")
        frames = [Image.new("L", (8, 8), 255) for _ in range(3)]
        buf = io.BytesIO()
        frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])
        cut = extract(
            content_type="image/tiff", filename="a.tiff", payload=buf.getvalue(), max_ocr_pages=2
        )
        whole = extract(
            content_type="image/tiff", filename="a.tiff", payload=buf.getvalue(), max_ocr_pages=3
        )
        assert _caps(cut) == (2, None, 0)
        assert _caps(whole) == (0, None, 0)

    @pytest.mark.parametrize(
        "chars, ceiling, bound, expected",
        [(100, 1_000, "chars", (None, None, 100)), (500, 1_000, "ceiling", (None, None, 0))],
    )
    def test_a_raw_tool_records_only_a_cut_at_the_character_bound(
        self, monkeypatch, caplog, chars, ceiling, bound, expected
    ):
        """The doc / ppt output cap is the lower of four bytes a character
        and a hardcoded ceiling: only a cut at the configured bound is
        recorded, and the WARNING names the bound as a fixed token."""
        from src.extractors import ppt
        from src.extractors._runner import ToolOutput

        def run_tool(_argv, _payload, *, max_output_bytes, **_kwargs):
            # Four bytes a character, so a cut at the ceiling yields no
            # more characters than the setting allows.
            return ToolOutput(("\U0001f600" * (max_output_bytes // 4)).encode(), True)

        home = Path(__import__("tempfile").mkdtemp())
        (home / "jre" / "bin").mkdir(parents=True)
        (home / "jre" / "bin" / "java").touch()
        monkeypatch.setattr(ppt, "PPT_HOME", home)
        monkeypatch.setattr(ppt, "run_tool", run_tool)
        monkeypatch.setattr(ppt, "_MAX_OUTPUT_BYTES", ceiling)
        caplog.set_level(logging.INFO)
        result = extract(
            content_type="application/vnd.ms-powerpoint",
            filename=f"{MARKER}.ppt",
            payload=make_ole2("PowerPoint Document"),
            max_extracted_chars=chars,
        )
        assert result.status == STATUS_SUCCESS
        assert _caps(result) == expected
        [line] = [r for r in caplog.records if "ppt_output_bytes" in r.getMessage()]
        assert line.levelno == logging.WARNING
        assert f"(bound={bound})" in line.getMessage()
        assert MARKER not in caplog.text

    @pytest.mark.parametrize(
        "chars, ceiling, expected",
        [
            (100, 1_000, (None, None, 100)),
            (250, 1_000, (None, None, 0)),
            (300, 1_000, (None, None, 0)),
            (None, 1_000, (None, None, 0)),
        ],
        ids=["chars", "equal", "ceiling", "no-limit"],
    )
    def test_catdoc_records_only_a_cut_at_the_character_bound(
        self, monkeypatch, caplog, chars, ceiling, expected
    ):
        from src.extractors import doc
        from src.extractors._runner import ToolOutput

        def run_tool(_argv, _payload, *, max_output_bytes, **_kwargs):
            return ToolOutput(("\U0001f600" * (max_output_bytes // 4)).encode(), True)

        monkeypatch.setattr(doc.shutil, "which", lambda _name: "/bin/catdoc")
        monkeypatch.setattr(doc, "run_tool", run_tool)
        monkeypatch.setattr(doc, "_MAX_OUTPUT_BYTES", ceiling)
        caplog.set_level(logging.INFO)
        result = extract(
            content_type="application/msword",
            filename="a.doc",
            payload=make_ole2("WordDocument"),
            max_extracted_chars=chars,
        )
        assert _caps(result) == expected
        bound = "chars" if expected[2] else "ceiling"
        [line] = [r for r in caplog.records if "doc_output_bytes" in r.getMessage()]
        assert f"(bound={bound})" in line.getMessage()

    def test_other_statuses_record_nothing(self, monkeypatch):
        too_large = extract(
            content_type="text/plain", filename="a.txt", payload=b"x" * 9, max_bytes=4
        )
        unsupported = extract(content_type="application/zip", filename="a.zip", payload=b"x")

        def boom(payload, **opts):
            raise ValueError(MARKER)

        monkeypatch.setattr("src.extractors._safe_import", lambda module_name: boom)
        failed = extract(content_type="text/plain", filename="a.txt", payload=b"x")
        assert [(r.status, _caps(r)) for r in (too_large, unsupported, failed)] == [
            (STATUS_TOO_LARGE, (None, None, None)),
            (STATUS_UNSUPPORTED, (None, None, None)),
            (STATUS_FAILED, (None, None, None)),
        ]

    def test_every_configured_limit_has_a_cap_column(self):
        """The dispatcher's parameters are the independent source: each
        configured limit that can cut text is recorded in a column, and
        every other parameter is excluded with its reason."""
        limits = {
            "max_ocr_pages": extractors.CAP_OCR_PAGES,
            "max_pdf_pages": extractors.CAP_DIGITAL_PAGES,
            "max_extracted_chars": extractors.CAP_EXTRACTED_CHARS,
        }
        excluded = {
            "content_type": "the label, not a limit",
            "filename": "the label, not a limit",
            "payload": "the bytes",
            "ocr_enabled": "OCR off is its own unsupported / kept-row path (#300)",
            "max_bytes": "skips the whole payload as too_large, refreshed by #693",
            "ocr_timeout_seconds": "a timeout fails the extraction; it never cuts text",
            "on_progress": "a callback",
        }
        params = set(inspect.signature(extract).parameters)
        assert params == set(limits) | set(excluded)
        assert sorted(limits.values()) == sorted(CAP_COLUMNS)


# --------------------------------------------------------------------------
# One eligibility for the lookup and the sweep

_STATUSES = (STATUS_SUCCESS, STATUS_EMPTY, STATUS_FAILED, STATUS_UNSUPPORTED)
_STAMPS = ("pdf-ocr@6", "pdf-digital@6", "image-ocr@4", None)
_RECORDS = (None, 0, 1)
_CAP_VALUES = (None, 0, 10)
_SKIPPED = (None, 0, 3)
# (ocr_enabled, OCR pages, digital pages, chars): 0 is no limit for the
# last two; the OCR page limit is at least 1.
_SETTINGS = (
    (True, 10, 10, 10),
    (True, 20, 10, 10),
    (True, 5, 5, 5),
    (True, 10, 0, 0),
    (True, 10, 20, 20),
    (False, 20, 0, 0),
    (False, 10, 10, 10),
    (True, 1, 1, 1),
)


def _catalogue() -> list[dict]:
    rows = []
    for i, (status, stamp, record, skipped, ocr, digital, chars) in enumerate(
        itertools.product(
            _STATUSES, _STAMPS, _RECORDS, _SKIPPED, _CAP_VALUES, _CAP_VALUES, _CAP_VALUES
        )
    ):
        rows.append(
            {
                "name": f"m{i}",
                "extraction_status": status,
                "extractor": stamp,
                "text_complete": record,
                "extracted_text": f"{MARKER} {i}" if status == STATUS_SUCCESS else None,
                "ocr_pages_skipped": skipped,
                "ocr_pages_cap": ocr,
                "digital_pages_cap": digital,
                "extracted_chars_cap": chars,
            }
        )
    return rows


@pytest.fixture(scope="module")
def catalogue_db(tmp_path_factory):
    """One message per catalogue row, each with one occurrence using its
    own cached row."""
    db = Database(tmp_path_factory.mktemp("cat") / "mail.db")
    rows = _catalogue()
    for row in rows:
        msg = make_message(
            message_id=f"{row['name']}@example.com", filepath=f"/maildir/cur/{row['name']}"
        )
        db.upsert_thread(make_thread(messages=[msg], thread_id=f"t-{row['name']}"), _UNIT_VECTOR)
        db._conn.execute(
            "INSERT INTO attachments (attachment_occurrence_id, claimant_id, attachment_id, "
            "thread_id, filename, content_type, size_bytes, seen_at, extractor_module, "
            "text_complete) VALUES (?, ?, ?, ?, 'f.pdf', 'application/pdf', 1, '2026-01-01', "
            "'pdf', 0)",
            (f"occ-{row['name']}", msg.claimant_id, f"h-{row['name']}", f"t-{row['name']}"),
        )
        db._conn.execute(
            "INSERT INTO attachment_extractions (attachment_id, extractor_module, "
            "extraction_status, extractor, extracted_text, extraction_error, extracted_at, "
            "text_complete, ocr_pages_skipped, ocr_pages_cap, digital_pages_cap, "
            "extracted_chars_cap) VALUES (?, 'pdf', ?, ?, ?, NULL, '2026-01-01', ?, ?, ?, ?, ?)",
            (
                f"h-{row['name']}",
                row["extraction_status"],
                row["extractor"],
                row["extracted_text"],
                row["text_complete"],
                row["ocr_pages_skipped"],
                row["ocr_pages_cap"],
                row["digital_pages_cap"],
                row["extracted_chars_cap"],
            ),
        )
        db._conn.commit()
    yield db, rows
    db.close()


@pytest.mark.parametrize("settings", _SETTINGS, ids=lambda s: "-".join(map(str, s)))
def test_the_sweep_sql_and_the_lookup_select_the_same_rows(catalogue_db, settings):
    db, rows = catalogue_db
    ocr_enabled, ocr_pages, digital, chars = settings
    raised, bootstrap = db.find_cap_refresh_attachment_filepaths(
        ocr_enabled=ocr_enabled,
        max_ocr_pages=ocr_pages,
        max_pdf_pages=digital,
        max_extracted_chars=chars,
    )
    limits = {"max_ocr_pages": ocr_pages, "max_pdf_pages": digital, "max_extracted_chars": chars}
    expect_raised = {
        f"/maildir/cur/{r['name']}"
        for r in rows
        if cap_raised(r, ocr_enabled=ocr_enabled, **limits)
    }
    expect_bootstrap = {
        f"/maildir/cur/{r['name']}" for r in rows if cap_bootstrap_due(r, ocr_enabled=ocr_enabled)
    }
    assert raised == expect_raised
    assert bootstrap == expect_bootstrap
    # The catalogue exercises every arm under this setting set as a whole.
    assert not raised & bootstrap
    attachment = _attachment(b"x")
    for row in rows:
        if row["extraction_status"] not in {STATUS_SUCCESS, STATUS_EMPTY}:
            continue
        due = f"/maildir/cur/{row['name']}" in raised | bootstrap
        hit = _cache_hit_short_circuits(row, attachment, "pdf", ocr_enabled, 10_000_000, **limits)
        assert hit is not due, row


def test_the_catalogue_reaches_every_arm(catalogue_db):
    """Guards the differential above: across the settings, each arm
    selects some rows and leaves others."""
    db, rows = catalogue_db
    seen_raised = seen_bootstrap = 0
    for ocr_enabled, ocr_pages, digital, chars in _SETTINGS:
        raised, bootstrap = db.find_cap_refresh_attachment_filepaths(
            ocr_enabled=ocr_enabled,
            max_ocr_pages=ocr_pages,
            max_pdf_pages=digital,
            max_extracted_chars=chars,
        )
        assert len(raised) < len(rows) and len(bootstrap) < len(rows)
        seen_raised += len(raised)
        seen_bootstrap += len(bootstrap)
    assert seen_raised and seen_bootstrap


@pytest.mark.parametrize(
    "recorded, current, due",
    [
        # OCR pages: only a higher value lifts the cut.
        ((10, None, None), (11, 0, 0), True),
        ((10, None, None), (10, 0, 0), False),
        ((10, None, None), (9, 0, 0), False),
        # Digital pages and chars: higher, or off (0).
        ((0, 10, None), (20, 11, 0), True),
        ((0, 10, None), (20, 0, 0), True),
        ((0, 10, None), (20, 10, 0), False),
        ((0, 10, None), (20, 9, 0), False),
        ((None, None, 10), (20, 0, 11), True),
        ((None, None, 10), (20, 0, 0), True),
        ((None, None, 10), (20, 0, 10), False),
        ((None, None, 10), (20, 0, 9), False),
        # Did not cut, or unknown: never due by this arm.
        ((0, 0, 0), (20, 0, 0), False),
        ((None, None, None), (20, 0, 0), False),
    ],
)
def test_raising_qualifies_and_lowering_never_does(recorded, current, due):
    row = dict(zip(CAP_COLUMNS, recorded, strict=True))
    row.update(extraction_status=STATUS_SUCCESS, extractor="pdf-digital@6", ocr_pages_skipped=0)
    ocr_pages, digital, chars = current
    assert (
        cap_raised(
            row,
            ocr_enabled=True,
            max_ocr_pages=ocr_pages,
            max_pdf_pages=digital,
            max_extracted_chars=chars,
        )
        is due
    )


# --------------------------------------------------------------------------
# Lookup


def _setup(tmp_path) -> Database:
    db = Database(tmp_path / "mail.db")
    db.upsert_thread(
        make_thread(messages=[make_message(message_id="msg@x")], thread_id="thread-x"),
        _UNIT_VECTOR,
    )
    return db


def _prepare(db, attachment, **overrides):
    kwargs = dict(
        attachment=attachment,
        claimant_id="msg@x",
        db=db,
        chunk_target_tokens=350,
        chunk_max_tokens=500,
        chunk_overlap_tokens=60,
        ocr_enabled=True,
        max_bytes=10_000_000,
        max_ocr_pages=20,
    )
    kwargs.update(overrides)
    return prepare_attachment_writes(**kwargs)


def _apply(db, plan):
    stored = db.get_chunk_ids_for_message("msg@x", attachment_id=plan.attachment.content_hash)
    new = [c for c in plan.chunks if c.chunk_id not in stored]
    plan.embeddings_by_chunk_id = {c.chunk_id: _UNIT_VECTOR for c in new}
    with db.transaction():
        apply_attachment_writes(plan=plan, claimant_id="msg@x", thread_id="thread-x", db=db)
    return new


class TestLookup:
    PAYLOAD = (f"{MARKER} " + "capword " * 40).encode()

    def _counting(self, monkeypatch) -> list[int | None]:
        calls: list[int | None] = []
        real = attachment_indexing.extract_attachment

        def counting(**kwargs):
            calls.append(kwargs["max_extracted_chars"])
            return real(**kwargs)

        monkeypatch.setattr(attachment_indexing, "extract_attachment", counting)
        return calls

    @staticmethod
    def _row(db, attachment):
        row = db.get_attachment_extraction(attachment.content_hash, "text")
        return (row["extraction_status"], row["text_complete"], *(row[c] for c in CAP_COLUMNS))

    @pytest.mark.parametrize(
        "later, extracted, row",
        [
            (50, False, (STATUS_SUCCESS, 0, None, None, 50)),
            (40, False, (STATUS_SUCCESS, 0, None, None, 50)),
            # Raised but still cutting: the new limit is recorded.
            (60, True, (STATUS_SUCCESS, 0, None, None, 60)),
            (None, True, (STATUS_SUCCESS, 1, None, None, 0)),
        ],
    )
    def test_a_raised_character_cap_re_extracts_and_rewrites_the_row(
        self, tmp_path, monkeypatch, later, extracted, row
    ):
        db = _setup(tmp_path)
        attachment = _attachment(self.PAYLOAD)
        _apply(db, _prepare(db, attachment, max_extracted_chars=50))
        assert self._row(db, attachment) == (STATUS_SUCCESS, 0, None, None, 50)
        calls = self._counting(monkeypatch)
        plan = _prepare(db, attachment, max_extracted_chars=later)
        assert calls == ([later] if extracted else [])
        assert plan.cached is not extracted
        new = _apply(db, plan)
        # The row and the occurrence's chunks in one transaction; the
        # diff-write path embeds only chunks that changed.
        assert self._row(db, attachment) == row
        assert db._conn.execute("SELECT text_complete FROM attachments").fetchone()[0] == row[1]
        if extracted:
            assert len(new) >= 1
            # Served from then on.
            calls.clear()
            _prepare(db, attachment, max_extracted_chars=later)
            assert calls == []
        if later is None:
            text = " ".join(r["text"] for r in db._conn.execute("SELECT text FROM message_chunks"))
            assert text.count("capword") == 40

    def test_a_legacy_incomplete_row_is_extracted_once(self, tmp_path, monkeypatch):
        db = _setup(tmp_path)
        attachment = _attachment(self.PAYLOAD)
        _apply(db, _prepare(db, attachment, max_extracted_chars=50))
        db._conn.execute(
            "UPDATE attachment_extractions SET ocr_pages_cap = NULL, digital_pages_cap = NULL, "
            "extracted_chars_cap = NULL"
        )
        db._conn.commit()
        calls = self._counting(monkeypatch)
        _apply(db, _prepare(db, attachment, max_extracted_chars=50))
        assert calls == [50]
        # Still cut by the same limit, now recorded: not extracted again.
        assert self._row(db, attachment) == (STATUS_SUCCESS, 0, None, None, 50)
        _apply(db, _prepare(db, attachment, max_extracted_chars=50))
        assert calls == [50]

    def test_an_ocr_row_is_kept_while_ocr_is_off(self, tmp_path, monkeypatch):
        db = _setup(tmp_path)
        attachment = _attachment(self.PAYLOAD)
        _apply(db, _prepare(db, attachment, max_extracted_chars=50))
        db._conn.execute("UPDATE attachment_extractions SET extractor = 'text-ocr@3'")
        db._conn.commit()
        calls = self._counting(monkeypatch)
        _prepare(db, attachment, max_extracted_chars=None, ocr_enabled=False)
        assert calls == []
        _prepare(db, attachment, max_extracted_chars=None, ocr_enabled=True)
        assert calls == [None]


class TestCutThatRemainsOnACachedResult:
    """A cut that remains on a result served from the cache is counted
    and logged at commit, as a fresh extraction's (#1201 for images)."""

    @pytest.fixture(autouse=True)
    def _fresh_counts(self):
        extractors.drain_extractor_counts()
        yield
        extractors.drain_extractor_counts()

    @pytest.mark.parametrize(
        "stamp, caps, counts, line",
        [
            (
                "image-ocr@4",
                (20, None, 0),
                {"ocr_capped_images": 1, "extractor_caps": 0},
                "image OCR capped: cached result stopped at 20 frames",
            ),
            (
                "pdf-digital@6",
                (0, 500, 0),
                {"ocr_capped_images": 0, "extractor_caps": 1},
                "extractor cap pdf_digital_pages: cached result stopped at 500 pages",
            ),
            (
                "docx@7",
                (None, None, 2_000_000),
                {"ocr_capped_images": 0, "extractor_caps": 1},
                "extractor cap extracted_chars: cached result was cut at 2000000 chars",
            ),
        ],
    )
    def test_it_is_counted_and_logged(self, caplog, stamp, caps, counts, line):
        caplog.set_level(logging.INFO)
        plan = attachment_indexing.AttachmentWritePlan(
            attachment=_attachment(MARKER.encode(), filename=f"{MARKER}.bin"),
            occurrence_id="occ",
            status=STATUS_SUCCESS,
            extraction_to_persist=None,
            cached=True,
            caps=caps,
            text_extractor=stamp,
        )
        record_committed_outcomes([plan])
        drained = extractors.drain_extractor_counts()
        assert {k: drained[k] for k in counts} == counts
        [record] = [r for r in caplog.records if "cached result" in r.getMessage()]
        assert record.levelno == logging.WARNING
        assert record.getMessage() == line
        assert MARKER not in caplog.text

    def test_a_fresh_or_uncut_result_is_not_counted_again(self, caplog):
        caplog.set_level(logging.INFO)
        plans = [
            attachment_indexing.AttachmentWritePlan(
                attachment=_attachment(b"x"),
                occurrence_id="a",
                status=STATUS_SUCCESS,
                extraction_to_persist=None,
                cached=False,
                caps=(20, 500, 9),
                text_extractor="image-ocr@4",
            ),
            attachment_indexing.AttachmentWritePlan(
                attachment=_attachment(b"y"),
                occurrence_id="b",
                status=STATUS_SUCCESS,
                extraction_to_persist=None,
                cached=True,
                caps=(0, 0, 0),
                text_extractor="pdf-digital@6",
            ),
        ]
        record_committed_outcomes(plans)
        drained = extractors.drain_extractor_counts()
        assert drained["ocr_capped_images"] == drained["extractor_caps"] == 0
        assert "cached result" not in caplog.text


# --------------------------------------------------------------------------
# Database


def test_the_record_round_trips_and_survives_a_purge_and_restore(tmp_path):
    db = _setup(tmp_path)
    db.store_attachment_extraction(
        attachment_id="h",
        extractor_module="pdf",
        extraction_status=STATUS_SUCCESS,
        extractor="pdf-ocr@6",
        extracted_text=MARKER,
        extraction_error=None,
        text_complete=False,
        ocr_pages_cap=20,
        digital_pages_cap=0,
        extracted_chars_cap=0,
    )
    row = db.get_attachment_extraction("h", "pdf")
    assert tuple(row[c] for c in CAP_COLUMNS) == (20, 0, 0)
    db._conn.execute("DELETE FROM attachment_extractions")
    db._conn.commit()
    db.restore_attachment_extraction(row)
    restored = db.get_attachment_extraction("h", "pdf")
    assert tuple(restored) == tuple(row)


@pytest.mark.parametrize("column", CAP_COLUMNS)
def test_a_negative_value_is_refused(tmp_path, column):
    db = _setup(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        db._conn.execute(
            "INSERT INTO attachment_extractions (attachment_id, extractor_module, "
            f"extraction_status, extracted_at, {column}) VALUES ('h', 'pdf', 'success', 'x', -1)"
        )


# --------------------------------------------------------------------------
# The startup sweep, end to end


def _write_eml(path: Path, message_id: str, payload: bytes, filename: str) -> None:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "Notes"
    msg["Message-ID"] = f"<{message_id}>"
    msg["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"
    msg.set_content("See the attached notes.")
    msg.add_attachment(payload, maintype="text", subtype="plain", filename=filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(msg))


class TestStartupSweep:
    SHARED = (f"{MARKER} " + "sharedword " * 40).encode()
    SHORT = b"short words"

    def _drain(self, db, queue, *, batch_size=10):
        return main._drain_queue_batched(
            db,
            make_mock_embedder(_UNIT_VECTOR),
            Threader(db),
            queue,
            batch_size=batch_size,
            timing_aggregator=TimingAggregator(window=4),
            max_passes=1,
        )

    @pytest.fixture
    def mailbox(self, tmp_path, monkeypatch):
        """Three messages carrying one payload the 60-character cap cuts
        (one dead-lettered), and one carrying a payload it does not."""
        maildir = tmp_path / MARKER
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", True)
        monkeypatch.setattr(main, "INDEXER_OCR_MAX_PAGES", 20)
        monkeypatch.setattr(main, "INDEXER_PDF_MAX_DIGITAL_PAGES", 500)
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS", 60)
        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
        paths = {}
        for name, payload in (
            ("first", self.SHARED),
            ("second", self.SHARED),
            ("dead", self.SHARED),
            ("short", self.SHORT),
        ):
            path = maildir / "INBOX" / "cur" / f"{name}.eml"
            _write_eml(path, f"{name}@example.com", payload, f"{MARKER}-{name}.txt")
            queue.enqueue(str(path), REASON_INITIAL_SCAN)
            paths[name] = str(path)
        self._drain(db, queue)
        queue.enqueue(paths["dead"], REASON_INITIAL_SCAN)
        queue.mark_dead_terminal(paths["dead"], stage="embed", error="x")
        yield db, queue, paths
        db.close()

    @staticmethod
    def _queued(db) -> dict[str, str]:
        rows = db._conn.execute(
            "SELECT filepath, reason FROM indexing_jobs WHERE status = 'queued'"
        ).fetchall()
        return {r["filepath"]: r["reason"] for r in rows}

    @staticmethod
    def _cap_lines(caplog) -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.getMessage().startswith("cap refresh sweep")]

    @staticmethod
    def _shared_words(db, filepath) -> int:
        rows = db._conn.execute(
            "SELECT c.text FROM message_chunks c JOIN message_thread_map m "
            "ON m.claimant_id = c.claimant_id WHERE m.filepath = ? "
            "AND c.attachment_id IS NOT NULL",
            (filepath,),
        ).fetchall()
        return " ".join(r["text"] for r in rows).count("sharedword")

    def test_the_same_or_a_lower_limit_queues_nothing(self, mailbox, monkeypatch, caplog):
        db, queue, _paths = mailbox
        caplog.set_level(logging.INFO)
        for chars in (60, 30):
            monkeypatch.setattr(main, "INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS", chars)
            assert main._requeue_stale_extractions(db, queue) == 0
        assert self._cap_lines(caplog) == []
        assert self._queued(db) == {}

    @pytest.mark.parametrize("raised", [10_000, 0], ids=["higher", "off"])
    def test_a_raised_limit_queues_every_message_using_the_row_once(
        self, mailbox, monkeypatch, caplog, raised
    ):
        db, queue, paths = mailbox
        assert self._shared_words(db, paths["first"]) < 40
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS", raised)
        caplog.set_level(logging.INFO)
        assert main._requeue_stale_extractions(db, queue) == 2
        assert self._queued(db) == {
            paths["first"]: REASON_REEXTRACT,
            paths["second"]: REASON_REEXTRACT,
        }
        [line] = self._cap_lines(caplog)
        assert line.levelno == logging.INFO
        assert line.getMessage() == (
            "cap refresh sweep (INDEXER_OCR_MAX_PAGES=20 INDEXER_PDF_MAX_DIGITAL_PAGES=500 "
            f"INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS={raised}): 3 message(s) with an "
            "attachment result a since-raised limit cut, 0 with one cached before the cap "
            "record that lost text (bootstrap, once); re-queued 2, already queued 0, "
            "skipped 1 dead-lettered (run make requeue-dead to refresh them)."
        )
        assert MARKER not in caplog.text
        # The occurrences due a refresh are unknown until re-indexed.
        assert {
            r["text_complete"]
            for r in db._conn.execute(
                "SELECT a.text_complete FROM attachments a JOIN message_thread_map m "
                "ON m.claimant_id = a.claimant_id WHERE m.filepath IN (?, ?, ?)",
                (paths["first"], paths["second"], paths["dead"]),
            )
        } == {None}

        extractor = MagicMock(side_effect=attachment_indexing.extract_attachment)
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        self._drain(db, queue)
        # One extraction for the shared row; both messages get all its text.
        assert extractor.call_count == 1
        assert self._shared_words(db, paths["first"]) == 40
        assert self._shared_words(db, paths["second"]) == 40
        caplog.clear()
        assert main._requeue_stale_extractions(db, queue) == 0
        assert self._cap_lines(caplog) == []

    def test_an_interrupted_drain_finishes_after_a_restart(self, mailbox, monkeypatch):
        """The shared row is replaced only after every message using it is
        queued: a restart after the first message's pass still leaves the
        second queued, and its pass is served the new row."""
        db, queue, paths = mailbox
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS", 0)
        assert main._requeue_stale_extractions(db, queue) == 2
        pending_at_extraction: list[bool] = []
        real = attachment_indexing.extract_attachment

        def spy(**kwargs):
            pending_at_extraction.append(
                queue.has_pending_row(paths["first"]) and queue.has_pending_row(paths["second"])
            )
            return real(**kwargs)

        monkeypatch.setattr(attachment_indexing, "extract_attachment", spy)
        self._drain(db, queue, batch_size=1)
        assert pending_at_extraction == [True]
        # "Restart": the row is fresh now, so the sweep finds nothing new,
        # and the other message is still queued.
        assert main._requeue_stale_extractions(db, queue) == 0
        assert len(self._queued(db)) == 1
        self._drain(db, queue)
        assert pending_at_extraction == [True]
        assert self._shared_words(db, paths["first"]) == 40
        assert self._shared_words(db, paths["second"]) == 40

    def test_an_already_queued_message_is_counted_and_left(self, mailbox, monkeypatch, caplog):
        db, queue, paths = mailbox
        queue.enqueue(paths["first"], REASON_INITIAL_SCAN)
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS", 0)
        caplog.set_level(logging.INFO)
        assert main._requeue_stale_extractions(db, queue) == 1
        assert self._queued(db) == {
            paths["first"]: REASON_INITIAL_SCAN,
            paths["second"]: REASON_REEXTRACT,
        }
        [line] = self._cap_lines(caplog)
        assert "re-queued 1, already queued 1, skipped 1 dead-lettered" in line.getMessage()

    def test_legacy_rows_are_bootstrapped_once(self, mailbox, monkeypatch, caplog):
        db, queue, paths = mailbox
        db._conn.execute(
            "UPDATE attachment_extractions SET ocr_pages_cap = NULL, digital_pages_cap = NULL, "
            "extracted_chars_cap = NULL"
        )
        db._conn.commit()
        caplog.set_level(logging.INFO)
        # Same limit: only the incomplete legacy row is due, once.
        assert main._requeue_stale_extractions(db, queue) == 2
        [line] = self._cap_lines(caplog)
        assert (
            "0 message(s) with an attachment result a since-raised limit cut, 3 with one "
            "cached before the cap record that lost text (bootstrap, once); re-queued 2"
        ) in line.getMessage()
        extractor = MagicMock(side_effect=attachment_indexing.extract_attachment)
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        self._drain(db, queue)
        assert extractor.call_count == 1
        rows = {
            r["extracted_text"].count("sharedword") if r["extracted_text"] else 0: tuple(
                r[c] for c in CAP_COLUMNS
            )
            for r in db._conn.execute("SELECT * FROM attachment_extractions")
        }
        # The shared row is still cut at 60 characters, now recorded; the
        # complete short row keeps its NULL record (it lost nothing).
        assert (None, None, 60) in rows.values()
        caplog.clear()
        assert main._requeue_stale_extractions(db, queue) == 0
        assert self._cap_lines(caplog) == []

    def test_an_ocr_row_is_not_queued_while_ocr_is_off(self, mailbox, monkeypatch):
        db, queue, _paths = mailbox
        db._conn.execute("UPDATE attachment_extractions SET extractor = 'text-ocr@3'")
        db._conn.commit()
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS", 0)
        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", False)
        assert main._requeue_stale_extractions(db, queue) == 0
        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", True)
        assert main._requeue_stale_extractions(db, queue) == 2

    def test_the_sweep_clears_through_the_shared_clearing(self, mailbox):
        """Each qualifying occurrence with a record is cleared through the
        sweep's bounded clearing, as the other refresh classes are."""
        db, _queue, _paths = mailbox
        assessed = CompletenessClearing(db)
        raised, bootstrap = db.find_cap_refresh_attachment_filepaths(
            ocr_enabled=True,
            max_ocr_pages=20,
            max_pdf_pages=500,
            max_extracted_chars=0,
            assessed=assessed,
        )
        assessed.flush()
        assert (len(raised), len(bootstrap), assessed.cleared) == (3, 0, 3)


class TestOcrOffToOn:
    """While OCR is off, a ``success`` / ``empty`` PDF row that OCR wrote
    or that OCR pages cut or skipped stays out of both arms (a refresh
    then could only record "OCR disabled" or forget its unread pages);
    once OCR is on it is refreshed once. Each row's expected outcome is
    written out by hand, not derived from the predicate."""

    # name: (stamp, text_complete, ocr_pages_skipped, caps); the limits in
    # force are 20 OCR pages, 20 digital pages and no character limit.
    ROWS = {
        # OCR read nothing on the capped scanned pages: a digital stamp.
        "digital_only": ("pdf-digital@6", 0, 4, (10, 0, 0)),
        # Digital and OCR text: an -ocr stamp.
        "mixed": ("pdf-ocr@6", 0, 2, (10, 0, 0)),
        # Cached before v11 (no cap record) with scanned pages skipped.
        "legacy_skipped": ("pdf-digital@6", 0, 3, (None, None, None)),
        # Cut by the OCR page limit, no skipped count recorded.
        "ocr_cap": ("pdf-digital@6", 0, None, (10, 0, 0)),
        # Control: no OCR involvement, cut by the digital page limit.
        "digital_cap_only": ("pdf-digital@6", 0, 0, (0, 10, 0)),
    }
    # Hand-written: which rows each start re-queues.
    OCR_OFF = {"digital_cap_only"}
    OCR_ON = {"digital_only", "mixed", "legacy_skipped", "ocr_cap"}

    @pytest.fixture
    def index(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
        monkeypatch.setattr(main, "INDEXER_OCR_MAX_PAGES", 20)
        monkeypatch.setattr(main, "INDEXER_PDF_MAX_DIGITAL_PAGES", 20)
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS", 0)
        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
        found = {}
        for name, (stamp, record, skipped, caps) in self.ROWS.items():
            attachment = _attachment(f"%PDF-1.7 {name}".encode(), f"{name}.pdf", "application/pdf")
            msg = make_message(message_id=f"{name}@example.com", filepath=f"/m/{name}")
            db.upsert_thread(make_thread(messages=[msg], thread_id=f"t-{name}"), _UNIT_VECTOR)
            db.upsert_attachment(
                claimant_id=msg.claimant_id,
                thread_id=f"t-{name}",
                attachment_id=attachment.content_hash,
                filename=attachment.filename,
                content_type=attachment.content_type,
                size_bytes=attachment.size,
                occurrence_id=f"occ-{name}",
                extractor_module="pdf",
            )
            db.store_attachment_extraction(
                attachment_id=attachment.content_hash,
                extractor_module="pdf",
                extraction_status=STATUS_SUCCESS,
                extractor=stamp,
                extracted_text=f"{MARKER} cut text",
                extraction_error=None,
                ocr_pages_skipped=skipped,
                text_complete=bool(record),
                ocr_pages_cap=caps[0],
                digital_pages_cap=caps[1],
                extracted_chars_cap=caps[2],
                # A PDF: no container identification applies (#1416).
                identifier="",
            )
            found[name] = (attachment, msg.claimant_id)
        yield db, queue, found
        db.close()

    @staticmethod
    def _queued(db) -> set[str]:
        return {
            r["filepath"].rsplit("/", 1)[1]
            for r in db._conn.execute("SELECT filepath FROM indexing_jobs WHERE status = 'queued'")
        }

    def _lookup(self, db, attachment, claimant, monkeypatch, *, ocr_enabled):
        fresh = extractors.ExtractionResult(
            status=STATUS_SUCCESS,
            extractor="pdf-ocr@6",
            text="fresh text",
            error=None,
            ocr_pages_skipped=0,
            text_complete=True,
            ocr_pages_cap=0,
            digital_pages_cap=0,
            extracted_chars_cap=0,
            identifier="",
        )
        extractor = MagicMock(return_value=fresh)
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        plan = prepare_attachment_writes(
            attachment=attachment,
            claimant_id=claimant,
            db=db,
            chunk_target_tokens=350,
            chunk_max_tokens=500,
            chunk_overlap_tokens=60,
            ocr_enabled=ocr_enabled,
            max_bytes=10_000_000,
            max_ocr_pages=20,
            max_pdf_pages=20,
            max_extracted_chars=None,
        )
        plan.embeddings_by_chunk_id = {c.chunk_id: _UNIT_VECTOR for c in plan.chunks}
        with db.transaction():
            apply_attachment_writes(
                plan=plan, claimant_id=claimant, thread_id=f"t-{attachment.filename[:-4]}", db=db
            )
        return extractor.call_count

    def test_kept_while_ocr_is_off_and_refreshed_once_it_is_on(self, index, monkeypatch):
        db, queue, found = index
        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", False)
        main._requeue_stale_extractions(db, queue)
        assert self._queued(db) == self.OCR_OFF
        extracted_off = {
            name
            for name, (attachment, claimant) in found.items()
            if self._lookup(db, attachment, claimant, monkeypatch, ocr_enabled=False)
        }
        assert extracted_off == self.OCR_OFF
        db._conn.execute("DELETE FROM indexing_jobs")
        db._conn.commit()

        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", True)
        main._requeue_stale_extractions(db, queue)
        assert self._queued(db) == self.OCR_ON
        extracted_on = {
            name
            for name, (attachment, claimant) in found.items()
            if self._lookup(db, attachment, claimant, monkeypatch, ocr_enabled=True)
        }
        assert extracted_on == self.OCR_ON
        db._conn.execute("DELETE FROM indexing_jobs")
        db._conn.commit()

        # Once: the refreshed rows record their caps and are served.
        main._requeue_stale_extractions(db, queue)
        assert self._queued(db) == set()
        assert not any(
            self._lookup(db, attachment, claimant, monkeypatch, ocr_enabled=True)
            for attachment, claimant in found.values()
        )
