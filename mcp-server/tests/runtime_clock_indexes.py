"""Run with the shipped image's Python; synthetic, in-memory SQLite only.

The runtime SQLite planner can differ from the host used by pytest. Exercise
the production predicate/page builders against the independently expected DDL.
"""

import sqlite3
import unittest

from src.lib.predicates import DATE_BASES, query_messages_leaves
from src.lib.sqlite import _clock_page_predicate, _message_expression


class RuntimeClockIndexes(unittest.TestCase):
    def test_tuple_seeks_and_unchanged_count_plans(self):
        work = {"sent": [], "occurred": []}
        for size in (5000, 50000):
            conn = sqlite3.connect(":memory:")
            try:
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.execute(
                    "CREATE TABLE messages (claimant_id TEXT PRIMARY KEY, sent_at TEXT, "
                    "sent_at_status TEXT, occurred_at TEXT, folder TEXT, seen INTEGER, subject TEXT, "
                    "subject_complete INTEGER DEFAULT 1)"
                )
                stamp = "2024-01-01T00:00:00+00:00"
                conn.executemany(
                    "INSERT INTO messages (claimant_id, sent_at, sent_at_status, occurred_at, folder, seen, subject) "
                    "VALUES (?, ?, 'parsed', ?, 'INBOX', 0, 'synthetic')",
                    ((f"claim-{i:06d}", stamp, stamp) for i in range(size)),
                )
                conn.execute("CREATE INDEX idx_folder ON messages(folder)")
                conn.create_function("mcp_casefold", 1, str.casefold, deterministic=True)
                for basis_name in ("sent", "occurred"):
                    basis = DATE_BASES[basis_name]
                    for filters in (
                        {},
                        {"folder": "INBOX"},
                        {"seen": False},
                        {"subject": "synthetic"},
                    ):
                        kwargs = dict.fromkeys(
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
                        kwargs.update(
                            filters,
                            date_basis=basis_name,
                            date_from="2023-01-01",
                            date_to="2025-01-01",
                        )
                        leaves = query_messages_leaves(**kwargs)
                        where, params = _message_expression(leaves, ())
                        count_sql = f"SELECT COUNT(*) FROM messages m WHERE {where}"
                        before = [
                            r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + count_sql, params)
                        ]
                        index = f"idx_messages_{basis_name}"
                        guard = (
                            "sent_at_status = 'parsed'"
                            if basis_name == "sent"
                            else "(occurred_at IS NOT NULL) = 1"
                        )
                        clock = "sent_at" if basis_name == "sent" else "occurred_at"
                        conn.execute(
                            f"CREATE INDEX {index} ON messages({clock}, claimant_id) WHERE {guard}"
                        )
                        after = [
                            r[3]
                            for r in conn.execute("EXPLAIN QUERY PLAN " + count_sql + " ", params)
                        ]
                        self.assertEqual(before, after)
                        self.assertEqual(conn.execute(count_sql, params).fetchone()[0], size)
                        page_where, page_params = _clock_page_predicate(
                            where, params, basis, leaves, has_cursor=True
                        )
                        sql = f"SELECT claimant_id FROM messages m WHERE {page_where} AND ({basis.page_clock}, m.claimant_id) < (?, ?) ORDER BY {basis.page_clock} DESC, m.claimant_id DESC LIMIT 26"
                        values = [*page_params, stamp, f"claim-{size // 10:06d}"]
                        plan = " ".join(
                            r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, values)
                        )
                        if not filters:
                            self.assertIn("SEARCH m USING", plan)
                            self.assertIn(index, plan)
                            self.assertNotIn("TEMP B-TREE", plan)
                        ticks = []
                        conn.set_progress_handler(lambda: ticks.append(1) or 0, 100)
                        rows = conn.execute(sql, values).fetchall()
                        conn.set_progress_handler(None, 0)
                        self.assertEqual(
                            [r[0] for r in rows],
                            [f"claim-{i:06d}" for i in range(size // 10 - 1, size // 10 - 27, -1)],
                        )
                        if not filters:
                            work[basis_name].append(len(ticks))
                        conn.execute(f"DROP INDEX {index}")
            finally:
                conn.close()
        for counts in work.values():
            self.assertLessEqual(counts[1], counts[0] + 2)
            self.assertLess(counts[1], 100)


if __name__ == "__main__":
    print(f"Runtime SQLite {sqlite3.sqlite_version}", flush=True)
    unittest.main()
