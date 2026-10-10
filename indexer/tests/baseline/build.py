"""Build the retrieval-baseline index.

Step 1 of 2 (step 2 is ``mcp-server/tests/baseline``): write the
synthetic Maildir, run the real ``initial_index`` over it with the
hashed embedder, and write the golden questions' query vectors. The
two services cannot share one Python process (both are a top-level
``src`` package), so the mcp-server side reads the database and the
precomputed vectors from ``out_dir`` instead of re-implementing the
embedder. Before indexing, the build records the loopback
hash-embedding service (``embed_server.py``) as the index's embedder
identity, with the port it was allocated (``record_embedder_identity``).

Usage, from ``indexer/``:

    uv run python -m tests.baseline.build <out_dir> <golden.json> [<cases.json>]

``cases.json`` (optional) is the answer-quality evaluation's case file
(``mcp-server/tests/answer_eval/cases.json``); the text each case's
tool embeds (``case_queries``: an ``ask_mailbox`` question, an
``extract_from_emails`` query, a ``brief_issue`` topic or a ``check_conclusion`` conclusion; a
``summarize_thread`` case looks its thread up by ID and embeds nothing) gets a query vector too, so the
evaluation can run the tools against this index.

The build lowers two attachment caps so the capped-attachment shapes
(t88, t89, #907) fit in small fixtures: ``INDEXER_ATTACHMENT_MAX_BYTES``
to ``CAPPED_ATTACHMENT_MAX_BYTES`` (64 KiB; production default 32 MB)
and ``INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS`` to
``CAPPED_ATTACHMENT_MAX_CHARS`` (20,000; production default 2,000,000).
The baseline's capped results are therefore not production behaviour.
``check_capped_attachments`` fails the build if any other attachment is
cut by them.

The OCR shapes (t90-t92, #908, #1113) run Tesseract and Poppler, so the
build needs ``tesseract``, ``pdftoppm`` and ``pdfinfo`` on ``PATH``
(macOS: ``brew install tesseract poppler``) and fails naming the missing
one, rather than recording the shapes as failed or OCR-disabled. It
forces OCR on (``INDEXER_OCR_ENABLED``) and lowers
``INDEXER_OCR_MAX_PAGES`` to ``CAPPED_OCR_MAX_PAGES`` (2; production
default 20), so t91's three-page scan has a page past the cap and t92's
three-frame TIFF a frame past it.
"""

import json
import shutil
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src import main
from src.database import EMBEDDING_DIM, Database
from src.embed_identity import verify_or_record_embedder
from src.embedder import EmbeddingBackend, OpenAIEmbedder
from src.queue import IndexingQueue
from src.threader import Threader

from tests.baseline.corpus import (
    CAPPED_ATTACHMENT_MAX_BYTES,
    CAPPED_ATTACHMENT_MAX_CHARS,
    CAPPED_OCR_MAX_PAGES,
    CHAR_CAPPED_FILENAME,
    TOO_LARGE_FILENAME,
    write_maildir,
)
from tests.baseline.embed_server import MODEL as HASH_MODEL
from tests.baseline.embed_server import serve
from tests.baseline.hash_embedder import HashEmbedder

# The binaries the OCR shapes run: pytesseract starts ``tesseract``, and
# pdf2image starts Poppler's ``pdfinfo`` (the page count, before every
# render) and ``pdftoppm`` (the render). A partial Poppler install can
# have one without the other (review round 1), so both are checked.
# ``test_ocr_binaries_cover_the_executables_the_ocr_path_starts`` checks
# this list against the commands those libraries name (review round 2).
OCR_BINARIES = ("tesseract", "pdftoppm", "pdfinfo")


def require_ocr_binaries() -> None:
    """Raise ``RuntimeError`` naming the first OCR binary missing from
    ``PATH``, so a build without it fails up front instead of recording
    t90-t92 as failed extractions."""
    for binary in OCR_BINARIES:
        if shutil.which(binary) is None:
            raise RuntimeError(
                f"the baseline's OCR shapes (t90-t92) need {binary} on PATH; install"
                " Tesseract and Poppler (macOS: brew install tesseract poppler;"
                " Debian/Ubuntu: apt-get install tesseract-ocr poppler-utils)"
            )


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


def case_queries(cases: dict) -> set[str]:
    """The text each answer-evaluation case's tool embeds for retrieval:
    ``arguments.question`` (``ask_mailbox``), ``query``
    (``extract_from_emails``, #1137), ``topic`` (``brief_issue``) or
    ``conclusion`` (``check_conclusion``, #1240). A ``summarize_thread``
    case names its thread by ID and embeds nothing, so it contributes no
    query."""
    return {
        case["arguments"][name]
        for case in cases["cases"]
        for name in ("question", "query", "topic", "conclusion")
        if isinstance(case["arguments"].get(name), str)
    }


def record_embedder_identity(db: Database) -> None:
    """Record the loopback hash-embedding service (``embed_server``) as
    the index's embedder, as the indexer does at startup: the production
    ``OpenAIEmbedder`` fetches the calibration vector from the service,
    and the row holds provider ``openai``, the endpoint the kernel
    allocated and ``HASH_MODEL``. mcp-server's identity check then
    accepts the index only against that endpoint, so a later run serves
    the service again on the recorded port (#1268).

    The service stops before indexing: ``initial_index`` still calls
    ``HashEmbedder`` directly. Over HTTP the vectors are the same, but
    ``OpenAIEmbedder`` rejects the zero vector ``embed_text`` returns for
    text without word characters, so routing the build through it could
    change what is indexed.
    """
    with serve() as endpoint:
        embedder = OpenAIEmbedder(endpoint, HASH_MODEL, api_key="unauthenticated")
        try:
            verify_or_record_embedder(
                db,
                embedder,
                provider="openai",
                endpoint=embedder.base_url,
                model=HASH_MODEL,
                dimensions=EMBEDDING_DIM,
            )
        finally:
            embedder.client.close()


def build(
    out_dir: Path,
    golden_path: Path,
    cases_path: Path | None = None,
    *,
    embedder: EmbeddingBackend | None = None,
    query_embedder: EmbeddingBackend | None = None,
    record_identity: Callable[[Database], None] = record_embedder_identity,
) -> dict[str, int]:
    """Build ``out_dir/mail.db`` and ``out_dir/query_vectors.json``.

    The query vectors cover the golden search queries, the semantic
    paraphrase questions and the evidence queries and, with
    ``cases_path``, the text every answer-evaluation case embeds
    (``case_queries``).

    ``embedder`` (default ``HashEmbedder``) embeds the chunks,
    ``query_embedder`` (default ``embedder``) the queries, and
    ``record_identity`` records the index's embedder identity before
    indexing; ``real_embedder.py`` passes a real provider's embedders
    and identity (#1439). ``make baseline`` uses the defaults.

    Returns the indexing queue's final status counts. Raises
    ``RuntimeError`` if any message failed to index, so a broken corpus
    or pipeline fails loudly rather than producing a partial baseline.
    """
    # A leftover index would make ``initial_index`` skip already-indexed
    # files and silently reuse stale state.
    if out_dir.exists() and any(out_dir.iterdir()):
        raise RuntimeError(f"{out_dir} is not empty; build into a fresh directory")
    require_ocr_binaries()
    out_dir.mkdir(parents=True, exist_ok=True)
    maildir = out_dir / "maildir"
    write_maildir(maildir)

    if embedder is None:
        embedder = HashEmbedder()
    if query_embedder is None:
        query_embedder = embedder
    db = Database(out_dir / "mail.db")
    try:
        record_identity(db)
        with (
            patch.object(main, "MAILDIR_PATH", maildir),
            patch.object(main, "_iter_maildir_messages", _sorted_walk),
            patch.object(main, "INDEXER_HEALTH_FILE", out_dir / "indexer-health"),
            patch.object(main, "INDEXER_ATTACHMENT_MAX_BYTES", CAPPED_ATTACHMENT_MAX_BYTES),
            patch.object(
                main, "INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS", CAPPED_ATTACHMENT_MAX_CHARS
            ),
            # OCR on whatever the environment says, and t91's scan one
            # page past the cap (#908) and t92's TIFF one frame past it
            # (#1113).
            patch.object(main, "INDEXER_OCR_ENABLED", True),
            patch.object(main, "INDEXER_OCR_MAX_PAGES", CAPPED_OCR_MAX_PAGES),
        ):
            main.initial_index(db, embedder, Threader(db), IndexingQueue(db))
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
    # ``semantic`` questions are ranked only by the real-embedder run
    # (#1439).
    queries = (
        {q["query"] for q in golden["search"]}
        | {q["query"] for q in golden.get("semantic", [])}
        | set(golden.get("evidence_queries", []))
    )
    if cases_path is not None:
        queries |= case_queries(json.loads(cases_path.read_text(encoding="utf-8")))
    ordered = sorted(queries)
    (out_dir / "query_vectors.json").write_text(
        json.dumps(dict(zip(ordered, query_embedder.embed_batch(ordered), strict=True))),
        encoding="utf-8",
    )
    return stats


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit("usage: python -m tests.baseline.build <out_dir> <golden.json> [<cases.json>]")
    cases = Path(sys.argv[3]) if len(sys.argv) == 4 else None
    print(build(Path(sys.argv[1]), Path(sys.argv[2]), cases))
