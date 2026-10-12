"""The #1218 sizing benchmark's synthetic index against the indexer.

``mcp-server/scripts/reconcile_bench.py`` writes its index row by row,
so it can build a large corpus (50,000 messages by default) in the
mcp-server image. Every value it
writes must be one the indexer writes for the same mail. Here the
benchmark renders each synthetic message as a Maildir file
(``render_eml``), the indexer's own first index (parser, threader,
chunker, extractors, writer) indexes those files, and every row and
schema object the benchmark builds is compared with what the indexer
stored (#1376: the corpus had drifted from production
one dimension at a time).
"""

import email
import importlib.util
import os
import re
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
import sqlite_vec
from src import main
from src.database import EMBEDDING_DIM, Database
from src.entities import AuthorityRules
from src.queue import IndexingQueue
from src.threader import Threader

from tests.conftest import TEST_ACCOUNT, make_mock_embedder

_SCRIPT = Path(__file__).resolve().parents[2] / "mcp-server" / "scripts" / "reconcile_bench.py"

# Tables the benchmark builds. The indexer writes others (threads,
# vectors, the thread map, the queue); no measured statement reads them,
# and a statement that did would fail on the benchmark's index. Only the
# WAL writer creates them (``_WRITER_TABLES``), after every other step.
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

# Tables the indexer writes for every message that no measured statement
# reads: only the WAL writer creates them (``_WRITER_SCHEMA``), so its
# commits touch what the indexer's do. The cadence test checks this list
# against the tables the indexer's own commits write.
_WRITER_TABLES = (
    "message_headers",
    "threads",
    "threads_fts",
    "threads_vec",
    "thread_vector_sums",
    "message_thread_map",
    "indexed_files",
    "entity_aliases",
    "message_chunks_vec",
    "indexing_jobs",
    "message_occurrences",
)

# Values only the wall clock decides: the time the indexer wrote the row.
_CLOCK = {
    "indexed_at",
    "first_indexed_at",
    "chunked_at",
    "seen_at",
    "extracted_at",
    "first_observed_at",
    "last_observed_at",
    "retired_at",
}


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
    db = Database(tmp_path / "indexer.db", account=replace(TEST_ACCOUNT, maildir_root=maildir))
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


def test_benchmark_extraction_stamps_are_the_indexers(bench):
    # The script runs in the mcp-server image, which has no indexer
    # package, so it keeps its own copies of the values the indexer
    # stamps on a PDF's cached extraction. Checked against their source
    # here, so a version bump names the copy to move (#1444 bumped the
    # schema under #1376's benchmark).
    from src.extractors import NOT_CONTAINER_IDENTIFIER, _stamp_extractor
    from src.extractors.pdf import EXTRACTOR_DIGITAL

    assert bench.PDF_EXTRACTOR == _stamp_extractor(bench.PDF_MODULE, EXTRACTOR_DIGITAL)
    assert bench.PDF_IDENTIFIER == NOT_CONTAINER_IDENTIFIER


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
        # Foreign keys too: the WAL writer writes, and a cascade is a write.
        "foreign_keys": sorted(conn.execute(f"PRAGMA foreign_key_list({table})")),
        "indexes": indexes,
        "virtual": re.sub(r"\s+", " ", virtual[0]) if virtual else None,
    }


def _with_vec(conn):
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


@pytest.mark.parametrize("table", [*_TABLES, *_WRITER_TABLES, "accounts"])
def test_benchmark_schema_is_the_indexers(bench, tmp_path, table):
    # Columns (type, NOT NULL, default, key) and indexes (columns,
    # uniqueness, partial condition) as the indexer creates them, so the
    # planner has the same choices, and foreign keys (their cascades are
    # writes the WAL writer makes as the indexer does). CHECK
    # constraints are left out: they write nothing.
    Database(tmp_path / "indexer.db", account=TEST_ACCOUNT).close()
    with (
        closing(_with_vec(sqlite3.connect(tmp_path / "indexer.db"))) as prod,
        closing(_with_vec(sqlite3.connect(":memory:"))) as ours,
    ):
        # The WAL writer's tables (``_WRITER_SCHEMA``) as well: its
        # commits write them as the indexer does.
        ours.executescript(bench._SCHEMA + bench._WRITER_SCHEMA)
        assert _schema(ours, table) == _schema(prod, table)


# --- the WAL writer's commits (#1476) ----------------------------------

# Every table the indexer writes for a message: the benchmark's, and the
# ones only the WAL writer creates (``_WRITER_SCHEMA``).
_WRITTEN = re.compile(
    r"^\s*(INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)"
    r"\s+\"?(\w+)",
    re.IGNORECASE,
)


def _writes(statements: list[str], tables: set[str]) -> list[list[str]]:
    """Each committed transaction's write statements on ``tables``, in
    order, as ``"<VERB> <table>"`` (INSERT, UPDATE or DELETE). A
    transaction that wrote none (a read the indexer commits, such as
    ``thread_chunk_mean``'s) adds no WAL frame and is left out."""
    out: list[list[str]] = []
    written: list[str] = []
    for statement in statements:
        match = _WRITTEN.match(statement)
        if match and match.group(2) in tables:
            verb = match.group(1).split()[0].upper()
            written.append(f"{'INSERT' if verb == 'REPLACE' else verb} {match.group(2)}")
        head = statement.strip().upper()
        if head.startswith(("COMMIT", "END")):
            if written:
                out.append(written)
            written = []
        elif head.startswith("ROLLBACK"):
            written = []
    return out


def _commits(statements: list[str], tables: set[str]) -> list[tuple[str, ...]]:
    """The tables each committed transaction wrote to (``_writes``)."""
    return [tuple(sorted({w.split()[1] for w in writes})) for writes in _writes(statements, tables)]


_SHAPE = {
    "identity": "typical",
    "records": "typical",
    "references": 0,
    "chunks": 1,
    "chunk_tokens": 40,
    "per_message": 1,
    "extracted_chars": 0,
}


def _plain_tables(conn) -> set[str]:
    """Every table of the indexer's schema but the shadow tables its
    FTS5 and vec0 tables keep (named after them)."""
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
    virtual = [name for name, sql in rows if sql.startswith("CREATE VIRTUAL")]
    return {name for name, _ in rows if not any(name.startswith(v + "_") for v in virtual)}


def _indexer_commits(bench, tmp_path, monkeypatch, n: int, batch: int):
    """The indexer's own commits for a first index of ``n`` benchmark
    messages at batch size ``batch``: its walk enqueues each, then its
    drain indexes them a batch at a time."""
    maildir = tmp_path / "maildir"
    # The writer starts after its eight-message seed. Compare those exact
    # files: header counts vary with the folder and reply position.
    for i in range(8, 8 + n):
        path = Path(bench.maildir_path(i, "typical", "typical", str(maildir)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            bench.render_eml(i, "typical", "typical", chunks=1, words=40, per_message=1)
        )
    statements: list[str] = []
    real = sqlite3.connect

    def traced(*args, **kwargs):
        # Traced from the start, so the connection's own settings are seen.
        conn = real(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    db = Database(tmp_path / "indexer.db", account=replace(TEST_ACCOUNT, maildir_root=maildir))
    # The settings the indexer's connection applies (secure_delete, the
    # page cache, ...): each changes which pages a commit writes, so the
    # writer applies every one of them (``_writer_commits``).
    statements.clear()
    db._connect().close()
    pragmas = [s for s in statements if s.upper().startswith("PRAGMA")]
    assert any("secure_delete" in p for p in pragmas)
    monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
    monkeypatch.setattr(main, "INITIAL_INDEX_BATCH_SIZE", batch)
    statements.clear()
    main.initial_index(
        db,
        make_mock_embedder([0.0] * EMBEDDING_DIM),
        Threader(db),
        IndexingQueue(db, max_attempts=3),
    )
    tables = _plain_tables(db._conn)
    db.close()
    return _writes(statements, tables), tables, pragmas


def _stop_after_batch(db: Path, stop: Path, messages: int):
    """A started thread that creates ``stop`` once the writer's first
    batch is complete (``messages`` rows in all, no job left). The
    writer then waits its hour-long interval, and stops before any
    commit once ``stop`` exists, so it writes exactly one batch."""
    import threading
    import time

    def run() -> None:
        ready = Path(str(stop) + ".ready")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if ready.exists():
                with closing(sqlite3.connect(db, timeout=30)) as conn:
                    rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                    jobs = conn.execute("SELECT COUNT(*) FROM indexing_jobs").fetchone()[0]
                if rows == messages and jobs == 0:
                    break
            time.sleep(0.01)
        stop.touch()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _setting(pragma: str) -> str:
    return re.sub(r"\s+", "", pragma).lower()


def _writer_commits(bench, tmp_path, monkeypatch, batch: int, tables: set[str], pragmas):
    """The WAL writer's commits for its first batch of ``batch``
    messages."""

    tmp_path.mkdir()
    db = tmp_path / "bench.db"
    bench.build(db, 8, 1, "typical", "typical")
    statements: list[str] = []
    real = bench.sqlite3.connect

    def traced(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(bench.sqlite3, "connect", traced)
    stop = tmp_path / "stop"

    timer = _stop_after_batch(db, stop, 8 + batch)
    bench.phase_writer(str(db), str(stop), batch, 3600.0, shape=_SHAPE)
    timer.join()
    # Every connection setting the indexer applies, the writer applies.
    ours = {_setting(s) for s in statements if s.upper().startswith("PRAGMA")}
    assert {_setting(p) for p in pragmas} <= ours
    # The setup only creates tables: every commit is the batch's.
    return _writes(statements, tables)


@pytest.mark.parametrize("batch", [1, 4])
def test_wal_writer_commits_what_the_indexer_commits_per_batch(bench, tmp_path, monkeypatch, batch):
    # One batch of a first index: a lone message (a thread root) at
    # batch size 1, and a thread root with its three replies at 4, so
    # both the lone survivor's charge and the several survivors' mark,
    # and both a new thread and a reply, are compared. The expected
    # sequence is the indexer's own, traced, never the writer's.
    expected, tables, pragmas = _indexer_commits(
        bench, tmp_path / "indexer", monkeypatch, batch, batch
    )
    ours = _writer_commits(bench, tmp_path / "writer", monkeypatch, batch, tables, pragmas)
    # Commit by commit, the same write statements in the same order (a
    # statement's place decides when its pages are dirtied and spill).
    assert len(ours) == len(expected)
    for n, (a, b) in enumerate(zip(ours, expected, strict=True)):
        assert a == b, f"commit {n}"
    # Every table the indexer wrote is one the benchmark builds or the
    # writer creates, and the writer creates exactly the rest.
    written = {w.split()[1] for writes in expected for w in writes}
    assert written - set(_TABLES) == set(_WRITER_TABLES) == set(bench.WRITER_TABLES)
    # The cadence the docs state: per message one enqueue, then charge,
    # phase 1 and refund, a charge and refund around chunking, and the
    # vector commit and the job's delete; per batch a lone survivor's
    # charge (and its refund) or the survivors' interrupted mark.
    assert len(expected) == (10 if batch == 1 else 8 * batch + 1)


def test_wal_writer_truncates_thread_bodies_as_the_indexer_does(bench):
    # The thread body cap counts real BPE tokens, with the indexer's
    # tokenizer asset, so a cut body is the indexer's byte for byte.
    from src.chunker import truncate_to_tokens

    texts = [
        "",
        "plain words " * 3000,
        bench._WIDE * 9000,
        "Participants: " + ", ".join(f"Person {n} <p{n}@x.example>" for n in range(2000)),
    ]
    for text in texts:
        for cap in (0, 1, 7, bench.THREAD_BODY_TEXT_MAX_TOKENS):
            assert bench._truncate_to_tokens(text, cap) == truncate_to_tokens(text, cap)


def test_wal_writer_constants_are_the_indexers(bench):
    import random
    import struct

    from src import threader, vector_sums

    assert bench.EMBEDDING_DIM == EMBEDDING_DIM
    assert bench.SUM_ENCODING_VERSION == vector_sums.ENCODING_VERSION
    assert bench.PER_MESSAGE_BODY_CAP_CHARS == threader.PER_MESSAGE_BODY_CAP_CHARS
    assert bench.THREAD_BODY_TEXT_MAX_TOKENS == threader.THREAD_BODY_TEXT_MAX_TOKENS
    from src import attachment_indexing, queue
    from src.extractors import CONTAINER_SIGNATURE_VERSION

    assert bench.EXTRACTION_TURN_LAUNCHES == attachment_indexing.EXTRACTION_TURN_LAUNCHES
    assert bench.EXTRACTION_TURN_SECONDS == attachment_indexing.EXTRACTION_TURN_SECONDS
    assert bench.STAGE_EXTRACT == queue.STAGE_EXTRACT
    assert bench.EXTRACTION_DEFERRED_ERROR == queue.EXTRACTION_DEFERRED_ERROR
    assert bench._INTERRUPTED == (queue.INTERRUPTED_STAGE, queue._INTERRUPTED_ERROR)
    assert bench.CONTAINER_SIGNATURE_VERSION == CONTAINER_SIGNATURE_VERSION
    assert bench._SUBJECT_PREFIX_RE.pattern == threader._SUBJECT_PREFIX_RE.pattern
    assert bench._SUBJECT_PREFIX_RE.flags == threader._SUBJECT_PREFIX_RE.flags
    assert (
        bench.FTS_REPLY_SUBJECTS_MAX,
        bench.FTS_REPLY_SUBJECTS_MAX_CHARS,
        bench.FTS_SUBJECT_SCAN_ROWS,
        bench.FTS_SUBJECT_SCAN_CHARS,
    ) == (
        threader.FTS_REPLY_SUBJECTS_MAX,
        threader.FTS_REPLY_SUBJECTS_MAX_CHARS,
        threader.FTS_SUBJECT_SCAN_ROWS,
        threader.FTS_SUBJECT_SCAN_CHARS,
    )
    subjects = ["Re: Fwd:  Hello   World", "AW: hello world", "SV:Other", "", "x" * 600]
    for subject in subjects:
        assert bench._normalize_subject(subject) == threader._normalize_subject(subject)
    assert bench._fts_subject_text("hello world", subjects * 30) == threader.fts_subject_text(
        "hello world", subjects * 30
    )
    assert bench.STEADY_STATE_BATCH_SIZE == main.STEADY_STATE_BATCH_SIZE
    assert bench.INITIAL_INDEX_BATCH_SIZE == main.INITIAL_INDEX_BATCH_SIZE
    rng = random.Random(3)
    blobs = [bench._unit_vector(rng) for _ in range(3)]
    total = [0] * EMBEDDING_DIM
    for blob in blobs:
        assert bench._vector_units(blob) == vector_sums.vector_units(blob)
        total = vector_sums.add_units(total, vector_sums.vector_units(blob))
        assert len(struct.unpack(f"{EMBEDDING_DIM}f", blob)) == EMBEDDING_DIM
    assert bench._encode_sum(total) == vector_sums.encode(total)
    assert bench._encode_sum([0] * EMBEDDING_DIM) == vector_sums.encode([0] * EMBEDDING_DIM)


# Values the writer does not reproduce on purpose: the wall clock and
# the file's mtime, and, where the indexer's embedder is the mock's
# zeros (the writer's vectors are random), the vector sums. With
# identical synthetic embeddings (``_identical_embeddings``) the sums
# are compared too.
_WRITER_UNCOMPARED = _CLOCK | {"mtime_ns", "sum"}
_WRITER_ROWS = {
    "messages": "claimant_id",
    "message_headers": "claimant_id, ordinal",
    "message_participants": "claimant_id, role, address",
    "message_participant_names": "claimant_id, role, address, name",
    "entities": "entity_id",
    "entity_aliases": "entity_id, alias",
    "message_chunks": "chunk_id",
    "attachments": "attachment_occurrence_id",
    "attachment_extractions": "attachment_id, extractor_module",
    "threads": "thread_id",
    "message_thread_map": "claimant_id",
    "indexed_files": "filepath",
    "thread_vector_sums": "thread_id",
    "indexing_jobs": "filepath",
    "message_occurrences": "id",
}
_FTS_WRITES = (
    "INSERT INTO threads_fts",
    "DELETE FROM threads_fts",
    "INSERT INTO message_chunks_fts",
    "INSERT INTO attachments_fts",
)


def _traced_rows(
    path: Path, root: str | None, drop: set[str] = _WRITER_UNCOMPARED, tables: dict = _WRITER_ROWS
) -> dict:
    with closing(sqlite3.connect(path)) as conn:
        out = {}
        for table, order in tables.items():
            rows = _without(_rows(conn, table, order), drop)
            for row in rows:
                if root and isinstance(row.get("filepath"), str):
                    row["filepath"] = row["filepath"].replace(root, "/maildir", 1)
            out[table] = rows
        return out


@pytest.mark.parametrize(
    "case", ["typical", "ascii998_worst_shallow", "cardinality", "cardinality_names"]
)
def test_wal_writer_writes_the_rows_the_indexer_writes(bench, tmp_path, monkeypatch, case):
    identity, records, _pm, _c, _w, references, _n = CASES[case]
    if case.endswith("_shallow"):
        real_shape = bench._shape

        def shallow(*args):
            shape = real_shape(*args)
            if "folder" in shape:
                shape["folder"] = "/".join(shape["folder"].split("/")[:2])
            return shape

        monkeypatch.setattr(bench, "_shape", shallow)
    # The writer's first batch on an empty corpus is numbered from 0, so
    # it writes the four messages (a thread root and three replies, an
    # attachment each) the indexer's first index writes here: every row
    # of every table and every FTS statement (values included; the FTS
    # tables keep no content to compare) must be the indexer's, so each
    # commit writes what the indexer's does, not only to the same
    # tables.
    maildir = tmp_path / "maildir"
    vendors: dict[str, str] = {}
    for i in range(4):
        path = Path(bench.maildir_path(i, identity, records, str(maildir)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            bench.render_eml(i, identity, records, references, chunks=1, words=40, per_message=1)
        )
        for _, address, _ in bench._shape(i, identity, records, references)["people"]:
            if bench.authority(address)[0] != "unclassified":
                vendors[address] = bench.authority(address)[0]
    statements: list[str] = []
    real = Database._connect

    def traced(self):
        conn = real(self)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(Database, "_connect", traced)
    db = Database(tmp_path / "indexer.db", account=replace(TEST_ACCOUNT, maildir_root=maildir))
    db.set_authority_rules(AuthorityRules(addresses=vendors))
    monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
    monkeypatch.setattr(main, "INITIAL_INDEX_BATCH_SIZE", 4)
    statements.clear()
    main.initial_index(
        db,
        make_mock_embedder([0.0] * EMBEDDING_DIM),
        Threader(db),
        IndexingQueue(db, max_attempts=3),
    )
    db.close()
    expected_fts = [s for s in statements if s.startswith(_FTS_WRITES)]
    tables = set(_TABLES) | set(_WRITER_TABLES)
    expected_writes = _writes(statements, tables)
    expected = _traced_rows(tmp_path / "indexer.db", str(maildir))

    ours_db = tmp_path / "writer.db"
    bench.build(ours_db, 0, 1, identity, records)
    ours_statements: list[str] = []
    real_connect = bench.sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(ours_statements.append)
        return conn

    monkeypatch.setattr(bench.sqlite3, "connect", traced_connect)
    stop = tmp_path / "stop"
    timer = _stop_after_batch(ours_db, stop, 4)
    shape = {**_SHAPE, "identity": identity, "records": records, "references": references}
    bench.phase_writer(str(ours_db), str(stop), 4, 3600.0, shape=shape)
    timer.join()
    ours = _traced_rows(ours_db, None)
    ours_fts = [s for s in ours_statements if s.startswith(_FTS_WRITES)]
    # Commit by commit, the same write statements in the same order.
    assert _writes(ours_statements, tables) == expected_writes
    problems = []
    for table in _WRITER_ROWS:
        problems += _differences(table, ours[table], expected[table])
    assert problems == []
    assert ours_fts == expected_fts


# --- attachment continuation passes (owner decision 2, 2026-10-11) ------


def _occurrence_states(conn, claimant_id: str) -> list[tuple]:
    """A message's occurrences after one pass: whether each is complete
    or deferred, and how many chunks and cached results it has."""
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT a.attachment_occurrence_id, a.text_complete, "
            "a.extraction_deferred_at IS NOT NULL, "
            "(SELECT COUNT(*) FROM message_chunks c WHERE c.claimant_id = a.claimant_id "
            " AND c.attachment_id = a.attachment_id), "
            "(SELECT COUNT(*) FROM attachment_extractions e "
            " WHERE e.attachment_id = a.attachment_id) "
            "FROM attachments a WHERE a.claimant_id = ? ORDER BY a.attachment_occurrence_id",
            (claimant_id,),
        )
    ]


def _seed_cache(bench, conn, per_message: int) -> None:
    # Message 0's first payload already extracted (by another message,
    # say): its occurrence is a cache hit, which costs no budget.
    _occurrence, attachment_id, _size, text = bench._record(
        0, "typical", "typical", 0, 1, 40, per_message, None
    ).attachments[0]
    conn.execute(bench._INSERT_EXTRACTION, (attachment_id, text, "2026-01-01T00:00:00+00:00"))
    conn.commit()


def _indexer_passes(bench, tmp_path, monkeypatch, n, batch, per_message, seconds, seed):
    """The indexer's first index of ``n`` messages of ``per_message``
    PDFs at batch size ``batch``, each extraction taking ``seconds`` on
    the budget's clock: its statements, its index and each pass's
    occurrence states."""
    import functools

    from src import attachment_indexing

    maildir = tmp_path / "maildir"
    for i in range(n):
        path = Path(bench.maildir_path(i, "typical", "typical", str(maildir)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            bench.render_eml(i, "typical", "typical", chunks=1, words=40, per_message=per_message)
        )
    # The benchmark's authority rules, as the writer applies them.
    vendors = {
        address: bench.authority(address)[0]
        for i in range(n)
        for _, address, _ in bench._shape(i, "typical", "typical", 0)["people"]
        if bench.authority(address)[0] != "unclassified"
    }
    now = [0.0]

    def clock() -> float:
        # Read once before and once after each extraction
        # (``_resolve_extracted_text``): every reading advances by
        # ``seconds``, so each extraction's difference is ``seconds``.
        now[0] += seconds
        return now[0]

    monkeypatch.setattr(
        main,
        "ExtractionBudget",
        functools.partial(attachment_indexing.ExtractionBudget, clock=clock),
    )
    statements: list[str] = []
    real = Database._connect

    def traced(self):
        conn = real(self)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(Database, "_connect", traced)
    db = Database(tmp_path / "indexer.db", account=replace(TEST_ACCOUNT, maildir_root=maildir))
    db.set_authority_rules(AuthorityRules(addresses=vendors))
    if seed:
        _seed_cache(bench, db._conn, per_message)
    passes: list[list[tuple]] = []
    real_2c = main._phase2c_commit_vectors

    def phase2c(entry, db_, *args, **kwargs):
        out = real_2c(entry, db_, *args, **kwargs)
        passes.append(_occurrence_states(db_._conn, entry.msg.claimant_id))
        return out

    monkeypatch.setattr(main, "_phase2c_commit_vectors", phase2c)
    monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
    monkeypatch.setattr(main, "INITIAL_INDEX_BATCH_SIZE", batch)
    statements.clear()
    main.initial_index(
        db,
        make_mock_embedder([0.0] * EMBEDDING_DIM),
        Threader(db),
        IndexingQueue(db, max_attempts=3),
    )
    db.close()
    monkeypatch.undo()
    return statements, tmp_path / "indexer.db", str(maildir), passes


def _writer_passes(bench, tmp_path, monkeypatch, n, batch, per_message, seconds, seed):
    """The writer's batches for the same ``n`` messages (numbered from 0
    on an empty corpus) and the same per-extraction ``seconds``, until
    every job is done."""
    tmp_path.mkdir()
    db = tmp_path / "writer.db"
    bench.build(db, 0, per_message, "typical", "typical")
    if seed:
        with closing(sqlite3.connect(db)) as conn:
            _seed_cache(bench, conn, per_message)
    statements: list[str] = []
    real = bench.sqlite3.connect

    def traced(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(bench.sqlite3, "connect", traced)
    passes: list[list[tuple]] = []
    real_2c = bench._writer_phase2c

    def phase2c(conn, rec, *args):
        out = real_2c(conn, rec, *args)
        passes.append(_occurrence_states(conn, rec.cid))
        return out

    monkeypatch.setattr(bench, "_writer_phase2c", phase2c)
    stop = tmp_path / "stop"
    timer = _stop_after_batch(db, stop, n)
    shape = {**_SHAPE, "per_message": per_message}
    # A first index, as the indexer's: the backlog enqueued before the
    # writer is ready, drained with no pause, then idle.
    bench.phase_writer(
        str(db),
        str(stop),
        batch,
        0.0,
        shape=shape,
        extract_seconds=seconds,
        lifecycle="drain",
        backlog=n,
    )
    timer.join()
    monkeypatch.undo()
    return statements, db, passes


@pytest.mark.real_extractor_child
@pytest.mark.parametrize(
    ("n", "batch", "per_message", "seconds", "seed", "pass_count"),
    [
        # The launches bind (the clock stands still): 64 extracted, 2 left.
        pytest.param(1, 1, 66, 0.0, False, 2, id="launch_limited"),
        # The seconds bind: three 2 s extractions reach 5 s, one left.
        pytest.param(1, 1, 4, 2.0, False, 2, id="time_limited"),
        # A cached payload costs nothing: three extracted, one deferred.
        pytest.param(1, 1, 5, 2.0, True, 2, id="cache_hit"),
        # Two messages of seven, three passes each, the continuations
        # taking turns in batches of their own.
        pytest.param(2, 2, 7, 2.0, False, 6, id="multi_pass"),
        # A first index of several batches (owner decision 3's drain):
        # without continuations, then with them, where each continuation
        # (due when its pass ends) is claimed behind the backlog's jobs
        # already due (due at their message time).
        pytest.param(4, 2, 1, 0.0, False, 4, id="several_batches"),
        pytest.param(4, 2, 4, 2.0, False, 8, id="continuations_behind_due"),
    ],
)
def test_wal_writer_continues_messages_as_the_indexer_does(
    bench, tmp_path, monkeypatch, n, batch, per_message, seconds, seed, pass_count
):
    # The expected passes are the indexer's own, traced with its budget's
    # clock injected so each extraction takes ``seconds``: the deferral
    # in the phase 2c transaction, the queue and phase 1 writes of every
    # further pass (no body re-appended), resolved occurrences left as
    # they are, the job's delete on the last pass only, and the state
    # each pass leaves.
    expected, prod_db, root, prod_passes = _indexer_passes(
        bench, tmp_path / "indexer", monkeypatch, n, batch, per_message, seconds, seed
    )
    ours, ours_db, our_passes = _writer_passes(
        bench, tmp_path / "writer", monkeypatch, n, batch, per_message, seconds, seed
    )
    assert len(prod_passes) == pass_count
    assert our_passes == prod_passes
    tables = set(_TABLES) | set(_WRITER_TABLES)
    theirs_writes, ours_writes = _writes(expected, tables), _writes(ours, tables)
    # The walk: the indexer enqueues each message in a commit of its own,
    # the writer's backlog in one, both before any measured window.
    assert theirs_writes[:n] == [["INSERT indexing_jobs"]] * n
    assert ours_writes[:1] == [["INSERT indexing_jobs"]]
    theirs_writes, ours_writes = theirs_writes[n:], ours_writes[1:]
    assert len(ours_writes) == len(theirs_writes)
    for k, (a, b) in enumerate(zip(ours_writes, theirs_writes, strict=True)):
        assert a == b, f"commit {k}"
    assert [s for s in ours if s.startswith(_FTS_WRITES)] == [
        s for s in expected if s.startswith(_FTS_WRITES)
    ]
    theirs_rows, ours_rows = _traced_rows(prod_db, root), _traced_rows(ours_db, None)
    problems = []
    for table in _WRITER_ROWS:
        problems += _differences(table, ours_rows[table], theirs_rows[table])
    assert problems == []


@pytest.mark.real_extractor_child
def test_wal_writer_counts_the_launches_of_the_benchmark_pdf(bench):
    from src.extractors import extract
    from src.extractors._runner import process_launches

    for i in range(3):
        before = process_launches()
        result = extract(
            content_type="application/pdf",
            filename="file.pdf",
            payload=bench.pdf_payload(bench.attachment_text(i, 0)),
            ocr_enabled=False,
            max_bytes=50_000_000,
            max_ocr_pages=10,
        )
        assert result.status == "success"
        assert process_launches() - before == bench.PDF_LAUNCHES


@pytest.mark.serial
@pytest.mark.real_extractor_child
def test_wal_writer_extraction_seconds_are_a_plain_timing(bench):
    # ``PDF_EXTRACT_SECONDS`` is the writer's default cost of one
    # extraction against the per-pass budget: a plain timing of the
    # indexer's extractor on the benchmark's PDF, here within a factor of
    # three (the live indexer's figure is #1469).
    import statistics
    import time

    from src.extractors import extract

    timings = []
    for i in range(9):
        payload = bench.pdf_payload(bench.attachment_text(i, 0))
        started = time.perf_counter()
        extract(
            content_type="application/pdf",
            filename="file.pdf",
            payload=payload,
            ocr_enabled=False,
            max_bytes=50_000_000,
            max_ocr_pages=10,
        )
        timings.append(time.perf_counter() - started)
    median = statistics.median(timings)
    assert median / 3 <= bench.PDF_EXTRACT_SECONDS <= median * 3


# --- the WAL baseline and the common-ID thread (owner, 2026-10-11, round 16)


def _synthetic_embedding(text: str) -> list[float]:
    """One unit vector per text, float32-exact, so the indexer stores the
    bytes the writer stores for the same chunk."""
    import hashlib
    import math
    import random
    import struct

    rng = random.Random(hashlib.sha256(text.encode()).digest())
    values = [rng.gauss(0.0, 1.0) for _ in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(v * v for v in values))
    packed = struct.pack(f"{EMBEDDING_DIM}f", *(v / norm for v in values))
    return list(struct.unpack(f"{EMBEDDING_DIM}f", packed))


def _identical_embeddings(bench, monkeypatch, records):
    """An indexer embedder and the writer's (``bench._embedder``) giving
    every chunk text the same synthetic embedding. The indexer embeds a
    message's first body chunk behind its subject line, so an input is
    mapped back to the chunk text it ends with."""
    import struct
    from unittest.mock import MagicMock

    texts = sorted({c[4] for rec in records for c in rec.chunks}, key=len, reverse=True)
    known = set(texts)
    cache: dict[str, list[float]] = {}

    def vector(text: str) -> list[float]:
        if text not in cache:
            cache[text] = _synthetic_embedding(text)
        return cache[text]

    def chunk_text(given: str) -> str:
        return given if given in known else next(t for t in texts if given.endswith(t))

    embedder = MagicMock()
    embedder.embed_batch.side_effect = lambda inputs, **_kw: [vector(chunk_text(t)) for t in inputs]
    monkeypatch.setattr(
        bench,
        "_embedder",
        lambda _pool, _start=0: lambda text: struct.pack(f"{EMBEDDING_DIM}f", *vector(text)),
    )
    return embedder


# Every table the indexer fills for a message, compared row by row with
# identical embeddings: the writer's tables, the benchmark's, and the
# vectors (their bytes).
_ALL_ROWS = {
    **_WRITER_ROWS,
    "attachments": "attachment_occurrence_id",
    "pending_deletions": "filepath",
}
_VECTORS = {"threads_vec": "thread_id", "message_chunks_vec": "chunk_id"}


def _vectors(path: Path) -> dict:
    with closing(_with_vec(sqlite3.connect(path))) as conn:
        return {
            table: list(conn.execute(f"SELECT {key}, embedding FROM {table} ORDER BY {key}"))
            for table, key in _VECTORS.items()
        }


def _shape_of(identity: str) -> dict:
    return {**_SHAPE, "identity": identity, "per_message": 2}


def _write_files(bench, maildir: Path, numbers, shape: dict, vendors: dict) -> None:
    for i in numbers:
        path = Path(bench.maildir_path(i, shape["identity"], shape["records"], str(maildir)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            bench.render_eml(
                i,
                shape["identity"],
                shape["records"],
                shape["references"],
                chunks=shape["chunks"],
                words=shape["chunk_tokens"],
                per_message=shape["per_message"],
            )
        )
        for _, address, _ in bench._shape(i, shape["identity"], shape["records"], 0)["people"]:
            if bench.authority(address)[0] != "unclassified":
                vendors[address] = bench.authority(address)[0]


def _indexer_baseline_and_replies(bench, tmp_path, monkeypatch, shape, n, batch, embedder):
    """The indexer's first index of messages 0..n-1 at batch size
    ``batch``, then of ``batch`` more (``n``..), as a later walk and
    drain index new mail: the statements of each, and the index."""
    maildir = tmp_path / "maildir"
    vendors: dict[str, str] = {}
    _write_files(bench, maildir, range(n), shape, vendors)
    for i in range(n, n + batch):
        for _, address, _ in bench._shape(i, shape["identity"], shape["records"], 0)["people"]:
            if bench.authority(address)[0] != "unclassified":
                vendors[address] = bench.authority(address)[0]
    statements: list[str] = []
    real = Database._connect

    def traced(self):
        conn = real(self)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(Database, "_connect", traced)
    db = Database(tmp_path / "indexer.db", account=replace(TEST_ACCOUNT, maildir_root=maildir))
    db.set_authority_rules(AuthorityRules(addresses=vendors))
    monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
    monkeypatch.setattr(main, "INITIAL_INDEX_BATCH_SIZE", batch)
    statements.clear()
    main.initial_index(db, embedder, Threader(db), IndexingQueue(db, max_attempts=3))
    first = list(statements)
    snapshot = tmp_path / "indexer-baseline.db"
    with closing(sqlite3.connect(snapshot)) as copy:
        db._conn.backup(copy)
    _write_files(bench, maildir, range(n, n + batch), shape, {})
    statements.clear()
    main.initial_index(db, embedder, Threader(db), IndexingQueue(db, max_attempts=3))
    second = list(statements)
    db.close()
    return first, snapshot, second, tmp_path / "indexer.db", str(maildir)


def _writer_baseline_and_replies(bench, tmp_path, monkeypatch, shape, n, batch):
    tmp_path.mkdir()
    corpus = tmp_path / "corpus.db"
    bench.build(corpus, n, shape["per_message"], shape["identity"], shape["records"])
    statements: list[str] = []
    real = bench.sqlite3.connect

    def traced(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(bench.sqlite3, "connect", traced)
    monkeypatch.setattr(bench, "INITIAL_INDEX_BATCH_SIZE", batch)
    base = str(tmp_path / "base.db")
    built = bench.build_wal_baseline(str(corpus), base, n, shape)
    first = list(statements)
    snapshot = tmp_path / "writer-baseline.db"
    with closing(real(base)) as src, closing(real(snapshot)) as copy:
        src.backup(copy)
    statements.clear()
    stop = tmp_path / "stop"
    timer = _stop_after_batch(Path(base), stop, n + batch)
    bench.phase_writer(base, str(stop), batch, 3600.0, shape=shape)
    timer.join()
    second = list(statements)
    return first, snapshot, second, Path(base), built


def _compare_indexes(ours: Path, theirs: Path, root: str) -> list[str]:
    drop = _CLOCK | {"mtime_ns"}
    mine = _traced_rows(ours, None, drop, _ALL_ROWS)
    expected = _traced_rows(theirs, root, drop, _ALL_ROWS)
    problems = []
    for table in _ALL_ROWS:
        problems += _differences(table, mine[table], expected[table])
    if _vectors(ours) != _vectors(theirs):
        problems.append("vectors differ")
    return problems


@pytest.mark.real_extractor_child
@pytest.mark.parametrize(
    ("identity", "n"),
    [
        ("typical", 8),
        ("ascii998common", 8),
        # A corpus that ends mid-thread: the new mail goes on from the
        # next message, the first two of it replies in that thread.
        ("typical", 6),
    ],
)
def test_wal_baseline_and_replies_are_what_the_indexer_writes(
    bench, tmp_path, monkeypatch, identity, n
):
    # The WAL baseline is the corpus indexed by the writer's own commits
    # (``build_wal_baseline``), in batches, as a first index makes them;
    # then a batch of new mail is indexed on top. ``ascii998common``
    # puts every message, the new ones included, in one thread: the
    # replies update the large existing thread. Against the indexer's
    # own first index and a later drain of the same files, with
    # identical embeddings: every commit's statements in order, every
    # row (folders included), every FTS statement with its values, the
    # vector tables' bytes and the encoded sums.
    batch = 4
    shape = _shape_of(identity)
    records = [bench._record(i, identity, "typical", 0, 1, 40, 2, None) for i in range(n + batch)]
    embedder = _identical_embeddings(bench, monkeypatch, records)
    first, their_base, second, theirs, root = _indexer_baseline_and_replies(
        bench, tmp_path / "indexer", monkeypatch, shape, n, batch, embedder
    )
    embedder = _identical_embeddings(bench, monkeypatch, records)
    ours_first, our_base, ours_second, ours, built = _writer_baseline_and_replies(
        bench, tmp_path / "writer", monkeypatch, shape, n, batch
    )
    tables = set(_TABLES) | set(_WRITER_TABLES)
    # The walk: one commit per file for the indexer, the backlog in one
    # for the writer (before any window).
    theirs_writes, ours_writes = _writes(first, tables), _writes(ours_first, tables)
    assert theirs_writes[:n] == [["INSERT indexing_jobs"]] * n
    assert ours_writes[:1] == [["INSERT indexing_jobs"]]
    assert ours_writes[1:] == theirs_writes[n:]
    assert [s for s in ours_first if s.startswith(_FTS_WRITES)] == [
        s for s in first if s.startswith(_FTS_WRITES)
    ]
    assert _compare_indexes(our_base, their_base, root) == []
    assert built["jobs_left"] == 0
    # The new mail: the indexer's walk enqueues each file, the writer's
    # arrival each message, then one batch.
    assert _writes(ours_second, tables) == _writes(second, tables)
    assert [s for s in ours_second if s.startswith(_FTS_WRITES)] == [
        s for s in second if s.startswith(_FTS_WRITES)
    ]
    assert _compare_indexes(ours, theirs, root) == []
    # What was compared: every message, its chunks' vectors and the
    # threads' non-empty sums (one thread for the common ID).
    with closing(_with_vec(sqlite3.connect(ours))) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == n + batch
        assert conn.execute("SELECT COUNT(*) FROM message_chunks_vec").fetchone()[0] == 3 * (
            n + batch
        )
        sums = conn.execute("SELECT count, length(sum) FROM thread_vector_sums").fetchall()
        assert len(sums) == (1 if identity == "ascii998common" else -(-(n + batch) // 4))
        assert all(count and size for count, size in sums)


@pytest.mark.serial
@pytest.mark.parametrize(
    "argv",
    [
        ["--messages", "300", "--per-message", "3"],
        # Two continuation passes a message (50 extractions a pass).
        ["--messages", "30", "--per-message", "60"],
    ],
)
def test_wal_baseline_estimate_is_near_a_measured_build(bench, tmp_path, argv):
    # ``wal_baseline_estimate``'s coefficients are one laptop's plain
    # timings; another host's build differs, but by less than a factor
    # of four here (a CI runner included), or the budget bounds nothing.
    import time

    args = bench.parse_args(
        [*argv, "--workdir", str(tmp_path), "--wal", "--missing", "0", "--k", "1"]
    )
    corpus = tmp_path / "corpus.db"
    bench.build(corpus, args.messages, args.per_message, "typical", "typical")
    shape = {"identity": "typical", "records": "typical", "per_message": args.per_message}
    started = time.perf_counter()
    bench.build_wal_baseline(str(corpus), str(tmp_path / "base.db"), args.messages, shape)
    measured = time.perf_counter() - started
    estimate = bench.wal_baseline_estimate(args)
    assert measured / 4 <= estimate <= measured * 4, (estimate, measured)


def test_benchmark_header_budgets_match_the_parser(bench):
    from src import parser

    for name in (
        "MAX_STORED_HEADER_FIELDS",
        "MAX_STORED_HEADER_BYTES",
        "MAX_STORED_HEADER_VALUE_BYTES",
    ):
        assert getattr(bench, name) == getattr(parser, name)
    for name in ("_ENCODED_WORD_RE", "_ENCODED_WORD_RUN_RE"):
        assert getattr(bench, name).pattern == getattr(parser, name).pattern
        assert getattr(bench, name).flags == getattr(parser, name).flags


def _assert_header_capture_matches_parser(bench, raw):
    from src.parser import _read_top_headers

    caps = Counter()
    expected, complete = _read_top_headers(email.message_from_bytes(raw), caps)
    rows, captured_complete = bench.header_capture(raw)
    assert rows == [
        (h.ordinal, h.name, h.name_normalized, h.raw_value, h.decoded_value) for h in expected
    ]
    assert captured_complete is complete
    assert dict(caps) == ({} if complete else {"headers_budget": 1})


@pytest.mark.parametrize("identity", ["typical", "ascii998", "ascii998common"])
@pytest.mark.parametrize(
    "records", ["typical", "worst", "mixed", "cardinality", "cardinality_names"]
)
@pytest.mark.parametrize("i", [0, 1, 50])
@pytest.mark.parametrize("per_message", [0, 1])
def test_generated_header_capture_matches_all_parser_producible_shapes(
    bench, identity, records, i, per_message
):
    raw = bench.render_eml(i, identity, records, 5, 1, 40, per_message)
    _assert_header_capture_matches_parser(bench, raw)


def test_generated_header_catalogue_covers_cli_shapes(bench):
    choices = {arg.name: set(arg.choices) for arg in bench.ARGS if arg.choices}
    # utf8x4 cannot be produced by the parser and never sizes limits.
    assert choices["identity"] - {"utf8x4"} == {"typical", "ascii998", "ascii998common"}
    assert choices["records"] == {"typical", "worst", "mixed", "cardinality", "cardinality_names"}
    _assert_header_capture_matches_parser(
        bench, bench.render_eml(0, "typical", "cardinality", 100_000, 1, 40, 0)
    )


@pytest.mark.parametrize(
    "fields",
    [
        ["x"] * 256,
        ["x"] * 257,
        ["x" * (16 * 1024 - 6)],
        ["x" * (16 * 1024 - 5)],
        ["aa" + "😀" * 4094],
        ["aaa" + "😀" * 4094],
        ["x" * (8192 - 6)] * 128,
        ["x" * (8192 - 6)] * 128 + ["x"],
    ],
    ids=[
        "field-edge",
        "field-over",
        "value-edge",
        "value-over",
        "utf8-edge",
        "utf8-over",
        "total-edge",
        "total-over",
    ],
)
def test_generated_header_capture_matches_each_budget_boundary(bench, fields):
    raw = ("\n".join("X-Test: " + value for value in fields) + "\n\nbody\n").encode()
    _assert_header_capture_matches_parser(bench, raw)


def test_generated_header_capture_keeps_folds_between_encoded_names(bench):
    value = bench._address_header(
        [("from", "one@example.com", "雪" * 30), ("from", "two@example.com", "😀" * 30)],
        "from",
    )
    _assert_header_capture_matches_parser(bench, ("From: " + value + "\n\nbody\n").encode())


def test_generated_header_capture_stops_at_the_first_oversized_field(bench):
    raw = ("X-First: kept\nX-Big: " + "x" * (16 * 1024) + "\nX-Last: dropped\n\nbody\n").encode()
    _assert_header_capture_matches_parser(bench, raw)
    rows, complete = bench.header_capture(raw)
    assert not complete
    assert [row[1] for row in rows] == ["X-First"]


@pytest.mark.parametrize(
    "value",
    [
        "  plain\u00a0  ",
        "=?utf-8?B?6Zuq?=\r\n\t=?utf-8?B?6Zuq?=  ",
        "雪 =?utf-8?B?6Zuq?=",
    ],
)
def test_generated_header_capture_preserves_decoding_and_strip(bench, value):
    _assert_header_capture_matches_parser(bench, ("X-Test: " + value + "\n\nbody\n").encode())


def test_generated_header_capture_refuses_unsupported_encodings(bench):
    with pytest.raises(ValueError, match="^unsupported generated header encoding$"):
        bench.header_capture(b"X-Test: =?iso-8859-1?Q?caf=E9?=\n\nbody\n")
