"""Bound values crossing SQLite's boundary, not just the displayed lists."""

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import replace

import pytest
from src.lib import sqlite as readers
from src.tools.outputs import message_headers
from src.tools.retrieval import _header_lines, _listed_lines

from tests.conftest import claimant_of


@pytest.mark.parametrize("reader", ["query", "thread", "message"])
def test_header_reads_bound_transferred_lists_and_keep_exact_counts(
    messages_db, monkeypatch, reader
):
    claimant = claimant_of("m1")
    references = [f"ref-{i}" for i in range(100_000)]
    with closing(sqlite3.connect(messages_db.path)) as writer:
        writer.execute(
            "UPDATE messages SET references_json = ?, subject = 'bounded headers' WHERE claimant_id = ?",
            (json.dumps(references), claimant),
        )
        writer.execute("DELETE FROM message_participants WHERE claimant_id = ?", (claimant,))
        writer.executemany(
            "INSERT INTO message_participants (claimant_id, role, address, name) VALUES (?, ?, ?, ?)",
            [
                (claimant, role, f"{role}-{i}@example.com", f"Name {i}")
                for role, n in (("from", 20), ("to", 9_970), ("cc", 10))
                for i in range(n)
            ],
        )
        writer.commit()

    transferred = {"participants": 0, "references": 0}
    connect = messages_db._connect
    convert = readers._row_to_message_record

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def __iter__(self):
            return iter(self.cursor)

        def fetchall(self):
            rows = self.cursor.fetchall()
            columns = {column[0] for column in self.cursor.description}
            if {"role", "address"} <= columns:
                transferred["participants"] += len(rows)
            return rows

    class Connection:
        def __init__(self):
            self.connection = connect()

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, *args):
            return Cursor(self.connection.execute(*args))

    def bounded_convert(row):
        if row["claimant_id"] == claimant:
            transferred["references"] += len(json.loads(row["references_json"]))
        return convert(row)

    monkeypatch.setattr(messages_db, "_connect", Connection)
    monkeypatch.setattr(readers, "_row_to_message_record", bounded_convert)
    started = time.monotonic()
    if reader == "query":
        record = messages_db.query_messages(subject="bounded headers", limit=1).messages[0]
    elif reader == "thread":
        record = messages_db.get_thread_page("t1", offset=0, limit=1, body_char_limit=100).messages[
            0
        ]
    else:
        record = messages_db.get_message_view(claimant).record

    assert time.monotonic() - started < 10
    assert record.claimant_id == claimant
    assert transferred == {"participants": 30, "references": 10}
    assert record.references == references[:10]
    assert [p.address for p in record.to] == [f"to-{i}@example.com" for i in range(10)]
    output = message_headers(record)
    assert (output.references_count, output.from_count, output.to_count, output.cc_count) == (
        100_000,
        20,
        9_970,
        10,
    )
    assert all(
        len(values) == 10 for values in (output.references, output.from_, output.to, output.cc)
    )
    prose = "\n".join(_header_lines(record))
    assert "(+99990 more)" in prose
    assert "(+9960 more)" in prose
    assert "(+10 more)" in "\n".join(_listed_lines(1, record, None))


def test_batched_headers_keep_order_counts_and_empty_roles_per_claimant(messages_db):
    references = ['quote"', "雪\x00😀", "back\\slash"] + [f"ref-{i}" for i in range(22)]
    with closing(sqlite3.connect(messages_db.path)) as writer:
        for message_id, count in (("m1", 23), ("m2", 17)):
            claimant = claimant_of(message_id)
            writer.execute(
                "UPDATE messages SET folder = 'Header tests', references_json = ? WHERE claimant_id = ?",
                (json.dumps(references if message_id == "m1" else []), claimant),
            )
            writer.execute("DELETE FROM message_participants WHERE claimant_id = ?", (claimant,))
            writer.executemany(
                "INSERT INTO message_participants (claimant_id, role, address, name) VALUES (?, 'to', ?, NULL)",
                [(claimant, f"{message_id}-{i:03}@example.com") for i in reversed(range(count))],
            )
        writer.commit()

    records = messages_db.query_messages(folder="Header tests").messages
    assert [record.message_id for record in records] == ["m2", "m1"]
    for record, count in zip(records, (17, 23), strict=True):
        headers = message_headers(record)
        assert headers.to_count == count
        assert headers.from_count == headers.cc_count == 0
        assert headers.from_ == headers.cc == []
        assert [person.address for person in headers.to] == [
            f"{record.message_id}-{i:03}@example.com" for i in range(count - 1, count - 11, -1)
        ]
    assert records[0].references == []
    assert message_headers(records[0]).references_count == 0
    assert records[1].references == references[:10]
    assert message_headers(records[1]).references_count == len(references)


def test_participant_window_does_not_visit_other_claimants(messages_db):
    record = messages_db.get_message_view(claimant_of("m1")).record

    def work():
        steps = 0

        def progress():
            nonlocal steps
            steps += 1
            return 0

        with closing(messages_db._connect()) as connection:
            connection.set_progress_handler(progress, 1)
            bounded = replace(record, from_=[], to=[], cc=[])
            readers._attach_participants(connection, [bounded])
            assert bounded.from_count == bounded.to_count == 1
            assert bounded.cc_count == 0
        return steps

    before = work()
    with closing(sqlite3.connect(messages_db.path)) as writer:
        writer.execute(
            "DELETE FROM message_participants WHERE claimant_id = ?", (claimant_of("m2"),)
        )
        writer.executemany(
            "INSERT INTO message_participants (claimant_id, role, address, name) VALUES (?, 'to', ?, NULL)",
            [(claimant_of("m2"), f"other-{i}@example.com") for i in range(10_000)],
        )
        writer.commit()
    assert work() <= before + 50
