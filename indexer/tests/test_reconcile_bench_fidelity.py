"""The #1218 sizing benchmark's synthetic index against the indexer.

``mcp-server/scripts/reconcile_bench.py`` writes its index row by row,
so it runs in the mcp-server image at 200,000 messages. Every value it
writes must be one the indexer writes for the same mail. Here the
benchmark renders each synthetic message as a Maildir file
(``render_eml``), the indexer's own first index (parser, threader,
chunker, extractors, writer) indexes those files, and every row and
schema object the benchmark builds is compared with what the indexer
stored (#1376 review round 29: the corpus had drifted from production
one dimension at a time).
"""

import importlib.util
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
from src import main
from src.database import EMBEDDING_DIM, Database
from src.entities import AuthorityRules
from src.queue import IndexingQueue
from src.threader import Threader

from tests.conftest import make_mock_embedder

_SCRIPT = Path(__file__).resolve().parents[2] / "mcp-server" / "scripts" / "reconcile_bench.py"

# Tables the benchmark builds. The indexer writes others (threads,
# vectors, the thread map, the queue); no measured statement reads them,
# and a statement that did would fail on the benchmark's index.
_TABLES = (
    "messages",
    "message_participants",
    "message_participant_names",
    "message_chunks",
    "message_chunks_fts",
    "entities",
    "attachments",
    "attachments_fts",
    "attachment_extractions",
    "pending_deletions",
)

# Values only the wall clock decides: the time the indexer wrote the row.
_CLOCK = {"indexed_at", "first_indexed_at", "chunked_at", "seen_at", "extracted_at"}


@pytest.fixture(scope="module")
def bench():
    # The script puts mcp-server/ first on sys.path for its own phases;
    # this suite's ``src`` is the indexer's, imported above.
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location("reconcile_bench", _SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # Registered before it runs: a dataclass looks its module up there.
        sys.modules["reconcile_bench"] = module
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


def _index_with_the_indexer(bench, tmp_path, monkeypatch, n, **shape) -> Path:
    maildir = tmp_path / "maildir"
    vendors: dict[str, str] = {}
    for i in range(n):
        path = Path(bench.maildir_path(i, shape["identity"], shape["records"], str(maildir)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bench.render_eml(i, **shape))
        for _, address, _ in bench._shape(
            i, shape["identity"], shape["records"], shape.get("references", 0)
        )["people"]:
            cls, _rule = bench.authority(address)
            if cls != "unclassified":
                vendors[address] = cls
    db = Database(tmp_path / "indexer.db")
    db.set_authority_rules(AuthorityRules(addresses=vendors))
    embedder = make_mock_embedder([0.0] * EMBEDDING_DIM)
    monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
    main.initial_index(db, embedder, Threader(db), IndexingQueue(db, max_attempts=1))
    db.close()
    return maildir


def _rows(conn, table: str, order: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    cols = [r[1] for r in conn.execute(f"PRAGMA table_xinfo({table})") if r[6] == 0]
    return [
        dict(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order}")
    ]


def _without(rows: list[dict], drop: set[str]) -> list[dict]:
    return [{k: v for k, v in r.items() if k not in drop} for r in rows]


def _differences(table: str, ours: list[dict], theirs: list[dict]) -> list[str]:
    """The first few differing (row, column) pairs, values cut short."""
    if len(ours) != len(theirs):
        return [f"{table}: {len(ours)} rows, the indexer wrote {len(theirs)}"]
    out = []
    for n, (a, b) in enumerate(zip(ours, theirs, strict=True)):
        out += [
            f"{table}[{n}].{k}: {str(a.get(k))[:80]!r} != {str(b.get(k))[:80]!r}"
            for k in sorted(set(a) | set(b))
            if a.get(k) != b.get(k)
        ]
    return out[:12]


CASES = {
    # identity, records, per_message, chunks, words, references, messages
    "typical": ("typical", "typical", 2, 1, 40, 0, 24),
    "ascii998_worst": ("ascii998", "worst", 1, 1, 40, 0, 8),
    "ascii998_worst_shallow": ("ascii998", "worst", 1, 1, 40, 0, 8),
    "common_mixed": ("ascii998common", "mixed", 2, 1, 40, 0, 8),
    "multi_chunk": ("typical", "typical", 1, 3, 250, 0, 8),
    "cardinality": ("typical", "cardinality", 1, 1, 40, 5, 3),
    "cardinality_names": ("typical", "cardinality_names", 0, 1, 40, 5, 3),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_benchmark_rows_are_the_rows_the_indexer_writes(bench, tmp_path, monkeypatch, case):
    identity, records, per_message, chunks, words, references, n = CASES[case]
    if case.endswith("_shallow"):
        # The worst records' nested Maildir path (about 3.6 KB) fits
        # Linux's 4,096-byte PATH_MAX, where the indexer runs, but not
        # macOS's 1,024: this case keeps two of its folder levels so
        # every other worst-case field is checked everywhere.
        real = bench._shape

        def shallow(*args):
            shape = real(*args)
            if "folder" in shape:
                shape["folder"] = "/".join(shape["folder"].split("/")[:2])
            return shape

        monkeypatch.setattr(bench, "_shape", shallow)
    else:
        longest = max(
            len(os.fsencode(bench.maildir_path(i, identity, records, str(tmp_path / "maildir"))))
            for i in range(n)
        )
        if longest >= os.pathconf(tmp_path, "PC_PATH_MAX"):
            pytest.skip("the nested Maildir path exceeds this platform's PATH_MAX (Linux only)")
    shape = {
        "identity": identity,
        "records": records,
        "references": references,
        "chunks": chunks,
        "words": words,
        "per_message": per_message,
    }
    maildir = _index_with_the_indexer(bench, tmp_path, monkeypatch, n, **shape)
    built = tmp_path / "bench.db"
    bench.build(built, n, per_message, identity, records, references, 0, chunks, words)

    with (
        closing(sqlite3.connect(tmp_path / "indexer.db")) as prod,
        closing(sqlite3.connect(built)) as ours,
    ):
        # Messages in the order the indexer inserted them (oldest first),
        # every column but the index-time clock; paths relative to the root.
        expected = _without(_rows(prod, "messages", "rowid"), _CLOCK)
        for row in expected:
            row["filepath"] = row["filepath"].replace(str(maildir), "/maildir", 1)
        problems = _differences(
            "messages", _without(_rows(ours, "messages", "rowid"), _CLOCK), expected
        )
        for table, order in (
            ("message_participants", "claimant_id, role, address"),
            ("message_participant_names", "claimant_id, role, address, name"),
            ("entities", "entity_id"),
            ("attachment_extractions", "attachment_id, extractor_module"),
            ("pending_deletions", "filepath"),
            # Chunks and occurrences in insertion order: their FTS rowids
            # follow it.
            ("message_chunks", "fts_rowid"),
            ("attachments", "fts_rowid"),
        ):
            problems += _differences(
                table,
                _without(_rows(ours, table, order), _CLOCK),
                _without(_rows(prod, table, order), _CLOCK),
            )
    assert problems == []


def test_benchmark_token_estimate_is_the_indexers(bench):
    from src.chunker import estimate_tokens

    texts = [bench._paragraph(i, c, w) for i in (0, 7, 49, 12345) for c in (0, 2) for w in (2, 40)]
    texts += [bench.attachment_text(i, k) for i in (0, 999) for k in (0, 4)]
    assert [bench.token_estimate(t) for t in texts] == [estimate_tokens(t) for t in texts]


def _schema(conn, table: str) -> dict:
    columns = list(conn.execute(f"PRAGMA table_xinfo({table})"))
    indexes = {
        name: (
            unique,
            [r[2] for r in conn.execute(f"PRAGMA index_xinfo({name})") if r[5]],
            re.sub(r"\s+", " ", sql or "").split(" WHERE ", 1)[1:],
        )
        for _, name, unique, _origin, _partial in conn.execute(f"PRAGMA index_list({table})")
        for (sql,) in conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,))
    }
    virtual = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = ? AND sql LIKE 'CREATE VIRTUAL%'", (table,)
    ).fetchone()
    return {
        "columns": columns,
        "indexes": indexes,
        "virtual": re.sub(r"\s+", " ", virtual[0]) if virtual else None,
    }


@pytest.mark.parametrize("table", _TABLES)
def test_benchmark_schema_is_the_indexers(bench, tmp_path, table):
    # Columns (type, NOT NULL, default, key) and indexes (columns,
    # uniqueness, partial condition) as the indexer creates them, so the
    # planner has the same choices. Foreign keys and CHECK constraints
    # are left out: they act on writes, and the measured statements read.
    Database(tmp_path / "indexer.db").close()
    with (
        closing(sqlite3.connect(tmp_path / "indexer.db")) as prod,
        closing(sqlite3.connect(":memory:")) as ours,
    ):
        ours.executescript(bench._SCHEMA)
        assert _schema(ours, table) == _schema(prod, table)
