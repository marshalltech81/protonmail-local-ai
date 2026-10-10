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
(``query_messages_leaves`` and ``compile_leaves``; unfiltered, Trash
is left out), so the scan is the one the server would run. ``--filtered``
also times filtered predicates (participant, subject, body text,
authority, dates) against one production page of the same query.
Timings are plain ``time.perf_counter`` differences, never a
profiler (AGENTS.md "Bound the work per input"). Each measured phase
runs in a fresh child process so its peak RSS (``VmHWM``) is its
own.

Run it inside the mcp-server image (docs/mcp-tools.md, "Certifying a
paged run: sizing"). The tables mirror the indexer's ``messages``,
``message_participants``, ``message_participant_names``,
``message_chunks`` and ``message_chunks_fts``, ``entities``,
``attachments``, ``attachment_extractions`` and ``pending_deletions``
with the indexes the measured statements use; other tables are left
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
from collections.abc import Iterable
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
    sent_at         TEXT,
    sent_at_status  TEXT CHECK (sent_at_status IN ('parsed', 'missing', 'invalid')),
    occurred_at     TEXT,
    effective_at    TEXT GENERATED ALWAYS AS
                    (COALESCE(occurred_at, sent_at, first_indexed_at)) VIRTUAL,
    in_reply_to     TEXT,
    references_json TEXT NOT NULL,
    has_attachments INTEGER NOT NULL,
    size_bytes      INTEGER,
    content_hash    TEXT,
    indexed_at      TEXT NOT NULL,
    first_indexed_at TEXT NOT NULL,
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
CREATE TABLE message_participant_names (
    claimant_id TEXT NOT NULL,
    role        TEXT NOT NULL,
    address     TEXT NOT NULL,
    name        TEXT NOT NULL,
    PRIMARY KEY (claimant_id, role, address, name)
);
CREATE INDEX idx_message_participant_names_address_name
    ON message_participant_names(address, name);
CREATE TABLE message_chunks (
    chunk_id      TEXT PRIMARY KEY,
    claimant_id   TEXT NOT NULL,
    thread_id     TEXT NOT NULL,
    chunk_index   INTEGER NOT NULL,
    text          TEXT NOT NULL,
    fts_rowid     INTEGER,
    attachment_id TEXT,
    kind          TEXT NOT NULL
);
CREATE INDEX idx_message_chunks_claimant ON message_chunks(claimant_id);
CREATE INDEX idx_message_chunks_fts_rowid ON message_chunks(fts_rowid);
CREATE VIRTUAL TABLE message_chunks_fts USING fts5(
    text, content='', contentless_delete=1, tokenize='porter unicode61'
);
CREATE TABLE entities (
    entity_id       TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    canonical_key   TEXT NOT NULL,
    organization_id TEXT,
    authority_class TEXT NOT NULL DEFAULT 'unclassified',
    authority_rule  TEXT
);
CREATE INDEX idx_entities_authority ON entities(authority_class);
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
    text_extractor            TEXT,
    extraction_deferred_at    TEXT
);
CREATE INDEX idx_attachments_deferred ON attachments(claimant_id, attachment_id)
    WHERE extraction_deferred_at IS NOT NULL;
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
-- Message and occurrence identities whose records are worst-case
-- (``--records mixed``), for an upload that leaves exactly those out.
CREATE TABLE bench_worst (identity TEXT PRIMARY KEY);
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


# The parser's per-message address cap (indexer/src/parser.py
# ``MAX_MESSAGE_ADDRESSES``): at most this many participant rows.
MAX_MESSAGE_ADDRESSES = 10_000
# indexer/src/parser.py ``MAX_EXTRA_PARTICIPANT_NAMES``.
MAX_EXTRA_PARTICIPANT_NAMES = 1_000
# A four-byte character: the most UTF-8 bytes a character-clipped field
# can carry per character.
_WIDE = "\U0001f600"


def _mixed_worst(i: int) -> bool:
    """Whether message ``i`` carries worst-case records under
    ``--records mixed``: one in fifty, so a large corpus stays buildable
    while every record a round returns can be worst-case."""
    return i % 50 == 1


def _shape(i: int, identity: str, records: str, references: int) -> dict:
    """The response-record fields of message ``i`` for one ``records``
    shape (``build``)."""
    if records == "mixed":
        records = "worst" if _mixed_worst(i) else "typical"
    if records == "worst":
        wide = _WIDE * 501
        people = [
            (role, f"{p:02d}{role}" + _WIDE * (501 - 2 - len(role)), wide)
            for role in ("from", "to", "cc")
            for p in range(11)
        ]
        return {
            "subject": _WIDE * 2000,
            "in_reply_to": wide,
            "references": [wide] * 11,
            "people": people,
            "file_name": f"{i:012d}" + "f" * (255 - 12),
            "filename": wide,
            "content_type": wide,
        }
    previous = message_id(i - 1, identity)
    base = {
        "subject": f"Synthetic subject {i}",
        "in_reply_to": previous,
        "file_name": f"{i}.bench:2,S",
        "filename": "file.pdf",
        "content_type": "application/pdf",
    }
    if records == "cardinality":
        counts = (("from", 1), ("to", 4_999), ("cc", MAX_MESSAGE_ADDRESSES - 5_000))
        return {
            **base,
            "references": [f"r{n}@x.example" for n in range(references)],
            "people": [
                (role, f"{role}{p}@x.example", f"Person {p}")
                for role, n in counts
                for p in range(n)
            ],
            # The parser also keeps up to MAX_EXTRA_PARTICIPANT_NAMES
            # further names of one address: name rows only.
            "extra_names": [
                ("to", "to0@x.example", f"Alias {n}") for n in range(MAX_EXTRA_PARTICIPANT_NAMES)
            ],
        }
    return {
        **base,
        "references": [previous],
        "people": [
            ("from", f"from0.{i % 500}{_DOMAIN}", "Person 0"),
            ("to", f"to0.{i % 500}{_DOMAIN}", "Person 0"),
            ("to", f"to1.{i % 500}{_DOMAIN}", "Person 1"),
            ("cc", f"cc0.{i % 500}{_DOMAIN}", None),
        ],
    }


# Words per synthetic body chunk: at least the 20 tokens a chunk of real
# mail carries (AGENTS.md: sparse chunks hid FTS5 segment growth, #1262).
BODY_TOKENS = 40
# Distinct filler words, so the FTS index carries a realistic vocabulary.
_VOCABULARY = 5000


def _body(i: int, chunk: int = 0, tokens: int = BODY_TOKENS) -> str:
    """Message ``i``'s body chunk ``chunk``: for chunk 0 ``alpha<i % 50>``
    (in every fiftieth body) and ``gamma<i>`` (in this body only), then
    filler words drawn from ``_VOCABULARY``, ``tokens`` words in all."""
    lead = [f"alpha{i % 50}", f"gamma{i}"] if chunk == 0 else []
    base = chunk * tokens
    filler = [
        f"w{(i * 7919 + (base + j) * 104729) % _VOCABULARY}" for j in range(tokens - len(lead))
    ]
    return " ".join([*lead, *filler])


def build(
    db_path: Path,
    messages: int,
    per_message: int,
    identity: str,
    records: str,
    references: int = 0,
    extracted_chars: int = 0,
    chunks: int = 1,
    chunk_tokens: int = BODY_TOKENS,
) -> dict:
    """Write the synthetic index: ``messages`` rows in shuffled insert
    order, threads of four, ``per_message`` attachment occurrences each.

    ``records`` sets the response-record fields: ``typical``; ``worst``,
    every character-clipped field past its clip in four-byte characters
    (a 2000-character subject, 11 participants per role, 11 references,
    In-Reply-To, filename and MIME type of 501 characters, a 255-byte
    file name); or ``cardinality``, the most rows a record can carry
    (``MAX_MESSAGE_ADDRESSES`` participants, the parser's cap, and
    ``references`` References entries, which the parser does not cap by
    count).

    Every message gets ``chunks`` body chunks of ``chunk_tokens`` words
    each (``_body``; the parser splits a large message into roughly
    1,000-token chunks), its display names in ``message_participant_names``,
    completeness flags of 1 so every filter decides, and each From
    address a person entity (every fifth address in order ``vendor``, the rest
    ``unclassified``). ``extracted_chars`` above 0 stores that many
    four-byte characters of extracted text on every extraction row
    (the indexer's default cap is 2,000,000 characters); 0 leaves it
    NULL."""
    rng = random.Random(1218)
    order = list(range(messages))
    rng.shuffle(order)
    t0 = time.perf_counter()
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        batch = 2000 if records != "cardinality" and not extracted_chars else 10
        text = _WIDE * extracted_chars if extracted_chars else None
        min_tokens = chunk_tokens
        n_chunks = n_parts = n_names = 0
        for start in range(0, messages, batch):
            msg_rows, part_rows, att_rows, ext_rows = [], [], [], []
            name_rows, chunk_rows, fts_rows, worst_rows = [], [], [], []
            for i in order[start : start + batch]:
                mid = message_id(i, identity)
                cid = claimant_of(mid)
                tid = message_id(i - i % 4, identity)
                folder = _folder(i)
                at = f"20{10 + i % 15:02d}-{1 + i % 12:02d}-{1 + i % 28:02d}T{i % 24:02d}:{i % 60:02d}:00+00:00"
                shape = _shape(i, identity, records, references)
                worst = records == "mixed" and _mixed_worst(i)
                if worst:
                    worst_rows.append((cid,))
                msg_rows.append(
                    (
                        cid,
                        mid,
                        tid,
                        f"/maildir/{folder}/cur/{shape['file_name']}",
                        folder,
                        shape["subject"],
                        at,
                        at,
                        shape["in_reply_to"],
                        json.dumps(shape["references"]),
                        1 if per_message else 0,
                        4096 + i,
                        hashlib.sha256(mid.encode()).hexdigest(),
                        at,
                        at,
                        i % 2,
                        0,
                    )
                )
                part_rows += [(cid, role, address, name) for role, address, name in shape["people"]]
                name_rows += [
                    (cid, role, address, name)
                    for role, address, name in shape["people"]
                    if name is not None
                ] + [
                    (cid, role, address, name)
                    for role, address, name in shape.get("extra_names", [])
                ]
                for c in range(chunks):
                    body = _body(i, c, chunk_tokens)
                    min_tokens = min(min_tokens, len(body.split()))
                    rowid = i * chunks + c + 1
                    fts_rows.append((rowid, body))
                    chunk_rows.append((f"{cid}:{c}", cid, tid, c, body, rowid, None, "body"))
                    n_chunks += 1
                for k in range(per_message):
                    payload = hashlib.sha256(f"{i}:{k}".encode()).hexdigest()
                    occurrence = hashlib.sha256(f"{cid}\0{payload}\0{k}".encode()).hexdigest()
                    att_rows.append(
                        (
                            occurrence,
                            cid,
                            payload,
                            tid,
                            shape["filename"],
                            shape["content_type"],
                            1000 + k,
                            at,
                        )
                    )
                    ext_rows.append((payload, text, at))
                    if worst:
                        worst_rows.append((occurrence,))
            conn.executemany(
                "INSERT INTO messages (claimant_id, message_id, thread_id, filepath, folder, "
                "subject, sent_at, occurred_at, in_reply_to, references_json, has_attachments, "
                "size_bytes, content_hash, indexed_at, first_indexed_at, sent_at_status, "
                "seen, sender_ambiguous, "
                "participant_names_complete, subject_complete, from_addresses_complete, "
                "to_addresses_complete, cc_addresses_complete, attachments_manifest_complete, "
                "body_complete) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'parsed', ?, ?, "
                "1, 1, 1, 1, 1, 1, 1)",
                msg_rows,
            )
            n_parts += len(part_rows)
            n_names += len(name_rows)
            conn.executemany("INSERT INTO message_participants VALUES (?, ?, ?, ?)", part_rows)
            conn.executemany("INSERT INTO message_participant_names VALUES (?, ?, ?, ?)", name_rows)
            conn.executemany("INSERT INTO message_chunks_fts (rowid, text) VALUES (?, ?)", fts_rows)
            conn.executemany("INSERT INTO bench_worst (identity) VALUES (?)", worst_rows)
            conn.executemany(
                "INSERT INTO message_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", chunk_rows
            )
            conn.executemany(
                "INSERT INTO attachments (attachment_occurrence_id, claimant_id, attachment_id, "
                "thread_id, filename, content_type, size_bytes, seen_at, extractor_module) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pdf')",
                att_rows,
            )
            conn.executemany(
                "INSERT INTO attachment_extractions (attachment_id, extractor_module, "
                "extraction_status, extractor, extracted_text, extracted_at) "
                "VALUES (?, 'pdf', 'success', 'pdf', ?, ?)",
                ext_rows,
            )
            conn.commit()
        conn.execute(
            "INSERT INTO entities (entity_id, kind, canonical_key, authority_class) "
            "SELECT 'e:' || address, 'person', address, "
            "CASE WHEN n % 5 = 0 THEN 'vendor' ELSE 'unclassified' END "
            "FROM (SELECT address, row_number() OVER (ORDER BY address) AS n "
            "FROM (SELECT DISTINCT address FROM message_participants WHERE role = 'from'))"
        )
        conn.commit()
        # No ANALYZE: neither the indexer nor the server runs it (nor
        # PRAGMA optimize), so a deployed index has no sqlite_stat1 and
        # the planner works without statistics, as it does here.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {
        "build_s": round(time.perf_counter() - t0, 2),
        "db_bytes": db_path.stat().st_size,
        "body_tokens_min": min_tokens,
        "chunks": n_chunks,
        "participant_rows": n_parts,
        "name_rows": n_names,
    }


# --- the measured statements -------------------------------------------


def scan_sql(kind: str, filters: dict | None = None) -> tuple[str, str, list]:
    """The count and identity-scan statements for ``kind`` under
    ``filters`` (``query_messages`` keyword arguments; none by default),
    from the server's own predicate compiler, as ``query_messages`` and
    ``query_attachments`` build them."""
    from src.lib.predicates import query_messages_leaves
    from src.lib.sqlite import _ATTACHMENT_FROM, _attachment_clauses, _message_expression

    filters = dict(filters or {})
    where = _where_leaves(filters.pop("where", None))
    # query_attachments' own filters, compiled by its own helper.
    own = {
        name: filters.pop(name, None)
        for name in ("filename", "content_type", "extraction_status", "claimant_id", "thread_id")
    }

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
    leaves = query_messages_leaves(**{**none, **filters})
    # The flat leaves and the explicit ``where`` expression, ANDed as
    # ``query_messages`` does (``_message_expression``).
    where_sql, params = _message_expression(leaves, where)
    if kind == "messages":
        if any(v is not None for v in own.values()):
            raise ValueError("attachment filters apply to occurrences only")
        frm, ident = "FROM messages m", "m.claimant_id"
    else:
        if where:
            raise ValueError("where applies to messages only")
        _, clauses, clause_params = _attachment_clauses(**own)
        where_sql = " AND ".join([where_sql, *clauses])
        params = [*params, *clause_params]
        frm, ident = _ATTACHMENT_FROM, "a.attachment_occurrence_id"
    count = f"SELECT COUNT(*) {frm} WHERE {where_sql}"  # nosec B608
    scan = f"SELECT {ident} {frm} WHERE {where_sql}"  # nosec B608
    return count, scan, params


def _where_leaves(spec: dict | None) -> list:
    """The normalized ``where`` leaves of a request ``spec`` (the tool's
    ``{"all": [...]}`` shape), as ``query_messages`` receives them."""
    if not spec:
        return []
    from src.lib.predicates import Where, normalize_where

    return normalize_where(Where.model_validate(spec))


def identity_hash(identity: bytes) -> bytes:
    return hashlib.sha256(IDENTITY_DOMAIN + identity).digest()


def _ro(db_path: str) -> sqlite3.Connection:
    from src.lib.sqlite import Database

    return Database(db_path)._connect()


def _rss_kib() -> int:
    """This process's peak RSS so far in KiB.

    On Linux, ``VmHWM`` from ``/proc/self/status``: ``ru_maxrss`` keeps
    the parent's peak across ``exec``, so a child launched by a parent
    that once held a large corpus would report the parent's figure.
    Elsewhere ``ru_maxrss`` (bytes on macOS)."""
    try:
        with open("/proc/self/status") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        pass
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


def phase_certificate(db_path: str, kind: str, method: str, filters: dict | None = None) -> dict:
    """COUNT, then every matching identity scanned, sorted by UTF-8
    bytes, de-duplicated and hashed (length-prefixed), in one read
    transaction. ``stream`` lets SQLite order the scan; ``collect``
    fetches every identity and sorts in Python."""
    _import_serializers()
    count_sql, scan, params = scan_sql(kind, filters)
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


def _materialize(
    conn: sqlite3.Connection, kind: str, missing: list[str]
) -> tuple[list[dict], dict[str, int]]:
    """The response records of ``missing``, read on ``conn`` (the
    round's transaction) and serialized as the query tools do, with the
    participant rows and References entries read for them: the server's
    readers load every one before the output clips each list."""
    from src.lib import sqlite as db
    from src.tools.outputs import listed_message
    from src.tools.retrieval import _listed_attachment

    out: list[dict] = []
    read = {"participant_rows": 0, "references": 0}
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
            read["participant_rows"] += sum(len(r.from_) + len(r.to) + len(r.cc) for r in records)
            read["references"] += sum(len(r.references) for r in records)
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
    return out, read


def unpack_hashes(body: bytes) -> list[bytes]:
    """The digests of a packed upload: one JSON string of base64 over
    the concatenated 32-byte digests, so the element count is the
    decoded length over 32, known before any per-digest object exists."""
    raw = base64.b64decode(json.loads(body)["hashes"], validate=True)
    if len(raw) % 32:
        raise ValueError("packed upload is not a whole number of digests")
    return [raw[i : i + 32] for i in range(0, len(raw), 32)]


def _sorted_identities(
    conn: sqlite3.Connection, scan: str, params: list, method: str
) -> Iterable[str]:
    """The scan's identities in UTF-8 byte order: ordered by SQLite
    (``stream``) or fetched whole and sorted here (``collect``)."""
    if method == "stream":
        return (row[0] for row in conn.execute(scan + " ORDER BY 1", params))
    ids = [row[0] for row in conn.execute(scan, params)]
    ids.sort(key=str.encode)
    return ids


def phase_reconcile(
    db_path: str,
    kind: str,
    k: int,
    request: str,
    hold_s: float = 0.0,
    method: str = "stream",
    worst_first: bool = False,
) -> dict:
    """One reconcile round: parse the packed upload, then in one read
    transaction count, scan (``method``, as ``phase_certificate``),
    certify, diff on hashes and materialize at most ``k`` missing
    records (lowest identities first).

    ``hold_s`` keeps the transaction open that much longer after the
    round's work, outside every timing: only the smoke test's WAL check
    uses it, so a tiny corpus still overlaps the writer. ``worst_first``
    materializes missing worst-case identities (``--records mixed``)
    before the others, so a round that misses every member still returns
    worst-case records."""
    _import_serializers()
    count_sql, scan, params = scan_sql(kind)
    t0 = time.perf_counter()
    client = set(unpack_hashes(Path(request).read_bytes()))
    t_parse = time.perf_counter()
    with closing(_ro(db_path)) as conn:
        conn.execute("BEGIN")
        count = conn.execute(count_sql, params).fetchone()[0]
        # BEGIN is deferred: the snapshot is taken by the COUNT, so the
        # wall clock the WAL phase lines up with its samples is read
        # after it.
        started_at = time.time()
        digest = hashlib.sha256(CERT_DOMAIN)
        server: set[bytes] = set()
        missing: list[str] = []
        others: list[str] = []
        worst = (
            {row[0] for row in conn.execute("SELECT identity FROM bench_worst")}
            if worst_first
            else set()
        )
        missing_total = 0
        for identity in _sorted_identities(conn, scan, params, method):
            b = identity.encode()
            digest.update(len(b).to_bytes(8, "big") + b)
            h = identity_hash(b)
            server.add(h)
            if h not in client:
                missing_total += 1
                if worst_first and identity not in worst:
                    if len(others) < k:
                        others.append(identity)
                elif len(missing) < k:
                    missing.append(identity)
        missing = (missing + others)[:k]
        extras = sorted(client - server)
        t_diff = time.perf_counter()
        records, read = _materialize(conn, kind, missing)
        t_records = time.perf_counter()
        if hold_s:
            time.sleep(hold_s)
        # Still inside the transaction: its frames are retained until the
        # rollback.
        ended_at = time.time()
        conn.rollback()
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
        **read,
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


# Filtered predicates the server compiles to different access paths:
# participant substring (address and display-name scan), exact sender,
# subject substring (casefold of every subject), body words (FTS5),
# authority class (entity join) and a date range. ``nobody`` matches
# nothing, so its scan visits every participant and name for no result.
def _wl(leaf: str, value: str, negate: bool = False, role: str = "from") -> dict:
    item = {"leaf": leaf, "value": value, "negate": negate}
    if leaf != "body_words":
        item["role"] = role
    return item


_WHERE_LEAVES: list[dict] = [
    _wl("address_contains", "nobody", True, "to"),
    _wl("display_name_contains", "nobody", True, "cc"),
    _wl("address_or_name_contains", "from0.7", True, "visible_recipient"),
    _wl("domain_is", "bench.example", False, "from"),
    _wl("address_is", "to1.3@bench.example", True, "to"),
    _wl("body_words", "gamma4242", True),
    _wl("body_words", "alpha7"),
    _wl("address_contains", "cc0", False, "cc"),
    _wl("address_contains", "from0", role="from"),
    _wl("display_name_contains", "person", False, "to"),
    _wl("body_words", "beta12", True),
    _wl("address_or_name_contains", "to1", role="to"),
    _wl("domain_is", "nowhere.example", True, "cc"),
    _wl("body_words", "delta99", True),
    _wl("address_contains", "x.example", True, "visible_recipient"),
    _wl("display_name_contains", "person 1", role="to"),
]

# Sixteen terms (the cap, ``_MAX_TEXT_TERMS``), each common in the
# vocabulary: every term compiles to its own FTS subquery.
_TERMS = " ".join(f"w{n}" for n in range(16))

MESSAGE_FILTERS: tuple[dict, ...] = (
    {"text": _TERMS},
    {"where": {"all": [{"leaf": "body_words", "value": _TERMS}]}},
    {"where": {"all": [{"leaf": "body_words", "value": _TERMS, "negate": True}]}},
    {"participant": "nobody"},
    {"participant": "from0.7"},
    {"sender": "from0.7@bench.example"},
    {"subject": "subject 4242"},
    {"text": "gamma4242"},
    {"text": "alpha7"},
    {"authority_class": "vendor"},
    {"date_from": "2015-01-01", "date_to": "2015-12-31"},
    # Explicit ``where`` expressions (``query_messages`` only): the node
    # cap of 16 spent on combined and negated address, display-name and
    # body-word leaves, which scan participant rows and the FTS index.
    {"where": {"all": _WHERE_LEAVES}},
    {"where": {"all": [{"any": _WHERE_LEAVES[:8]}, *_WHERE_LEAVES[8:15]]}},
)
# ``query_attachments`` takes the message leaves except subject, body
# text and authority, plus its own: a filename substring (casefolded,
# over sender-controlled text), MIME type, extraction status, carrying
# claimant and thread (``thread_id`` is filled in per corpus).
OCCURRENCE_FILTERS: tuple[dict, ...] = (
    {"participant": "nobody"},
    {"participant": "from0.7"},
    {"date_from": "2015-01-01", "date_to": "2015-12-31"},
    {"filename": "nomatch"},
    {"filename": "file"},
    {"content_type": "application/pdf"},
    {"extraction_status": "success"},
    {"extraction_status": "none"},
    {"thread_id": None},
)


def phase_filtered(db_path: str, kind: str, filters: dict, reverse: bool = False) -> dict:
    """One production page of the query under ``filters``
    (``Database.query_messages`` or ``query_attachments`` with
    ``limit=1``: its counts and first row) and the certificate over the
    same predicate by each scan method, each timed on its own.

    Whatever runs after another reads pages it cached, so ``reverse``
    flips the whole order (page, stream, collect) from one repeat to the
    next."""
    from src.lib.sqlite import Database

    _import_serializers()
    db = Database(db_path)

    def page_run() -> dict:
        t0 = time.perf_counter()
        if kind == "messages":
            rest = {k: v for k, v in filters.items() if k != "where"}
            page = db.query_messages(limit=1, where=_where_leaves(filters.get("where")), **rest)
        else:
            page = db.query_attachments(limit=1, **filters)
        return {"page_s": time.perf_counter() - t0, "page_total": page.total_matches}

    steps = ["page", "stream", "collect"]
    done = {}
    for step in reversed(steps) if reverse else steps:
        done[step] = (
            page_run() if step == "page" else phase_certificate(db_path, kind, step, filters)
        )
    page, cert, collected = done["page"], done["stream"], done["collect"]
    if collected["digest"] != cert["digest"]:
        raise ValueError("stream and collect certificates differ")
    return {
        "page_total": page["page_total"],
        "count": cert["count"],
        "scanned": cert["scanned"],
        "page_s": page["page_s"],
        "certificate_s": cert["total_s"],
        "certificate_collect_s": collected["total_s"],
        "rss_kib": _rss_kib(),
    }


def write_request_shape(out: Path, shape: str, n: int) -> int:
    """A synthetic upload of ``n`` digests in ``shape``, or for
    ``short_array`` as many two-character strings as fit in the byte
    size of ``n`` hex digests; returns its size in bytes."""
    if shape == "packed":
        digests = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(n))
        body = json.dumps({"hashes": base64.b64encode(digests).decode()}).encode()
    elif shape == "hex_array":
        body = json.dumps(
            {"hashes": [hashlib.sha256(str(i).encode()).hexdigest() for i in range(n)]},
            separators=(",", ":"),
        ).encode()
    else:
        budget = 67 * n + 12
        body = ('{"hashes":[' + ",".join(['"00"'] * ((budget - 13) // 5)) + "]}").encode()
    out.write_bytes(body)
    return len(body)


def phase_request_shape(path: str, shape: str) -> dict:
    """Parse one synthetic upload as the server would: the packed
    string decoded and split, an array parsed by ``json.loads`` (the
    elements exist before any can be checked). Timed with the file read
    excluded; peak RSS includes the body, as a server holds it."""
    body = Path(path).read_bytes()
    t0 = time.perf_counter()
    if shape == "packed":
        digests = unpack_hashes(body)
    else:
        digests = json.loads(body)["hashes"]
    parse_s = time.perf_counter() - t0
    return {"elements": len(digests), "parse_s": parse_s, "rss_kib": _rss_kib()}


def phase_writer(db_path: str, stop: str, commit_bytes: int, interval: float) -> dict:
    """A concurrent writer: until ``stop`` exists, commit transactions
    that flip eight messages' ``seen`` and append ``commit_bytes`` of
    ballast, as the indexer's steady-state batch of eight would."""
    rng = random.Random(7)
    commits = 0
    times: list[float] = []
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
            # Stamped after the commit: a commit is in a round's window
            # only once it is durable.
            times.append(time.time())
            commits += 1
            if interval:
                time.sleep(interval)
    return {"commits": commits, "commit_times": times}


# --- orchestration ------------------------------------------------------


def _child(phase: str, **kwargs) -> dict:
    """Run ``phase`` in a fresh interpreter and return its JSON. A failed
    child raises with the last 4,000 characters of its stderr, so the
    cause shows where the benchmark ran (its output is synthetic)."""
    out = subprocess.run(
        [sys.executable, __file__, "_phase", phase, json.dumps(kwargs)],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"benchmark phase {phase} exited with status {out.returncode}:\n{out.stderr[-4000:]}"
        )
    return json.loads(out.stdout)


def _median(runs: list[dict], key: str) -> float:
    return round(statistics.median(r[key] for r in runs), 4)


def write_request(
    db_path: str,
    kind: str,
    missing: int,
    extras: int,
    out: Path,
    missing_from: str = "spread",
    upload_total: int = 0,
    all_extras: bool = False,
) -> dict:
    """The client's upload: the SHA-256 of every matching identity but
    ``missing`` of them, plus ``extras`` hashes the server does not
    hold. ``missing_from`` ``spread`` leaves members out evenly;
    ``worst`` leaves out worst-case ones (``--records mixed``), so every
    record a round returns is worst-case. Also sizes the same upload as
    base64."""
    count_sql, scan, params = scan_sql(kind)
    with closing(_ro(db_path)) as conn:
        conn.execute("BEGIN")
        ids = [row[0].encode() for row in conn.execute(scan + " ORDER BY 1", params)]
        conn.rollback()
    if missing_from == "worst":
        with closing(_ro(db_path)) as conn:
            worst = {row[0].encode() for row in conn.execute("SELECT identity FROM bench_worst")}
        candidates = [n for n, b in enumerate(ids) if b in worst]
    else:
        candidates = list(range(len(ids)))
    # Every mode leaves out exactly ``missing`` members or refuses.
    if len(candidates) < missing and not all_extras:
        raise ValueError(
            f"--missing-from {missing_from}: {len(candidates)} {kind} can be left out, "
            f"--missing asks for {missing}; build more with --messages or lower --missing"
        )
    if missing_from == "worst":
        skip = set(candidates[:missing])
    else:
        step = max(1, len(ids) // missing) if missing else 0
        skip = set(range(0, len(ids), step)[:missing]) if missing else set()
    held = [identity_hash(b) for n, b in enumerate(ids) if n not in skip]
    if all_extras:
        # The accepted worst upload: no member, only hashes the server
        # does not hold; every member is missing.
        held = []
    if upload_total:
        # Fill the upload to ``upload_total`` digests (a set cap) with
        # extras, the largest request the cap accepts.
        extras = max(0, upload_total - len(held))
    held += [hashlib.sha256(f"extra:{n}".encode()).digest() for n in range(extras)]
    n_extras = extras
    hex_body = json.dumps({"hashes": [h.hex() for h in held]}, separators=(",", ":")).encode()
    b64 = json.dumps(
        {"hashes": [base64.b64encode(h).decode() for h in held]}, separators=(",", ":")
    ).encode()
    packed = json.dumps({"hashes": base64.b64encode(b"".join(held)).decode()}).encode()
    out.write_bytes(packed)
    return {
        "members": len(ids),
        "uploaded": len(held),
        "extras": n_extras,
        "request_bytes_hex": len(hex_body),
        "request_bytes_base64": len(b64),
        "request_bytes_packed": len(packed),
        "max_identity_bytes": max((len(b) for b in ids), default=0),
    }


def run_wal(
    db_path: str,
    kind: str,
    k: int,
    request: str,
    commit_bytes: int,
    interval: float,
    hold_s: float = 0.0,
    worst_first: bool = False,
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
    # Whatever happens below, the writer is stopped and reaped before
    # this returns or raises: left running, it commits until the disk
    # fills.
    try:
        warm_max = 0
        deadline = time.perf_counter() + 3
        while time.perf_counter() < deadline:
            warm_max = max(warm_max, size())
            time.sleep(0.01)
        result: dict = {}
        failure: list[BaseException] = []

        def reader() -> None:
            try:
                result.update(
                    _child(
                        "reconcile",
                        db_path=db_path,
                        kind=kind,
                        k=k,
                        request=request,
                        hold_s=hold_s,
                        worst_first=worst_first,
                    )
                )
            except BaseException as exc:  # re-raised below, after the writer stops
                failure.append(exc)

        t = threading.Thread(target=reader)
        samples: list[tuple[float, int]] = []
        t.start()
        while t.is_alive():
            samples.append((time.time(), size()))
            time.sleep(0.005)
        if failure:
            raise RuntimeError("reconcile round failed") from failure[0]
    finally:
        stop.touch()
        try:
            out, _ = writer.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            writer.kill()
            out, _ = writer.communicate()
        stop.unlink()
    writer_out = json.loads(out)
    # The WAL when the round's transaction began, its largest size
    # while the transaction was open, and the writer commits that
    # landed in that interval.
    start, end = result["started_at"], result["ended_at"]
    at_start = max((s for when, s in samples if when <= start), default=0)
    in_window = [s for when, s in samples if start <= when <= end]
    overlapping = sum(1 for t in writer_out["commit_times"] if start < t < end)
    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        busy, log_pages, ckpt_pages = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return {
        "commit_bytes": commit_bytes,
        "writer_interval_s": interval,
        "wal_steady_max_bytes": warm_max,
        "wal_at_transaction_start_bytes": at_start,
        "wal_max_during_transaction_bytes": max(in_window, default=0),
        "commits_during_transaction": overlapping,
        # WAL growth over the transaction per overlapping commit: the
        # bytes one such commit leaves retained.
        "wal_bytes_per_commit": (
            (max(in_window, default=0) - at_start) // overlapping if overlapping else None
        ),
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
        "build": build(
            db_path,
            args.messages,
            args.per_message,
            args.identity,
            args.records,
            args.references,
            args.extracted_chars,
            args.chunks,
            args.chunk_tokens,
        ),
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
        by_method = _alternating(
            args.repeat, lambda m, kind=kind: _child("certificate", db_path=db, kind=kind, method=m)
        )
        for method, runs in by_method.items():
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
                "first_runs": math.ceil(args.repeat / 2)
                if method == "stream"
                else args.repeat // 2,
            }
        if (
            report["certificate"][f"{kind}/stream"]["digest"]
            != report["certificate"][f"{kind}/collect"]["digest"]
        ):
            raise SystemExit(f"{kind}: SQL order and byte order digests differ")
    if args.filtered:
        report["filtered"] = {
            kind: [
                {"filters": f, **_filtered_runs(db, kind, f, args.repeat)}
                for f in (
                    MESSAGE_FILTERS
                    if kind == "messages"
                    else [_fill_thread(f, args.identity) for f in OCCURRENCE_FILTERS]
                )
            ]
            for kind in ("messages", "occurrences")
        }
    if args.request_shapes:
        report["request_shapes"] = run_request_shapes(work, args.request_shapes)
    report["reconcile"] = {}
    for kind in ("messages", "occurrences"):
        request = work / f"request-{kind}.json"
        req = write_request(
            db,
            kind,
            args.missing,
            args.extras,
            request,
            args.missing_from,
            args.upload_total[0 if kind == "messages" else 1] if args.upload_total else 0,
            args.all_extras,
        )
        report["reconcile"][kind] = {"request": req}
        report["reconcile"][kind].update(_reconcile_runs(db, kind, request, req, args))
    if args.wal:
        report["wal"] = [
            {
                "kind": kind,
                **run_wal(
                    db,
                    kind,
                    max(args.k),
                    str(work / f"request-{kind}.json"),
                    commit_bytes,
                    interval,
                    args.wal_hold,
                    args.all_extras,
                ),
            }
            for kind in ("messages", "occurrences")
            for commit_bytes in args.writer_commit_bytes
            for interval in args.writer_interval
        ]
    return report


def _fill_thread(filters: dict, identity: str) -> dict:
    """``filters`` with a ``thread_id`` placeholder set to the corpus's
    first thread (message 0's root)."""
    if "thread_id" in filters and filters["thread_id"] is None:
        return {"thread_id": message_id(0, identity)}
    return filters


def _filtered_runs(db: str, kind: str, filters: dict, repeat: int) -> dict:
    runs = [
        _child("filtered", db_path=db, kind=kind, filters=filters, reverse=n % 2 == 1)
        for n in range(repeat)
    ]
    if any(r["scanned"] != r["count"] for r in runs):
        raise SystemExit(f"{kind} {filters}: scanned and counted sets differ")
    return {
        "page_total": runs[0]["page_total"],
        "count": runs[0]["count"],
        "page_s": _median(runs, "page_s"),
        "certificate_s": _median(runs, "certificate_s"),
        "certificate_collect_s": _median(runs, "certificate_collect_s"),
        "rss_kib": max(r["rss_kib"] for r in runs),
    }


def run_request_shapes(work: Path, n: int) -> dict:
    """Parse cost and peak RSS of each upload shape at ``n`` digests."""
    out = {}
    for shape in ("packed", "hex_array", "short_array"):
        path = work / f"shape-{shape}.json"
        size = write_request_shape(path, shape, n)
        result = _child("request_shape", path=str(path), shape=shape)
        out[shape] = {"bytes": size, **result}
        path.unlink()
    return out


def _alternating(repeat: int, call) -> dict[str, list[dict]]:
    """``call(method)`` ``repeat`` times for each scan method, the method
    that runs first alternating each repeat. Each run is its own
    process, but the page cache is the host's: a method that always ran
    second would inherit the pages the first read."""
    runs: dict[str, list[dict]] = {"stream": [], "collect": []}
    for n in range(repeat):
        for method in ("stream", "collect") if n % 2 == 0 else ("collect", "stream"):
            runs[method].append(call(method))
    return runs


def _balanced_k(repeat: int, ks: list[int], call) -> dict[int, dict[str, list[dict]]]:
    """``call(k, method)`` ``repeat`` times for each K and scan method.
    The page cache is the host's and persists across runs, so the order
    alternates each repeat: K ascending, then descending, and within a K
    the method that runs first swaps too."""
    runs: dict[int, dict[str, list[dict]]] = {k: {"stream": [], "collect": []} for k in ks}
    for n in range(repeat):
        for k in ks if n % 2 == 0 else list(reversed(ks)):
            for method in ("stream", "collect") if n % 2 == 0 else ("collect", "stream"):
                runs[k][method].append(call(k, method))
    return runs


def _reconcile_runs(db: str, kind: str, request: Path, req: dict, args: argparse.Namespace) -> dict:
    out: dict = {"stream": {}, "collect": {}}
    by_k = _balanced_k(
        args.repeat,
        args.k,
        lambda k, m: _child(
            "reconcile",
            db_path=db,
            kind=kind,
            k=k,
            request=str(request),
            method=m,
            worst_first=args.all_extras,
        ),
    )
    for k in args.k:
        for method, runs in by_k[k].items():
            r0 = runs[0]
            out[method][str(k)] = {
                "returned": r0["returned"],
                "participant_rows": r0["participant_rows"],
                "references": r0["references"],
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
    return out


def main(argv: list[str] | None = None) -> dict:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_phase"]:
        phases = {
            "idle": phase_idle,
            "certificate": phase_certificate,
            "reconcile": phase_reconcile,
            "filtered": phase_filtered,
            "request_shape": phase_request_shape,
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
    p.add_argument(
        "--records", choices=("typical", "worst", "mixed", "cardinality"), default="typical"
    )
    p.add_argument(
        "--upload-total",
        type=lambda s: [int(x) for x in s.split(",")],
        default=None,
        help="messages,occurrences: fill each upload to this many digests with extras",
    )
    p.add_argument(
        "--missing-from",
        choices=("spread", "worst"),
        default="spread",
        help="which members the upload leaves out (worst: the mixed corpus's worst records)",
    )
    p.add_argument(
        "--references",
        type=int,
        default=100_000,
        help="References entries per message (cardinality)",
    )
    p.add_argument(
        "--k", type=lambda s: [int(x) for x in s.split(",")], default=[100, 500, 1000, 2000, 5000]
    )
    p.add_argument("--missing", type=int, default=5000, help="members the client upload leaves out")
    p.add_argument(
        "--extras", type=int, default=100, help="hashes the client holds that the server does not"
    )
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--wal", action="store_true", help="also measure WAL growth under a writer")
    p.add_argument(
        "--writer-commit-bytes",
        type=lambda s: [int(x) for x in s.split(",")],
        default=[128 * 1024],
    )
    p.add_argument(
        "--chunks",
        type=int,
        default=1,
        help="body chunks per message (the parser splits large mail)",
    )
    p.add_argument("--chunk-tokens", type=int, default=BODY_TOKENS, help="words per body chunk")
    p.add_argument(
        "--all-extras",
        action="store_true",
        help="the upload holds no member: --upload-total digests, all extras "
        "(the accepted worst case), and a round returns worst-case records first",
    )
    p.add_argument("--filtered", action="store_true", help="also time filtered predicates")
    p.add_argument(
        "--extracted-chars",
        type=int,
        default=0,
        help="four-byte characters of extracted text per extraction row (0: none)",
    )
    p.add_argument(
        "--request-shapes",
        type=int,
        default=0,
        help="also time parsing each upload shape at this many digests (0: skip)",
    )
    p.add_argument(
        "--writer-interval", type=lambda s: [float(x) for x in s.split(",")], default=[0.0, 0.1]
    )
    p.add_argument(
        "--wal-hold",
        type=float,
        default=0.0,
        help="seconds the WAL round keeps its transaction open after its work (smoke test only)",
    )
    report = run(p.parse_args(argv))
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    main()
