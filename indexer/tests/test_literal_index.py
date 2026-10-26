"""Bounded indexing of synthetic full sources and current attachment text."""

import pytest
from src import literal

from tests.conftest import make_thread
from tests.test_full_body import parse


def store(db, text):
    message = parse(text.encode())
    db.upsert_thread(make_thread(messages=[message]), [0.0] * 4096)
    return message


def test_body_windows_share_source_fragments_and_overlap_boundaries(db):
    msg = store(db, "x" * 8190 + "A\x00ß猫" + "y" * 9000)
    windows = db._conn.execute(
        "SELECT ordinal,byte_count FROM literal_windows ORDER BY ordinal"
    ).fetchall()
    assert len(windows) == 3
    assert max(row[1] for row in windows) <= 36764
    assert db._conn.execute("SELECT body_complete FROM literal_state").fetchone()[0] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM literal_attachment_spans").fetchone()[0] == 0
    token = literal.gram_token("a\x00s")
    assert (
        db._conn.execute(
            "SELECT COUNT(*) FROM literal_long_fts WHERE literal_long_fts MATCH ?", (token,)
        ).fetchone()[0]
        == 1
    )
    assert msg.full_body is not None


@pytest.mark.parametrize(
    "dimension", ["MAX_WINDOWS", "MAX_BYTES", "MAX_METADATA", "MAX_MEMBERSHIP"]
)
def test_membership_budget_precedes_token_work_and_discloses_loss(
    db, monkeypatch, caplog, dimension
):
    monkeypatch.setattr(literal, dimension, 0 if dimension == "MAX_WINDOWS" else 1)
    monkeypatch.setattr(literal, "_tokens", lambda *a: pytest.fail("token construction ran"))
    store(db, "SYNTHETIC_PRIVATE_WORDS")
    assert db._conn.execute("SELECT COUNT(*) FROM literal_windows").fetchone()[0] == 0
    assert db._conn.execute("SELECT body_complete FROM literal_state").fetchone()[0] == 0
    assert "Literal index incomplete: windows=0" in caplog.text
    assert "SYNTHETIC_PRIVATE_WORDS" not in caplog.text


def test_reparse_and_cascade_remove_postings_and_schedule_scrub(db):
    msg = store(db, "first synthetic literal")
    token = literal.gram_token("fir")
    assert (
        db._conn.execute(
            "SELECT COUNT(*) FROM literal_long_fts WHERE literal_long_fts MATCH ?", (token,)
        ).fetchone()[0]
        == 1
    )
    with db.transaction():
        db._conn.execute("DELETE FROM message_body_parts WHERE claimant_id=?", (msg.claimant_id,))
    assert (
        db._conn.execute(
            "SELECT COUNT(*) FROM literal_long_fts WHERE literal_long_fts MATCH ?", (token,)
        ).fetchone()[0]
        == 0
    )
    assert db._conn.execute("SELECT COUNT(*) FROM literal_scrub").fetchone()[0] == 1
    assert set(literal.FTS_TABLES) <= set(db.scrub_reaped_fts())


def test_literal_replacement_rolls_back_postings_state_and_scrub_marker(db):
    msg = store(db, "original synthetic literal words")
    before = [tuple(r) for r in db._conn.execute("SELECT * FROM literal_windows")]
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with db.transaction():
            literal.rebuild_body(db._conn.cursor(), msg.claimant_id)
            raise RuntimeError("synthetic rollback")
    assert [tuple(r) for r in db._conn.execute("SELECT * FROM literal_windows")] == before
    assert db._conn.execute("SELECT COUNT(*) FROM literal_scrub").fetchone()[0] == 0
    token = literal.gram_token("ori")
    assert (
        db._conn.execute(
            "SELECT COUNT(*) FROM literal_long_fts WHERE literal_long_fts MATCH ?", (token,)
        ).fetchone()[0]
        == 1
    )


def test_v24_migration_queues_reparse_and_preserves_retrieval_evidence(tmp_path):
    from src.database import SCHEMA_VERSION, Database

    from tests.conftest import TEST_ACCOUNT, drop_literal_schema
    from tests.test_database import _schema_shape

    old = Database(tmp_path / "old.db", account=TEST_ACCOUNT)
    store(old, "migration synthetic source")
    before = [tuple(r) for r in old._conn.execute("SELECT * FROM messages")]
    drop_literal_schema(old._conn)
    old._conn.execute("UPDATE schema_version SET version=24")
    old._conn.commit()
    old.close()
    migrated = Database(tmp_path / "old.db", account=TEST_ACCOUNT)
    fresh = Database(tmp_path / "fresh.db", account=TEST_ACCOUNT)
    try:
        assert SCHEMA_VERSION == 29
        assert _schema_shape(migrated._conn) == _schema_shape(fresh._conn)
        assert [tuple(r) for r in migrated._conn.execute("SELECT * FROM messages")] == before
        assert migrated._conn.execute("SELECT COUNT(*) FROM literal_state").fetchone()[0] == 0
        assert (
            migrated._conn.execute(
                "SELECT COUNT(*) FROM indexing_jobs WHERE reason='reparse'"
            ).fetchone()[0]
            == 1
        )
    finally:
        migrated.close()
        fresh.close()


def test_window_admission_stops_source_reads_at_constant_work(db, monkeypatch):
    monkeypatch.setattr(literal, "MAX_WINDOWS", 1)
    measures = []
    for windows in (4, 40):
        msg = store(
            db,
            (
                "twenty ordinary synthetic words preserve realistic density in each bounded source fragment. "
                * 1000
            )[: windows * 8192],
        )
        copied = []
        original = db._conn.row_factory

        def row(cursor, values):
            copied.append(sum(len(value) for value in values if isinstance(value, bytes)))
            return original(cursor, values)

        db._conn.row_factory = row
        try:
            with db.transaction():
                literal.rebuild_body(db._conn.cursor(), msg.claimant_id)
        finally:
            db._conn.row_factory = original
        measures.append(sum(copied))
    assert measures[0] == measures[1]
    assert 0 < measures[1] <= 3 * 32768


def test_approved_membership_limit_is_sixteen_mib():
    assert literal.MAX_MEMBERSHIP == 16 * 1024 * 1024


@pytest.mark.parametrize("failure", ["low_space", "probe_failed"])
def test_storage_guard_precedes_token_and_posting_work(db, monkeypatch, caplog, failure):
    import os
    from types import SimpleNamespace

    def space(path):
        if failure == "probe_failed":
            raise OSError("SYNTHETIC_PRIVATE_WORDS")
        return SimpleNamespace(f_bavail=(1024**3 - 1) // 4096, f_frsize=4096)

    monkeypatch.setattr(os, "statvfs", space)
    monkeypatch.setattr(literal, "_tokens", lambda *a: pytest.fail("token construction ran"))
    store(db, "SYNTHETIC_PRIVATE_WORDS")
    assert db._conn.execute("SELECT COUNT(*) FROM literal_windows").fetchone()[0] == 0
    assert db._conn.execute("SELECT body_complete FROM literal_state").fetchone()[0] == 0
    assert "storage_limited=1" in caplog.text
    assert "SYNTHETIC_PRIVATE_WORDS" not in caplog.text


def test_storage_guard_rechecks_each_window_and_does_not_reset_for_next_message(db, monkeypatch):
    import os
    from types import SimpleNamespace

    probes, tokens = [], []
    available = 1024**3
    original = literal._tokens

    def space(path):
        probes.append(path)
        return SimpleNamespace(f_bavail=available, f_frsize=1)

    def construct(*args):
        nonlocal available
        tokens.append(1)
        available = 1024**3 - 1
        return original(*args)

    monkeypatch.setattr(os, "statvfs", space)
    monkeypatch.setattr(literal, "_tokens", construct)
    first = store(db, "SYNTHETIC_FIRST_MARKER " + "ordinary synthetic prose " * 1000)
    assert len(tokens) == 1
    assert len(probes) == 2
    assert db._conn.execute("SELECT COUNT(*) FROM literal_windows").fetchone()[0] == 1
    assert db._conn.execute("SELECT body_complete FROM literal_state").fetchone()[0] == 0
    # A current positive survives the stop; a new claimant gets no space bypass.
    token = literal.gram_token("syn")
    assert (
        db._conn.execute(
            "SELECT COUNT(*) FROM literal_long_fts WHERE literal_long_fts MATCH ?", (token,)
        ).fetchone()[0]
        == 1
    )
    second = store(db, "different synthetic message")
    assert second.claimant_id != first.claimant_id
    assert len(tokens) == 1
    assert (
        db._conn.execute(
            "SELECT body_complete FROM literal_state WHERE claimant_id=?", (second.claimant_id,)
        ).fetchone()[0]
        == 0
    )
