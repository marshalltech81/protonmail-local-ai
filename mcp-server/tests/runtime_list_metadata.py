"""Actual metadata readers on host and shipped SQLite; synthetic data only."""

import json
import sqlite3
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.reconcile_bench import _SCHEMA
from src.lib.list_metadata import metadata_result_sql, sqlite_normalize_fields, with_metadata
from src.lib.predicates import Where, normalize_where
from src.lib.sqlite import Database


class MemoryDatabase(Database):
    def _validate_path(self):
        pass


class RuntimeListMetadata(unittest.TestCase):
    def test_nul_safe_guards_precede_python(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(
            "CREATE TABLE messages(claimant_id TEXT PRIMARY KEY,headers_complete INTEGER); "
            "CREATE TABLE message_headers(claimant_id TEXT,name_normalized TEXT,raw_value TEXT);"
        )
        conn.executemany("INSERT INTO messages VALUES (?,1)", [("large",), ("nul",)])
        conn.executemany(
            "INSERT INTO message_headers VALUES (?,'list-id',?)",
            [("large", "\x00" + "x" * 40000), ("nul", "<a.example>\x00")],
        )
        calls = []
        conn.create_function("mcp_list_metadata", 2, lambda *args: calls.append(args))
        sql = "SELECT m.claimant_id," + metadata_result_sql("list_id") + " FROM messages m"
        rows = conn.execute(with_metadata(sql, ("list_id",))).fetchall()
        self.assertEqual(calls, [])
        self.assertEqual(
            {key: json.loads(value) for key, value in rows},
            {"large": [None, "budget_exceeded"], "nul": [None, "malformed"]},
        )

    def test_actual_reader_growth_and_normalization_work(self):
        measured = []
        for size in (5000, 50000):
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.executescript("""
                CREATE TABLE threads (thread_id TEXT PRIMARY KEY, subject TEXT);
                CREATE TABLE message_headers (claimant_id TEXT, ordinal INTEGER,
                    name TEXT, name_normalized TEXT, raw_value TEXT, decoded_value TEXT,
                    PRIMARY KEY(claimant_id, ordinal));
                CREATE INDEX idx_message_headers_name_claimant
                    ON message_headers(name_normalized, claimant_id);
            """)
            conn.execute("PRAGMA temp_store=MEMORY")
            stamp = "2024-01-01T00:00:00+00:00"
            conn.executemany(
                "INSERT INTO messages (claimant_id,message_id,thread_id,subject,"
                "sent_at,sent_at_status,references_json,has_attachments,indexed_at,first_indexed_at,"
                "sender_ambiguous,from_addresses_complete,to_addresses_complete,cc_addresses_complete,"
                "attachments_manifest_complete,attachment_rows_current,body_complete,subject_complete,headers_complete) "
                "VALUES (?,?,?,'synthetic',?,'parsed','[]',0,?,?,0,1,1,1,1,1,1,1,1)",
                ((f"c{i:06}", f"m{i:06}", f"t{i:06}", stamp, stamp, stamp) for i in range(size)),
            )
            # Folder and the file locator live on message_occurrences
            # since the occurrence cutover (#1567, #1568), not on
            # ``messages`` directly.
            conn.executemany(
                "INSERT INTO message_occurrences (account_id, folder, uniq, claimant_id, "
                "relative_path, seen, flagged, replied, trashed, first_observed_at, "
                "last_observed_at, state, provenance) "
                "VALUES (1, 'INBOX', ?, ?, ?, 0, 0, 0, 0, ?, ?, 'active', 'parse')",
                ((f"m{i:06}", f"c{i:06}", f"/synthetic/{i}", stamp, stamp) for i in range(size)),
            )
            conn.executemany(
                "INSERT INTO threads VALUES (?, 'synthetic')", ((f"t{i:06}",) for i in range(size))
            )
            raw = "Synthetic recurring bulletin <series.example>"
            conn.executemany(
                "INSERT INTO message_headers VALUES (?,0,'List-Id','list-id',?,NULL)",
                ((f"c{i:06}", raw) for i in range(size)),
            )
            body = " ".join(f"syntheticword{i}" for i in range(30))
            conn.executemany(
                "INSERT INTO message_chunks VALUES (?,?,?,0,?,0,?,30,'',?,NULL,'body')",
                (
                    (f"chunk{i}", f"c{i:06}", f"t{i:06}", body, len(body), i + 1)
                    for i in range(size)
                ),
            )
            conn.executemany(
                "INSERT INTO message_chunks_fts(rowid,text) VALUES (?,?)",
                ((i + 1, body) for i in range(size)),
            )
            conn.commit()
            work = {"connections": 0, "normalizations": 0, "bytes": 0}
            counted = False

            def normalize(name, encoded):
                if counted:
                    work["normalizations"] += 1
                    work["bytes"] += sum(len(value) for value in json.loads(encoded))
                return sqlite_normalize_fields(name, encoded)

            conn.create_function("mcp_list_metadata", 2, normalize, deterministic=True)

            class Reader:
                def execute(self, sql, params=()):
                    return conn.execute(sql, params)

                def rollback(self):
                    conn.rollback()

                def close(self):
                    conn.rollback()

            db = MemoryDatabase("/synthetic/unused")

            def connect():
                if counted:
                    work["connections"] += 1
                return Reader()

            db._connect = connect
            where = normalize_where(
                Where.model_validate({"all": [{"leaf": "list_id_is", "value": "series.example"}]})
            )

            def read_all():
                messages = db.query_messages(where=where, limit=1)
                threads = db.query_threads(where=where, limit=1)
                groups = db.aggregate_messages(
                    group_by="list_id", where=where, sample_per_group=1, limit=1
                )
                self.assertEqual(messages.total_matches, size)
                self.assertEqual(threads.total_matching_messages, size)
                self.assertEqual(groups.total_matches, size)
                self.assertEqual(groups.groups[0].messages, size)
                self.assertEqual(groups.samples_returned, 1)

            start = time.perf_counter()
            read_all()
            elapsed = time.perf_counter() - start
            counted = True
            read_all()
            self.assertEqual(work["connections"], 3)
            self.assertEqual(work["normalizations"], 9 * size)
            self.assertEqual(work["bytes"], 9 * size * len(raw))
            self.assertLess(elapsed, 90)
            measured.append(elapsed)
            print(
                json.dumps(
                    {
                        "sqlite": sqlite3.sqlite_version,
                        "messages": size,
                        "plain_seconds": elapsed,
                        **work,
                    }
                ),
                flush=True,
            )
        self.assertLess(measured[1], max(2, measured[0]) * 18)


unittest.main()
