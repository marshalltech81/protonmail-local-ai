"""Tests for src/main.py.

Covers the watchdog-facing behaviors that cannot be verified by
``database`` or ``threader`` tests alone: that ``on_moved`` indexes the
destination of a Maildir rename (standard delivery path), that
``initial_index`` refreshes the health file periodically so long scans
do not exceed ``HEALTH_MAX_AGE_SECONDS``, and that the startup probe
validates the running embedding model's output dimension against the
schema-reserved vector dimension.

``main`` orchestrates watchdog, the embedding service, and filesystem I/O; these tests
exercise it with stub collaborators rather than booting a live indexer.
"""

from pathlib import Path

import pytest
from src import main
from src.database import EMBEDDING_DIM, Database
from src.queue import REASON_INITIAL_SCAN, IndexingQueue
from src.threader import Threader

from tests.conftest import make_mock_embedder


class _FakeEvent:
    def __init__(self, src_path: str, dest_path: str, is_directory: bool = False):
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


def _make_queue(db: Database) -> IndexingQueue:
    """Queue with tight retry limits so tests that exercise failure
    paths don't wait on real-world 30 s backoffs."""
    return IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)


def _write_eml(
    path: Path,
    message_id: str,
    subject: str = "Hello",
    *,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    date: str = "Mon, 01 Jan 2024 12:00:00 +0000",
    from_addr: str = "alice@example.com",
    to_addr: str = "bob@example.com",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = [
        f"From: {from_addr}",
        f"To: {to_addr}",
        f"Subject: {subject}",
        f"Message-ID: <{message_id}>",
        f"Date: {date}",
        "Content-Type: text/plain; charset=utf-8",
    ]
    if in_reply_to:
        headers.append(f"In-Reply-To: <{in_reply_to}>")
    if references:
        headers.append("References: " + " ".join(f"<{r}>" for r in references))
    path.write_text(
        "\r\n".join(headers) + f"\r\n\r\nBody of {message_id}.\r\n",
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


class TestReadEmbedApiKey:
    """Coverage for ``main._read_embed_api_key``.

    The Docker secret path takes precedence over the env var; when the
    secret file is unreadable the function falls back to env rather
    than crashing the indexer at import time.
    """

    def test_returns_secret_file_contents_stripped(self, tmp_path, monkeypatch):
        secret = tmp_path / "embed_api_key"
        secret.write_text("  sk-abc123\n", encoding="utf-8")  # pragma: allowlist secret
        monkeypatch.setattr(main, "Path", lambda _p: secret)
        monkeypatch.delenv("EMBED_API_KEY", raising=False)
        assert main._read_embed_api_key() == "sk-abc123"  # pragma: allowlist secret

    def test_falls_back_to_env_when_secret_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "Path", lambda _p: tmp_path / "does-not-exist")
        monkeypatch.setenv("EMBED_API_KEY", "  env-key  ")
        assert main._read_embed_api_key() == "env-key"

    def test_returns_empty_when_neither_source_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "Path", lambda _p: tmp_path / "does-not-exist")
        monkeypatch.delenv("EMBED_API_KEY", raising=False)
        assert main._read_embed_api_key() == ""

    def test_falls_back_to_env_on_oserror_reading_secret(self, monkeypatch, caplog):
        # If the secret file exists but can't be read (perms regression
        # in a future deploy), don't crash — log and fall through to env
        # so the operator can recover by setting EMBED_API_KEY.

        class _UnreadableSecretPath:
            def exists(self) -> bool:
                return True

            def read_text(self, **_kwargs) -> str:
                raise PermissionError("simulated perms regression")

        monkeypatch.setattr(main, "Path", lambda _p: _UnreadableSecretPath())
        monkeypatch.setenv("EMBED_API_KEY", "fallback-key")
        with caplog.at_level("WARNING"):
            assert main._read_embed_api_key() == "fallback-key"
        assert "could not read" in caplog.text


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

        embedder = make_mock_embedder()
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

        embedder = make_mock_embedder()
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

        embedder = make_mock_embedder()
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
        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM

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
        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)

        main.initial_index(db, embedder, threader, _make_queue(db))

        row = db._conn.execute(
            "SELECT folder FROM threads WHERE thread_id = 'nested@example.com'"
        ).fetchone()
        assert row["folder"] == "Clients/ABC"


class TestInitialIndexHeartbeat:
    def test_health_file_refreshed_at_least_once_per_processed_message(self, tmp_path, monkeypatch):
        """``initial_index`` must refresh the heartbeat often enough
        that embedding a large mailbox does not exceed
        ``HEALTH_MAX_AGE_SECONDS`` mid-scan. The batched two-phase
        indexer touches the heartbeat at four points per batch: once
        per Phase 1 commit, once per Phase 2a entry (slow attachment
        OCR cannot starve the heartbeat), once before the bulk embed
        call (slow cloud round-trip cannot starve it either), and once
        per Phase 2c commit. The exact count varies with batch
        boundaries but must be at least N to prove the heartbeat keeps
        up with progress."""
        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        inbox.mkdir(parents=True)

        message_count = 5
        for i in range(message_count):
            _write_eml(inbox / f"m{i}.eml", f"m{i}@example.com")

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)

        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM

        touches: list[None] = []
        monkeypatch.setattr(main, "touch_health_file", lambda: touches.append(None))
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)

        main.initial_index(db, embedder, threader, _make_queue(db))

        # At least one touch per processed message — Phase 1 + Phase 2a
        # + Phase 2c each fire once per message, plus one pre-embed
        # touch per batch. The lower bound matches the spec; an upper
        # bound would over-pin the implementation.
        assert len(touches) >= message_count

    def test_phase2a_per_entry_heartbeat_does_not_starve_during_slow_chunking(
        self, tmp_path, monkeypatch
    ):
        """Phase 2a runs chunk + extract + OCR sequentially across the
        batch before Phase 2b's pre-embed touch. With
        ``INITIAL_INDEX_BATCH_SIZE=50`` and an attachment-heavy mailbox
        the cumulative Phase 2a work can exceed
        ``HEALTH_MAX_AGE_SECONDS`` (90 s in the healthcheck script).
        The drainer must touch the heartbeat after every Phase 2a
        entry; this test pins that contract by making each entry
        observably slow and asserting the touch count grows
        commensurately during Phase 2a."""
        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        inbox.mkdir(parents=True)

        # 5 messages so Phase 2a runs the loop body 5 times within a
        # single batch (default batch_size=50 holds them all).
        for i in range(5):
            _write_eml(inbox / f"m{i}.eml", f"m{i}@example.com")

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)

        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM

        # Track touches with phase markers so the assertion can prove
        # touches happened DURING Phase 2a, not just before/after.
        marker_touches: list[str] = []
        from src import main as main_mod

        # Wrap _phase2a_collect_chunks so each call records a marker
        # before AND after, sandwiching where the per-entry heartbeat
        # touch must fire.
        original_phase2a = main_mod._phase2a_collect_chunks

        def slow_phase2a(state, db_arg, all_texts):
            marker_touches.append("phase2a:enter")
            result = original_phase2a(state, db_arg, all_texts)
            marker_touches.append("phase2a:exit")
            return result

        def recording_touch():
            marker_touches.append("touch")

        monkeypatch.setattr(main_mod, "_phase2a_collect_chunks", slow_phase2a)
        monkeypatch.setattr(main_mod, "touch_health_file", recording_touch)
        monkeypatch.setattr(main_mod, "MAILDIR_PATH", maildir)

        main.initial_index(db, embedder, threader, _make_queue(db))

        # Find each Phase 2a entry's exit and the next event after it.
        # A "touch" must appear after every "phase2a:exit" before the
        # next "phase2a:enter" or the bulk-embed touch — that's the
        # per-entry heartbeat we're asserting on.
        phase2a_exits = [i for i, m in enumerate(marker_touches) if m == "phase2a:exit"]
        assert len(phase2a_exits) == 5, f"expected 5 Phase 2a calls, got {len(phase2a_exits)}"
        for exit_idx in phase2a_exits:
            # The very next event after exit must be a touch.
            assert exit_idx + 1 < len(marker_touches), "no event followed Phase 2a exit"
            assert marker_touches[exit_idx + 1] == "touch", (
                f"Phase 2a entry at index {exit_idx} was not followed by a "
                f"heartbeat touch — got {marker_touches[exit_idx + 1]!r} "
                f"instead. Without a per-entry touch, a 50-message batch with "
                f"slow OCR can age the health file past HEALTH_MAX_AGE_SECONDS."
            )


class TestInitialIndexDeadLetterRespect:
    """``initial_index`` must NOT re-enqueue dead-lettered files.

    The bug this guards against was observed in production: a file
    that exhausted its retries (e.g. embedding service 500'd on a poison-pill
    payload) would be re-enqueued on every container restart because
    the initial scan walks the Maildir, sees the file isn't in
    ``messages``, and clobbers the dead row via INSERT OR REPLACE.
    Each restart then burns another 5-attempt cascade and re-deads
    the same file. ``is_dead`` checks the queue's status before
    re-enqueueing so dead-lettered files stay dead until something
    really changes about them.
    """

    def test_dead_lettered_file_is_not_re_enqueued_on_initial_index(self, tmp_path, monkeypatch):
        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        inbox.mkdir(parents=True)

        # File exists on disk and has previously failed all retries.
        # We synthesize the dead row directly: it's faster and more
        # reliable than driving a real failure through the worker.
        dest = inbox / "msg.eml"
        _write_eml(dest, "deadletter@example.com")

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM
        queue = IndexingQueue(db, max_attempts=1, base_backoff_seconds=0)

        # Drive one failure to land the row in dead state. We patch
        # parse_email so the failure is deterministic and fast.
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        monkeypatch.setattr(
            main, "parse_email", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("nope"))
        )
        queue.enqueue(str(dest), REASON_INITIAL_SCAN)
        main.drain_queue(queue, db, embedder, threader)
        assert queue.is_dead(str(dest)) is True

        # Restore the real parse_email so initial_index would otherwise
        # succeed on this file. The point of the test is that
        # initial_index doesn't TRY to re-process it because the row
        # is dead.
        from src.parser import parse_email as real_parse_email

        monkeypatch.setattr(main, "parse_email", real_parse_email)

        # Run initial_index. The dead row should be left alone.
        before_attempts = db.queue_get_attempts(str(dest))
        main.initial_index(db, embedder, threader, queue)
        after_attempts = db.queue_get_attempts(str(dest))

        # Row still exists, still dead, attempts unchanged.
        assert queue.is_dead(str(dest)) is True
        assert before_attempts == after_attempts


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

        embedder = make_mock_embedder()
        # First embed call raises (transient embedding service error); second call succeeds.
        embedder.embed.side_effect = [
            RuntimeError("embedding service unavailable"),
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
        # Phase 1 commits thread membership + indexed_files eagerly so
        # subsequent batch members can thread against this message; a
        # Phase 2 (embed) failure leaves the file ``is_indexed`` but
        # chunkless until the retry succeeds. The queue's retry
        # cascade is the contract that closes the loop.
        assert db.is_indexed(str(dest))
        assert not db.get_chunk_ids_for_message("retry@example.com")
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

    def test_parser_content_pathology_routes_to_queue_retry(self, tmp_path, monkeypatch):
        # Behavior contract: an exception raised mid-parse (malformed
        # MIME the ``email`` module cannot decompose, html2text
        # blowup, anything the body / attachment walker raises and
        # does not catch locally) routes through the queue's retry +
        # dead-letter cascade. ``parse_email`` does NOT have a blanket
        # ``except Exception`` that would collapse such failures into
        # ``None`` — that shape silently un-indexed every affected
        # file with no operator visibility. After ``max_attempts``
        # failures the queue row is ``status='dead'`` and
        # ``indexed_files`` has no row for the file (the worker did
        # NOT mark it successfully processed).
        dest = tmp_path / "INBOX" / "new" / "broken.eml"
        _write_eml(dest, "broken@example.com")

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        queue = _make_queue(db)  # max_attempts=3
        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM

        # Simulate a parser content-pathology failure by patching the
        # body/attachment walker to raise. ``parse_email`` no longer
        # has a broad ``except Exception`` to catch this, so it
        # propagates out and the worker marks the queue row failed.
        from src import parser

        def boom(msg):
            raise RuntimeError("simulated html2text runaway")

        monkeypatch.setattr(parser, "_extract_body_and_attachments", boom)

        queue.enqueue(str(dest), "test")
        main.drain_queue(queue, db, embedder, threader)
        main.drain_queue(queue, db, embedder, threader)
        main.drain_queue(queue, db, embedder, threader)

        # Critical: the file must NOT be in indexed_files. A blanket
        # ``except Exception`` in the parser would collapse the
        # exception into ``message is None`` and the worker would
        # call ``mark_succeeded``, leaving indexed_files populated
        # and the message permanently invisible to retrieval. The
        # current parser propagates so the worker treats this as a
        # real parse-stage failure.
        assert not db.is_indexed(str(dest)), (
            "parser content-pathology must NOT be treated as terminal "
            "success — silently dropping the file is the regression "
            "this contract guards against"
        )
        assert queue.stats() == {"queued": 0, "dead": 1}
        row = db._conn.execute(
            "SELECT last_stage, last_error FROM indexing_jobs WHERE filepath = ?",
            (str(dest),),
        ).fetchone()
        assert row["last_stage"] == "parse"
        assert "simulated html2text runaway" in row["last_error"]

    def test_persistent_embed_failure_transitions_to_dead(self, tmp_path):
        dest = tmp_path / "INBOX" / "new" / "msg.eml"
        _write_eml(dest, "giveup@example.com")

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        queue = _make_queue(db)  # max_attempts=3

        embedder = make_mock_embedder()
        embedder.embed.side_effect = RuntimeError("embedding service still down")

        queue.enqueue(str(dest), "test")

        # Drain three times — each attempt fails, the third crosses
        # max_attempts and transitions the row to ``dead``.
        main.drain_queue(queue, db, embedder, threader)
        main.drain_queue(queue, db, embedder, threader)
        main.drain_queue(queue, db, embedder, threader)

        # Under the batched pipeline, Phase 1 commits thread membership
        # + indexed_files before Phase 2 (embed) runs. A persistent
        # Phase 2 failure leaves the file ``is_indexed=True`` (the
        # thread row is durable) but chunkless and dead-lettered. The
        # ``_recover_zero_vector_threads`` sweep picks this up later
        # if the underlying cause clears; until then, the dead-letter
        # row is the operator-visible signal.
        assert db.is_indexed(str(dest)), (
            "Phase 1 commit lands eagerly even when Phase 2 fails — "
            "see _drain_queue_batched failure isolation"
        )
        assert not db.get_chunk_ids_for_message("giveup@example.com"), (
            "Phase 2c never ran, so no chunks for this message"
        )
        assert queue.stats() == {"queued": 0, "dead": 1}
        row = db._conn.execute(
            "SELECT last_stage, last_error FROM indexing_jobs WHERE filepath = ?",
            (str(dest),),
        ).fetchone()
        assert row["last_stage"] == "embed"
        assert "embedding service still down" in row["last_error"]

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
        embedder = make_mock_embedder()
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
        embedder = make_mock_embedder()
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
        embedder = make_mock_embedder()
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
        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM
        main._validate_embedding_dim(embedder)
        embedder.embed.assert_called_once()

    def test_mismatched_dim_raises_systemexit(self):
        """A 1024-dim model (e.g. mxbai-embed-large) against a 4096-reserved
        schema must fail fast at startup rather than surface later as a
        cryptic sqlite-vec insert error."""
        embedder = make_mock_embedder()
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

        # MagicMock returns the SAME (already unit-norm) embedding for
        # every call. The thread vector — computed as the mean of all
        # chunk embeddings, then unit-normalized at the DB write
        # boundary — must therefore equal that embedding regardless of
        # how many chunks the chunker emitted. Using a unit-norm
        # chunk_vec keeps the assertion direct: mean-of-identical-unit-
        # vectors is itself unit-norm, so the normalize step is a
        # no-op against this fixture.
        unit_component = 1.0 / (EMBEDDING_DIM**0.5)
        chunk_vec = [unit_component] * EMBEDDING_DIM
        embedder = make_mock_embedder()
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
        deterministic chunk_ids so a re-index burns zero extra embedding
        service round-trips."""
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

        embedder = make_mock_embedder()
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

    def test_attachment_embed_failure_does_not_persist_partial_chunks(self, tmp_path):
        """A failing attachment embed must surface as a retryable
        embed-stage failure and leave NO chunk / attachment / extraction
        rows behind. Phase 1's thread + indexed_files commits land
        eagerly (the C1 batched-pipeline contract), so ``is_indexed``
        is True after Phase 2 fails — but the chunk / attachment /
        extraction tables are guarded by Phase 2c's per-message
        transaction and must remain empty so the queue retry replays
        the whole message cleanly when the embedder recovers."""
        db_path = tmp_path / "db" / "mail.db"
        db = Database(db_path)
        threader = Threader(db)

        dest = tmp_path / "INBOX" / "cur" / "msg.eml"
        _write_eml_with_text_attachment(dest, "attachment-retry@x")

        embedder = make_mock_embedder()
        embedder.embed.side_effect = [
            [0.1] * EMBEDDING_DIM,  # body chunk
            RuntimeError("embedding service attachment failure"),
        ]

        import src.main as main_mod

        original_root = main_mod.MAILDIR_PATH
        main_mod.MAILDIR_PATH = tmp_path
        try:
            ok, stage, err, _ = main._index_one_file(dest, db, embedder, threader)
        finally:
            main_mod.MAILDIR_PATH = original_root

        assert not ok
        # Attachment embedding happens in the embed phase, outside the
        # Phase 2c write transaction, so the failure surfaces as
        # ``embed`` rather than ``db_write``.
        assert stage == "embed"
        assert err is not None
        assert "embedding service attachment failure" in err
        # Phase 1 commit is durable (thread membership + indexed_files);
        # Phase 2c never ran, so chunks / attachments / extractions
        # remain unwritten and the queue retry can replay cleanly.
        assert not db.get_chunk_ids_for_message("attachment-retry@x")
        assert db._conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
        assert db._conn.execute("SELECT COUNT(*) FROM attachment_extractions").fetchone()[0] == 0


class TestBatchedInitialIndex:
    """C1 invariants for the cross-message batched initial indexer.

    Phase 1 (per message) commits thread membership with a seed
    thread vector chosen from a three-case priority chain: mean of
    existing chunk vectors when the thread is already indexed with
    content; the prior threads_vec row when the thread is chunkless
    but has a non-zero vector (subject-fallback threads); placeholder
    zero only for genuinely new threads. So (a) the next message in
    the batch sees this message's thread when computing its own
    assignment, and (b) a Phase 2 failure cannot regress an
    already-good thread vector — chunk-derived OR subject-fallback.
    Phase 2 batches the embed call across the whole batch; Phase 2c
    per-message commits the chunk + vector writes and replaces the
    seed thread vector with the real mean-of-chunks vector (or a
    subject-fallback embed for chunk-less threads). The tests below
    pin the load-bearing correctness properties: in-batch sibling
    threading, no-chunk subject fallback, Phase 2 failure
    preservation of both chunk-bearing and chunkless thread vectors,
    and partial-failure isolation across phases.
    """

    def _setup(self, tmp_path):
        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        inbox.mkdir(parents=True)
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        return maildir, inbox, db, threader

    def _run(self, db, embedder, threader, queue, monkeypatch, maildir):
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        monkeypatch.setattr(main, "touch_health_file", lambda: None)
        main.initial_index(db, embedder, threader, queue)

    def test_in_batch_reply_chain_merges_into_single_thread(self, tmp_path, monkeypatch):
        # The headline correctness test for C1: when message A and its
        # reply B land in the same batch, B must thread into A's thread
        # rather than creating a sibling. Phase 1 commits A's thread
        # before B's threader runs, so B's In-Reply-To lookup hits A's
        # message_id in message_thread_map.
        maildir, inbox, db, threader = self._setup(tmp_path)
        _write_eml(inbox / "a.eml", "a@example.com", subject="Project kickoff")
        _write_eml(
            inbox / "b.eml",
            "b@example.com",
            subject="Re: Project kickoff",
            in_reply_to="a@example.com",
            date="Mon, 01 Jan 2024 13:00:00 +0000",
            from_addr="bob@example.com",
            to_addr="alice@example.com",
        )

        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM
        queue = _make_queue(db)
        self._run(db, embedder, threader, queue, monkeypatch, maildir)

        thread_a = db.find_thread_by_message_id("a@example.com")
        thread_b = db.find_thread_by_message_id("b@example.com")
        assert thread_a is not None and thread_b is not None
        assert thread_a == thread_b, (
            "Reply B must thread into A's thread when both arrive in the "
            "same batch — Phase 1 must commit A's thread membership before "
            "B's threader runs"
        )

    def test_partial_phase1_parse_failure_does_not_stall_batch(self, tmp_path, monkeypatch):
        # One message in the batch has a corrupt header; the other two
        # must still index successfully. The corrupt one ends up
        # marked failed (or skipped) without aborting Phase 2 for the
        # survivors.
        maildir, inbox, db, threader = self._setup(tmp_path)
        _write_eml(inbox / "ok1.eml", "ok1@example.com")
        # Corrupt — no headers at all, parser will return None or raise.
        (inbox / "bad.eml").write_text("not a real email", encoding="utf-8")
        _write_eml(inbox / "ok2.eml", "ok2@example.com")

        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.2] * EMBEDDING_DIM
        queue = _make_queue(db)
        self._run(db, embedder, threader, queue, monkeypatch, maildir)

        # Survivors should be indexed end-to-end (chunks + vectors).
        assert db.is_indexed(str(inbox / "ok1.eml"))
        assert db.is_indexed(str(inbox / "ok2.eml"))
        # Bad message should not be indexed.
        assert not db.is_indexed(str(inbox / "bad.eml"))
        # Survivors have at least one chunk vector each (Phase 2c
        # actually wrote chunks, not just Phase 1 placeholder).
        assert db.get_chunk_ids_for_message("ok1@example.com")
        assert db.get_chunk_ids_for_message("ok2@example.com")

    def test_phase2_embed_failure_leaves_phase1_state_and_requeues(self, tmp_path, monkeypatch):
        # When the bulk embed call fails, every message in the batch
        # has Phase 1 commits (thread + map + indexed_files with a
        # placeholder zero-vector) but no chunks/vectors. The queue
        # rows go back to 'queued' (via mark_failed) so the next pass
        # retries Phase 2.
        maildir, inbox, db, threader = self._setup(tmp_path)
        for i in range(3):
            _write_eml(inbox / f"m{i}.eml", f"m{i}@example.com")

        embedder = make_mock_embedder()
        embedder.embed_batch.side_effect = RuntimeError("simulated cloud outage")
        queue = _make_queue(db)
        self._run(db, embedder, threader, queue, monkeypatch, maildir)

        # Phase 1 commits persisted: thread membership exists.
        for i in range(3):
            assert db.find_thread_by_message_id(f"m{i}@example.com") is not None
        # Phase 2 never wrote chunks, so search-by-chunks misses these.
        for i in range(3):
            assert not db.get_chunk_ids_for_message(f"m{i}@example.com")
        # Queue rows are marked failed (advancing attempts), eligible for
        # retry on the next pass. With max_attempts=3 and the embed
        # always failing, they end up dead-lettered after retries.
        stats = queue.stats()
        # All 3 messages exhausted retries (3 attempts each) → dead.
        assert stats["dead"] == 3
        assert stats["queued"] == 0

    def test_no_chunk_message_uses_subject_fallback_for_thread_vector(self, tmp_path, monkeypatch):
        # Regression: a message with no body chunks and no attachment
        # chunks (blank body, only-quoted body that strips to empty,
        # all-unsupported attachments) must NOT leave its thread
        # permanently stuck at the Phase 1 placeholder zero-vector.
        # The pre-batched path embedded the subject as a fallback via
        # _seed_thread_embedding; the batched path threads the same
        # fallback through Phase 2b in the shared embed_batch call.
        import struct

        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        inbox.mkdir(parents=True)

        # Empty body — no chunks emitted by chunk_message — and no
        # attachments. The fallback path is the only way this thread
        # gets a non-zero vector.
        eml = inbox / "blank.eml"
        eml.write_text(
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: Quarterly review\r\n"
            "Message-ID: <blank@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n",
            encoding="utf-8",
        )

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)

        # Embedder returns a deterministic non-zero vector so the
        # post-write check can distinguish "fallback embed ran" from
        # "placeholder zero stayed". 0.5 is exactly representable in
        # float32 so it round-trips through threads_vec storage
        # without the ~1e-8 quantization noise of less-friendly
        # constants like 0.42.
        sentinel = [0.5] * EMBEDDING_DIM
        embedder = make_mock_embedder()
        embedder.embed.return_value = sentinel

        queue = _make_queue(db)
        self._run(db, embedder, threader, queue, monkeypatch, maildir)

        # Find the thread for the blank message.
        thread_id = db.find_thread_by_message_id("blank@example.com")
        assert thread_id is not None

        row = db._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        assert row is not None
        # ``sqlite_vec.serialize_float32`` writes the array as
        # little-endian float32 bytes; unpack the same shape for
        # comparison.
        raw = row["embedding"]
        stored_vec = list(struct.unpack(f"<{len(raw) // 4}f", raw))
        assert any(v != 0.0 for v in stored_vec), (
            "thread vector must NOT be the placeholder zero — Phase 2a "
            "should have added a subject fallback to the embed batch and "
            "Phase 2c should have used it instead of leaving the placeholder"
        )
        # The fallback should embed the subject string. Our mock
        # returns the same (non-unit) vector regardless of input.
        # ``replace_thread_vector`` normalizes at the boundary, so the
        # stored vector is the L2-normalized sentinel — that's enough
        # to prove the fallback path ran (vs. chunks-mean, which never
        # got chunks here, or the placeholder zero).
        from src.chunker import l2_normalize

        expected = l2_normalize(sentinel)
        assert stored_vec == pytest.approx(expected, abs=1e-6), (
            "thread vector should equal the L2-normalized embedder "
            "return value for the subject fallback, not be derived "
            "from chunks (none exist)"
        )

    def test_phase2_failure_preserves_existing_thread_vector(self, tmp_path, monkeypatch):
        # Regression: Phase 1 used to seed every upsert_thread with
        # _ZERO_THREAD_VECTOR. For a NEW message on an EXISTING thread
        # that already had a valid mean-of-chunks vector, the upsert
        # destroyed the prior vector before Phase 2 ran. If Phase 2
        # then failed (embed outage, queue retry, dead-letter), the
        # thread was left permanently zero — a real retrieval-quality
        # regression on the parent thread, triggered by a transient
        # embed error on a single new sibling message.
        import struct

        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        inbox.mkdir(parents=True)

        # First pass: index message A successfully so the thread has a
        # real, non-zero vector in threads_vec.
        _write_eml(inbox / "a.eml", "a@example.com", subject="Project alpha")
        sentinel = [0.25] * EMBEDDING_DIM  # exactly representable in float32
        embedder_ok = make_mock_embedder()
        embedder_ok.embed.return_value = sentinel
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        queue = _make_queue(db)
        self._run(db, embedder_ok, threader, queue, monkeypatch, maildir)

        thread_id = db.find_thread_by_message_id("a@example.com")
        assert thread_id is not None
        row = db._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        raw = row["embedding"]
        prior_vec = list(struct.unpack(f"<{len(raw) // 4}f", raw))
        # Stored thread vectors are L2-normalized at the DB write
        # boundary; compare against the normalized sentinel.
        from src.chunker import l2_normalize

        assert prior_vec == pytest.approx(l2_normalize(sentinel), abs=1e-6), (
            "first-pass vector should be the L2-normalized embedder response"
        )

        # Second pass: a reply B arrives that threads into A. The new
        # embedder fails during embed_batch (simulated cloud outage).
        # Phase 1 must seed the upsert with the existing thread's
        # chunk-mean so Phase 2's failure cannot regress the vector.
        _write_eml(
            inbox / "b.eml",
            "b@example.com",
            subject="Re: Project alpha",
            in_reply_to="a@example.com",
            date="Mon, 01 Jan 2024 13:00:00 +0000",
            from_addr="bob@example.com",
            to_addr="alice@example.com",
        )
        embedder_fail = make_mock_embedder()
        embedder_fail.embed_batch.side_effect = RuntimeError("simulated cloud outage")
        # Patch wait_exponential so retry-cascade tests don't sleep.
        monkeypatch.setattr("src.embedder.wait_exponential", lambda **_: lambda *_: 0)
        self._run(db, embedder_fail, threader, queue, monkeypatch, maildir)

        # B should NOT be indexed (Phase 2 failed). A's thread vector
        # MUST still match prior_vec — Phase 1's seed preserves it.
        row_after = db._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        assert row_after is not None, "thread row must still exist"
        raw_after = row_after["embedding"]
        vec_after = list(struct.unpack(f"<{len(raw_after) // 4}f", raw_after))
        assert vec_after == prior_vec, (
            "existing thread vector must survive a Phase 2 embed failure on a "
            "new sibling message — Phase 1 must seed with the existing "
            "chunk-mean, not the placeholder zero"
        )
        assert any(v != 0.0 for v in vec_after), (
            "sanity: stored vector must not be the zero placeholder"
        )

    def test_phase2_failure_preserves_chunkless_subject_fallback_vector(
        self, tmp_path, monkeypatch
    ):
        # Regression: Phase 1's seed used to fall through to
        # _ZERO_THREAD_VECTOR whenever the thread had no chunk
        # embeddings — including the case where a prior blank-body
        # message had stored a valid subject-fallback vector. A new
        # sibling on that chunkless thread + a transient embed
        # failure would then leave the parent thread permanently
        # zero, and is_indexed=True on the new message blocks normal
        # restart re-indexing. Pinned by reading the existing
        # threads_vec row in Phase 1 and preserving any non-zero
        # value when no chunks are available.
        import struct

        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        inbox.mkdir(parents=True)

        # First pass: blank-body message A. Phase 2a's subject fallback
        # writes a non-zero thread vector (no chunks committed).
        eml_a = inbox / "a.eml"
        eml_a.write_text(
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: Quarterly review\r\n"
            "Message-ID: <a@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n",
            encoding="utf-8",
        )
        sentinel = [0.5] * EMBEDDING_DIM
        embedder_ok = make_mock_embedder()
        embedder_ok.embed.return_value = sentinel
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        queue = _make_queue(db)
        self._run(db, embedder_ok, threader, queue, monkeypatch, maildir)

        thread_id = db.find_thread_by_message_id("a@example.com")
        assert thread_id is not None
        # Sanity: thread is chunkless but its vector is the subject
        # fallback, not zero.
        assert not db.get_thread_chunk_embeddings(thread_id), (
            "blank-body message must not leave chunks on the thread"
        )
        row = db._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        raw = row["embedding"]
        prior_vec = list(struct.unpack(f"<{len(raw) // 4}f", raw))
        # Stored thread vectors are L2-normalized at the DB write
        # boundary; compare against the normalized sentinel.
        from src.chunker import l2_normalize

        assert prior_vec == pytest.approx(l2_normalize(sentinel), abs=1e-6)

        # Second pass: a blank-body reply B threads into A. The new
        # embedder fails. Phase 1 must NOT seed with zero — there is no
        # chunk-mean to fall back to, but the prior subject-fallback
        # vector on threads_vec is the right thing to preserve.
        eml_b = inbox / "b.eml"
        eml_b.write_text(
            "From: bob@example.com\r\n"
            "To: alice@example.com\r\n"
            "Subject: Re: Quarterly review\r\n"
            "Message-ID: <b@example.com>\r\n"
            "In-Reply-To: <a@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 13:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n",
            encoding="utf-8",
        )
        embedder_fail = make_mock_embedder()
        embedder_fail.embed_batch.side_effect = RuntimeError("simulated cloud outage")
        monkeypatch.setattr("src.embedder.wait_exponential", lambda **_: lambda *_: 0)
        self._run(db, embedder_fail, threader, queue, monkeypatch, maildir)

        # Existing chunkless thread MUST still carry the subject vector,
        # even though Phase 2 never produced a new vector for B.
        row_after = db._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        assert row_after is not None
        raw_after = row_after["embedding"]
        vec_after = list(struct.unpack(f"<{len(raw_after) // 4}f", raw_after))
        assert vec_after == prior_vec, (
            "chunkless thread's subject-fallback vector must survive a Phase 2 "
            "failure on a new sibling message — Phase 1 must read the existing "
            "threads_vec row, not seed unconditionally with zero"
        )
        assert any(v != 0.0 for v in vec_after), (
            "sanity: stored vector must not be the zero placeholder"
        )

    def test_dead_lettered_zero_vector_thread_is_not_auto_recovered(self, tmp_path, monkeypatch):
        # Policy: a dead-lettered row from a deterministic Phase 2
        # failure stays dead until the operator intervenes. Without
        # this gate, every container restart (and every periodic
        # sweep) would resurrect the same poison-pill payload,
        # burning embedder quota indefinitely and undoing
        # ``initial_index``'s deliberate ``is_dead`` skip.
        #
        # Construction: first pass dead-letters via a deterministic
        # embedder failure → stuck thread (chunkless + zero vec) +
        # ``status='dead'`` queue row. Second pass with a healthy
        # embedder must NOT auto-recover the dead row.
        import struct

        maildir, inbox, db, threader = self._setup(tmp_path)
        _write_eml(inbox / "m.eml", "m@example.com", subject="Quarterly review")

        # First pass: embedder fails. Phase 1 commits, Phase 2 fails,
        # message dead-lettered after retries.
        embedder_fail = make_mock_embedder()
        embedder_fail.embed_batch.side_effect = RuntimeError("simulated outage")
        queue = _make_queue(db)
        monkeypatch.setattr("src.embedder.wait_exponential", lambda **_: lambda *_: 0)
        self._run(db, embedder_fail, threader, queue, monkeypatch, maildir)

        # Confirm the stuck state: dead row, zero vec, no chunks.
        thread_id = db.find_thread_by_message_id("m@example.com")
        assert thread_id is not None
        assert db.is_indexed(str(inbox / "m.eml"))
        assert not db.get_chunk_ids_for_message("m@example.com")
        row = db._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        raw = row["embedding"]
        stuck_vec = list(struct.unpack(f"<{len(raw) // 4}f", raw))
        assert all(v == 0.0 for v in stuck_vec), "first pass should leave zero vector"
        assert queue.is_dead(str(inbox / "m.eml")), "first pass should dead-letter the file"

        # Second pass with a working embedder: the dead row stays
        # dead, the thread stays at zero vec, no chunks materialize.
        # The ``initial_index`` walk skips because ``is_dead``, and the
        # recovery sweep skips because the file is dead-lettered.
        embedder_ok = make_mock_embedder()
        embedder_ok.embed.return_value = [0.5] * EMBEDDING_DIM
        self._run(db, embedder_ok, threader, queue, monkeypatch, maildir)

        assert queue.is_dead(str(inbox / "m.eml")), (
            "dead-lettered file must remain dead until operator intervention"
        )
        row_after = db._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        raw_after = row_after["embedding"]
        vec_after = list(struct.unpack(f"<{len(raw_after) // 4}f", raw_after))
        assert all(v == 0.0 for v in vec_after), (
            "thread vector must stay at zero — no auto-resurrection"
        )
        assert not db.get_chunk_ids_for_message("m@example.com"), (
            "no chunks should materialize without operator intervention"
        )

    def test_resurrect_dead_opt_in_recovers_dead_lettered_thread(self, tmp_path, monkeypatch):
        # The opt-in escape hatch — for a future operator tool that
        # confirms the underlying cause is fixed and clears the dead
        # state intentionally. Same precondition as the policy test
        # above; this one calls ``_recover_zero_vector_threads`` with
        # ``resurrect_dead=True`` directly to verify the path still
        # works when invoked deliberately.
        import struct

        from src.timings import TimingAggregator

        maildir, inbox, db, threader = self._setup(tmp_path)
        _write_eml(inbox / "m.eml", "m@example.com", subject="Quarterly review")

        embedder_fail = make_mock_embedder()
        embedder_fail.embed_batch.side_effect = RuntimeError("simulated outage")
        queue = _make_queue(db)
        monkeypatch.setattr("src.embedder.wait_exponential", lambda **_: lambda *_: 0)
        self._run(db, embedder_fail, threader, queue, monkeypatch, maildir)
        assert queue.is_dead(str(inbox / "m.eml"))

        # Operator action: resurrect the dead row, then drain with a
        # working embedder.
        re_enqueued = main._recover_zero_vector_threads(db, queue, resurrect_dead=True)
        assert re_enqueued == 1
        assert queue.has_pending_row(str(inbox / "m.eml"))

        embedder_ok = make_mock_embedder()
        embedder_ok.embed.return_value = [0.5] * EMBEDDING_DIM
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        monkeypatch.setattr(main, "touch_health_file", lambda: None)
        main._drain_queue_batched(
            db,
            embedder_ok,
            threader,
            queue,
            batch_size=8,
            timing_aggregator=TimingAggregator(window=10),
            max_passes=1,
        )

        thread_id = db.find_thread_by_message_id("m@example.com")
        row_after = db._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        vec_after = list(
            struct.unpack(f"<{len(row_after['embedding']) // 4}f", row_after["embedding"])
        )
        assert any(v != 0.0 for v in vec_after)
        assert db.get_chunk_ids_for_message("m@example.com")
        assert queue.stats() == {"queued": 0, "dead": 0}

    def test_recovery_sweep_skips_chunkless_subject_fallback_threads(self, tmp_path, monkeypatch):
        # Healthy chunkless threads (blank-body messages whose vector
        # came from Phase 2c's subject fallback) have no chunks but a
        # NON-ZERO threads_vec row. They must NOT be re-enqueued —
        # they're already correctly indexed.
        maildir, inbox, db, threader = self._setup(tmp_path)
        # Blank body → subject fallback path. Chunkless but non-zero
        # vector after a successful pass.
        eml = inbox / "blank.eml"
        eml.write_text(
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            "Subject: Quarterly review\r\n"
            "Message-ID: <blank@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n",
            encoding="utf-8",
        )
        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.5] * EMBEDDING_DIM
        queue = _make_queue(db)
        self._run(db, embedder, threader, queue, monkeypatch, maildir)

        # Sanity: chunkless but non-zero vector, queue empty.
        thread_id = db.find_thread_by_message_id("blank@example.com")
        assert thread_id is not None
        assert not db.get_chunk_ids_for_message("blank@example.com")
        assert queue.stats() == {"queued": 0, "dead": 0}

        # Recovery sweep should be a no-op — the DB query filters out
        # non-zero-vec chunkless threads.
        recovered = main._recover_zero_vector_threads(db, queue)
        assert recovered == 0
        assert queue.stats() == {"queued": 0, "dead": 0}

    def test_recovery_sweep_skips_files_with_active_queued_row(self, tmp_path, monkeypatch):
        # If the file is already in 'queued' state (active retry
        # cascade), the recovery sweep must NOT clobber its row —
        # that would reset attempts mid-cascade and could let a
        # genuinely-broken file loop forever.
        maildir, inbox, db, threader = self._setup(tmp_path)
        _write_eml(inbox / "m.eml", "m@example.com")

        # Manually create the stuck state: Phase 1 commits + zero vec
        # + no chunks + an ACTIVE queued row. (Mimic Phase 2 failing
        # but not yet exhausting retries.)
        from src.threader import Threader as _Threader

        threader_local = _Threader(db)
        from src.parser import parse_email

        msg = parse_email(inbox / "m.eml", maildir_root=maildir)
        thread = threader_local.assign_thread(msg)
        db.upsert_thread(thread, [0.0] * EMBEDDING_DIM)  # zero seed

        queue = _make_queue(db)
        # Insert an active 'queued' row mimicking a retry attempt
        # mid-cascade: enqueue resets attempts to 0 by design.
        queue.enqueue(str(inbox / "m.eml"), reason="initial_scan")
        assert queue.has_pending_row(str(inbox / "m.eml"))
        attempts_before = db.queue_get_attempts(str(inbox / "m.eml"))

        recovered = main._recover_zero_vector_threads(db, queue)
        # Active row → skipped, NOT re-enqueued.
        assert recovered == 0
        # Row still queued, attempts unchanged.
        assert queue.has_pending_row(str(inbox / "m.eml"))
        assert db.queue_get_attempts(str(inbox / "m.eml")) == attempts_before

    def test_phase2c_commit_failure_isolates_to_one_message(self, tmp_path, monkeypatch):
        # If replace_message_chunks fails for one message in the
        # batch, that message is marked failed but the others succeed.
        # Per-message transactions in Phase 2c provide the isolation.
        maildir, inbox, db, threader = self._setup(tmp_path)
        _write_eml(inbox / "ok1.eml", "ok1@example.com")
        _write_eml(inbox / "victim.eml", "victim@example.com")
        _write_eml(inbox / "ok2.eml", "ok2@example.com")

        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.3] * EMBEDDING_DIM
        queue = _make_queue(db)

        original = db.replace_message_chunks

        def selective_fail(*args, **kwargs):
            if kwargs.get("message_id") == "victim@example.com":
                raise RuntimeError("simulated db error for victim")
            return original(*args, **kwargs)

        monkeypatch.setattr(db, "replace_message_chunks", selective_fail)
        self._run(db, embedder, threader, queue, monkeypatch, maildir)

        # Survivors fully indexed
        assert db.is_indexed(str(inbox / "ok1.eml"))
        assert db.is_indexed(str(inbox / "ok2.eml"))
        assert db.get_chunk_ids_for_message("ok1@example.com")
        assert db.get_chunk_ids_for_message("ok2@example.com")
        # Victim never got chunks (Phase 2c rolled back its transaction)
        assert not db.get_chunk_ids_for_message("victim@example.com")

    def test_same_thread_phase2c_failure_does_not_leave_sibling_with_zero_vec(
        self, tmp_path, monkeypatch
    ):
        # Regression for the pending-vs-committed conflation: when
        # message A (with body chunks) and message B (blank reply, same
        # thread) arrive in the same batch and A's Phase 2c
        # ``replace_message_chunks`` fails, B used to skip its subject
        # fallback because Phase 2a saw "an earlier sibling already
        # queued chunks for this thread". With A rolled back, the
        # thread had NO committed chunks AND no fallback embed slot,
        # so B's Phase 2c left ``threads_vec`` at the Phase 1 zero
        # placeholder. B was marked succeeded.
        #
        # Fix: fallback gating is now on COMMITTED chunks only. B
        # reserves a fallback slot regardless of A's pending chunks.
        # Phase 2c's three-case priority chain still prefers
        # mean(committed chunks) when A succeeds, so the extra slot
        # is harmless on the happy path.
        import struct

        maildir, inbox, db, threader = self._setup(tmp_path)
        _write_eml(
            inbox / "a.eml",
            "a@example.com",
            subject="Project status",
            from_addr="alice@example.com",
            to_addr="bob@example.com",
        )
        # B is a blank-body reply on the same thread (chunkless).
        eml_b = inbox / "b.eml"
        eml_b.write_text(
            "From: bob@example.com\r\n"
            "To: alice@example.com\r\n"
            "Subject: Re: Project status\r\n"
            "Message-ID: <b@example.com>\r\n"
            "In-Reply-To: <a@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 13:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n",
            encoding="utf-8",
        )

        embedder = make_mock_embedder()
        # Use a sentinel value so we can verify it landed via the
        # subject-fallback path rather than a zero placeholder.
        sentinel = [0.7] * EMBEDDING_DIM
        embedder.embed.return_value = sentinel
        embedder.embed_batch.return_value = [sentinel]
        # Fall back to per-message embed call shape: when Phase 2b
        # asks for N texts, return N copies of the sentinel.
        embedder.embed_batch.side_effect = lambda texts: [list(sentinel) for _ in texts]
        queue = _make_queue(db)

        original = db.replace_message_chunks

        def fail_for_a(*args, **kwargs):
            if kwargs.get("message_id") == "a@example.com":
                raise RuntimeError("simulated Phase 2c failure for A")
            return original(*args, **kwargs)

        monkeypatch.setattr(db, "replace_message_chunks", fail_for_a)
        self._run(db, embedder, threader, queue, monkeypatch, maildir)

        # A failed Phase 2c → marked failed, queue retains a row.
        assert not db.get_chunk_ids_for_message("a@example.com"), (
            "A's chunk write rolled back via per-message transaction"
        )
        # B is chunkless by construction, but its thread vector must
        # NOT be the zero placeholder. The fix reserves a subject-
        # fallback slot for B independently of A's pending state.
        thread_id = db.find_thread_by_message_id("b@example.com")
        assert thread_id is not None
        row = db._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        vec = list(struct.unpack(f"<{len(row['embedding']) // 4}f", row["embedding"]))
        assert any(v != 0.0 for v in vec), (
            "thread vector must not be the Phase 1 zero placeholder; "
            "with A's chunks rolled back, B's subject fallback is the "
            "only path that lifts the thread out of zero. The earlier "
            "shape suppressed B's fallback because A had pending "
            "chunks, leaving the thread permanently degraded after "
            "A's Phase 2c rolled back."
        )


class TestSteadyStateBatchedDrain:
    """The production main loop calls
    ``_drain_queue_batched(..., max_passes=1, batch_size=STEADY_STATE_BATCH_SIZE)``.
    These tests exercise that exact shape so a regression in the
    steady-state path cannot pass while only the initial-scan path
    is covered.
    """

    def _setup(self, tmp_path, monkeypatch):
        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        inbox.mkdir(parents=True)
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        monkeypatch.setattr(main, "touch_health_file", lambda: None)
        return maildir, inbox, db, threader

    def test_max_passes_one_indexes_a_burst_in_a_single_call(self, tmp_path, monkeypatch):
        # Three Maildir files arrive together — the steady-state path
        # has to drain all three in one tick (batch_size>=3, max_passes=1).
        # Verifies the production wiring: queue rows removed, chunks
        # committed, indexed_files populated.
        from src.timings import TimingAggregator

        maildir, inbox, db, threader = self._setup(tmp_path, monkeypatch)
        _write_eml(inbox / "m1.eml", "m1@example.com")
        _write_eml(inbox / "m2.eml", "m2@example.com")
        _write_eml(inbox / "m3.eml", "m3@example.com")

        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.1] * EMBEDDING_DIM
        queue = _make_queue(db)
        for path in (inbox / "m1.eml", inbox / "m2.eml", inbox / "m3.eml"):
            queue.enqueue(str(path), REASON_INITIAL_SCAN)

        processed = main._drain_queue_batched(
            db,
            embedder,
            threader,
            queue,
            batch_size=8,
            timing_aggregator=TimingAggregator(window=10),
            max_passes=1,
        )
        assert processed == 3
        for mid, path in (
            ("m1@example.com", inbox / "m1.eml"),
            ("m2@example.com", inbox / "m2.eml"),
            ("m3@example.com", inbox / "m3.eml"),
        ):
            assert db.is_indexed(str(path))
            assert db.get_chunk_ids_for_message(mid)
        assert queue.stats() == {"queued": 0, "dead": 0}

    def test_max_passes_one_yields_after_one_batch(self, tmp_path, monkeypatch):
        # 5 files, batch_size=2, max_passes=1 → exactly 2 indexed,
        # 3 still queued for the next tick. Proves the steady-state
        # path interleaves with the reconciler / WAL / recovery sweeps
        # instead of draining greedily to empty (the initial-scan
        # behavior).
        from src.timings import TimingAggregator

        maildir, inbox, db, threader = self._setup(tmp_path, monkeypatch)
        for i in range(5):
            _write_eml(inbox / f"m{i}.eml", f"m{i}@example.com")
        queue = _make_queue(db)
        for i in range(5):
            queue.enqueue(str(inbox / f"m{i}.eml"), REASON_INITIAL_SCAN)

        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.2] * EMBEDDING_DIM

        processed = main._drain_queue_batched(
            db,
            embedder,
            threader,
            queue,
            batch_size=2,
            timing_aggregator=TimingAggregator(window=10),
            max_passes=1,
        )
        assert processed == 2
        assert queue.stats() == {"queued": 3, "dead": 0}


class TestPeriodicRecoverySkipsDeadLetter:
    """``_recover_zero_vector_threads(resurrect_dead=False)`` must
    preserve the durable queue's bounded-retry contract.

    Without the gate, a deterministic Phase 2 failure that has
    already exhausted ``max_attempts`` would be resurrected every
    sweep interval, burning embedder quota indefinitely.
    """

    def test_dead_letter_left_alone_when_resurrect_dead_false(self, tmp_path, monkeypatch):
        from src.parser import parse_email

        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        inbox.mkdir(parents=True)
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)

        # Set up a stuck zero-vector chunkless thread by routing through
        # the real ``upsert_thread`` with the placeholder zero seed.
        _write_eml(inbox / "stuck.eml", "stuck@example.com")
        msg = parse_email(inbox / "stuck.eml", maildir_root=maildir)
        assert msg is not None
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        thread = threader.assign_thread(msg)
        db.upsert_thread(thread, [0.0] * EMBEDDING_DIM)
        # Confirm the precondition: chunkless + zero-vec thread visible
        # to the recovery query.
        assert db.find_zero_vector_chunkless_thread_filepaths() == [str(inbox / "stuck.eml")]

        # Push the queue row to status='dead' (simulate exhausted retries).
        queue = _make_queue(db)
        queue.enqueue(str(inbox / "stuck.eml"), REASON_INITIAL_SCAN)
        for _ in range(queue.max_attempts):
            queue.mark_failed(
                str(inbox / "stuck.eml"),
                stage="embed",
                error="deterministic provider failure",
            )
        assert queue.is_dead(str(inbox / "stuck.eml"))

        # Default call (no kwarg): dead row must remain dead. The
        # default policy is uniform across startup and periodic —
        # dead = operator-visible terminal state.
        re_enqueued = main._recover_zero_vector_threads(db, queue)
        assert re_enqueued == 0
        assert queue.is_dead(str(inbox / "stuck.eml"))
        assert not queue.has_pending_row(str(inbox / "stuck.eml"))

        # Explicit ``resurrect_dead=False`` must do the same thing —
        # the parameter exists for the opt-in opposite (operator-
        # initiated rescue), and the default-False case should match
        # the explicit-False case exactly.
        re_enqueued = main._recover_zero_vector_threads(db, queue, resurrect_dead=False)
        assert re_enqueued == 0
        assert queue.is_dead(str(inbox / "stuck.eml"))

        # Explicit ``resurrect_dead=True`` IS the operator-rescue
        # opt-in path. When called deliberately, the dead row gets
        # cleared and re-enqueued with a fresh attempts budget. This
        # is the only way to auto-clear a dead row in the current
        # codebase; no production call site uses it.
        re_enqueued = main._recover_zero_vector_threads(db, queue, resurrect_dead=True)
        assert re_enqueued == 1
        assert queue.has_pending_row(str(inbox / "stuck.eml"))
