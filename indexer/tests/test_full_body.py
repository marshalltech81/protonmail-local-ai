"""Complete source evidence is additive to the existing retrieval representation."""

import hashlib
import sqlite3

import pytest
from src import full_body
from src.parser import SourceMetadata, parse_email_bytes


def parse(body: bytes, headers: bytes = b"Content-Type: text/plain; charset=utf-8\r\n"):
    return parse_email_bytes(
        b"Message-ID: <full-body@example.test>\r\n" + headers + b"\r\n" + body,
        SourceMetadata(
            folder="INBOX",
            path="/synthetic/cur/body:2,",
            flags=frozenset(),
            size=None,
            mtime_ns=None,
        ),
    )


def test_original_body_preserves_quotes_whitespace_and_nul():
    original = (
        "  reply\r\n\r\nOn today <a@example.test>\r\nwrote:\r\n> quote\r\n-- \r\nsignature\x00\r\n"
    )
    message = parse(original.encode())
    assert message is not None
    assert message.body_text == original.strip()
    capture = message.full_body
    assert capture is not None and capture.complete
    assert capture.parts[0].text == original
    ranges = list(full_body.classify(original))
    assert "".join(original[start:end] for start, end, _ in ranges) == original
    assert {kind for _, _, kind in ranges} >= {"body", "quote", "signature"}


def test_alternative_sources_are_independent_and_selection_is_retained():
    body = b"--b\r\nContent-Type: text/plain\r\n\r\n  plain  \r\n--b\r\nContent-Type: text/plain\r\n\r\n other \r\n--b--\r\n"
    message = parse(body, b"Content-Type: multipart/alternative; boundary=b\r\n")
    assert message is not None and message.body_text == "plain"
    sources = [part for part in message.full_body.parts if part.text is not None]
    assert [p.text for p in sources] == ["  plain  ", " other "]
    assert [p.selected for p in sources] == [True, False]
    assert [p.parent for p in sources] == [0, 0]


def test_source_byte_and_metadata_guards_precede_repeated_work(monkeypatch):
    monkeypatch.setattr(full_body, "MAX_SOURCE_BYTES", 9)
    capture = full_body.prepare([full_body.Part(0, -1, "plain", True, "😀😀😀")], "c")
    assert capture.parts[0].text == "😀😀"
    assert capture.source_bytes == 8 and not capture.complete
    monkeypatch.setattr(full_body, "MAX_METADATA_BYTES", 1)
    capture = full_body.prepare([full_body.Part(0, -1, "plain", True, "private")], "c")
    assert not capture.parts and not capture.complete and capture.metadata_bytes == 0


def test_fragments_are_codepoint_aligned_and_bounded():
    text = "😀" * (full_body.FRAGMENT_CHARS + 1)
    fragments = list(full_body.fragments(text))
    assert [len(data) for _, _, data in fragments] == [32768, 4]
    assert b"".join(data for _, _, data in fragments).decode() == text


def test_capped_source_keeps_gapfree_full_interior_fragments(monkeypatch):
    monkeypatch.setattr(full_body, "MAX_SOURCE_BYTES", 32768 + 7)
    capture = full_body.prepare([full_body.Part(0, -1, "plain", True, "😀" * 20000)], "c")
    rows = list(full_body.fragments(capture.parts[0].text))
    assert [(start, end) for start, end, _ in rows] == [(0, 8192), (8192, 8193)]
    assert sum(len(data) for _, _, data in rows) == 32772
    assert not capture.complete


def test_original_deadline_skips_only_additive_html_pass(monkeypatch):
    calls = []
    monkeypatch.setattr(
        full_body, "run_child", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setattr(full_body.time, "monotonic", lambda: 100.0)
    assert full_body.convert_html(["<p>body</p>"], 110.0) == ([], True, [full_body.DEADLINE_LOSS])
    assert not calls


def test_full_html_failure_preserves_existing_legacy_result(monkeypatch, caplog):
    from src.extractors._runner import ChildError

    monkeypatch.setattr(
        full_body, "convert_html", lambda *args: (_ for _ in ()).throw(ChildError("ValueError"))
    )
    message = parse(b"<p>PRIVATE_SYNTHETIC_BODY</p>", b"Content-Type: text/html\r\n")
    assert message.body_text == "PRIVATE_SYNTHETIC_BODY"
    assert not message.full_body.complete
    assert "PRIVATE_SYNTHETIC_BODY" not in caplog.text


def test_complete_body_commit_is_atomic_and_cascades(db):
    from src.database import EMBEDDING_DIM

    from tests.conftest import make_thread

    message = parse(b"source\r\n> quote\r\n")
    thread = make_thread(messages=[message], thread_id="body-thread")
    db.upsert_thread(thread, [0.0] * EMBEDDING_DIM)
    generation = db._conn.execute("SELECT full_body_generation FROM messages").fetchone()[0]
    assert db._conn.execute("SELECT full_body_complete FROM messages").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        with db._conn:
            full_body.store(
                db._conn.cursor(),
                message.claimant_id,
                full_body.prepare(
                    [full_body.Part(0, -1, "invalid", True, "changed")], message.claimant_id
                ),
            )
    assert db._conn.execute("SELECT full_body_generation FROM messages").fetchone()[0] == generation
    assert (
        db._conn.execute("SELECT data FROM message_body_spans").fetchone()[0]
        == b"source\r\n> quote\r\n"
    )
    db._conn.execute("DELETE FROM message_thread_map WHERE claimant_id=?", (message.claimant_id,))
    assert db._conn.execute("SELECT COUNT(*) FROM message_body_parts").fetchone()[0] == 0


def test_full_body_migration_leaves_old_sources_unknown_and_queues_reparse(tmp_path):
    from pathlib import Path

    from src.database import EMBEDDING_DIM, Database
    from src.queue import REPARSE_ENQUEUE_SQL

    from tests.conftest import TEST_ACCOUNT, make_thread

    path = tmp_path / "upgrade.db"
    db = Database(path, account=TEST_ACCOUNT)
    message = parse(b"original synthetic body")
    db.upsert_thread(make_thread(messages=[message], thread_id="upgrade"), [0.0] * EMBEDDING_DIM)
    for table in ("message_body_ranges", "message_body_spans", "message_body_parts"):
        db._conn.execute(f"DROP TABLE {table}")
    for column in ("full_body_complete", "full_body_generation", "full_body_version"):
        db._conn.execute(f"ALTER TABLE messages DROP COLUMN {column}")
    db._conn.execute("UPDATE schema_version SET version=17")
    db._conn.commit()
    db.close()
    upgraded = Database(path, account=TEST_ACCOUNT)
    try:
        assert tuple(
            upgraded._conn.execute(
                "SELECT full_body_complete,full_body_generation,full_body_version FROM messages"
            ).fetchone()
        ) == (None, None, None)
        assert upgraded._conn.execute("SELECT reason FROM indexing_jobs").fetchone()[0] == "reparse"
        assert upgraded._conn.execute("SELECT COUNT(*) FROM message_body_parts").fetchone()[0] == 0
        assert (
            REPARSE_ENQUEUE_SQL
            in (Path(__file__).parents[1] / "src/migrations/0018_full_decoded_body.sql").read_text()
        )
    finally:
        upgraded.close()


def test_queued_reparse_preserves_chunks_without_embedding_again(tmp_path):
    from src.database import Database
    from src.queue import REASON_INITIAL_SCAN, REASON_REPARSE
    from src.threader import Threader

    from tests.conftest import TEST_ACCOUNT, make_mock_embedder
    from tests.test_main import _drain, _make_queue, _write_eml

    path = tmp_path / "INBOX/cur/full.eml"
    _write_eml(path, "source-reparse@example.test")
    db = Database(tmp_path / "mail.db", account=TEST_ACCOUNT)
    queue = _make_queue(db)
    embedder = make_mock_embedder([0.1] * 4096)
    threader = Threader(db)
    try:
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        _drain(queue, db, embedder, threader, max_passes=1)
        before = [
            tuple(row) for row in db._conn.execute("SELECT chunk_id,text FROM message_chunks")
        ]
        calls = embedder.embed.call_count
        assert calls and before
        queue.enqueue(str(path), REASON_REPARSE)
        _drain(queue, db, embedder, threader, max_passes=1)
        assert embedder.embed.call_count == calls
        assert [
            tuple(row) for row in db._conn.execute("SELECT chunk_id,text FROM message_chunks")
        ] == before
        assert db._conn.execute("SELECT full_body_complete FROM messages").fetchone()[0] == 1
        assert db._conn.execute("SELECT COUNT(*) FROM message_body_spans").fetchone()[0] > 0
    finally:
        db.close()


def test_parent_capture_memory_pressure_preserves_the_inflight_attempt(tmp_path, monkeypatch):
    from src.database import Database
    from src.queue import REASON_INITIAL_SCAN
    from src.threader import Threader

    from tests.conftest import TEST_ACCOUNT, make_mock_embedder
    from tests.test_main import _drain, _make_queue, _write_eml

    path = tmp_path / "INBOX/cur/pressure.eml"
    _write_eml(path, "pressure@example.test")
    db = Database(tmp_path / "mail.db", account=TEST_ACCOUNT)
    queue = _make_queue(db)
    queue.enqueue(str(path), REASON_INITIAL_SCAN)

    def fail(*_args, **_kwargs):
        raise MemoryError("PRIVATE_SYNTHETIC_BODY")

    monkeypatch.setattr(full_body, "prepare", fail)
    try:
        with pytest.raises(MemoryError):
            _drain(queue, db, make_mock_embedder(), Threader(db), max_passes=1)
        row = db._conn.execute("SELECT attempts,last_error FROM indexing_jobs").fetchone()
        assert row[0] == 1
        assert "PRIVATE_SYNTHETIC_BODY" not in (row[1] or "")
    finally:
        db.close()


def test_classifier_stops_before_range_and_line_work_caps(monkeypatch):
    monkeypatch.setattr(full_body, "MAX_CLASSIFIED_LINES", 3)
    monkeypatch.setattr(full_body, "MAX_CLASSIFICATION_RUNS", 2)
    ranges = list(full_body.classify("> q\r\nreply\r\n> q\r\nreply\r\n"))
    assert len(ranges) == 2
    assert ranges[-1][1] == len("> q\r\nreply\r\n")


def test_stored_sources_and_ranges_cascade_and_generation_is_content_bound(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        "CREATE TABLE messages(claimant_id TEXT PRIMARY KEY, full_body_complete INTEGER, full_body_generation TEXT, full_body_version INTEGER)"
    )
    connection.executescript(full_body.SCHEMA_SQL)
    connection.execute("INSERT INTO messages(claimant_id) VALUES ('c')")
    capture = full_body.prepare([full_body.Part(0, -1, "plain", True, "body\r\n> quote\r\n")], "c")
    full_body.store(connection.cursor(), "c", capture)
    rows = connection.execute("SELECT data FROM message_body_spans ORDER BY ordinal").fetchall()
    assert b"".join(row[0] for row in rows) == b"body\r\n> quote\r\n"
    first_generation = connection.execute("SELECT full_body_generation FROM messages").fetchone()[0]
    assert len(first_generation) == hashlib.sha256().digest_size * 2
    original = "body\r\n> quote\r\n"
    monkeypatch.setattr(
        full_body, "classify", lambda text, **kwargs: iter([(0, len(text), "body")])
    )
    full_body.store(
        connection.cursor(),
        "c",
        full_body.prepare([full_body.Part(0, -1, "plain", True, original)], "c"),
    )
    body_generation = connection.execute("SELECT full_body_generation FROM messages").fetchone()[0]
    monkeypatch.setattr(
        full_body, "classify", lambda text, **kwargs: iter([(0, len(text), "quote")])
    )
    full_body.store(
        connection.cursor(),
        "c",
        full_body.prepare([full_body.Part(0, -1, "plain", True, original)], "c"),
    )
    assert (
        connection.execute("SELECT full_body_generation FROM messages").fetchone()[0]
        != body_generation
    )
    full_body.store(connection.cursor(), "c", full_body.prepare([], "c"))
    assert connection.execute("SELECT COUNT(*) FROM message_body_spans").fetchone()[0] == 0
    assert (
        connection.execute("SELECT full_body_generation FROM messages").fetchone()[0]
        != first_generation
    )
    connection.execute("DELETE FROM messages")
    assert connection.execute("SELECT COUNT(*) FROM message_body_parts").fetchone()[0] == 0
    connection.close()


def test_html_source_preserves_converter_whitespace_without_changing_legacy():
    message = parse(b"<pre>  indented\n</pre>", b"Content-Type: text/html\r\n")
    assert message is not None
    source = next(p for p in message.full_body.parts if p.text is not None)
    assert source.text.startswith("\n") and source.text.endswith("\n")
    assert source.text.strip() == message.body_text


@pytest.mark.parametrize("payload", [b"/w==", b"YWJj$"])
def test_decoding_loss_has_independent_source_provenance(payload):
    message = parse(
        payload, b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n"
    )
    assert message is not None and not message.full_body.complete
    assert any(p.losses for p in message.full_body.parts)
