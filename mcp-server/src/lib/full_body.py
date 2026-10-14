"""Internal bounded full-source reader for future evidence jobs (#1362)."""

import sqlite3

VERSION = 1
MAX_FRAGMENT_BYTES = 32768
MAX_FRAGMENTS = 8
MAX_METADATA_ROWS = 256


class BodySourceError(ValueError):
    """Fixed-text stale, unknown or malformed private source evidence."""


def _generation(conn: sqlite3.Connection, claimant: str, generation: str) -> None:
    row = conn.execute(
        """SELECT CASE WHEN typeof(full_body_generation)='text'
        AND length(CAST(full_body_generation AS BLOB))=64 THEN full_body_generation END,
        CASE WHEN typeof(full_body_version)='integer' THEN full_body_version END
        FROM messages WHERE claimant_id=?""",
        (claimant,),
    ).fetchone()
    if row is None or row[0] != generation or row[1] != VERSION:
        raise BodySourceError("full decoded body generation unavailable or changed")


def read_manifest(conn: sqlite3.Connection, claimant: str, generation: str) -> dict:
    _generation(conn, claimant, generation)
    row = conn.execute(
        "SELECT CASE WHEN full_body_complete IN (0,1) THEN full_body_complete END FROM messages WHERE claimant_id=?",
        (claimant,),
    ).fetchone()
    return {
        "version": VERSION,
        "generation": generation,
        "complete": None if row is None or row[0] is None else bool(row[0]),
    }


def read_page(
    conn: sqlite3.Connection,
    claimant: str,
    generation: str,
    part: int,
    after: int = -1,
    limit: int = MAX_FRAGMENTS,
) -> list[dict]:
    """Inside the caller's read transaction; ordinal continuation never crosses sources."""
    if not 1 <= limit <= MAX_FRAGMENTS or part < 0 or after < -1:
        raise BodySourceError("invalid full decoded body page bounds")
    _generation(conn, claimant, generation)
    rows = conn.execute(
        """
        SELECT CASE WHEN typeof(ordinal)='integer' THEN ordinal END,
          CASE WHEN typeof(start)='integer' THEN start END,
          CASE WHEN typeof(end)='integer' THEN end END,
          CASE WHEN typeof(data)='blob' AND length(data)<=32768 THEN data END
        FROM message_body_spans WHERE claimant_id=? AND part=? AND ordinal>?
        ORDER BY ordinal LIMIT ?
        """,
        (claimant, part, after, limit),
    ).fetchall()
    result = []
    for ordinal, start, end, data in rows:
        if data is None or ordinal is None or start is None or end is None:
            raise BodySourceError("full decoded body fragment exceeds its bound")
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            raise BodySourceError("full decoded body fragment is not valid UTF8") from None
        if len(text) != end - start:
            raise BodySourceError("full decoded body fragment offsets are invalid")
        result.append({"ordinal": ordinal, "start": start, "end": end, "text": text})
    return result


def read_metadata(
    conn: sqlite3.Connection,
    claimant: str,
    generation: str,
    after: int = -1,
    limit: int = MAX_METADATA_ROWS,
) -> list[dict]:
    if not 1 <= limit <= MAX_METADATA_ROWS or after < -1:
        raise BodySourceError("invalid full decoded body metadata page bounds")
    _generation(conn, claimant, generation)
    rows = conn.execute(
        """
        SELECT CASE WHEN typeof(ordinal)='integer' THEN ordinal END,
          CASE WHEN typeof(parent)='integer' THEN parent END,
          CASE WHEN representation IN ('container','alternative','related','message','plain','html','other')
               THEN representation END,
          CASE WHEN typeof(selected)='integer' THEN selected END,
          CASE WHEN typeof(losses)='integer' THEN losses END,
          CASE WHEN typeof(source_chars)='integer' THEN source_chars END,
          CASE WHEN typeof(source_bytes)='integer' THEN source_bytes END,
          CASE WHEN source_digest IS NULL OR
               (typeof(source_digest)='text' AND length(CAST(source_digest AS BLOB))=64)
               THEN source_digest END,
          CASE WHEN typeof(classified_until)='integer' THEN classified_until END
        FROM message_body_parts WHERE claimant_id=? AND ordinal>?
        ORDER BY ordinal LIMIT ?
        """,
        (claimant, after, limit),
    ).fetchall()
    names = (
        "part",
        "parent",
        "representation",
        "selected",
        "losses",
        "source_chars",
        "source_bytes",
        "source_digest",
        "classified_until",
    )
    result = []
    for row in rows:
        if (
            any(row[index] is None for index in (0, 1, 2, 3, 4))
            or (row[5] is not None and row[7] is None)
            or (
                row[2] in ("plain", "html", "other")
                and any(row[index] is None for index in (5, 6, 7, 8))
            )
        ):
            raise BodySourceError("full decoded body metadata is invalid")
        result.append(dict(zip(names, row, strict=True)))
    return result


def read_ranges(
    conn: sqlite3.Connection,
    claimant: str,
    generation: str,
    part: int,
    after: int = -1,
    limit: int = MAX_METADATA_ROWS,
) -> list[dict]:
    if not 1 <= limit <= MAX_METADATA_ROWS or part < 0 or after < -1:
        raise BodySourceError("invalid full decoded body range page bounds")
    _generation(conn, claimant, generation)
    rows = conn.execute(
        """
        SELECT CASE WHEN typeof(start)='integer' THEN start END,
          CASE WHEN typeof(end)='integer' THEN end END,
          CASE WHEN kind IN ('body','quote','signature','forwarded')
                             THEN kind END
        FROM message_body_ranges WHERE claimant_id=? AND part=? AND start>?
        ORDER BY start LIMIT ?
        """,
        (claimant, part, after, limit),
    ).fetchall()
    if any(any(value is None for value in row) for row in rows):
        raise BodySourceError("full decoded body classification is invalid")
    return [dict(zip(("start", "end", "kind"), row, strict=True)) for row in rows]
