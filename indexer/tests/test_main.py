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

import json
from pathlib import Path

import pytest
from src import main
from src.database import EMBEDDING_DIM, Database
from src.queue import REASON_INITIAL_SCAN, IndexingQueue
from src.threader import Threader

from tests.conftest import make_mock_embedder

# Captured before any test monkeypatches the name, so the sorted
# wrapper installed by ``_run`` still walks the real Maildir.
_REAL_ITER_MAILDIR_MESSAGES = main._iter_maildir_messages

_UNIT_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIM - 1)


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

    def test_unreadable_secret_file_fails_closed(self, monkeypatch):
        # An unreadable mounted Docker secret is a deployment
        # misconfiguration, not a fall-through case: silently dropping
        # to the env fallback would either send an empty bearer token
        # to a remote embedder or use a stale env value the operator
        # thought the secret had superseded. The indexer must refuse
        # to start, surfacing the OSError so the operator fixes the
        # mount/perms before any embed call goes out.

        class _UnreadableSecretPath:
            def exists(self) -> bool:
                return True

            def read_text(self, **_kwargs) -> str:
                raise PermissionError("simulated perms regression")

        monkeypatch.setattr(main, "Path", lambda _p: _UnreadableSecretPath())
        monkeypatch.setenv("EMBED_API_KEY", "fallback-key")
        with pytest.raises(PermissionError, match="simulated perms regression"):
            main._read_embed_api_key()


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

    def test_one_off_batch_embed_failure_recovers_in_same_pass(self, tmp_path):
        """A batch embed that fails once while the embedder is healthy
        (the probe succeeds) is retried per message immediately — the
        message is indexed in the same pass and spends no attempt."""
        dest = tmp_path / "INBOX" / "new" / "msg.eml"
        _write_eml(dest, "retry@example.com")

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        queue = _make_queue(db)

        embedder = make_mock_embedder()
        embedder.embed.side_effect = [
            _status_error(503),  # batch embed
            _UNIT_VECTOR,  # health probe
            _UNIT_VECTOR,  # per-message re-embed
        ]
        queue.enqueue(str(dest), "test")

        main.drain_queue(queue, db, embedder, threader, max_batch=1)

        assert db.get_chunk_ids_for_message("retry@example.com")
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

    def test_persistent_embed_failure_is_deferred_never_dead(self, tmp_path):
        """An embedder that fails every call — including the health
        probe — is an infrastructure problem, not a property of the
        message. The row is deferred without spending attempts however
        many passes run, so no outage can dead-letter mail."""
        dest = tmp_path / "INBOX" / "new" / "msg.eml"
        _write_eml(dest, "giveup@example.com")

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        queue = _make_queue(db)  # max_attempts=3

        embedder = make_mock_embedder()
        embedder.embed.side_effect = _connection_error()
        queue.enqueue(str(dest), "test")

        for _ in range(5):
            main.drain_queue(queue, db, embedder, threader)
            _make_due(db)

        # Phase 1 commits thread membership + indexed_files eagerly, so
        # the file is keyword-searchable but chunkless until the
        # embedder returns.
        assert db.is_indexed(str(dest))
        assert not db.get_chunk_ids_for_message("giveup@example.com")
        assert queue.stats() == {"queued": 1, "dead": 0}
        row = db._conn.execute(
            "SELECT attempts, last_stage, last_error_class FROM indexing_jobs WHERE filepath = ?",
            (str(dest),),
        ).fetchone()
        assert row["attempts"] == 0
        assert row["last_stage"] == "embed"
        assert row["last_error_class"] == "retryable"

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

    def test_oversized_file_dead_letters_not_terminal_success(self, tmp_path, monkeypatch):
        # Models a >50 MB ``.eml``. Previous behavior routed oversized
        # through ``mark_skipped`` (delete the queue row); because the
        # file is still present on disk, ``initial_index`` re-enqueued
        # the same file on every container restart, and ``_index_one_file``
        # interpreted "row gone + file present" as success — the file
        # was never actually indexed. The fix routes oversized through
        # ``mark_dead_terminal``: row stays at status='dead', the
        # ``is_dead`` gate skips it on restart, and ``_index_one_file``
        # returns ``(False, "parse", ...)``.
        monkeypatch.setenv("INDEXER_PARSE_MAX_BYTES", "1024")

        dest = tmp_path / "INBOX" / "cur" / "huge.eml"
        dest.parent.mkdir(parents=True)
        # Write a file larger than the 1 KiB cap above.
        dest.write_bytes(b"X" * 2048)

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM

        ok, stage, err, _ = main._index_one_file(dest, db, embedder, threader)

        assert ok is False
        assert stage == "parse"
        assert err is not None
        assert "oversized" in err

        # Durable dead-letter row prevents re-enqueue on restart.
        row = db._conn.execute(
            "SELECT status, last_stage FROM indexing_jobs WHERE filepath = ?", (str(dest),)
        ).fetchone()
        assert row is not None
        assert row["status"] == "dead"
        assert row["last_stage"] == "parse"

        # ``is_dead`` (initial_index's gate) reports True so the next
        # container restart skips the file instead of re-enqueueing it.
        queue = IndexingQueue(db, max_attempts=1, base_backoff_seconds=0)
        assert queue.is_dead(str(dest)) is True

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


class TestValidateEmbedConfig:
    """``_validate_embed_config`` enforces the per-layer startup contract:
    every operator-supplied env var the embedder needs must be present
    and non-empty before the indexer constructs the embedder client.

    Pre-tightening, ``EMBED_API_KEY`` could be empty and the indexer
    would happily start, only failing at the first embed call against
    a remote provider with a 401. Now empty keys fail closed at startup
    so a missing secret surfaces in the same place as a missing
    ``EMBED_BASE_URL`` or ``EMBED_MODEL``.
    """

    def test_complete_config_passes_silently(self, monkeypatch):
        monkeypatch.setattr(main, "EMBED_BASE_URL", "http://x/v1")
        monkeypatch.setattr(main, "EMBED_MODEL", "qwen-embed")
        monkeypatch.setattr(main, "EMBED_API_KEY", "sk-real")  # pragma: allowlist secret
        # No raise == pass.
        main._validate_embed_config()

    def test_empty_base_url_passes(self, monkeypatch):
        # Empty ``EMBED_BASE_URL`` is intentional: it means "use the
        # SDK default" (OpenAI proper). The required non-empty
        # ``EMBED_API_KEY`` is the explicit-intent signal — an
        # operator with a real ``sk-...`` has unambiguously chosen
        # their provider, so we trust the SDK fallback. Symmetric with
        # ``INFERENCE_MODE=anthropic``'s empty-URL behavior.
        monkeypatch.setattr(main, "EMBED_BASE_URL", "")
        monkeypatch.setattr(main, "EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")
        monkeypatch.setattr(main, "EMBED_API_KEY", "sk-real")  # pragma: allowlist secret
        main._validate_embed_config()

    def test_missing_model_raises(self, monkeypatch):
        # ``EMBED_MODEL`` stays required: no SDK has a default model,
        # so an empty value always fails at request time. Catching it
        # at startup gives an actionable error.
        monkeypatch.setattr(main, "EMBED_BASE_URL", "http://x/v1")
        monkeypatch.setattr(main, "EMBED_MODEL", "")
        monkeypatch.setattr(main, "EMBED_API_KEY", "sk-real")  # pragma: allowlist secret
        with pytest.raises(ValueError, match="EMBED_MODEL"):
            main._validate_embed_config()

    def test_empty_api_key_raises(self, monkeypatch):
        # The startup contract: every enabled operator-supplied layer
        # needs a non-empty key. The key is the explicit-intent signal
        # that lets us trust an empty ``EMBED_BASE_URL`` as "use the
        # SDK default" rather than "I forgot to configure." Operators
        # pointing at an unauthenticated host-side server supply any
        # placeholder (``unauthenticated``) rather than leaving the
        # secret file empty.
        monkeypatch.setattr(main, "EMBED_BASE_URL", "http://x/v1")
        monkeypatch.setattr(main, "EMBED_MODEL", "qwen-embed")
        monkeypatch.setattr(main, "EMBED_API_KEY", "")
        with pytest.raises(ValueError, match="EMBED_API_KEY"):
            main._validate_embed_config()

    def test_placeholder_api_key_passes(self, monkeypatch):
        # The contract is "non-empty," not "well-formed." A placeholder
        # string for an unauthenticated host-side server passes startup
        # validation; the SDK sends it as a bearer token that compat
        # servers ignore.
        monkeypatch.setattr(main, "EMBED_BASE_URL", "http://x/v1")
        monkeypatch.setattr(main, "EMBED_MODEL", "qwen-embed")
        monkeypatch.setattr(main, "EMBED_API_KEY", "unauthenticated")
        main._validate_embed_config()

    def test_base_url_with_userinfo_raises(self, monkeypatch):
        # URLs that embed a ``user:pass@host`` userinfo authority flow
        # into the startup log naming the resolved wire endpoint,
        # leaking embedded credentials. Reject at startup; secrets
        # belong in ``.secrets/embed_api_key.txt``.
        monkeypatch.setattr(
            main,
            "EMBED_BASE_URL",
            "https://user:token@gateway.example/v1",  # pragma: allowlist secret
        )
        monkeypatch.setattr(main, "EMBED_MODEL", "qwen-embed")
        monkeypatch.setattr(main, "EMBED_API_KEY", "sk-real")  # pragma: allowlist secret
        with pytest.raises(ValueError, match="EMBED_BASE_URL.*credentials"):
            main._validate_embed_config()


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

        def embed(text):
            if "attachment text" in text:
                raise _status_error(500)
            return [0.1] * EMBEDDING_DIM

        embedder.embed.side_effect = embed

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
        assert "status=500" in err
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
        # Pin the initial-scan arrival order to filename order. Production
        # iterates in raw ``rglob`` order, which is filesystem-dependent
        # (creation order on APFS, hash order on ext4). Several tests in
        # this class assert in-batch threading behavior that only holds
        # when a root message is enqueued before its reply, so an
        # unsorted walk makes them pass locally and fail on CI. Sorting
        # here keeps those tests deterministic without pretending
        # production guarantees an order it does not.
        monkeypatch.setattr(
            main,
            "_iter_maildir_messages",
            lambda root: iter(sorted(_REAL_ITER_MAILDIR_MESSAGES(root))),
        )
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

    def test_subject_fallback_embeds_original_case_subject(self, tmp_path, monkeypatch):
        # The subject-fallback path is the ONLY vector a chunkless
        # thread ever gets, so it should use the message's
        # ORIGINAL-case subject (with ``Re:`` / ``Fwd:`` intact) — not
        # the threader's normalized grouping key, which has been
        # lowercased and had reply prefixes stripped. Stripping
        # semantic context from the embed input degrades retrieval for
        # blank-body messages whose subject is the only signal we have.
        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        inbox.mkdir(parents=True)

        # Blank body so chunk_message emits nothing and the fallback
        # path is the only embed contribution.
        original_subject = "Re: Quarterly Review (Q1 follow-up)"
        eml = inbox / "blank.eml"
        eml.write_text(
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            f"Subject: {original_subject}\r\n"
            "Message-ID: <case@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n",
            encoding="utf-8",
        )

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        embedder = make_mock_embedder(vector=[0.25] * EMBEDDING_DIM)
        queue = _make_queue(db)
        self._run(db, embedder, threader, queue, monkeypatch, maildir)

        embedded_texts = [call.args[0] for call in embedder.embed.call_args_list]
        assert original_subject in embedded_texts, (
            f"subject fallback must embed the original-case subject, "
            f"not the normalized grouping key; embedded: {embedded_texts!r}"
        )
        # The normalized grouping key would have ``re:`` stripped and
        # everything lowercased — that is exactly what we MUST NOT
        # have embedded.
        normalized = "quarterly review (q1 follow-up)"
        assert normalized not in embedded_texts, (
            "the fallback must not embed the normalized grouping key "
            "(lowercased + Re:-stripped) — that loses semantic context"
        )

    def test_subject_fallback_is_stable_across_chunkless_replies(self, tmp_path, monkeypatch):
        # Regression: every chunkless arrival on a still-chunkless
        # thread reserves a fallback slot AND Phase 2c unconditionally
        # overwrites the prior thread vector with the new message's
        # subject embedding. With ``state.msg.subject`` as the source,
        # successive replies (``Re: Quarterly Review``, ``Fwd: ...``)
        # produced ARRIVAL-ORDER-DEPENDENT thread vectors on the same
        # logical thread — a silent RAG-recall hazard. Sourcing from
        # ``display_subject`` (the oldest message's original-case
        # subject, maintained by ``upsert_thread``'s merge) makes the
        # fallback text stable across the lifetime of the thread.
        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        inbox.mkdir(parents=True)

        # Two blank-body messages in the same thread (reply via
        # In-Reply-To) with DIFFERENT subjects. Without the fix, msg2's
        # subject would overwrite msg1's in the thread vector slot.
        original_subject = "Quarterly Review"
        reply_subject = "Re: Quarterly Review (please review)"

        # Numeric prefixes put the root ahead of the reply under the
        # filename-sorted arrival order pinned by ``_run``.
        eml1 = inbox / "1-root.eml"
        eml1.write_text(
            "From: alice@example.com\r\n"
            "To: bob@example.com\r\n"
            f"Subject: {original_subject}\r\n"
            "Message-ID: <root@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n",
            encoding="utf-8",
        )

        eml2 = inbox / "2-reply.eml"
        eml2.write_text(
            "From: bob@example.com\r\n"
            "To: alice@example.com\r\n"
            f"Subject: {reply_subject}\r\n"
            "Message-ID: <reply@example.com>\r\n"
            "In-Reply-To: <root@example.com>\r\n"
            "Date: Tue, 02 Jan 2024 12:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n",
            encoding="utf-8",
        )

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        embedder = make_mock_embedder(vector=[0.5] * EMBEDDING_DIM)
        queue = _make_queue(db)
        self._run(db, embedder, threader, queue, monkeypatch, maildir)

        # Both messages went through the same initial-scan batch. The
        # final fallback embedding MUST have been the OLDEST message's
        # original-case subject — not the reply's "Re: ..." form.
        embedded_texts = [call.args[0] for call in embedder.embed.call_args_list]
        assert original_subject in embedded_texts, (
            f"fallback must embed the OLDEST message's subject across "
            f"chunkless replies (kept in display_subject); embedded: "
            f"{embedded_texts!r}"
        )
        # If the regression returned, ``reply_subject`` would also
        # appear in the embed inputs because Phase 2a would have
        # reserved a second fallback slot for it.
        assert reply_subject not in embedded_texts, (
            f"reply's subject must NOT be embedded as a separate "
            f"fallback — that's what made the thread vector order-"
            f"dependent; embedded: {embedded_texts!r}"
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
        embedder.embed_batch.side_effect = lambda texts, **_kw: [list(sentinel) for _ in texts]
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


class TestEnqueueUnindexedMessages:
    """The Maildir walk shared by the startup scan and the periodic
    rescan. The periodic rescan is the eventual-completeness backstop
    for files whose watchdog event was missed (restart, event
    coalescing, a delivery during a window the observer wasn't
    running), so it must find them — without resetting work the queue
    already owns."""

    def _setup(self, tmp_path, monkeypatch):
        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        inbox.mkdir(parents=True)
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        return maildir, inbox, db, _make_queue(db)

    def test_enqueues_file_that_arrived_without_an_event(self, tmp_path, monkeypatch):
        maildir, inbox, db, queue = self._setup(tmp_path, monkeypatch)
        missed = inbox / "missed.eml"
        _write_eml(missed, "missed@example.com")

        enqueued = main._enqueue_unindexed_messages(db, queue, maildir, main.REASON_RESCAN)

        assert enqueued == 1
        assert queue.has_pending_row(str(missed))

    def test_skips_already_indexed_files(self, tmp_path, monkeypatch):
        maildir, inbox, db, queue = self._setup(tmp_path, monkeypatch)
        _write_eml(inbox / "done.eml", "done@example.com")
        main.initial_index(db, make_mock_embedder(_UNIT_VECTOR), Threader(db), queue)
        assert db.is_indexed(str(inbox / "done.eml"))

        enqueued = main._enqueue_unindexed_messages(db, queue, maildir, main.REASON_RESCAN)

        assert enqueued == 0
        assert not queue.has_pending_row(str(inbox / "done.eml"))

    def test_leaves_dead_lettered_files_alone(self, tmp_path, monkeypatch):
        maildir, inbox, db, queue = self._setup(tmp_path, monkeypatch)
        dead = inbox / "dead.eml"
        _write_eml(dead, "dead@example.com")
        queue.enqueue(str(dead), REASON_INITIAL_SCAN)
        for _ in range(3):
            queue.mark_failed(str(dead), stage="parse", error="boom")
        assert queue.is_dead(str(dead))
        attempts_before = db.queue_get_attempts(str(dead))

        enqueued = main._enqueue_unindexed_messages(db, queue, maildir, main.REASON_RESCAN)

        assert enqueued == 0
        assert queue.is_dead(str(dead))
        assert db.queue_get_attempts(str(dead)) == attempts_before

    def test_does_not_reset_an_active_retry_cascade(self, tmp_path, monkeypatch):
        """``enqueue`` is INSERT OR REPLACE, so a walk that re-enqueued
        a queued-but-backing-off row would zero its attempts and let a
        restart loop (or every periodic rescan) retry it forever
        without ever reaching the dead-letter state."""
        maildir, inbox, db, queue = self._setup(tmp_path, monkeypatch)
        retrying = inbox / "retrying.eml"
        _write_eml(retrying, "retrying@example.com")
        queue.enqueue(str(retrying), REASON_INITIAL_SCAN)
        queue.mark_failed(str(retrying), stage="embed", error="timeout")
        assert queue.has_pending_row(str(retrying))
        assert db.queue_get_attempts(str(retrying)) == 1

        enqueued = main._enqueue_unindexed_messages(db, queue, maildir, main.REASON_RESCAN)

        assert enqueued == 0
        assert db.queue_get_attempts(str(retrying)) == 1

    def test_unindexable_message_reaches_terminal_state_not_rescan_loop(
        self, tmp_path, monkeypatch
    ):
        """A message the parser rejects outright (no Message-ID) can
        never be indexed. It must land in a visible terminal state that
        the walk skips — otherwise every periodic rescan re-enqueues and
        re-parses it forever."""
        maildir, inbox, db, queue = self._setup(tmp_path, monkeypatch)
        no_id = inbox / "no-id.eml"
        no_id.write_text("From: alice@example.com\r\nSubject: x\r\n\r\nbody\r\n", encoding="utf-8")
        main.initial_index(db, make_mock_embedder(), Threader(db), queue)

        enqueued = main._enqueue_unindexed_messages(db, queue, maildir, main.REASON_RESCAN)

        assert enqueued == 0
        assert queue.is_dead(str(no_id))


class _FakeObserver:
    def __init__(self, events: list[str]):
        self._events = events

    def schedule(self, *args, **kwargs):
        pass

    def start(self):
        self._events.append("observer_start")

    def stop(self):
        pass

    def join(self):
        pass


class TestMainStartupAndLoop:
    """``main()`` wiring: the observer must be live before the initial
    drain (otherwise mail delivered during a multi-hour initial index is
    never enqueued), and the main loop must periodically re-walk the
    Maildir so a missed event cannot cause a permanent omission."""

    def _run_main(self, tmp_path, monkeypatch, *, sweep_due: bool):
        events: list[str] = []
        db = Database(tmp_path / "mail.db")
        monkeypatch.setattr(main, "MAILDIR_PATH", tmp_path / "maildir")
        monkeypatch.setattr(main, "_validate_embed_config", lambda: None)
        monkeypatch.setattr(main, "_validate_embedding_dim", lambda e: None)
        monkeypatch.setattr(main, "Database", lambda path: db)
        monkeypatch.setattr(main, "OpenAIEmbedder", lambda **kw: make_mock_embedder())
        monkeypatch.setattr(main, "touch_health_file", lambda: None)
        monkeypatch.setattr(main, "sweep_paths", lambda db: None)
        monkeypatch.setattr(main, "Observer", lambda: _FakeObserver(events))
        monkeypatch.setattr(
            main,
            "initial_index",
            lambda *a, **kw: (
                events.append(f"initial_index:skip_trashed={kw.get('skip_trashed')}"),
                events.append(f"initial_breaker={id(kw.get('breaker'))}"),
            ),
        )
        monkeypatch.setattr(
            main,
            "_drain_queue_batched",
            lambda *a, **kw: events.append(f"drain:breaker={id(kw.get('breaker'))}") or 0,
        )
        monkeypatch.setattr(main, "_recover_zero_vector_threads", lambda *a, **kw: 0)
        monkeypatch.setattr(
            main,
            "_enqueue_unindexed_messages",
            lambda db, queue, root, reason, **kw: (
                events.append(f"walk:{reason}:skip_trashed={kw.get('skip_trashed')}") or 0
            ),
        )
        if sweep_due:
            monkeypatch.setattr(main, "RECOVERY_SWEEP_INTERVAL_SECS", 0)

        def _stop(_seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr(main.time, "sleep", _stop)
        main.main()
        return events

    def test_observer_starts_before_initial_drain(self, tmp_path, monkeypatch):
        events = self._run_main(tmp_path, monkeypatch, sweep_due=False)

        init = next(e for e in events if e.startswith("initial_index"))
        assert "observer_start" in events
        assert events.index("observer_start") < events.index(init)

    def test_initial_drain_and_main_loop_share_one_outage_breaker(self, tmp_path, monkeypatch):
        events = self._run_main(tmp_path, monkeypatch, sweep_due=False)

        initial = next(e for e in events if e.startswith("initial_breaker="))
        drain = next(e for e in events if e.startswith("drain:breaker="))
        assert initial.split("=")[1] == drain.split("=")[1]
        assert initial.split("=")[1] != str(id(None))

    def test_main_loop_periodically_rewalks_the_maildir(self, tmp_path, monkeypatch):
        events = self._run_main(tmp_path, monkeypatch, sweep_due=True)

        assert any(e.startswith(f"walk:{main.REASON_RESCAN}:") for e in events)

    def test_trashed_files_skipped_only_when_deletion_reconciliation_enabled(
        self, tmp_path, monkeypatch
    ):
        """With reconciliation on, a T-flagged file means "deleted
        upstream" — every Maildir walk must skip it or reaped messages
        come back. With it off (append-only default), behavior is
        unchanged."""
        monkeypatch.setenv("INDEXER_DELETION_ENABLED", "true")
        events = self._run_main(tmp_path, monkeypatch, sweep_due=True)
        assert "initial_index:skip_trashed=True" in events
        assert f"walk:{main.REASON_RESCAN}:skip_trashed=True" in events

        monkeypatch.setenv("INDEXER_DELETION_ENABLED", "false")
        events = self._run_main(tmp_path / "off", monkeypatch, sweep_due=True)
        assert "initial_index:skip_trashed=False" in events
        assert f"walk:{main.REASON_RESCAN}:skip_trashed=False" in events


class TestReapedMessagesStayDeleted:
    """Deletion reconciliation with the default ``unlink_on_reap=False``
    keeps a reaped message's T-flagged ``.eml`` on disk. That file is no
    longer indexed or queued, so any enqueue path that treats "on disk
    but not indexed" as undiscovered mail would resurrect it into
    search — and the next sweep would start a fresh grace window."""

    def _indexed_then_reaped(self, tmp_path, monkeypatch):
        from src.reconciler import Reconciler, ReconcilerConfig

        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        live = inbox / "1700000000.M1.host:2,S"
        _write_eml(live, "reaped@example.com")
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        embedder = make_mock_embedder(_UNIT_VECTOR)
        queue = _make_queue(db)
        main.initial_index(db, embedder, threader, queue)
        assert db.is_indexed(str(live))
        assert db.queue_get_status(str(live)) is None

        trashed = inbox / "1700000000.M1.host:2,ST"
        live.rename(trashed)
        reconciler = Reconciler(
            db,
            embedder,
            threader,
            ReconcilerConfig(
                enabled=True,
                grace_days=0,
                sweep_interval_secs=60,
                max_batch_pct=1.0,
                force=False,
                unlink_on_reap=False,
            ),
            maildir_root=maildir,
        )
        reconciler.sweep()
        reconciler.reap()
        assert trashed.exists()
        assert not db.is_indexed(str(trashed))
        return maildir, inbox, trashed, db, embedder, threader, queue, reconciler

    def test_reap_then_rescan_then_drain_does_not_resurrect(self, tmp_path, monkeypatch):
        maildir, _, trashed, db, embedder, threader, queue, _ = self._indexed_then_reaped(
            tmp_path, monkeypatch
        )

        enqueued = main._enqueue_unindexed_messages(
            db, queue, maildir, main.REASON_RESCAN, skip_trashed=True
        )
        main.drain_queue(queue, db, embedder, threader)

        assert enqueued == 0
        assert not queue.has_pending_row(str(trashed))
        assert not db.is_indexed(str(trashed))

    def test_restart_scan_does_not_resurrect(self, tmp_path, monkeypatch):
        maildir, _, trashed, db, embedder, threader, queue, _ = self._indexed_then_reaped(
            tmp_path, monkeypatch
        )

        main.initial_index(db, embedder, threader, queue, skip_trashed=True)

        assert not db.is_indexed(str(trashed))

    def test_flag_rename_of_reaped_file_does_not_resurrect(self, tmp_path, monkeypatch):
        """mbsync renames a reaped (still T-flagged) file for another
        flag change. The source is not indexed, so the watchdog's
        new-delivery branch must not mistake it for fresh mail."""
        _, inbox, trashed, db, _, _, queue, reconciler = self._indexed_then_reaped(
            tmp_path, monkeypatch
        )
        renamed = inbox / "1700000000.M1.host:2,RST"
        trashed.rename(renamed)
        handler = main.MaildirHandler(db, queue, reconciler=reconciler)

        handler.on_moved(_FakeEvent(str(trashed), str(renamed)))

        assert not queue.has_pending_row(str(renamed))

    def test_undeleted_upstream_is_reindexed(self, tmp_path, monkeypatch):
        """If mbsync clears the T flag after a reap (message restored
        upstream), the file is live mail again and must come back."""
        maildir, inbox, trashed, db, _, _, queue, _ = self._indexed_then_reaped(
            tmp_path, monkeypatch
        )
        restored = inbox / "1700000000.M1.host:2,S"
        trashed.rename(restored)

        enqueued = main._enqueue_unindexed_messages(
            db, queue, maildir, main.REASON_RESCAN, skip_trashed=True
        )

        assert enqueued == 1
        assert queue.has_pending_row(str(restored))

    def test_trashed_files_still_indexed_when_reconciliation_disabled(self, tmp_path, monkeypatch):
        maildir = tmp_path / "maildir"
        trashed = maildir / "INBOX" / "cur" / "1700000000.M1.host:2,ST"
        _write_eml(trashed, "kept@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)

        enqueued = main._enqueue_unindexed_messages(db, queue, maildir, main.REASON_RESCAN)

        assert enqueued == 1


def _status_error(status_code: int):
    import httpx
    from openai import APIStatusError

    return APIStatusError(
        message=f"{status_code} error",
        response=httpx.Response(status_code, request=httpx.Request("POST", "http://x")),
        body=None,
    )


def _connection_error():
    import httpx
    from openai import APIConnectionError

    return APIConnectionError(request=httpx.Request("POST", "http://x"))


def _make_due(db: Database) -> None:
    db._conn.execute("UPDATE indexing_jobs SET next_attempt_at = '2000-01-01T00:00:00+00:00'")
    db._conn.commit()


class TestEmbedFailureHandling:
    """A failed batch embed is either an outage (the embedder can't
    embed anything) or a bad input (the embedder is healthy but rejects
    one message). An outage must never spend attempt budgets — no
    outage may dead-letter mail — and a bad input must never take its
    batchmates down with it."""

    def _setup(self, tmp_path, monkeypatch, bodies: dict[str, str]):
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        paths = {}
        for name, body in bodies.items():
            path = maildir / "INBOX" / "cur" / f"{name}.eml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"From: a@example.com\r\nTo: b@example.com\r\nSubject: {name}\r\n"
                f"Message-ID: <{name}@example.com>\r\n"
                "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n\r\n"
                f"{body}\r\n",
                encoding="utf-8",
            )
            paths[name] = str(path)
        db = Database(tmp_path / "mail.db")
        queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)
        for p in paths.values():
            queue.enqueue(p, REASON_INITIAL_SCAN)
        return db, Threader(db), queue, paths

    def _row(self, db, path):
        return db._conn.execute(
            "SELECT status, attempts, last_stage, last_error_class FROM indexing_jobs "
            "WHERE filepath = ?",
            (path,),
        ).fetchone()

    def _drain(self, db, embedder, threader, queue, breaker=None):
        return main._drain_queue_batched(
            db,
            embedder,
            threader,
            queue,
            batch_size=10,
            timing_aggregator=main.TimingAggregator(window=4),
            max_passes=1,
            breaker=breaker,
        )

    def test_outage_defers_batch_without_spending_attempts(self, tmp_path, monkeypatch):
        db, threader, queue, paths = self._setup(
            tmp_path, monkeypatch, {"a": "alpha body", "b": "beta body"}
        )
        embedder = make_mock_embedder()
        embedder.embed.side_effect = _connection_error()

        for _ in range(10):
            self._drain(db, embedder, threader, queue)
            _make_due(db)

        for p in paths.values():
            row = self._row(db, p)
            assert row["status"] == "queued"
            assert row["attempts"] == 0
            assert row["last_stage"] == "embed"
            assert row["last_error_class"] == "retryable"
        assert queue.stats()["dead"] == 0

    def test_outage_recovers_once_embedder_returns(self, tmp_path, monkeypatch):
        db, threader, queue, paths = self._setup(tmp_path, monkeypatch, {"a": "alpha body"})
        embedder = make_mock_embedder()
        embedder.embed.side_effect = _connection_error()
        self._drain(db, embedder, threader, queue)

        embedder.embed.side_effect = None
        embedder.embed.return_value = _UNIT_VECTOR
        _make_due(db)
        self._drain(db, embedder, threader, queue)

        assert queue.stats() == {"queued": 0, "dead": 0}
        assert db.get_chunk_ids_for_message("a@example.com")

    def test_config_error_defers_as_operator_action(self, tmp_path, monkeypatch):
        db, threader, queue, paths = self._setup(tmp_path, monkeypatch, {"a": "alpha body"})
        embedder = make_mock_embedder()
        embedder.embed.side_effect = _status_error(401)

        for _ in range(5):
            self._drain(db, embedder, threader, queue)
            _make_due(db)

        row = self._row(db, paths["a"])
        assert row["status"] == "queued"
        assert row["attempts"] == 0
        assert row["last_error_class"] == "operator_action_required"

    def test_rejected_input_is_isolated_and_batchmates_indexed(self, tmp_path, monkeypatch):
        db, threader, queue, paths = self._setup(
            tmp_path,
            monkeypatch,
            {"good1": "fine text one", "bad": "POISON input", "good2": "fine text two"},
        )
        embedder = make_mock_embedder()

        def embed(text):
            if "POISON" in text:
                raise _status_error(400)
            return _UNIT_VECTOR

        embedder.embed.side_effect = embed

        self._drain(db, embedder, threader, queue)

        assert db.get_chunk_ids_for_message("good1@example.com")
        assert db.get_chunk_ids_for_message("good2@example.com")
        row = self._row(db, paths["bad"])
        assert row["status"] == "dead"
        assert row["last_stage"] == "embed"
        assert row["last_error_class"] == "permanent_source_failure"
        assert queue.stats() == {"queued": 0, "dead": 1}

    def test_input_that_crashes_provider_spends_attempts_not_the_queue(self, tmp_path, monkeypatch):
        """A provider that 500s on one specific input looks like an
        outage from the batch alone. The probe proves the embedder is
        healthy, so only that message retries (and eventually dies) —
        it cannot stall everything else behind the circuit breaker."""
        db, threader, queue, paths = self._setup(
            tmp_path, monkeypatch, {"good": "fine text", "bad": "POISON input"}
        )
        embedder = make_mock_embedder()

        def embed(text):
            if "POISON" in text:
                raise _status_error(500)
            return _UNIT_VECTOR

        embedder.embed.side_effect = embed
        breaker = main._EmbedOutageBreaker()

        self._drain(db, embedder, threader, queue, breaker=breaker)

        assert db.get_chunk_ids_for_message("good@example.com")
        row = self._row(db, paths["bad"])
        assert row["status"] == "queued"
        assert row["attempts"] == 1
        assert row["last_error_class"] == "retryable"
        assert breaker.allow(main.time.monotonic())

    def test_outage_trips_breaker_and_open_breaker_pauses_draining(self, tmp_path, monkeypatch):
        db, threader, queue, paths = self._setup(tmp_path, monkeypatch, {"a": "alpha body"})
        embedder = make_mock_embedder()
        embedder.embed.side_effect = _connection_error()
        breaker = main._EmbedOutageBreaker()

        self._drain(db, embedder, threader, queue, breaker=breaker)
        assert not breaker.allow(main.time.monotonic())

        _make_due(db)
        calls_before = embedder.embed.call_count
        processed = self._drain(db, embedder, threader, queue, breaker=breaker)

        assert processed == 0
        assert embedder.embed.call_count == calls_before

    def _recover_and_drain(self, db, embedder, threader, queue):
        embedder.embed.side_effect = None
        embedder.embed.return_value = _UNIT_VECTOR
        _make_due(db)
        self._drain(db, embedder, threader, queue)

    def test_auth_failure_during_isolation_defers_instead_of_dead_lettering(
        self, tmp_path, monkeypatch
    ):
        """batch -> 503, probe -> ok, individual retry -> 401. A probe
        that passed moments earlier does not make a 401 the message's
        fault: the key was revoked or a gateway rejected auth. Every
        remaining message is deferred as operator action and the
        breaker pauses — nothing is dead-lettered."""
        db, threader, queue, paths = self._setup(
            tmp_path, monkeypatch, {"a": "alpha body", "b": "beta body"}
        )
        embedder = make_mock_embedder()
        state = {"batch_done": False}

        def embed(text):
            if text == main._EMBED_PROBE_TEXT:
                return _UNIT_VECTOR
            if not state["batch_done"]:
                state["batch_done"] = True
                raise _status_error(503)
            raise _status_error(401)

        embedder.embed.side_effect = embed
        breaker = main._EmbedOutageBreaker()

        self._drain(db, embedder, threader, queue, breaker=breaker)

        for p in paths.values():
            row = self._row(db, p)
            assert row["status"] == "queued"
            assert row["attempts"] == 0
            assert row["last_error_class"] == "operator_action_required"
        assert not breaker.allow(main.time.monotonic())

        self._recover_and_drain(db, embedder, threader, queue)
        assert queue.stats() == {"queued": 0, "dead": 0}
        assert db.get_chunk_ids_for_message("a@example.com")
        assert db.get_chunk_ids_for_message("b@example.com")

    def test_rate_limit_on_real_requests_never_dead_letters(self, tmp_path, monkeypatch):
        """The tiny probe fits the provider's remaining capacity but the
        real request is rate-limited (429), pass after pass. That is
        infrastructure, not the message: attempts stay untouched."""
        db, threader, queue, paths = self._setup(tmp_path, monkeypatch, {"a": "alpha body"})
        embedder = make_mock_embedder()

        def embed(text):
            if text == main._EMBED_PROBE_TEXT:
                return _UNIT_VECTOR
            raise _status_error(429)

        embedder.embed.side_effect = embed

        for _ in range(8):
            self._drain(db, embedder, threader, queue)
            _make_due(db)

        row = self._row(db, paths["a"])
        assert row["status"] == "queued"
        assert row["attempts"] == 0
        assert row["last_error_class"] == "retryable"

        self._recover_and_drain(db, embedder, threader, queue)
        assert queue.stats() == {"queued": 0, "dead": 0}
        assert db.get_chunk_ids_for_message("a@example.com")

    def test_pause_mid_isolation_commits_messages_already_embedded(self, tmp_path, monkeypatch):
        """Isolation embeds messages in batch order. If the provider
        starts rate-limiting partway through, messages that already
        embedded are indexed; only the rest are deferred."""
        db, threader, queue, paths = self._setup(
            tmp_path,
            monkeypatch,
            {"a1": "first message", "b2": "THROTTLED second", "c3": "third message"},
        )
        embedder = make_mock_embedder()
        state = {"batch_done": False}

        def embed(text):
            if text == main._EMBED_PROBE_TEXT:
                return _UNIT_VECTOR
            if not state["batch_done"]:
                state["batch_done"] = True
                raise _status_error(503)
            if "THROTTLED" in text:
                state["throttled"] = True
            if state.get("throttled"):
                raise _status_error(429)
            return _UNIT_VECTOR

        embedder.embed.side_effect = embed

        # Rows are claimed in enqueue order: a1, b2, c3.
        self._drain(db, embedder, threader, queue)

        embedded = [n for n in paths if db.get_chunk_ids_for_message(f"{n}@example.com")]
        deferred = [n for n in paths if queue.has_pending_row(paths[n])]
        assert embedded == ["a1"]
        assert deferred == ["b2", "c3"]
        for n in deferred:
            assert self._row(db, paths[n])["attempts"] == 0
        assert queue.stats()["dead"] == 0

    def test_provider_error_charges_message_only_when_reprobe_passes(self, tmp_path, monkeypatch):
        """A 5xx during isolation is ambiguous. If a fresh probe fails,
        the provider went down: defer, no attempt spent."""
        db, threader, queue, paths = self._setup(tmp_path, monkeypatch, {"a": "alpha body"})
        embedder = make_mock_embedder()
        probes = {"n": 0}

        def embed(text):
            if text == main._EMBED_PROBE_TEXT:
                probes["n"] += 1
                if probes["n"] == 1:
                    return _UNIT_VECTOR
                raise _connection_error()
            raise _status_error(500)

        embedder.embed.side_effect = embed

        self._drain(db, embedder, threader, queue)

        row = self._row(db, paths["a"])
        assert row["attempts"] == 0
        assert row["status"] == "queued"
        assert row["last_error_class"] == "retryable"


class TestEmbedOutageBreaker:
    def test_backoff_doubles_to_cap_and_resets_on_success(self):
        b = main._EmbedOutageBreaker(base_seconds=30, cap_seconds=100)
        assert b.allow(0.0)

        assert b.record_failure(0.0) == 30
        assert not b.allow(29.0)
        assert b.allow(30.0)
        assert b.record_failure(30.0) == 60
        assert b.record_failure(90.0) == 100
        assert b.record_failure(190.0) == 100

        b.record_success()
        assert b.allow(190.0)
        assert b.record_failure(190.0) == 30


def _mock_transport_embedder(handler):
    """A real ``OpenAIEmbedder`` whose SDK client talks to an in-process
    ``httpx.MockTransport``, so tests exercise actual HTTP request
    boundaries (how many inputs share one request) rather than a
    per-text mock."""
    import httpx
    from openai import OpenAI
    from src.embedder import OpenAIEmbedder

    embedder = OpenAIEmbedder(base_url="http://embed.test/v1", model="m", api_key="k")
    embedder.client = OpenAI(
        base_url="http://embed.test/v1",
        api_key="k",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return embedder


def _embeddings_response(inputs: list[str]):
    import httpx

    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": "m",
            "data": [
                {"object": "embedding", "index": i, "embedding": _UNIT_VECTOR}
                for i in range(len(inputs))
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )


class TestRequestLimitIsNotSourceFailure:
    """A provider rejecting a *request* (413 / 400 / 422) has not shown
    that any of the message's content is bad when the request carried
    several inputs — the limit may be the batch, not the source. Only a
    rejection of a single input is evidence against the message."""

    def _drain_one(self, tmp_path, monkeypatch, handler):
        import json

        monkeypatch.setattr(main, "MAILDIR_PATH", tmp_path)
        dest = tmp_path / "INBOX" / "cur" / "msg.eml"
        _write_eml_with_text_attachment(dest, "limits@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(dest), REASON_INITIAL_SCAN)
        sizes: list[int] = []

        def recording(request):
            inputs = json.loads(request.content)["input"]
            sizes.append(len(inputs))
            return handler(inputs)

        main.drain_queue(queue, db, _mock_transport_embedder(recording), Threader(db))
        return db, queue, str(dest), sizes

    def test_multi_input_request_rejection_retries_one_input_per_request(
        self, tmp_path, monkeypatch
    ):
        import httpx

        def handler(inputs):
            if len(inputs) > 1:
                return httpx.Response(413, json={"error": {"message": "payload too large"}})
            return _embeddings_response(inputs)

        db, queue, path, sizes = self._drain_one(tmp_path, monkeypatch, handler)

        assert sizes[:3] == [2, 1, 2], "batch, probe, then the message's own combined request"
        assert all(n == 1 for n in sizes[3:])
        assert db.get_chunk_ids_for_message("limits@example.com")
        assert queue.stats() == {"queued": 0, "dead": 0}

    def test_input_rejected_on_its_own_is_a_permanent_source_failure(self, tmp_path, monkeypatch):
        import httpx

        def handler(inputs):
            if len(inputs) > 1 or any("attachment text" in t for t in inputs):
                return httpx.Response(400, json={"error": {"message": "bad input"}})
            return _embeddings_response(inputs)

        db, queue, path, sizes = self._drain_one(tmp_path, monkeypatch, handler)

        row = db._conn.execute(
            "SELECT status, last_error_class FROM indexing_jobs WHERE filepath = ?", (path,)
        ).fetchone()
        assert row["status"] == "dead"
        assert row["last_error_class"] == "permanent_source_failure"
        assert not db.get_chunk_ids_for_message("limits@example.com")


def test_probe_refreshes_heartbeat_before_its_own_retry_cycle(monkeypatch):
    """The failure that triggers a probe may have spent a full retry
    cycle without a heartbeat; the probe must not stack a second silent
    cycle on top, or a hung embedder trips the container healthcheck."""
    touches: list[bool] = []
    embedder = make_mock_embedder(_UNIT_VECTOR)

    def touch():
        touches.append(embedder.embed.called)

    monkeypatch.setattr(main, "touch_health_file", touch)

    assert main._probe_embedder(embedder) is None
    assert touches == [False], "heartbeat must be refreshed before the probe request"


def test_stage_errors_never_persist_the_decoded_payload(tmp_path, monkeypatch):
    """``repr()`` of a UnicodeDecodeError embeds the entire byte buffer
    it was decoding — email content — which would land in
    ``indexing_jobs.last_error`` and operator logs. The persisted error
    keeps the type and reason, not the payload."""
    dest = tmp_path / "INBOX" / "cur" / "msg.eml"
    _write_eml(dest, "decode@example.com")
    db = Database(tmp_path / "mail.db")
    queue = _make_queue(db)
    queue.enqueue(str(dest), REASON_INITIAL_SCAN)

    def boom(*_a, **_kw):
        raise UnicodeDecodeError("utf-8", b"PRIVATE-BODY-TEXT\xff", 17, 18, "invalid start byte")

    monkeypatch.setattr(main, "parse_email", boom)
    main.drain_queue(queue, db, make_mock_embedder(_UNIT_VECTOR), Threader(db), max_batch=1)

    row = db._conn.execute(
        "SELECT last_stage, last_error FROM indexing_jobs WHERE filepath = ?", (str(dest),)
    ).fetchone()
    assert row["last_stage"] == "parse"
    assert "UnicodeDecodeError" in row["last_error"]
    assert "invalid start byte" in row["last_error"]
    assert "PRIVATE-BODY-TEXT" not in row["last_error"]


class TestMessageRecordsEndToEnd:
    """Per-message records through the real parser, watcher, and reconciler."""

    def test_quoted_display_names_with_commas_survive_parsing(self, tmp_path, monkeypatch):
        """``"Last, First" <addr>`` is a common display-name format. The
        parser must hand the writer a string that still parses as one
        address, or the recipient silently disappears from
        ``message_participants`` (and from thread participant lists)."""
        maildir = tmp_path / "maildir"
        path = maildir / "INBOX" / "cur" / "quoted.eml"
        path.parent.mkdir(parents=True)
        path.write_text(
            'From: "Roe, Alex" <alex@example.com>\r\n'
            'To: "Doe, Jane" <jane@example.com>, Plain Name <plain@example.com>\r\n'
            'Cc: "Smith, Bob" <bob@example.com>\r\n'
            "Subject: Quoted names\r\n"
            "Message-ID: <quoted@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\nBody.\r\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        main.initial_index(db, make_mock_embedder(_UNIT_VECTOR), Threader(db), _make_queue(db))

        participants = {
            (r["role"], r["address"], r["name"])
            for r in db._conn.execute("SELECT role, address, name FROM message_participants")
        }
        assert participants == {
            ("from", "alex@example.com", "Roe, Alex"),
            ("to", "jane@example.com", "Doe, Jane"),
            ("to", "plain@example.com", "Plain Name"),
            ("cc", "bob@example.com", "Smith, Bob"),
        }

    def test_non_ascii_addresses_and_names_ingest(self, tmp_path, monkeypatch):
        """One non-ASCII recipient (local part or domain) must not make
        the whole message unindexable, and non-ASCII display names are
        stored readable — not as RFC 2047 encoded-words."""
        maildir = tmp_path / "maildir"
        path = maildir / "INBOX" / "cur" / "utf8.eml"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            (
                "From: José Álvarez <jose@example.com>\r\n"
                'To: josé@example.com, "Doe, Jane" <jane@example.com>,'
                " =?utf-8?q?Zo=C3=AB_Ng?= <zoe@example.com>\r\n"
                "Cc: <user@exämple.com>\r\n"
                "Subject: Unicode recipients\r\n"
                "Message-ID: <utf8@example.com>\r\n"
                "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n\r\nBody text.\r\n"
            ).encode()
        )
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        main.initial_index(db, make_mock_embedder(_UNIT_VECTOR), Threader(db), queue)

        assert queue.stats() == {"queued": 0, "dead": 0}
        assert db.get_chunk_ids_for_message("utf8@example.com")
        participants = {
            (r["role"], r["address"], r["name"])
            for r in db._conn.execute("SELECT role, address, name FROM message_participants")
        }
        assert participants == {
            ("from", "jose@example.com", "José Álvarez"),
            ("to", "josé@example.com", None),
            ("to", "jane@example.com", "Doe, Jane"),
            ("to", "zoe@example.com", "Zoë Ng"),
            ("cc", "user@exämple.com", None),
        }

    def test_failed_cross_folder_move_stays_recoverable(self, tmp_path, monkeypatch):
        """If recording a cross-folder move fails, nothing is half-written:
        the locator, file identity, and folder all roll back together, the
        destination stays unindexed, and the next Maildir walk re-indexes
        it with the right folder."""
        maildir = tmp_path / "maildir"
        src = maildir / "INBOX" / "cur" / "1700000000.M1.host:2,S"
        _write_eml(src, "atomic@example.com")
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        embedder = make_mock_embedder(_UNIT_VECTOR)
        main.initial_index(db, embedder, Threader(db), queue)

        dest = maildir / "Archive" / "cur" / "1700000000.M1.host:2,S"
        dest.parent.mkdir(parents=True)
        src.rename(dest)
        db._conn.execute(
            "CREATE TRIGGER fail_folder BEFORE UPDATE OF folder ON messages "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        main.MaildirHandler(db, queue).on_moved(_FakeEvent(str(src), str(dest)))

        row = db._conn.execute("SELECT folder, filepath FROM messages").fetchone()
        assert (row["folder"], row["filepath"]) == ("INBOX", str(src))
        assert not db.is_indexed(str(dest))

        db._conn.execute("DROP TRIGGER fail_folder")
        assert main._enqueue_unindexed_messages(db, queue, maildir, main.REASON_RESCAN) == 1
        main.drain_queue(queue, db, embedder, Threader(db))
        row = db._conn.execute("SELECT folder, filepath FROM messages").fetchone()
        assert (row["folder"], row["filepath"]) == ("Archive", str(dest))

    def _ingest_headers(self, tmp_path, monkeypatch, headers: str, message_id: str):
        maildir = tmp_path / "maildir"
        path = maildir / "INBOX" / "cur" / f"{message_id}.eml"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            (
                headers + f"Subject: Header edge case\r\nMessage-ID: <{message_id}>\r\n"
                "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n\r\nReadable body.\r\n"
            ).encode()
        )
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        main.initial_index(db, make_mock_embedder(_UNIT_VECTOR), Threader(db), queue)
        participants = {
            (r["role"], r["address"], r["name"])
            for r in db._conn.execute("SELECT role, address, name FROM message_participants")
        }
        return db, queue, participants

    def test_malformed_encoded_names_do_not_abort_ingestion(self, tmp_path, monkeypatch):
        """A display name with a broken encoded-word (bad base64) must not
        dead-letter the message: the address is kept and the raw name
        text stands in for the undecodable name."""
        db, queue, participants = self._ingest_headers(
            tmp_path,
            monkeypatch,
            "From: alice@example.com\r\n"
            "To: =?utf-8?b?x?= <bob@example.com>\r\n"
            "Cc: =?utf-8?b?y?= <carol@example.com>\r\n",
            "malformed@example.com",
        )
        assert queue.stats() == {"queued": 0, "dead": 0}
        assert db.get_chunk_ids_for_message("malformed@example.com")
        assert participants == {
            ("from", "alice@example.com", None),
            ("to", "bob@example.com", "=?utf-8?b?x?="),
            ("cc", "carol@example.com", "=?utf-8?b?y?="),
        }

    def test_encoded_sender_name_with_comma_keeps_the_sender(self, tmp_path, monkeypatch):
        _db, _queue, participants = self._ingest_headers(
            tmp_path,
            monkeypatch,
            "From: =?utf-8?q?Doe=2C_Jane?= <jane@example.com>\r\nTo: bob@example.com\r\n",
            "encoded-from@example.com",
        )
        assert ("from", "jane@example.com", "Doe, Jane") in participants

    def test_every_author_of_a_multi_author_from_is_recorded(self, tmp_path, monkeypatch):
        _db, _queue, participants = self._ingest_headers(
            tmp_path,
            monkeypatch,
            "From: Alice <alice@example.com>, Carol <carol@example.com>\r\n"
            "Sender: alice@example.com\r\nTo: bob@example.com\r\n",
            "multi-author@example.com",
        )
        assert {p for p in participants if p[0] == "from"} == {
            ("from", "alice@example.com", "Alice"),
            ("from", "carol@example.com", "Carol"),
        }

    # Real-world address-header shapes. Each case is ingested end to end
    # (parse -> index) and must (a) never cost the message its place in
    # the index and (b) record exactly these participants. The From line
    # is the same in every case unless the case overrides it.
    _ADDRESS_CASES = [
        ("bare", "To: bob@example.com", {("to", "bob@example.com", None)}),
        ("angle_only", "To: <bob@example.com>", {("to", "bob@example.com", None)}),
        ("named", "To: Bob Smith <bob@example.com>", {("to", "bob@example.com", "Bob Smith")}),
        (
            "quoted_comma",
            'To: "Doe, Jane" <jane@example.com>',
            {("to", "jane@example.com", "Doe, Jane")},
        ),
        (
            "escaped_quotes",
            'To: "Jane \\"JJ\\" Doe" <jj@example.com>',
            {("to", "jj@example.com", 'Jane "JJ" Doe')},
        ),
        (
            "unquoted_period",
            "To: Dr. Who <who@example.com>",
            {("to", "who@example.com", "Dr. Who")},
        ),
        (
            "comment_name",
            "To: bob@example.com (Bob Smith)",
            {("to", "bob@example.com", "Bob Smith")},
        ),
        ("empty_quoted_name", 'To: "" <bob@example.com>', {("to", "bob@example.com", None)}),
        (
            "case_and_plus_tag",
            "To: Bob+Tag@Mail.Example.COM",
            {("to", "bob+tag@mail.example.com", None)},
        ),
        (
            "folded_multi",
            "To: Bob <bob@example.com>,\r\n Carol <carol@example.com>",
            {("to", "bob@example.com", "Bob"), ("to", "carol@example.com", "Carol")},
        ),
        (
            "blank_elements",
            "To: bob@example.com, , carol@example.com,",
            {("to", "bob@example.com", None), ("to", "carol@example.com", None)},
        ),
        ("empty_group", "To: undisclosed-recipients:;", set()),
        (
            "group_with_members",
            "To: Team: ann@example.com, Ben <ben@example.com>;",
            {("to", "ann@example.com", None), ("to", "ben@example.com", "Ben")},
        ),
        ("garbage_entry", "To: not an address", set()),
        (
            "same_person_to_and_cc",
            "To: bob@example.com\r\nCc: Bob <BOB@example.com>",
            {("to", "bob@example.com", None), ("cc", "bob@example.com", "Bob")},
        ),
        (
            "encoded_q",
            "To: =?utf-8?q?Zo=C3=AB_Ng?= <zoe@example.com>",
            {("to", "zoe@example.com", "Zoë Ng")},
        ),
        (
            "encoded_b",
            "To: =?utf-8?b?Sm9zw6kgw4FsdmFyZXo=?= <jose@example.com>",
            {("to", "jose@example.com", "José Álvarez")},
        ),
        (
            "encoded_comma",
            "To: =?utf-8?q?Doe=2C_Jane?= <jane@example.com>",
            {("to", "jane@example.com", "Doe, Jane")},
        ),
        (
            "encoded_malformed",
            "To: =?utf-8?b?x?= <bob@example.com>",
            {("to", "bob@example.com", "=?utf-8?b?x?=")},
        ),
        (
            "encoded_unknown_charset",
            "To: =?x-bogus?q?Bob?= <bob@example.com>",
            {("to", "bob@example.com", "Bob")},
        ),
        (
            "raw_utf8_name",
            "To: José Álvarez <jose@example.com>",
            {("to", "jose@example.com", "José Álvarez")},
        ),
        (
            "raw_utf8_quoted_comma",
            'To: "Álvarez, José" <jose@example.com>',
            {("to", "jose@example.com", "Álvarez, José")},
        ),
        ("raw_utf8_local_part", "To: josé@example.com", {("to", "josé@example.com", None)}),
        ("raw_utf8_domain", "To: <user@exämple.com>", {("to", "user@exämple.com", None)}),
        ("no_recipients", "", set()),
        # Strict parsing rejects this CVE-2023-27043-style ambiguous input,
        # and no lenient parser second-guesses it: nothing is recorded, as
        # on base. The message itself still indexes.
        ("crafted_ambiguous", "To: alice@example.org)<bob@example.org>", set()),
        (
            "blank_elements_in_group",
            "To: Team: ann@example.com, , ben@example.com,;",
            {("to", "ann@example.com", None), ("to", "ben@example.com", None)},
        ),
        (
            "leading_comma",
            "To: , bob@example.com",
            {("to", "bob@example.com", None)},
        ),
        # Name decoding must never cost the message or corrupt text:
        # a UTF-7 word that decodes to a lone surrogate, a charset label
        # the codec lookup rejects, and plain Unicode next to an
        # encoded-word all keep a usable name.
        (
            "encoded_to_lone_surrogate",
            "To: =?utf-7?q?+2AA-?= <bob@example.com>",
            {("to", "bob@example.com", "=?utf-7?q?+2AA-?=")},
        ),
        (
            "nul_in_charset_label",
            "To: =?utf-8\x00?q?Bob?= <bob@example.com>",
            {("to", "bob@example.com", "=?utf-8\x00?q?Bob?=")},
        ),
        (
            "unicode_then_encoded_word",
            "To: José =?utf-8?q?Garc=C3=ADa?= <jose@example.com>",
            {("to", "jose@example.com", "José García")},
        ),
        (
            "adjacent_encoded_words",
            "To: =?utf-8?q?Zo=C3=AB?= =?utf-8?q?_Ng?= <zoe@example.com>",
            {("to", "zoe@example.com", "Zoë Ng")},
        ),
        # Strict parsing rejects deeply nested unmatched comments; the
        # lenient fallback recurses past Python's limit on them. That must
        # cost only the (unparseable) recipients, never the message.
        (
            "unmatched_parens_to",
            "To: bob@example.com " + "\r\n ".join(["(" * 60] * 20),
            set(),
        ),
        (
            "unmatched_parens_cc",
            "To: carol@example.com\r\nCc: bob@example.com " + "\r\n ".join(["(" * 60] * 20),
            {("to", "carol@example.com", None)},
        ),
        # A decoded name must never change WHO the recipient is. A CR/LF
        # decoded from an encoded-word broke re-parsing: "Mallory@...\r"
        # was recorded as the address instead of bob, and a comma variant
        # lost the recipient entirely. Such tokens keep their raw text.
        (
            "decoded_cr_and_at_in_name",
            "To: =?utf-8?q?Mallory=40example.com=0D?= <bob@example.com>",
            {("to", "bob@example.com", "=?utf-8?q?Mallory=40example.com=0D?=")},
        ),
        (
            "decoded_cr_and_comma_in_name",
            "To: =?utf-8?q?Doe=2C=0D_Jane?= <jane@example.com>",
            {("to", "jane@example.com", "=?utf-8?q?Doe=2C=0D_Jane?=")},
        ),
        (
            "decoded_lf_in_name",
            "To: =?utf-8?q?Bob=0AEvil?= <bob@example.com>",
            {("to", "bob@example.com", "=?utf-8?q?Bob=0AEvil?=")},
        ),
        # Blank-element cleanup must not touch quoted strings or comments:
        # rewriting "a, ,b"@example.com to "a,b"@... invents a mailbox.
        (
            "blank_cleanup_spares_quoted_local_part",
            'To: "a, ,b"@example.com, carol@example.com,',
            {("to", '"a, ,b"@example.com', None), ("to", "carol@example.com", None)},
        ),
        (
            "blank_cleanup_with_comment",
            "To: bob@example.com (x y), , carol@example.com",
            {("to", "bob@example.com", "x y"), ("to", "carol@example.com", None)},
        ),
        # A comment containing commas is valid RFC 5322. The list is split
        # at top-level commas only, so the comment stays part of its element.
        (
            "comment_containing_commas",
            "To: bob@example.com (x, y), carol@example.com",
            {("to", "bob@example.com", "x, y"), ("to", "carol@example.com", None)},
        ),
        # An address-shaped display name stays a name; the real address is
        # the angle-bracket one.
        (
            "address_shaped_name",
            'To: "alice@example.org" <bob@example.org>',
            {("to", "bob@example.org", "alice@example.org")},
        ),
    ]

    def test_mixed_encoded_and_unicode_sender_stays_findable(self, tmp_path, monkeypatch):
        """An encoded-word next to already-Unicode text must decode only
        the encoded part; re-decoding the Unicode turned 李雷 into
        backslash-u escapes in both the participant row and the thread
        sender list that ``find_contact`` reads."""
        db, _queue, participants = self._ingest_headers(
            tmp_path,
            monkeypatch,
            "From: =?utf-8?q?Dr.?= 李雷 <li@example.com>\r\nTo: bob@example.com\r\n",
            "mixed-from@example.com",
        )
        assert ("from", "li@example.com", "Dr. 李雷") in participants
        row = db._conn.execute("SELECT senders, participants FROM threads").fetchone()
        names = json.loads(row["senders"]) + json.loads(row["participants"])
        assert any("李雷" in n for n in names)
        assert not any("\\u" in n for n in names)

    def test_parentheses_in_encoded_word_labels_do_not_dead_letter(self, tmp_path, monkeypatch):
        """Encoded-word contents are opaque: 1,200 nested parentheses in
        charset labels used to reach the recursive comment parser via
        the From header and dead-letter the message."""
        from_value = "\r\n ".join(
            ["=?x" + "(" * 60 + "?q?A?="] * 20 + ["=?x" + ")" * 60 + "?q?B?="] * 20
        )
        db, queue, participants = self._ingest_headers(
            tmp_path,
            monkeypatch,
            f"From: {from_value} <bob@example.com>\r\nTo: reader@example.com\r\n",
            "nested-labels@example.com",
        )
        assert queue.stats() == {"queued": 0, "dead": 0}
        assert db.get_chunk_ids_for_message("nested-labels@example.com")
        assert {p[1] for p in participants if p[0] == "from"} == {"bob@example.com"}

    def test_encoded_word_parentheses_cannot_alter_the_sender_address(self, tmp_path, monkeypatch):
        """In ``bob@example.com (=?utf-8?q?A)_(B?=)`` the encoded-word's own
        parentheses were parsed as comment delimiters, fabricating the
        sender ``bob@example.com_``."""
        db, _queue, participants = self._ingest_headers(
            tmp_path,
            monkeypatch,
            "From: bob@example.com (=?utf-8?q?A)_(B?=)\r\nTo: reader@example.com\r\n",
            "encoded-comment@example.com",
        )
        assert {p[1] for p in participants if p[0] == "from"} == {"bob@example.com"}
        senders = json.loads(db._conn.execute("SELECT senders FROM threads").fetchone()[0])
        assert not any("example.com_" in s for s in senders)

    def test_long_folded_encoded_name_is_decoded(self, tmp_path, monkeypatch):
        """A valid name longer than 998 characters in total — but made of
        short encoded-words on short lines — must still decode, or
        name lookup (find_contact) stops finding the sender."""
        import email.header

        folded = email.header.Header("李雷" * 120, "utf-8", maxlinelen=70).encode(linesep="\r\n")
        db, _queue, participants = self._ingest_headers(
            tmp_path,
            monkeypatch,
            f"From: {folded} <bob@example.com>\r\nTo: reader@example.com\r\n",
            "long-name@example.com",
        )
        assert ("from", "bob@example.com", "李雷" * 120) in participants
        senders = json.loads(db._conn.execute("SELECT senders FROM threads").fetchone()[0])
        assert any("李雷" in s for s in senders)

    def test_group_syntax_inside_encoded_words_parses_in_linear_time(self, tmp_path):
        """Group syntax carried inside encoded-words drove the standard
        library's group parser quadratic via the From header (3.1 s at
        200 KB)."""
        import time

        from src.parser import parse_email

        value = "\r\n ".join(
            ["=?x:?q?Bob?="] + ["=?" + ",".join(["a@x"] * 10) + "?q?A?="] * 4000 + ["=?;?q?B?="]
        )
        path = tmp_path / "INBOX" / "cur" / "group-ew.eml"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            (
                f"From: {value} <bob@example.com>\r\nTo: reader@example.com\r\n"
                "Subject: s\r\nMessage-ID: <group-ew@example.com>\r\n"
                "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n\r\nBody.\r\n"
            ).encode()
        )
        start = time.perf_counter()
        msg = parse_email(path, maildir_root=tmp_path)
        elapsed = time.perf_counter() - start
        assert msg is not None
        assert elapsed < 0.5, f"parse took {elapsed:.2f}s"

    def test_rejected_huge_group_header_parses_in_linear_time(self, tmp_path):
        """A header strict parsing rejects must not fall through to work that
        grows quadratically with its size: a 600 KB ``)Group: ...;`` header
        took ~2 s through a lenient re-parse (the worker is synchronous)."""
        import time

        from src.parser import parse_email

        group = (
            ")Group: " + ",\r\n ".join(", ".join(["a@example.com"] * 40) for _ in range(1000)) + ";"
        )
        path = tmp_path / "INBOX" / "cur" / "group.eml"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            (
                "From: alice@example.com\r\nTo: "
                + group
                + "\r\nSubject: s\r\nMessage-ID: <group@example.com>\r\n"
                "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n\r\nBody.\r\n"
            ).encode()
        )
        start = time.perf_counter()
        msg = parse_email(path, maildir_root=tmp_path)
        elapsed = time.perf_counter() - start
        assert msg is not None
        assert elapsed < 0.5, f"parse took {elapsed:.2f}s"

    def test_malformed_encoded_prefixes_parse_in_linear_time(self, tmp_path):
        """Thousands of unfinished ``=?utf-8?q?`` prefixes in one name must
        not trigger a quadratic re-scan (the worker is synchronous, so a
        slow parse stalls every queued message behind it)."""
        import time

        from src.parser import parse_email

        repeated = "=?utf-8?q?abc "
        chunks = [repeated * 5 for _ in range(1600)]  # 8,000 prefixes, short folded lines
        path = tmp_path / "INBOX" / "cur" / "slow.eml"
        path.parent.mkdir(parents=True)
        path.write_bytes(
            (
                "From: alice@example.com\r\nTo: "
                + "\r\n ".join(chunks)
                + "<bob@example.com>\r\nSubject: s\r\nMessage-ID: <slow@example.com>\r\n"
                "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n\r\nBody.\r\n"
            ).encode()
        )
        start = time.perf_counter()
        msg = parse_email(path, maildir_root=tmp_path)
        elapsed = time.perf_counter() - start
        assert msg is not None
        assert elapsed < 0.5, f"parse took {elapsed:.2f}s"

    @pytest.mark.parametrize(
        ("headers", "expected_recipients"),
        [pytest.param(h, e, id=i) for i, h, e in _ADDRESS_CASES],
    )
    def test_address_header_corpus(self, tmp_path, monkeypatch, headers, expected_recipients):
        message_id = "corpus@example.com"
        db, queue, participants = self._ingest_headers(
            tmp_path,
            monkeypatch,
            "From: Sender <sender@example.com>\r\n" + (headers + "\r\n" if headers else ""),
            message_id,
        )
        assert queue.stats() == {"queued": 0, "dead": 0}, "message must never be dead-lettered"
        assert db.get_chunk_ids_for_message(message_id), "body must be indexed"
        assert participants == {("from", "sender@example.com", "Sender")} | expected_recipients

    @pytest.mark.parametrize("with_reconciler", [False, True])
    def test_cross_folder_move_updates_message_folder(self, tmp_path, monkeypatch, with_reconciler):
        """The watcher's rename fast path (indexed source) must keep the
        per-message folder in step when a move crosses folders — otherwise
        the record points at Archive but still claims INBOX, and folder
        predicates mis-enumerate it forever."""
        maildir = tmp_path / "maildir"
        src = maildir / "INBOX" / "cur" / "1700000000.M1.host:2,S"
        _write_eml(src, "moved@example.com")
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        main.initial_index(db, make_mock_embedder(_UNIT_VECTOR), Threader(db), queue)

        dest = maildir / "Archive" / "cur" / "1700000000.M1.host:2,S"
        dest.parent.mkdir(parents=True)
        src.rename(dest)
        reconciler = None
        if with_reconciler:
            from src.reconciler import Reconciler, ReconcilerConfig

            reconciler = Reconciler(
                db,
                make_mock_embedder(_UNIT_VECTOR),
                Threader(db),
                ReconcilerConfig(
                    enabled=True,
                    grace_days=7,
                    sweep_interval_secs=60,
                    max_batch_pct=1.0,
                    force=False,
                    unlink_on_reap=False,
                ),
                maildir_root=maildir,
            )
        main.MaildirHandler(db, queue, reconciler=reconciler).on_moved(
            _FakeEvent(str(src), str(dest))
        )

        row = db._conn.execute(
            "SELECT folder, filepath FROM messages WHERE message_id = 'moved@example.com'"
        ).fetchone()
        assert row["filepath"] == str(dest)
        assert row["folder"] == "Archive"

    def test_flag_rename_within_folder_keeps_folder(self, tmp_path, monkeypatch):
        maildir = tmp_path / "maildir"
        src = maildir / "Clients" / "Acme" / "cur" / "1700000000.M1.host:2,S"
        _write_eml(src, "flag@example.com")
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        main.initial_index(db, make_mock_embedder(_UNIT_VECTOR), Threader(db), queue)
        folder_before = db._conn.execute("SELECT folder FROM messages").fetchone()["folder"]

        dest = src.with_name("1700000000.M1.host:2,RS")
        src.rename(dest)
        main.MaildirHandler(db, queue).on_moved(_FakeEvent(str(src), str(dest)))

        row = db._conn.execute("SELECT folder, filepath FROM messages").fetchone()
        assert row["filepath"] == str(dest)
        assert row["folder"] == folder_before

    def test_indexing_records_message_with_source_hash_and_reap_clears_it(
        self, tmp_path, monkeypatch
    ):
        """Through the real parse -> index path, the ``messages`` row's
        ``content_hash`` is the SHA-256 of the raw file on disk (the
        provenance anchor), and a reconciler reap removes the row and its
        participants via the ``message_thread_map`` cascade."""
        import hashlib

        from src.reconciler import Reconciler, ReconcilerConfig

        maildir = tmp_path / "maildir"
        live = maildir / "INBOX" / "cur" / "1700000000.M1.host:2,S"
        _write_eml(
            live,
            "e2e@example.com",
            from_addr="Alice <Alice@Example.com>",
            to_addr="bob@example.com",
        )
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        embedder = make_mock_embedder(_UNIT_VECTOR)
        threader = Threader(db)
        main.initial_index(db, embedder, threader, _make_queue(db))

        row = db._conn.execute(
            "SELECT filepath, folder, content_hash, size_bytes FROM messages "
            "WHERE message_id = 'e2e@example.com'"
        ).fetchone()
        raw = live.read_bytes()
        assert row["filepath"] == str(live)
        assert row["folder"] == "INBOX"
        assert row["content_hash"] == hashlib.sha256(raw).hexdigest()
        assert row["size_bytes"] == len(raw)
        participants = {
            (r["role"], r["address"])
            for r in db._conn.execute("SELECT role, address FROM message_participants")
        }
        assert participants == {("from", "alice@example.com"), ("to", "bob@example.com")}

        trashed = live.with_name("1700000000.M1.host:2,ST")
        live.rename(trashed)
        reconciler = Reconciler(
            db,
            embedder,
            threader,
            ReconcilerConfig(
                enabled=True,
                grace_days=0,
                sweep_interval_secs=60,
                max_batch_pct=1.0,
                force=False,
                unlink_on_reap=False,
            ),
            maildir_root=maildir,
        )
        reconciler.sweep()
        reconciler.reap()

        assert db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert db._conn.execute("SELECT COUNT(*) FROM message_participants").fetchone()[0] == 0
