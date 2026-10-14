"""Count predicate evidence in the caller's mailbox-status snapshot."""

import re
import sqlite3

from .predicates import LEAVES


def query_capabilities(conn: sqlite3.Connection) -> dict:
    tables = {"messages"}
    for kind in LEAVES.values():
        if kind.schema is None:
            raise ValueError("Registered predicate has no schema dependency rule")
        tables.update(kind.schema)
    columns = {
        table: {row[0] for row in conn.execute("SELECT name FROM pragma_table_xinfo(?)", (table,))}
        for table in tables
    }
    rules = {}
    result: dict[str, dict[str, str | int | None]] = {}
    for name, kind in LEAVES.items():
        if kind.coverage is None:
            raise ValueError("Registered predicate has no coverage rule")
        complete, unassessed = kind.coverage
        sql = complete + " " + unassessed
        required = set(re.findall(r"\bm\.([a-z_]+)", sql))
        assert kind.schema is not None
        if not required <= columns["messages"] or any(
            not set(fields) <= columns[table] for table, fields in kind.schema.items()
        ):
            result[name] = {
                "status": "unavailable",
                "available_messages": None,
                "unassessed_messages": None,
                "missing_messages": None,
                "condition": "Restore required schema through indexer upgrade and migrations, "
                "or rebuild a damaged index; reparse existing mail.",
            }
        elif (
            kind.identity_dependent
            and not conn.execute(
                "SELECT EXISTS(SELECT 1 FROM operator_identity WHERE id=1 AND state='configured')"
            ).fetchone()[0]
        ):
            result[name] = {
                "status": "unavailable",
                "available_messages": None,
                "unassessed_messages": None,
                "missing_messages": None,
                "condition": "Configure explicit own addresses in config/identity.toml and restart the indexer.",
            }

        elif kind.coverage_unmeasured:
            result[name] = {
                "status": "unmeasured",
                "available_messages": None,
                "unassessed_messages": None,
                "missing_messages": None,
                "condition": "Normalized header usability is measured only by requested queries; complete capture does not establish valid normalization.",
            }
        else:
            rules[name] = (complete, unassessed)
    expressions = ["COUNT(*)"]
    for complete, unassessed in rules.values():
        expressions += [
            f"TOTAL(({complete}) IS TRUE)",
            f"TOTAL(({complete}) IS NOT TRUE AND ({unassessed}) IS TRUE)",
        ]
    # One message scan, with indexed participant probes for direction;
    # attachment occurrences are grouped once,
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
