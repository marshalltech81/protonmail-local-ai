"""Tests for the retrieval-baseline builder (corpus, embedder, build).

The golden questions themselves run in ``mcp-server/tests/baseline``;
these keep the indexer-side inputs deterministic and the build clean.
"""

import ast
import inspect
import io
import json
import logging
import math
import os
import re
import shutil
import sys
from pathlib import Path

import pypdf
import pytest
from PIL import Image
from src.database import EMBEDDING_DIM, Database
from src.extractors import EXTRACTOR_VERSIONS, _resolve_extractor

from tests.baseline.build import OCR_BINARIES, build, case_queries, check_capped_attachments
from tests.baseline.corpus import (
    _FIXTURES,
    CAPPED_ATTACHMENT_MAX_BYTES,
    CAPPED_ATTACHMENT_MAX_CHARS,
    CAPPED_OCR_MAX_PAGES,
    CHAR_CAPPED_FILENAME,
    OCR_CAPPED_PDF_FILENAME,
    OCR_CAPPED_TIFF_FILENAME,
    OCR_IMAGE_FILENAME,
    OCR_IMAGE_TEXT,
    OCR_PDF_PAGES,
    OCR_TIFF_FRAMES,
    THREADS,
    TOO_LARGE_FILENAME,
    _docx,
    _xlsx,
    thread_id,
    write_maildir,
)
from tests.baseline.fixtures import generate
from tests.baseline.hash_embedder import HashEmbedder, embed_text

_GOLDEN = Path(__file__).parents[3] / "mcp-server" / "tests" / "baseline" / "golden.json"

# The build runs OCR on t90-t92 (#908, #1113), so off CI the tests that
# build skip without Tesseract and Poppler, as the catdoc tests do
# (``test_legacy_office.py``); CI installs both, and
# ``test_ocr_binaries_are_installed_in_ci`` fails there if one is missing.
_IN_CI = bool(os.environ.get("CI"))
requires_ocr = pytest.mark.skipif(
    any(shutil.which(binary) is None for binary in OCR_BINARIES) and not _IN_CI,
    reason="tesseract, pdftoppm or pdfinfo is not installed (brew install tesseract poppler)",
)


def _norm(text: str) -> str:
    """Case-folded with whitespace normalised: how OCR'd words are matched."""
    return " ".join(text.split()).casefold()


def _aggregate(caplog, field: str) -> int:
    """The sum of ``field`` over the build's ``attachments n=...`` lines."""
    values = [
        int(m.group(1))
        for r in caplog.records
        if (m := re.search(rf"^attachments n=\d+ .*\b{field}=(\d+)\b", r.getMessage()))
    ]
    assert values
    return sum(values)


def test_ocr_binaries_are_installed_in_ci():
    """The building tests skip only off CI: CI installs Tesseract and
    Poppler, so a missing binary there is a failure, not a skip."""
    if not _IN_CI:
        pytest.skip("only checked in CI")
    assert all(shutil.which(binary) is not None for binary in OCR_BINARIES)


def test_ocr_binaries_cover_the_executables_the_ocr_path_starts():
    """Review round 2 on #908 (AGENTS.md: a list that must cover every
    item is checked against the code, not against itself): the preflight
    list equals the commands the OCR libraries start. pytesseract names
    its command in ``tesseract_cmd``; pdf2image names each Poppler
    command in a ``_get_command_path("...")`` call. Excluded, with the
    reason checked: ``pdftocairo``, which pdf2image runs only with
    ``use_pdftocairo=True``, and the PDF extractor never passes it."""
    import pdf2image.pdf2image as pdf2image_module
    import pytesseract
    from src.extractors import pdf as pdf_extractor

    started = {pytesseract.pytesseract.tesseract_cmd}
    started |= set(re.findall(r'_get_command_path\("(\w+)"', inspect.getsource(pdf2image_module)))
    excluded = {"pdftocairo"}
    assert excluded < started
    assert "pdftocairo" not in inspect.getsource(pdf_extractor)
    assert started - excluded == set(OCR_BINARIES)


# The TIFF tags Pillow's ``save_all`` writes for an 8-bit grey frame:
# geometry, sample layout, compression and strip placement. Any other
# tag, and any ASCII tag at all (Software 305, DateTime 306, Artist 315,
# ImageDescription 270, Make 271, Model 272, HostComputer 316), would be
# metadata the fixture must not carry.
_TIFF_STRUCTURAL_TAGS = {256, 257, 258, 259, 262, 273, 278, 279, 284}
_TIFF_ASCII_TYPE = 2


def _tiff_frames(raw: bytes) -> list[dict[int, int]]:
    """Each frame's tag number -> TIFF type, seeking frame by frame."""
    frames = []
    with Image.open(io.BytesIO(raw)) as image:
        assert image.format == "TIFF"
        while True:
            frames.append({tag: image.tag_v2.tagtype[tag] for tag in image.tag_v2})
            try:
                image.seek(len(frames))
            except EOFError:
                return frames


def test_ocr_fixtures_carry_no_metadata():
    """#908, #1113: the committed images name no author or tool: the PNG
    has only its header, data and end chunks, the PDF has no Info
    dictionary, no text layer and ``OCR_PDF_PAGES`` pages, and every
    frame of the TIFF carries only structural tags (no ASCII tag) and
    there are ``OCR_TIFF_FRAMES`` of them."""
    png = (_FIXTURES / OCR_IMAGE_FILENAME).read_bytes()
    chunks, offset = [], 8
    while offset < len(png):
        length = int.from_bytes(png[offset : offset + 4], "big")
        chunks.append(png[offset + 4 : offset + 8])
        offset += 12 + length
    assert chunks == [b"IHDR", b"IDAT", b"IEND"]
    raw = (_FIXTURES / OCR_CAPPED_PDF_FILENAME).read_bytes()
    assert b"/Info" not in raw
    reader = pypdf.PdfReader(io.BytesIO(raw))
    assert reader.metadata is None
    assert [page.extract_text() for page in reader.pages] == [""] * len(OCR_PDF_PAGES)
    assert len(OCR_PDF_PAGES) == CAPPED_OCR_MAX_PAGES + 1
    frames = _tiff_frames((_FIXTURES / OCR_CAPPED_TIFF_FILENAME).read_bytes())
    assert len(frames) == len(OCR_TIFF_FRAMES) == CAPPED_OCR_MAX_PAGES + 1
    for tags in frames:
        assert set(tags) <= _TIFF_STRUCTURAL_TAGS, sorted(set(tags) - _TIFF_STRUCTURAL_TAGS)
        assert _TIFF_ASCII_TYPE not in tags.values()


def test_generator_reads_the_corpus_text_from_its_source():
    """Review round 1 on #908: the generator must run before the images
    exist, so it reads the corpus's constants from the source instead of
    importing the corpus, which reads the images at import."""
    tree = ast.parse(Path(generate.__file__).read_text(encoding="utf-8"))
    imported = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("corpus" in name for name in imported), imported
    assert (
        generate.OCR_IMAGE_FILENAME,
        generate.OCR_IMAGE_TEXT,
        generate.OCR_CAPPED_PDF_FILENAME,
        generate.OCR_PDF_PAGES,
        generate.OCR_CAPPED_TIFF_FILENAME,
        generate.OCR_TIFF_FRAMES,
    ) == (
        OCR_IMAGE_FILENAME,
        OCR_IMAGE_TEXT,
        OCR_CAPPED_PDF_FILENAME,
        OCR_PDF_PAGES,
        OCR_CAPPED_TIFF_FILENAME,
        OCR_TIFF_FRAMES,
    )
    with pytest.raises(LookupError, match="NOT_A_CORPUS_CONSTANT"):
        generate._corpus_constant("NOT_A_CORPUS_CONSTANT")


def test_generator_writes_the_three_images(tmp_path):
    """The recipe writes a grey PNG of the fixture size, a PDF with one
    image page per line, no text layer and no Info dictionary, and a
    TIFF with one grey frame of the fixture size per line, every frame
    carrying only structural tags (#1113)."""
    png, pdf, tiff = generate.write(tmp_path)
    assert (png.name, pdf.name, tiff.name) == (
        OCR_IMAGE_FILENAME,
        OCR_CAPPED_PDF_FILENAME,
        OCR_CAPPED_TIFF_FILENAME,
    )
    with Image.open(png) as image:
        assert (image.format, image.mode, image.size) == ("PNG", "L", generate._SIZE)
    raw = pdf.read_bytes()
    assert b"/Info" not in raw
    reader = pypdf.PdfReader(io.BytesIO(raw))
    assert reader.metadata is None
    assert [page.extract_text() for page in reader.pages] == [""] * len(OCR_PDF_PAGES)
    frames = _tiff_frames(tiff.read_bytes())
    assert len(frames) == len(OCR_TIFF_FRAMES)
    assert all(set(tags) <= _TIFF_STRUCTURAL_TAGS for tags in frames)
    assert all(_TIFF_ASCII_TYPE not in tags.values() for tags in frames)
    with Image.open(tiff) as image:
        assert (image.mode, image.size) == ("L", generate._SIZE)
    # Deflated: the raw frames would be past the build's lowered
    # attachment byte cap and t92 would index as ``too_large``.
    assert tiff.stat().st_size < CAPPED_ATTACHMENT_MAX_BYTES


def _read_tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class TestCorpus:
    def test_maildir_is_byte_identical_across_runs(self, tmp_path):
        write_maildir(tmp_path / "a")
        write_maildir(tmp_path / "b")
        assert _read_tree(tmp_path / "a") == _read_tree(tmp_path / "b")

    def test_every_message_is_written(self, tmp_path):
        count = write_maildir(tmp_path)
        assert count == sum(len(msgs) for msgs in THREADS.values())
        assert len(_read_tree(tmp_path)) == count


class TestHashEmbedder:
    def test_deterministic_unit_vectors(self):
        a, b = embed_text("Roof repair estimate"), embed_text("Roof repair estimate")
        assert a == b
        assert len(a) == EMBEDDING_DIM
        assert math.isclose(math.fsum(x * x for x in a), 1.0)

    def test_empty_text_is_zero_vector(self):
        assert embed_text("  --  ") == [0.0] * EMBEDDING_DIM

    def test_trigrams_link_misspellings(self):
        def cos(x, y):
            return math.fsum(p * q for p, q in zip(x, y, strict=True))

        word = embed_text("hatchback")
        assert cos(word, embed_text("hatchbak")) > cos(word, embed_text("invoice"))

    def test_embed_batch_reports_completion(self):
        calls = []
        vectors = HashEmbedder().embed_batch(["a", "b"], on_batch_complete=lambda: calls.append(1))
        assert vectors == [embed_text("a"), embed_text("b")]
        assert calls == [1]


class TestBuild:
    @requires_ocr
    def test_indexes_whole_corpus(self, tmp_path):
        out = tmp_path / "out"
        assert build(out, _GOLDEN) == {"queued": 0, "dead": 0}

        db = Database(out / "mail.db")
        try:
            conn = db._conn
            threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            attachments = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
        finally:
            db.close()
        # One thread per corpus entry means every reply found its root.
        assert threads == len(THREADS)
        # Plus one: t86's attached email carries its own attachment (#909).
        listed = sum(len(m.attachments) for msgs in THREADS.values() for m in msgs)
        assert attachments == listed + 1

        vectors = json.loads((out / "query_vectors.json").read_text(encoding="utf-8"))
        golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
        assert set(vectors) == {q["query"] for q in golden["search"]} | set(
            golden["evidence_queries"]
        )

    def test_case_queries_are_what_each_tool_embeds(self):
        """#656: an ask_mailbox question is embedded; a summarize_thread
        case (a thread ID lookup) is not."""
        cases = {
            "cases": [
                {"tool": "ask_mailbox", "arguments": {"question": "Synthetic question?"}},
                {
                    "tool": "summarize_thread",
                    "arguments": {"thread_id": "t05.1@baseline.example", "style": "brief"},
                },
            ]
        }
        assert case_queries(cases) == {"Synthetic question?"}

    @requires_ocr
    def test_embeds_answer_eval_case_questions(self, tmp_path):
        cases = tmp_path / "cases.json"
        question = "Synthetic question about the roof?"
        cases.write_text(
            json.dumps(
                {
                    "cases": [
                        {"arguments": {"question": question}},
                        {"arguments": {"thread_id": "t05.1@baseline.example"}},
                    ]
                }
            )
        )
        out = tmp_path / "out"
        build(out, _GOLDEN, cases)

        vectors = json.loads((out / "query_vectors.json").read_text(encoding="utf-8"))
        golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
        assert set(vectors) == {q["query"] for q in golden["search"]} | set(
            golden["evidence_queries"]
        ) | {question}
        assert vectors[question] == embed_text(question)

    @requires_ocr
    def test_attachment_shapes_extract_as_documented(self, tmp_path, caplog):
        """#906: t78's real PDF and t81's PDF under a ``.txt`` name are
        extracted by the digital PDF extractor (t81 dispatched by MIME);
        t79 and t80 share one payload, so one extraction row serves both
        and the second occurrence is the build's only cache hit."""
        out = tmp_path / "out"
        with caplog.at_level(logging.INFO, logger="indexer"):
            build(out, _GOLDEN)

        cached = [
            int(m.group(1))
            for r in caplog.records
            if (m := re.search(r"^attachments n=\d+ .*\bcached=(\d+)\b", r.getMessage()))
        ]
        assert cached and sum(cached) == 1

        db = Database(out / "mail.db")
        try:
            rows = db._conn.execute(
                "SELECT a.thread_id, a.filename, a.content_type, a.attachment_id,"
                " e.extraction_status, e.extractor"
                " FROM attachments a JOIN attachment_extractions e USING (attachment_id)"
                " WHERE a.thread_id IN (?, ?, ?, ?) ORDER BY a.thread_id",
                tuple(thread_id(n) for n in (78, 79, 80, 81)),
            ).fetchall()
            pattern_id = rows[1][3]
            extraction_rows = db._conn.execute(
                "SELECT COUNT(*) FROM attachment_extractions WHERE attachment_id = ?",
                (pattern_id,),
            ).fetchone()[0]
        finally:
            db.close()
        pdf, pattern, handout, ticket = rows
        assert pdf[1:3] == ("honey-order.pdf", "application/pdf")
        assert pdf[4:] == ("success", f"pdf-digital@{EXTRACTOR_VERSIONS['pdf']}")
        assert [pattern[1], handout[1]] == ["pinwheel-pattern.txt", "guild-handout.txt"]
        assert pattern[3] == handout[3] and pattern[4] == "success"
        assert extraction_rows == 1
        assert ticket[1:3] == ("stargazing-ticket.txt", "application/pdf")
        assert _resolve_extractor(ticket[2], ticket[1]) == ("pdf", "mime")
        assert ticket[4:] == ("success", f"pdf-digital@{EXTRACTOR_VERSIONS['pdf']}")
        assert len({pdf[3], pattern[3], ticket[3]}) == 3

    @requires_ocr
    def test_format_shapes_extract_as_documented(self, tmp_path, caplog):
        """#909: t82's DOCX and t83's XLSX extract (the XLSX's second
        sheet included), t84's JSON is ``unsupported``, t85's blank text
        is ``empty``, t86's attached email is kept as an ``unsupported``
        container whose own attachment is extracted, with no parser cap
        firing, and t87's RFC 2231 filename is stored decoded."""
        out = tmp_path / "out"
        with caplog.at_level(logging.INFO, logger="indexer"):
            build(out, _GOLDEN)

        messages = [r.getMessage() for r in caplog.records]
        assert not [m for m in messages if m.startswith("parser work caps")]
        parser_caps = [
            int(m.group(1))
            for message in messages
            if (m := re.search(r"^attachments n=\d+ .*\bparser_caps_messages=(\d+)\b", message))
        ]
        assert parser_caps and sum(parser_caps) == 0

        db = Database(out / "mail.db")
        try:
            rows = db._conn.execute(
                "SELECT a.thread_id, a.filename, a.content_type, e.extraction_status,"
                " e.extractor, e.extracted_text"
                " FROM attachments a JOIN attachment_extractions e USING (attachment_id)"
                f" WHERE a.thread_id IN ({','.join('?' * 6)})"
                " ORDER BY a.thread_id, a.filename",
                tuple(thread_id(n) for n in range(82, 88)),
            ).fetchall()
        finally:
            db.close()
        shapes = [(tid.split(".")[0], *rest[:4]) for tid, *rest in rows]
        docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert shapes == [
            ("t82", "rehearsal-notes.docx", docx, "success", f"docx@{EXTRACTOR_VERSIONS['docx']}"),
            ("t83", "seed-swap.xlsx", xlsx, "success", f"xlsx@{EXTRACTOR_VERSIONS['xlsx']}"),
            ("t84", "kite-roster.json", "application/json", "unsupported", None),
            ("t85", "spring-rota.txt", "text/plain", "empty", f"text@{EXTRACTOR_VERSIONS['text']}"),
            ("t86", "crossing.txt", "text/plain", "success", f"text@{EXTRACTOR_VERSIONS['text']}"),
            ("t86", "ferry-crossing.eml", "message/rfc822", "unsupported", None),
            (
                "t87",
                "fête-des-Mélèzes.txt",
                "text/plain",
                "success",
                f"text@{EXTRACTOR_VERSIONS['text']}",
            ),
        ]
        text = {row[1]: row[5] for row in rows}
        assert "Ravensholm abbey" in text["rehearsal-notes.docx"]
        assert (
            "[Sheet: Pickup]\nCollect your sachets from the Quillon greenhouse"
            in (text["seed-swap.xlsx"])
        )
        assert text["kite-roster.json"] is None and text["spring-rota.txt"] is None
        assert "Corrigan" in text["crossing.txt"]
        assert text["ferry-crossing.eml"] is None

    @requires_ocr
    def test_capped_shapes_hit_the_build_caps(self, tmp_path, caplog):
        """#907: under the build's lowered caps, t88's attachment is
        ``too_large`` (no extractor, no text) and t89's is ``success``
        cut at the extracted-characters cap, its last sentence lost,
        with one ``extracted_chars`` WARNING and the aggregate counts."""
        out = tmp_path / "out"
        with caplog.at_level(logging.INFO, logger="indexer"):
            build(out, _GOLDEN)

        cap_warnings = [
            r for r in caplog.records if r.getMessage().startswith("extractor cap extracted_chars:")
        ]
        assert [r.levelno for r in cap_warnings] == [logging.WARNING]
        assert cap_warnings[0].getMessage().endswith(f" to {CAPPED_ATTACHMENT_MAX_CHARS} chars")
        assert "Kittiwake" not in caplog.text and "Corncrake" not in caplog.text

        def summed(field: str) -> int:
            values = [
                int(m.group(1))
                for r in caplog.records
                if (m := re.search(rf"^attachments n=\d+ .*\b{field}=(\d+)\b", r.getMessage()))
            ]
            assert values
            return sum(values)

        assert summed("too_large") == 1
        assert summed("extractor_caps") == 1

        db = Database(out / "mail.db")
        try:
            rows = db._conn.execute(
                "SELECT a.thread_id, a.filename, a.size_bytes, e.extraction_status,"
                " e.extractor, e.extracted_text"
                " FROM attachments a JOIN attachment_extractions e USING (attachment_id)"
                " WHERE a.thread_id IN (?, ?) ORDER BY a.thread_id",
                (thread_id(88), thread_id(89)),
            ).fetchall()
        finally:
            db.close()
        too_large, capped = rows
        assert too_large[1] == TOO_LARGE_FILENAME
        assert too_large[2] > CAPPED_ATTACHMENT_MAX_BYTES
        assert too_large[3:] == ("too_large", None, None)
        assert capped[1] == CHAR_CAPPED_FILENAME
        assert capped[3:5] == ("success", f"text@{EXTRACTOR_VERSIONS['text']}")
        assert len(capped[5]) == CAPPED_ATTACHMENT_MAX_CHARS
        assert capped[5].startswith("Corncrake Wheelers ride route notes.")
        # The cut ends between words: the answer evaluation's index check
        # (``index_identity``) refuses chunk text holding a partial word.
        assert not capped[5][-1].isalnum()
        assert "Kittiwake" not in capped[5]
        (source,) = [a.text for m in THREADS[89] for a in m.attachments]
        assert isinstance(source, str) and source.index("Kittiwake") > CAPPED_ATTACHMENT_MAX_CHARS

    @requires_ocr
    def test_build_refuses_an_unexpected_capped_attachment(self, tmp_path):
        """#907: the lowered caps must cut only t88 and t89; a corpus edit
        that pushes another attachment past either cap fails the build."""
        out = tmp_path / "out"
        build(out, _GOLDEN)
        check_capped_attachments(out / "mail.db")

        db = Database(out / "mail.db")
        try:
            with db._conn as conn:
                conn.execute(
                    "UPDATE attachment_extractions SET extraction_status = 'too_large',"
                    " extracted_text = NULL WHERE attachment_id = (SELECT attachment_id"
                    " FROM attachments WHERE filename = 'roof-estimate.txt')"
                )
        finally:
            db.close()
        with pytest.raises(RuntimeError, match="roof-estimate.txt"):
            check_capped_attachments(out / "mail.db")

    def test_ooxml_attachments_are_platform_independent(self, monkeypatch):
        """#909 review round 1: ``zipfile.ZipInfo`` records the creating
        system from ``sys.platform`` (0 on Windows, 3 elsewhere), and the
        claimant IDs and ``index_sha256`` hash the message bytes, so the
        DOCX and XLSX threads must serialize identically on Windows."""

        # The corpus builds its payloads at import, so build them anew.
        def payloads() -> list[bytes]:
            return [_docx(("Ravensholm abbey",)), _xlsx({"Pickup": (("Quillon",),)})]

        here = payloads()
        monkeypatch.setattr(sys, "platform", "win32")
        assert payloads() == here

    @pytest.mark.parametrize("binary", OCR_BINARIES)
    def test_build_fails_naming_a_missing_ocr_binary(self, tmp_path, monkeypatch, binary):
        """#908: without Tesseract or Poppler the build stops up front
        with fixed text naming the binary, instead of indexing t90 and
        t91 as failed extractions."""
        monkeypatch.setattr(
            shutil, "which", lambda name, *a, **k: None if name == binary else f"/usr/bin/{name}"
        )
        out = tmp_path / "out"
        with pytest.raises(RuntimeError, match=rf"need {binary} on PATH"):
            build(out, _GOLDEN)
        assert not out.exists()

    @requires_ocr
    def test_ocr_shapes_extract_as_documented(self, tmp_path, caplog):
        """#908, #1113: t90's PNG is read by the image OCR extractor,
        t91's scanned PDF by the PDF extractor's OCR fallback up to the
        build's lowered page cap, and t92's multipage TIFF by the image
        OCR extractor frame by frame up to the same cap: the first pages
        and frames are extracted and the last of each is not, with one
        ``pdf OCR capped`` and one ``image OCR capped`` WARNING (#885),
        in indexing order, and the aggregate counts. OCR'd words are
        matched case-insensitively with whitespace normalised, since
        Tesseract versions differ."""
        out = tmp_path / "out"
        with caplog.at_level(logging.INFO, logger="indexer"):
            build(out, _GOLDEN)

        capped = [r for r in caplog.records if "OCR capped" in r.getMessage()]
        assert [(r.levelno, r.getMessage()) for r in capped] == [
            (
                logging.WARNING,
                f"pdf OCR capped at {CAPPED_OCR_MAX_PAGES} of {len(OCR_PDF_PAGES)} scanned pages",
            ),
            (
                logging.WARNING,
                f"image OCR capped at {CAPPED_OCR_MAX_PAGES} of at least"
                f" {CAPPED_OCR_MAX_PAGES + 1} frames",
            ),
        ]
        assert _aggregate(caplog, "ocr_capped_pdfs") == 1
        assert _aggregate(caplog, "ocr_pages_skipped") == len(OCR_PDF_PAGES) - CAPPED_OCR_MAX_PAGES
        assert _aggregate(caplog, "ocr_capped_images") == 1
        for line in (OCR_IMAGE_TEXT, *OCR_PDF_PAGES, *OCR_TIFF_FRAMES):
            assert _norm(line).split()[0] not in caplog.text.casefold()

        db = Database(out / "mail.db")
        try:
            rows = db._conn.execute(
                "SELECT a.filename, a.content_type, e.extraction_status, e.extractor,"
                " e.extracted_text"
                " FROM attachments a JOIN attachment_extractions e USING (attachment_id)"
                " WHERE a.thread_id IN (?, ?, ?) ORDER BY a.thread_id",
                (thread_id(90), thread_id(91), thread_id(92)),
            ).fetchall()
        finally:
            db.close()
        image, scan, fax = rows
        assert image[:4] == (
            OCR_IMAGE_FILENAME,
            "image/png",
            "success",
            f"image-ocr@{EXTRACTOR_VERSIONS['image']}",
        )
        assert _norm(OCR_IMAGE_TEXT) in _norm(image[4])
        assert scan[:4] == (
            OCR_CAPPED_PDF_FILENAME,
            "application/pdf",
            "success",
            f"pdf-ocr@{EXTRACTOR_VERSIONS['pdf']}",
        )
        read, (lost,) = OCR_PDF_PAGES[:CAPPED_OCR_MAX_PAGES], OCR_PDF_PAGES[CAPPED_OCR_MAX_PAGES:]
        assert all(_norm(page) in _norm(scan[4]) for page in read)
        assert _norm(lost).split()[0] not in _norm(scan[4])
        assert fax[:4] == (
            OCR_CAPPED_TIFF_FILENAME,
            "image/tiff",
            "success",
            f"image-ocr@{EXTRACTOR_VERSIONS['image']}",
        )
        read_frames = OCR_TIFF_FRAMES[:CAPPED_OCR_MAX_PAGES]
        (lost_frame,) = OCR_TIFF_FRAMES[CAPPED_OCR_MAX_PAGES:]
        assert all(_norm(frame) in _norm(fax[4]) for frame in read_frames)
        assert _norm(lost_frame).split()[0] not in _norm(fax[4])

    def test_refuses_non_empty_output_dir(self, tmp_path):
        (tmp_path / "leftover").write_text("x")
        with pytest.raises(RuntimeError, match="not empty"):
            build(tmp_path, _GOLDEN)
