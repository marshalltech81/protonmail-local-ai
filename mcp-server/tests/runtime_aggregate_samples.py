"""Actual aggregation reader on synthetic schema/rows, host and shipped SQLite.

Run directly with mcp-server on PYTHONPATH and scripts mounted at /app/scripts.
No mailbox volumes, network, provider calls or pytest dependency.
"""

import json
import sqlite3
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.reconcile_bench import _SCHEMA
from src.lib.sqlite import Database


class MemoryDatabase(Database):
    def _validate_path(self):
        # This test injects an in-memory connection, not a filesystem index.
        pass


class RuntimeAggregateSamples(unittest.TestCase):
    def test_sample_growth_and_selected_metadata(self):
        growth = {}
        for shape in ("balanced", "skew", "fanout"):
            for size in (5000, 50000):
                conn = sqlite3.connect(":memory:")
                self.addCleanup(conn.close)
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.row_factory = sqlite3.Row
                conn.executescript(_SCHEMA)
                conn.create_function("mcp_casefold", 1, str.casefold, deterministic=True)
                stamp = "2024-01-01T00:00:00+00:00"
                conn.executemany(
                    "INSERT INTO messages (claimant_id,message_id,thread_id,subject,"
                    "sent_at,sent_at_status,occurred_at,references_json,has_attachments,indexed_at,"
                    "first_indexed_at,sender_ambiguous,from_addresses_complete,to_addresses_complete,"
                    "cc_addresses_complete,attachments_manifest_complete,attachment_rows_current,body_complete,subject_complete) "
                    "VALUES (?,?,?, 'synthetic recurring bulletin',?,'parsed',?,'[]',0,?,?,0,1,1,1,1,1,1,1)",
                    (
                        (
                            f"c{i:06}",
                            f"m{i:06}",
                            f"t{i // 4}",
                            stamp,
                            stamp,
                            stamp,
                            stamp,
                        )
                        for i in range(size)
                    ),
                )
                # Folder and flags live on message_occurrences since the
                # occurrence cutover (#1567, #1568): aggregate_messages's
                # default_scope leaf (always compiled in without an
                # explicit folder filter) needs one active, non-trashed
                # occurrence per message, outside Trash/Spam, to count it.
                conn.executemany(
                    "INSERT INTO message_occurrences (account_id, folder, uniq, claimant_id, "
                    "relative_path, seen, flagged, replied, trashed, first_observed_at, "
                    "last_observed_at, state, provenance) "
                    "VALUES (1, 'INBOX', ?, ?, ?, 0, 0, 0, 0, ?, ?, 'active', 'parse')",
                    (
                        (f"m{i:06}", f"c{i:06}", f"INBOX/cur/m{i:06}:2,S", stamp, stamp)
                        for i in range(size)
                    ),
                )
                conn.executemany(
                    "INSERT INTO message_participants VALUES (?, 'from', ?, 'Synthetic')",
                    (
                        (
                            f"c{i:06}",
                            f"s{(0 if shape == 'skew' and i < size * 9 // 10 else i % 50):02}@example.test",
                        )
                        for i in range(size)
                    ),
                )
                text = " ".join(f"syntheticword{i}" for i in range(32))
                conn.executemany(
                    "INSERT INTO message_chunks_fts(rowid,text) VALUES (?,?)",
                    ((i + 1, text) for i in range(size)),
                )
                conn.executemany(
                    "INSERT INTO message_chunks (chunk_id,claimant_id,thread_id,chunk_index,text,char_start,char_end,token_est,chunked_at,fts_rowid,kind) VALUES (?,?,?,0,?,0,?,32,?,?, 'body')",
                    (
                        (f"k{i}", f"c{i:06}", f"t{i // 4}", text, len(text), stamp, i + 1)
                        for i in range(size)
                    ),
                )
                if shape == "fanout":
                    conn.executemany(
                        "INSERT INTO message_participants VALUES ('c000000','from',?,'Synthetic')",
                        ((f"fan{i:03}@example.test",) for i in range(100)),
                    )
                    conn.execute(
                        "UPDATE messages SET has_attachments=1 WHERE claimant_id='c000000'"
                    )
                    conn.executemany(
                        "INSERT INTO attachments (attachment_occurrence_id,claimant_id,attachment_id,thread_id,filename,content_type,size_bytes,seen_at) VALUES (?,'c000000',?,'t0','synthetic.pdf','application/pdf',1,?)",
                        ((f"o{i}", f"a{i}", stamp) for i in range(100)),
                    )
                conn.commit()
                counts = {}
                ticks = [0]

                class Reader:
                    def execute(self, sql, params):
                        sql = sql.replace(
                            "k.ok IS 1 AND k.value IS sp.value",
                            "sample_membership(k.ok) IS 1 AND k.value IS sp.value",
                        )
                        sql = sql.replace(
                            "hex(substr(CAST(m.subject AS BLOB), 1, 2001))",
                            "hex(sample_subject(substr(CAST(m.subject AS BLOB), 1, 2001)))",
                        )
                        counts["statements"] += 1
                        return conn.execute(sql, params)

                    def close(self):
                        pass

                def probe(key, value):
                    counts[key] += 1
                    return value

                conn.create_function("sample_membership", 1, lambda v: probe("memberships", v))
                conn.create_function("sample_subject", 1, lambda v: probe("subjects", v))
                db = MemoryDatabase("/synthetic/unused")

                def connect():
                    counts["connections"] += 1
                    return Reader()

                db._connect = connect
                for basis in ("effective", "sent", "occurred"):
                    for n in (0, 3):
                        counts.update(statements=0, connections=0, memberships=0, subjects=0)
                        ticks[0] = 0

                        def progress():
                            ticks[0] += 1000
                            return 0

                        conn.set_progress_handler(progress, 1000)
                        start = time.perf_counter()
                        page = db.aggregate_messages(
                            group_by="sender_address",
                            sample_per_group=n,
                            limit=10,
                            date_basis=basis,
                        )
                        elapsed = time.perf_counter() - start
                        conn.set_progress_handler(None, 0)
                        self.assertEqual(counts["statements"], 1)
                        self.assertEqual(counts["connections"], 1)
                        self.assertEqual(counts["subjects"], page.samples_returned)
                        self.assertEqual(page.samples_returned, 30 if n else 0)
                        self.assertLessEqual(counts["memberships"], 10 * (size + 100))
                        self.assertLess(elapsed, 30)
                        self.assertEqual(page.total_matches, size)
                        if n:
                            for group in page.groups:
                                ids = [s.claimant_id for s in group.samples]
                                self.assertEqual(ids, sorted(ids))
                                self.assertEqual(len(ids), len(set(ids)))
                        key = (shape, basis, n)
                        growth.setdefault(key, []).append(ticks[0])
                        print(
                            json.dumps(
                                dict(
                                    sqlite=sqlite3.sqlite_version,
                                    shape=shape,
                                    size=size,
                                    basis=basis,
                                    samples=n,
                                    seconds=round(elapsed, 4),
                                    steps=ticks[0],
                                    **counts,
                                )
                            ),
                            flush=True,
                        )
        for key, (small, large) in growth.items():
            self.assertLess(large, small * 18, key)


if __name__ == "__main__":
    unittest.main()
