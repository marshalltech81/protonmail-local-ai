import hashlib
import sqlite3
from pathlib import Path

import pytest
from src.lib.full_body import BodySourceError, read_metadata, read_page, read_ranges


@pytest.fixture
def source():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE messages(claimant_id TEXT PRIMARY KEY,full_body_generation TEXT,full_body_version INTEGER)"
    )
    migration = (
        Path(__file__).resolve().parents[2] / "indexer/src/migrations/0018_full_decoded_body.sql"
    ).read_text()
    connection.executescript(
        migration[migration.index("CREATE TABLE") : migration.index("INSERT INTO indexing_jobs")]
    )
    generation = hashlib.sha256(b"synthetic-source-generation").hexdigest()
    connection.execute("INSERT INTO messages VALUES ('c',?,1)", (generation,))
    connection.execute(
        "INSERT INTO message_body_parts VALUES ('c',0,-1,'plain',1,0,73728,294912,?,73728)",
        (generation,),
    )
    for ordinal in range(9):
        connection.execute(
            "INSERT INTO message_body_spans VALUES ('c',0,?,?,?,?)",
            (ordinal, ordinal * 8192, (ordinal + 1) * 8192, ("😀" * 8192).encode()),
        )
    connection.execute("INSERT INTO message_body_ranges VALUES ('c',0,0,73728,'body')")
    yield connection, generation
    connection.close()


def test_read_page_fetches_at_most_eight_complete_codepoint_fragments(source):
    connection, generation = source
    page = read_page(connection, "c", generation, 0)
    assert len(page) == 8
    assert sum(len(row["text"].encode()) for row in page) == 256 * 1024
    assert len(read_page(connection, "c", generation, 0, page[-1]["ordinal"])) == 1
    assert read_metadata(connection, "c", generation)[0]["source_bytes"] == 294912
    assert read_ranges(connection, "c", generation, 0)[0]["kind"] == "body"


def test_generation_change_is_rejected_before_fragment_transfer(source):
    connection, generation = source
    connection.execute("UPDATE messages SET full_body_generation=?", ("f" * 64,))
    with pytest.raises(BodySourceError, match="generation"):
        read_page(connection, "c", generation, 0)


def test_sql_guard_withholds_forged_oversized_blob_before_python_decode(source):
    connection, generation = source
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "UPDATE message_body_spans SET data=? WHERE ordinal=0", (b"PRIVATE_BODY_MARKER" * 100000,)
    )
    transferred = []

    def record_bytes(_cursor, row):
        transferred.append(sum(len(value) for value in row if isinstance(value, bytes)))
        return row

    connection.row_factory = record_bytes
    with pytest.raises(BodySourceError, match="bound") as error:
        read_page(connection, "c", generation, 0)
    assert "PRIVATE_BODY_MARKER" not in str(error.value)
    # Other bounded page fragments may transfer; the oversized forged one does not.
    assert sum(transferred) == 7 * 32768
    transferred.clear()
    connection.execute("SELECT data FROM message_body_spans WHERE ordinal=0").fetchone()
    assert sum(transferred) == 1900000  # The same guard-free shape fails the transfer gate.


@pytest.mark.parametrize("limit", [0, 9, 100000])
def test_read_limit_is_a_fixed_guard(source, limit):
    connection, generation = source
    with pytest.raises(BodySourceError, match="bounds"):
        read_page(connection, "c", generation, 0, limit=limit)
