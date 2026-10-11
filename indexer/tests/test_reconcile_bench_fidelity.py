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
import sqlite_vec
from src import main
from src.database import EMBEDDING_DIM, Database
from src.entities import AuthorityRules
from src.queue import IndexingQueue
from src.threader import Threader

from tests.conftest import make_mock_embedder

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
    "threads",
    "threads_fts",
    "threads_vec",
    "thread_vector_sums",
    "message_thread_map",
    "indexed_files",
    "entity_aliases",
    "message_chunks_vec",
    "indexing_jobs",
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
        "indexes": indexes,
        "virtual": re.sub(r"\s+", " ", virtual[0]) if virtual else None,
    }


def _with_vec(conn):
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


@pytest.mark.parametrize("table", [*_TABLES, *_WRITER_TABLES])
def test_benchmark_schema_is_the_indexers(bench, tmp_path, table):
    # Columns (type, NOT NULL, default, key) and indexes (columns,
    # uniqueness, partial condition) as the indexer creates them, so the
    # planner has the same choices. Foreign keys and CHECK constraints
    # are left out: they act on writes, and the measured statements read.
    Database(tmp_path / "indexer.db").close()
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
    for i in range(n):
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
    db = Database(tmp_path / "indexer.db")
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


# Values the writer does not reproduce on purpose: the wall clock, the
# folder (its messages are stored in Trash), the file's mtime, and the
# vector sums (its vectors are random, the mock embedder's are zero).
_WRITER_UNCOMPARED = _CLOCK | {"folder", "mtime_ns", "sum"}
_WRITER_ROWS = {
    "messages": "claimant_id",
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
}
_FTS_WRITES = (
    "INSERT INTO threads_fts",
    "DELETE FROM threads_fts",
    "INSERT INTO message_chunks_fts",
    "INSERT INTO attachments_fts",
)


def _traced_rows(path: Path, root: str | None) -> dict:
    with closing(sqlite3.connect(path)) as conn:
        out = {}
        for table, order in _WRITER_ROWS.items():
            rows = _without(_rows(conn, table, order), _WRITER_UNCOMPARED)
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
    db = Database(tmp_path / "indexer.db")
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
