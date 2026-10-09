"""Keeps the #1218 sizing benchmark (``scripts/reconcile_bench.py``)
runnable and its measurements honest, at a size that runs in seconds."""

import importlib.util
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconcile_bench.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("reconcile_bench", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("identity", ["typical", "ascii998", "utf8x4"])
def test_message_id_widths(bench, identity):
    ids = [bench.message_id(i, identity) for i in (0, 1, 1023, 1024, 49_999)]
    assert len(set(ids)) == len(ids)
    if identity != "typical":
        assert all(len(i) == bench.MESSAGE_ID_MAX_CHARS for i in ids)
    if identity == "utf8x4":
        # The UTF-8 upper bound: four bytes a character, plus the suffix.
        assert len(bench.claimant_of(ids[0]).encode()) == 4 * 998 + 17


@pytest.fixture(scope="module")
def report(bench, tmp_path_factory):
    work = tmp_path_factory.mktemp("bench")
    return bench.main(
        [
            "--workdir",
            str(work),
            "--messages",
            "240",
            "--per-message",
            "2",
            "--identity",
            "utf8x4",
            "--records",
            "worst",
            "--k",
            "10,40",
            "--missing",
            "30",
            "--extras",
            "4",
            "--repeat",
            "1",
            "--wal",
            "--writer-interval",
            "0.005",
            # Keeps the tiny round's transaction open long enough for
            # the writer to commit inside it.
            "--wal-hold",
            "1.0",
        ]
    )


@pytest.fixture(scope="module")
def cardinality(bench, tmp_path_factory):
    work = tmp_path_factory.mktemp("cardinality")
    return bench.main(
        [
            "--workdir",
            str(work),
            "--messages",
            "40",
            "--per-message",
            "1",
            "--records",
            "cardinality",
            "--references",
            "1000",
            "--k",
            "3,10",
            "--missing",
            "10",
            "--extras",
            "0",
            "--repeat",
            "1",
        ]
    )


def test_certificate_scans_exactly_the_counted_set(report):
    # 240 messages, 5 % in Trash (left out by the default predicate).
    for kind, members in (("messages", 228), ("occurrences", 456)):
        stream = report["certificate"][f"{kind}/stream"]
        collect = report["certificate"][f"{kind}/collect"]
        assert stream["count"] == collect["count"] == members
        # SQLite's ORDER BY on the identity is UTF-8 byte order, so both
        # methods hash the same sequence.
        assert stream["digest"] == collect["digest"]
    assert report["certificate"]["messages/stream"]["max_identity_bytes"] == 4 * 998 + 17
    assert report["certificate"]["occurrences/stream"]["max_identity_bytes"] == 64


def test_reconcile_round_returns_at_most_k_missing_records(report):
    for kind, members in (("messages", 228), ("occurrences", 456)):
        result = report["reconcile"][kind]
        assert result["request"]["members"] == members
        assert result["request"]["uploaded"] == members - 30 + 4
        # Hex: 64 characters plus quotes and a comma per hash.
        assert result["request"]["request_bytes_hex"] == 13 + 67 * (members - 30 + 4) - 1
        for k, round_ in result["k"].items():
            assert round_["missing_total"] == 30
            assert round_["returned"] == min(int(k), 30)
            assert round_["extras"] == 4
            assert round_["rounds_to_repair"] == math.ceil(30 / int(k))
            assert round_["rounds_from_empty"] == math.ceil(members / int(k))
            assert round_["response_bytes"] > round_["returned"] * 1000
            # Every extra comes back as a 64-character hash.
            assert round_["response_bytes"] > 66 * round_["extras"]


def test_worst_records_carry_four_byte_fields_past_their_clips(report):
    # 33 participants, each a name and an address of 500 kept characters
    # of four bytes; an occurrence's filename and MIME type likewise.
    messages = report["reconcile"]["messages"]["k"]["10"]
    assert messages["record_bytes_max"] > 33 * 2 * 500 * 4
    assert messages["participant_rows"] == 10 * 33
    assert messages["references"] == 10 * 11
    occurrences = report["reconcile"]["occurrences"]["k"]["10"]
    assert occurrences["record_bytes_max"] > 2 * 500 * 4
    assert occurrences["participant_rows"] == occurrences["references"] == 0


def test_cardinality_records_read_every_stored_row(bench, cardinality):
    # The record readers load every participant row and References entry
    # before the output clips them, so a round's work is K times the
    # stored cardinality, not K times the listed one.
    for k, round_ in cardinality["reconcile"]["messages"]["k"].items():
        assert round_["returned"] == int(k)
        assert round_["participant_rows"] == int(k) * bench.MAX_MESSAGE_ADDRESSES
        assert round_["references"] == int(k) * 1000


def test_wal_is_reclaimed_after_the_round(report):
    assert [w["kind"] for w in report["wal"]] == ["messages", "occurrences"]
    for wal in report["wal"]:
        commits = wal["commits_during_transaction"]
        assert commits > 0
        # Every frame committed while the round held its snapshot stays
        # in the WAL, beyond what the steady state can reuse.
        assert (
            wal["wal_max_during_transaction_bytes"]
            >= commits * wal["commit_bytes"] - wal["wal_steady_max_bytes"]
        )
        assert wal["checkpoint_after"]["busy"] == 0
        assert wal["wal_after_checkpoint_bytes"] == 0


def test_failed_round_stops_the_writer(bench, tmp_path):
    # A reader that fails (here: its request file is missing) must not
    # leave the unthrottled writer committing after run_wal returns.
    db = tmp_path / "failed.db"
    bench.build(db, 20, 1, "typical", "typical")

    def commits() -> int:
        with closing(sqlite3.connect(db)) as conn:
            return conn.execute("SELECT COUNT(*) FROM bench_commits").fetchone()[0]

    with pytest.raises(RuntimeError, match="reconcile round failed"):
        bench.run_wal(str(db), "messages", 5, str(tmp_path / "missing.json"), 4096, 0.0)
    after = commits()
    assert after > 0
    time.sleep(0.5)
    assert commits() == after
    assert not (tmp_path / "failed.db.stop").exists()
