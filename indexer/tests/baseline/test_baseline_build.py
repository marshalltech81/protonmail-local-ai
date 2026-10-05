"""Tests for the retrieval-baseline builder (corpus, embedder, build).

The golden questions themselves run in ``mcp-server/tests/baseline``;
these keep the indexer-side inputs deterministic and the build clean.
"""

import json
import math
from pathlib import Path

import pytest
from src.database import EMBEDDING_DIM, Database

from tests.baseline.build import build
from tests.baseline.corpus import THREADS, write_maildir
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
        assert attachments == sum(len(m.attachments) for msgs in THREADS.values() for m in msgs)

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

    def test_refuses_non_empty_output_dir(self, tmp_path):
        (tmp_path / "leftover").write_text("x")
        with pytest.raises(RuntimeError, match="not empty"):
            build(tmp_path, _GOLDEN)
