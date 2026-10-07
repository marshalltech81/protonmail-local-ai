"""Build the retrieval-baseline index.

Step 1 of 2 (step 2 is ``mcp-server/tests/baseline``): write the
synthetic Maildir, run the real ``initial_index`` over it with the
hashed embedder, and write the golden questions' query vectors. The
two services cannot share one Python process (both are a top-level
``src`` package), so the mcp-server side reads the database and the
precomputed vectors from ``out_dir`` instead of re-implementing the
embedder.

Usage, from ``indexer/``:

    uv run python -m tests.baseline.build <out_dir> <golden.json> [<cases.json>]

``cases.json`` (optional) is the answer-quality evaluation's case file
(``mcp-server/tests/answer_eval/cases.json``); each case's
``arguments.question`` gets a query vector too, so the evaluation can
run ``ask_mailbox`` against this index.

The build lowers two attachment caps so the capped-attachment shapes
(t88, t89, #907) fit in small fixtures: ``INDEXER_ATTACHMENT_MAX_BYTES``
to ``CAPPED_ATTACHMENT_MAX_BYTES`` (64 KiB; production default 32 MB)
and ``INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS`` to
``CAPPED_ATTACHMENT_MAX_CHARS`` (20,000; production default 2,000,000).
The baseline's capped results are therefore not production behaviour.
``check_capped_attachments`` fails the build if any other attachment is
cut by them.
"""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src import main
from src.database import Database
from src.queue import IndexingQueue
from src.threader import Threader

from tests.baseline.corpus import (
    CAPPED_ATTACHMENT_MAX_BYTES,
    CAPPED_ATTACHMENT_MAX_CHARS,
    CHAR_CAPPED_FILENAME,
    TOO_LARGE_FILENAME,
    write_maildir,
)
from tests.baseline.hash_embedder import HashEmbedder, embed_text


def _sorted_walk(root: Path):
    # Production walks in filesystem order (creation order on APFS, hash
    # order on ext4). Sorting by the corpus's zero-padded ``<seq>.eml``
    # file name, not the full path, keeps roots ahead of replies across
    # folders (a ``Sent`` root before its ``INBOX`` reply) on every
    # platform, so threading — and therefore the baseline — is identical.
    files = (p for p in root.rglob("*") if p.is_file() and p.parent.name in ("cur", "new"))
    return iter(sorted(files, key=lambda p: p.name))


def check_capped_attachments(db_path: Path) -> None:
    """Raise ``RuntimeError`` unless the lowered caps cut exactly the two
    capped shapes: t88's attachment ``too_large`` and t89's extracted
    text at the character cap. Every other baseline attachment is a few
    KB, so a corpus edit that pushes one past a cap is caught here."""
    with closing(sqlite3.connect(db_path)) as conn:
        too_large = sorted(
            name
            for (name,) in conn.execute(
                "SELECT a.filename FROM attachments a JOIN attachment_extractions e"
                " USING (attachment_id) WHERE e.extraction_status = 'too_large'"
            )
        )
        truncated = sorted(
            name
            for (name,) in conn.execute(
                "SELECT a.filename FROM attachments a JOIN attachment_extractions e"
                " USING (attachment_id) WHERE length(e.extracted_text) >= ?",
                (CAPPED_ATTACHMENT_MAX_CHARS,),
            )
        )
    if too_large != [TOO_LARGE_FILENAME] or truncated != [CHAR_CAPPED_FILENAME]:
        raise RuntimeError(
            "the build's lowered attachment caps cut unexpected attachments:"
            f" too_large={too_large} truncated={truncated}"
        )


def build(out_dir: Path, golden_path: Path, cases_path: Path | None = None) -> dict[str, int]:
    """Build ``out_dir/mail.db`` and ``out_dir/query_vectors.json``.

    The query vectors cover the golden search queries and evidence
    queries and, with ``cases_path``, every answer-evaluation case's
    question.

    Returns the indexing queue's final status counts. Raises
    ``RuntimeError`` if any message failed to index, so a broken corpus
    or pipeline fails loudly rather than producing a partial baseline.
    """
    # A leftover index would make ``initial_index`` skip already-indexed
    # files and silently reuse stale state.
    if out_dir.exists() and any(out_dir.iterdir()):
        raise RuntimeError(f"{out_dir} is not empty; build into a fresh directory")
    out_dir.mkdir(parents=True, exist_ok=True)
    maildir = out_dir / "maildir"
    write_maildir(maildir)

    db = Database(out_dir / "mail.db")
    try:
        with (
            patch.object(main, "MAILDIR_PATH", maildir),
            patch.object(main, "_iter_maildir_messages", _sorted_walk),
            patch.object(main, "INDEXER_HEALTH_FILE", out_dir / "indexer-health"),
            patch.object(main, "INDEXER_ATTACHMENT_MAX_BYTES", CAPPED_ATTACHMENT_MAX_BYTES),
            patch.object(
                main, "INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS", CAPPED_ATTACHMENT_MAX_CHARS
            ),
        ):
            main.initial_index(db, HashEmbedder(), Threader(db), IndexingQueue(db))
        stats = IndexingQueue(db).stats()
    finally:
        db.close()
    unfinished = {k: v for k, v in stats.items() if v}
    if unfinished:
        raise RuntimeError(f"baseline corpus did not index cleanly: {unfinished}")
    check_capped_attachments(out_dir / "mail.db")

    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    # ``evidence_queries`` are get_evidence lookups the outstanding-items
    # reachability checks make (#798); they rank nothing in the snapshot.
    queries = {q["query"] for q in golden["search"]} | set(golden.get("evidence_queries", []))
    if cases_path is not None:
        cases = json.loads(cases_path.read_text(encoding="utf-8"))
        queries |= {c["arguments"]["question"] for c in cases["cases"]}
    (out_dir / "query_vectors.json").write_text(
        json.dumps({q: embed_text(q) for q in sorted(queries)}), encoding="utf-8"
    )
    return stats


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit("usage: python -m tests.baseline.build <out_dir> <golden.json> [<cases.json>]")
    cases = Path(sys.argv[3]) if len(sys.argv) == 4 else None
    print(build(Path(sys.argv[1]), Path(sys.argv[2]), cases))
