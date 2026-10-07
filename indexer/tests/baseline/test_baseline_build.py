"""Tests for the retrieval-baseline builder (corpus, embedder, build).

The golden questions themselves run in ``mcp-server/tests/baseline``;
these keep the indexer-side inputs deterministic and the build clean.
"""

import json
import logging
import math
import re
import sys
from pathlib import Path

import pytest
from src.database import EMBEDDING_DIM, Database
from src.extractors import EXTRACTOR_VERSIONS, _resolve_extractor

from tests.baseline.build import build, check_capped_attachments
from tests.baseline.corpus import (
    CAPPED_ATTACHMENT_MAX_BYTES,
    CAPPED_ATTACHMENT_MAX_CHARS,
    CHAR_CAPPED_FILENAME,
    THREADS,
    TOO_LARGE_FILENAME,
    _docx,
    _xlsx,
    thread_id,
    write_maildir,
)
from tests.baseline.hash_embedder import HashEmbedder, embed_text

_GOLDEN = Path(__file__).parents[3] / "mcp-server" / "tests" / "baseline" / "golden.json"


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

    def test_embeds_answer_eval_case_questions(self, tmp_path):
        cases = tmp_path / "cases.json"
        question = "Synthetic question about the roof?"
        cases.write_text(json.dumps({"cases": [{"arguments": {"question": question}}]}))
        out = tmp_path / "out"
        build(out, _GOLDEN, cases)

        vectors = json.loads((out / "query_vectors.json").read_text(encoding="utf-8"))
        golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
        assert set(vectors) == {q["query"] for q in golden["search"]} | set(
            golden["evidence_queries"]
        ) | {question}
        assert vectors[question] == embed_text(question)

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

    def test_refuses_non_empty_output_dir(self, tmp_path):
        (tmp_path / "leftover").write_text("x")
        with pytest.raises(RuntimeError, match="not empty"):
            build(tmp_path, _GOLDEN)
