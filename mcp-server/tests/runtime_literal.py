"""Synthetic actual-writer/reader work and lifecycle gate on host and shipped SQLite."""

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

root = (
    Path(os.environ["LITERAL_ROOT"])
    if "LITERAL_ROOT" in os.environ
    else Path(__file__).resolve().parents[2]
)
sys.path.insert(0, str(root))
writer = importlib.import_module("indexer.src.literal")
body = importlib.import_module("indexer.src.full_body")
reader = importlib.import_module("mcp-server.src.lib.literal")


class Measured(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        if " MATCH " in sql:
            assert (
                len(parameters[0]) <= 192
            )  # A raw 1000-character positional phrase fails this gate.
            self.match_sizes.append(len(parameters[0]))
        return super().execute(sql, parameters)


def create_schema(conn):
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA secure_delete=ON")
    conn.executescript("""
    CREATE TABLE messages(claimant_id TEXT PRIMARY KEY,full_body_complete INTEGER,full_body_generation TEXT,full_body_version INTEGER,has_attachments INTEGER DEFAULT 0,attachments_manifest_complete INTEGER DEFAULT 1,attachment_rows_current INTEGER DEFAULT 1);
    CREATE TABLE attachments(attachment_occurrence_id TEXT PRIMARY KEY,claimant_id TEXT,attachment_id TEXT,extractor_module TEXT,text_extractor TEXT,text_complete INTEGER,extraction_deferred_at TEXT);
    CREATE TABLE attachment_extractions(attachment_id TEXT,extractor_module TEXT,extracted_at TEXT,extractor TEXT,extracted_text TEXT);
    """)
    conn.executescript(body.SCHEMA_SQL)
    writer.create_schema(conn.cursor())


def measure(shape, count):
    with tempfile.TemporaryDirectory(prefix="literal-runtime-") as scratch:
        path = Path(scratch) / "synthetic.db"
        conn = sqlite3.connect(path, factory=Measured)
        conn.match_sizes = []
        try:
            create_schema(conn)
            unit = "Synthetic prose includes more than twenty ordinary words per passage and preserves exact punctuation, spaces, identifiers, dates, quotes, boundaries and evidence for this repeatable indexing performance measurement. "
            text = (unit * 50)[:8192]
            needle = "ordinary words per passage"
            if shape == "repeated":
                text = (("a" * 1200 + " " + unit) * 8)[:8192]
                needle = "a" * 1000
            elif shape == "misaligned":
                text = ("ΐ" * 7000 + unit * 10)[:8192]
                needle = ("\u0308\u0301ι" * 334)[:1000]
            elif shape == "unicode":
                text = (unit + " Straße 猫犬鳥 \x00 ") * 40
                text = text[:8192]
                needle = "strasse 猫犬鳥 \x00"
            build_started = time.perf_counter()
            for i in range(count):
                cid = f"synthetic-{i}"
                conn.execute("INSERT INTO messages(claimant_id) VALUES(?)", (cid,))
                capture = body.prepare([body.Part(0, -1, "plain", True, text)], cid)
                body.store(conn.cursor(), cid, capture)
                writer.rebuild_body(conn.cursor(), cid)
            conn.commit()
            build = time.perf_counter() - build_started
            checks = []
            actual_contains = reader.contains

            def counted(*args):
                checks.append(len(args[0].encode()))
                return actual_contains(*args)

            reader.contains = counted
            transferred = []

            def row(cursor, values):
                transferred.append(sum(len(x) for x in values if isinstance(x, bytes)))
                return values

            conn.row_factory = row
            where = [
                type(
                    "W",
                    (),
                    {"leaf": type("L", (), {"name": "body_phrase", "value": needle.casefold()})()},
                )()
            ]
            started = time.perf_counter()
            try:
                with reader.QueryBudget(conn) as budget:
                    budget.prepare(where)
                    params = []
                    sql = reader.compile_phrase("b", needle.casefold(), params)
                    for _ in range(3):
                        assert conn.execute(
                            "SELECT COUNT(*) FROM messages m WHERE " + sql, params
                        ).fetchone()[0] == (0 if shape == "misaligned" else count)
                    candidates, charged = budget.candidates, budget.bytes
            finally:
                reader.contains = actual_contains
            elapsed = time.perf_counter() - started
            assert elapsed < 10
            assert len(checks) == count
            assert candidates == count
            assert sum(transferred) <= charged <= reader.MAX_VERIFICATION_BYTES
            assert len(conn.match_sizes) == 1
            conn.row_factory = None
            conn.execute("DELETE FROM messages")
            conn.commit()
            for table in writer.FTS_TABLES:
                conn.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
                assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM literal_windows").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM literal_scrub").fetchone()[0] == 1
            print(
                json.dumps(
                    {
                        "shape": shape,
                        "messages": count,
                        "source_bytes": len(text.encode()) * count,
                        "build_seconds": build,
                        "query_seconds": elapsed,
                        "verified_windows": len(checks),
                        "transfer_bytes": sum(transferred),
                        "charged_bytes": charged,
                        "match_operands": len(conn.match_sizes),
                        "max_match_chars": max(conn.match_sizes),
                        "sqlite": sqlite3.sqlite_version,
                        "db_bytes": path.stat().st_size,
                        "wal_bytes": Path(str(path) + "-wal").stat().st_size,
                    }
                ),
                flush=True,
            )
        finally:
            conn.close()


def measure_cap():
    state = 731
    unit = "Synthetic prose has more than twenty ordinary words and preserves a realistic source density for bounding the literal index construction work of a large document. "
    blocks = []
    for _ in range(1400):
        chars = []
        for _ in range(8192 - len(unit)):
            state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
            chars.append(chr(0x4E00 + state % 20000))
        blocks.append(unit + "".join(chars))
    text = "".join(blocks)
    del blocks
    with tempfile.TemporaryDirectory(prefix="literal-cap-runtime-") as scratch:
        path = Path(scratch) / "synthetic.db"
        conn = sqlite3.connect(path)
        try:
            create_schema(conn)
            conn.execute("INSERT INTO messages(claimant_id) VALUES('synthetic')")
            capture = body.prepare([body.Part(0, -1, "plain", True, text)], "synthetic")
            body.store(conn.cursor(), "synthetic", capture)
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            before = path.stat().st_size
            constructed = []
            original = writer._tokens

            def counted(*args):
                constructed.append(1)
                return original(*args)

            writer._tokens = counted
            started = time.perf_counter()
            try:
                writer.rebuild_body(conn.cursor(), "synthetic")
            finally:
                writer._tokens = original
            conn.commit()
            elapsed = time.perf_counter() - started
            complete, windows, source, metadata, membership = conn.execute(
                "SELECT body_complete,body_windows,body_bytes,body_metadata,body_membership FROM literal_state"
            ).fetchone()
            assert capture.complete and complete == 0
            assert (
                windows
                == conn.execute("SELECT COUNT(*) FROM literal_windows").fetchone()[0]
                <= writer.MAX_WINDOWS
            )
            assert (
                source <= writer.MAX_BYTES
                and metadata <= writer.MAX_METADATA
                and membership <= writer.MAX_MEMBERSHIP
            )
            assert writer.MAX_MEMBERSHIP == 16 * 1024 * 1024
            assert len(constructed) == windows <= 32
            added = (
                conn.execute("PRAGMA page_count").fetchone()[0]
                * conn.execute("PRAGMA page_size").fetchone()[0]
                - before
            )
            wal = Path(str(path) + "-wal").stat().st_size
            assert added + wal <= 32 * 1024 * 1024
            assert elapsed < 10
            print(
                json.dumps(
                    {
                        "shape": "capped",
                        "messages": 1,
                        "source_bytes": len(text.encode()),
                        "index_seconds": elapsed,
                        "body_complete": complete,
                        "windows": windows,
                        "membership_bytes": membership,
                        "constructed_windows": len(constructed),
                        "added_database_bytes": added,
                        "db_bytes": path.stat().st_size,
                        "wal_bytes": Path(str(path) + "-wal").stat().st_size,
                        "sqlite": sqlite3.sqlite_version,
                    }
                ),
                flush=True,
            )
        finally:
            conn.close()


def measure_storage_guard():
    with tempfile.TemporaryDirectory(prefix="literal-storage-runtime-") as scratch:
        path = Path(scratch) / "synthetic.db"
        conn = sqlite3.connect(path)
        original = writer.MIN_FREE_BYTES
        try:
            create_schema(conn)
            conn.execute("INSERT INTO messages(claimant_id) VALUES('synthetic')")
            capture = body.prepare(
                [body.Part(0, -1, "plain", True, "synthetic private marker")], "synthetic"
            )
            body.store(conn.cursor(), "synthetic", capture)
            conn.commit()
            # Exercise the real statvfs path without filling a filesystem.
            space = os.statvfs(path)
            assert original == 1024**3
            writer.MIN_FREE_BYTES = space.f_bavail * space.f_frsize + 1
            writer.rebuild_body(conn.cursor(), "synthetic")
            conn.commit()
            assert conn.execute("SELECT COUNT(*) FROM literal_windows").fetchone()[0] == 0
            assert conn.execute("SELECT body_complete FROM literal_state").fetchone()[0] == 0
            where = [
                type(
                    "W", (), {"leaf": type("L", (), {"name": "body_phrase", "value": "marker"})()}
                )()
            ]
            with reader.QueryBudget(conn) as budget:
                budget.prepare(where)
                params = []
                predicate = reader.compile_phrase("b", "marker", params)
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM messages m WHERE (" + predicate + ") IS NULL", params
                    ).fetchone()[0]
                    == 1
                )
            print(
                json.dumps({"shape": "storage_limited", "windows": 0, "body_complete": 0}),
                flush=True,
            )
        finally:
            writer.MIN_FREE_BYTES = original
            conn.close()


if __name__ == "__main__":
    for shape in ("prose", "repeated", "unicode", "misaligned"):
        for count in (40, 400):
            measure(shape, count)

    measure_cap()
    measure_storage_guard()
