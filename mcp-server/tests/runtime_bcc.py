"""Actual Bcc readers at 5k/50k synthetic messages on shipped SQLite."""

import sqlite3
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.reconcile_bench import _SCHEMA
from src.lib.predicates import LEAVES, Where, normalize_where
from src.lib.sqlite import Database


class MemoryDatabase(Database):
    def _validate_path(self):
        pass


class RuntimeBcc(unittest.TestCase):
    def test_actual_readers_and_indexed_bcc_growth(self):
        measured = []
        for size in (5000, 50000):
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.executescript(_SCHEMA)
            conn.execute("CREATE TABLE threads(thread_id TEXT PRIMARY KEY, subject TEXT)")
            stamp = "2024-01-01T00:00:00+00:00"
            conn.executemany(
                "INSERT INTO messages(claimant_id,message_id,thread_id,filepath,folder,subject,"
                "sent_at,sent_at_status,references_json,has_attachments,indexed_at,first_indexed_at,"
                "sender_ambiguous,from_addresses_complete,to_addresses_complete,cc_addresses_complete,"
                "bcc_addresses_complete,subject_complete,body_complete) "
                "VALUES(?,?,?,?,'INBOX','synthetic',?,'parsed','[]',0,?,?,0,1,1,1,1,1,1)",
                (
                    (f"c{i}", f"m{i}", f"t{i}", f"/synthetic/{i}", stamp, stamp, stamp)
                    for i in range(size)
                ),
            )
            conn.executemany(
                "INSERT INTO threads VALUES(?,'synthetic')", ((f"t{i}",) for i in range(size))
            )
            conn.executemany(
                "INSERT INTO message_participants VALUES(?,'bcc','target@example.test',NULL)",
                ((f"c{i}",) for i in range(size)),
            )
            text = " ".join(f"syntheticword{i}" for i in range(30))
            conn.executemany(
                "INSERT INTO message_chunks VALUES (?,?,?,0,?,0,?,30,'',?,NULL,'body')",
                ((f"k{i}", f"c{i}", f"t{i}", text, len(text), i + 1) for i in range(size)),
            )
            conn.executemany(
                "INSERT INTO message_chunks_fts(rowid,text) VALUES (?,?)",
                ((i + 1, text) for i in range(size)),
            )
            conn.commit()
            params = []
            sql = LEAVES["address_is"].compile(("bcc", "target@example.test"), params)

            def indexed(statement):
                plan = conn.execute(
                    "EXPLAIN QUERY PLAN SELECT " + statement + " FROM messages m", params
                ).fetchall()
                return any("idx_message_participants_address" in r[3] for r in plan)

            self.assertTrue(indexed(sql))
            # Prove the index-plan gate detects a known-bad full participant scan.
            self.assertFalse(
                indexed(
                    sql.replace(
                        "FROM message_participants ", "FROM message_participants NOT INDEXED "
                    )
                )
            )
            work = {"connections": 0, "ticks": 0}

            def tick():
                work["ticks"] += 1
                return 0

            conn.set_progress_handler(tick, 1000)

            class Reader:
                def execute(self, statement, values=()):
                    return conn.execute(statement, values)

                def rollback(self):
                    conn.rollback()

                def close(self):
                    conn.rollback()

            db = MemoryDatabase("/synthetic/unused")

            def connect():
                work["connections"] += 1
                return Reader()

            db._connect = connect
            where = normalize_where(
                Where.model_validate(
                    {"all": [{"leaf": "address_is", "role": "bcc", "value": "target@example.test"}]}
                )
            )
            started = time.perf_counter()
            qm = db.query_messages(where=where, limit=1)
            qt = db.query_threads(where=where, limit=1)
            ag = db.aggregate_messages(group_by="direction", where=where, limit=1)
            elapsed = time.perf_counter() - started
            self.assertEqual(
                (qm.total_matches, qt.total_matching_messages, ag.total_matches), (size, size, size)
            )
            self.assertEqual(
                (qm.indeterminate, qt.indeterminate_messages, ag.indeterminate), (0, 0, 0)
            )
            self.assertEqual(qm.messages[0].bcc_count, 1)
            self.assertEqual(work["connections"], 3)
            measured.append(work["ticks"])
            print(dict(sqlite=sqlite3.sqlite_version, size=size, seconds=round(elapsed, 3), **work))
        self.assertLess(measured[1], measured[0] * 15)


if __name__ == "__main__":
    unittest.main()
