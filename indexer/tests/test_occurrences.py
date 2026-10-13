"""#1566: each Maildir copy of a message is its own occurrence.

Ingest binds every parsed file to an occurrence keyed by (account,
account-relative folder, Maildir uniq, claimant ID), with its path
relative to the account's Maildir root; an observed rename keeps it
(flags, ``new``/``cur``) or replaces it (folder or uniq); the v14
migration seeds one per message and queues a reparse. Since v15 the
reconciler, reaper and MCP server read occurrences, and the per-copy
``messages`` columns are gone (#1567, #1568)."""

import logging
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import pytest
from src import account as account_mod
from src import main
from src.database import EMBEDDING_DIM, Database
from src.occurrences import OUTSIDE_ROOT, OccurrenceLocatorError, locate
from src.parser import _derive_folder, parse_email
from src.queue import REASON_INITIAL_SCAN, REASON_REPARSE, IndexingQueue
from src.reconciler import Reconciler, ReconcilerConfig, sweep_paths
from src.threader import Threader
from src.timings import TimingAggregator

from tests.conftest import TEST_ACCOUNT, lose_message, make_mock_embedder, make_thread
from tests.test_database import _v13_from_fresh

MARKER = "SYNTHETIC_OCCURRENCE_MARKER"
_CONFIG = ReconcilerConfig(
    enabled=True,
    grace_days=1,
    sweep_interval_secs=3600,
    max_batch_pct=1.0,
    force=False,
)
_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIM - 1)


def _account(root: Path):
    return replace(TEST_ACCOUNT, maildir_root=root)


def _eml(message_id: str, body: str = "body") -> bytes:
    return (
        "From: alice@example.com\r\n"
        "To: bob@example.com\r\n"
        f"Subject: {MARKER} {message_id}\r\n"
        f"Message-ID: <{message_id}>\r\n"
        "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n{body} {MARKER}\r\n"
    ).encode()


def _put(root: Path, relative: str, raw: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _drain(db: Database, paths: list[Path], embedder=None) -> None:
    queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
    for path in paths:
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
    # The parser names folders against the same root, as in production.
    with patch.object(main, "MAILDIR_PATH", db.account.maildir_root):
        main._drain_queue_batched(
            db,
            embedder or make_mock_embedder(_VECTOR),
            Threader(db),
            queue,
            batch_size=4,
            timing_aggregator=TimingAggregator(window=4),
        )


def _occurrences(db: Database) -> list[dict]:
    return [
        dict(r)
        for r in db._conn.execute(
            "SELECT o.*, a.scope_uuid FROM message_occurrences o "
            "JOIN accounts a ON a.id = o.account_id ORDER BY o.id"
        )
    ]


def _active(db: Database) -> list[dict]:
    return [o for o in _occurrences(db) if o["state"] == "active"]


class TestLocate:
    @pytest.mark.parametrize(
        "relative",
        [
            "INBOX/cur/m1:2,S",
            "INBOX/new/m2",
            "Folders/.Clients/.cur/cur/m3:2,FS",
            "Folders/Plain/cur/m4",
            "Archive/cur/m5:2,ST",
            "cur/m6",
        ],
    )
    def test_folder_is_the_parsers_folder_inside_the_root(self, relative):
        root = Path("/data/maildir")
        where = locate(root / relative, root)
        assert where.folder == _derive_folder(root / relative, root)
        assert where.relative_path == relative
        assert where.uniq == PurePosixPath(relative).name.split(":2,")[0]

    @pytest.mark.parametrize(
        "path",
        [
            "/data/maildir2/INBOX/cur/m1",  # shares the root's spelling as a prefix
            "/data/mail/INBOX/cur/m1",
            "/data/maildir/../maildir2/INBOX/cur/m1",
            "/data/maildir/INBOX/../../etc/m1",
            "/data/maildir",
            "data/maildir/INBOX/cur/m1",
            "/elsewhere/INBOX/cur/m1",
        ],
    )
    def test_a_path_outside_the_root_is_refused_without_the_path(self, path):
        with pytest.raises(OccurrenceLocatorError) as exc:
            locate(path, Path("/data/maildir"))
        assert str(exc.value) == OUTSIDE_ROOT
        assert "maildir2" not in str(exc.value) and "m1" not in str(exc.value)

    def test_the_parsers_leaf_fallback_is_not_used(self):
        """``_derive_folder`` names a path outside the root by its leaf
        folder; the locator refuses it instead."""
        outside = Path("/data/other/INBOX/cur/m1")
        assert _derive_folder(outside, Path("/data/maildir")) == "INBOX"
        with pytest.raises(OccurrenceLocatorError):
            locate(outside, Path("/data/maildir"))


class TestIngest:
    def test_equal_byte_copies_in_two_folders_are_two_occurrences(self, tmp_path):
        root = tmp_path / "maildir"
        raw = _eml("copy@example.test")
        inbox = _put(root, "INBOX/cur/u1:2,S", raw)
        archive = _put(root, "Archive/cur/u2:2,F", raw)
        db = Database(tmp_path / "mail.db", account=_account(root))
        try:
            _drain(db, [inbox, archive])
            rows = _active(db)
            assert [(o["folder"], o["relative_path"]) for o in rows] == [
                ("INBOX", "INBOX/cur/u1:2,S"),
                ("Archive", "Archive/cur/u2:2,F"),
            ]
            assert rows[0]["claimant_id"] == rows[1]["claimant_id"]
            assert [(o["seen"], o["flagged"]) for o in rows] == [(1, 0), (0, 1)]
            assert [o["uniq"] for o in rows] == ["u1", "u2"]
            for o in rows:
                assert o["scope_uuid"] == TEST_ACCOUNT.scope_uuid
                assert o["account_id"] == db.account_id
                assert o["provenance"] == "parse"
                assert o["first_observed_at"] and o["last_observed_at"]
                # Unknown until #1081 writes it.
                assert o["internal_at"] is None
                assert o["trashed"] == 0
            # Dormant: the message row still holds one path, as before.
            assert db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        finally:
            db.close()

    def test_a_trashed_file_is_a_trashed_occurrence(self, tmp_path):
        root = tmp_path / "maildir"
        path = _put(root, "INBOX/cur/t1:2,ST", _eml("t@example.test"))
        db = Database(tmp_path / "mail.db", account=_account(root))
        try:
            # Indexed directly: the drain parks trashed files in mirror mode.
            db.upsert_thread(make_thread([parse_email(path, maildir_root=root)]), _VECTOR)
            (row,) = _active(db)
            assert (row["seen"], row["trashed"]) == (1, 1)
        finally:
            db.close()

    def test_reindexing_the_same_file_keeps_its_occurrence(self, tmp_path):
        root = tmp_path / "maildir"
        path = _put(root, "INBOX/cur/r1:2,S", _eml("r@example.test"))
        db = Database(tmp_path / "mail.db", account=_account(root))
        try:
            _drain(db, [path])
            (before,) = _active(db)
            _drain(db, [path])
            (after,) = _occurrences(db)
            assert after["id"] == before["id"]
            assert after["first_observed_at"] == before["first_observed_at"]
            assert after["last_observed_at"] >= before["last_observed_at"]
        finally:
            db.close()

    def test_new_bytes_at_a_path_replace_its_occurrence(self, tmp_path):
        root = tmp_path / "maildir"
        path = _put(root, "INBOX/cur/c1:2,S", _eml("old@example.test"))
        db = Database(tmp_path / "mail.db", account=_account(root))
        try:
            _drain(db, [path])
            (old,) = _active(db)
            path.write_bytes(_eml("new@example.test"))
            _drain(db, [path])
            rows = _occurrences(db)
            assert [(o["id"], o["state"]) for o in rows] == [
                (old["id"], "retired"),
                (rows[1]["id"], "active"),
            ]
            assert rows[0]["retired_at"] is not None
            assert rows[0]["relative_path"] == rows[1]["relative_path"] == "INBOX/cur/c1:2,S"
            assert rows[1]["claimant_id"] != old["claimant_id"]
        finally:
            db.close()

    def test_a_file_outside_the_root_fails_its_write_closed(self, tmp_path):
        root = tmp_path / "maildir"
        outside = _put(tmp_path, "maildir2/INBOX/cur/o1", _eml("o@example.test"))
        db = Database(tmp_path / "mail.db", account=_account(root))
        try:
            msg = parse_email(outside, maildir_root=root)
            with pytest.raises(OccurrenceLocatorError):
                db.upsert_thread(make_thread([msg]), _VECTOR)
            # The whole Phase 1 write rolled back.
            assert db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
            assert _occurrences(db) == []
        finally:
            db.close()

    def test_a_rebuild_over_the_same_maildir_reproduces_the_keys(self, tmp_path):
        root = tmp_path / "maildir"
        paths = [
            _put(root, "INBOX/cur/k1:2,S", _eml("k1@example.test")),
            _put(root, "Folders/.Clients/cur/k2", _eml("k2@example.test")),
            _put(root, "Archive/new/k3", _eml("k1@example.test")),
        ]

        def keys(name: str) -> set[tuple]:
            db = Database(tmp_path / name, account=_account(root))
            try:
                _drain(db, paths)
                return {
                    (o["scope_uuid"], o["folder"], o["uniq"], o["claimant_id"], o["relative_path"])
                    for o in _active(db)
                }
            finally:
                db.close()

        first = keys("a.db")
        assert len(first) == 3
        assert keys("b.db") == first

    def test_relocating_the_root_alone_keeps_the_occurrences(self, tmp_path):
        old_root = tmp_path / "old"
        path = _put(old_root, "INBOX/cur/l1:2,S", _eml("l@example.test"))
        db = Database(tmp_path / "mail.db", account=_account(old_root))
        _drain(db, [path])
        (before,) = _active(db)
        db.close()
        new_root = tmp_path / "new"
        shutil.move(old_root, new_root)
        db = Database(tmp_path / "mail.db", account=_account(new_root))
        try:
            _drain(db, [new_root / "INBOX/cur/l1:2,S"])
            (after,) = _occurrences(db)
            assert after["id"] == before["id"]
            assert after["relative_path"] == before["relative_path"]
            assert after["state"] == "active"
        finally:
            db.close()


class TestRenames:
    def _one(self, tmp_path, relative="INBOX/cur/f1:2,S"):
        root = tmp_path / "maildir"
        path = _put(root, relative, _eml("f@example.test"))
        db = Database(tmp_path / "mail.db", account=_account(root))
        _drain(db, [path])
        return db, root, path

    def test_a_flag_rename_keeps_the_occurrence(self, tmp_path):
        db, root, path = self._one(tmp_path)
        try:
            (before,) = _active(db)
            dest = root / "INBOX/cur/f1:2,FRS"
            path.rename(dest)
            db.update_filepath(str(path), str(dest), move_occurrence=True)
            (after,) = _occurrences(db)
            assert after["id"] == before["id"]
            assert after["relative_path"] == "INBOX/cur/f1:2,FRS"
            assert (after["seen"], after["flagged"], after["replied"]) == (1, 1, 1)
            assert after["last_observed_at"] >= before["last_observed_at"]
            assert after["provenance"] == "parse"
        finally:
            db.close()

    def test_new_to_cur_keeps_the_occurrence(self, tmp_path):
        db, root, path = self._one(tmp_path, "INBOX/new/f1")
        try:
            (before,) = _active(db)
            dest = root / "INBOX/cur/f1:2,S"
            dest.parent.mkdir(parents=True, exist_ok=True)
            path.rename(dest)
            db.update_filepath(str(path), str(dest), move_occurrence=True)
            (after,) = _occurrences(db)
            assert after["id"] == before["id"]
            assert after["relative_path"] == "INBOX/cur/f1:2,S"
        finally:
            db.close()

    def test_a_trash_flag_rename_marks_the_occurrence_trashed(self, tmp_path):
        db, root, path = self._one(tmp_path)
        try:
            dest = root / "INBOX/cur/f1:2,ST"
            path.rename(dest)
            db.update_filepath(str(path), str(dest), move_occurrence=True)
            (after,) = _occurrences(db)
            assert (after["state"], after["trashed"]) == ("active", 1)
        finally:
            db.close()

    def test_a_cross_folder_move_retires_and_replaces_atomically(self, tmp_path):
        db, root, path = self._one(tmp_path)
        try:
            (before,) = _active(db)
            dest = _put(root, "Archive/cur/f1:2,S", path.read_bytes())
            path.unlink()
            db.update_filepath(str(path), str(dest), move_occurrence=True)
            old, new = _occurrences(db)
            assert (old["id"], old["state"]) == (before["id"], "retired")
            assert old["retired_at"] is not None
            # The retired row keeps its locator for diagnostics.
            assert (old["folder"], old["relative_path"]) == ("INBOX", "INBOX/cur/f1:2,S")
            assert (new["state"], new["folder"], new["relative_path"]) == (
                "active",
                "Archive",
                "Archive/cur/f1:2,S",
            )
            assert new["claimant_id"] == before["claimant_id"]
            assert new["provenance"] == "rename"
        finally:
            db.close()

    def test_a_failed_move_changes_nothing(self, tmp_path):
        db, root, path = self._one(tmp_path)
        try:
            (before,) = _active(db)
            outside = tmp_path / "maildir2" / "Archive" / "cur" / "f1:2,S"
            with pytest.raises(OccurrenceLocatorError):
                db.update_filepath(str(path), str(outside), move_occurrence=True)
            assert _occurrences(db) == [before]
            assert db.is_indexed(str(path))
        finally:
            db.close()

    def test_a_new_file_at_a_retired_path_binds_by_its_hash(self, tmp_path):
        db, root, path = self._one(tmp_path)
        try:
            dest = _put(root, "Archive/cur/f1:2,S", path.read_bytes())
            path.unlink()
            db.update_filepath(str(path), str(dest), move_occurrence=True)
            (retired,) = [o for o in _occurrences(db) if o["state"] == "retired"]
            # Other bytes arrive at the retired path: a new row for them.
            _put(root, "INBOX/cur/f1:2,S", _eml("other@example.test"))
            _drain(db, [path])
            rows = _occurrences(db)
            assert [o for o in rows if o["id"] == retired["id"]] == [retired]
            fresh = [
                o
                for o in rows
                if o["state"] == "active" and o["relative_path"] == retired["relative_path"]
            ]
            assert len(fresh) == 1
            assert fresh[0]["id"] != retired["id"]
            assert fresh[0]["claimant_id"] != retired["claimant_id"]
            # The same bytes coming back are a new row too, never the retired one.
            path.write_bytes(dest.read_bytes())
            _drain(db, [path])
            back = [o for o in _active(db) if o["relative_path"] == retired["relative_path"]]
            assert len(back) == 1
            assert back[0]["claimant_id"] == retired["claimant_id"]
            assert back[0]["id"] not in {retired["id"], fresh[0]["id"]}
            assert [o for o in _occurrences(db) if o["id"] == retired["id"]] == [retired]
        finally:
            db.close()

    def test_an_observed_rename_event_carries_the_occurrence(self, tmp_path):
        """The watcher's rename event reaches ``update_filepath`` through
        ``Reconciler.handle_moved`` (mirror mode) as an observed move."""
        db, root, path = self._one(tmp_path)
        try:
            (before,) = _active(db)
            dest = root / "INBOX/cur/f1:2,FS"
            path.rename(dest)
            Reconciler(db, make_mock_embedder(_VECTOR), _CONFIG, maildir_root=root).handle_moved(
                str(path), str(dest)
            )
            (after,) = _occurrences(db)
            assert (after["id"], after["relative_path"]) == (before["id"], "INBOX/cur/f1:2,FS")
        finally:
            db.close()

    @pytest.mark.parametrize("sweep", ["startup", "reconciler"])
    def test_a_rename_inferred_by_a_sweep_moves_the_occurrence_once_its_bytes_match(
        self, tmp_path, sweep
    ):
        """A sweep finds a renamed file by its uniq; the occurrence follows
        only once the file's bytes are verified (#1567), never bound to
        the claimant on the uniq alone (Codex round 3 on #1601)."""
        db, root, path = self._one(tmp_path)
        try:
            (before,) = _occurrences(db)
            dest = root / "INBOX/cur/f1:2,FS"
            path.rename(dest)
            if sweep == "startup":
                assert sweep_paths(db, maildir_root=root)["renamed"] == 1
            else:
                rec = Reconciler(db, make_mock_embedder(_VECTOR), _CONFIG, maildir_root=root)
                assert rec.sweep()["renamed"] == 1
            (after,) = _occurrences(db)
            assert (after["id"], after["relative_path"]) == (before["id"], "INBOX/cur/f1:2,FS")
            assert after["provenance"] == before["provenance"]
        finally:
            db.close()

    def test_a_reaped_message_takes_its_occurrences_with_it(self, tmp_path):
        db, root, path = self._one(tmp_path)
        try:
            (row,) = _active(db)
            lose_message(db, row["claimant_id"])
            thread_id = db._conn.execute("SELECT thread_id FROM messages").fetchone()[0]
            assert db.delete_thread_completely(thread_id)
            assert _occurrences(db) == []
        finally:
            db.close()

    def test_two_active_rows_cannot_share_a_path(self, tmp_path):
        db, root, path = self._one(tmp_path)
        try:
            (row,) = _active(db)
            with pytest.raises(sqlite3.IntegrityError):
                db._conn.execute(
                    "INSERT INTO message_occurrences (account_id, folder, uniq, claimant_id, "
                    "relative_path, seen, flagged, replied, trashed, state, provenance) "
                    "VALUES (?, 'Other', 'x', ?, ?, 0, 0, 0, 0, 'active', 'parse')",
                    (row["account_id"], row["claimant_id"], row["relative_path"]),
                )
            db._conn.rollback()
        finally:
            db.close()


def _v13_index(tmp_path: Path, root: Path) -> tuple[Path, dict]:
    """A populated v13 index built by the real pipeline: two messages, one
    byte-identical copy of the first in another folder (mapped elsewhere),
    and one dead-lettered file."""
    raw = _eml("seed1@example.test", "first")
    a = _put(root, "INBOX/cur/s1:2,S", raw)
    b = _put(root, "Folders/.Clients/cur/s2:2,F", _eml("seed2@example.test", "second"))
    dead = _put(root, "INBOX/cur/s3", _eml("seed3@example.test", "third"))
    db = Database(tmp_path / "mail.db", account=_account(root))
    _drain(db, [a, b])
    # Trashed after indexing, as mbsync flags a deletion.
    trashed = b.with_name("s2:2,FT")
    b.rename(trashed)
    db.update_filepath(str(b), str(trashed), move_occurrence=True)
    b = trashed
    queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
    queue.enqueue(str(dead), REASON_INITIAL_SCAN)
    queue.mark_dead_terminal(str(dead), stage="parse", error="oversized: too large")
    state = {
        "chunks": db._conn.execute(
            "SELECT c.chunk_id, c.claimant_id, c.thread_id, v.embedding FROM message_chunks c "
            "JOIN message_chunks_vec v ON v.chunk_id = c.chunk_id ORDER BY c.chunk_id"
        ).fetchall(),
        "threads": db._conn.execute(
            "SELECT t.thread_id, t.message_ids, v.embedding FROM threads t "
            "JOIN threads_vec v ON v.thread_id = t.thread_id ORDER BY t.thread_id"
        ).fetchall(),
        "paths": {"a": a, "b": b, "dead": dead},
    }
    state["chunks"] = [tuple(r) for r in state["chunks"]]
    state["threads"] = [tuple(r) for r in state["threads"]]
    # v13's columns, filled from the occurrences as v13's ingest kept them.
    _v13_from_fresh(db)
    state["messages"] = [
        tuple(r)
        for r in db._conn.execute(
            "SELECT claimant_id, message_id, thread_id, filepath, folder, seen, flagged "
            "FROM messages ORDER BY claimant_id"
        )
    ]
    db._conn.commit()
    db.close()
    return tmp_path / "mail.db", state


class TestMigrationV14:
    def test_seeds_one_occurrence_per_message_and_queues_a_reparse(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        root = tmp_path / "maildir"
        path, before = _v13_index(tmp_path, root)
        db = Database(path, account=_account(root))
        try:
            assert db._conn.execute("SELECT version FROM schema_version").fetchone()[0] == 15
            rows = _occurrences(db)
            by_claimant = {o["claimant_id"]: o for o in rows}
            assert len(rows) == len(before["messages"]) == 2
            for claimant, _mid, _tid, filepath, folder, seen, flagged in before["messages"]:
                o = by_claimant[claimant]
                where = locate(filepath, root)
                assert (o["folder"], o["uniq"], o["relative_path"]) == (
                    where.folder,
                    where.uniq,
                    where.relative_path,
                )
                assert o["folder"] == folder
                assert (o["seen"], o["flagged"]) == (seen, flagged)
                assert o["provenance"] == "migration"
                # Derived, not observed.
                assert o["first_observed_at"] is None and o["last_observed_at"] is None
                assert o["internal_at"] is None
                assert o["state"] == "active"
                assert o["account_id"] == db.account_id
            trashed = by_claimant[
                next(m[0] for m in before["messages"] if m[3] == str(before["paths"]["b"]))
            ]
            assert trashed["trashed"] == 1
            # Nothing else moved: claimant, chunk and thread IDs and vectors.
            assert [
                tuple(r)
                for r in db._conn.execute(
                    "SELECT c.chunk_id, c.claimant_id, c.thread_id, v.embedding "
                    "FROM message_chunks c JOIN message_chunks_vec v "
                    "ON v.chunk_id = c.chunk_id ORDER BY c.chunk_id"
                )
            ] == before["chunks"]
            assert [
                tuple(r)
                for r in db._conn.execute(
                    "SELECT t.thread_id, t.message_ids, v.embedding FROM threads t "
                    "JOIN threads_vec v ON v.thread_id = t.thread_id ORDER BY t.thread_id"
                )
            ] == before["threads"]
            jobs = {
                r[0]: (r[1], r[2])
                for r in db._conn.execute("SELECT filepath, reason, status FROM indexing_jobs")
            }
            assert jobs == {
                str(before["paths"]["a"]): (REASON_REPARSE, "queued"),
                str(before["paths"]["b"]): (REASON_REPARSE, "queued"),
                str(before["paths"]["dead"]): (REASON_INITIAL_SCAN, "dead"),
            }
        finally:
            db.close()
        assert "Migration v14 seeded 2 mailbox occurrence(s)" in caplog.text
        assert "bound no occurrence" not in caplog.text
        assert MARKER not in caplog.text

    def test_the_reparse_observes_the_seed_without_embedding(self, tmp_path):
        root = tmp_path / "maildir"
        path, before = _v13_index(tmp_path, root)
        # A byte-identical copy indexed before v14 under its own path but
        # mapped nowhere: the seed cannot see it; the reparse binds it.
        copy = _put(root, "Archive/cur/s9:2,S", before["paths"]["a"].read_bytes())
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO indexed_files (filepath, indexed_at, size, mtime_ns, content_hash) "
            "SELECT ?, indexed_at, size, mtime_ns, content_hash FROM indexed_files WHERE filepath = ?",
            (str(copy), str(before["paths"]["a"])),
        )
        conn.commit()
        conn.close()
        db = Database(path, account=_account(root))
        try:
            seeded = {o["relative_path"]: o for o in _occurrences(db)}
            assert set(seeded) == {"INBOX/cur/s1:2,S", "Folders/.Clients/cur/s2:2,FT"}
            embedder = make_mock_embedder(_VECTOR)
            _drain(db, [], embedder)
            assert embedder.embed_batch.call_count == 0
            assert embedder.embed.call_count == 0
            rows = {o["relative_path"]: o for o in _active(db)}
            # The mapped copy's seeded row is observed in place; the
            # trashed one is parked by the mirror-mode drain and waits.
            observed = rows["INBOX/cur/s1:2,S"]
            assert observed["id"] == seeded["INBOX/cur/s1:2,S"]["id"]
            assert observed["provenance"] == "migration"
            assert observed["last_observed_at"] is not None
            assert rows["Archive/cur/s9:2,S"]["claimant_id"] == observed["claimant_id"]
            assert rows["Archive/cur/s9:2,S"]["provenance"] == "parse"
            assert [
                tuple(r)
                for r in db._conn.execute(
                    "SELECT c.chunk_id, c.claimant_id, c.thread_id, v.embedding "
                    "FROM message_chunks c JOIN message_chunks_vec v "
                    "ON v.chunk_id = c.chunk_id ORDER BY c.chunk_id"
                )
            ] == before["chunks"]
            assert (
                db._conn.execute(
                    "SELECT status FROM indexing_jobs WHERE filepath = ?",
                    (str(before["paths"]["dead"]),),
                ).fetchone()[0]
                == "dead"
            )
        finally:
            db.close()

    @pytest.mark.parametrize(
        "configured",
        [
            "maildir2",  # a sibling sharing the root's spelling as a prefix
            "maildir/INBOX",  # a root inside the old one
            "elsewhere",
        ],
    )
    def test_a_path_outside_the_root_fails_the_migration_closed(self, tmp_path, caplog, configured):
        root = tmp_path / "maildir"
        path, _before = _v13_index(tmp_path, root)
        with pytest.raises(OccurrenceLocatorError) as exc:
            Database(path, account=_account(tmp_path / configured))
        assert str(exc.value) == OUTSIDE_ROOT
        assert str(tmp_path) not in str(exc.value)
        assert MARKER not in caplog.text
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 13
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            assert "accounts" not in tables and "message_occurrences" not in tables
            assert conn.execute("SELECT COUNT(*) FROM indexing_jobs").fetchone()[0] == 1
        finally:
            conn.close()
        # The right root then migrates it.
        Database(path, account=_account(root)).close()

    @pytest.mark.parametrize("configured", ["maildir", "sub/../maildir"])
    def test_relative_paths_a_v13_index_stored_are_seeded(self, tmp_path, monkeypatch, configured):
        """An indexer run outside Compose with a relative ``MAILDIR_PATH``
        walked a relative root and stored relative paths; the root is
        now made absolute, so the seed resolves those legacy paths the
        same way before relativizing them, instead of failing every
        retry (Codex round 4 on #1601)."""
        root = tmp_path / "maildir"
        path, before = _v13_index(tmp_path, root)
        conn = sqlite3.connect(path)
        prefix = str(tmp_path) + "/"
        for table in ("messages", "message_thread_map", "indexed_files"):
            conn.execute(
                f"UPDATE {table} SET filepath = substr(filepath, ?) "  # nosec B608
                f"WHERE filepath LIKE ? || '%'",
                (len(prefix) + 1, prefix),
            )
        conn.commit()
        assert all(not r[0].startswith("/") for r in conn.execute("SELECT filepath FROM messages"))
        conn.close()
        (tmp_path / "sub").mkdir()
        monkeypatch.chdir(tmp_path)
        account = _account(account_mod.maildir_root_from_env({"MAILDIR_PATH": configured}))
        db = Database(path, account=account)
        try:
            assert sorted(o["relative_path"] for o in _occurrences(db)) == [
                "Folders/.Clients/cur/s2:2,FT",
                "INBOX/cur/s1:2,S",
            ]
        finally:
            db.close()

    def test_an_absolute_legacy_path_outside_the_root_still_fails(self, tmp_path):
        """Only relative paths are resolved; an absolute one is compared
        as stored."""
        root = tmp_path / "maildir"
        path, _before = _v13_index(tmp_path, root)
        with pytest.raises(OccurrenceLocatorError):
            Database(path, account=_account(tmp_path / "other"))

    def test_a_message_whose_path_holds_other_bytes_is_left_for_the_reparse(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        root = tmp_path / "maildir"
        path, before = _v13_index(tmp_path, root)
        conn = sqlite3.connect(path)
        conn.execute(
            "UPDATE indexed_files SET content_hash = 'other' WHERE filepath = ?",
            (str(before["paths"]["a"]),),
        )
        conn.commit()
        conn.close()
        db = Database(path, account=_account(root))
        try:
            assert [o["relative_path"] for o in _active(db)] == ["Folders/.Clients/cur/s2:2,FT"]
            # v15 gives it a retired occurrence (#1567): no file holds its bytes.
            (retired,) = [o for o in _occurrences(db) if o["state"] == "retired"]
            assert (retired["relative_path"], retired["provenance"]) == (
                "INBOX/cur/s1:2,S",
                "migration",
            )
            jobs = {r[0] for r in db._conn.execute("SELECT filepath FROM indexing_jobs")}
            assert str(before["paths"]["a"]) in jobs
            # A rename of that file before its reparse binds nothing: its
            # recorded bytes are no message's, and the parse binds it.
            a = before["paths"]["a"]
            dest = a.with_name("s1:2,FS")
            a.rename(dest)
            db.update_filepath(str(a), str(dest), move_occurrence=True)
            assert [o["relative_path"] for o in _active(db)] == ["Folders/.Clients/cur/s2:2,FT"]
            assert db.is_indexed(str(dest))
        finally:
            db.close()
        lines = [r for r in caplog.records if "bound no occurrence" in r.getMessage()]
        assert [(r.levelno, r.args) for r in lines] == [(logging.WARNING, (1,))]
        assert "Migration v14 seeded 1 mailbox occurrence(s)" in caplog.text
        assert MARKER not in caplog.text

    def test_the_migration_ends_with_the_shared_reparse_statement(self):
        from src.queue import REPARSE_ENQUEUE_SQL

        sql = (
            Path(main.__file__).parent / "migrations" / "0014_account_occurrences.sql"
        ).read_text()
        assert sql.rstrip().endswith(REPARSE_ENQUEUE_SQL)
