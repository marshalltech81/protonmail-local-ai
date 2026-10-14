"""Bounded original decoded MIME sources, independent of retrieval text (#1362)."""

import hashlib
import heapq
import json
import logging
import re
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, replace

from .extractors import CHILD_DEGRADATION_KEYS
from .extractors._runner import run_child
from .quoting import (
    _LINE_TERMINATORS,
    _OUTLOOK_BLOCK_PATTERN,
    _WRAPPED_REPLY_HEADER_PATTERNS,
    _hard_cut_kind,
    _is_quoted_line,
    _is_reply_header,
)
from .rate_limited_log import LineBudget

log = logging.getLogger("indexer.full_body")
_warnings = LineBudget(limit=20, window_secs=60.0, buckets=("body",))
_counts = {"captured": 0, "incomplete": 0, "classification_partial": 0, "source_bytes": 0}


def warn(message: str, *counts: object) -> None:
    _warnings.log(log, "body", message, *counts)


def log_summary() -> None:
    suppressed = _warnings.drain("body")
    if any(_counts.values()) or suppressed:
        log.info(
            "full_body: captured=%d incomplete=%d classification_partial=%d source_bytes=%d suppressed_lines=%d",
            *_counts.values(),
            suppressed,
        )
        for key in _counts:
            _counts[key] = 0


VERSION = 1
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024 * 1024
MAX_PARTS = 10_000
MAX_SOURCES = 200
MAX_FRAGMENTS = 10_000
FRAGMENT_CHARS = 8192
MAX_CLASSIFIED_LINES = 1_000_000
MAX_CLASSIFICATION_RUNS = 65_536
FINALIZE_RESERVE_SECONDS = 16.0
TRANSFER_LOSS = 1
CHARSET_LOSS = 2
STRUCTURE_LOSS = 4
CONVERSION_LOSS = 8
BUDGET_LOSS = 16
DEADLINE_LOSS = 32
_LINES = re.compile(
    r"[^\n\r\v\f\x1c-\x1e\x85\u2028\u2029]*(?:\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]|$)"
)
_SURROGATES = re.compile("[\ud800-\udfff]")

SCHEMA_SQL = """
CREATE TABLE message_body_parts (
 claimant_id TEXT NOT NULL REFERENCES messages(claimant_id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL, parent INTEGER NOT NULL,
 representation TEXT NOT NULL CHECK(representation IN ('container','alternative','related','message','plain','html','other')),
 selected INTEGER NOT NULL CHECK(selected IN (0,1)),
 losses INTEGER NOT NULL, source_chars INTEGER, source_bytes INTEGER,
 source_digest TEXT, classified_until INTEGER,
 PRIMARY KEY(claimant_id,ordinal)
) WITHOUT ROWID;
CREATE TABLE message_body_spans (
 claimant_id TEXT NOT NULL, part INTEGER NOT NULL, ordinal INTEGER NOT NULL,
 start INTEGER NOT NULL, end INTEGER NOT NULL,
 data BLOB NOT NULL CHECK(typeof(data)='blob' AND length(data)<=32768),
 PRIMARY KEY(claimant_id,part,ordinal),
 FOREIGN KEY(claimant_id,part) REFERENCES message_body_parts(claimant_id,ordinal) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TABLE message_body_ranges (
 claimant_id TEXT NOT NULL, part INTEGER NOT NULL,
 start INTEGER NOT NULL, end INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('body','quote','signature','forwarded')),
 PRIMARY KEY(claimant_id,part,start),
 FOREIGN KEY(claimant_id,part) REFERENCES message_body_parts(claimant_id,ordinal) ON DELETE CASCADE
) WITHOUT ROWID;
"""


@dataclass
class Part:
    ordinal: int
    parent: int
    representation: str
    selected: bool
    text: str | None = None
    losses: int = 0


@dataclass
class Capture:
    parts: list[Part] = field(default_factory=list)
    complete: bool = True
    source_bytes: int = 0
    metadata_bytes: int = 0
    fragment_count: int = 0
    deadline: float | None = None


def expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def convert_html(bodies: list[str], deadline: float | None) -> tuple[list[str], bool, list[int]]:
    from .extractors import html
    from .extractors.html_full_body import CAP_TEXT

    losses = [0] * len(bodies)
    timeout = html.CHILD_TIMEOUT_SECONDS
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic() - FINALIZE_RESERVE_SECONDS)
    if timeout <= 0:
        return [], True, [DEADLINE_LOSS] * len(bodies)
    documents = []
    input_bytes = 0
    for position, body in enumerate(bodies):
        chunks = []
        for start in range(0, len(body), FRAGMENT_CHARS):
            piece, replaced = _SURROGATES.subn("?", body[start : start + FRAGMENT_CHARS])
            if replaced:
                losses[position] |= CHARSET_LOSS
            data = piece.encode("utf-8")
            input_bytes += len(data)
            if input_bytes > MAX_SOURCE_BYTES:
                raise html.ChildOutputError
            if expired(deadline):
                return [], True, [DEADLINE_LOSS] * len(bodies)
            chunks.append(data)
        documents.append(b"".join(chunks))
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic() - FINALIZE_RESERVE_SECONDS)
    if timeout <= 0:
        return [], True, [DEADLINE_LOSS] * len(bodies)
    result = run_child(
        "html_full_body",
        b"".join(documents),
        options=[str(len(document)) for document in documents],
        max_address_space_bytes=html.CHILD_MAX_ADDRESS_SPACE_BYTES,
        max_cpu_seconds=html.CHILD_MAX_CPU_SECONDS,
        timeout_seconds=timeout,
        max_output_bytes=html._MAX_OUTPUT_BYTES,
        caps=frozenset({CAP_TEXT}),
        counts=CHILD_DEGRADATION_KEYS,
    )
    texts = html.split_texts(result.text, len(documents))
    cut = CAP_TEXT in result.caps
    if len(texts) != len(documents) and not (cut and texts):
        raise html.ChildOutputError
    if any(result.counts.values()):
        losses = [value | CONVERSION_LOSS for value in losses]
    return texts, cut, losses


def fragments(text: str) -> Iterator[tuple[int, int, bytes]]:
    for start in range(0, len(text), FRAGMENT_CHARS):
        end = min(start + FRAGMENT_CHARS, len(text))
        yield start, end, text[start:end].encode("utf-8")


def prepare(
    parts: list[Part], claimant: str, *, complete: bool = True, deadline: float | None = None
) -> Capture:
    """Charge every retained part/fragment before copying or encoding its text."""
    capture = Capture(complete=complete, deadline=deadline)
    charge = 2 * len(claimant.encode("utf-8")) + 256
    sources = 0
    for part in parts:
        if len(capture.parts) >= MAX_PARTS or capture.metadata_bytes + charge > MAX_METADATA_BYTES:
            capture.complete = False
            break
        capture.metadata_bytes += charge
        part = replace(part)
        capture.parts.append(part)
        capture.complete &= not bool(part.losses)
        if part.text is None:
            continue
        sources += 1
        if sources > MAX_SOURCES or expired(deadline):
            part.text = ""
            part.losses |= DEADLINE_LOSS if expired(deadline) else BUDGET_LOSS
            capture.complete = False
            continue
        end = 0
        repaired: list[str] | None = None
        for start in range(0, len(part.text), FRAGMENT_CHARS):
            if (
                capture.fragment_count >= MAX_FRAGMENTS
                or capture.metadata_bytes + charge > MAX_METADATA_BYTES
                or expired(deadline)
            ):
                part.losses |= DEADLINE_LOSS if expired(deadline) else BUDGET_LOSS
                break
            # Encode only one bounded candidate, then retain a whole-codepoint prefix.
            piece = part.text[start : start + FRAGMENT_CHARS]
            piece, replacements = _SURROGATES.subn("?", piece)
            if replacements:
                part.losses |= CHARSET_LOSS
                if repaired is None:
                    repaired = [part.text[:start]]
            available = MAX_SOURCE_BYTES - capture.source_bytes
            data = piece.encode("utf-8")
            if len(data) > available:
                piece = data[:available].decode("utf-8", errors="ignore")
                data = piece.encode("utf-8")
                part.losses |= BUDGET_LOSS
            if not piece:
                break
            capture.fragment_count += 1
            capture.metadata_bytes += charge
            capture.source_bytes += len(data)
            if repaired is not None:
                repaired.append(piece)
            end = start + len(piece)
            if part.losses & BUDGET_LOSS:
                break
        original_length = len(part.text)
        if repaired is not None:
            part.text = "".join(repaired)
        elif end < original_length:
            part.text = part.text[:end]
        if end < original_length:
            part.losses |= BUDGET_LOSS
        capture.complete &= not bool(part.losses)
    return capture


def classify(
    text: str,
    *,
    deadline: float | None = None,
    line_limit: int | None = None,
    run_limit: int | None = None,
    work: list[int] | None = None,
) -> Iterator[tuple[int, int, str]]:
    """Heuristic ranges on original characters; never reconstruct embedding text.

    The caller has already guarded the shared retained UTF8 byte budget. Regex
    iterators retain constant state; the seven multiline patterns are bounded
    passes over that text. An omitted suffix has unknown classification.
    """
    line_limit = MAX_CLASSIFIED_LINES if line_limit is None else line_limit
    run_limit = MAX_CLASSIFICATION_RUNS if run_limit is None else run_limit
    if expired(deadline) or run_limit <= 0 or line_limit <= 0 or len(text) > MAX_SOURCE_BYTES:
        return
    wrapped = heapq.merge(
        *(
            ((match.start(), match.end()) for match in pattern.finditer(text))
            for pattern in _WRAPPED_REPLY_HEADER_PATTERNS
        )
    )
    next_wrapped = next(wrapped, None)
    outlook = _OUTLOOK_BLOCK_PATTERN.search(text)
    cut = len(text) if outlook is None else outlook.start()
    mode = "body"
    current = None
    start = end = runs = 0
    for visited, match in enumerate(_LINES.finditer(text)):
        if not match.group():
            continue
        if visited >= line_limit or expired(deadline):
            break
        if work is not None:
            work[0] += 1
        offset = match.start()
        while next_wrapped is not None and next_wrapped[1] <= offset:
            next_wrapped = next(wrapped, None)
        line = match.group().rstrip(_LINE_TERMINATORS)
        if offset == cut:
            mode = "quote"
        marker = _hard_cut_kind(line)
        kind: str
        if marker is not None:
            mode = kind = marker
        elif (
            (
                next_wrapped is not None
                and offset < next_wrapped[1]
                and match.end() > next_wrapped[0]
            )
            or _is_reply_header(line)
            or _is_quoted_line(line)
        ):
            kind = "quote"
        else:
            kind = mode
        if current is not None and kind != current:
            yield start, end, current
            runs += 1
            if runs >= run_limit:
                return
            start = offset
        current = kind
        end = match.end()
    if current is not None and runs < run_limit:
        yield start, end, current


def store(cur: sqlite3.Cursor, claimant: str, capture: Capture | None) -> None:
    """Replace sources atomically in the caller's message transaction."""
    cur.execute("DELETE FROM message_body_parts WHERE claimant_id=?", (claimant,))
    if capture is None:
        cur.execute(
            "UPDATE messages SET full_body_complete=NULL, full_body_generation=NULL, full_body_version=NULL WHERE claimant_id=?",
            (claimant,),
        )
        return
    generation = hashlib.sha256(f"body-source@{VERSION}\0".encode())
    charge = 2 * len(claimant.encode("utf-8")) + 256
    lines_left, ranges_left = MAX_CLASSIFIED_LINES, MAX_CLASSIFICATION_RUNS
    stored_bytes = 0
    for part in capture.parts:
        source = hashlib.sha256()
        size = 0
        cur.execute(
            "INSERT INTO message_body_parts (claimant_id,ordinal,parent,representation,selected,losses) VALUES (?,?,?,?,?,?)",
            (
                claimant,
                part.ordinal,
                part.parent,
                part.representation,
                int(part.selected),
                part.losses,
            ),
        )
        classified_until = 0
        classification = hashlib.sha256()
        if part.text is not None:
            stored_end = 0
            for ordinal, (start, end, data) in enumerate(fragments(part.text)):
                if expired(capture.deadline):
                    part.losses |= DEADLINE_LOSS
                    capture.complete = False
                    break
                source.update(data)
                size += len(data)
                stored_bytes += len(data)
                cur.execute(
                    "INSERT INTO message_body_spans VALUES (?,?,?,?,?,?)",
                    (claimant, part.ordinal, ordinal, start, end, data),
                )
                stored_end = end
            if stored_end < len(part.text):
                part.text = part.text[:stored_end]
            work = [0]
            count = 0
            for start, end, kind in classify(
                part.text,
                deadline=capture.deadline,
                line_limit=lines_left,
                run_limit=ranges_left,
                work=work,
            ):
                if capture.metadata_bytes + charge > MAX_METADATA_BYTES:
                    break
                capture.metadata_bytes += charge
                cur.execute(
                    "INSERT INTO message_body_ranges VALUES (?,?,?,?,?)",
                    (claimant, part.ordinal, start, end, kind),
                )
                classified_until = end
                classification.update(
                    json.dumps((start, end, kind), separators=(",", ":")).encode()
                )
                count += 1
            lines_left -= work[0]
            ranges_left -= count
            if classified_until < len(part.text):
                _counts["classification_partial"] += 1
                warn(
                    "full decoded body classification partial: source_chars=%d classified_chars=%d",
                    len(part.text),
                    classified_until,
                )
            cur.execute(
                "UPDATE message_body_parts SET source_chars=?,source_bytes=?,source_digest=?,classified_until=?,losses=? WHERE claimant_id=? AND ordinal=?",
                (
                    len(part.text),
                    size,
                    source.hexdigest(),
                    classified_until,
                    part.losses,
                    claimant,
                    part.ordinal,
                ),
            )
        generation.update(
            json.dumps(
                (
                    part.ordinal,
                    part.parent,
                    part.representation,
                    part.selected,
                    part.losses,
                    len(part.text) if part.text is not None else None,
                    source.hexdigest(),
                    classified_until,
                    classification.hexdigest(),
                ),
                separators=(",", ":"),
            ).encode()
        )
    cur.execute(
        "UPDATE messages SET full_body_complete=?, full_body_generation=?, full_body_version=? WHERE claimant_id=?",
        (int(capture.complete), generation.hexdigest(), VERSION, claimant),
    )
    _counts["captured"] += 1
    _counts["incomplete"] += int(not capture.complete)
    _counts["source_bytes"] += stored_bytes
