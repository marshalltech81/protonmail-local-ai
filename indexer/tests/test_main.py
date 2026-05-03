"""Tests for src/main.py.

Covers the watchdog-facing behaviors that cannot be verified by
``database`` or ``threader`` tests alone: that ``on_moved`` indexes the
destination of a Maildir rename (standard delivery path), that
``initial_index`` refreshes the health file periodically so long scans
do not exceed ``HEALTH_MAX_AGE_SECONDS``, and that the startup probe
validates the running embedding model's output dimension against the
schema-reserved vector dimension.

``main`` orchestrates watchdog, Ollama, and filesystem I/O; these tests
exercise it with stub collaborators rather than booting a live indexer.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from src import main
from src.database import EMBEDDING_DIM, Database
from src.queue import REASON_INITIAL_SCAN, IndexingQueue
from src.threader import Threader


class _FakeEvent:
    def __init__(self, src_path: str, dest_path: str, is_directory: bool = False):
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


def _make_queue(db: Database) -> IndexingQueue:
    """Queue with tight retry limits so tests that exercise failure
    paths don't wait on real-world 30 s backoffs."""
    return IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)


def _write_eml(path: Path, message_id: str, subject: str = "Hello") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"From: alice@example.com\r\n"
        f"To: bob@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: <{message_id}>\r\n"
        f"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"Body of {message_id}.\r\n",
        encoding="utf-8",
    )


def _write_eml_with_text_attachment(path: Path, message_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"From: alice@example.com\r\n"
        f"To: bob@example.com\r\n"
        f"Subject: Attachment retry\r\n"
        f"Message-ID: <{message_id}>\r\n"
        f"Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: multipart/mixed; boundary=frontier\r\n"
        f"\r\n"
        f"--frontier\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"Body of {message_id}.\r\n"
        f"\r\n"
        f"--frontier\r\n"
        f"Content-Type: text/plain; name=note.txt\r\n"
        f"Content-Disposition: attachment; filename=note.txt\r\n"
        f"Content-Transfer-Encoding: 7bit\r\n"
        f"\r\n"
        f"attachment text that should be chunked\r\n"
        f"--frontier--\r\n",
        encoding="utf-8",
    )


class TestOnMovedIndexesDestination:
    def test_rename_into_new_indexes_destination(self, tmp_path, monkeypatch):
        """Regression: Maildir delivery writes a file under ``tmp/`` then
        renames it into ``new/``. Prior behavior only fired ``on_created``
        for the source rename event, leaving the message unindexed until
        restart. ``on_moved`` must enqueue the destination and the
        worker drain must then pick it up."""
        db_path = tmp_path / "db" / "mail.db"
        db = Database(db_path)
        threader = Threader(db)
        queue = _make_queue(db)

        # Populate a real Maildir destination file so the pipeline
        # succeeds end-to-end through the parser.
        dest = tmp_path / "INBOX" / "new" / "msg.eml"
        _write_eml(dest, "moved@example.com")

        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM

        handler = main.MaildirHandler(db, queue)
        handler.on_moved(
            _FakeEvent(
                src_path=str(tmp_path / "tmp" / "msg.eml"),
                dest_path=str(dest),
            )
        )
        # The event only enqueued — the file becomes indexed once the
        # worker drains the queue.
        assert not db.is_indexed(str(dest))
        main.drain_queue(queue, db, embedder, threader)
        assert db.is_indexed(str(dest))

    def test_directory_moves_are_ignored(self, tmp_path):
        db = Database(tmp_path / "db" / "mail.db")
        handler = main.MaildirHandler(db, _make_queue(db))
        # Directory events should not cause an index attempt
        handler.on_moved(
            _FakeEvent(
                src_path=str(tmp_path / "a"),
                dest_path=str(tmp_path / "b"),
                is_directory=True,
            )
        )
        # Trivially: no crash, and no new indexed files
        assert db.count_total_messages() == 0

    def test_already_indexed_destination_is_not_reindexed(self, tmp_path):
        """A rename from ``cur/msg`` to ``cur/msg,S`` (flag change) must
        not re-parse and re-embed an already-indexed message."""
        db_path = tmp_path / "db" / "mail.db"
        db = Database(db_path)
        threader = Threader(db)
        queue = _make_queue(db)

        dest = tmp_path / "INBOX" / "cur" / "msg.eml"
        _write_eml(dest, "flag_change@example.com")

        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM

        handler = main.MaildirHandler(db, queue)

        # First delivery enqueues and drains, indexing the message.
        handler.on_moved(_FakeEvent(src_path=str(tmp_path / "tmp" / "m"), dest_path=str(dest)))
        main.drain_queue(queue, db, embedder, threader)
        first_call_count = embedder.embed.call_count
        assert first_call_count == 1

        # Second move event on the same path (e.g., flag rename) must not
        # re-enqueue work or trigger another embed.
        handler.on_moved(_FakeEvent(src_path=str(dest), dest_path=str(dest)))
        main.drain_queue(queue, db, embedder, threader)
        assert embedder.embed.call_count == first_call_count

    def test_flag_rename_moves_filepath_without_reindexing(self, tmp_path):
        """Maildir flag changes land as on_moved(src=old_name, dest=new_name)
        where both live in the same ``cur/`` directory and the source path
        is already indexed. The prior on_moved fix re-indexed the new path
        because ``is_indexed(dest)`` was False (indexed_files still held
        the old name). Now we must detect the rename, skip the re-embed,
        and move the stored filepath forward."""
        db_path = tmp_path / "db" / "mail.db"
        db = Database(db_path)
        threader = Threader(db)
        queue = _make_queue(db)

        # Deliver msg:2,S into cur/
        original_path = tmp_path / "INBOX" / "cur" / "1738500000.uniq.proton:2,S"
        _write_eml(original_path, "flag@example.com")

        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM

        handler = main.MaildirHandler(db, queue)
        handler.on_moved(
            _FakeEvent(src_path=str(tmp_path / "tmp" / "m"), dest_path=str(original_path))
        )
        main.drain_queue(queue, db, embedder, threader)
        deliveries = embedder.embed.call_count
        assert deliveries == 1

        # mbsync marks it replied: renames file to msg:2,SR.
        renamed_path = tmp_path / "INBOX" / "cur" / "1738500000.uniq.proton:2,SR"
        original_path.rename(renamed_path)
        handler.on_moved(_FakeEvent(src_path=str(original_path), dest_path=str(renamed_path)))
        main.drain_queue(queue, db, embedder, threader)

        # Must not have re-parsed or re-embedded.
        assert embedder.embed.call_count == deliveries
        # indexed_files now tracks the new filepath; old path is gone.
        assert db.is_indexed(str(renamed_path))
        assert not db.is_indexed(str(original_path))


class TestInitialIndexNestedFolders:
    def test_recursive_scan_indexes_nested_folders(self, tmp_path, monkeypatch):
        """Regression: ``initial_index`` walked only one level under
        ``MAILDIR_PATH``. With mbsync ``SubFolders Verbatim``, nested
        folders like ``Clients/ABC`` were never scanned. The recursive
        walk now picks them up at any depth."""
        maildir = tmp_path / "maildir"
        nested = maildir / "Clients" / "ABC" / "cur"
        nested.mkdir(parents=True)
        flat = maildir / "INBOX" / "cur"
        flat.mkdir(parents=True)

        _write_eml(nested / "deep.eml", "deep@example.com")
        _write_eml(flat / "top.eml", "top@example.com")

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * 768

        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        main.initial_index(db, embedder, threader, _make_queue(db))

        assert db.is_indexed(str(nested / "deep.eml"))
        assert db.is_indexed(str(flat / "top.eml"))

    def test_nested_folder_stored_as_relative_path(self, tmp_path, monkeypatch):
        """Once indexed, a nested message's stored ``folder`` reflects the
        full relative path under the Maildir root."""
        maildir = tmp_path / "maildir"
        nested = maildir / "Clients" / "ABC" / "cur"
        nested.mkdir(parents=True)
        _write_eml(nested / "m.eml", "nested@example.com")

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * 768
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)

        main.initial_index(db, embedder, threader, _make_queue(db))

        row = db._conn.execute(
            "SELECT folder FROM threads WHERE thread_id = 'nested@example.com'"
        ).fetchone()
        assert row["folder"] == "Clients/ABC"


class TestInitialIndexHeartbeat:
    def test_health_file_refreshed_after_every_processed_message(self, tmp_path, monkeypatch):
        """``initial_index`` must refresh the heartbeat after every
        processed message so that embedding a large mailbox does not
        exceed ``HEALTH_MAX_AGE_SECONDS`` mid-scan. A single batch of 25
        jobs at ~5 embeds/sec with chunky attachments easily exceeds the
        90s threshold; per-job touch decouples healthcheck cadence from
        per-batch duration."""
        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        inbox.mkdir(parents=True)

        message_count = 5
        for i in range(message_count):
            _write_eml(inbox / f"m{i}.eml", f"m{i}@example.com")

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)

        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM

        touches: list[None] = []
        monkeypatch.setattr(main, "touch_health_file", lambda: touches.append(None))
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)

        main.initial_index(db, embedder, threader, _make_queue(db))

        # One touch per processed message in the drain loop. (The
        # outer ``main()`` adds two more touches — pre-call and
        # post-call — but those are not exercised by this test.)
        assert len(touches) == message_count


class TestDrainQueueRetryAndDeadLetter:
    """End-to-end coverage of the queue worker: transient embedding
    failure retries until it succeeds; persistent failure transitions
    the row to ``dead`` after ``max_attempts``."""

    def test_transient_embed_failure_retries_and_eventually_succeeds(self, tmp_path):
        dest = tmp_path / "INBOX" / "new" / "msg.eml"
        _write_eml(dest, "retry@example.com")

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        queue = _make_queue(db)

        embedder = MagicMock()
        # First embed call raises (Ollama transient); second call succeeds.
        embedder.embed.side_effect = [
            RuntimeError("ollama unavailable"),
            [0.0] * EMBEDDING_DIM,
        ]

        queue.enqueue(str(dest), "test")

        # ``max_batch=1`` models the main loop's interleaving behavior:
        # each pass through drain processes at most one job before
        # yielding to other concerns (reconciler, health file). With
        # the tight zero-backoff queue fixture, the two calls run
        # attempt 1 (fail → re-queued) and attempt 2 (success → row
        # deleted) on separate passes.
        attempted_first = main.drain_queue(queue, db, embedder, threader, max_batch=1)
        assert attempted_first == 1
        assert not db.is_indexed(str(dest))
        row = db._conn.execute(
            "SELECT attempts, status FROM indexing_jobs WHERE filepath = ?",
            (str(dest),),
        ).fetchone()
        assert row["attempts"] == 1
        assert row["status"] == "queued"

        attempted_second = main.drain_queue(queue, db, embedder, threader, max_batch=1)
        assert attempted_second == 1
        assert db.is_indexed(str(dest))
        assert queue.stats() == {"queued": 0, "dead": 0}

    def test_persistent_embed_failure_transitions_to_dead(self, tmp_path):
        dest = tmp_path / "INBOX" / "new" / "msg.eml"
        _write_eml(dest, "giveup@example.com")

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        queue = _make_queue(db)  # max_attempts=3

        embedder = MagicMock()
        embedder.embed.side_effect = RuntimeError("ollama still down")

        queue.enqueue(str(dest), "test")

        # Drain three times — each attempt fails, the third crosses
        # max_attempts and transitions the row to ``dead``.
        main.drain_queue(queue, db, embedder, threader)
        main.drain_queue(queue, db, embedder, threader)
        main.drain_queue(queue, db, embedder, threader)

        assert not db.is_indexed(str(dest))
        assert queue.stats() == {"queued": 0, "dead": 1}
        row = db._conn.execute(
            "SELECT last_stage, last_error FROM indexing_jobs WHERE filepath = ?",
            (str(dest),),
        ).fetchone()
        assert row["last_stage"] == "embed"
        assert "ollama still down" in row["last_error"]

    def test_missing_file_routes_to_skip_not_retry(self, tmp_path):
        # Models the mbsync flag-rename race: file existed at enqueue
        # time, then mbsync renamed it (added an IMAP flag suffix)
        # before the worker could read it. The original path is gone
        # forever; retrying it 5 times wastes ~30 minutes of backoff
        # before dead-lettering, and the renamed file enters the queue
        # under its new name via a fresh IN_MOVED_TO event anyway.
        # ``_index_one_file`` must distinguish this from EACCES so the
        # worker can drop the row instead of consuming retry budget.
        dest = tmp_path / "INBOX" / "cur" / "definitely-not-here.eml"

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM

        ok, stage, err, _ = main._index_one_file(dest, db, embedder, threader)

        assert ok is False
        assert stage == "parse_skipped_missing"
        assert err is not None
        assert "FileNotFoundError" in err or "No such file" in err

    def test_drain_queue_skips_row_when_file_missing_at_parse(self, tmp_path):
        # End-to-end: enqueue a path that doesn't exist on disk, drain,
        # confirm the row was DELETED (not dead-lettered, not retained
        # in the queue with attempts incremented). One drain pass; if
        # this regressed and routed to mark_failed instead, attempts
        # would be 1 and status would be queued (with backoff).
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM
        queue = _make_queue(db)

        gone = tmp_path / "INBOX" / "cur" / "vanished.eml"
        queue.enqueue(str(gone), REASON_INITIAL_SCAN)

        main.drain_queue(queue, db, embedder, threader)

        # Row is gone — no retry, no dead-letter row.
        assert queue.stats() == {"queued": 0, "dead": 0}
        row = db._conn.execute(
            "SELECT 1 FROM indexing_jobs WHERE filepath = ?", (str(gone),)
        ).fetchone()
        assert row is None

    def test_unreadable_file_routes_to_retry_not_terminal_success(self, tmp_path):
        # Models the mbsync 0600→0644 chmod race: the watchdog enqueues a
        # newly-delivered file before mbsync's post-sync chmod hook makes
        # it readable. ``_index_one_file`` must surface that as a parse
        # failure so the queue retries on backoff. The previous behavior
        # (parse_email caught EACCES, returned None, worker treated None
        # as terminal success) silently dropped the message.
        import os

        dest = tmp_path / "INBOX" / "new" / "msg.eml"
        _write_eml(dest, "race@example.com")
        os.chmod(dest, 0o000)

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM
        try:
            ok, stage, err, _ = main._index_one_file(dest, db, embedder, threader)
        finally:
            os.chmod(dest, 0o644)

        assert ok is False
        assert stage == "parse"
        assert err is not None
        assert "PermissionError" in err or "Errno 13" in err


class TestValidateEmbeddingDim:
    def test_matching_dim_passes_silently(self):
        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM
        main._validate_embedding_dim(embedder)
        embedder.embed.assert_called_once()

    def test_mismatched_dim_raises_systemexit(self):
        """A 1024-dim model (e.g. mxbai-embed-large) against a 768-reserved
        schema must fail fast at startup rather than surface later as a
        cryptic sqlite-vec insert error."""
        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * (EMBEDDING_DIM + 256)
        with pytest.raises(SystemExit) as exc_info:
            main._validate_embedding_dim(embedder)
        assert str(EMBEDDING_DIM) in str(exc_info.value)


class TestIndexOneFileChunking:
    """End-to-end of the schema-v9 chunker integration through the
    real ``_index_one_file`` path — chunker is invoked for each new
    message, every new chunk gets an embed call, chunks land in the
    three chunk tables, and the thread vector is the mean of those
    chunk vectors rather than a single embed of the merged body.
    """

    def test_chunks_land_and_thread_vector_is_chunk_mean(self, tmp_path):

        db_path = tmp_path / "db" / "mail.db"
        db = Database(db_path)
        threader = Threader(db)

        # A multi-paragraph body so the chunker emits at least one
        # chunk; defaults are tuned to ~350 token target so a short
        # body fits in one chunk, exercising the "single chunk per
        # message" path that nonetheless writes through the chunk
        # tables and drives the mean-vector computation.
        body = "Paragraph one with some content.\n\nParagraph two follows.\n"
        dest = tmp_path / "INBOX" / "cur" / "msg.eml"
        dest.parent.mkdir(parents=True)
        dest.write_text(
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: chunked\r\n"
            "Message-ID: <chunked@x>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n" + body,
            encoding="utf-8",
        )

        # MagicMock returns the SAME embedding for every call. The thread
        # vector — computed as the mean of all chunk embeddings — must
        # therefore equal that embedding regardless of how many chunks
        # the chunker emitted.
        chunk_vec = [0.42] * EMBEDDING_DIM
        embedder = MagicMock()
        embedder.embed.return_value = chunk_vec

        # Patch MAILDIR_PATH so parse_email's relative-folder calculation
        # works against tmp_path — the indexer normally roots that at
        # /maildir.
        import src.main as main_mod

        original_root = main_mod.MAILDIR_PATH
        main_mod.MAILDIR_PATH = tmp_path
        try:
            ok, stage, err, _ = main._index_one_file(dest, db, embedder, threader)
        finally:
            main_mod.MAILDIR_PATH = original_root

        assert ok, f"failed at {stage}: {err}"

        # Chunk(s) for this message landed in all three indexes.
        chunk_ids = db.get_chunk_ids_for_message("chunked@x")
        assert len(chunk_ids) >= 1
        for cid in chunk_ids:
            vec_count = db._conn.execute(
                "SELECT COUNT(*) FROM message_chunks_vec WHERE chunk_id = ?", (cid,)
            ).fetchone()[0]
            assert vec_count == 1

        # Thread vector equals the (constant) chunk vector — proves the
        # mean-of-chunks path drove the upsert, not a separate embed of
        # the merged body.
        import struct

        row = db._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?", ("chunked@x",)
        ).fetchone()
        assert row is not None
        stored = list(struct.unpack(f"{EMBEDDING_DIM}f", row["embedding"]))
        assert stored == pytest.approx(chunk_vec, rel=1e-5)

    def test_replay_same_message_skips_re_embedding_existing_chunks(self, tmp_path):
        """Idempotency: chunking the same body twice must not re-embed
        chunks that are already stored. The diff path keys on
        deterministic chunk_ids so a re-index burns zero extra Ollama
        round-trips."""
        db_path = tmp_path / "db" / "mail.db"
        db = Database(db_path)
        threader = Threader(db)

        dest = tmp_path / "INBOX" / "cur" / "msg.eml"
        dest.parent.mkdir(parents=True)
        dest.write_text(
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: replay\r\n"
            "Message-ID: <replay@x>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "Single short paragraph.\n",
            encoding="utf-8",
        )

        embedder = MagicMock()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM

        import src.main as main_mod

        original_root = main_mod.MAILDIR_PATH
        main_mod.MAILDIR_PATH = tmp_path
        try:
            ok, _, _, _ = main._index_one_file(dest, db, embedder, threader)
            assert ok
            first_call_count = embedder.embed.call_count

            # Second pass: same file, same body, same chunk_ids.
            # Threader will see the existing thread and produce a Thread
            # whose ``messages`` list contains just this re-arrived
            # message; the chunker emits the same chunk_ids; the diff
            # path skips them all and embed should not be called again.
            ok2, _, _, _ = main._index_one_file(dest, db, embedder, threader)
            assert ok2
        finally:
            main_mod.MAILDIR_PATH = original_root

        # The second pass should not have triggered any new embed calls.
        assert embedder.embed.call_count == first_call_count

    def test_attachment_embed_failure_does_not_persist_partial_state(self, tmp_path):
        """Attachment chunk embedding now runs in the embed phase, before the
        DB write transaction opens. A failing Ollama call must surface as a
        retryable embed-stage failure and leave zero rows behind — neither
        the body, nor the attachment row, nor the extraction cache should
        be persisted, so the queue can replay the whole message cleanly."""
        db_path = tmp_path / "db" / "mail.db"
        db = Database(db_path)
        threader = Threader(db)

        dest = tmp_path / "INBOX" / "cur" / "msg.eml"
        _write_eml_with_text_attachment(dest, "attachment-retry@x")

        embedder = MagicMock()
        embedder.embed.side_effect = [
            [0.1] * EMBEDDING_DIM,  # body chunk
            RuntimeError("ollama attachment failure"),
        ]

        import src.main as main_mod

        original_root = main_mod.MAILDIR_PATH
        main_mod.MAILDIR_PATH = tmp_path
        try:
            ok, stage, err, _ = main._index_one_file(dest, db, embedder, threader)
        finally:
            main_mod.MAILDIR_PATH = original_root

        assert not ok
        # Attachment embedding now happens in the embed phase, outside the
        # ``with db.transaction()`` block, so the failure surfaces as
        # ``embed`` rather than ``db_write``. The queue retries on either.
        assert stage == "embed"
        assert err is not None
        assert "ollama attachment failure" in err
        assert not db.is_indexed(str(dest))
        assert db.count_total_messages() == 0
        assert not db.get_chunk_ids_for_message("attachment-retry@x")
        assert db._conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
        assert db._conn.execute("SELECT COUNT(*) FROM attachment_extractions").fetchone()[0] == 0
