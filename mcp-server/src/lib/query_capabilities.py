"""Count predicate evidence in the caller's mailbox-status snapshot."""

import re
import sqlite3

from .predicates import LEAVES


def query_capabilities(conn: sqlite3.Connection) -> dict:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    attachment_columns = {row[1] for row in conn.execute("PRAGMA table_info(attachments)")}
    rules = {}
    result: dict[str, dict[str, str | int | None]] = {}
    for name, kind in LEAVES.items():
        if kind.coverage is None:
            raise ValueError("Registered predicate has no coverage rule")
        complete, unassessed = kind.coverage
        sql = complete + " " + unassessed
        required = set(re.findall(r"\bm\.([a-z_]+)", sql))
        attachments = "a." in sql
        if not required <= columns or (
            attachments and not {"claimant_id", "text_complete"} <= attachment_columns
        ):
            result[name] = {
                "status": "unavailable",
                "available_messages": None,
                "unassessed_messages": None,
                "missing_messages": None,
                "condition": "Upgrade the indexer and apply its schema migrations, then reparse mail.",
            }
        else:
            rules[name] = (complete, unassessed)
    expressions = ["COUNT(*)"]
    for complete, unassessed in rules.values():
        expressions += [
            f"TOTAL(({complete}) IS TRUE)",
            f"TOTAL(({complete}) IS NOT TRUE AND ({unassessed}) IS TRUE)",
        ]
    # One flags-only message scan; attachment occurrences are grouped once,
    # without reading filenames, extracted text, FTS tables or payloads.
    join = ""
    if any("a." in complete + unassessed for complete, unassessed in rules.values()):
        join = """LEFT JOIN (
            SELECT claimant_id, COUNT(*) AS n,
                   TOTAL(text_complete = 0) AS incomplete,
                   TOTAL(text_complete IS NULL) AS unassessed
            FROM attachments GROUP BY claimant_id
        ) a ON a.claimant_id = m.claimant_id"""
    # Every expression and the join come from code-owned registry literals;
    # neither tool arguments nor stored mail values enter the SQL text.
    row = conn.execute(
        f"SELECT {', '.join(expressions)} FROM messages m {join}"  # nosec B608
    ).fetchone()
    total = row[0]
    for i, name in enumerate(rules):
        available, awaiting = int(row[1 + 2 * i]), int(row[2 + 2 * i])
        missing = total - available - awaiting
        result[name] = {
            "status": (
                "supported after backfill"
                if awaiting
                else "supported when present"
                if missing
                else "supported"
            ),
            "available_messages": available,
            "unassessed_messages": awaiting,
            "missing_messages": missing,
            "condition": None,
        }
    return {name: result[name] for name in LEAVES}
