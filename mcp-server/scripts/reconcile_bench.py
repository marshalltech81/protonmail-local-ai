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
``attachments``, ``attachments_fts``, ``attachment_extractions`` and
``pending_deletions`` with all their indexes, and hold the rows the
indexer writes for the messages ``render_eml`` describes
(indexer/tests/test_reconcile_bench_fidelity.py indexes those files
with the indexer and compares). Other tables (threads, vectors, the
queue) are left out because no measured statement reads them.
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
import time
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.header import Header
from email.utils import format_datetime, formataddr
from pathlib import Path
from urllib.parse import quote

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
    char_start    INTEGER NOT NULL,
    char_end      INTEGER NOT NULL,
    token_est     INTEGER NOT NULL,
    chunked_at    TEXT NOT NULL,
    fts_rowid     INTEGER,
    attachment_id TEXT,
    kind          TEXT NOT NULL
);
CREATE INDEX idx_message_chunks_claimant ON message_chunks(claimant_id);
CREATE INDEX idx_message_chunks_thread ON message_chunks(thread_id);
CREATE INDEX idx_message_chunks_attachment ON message_chunks(attachment_id);
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
CREATE INDEX idx_entities_organization ON entities(organization_id);
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
    ocr_pages_cap      INTEGER CHECK (ocr_pages_cap >= 0),
    digital_pages_cap  INTEGER CHECK (digital_pages_cap >= 0),
    extracted_chars_cap INTEGER CHECK (extracted_chars_cap >= 0),
    PRIMARY KEY (attachment_id, extractor_module)
);
CREATE TABLE pending_deletions (
    filepath    TEXT PRIMARY KEY,
    claimant_id TEXT NOT NULL,
    thread_id   TEXT NOT NULL,
    marked_at   TEXT NOT NULL
);
CREATE INDEX idx_pending_deletions_thread ON pending_deletions(thread_id);
CREATE VIRTUAL TABLE attachments_fts USING fts5(
    filename,
    content_type,
    content='',
    contentless_delete=1,
    tokenize='porter unicode61'
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
    """Synthetic Message-ID ``i`` in one of four widths: ``typical``
    (about 50 ASCII characters), ``ascii998`` (998 ASCII characters),
    ``ascii998common`` (one 998-character ID every file claims) or
    ``utf8x4`` (998 four-byte characters, a stress shape the parser
    cannot produce, #1424)."""
    if identity == "typical":
        return f"{hashlib.sha256(str(i).encode()).hexdigest()[:32]}.{i:010d}{_DOMAIN}"
    if identity == "ascii998":
        head = f"{i:010d}"
        return head + "x" * (MESSAGE_ID_MAX_CHARS - len(head) - len(_DOMAIN)) + _DOMAIN
    if identity == "utf8x4":
        # Three base-1024 digits as four-byte characters keep IDs unique.
        digits = [chr(0x10000 + (i >> s & 0x3FF)) for s in (20, 10, 0)]
        return "".join(digits) + "\U0001f600" * (MESSAGE_ID_MAX_CHARS - 3)
    if identity == "ascii998common":
        # One maximum-length Message-ID claimed by every file: claimant
        # IDs share their first 998 bytes and differ in the hash suffix.
        return "c" * (MESSAGE_ID_MAX_CHARS - len(_DOMAIN)) + _DOMAIN
    raise ValueError(f"unknown identity width {identity!r}")


def claimant_of(mid: str, raw: bytes) -> str:
    """The claimant ID of a file holding ``raw`` that claims ``mid``:
    the Message-ID plus ``#`` and the first 16 hex digits of the file's
    SHA-256 (indexer/src/parser.py ``claimant_id``)."""
    return f"{mid}#{hashlib.sha256(raw).hexdigest()[:16]}"


def sent_at(i: int) -> datetime:
    """Message ``i``'s Date (and, outside Sent, its Received) time.
    Threads of four share their root's time plus one hour per reply, so
    a reply is never older than its parent: the indexer's first index
    runs oldest first and joins a reply only to an indexed parent."""
    r = i - i % 4
    root = datetime(2010 + r % 15, 1 + r % 12, 1 + r % 28, r % 24, r % 60, tzinfo=UTC)
    return root + timedelta(hours=i % 4)


def _folder(i: int) -> str:
    # 5 % Trash (left out by the default predicate), 10 % Sent,
    # 25 % Archive, 60 % INBOX.
    r = i % 20
    return "Trash" if r == 0 else "Sent" if r < 3 else "Archive" if r < 8 else "INBOX"


def disk_folder(folder: str) -> str:
    """The directory mbsync writes ``folder`` to under ``SubFolders
    Legacy``: each component after the first gets a leading dot (the
    parser's ``_derive_folder`` removes them)."""
    return "/.".join(folder.split("/"))


# The parser's per-message address cap (indexer/src/parser.py
# ``MAX_MESSAGE_ADDRESSES``): at most this many participant rows.
MAX_MESSAGE_ADDRESSES = 10_000
# indexer/src/parser.py ``MAX_EXTRA_PARTICIPANT_NAMES``.
MAX_EXTRA_PARTICIPANT_NAMES = 1_000
# indexer/src/database.py ``MAX_ENTITY_PARTICIPANTS_PER_MESSAGE``.
MAX_ENTITY_PARTICIPANTS_PER_MESSAGE = 200
# A four-byte character: the most UTF-8 bytes a character-clipped field
# can carry per character.
_WIDE = "\U0001f600"
# Linux ``HOST_NAME_MAX``: the longest host name in an mbsync file name.
_HOST_MAX = 64


def _mixed_worst(i: int) -> bool:
    """Whether message ``i`` carries worst-case records under
    ``--records mixed``: one in fifty, never a thread root, so a large
    corpus stays buildable while every record a round returns can be
    worst-case."""
    return i % 50 == 1


def _wide_digits(i: int) -> str:
    """``i`` as three four-byte characters, so wide fields stay unique."""
    return "".join(chr(0x10000 + (i >> s & 0x3FF)) for s in (20, 10, 0))


def _shape(i: int, identity: str, records: str, references: int) -> dict:
    """The header fields of message ``i`` for one ``records`` shape
    (``build``). Every value is one the parser stores as given (the
    fidelity test parses ``render_eml`` and compares)."""
    if records == "mixed":
        records = "worst" if _mixed_worst(i) else "typical"
    parent = message_id(i - 1, identity) if i % 4 else None
    if records == "worst":
        # Fields the parser stores and the tools return, past their
        # clips: four-byte characters only where a header's RFC 2047
        # encoded-word or a decoded name can carry them (subject,
        # display names, filename); addresses, Message-IDs and the MIME
        # type are ASCII, and an address holds an ``@``
        # (``canonical_addr`` drops one without). In-Reply-To names a
        # message outside the corpus and References end with the parent,
        # which the threader then finds, so the message stays in its
        # thread. The subject is unique, so no subject fallback merges
        # two threads.
        wide = _WIDE * 501
        absent = "i" * MESSAGE_ID_MAX_CHARS
        return {
            "subject": _wide_digits(i) + _WIDE * 1997,
            "in_reply_to": absent,
            "references": [absent] * 10 + [parent or absent],
            "people": [
                (role, f"{p:02d}{role}" + "a" * (590 - len(role)) + "@x.example", wide)
                for role in ("from", "to", "cc")
                for p in range(11)
            ],
            "host": "h" * _HOST_MAX,
            # The extension routes the payload to the PDF extractor
            # whatever its MIME type, so the occurrence has text.
            "filename": _WIDE * 497 + ".pdf",
            "content_type": "application/" + "x" * 600,
            # Nested folders of 63 four-byte characters (252 bytes,
            # under a file system's 255-byte name limit; isync 1.5
            # writes decoded UTF-8 names) to about 3.5 KB of path.
            "folder": "/".join([_WIDE * 63] * 14),
        }
    base = {
        "subject": f"Synthetic subject {i:010d}",
        "in_reply_to": parent,
        "host": "bench",
        "filename": "file.pdf",
        "content_type": "application/pdf",
    }
    if records in ("cardinality", "cardinality_names"):
        # The parser's address budget is shared by every kept occurrence,
        # the repeats that carry an address's further names included. The
        # first name of each unique address spends no extra-name budget,
        # so ``cardinality`` keeps 10,000 uniquely named addresses and
        # ``cardinality_names`` trades 1,000 of them for repeats that
        # carry alternate names.
        extra = MAX_EXTRA_PARTICIPANT_NAMES if records == "cardinality_names" else 0
        unique = MAX_MESSAGE_ADDRESSES - extra
        counts = (("from", 1), ("to", 4_999), ("cc", unique - 5_000))
        return {
            **base,
            "references": [f"r{n}@x.example" for n in range(references)]
            + ([parent] if parent else []),
            "people": [
                (role, f"{role}{p}@x.example", f"Person {p}")
                for role, n in counts
                for p in range(n)
            ],
            # Further names of one address, after its first: name rows only.
            "extra_names": [("to", "to0@x.example", f"Alias {n}") for n in range(extra)],
        }
    return {
        **base,
        "references": [parent] if parent else [],
        "people": [
            ("from", f"from0.{i % 500:03d}{_DOMAIN}", "Person 0"),
            ("to", f"to0.{i % 500:03d}{_DOMAIN}", "Person 0"),
            ("to", f"to1.{i % 500:03d}{_DOMAIN}", "Person 1"),
            ("cc", f"cc0.{i % 500:03d}{_DOMAIN}", None),
        ],
    }


def file_name(i: int, host: str) -> str:
    """mbsync's Maildir file name for message ``i`` (``<time>.<pid>_<n>.
    <host>,U=<uid>:2,<flags>``): seen (``S``) on every other message."""
    flags = "S" if i % 2 else ""
    return f"{1_700_000_000 + i}.{1000 + i % 9000}_{i + 1}.{host},U={i + 1}:2,{flags}"


def maildir_path(i: int, identity: str, records: str, root: str = "/maildir") -> str:
    """Message ``i``'s file under the Maildir ``root``."""
    shape = _shape(i, identity, records, 0)
    folder = shape.get("folder") or _folder(i)
    return f"{root}/{disk_folder(folder)}/cur/{file_name(i, shape['host'])}"


# Words per synthetic body chunk: at least the 20 tokens a chunk of real
# mail carries (AGENTS.md: sparse chunks hid FTS5 segment growth, #1262).
BODY_TOKENS = 40
# Distinct filler words (``w1000`` to ``w5999``, five tokens each), so
# the FTS index carries a realistic vocabulary.
_VOCABULARY = 5000
# The indexer's default chunk budgets (indexer/src/main.py
# ``INDEXER_CHUNK_TARGET_TOKENS`` / ``INDEXER_CHUNK_MAX_TOKENS``): a
# chunk closes once it reaches the target; none exceeds the maximum.
CHUNK_TARGET_TOKENS = 1000
CHUNK_MAX_TOKENS = 1500
# Words of extracted text per attachment payload.
ATTACHMENT_WORDS = 40


def _filler(seed: int, n: int, base: int) -> list[str]:
    return [f"w{1000 + (seed * 7919 + (base + j) * 104729) % _VOCABULARY}" for j in range(n)]


def _paragraph(i: int, chunk: int, words: int) -> str:
    """Body chunk ``chunk`` of message ``i``: for chunk 0 ``alpha<i % 50>``
    (in every fiftieth body) and ``gamma<i>`` (in this body only), then
    filler words, ``words`` words in all."""
    lead = [f"alpha{i % 50:02d}", f"gamma{i:010d}"] if chunk == 0 else []
    return " ".join([*lead, *_filler(i, words - len(lead), chunk * words)])


def attachment_text(i: int, k: int) -> str:
    """The text of message ``i``'s ``k``-th payload, as its extractor
    returns it."""
    return " ".join([f"doc{i:010d}", f"part{k:04d}", *_filler(i + k, ATTACHMENT_WORDS - 2, 7)])


def token_estimate(text: str) -> int:
    """The indexer's token estimate (its bundled tokenizer) of text made
    of this benchmark's words: one token for the letters of a word and
    one per digit (the fidelity test checks it against the tokenizer)."""
    return sum(1 + sum(c.isdigit() for c in word) for word in text.split())


def chunk_layout(paragraphs: list[str]) -> list[tuple[str, int, int]]:
    """``(text, char_start, char_end)`` of each chunk the indexer cuts
    from a body of ``paragraphs`` joined by blank lines: one paragraph a
    chunk while each holds from ``CHUNK_TARGET_TOKENS`` to
    ``CHUNK_MAX_TOKENS`` tokens, or the whole body when it holds at most
    ``CHUNK_MAX_TOKENS`` (``bench_args`` refuses anything else)."""
    if len(paragraphs) == 1:
        return [(paragraphs[0], 0, len(paragraphs[0]))]
    out, offset = [], 0
    for p in paragraphs:
        out.append((p, offset, offset + len(p)))
        offset += len(p) + 2
    return out


def pdf_payload(text: str) -> bytes:
    """A one-page PDF whose text layer is ``text`` (ASCII words)."""
    stream = f"BT /F1 10 Tf 20 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def _mailbox(address: str, name: str | None) -> str:
    """One address with its display name; a non-ASCII name as RFC 2047
    encoded-words of at most 75 characters, folded, as a mail client
    writes it (the parser leaves a longer encoded-word undecoded)."""
    if name and not name.isascii():
        return f"{Header(name, 'utf-8').encode()} <{address}>"
    return formataddr((name or "", address))


def _address_header(people: list[tuple[str, str, str | None]], role: str) -> str:
    return ",\n ".join(_mailbox(address, name) for r, address, name in people if r == role)


def render_eml(
    i: int,
    identity: str,
    records: str,
    references: int = 0,
    chunks: int = 1,
    words: int = BODY_TOKENS,
    per_message: int = 0,
) -> bytes:
    """The Maildir file of message ``i``: the bytes the benchmark's row
    for ``i`` stands for. ``build`` hashes them for the claimant ID and
    ``content_hash`` and stores their length; the fidelity test parses
    them with the indexer and compares every stored value."""
    shape = _shape(i, identity, records, references)
    when = format_datetime(sent_at(i))
    folder = shape.get("folder") or _folder(i)
    people = [*shape["people"], *shape.get("extra_names", [])]
    head = []
    if folder != "Sent":
        # Delivery time: sent mail carries no Received header.
        head.append(f"Received: from relay.bench.example by mx.bench.example; {when}")
    head += [f"Date: {when}", f"Message-ID: <{message_id(i, identity)}>"]
    if shape["in_reply_to"]:
        head.append(f"In-Reply-To: <{shape['in_reply_to']}>")
    if shape["references"]:
        head.append("References: " + "\n ".join(f"<{r}>" for r in shape["references"]))
    subject = shape["subject"]
    if not subject.isascii():
        subject = Header(subject, "utf-8").encode()
    head.append(f"Subject: {subject}")
    for role, name in (("from", "From"), ("to", "To"), ("cc", "Cc")):
        value = _address_header(people, role)
        if value:
            head.append(f"{name}: {value}")
    head.append("MIME-Version: 1.0")
    body = "\n\n".join(_paragraph(i, c, words) for c in range(chunks)) + "\n"
    text_part = "Content-Type: text/plain; charset=us-ascii\nContent-Transfer-Encoding: 7bit\n"
    if not per_message:
        return ("\n".join(head) + "\n" + text_part + "\n" + body).encode()
    boundary = f"bench-{i:010d}"
    parts = [text_part + "\n" + body]
    for k in range(per_message):
        payload = base64.encodebytes(pdf_payload(attachment_text(i, k))).decode()
        name = quote(shape["filename"], safe="")
        parts.append(
            f"Content-Type: {shape['content_type']}\n"
            f"Content-Disposition: attachment; filename*=utf-8''{name}\n"
            "Content-Transfer-Encoding: base64\n\n" + payload
        )
    head.append(f'Content-Type: multipart/mixed; boundary="{boundary}"')
    mime = "".join(f"--{boundary}\n{p}\n" for p in parts) + f"--{boundary}--\n"
    return ("\n".join(head) + "\n\n" + mime).encode()


def authority(address: str) -> tuple[str, str | None]:
    """The class and rule the indexer's rules give ``address`` under the
    benchmark's rules file: about one From address in five listed as
    ``vendor`` (``[vendor] addresses = [...]``), every other address and
    every domain unclassified."""
    local = address.split("@")[0].lstrip("0123456789")
    if local.startswith("from") and hashlib.sha256(address.encode()).digest()[0] % 5 == 0:
        return "vendor", f"address:{address}"
    return "unclassified", None


# The extractor stamp, ``extractor_module`` and per-result fields the
# indexer stores for a payload its PDF extractor reads in full.
PDF_MODULE = "pdf"
PDF_EXTRACTOR = "pdf-digital@5"


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
    """Write the synthetic index as the indexer would from the files
    ``render_eml`` describes: one transaction per message, oldest first
    (the order of a first index), threads of four, ``per_message`` PDF
    attachments each.

    ``records`` sets the header fields: ``typical``; ``worst``, every
    character-clipped field past its clip with what the parser can store
    (four-byte characters in the subject, display names and filename;
    ASCII addresses with an ``@``, IDs and MIME type; 11 participants
    per role and 11 references; a nested Maildir path); ``mixed``, one
    message in fifty ``worst``; ``cardinality``, the most rows a record
    can carry (``MAX_MESSAGE_ADDRESSES`` uniquely named addresses, the
    parser's cap, and ``references`` References entries, which the
    parser does not cap by count); or ``cardinality_names``, the same
    cap spent on ``MAX_MESSAGE_ADDRESSES`` minus
    ``MAX_EXTRA_PARTICIPANT_NAMES`` unique addresses plus that many
    repeats of one address carrying further names.

    Every message has ``chunks`` body paragraphs of ``chunk_tokens``
    words, which the indexer cuts into that many chunks
    (``chunk_layout``). ``extracted_chars`` above 0 is a stress shape:
    every extraction row then holds that many four-byte characters
    instead of its payload's text, and the attachment chunks that text
    would yield are not built (``bench_args`` refuses it with
    ``--filtered``, the only step that reads chunks)."""
    t0 = time.perf_counter()
    # A first index queues every file at its message time and takes
    # them oldest first (path order breaks ties).
    order = sorted(range(messages), key=lambda i: (sent_at(i), maildir_path(i, identity, records)))
    indexed = datetime(2026, 1, 1, tzinfo=UTC)
    stress_text = _WIDE * extracted_chars if extracted_chars else None
    min_tokens = max_tokens = None
    n_chunks = n_parts = n_names = n_att_fts = 0
    entities: set[str] = set()
    with closing(sqlite3.connect(db_path, isolation_level=None)) as conn:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        for n, i in enumerate(order):
            at = sent_at(i).isoformat()
            now = (indexed + timedelta(milliseconds=50 * n)).isoformat()
            shape = _shape(i, identity, records, references)
            raw = render_eml(i, identity, records, references, chunks, chunk_tokens, per_message)
            mid = message_id(i, identity)
            cid = claimant_of(mid, raw)
            tid = message_id(i - i % 4, identity)
            folder = shape.get("folder") or _folder(i)
            path = maildir_path(i, identity, records)
            conn.execute("BEGIN")
            conn.execute(
                "INSERT INTO messages (claimant_id, message_id, thread_id, filepath, folder, "
                "subject, sent_at, sent_at_status, occurred_at, in_reply_to, references_json, "
                "has_attachments, size_bytes, content_hash, indexed_at, first_indexed_at, "
                "seen, flagged, replied, sender_ambiguous, participant_names_complete, "
                "subject_complete, from_addresses_complete, to_addresses_complete, "
                "cc_addresses_complete, attachments_manifest_complete, body_complete, caps_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, "
                "1, 1, 1, 1, 1, 1, 1, '{}')",
                (
                    cid,
                    mid,
                    tid,
                    path,
                    folder,
                    shape["subject"],
                    at,
                    None if folder == "Sent" else at,
                    shape["in_reply_to"],
                    json.dumps(shape["references"]),
                    1 if per_message else 0,
                    len(raw),
                    hashlib.sha256(raw).hexdigest(),
                    now,
                    now,
                    i % 2,
                ),
            )
            # One participant row per (role, address), named by its first
            # display name; every distinct name in the name rows.
            part_rows: dict[tuple[str, str], str | None] = {}
            name_rows: dict[tuple[str, str, str], None] = {}
            for role, address, name in [*shape["people"], *shape.get("extra_names", [])]:
                part_rows.setdefault((role, address), name)
                if name is not None:
                    name_rows.setdefault((role, address, name))
            conn.executemany(
                "INSERT INTO message_participants VALUES (?, ?, ?, ?)",
                [(cid, role, address, name) for (role, address), name in part_rows.items()],
            )
            conn.executemany(
                "INSERT INTO message_participant_names VALUES (?, ?, ?, ?)",
                [(cid, *row) for row in name_rows],
            )
            n_parts += len(part_rows)
            n_names += len(name_rows)
            # Person entities for the first distinct addresses (authors
            # first), each with its organization.
            for address in list(dict.fromkeys(a for _, a in part_rows))[
                :MAX_ENTITY_PARTICIPANTS_PER_MESSAGE
            ]:
                if address in entities:
                    continue
                entities.add(address)
                domain = address.rpartition("@")[2]
                conn.execute(
                    "INSERT OR IGNORE INTO entities (entity_id, kind, canonical_key, "
                    "organization_id, authority_class, authority_rule) "
                    "VALUES (?, 'organization', ?, NULL, 'unclassified', NULL)",
                    (f"org:{domain}", domain),
                )
                conn.execute(
                    "INSERT INTO entities (entity_id, kind, canonical_key, organization_id, "
                    "authority_class, authority_rule) VALUES (?, 'person', ?, ?, ?, ?)",
                    (f"person:{address}", address, f"org:{domain}", *authority(address)),
                )
            paragraphs = [_paragraph(i, c, chunk_tokens) for c in range(chunks)]
            chunk_rows = [
                (cid, None, "body", c, text, start, end)
                for c, (text, start, end) in enumerate(chunk_layout(paragraphs))
            ]
            for k in range(per_message):
                text = attachment_text(i, k)
                payload = pdf_payload(text)
                attachment_id = hashlib.sha256(payload).hexdigest()
                occurrence = hashlib.sha256(
                    f"{cid}\0{attachment_id}\0{shape['filename']}\0{k}".encode()
                ).hexdigest()
                n_att_fts += 1
                conn.execute(
                    "INSERT INTO attachments_fts (rowid, filename, content_type) VALUES (?, ?, ?)",
                    (n_att_fts, shape["filename"], shape["content_type"]),
                )
                conn.execute(
                    "INSERT INTO attachments (attachment_occurrence_id, claimant_id, "
                    "attachment_id, thread_id, filename, content_type, size_bytes, seen_at, "
                    "fts_rowid, extractor_module, text_complete, text_extractor) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (
                        occurrence,
                        cid,
                        attachment_id,
                        tid,
                        shape["filename"],
                        shape["content_type"],
                        len(payload),
                        now,
                        n_att_fts,
                        PDF_MODULE,
                        PDF_EXTRACTOR,
                    ),
                )
                conn.execute(
                    "INSERT INTO attachment_extractions (attachment_id, extractor_module, "
                    "extraction_status, extractor, extracted_text, extracted_at, "
                    "ocr_pages_skipped, text_complete, ocr_pages_cap, digital_pages_cap, "
                    "extracted_chars_cap) VALUES (?, ?, 'success', ?, ?, ?, 0, 1, 0, 0, 0)",
                    (attachment_id, PDF_MODULE, PDF_EXTRACTOR, stress_text or text, now),
                )
                if not stress_text:
                    chunk_rows.append(
                        (
                            f"{cid}::{attachment_id}",
                            attachment_id,
                            "attachment",
                            0,
                            text,
                            0,
                            len(text),
                        )
                    )
            for index, (pk, attachment_id, kind, c, text, start, end) in enumerate(chunk_rows):
                tokens = token_estimate(text)
                if kind == "body":
                    min_tokens = tokens if min_tokens is None else min(min_tokens, tokens)
                    max_tokens = tokens if max_tokens is None else max(max_tokens, tokens)
                n_chunks += 1
                # Production inserts without a rowid, in insertion order.
                conn.execute(
                    "INSERT INTO message_chunks_fts (rowid, text) VALUES (?, ?)", (n_chunks, text)
                )
                digest = hashlib.sha256(f"{pk}\0{c}\0".encode() + text.encode()).hexdigest()
                conn.execute(
                    "INSERT INTO message_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        digest,
                        cid,
                        tid,
                        c,
                        text,
                        start,
                        end,
                        tokens,
                        now,
                        n_chunks,
                        attachment_id,
                        kind,
                    ),
                )
            if records == "mixed" and _mixed_worst(i):
                conn.execute("INSERT INTO bench_worst (identity) VALUES (?)", (cid,))
                conn.executemany(
                    "INSERT INTO bench_worst (identity) SELECT attachment_occurrence_id "
                    "FROM attachments WHERE claimant_id = ?",
                    [(cid,)],
                )
            conn.execute("COMMIT")
        # No ANALYZE: neither the indexer nor the server runs it (nor
        # PRAGMA optimize), so a deployed index has no sqlite_stat1 and
        # the planner works without statistics, as it does here.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {
        "build_s": round(time.perf_counter() - t0, 2),
        "db_bytes": db_path.stat().st_size,
        "body_tokens_min": min_tokens,
        "body_tokens_max": max_tokens,
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
        # The scan is done once every identity is fetched, ordered and
        # hashed, for either method.
        t_scan = time.perf_counter()
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
        "fetch_s": t_fetch - t_count,
        "scan_s": t_scan - t_count,
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
    # The server holds the request body before it parses it: the file
    # read is outside every timing.
    request_body = Path(request).read_bytes()
    t0 = time.perf_counter()
    client = set(unpack_hashes(request_body))
    t_parse = time.perf_counter()
    with closing(_ro(db_path)) as conn:
        conn.execute("BEGIN")
        # BEGIN is deferred: the first statement takes the snapshot when
        # it starts, so a trivial read takes it, and the monotonic clock the
        # WAL phase lines up with its samples is read right after,
        # before the COUNT scan.
        conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone()
        started_at = time.monotonic()
        count = conn.execute(count_sql, params).fetchone()[0]
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
        ended_at = time.monotonic()
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
    _wl("address_or_name_contains", "from0.007", True, "visible_recipient"),
    _wl("domain_is", "bench.example", False, "from"),
    _wl("address_is", "to1.003@bench.example", True, "to"),
    _wl("body_words", "gamma0000004242", True),
    _wl("body_words", "alpha07"),
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
_TERMS = " ".join(f"w{n}" for n in range(1000, 1016))

MESSAGE_FILTERS: tuple[dict, ...] = (
    {"text": _TERMS},
    {"where": {"all": [{"leaf": "body_words", "value": _TERMS}]}},
    {"where": {"all": [{"leaf": "body_words", "value": _TERMS, "negate": True}]}},
    {"participant": "nobody"},
    {"participant": "from0.007"},
    {"sender": "from0.007@bench.example"},
    {"subject": "subject 0000004242"},
    {"text": "gamma0000004242"},
    {"text": "alpha07"},
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
    {"participant": "from0.007"},
    {"date_from": "2015-01-01", "date_to": "2015-12-31"},
    {"filename": "nomatch"},
    {"filename": "file"},
    {"content_type": "application/pdf"},
    {"extraction_status": "success"},
    {"extraction_status": "none"},
    {"thread_id": None},
)


def phase_filtered(db_path: str, kind: str, filters: dict, rotate: int = 0) -> dict:
    """One production page of the query under ``filters``
    (``Database.query_messages`` or ``query_attachments`` with
    ``limit=1``: its counts and first row) and the certificate over the
    same predicate by each scan method, each timed on its own.

    Whatever runs after another reads pages it cached, so ``rotate``
    rotates the order (page, stream, collect) by that many places: with
    a repeat count that is a multiple of three each step runs first
    equally often."""
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
    for step in steps[rotate % 3 :] + steps[: rotate % 3]:
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
    elif shape == "packed_junk":
        # A packed envelope with no digests and an ignored member holding
        # as many two-character strings as fit in the byte size of ``n``
        # packed digests: ``unpack_hashes`` selects
        # ``hashes`` only after ``json.loads`` has built everything.
        budget = 4 * ((32 * n + 2) // 3) + 14  # the packed upload's size
        body = ('{"hashes":"","junk":[' + ",".join(['"00"'] * ((budget - 22) // 5)) + "]}").encode()
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
    if shape in ("packed", "packed_junk"):
        digests = unpack_hashes(body)
    else:
        digests = json.loads(body)["hashes"]
    parse_s = time.perf_counter() - t0
    elements = body.count(b'"00"') if shape == "packed_junk" else len(digests)
    return {"elements": elements, "parse_s": parse_s, "rss_kib": _rss_kib()}


def phase_writer(db_path: str, stop: str, commit_bytes: int, interval: float) -> dict:
    """A concurrent writer: until ``stop`` exists, commit transactions
    that flip eight messages' ``seen`` and append ``commit_bytes`` of
    ballast, as the indexer's steady-state batch of eight would.

    After each commit it records the time and the WAL file's size, so a
    window's commit count and its WAL sizes come from the same records:
    only this process writes the WAL, and the file never shrinks until
    a truncating checkpoint, which ``run_wal`` runs after the round."""
    rng = random.Random(7)
    wal = db_path + "-wal"
    commits = 0
    times: list[tuple[float, int]] = []
    with closing(sqlite3.connect(db_path, isolation_level=None, timeout=30)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        top = conn.execute("SELECT MAX(rowid) FROM messages").fetchone()[0]
        while not os.path.exists(stop):
            conn.execute("BEGIN IMMEDIATE")
            # Eight distinct messages (``check_args`` requires eight).
            rowids = rng.sample(range(1, top + 1), 8)
            flipped = conn.execute(
                "UPDATE messages SET seen = 1 - seen WHERE rowid IN (?, ?, ?, ?, ?, ?, ?, ?)",
                rowids,
            ).rowcount
            if flipped != 8:
                raise RuntimeError(f"writer updated {flipped} messages, not 8")
            cur = conn.execute(
                "INSERT INTO bench_ballast (payload) VALUES (randomblob(?))", (commit_bytes,)
            )
            conn.execute("DELETE FROM bench_ballast WHERE id <= ?", (cur.lastrowid - 64,))
            conn.execute("COMMIT")
            # Stamped after the commit: a commit is in a round's window
            # only once it is durable, and its frames are in the size.
            times.append((time.monotonic(), os.stat(wal).st_size))
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
    upload_total: int | None = None,
    all_extras: bool = False,
) -> dict:
    """The client's upload: the SHA-256 of every matching identity but
    ``missing`` of them, plus ``extras`` hashes the server does not
    hold. ``missing_from`` ``spread`` leaves members out evenly;
    ``worst`` leaves out worst-case ones (``--records mixed``), so every
    record a round returns is worst-case. ``upload_total`` (``None``:
    no total) fills the upload with extras to that many digests;
    ``all_extras`` uploads no member. Also sizes the same upload as
    base64. The arguments were checked against the corpus before it was
    built (``check_args``); a corpus that disagrees is a benchmark bug."""
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
    if len(candidates) < missing and not all_extras:
        raise RuntimeError(f"{kind}: {len(candidates)} can be left out, not {missing}")
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
    if upload_total is not None and len(held) > upload_total:
        raise RuntimeError(f"{kind}: {len(held)} members held, over the total {upload_total}")
    if upload_total is not None:
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


def wal_window(commits: list[tuple[float, int]], start: float, end: float) -> tuple[int, int, int]:
    """The WAL size after the last commit stamped at or before ``start``,
    after the last one stamped in ``(start, end]``, and how many were:
    one boundary (the writer's post-commit stamps) for the sizes and
    the count. The file only grows meanwhile, so the size after a
    commit is the largest so far."""
    at_start = max((size for when, size in commits if when <= start), default=0)
    inside = [size for when, size in commits if start < when <= end]
    return at_start, max(inside, default=at_start), len(inside)


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
    it. Every size and count comes from the writer's own post-commit
    records (``phase_writer``), split at the round's window by the same
    timestamps, so no commit is counted without its frames or the
    reverse."""
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
        # Three seconds of steady state before the round.
        time.sleep(3)
        try:
            result = _child(
                "reconcile",
                db_path=db_path,
                kind=kind,
                k=k,
                request=request,
                hold_s=hold_s,
                worst_first=worst_first,
            )
        except RuntimeError as exc:  # raised after the writer stops (finally)
            raise RuntimeError("reconcile round failed") from exc
    finally:
        stop.touch()
        try:
            out, _ = writer.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            writer.kill()
            out, _ = writer.communicate()
        stop.unlink()
    writer_out = json.loads(out)
    at_start, at_end, overlapping = wal_window(
        writer_out["commit_times"], result["started_at"], result["ended_at"]
    )
    with closing(sqlite3.connect(db_path, timeout=30)) as conn:
        busy, log_pages, ckpt_pages = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return {
        "commit_bytes": commit_bytes,
        "writer_interval_s": interval,
        "hold_s": hold_s,
        "wal_at_transaction_start_bytes": at_start,
        "wal_max_during_transaction_bytes": at_end,
        "commits_during_transaction": overlapping,
        # WAL growth over the transaction per overlapping commit: the
        # bytes one such commit leaves retained.
        "wal_bytes_per_commit": (at_end - at_start) // overlapping if overlapping else None,
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
        # Every argument (the workload), and the software that ran it.
        "config": {
            **{a.name: getattr(args, a.name) for a in ARGS if a.name != "workdir"},
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
            None if args.upload_total is None else args.upload_total[kind == "occurrences"],
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
        _child("filtered", db_path=db, kind=kind, filters=filters, rotate=n) for n in range(repeat)
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
    for shape in ("packed", "packed_junk", "hex_array", "short_array"):
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


# --- arguments -----------------------------------------------------------
#
# Every argument is one row of ``ARGS``. ``parse_args`` builds the parser
# from the table and ``check_args`` checks every value against its row,
# then every combination in ``_RULES``, before anything is built.

# indexer/src/parser.py ``_DEFAULT_PARSE_MAX_BYTES``: a larger file is
# not indexed.
PARSE_MAX_BYTES = 50_000_000
# indexer/src/parser.py ``MAX_WALKED_PARTS``, less the multipart root
# and the body part: the most attachment parts the parser reaches.
MAX_ATTACHMENTS = 10_000 - 2
# The extractors' text cap when the indexer's own is off.
MAX_EXTRACTED_CHARS = 10_000_000
# ``message_id`` and ``_wide_digits`` stay unique below this.
MAX_MESSAGES = 2**30
# SQLite's default largest blob (``SQLITE_MAX_LENGTH``).
MAX_BLOB = 1_000_000_000


@dataclass(frozen=True)
class Arg:
    """One command-line argument: ``kind`` is ``int``, ``float``,
    ``ints`` or ``floats`` (comma-separated, ``length`` values when
    set), ``choice``, ``flag`` or ``path``; ``low`` and ``high`` bound
    each number (``None``: no bound of its own; ``_RULES`` may bound it
    by the corpus); ``absent`` says what leaving it out means."""

    name: str
    kind: str
    default: object = None
    low: float | None = None
    high: float | None = None
    choices: tuple[str, ...] = ()
    length: int | None = None
    absent: str = ""
    help: str = ""

    @property
    def flag(self) -> str:
        return "--" + self.name.replace("_", "-")


ARGS: tuple[Arg, ...] = (
    Arg("workdir", "path", "/tmp/reconcile-bench", help="where the index is built"),
    Arg("messages", "int", 50_000, 1, MAX_MESSAGES, help="messages in the corpus"),
    Arg("per_message", "int", 3, 0, MAX_ATTACHMENTS, help="PDF attachments per message"),
    Arg(
        "identity", "choice", "typical", choices=("typical", "ascii998", "ascii998common", "utf8x4")
    ),
    Arg(
        "records",
        "choice",
        "typical",
        choices=("typical", "worst", "mixed", "cardinality", "cardinality_names"),
    ),
    Arg(
        "references",
        "int",
        None,
        0,
        absent="100,000 with cardinality records, else none",
        help="References entries per message (cardinality records only)",
    ),
    Arg(
        "extracted_chars",
        "int",
        0,
        0,
        MAX_EXTRACTED_CHARS,
        help="stress: four-byte characters on every extraction row instead of its text (0: none)",
    ),
    Arg("chunks", "int", 1, 1, help="body chunks per message"),
    Arg(
        "chunk_tokens",
        "int",
        BODY_TOKENS,
        2,
        # The longest first paragraph within CHUNK_MAX_TOKENS: 14 tokens
        # of lead words, then five a filler word.
        (CHUNK_MAX_TOKENS - 14) // 5 + 2,
        help="words per body chunk",
    ),
    Arg("k", "ints", [100, 500, 1000, 2000, 5000], 1, help="records per round"),
    Arg(
        "missing",
        "int",
        None,
        0,
        absent="5,000 (none with --all-extras)",
        help="members the upload leaves out",
    ),
    Arg(
        "missing_from",
        "choice",
        "spread",
        choices=("spread", "worst"),
        help="which members the upload leaves out (worst: the mixed corpus's worst records)",
    ),
    Arg(
        "extras",
        "int",
        None,
        0,
        absent="100 (with --upload-total: what fills the total)",
        help="hashes the client holds that the server does not",
    ),
    Arg(
        "upload_total",
        "ints",
        None,
        1,
        length=2,
        absent="no total",
        help="messages,occurrences: fill each upload to this many digests with extras",
    ),
    Arg(
        "all_extras",
        "flag",
        False,
        help="the upload holds no member: --upload-total digests, all extras "
        "(the accepted worst case), and a round returns worst-case records first",
    ),
    Arg(
        "repeat",
        "int",
        None,
        1,
        absent="2, or 6 with --filtered",
        help="runs of each step: 1, or a multiple of 2 (6 with --filtered)",
    ),
    Arg("filtered", "flag", False, help="also time filtered predicates"),
    Arg(
        "request_shapes",
        "int",
        0,
        0,
        help="also time parsing each upload shape at this many digests (0: skip)",
    ),
    Arg("wal", "flag", False, help="also measure WAL growth under a writer"),
    Arg(
        "writer_commit_bytes",
        "ints",
        None,
        # randomblob() makes at least one byte.
        1,
        MAX_BLOB,
        absent="131072 with --wal",
        help="ballast bytes per writer commit (--wal only)",
    ),
    Arg(
        "writer_interval",
        "floats",
        None,
        0,
        absent="0,0.1 with --wal",
        help="seconds between writer commits (--wal only)",
    ),
    Arg(
        "wal_hold",
        "float",
        None,
        0,
        absent="0 with --wal",
        help="seconds the WAL round keeps its transaction open after its work "
        "(--wal only; smoke test)",
    ),
)


def _number_list(kind: str):
    convert = int if kind == "ints" else float

    def parse(text: str) -> list:
        return [convert(x) for x in text.split(",")]

    return parse


def _matching(messages: int, records: str) -> int:
    """Messages the unfiltered predicate matches: all but Trash (every
    twentieth, from message 0), and every one for ``worst`` records,
    which sit in a nested folder."""
    return messages if records == "worst" else messages - (messages + 19) // 20


def _worst(messages: int) -> int:
    """``--records mixed`` worst-case messages (``_mixed_worst``); none
    is in Trash."""
    return len(range(1, messages, 50))


# Every number a message's text carries from ``i`` or ``k`` is
# zero-padded, so its file size and its paragraphs' token counts depend
# only on ``i`` modulo this period (thread position, folder and the
# ``mixed`` worst-case slot): the first ``SHAPE_PERIOD`` messages hold
# every size and count the corpus has (``test_shapes_repeat_with_the_period``).
SHAPE_PERIOD = 100


def _paragraph_tokens(args: argparse.Namespace) -> list[int]:
    """The token estimate of every body paragraph of the corpus."""
    return [
        token_estimate(_paragraph(i, c, args.chunk_tokens))
        for i in range(min(args.messages, SHAPE_PERIOD))
        for c in range(min(args.chunks, 2))
    ]


def _largest_file(args: argparse.Namespace) -> int:
    """The largest Maildir file of the corpus, exactly: every size occurs
    in the first ``SHAPE_PERIOD`` messages, and every attachment part of
    a message has one size, so ``n`` parts cost ``n - 1`` times the
    second part's bytes more than one."""

    def size(i: int, parts: int) -> int:
        return len(
            render_eml(
                i,
                args.identity,
                args.records,
                args.references,
                args.chunks,
                args.chunk_tokens,
                parts,
            )
        )

    largest = 0
    for i in range(min(args.messages, SHAPE_PERIOD)):
        if args.per_message < 2:
            largest = max(largest, size(i, args.per_message))
        else:
            one, two = size(i, 1), size(i, 2)
            largest = max(largest, one + (args.per_message - 1) * (two - one))
    return largest


def _missing_problem(args: argparse.Namespace) -> str | None:
    members = _matching(args.messages, args.records)
    for kind, scale in (("messages", 1), ("occurrences", args.per_message)):
        pool = (_worst(args.messages) if args.missing_from == "worst" else members) * scale
        if args.missing > pool:
            return (
                f"--missing {args.missing}: --missing-from {args.missing_from} can leave out "
                f"{pool} {kind}; build more with --messages or lower --missing"
            )
        if args.upload_total is not None and not args.all_extras:
            total = args.upload_total[kind == "occurrences"]
            if members * scale - args.missing > total:
                return (
                    f"--upload-total {total}: the {kind} upload holds "
                    f"{members * scale - args.missing} members before any extras; raise it "
                    "or leave more members out with --missing"
                )
    return None


def _all_extras_problem(args: argparse.Namespace) -> str | None:
    worst = _worst(args.messages)
    if worst < max(args.k) or worst * args.per_message < max(args.k):
        return (
            f"--all-extras: {worst} worst-case messages and {worst * args.per_message} "
            f"occurrences cannot fill K = {max(args.k)}; raise --messages or --per-message"
        )
    return None


def _chunk_problem(args: argparse.Namespace) -> str | None:
    tokens = _paragraph_tokens(args)
    low = CHUNK_TARGET_TOKENS if args.chunks > 1 else 1
    if min(tokens) < low or max(tokens) > CHUNK_MAX_TOKENS:
        return (
            f"--chunks {args.chunks} --chunk-tokens {args.chunk_tokens}: paragraphs of "
            f"{min(tokens)} to {max(tokens)} tokens; the indexer cuts one chunk per paragraph "
            f"only from {low} to {CHUNK_MAX_TOKENS} tokens"
        )
    return None


# Combinations: (applies, problem) pairs, each ``problem`` a message or
# ``None``, checked in order once every value is in range.
_RULES: tuple = (
    (
        lambda a: a.upload_total is not None and a.explicit_extras,
        lambda a: "--extras and --upload-total exclude each other (the total sets the extras)",
    ),
    (
        lambda a: a.all_extras,
        lambda a: (
            "--all-extras needs --upload-total"
            if a.upload_total is None
            else "--all-extras needs --records mixed (the worst-case records it returns)"
            if a.records != "mixed"
            else "--all-extras excludes --missing and --missing-from worst (it holds no member)"
            if a.explicit_missing or a.missing_from == "worst"
            else _all_extras_problem(a)
        ),
    ),
    (
        lambda a: a.missing_from == "worst" and a.missing and a.records != "mixed",
        lambda a: "--missing-from worst with --missing needs --records mixed",
    ),
    (
        lambda a: a.missing_from == "worst" and not a.missing and not a.all_extras,
        lambda a: "--missing-from worst with --missing 0 leaves nothing out",
    ),
    (
        lambda a: a.extracted_chars and not a.per_message,
        lambda a: "--extracted-chars needs --per-message of at least 1 (no extraction rows)",
    ),
    (lambda a: not a.all_extras, _missing_problem),
    (
        lambda a: a.explicit_references and not a.records.startswith("cardinality"),
        lambda a: "--references applies to cardinality records only",
    ),
    (
        lambda a: a.filtered and a.chunk_tokens < 20,
        lambda a: "--filtered needs --chunk-tokens of at least 20 (sparse chunks hide FTS5 growth)",
    ),
    (
        lambda a: a.filtered and a.extracted_chars,
        lambda a: "--filtered excludes --extracted-chars (its attachment chunks are not built)",
    ),
    (lambda a: True, _chunk_problem),
    (
        lambda a: a.wal and a.messages < 8,
        lambda a: "--wal needs at least 8 messages (each writer commit updates eight)",
    ),
    (
        lambda a: not a.wal and a.explicit_wal,
        lambda a: "--writer-commit-bytes, --writer-interval and --wal-hold need --wal",
    ),
    (
        lambda a: a.repeat > 1 and a.repeat % (6 if a.filtered else 2),
        lambda a: (
            f"--repeat {a.repeat} cannot balance the run orders: use 1 or a multiple "
            f"of {6 if a.filtered else 2}"
        ),
    ),
    (
        lambda a: True,
        lambda a: (
            f"a message of {_largest_file(a)} bytes is over the indexer's {PARSE_MAX_BYTES}-byte "
            "file limit; lower --references, --chunks, --chunk-tokens or --per-message"
            if _largest_file(a) > PARSE_MAX_BYTES
            else None
        ),
    ),
)


def check_args(args: argparse.Namespace) -> argparse.Namespace:
    """Check every argument against its ``ARGS`` row, fill in what an
    absent one means, then check every combination (``_RULES``) against
    the corpus the arguments describe. Raises ``SystemExit`` naming the
    first problem; nothing has been built."""
    for arg in ARGS:
        value = getattr(args, arg.name)
        if value is None or arg.kind in ("path", "flag", "choice"):
            continue
        values = value if arg.kind in ("ints", "floats") else [value]
        if arg.length is not None and len(values) != arg.length:
            raise SystemExit(f"{arg.flag} takes {arg.length} values")
        for v in values:
            if isinstance(v, float) and not math.isfinite(v):
                raise SystemExit(f"{arg.flag} must be finite")
            if arg.low is not None and v < arg.low:
                raise SystemExit(f"{arg.flag} must be at least {arg.low}")
            if arg.high is not None and v > arg.high:
                raise SystemExit(f"{arg.flag} must be at most {arg.high}")
    args.explicit_extras = args.extras is not None
    args.explicit_missing = args.missing is not None
    args.explicit_references = args.references is not None
    args.explicit_wal = any(
        getattr(args, n) is not None for n in ("writer_commit_bytes", "writer_interval", "wal_hold")
    )
    if args.extras is None:
        args.extras = 0 if args.upload_total is not None else 100
    if args.missing is None:
        args.missing = 0 if args.all_extras else 5000
    if args.references is None:
        args.references = 100_000 if args.records.startswith("cardinality") else 0
    if args.repeat is None:
        args.repeat = 6 if args.filtered else 2
    if args.writer_commit_bytes is None:
        args.writer_commit_bytes = [128 * 1024]
    if args.writer_interval is None:
        args.writer_interval = [0.0, 0.1]
    if args.wal_hold is None:
        args.wal_hold = 0.0
    for applies, problem in _RULES:
        if applies(args) and (message := problem(args)):
            raise SystemExit(message)
    for name in ("explicit_extras", "explicit_missing", "explicit_references", "explicit_wal"):
        delattr(args, name)
    return args


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse ``argv`` with a parser built from ``ARGS`` (a value of the
    wrong type exits there) and check it (``check_args``)."""
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for arg in ARGS:
        help_text = arg.help + (f" (absent: {arg.absent})" if arg.absent else "")
        if arg.kind == "flag":
            p.add_argument(arg.flag, action="store_true", help=help_text)
        elif arg.kind == "choice":
            p.add_argument(arg.flag, choices=arg.choices, default=arg.default, help=help_text)
        else:
            convert = {"int": int, "float": float, "path": str}.get(arg.kind) or _number_list(
                arg.kind
            )
            p.add_argument(arg.flag, type=convert, default=arg.default, help=help_text)
    return check_args(p.parse_args(argv))


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
    report = run(parse_args(argv))
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    main()
