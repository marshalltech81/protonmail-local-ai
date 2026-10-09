"""Synthetic sizing benchmark for paged-run certification (#1218, PR0).

Measures what the planned certificate and reconcile round (#1218,
Design A) would cost, on a synthetic index only, before any protocol
code exists:

- the identity scan, sort and hash of the matching set inside one read
  transaction, for messages (claimant IDs) and attachment occurrences;
- a reconcile round: the client's uploaded SHA-256 per identity, the
  server-side diff at one snapshot, and at most K missing records
  materialized in the same transaction;
- request and response bytes, peak RSS and WAL growth under a
  concurrent writer while the round holds its snapshot.

Every value is generated here; no mail is read. The mailbox connection
is the server's own (``Database._connect``: ``mode=ro`` and
``query_only``) and the predicate SQL is the server's
(``query_messages_leaves`` and ``compile_leaves`` with no filters, so
Trash is left out), so the scan is the one the server would run.
Timings are plain ``time.perf_counter`` differences, never a
profiler (AGENTS.md "Bound the work per input"). Each measured phase
runs in a fresh child process so its peak RSS (``ru_maxrss``) is its
own.

Run it inside the mcp-server image (docs/mcp-tools.md, "Certifying a
paged run: sizing"). The tables mirror the indexer's ``messages``,
``message_participants``, ``attachments``, ``attachment_extractions``
and ``pending_deletions`` with their indexes; other tables are left
out because no measured statement reads them.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import random
import resource
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

# ``src`` is the mcp-server package: one directory up from this file in
# the repository and in the image (mounted at /app/scripts).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Domain tags for the two hashes. Measurement only: PR1 fixes the
# canonical form; these only make the work the same shape.
CERT_DOMAIN = b"protonmail-local-ai/query-certificate/v0\x00"
IDENTITY_DOMAIN = b"protonmail-local-ai/identity-hash/v0\x00"

_SCHEMA = """
CREATE TABLE messages (
    claimant_id     TEXT PRIMARY KEY,
    message_id      TEXT NOT NULL,
    thread_id       TEXT NOT NULL,
    filepath        TEXT NOT NULL,
    folder          TEXT NOT NULL,
    subject         TEXT NOT NULL,
    sent_at         TEXT NOT NULL,
    occurred_at     TEXT,
    effective_at    TEXT GENERATED ALWAYS AS (COALESCE(occurred_at, sent_at)) VIRTUAL,
    in_reply_to     TEXT,
    references_json TEXT NOT NULL,
    has_attachments INTEGER NOT NULL,
    size_bytes      INTEGER,
    content_hash    TEXT,
    indexed_at      TEXT NOT NULL,
    seen            INTEGER NOT NULL DEFAULT 0,
    flagged         INTEGER NOT NULL DEFAULT 0,
    replied         INTEGER NOT NULL DEFAULT 0,
    sender_ambiguous INTEGER CHECK (sender_ambiguous IN (0, 1)),
    participant_names_complete INTEGER CHECK (participant_names_complete IN (0, 1)),
    subject_complete INTEGER CHECK (subject_complete IN (0, 1)),
    from_addresses_complete INTEGER CHECK (from_addresses_complete IN (0, 1)),
    to_addresses_complete INTEGER CHECK (to_addresses_complete IN (0, 1)),
    cc_addresses_complete INTEGER CHECK (cc_addresses_complete IN (0, 1)),
    attachments_manifest_complete INTEGER CHECK (attachments_manifest_complete IN (0, 1)),
    body_complete INTEGER CHECK (body_complete IN (0, 1)),
    caps_json TEXT
);
CREATE INDEX idx_messages_message ON messages(message_id, claimant_id);
CREATE INDEX idx_messages_message_effective ON messages(message_id, effective_at, claimant_id);
CREATE INDEX idx_messages_thread_effective ON messages(thread_id, effective_at);
CREATE INDEX idx_messages_folder_effective ON messages(folder, effective_at);
CREATE INDEX idx_messages_effective ON messages(effective_at);
CREATE INDEX idx_messages_filepath ON messages(filepath);
CREATE TABLE message_participants (
    claimant_id TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('from', 'to', 'cc')),
    address     TEXT NOT NULL,
    name        TEXT,
    PRIMARY KEY (claimant_id, role, address)
);
CREATE INDEX idx_message_participants_address ON message_participants(address, role);
CREATE TABLE attachments (
    attachment_occurrence_id TEXT PRIMARY KEY,
    claimant_id               TEXT NOT NULL,
    attachment_id             TEXT NOT NULL,
    thread_id                 TEXT NOT NULL,
    filename                  TEXT NOT NULL,
    content_type              TEXT NOT NULL,
    size_bytes                INTEGER NOT NULL,
    seen_at                   TEXT NOT NULL,
    fts_rowid                 INTEGER,
    extractor_module          TEXT NOT NULL DEFAULT '',
    text_complete             INTEGER CHECK (text_complete IN (0, 1)),
    text_extractor            TEXT
);
CREATE INDEX idx_attachments_attachment_id ON attachments(attachment_id);
CREATE INDEX idx_attachments_thread ON attachments(thread_id);
CREATE INDEX idx_attachments_claimant ON attachments(claimant_id);
CREATE INDEX idx_attachments_fts_rowid ON attachments(fts_rowid);
CREATE TABLE attachment_extractions (
    attachment_id      TEXT NOT NULL,
    extractor_module   TEXT NOT NULL,
    extraction_status  TEXT NOT NULL,
    extractor          TEXT,
    extracted_text     TEXT,
    extraction_error   TEXT,
    extracted_at       TEXT NOT NULL,
    ocr_pages_skipped  INTEGER CHECK (ocr_pages_skipped >= 0),
    text_complete      INTEGER CHECK (text_complete IN (0, 1)),
    PRIMARY KEY (attachment_id, extractor_module)
);
CREATE TABLE pending_deletions (
    filepath    TEXT PRIMARY KEY,
    claimant_id TEXT NOT NULL,
    thread_id   TEXT NOT NULL,
    marked_at   TEXT NOT NULL
);
-- Writer ballast for the WAL phase: stands for the chunk and vector
-- rows an indexer commit writes.
CREATE TABLE bench_ballast (id INTEGER PRIMARY KEY, payload BLOB NOT NULL);
"""

# Longest Message-ID the indexer accepts (indexer/src/parser.py
# ``MESSAGE_ID_MAX_CHARS``), and the claimant suffix ``#`` + 16 hex.
MESSAGE_ID_MAX_CHARS = 998
_DOMAIN = "@bench.example"


def message_id(i: int, identity: str) -> str:
    """Synthetic Message-ID ``i`` in one of three widths: ``typical``
    (about 50 ASCII characters), ``ascii998`` (998 ASCII characters) or
    ``utf8x4`` (998 four-byte characters, the UTF-8 upper bound)."""
    if identity == "typical":
        return f"{hashlib.sha256(str(i).encode()).hexdigest()[:32]}.{i}{_DOMAIN}"
    if identity == "ascii998":
        head = f"{i:010d}"
        return head + "x" * (MESSAGE_ID_MAX_CHARS - len(head) - len(_DOMAIN)) + _DOMAIN
    if identity == "utf8x4":
        # Three base-1024 digits as four-byte characters keep IDs unique.
        digits = [chr(0x10000 + (i >> s & 0x3FF)) for s in (20, 10, 0)]
        return "".join(digits) + "\U0001f600" * (MESSAGE_ID_MAX_CHARS - 3)
    raise ValueError(f"unknown identity width {identity!r}")


def claimant_of(mid: str) -> str:
    return f"{mid}#{hashlib.sha256(mid.encode()).hexdigest()[:16]}"


def _folder(i: int) -> str:
    # 5 % Trash (left out by the default predicate), 10 % Sent,
    # 25 % Archive, 60 % INBOX.
    r = i % 20
    return "Trash" if r == 0 else "Sent" if r < 3 else "Archive" if r < 8 else "INBOX"


def build(db_path: Path, messages: int, per_message: int, identity: str, records: str) -> dict:
    """Write the synthetic index: ``messages`` rows in shuffled insert
    order, threads of four, ``per_message`` attachment occurrences each.
    ``records`` ``worst`` fills every field a response record carries to
    past its clip (a 2000-character subject, 11 participants per role
    and 11 references of 501 characters, a 255-byte file name)."""
    worst = records == "worst"
    rng = random.Random(1218)
    order = list(range(messages))
    rng.shuffle(order)
    t0 = time.perf_counter()
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        long = "y" * 501
        for start in range(0, messages, 2000):
            msg_rows, part_rows, att_rows, ext_rows = [], [], [], []
            for i in order[start : start + 2000]:
                mid = message_id(i, identity)
                cid = claimant_of(mid)
                tid = message_id(i - i % 4, identity)
                folder = _folder(i)
                at = f"20{10 + i % 15:02d}-{1 + i % 12:02d}-{1 + i % 28:02d}T{i % 24:02d}:{i % 60:02d}:00+00:00"
                name = f"{i:012d}" + "f" * (255 - 12) if worst else f"{i}.bench:2,S"
                refs = [long] * 11 if worst else [message_id(i - 1, identity)]
                msg_rows.append(
                    (
                        cid,
                        mid,
                        tid,
                        f"/maildir/{folder}/cur/{name}",
                        folder,
                        "s" * 2000 if worst else f"Synthetic subject {i}",
                        at,
                        at,
                        long if worst else message_id(i - 1, identity),
                        json.dumps(refs),
                        1 if per_message else 0,
                        4096 + i,
                        hashlib.sha256(mid.encode()).hexdigest(),
                        at,
                        i % 2,
                        0,
                    )
                )
                for role, n in (("from", 11 if worst else 1), ("to", 11 if worst else 2)):
                    for p in range(n):
                        address = (
                            f"{p:02d}{role}" + "a" * (501 - 6 - len(_DOMAIN)) + _DOMAIN
                            if worst
                            else f"{role}{p}.{i % 500}{_DOMAIN}"
                        )
                        part_rows.append((cid, role, address, long if worst else f"Person {p}"))
                for p in range(11 if worst else 1):
                    address = (
                        f"{p:02d}cc" + "a" * (501 - 4 - len(_DOMAIN)) + _DOMAIN
                        if worst
                        else f"cc{p}.{i % 500}{_DOMAIN}"
                    )
                    part_rows.append((cid, "cc", address, long if worst else None))
                for k in range(per_message):
                    payload = hashlib.sha256(f"{i}:{k}".encode()).hexdigest()
                    occurrence = hashlib.sha256(f"{cid}\0{payload}\0{k}".encode()).hexdigest()
                    filename = ("n" * 2001 if worst else f"file{k}") + ".pdf"
                    att_rows.append(
                        (occurrence, cid, payload, tid, filename, "application/pdf", 1000 + k, at)
                    )
                    ext_rows.append((payload, at))
            conn.executemany(
                "INSERT INTO messages (claimant_id, message_id, thread_id, filepath, folder, "
                "subject, sent_at, occurred_at, in_reply_to, references_json, has_attachments, "
                "size_bytes, content_hash, indexed_at, seen, sender_ambiguous) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                msg_rows,
            )
            conn.executemany("INSERT INTO message_participants VALUES (?, ?, ?, ?)", part_rows)
            conn.executemany(
                "INSERT INTO attachments (attachment_occurrence_id, claimant_id, attachment_id, "
                "thread_id, filename, content_type, size_bytes, seen_at, extractor_module) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pdf')",
                att_rows,
            )
            conn.executemany(
                "INSERT INTO attachment_extractions (attachment_id, extractor_module, "
                "extraction_status, extractor, extracted_at) VALUES (?, 'pdf', 'success', 'pdf', ?)",
                ext_rows,
            )
            conn.commit()
        conn.execute("ANALYZE")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"build_s": round(time.perf_counter() - t0, 2), "db_bytes": db_path.stat().st_size}


# --- the measured statements -------------------------------------------


def scan_sql(kind: str) -> tuple[str, str, list]:
    """The count and identity-scan statements for ``kind`` with no
    filters, from the server's own predicate compiler."""
    from src.lib.predicates import compile_leaves, query_messages_leaves
    from src.lib.sqlite import _ATTACHMENT_FROM

    none = dict.fromkeys(
        (
            "sender",
            "recipient",
            "participant",
            "subject",
            "text",
            "folder",
            "date_from",
            "date_to",
            "has_attachments",
            "authority_class",
            "seen",
            "flagged",
        )
    )
    where_sql, params = compile_leaves(query_messages_leaves(**none))
    if kind == "messages":
        frm, ident = "FROM messages m", "m.claimant_id"
    else:
        frm, ident = _ATTACHMENT_FROM, "a.attachment_occurrence_id"
    count = f"SELECT COUNT(*) {frm} WHERE {where_sql}"  # nosec B608
    scan = f"SELECT {ident} {frm} WHERE {where_sql}"  # nosec B608
    return count, scan, params


def identity_hash(identity: bytes) -> bytes:
    return hashlib.sha256(IDENTITY_DOMAIN + identity).digest()


def _ro(db_path: str) -> sqlite3.Connection:
    from src.lib.sqlite import Database

    return Database(db_path)._connect()


def _rss_kib() -> int:
    """Peak RSS so far in KiB (``ru_maxrss`` is bytes on macOS)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak // 1024 if sys.platform == "darwin" else peak


def _import_serializers() -> None:
    """Import the record serializers up front, so their import time is
    outside every timing and inside every RSS figure, the baseline's
    included."""
    import src.tools.retrieval  # noqa: F401


def phase_idle(db_path: str) -> dict:
    """The baseline: imports, a read-only connection and an empty read
    transaction."""
    _import_serializers()
    count_sql, _, params = scan_sql("messages")
    with closing(_ro(db_path)) as conn:
        conn.execute("BEGIN")
        conn.execute(count_sql, params).fetchone()
        conn.rollback()
    return {"rss_kib": _rss_kib()}


def phase_certificate(db_path: str, kind: str, method: str) -> dict:
    """COUNT, then every matching identity scanned, sorted by UTF-8
    bytes, de-duplicated and hashed (length-prefixed), in one read
    transaction. ``stream`` lets SQLite order the scan; ``collect``
    fetches every identity and sorts in Python."""
    _import_serializers()
    count_sql, scan, params = scan_sql(kind)
    t0 = time.perf_counter()
    with closing(_ro(db_path)) as conn:
        conn.execute("BEGIN")
        count = conn.execute(count_sql, params).fetchone()[0]
        t_count = time.perf_counter()
        digest = hashlib.sha256(CERT_DOMAIN)
        scanned = unique = identity_bytes = max_bytes = 0
        if method == "stream":
            prev = None
            for row in conn.execute(scan + " ORDER BY 1", params):
                b = row[0].encode()
                scanned += 1
                if b == prev:
                    continue
                prev = b
                unique += 1
                identity_bytes += len(b)
                max_bytes = max(max_bytes, len(b))
                digest.update(len(b).to_bytes(8, "big") + b)
            t_fetch = time.perf_counter()
        else:
            ids = [row[0].encode() for row in conn.execute(scan, params)]
            scanned = len(ids)
            t_fetch = time.perf_counter()
            ids.sort()
            prev = None
            for b in ids:
                if b == prev:
                    continue
                prev = b
                unique += 1
                identity_bytes += len(b)
                max_bytes = max(max_bytes, len(b))
                digest.update(len(b).to_bytes(8, "big") + b)
        conn.rollback()
    t_end = time.perf_counter()
    return {
        "count": count,
        "scanned": scanned,
        "unique": unique,
        "identity_bytes": identity_bytes,
        "max_identity_bytes": max_bytes,
        "digest": digest.hexdigest(),
        "count_s": t_count - t0,
        "scan_s": t_fetch - t_count,
        "total_s": t_end - t0,
        "rss_kib": _rss_kib(),
    }


def _materialize(conn: sqlite3.Connection, kind: str, missing: list[str]) -> list[dict]:
    """The response records of ``missing``, read on ``conn`` (the
    round's transaction) and serialized as the query tools do."""
    from src.lib import sqlite as db
    from src.tools.outputs import listed_message
    from src.tools.retrieval import _listed_attachment

    out: list[dict] = []
    for start in range(0, len(missing), 500):
        chunk = missing[start : start + 500]
        marks = ",".join("?" * len(chunk))
        if kind == "messages":
            rows = conn.execute(
                f"SELECT {db._MESSAGE_COLUMNS} FROM messages m "  # nosec B608
                f"WHERE m.claimant_id IN ({marks}) ORDER BY m.claimant_id",
                chunk,
            ).fetchall()
            records = [db._row_to_message_record(r) for r in rows]
            db._attach_participants(conn, records)
            out += [listed_message(r).model_dump(mode="json", by_alias=True) for r in records]
        else:
            rows = conn.execute(
                f"SELECT {db._OCCURRENCE_COLUMNS} {db._ATTACHMENT_FROM} "  # nosec B608
                f"WHERE a.attachment_occurrence_id IN ({marks}) "
                "ORDER BY a.attachment_occurrence_id",
                chunk,
            ).fetchall()
            out += [
                _listed_attachment(db._row_to_occurrence(r)).model_dump(mode="json", by_alias=True)
                for r in rows
            ]
    return out


def phase_reconcile(db_path: str, kind: str, k: int, request: str) -> dict:
    """One reconcile round: parse the uploaded hashes, then in one read
    transaction count, scan, certify, diff on hashes and materialize at
    most ``k`` missing records (lowest identities first)."""
    _import_serializers()
    count_sql, scan, params = scan_sql(kind)
    t0 = time.perf_counter()
    hashes = json.loads(Path(request).read_bytes())["hashes"]
    client = {bytes.fromhex(h) for h in hashes}
    del hashes
    t_parse = time.perf_counter()
    with closing(_ro(db_path)) as conn:
        conn.execute("BEGIN")
        # Wall clock, for the WAL phase to line up with its samples.
        started_at = time.time()
        count = conn.execute(count_sql, params).fetchone()[0]
        digest = hashlib.sha256(CERT_DOMAIN)
        server: set[bytes] = set()
        missing: list[str] = []
        missing_total = 0
        for row in conn.execute(scan + " ORDER BY 1", params):
            b = row[0].encode()
            digest.update(len(b).to_bytes(8, "big") + b)
            h = identity_hash(b)
            server.add(h)
            if h not in client:
                missing_total += 1
                if len(missing) < k:
                    missing.append(row[0])
        extras = sorted(client - server)
        t_diff = time.perf_counter()
        records = _materialize(conn, kind, missing)
        t_records = time.perf_counter()
        conn.rollback()
        ended_at = time.time()
    response = {
        "complete": missing_total <= k,
        "snapshot_count": count,
        "certificate": digest.hexdigest(),
        "missing_total": missing_total,
        "missing": records,
        "extras": [h.hex() for h in extras],
    }
    body = json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode()
    t_end = time.perf_counter()
    return {
        "count": count,
        "scanned": len(server),
        "missing_total": missing_total,
        "returned": len(records),
        "extras": len(extras),
        "response_bytes": len(body),
        "record_bytes_max": max(
            (
                len(json.dumps(r, separators=(",", ":"), ensure_ascii=False).encode())
                for r in records
            ),
            default=0,
        ),
        "parse_s": t_parse - t0,
        "diff_s": t_diff - t_parse,
        "records_s": t_records - t_diff,
        "transaction_s": t_records - t_parse,
        "total_s": t_end - t0,
        "started_at": started_at,
        "ended_at": ended_at,
        "rss_kib": _rss_kib(),
    }


def phase_writer(db_path: str, stop: str, commit_bytes: int, interval: float) -> dict:
    """A concurrent writer: until ``stop`` exists, commit transactions
    that flip eight messages' ``seen`` and append ``commit_bytes`` of
    ballast, as the indexer's steady-state batch of eight would."""
    rng = random.Random(7)
    commits = 0
    with closing(sqlite3.connect(db_path, isolation_level=None, timeout=30)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        top = conn.execute("SELECT MAX(rowid) FROM messages").fetchone()[0]
        while not os.path.exists(stop):
            conn.execute("BEGIN IMMEDIATE")
            rowids = [rng.randint(1, top) for _ in range(8)]
            conn.execute(
                "UPDATE messages SET seen = 1 - seen WHERE rowid IN (?, ?, ?, ?, ?, ?, ?, ?)",
                rowids,
            )
            cur = conn.execute(
                "INSERT INTO bench_ballast (payload) VALUES (randomblob(?))", (commit_bytes,)
            )
            conn.execute("DELETE FROM bench_ballast WHERE id <= ?", (cur.lastrowid - 64,))
            conn.execute("COMMIT")
            commits += 1
            if interval:
                time.sleep(interval)
    return {"commits": commits}


# --- orchestration ------------------------------------------------------


def _child(phase: str, **kwargs) -> dict:
    """Run ``phase`` in a fresh interpreter and return its JSON."""
    out = subprocess.run(
        [sys.executable, __file__, "_phase", phase, json.dumps(kwargs)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout)


def _median(runs: list[dict], key: str) -> float:
    return round(statistics.median(r[key] for r in runs), 4)


def write_request(db_path: str, kind: str, missing: int, extras: int, out: Path) -> dict:
    """The client's upload: the SHA-256 of every matching identity but
    ``missing`` of them (spread evenly), plus ``extras`` hashes the
    server does not hold. Also sizes the same upload as base64."""
    count_sql, scan, params = scan_sql(kind)
    with closing(_ro(db_path)) as conn:
        conn.execute("BEGIN")
        ids = [row[0].encode() for row in conn.execute(scan + " ORDER BY 1", params)]
        conn.rollback()
    step = max(1, len(ids) // missing) if missing else 0
    skip = set(range(0, len(ids), step)[:missing]) if missing else set()
    held = [identity_hash(b) for n, b in enumerate(ids) if n not in skip]
    held += [hashlib.sha256(f"extra:{n}".encode()).digest() for n in range(extras)]
    body = json.dumps({"hashes": [h.hex() for h in held]}, separators=(",", ":")).encode()
    out.write_bytes(body)
    b64 = json.dumps(
        {"hashes": [base64.b64encode(h).decode() for h in held]}, separators=(",", ":")
    ).encode()
    return {
        "members": len(ids),
        "uploaded": len(held),
        "request_bytes_hex": len(body),
        "request_bytes_base64": len(b64),
        "max_identity_bytes": max((len(b) for b in ids), default=0),
    }


def run_wal(
    db_path: str, kind: str, k: int, request: str, commit_bytes: int, interval: float
) -> dict:
    """WAL growth while one reconcile round holds its snapshot under a
    concurrent writer, and whether a TRUNCATE checkpoint then reclaims
    it."""
    wal = Path(db_path + "-wal")
    stop = Path(db_path + ".stop")
    stop.unlink(missing_ok=True)

    def size() -> int:
        try:
            return wal.stat().st_size
        except FileNotFoundError:
            return 0

    def ballast_top() -> int:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as c:
            return c.execute("SELECT COALESCE(MAX(id), 0) FROM bench_ballast").fetchone()[0]

    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer = subprocess.Popen(
        [
            sys.executable,
            __file__,
            "_phase",
            "writer",
            json.dumps(
                {
                    "db_path": db_path,
                    "stop": str(stop),
                    "commit_bytes": commit_bytes,
                    "interval": interval,
                }
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    warm_max = 0
    deadline = time.perf_counter() + 3
    while time.perf_counter() < deadline:
        warm_max = max(warm_max, size())
        time.sleep(0.01)
    top0 = ballast_top()
    result: dict = {}

    def reader() -> None:
        result.update(_child("reconcile", db_path=db_path, kind=kind, k=k, request=request))

    t = threading.Thread(target=reader)
    samples: list[tuple[float, int]] = []
    t.start()
    while t.is_alive():
        samples.append((time.time(), size()))
        time.sleep(0.005)
    top1 = ballast_top()
    # The WAL when the round's transaction began, and its largest size
    # from then until 0.1 s after it ended (a retained frame shows up
    # by the next commit).
    start, end = result["started_at"], result["ended_at"]
    at_start = max((s for when, s in samples if when <= start), default=0)
    in_window = [s for when, s in samples if start <= when <= end + 0.1]
    stop.touch()
    writer_out = json.loads(writer.communicate(timeout=60)[0])
    stop.unlink()
    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        busy, log_pages, ckpt_pages = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return {
        "commit_bytes": commit_bytes,
        "writer_interval_s": interval,
        "wal_steady_max_bytes": warm_max,
        "wal_at_transaction_start_bytes": at_start,
        "wal_max_during_transaction_bytes": max(in_window, default=0),
        "commits_during_reader": top1 - top0,
        "writer_commits_total": writer_out["commits"],
        "reader_transaction_s": round(result["transaction_s"], 3),
        "checkpoint_after": {"busy": busy, "log_pages": log_pages, "checkpointed": ckpt_pages},
        "wal_after_checkpoint_bytes": size(),
    }


def run(args: argparse.Namespace) -> dict:
    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    db_path = work / f"bench-{args.messages}-{args.per_message}-{args.identity}-{args.records}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    report: dict = {
        "config": {
            "messages": args.messages,
            "occurrences_per_message": args.per_message,
            "identity": args.identity,
            "records": args.records,
            "repeat": args.repeat,
            "sqlite": sqlite3.sqlite_version,
            "python": sys.version.split()[0],
        },
        "build": build(db_path, args.messages, args.per_message, args.identity, args.records),
    }
    db = str(db_path)
    report["idle_rss_kib"] = _child("idle", db_path=db)["rss_kib"]
    with closing(_ro(db)) as conn:
        report["plans"] = {
            kind: [
                row[3]
                for row in conn.execute(
                    "EXPLAIN QUERY PLAN " + scan_sql(kind)[1] + " ORDER BY 1", scan_sql(kind)[2]
                )
            ]
            for kind in ("messages", "occurrences")
        }
    report["certificate"] = {}
    for kind in ("messages", "occurrences"):
        for method in ("stream", "collect"):
            runs = [
                _child("certificate", db_path=db, kind=kind, method=method)
                for _ in range(args.repeat)
            ]
            if len({r["digest"] for r in runs}) != 1 or any(
                r["scanned"] != r["count"] for r in runs
            ):
                raise SystemExit(f"{kind}/{method}: digest or count mismatch")
            report["certificate"][f"{kind}/{method}"] = {
                "count": runs[0]["count"],
                "identity_bytes": runs[0]["identity_bytes"],
                "max_identity_bytes": runs[0]["max_identity_bytes"],
                "digest": runs[0]["digest"],
                "count_s": _median(runs, "count_s"),
                "scan_s": _median(runs, "scan_s"),
                "total_s": _median(runs, "total_s"),
                "rss_kib": max(r["rss_kib"] for r in runs),
            }
        if (
            report["certificate"][f"{kind}/stream"]["digest"]
            != report["certificate"][f"{kind}/collect"]["digest"]
        ):
            raise SystemExit(f"{kind}: SQL order and byte order digests differ")
    report["reconcile"] = {}
    for kind in ("messages", "occurrences"):
        request = work / f"request-{kind}.json"
        req = write_request(db, kind, args.missing, args.extras, request)
        rounds = {}
        for k in args.k:
            runs = [
                _child("reconcile", db_path=db, kind=kind, k=k, request=str(request))
                for _ in range(args.repeat)
            ]
            r0 = runs[0]
            rounds[str(k)] = {
                "returned": r0["returned"],
                "extras": r0["extras"],
                "missing_total": r0["missing_total"],
                "response_bytes": r0["response_bytes"],
                "record_bytes_max": r0["record_bytes_max"],
                "rounds_to_repair": math.ceil(r0["missing_total"] / k),
                "rounds_from_empty": math.ceil(req["members"] / k),
                "parse_s": _median(runs, "parse_s"),
                "diff_s": _median(runs, "diff_s"),
                "records_s": _median(runs, "records_s"),
                "transaction_s": _median(runs, "transaction_s"),
                "total_s": _median(runs, "total_s"),
                "rss_kib": max(r["rss_kib"] for r in runs),
            }
        report["reconcile"][kind] = {"request": req, "k": rounds}
    if args.wal:
        report["wal"] = [
            {
                "kind": kind,
                **run_wal(
                    db,
                    kind,
                    max(args.k),
                    str(work / f"request-{kind}.json"),
                    args.writer_commit_bytes,
                    interval,
                ),
            }
            for kind in ("messages", "occurrences")
            for interval in args.writer_interval
        ]
    return report


def main(argv: list[str] | None = None) -> dict:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_phase"]:
        phases = {
            "idle": phase_idle,
            "certificate": phase_certificate,
            "reconcile": phase_reconcile,
            "writer": phase_writer,
        }
        out = phases[argv[1]](**json.loads(argv[2]))
        print(json.dumps(out))
        return out
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workdir", default="/tmp/reconcile-bench")
    p.add_argument("--messages", type=int, default=50_000)
    p.add_argument("--per-message", type=int, default=3, help="attachment occurrences per message")
    p.add_argument("--identity", choices=("typical", "ascii998", "utf8x4"), default="typical")
    p.add_argument("--records", choices=("typical", "worst"), default="typical")
    p.add_argument(
        "--k", type=lambda s: [int(x) for x in s.split(",")], default=[100, 500, 1000, 2000, 5000]
    )
    p.add_argument("--missing", type=int, default=5000, help="members the client upload leaves out")
    p.add_argument(
        "--extras", type=int, default=100, help="hashes the client holds that the server does not"
    )
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--wal", action="store_true", help="also measure WAL growth under a writer")
    p.add_argument("--writer-commit-bytes", type=int, default=128 * 1024)
    p.add_argument(
        "--writer-interval", type=lambda s: [float(x) for x in s.split(",")], default=[0.0, 0.1]
    )
    report = run(p.parse_args(argv))
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    main()
