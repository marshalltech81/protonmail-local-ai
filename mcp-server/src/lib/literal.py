"""Bounded literal phrase candidates and original-boundary verification (#1388)."""

import unicodedata

UNICODE_VERSION = unicodedata.unidata_version
MAX_NEEDLE = 1000


def normalize_phrase(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_NEEDLE:
        raise ValueError("literal phrase must hold 1 to 1000 folded characters")
    folded = value.casefold()
    if not 1 <= len(folded) <= MAX_NEEDLE or any(0xD800 <= ord(c) <= 0xDFFF for c in folded):
        raise ValueError("literal phrase must hold 1 to 1000 valid folded characters")
    return folded


def gram_token(text: str) -> str:
    return "g" + "".join(f"{ord(c):06x}" for c in text)


def candidate_tokens(needle: str) -> tuple[str, ...]:
    if len(needle) < 3:
        return (gram_token(needle),)
    grams = list(dict.fromkeys(gram_token(needle[i : i + 3]) for i in range(len(needle) - 2)))
    return (
        tuple(grams[(i * (len(grams) - 1)) // 7] for i in range(8))
        if len(grams) > 8
        else tuple(grams)
    )


def contains(text: str, needle: str, intervals: list[tuple[int, int]]) -> bool:
    """A match must begin and end at original character boundaries in one run."""
    for start, end in intervals:
        original = text[start:end]
        folded = original.casefold()
        if needle not in folded:
            continue
        pieces = [c.casefold() for c in original]
        if all(len(piece) == 1 for piece in pieces):
            return True
        boundaries = {0}
        offset = 0
        for piece in pieces:
            offset += len(piece)
            boundaries.add(offset)
        # Repeated find(start+1) rescans a long needle at every overlapping,
        # misaligned hit. KMP keeps source and operand work linear instead.
        prefix = [0] * len(needle)
        matched = 0
        for i in range(1, len(needle)):
            while matched and needle[i] != needle[matched]:
                matched = prefix[matched - 1]
            if needle[i] == needle[matched]:
                matched += 1
            prefix[i] = matched
        matched = 0
        for i, char in enumerate(folded):
            while matched and char != needle[matched]:
                matched = prefix[matched - 1]
            if char == needle[matched]:
                matched += 1
            if matched == len(needle):
                if i + 1 - len(needle) in boundaries and i + 1 in boundaries:
                    return True
                matched = prefix[matched - 1]

    return False


# SQL is code-owned; operands are bound and only integer window IDs leave SQLite.
LEAF_SCOPES = {
    "body_phrase": "b",
    "authored_body_phrase": "a",
    "quote_phrase": "q",
    "signature_phrase": "s",
    "forwarded_phrase": "f",
    "attachment_text_phrase": "t",
}
KINDS = {"a": "body", "q": "quote", "s": "signature", "f": "forwarded"}
VERSION = 1
MAX_LEAF_CANDIDATES = 200_000
MAX_REQUEST_CANDIDATES = 1_000_000
MAX_VERIFICATION_BYTES = 64 * 1024 * 1024
DEADLINE_SECONDS = 10.0
CURRENT_SQL = """(
 (w.part IS NOT NULL AND lm.full_body_version=1 AND w.generation=lm.full_body_generation)
 OR (w.occurrence_id IS NOT NULL AND lm.attachment_rows_current=1
 AND at.extraction_deferred_at IS NULL AND at.text_extractor IS ex.extractor
 AND w.generation=ex.extracted_at)
)"""
CURRENT_JOINS = f"""JOIN messages lm ON lm.claimant_id=w.claimant_id
JOIN literal_state li ON li.claimant_id=w.claimant_id AND li.version=1 AND li.unicode_version='{UNICODE_VERSION}'
LEFT JOIN attachments at ON at.attachment_occurrence_id=w.occurrence_id
LEFT JOIN attachment_extractions ex ON ex.attachment_id=at.attachment_id AND ex.extractor_module=at.extractor_module"""


def complete_sql(scope: str) -> str:
    if scope == "t":
        return f"(m.attachments_manifest_complete=1\n        AND (m.has_attachments=0 OR (m.attachment_rows_current=1\n        AND (SELECT attachments_complete=1 AND version=1 AND unicode_version='{UNICODE_VERSION}' FROM literal_state ls WHERE ls.claimant_id=m.claimant_id)\n        AND NOT EXISTS(SELECT 1 FROM attachments la\n        LEFT JOIN attachment_extractions le ON le.attachment_id=la.attachment_id AND le.extractor_module=la.extractor_module\n        LEFT JOIN literal_attachment_state las ON las.occurrence_id=la.attachment_occurrence_id\n        WHERE la.claimant_id=m.claimant_id AND (la.text_complete IS NOT 1 OR la.extraction_deferred_at IS NOT NULL\n        OR la.text_extractor IS NOT le.extractor OR las.generation IS NOT le.extracted_at OR las.complete IS NOT 1)))))"  # nosec B608 -- fixed SQL and interpreter Unicode version; operands are bound
    column = "body_complete" if scope == "b" else "classified_complete"
    return f"(m.full_body_version=1 AND (SELECT {column}=1 AND version=1 AND unicode_version='{UNICODE_VERSION}'\n    AND generation=m.full_body_generation FROM literal_state ls WHERE ls.claimant_id=m.claimant_id))"  # nosec B608 -- fixed SQL and interpreter Unicode version; operands are bound


def unassessed_sql(scope: str) -> str:
    base = f"NOT EXISTS(SELECT 1 FROM literal_state ls WHERE ls.claimant_id=m.claimant_id AND ls.version=1 AND ls.unicode_version='{UNICODE_VERSION}')"  # nosec B608 -- fixed SQL and interpreter Unicode version; operands are bound
    if scope == "t":
        return f"({base} OR m.attachments_manifest_complete IS NULL OR (m.has_attachments IS NOT 0 AND (m.attachment_rows_current IS NULL OR (SELECT attachments_complete IS NULL FROM literal_state ls WHERE ls.claimant_id=m.claimant_id) OR EXISTS(SELECT 1 FROM attachments la WHERE la.claimant_id=m.claimant_id AND la.text_complete IS NULL))))"  # nosec B608 -- fixed SQL and interpreter Unicode version; operands are bound
    column = "body_complete" if scope == "b" else "classified_complete"
    return f"({base} OR m.full_body_generation IS NULL OR (SELECT {column} IS NULL OR generation IS NOT m.full_body_generation FROM literal_state ls WHERE ls.claimant_id=m.claimant_id))"  # nosec B608 -- fixed SQL and interpreter Unicode version; operands are bound


def compile_phrase(scope: str, needle: str, params: list) -> str:
    params.append(scope + needle)
    source = "w.part IS NULL" if scope == "t" else "w.part IS NOT NULL"
    return f"CASE WHEN EXISTS(SELECT 1 FROM literal_windows w {CURRENT_JOINS}\n    WHERE w.claimant_id=m.claimant_id AND {source} AND {CURRENT_SQL}\n    AND mcp_literal(?,w.id)=1) THEN 1 WHEN {complete_sql(scope)} THEN 0 ELSE NULL END"  # nosec B608 -- fixed SQL and interpreter Unicode version; operands are bound


def _refused(
    message="literal phrase query work limit exhausted; narrow the phrase or split the request",
):
    from .predicates import InvalidFilterError

    return InvalidFilterError("where", message)


class QueryBudget:
    """One bounded preparation/cache per actual read connection and request."""

    def __init__(self, conn):
        self.conn = conn
        self.deadline = None
        self.candidates = 0
        self.bytes = 0
        self.matches: dict[str, set[int]] = {}

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        import sqlite3
        import time

        if self.deadline is not None:
            self.conn.set_progress_handler(None, 0)
        if (
            kind is sqlite3.OperationalError
            and self.deadline is not None
            and time.monotonic() >= self.deadline
        ):
            raise _refused() from None
        return False

    def _check(self):
        import time

        assert self.deadline is not None
        if time.monotonic() >= self.deadline:
            raise _refused()

    def prepare(self, where):
        import time

        requests = {
            (LEAF_SCOPES[w.leaf.name], w.leaf.value) for w in where if w.leaf.name in LEAF_SCOPES
        }
        if not requests:
            return
        self.deadline = time.monotonic() + DEADLINE_SECONDS
        self.conn.set_progress_handler(lambda: int(time.monotonic() >= self.deadline), 1000)
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN")
        tables = {
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('literal_windows','literal_long_fts','literal_short_fts','literal_state','literal_attachment_spans','literal_attachment_state')"
            )
        }
        if len(tables) != 6:
            raise _refused(
                "literal phrase indexes unavailable; upgrade the indexer and reparse existing mail"
            )
        for scope, needle in sorted(requests):
            self._check()
            short = len(needle) <= 2
            table = "literal_short_fts" if short else "literal_long_fts"
            tokens = candidate_tokens(needle)
            query = scope + tokens[0] if short else " AND ".join(tokens)
            # LIMIT before counting or transferring payload: a broad posting list
            # never forces an exhaustive preflight count.
            rows = self.conn.execute(
                f"SELECT w.id,CASE WHEN typeof(w.read_bytes)='integer' AND w.read_bytes BETWEEN 0 AND 36764 THEN w.read_bytes END,CASE WHEN typeof(w.ranges)='text' AND length(CAST(w.ranges AS BLOB))<=524288 THEN length(CAST(w.ranges AS BLOB)) END FROM {table} CROSS JOIN literal_windows w ON w.id={table}.rowid WHERE {table} MATCH ? LIMIT ?",  # nosec B608 -- fixed table names; values bound
                (query, MAX_LEAF_CANDIDATES + 1),
            ).fetchall()
            if (
                len(rows) > MAX_LEAF_CANDIDATES
                or self.candidates + len(rows) > MAX_REQUEST_CANDIDATES
            ):
                raise _refused()
            if any(size is None or metadata is None for _, size, metadata in rows):
                raise _refused(
                    "literal phrase index contains invalid window metadata; rebuild the index"
                )
            self.candidates += len(rows)
            # The second fragment's bounded byte prefix can include extra UTF8
            # characters beyond the 999 needed; charge the complete transfer.
            self.bytes += sum(size + metadata for _, size, metadata in rows)
            if self.bytes > MAX_VERIFICATION_BYTES:
                raise _refused()
            found: set[int] = set()
            for start in range(0, len(rows), 128):
                self._check()
                ids = [r[0] for r in rows[start : start + 128]]
                placeholders = ",".join("?" for _ in ids)
                data = self.conn.execute(
                    f"""SELECT w.id,w.byte_count,
                    CASE WHEN w.part IS NOT NULL THEN CASE WHEN typeof(bs.data)='blob' AND length(bs.data)<=32768 THEN bs.data END
                      ELSE CASE WHEN typeof(ats.data)='blob' AND length(ats.data)<=32768 THEN ats.data END END,
                    CASE WHEN w.part IS NOT NULL THEN CASE WHEN typeof(bn.data)='blob' AND length(bn.data)<=32768 THEN substr(bn.data,1,3996) END
                      ELSE CASE WHEN typeof(atn.data)='blob' AND length(atn.data)<=32768 THEN substr(atn.data,1,3996) END END,
                    CASE WHEN typeof(w.ranges)='text' AND length(CAST(w.ranges AS BLOB))<=524288 THEN w.ranges END
                    FROM literal_windows w {CURRENT_JOINS}
                    LEFT JOIN message_body_spans bs ON bs.claimant_id=w.claimant_id AND bs.part=w.part AND bs.ordinal=w.ordinal
                    LEFT JOIN message_body_spans bn ON bn.claimant_id=w.claimant_id AND bn.part=w.part AND bn.ordinal=w.ordinal+1
                    LEFT JOIN literal_attachment_spans ats ON ats.occurrence_id=w.occurrence_id AND ats.ordinal=w.ordinal
                    LEFT JOIN literal_attachment_spans atn ON atn.occurrence_id=w.occurrence_id AND atn.ordinal=w.ordinal+1
                    WHERE w.id IN ({placeholders}) AND {CURRENT_SQL}""",  # nosec B608 -- code-owned SQL, integer IDs bound
                    ids,
                )
                for window, size, first, following, ranges in data:
                    self._check()
                    if first is None or ranges is None:
                        raise _refused(
                            "literal phrase index contains invalid fragments; rebuild the index"
                        )
                    import codecs
                    import json

                    try:
                        tail = codecs.getincrementaldecoder("utf-8")().decode(
                            following or b"", final=False
                        )[:999]
                        head = first.decode("utf-8")
                        text = head + tail
                        if (
                            len(head) > 8192
                            or (following is not None and len(head) != 8192)
                            or len(text.encode("utf-8")) != size
                        ):
                            raise ValueError
                        classified = json.loads(ranges)
                        previous = 0
                        for a, b, kind in classified:
                            if (
                                type(a) is not int
                                or type(b) is not int
                                or not previous <= a < b <= len(text)
                                or kind not in KINDS.values()
                            ):
                                raise ValueError
                            previous = b
                        if scope in ("b", "t"):
                            intervals = [(0, len(text))]
                        else:
                            intervals = [
                                (a, b) for a, b, kind in classified if kind == KINDS[scope]
                            ]
                        if contains(text, needle, intervals):
                            found.add(window)
                    except ValueError, TypeError, UnicodeError:
                        raise _refused(
                            "literal phrase index contains invalid source evidence; rebuild the index"
                        ) from None
            self.matches[scope + needle] = found
        self.conn.create_function(
            "mcp_literal",
            2,
            lambda key, window: int(window in self.matches.get(key, set())),
            deterministic=True,
        )
