"""Actual full-source storage/read work and lifecycle on shipped SQLite (#1362)."""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

root = (
    Path(os.environ["FULL_BODY_ROOT"])
    if "FULL_BODY_ROOT" in os.environ
    else Path(__file__).resolve().parents[2]
)
sys.path.insert(0, str(root / "indexer"))
from src import full_body  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "body_reader", root / "mcp-server/src/lib/full_body.py"
)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


def measure(shape: str, key_length: int) -> None:
    unit = (
        "Synthetic original prose preserves all forty ordinary words for complete reading, punctuation and whitespace. "
        * 4
        + "\r\n"
    )
    target = full_body.MAX_SOURCE_BYTES
    if shape == "ranges":
        text = (unit * (target // len(unit) + 1))[: target - 1024 * 1024]
        text += ("> synthetic quote\r\nordinary inline answer\r\n" * 30000)[: 1024 * 1024]
    elif shape == "unicode":
        text = "😀" * (target // 4)
    else:
        text = (unit * (target // len(unit) + 1))[:target]
    claimant = "a" * key_length
    with tempfile.TemporaryDirectory(prefix="full-body-runtime-") as scratch:
        path = Path(scratch) / "mail.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "CREATE TABLE messages(claimant_id TEXT PRIMARY KEY,full_body_complete INTEGER,full_body_generation TEXT,full_body_version INTEGER)"
        )
        conn.executescript(full_body.SCHEMA_SQL)
        conn.execute("INSERT INTO messages(claimant_id) VALUES (?)", (claimant,))
        conn.commit()
        started = time.perf_counter()
        capture = full_body.prepare([full_body.Part(0, -1, "plain", True, text)], claimant)
        with conn:
            full_body.store(conn.cursor(), claimant, capture)
        elapsed = time.perf_counter() - started
        generation = conn.execute("SELECT full_body_generation FROM messages").fetchone()[0]
        fragments = conn.execute(
            "SELECT COUNT(*),SUM(length(data)) FROM message_body_spans"
        ).fetchone()
        ranges = conn.execute("SELECT COUNT(*) FROM message_body_ranges").fetchone()[0]
        assert capture.metadata_bytes <= full_body.MAX_METADATA_BYTES
        assert fragments[0] <= full_body.MAX_FRAGMENTS
        assert fragments[1] <= full_body.MAX_SOURCE_BYTES
        assert ranges <= full_body.MAX_CLASSIFICATION_RUNS
        bad_interior = conn.execute(
            "SELECT COUNT(*) FROM message_body_spans WHERE end-start!=8192 AND ordinal!=(SELECT MAX(ordinal) FROM message_body_spans)"
        ).fetchone()[0]
        assert bad_interior == 0
        page = reader.read_page(conn, claimant, generation, 0)
        delivered = sum(len(row["text"].encode()) for row in page)
        assert len(page) <= 8 and delivered <= 256 * 1024
        assert elapsed < full_body.FINALIZE_RESERVE_SECONDS, (shape, elapsed)
        db_bytes = path.stat().st_size
        wal_bytes = Path(str(path) + "-wal").stat().st_size
        started = time.perf_counter()
        with conn:
            conn.execute("DELETE FROM messages WHERE claimant_id=?", (claimant,))
        deletion = time.perf_counter() - started
        assert conn.execute("SELECT COUNT(*) FROM message_body_parts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM message_body_spans").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM message_body_ranges").fetchone()[0] == 0
        print(
            json.dumps(
                dict(
                    sqlite=sqlite3.sqlite_version,
                    shape=shape,
                    key_bytes=key_length,
                    seconds=elapsed,
                    fragment_rows=fragments[0],
                    stored_bytes=fragments[1],
                    range_rows=ranges,
                    metadata_charged_bytes=capture.metadata_bytes,
                    read_bytes=delivered,
                    db_bytes=db_bytes,
                    wal_bytes=wal_bytes,
                    delete_seconds=deletion,
                )
            ),
            flush=True,
        )
        conn.close()


if __name__ == "__main__":
    for shape, key in (("plain", 97), ("unicode", 97), ("ranges", 1032), ("plain", 4025)):
        measure(shape, key)
