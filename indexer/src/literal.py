"""Separate bounded literal indexes over preserved sources; retrieval vectors stay unchanged."""

import codecs
import json
import logging
import sqlite3
import time
import unicodedata
from dataclasses import dataclass

from .rate_limited_log import LineBudget

log = logging.getLogger("indexer.literal")
_warnings = LineBudget(limit=20, window_secs=60.0, buckets=("index",))
VERSION = 1
FRAGMENT = 8192
OVERLAP = 999
MAX_WINDOWS = 10_000
MAX_BYTES = 128 * 1024 * 1024
MAX_METADATA = 64 * 1024 * 1024
MAX_MEMBERSHIP = 512 * 1024 * 1024
SCOPES = {"body": "a", "quote": "q", "signature": "s", "forwarded": "f"}
FTS_TABLES = ("literal_long_fts", "literal_short_fts")

SCHEMA_SQL = """
CREATE TABLE literal_state (
 claimant_id TEXT PRIMARY KEY REFERENCES messages(claimant_id) ON DELETE CASCADE,
 version INTEGER NOT NULL, unicode_version TEXT NOT NULL, generation TEXT,
 body_complete INTEGER, classified_complete INTEGER, attachments_complete INTEGER,
 body_windows INTEGER NOT NULL, body_bytes INTEGER NOT NULL,
 body_metadata INTEGER NOT NULL, body_membership INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE literal_attachment_state (
 occurrence_id TEXT PRIMARY KEY REFERENCES attachments(attachment_occurrence_id) ON DELETE CASCADE,
 generation TEXT, complete INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE literal_attachment_spans (
 occurrence_id TEXT NOT NULL REFERENCES attachments(attachment_occurrence_id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL, data BLOB NOT NULL CHECK(typeof(data)='blob' AND length(data)<=32768),
 PRIMARY KEY(occurrence_id,ordinal)
) WITHOUT ROWID;
CREATE TABLE literal_windows (
 id INTEGER PRIMARY KEY,
 claimant_id TEXT NOT NULL REFERENCES messages(claimant_id) ON DELETE CASCADE,
 part INTEGER, occurrence_id TEXT REFERENCES attachments(attachment_occurrence_id) ON DELETE CASCADE,
 generation TEXT NOT NULL, ordinal INTEGER NOT NULL,
 byte_count INTEGER NOT NULL CHECK(byte_count BETWEEN 0 AND 36764),
 read_bytes INTEGER NOT NULL CHECK(read_bytes BETWEEN 0 AND 36764),
 ranges TEXT NOT NULL CHECK(length(CAST(ranges AS BLOB))<=524288),
 CHECK((part IS NULL) <> (occurrence_id IS NULL)),
 FOREIGN KEY(claimant_id,part) REFERENCES message_body_parts(claimant_id,ordinal) ON DELETE CASCADE
);
CREATE INDEX idx_literal_claimant ON literal_windows(claimant_id);
CREATE INDEX idx_literal_occurrence ON literal_windows(occurrence_id);
CREATE VIRTUAL TABLE literal_long_fts USING fts5(tokens,tokenize='ascii',detail=none,content='',contentless_delete=1);
CREATE VIRTUAL TABLE literal_short_fts USING fts5(tokens,tokenize='ascii',detail=none,content='',contentless_delete=1);
CREATE TABLE literal_scrub (id INTEGER PRIMARY KEY CHECK(id=1));
CREATE TRIGGER literal_window_delete AFTER DELETE ON literal_windows BEGIN
 DELETE FROM literal_long_fts WHERE rowid=old.id;
 DELETE FROM literal_short_fts WHERE rowid=old.id;
 INSERT OR IGNORE INTO literal_scrub VALUES(1);
END;
"""


def gram_token(text: str) -> str:
    return "g" + "".join(f"{ord(c):06x}" for c in text)


@dataclass
class Budget:
    windows: int = 0
    source_bytes: int = 0
    metadata: int = 0
    membership: int = 0
    capped: bool = False

    def admit(self, size: int, metadata: int, membership: int, deadline: float | None) -> bool:
        if (
            self.windows + 1 > MAX_WINDOWS
            or self.source_bytes + size > MAX_BYTES
            or self.metadata + metadata > MAX_METADATA
            or self.membership + membership > MAX_MEMBERSHIP
            or (deadline is not None and time.monotonic() >= deadline)
        ):
            self.capped = True
            return False
        self.windows += 1
        self.source_bytes += size
        self.metadata += metadata
        self.membership += membership
        return True


def _tokens(text: str, ranges: list[tuple[int, int, str]], attachment: bool) -> tuple[str, str]:
    folded = text.casefold()
    long = {gram_token(folded[i : i + 3]) for i in range(len(folded) - 2)}
    short: set[str] = set()
    kinds = [""] * len(text)
    for start, end, kind in ranges:
        kinds[start:end] = [SCOPES[kind]] * (end - start)
    pieces = [c.casefold() for c in text]
    scope = "t" if attachment else "b"
    for i, piece in enumerate(pieces):
        for count in (1, 2):
            if count == 2:
                if i + 1 == len(pieces):
                    continue
                piece = pieces[i] + pieces[i + 1]
            if 1 <= len(piece) <= 2:
                token = gram_token(piece)
                short.add(scope + token)
                if not attachment and kinds[i] and (count == 1 or kinds[i] == kinds[i + 1]):
                    short.add(kinds[i] + token)
    return " ".join(sorted(long)), " ".join(sorted(short))


def _write_window(
    cur, claimant, part, occurrence, generation, ordinal, text, ranges, budget, deadline, read_bytes
):
    data_bytes = len(text.encode("utf-8"))
    encoded_ranges = json.dumps(ranges, separators=(",", ":"))
    # Charge every generated gram before deduplication, not just retained tokens.
    folded_size = len(text.casefold())
    membership = max(folded_size - 2, 0) * 20 + len(text) * 48
    metadata = len(encoded_ranges.encode()) + 2 * len(claimant.encode()) + 256
    if not budget.admit(data_bytes, metadata, membership, deadline):
        return False
    long, short = _tokens(text, ranges, occurrence is not None)
    cur.execute(
        "INSERT INTO literal_windows(claimant_id,part,occurrence_id,generation,ordinal,byte_count,read_bytes,ranges) VALUES(?,?,?,?,?,?,?,?)",
        (claimant, part, occurrence, generation, ordinal, data_bytes, read_bytes, encoded_ranges),
    )
    rowid = cur.lastrowid
    cur.execute("INSERT INTO literal_long_fts(rowid,tokens) VALUES(?,?)", (rowid, long))
    cur.execute("INSERT INTO literal_short_fts(rowid,tokens) VALUES(?,?)", (rowid, short))
    return True


def _warn(budget):
    if budget.capped:
        _warnings.log(
            log,
            "index",
            "Literal index incomplete: windows=%d source_bytes=%d metadata_bytes=%d membership_bytes=%d",
            budget.windows,
            budget.source_bytes,
            budget.metadata,
            budget.membership,
        )


def log_summary():
    suppressed = _warnings.drain("index")
    if suppressed:
        log.warning("Literal index incomplete: suppressed_lines=%d", suppressed)


def rebuild_body(cur: sqlite3.Cursor, claimant: str, deadline: float | None = None) -> None:
    """After full_body.store, in its caller's transaction; no raw body copy is stored."""
    cur.execute("DELETE FROM literal_windows WHERE claimant_id=?", (claimant,))
    cur.execute(
        "DELETE FROM literal_attachment_spans WHERE occurrence_id IN (SELECT attachment_occurrence_id FROM attachments WHERE claimant_id=?)",
        (claimant,),
    )
    complete, generation, version = cur.execute(
        "SELECT full_body_complete,full_body_generation,full_body_version FROM messages WHERE claimant_id=?",
        (claimant,),
    ).fetchone()
    budget = Budget()
    classified = complete == 1
    if generation is not None and version == 1:
        parts = cur.execute(
            "SELECT ordinal,source_chars,classified_until FROM message_body_parts WHERE claimant_id=? AND source_chars IS NOT NULL ORDER BY ordinal",
            (claimant,),
        ).fetchall()
        for part, chars, until in parts:
            classified &= until == chars
            ranges = cur.execute(
                "SELECT start,end,kind FROM message_body_ranges WHERE claimant_id=? AND part=? ORDER BY start",
                (claimant, part),
            ).fetchall()
            fragments = iter(
                cur.connection.execute(
                    "SELECT ordinal,data FROM message_body_spans WHERE claimant_id=? AND part=? ORDER BY ordinal",
                    (claimant, part),
                )
            )
            current = next(fragments, None)
            next_fragment = next(fragments, None)
            first_range = 0
            while current is not None:
                ordinal, data = current
                text = data.decode("utf-8")
                if next_fragment is not None:
                    text += next_fragment[1].decode("utf-8")[:OVERLAP]
                start, end = ordinal * FRAGMENT, ordinal * FRAGMENT + len(text)
                while first_range < len(ranges) and ranges[first_range][1] <= start:
                    first_range += 1
                scoped = []
                for i in range(first_range, len(ranges)):
                    a, b, kind = ranges[i]
                    if a >= end:
                        break
                    scoped.append((max(a, start) - start, min(b, end) - start, kind))
                if not _write_window(
                    cur,
                    claimant,
                    part,
                    None,
                    generation,
                    ordinal,
                    text,
                    scoped,
                    budget,
                    deadline,
                    len(data)
                    + (min(3996, len(next_fragment[1])) if next_fragment is not None else 0),
                ):
                    break
                current, next_fragment = next_fragment, next(fragments, None)
            if budget.capped:
                break
    cur.execute(
        "INSERT INTO literal_state VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(claimant_id) DO UPDATE SET version=excluded.version,unicode_version=excluded.unicode_version,generation=excluded.generation,body_complete=excluded.body_complete,classified_complete=excluded.classified_complete,attachments_complete=NULL,body_windows=excluded.body_windows,body_bytes=excluded.body_bytes,body_metadata=excluded.body_metadata,body_membership=excluded.body_membership",
        (
            claimant,
            VERSION,
            unicodedata.unidata_version,
            generation,
            int(complete == 1 and not budget.capped) if generation is not None else None,
            int(classified and not budget.capped) if generation is not None else None,
            None,
            budget.windows,
            budget.source_bytes,
            budget.metadata,
            budget.membership,
        ),
    )
    _warn(budget)


def rebuild_attachments(cur: sqlite3.Cursor, claimant: str, enabled: bool) -> None:
    """Index the current cache results after the message's Phase 2 writes."""
    cur.execute(
        "DELETE FROM literal_windows WHERE claimant_id=? AND occurrence_id IS NOT NULL", (claimant,)
    )
    cur.execute(
        "DELETE FROM literal_attachment_spans WHERE occurrence_id IN (SELECT attachment_occurrence_id FROM attachments WHERE claimant_id=?)",
        (claimant,),
    )
    cur.execute(
        "DELETE FROM literal_attachment_state WHERE occurrence_id IN (SELECT attachment_occurrence_id FROM attachments WHERE claimant_id=?)",
        (claimant,),
    )
    row = cur.execute(
        "SELECT body_windows,body_bytes,body_metadata,body_membership FROM literal_state WHERE claimant_id=?",
        (claimant,),
    ).fetchone()
    if row is None:
        return
    budget = Budget(*row)
    complete = enabled
    if enabled:
        occurrences = cur.execute(
            "SELECT a.attachment_occurrence_id,e.extracted_at,e.rowid,a.text_complete,a.extraction_deferred_at FROM attachments a LEFT JOIN attachment_extractions e ON e.attachment_id=a.attachment_id AND e.extractor_module=a.extractor_module WHERE a.claimant_id=? ORDER BY a.attachment_occurrence_id LIMIT 10001",
            (claimant,),
        ).fetchall()
        if len(occurrences) > MAX_WINDOWS:
            budget.capped = True
            occurrences = []
        for occurrence, generation, extraction_rowid, full, deferred in occurrences:
            complete &= full == 1 and deferred is None
            cur.execute(
                "INSERT INTO literal_attachment_state VALUES(?,?,?)",
                (occurrence, generation, int(full == 1 and deferred is None)),
            )
            if generation is None or extraction_rowid is None or deferred is not None:
                continue
            has_text = cur.execute(
                "SELECT typeof(extracted_text)='text' FROM attachment_extractions WHERE rowid=?",
                (extraction_rowid,),
            ).fetchone()[0]
            if not has_text:
                continue
            with cur.connection.blobopen(
                "attachment_extractions", "extracted_text", extraction_rowid, readonly=True
            ) as blob:
                if len(blob) > MAX_BYTES - budget.source_bytes:
                    budget.capped = True
                    break
                fragments = iter(_attachment_fragments(blob))
                current, following = next(fragments, None), next(fragments, None)
                ordinal = 0
                while current is not None:
                    window = current + (following[:OVERLAP] if following is not None else "")
                    if not _write_window(
                        cur,
                        claimant,
                        None,
                        occurrence,
                        generation,
                        ordinal,
                        window,
                        [],
                        budget,
                        None,
                        len(current.encode())
                        + (min(3996, len(following.encode())) if following is not None else 0),
                    ):
                        break
                    cur.execute(
                        "INSERT OR IGNORE INTO literal_attachment_spans VALUES(?,?,?)",
                        (occurrence, ordinal, current.encode()),
                    )
                    if following is not None:
                        cur.execute(
                            "INSERT OR IGNORE INTO literal_attachment_spans VALUES(?,?,?)",
                            (occurrence, ordinal + 1, following.encode()),
                        )
                    ordinal += 1
                    current, following = following, next(fragments, None)
                if budget.capped:
                    break
    cur.execute(
        "UPDATE literal_state SET attachments_complete=? WHERE claimant_id=?",
        (int(complete and not budget.capped), claimant),
    )
    if budget.capped:
        cur.execute(
            "UPDATE literal_attachment_state SET complete=0 WHERE occurrence_id IN (SELECT attachment_occurrence_id FROM attachments WHERE claimant_id=?)",
            (claimant,),
        )
    _warn(budget)


def _attachment_fragments(blob):
    """Incremental BLOB reads avoid rescanning TEXT prefixes or copying a document."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    left = len(blob)
    while left:
        raw = blob.read(min(left, 32768))
        left -= len(raw)
        pending += decoder.decode(raw, final=left == 0)
        while len(pending) >= FRAGMENT:
            yield pending[:FRAGMENT]
            pending = pending[FRAGMENT:]
    if pending:
        yield pending


def create_schema(cur):
    statement = ""
    for line in SCHEMA_SQL.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            cur.execute(statement)
            statement = ""
