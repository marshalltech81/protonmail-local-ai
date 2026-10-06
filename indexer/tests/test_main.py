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

import email.errors
import json
import logging
import os
import sqlite3
import sys
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import httpx2
import pytest
from openai import APIStatusError
from src import main, parser
from src.database import EMBEDDING_DIM, Database
from src.embed_identity import CALIBRATION_TEXT
from src.maildir import SyncStamp
from src.queue import REASON_INITIAL_SCAN, REASON_REEXTRACT, IndexingQueue
from src.threader import Threader
from src.timings import TimingAggregator

from tests.conftest import make_message, make_mock_embedder, make_thread

# Captured before any test monkeypatches the name, so the sorted
# wrapper installed by ``_run`` still walks the real Maildir.
_REAL_ITER_MAILDIR_MESSAGES = main._iter_maildir_messages

_UNIT_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIM - 1)


def _set_parser_clock(monkeypatch, when: datetime) -> None:
    """Pin the parser's ``datetime.now`` (its missing-Date fallback)."""

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(parser, "datetime", _Clock)


class _FakeEvent:
    def __init__(self, src_path: str, dest_path: str, is_directory: bool = False):
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


def _make_queue(db: Database) -> IndexingQueue:
    """Queue with tight retry limits so tests that exercise failure
    paths don't wait on real-world 30 s backoffs."""
    return IndexingQueue(db, max_attempts=3, base_backoff_seconds=0)


def _drain(
    queue: IndexingQueue,
    db: Database,
    embedder,
    threader: Threader,
    *,
    batch_size: int = 8,
    max_passes: int | None = None,
) -> int:
    """Run ``main._drain_queue_batched`` with a throwaway timing aggregator."""
    return main._drain_queue_batched(
        db,
        embedder,
        threader,
        queue,
        batch_size=batch_size,
        max_passes=max_passes,
        timing_aggregator=TimingAggregator(window=4),
    )


def _index_one(path: Path, db: Database, embedder, threader: Threader):
    """Drain one file through a private 1-attempt queue and report
    ``(succeeded, stage, error)`` from its queue row.

    One attempt means any failure dead-letters immediately, so the row
    is either gone (indexed, or skipped because the file vanished) or
    ``dead`` with the failing stage and error.
    """
    private_queue = IndexingQueue(db, max_attempts=1, base_backoff_seconds=0)
    private_queue.enqueue(str(path), reason="index_one_file")
    _drain(private_queue, db, embedder, threader, batch_size=1, max_passes=1)
    row = db._conn.execute(
        "SELECT status, last_stage, last_error FROM indexing_jobs WHERE filepath = ?",
        (str(path),),
    ).fetchone()
    if row is None:
        # Row deleted: indexed, or mark_skipped because the file vanished
        # between enqueue and parse (the mbsync flag-rename race).
        if not path.exists():
            return (
                False,
                "parse_skipped_missing",
                "FileNotFoundError(file moved between enqueue and parse)",
            )
        return True, "db_write", None
    return False, row["last_stage"] or "unknown", row["last_error"] or ""


def _chunk_ids(db: Database, message_id: str, attachment_id: str | None = None) -> set[str]:
    """Chunk IDs stored for every claimant of the bare ``message_id``.

    Chunks are keyed by claimant ID (Message-ID plus a hash of the
    file's bytes), which tests writing real files do not know."""
    ids: set[str] = set()
    for row in db._conn.execute(
        "SELECT claimant_id FROM message_thread_map WHERE message_id = ?", (message_id,)
    ):
        ids |= db.get_chunk_ids_for_message(row["claimant_id"], attachment_id=attachment_id)
    return ids


def _write_eml(
    path: Path,
    message_id: str,
    subject: str = "Hello",
    *,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    date: str | None = "Mon, 01 Jan 2024 12:00:00 +0000",
    from_addr: str = "alice@example.com",
    to_addr: str = "bob@example.com",
    body: str | None = None,
    received: str | None = None,
) -> None:
    """``date=None`` omits the Date header entirely; ``body`` defaults
    to ``Body of <message_id>.``; ``received`` adds a top ``Received:``
    header dated with it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = [
        f"From: {from_addr}",
        f"To: {to_addr}",
        f"Subject: {subject}",
        f"Message-ID: <{message_id}>",
        "Content-Type: text/plain; charset=utf-8",
    ]
    if date is not None:
        headers.insert(4, f"Date: {date}")
    if in_reply_to:
        headers.append(f"In-Reply-To: <{in_reply_to}>")
    if references:
        headers.append("References: " + " ".join(f"<{r}>" for r in references))
    if received is not None:
        headers.insert(0, f"Received: from mx.example.net by mail.example.org; {received}")
    path.write_text(
        "\r\n".join(headers) + f"\r\n\r\n{body or f'Body of {message_id}.'}\r\n",
        encoding="utf-8",
    )


def _write_eml_with_text_attachment(
    path: Path, message_id: str, *, body: str | None = None
) -> None:
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
        f"{body or f'Body of {message_id}.'}\r\n"
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


class TestIntEnv:
    """``main._int_env`` validates rather than coerces (#481).

    Unset or empty yields the default; a non-integer or a value below
    the minimum stops startup naming the variable, instead of silently
    falling back or clamping (``-5`` used to become ``1``).
    """

    def test_unset_and_empty_yield_default(self, monkeypatch):
        monkeypatch.delenv("FAKE_INT_VAR", raising=False)
        assert main._int_env("FAKE_INT_VAR", 7) == 7
        monkeypatch.setenv("FAKE_INT_VAR", "  ")
        assert main._int_env("FAKE_INT_VAR", 7) == 7

    def test_valid_values_are_returned(self, monkeypatch):
        monkeypatch.setenv("FAKE_INT_VAR", " 42 ")
        assert main._int_env("FAKE_INT_VAR", 7) == 42
        monkeypatch.setenv("FAKE_INT_VAR", "0")
        assert main._int_env("FAKE_INT_VAR", 7, minimum=0) == 0
        monkeypatch.setenv("FAKE_INT_VAR", "60")
        assert main._int_env("FAKE_INT_VAR", 600, minimum=60) == 60

    @pytest.mark.parametrize("raw", ["-5", "0"])
    def test_below_minimum_fails(self, monkeypatch, raw):
        monkeypatch.setenv("FAKE_INT_VAR", raw)
        with pytest.raises(ValueError, match="FAKE_INT_VAR.*>= 1"):
            main._int_env("FAKE_INT_VAR", 7)

    def test_below_explicit_minimum_fails(self, monkeypatch):
        monkeypatch.setenv("FAKE_INT_VAR", "59")
        with pytest.raises(ValueError, match="FAKE_INT_VAR.*>= 60"):
            main._int_env("FAKE_INT_VAR", 600, minimum=60)

    @pytest.mark.parametrize("raw", ["tru", "nan", "inf", "1.5", "ten"])
    def test_non_integer_fails(self, monkeypatch, raw):
        monkeypatch.setenv("FAKE_INT_VAR", raw)
        with pytest.raises(ValueError, match="FAKE_INT_VAR.*integer"):
            main._int_env("FAKE_INT_VAR", 7)


class TestChunkBudgets:
    """The chunk budgets are checked against each other at startup (#507).

    ``chunker.chunk_message`` requires ``target <= max`` and
    ``overlap < target`` and raises on every message otherwise, so a bad
    combination used to start cleanly and dead-letter the whole queue.
    """

    @pytest.mark.parametrize(
        ("target", "max_", "overlap"),
        [
            (1000, 1500, 150),  # the defaults
            (1500, 1500, 150),  # target == max is allowed
            (1000, 1500, 999),  # overlap == target - 1 is allowed
            (1000, 1500, 0),  # no overlap
            (1, 1, 0),  # smallest valid budgets
        ],
    )
    def test_valid_combinations_pass(self, target, max_, overlap):
        main._check_chunk_budgets(target, max_, overlap)

    def test_module_defaults_pass(self):
        main._check_chunk_budgets(
            main.CHUNK_TARGET_TOKENS, main.CHUNK_MAX_TOKENS, main.CHUNK_OVERLAP_TOKENS
        )

    def test_target_above_max_fails(self):
        with pytest.raises(
            ValueError,
            match=r"INDEXER_CHUNK_TARGET_TOKENS=2000 must be <= INDEXER_CHUNK_MAX_TOKENS=1500",
        ):
            main._check_chunk_budgets(2000, 1500, 150)

    @pytest.mark.parametrize("overlap", [1000, 1001])
    def test_overlap_not_below_target_fails(self, overlap):
        with pytest.raises(
            ValueError,
            match=rf"INDEXER_CHUNK_OVERLAP_TOKENS={overlap} must be < "
            r"INDEXER_CHUNK_TARGET_TOKENS=1000",
        ):
            main._check_chunk_budgets(1000, 1500, overlap)

    @pytest.mark.parametrize(
        ("target", "max_", "overlap"),
        [
            (1000, 1500, 150),
            (1500, 1500, 1499),
            (2000, 1500, 150),
            (1000, 1500, 1000),
        ],
    )
    def test_checks_agree_with_the_chunker(self, target, max_, overlap):
        """Startup rejects exactly the combinations the chunker rejects."""
        from src.chunker import chunk_message

        try:
            chunk_message(
                message_pk="m1",
                body_text="hello world",
                target_tokens=target,
                max_tokens=max_,
                overlap_tokens=overlap,
            )
            chunker_ok = True
        except ValueError:
            chunker_ok = False
        try:
            main._check_chunk_budgets(target, max_, overlap)
            startup_ok = True
        except ValueError:
            startup_ok = False
        assert startup_ok == chunker_ok

    def test_import_fails_on_a_bad_combination(self):
        """The check runs when the module loads, not per message."""
        import subprocess  # nosec B404 - runs this test's own interpreter
        import sys

        env = {**os.environ, "INDEXER_CHUNK_TARGET_TOKENS": "2000"}
        env.pop("INDEXER_CHUNK_MAX_TOKENS", None)
        result = subprocess.run(  # nosec B603 - fixed argv, no shell
            [sys.executable, "-c", "import src.main"],
            cwd=Path(main.__file__).resolve().parent.parent,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode != 0
        assert "INDEXER_CHUNK_TARGET_TOKENS=2000 must be <=" in result.stderr


class TestBoolEnv:
    """``main._bool_env`` accepts a fixed vocabulary and rejects the rest (#481).

    ``INDEXER_OCR_ENABLED=tru`` used to read as false and silently
    disable OCR.
    """

    def test_unset_and_empty_yield_default(self, monkeypatch):
        monkeypatch.delenv("FAKE_BOOL_VAR", raising=False)
        assert main._bool_env("FAKE_BOOL_VAR", True) is True
        assert main._bool_env("FAKE_BOOL_VAR", False) is False
        monkeypatch.setenv("FAKE_BOOL_VAR", " ")
        assert main._bool_env("FAKE_BOOL_VAR", True) is True

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
    def test_truthy_values(self, monkeypatch, raw):
        monkeypatch.setenv("FAKE_BOOL_VAR", raw)
        assert main._bool_env("FAKE_BOOL_VAR", False) is True

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " Off "])
    def test_falsy_values(self, monkeypatch, raw):
        monkeypatch.setenv("FAKE_BOOL_VAR", raw)
        assert main._bool_env("FAKE_BOOL_VAR", True) is False

    @pytest.mark.parametrize("raw", ["tru", "-5", "nan", "inf", "enabled"])
    def test_unrecognized_values_fail(self, monkeypatch, raw):
        monkeypatch.setenv("FAKE_BOOL_VAR", raw)
        with pytest.raises(ValueError, match="FAKE_BOOL_VAR.*not recognized"):
            main._bool_env("FAKE_BOOL_VAR", True)


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
        _drain(queue, db, embedder, threader)
        assert db.is_indexed(str(dest))

    def test_pending_retry_survives_a_flag_rename(self, tmp_path):
        """Regression (#203): Phase 1 committed reply B (so its path is
        indexed), the embedder was down so B's job was deferred, then
        mbsync renamed B for a flag change. The rename moved the indexed
        path but not the job, whose old path then hit FileNotFoundError
        and was dropped — B's chunks were never written, with no pending
        or dead job left to say so."""
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        queue = _make_queue(db)
        embedder = make_mock_embedder()
        embedder.embed.return_value = _UNIT_VECTOR

        root = tmp_path / "INBOX" / "cur" / "a:2,S"
        _write_eml(root, "a@example.com", subject="Plan")
        queue.enqueue(str(root), REASON_INITIAL_SCAN)
        _drain(queue, db, embedder, threader)

        reply = tmp_path / "INBOX" / "cur" / "b:2,S"
        _write_eml(reply, "b@example.com", subject="Re: Plan", in_reply_to="a@example.com")
        queue.enqueue(str(reply), REASON_INITIAL_SCAN)
        embedder.embed.side_effect = _connection_error()  # outage: deferred
        _drain(queue, db, embedder, threader)
        assert db.is_indexed(str(reply))
        assert not _chunk_ids(db, "b@example.com")

        renamed = reply.with_name("b:2,RS")
        reply.rename(renamed)
        main.MaildirHandler(db, queue).on_moved(
            _FakeEvent(src_path=str(reply), dest_path=str(renamed))
        )

        embedder.embed.side_effect = None
        _make_due(db)
        _drain(queue, db, embedder, threader)

        assert _chunk_ids(db, "b@example.com")
        assert queue.stats() == {"queued": 0, "dead": 0}

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
        _drain(queue, db, embedder, threader)
        first_call_count = embedder.embed.call_count
        assert first_call_count == 1

        # Second move event on the same path (e.g., flag rename) must not
        # re-enqueue work or trigger another embed.
        handler.on_moved(_FakeEvent(src_path=str(dest), dest_path=str(dest)))
        _drain(queue, db, embedder, threader)
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
        _drain(queue, db, embedder, threader)
        deliveries = embedder.embed.call_count
        assert deliveries == 1

        # mbsync marks it replied: renames file to msg:2,SR.
        renamed_path = tmp_path / "INBOX" / "cur" / "1738500000.uniq.proton:2,SR"
        original_path.rename(renamed_path)
        handler.on_moved(_FakeEvent(src_path=str(original_path), dest_path=str(renamed_path)))
        _drain(queue, db, embedder, threader)

        # Must not have re-parsed or re-embedded.
        assert embedder.embed.call_count == deliveries
        # indexed_files now tracks the new filepath; old path is gone.
        assert db.is_indexed(str(renamed_path))
        assert not db.is_indexed(str(original_path))

    def test_flag_rename_updates_read_state_without_reindexing(self, tmp_path):
        """mbsync carries a read / flag change from Proton as a rename
        (new/ -> cur/, then ``:2,S`` -> ``:2,FS``). The message's state
        follows each rename with no parse, embed or duplicate row."""
        db = Database(tmp_path / "db" / "mail.db")
        threader = Threader(db)
        queue = _make_queue(db)
        delivered = tmp_path / "INBOX" / "new" / "1738500000.state.proton"
        _write_eml(delivered, "state@example.com")
        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM
        handler = main.MaildirHandler(db, queue)
        handler.on_moved(_FakeEvent(src_path=str(tmp_path / "tmp" / "m"), dest_path=str(delivered)))
        _drain(queue, db, embedder, threader)
        assert embedder.embed.call_count == 1

        def state():
            return [
                tuple(r)
                for r in db._conn.execute("SELECT filepath, seen, flagged, replied FROM messages")
            ]

        assert state() == [(str(delivered), 0, 0, 0)]

        read = tmp_path / "INBOX" / "cur" / "1738500000.state.proton:2,S"
        read.parent.mkdir(parents=True, exist_ok=True)
        delivered.rename(read)
        handler.on_moved(_FakeEvent(src_path=str(delivered), dest_path=str(read)))
        starred = read.with_name("1738500000.state.proton:2,FS")
        read.rename(starred)
        handler.on_moved(_FakeEvent(src_path=str(read), dest_path=str(starred)))
        _drain(queue, db, embedder, threader)

        assert embedder.embed.call_count == 1
        assert state() == [(str(starred), 1, 1, 0)]


class TestInitialIndexNestedFolders:
    def test_recursive_scan_indexes_nested_folders(self, tmp_path, monkeypatch):
        """Regression: ``initial_index`` walked only one level under
        ``MAILDIR_PATH``, so nested folders like ``Clients/ABC``
        (``Clients/.ABC`` under mbsync's ``SubFolders Legacy``) were never
        scanned. The recursive walk now picks them up at any depth."""
        maildir = tmp_path / "maildir"
        nested = maildir / "Clients" / ".ABC" / "cur"
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
        """Once indexed, a nested message's stored ``folder`` is the full
        folder name, read back from mbsync's ``SubFolders Legacy`` path."""
        maildir = tmp_path / "maildir"
        nested = maildir / "Clients" / ".ABC" / "cur"
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

        def slow_phase2a(state, db_arg, all_texts, **kwargs):
            marker_touches.append("phase2a:enter")
            result = original_phase2a(state, db_arg, all_texts, **kwargs)
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
        _drain(queue, db, embedder, threader)
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

        _drain(queue, db, embedder, threader, batch_size=1, max_passes=1)

        assert _chunk_ids(db, "retry@example.com")
        assert queue.stats() == {"queued": 0, "dead": 0}

    def test_unreadable_file_during_sync_is_deferred_not_dead_lettered(self, tmp_path, monkeypatch):
        """Regression (#212): mbsync relaxes new files' permissions only
        after the whole sync finishes, so during a long sync the indexer
        can read a 0600 file for longer than its retry budget. Charging
        those PermissionErrors dead-lettered valid mail; they are now
        deferred without spending attempts, and the file indexes once
        readable."""
        dest = tmp_path / "INBOX" / "new" / "fresh.eml"
        _write_eml(dest, "fresh@example.com")
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        queue = _make_queue(db)  # max_attempts=3
        embedder = make_mock_embedder()
        embedder.embed.return_value = _UNIT_VECTOR
        real_parse = main.parse_email

        def unreadable(path, **kwargs):
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(main, "parse_email", unreadable)
        queue.enqueue(str(dest), "test")
        for _ in range(6):
            _drain(queue, db, embedder, threader)
            row = db._conn.execute(
                "SELECT status, attempts, last_stage, next_attempt_at FROM indexing_jobs"
            ).fetchone()
            assert row["status"] == "queued"
            assert row["attempts"] == 0
            assert row["last_stage"] == "parse"
            _make_due(db)

        monkeypatch.setattr(main, "parse_email", real_parse)
        _drain(queue, db, embedder, threader)

        assert _chunk_ids(db, "fresh@example.com")
        assert queue.stats() == {"queued": 0, "dead": 0}

    def test_permission_deferral_is_told_apart_from_a_permission_retry(self, tmp_path, monkeypatch):
        """Codex round 2 on #904: a deferral and a retry of the same
        unreadable file both fail at the parse stage on a
        ``PermissionError``. The deferral writes the fixed deferral text,
        so the heartbeat counts only it as ``deferred_permission``; the
        retry after the 24 h window keeps the errno text and counts as
        ``retrying``."""
        from src.queue import PERMISSION_DEFERRED_ERROR

        dest = tmp_path / "INBOX" / "new" / "fresh.eml"
        _write_eml(dest, "fresh@example.com")
        db = Database(tmp_path / "mail.db")
        # A real backoff, so the retry path records one attempt per drain.
        queue = IndexingQueue(db, max_attempts=3, base_backoff_seconds=60)
        monkeypatch.setattr(
            main,
            "parse_email",
            lambda path, **kw: (_ for _ in ()).throw(
                PermissionError(13, f"{SYNTHETIC_MARKER} denied", f"/x/{SYNTHETIC_MARKER}")
            ),
        )
        queue.enqueue(str(dest), "test")

        _drain(queue, db, make_mock_embedder(), Threader(db))
        row = db._conn.execute("SELECT attempts, last_error FROM indexing_jobs").fetchone()
        assert row["attempts"] == 0
        assert row["last_error"] == PERMISSION_DEFERRED_ERROR
        assert SYNTHETIC_MARKER not in row["last_error"]
        counts = queue.heartbeat_counts()
        assert (counts["deferred_permission"], counts["retrying"]) == (1, 0)

        db._conn.execute("UPDATE indexing_jobs SET created_at = '2000-01-01T00:00:00+00:00'")
        db._conn.commit()
        _make_due(db)
        _drain(queue, db, make_mock_embedder(), Threader(db))
        row = db._conn.execute("SELECT attempts, last_error FROM indexing_jobs").fetchone()
        assert row["attempts"] == 1
        assert row["last_error"] == f"PermissionError: [Errno 13] {os.strerror(13)}"
        counts = queue.heartbeat_counts()
        assert (counts["deferred_permission"], counts["retrying"]) == (0, 1)

    def test_file_unreadable_for_a_day_takes_the_normal_retry_path(self, tmp_path, monkeypatch):
        """A permissions fault that outlasts any sync still ends in a
        visible terminal state rather than deferring forever."""
        dest = tmp_path / "INBOX" / "new" / "stuck.eml"
        _write_eml(dest, "stuck@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        monkeypatch.setattr(
            main,
            "parse_email",
            lambda path, **kw: (_ for _ in ()).throw(PermissionError(13, "denied")),
        )
        queue.enqueue(str(dest), "test")
        db._conn.execute("UPDATE indexing_jobs SET created_at = '2000-01-01T00:00:00+00:00'")
        db._conn.commit()

        _drain(queue, db, make_mock_embedder(), Threader(db))

        assert queue.is_dead(str(dest))

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

        def boom(msg, **_kwargs):
            raise RuntimeError("simulated html2text runaway")

        monkeypatch.setattr(parser, "_extract_body_and_attachments", boom)

        queue.enqueue(str(dest), "test")
        _drain(queue, db, embedder, threader)
        _drain(queue, db, embedder, threader)
        _drain(queue, db, embedder, threader)

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
        assert row["last_error"] == "RuntimeError"

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
            _drain(queue, db, embedder, threader)
            _make_due(db)

        # Phase 1 commits thread membership + indexed_files eagerly, so
        # the file is keyword-searchable but chunkless until the
        # embedder returns.
        assert db.is_indexed(str(dest))
        assert not _chunk_ids(db, "giveup@example.com")
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
        # ``_index_one`` must distinguish this from EACCES so the
        # worker can drop the row instead of consuming retry budget.
        dest = tmp_path / "INBOX" / "cur" / "definitely-not-here.eml"

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        embedder = make_mock_embedder()
        embedder.embed.return_value = [0.0] * EMBEDDING_DIM

        ok, stage, err = _index_one(dest, db, embedder, threader)

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

        _drain(queue, db, embedder, threader)

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
        # the same file on every container restart, and ``_index_one``
        # interpreted "row gone + file present" as success — the file
        # was never actually indexed. The fix routes oversized through
        # ``mark_dead_terminal``: row stays at status='dead', the
        # ``is_dead`` gate skips it on restart, and ``_index_one``
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

        ok, stage, err = _index_one(dest, db, embedder, threader)

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
        # it readable. ``_index_one`` must surface that as a parse
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
            ok, stage, err = _index_one(dest, db, embedder, threader)
        finally:
            os.chmod(dest, 0o644)

        assert ok is False
        assert stage == "parse"
        assert err is not None
        assert "PermissionError" in err or "Errno 13" in err


class TestValidateEmbedConfig:
    """``_validate_embed_config`` enforces the per-layer startup contract
    before the indexer constructs the embedder client: ``EMBED_MODEL``
    and ``EMBED_API_KEY`` must be non-empty, and ``EMBED_BASE_URL`` must
    name the endpoint: a URL, or ``default`` for the SDK default (#750).

    Pre-tightening, ``EMBED_API_KEY`` could be empty and the indexer
    would happily start, only failing at the first embed call against
    a remote provider with a 401. Now empty keys fail closed at startup
    so a missing secret surfaces in the same place as a missing
    ``EMBED_MODEL``.
    """

    def test_complete_config_passes_silently(self, monkeypatch):
        monkeypatch.setattr(main, "EMBED_BASE_URL", "http://x/v1")
        monkeypatch.setattr(main, "EMBED_MODEL", "qwen-embed")
        monkeypatch.setattr(main, "EMBED_API_KEY", "sk-real")  # pragma: allowlist secret
        # No raise == pass.
        main._validate_embed_config()

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_base_url_raises(self, monkeypatch, value):
        # #750: an API key is not consent to the SDK's default endpoint,
        # because the request body (mail text) is sent before the
        # provider checks the key. An empty URL fails closed, naming
        # both fixes and where ``default`` sends mail, never the key.
        monkeypatch.setattr(main, "EMBED_BASE_URL", value)
        monkeypatch.setattr(main, "EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")
        monkeypatch.setattr(main, "EMBED_API_KEY", "sk-real")  # pragma: allowlist secret
        with pytest.raises(ValueError) as excinfo:
            main._validate_embed_config()
        assert str(excinfo.value) == (
            "EMBED_BASE_URL is empty: set it to the provider's URL, or to `default` "
            "to use the SDK's default endpoint (sends mail to api.openai.com)."
        )
        assert "sk-real" not in str(excinfo.value)

    @pytest.mark.parametrize("value", ["default", " Default ", "DEFAULT"])
    def test_default_selects_the_official_endpoint(self, monkeypatch, value):
        # ``default`` is pinned to OpenAI's own URL, so the SDK's
        # ``OPENAI_BASE_URL`` cannot redirect it (Codex round 1 on #773).
        monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.example/v1")
        monkeypatch.setattr(main, "EMBED_BASE_URL", value)
        monkeypatch.setattr(main, "EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")
        monkeypatch.setattr(main, "EMBED_API_KEY", "sk-real")  # pragma: allowlist secret
        assert main._validate_embed_config() == "https://api.openai.com/v1"

    def test_real_url_is_returned(self, monkeypatch):
        monkeypatch.setattr(main, "EMBED_BASE_URL", "http://host.docker.internal:8001/v1")
        monkeypatch.setattr(main, "EMBED_MODEL", "qwen-embed")
        monkeypatch.setattr(main, "EMBED_API_KEY", "unauthenticated")
        assert main._validate_embed_config() == "http://host.docker.internal:8001/v1"

    def test_missing_model_raises(self, monkeypatch):
        # ``EMBED_MODEL`` stays required: no SDK has a default model,
        # so an empty value always fails at request time. Catching it
        # at startup gives an actionable error.
        monkeypatch.setattr(main, "EMBED_BASE_URL", "http://x/v1")
        monkeypatch.setattr(main, "EMBED_MODEL", "")
        monkeypatch.setattr(main, "EMBED_API_KEY", "sk-real")  # pragma: allowlist secret
        with pytest.raises(ValueError, match="EMBED_MODEL"):
            main._validate_embed_config()

    def test_whitespace_only_model_raises(self, monkeypatch):
        """Codex round 2 on #773: the contract is non-empty after
        trimming, so blank space is as missing as an empty value."""
        monkeypatch.setattr(main, "EMBED_BASE_URL", "http://x/v1")
        monkeypatch.setattr(main, "EMBED_MODEL", "   ")
        monkeypatch.setattr(main, "EMBED_API_KEY", "sk-real")  # pragma: allowlist secret
        with pytest.raises(ValueError, match="EMBED_MODEL"):
            main._validate_embed_config()

    def test_empty_api_key_raises(self, monkeypatch):
        # The startup contract: every enabled operator-supplied layer
        # needs a non-empty key. Operators pointing at an unauthenticated host-side server supply any
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


class TestMainEmbedEndpoint:
    """#750: ``main()`` resolves ``EMBED_BASE_URL`` before it opens the
    database or builds the embedder, so an empty value cannot reach the
    SDK's default endpoint."""

    class _Stop(Exception):
        pass

    def _configure(self, monkeypatch, base_url):
        monkeypatch.setattr(main, "EMBED_BASE_URL", base_url)
        monkeypatch.setattr(main, "EMBED_MODEL", "synthetic-embed")
        monkeypatch.setattr(main, "EMBED_API_KEY", "unauthenticated")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    def test_empty_url_fails_before_any_client_or_database(self, monkeypatch):
        self._configure(monkeypatch, "")
        built: list[str] = []
        monkeypatch.setattr(main, "Database", lambda path: built.append("database"))
        monkeypatch.setattr(main, "OpenAIEmbedder", lambda **kw: built.append("embedder"))
        # The SDK constructor itself, in case anything bypasses the wrapper.
        monkeypatch.setattr("src.embedder.OpenAI", lambda **kw: built.append("sdk"))
        with pytest.raises(ValueError, match="EMBED_BASE_URL is empty"):
            main.main()
        assert built == []

    def _run_to_embedder(self, tmp_path, monkeypatch, caplog, base_url):
        """Run ``main()`` with the real ``OpenAIEmbedder`` until just after
        it is built; return the kwargs the SDK constructor received."""
        self._configure(monkeypatch, base_url)
        from openai import OpenAI as real_openai

        sdk_calls: list[dict] = []

        def recording_openai(**kwargs):
            sdk_calls.append(kwargs)
            return real_openai(**kwargs)

        monkeypatch.setattr("src.embedder.OpenAI", recording_openai)
        db = Database(tmp_path / "mail.db")
        monkeypatch.setattr(main, "Database", lambda path: db)

        def stop(_db):
            raise self._Stop

        monkeypatch.setattr(main, "Threader", stop)
        caplog.set_level(logging.INFO)
        with pytest.raises(self._Stop):
            main.main()
        return sdk_calls

    @pytest.mark.parametrize("value", ["default", " Default "])
    def test_default_pins_the_sdk_client_to_the_official_url(
        self, tmp_path, monkeypatch, caplog, value
    ):
        monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.example/v1")
        [kwargs] = self._run_to_embedder(tmp_path, monkeypatch, caplog, value)
        assert kwargs["base_url"] == "https://api.openai.com/v1"
        # The privacy warning names the SDK's default host.
        assert (
            "Privacy: EMBED_MODE=openai sends email text off this host, to api.openai.com."
            in caplog.text
        )

    def test_real_url_is_passed_to_the_sdk(self, tmp_path, monkeypatch, caplog):
        [kwargs] = self._run_to_embedder(
            tmp_path, monkeypatch, caplog, "http://host.docker.internal:8001/v1/"
        )
        assert kwargs["base_url"] == "http://host.docker.internal:8001/v1"
        assert "Privacy:" not in caplog.text


class TestWarnIfRemoteEndpoint:
    """``_warn_if_remote_endpoint`` flags an embedder that is not
    host-local: indexing sends mail text there (#622)."""

    @staticmethod
    def _warnings(caplog):
        return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]

    @pytest.mark.parametrize(
        ("url", "host"),
        [
            ("https://api.openai.com/v1", "api.openai.com"),
            ("http://192.0.2.10:1234/v1", "192.0.2.10"),
        ],
    )
    def test_remote_url_warns_once(self, caplog, url, host):
        caplog.set_level(logging.DEBUG)
        main._warn_if_remote_endpoint("EMBED_MODE", "openai", url, "email text")
        [message] = self._warnings(caplog)
        assert "EMBED_MODE=openai" in message
        assert host in message
        assert "/v1" not in message
        assert ":1234" not in message

    def test_empty_url_warns_about_sdk_default(self, caplog):
        caplog.set_level(logging.DEBUG)
        main._warn_if_remote_endpoint("EMBED_MODE", "openai", "", "email text")
        [message] = self._warnings(caplog)
        assert "default endpoint" in message

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:1234/v1",
            "http://[::1]:8000/v1",
            "http://localhost:8001/v1",
            "http://host.docker.internal:8001/v1",
        ],
    )
    def test_host_local_url_is_silent(self, caplog, url):
        caplog.set_level(logging.DEBUG)
        main._warn_if_remote_endpoint("EMBED_MODE", "openai", url, "email text")
        assert self._warnings(caplog) == []


class TestStartupWidthCheck:
    """The startup width check rides on the calibration request (#841)."""

    def _embedder(self, vector=None):
        embedder = make_mock_embedder(vector)
        embedder.base_url = "http://host.docker.internal:8001/v1"
        return embedder

    def test_matching_dim_passes_with_one_request(self, tmp_path):
        embedder = self._embedder([0.1] * EMBEDDING_DIM)
        main._check_embedder_identity(Database(tmp_path / "mail.db"), embedder)
        embedder.embed.assert_called_once_with(CALIBRATION_TEXT)

    def test_mismatched_dim_raises_systemexit(self, tmp_path):
        """A 1024-dim model (e.g. mxbai-embed-large) against a 4096-reserved
        schema must fail fast at startup rather than surface later as a
        cryptic sqlite-vec insert error."""
        embedder = self._embedder([0.0] * (EMBEDDING_DIM + 256))
        with pytest.raises(SystemExit) as exc_info:
            main._check_embedder_identity(Database(tmp_path / "mail.db"), embedder)
        assert str(EMBEDDING_DIM) in str(exc_info.value)

    def test_request_failure_exits_without_provider_text(self, tmp_path, caplog):
        """The calibration request runs right after ``wait_for_ready``; a
        failure there exits with type and status only, not the
        provider's response body (#686)."""
        caplog.set_level(logging.DEBUG)
        body = {"error": {"message": "provider text MARKER-686"}}
        embedder = self._embedder()
        embedder.embed.side_effect = APIStatusError(
            message=f"Error code: 402 - {body}",
            response=httpx2.Response(402, json=body, request=httpx2.Request("POST", "http://x")),
            body=body,
        )
        with pytest.raises(SystemExit) as exc_info:
            main._check_embedder_identity(Database(tmp_path / "mail.db"), embedder)
        exc = exc_info.value
        rendered = "".join(traceback.format_exception(exc))
        for text in (str(exc), repr(exc), rendered, caplog.text):
            assert "MARKER-686" not in text
        assert "APIStatusError: status=402" in str(exc)


class TestIndexOneFileChunking:
    """End-to-end of the schema-v9 chunker integration through the
    real ``_index_one`` path — chunker is invoked for each new
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
            ok, stage, err = _index_one(dest, db, embedder, threader)
        finally:
            main_mod.MAILDIR_PATH = original_root

        assert ok, f"failed at {stage}: {err}"

        # Chunk(s) for this message landed in all three indexes.
        chunk_ids = _chunk_ids(db, "chunked@x")
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
            ok, _, _ = _index_one(dest, db, embedder, threader)
            assert ok
            first_call_count = embedder.embed.call_count

            # Second pass: same file, same body, same chunk_ids.
            # Threader will see the existing thread and produce a Thread
            # whose ``messages`` list contains just this re-arrived
            # message; the chunker emits the same chunk_ids; the diff
            # path skips them all and embed should not be called again.
            ok2, _, _ = _index_one(dest, db, embedder, threader)
            assert ok2
        finally:
            main_mod.MAILDIR_PATH = original_root

        # The second pass should not have triggered any new embed calls.
        assert embedder.embed.call_count == first_call_count

    def test_attachment_stored_chunk_ids_are_looked_up_once(self, tmp_path, monkeypatch):
        """#845: preparing an attachment does not diff stored chunk IDs;
        the batched pipeline does it once per occurrence, then embeds."""
        db = Database(tmp_path / "db" / "mail.db")
        threader = Threader(db)
        dest = tmp_path / "INBOX" / "cur" / "msg.eml"
        _write_eml_with_text_attachment(dest, "attachment-lookup@x")

        lookups: list[str | None] = []
        real_lookup = db.get_chunk_ids_for_message

        def counting_lookup(claimant_id, attachment_id=None):
            lookups.append(attachment_id)
            return real_lookup(claimant_id, attachment_id=attachment_id)

        monkeypatch.setattr(db, "get_chunk_ids_for_message", counting_lookup)
        monkeypatch.setattr(main, "MAILDIR_PATH", tmp_path)
        embedder = make_mock_embedder([0.1] * EMBEDDING_DIM)

        ok, _, _ = _index_one(dest, db, embedder, threader)

        assert ok
        attachment_lookups = [a for a in lookups if a is not None]
        assert len(attachment_lookups) == 1
        assert _chunk_ids(db, "attachment-lookup@x")
        embedded = [t for call in embedder.embed_batch.call_args_list for t in call.args[0]]
        assert embedded.count("attachment text that should be chunked") == 1

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
            ok, stage, err = _index_one(dest, db, embedder, threader)
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
        assert not _chunk_ids(db, "attachment-retry@x")
        assert db._conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
        assert db._conn.execute("SELECT COUNT(*) FROM attachment_extractions").fetchone()[0] == 0


class _WorkerKilled(BaseException):
    """Stands in for the process dying mid-step (OOM kill, a re-raised
    ``MemoryError``, the stall guard's ``os._exit``): nothing in the
    pipeline catches it, so no outcome is recorded."""


class TestInterruptedMessagesDeadLetter:
    """#235: a message that kills the worker was re-claimed at
    ``attempts=0`` after every restart, forever. Now each death charges
    that message — and only that message — one attempt."""

    def _run_until_quiet(self, tmp_path, poison: Path, healthy: Path, max_restarts: int = 6):
        db = Database(tmp_path / "mail.db")
        embedder = make_mock_embedder()
        embedder.embed.return_value = _UNIT_VECTOR
        _make_queue(db).enqueue(str(poison), REASON_INITIAL_SCAN)
        _make_queue(db).enqueue(str(healthy), REASON_INITIAL_SCAN)
        deaths = 0
        healthy_charges: list[int | None] = []
        for _ in range(max_restarts):
            queue = _make_queue(db)  # a restarted indexer, max_attempts=3
            try:
                _drain(queue, db, embedder, Threader(db))
            except _WorkerKilled:
                deaths += 1
                healthy_charges.append(db.queue_get_attempts(str(healthy)))
                continue
            break
        return db, queue, deaths, healthy_charges

    def test_message_that_kills_the_parser_reaches_dead(self, tmp_path, monkeypatch):
        poison = tmp_path / "INBOX" / "new" / "poison.eml"
        healthy = tmp_path / "INBOX" / "new" / "healthy.eml"
        _write_eml(poison, "poison@example.com")
        _write_eml(healthy, "healthy@example.com")

        from src import parser

        real_parse = parser.parse_email

        def parse(path, *args, **kwargs):
            if Path(path).name == "poison.eml":
                raise _WorkerKilled
            return real_parse(path, *args, **kwargs)

        monkeypatch.setattr(main, "parse_email", parse)

        db, queue, deaths, healthy_charges = self._run_until_quiet(tmp_path, poison, healthy)

        assert deaths == 3
        assert queue.is_dead(str(poison))
        assert (
            db._conn.execute(
                "SELECT last_stage FROM indexing_jobs WHERE filepath = ?", (str(poison),)
            ).fetchone()["last_stage"]
            == "interrupted"
        )
        assert all(charge in (None, 0) for charge in healthy_charges)
        assert _chunk_ids(db, "healthy@example.com")

    def test_crash_from_batch_memory_retries_the_row_alone(self, tmp_path, monkeypatch):
        """Review round 1: an out-of-memory kill at row N can come from
        the whole batch's footprint, and a restart replays the same batch
        in the same order. The interrupted row runs alone next, so a
        message that is fine on its own is indexed, not dead-lettered."""
        first = tmp_path / "INBOX" / "new" / "first.eml"
        second = tmp_path / "INBOX" / "new" / "second.eml"
        _write_eml(first, "first@example.com")
        _write_eml(second, "second@example.com")

        real_phase1 = main._phase1_commit_thread
        seen: list[str] = []

        def phase1(row, db, threader, queue):
            seen.append(row["filepath"])
            # "Out of memory" only when another message is in the batch.
            if row["filepath"].endswith("second.eml") and len(seen) > 1:
                raise _WorkerKilled
            return real_phase1(row, db, threader, queue)

        monkeypatch.setattr(main, "_phase1_commit_thread", phase1)

        db = Database(tmp_path / "mail.db")
        embedder = make_mock_embedder()
        embedder.embed.return_value = _UNIT_VECTOR
        _make_queue(db).enqueue(str(first), REASON_INITIAL_SCAN)
        _make_queue(db).enqueue(str(second), REASON_INITIAL_SCAN)
        deaths = 0
        for _ in range(6):
            seen.clear()
            try:
                _drain(_make_queue(db), db, embedder, Threader(db))
            except _WorkerKilled:
                deaths += 1
                continue
            if db.queue_stats()["queued"] == 0:
                break

        assert deaths == 1
        assert db.queue_stats() == {"queued": 0, "dead": 0}
        assert _chunk_ids(db, "first@example.com")
        assert _chunk_ids(db, "second@example.com")

    def test_message_that_kills_extraction_reaches_dead(self, tmp_path, monkeypatch):
        poison = tmp_path / "INBOX" / "new" / "poison.eml"
        healthy = tmp_path / "INBOX" / "new" / "healthy.eml"
        _write_eml(poison, "poison@example.com")
        _write_eml(healthy, "healthy@example.com")

        real_collect = main._phase2a_collect_chunks

        def collect(entry, db, all_texts, **kwargs):
            if entry.row["filepath"].endswith("poison.eml"):
                raise _WorkerKilled
            return real_collect(entry, db, all_texts, **kwargs)

        monkeypatch.setattr(main, "_phase2a_collect_chunks", collect)

        db, queue, deaths, healthy_charges = self._run_until_quiet(tmp_path, poison, healthy)

        assert deaths == 3
        assert queue.is_dead(str(poison))
        assert all(charge in (None, 0) for charge in healthy_charges)
        assert _chunk_ids(db, "healthy@example.com")


class TestInterruptedEmbedPhase:
    """Review round 2: a kill during the bulk embed or the vector commit
    (Phase 2b / 2c) happened after every per-message charge was refunded,
    so the restart rebuilt the same batch and could crash forever."""

    def _drain_with_restarts(self, tmp_path, embed_batch, max_restarts: int = 8):
        a = tmp_path / "INBOX" / "new" / "a.eml"
        b = tmp_path / "INBOX" / "new" / "b.eml"
        _write_eml(a, "a@example.com")
        _write_eml(b, "b@example.com")
        db = Database(tmp_path / "mail.db")
        embedder = make_mock_embedder()
        embedder.embed.return_value = _UNIT_VECTOR
        embedder.embed_batch.side_effect = embed_batch
        _make_queue(db).enqueue(str(a), REASON_INITIAL_SCAN)
        _make_queue(db).enqueue(str(b), REASON_INITIAL_SCAN)
        deaths = 0
        for _ in range(max_restarts):
            try:
                _drain(_make_queue(db), db, embedder, Threader(db))
            except _WorkerKilled:
                deaths += 1
                continue
            if db.queue_stats()["queued"] == 0:
                break
        return db, deaths

    def test_batch_wide_embed_kill_replays_messages_alone(self, tmp_path):
        """Out of memory only while embedding both messages together."""

        def embed_batch(texts, **_kw):
            if sum("Body of" in t for t in texts) > 1:
                raise _WorkerKilled
            return [_UNIT_VECTOR for _ in texts]

        db, deaths = self._drain_with_restarts(tmp_path, embed_batch)

        assert deaths == 1
        assert db.queue_stats() == {"queued": 0, "dead": 0}
        assert _chunk_ids(db, "a@example.com")
        assert _chunk_ids(db, "b@example.com")

    def test_message_that_kills_the_embed_alone_reaches_dead(self, tmp_path):
        def embed_batch(texts, **_kw):
            if any("Body of a@" in t for t in texts):
                raise _WorkerKilled
            return [_UNIT_VECTOR for _ in texts]

        db, deaths = self._drain_with_restarts(tmp_path, embed_batch)

        assert deaths == 4  # once in the batch, then 3 times alone
        assert db.queue_stats() == {"queued": 0, "dead": 1}
        assert _make_queue(db).is_dead(str(tmp_path / "INBOX" / "new" / "a.eml"))
        assert _chunk_ids(db, "b@example.com")


class TestStallGuardProgress:
    """Review round 2: the limit bounded a whole message, so several
    legitimately slow scanned PDFs in one message exceeded it on every
    restart. Each attachment now counts as progress."""

    def _drain_one_counting_progress(self, tmp_path, monkeypatch, embed_batch):
        dest = tmp_path / "INBOX" / "new" / "big.eml"
        _write_eml(dest, "big@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(dest), REASON_INITIAL_SCAN)
        progress: list[int] = []
        real_note = queue.note_progress
        monkeypatch.setattr(queue, "note_progress", lambda: (progress.append(1), real_note()))
        embedder = make_mock_embedder()
        embedder.embed.return_value = _UNIT_VECTOR
        embedder.embed_batch.side_effect = embed_batch
        _drain(queue, db, embedder, Threader(db))
        assert _chunk_ids(db, "big@example.com")
        return progress

    def test_each_embed_sub_batch_counts_as_progress(self, tmp_path, monkeypatch):
        """Review round 3: a lone survivor stays charged, and so watched,
        through the bulk embed. A large message's many individually
        bounded embed requests must each restart the clock, or a healthy
        message on a slow embedder is killed after the limit."""

        def embed_batch(texts, on_batch_complete=None, **_kw):
            for _ in range(3):
                on_batch_complete()
            return [_UNIT_VECTOR for _ in texts]

        progress = self._drain_one_counting_progress(tmp_path, monkeypatch, embed_batch)
        assert len(progress) == 3

    def test_isolated_re_embed_counts_as_progress(self, tmp_path, monkeypatch):
        calls = {"n": 0}

        def embed_batch(texts, on_batch_complete=None, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _status_error(503)  # batch embed fails; probe passes
            for _ in range(2):
                on_batch_complete()
            return [_UNIT_VECTOR for _ in texts]

        progress = self._drain_one_counting_progress(tmp_path, monkeypatch, embed_batch)
        assert len(progress) >= 2

    def test_phase2a_reports_progress_per_attachment(self, tmp_path):
        dest = tmp_path / "INBOX" / "new" / "att.eml"
        _write_eml_with_text_attachment(dest, "att@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(dest), REASON_INITIAL_SCAN)
        row = queue.claim_batch(1)[0]
        entry = main._phase1_commit_thread(row, db, Threader(db), queue)
        assert entry is not None and entry.msg.attachments

        calls: list[int] = []
        ok, _ = main._phase2a_collect_chunks(entry, db, [], progress=lambda: calls.append(1))

        assert ok
        assert len(calls) == len(entry.msg.attachments)

    def test_phase2a_refreshes_heartbeat_during_extraction(self, tmp_path, monkeypatch):
        """#485: a long OCR refreshes the heartbeat page by page, but does
        not restart the stall guard's clock, which stays per attachment
        so a stuck extraction is still caught."""
        from src import attachment_indexing
        from src.extractors import extract as real_extract

        dest = tmp_path / "INBOX" / "new" / "att.eml"
        _write_eml_with_text_attachment(dest, "att@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(dest), REASON_INITIAL_SCAN)
        row = queue.claim_batch(1)[0]
        entry = main._phase1_commit_thread(row, db, Threader(db), queue)
        assert entry is not None and entry.msg.attachments

        def paged_extract(*, on_progress=None, **kwargs):
            assert on_progress is not None
            for _ in range(4):  # four pages read
                on_progress()
            return real_extract(**kwargs)

        touches: list[int] = []
        monkeypatch.setattr(attachment_indexing, "extract_attachment", paged_extract)
        monkeypatch.setattr(main, "touch_health_file", lambda: touches.append(1))
        guard: list[int] = []
        ok, _ = main._phase2a_collect_chunks(entry, db, [], progress=lambda: guard.append(1))

        assert ok
        assert len(touches) == 4 * len(entry.msg.attachments)
        assert len(guard) == len(entry.msg.attachments)

    def test_heartbeat_failure_during_extraction_does_not_fail_the_attachment(
        self, tmp_path, monkeypatch
    ):
        """A heartbeat write that fails inside an extractor would otherwise
        be caught by the dispatcher and cached as a ``failed`` extraction
        of a healthy payload. The file then goes stale, which the
        healthcheck reports."""
        dest = tmp_path / "INBOX" / "new" / "att.eml"
        _write_eml_with_text_attachment(dest, "att@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(dest), REASON_INITIAL_SCAN)
        row = queue.claim_batch(1)[0]
        entry = main._phase1_commit_thread(row, db, Threader(db), queue)
        assert entry is not None

        calls: list[int] = []

        def broken_touch():
            calls.append(1)
            raise OSError(28, "No space left on device")

        from src import attachment_indexing
        from src.extractors import extract as real_extract

        def paged_extract(*, on_progress=None, **kwargs):
            assert on_progress is not None
            on_progress()  # the extractor reports a page read
            return real_extract(**kwargs)

        monkeypatch.setattr(attachment_indexing, "extract_attachment", paged_extract)
        monkeypatch.setattr(main, "touch_health_file", broken_touch)
        all_texts: list[str] = []
        ok, _ = main._phase2a_collect_chunks(entry, db, all_texts)

        assert ok
        assert calls == [1]
        assert any("attachment text" in t for t in all_texts)


class TestHostPressureEscapesPhase2a:
    """#707 review round 1: the PDF extractor and the dispatcher re-raise
    ``MemoryError`` / ``RecursionError``, but Phase 2a's ``except
    Exception`` turned them into a ``chunk`` failure, spending the
    message's attempt as an ordinary error. They must escape the step
    with the ``begin_attempt`` charge still held, as ``IndexingQueue``
    documents for a process that dies mid-step."""

    @staticmethod
    def _write_eml_with_pdf(path: Path, message_id: str) -> None:
        import base64

        payload = base64.b64encode(b"%PDF-1.7 synthetic").decode("ascii")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"From: alice@example.com\r\n"
            f"To: bob@example.com\r\n"
            f"Subject: Synthetic PDF\r\n"
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
            f"Content-Type: application/pdf; name=doc.pdf\r\n"
            f"Content-Disposition: attachment; filename=doc.pdf\r\n"
            f"Content-Transfer-Encoding: base64\r\n"
            f"\r\n"
            f"{payload}\r\n"
            f"--frontier--\r\n",
            encoding="utf-8",
        )

    @pytest.mark.parametrize("error", [MemoryError, RecursionError])
    def test_host_pressure_on_a_pdf_page_escapes_the_drain_with_the_charge_held(
        self, tmp_path, monkeypatch, error
    ):
        from src.extractors import pdf

        calls: list[int] = []

        class Page:
            def __init__(self, index):
                self.index = index

            def extract_text(self):
                calls.append(self.index)
                if self.index == 1:
                    raise error("SYNTHETIC_PAGE_MARKER")
                return f"Synthetic page {self.index} text long enough to count as digital."

        class FakeReader:
            def __init__(self, stream):
                self.pages = [Page(0), Page(1), Page(2)]

        monkeypatch.setattr(pdf.pypdf, "PdfReader", FakeReader)

        dest = tmp_path / "INBOX" / "new" / "pdf.eml"
        self._write_eml_with_pdf(dest, "pdf@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(dest), REASON_INITIAL_SCAN)
        embedder = make_mock_embedder()

        with pytest.raises(error):
            _drain(queue, db, embedder, Threader(db), batch_size=1, max_passes=1)

        # Extraction stopped at the failing page, and nothing after it ran:
        # no extraction cached, no embed call.
        assert calls == [0, 1]
        assert db._conn.execute("SELECT COUNT(*) FROM attachment_extractions").fetchone()[0] == 0
        embedder.embed_batch.assert_not_called()
        # The row is still queued with the Phase 2a charge held and marked
        # interrupted, not recorded as a ``chunk`` failure.
        row = db._conn.execute(
            "SELECT status, attempts, last_stage FROM indexing_jobs WHERE filepath = ?",
            (str(dest),),
        ).fetchone()
        assert tuple(row) == ("queued", 1, "interrupted")


class TestReprocessKeepsThreadMembership:
    def test_reprocessed_reply_keeps_its_thread_and_chunks_consistent(self, tmp_path):
        """Regression (#204): reply B indexed before its parent A gets its
        own thread. When B was reprocessed later (renamed while the
        indexer was down, so its new path looked unindexed), threading
        re-resolved it into A's thread: the map moved but B's chunks kept
        thread B, and both thread rows listed B."""
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        queue = _make_queue(db)
        embedder = make_mock_embedder()
        embedder.embed.return_value = _UNIT_VECTOR

        reply = tmp_path / "INBOX" / "cur" / "b:2,S"
        _write_eml(reply, "b@example.com", subject="Budget reply", in_reply_to="a@example.com")
        queue.enqueue(str(reply), REASON_INITIAL_SCAN)
        _drain(queue, db, embedder, threader)
        parent = tmp_path / "INBOX" / "cur" / "a:2,S"
        _write_eml(parent, "a@example.com", subject="Quarterly plan")
        queue.enqueue(str(parent), REASON_INITIAL_SCAN)
        _drain(queue, db, embedder, threader)
        thread_b = db.find_thread_by_message_id("b@example.com")

        renamed = reply.with_name("b:2,RS")
        reply.rename(renamed)
        queue.enqueue(str(renamed), REASON_INITIAL_SCAN)  # the startup walk
        _drain(queue, db, embedder, threader)

        assert db.find_thread_by_message_id("b@example.com") == thread_b
        chunk_threads = {
            r["thread_id"]
            for r in db._conn.execute(
                "SELECT thread_id FROM message_chunks WHERE "
                "claimant_id IN (SELECT claimant_id FROM message_thread_map WHERE message_id = ?)",
                ("b@example.com",),
            )
        }
        assert chunk_threads == {thread_b}
        listing_b = [
            r["thread_id"]
            for r in db._conn.execute("SELECT thread_id, message_ids FROM threads")
            # ``message_ids`` lists claimant IDs: Message-ID plus "#<hash>".
            if any(c.startswith("b@example.com#") for c in json.loads(r["message_ids"]))
        ]
        assert listing_b == [thread_b]


def _message_dates(db: Database, message_id: str) -> tuple[str, tuple[str, str]]:
    """``(messages.sent_at, (date_first, date_last))``."""
    sent_at = db._conn.execute(
        "SELECT sent_at FROM messages WHERE message_id = ?", (message_id,)
    ).fetchone()["sent_at"]
    thread = db._conn.execute(
        "SELECT date_first, date_last FROM threads WHERE thread_id = ?",
        (db.find_thread_by_message_id(message_id),),
    ).fetchone()
    return sent_at, (thread["date_first"], thread["date_last"])


class TestReprocessKeepsFirstDate:
    """#297, first half: the parser falls back to the current time for a
    missing or unparseable Date header, so reprocessing an undated
    message (a rename seen while the indexer was down, a retry) used to
    re-date its ``messages`` row and widen its thread's range while the
    retained chunks kept the first date. The first persisted date wins
    for a fallback date; a real header date is still taken as parsed."""

    _FIRST = datetime(2026, 1, 1, tzinfo=UTC)
    _LATER = datetime(2026, 9, 30, tzinfo=UTC)

    def _index(self, db, threader, path):
        queue = _make_queue(db)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        _drain(queue, db, make_mock_embedder(_UNIT_VECTOR), threader)

    def test_dated_message_keeps_header_date_on_reprocess(self, tmp_path, monkeypatch):
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        path = tmp_path / "INBOX" / "cur" / "d:2,S"
        _write_eml(path, "dated@example.com")
        _set_parser_clock(monkeypatch, self._FIRST)
        self._index(db, threader, path)

        renamed = path.with_name("d:2,RS")
        path.rename(renamed)
        _set_parser_clock(monkeypatch, self._LATER)
        self._index(db, threader, renamed)

        header = "2024-01-01T12:00:00+00:00"
        assert _message_dates(db, "dated@example.com") == (header, (header, header))

    def test_changed_header_date_is_its_own_claimant(self, tmp_path, monkeypatch):
        """Only a fallback date defers to the stored one, and only for the
        same file: a file whose real Date header differs has different
        bytes, so it is another claimant of the Message-ID (#217) with its
        own header date, and the first keeps its own."""
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        path = tmp_path / "INBOX" / "cur" / "c:2,S"
        _write_eml(path, "changed@example.com")
        self._index(db, threader, path)

        rewritten = path.with_name("c:2,RS")
        path.unlink()
        _write_eml(rewritten, "changed@example.com", date="Tue, 02 Jan 2024 12:00:00 +0000")
        self._index(db, threader, rewritten)

        rows = db._conn.execute(
            "SELECT filepath, sent_at FROM messages WHERE message_id = ?",
            ("changed@example.com",),
        ).fetchall()
        assert {(r["filepath"], r["sent_at"]) for r in rows} == {
            (str(path), "2024-01-01T12:00:00+00:00"),
            (str(rewritten), "2024-01-02T12:00:00+00:00"),
        }

    @pytest.mark.parametrize("date", [None, "not-a-date"], ids=["missing", "malformed"])
    def test_undated_message_keeps_first_date_on_reprocess(self, tmp_path, monkeypatch, date):
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        path = tmp_path / "INBOX" / "cur" / "u:2,S"
        _write_eml(path, "undated@example.com", date=date)
        _set_parser_clock(monkeypatch, self._FIRST)
        self._index(db, threader, path)

        renamed = path.with_name("u:2,RS")
        path.rename(renamed)
        _set_parser_clock(monkeypatch, self._LATER)
        self._index(db, threader, renamed)

        first = self._FIRST.isoformat()
        assert _message_dates(db, "undated@example.com") == (first, (first, first))

    @pytest.mark.parametrize(
        ("received", "expected_span"),
        [(None, None), ("Fri, 05 Jan 2024 06:00:00 +0000", "2024-01-05T06:00:00+00:00")],
        ids=["undated", "delivered"],
    )
    def test_reap_rebuild_keeps_undated_survivor_first_date(
        self, tmp_path, monkeypatch, received, expected_span
    ):
        """The reaper re-parses a thread's survivors to rebuild its row;
        an undated survivor must not re-date the thread there either,
        and a delivered survivor's span is its ``occurred_at``."""
        from src.reconciler import Reconciler, ReconcilerConfig

        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        root = inbox / "1700000000.M1.host:2,S"
        reply = inbox / "1700000001.M1.host:2,S"
        _write_eml(root, "root@example.com", subject="Plan", date=None, received=received)
        _write_eml(reply, "reply@example.com", subject="Re: Plan", in_reply_to="root@example.com")
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        embedder = make_mock_embedder(_UNIT_VECTOR)
        _set_parser_clock(monkeypatch, self._FIRST)
        # One file at a time: the Maildir walk order is filesystem order.
        self._index(db, threader, root)
        self._index(db, threader, reply)
        assert db.find_thread_by_message_id("reply@example.com") == "root@example.com"

        reply.rename(inbox / "1700000001.M1.host:2,ST")
        reconciler = Reconciler(
            db,
            embedder,
            ReconcilerConfig(
                enabled=True,
                grace_days=0,
                sweep_interval_secs=60,
                max_batch_pct=1.0,
                force=False,
            ),
            maildir_root=maildir,
        )
        reconciler.sweep()
        _set_parser_clock(monkeypatch, self._LATER)
        reconciler.reap()

        assert db.find_thread_by_message_id("reply@example.com") is None
        first = self._FIRST.isoformat()
        span = expected_span or first
        assert _message_dates(db, "root@example.com") == (first, (span, span))

    @pytest.mark.parametrize("date", [None, "not-a-date"], ids=["missing", "malformed"])
    def test_undated_delivered_message_dates_agree_on_reprocess(self, tmp_path, monkeypatch, date):
        """#297 regression: an undated or malformed-date message with a
        top Received header, indexed and reprocessed under two clocks,
        keeps one ``sent_at`` and one ``occurred_at``; its thread span
        is its effective time, and its chunks carry no date of their
        own (#575), so every passage reads that same message row."""
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        path = tmp_path / "INBOX" / "cur" / "r:2,S"
        _write_eml(
            path,
            "delivered@example.com",
            date=date,
            received="Fri, 05 Jan 2024 06:00:00 +0000",
        )
        _set_parser_clock(monkeypatch, self._FIRST)
        self._index(db, threader, path)

        renamed = path.with_name("r:2,RS")
        path.rename(renamed)
        _set_parser_clock(monkeypatch, self._LATER)
        self._index(db, threader, renamed)

        occurred = "2024-01-05T06:00:00+00:00"
        row = db._conn.execute(
            "SELECT sent_at, occurred_at, effective_at FROM messages WHERE message_id = ?",
            ("delivered@example.com",),
        ).fetchone()
        assert (row["sent_at"], row["occurred_at"], row["effective_at"]) == (
            self._FIRST.isoformat(),
            occurred,
            occurred,
        )
        assert _message_dates(db, "delivered@example.com")[1] == (occurred, occurred)
        chunk_columns = {r["name"] for r in db._conn.execute("PRAGMA table_info(message_chunks)")}
        assert not {c for c in chunk_columns if "date" in c or c.endswith("_at")} - {"chunked_at"}
        passage_dates = {
            r[0]
            for r in db._conn.execute(
                "SELECT m.effective_at FROM message_chunks c "
                "JOIN messages m ON m.claimant_id = c.claimant_id"
            )
        }
        assert passage_dates == {occurred}


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
        assert _chunk_ids(db, "ok1@example.com")
        assert _chunk_ids(db, "ok2@example.com")

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
            assert not _chunk_ids(db, f"m{i}@example.com")
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
        assert not _chunk_ids(db, "m@example.com")
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
        assert not _chunk_ids(db, "m@example.com"), (
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
        assert _chunk_ids(db, "m@example.com")
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
        assert not _chunk_ids(db, "blank@example.com")
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
            if kwargs["claimant_id"].startswith("victim@example.com#"):
                raise RuntimeError("simulated db error for victim")
            return original(*args, **kwargs)

        monkeypatch.setattr(db, "replace_message_chunks", selective_fail)
        self._run(db, embedder, threader, queue, monkeypatch, maildir)

        # Survivors fully indexed
        assert db.is_indexed(str(inbox / "ok1.eml"))
        assert db.is_indexed(str(inbox / "ok2.eml"))
        assert _chunk_ids(db, "ok1@example.com")
        assert _chunk_ids(db, "ok2@example.com")
        # Victim never got chunks (Phase 2c rolled back its transaction)
        assert not _chunk_ids(db, "victim@example.com")

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
            if kwargs["claimant_id"].startswith("a@example.com#"):
                raise RuntimeError("simulated Phase 2c failure for A")
            return original(*args, **kwargs)

        monkeypatch.setattr(db, "replace_message_chunks", fail_for_a)
        self._run(db, embedder, threader, queue, monkeypatch, maildir)

        # A failed Phase 2c → marked failed, queue retains a row.
        assert not _chunk_ids(db, "a@example.com"), (
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
            assert _chunk_ids(db, mid)
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


STAMP_JSON = '{"completed_at": "2026-09-28T12:00:00Z", "sync_interval_secs": 60}'
STAMP = SyncStamp(completed_at="2026-09-28T12:00:00+00:00", sync_interval_secs=60)


class TestIngestionStateRecorder:
    """The recorder writes the last acknowledged mbsync sync and the
    indexer's own liveness into ``ingestion_state`` for
    ``get_mailbox_status``, at most once per interval. A sync is
    acknowledged only once every message it delivered is queued."""

    def _state(self, db):
        return db._conn.execute(
            "SELECT sync_completed_at, sync_interval_secs, indexer_seen_at FROM ingestion_state"
        ).fetchone()

    def test_stamp_on_disk_is_not_recorded_until_acknowledged(self, tmp_path, db):
        """The stamp can be on disk while the watcher is still behind on
        the deliveries that preceded it."""
        (tmp_path / main.SYNC_STAMP_NAME).write_text(STAMP_JSON)
        main._IngestionStateRecorder(db, tmp_path).maybe_record(now=100.0)

        row = self._state(db)
        assert row["sync_completed_at"] is None
        assert row["indexer_seen_at"]

    def test_records_the_acknowledged_stamp(self, tmp_path, db):
        recorder = main._IngestionStateRecorder(db, tmp_path)
        recorder.acknowledge(STAMP)
        recorder.maybe_record(now=100.0)

        row = self._state(db)
        assert row["sync_completed_at"] == STAMP.completed_at
        assert row["sync_interval_secs"] == 60

    def test_acknowledgement_never_moves_backwards(self, tmp_path, db):
        """While the acknowledged stamp is not ahead of the clock."""
        recorder = main._IngestionStateRecorder(db, tmp_path)
        recorder.acknowledge(STAMP)
        recorder.acknowledge(SyncStamp("2026-09-28T11:00:00+00:00", 60))
        recorder.acknowledge(None)
        recorder.maybe_record(now=100.0)

        assert self._state(db)["sync_completed_at"] == STAMP.completed_at

    def test_a_future_acknowledged_stamp_yields_to_a_new_sync(self, tmp_path, db):
        """After a clock rollback the acknowledged stamp is ahead of the
        clock and every later sync sorts earlier; without yielding, the
        status stays not current until the clock catches up (#332)."""
        recorder = main._IngestionStateRecorder(db, tmp_path)
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        recorder.acknowledge(SyncStamp(future, 60))
        recorder.acknowledge(STAMP)
        recorder.maybe_record(now=100.0)

        assert self._state(db)["sync_completed_at"] == STAMP.completed_at

    def test_writes_at_most_once_per_interval(self, tmp_path, db):
        recorder = main._IngestionStateRecorder(db, tmp_path, interval_secs=30)
        recorder.maybe_record(now=100.0)
        recorder.acknowledge(STAMP)
        recorder.maybe_record(now=129.0)
        assert self._state(db)["sync_completed_at"] is None

        recorder.maybe_record(now=130.0)
        assert self._state(db)["sync_completed_at"] == STAMP.completed_at

    def test_read_stamp_logs_a_malformed_stamp(self, tmp_path, db, caplog):
        (tmp_path / main.SYNC_STAMP_NAME).write_text("garbage")

        assert main._IngestionStateRecorder(db, tmp_path).read_stamp() is None
        assert "sync stamp" in caplog.text

    def test_write_failure_is_logged_and_retried_next_call(self, tmp_path, db, caplog, monkeypatch):
        recorder = main._IngestionStateRecorder(db, tmp_path, interval_secs=30)

        def boom(**kw):
            raise sqlite3.OperationalError("database is locked")

        with monkeypatch.context() as m:
            m.setattr(db, "record_ingestion_state", boom)
            recorder.maybe_record(now=100.0)
        assert "ingestion state" in caplog.text

        recorder.maybe_record(now=101.0)
        assert self._state(db) is not None

    def test_health_heartbeat_records_liveness(self, tmp_path, db, monkeypatch):
        """Every health heartbeat — per message, per embed batch — also
        refreshes liveness, so a long drain pass never reads as a
        stopped indexer."""
        monkeypatch.setattr(main, "INDEXER_HEALTH_FILE", tmp_path / "healthy")
        monkeypatch.setattr(main, "_ingestion_state", main._IngestionStateRecorder(db, tmp_path))
        main.touch_health_file()

        assert self._state(db)["indexer_seen_at"]

    def test_stamp_rename_acknowledges_the_sync_named_in_the_event(self, tmp_path, db, monkeypatch):
        """By the time the watcher handles the stamp's rename, every
        earlier delivery event has been handled (watchdog dispatches in
        order). The file may already hold a later sync whose deliveries
        are still behind in the event queue, so the event's own sync —
        carried in the temporary file's name — is what gets acknowledged."""
        from watchdog.events import FileMovedEvent

        monkeypatch.setattr(main, "MAILDIR_PATH", tmp_path)
        recorder = main._IngestionStateRecorder(db, tmp_path)
        handler = main.MaildirHandler(db, _make_queue(db), ingestion_state=recorder)
        (tmp_path / main.SYNC_STAMP_NAME).write_text(
            '{"completed_at": "2026-09-28T12:05:00Z", "sync_interval_secs": 60}'
        )
        handler.on_moved(
            FileMovedEvent(
                str(tmp_path / ".mbsync-last-sync.2026-09-28T12:00:00Z.60.tmp"),
                str(tmp_path / main.SYNC_STAMP_NAME),
            )
        )
        recorder.maybe_record(now=100.0)

        assert self._state(db)["sync_completed_at"] == STAMP.completed_at

    def test_unrecognized_rename_onto_the_stamp_is_not_acknowledged(
        self, tmp_path, db, caplog, monkeypatch
    ):
        from watchdog.events import FileMovedEvent

        monkeypatch.setattr(main, "MAILDIR_PATH", tmp_path)
        recorder = main._IngestionStateRecorder(db, tmp_path)
        handler = main.MaildirHandler(db, _make_queue(db), ingestion_state=recorder)
        handler.on_moved(
            FileMovedEvent(str(tmp_path / "other.tmp"), str(tmp_path / main.SYNC_STAMP_NAME))
        )
        recorder.maybe_record(now=100.0)

        assert self._state(db)["sync_completed_at"] is None
        assert "sync stamp" in caplog.text

    def test_initial_index_acknowledges_the_stamp_read_before_its_walk(
        self, tmp_path, db, monkeypatch
    ):
        """A sync that completes during the walk may have delivered
        files the walk already passed, so only the stamp read before the
        walk is acknowledged."""
        maildir = tmp_path / "maildir"
        maildir.mkdir()
        (maildir / main.SYNC_STAMP_NAME).write_text(STAMP_JSON)
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        monkeypatch.setattr(main, "touch_health_file", lambda: None)

        def walk(*args, **kwargs):
            (maildir / main.SYNC_STAMP_NAME).write_text(
                '{"completed_at": "2026-09-28T12:05:00Z", "sync_interval_secs": 60}'
            )
            return 0

        monkeypatch.setattr(main, "_enqueue_unindexed_messages", walk)
        recorder = main._IngestionStateRecorder(db, maildir)
        main.initial_index(
            db, make_mock_embedder(), Threader(db), _make_queue(db), ingestion_state=recorder
        )
        recorder.maybe_record(now=100.0)

        assert self._state(db)["sync_completed_at"] == STAMP.completed_at


class TestRequeueStaleExtractions:
    """Bumping an extractor's version re-queues the messages whose cached
    extraction came from an older version, once, so a fixed extractor
    also repairs mail indexed before the fix (#226, #228)."""

    @staticmethod
    def _write_docx_eml(
        path: Path, message_id: str, body: str = "See the attached contract."
    ) -> None:
        import io
        from email.message import EmailMessage

        import docx
        from docx.shared import Inches

        document = docx.Document()
        document.add_paragraph("body paragraph")
        header = document.sections[0].header
        header.add_table(rows=1, cols=1, width=Inches(2)).cell(0, 0).text = "HEADER_MARK"
        buf = io.BytesIO()
        document.save(buf)

        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com"
        msg["Subject"] = "Contract"
        msg["Message-ID"] = f"<{message_id}>"
        msg["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"
        msg.set_content(body)
        msg.add_attachment(
            buf.getvalue(),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="contract.docx",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(msg))

    def _drain(self, db, queue):
        embedder = make_mock_embedder(_UNIT_VECTOR)
        return main._drain_queue_batched(
            db,
            embedder,
            Threader(db),
            queue,
            batch_size=10,
            timing_aggregator=main.TimingAggregator(window=4),
            max_passes=1,
        )

    @staticmethod
    def _thread_vector(db, thread_id: str) -> list[float]:
        import struct

        blob = db._conn.execute(
            "SELECT embedding FROM threads_vec WHERE thread_id = ?", (thread_id,)
        ).fetchone()["embedding"]
        return list(struct.unpack(f"{len(blob) // 4}f", blob))

    def _attachment_chunk_text(self, db) -> str:
        rows = db._conn.execute(
            "SELECT text FROM message_chunks WHERE attachment_id IS NOT NULL"
        ).fetchall()
        return " ".join(r["text"] for r in rows)

    def test_old_version_rows_are_re_extracted_once(self, tmp_path, monkeypatch):
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        path = maildir / "INBOX" / "cur" / "contract.eml"
        self._write_docx_eml(path, "contract@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        # Index with the pre-fix walker's output: unversioned and
        # missing the header table.
        from src import attachment_indexing
        from src.extractors import STATUS_SUCCESS, ExtractionResult

        with monkeypatch.context() as m:
            m.setattr(
                attachment_indexing,
                "extract_attachment",
                lambda **_kw: ExtractionResult(
                    status=STATUS_SUCCESS, extractor="docx", text="body paragraph", error=None
                ),
            )
            self._drain(db, queue)
        assert "HEADER_MARK" not in self._attachment_chunk_text(db)

        assert main._requeue_stale_extractions(db, queue) == 1
        self._drain(db, queue)

        assert "HEADER_MARK" in self._attachment_chunk_text(db)
        row = db._conn.execute("SELECT extractor FROM attachment_extractions").fetchone()
        assert row["extractor"] == "docx@3"
        assert main._requeue_stale_extractions(db, queue) == 0

    def test_alias_messages_are_requeued_and_rebuilt(self, tmp_path, monkeypatch):
        """A message carrying the same bytes as ``.bin`` shares the stale
        cache row and indexed its old text, so it is rebuilt too."""
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        docx_path = maildir / "INBOX" / "cur" / "contract.eml"
        self._write_docx_eml(docx_path, "contract@example.com")
        blob_path = maildir / "INBOX" / "cur" / "blob.eml"
        raw = docx_path.read_bytes()
        raw = raw.replace(b"contract@example.com", b"blob@example.com")
        raw = raw.replace(
            b"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"application/octet-stream",
        ).replace(b"contract.docx", b"blob.bin")
        blob_path.write_bytes(raw)
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(docx_path), REASON_INITIAL_SCAN)
        queue.enqueue(str(blob_path), REASON_INITIAL_SCAN)
        from src import attachment_indexing
        from src.extractors import STATUS_SUCCESS, ExtractionResult

        with monkeypatch.context() as m:
            m.setattr(
                attachment_indexing,
                "extract_attachment",
                lambda **_kw: ExtractionResult(
                    status=STATUS_SUCCESS, extractor="docx", text="body paragraph", error=None
                ),
            )
            self._drain(db, queue)

        assert main._requeue_stale_extractions(db, queue) == 2
        self._drain(db, queue)

        for mid in ("contract@example.com", "blob@example.com"):
            rows = db._conn.execute(
                "SELECT text FROM message_chunks WHERE "
                "claimant_id IN (SELECT claimant_id FROM message_thread_map WHERE message_id = ?) "
                "AND attachment_id IS NOT NULL",
                (mid,),
            ).fetchall()
            assert "HEADER_MARK" in " ".join(r["text"] for r in rows), mid
        assert main._requeue_stale_extractions(db, queue) == 0

    def test_nothing_is_requeued_when_extraction_is_disabled(self, tmp_path, monkeypatch):
        # The drain skips attachments then, so the rows would never be
        # re-stamped and every restart would re-queue the same messages.
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        path = maildir / "INBOX" / "cur" / "contract.eml"
        self._write_docx_eml(path, "contract@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        self._drain(db, queue)
        with db.transaction():
            db._conn.execute("UPDATE attachment_extractions SET extractor = 'docx'")

        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", False)
        assert main._requeue_stale_extractions(db, queue) == 0

    def test_unversioned_xlsx_rows_are_requeued(self, tmp_path, monkeypatch):
        # Rows written before ``xlsx`` was versioned (#294) are stale.
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        path = maildir / "INBOX" / "cur" / "contract.eml"
        self._write_docx_eml(path, "contract@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        self._drain(db, queue)
        with db.transaction():
            db._conn.execute("UPDATE attachment_extractions SET extractor = 'xlsx'")
        assert main._requeue_stale_extractions(db, queue) == 1

        with db.transaction():
            db._conn.execute("UPDATE attachment_extractions SET extractor = 'xlsx@4'")
        self._drain(db, queue)
        assert main._requeue_stale_extractions(db, queue) == 0

    def test_ocr_rows_are_not_requeued_while_ocr_is_off(self, tmp_path, monkeypatch):
        # Review round 1 on #262: the refresh would hit the OCR-disabled
        # gate and replace the indexed OCR text with nothing, and every
        # restart would re-queue the same messages.
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        path = maildir / "INBOX" / "cur" / "contract.eml"
        self._write_docx_eml(path, "contract@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        self._drain(db, queue)
        with db.transaction():
            db._conn.execute("UPDATE attachment_extractions SET extractor = 'image-ocr'")

        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", False)
        assert main._requeue_stale_extractions(db, queue) == 0
        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", True)
        assert main._requeue_stale_extractions(db, queue) == 1

    def test_thread_vector_is_replaced_when_the_last_chunks_are_cleared(
        self, tmp_path, monkeypatch
    ):
        """An attachment-only thread whose stale extraction now yields no
        text loses its last chunks; the thread vector, seeded from those
        chunks, must be replaced by the subject fallback rather than
        keep matching the removed text."""
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        path = maildir / "INBOX" / "cur" / "contract.eml"
        self._write_docx_eml(path, "contract@example.com", body="")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        from src import attachment_indexing
        from src.extractors import STATUS_EMPTY, STATUS_SUCCESS, ExtractionResult

        with monkeypatch.context() as m:
            m.setattr(
                attachment_indexing,
                "extract_attachment",
                lambda **_kw: ExtractionResult(
                    status=STATUS_SUCCESS, extractor="docx", text="body paragraph", error=None
                ),
            )
            self._drain(db, queue)
        thread_id = db._conn.execute(
            "SELECT thread_id FROM message_thread_map WHERE message_id = 'contract@example.com'"
        ).fetchone()["thread_id"]
        assert self._thread_vector(db, thread_id) == _UNIT_VECTOR

        fallback_vector = [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2)
        embedder = make_mock_embedder()
        embedder.embed.side_effect = lambda text: (
            fallback_vector if text == "Contract" else _UNIT_VECTOR
        )
        assert main._requeue_stale_extractions(db, queue) == 1
        with monkeypatch.context() as m:
            m.setattr(
                attachment_indexing,
                "extract_attachment",
                lambda **_kw: ExtractionResult(
                    status=STATUS_EMPTY, extractor="docx@3", text=None, error=None
                ),
            )
            main._drain_queue_batched(
                db,
                embedder,
                Threader(db),
                queue,
                batch_size=10,
                timing_aggregator=main.TimingAggregator(window=4),
                max_passes=1,
            )

        assert not _chunk_ids(db, "contract@example.com", attachment_id=None)
        assert not db.thread_has_chunks(thread_id)
        assert self._thread_vector(db, thread_id) == fallback_vector

    def test_pending_and_dead_rows_are_left_alone(self, tmp_path, monkeypatch):
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        path = maildir / "INBOX" / "cur" / "contract.eml"
        self._write_docx_eml(path, "contract@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        self._drain(db, queue)
        with db.transaction():
            db._conn.execute("UPDATE attachment_extractions SET extractor = 'docx'")

        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        assert main._requeue_stale_extractions(db, queue) == 0
        for _ in range(queue.max_attempts):
            queue.mark_failed(str(path), stage="embed", error="x")
        assert queue.is_dead(str(path))
        assert main._requeue_stale_extractions(db, queue) == 0


class TestRequeueOcrDisabledExtractions:
    """#300: attachments skipped while OCR was off are cached "OCR
    disabled" (images, and PDFs with no digital text layer). Turning OCR
    on must re-queue the messages carrying them once, or the skipped
    attachments are never read."""

    @staticmethod
    def _write_eml(path: Path, message_id: str, payload: bytes, ctype: str, filename: str):
        from email.message import EmailMessage

        maintype, subtype = ctype.split("/")
        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com"
        msg["Subject"] = "Scan"
        msg["Message-ID"] = f"<{message_id}>"
        msg["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"
        msg.set_content("See the attached scan.")
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(msg))

    @staticmethod
    def _png() -> bytes:
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (8, 8), color="white").save(buf, "PNG")
        return buf.getvalue()

    @staticmethod
    def _scanned_pdf() -> bytes:
        import io

        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    def _drain(self, db, queue, **kwargs):
        return main._drain_queue_batched(
            db,
            make_mock_embedder(_UNIT_VECTOR),
            Threader(db),
            queue,
            batch_size=10,
            timing_aggregator=main.TimingAggregator(window=4),
            max_passes=1,
            **kwargs,
        )

    def _index_with_ocr_off(self, tmp_path, monkeypatch, messages, **drain_kwargs):
        """Index ``messages`` (name -> (payload, MIME type, filename)) with
        OCR off; return the db, queue and paths."""
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        paths = {}
        for name, (payload, ctype, filename) in messages.items():
            path = maildir / "INBOX" / "cur" / f"{name}.eml"
            self._write_eml(path, f"{name}@example.com", payload, ctype, filename)
            queue.enqueue(str(path), REASON_INITIAL_SCAN)
            paths[name] = str(path)
        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", False)
        self._drain(db, queue, **drain_kwargs)
        return db, queue, paths

    @staticmethod
    def _queued(db) -> dict[str, str]:
        rows = db._conn.execute(
            "SELECT filepath, reason FROM indexing_jobs WHERE status = 'queued'"
        ).fetchall()
        return {r["filepath"]: r["reason"] for r in rows}

    def test_skipped_images_and_scanned_pdfs_are_requeued_once(self, tmp_path, monkeypatch):
        from src import attachment_indexing
        from src.extractors import (
            OCR_DISABLED_ERROR,
            SCANNED_PDF_OCR_DISABLED_ERROR,
            STATUS_SUCCESS,
            ExtractionResult,
        )

        db, queue, paths = self._index_with_ocr_off(
            tmp_path,
            monkeypatch,
            {
                "photo": (self._png(), "image/png", "photo.png"),
                "scan": (self._scanned_pdf(), "application/pdf", "scan.pdf"),
            },
        )
        rows = db.find_ocr_disabled_attachments()
        assert sorted((r["filepath"], r["extraction_error"]) for r in rows) == sorted(
            [
                (paths["photo"], OCR_DISABLED_ERROR),
                (paths["scan"], SCANNED_PDF_OCR_DISABLED_ERROR),
            ]
        )
        assert self._queued(db) == {}

        # Still off: nothing to re-run.
        assert main._requeue_stale_extractions(db, queue) == 0
        assert self._queued(db) == {}

        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", True)
        assert main._requeue_stale_extractions(db, queue) == 2
        assert self._queued(db) == {
            paths["photo"]: REASON_REEXTRACT,
            paths["scan"]: REASON_REEXTRACT,
        }

        extractor = MagicMock(
            return_value=ExtractionResult(
                status=STATUS_SUCCESS, extractor="image-ocr@3", text="scanned words", error=None
            )
        )
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        self._drain(db, queue)
        assert extractor.call_count == 2
        assert db.find_ocr_disabled_attachments() == []

        # The next startup finds nothing left to re-run.
        assert main._requeue_stale_extractions(db, queue) == 0
        assert self._queued(db) == {}

    _MESSAGES = {
        "photo": ("png", "image/png", "SYNTHETIC_FILENAME_MARKER.png"),
        "scan": ("pdf", "application/pdf", "SYNTHETIC_FILENAME_MARKER.pdf"),
    }

    def _marker_messages(self):
        payloads = {"png": self._png(), "pdf": self._scanned_pdf()}
        return {
            name: (payloads[kind], ctype, filename)
            for name, (kind, ctype, filename) in self._MESSAGES.items()
        }

    @staticmethod
    def _aggregate_lines(caplog):
        return [
            (r.levelname, r.getMessage())
            for r in caplog.records
            if r.getMessage().startswith("attachments n=")
        ]

    def test_initial_index_cadence_logs_the_attachment_aggregate(
        self, tmp_path, monkeypatch, caplog
    ):
        """#871: during the initial index (``summary_every``), each timing
        summary is followed by the attachments line. Attachments skipped
        while OCR is off are missing from search, so the line is a
        WARNING (review round 1). Counts only: no filename."""
        from src import attachment_indexing

        caplog.set_level("INFO")
        attachment_indexing.attachment_outcomes.drain()
        self._index_with_ocr_off(tmp_path, monkeypatch, self._marker_messages(), summary_every=1)
        assert self._aggregate_lines(caplog) == [
            (
                "WARNING",
                "attachments n=2 success=0 failed=0 unsupported=0 too_large=0 "
                "ocr_disabled=2 empty=0 cached=0 pdf_pages_failed=0 "
                "pdf_pages_unrecovered=0 ocr_capped_pdfs=0 ocr_pages_skipped=0 parser_caps_messages=0 "
                "warnings_suppressed=0",
            )
        ]
        assert "SYNTHETIC_FILENAME_MARKER" not in caplog.text
        # Drained by the line: the next summary starts from zero.
        assert attachment_indexing.attachment_outcomes.drain()["ocr_disabled"] == 0

    def test_steady_state_drain_leaves_the_summary_to_its_caller(
        self, tmp_path, monkeypatch, caplog
    ):
        """Review round 1: without ``summary_every`` (the steady-state
        loop) the drain itself logs no summary; the loop flushes it
        (``_steady_state_summary_due``)."""
        from src import attachment_indexing

        caplog.set_level("INFO")
        attachment_indexing.attachment_outcomes.drain()
        self._index_with_ocr_off(tmp_path, monkeypatch, self._marker_messages())
        assert self._aggregate_lines(caplog) == []
        main._log_attachment_outcomes()
        assert [level for level, _ in self._aggregate_lines(caplog)] == ["WARNING"]

    def test_occurrence_that_would_not_rerun_is_not_requeued(self, tmp_path, monkeypatch):
        """Bytes cached "OCR disabled" from an image, carried only as
        ``.bin`` by a live message: reprocessing that message would serve
        the row again (no extractor for ``.bin``), so re-queueing it would
        repeat on every startup."""
        db, queue, paths = self._index_with_ocr_off(
            tmp_path,
            monkeypatch,
            {
                "photo": (self._png(), "image/png", "photo.png"),
                "blob": (self._png(), "application/octet-stream", "blob.bin"),
            },
        )
        assert len(db.find_ocr_disabled_attachments()) == 2
        queue.enqueue(paths["photo"], REASON_INITIAL_SCAN)
        for _ in range(queue.max_attempts):
            queue.mark_failed(paths["photo"], stage="embed", error="x")
        assert queue.is_dead(paths["photo"])

        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", True)
        assert main._requeue_stale_extractions(db, queue) == 0
        assert main._requeue_stale_extractions(db, queue) == 0
        assert self._queued(db) == {}

    def test_pending_rows_and_disabled_extraction_are_left_alone(self, tmp_path, monkeypatch):
        db, queue, paths = self._index_with_ocr_off(
            tmp_path, monkeypatch, {"photo": (self._png(), "image/png", "photo.png")}
        )
        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", True)
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", False)
        assert main._requeue_stale_extractions(db, queue) == 0
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
        queue.enqueue(paths["photo"], REASON_INITIAL_SCAN)
        assert main._requeue_stale_extractions(db, queue) == 0
        assert self._queued(db) == {paths["photo"]: REASON_INITIAL_SCAN}


class TestRequeueNewlyDispatchedExtensions:
    """#691 review round 1: an attachment whose only routing hint is its
    filename extension (a ``.heic`` sent as ``application/octet-stream``)
    was cached ``unsupported`` with no extractor before that extension
    dispatched. The row carries no extractor version, so the ``image``
    bump cannot mark it stale; the startup sweep must re-queue messages
    whose occurrence now selects an extractor, once."""

    _write_eml = staticmethod(TestRequeueOcrDisabledExtractions._write_eml)
    _drain = TestRequeueOcrDisabledExtractions._drain
    _queued = staticmethod(TestRequeueOcrDisabledExtractions._queued)

    def _index_before_heic_dispatch(self, tmp_path, monkeypatch, messages):
        from src import extractors

        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        paths = {}
        for name, (payload, ctype, filename) in messages.items():
            path = maildir / "INBOX" / "cur" / f"{name}.eml"
            self._write_eml(path, f"{name}@example.com", payload, ctype, filename)
            queue.enqueue(str(path), REASON_INITIAL_SCAN)
            paths[name] = str(path)
        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", True)
        monkeypatch.delitem(extractors._EXT_DISPATCH, ".heic")
        self._drain(db, queue)
        # The upgrade: ``.heic`` dispatches again.
        monkeypatch.setitem(extractors._EXT_DISPATCH, ".heic", "image")
        return db, queue, paths

    def test_occurrence_whose_extension_now_dispatches_is_requeued_once(
        self, tmp_path, monkeypatch
    ):
        from src import attachment_indexing
        from src.extractors import NO_EXTRACTOR_ERROR, STATUS_SUCCESS, ExtractionResult

        db, queue, paths = self._index_before_heic_dispatch(
            tmp_path,
            monkeypatch,
            {
                "photo": (b"synthetic heic bytes", "application/octet-stream", "IMG_0001.HEIC"),
                "blob": (b"synthetic other bytes", "application/octet-stream", "blob.bin"),
            },
        )
        rows = db._conn.execute(
            "SELECT extraction_status, extractor, extraction_error FROM attachment_extractions"
        ).fetchall()
        assert [tuple(r) for r in rows] == [("unsupported", None, NO_EXTRACTOR_ERROR)] * 2
        assert self._queued(db) == {}

        assert main._requeue_stale_extractions(db, queue) == 1
        assert self._queued(db) == {paths["photo"]: REASON_REEXTRACT}

        extractor = MagicMock(
            return_value=ExtractionResult(
                status=STATUS_SUCCESS, extractor="image-ocr@3", text="photo words", error=None
            )
        )
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        self._drain(db, queue)
        assert extractor.call_count == 1

        # The row is rewritten, so the next startup finds nothing; the
        # ``.bin`` occurrence still selects no extractor and is never queued.
        assert main._requeue_stale_extractions(db, queue) == 0
        assert self._queued(db) == {}

    def test_requeued_whatever_the_ocr_setting(self, tmp_path, monkeypatch):
        """Selecting an extractor does not depend on OCR: with OCR off the
        re-run records "OCR disabled", which the OCR sweep picks up later."""
        db, queue, paths = self._index_before_heic_dispatch(
            tmp_path,
            monkeypatch,
            {"photo": (b"synthetic heic bytes", "application/octet-stream", "IMG_0001.heic")},
        )
        monkeypatch.setattr(main, "INDEXER_OCR_ENABLED", False)
        assert main._requeue_stale_extractions(db, queue) == 1
        assert self._queued(db) == {paths["photo"]: REASON_REEXTRACT}

    def test_pending_dead_and_disabled_extraction_are_left_alone(self, tmp_path, monkeypatch):
        db, queue, paths = self._index_before_heic_dispatch(
            tmp_path,
            monkeypatch,
            {"photo": (b"synthetic heic bytes", "application/octet-stream", "IMG_0001.heic")},
        )
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", False)
        assert main._requeue_stale_extractions(db, queue) == 0
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
        queue.enqueue(paths["photo"], REASON_INITIAL_SCAN)
        for _ in range(queue.max_attempts):
            queue.mark_failed(paths["photo"], stage="embed", error="x")
        assert queue.is_dead(paths["photo"])
        assert main._requeue_stale_extractions(db, queue) == 0
        assert self._queued(db) == {}


class TestRequeueTooLargeThatNowFits:
    """#693: attachments cached ``too_large`` under a smaller
    ``INDEXER_ATTACHMENT_MAX_BYTES`` must be read once the operator raises
    the cap. The startup sweep re-queues, once, every message carrying
    bytes whose size now fits; bytes still over the cap stay ``too_large``
    and are never re-queued."""

    _write_eml = staticmethod(TestRequeueOcrDisabledExtractions._write_eml)
    _drain = TestRequeueOcrDisabledExtractions._drain
    _queued = staticmethod(TestRequeueOcrDisabledExtractions._queued)

    def _index_under_cap(self, tmp_path, monkeypatch, messages, cap):
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        paths = {}
        for name, payload in messages.items():
            path = maildir / "INBOX" / "cur" / f"{name}.eml"
            self._write_eml(path, f"{name}@example.com", payload, "text/plain", f"{name}.txt")
            queue.enqueue(str(path), REASON_INITIAL_SCAN)
            paths[name] = str(path)
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_MAX_BYTES", cap)
        self._drain(db, queue)
        return db, queue, paths

    @staticmethod
    def _statuses(db) -> list[str]:
        rows = db._conn.execute(
            "SELECT extraction_status FROM attachment_extractions ORDER BY extraction_status"
        ).fetchall()
        return [r["extraction_status"] for r in rows]

    def test_newly_fitting_rows_are_requeued_once(self, tmp_path, monkeypatch):
        from src import attachment_indexing

        db, queue, paths = self._index_under_cap(
            tmp_path,
            monkeypatch,
            {
                "fits": b"fitting words " * 300,  # 4,200 bytes
                "still_big": b"oversized words " * 600,  # 9,600 bytes
                "dead": b"dead letter words " * 200,  # 3,600 bytes
            },
            cap=1_000,
        )
        assert self._statuses(db) == ["too_large"] * 3
        queue.enqueue(paths["dead"], REASON_INITIAL_SCAN)
        for _ in range(queue.max_attempts):
            queue.mark_failed(paths["dead"], stage="embed", error="x")
        assert queue.is_dead(paths["dead"])

        # Same cap: nothing fits yet.
        assert main._requeue_stale_extractions(db, queue) == 0
        assert self._queued(db) == {}

        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_MAX_BYTES", 5_000)
        assert main._requeue_stale_extractions(db, queue) == 1
        assert self._queued(db) == {paths["fits"]: REASON_REEXTRACT}

        extractor = MagicMock(side_effect=attachment_indexing.extract_attachment)
        monkeypatch.setattr(attachment_indexing, "extract_attachment", extractor)
        self._drain(db, queue)
        assert extractor.call_count == 1
        assert self._statuses(db) == ["success", "too_large", "too_large"]

        # The fitting row was rewritten and the oversized one still does
        # not fit, so later startups re-queue nothing.
        assert main._requeue_stale_extractions(db, queue) == 0
        assert main._requeue_stale_extractions(db, queue) == 0
        assert self._queued(db) == {}

    def test_every_message_carrying_the_bytes_is_requeued(self, tmp_path, monkeypatch):
        """The row is shared by content hash, so each message carrying the
        bytes indexed no text for them and is rebuilt."""
        payload = b"shared words " * 300
        db, queue, paths = self._index_under_cap(
            tmp_path, monkeypatch, {"first": payload, "second": payload}, cap=1_000
        )
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_MAX_BYTES", len(payload))
        assert main._requeue_stale_extractions(db, queue) == 2
        assert self._queued(db) == {
            paths["first"]: REASON_REEXTRACT,
            paths["second"]: REASON_REEXTRACT,
        }

    def test_pending_rows_and_disabled_extraction_are_left_alone(self, tmp_path, monkeypatch):
        db, queue, paths = self._index_under_cap(
            tmp_path, monkeypatch, {"fits": b"fitting words " * 300}, cap=1_000
        )
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_MAX_BYTES", 5_000)
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", False)
        assert main._requeue_stale_extractions(db, queue) == 0
        monkeypatch.setattr(main, "INDEXER_ATTACHMENT_EXTRACTION_ENABLED", True)
        queue.enqueue(paths["fits"], REASON_INITIAL_SCAN)
        assert main._requeue_stale_extractions(db, queue) == 0
        assert self._queued(db) == {paths["fits"]: REASON_INITIAL_SCAN}


class TestAttachmentMaxBytesDefault:
    """#693: the default cap is 32 MiB, the same in the code, Compose and
    ``.env.example``, and an attachment that size fits in an ``.eml``
    under the default ``INDEXER_PARSE_MAX_BYTES`` and the parser's decode
    budget, so it can reach the extractor at all."""

    _DEFAULT = 32 * 1024 * 1024
    _REPO = Path(__file__).resolve().parents[2]

    def test_code_default(self):
        assert main._DEFAULT_ATTACHMENT_MAX_BYTES == self._DEFAULT
        if "INDEXER_ATTACHMENT_MAX_BYTES" not in os.environ:
            assert main.INDEXER_ATTACHMENT_MAX_BYTES == self._DEFAULT

    def test_compose_and_env_example_match(self):
        compose = (self._REPO / "docker-compose.yml").read_text()
        assert f"${{INDEXER_ATTACHMENT_MAX_BYTES:-{self._DEFAULT}}}" in compose
        env_example = (self._REPO / ".env.example").read_text()
        assert f"\nINDEXER_ATTACHMENT_MAX_BYTES={self._DEFAULT}\n" in env_example

    def test_fits_under_the_parse_caps(self):
        # base64 turns each 57 bytes into a 76-character line plus CRLF.
        encoded = -(-self._DEFAULT // 57) * 78
        assert encoded < parser._DEFAULT_PARSE_MAX_BYTES
        assert self._DEFAULT <= parser.MAX_DECODED_ATTACHMENT_BYTES


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

    def _claim_order(self, db, queue) -> list[str]:
        return [Path(r["filepath"]).name for r in queue.claim_batch(100)]

    def _three_folders(self, maildir):
        """One message per folder, written so walk order differs from
        date order; Sent mail has no ``Received:`` header."""
        _write_eml(
            maildir / "Archive" / "cur" / "old",
            "old@example.com",
            received="Mon, 01 Jan 2018 09:00:00 +0000",
        )
        _write_eml(
            maildir / "Sent" / "cur" / "mid",
            "mid@example.com",
            date="Wed, 01 Jan 2020 09:00:00 +0000",
        )
        _write_eml(
            maildir / "INBOX" / "cur" / "new",
            "new@example.com",
            received="Thu, 01 Jan 2026 09:00:00 +0000",
        )
        (maildir / "INBOX" / "cur" / "undated").write_bytes(
            b"Message-ID: <u@example.com>\r\n\r\nx\r\n"
        )

    def test_oldest_first_queues_by_message_time_across_folders(self, tmp_path, monkeypatch):
        """#699/#752: the queue hands out the oldest message first,
        whatever folder it is in (walk order here is Archive, INBOX,
        Sent); undated last."""
        maildir, _inbox, db, queue = self._setup(tmp_path, monkeypatch)
        self._three_folders(maildir)
        main._enqueue_unindexed_messages(
            db, queue, maildir, main.REASON_INITIAL_SCAN, oldest_first=True
        )
        assert self._claim_order(db, queue) == ["old", "mid", "new", "undated"]

    def test_mail_delivered_during_the_scan_is_queued_after_the_backlog(
        self, tmp_path, monkeypatch
    ):
        """Codex round 1 on #754: the watcher runs during the initial
        scan, so mail delivered while it reads headers is queued at once.
        The backlog is due at each message's own time, so it is still
        handed out first."""
        maildir, _inbox, db, queue = self._setup(tmp_path, monkeypatch)
        self._three_folders(maildir)
        real = main.message_sort_time
        delivered: list[bool] = []

        def sort_time_with_a_delivery(path):
            if not delivered:
                delivered.append(True)
                live = maildir / "INBOX" / "new" / "live"
                _write_eml(live, "live@example.com")
                queue.enqueue(str(live), main.REASON_ON_CREATED)
            return real(path)

        monkeypatch.setattr(main, "message_sort_time", sort_time_with_a_delivery)
        main._enqueue_unindexed_messages(
            db, queue, maildir, main.REASON_INITIAL_SCAN, oldest_first=True
        )
        assert self._claim_order(db, queue) == ["old", "mid", "new", "undated", "live"]

    def test_a_resumed_scan_interleaves_with_the_queued_backlog(self, tmp_path, monkeypatch):
        """A scan resumed after a restart queues newly found mail by
        date among the rows the earlier scan left, not ahead of them."""
        maildir, _inbox, db, queue = self._setup(tmp_path, monkeypatch)
        _write_eml(
            maildir / "INBOX" / "cur" / "mid",
            "mid@example.com",
            received="Wed, 01 Jan 2020 09:00:00 +0000",
        )
        main._enqueue_unindexed_messages(
            db, queue, maildir, main.REASON_INITIAL_SCAN, oldest_first=True
        )
        _write_eml(
            maildir / "Archive" / "cur" / "old",
            "old@example.com",
            received="Mon, 01 Jan 2018 09:00:00 +0000",
        )
        main._enqueue_unindexed_messages(
            db, queue, maildir, main.REASON_INITIAL_SCAN, oldest_first=True
        )
        assert self._claim_order(db, queue) == ["old", "mid"]

    def test_rows_queued_before_this_order_existed_are_redated(self, tmp_path, monkeypatch):
        """Codex round 2 on #754: an upgrade can leave initial-scan rows
        queued by the old walk order, due at their enqueue time. The
        oldest-first walk re-dates every untried queued row to its
        message time, so an old parent queued that way still goes
        before a newly found reply."""
        maildir, _inbox, db, queue = self._setup(tmp_path, monkeypatch)
        root = maildir / "INBOX" / "cur" / "root"
        _write_eml(root, "root@example.com", received="Mon, 01 Jan 2024 09:00:00 +0000")
        queue.enqueue(str(root), main.REASON_INITIAL_SCAN)  # the old version, due now
        _write_eml(
            maildir / "Sent" / "cur" / "reply",
            "reply@example.com",
            in_reply_to="root@example.com",
            date="Tue, 02 Jan 2024 09:00:00 +0000",
        )
        assert (
            main._enqueue_unindexed_messages(
                db, queue, maildir, main.REASON_INITIAL_SCAN, oldest_first=True
            )
            == 1
        )
        assert self._claim_order(db, queue) == ["root", "reply"]

    def test_a_retrying_row_keeps_its_backoff(self, tmp_path, monkeypatch):
        """Only untried rows are re-dated: a row in its retry cascade
        stays due when its backoff ends."""
        maildir, _inbox, db, queue = self._setup(tmp_path, monkeypatch)
        old = maildir / "INBOX" / "cur" / "old"
        _write_eml(old, "old@example.com", received="Mon, 01 Jan 2018 09:00:00 +0000")
        queue.enqueue(str(old), main.REASON_INITIAL_SCAN)
        queue.mark_failed(str(old), stage="embed", error="EmbedResponseError: fixed text")
        before = db._conn.execute(
            "SELECT next_attempt_at FROM indexing_jobs WHERE filepath = ?", (str(old),)
        ).fetchone()[0]
        main._enqueue_unindexed_messages(
            db, queue, maildir, main.REASON_INITIAL_SCAN, oldest_first=True
        )
        after = db._conn.execute(
            "SELECT next_attempt_at FROM indexing_jobs WHERE filepath = ?", (str(old),)
        ).fetchone()[0]
        assert after == before

    def test_a_future_dated_message_is_due_now(self, tmp_path, monkeypatch):
        """A sender-controlled date in the future must not hold a row
        back: its due time is capped at the scan's start."""
        maildir, _inbox, db, queue = self._setup(tmp_path, monkeypatch)
        _write_eml(
            maildir / "Sent" / "cur" / "future",
            "future@example.com",
            date="Fri, 01 Jan 2100 09:00:00 +0000",
        )
        main._enqueue_unindexed_messages(
            db, queue, maildir, main.REASON_INITIAL_SCAN, oldest_first=True
        )
        assert self._claim_order(db, queue) == ["future"]

    def test_reply_in_sent_is_indexed_after_the_message_it_answers(self, tmp_path, monkeypatch):
        """#752: a reply walked before the message it answers (here in an
        earlier folder) still lands in the same thread, because the
        initial scan indexes oldest first."""
        maildir, _inbox, db, queue = self._setup(tmp_path, monkeypatch)
        _write_eml(
            maildir / "AAA-Sent" / "cur" / "reply",
            "reply@example.com",
            subject="Re: Dinner",
            in_reply_to="root@example.com",
            references=["root@example.com"],
            date="Tue, 02 Jan 2024 09:00:00 +0000",
            from_addr="bob@example.com",
            to_addr="alice@example.com",
        )
        _write_eml(
            maildir / "INBOX" / "cur" / "root",
            "root@example.com",
            subject="Dinner",
            received="Mon, 01 Jan 2024 09:00:00 +0000",
        )
        main.initial_index(db, make_mock_embedder(_UNIT_VECTOR), Threader(db), queue)
        assert db.find_thread_by_message_id("reply@example.com") == db.find_thread_by_message_id(
            "root@example.com"
        )

    def test_no_order_reads_no_headers(self, tmp_path, monkeypatch):
        """A rescan keeps walk order and reads no message headers."""
        maildir, _inbox, db, queue = self._setup(tmp_path, monkeypatch)
        self._three_folders(maildir)
        monkeypatch.setattr(main, "message_sort_time", lambda p: pytest.fail("read headers"))
        assert main._enqueue_unindexed_messages(db, queue, maildir, main.REASON_RESCAN) == 4

    def test_initial_index_queues_oldest_first(self, tmp_path, monkeypatch):
        flags: list[bool] = []

        def walk(*args, oldest_first=False, **kwargs):
            flags.append(oldest_first)
            return 0

        monkeypatch.setattr(main, "_enqueue_unindexed_messages", walk)
        db = Database(tmp_path / "mail.db")
        main.initial_index(db, make_mock_embedder(), Threader(db), _make_queue(db))
        assert flags == [True]

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

    def test_over_long_message_id_is_dead_lettered_without_the_id(
        self, tmp_path, monkeypatch, caplog
    ):
        """A Message-ID over 998 characters takes the no-Message-ID
        dead-letter path; the ID is sender-controlled, so neither the
        log nor ``last_error`` may carry it."""
        maildir, inbox, db, queue = self._setup(tmp_path, monkeypatch)
        long_id = inbox / "long-id.eml"
        marker = "MSGID998MARKER"
        _write_eml(long_id, marker + "x" * (999 - len(marker) - 13) + "@example.test")
        with caplog.at_level(logging.DEBUG):
            main.initial_index(db, make_mock_embedder(), Threader(db), queue)

        assert queue.is_dead(str(long_id))
        row = db._conn.execute(
            "SELECT last_stage, last_error FROM indexing_jobs WHERE filepath = ?",
            (str(long_id),),
        ).fetchone()
        assert row["last_stage"] == "parse"
        assert row["last_error"] == "unindexable: no Message-ID or one over 998 characters"
        assert marker not in caplog.text
        assert db._conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0


def _job_reasons(db: Database) -> dict[str, str]:
    return {
        Path(row["filepath"]).name: row["reason"]
        for row in db._conn.execute("SELECT filepath, reason FROM indexing_jobs")
    }


class _CountingObserver:
    """Stand-in observer for ``FolderWatchRefresher``: counts schedules."""

    def __init__(self):
        self.calls: list[str] = []

    def schedule(self, handler, path, *, recursive=False):
        self.calls.append("schedule")
        return object()

    def unschedule(self, watch):
        self.calls.append("unschedule")


class TestLateFolderWatches:
    """#516: mbsync creates folders 0700 and opens them to the indexer
    only after the sync, so the watch cannot cover a folder created
    during it. Each sync stamp triggers a re-watch of such folders."""

    _STAMP_TMP = ".mbsync-last-sync.2026-09-28T12:00:00Z.60.tmp"

    def test_stamp_rename_signals_a_completed_sync(self, tmp_path, db, monkeypatch):
        from watchdog.events import FileMovedEvent

        monkeypatch.setattr(main, "MAILDIR_PATH", tmp_path)
        sync_completed = main.threading.Event()
        handler = main.MaildirHandler(db, _make_queue(db), sync_completed=sync_completed)
        handler.on_moved(
            FileMovedEvent(str(tmp_path / self._STAMP_TMP), str(tmp_path / main.SYNC_STAMP_NAME))
        )

        assert sync_completed.is_set()

    def test_unrecognized_rename_onto_the_stamp_signals_nothing(self, tmp_path, db, monkeypatch):
        from watchdog.events import FileMovedEvent

        monkeypatch.setattr(main, "MAILDIR_PATH", tmp_path)
        sync_completed = main.threading.Event()
        handler = main.MaildirHandler(db, _make_queue(db), sync_completed=sync_completed)
        handler.on_moved(
            FileMovedEvent(str(tmp_path / "other.tmp"), str(tmp_path / main.SYNC_STAMP_NAME))
        )

        assert not sync_completed.is_set()

    def test_permission_repair_marker_signals_a_rewatch_but_no_sync(
        self, tmp_path, db, monkeypatch
    ):
        """#524: mbsync renames the marker into place after every
        permission repair, a failed sync attempt included. It triggers
        the re-watch, but it is not a completed sync: nothing is
        acknowledged and nothing is queued."""
        from watchdog.events import FileMovedEvent

        monkeypatch.setattr(main, "MAILDIR_PATH", tmp_path)
        sync_completed = main.threading.Event()
        ingestion_state = main._IngestionStateRecorder(db, tmp_path)
        handler = main.MaildirHandler(
            db, _make_queue(db), ingestion_state=ingestion_state, sync_completed=sync_completed
        )
        handler.on_moved(
            FileMovedEvent(
                str(tmp_path / f"{main.PERMS_REPAIRED_NAME}.tmp"),
                str(tmp_path / main.PERMS_REPAIRED_NAME),
            )
        )

        assert sync_completed.is_set()
        assert ingestion_state._acked is None
        assert _job_reasons(db) == {}

    def test_permission_repair_marker_counts_only_at_the_root(self, tmp_path, db, monkeypatch):
        from watchdog.events import FileMovedEvent

        monkeypatch.setattr(main, "MAILDIR_PATH", tmp_path)
        sync_completed = main.threading.Event()
        handler = main.MaildirHandler(db, _make_queue(db), sync_completed=sync_completed)
        box = tmp_path / "Box" / "cur"
        handler.on_moved(
            FileMovedEvent(
                str(box / f"{main.PERMS_REPAIRED_NAME}.tmp"), str(box / main.PERMS_REPAIRED_NAME)
            )
        )

        assert not sync_completed.is_set()

    def test_directory_create_marks_the_watch_stale(self, tmp_path, db):
        from watchdog.events import DirCreatedEvent, FileCreatedEvent

        created = main.threading.Event()
        handler = main.MaildirHandler(db, _make_queue(db), directory_created=created)
        handler.on_created(FileCreatedEvent(str(tmp_path / "Box" / "new" / "m")))
        assert not created.is_set()

        handler.on_created(DirCreatedEvent(str(tmp_path / "Box")))
        assert created.is_set()

    def test_refresh_watches_a_new_folder_and_queues_its_mail(self, tmp_path, db, monkeypatch):
        maildir = tmp_path / "maildir"
        _write_eml(maildir / "INBOX" / "cur" / "old.eml:2,S", "old@example.com")
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        steps: list[str] = []
        monkeypatch.setattr(main, "sweep_paths", lambda db: steps.append("sweep_paths"))
        queue = _make_queue(db)
        observer = _CountingObserver()
        refresher = main.FolderWatchRefresher(maildir, observer, handler=None)  # type: ignore[arg-type]
        refresher.start()
        db._conn.execute("DELETE FROM indexing_jobs")
        _write_eml(maildir / "Late" / "new" / "late.eml", "late@example.com")

        assert main._refresh_folder_watches(refresher, db, queue) is True
        assert observer.calls == ["schedule", "unschedule", "schedule"]
        # Renames lost while the watch was replaced are healed before
        # the walk, as at startup.
        assert steps == ["sweep_paths"]
        assert _job_reasons(db) == {
            "late.eml": main.REASON_RESCAN,
            "old.eml:2,S": main.REASON_RESCAN,
        }

        # A later sync with no new folder costs neither a watch nor a walk.
        db._conn.execute("DELETE FROM indexing_jobs")
        assert main._refresh_folder_watches(refresher, db, queue) is False
        assert observer.calls == ["schedule", "unschedule", "schedule"]
        assert steps == ["sweep_paths"]
        assert _job_reasons(db) == {}

    @pytest.mark.parametrize("failing_step", ["sweep_paths", "_enqueue_unindexed_messages"])
    def test_failed_recovery_walk_is_retried_on_the_next_refresh(
        self, tmp_path, db, monkeypatch, failing_step
    ):
        """#529: once the watch is replaced, the recovery steps must run
        to completion. A step that raises is retried by the next refresh
        (a sync stamp or the periodic tick), once per call, until both
        steps succeed; then later refreshes do no walk."""
        maildir = tmp_path / "maildir"
        _write_eml(maildir / "INBOX" / "cur" / "old.eml:2,S", "old@example.com")
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        attempts: list[str] = []
        failures = {"left": 2}
        real_walk = main._enqueue_unindexed_messages

        def step(name, real):
            def run(*a, **kw):
                attempts.append(name)
                if name == failing_step and failures["left"]:
                    failures["left"] -= 1
                    raise sqlite3.OperationalError("database is locked")
                return real(*a, **kw)

            return run

        monkeypatch.setattr(main, "sweep_paths", step("sweep_paths", lambda db: 0))
        monkeypatch.setattr(
            main, "_enqueue_unindexed_messages", step("_enqueue_unindexed_messages", real_walk)
        )
        queue = _make_queue(db)
        observer = _CountingObserver()
        refresher = main.FolderWatchRefresher(maildir, observer, handler=None)  # type: ignore[arg-type]
        refresher.start()
        db._conn.execute("DELETE FROM indexing_jobs")
        _write_eml(maildir / "Late" / "new" / "late.eml", "late@example.com")

        for _ in range(2):
            attempts.clear()
            with pytest.raises(sqlite3.OperationalError):
                main._refresh_folder_watches(refresher, db, queue)
            # One attempt per call: a failure does not spin.
            assert attempts.count(failing_step) == 1
        # The watch is replaced once; only the recovery is retried.
        assert observer.calls == ["schedule", "unschedule", "schedule"]

        attempts.clear()
        assert main._refresh_folder_watches(refresher, db, queue) is True
        assert attempts == ["sweep_paths", "_enqueue_unindexed_messages"]
        assert _job_reasons(db) == {
            "late.eml": main.REASON_RESCAN,
            "old.eml:2,S": main.REASON_RESCAN,
        }

        attempts.clear()
        assert main._refresh_folder_watches(refresher, db, queue) is False
        assert attempts == []
        assert observer.calls == ["schedule", "unschedule", "schedule"]

    @pytest.mark.skipif(
        not sys.platform.startswith("linux") or os.geteuid() == 0,
        reason="the EACCES gap is inotify-specific, and root can enter a 000 directory",
    )
    def test_delivery_into_a_late_folder_is_queued_by_the_watcher(self, tmp_path, db, monkeypatch):
        """Acceptance for #516 against watchdog's real inotify observer:
        a folder unreadable when the watch starts, opened by the sync's
        permission repair, is watched after the sync stamp."""
        import threading
        import time as _time

        from watchdog.observers import Observer

        maildir = tmp_path / "maildir"
        late = maildir / "Late"
        for sub in ("cur", "new", "tmp"):
            (late / sub).mkdir(parents=True)
        # Delivered by the sync while the folder is still 0700 (owned by
        # mbsync; here, unreadable to everyone).
        _write_eml(late / "new" / "first", "first@example.com")
        late.chmod(0o000)
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        queue = _make_queue(db)
        sync_completed = threading.Event()
        handler = main.MaildirHandler(db, queue, sync_completed=sync_completed)
        observer = Observer()
        refresher = main.FolderWatchRefresher(maildir, observer, handler)
        refresher.start()
        observer.start()

        def wait_for(name: str, timeout: float = 5.0) -> str | None:
            deadline = _time.monotonic() + timeout
            while _time.monotonic() < deadline:
                reason = _job_reasons(db).get(name)
                if reason is not None:
                    return reason
                _time.sleep(0.05)
            return None

        try:
            # The sync repairs permissions, then stamps. Nothing has
            # watched the folder: a delivery into it stays unqueued.
            late.chmod(0o755)
            _write_eml(late / "tmp" / "unseen", "unseen@example.com")
            (late / "tmp" / "unseen").rename(late / "new" / "unseen")
            assert wait_for("unseen", timeout=1.0) is None, "watch already covered the late folder"
            (maildir / self._STAMP_TMP).write_text(STAMP_JSON)
            (maildir / self._STAMP_TMP).rename(maildir / main.SYNC_STAMP_NAME)
            assert sync_completed.wait(5)

            assert main._refresh_folder_watches(refresher, db, queue) is True
            # Mail delivered before the re-watch is queued by its walk.
            assert _job_reasons(db)["first"] == main.REASON_RESCAN
            assert _job_reasons(db)["unseen"] == main.REASON_RESCAN

            # The next delivery reaches the watcher, not a sweep.
            _write_eml(late / "tmp" / "second", "second@example.com")
            (late / "tmp" / "second").rename(late / "new" / "second")
            assert wait_for("second") == main.REASON_ON_MOVED
        finally:
            late.chmod(0o755)
            observer.stop()
            observer.join()

    @pytest.mark.skipif(
        not sys.platform.startswith("linux") or os.geteuid() == 0,
        reason="the EACCES gap is inotify-specific, and root can enter a 000 directory",
    )
    def test_failed_sync_attempt_gets_its_folder_watched_without_a_sweep(
        self, tmp_path, db, monkeypatch
    ):
        """#524, against watchdog's real inotify observer: a failed sync
        attempt still repairs permissions but writes no stamp. The
        repair marker that follows it re-watches the folder it opened,
        with no recovery sweep. Each marker costs at most one
        re-schedule, and none when no directory changed."""
        import threading
        import time as _time

        from watchdog.observers import Observer

        schedules: list[str] = []

        class _Counting(Observer):
            def schedule(self, *a, **kw):
                schedules.append("schedule")
                return super().schedule(*a, **kw)

        maildir = tmp_path / "maildir"
        late = maildir / "Late"
        for sub in ("cur", "new", "tmp"):
            (late / sub).mkdir(parents=True)
        _write_eml(late / "new" / "first", "first@example.com")
        late.chmod(0o000)
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        queue = _make_queue(db)
        sync_completed = threading.Event()
        handler = main.MaildirHandler(db, queue, sync_completed=sync_completed)
        observer = _Counting()
        refresher = main.FolderWatchRefresher(maildir, observer, handler)
        refresher.start()
        observer.start()

        def wait_for(name: str, timeout: float = 5.0) -> str | None:
            deadline = _time.monotonic() + timeout
            while _time.monotonic() < deadline:
                reason = _job_reasons(db).get(name)
                if reason is not None:
                    return reason
                _time.sleep(0.05)
            return None

        def signal_repair() -> None:
            # As mbsync's signal_perms_repaired: an empty file renamed
            # into place at the Maildir root.
            tmp = maildir / f"{main.PERMS_REPAIRED_NAME}.tmp"
            tmp.write_text("")
            tmp.rename(maildir / main.PERMS_REPAIRED_NAME)
            assert sync_completed.wait(5)
            sync_completed.clear()

        try:
            # The failed attempt repairs permissions; no stamp follows.
            late.chmod(0o755)
            signal_repair()
            assert main._refresh_folder_watches(refresher, db, queue) is True
            assert schedules == ["schedule", "schedule"]
            assert _job_reasons(db)["first"] == main.REASON_RESCAN
            assert not (maildir / main.SYNC_STAMP_NAME).exists()

            _write_eml(late / "tmp" / "second", "second@example.com")
            (late / "tmp" / "second").rename(late / "new" / "second")
            assert wait_for("second") == main.REASON_ON_MOVED

            # Another failed attempt that opened nothing: a folder walk,
            # no re-schedule and no Maildir walk.
            db._conn.execute("DELETE FROM indexing_jobs")
            signal_repair()
            assert main._refresh_folder_watches(refresher, db, queue) is False
            assert schedules == ["schedule", "schedule"]
            assert _job_reasons(db) == {}
        finally:
            late.chmod(0o755)
            observer.stop()
            observer.join()

    @pytest.mark.skipif(
        not sys.platform.startswith("linux") or os.geteuid() == 0,
        reason="the EACCES gap is inotify-specific, and root can enter a 000 directory",
    )
    def test_folder_deleted_and_recreated_is_watched_again(self, tmp_path, db, monkeypatch):
        """Review round 2, against the real inotify observer: removing a
        watched folder drops its watch, and mbsync recreates it 0700, so
        watchdog cannot watch the replacement, whose inode number may be
        the old one's."""
        import shutil
        import threading
        import time as _time

        from watchdog.observers import Observer

        maildir = tmp_path / "maildir"
        box = maildir / "Box"
        for sub in ("cur", "new", "tmp"):
            (box / sub).mkdir(parents=True)
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        queue = _make_queue(db)
        created = threading.Event()
        handler = main.MaildirHandler(db, queue, directory_created=created)
        observer = Observer()
        refresher = main.FolderWatchRefresher(maildir, observer, handler, directory_created=created)
        refresher.start()
        observer.start()

        def wait_for(name: str, timeout: float = 5.0) -> str | None:
            deadline = _time.monotonic() + timeout
            while _time.monotonic() < deadline:
                reason = _job_reasons(db).get(name)
                if reason is not None:
                    return reason
                _time.sleep(0.05)
            return None

        try:
            shutil.rmtree(box)
            box.mkdir(mode=0o000)
            assert created.wait(5)
            box.chmod(0o755)
            for sub in ("cur", "new", "tmp"):
                (box / sub).mkdir()
            _write_eml(box / "tmp" / "unseen", "unseen@example.com")
            (box / "tmp" / "unseen").rename(box / "new" / "unseen")
            assert wait_for("unseen", timeout=1.0) is None, "replacement already watched"

            assert main._refresh_folder_watches(refresher, db, queue) is True
            _write_eml(box / "tmp" / "after", "after@example.com")
            (box / "tmp" / "after").rename(box / "new" / "after")
            assert wait_for("after") == main.REASON_ON_MOVED
        finally:
            if box.exists():
                box.chmod(0o755)
            observer.stop()
            observer.join()


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

    def _run_main(
        self,
        tmp_path,
        monkeypatch,
        *,
        sweep_due: bool,
        drain=None,
        synced=False,
        refresh=None,
        embed_url="http://host.docker.internal:8001/v1",
        embed_vector=None,
    ):
        events: list[str] = []
        self._events = events
        db = Database(tmp_path / "mail.db")
        self._db = db
        monkeypatch.setattr(main, "_ingestion_state", None)
        monkeypatch.setattr(main, "MAILDIR_PATH", tmp_path / "maildir")
        monkeypatch.setattr(main, "_validate_embed_config", lambda: None)
        monkeypatch.setattr(main, "Database", lambda path: db)
        embedder = make_mock_embedder(embed_vector or [0.1] * EMBEDDING_DIM)
        embedder.base_url = embed_url
        self._embedder = embedder
        monkeypatch.setattr(main, "OpenAIEmbedder", lambda **kw: embedder)
        monkeypatch.setattr(main, "touch_health_file", lambda: None)
        monkeypatch.setattr(main, "sweep_paths", lambda db: events.append("sweep_paths"))
        monkeypatch.setattr(main, "Observer", lambda: _FakeObserver(events))
        monkeypatch.setattr(
            main,
            "initial_index",
            lambda *a, **kw: (
                events.append(f"initial_index:skip_trashed={kw.get('skip_trashed')}"),
                events.append(f"initial_breaker={id(kw.get('breaker'))}"),
                events.append(f"initial_state={id(kw.get('ingestion_state'))}"),
            ),
        )
        monkeypatch.setattr(
            main,
            "_drain_queue_batched",
            lambda *a, **kw: (
                (
                    events.append(f"drain:breaker={id(kw.get('breaker'))}"),
                    events.append(f"drain:state={id(kw.get('ingestion_state'))}"),
                )
                and 0
            ),
        )
        if drain is not None:
            monkeypatch.setattr(main, "_drain_queue_batched", drain)
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
        monkeypatch.setattr(
            main,
            "_refresh_folder_watches",
            refresh
            or (lambda fw, db, queue, **kw: events.append(f"refresh:{kw.get('skip_trashed')}")),
        )
        if synced:
            # A sync stamp arrived before the main loop's first tick.
            real_handler = main.MaildirHandler

            def handler(*a, **kw):
                kw["sync_completed"].set()
                return real_handler(*a, **kw)

            monkeypatch.setattr(main, "MaildirHandler", handler)

        class _FakeStallGuard:
            def __init__(self, queue, *, limit_seconds):
                self.limit_seconds = limit_seconds

            def start(self):
                events.append(f"stall_guard:{self.limit_seconds}")

        # A real guard thread would hit the patched ``time.sleep`` below.
        monkeypatch.setattr(main, "StallGuard", _FakeStallGuard)

        def _stop(_seconds):
            raise KeyboardInterrupt

        monkeypatch.setattr(main.time, "sleep", _stop)
        main.main()
        return events

    # --- startup embedder checks (#841) ------------------------------------
    # Wrong vector width and a changed embedder identity each stop startup
    # before indexing, on a fresh index and on one that already records
    # its embedder.

    def _record_identity(self, tmp_path, monkeypatch, vector):
        db = Database(tmp_path / "mail.db")
        embedder = make_mock_embedder(vector)
        embedder.base_url = "http://host.docker.internal:8001/v1"
        main._check_embedder_identity(db, embedder)
        db.close()

    def _generations(self):
        return self._db._conn.execute("SELECT model, dimensions FROM vector_generations").fetchall()

    def _assert_stopped_before_indexing(self, tmp_path, monkeypatch, vector):
        with pytest.raises(SystemExit) as info:
            self._run_main(tmp_path, monkeypatch, sweep_due=False, embed_vector=vector)
        assert not any(e.startswith("initial_index") for e in self._events)
        return str(info.value.code)

    def test_fresh_index_with_wrong_width_stops_before_recording(self, tmp_path, monkeypatch):
        message = self._assert_stopped_before_indexing(
            tmp_path, monkeypatch, [0.1] * (EMBEDDING_DIM + 256)
        )
        assert message == (
            f"Embedder produced {EMBEDDING_DIM + 256}-dim vectors, but the SQLite "
            f"schema reserves {EMBEDDING_DIM}-dim (threads_vec "
            f"FLOAT[{EMBEDDING_DIM}]). Either switch to a model that "
            f"outputs {EMBEDDING_DIM}-dim vectors, or migrate the schema."
        )
        assert self._generations() == []

    def test_existing_index_with_wrong_width_stops_with_the_width_message(
        self, tmp_path, monkeypatch
    ):
        self._record_identity(tmp_path, monkeypatch, [0.1] * EMBEDDING_DIM)
        message = self._assert_stopped_before_indexing(
            tmp_path, monkeypatch, [0.1] * (EMBEDDING_DIM - 1)
        )
        assert message.startswith(f"Embedder produced {EMBEDDING_DIM - 1}-dim vectors")
        assert [tuple(r) for r in self._generations()] == [(main.EMBED_MODEL, EMBEDDING_DIM)]

    def test_existing_index_with_another_embedder_stops_with_the_identity_message(
        self, tmp_path, monkeypatch
    ):
        recorded = [0.0] * EMBEDDING_DIM
        recorded[0] = 1.0
        self._record_identity(tmp_path, monkeypatch, recorded)
        other = [0.0] * EMBEDDING_DIM
        other[1] = 1.0
        message = self._assert_stopped_before_indexing(tmp_path, monkeypatch, other)
        assert "not the one that built this index" in message
        assert "calibration vector: cosine distance" in message

    def test_fresh_index_holding_messages_stops_with_the_identity_message(
        self, tmp_path, monkeypatch
    ):
        db = Database(tmp_path / "mail.db")
        db.upsert_thread(
            make_thread(messages=[make_message(message_id="m@example.com")]),
            [0.0] * EMBEDDING_DIM,
        )
        db.close()
        message = self._assert_stopped_before_indexing(tmp_path, monkeypatch, [0.1] * EMBEDDING_DIM)
        assert "holds messages but no record of the embedder" in message
        assert self._generations() == []

    def test_fresh_index_records_the_embedder_and_indexes(self, tmp_path, monkeypatch):
        events = self._run_main(
            tmp_path, monkeypatch, sweep_due=False, embed_vector=[0.1] * EMBEDDING_DIM
        )
        assert any(e.startswith("initial_index") for e in events)
        assert [tuple(r) for r in self._generations()] == [(main.EMBED_MODEL, EMBEDDING_DIM)]

    def test_startup_probes_the_embedder_once(self, tmp_path, monkeypatch):
        """#841: the width check reuses the calibration vector, so startup
        makes one embed request, on a fresh index and on a recorded one."""
        for _ in range(2):
            self._run_main(
                tmp_path, monkeypatch, sweep_due=False, embed_vector=[0.1] * EMBEDDING_DIM
            )
            assert [c.args for c in self._embedder.embed.call_args_list] == [(CALIBRATION_TEXT,)]
            self._db.close()

    def test_stall_guard_starts_before_initial_drain(self, tmp_path, monkeypatch):
        """#235: the guard must be watching during the initial drain,
        which is where a hostile message first stalls the worker."""
        monkeypatch.setattr(main, "INDEXER_MESSAGE_TIMEOUT_SECONDS", 1234)
        events = self._run_main(tmp_path, monkeypatch, sweep_due=False)

        init = next(e for e in events if e.startswith("initial_index"))
        assert events.index("stall_guard:1234") < events.index(init)

    def test_stall_guard_disabled_at_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "INDEXER_MESSAGE_TIMEOUT_SECONDS", 0)
        events = self._run_main(tmp_path, monkeypatch, sweep_due=False)

        assert not any(e.startswith("stall_guard:") for e in events)

    def test_rename_sweep_runs_before_initial_index(self, tmp_path, monkeypatch):
        """#204 follow-up: files renamed while the indexer was down must
        have their stored paths healed before the startup walk, or the
        walk sees each renamed path as unindexed mail and reprocesses it."""
        events = self._run_main(tmp_path, monkeypatch, sweep_due=False)

        init = next(e for e in events if e.startswith("initial_index"))
        assert events.index("sweep_paths") < events.index(init)

    def test_remote_embedder_warns_once_with_host_only(self, tmp_path, monkeypatch, caplog):
        """#622: an embedder off the host receives mail text, so startup
        logs one WARNING naming the mode and host, not the path or key."""
        monkeypatch.setattr(
            main, "EMBED_API_KEY", "sk-synthetic-marker"
        )  # pragma: allowlist secret
        caplog.set_level(logging.DEBUG)
        self._run_main(
            tmp_path,
            monkeypatch,
            sweep_due=False,
            embed_url="https://embed.example:8443/v1?tenant=SYNTHETIC_QUERY",
        )

        warnings = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Privacy:")]
        assert len(warnings) == 1
        assert "EMBED_MODE=openai" in warnings[0]
        assert "embed.example" in warnings[0]
        for part in ("/v1", "SYNTHETIC_QUERY", ":8443"):
            assert part not in warnings[0]
        assert "sk-synthetic-marker" not in caplog.text

    def test_host_local_embedder_does_not_warn(self, tmp_path, monkeypatch, caplog):
        caplog.set_level(logging.DEBUG)
        self._run_main(tmp_path, monkeypatch, sweep_due=False)

        assert not [r for r in caplog.records if r.getMessage().startswith("Privacy:")]

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

    def test_heartbeat_and_initial_index_share_one_recorder(self, tmp_path, monkeypatch):
        """``get_mailbox_status`` needs liveness through the initial
        drain as well as in steady state."""
        events = self._run_main(tmp_path, monkeypatch, sweep_due=False)

        initial = next(e for e in events if e.startswith("initial_state="))
        assert initial == f"initial_state={id(main._ingestion_state)}"
        assert main._ingestion_state is not None

    def test_periodic_rescan_acknowledges_the_stamp(self, tmp_path, monkeypatch):
        (tmp_path / "maildir").mkdir()
        (tmp_path / "maildir" / main.SYNC_STAMP_NAME).write_text(STAMP_JSON)
        self._run_main(tmp_path, monkeypatch, sweep_due=True)

        main._ingestion_state.maybe_record(now=10**9)
        row = self._db._conn.execute("SELECT sync_completed_at FROM ingestion_state").fetchone()
        assert row["sync_completed_at"] == STAMP.completed_at

    def test_drain_failure_log_keeps_mail_out(self, tmp_path, monkeypatch, caplog):
        """The main loop's drain backstop logs through the same
        classification as ``last_error`` (#257)."""

        def drain(*_a, **_kw):
            raise ValueError(SYNTHETIC_MARKER)

        self._run_main(tmp_path, monkeypatch, sweep_due=False, drain=drain)

        assert "queue drain failed: ValueError" in caplog.text
        assert SYNTHETIC_MARKER not in caplog.text

    def test_main_loop_rewatches_folders_after_a_sync(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INDEXER_DELETION_ENABLED", "true")
        assert "refresh:True" in self._run_main(tmp_path, monkeypatch, sweep_due=False, synced=True)

    def test_recovery_sweep_also_rewatches_folders(self, tmp_path, monkeypatch):
        """Review round 1: a failed sync attempt still opens the folders
        it created but writes no stamp, and a failed re-schedule leaves
        no watch until the next refresh. The sweep cadence bounds both."""
        monkeypatch.setenv("INDEXER_DELETION_ENABLED", "true")
        events = self._run_main(tmp_path, monkeypatch, sweep_due=True)

        assert "refresh:True" in events

    def test_main_loop_leaves_the_watch_alone_without_a_sync(self, tmp_path, monkeypatch):
        events = self._run_main(tmp_path, monkeypatch, sweep_due=False)

        assert not any(e.startswith("refresh:") for e in events)

    def test_rewatch_failure_log_keeps_paths_out(self, tmp_path, monkeypatch, caplog):
        def refresh(*_a, **_kw):
            raise PermissionError(13, "denied", SYNTHETIC_MARKER)

        self._run_main(tmp_path, monkeypatch, sweep_due=False, synced=True, refresh=refresh)

        assert "Maildir watch refresh failed: PermissionError" in caplog.text
        assert SYNTHETIC_MARKER not in caplog.text

    def test_main_loop_periodically_rewalks_the_maildir(self, tmp_path, monkeypatch):
        events = self._run_main(tmp_path, monkeypatch, sweep_due=True)

        assert any(e.startswith(f"walk:{main.REASON_RESCAN}:") for e in events)

    def test_trashed_files_skipped_only_when_deletion_reconciliation_enabled(
        self, tmp_path, monkeypatch
    ):
        """With reconciliation on, a T-flagged file means "deleted
        upstream" — every Maildir walk must skip it or reaped messages
        come back. With it off (archive opt-out), the index is
        append-only and T-flagged files are indexed like any other."""
        monkeypatch.setenv("INDEXER_DELETION_ENABLED", "true")
        events = self._run_main(tmp_path, monkeypatch, sweep_due=True)
        assert "initial_index:skip_trashed=True" in events
        assert f"walk:{main.REASON_RESCAN}:skip_trashed=True" in events

        monkeypatch.setenv("INDEXER_DELETION_ENABLED", "false")
        events = self._run_main(tmp_path / "off", monkeypatch, sweep_due=True)
        assert "initial_index:skip_trashed=False" in events
        assert f"walk:{main.REASON_RESCAN}:skip_trashed=False" in events

    def test_mirror_is_the_default_when_unset(self, tmp_path, monkeypatch):
        """Mirror is the shipped default: with no variable set the
        indexer starts the reconciler and its walks skip T-flagged
        files."""
        monkeypatch.delenv("INDEXER_DELETION_ENABLED", raising=False)
        events = self._run_main(tmp_path, monkeypatch, sweep_due=True)
        assert "initial_index:skip_trashed=True" in events
        assert f"walk:{main.REASON_RESCAN}:skip_trashed=True" in events


class TestReapedMessagesStayDeleted:
    """Deletion reconciliation keeps a reaped message's T-flagged
    ``.eml`` on disk: the indexer never deletes Maildir files. That
    file is no longer indexed or queued, so any enqueue path that
    treats "on disk but not indexed" as undiscovered mail would
    resurrect it into search — and the next sweep would start a fresh
    grace window."""

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
            ReconcilerConfig(
                enabled=True,
                grace_days=0,
                sweep_interval_secs=60,
                max_batch_pct=1.0,
                force=False,
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
        _drain(queue, db, embedder, threader)

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
    import httpx2
    from openai import APIStatusError

    return APIStatusError(
        message=f"{status_code} error",
        response=httpx2.Response(status_code, request=httpx2.Request("POST", "http://x")),
        body=None,
    )


def _connection_error():
    import httpx2
    from openai import APIConnectionError

    return APIConnectionError(request=httpx2.Request("POST", "http://x"))


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
        assert _chunk_ids(db, "a@example.com")

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

        assert _chunk_ids(db, "good1@example.com")
        assert _chunk_ids(db, "good2@example.com")
        row = self._row(db, paths["bad"])
        assert row["status"] == "dead"
        assert row["last_stage"] == "embed"
        assert row["last_error_class"] == "permanent_source_failure"
        assert queue.stats() == {"queued": 0, "dead": 1}

    def test_non_finite_vector_is_isolated_and_never_committed(self, tmp_path, monkeypatch):
        """A 200 response carrying NaN (base64 float32, the SDK's own
        encoding) must not be stored as a successfully indexed vector,
        and its batchmates must still be indexed (#232)."""
        import base64
        import math
        import struct

        import httpx2

        db, threader, queue, paths = self._setup(
            tmp_path,
            monkeypatch,
            {"good1": "fine text one", "bad": "POISON input", "good2": "fine text two"},
        )
        nan_vec = [float("nan")] + [0.0] * (EMBEDDING_DIM - 1)
        nan_b64 = base64.b64encode(struct.pack(f"{EMBEDDING_DIM}f", *nan_vec)).decode()
        unit_b64 = base64.b64encode(struct.pack(f"{EMBEDDING_DIM}f", *_UNIT_VECTOR)).decode()

        def handler(request):
            inputs = json.loads(request.content)["input"]
            data = [
                {
                    "object": "embedding",
                    "index": i,
                    "embedding": nan_b64 if "POISON" in text else unit_b64,
                }
                for i, text in enumerate(inputs)
            ]
            return httpx2.Response(
                200,
                json={"object": "list", "model": "m", "data": data, "usage": {}},
            )

        self._drain(db, _mock_transport_embedder(handler), threader, queue)

        assert _chunk_ids(db, "good1@example.com")
        assert _chunk_ids(db, "good2@example.com")
        assert not _chunk_ids(db, "bad@example.com")
        assert self._row(db, paths["bad"])["status"] in ("queued", "dead")
        for row in db._conn.execute("SELECT embedding FROM threads_vec").fetchall():
            blob = row["embedding"]
            assert all(math.isfinite(v) for v in struct.unpack(f"{len(blob) // 4}f", blob))

    def test_all_zero_vector_is_isolated_and_never_committed(self, tmp_path, monkeypatch, caplog):
        """A 200 response carrying an all-zero vector must not settle the
        job as successfully embedded, its batchmates must still be
        indexed, and neither the mail text nor response values may reach
        the log or ``last_error`` (#304)."""
        import base64
        import struct

        import httpx2

        marker = "SYNTHETIC_ZERO_MARKER"
        db, threader, queue, paths = self._setup(
            tmp_path,
            monkeypatch,
            {"good1": "fine text one", "bad": f"POISON {marker}", "good2": "fine text two"},
        )
        zero_b64 = base64.b64encode(struct.pack(f"{EMBEDDING_DIM}f", *([0.0] * EMBEDDING_DIM)))
        unit_b64 = base64.b64encode(struct.pack(f"{EMBEDDING_DIM}f", *_UNIT_VECTOR)).decode()

        def handler(request):
            inputs = json.loads(request.content)["input"]
            data = [
                {
                    "object": "embedding",
                    "index": i,
                    "embedding": zero_b64.decode() if "POISON" in text else unit_b64,
                }
                for i, text in enumerate(inputs)
            ]
            return httpx2.Response(
                200,
                json={"object": "list", "model": "m", "data": data, "usage": {}},
            )

        with caplog.at_level("DEBUG"):
            self._drain(db, _mock_transport_embedder(handler), threader, queue)

        assert _chunk_ids(db, "good1@example.com")
        assert _chunk_ids(db, "good2@example.com")
        assert not _chunk_ids(db, "bad@example.com")
        assert self._row(db, paths["bad"])["status"] in ("queued", "dead")
        last_error = db._conn.execute(
            "SELECT last_error FROM indexing_jobs WHERE filepath = ?", (paths["bad"],)
        ).fetchone()["last_error"]
        assert "all-zero" in last_error
        assert marker not in last_error
        assert marker not in caplog.text
        # The failed thread keeps its intentional phase-one zero placeholder
        # in ``threads_vec``; only chunk vectors are checked.
        zero_blob = struct.pack(f"{EMBEDDING_DIM}f", *([0.0] * EMBEDDING_DIM))
        for row in db._conn.execute("SELECT embedding FROM message_chunks_vec").fetchall():
            assert row["embedding"] != zero_blob

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

        assert _chunk_ids(db, "good@example.com")
        row = self._row(db, paths["bad"])
        assert row["status"] == "queued"
        assert row["attempts"] == 1
        assert row["last_error_class"] == "retryable"
        assert breaker.allow(main.time.monotonic())

    def test_batch_with_nothing_to_embed_does_not_close_the_breaker(
        self, tmp_path, monkeypatch, caplog
    ):
        """Codex round 2 on #904: a due batch of already-indexed,
        unchanged messages sends no embed request, so it proves nothing
        about the provider. It must neither log the recovery line nor
        reset the breaker; only a successful embed request does."""
        caplog.set_level(logging.INFO)
        db, threader, queue, paths = self._setup(tmp_path, monkeypatch, {"a": "alpha body"})
        embedder = make_mock_embedder()
        embedder.embed.return_value = _UNIT_VECTOR
        self._drain(db, embedder, threader, queue)
        assert _chunk_ids(db, "a@example.com")

        breaker = main._EmbedOutageBreaker()
        breaker.record_failure(0.0)  # an outage whose pause has elapsed
        queue.enqueue(paths["a"], REASON_INITIAL_SCAN)
        _make_due(db)
        calls_before = embedder.embed.call_count
        caplog.clear()

        processed = self._drain(db, embedder, threader, queue, breaker=breaker)

        assert processed == 1
        assert embedder.embed.call_count == calls_before
        assert breaker.consecutive_failures == 1
        assert "embedder recovered" not in caplog.text

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
        assert _chunk_ids(db, "a@example.com")
        assert _chunk_ids(db, "b@example.com")

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
        assert _chunk_ids(db, "a@example.com")

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

        embedded = [n for n in paths if _chunk_ids(db, f"{n}@example.com")]
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
    ``httpx2.MockTransport``, so tests exercise actual HTTP request
    boundaries (how many inputs share one request) rather than a
    per-text mock."""
    import httpx2
    from openai import OpenAI
    from src.embedder import OpenAIEmbedder

    embedder = OpenAIEmbedder(base_url="http://embed.test/v1", model="m", api_key="k")
    embedder.client = OpenAI(
        base_url="http://embed.test/v1",
        api_key="k",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    return embedder


def _embeddings_response(inputs: list[str]):
    import httpx2

    return httpx2.Response(
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

        _drain(queue, db, _mock_transport_embedder(recording), Threader(db))
        return db, queue, str(dest), sizes

    def test_multi_input_request_rejection_retries_one_input_per_request(
        self, tmp_path, monkeypatch
    ):
        import httpx2

        def handler(inputs):
            if len(inputs) > 1:
                return httpx2.Response(413, json={"error": {"message": "payload too large"}})
            return _embeddings_response(inputs)

        db, queue, path, sizes = self._drain_one(tmp_path, monkeypatch, handler)

        assert sizes[:3] == [2, 1, 2], "batch, probe, then the message's own combined request"
        assert all(n == 1 for n in sizes[3:])
        assert _chunk_ids(db, "limits@example.com")
        assert queue.stats() == {"queued": 0, "dead": 0}

    def test_input_rejected_on_its_own_is_a_permanent_source_failure(self, tmp_path, monkeypatch):
        import httpx2

        def handler(inputs):
            if len(inputs) > 1 or any("attachment text" in t for t in inputs):
                return httpx2.Response(400, json={"error": {"message": "bad input"}})
            return _embeddings_response(inputs)

        db, queue, path, sizes = self._drain_one(tmp_path, monkeypatch, handler)

        row = db._conn.execute(
            "SELECT status, last_error_class FROM indexing_jobs WHERE filepath = ?", (path,)
        ).fetchone()
        assert row["status"] == "dead"
        assert row["last_error_class"] == "permanent_source_failure"
        assert not _chunk_ids(db, "limits@example.com")


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
    _drain(queue, db, make_mock_embedder(_UNIT_VECTOR), Threader(db), batch_size=1, max_passes=1)

    row = db._conn.execute(
        "SELECT last_stage, last_error FROM indexing_jobs WHERE filepath = ?", (str(dest),)
    ).fetchone()
    assert row["last_stage"] == "parse"
    assert row["last_error"] == "UnicodeDecodeError"
    assert "PRIVATE-BODY-TEXT" not in row["last_error"]


SYNTHETIC_MARKER = "SYNTHETIC_MARKER_257"

# Exceptions whose text can quote the message they were raised on: the
# email generator quotes the header it refused, a parser defect its
# line, a codec its data and a charset lookup the sender's label.
_CONTENT_QUOTING_ERRORS = [
    pytest.param(
        lambda: email.errors.HeaderWriteError(
            f"folded header contains newline: 'X-Note: {SYNTHETIC_MARKER}'"
        ),
        "HeaderWriteError",
        id="header-write",
    ),
    pytest.param(
        lambda: email.errors.MessageDefect(f" {SYNTHETIC_MARKER}"),
        "MessageDefect",
        id="message-defect",
    ),
    pytest.param(
        lambda: UnicodeEncodeError("ascii", f"{SYNTHETIC_MARKER}\u00e9", 0, 1, SYNTHETIC_MARKER),
        "UnicodeEncodeError",
        id="unicode-encode",
    ),
    pytest.param(
        lambda: LookupError(f"unknown encoding: {SYNTHETIC_MARKER}"),
        "LookupError",
        id="lookup",
    ),
    pytest.param(lambda: ValueError(SYNTHETIC_MARKER), "ValueError", id="value"),
]


class TestStageErrorsKeepMailOutOfLastError:
    """``_stage_error`` keeps exception text only for types that cannot
    carry mail data; anything else is persisted, and logged by the
    queue, as its type name alone (#257)."""

    def _drain_one(self, tmp_path, monkeypatch, caplog, target: str, exc: BaseException):
        dest = tmp_path / "INBOX" / "cur" / "msg.eml"
        _write_eml(dest, "stage@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(dest), REASON_INITIAL_SCAN)

        def boom(*_a, **_kw):
            raise exc

        monkeypatch.setattr(main, target, boom)
        caplog.set_level(logging.DEBUG)
        _drain(queue, db, make_mock_embedder(_UNIT_VECTOR), Threader(db))
        return db._conn.execute(
            "SELECT last_stage, last_error FROM indexing_jobs WHERE filepath = ?", (str(dest),)
        ).fetchone()

    @pytest.mark.parametrize(("make_exc", "type_name"), _CONTENT_QUOTING_ERRORS)
    def test_parse_failure_persists_type_only(
        self, tmp_path, monkeypatch, caplog, make_exc, type_name
    ):
        row = self._drain_one(tmp_path, monkeypatch, caplog, "parse_email", make_exc())

        assert row["last_stage"] == "parse"
        assert row["last_error"] == type_name
        assert SYNTHETIC_MARKER not in caplog.text
        assert f"error={type_name}" in caplog.text

    @pytest.mark.parametrize(("make_exc", "type_name"), _CONTENT_QUOTING_ERRORS)
    def test_chunk_failure_persists_type_only(
        self, tmp_path, monkeypatch, caplog, make_exc, type_name
    ):
        row = self._drain_one(tmp_path, monkeypatch, caplog, "chunk_segments", make_exc())

        assert row["last_stage"] == "chunk"
        assert row["last_error"] == type_name
        assert SYNTHETIC_MARKER not in caplog.text
        assert f"error={type_name}" in caplog.text

    def test_os_error_keeps_errno_text_only(self, tmp_path, monkeypatch, caplog):
        """An ``OSError`` keeps its errno and the fixed ``os.strerror``
        text, which operators need to diagnose a file fault. Its message
        and filename are not kept: any library can raise ``OSError`` with
        arbitrary text, and the job row already records the path."""
        row = self._drain_one(
            tmp_path,
            monkeypatch,
            caplog,
            "chunk_segments",
            PermissionError(13, f"{SYNTHETIC_MARKER} denied", f"/x/{SYNTHETIC_MARKER}"),
        )

        assert row["last_stage"] == "chunk"
        assert row["last_error"] == f"PermissionError: [Errno 13] {os.strerror(13)}"
        assert SYNTHETIC_MARKER not in caplog.text

    @pytest.mark.parametrize(
        "exc",
        [
            OSError(SYNTHETIC_MARKER),
            OSError("not-an-errno", SYNTHETIC_MARKER),
        ],
    )
    def test_os_error_without_an_errno_is_type_only(self, exc):
        """Library code raises ``OSError`` with free text (no errno);
        that text can quote the attachment being read."""
        assert main._stage_error(exc) == "OSError"

    @pytest.mark.parametrize("errno", [2**31, -(2**31) - 1, 2**70])
    def test_os_error_with_errno_outside_c_int_is_type_only(self, errno):
        """``os.strerror`` raises ``OverflowError`` outside the C int
        range; that must not escape the stage handlers and stop the
        drain."""
        assert main._stage_error(OSError(errno, SYNTHETIC_MARKER)) == "OSError"

    def test_oversized_message_error_keeps_its_text(self):
        exc = parser.OversizedMessageError(Path("/maildir/INBOX/cur/x"), 20, 10)

        assert main._stage_error(exc) == f"OversizedMessageError: {exc}"

    def test_sqlite_error_is_type_only(self):
        """FTS5 query errors quote the bound query string ("no such
        column: <term>"), so sqlite text is not kept."""
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE f USING fts5(x)")
        with pytest.raises(sqlite3.OperationalError) as info:
            conn.execute("SELECT * FROM f WHERE f MATCH ?", (f"{SYNTHETIC_MARKER}: y",))
        assert SYNTHETIC_MARKER in str(info.value)

        assert main._stage_error(info.value) == "OperationalError"


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
        assert _chunk_ids(db, "utf8@example.com")
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
        _drain(queue, db, embedder, Threader(db))
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
        assert _chunk_ids(db, "malformed@example.com")
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
        assert _chunk_ids(db, "nested-labels@example.com")
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

    @pytest.mark.parametrize("header", ["From", "To"])
    def test_restored_encoded_word_cannot_reenter_a_reparser(self, tmp_path, monkeypatch, header):
        """An encoded-word whose label nests 2,400 parentheses parses as an
        opaque placeholder, but restoring it into the address re-created
        the paren bomb for the identity re-parse (and, via the from_addr
        fallback, for canonical_addr in the threader). Unsafe restored
        addresses are discarded; the message must always ingest."""
        value = "=?x" + "(" * 1200 + ")" * 1200 + "?q?bob@example.com?= (Bob)"
        other = "To: reader@example.com\r\n" if header == "From" else "From: alice@example.com\r\n"
        db, queue, participants = self._ingest_headers(
            tmp_path,
            monkeypatch,
            f"{header}: {value}\r\n{other}",
            "restored-bomb@example.com",
        )
        assert queue.stats() == {"queued": 0, "dead": 0}
        assert _chunk_ids(db, "restored-bomb@example.com")
        assert not any("(" in addr for _role, addr, _name in participants)

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
        assert _chunk_ids(db, message_id), "body must be indexed"
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
                ReconcilerConfig(
                    enabled=True,
                    grace_days=7,
                    sweep_interval_secs=60,
                    max_batch_pct=1.0,
                    force=False,
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
            ReconcilerConfig(
                enabled=True,
                grace_days=0,
                sweep_interval_secs=60,
                max_batch_pct=1.0,
                force=False,
            ),
            maildir_root=maildir,
        )
        reconciler.sweep()
        reconciler.reap()

        assert db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert db._conn.execute("SELECT COUNT(*) FROM message_participants").fetchone()[0] == 0


class TestBatchSharesExtraction:
    """#237: identical attachment bytes prepared in one batch were
    extracted once per occurrence, because each occurrence consulted only
    the committed cache, and copies inside one message queued their
    identical chunks for embedding once per copy."""

    PAYLOAD = b"SHARED_ATTACHMENT_MARKER quarterly figures"

    def _write(self, path: Path, message_id: str, filenames: list[str]) -> None:
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = "alice@example.com"
        msg["To"] = "bob@example.com"
        msg["Subject"] = f"Report {message_id}"
        msg["Message-ID"] = f"<{message_id}>"
        msg["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"
        msg.set_content(f"Body of {message_id}.")
        for filename in filenames:
            maintype, subtype = (
                ("application", "octet-stream") if filename.endswith(".bin") else ("text", "plain")
            )
            msg.add_attachment(self.PAYLOAD, maintype=maintype, subtype=subtype, filename=filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(msg))

    def _drain(self, tmp_path, monkeypatch, messages: dict[str, list[str]], embedder=None):
        from src import attachment_indexing
        from src.extractors import extract

        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        for message_id, filenames in messages.items():
            path = maildir / "INBOX" / "cur" / f"{message_id}.eml"
            self._write(path, message_id, filenames)
            queue.enqueue(str(path), REASON_INITIAL_SCAN)
        calls: list[str] = []

        def counting_extract(**kwargs):
            calls.append(kwargs["filename"])
            return extract(**kwargs)

        monkeypatch.setattr(attachment_indexing, "extract_attachment", counting_extract)
        embedder = embedder or make_mock_embedder(_UNIT_VECTOR)
        main._drain_queue_batched(
            db,
            embedder,
            Threader(db),
            queue,
            batch_size=10,
            timing_aggregator=main.TimingAggregator(window=4),
            max_passes=1,
        )
        embedded = [t for call in embedder.embed_batch.call_args_list for t in call.args[0]]
        return db, calls, embedded

    def _attachment_chunks(self, db) -> list[tuple[str, str]]:
        rows = db._conn.execute(
            "SELECT m.message_id, c.text FROM message_chunks c "
            "JOIN message_thread_map m ON m.claimant_id = c.claimant_id "
            "WHERE c.attachment_id IS NOT NULL"
        ).fetchall()
        return sorted((r["message_id"], r["text"]) for r in rows)

    def test_same_bytes_across_messages_extract_once(self, tmp_path, monkeypatch):
        messages = {f"m{i}@example.com": [f"report{i}.txt"] for i in range(5)}
        db, calls, _ = self._drain(tmp_path, monkeypatch, messages)
        assert len(calls) == 1
        chunks = self._attachment_chunks(db)
        assert [mid for mid, _ in chunks] == sorted(messages)
        assert all("SHARED_ATTACHMENT_MARKER" in text for _, text in chunks)
        assert db._conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 5
        assert db._conn.execute("SELECT COUNT(*) FROM attachment_extractions").fetchone()[0] == 1

    def test_copies_in_one_message_extract_and_embed_once(self, tmp_path, monkeypatch):
        db, calls, embedded = self._drain(
            tmp_path, monkeypatch, {"m@example.com": ["scan0.txt", "scan1.txt"]}
        )
        assert len(calls) == 1
        assert sum("SHARED_ATTACHMENT_MARKER" in t for t in embedded) == 1
        assert len(self._attachment_chunks(db)) == 1
        assert db._conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 2

    def test_copies_in_one_message_embed_once_when_isolated(self, tmp_path, monkeypatch):
        # The bulk embed fails, the probe passes, and each message is
        # embedded on its own: the shared slot is still embedded once.
        embedder = make_mock_embedder(_UNIT_VECTOR)
        delegate = embedder.embed_batch.side_effect

        def fail_first(texts, **kwargs):
            if embedder.embed_batch.call_count == 1:
                raise RuntimeError("batch rejected")
            return delegate(texts, **kwargs)

        embedder.embed_batch.side_effect = fail_first
        db, _, embedded = self._drain(
            tmp_path,
            monkeypatch,
            {"m@example.com": ["scan0.txt", "scan1.txt"]},
            embedder=embedder,
        )
        isolated = [t for call in embedder.embed_batch.call_args_list[1:] for t in call.args[0]]
        assert sum("SHARED_ATTACHMENT_MARKER" in t for t in isolated) == 1
        assert len(self._attachment_chunks(db)) == 1

    def test_no_text_copy_does_not_clear_a_filled_copy(self, tmp_path, monkeypatch):
        """Review round 2: re-indexing a message whose ``.bin`` copy still
        reads the cached ``unsupported`` row while its ``.txt`` copy of the
        same bytes extracts. The ``.bin`` plan cleared the shared chunk
        slice, and the ``.txt`` plan, having embedded nothing because its
        chunks were already stored, then failed to restore it."""
        from src.extractors import STATUS_UNSUPPORTED

        messages = {"m@example.com": ["blob.bin", "doc.txt"]}
        db, _, _ = self._drain(tmp_path, monkeypatch, messages)
        assert len(self._attachment_chunks(db)) == 1
        content_hash = db._conn.execute(
            "SELECT attachment_id FROM attachment_extractions"
        ).fetchone()["attachment_id"]
        db.store_attachment_extraction(
            attachment_id=content_hash,
            extraction_status=STATUS_UNSUPPORTED,
            extractor=None,
            extracted_text=None,
            extraction_error="no extractor for content_type='application/octet-stream'",
        )
        queue = _make_queue(db)
        path = tmp_path / "maildir" / "INBOX" / "cur" / "m@example.com.eml"
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        main._drain_queue_batched(
            db,
            make_mock_embedder(_UNIT_VECTOR),
            Threader(db),
            queue,
            batch_size=10,
            timing_aggregator=main.TimingAggregator(window=4),
            max_passes=1,
        )
        # A succeeded job's row is deleted; a failed one stays queued.
        assert db._conn.execute("SELECT COUNT(*) FROM indexing_jobs").fetchone()[0] == 0
        assert len(self._attachment_chunks(db)) == 1


class TestTrashedFilesWithReconciliation:
    """#244: with deletion reconciliation on, a T-flagged file is deleted
    upstream; stale or in-flight work must not index it back."""

    def test_drain_drops_a_trashed_job(self, tmp_path, monkeypatch):
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        path = maildir / "INBOX" / "cur" / "1700000000.M1.host:2,ST"
        _write_eml(path, "trashed@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        main._drain_queue_batched(
            db,
            make_mock_embedder(_UNIT_VECTOR),
            Threader(db),
            queue,
            batch_size=10,
            timing_aggregator=main.TimingAggregator(window=4),
            max_passes=1,
            skip_trashed=True,
        )
        assert not db.is_indexed(str(path))
        assert not queue.has_pending_row(str(path))

    def test_a_tombstoned_message_keeps_its_job_until_restored(self, tmp_path, monkeypatch):
        """Review round 1: dropping the job of an indexed message lost its
        only retry. If mbsync clears the T flag before the reap, nothing
        re-queues a message whose thread already has chunks. The job is
        parked instead, moves with the rename back, and then runs."""
        from src.reconciler import Reconciler, ReconcilerConfig

        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        live = maildir / "INBOX" / "cur" / "1700000000.M1.host:2,S"
        trashed = maildir / "INBOX" / "cur" / "1700000000.M1.host:2,ST"
        _write_eml(live, "parked@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)

        def drain():
            main._drain_queue_batched(
                db,
                make_mock_embedder(_UNIT_VECTOR),
                Threader(db),
                queue,
                batch_size=10,
                timing_aggregator=main.TimingAggregator(window=4),
                max_passes=1,
                skip_trashed=True,
            )

        reconciler = Reconciler(
            db,
            make_mock_embedder(_UNIT_VECTOR),
            ReconcilerConfig(
                enabled=True,
                grace_days=7,
                sweep_interval_secs=60,
                max_batch_pct=1.0,
                force=False,
            ),
        )
        queue.enqueue(str(live), REASON_INITIAL_SCAN)
        drain()
        live.rename(trashed)
        reconciler.handle_moved(str(live), str(trashed))  # tombstones it
        queue.enqueue(str(trashed), REASON_INITIAL_SCAN)  # e.g. a retry still pending

        drain()
        job = db._conn.execute("SELECT filepath, attempts FROM indexing_jobs").fetchone()
        assert (job["filepath"], job["attempts"]) == (str(trashed), 0)

        # Restoring clears the tombstone, moves the job, and makes it due
        # now rather than after the park delay.
        trashed.rename(live)
        reconciler.handle_moved(str(trashed), str(live))
        drain()
        assert not queue.has_pending_row(str(live))
        assert db.is_indexed(str(live))

    def test_a_parked_job_moved_without_a_tombstone_runs_at_once(self, tmp_path, monkeypatch):
        """Review round 3: at startup ``sweep_paths`` can move a job onto a
        T path and the drain park it before any tombstone exists, so a
        restore has no tombstone to clear. Moving a parked job off the
        path makes it due whatever the tombstone state."""
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        live = maildir / "INBOX" / "cur" / "1700000000.M1.host:2,S"
        trashed = maildir / "INBOX" / "cur" / "1700000000.M1.host:2,ST"
        _write_eml(live, "startup@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)

        def drain():
            main._drain_queue_batched(
                db,
                make_mock_embedder(_UNIT_VECTOR),
                Threader(db),
                queue,
                batch_size=10,
                timing_aggregator=main.TimingAggregator(window=4),
                max_passes=1,
                skip_trashed=True,
            )

        queue.enqueue(str(live), REASON_INITIAL_SCAN)
        drain()
        live.rename(trashed)
        db.update_filepath(str(live), str(trashed))
        queue.enqueue(str(trashed), REASON_INITIAL_SCAN)
        drain()  # parked
        assert queue.has_pending_row(str(trashed))
        assert not db.has_pending_deletion(str(trashed))

        trashed.rename(live)
        db.update_filepath(str(trashed), str(live))
        drain()
        assert not queue.has_pending_row(str(live))

    def test_drain_indexes_a_trashed_file_without_reconciliation(self, tmp_path, monkeypatch):
        # The index is append-only then: trashed mail is indexed as before.
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        path = maildir / "INBOX" / "cur" / "1700000000.M1.host:2,ST"
        _write_eml(path, "trashed@example.com")
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        queue.enqueue(str(path), REASON_INITIAL_SCAN)
        main._drain_queue_batched(
            db,
            make_mock_embedder(_UNIT_VECTOR),
            Threader(db),
            queue,
            batch_size=10,
            timing_aggregator=main.TimingAggregator(window=4),
            max_passes=1,
        )
        assert db.is_indexed(str(path))

    def test_recovery_skips_trashed_files(self, tmp_path, monkeypatch):
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        trashed = str(tmp_path / "INBOX" / "cur" / "1.M1.host:2,ST")
        live = str(tmp_path / "INBOX" / "cur" / "2.M2.host:2,S")
        monkeypatch.setattr(
            db, "find_zero_vector_chunkless_thread_filepaths", lambda: [trashed, live]
        )
        assert main._recover_zero_vector_threads(db, queue, skip_trashed=True) == 1
        assert not queue.has_pending_row(trashed)
        assert queue.has_pending_row(live)


class TestReplySubjectSearchable:
    """#303: a reply whose subject differs from the thread's (after
    Re:/Fwd: normalization) must be findable by keyword and semantic
    search. Review round 1: stored chunks stay body-only (they are the
    authoritative body store), and the keyword path must survive a
    thread body already at its token cap."""

    TOKEN = "APPROVALZX731"
    CHANGED_SUBJECT = f"Re: Budget review {TOKEN}"

    def _write_thread(self, tmp_path, *, root_body=None, changed_body=None):
        root = tmp_path / "INBOX" / "cur" / "root.eml"
        changed = tmp_path / "INBOX" / "cur" / "changed.eml"
        same = tmp_path / "INBOX" / "cur" / "same.eml"
        _write_eml(root, "root@example.com", "Budget review", body=root_body)
        _write_eml(
            changed,
            "changed@example.com",
            self.CHANGED_SUBJECT,
            in_reply_to="root@example.com",
            references=["root@example.com"],
            date="Mon, 01 Jan 2024 13:00:00 +0000",
            body=changed_body,
        )
        _write_eml(
            same,
            "same@example.com",
            "RE: Fwd: budget  review",
            in_reply_to="changed@example.com",
            references=["root@example.com", "changed@example.com"],
            date="Mon, 01 Jan 2024 14:00:00 +0000",
        )
        return root, changed, same

    def _index_thread(self, tmp_path, **bodies):
        paths = self._write_thread(tmp_path, **bodies)
        db = Database(tmp_path / "mail.db")
        embedder = make_mock_embedder(_UNIT_VECTOR)
        threader = Threader(db)
        for path in paths:
            assert _index_one(path, db, embedder, threader)[0]
        inputs = [t for call in embedder.embed_batch.call_args_list for t in call.args[0]]
        return db, embedder, threader, paths, inputs

    @staticmethod
    def _thread_fts_hits(db, term):
        return {
            r[0]
            for r in db._conn.execute(
                "SELECT t.thread_id FROM threads_fts f JOIN threads t "
                "ON t.fts_rowid = f.rowid WHERE threads_fts MATCH ?",
                (term,),
            )
        }

    @staticmethod
    def _chunk_fts_hits(db, term):
        return {
            r[0]
            for r in db._conn.execute(
                "SELECT m.message_id FROM message_chunks_fts f JOIN message_chunks c "
                "ON c.fts_rowid = f.rowid JOIN message_thread_map m "
                "ON m.claimant_id = c.claimant_id WHERE message_chunks_fts MATCH ?",
                (term,),
            )
        }

    def test_reply_subject_matches_thread_fts_only(self, tmp_path):
        db, *_ = self._index_thread(tmp_path)
        assert self._thread_fts_hits(db, self.TOKEN) == {"root@example.com"}
        # Chunk FTS backs the body-only ``query_messages(text=...)``.
        assert self._chunk_fts_hits(db, self.TOKEN) == set()
        row = db._conn.execute(
            "SELECT subject, body_text FROM threads WHERE thread_id = ?", ("root@example.com",)
        ).fetchone()
        assert row["subject"] == "budget review"
        assert self.TOKEN not in row["body_text"]

    def test_stored_chunks_stay_body_only(self, tmp_path):
        from src.chunker import chunk_message

        db, _, _, (_root, changed, _same), _ = self._index_thread(tmp_path)
        rows = db._conn.execute(
            "SELECT chunk_id, text, char_start FROM message_chunks WHERE "
            "claimant_id IN (SELECT claimant_id FROM message_thread_map WHERE message_id = ?)",
            ("changed@example.com",),
        ).fetchall()
        parsed = parser.parse_email(changed)
        assert parsed is not None
        expected = chunk_message(
            message_pk=parsed.claimant_id,
            body_text="Body of changed@example.com.",
            target_tokens=main.CHUNK_TARGET_TOKENS,
            max_tokens=main.CHUNK_MAX_TOKENS,
            overlap_tokens=main.CHUNK_OVERLAP_TOKENS,
        )
        assert [(r["chunk_id"], r["text"], r["char_start"]) for r in rows] == [
            (c.chunk_id, c.text, c.char_start) for c in expected
        ]
        assert not any("Subject:" in r["text"] for r in rows)

    def test_first_chunk_embed_input_carries_the_subject(self, tmp_path):
        paragraph = " ".join(f"word{i}" for i in range(300))
        changed_body = "\n\n".join([paragraph] * 3)
        db, *_, inputs = self._index_thread(tmp_path, changed_body=changed_body)
        texts = [
            r[0]
            for r in db._conn.execute(
                "SELECT text FROM message_chunks WHERE "
                "claimant_id IN (SELECT claimant_id FROM message_thread_map WHERE message_id = ?) ORDER BY chunk_index",
                ("changed@example.com",),
            )
        ]
        assert len(texts) > 1
        prefix = f"Subject: {self.CHANGED_SUBJECT}\n\n"
        assert prefix + texts[0] in inputs
        for text in texts[1:]:
            assert text in inputs
        assert [t for t in inputs if self.TOKEN in t] == [prefix + texts[0]]

    def test_unchanged_subjects_reach_the_embed_input_too(self, tmp_path):
        """#687: the root and a reply keeping the thread subject carry
        their own subject as well, so a topic named only in the subject
        is in a vector. Stored chunks stay body-only."""
        db, *_, inputs = self._index_thread(tmp_path)
        assert "Subject: Budget review\n\nBody of root@example.com." in inputs
        assert "Subject: RE: Fwd: budget  review\n\nBody of same@example.com." in inputs
        assert "Body of root@example.com." not in inputs
        stored = {r[0] for r in db._conn.execute("SELECT text FROM message_chunks")}
        assert not any("Subject:" in t for t in stored)

    def test_single_message_thread_embeds_its_subject(self, tmp_path):
        path = tmp_path / "INBOX" / "cur" / "solo.eml"
        _write_eml(path, "solo@example.com", "Re: Dana leaving", body="Thanks, will pass it on.")
        db = Database(tmp_path / "mail.db")
        embedder = make_mock_embedder(_UNIT_VECTOR)
        assert _index_one(path, db, embedder, Threader(db))[0]
        inputs = [t for call in embedder.embed_batch.call_args_list for t in call.args[0]]
        assert inputs == ["Subject: Re: Dana leaving\n\nThanks, will pass it on."]

    def test_blank_subject_adds_no_prefix(self, tmp_path):
        path = tmp_path / "INBOX" / "cur" / "blank.eml"
        _write_eml(path, "blank@example.com", "  ", body="Body only.")
        db = Database(tmp_path / "mail.db")
        embedder = make_mock_embedder(_UNIT_VECTOR)
        assert _index_one(path, db, embedder, Threader(db))[0]
        inputs = [t for call in embedder.embed_batch.call_args_list for t in call.args[0]]
        assert inputs == ["Body only."]

    def test_missing_subject_header_adds_no_prefix(self, tmp_path):
        """The parser fills an absent Subject with the "(no subject)"
        placeholder; it must not reach the vectors as if it were text."""
        path = tmp_path / "INBOX" / "cur" / "headerless.eml"
        path.parent.mkdir(parents=True)
        path.write_text(
            "From: alice@example.com\r\nTo: bob@example.com\r\n"
            "Message-ID: <headerless@example.com>\r\n"
            "Date: Mon, 01 Jan 2024 12:00:00 +0000\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\nBody only.\r\n",
            encoding="utf-8",
        )
        db = Database(tmp_path / "mail.db")
        embedder = make_mock_embedder(_UNIT_VECTOR)
        assert _index_one(path, db, embedder, Threader(db))[0]
        inputs = [t for call in embedder.embed_batch.call_args_list for t in call.args[0]]
        assert inputs == ["Body only."]

    @pytest.mark.parametrize("subject", ["(no subject)", "Re: (No Subject)", "Fwd:  "])
    def test_placeholder_or_prefix_only_subject_adds_no_prefix(self, tmp_path, subject):
        """A literal "(no subject)" cannot be told apart from the parser's
        placeholder, so it is treated as absent too, as is a reply to a
        subjectless message or a subject that is only a prefix."""
        path = tmp_path / "INBOX" / "cur" / "placeholder.eml"
        _write_eml(path, "placeholder@example.com", subject, body="Body only.")
        db = Database(tmp_path / "mail.db")
        embedder = make_mock_embedder(_UNIT_VECTOR)
        assert _index_one(path, db, embedder, Threader(db))[0]
        inputs = [t for call in embedder.embed_batch.call_args_list for t in call.args[0]]
        assert inputs == ["Body only."]

    def test_reindexing_the_reply_embeds_nothing_new(self, tmp_path):
        db, embedder, threader, (_root, changed, _same), _ = self._index_thread(tmp_path)
        before = embedder.embed_batch.call_count
        assert _index_one(changed, db, embedder, threader)[0]
        new_inputs = [
            t for call in embedder.embed_batch.call_args_list[before:] for t in call.args[0]
        ]
        assert new_inputs == []
        assert self._thread_fts_hits(db, self.TOKEN) == {"root@example.com"}

    def test_reply_subject_matches_when_body_is_at_the_cap(self, tmp_path):
        """The thread body is prefix-preserving and token-capped, so a
        reply arriving after it is full adds nothing to ``body_text``;
        its changed subject must still reach ``threads_fts``."""
        from src.chunker import estimate_tokens
        from src.threader import THREAD_BODY_TEXT_MAX_TOKENS

        filler = " ".join(f"f{i}" for i in range(600))
        paths = [tmp_path / "INBOX" / "cur" / "root.eml"]
        _write_eml(paths[0], "root@example.com", "Budget review", body=filler)
        for n in range(12):
            paths.append(tmp_path / "INBOX" / "cur" / f"fill{n}.eml")
            _write_eml(
                paths[-1],
                f"fill{n}@example.com",
                "Re: Budget review",
                in_reply_to="root@example.com",
                references=["root@example.com"],
                date=f"Mon, 01 Jan 2024 12:{n + 10}:00 +0000",
                body=filler,
            )
        paths.append(tmp_path / "INBOX" / "cur" / "changed.eml")
        _write_eml(
            paths[-1],
            "changed@example.com",
            self.CHANGED_SUBJECT,
            in_reply_to="root@example.com",
            references=["root@example.com"],
            date="Mon, 01 Jan 2024 13:00:00 +0000",
        )
        db = Database(tmp_path / "mail.db")
        embedder = make_mock_embedder(_UNIT_VECTOR)
        threader = Threader(db)
        for path in paths:
            assert _index_one(path, db, embedder, threader)[0]
        body = db._conn.execute(
            "SELECT body_text FROM threads WHERE thread_id = ?", ("root@example.com",)
        ).fetchone()[0]
        assert estimate_tokens(body) >= THREAD_BODY_TEXT_MAX_TOKENS - 50
        assert "Body of changed@example.com." not in body
        assert self._thread_fts_hits(db, self.TOKEN) == {"root@example.com"}

    def test_embed_input_stays_within_the_chunk_ceiling(self, tmp_path):
        """Review round 2: the subject prefix must not push a chunk's
        embedding input past ``CHUNK_MAX_TOKENS``. A 50,000-character
        subject is cut to fit; the stored chunk stays body-only."""
        from src.chunker import estimate_tokens

        huge = "Re: Budget review " + " ".join(f"s{i}" for i in range(10_000))
        assert len(huge) > 50_000
        paths = self._write_thread(tmp_path)
        _write_eml(
            paths[1],
            "changed@example.com",
            huge,
            in_reply_to="root@example.com",
            references=["root@example.com"],
            date="Mon, 01 Jan 2024 13:00:00 +0000",
        )
        db = Database(tmp_path / "mail.db")
        embedder = make_mock_embedder(_UNIT_VECTOR)
        threader = Threader(db)
        for path in paths:
            assert _index_one(path, db, embedder, threader)[0]
        inputs = [t for call in embedder.embed_batch.call_args_list for t in call.args[0]]
        assert all(estimate_tokens(t) <= main.CHUNK_MAX_TOKENS for t in inputs)
        first = next(t for t in inputs if t.endswith("Body of changed@example.com."))
        assert first.startswith("Subject: Re: Budget review s0 s1")
        stored = db._conn.execute(
            "SELECT text FROM message_chunks WHERE "
            "claimant_id IN (SELECT claimant_id FROM message_thread_map WHERE message_id = ?)",
            ("changed@example.com",),
        ).fetchone()[0]
        assert stored == "Body of changed@example.com."

    def test_prefix_is_cut_or_dropped_near_the_ceiling(self, monkeypatch):
        from src.chunker import estimate_tokens, truncate_to_tokens

        monkeypatch.setattr(main, "CHUNK_MAX_TOKENS", 120)
        subject_line = "Subject: " + " ".join(f"s{i}" for i in range(20_000))
        near = " ".join(f"w{i}" for i in range(40))
        assert 100 < estimate_tokens(near) < 115
        text = main._chunk_embed_input(subject_line, near)
        assert text.startswith("Subject: s0") and text.endswith("\n\n" + near)
        assert estimate_tokens(text) <= 120

        full = " ".join(f"w{i}" for i in range(80))
        full = truncate_to_tokens(full, 120)
        assert estimate_tokens(full) == 120
        assert main._chunk_embed_input(subject_line, full) == full


class TestMessageIdClaimants:
    """#217: two files claiming one Message-ID with different content are
    both kept, each under its own claimant ID (the Message-ID plus a
    short hash of the file's bytes). The second used to overwrite the
    first's message record, source locator, participants and chunks,
    while the thread body kept the first's text and its file stayed
    marked indexed."""

    MID = "dup@example.com"

    def _setup(self, tmp_path, monkeypatch):
        maildir = tmp_path / "maildir"
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        inbox = maildir / "INBOX" / "cur"
        first = inbox / "1700000000.M1.host:2,S"
        second = inbox / "1700000001.M1.host:2,S"
        _write_eml_with_text_attachment(first, self.MID, body="Original wording alphaword.")
        _write_eml_with_text_attachment(second, self.MID, body="Replacement wording betaword.")
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        embedder = make_mock_embedder(_UNIT_VECTOR)
        return maildir, inbox, first, second, db, threader, embedder

    def _index(self, db, threader, embedder, *paths):
        queue = _make_queue(db)
        # One file per drain: the arrival order is part of the shape.
        for path in paths:
            queue.enqueue(str(path), REASON_INITIAL_SCAN)
            _drain(queue, db, embedder, threader, batch_size=1)

    @staticmethod
    def _claimant(path: Path) -> str:
        msg = parser.parse_email(path)
        assert msg is not None
        return msg.claimant_id

    @staticmethod
    def _state(db) -> dict[str, list[tuple]]:
        """Every per-message row, for before/after comparisons."""

        def rows(sql: str) -> list[tuple]:
            return sorted(tuple(r) for r in db._conn.execute(sql))

        return {
            "messages": rows("SELECT claimant_id, message_id, filepath FROM messages"),
            "map": rows("SELECT claimant_id, message_id, filepath FROM message_thread_map"),
            "chunks": rows("SELECT claimant_id, chunk_id, attachment_id FROM message_chunks"),
            "attachments": rows("SELECT attachment_occurrence_id, claimant_id FROM attachments"),
            "participants": rows("SELECT claimant_id, role, address FROM message_participants"),
            "threads": rows("SELECT thread_id, message_ids, body_text FROM threads"),
        }

    def _chunk_text(self, db, claimant: str, *, attachment: bool) -> str:
        op = "IS NOT NULL" if attachment else "IS NULL"
        return " ".join(
            r["text"]
            for r in db._conn.execute(
                f"SELECT text FROM message_chunks WHERE claimant_id = ? AND attachment_id {op}",
                (claimant,),
            )
        )

    def test_both_claimants_are_indexed_and_kept_apart(self, tmp_path, monkeypatch):
        _, _, first, second, db, threader, embedder = self._setup(tmp_path, monkeypatch)
        self._index(db, threader, embedder, first, second)

        a, b = self._claimant(first), self._claimant(second)
        assert a != b
        assert a.startswith(self.MID + "#") and b.startswith(self.MID + "#")
        rows = db._conn.execute(
            "SELECT claimant_id, filepath FROM messages WHERE message_id = ?", (self.MID,)
        ).fetchall()
        assert {(r["claimant_id"], r["filepath"]) for r in rows} == {
            (a, str(first)),
            (b, str(second)),
        }
        assert db.is_indexed(str(first)) and db.is_indexed(str(second))

        body_a = self._chunk_text(db, a, attachment=False)
        body_b = self._chunk_text(db, b, attachment=False)
        assert "alphaword" in body_a and "betaword" not in body_a
        assert "betaword" in body_b and "alphaword" not in body_b
        # Each claimant owns its own occurrence and chunks of the
        # attachment both carry.
        assert "attachment text" in self._chunk_text(db, a, attachment=True)
        assert "attachment text" in self._chunk_text(db, b, attachment=True)
        occurrences = db._conn.execute("SELECT claimant_id FROM attachments").fetchall()
        assert sorted(r["claimant_id"] for r in occurrences) == sorted([a, b])

        # One thread (membership stays keyed by Message-ID) listing both
        # claimants, its body carrying both texts, so coarse and precise
        # retrieval agree.
        threads = db._conn.execute("SELECT message_ids, body_text FROM threads").fetchall()
        assert len(threads) == 1
        assert sorted(json.loads(threads[0]["message_ids"])) == sorted([a, b])
        assert "alphaword" in threads[0]["body_text"]
        assert "betaword" in threads[0]["body_text"]

    def test_reprocessing_is_idempotent(self, tmp_path, monkeypatch):
        _, _, first, second, db, threader, embedder = self._setup(tmp_path, monkeypatch)
        self._index(db, threader, embedder, first, second)
        before = self._state(db)
        embedder.embed_batch.reset_mock()

        self._index(db, threader, embedder, second, first)

        assert self._state(db) == before
        # Every chunk ID was already stored, so nothing is re-embedded.
        embedded = [t for call in embedder.embed_batch.call_args_list for t in call.args[0]]
        assert embedded == []

    def test_reaping_one_claimant_leaves_the_other_intact(self, tmp_path, monkeypatch):
        from src.reconciler import Reconciler, ReconcilerConfig

        maildir, inbox, first, second, db, threader, embedder = self._setup(tmp_path, monkeypatch)
        self._index(db, threader, embedder, first, second)
        a, b = self._claimant(first), self._claimant(second)
        survivor_before = {
            table: [row for row in rows if b in row] for table, rows in self._state(db).items()
        }

        first.rename(inbox / "1700000000.M1.host:2,ST")
        reconciler = Reconciler(
            db,
            embedder,
            ReconcilerConfig(
                enabled=True,
                grace_days=0,
                sweep_interval_secs=60,
                max_batch_pct=1.0,
                force=False,
            ),
            maildir_root=maildir,
        )
        reconciler.sweep()
        reconciler.reap()

        after = self._state(db)
        for table in ("messages", "map", "chunks", "attachments", "participants"):
            assert not [row for row in after[table] if a in row], table
            assert [row for row in after[table] if b in row] == survivor_before[table], table
        (thread,) = after["threads"]
        assert json.loads(thread[1]) == [b]
        assert "betaword" in thread[2] and "alphaword" not in thread[2]
        assert db.is_indexed(str(second))


class TestReapLeavesNoContent:
    """PLAN Phase 4 item 4: a reap keeps an identifier-only record so a
    cited source can be reported as reaped. Nothing about the
    reaped message's content may survive it: no subject, body or
    participant text in any table, including that record."""

    _SUBJECT = "Zqxsubjectmarker quarterly"
    _BODY = "Zqxbodymarker paragraph text."

    def _reconciler(self, db, embedder, maildir):
        from src.reconciler import Reconciler, ReconcilerConfig

        return Reconciler(
            db,
            embedder,
            ReconcilerConfig(
                enabled=True,
                grace_days=0,
                sweep_interval_secs=60,
                max_batch_pct=1.0,
                force=False,
            ),
            maildir_root=maildir,
        )

    @staticmethod
    def _tables_holding(db, needles: list[str]) -> set[str]:
        """Every regular table (FTS / vec shadow tables included) with a
        text or blob value containing one of ``needles``, case-folded."""
        found: set[str] = set()
        tables = [
            r[0]
            for r in db._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND sql NOT LIKE 'CREATE VIRTUAL%'"
            )
        ]
        for table in tables:
            for row in db._conn.execute(f'SELECT * FROM "{table}"'):  # nosec B608
                for value in row:
                    if isinstance(value, bytes):
                        text = value.decode("latin-1").lower()
                    elif isinstance(value, str):
                        text = value.lower()
                    else:
                        continue
                    if any(n.lower() in text for n in needles):
                        found.add(table)
        return found

    def _index(self, tmp_path, monkeypatch, *, with_reply: bool):
        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        root = inbox / "1700000000.M1.host:2,S"
        _write_eml(
            root,
            "zqxreaped@example.com",
            self._SUBJECT,
            body=self._BODY,
            from_addr="Zqxsendermarker <zqxsender@example.com>",
        )
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)
        db = Database(tmp_path / "mail.db")
        threader = Threader(db)
        embedder = make_mock_embedder(_UNIT_VECTOR)
        queue = _make_queue(db)
        main.initial_index(db, embedder, threader, queue)
        if with_reply:
            # Indexed after the root, so the walk order (filesystem
            # dependent) cannot thread the reply on its own.
            _write_eml(
                inbox / "1700000001.M2.host:2,S",
                "survivor@example.com",
                "Re: " + self._SUBJECT,
                in_reply_to="zqxreaped@example.com",
                date="Tue, 02 Jan 2024 12:00:00 +0000",
                body="The surviving reply.",
            )
            main.initial_index(db, embedder, threader, queue)
            assert db._conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1
        assert {"threads", "message_chunks"} <= self._tables_holding(db, ["zqxbodymarker"])
        root.rename(inbox / "1700000000.M1.host:2,ST")
        return db, self._reconciler(db, embedder, maildir)

    def test_full_reap_leaves_only_identifiers(self, tmp_path, monkeypatch):
        db, reconciler = self._index(tmp_path, monkeypatch, with_reply=False)
        reconciler.sweep()
        assert reconciler.reap()["threads_reaped"] == 1

        needles = ["zqxsubjectmarker", "zqxbodymarker", "zqxsendermarker"]
        assert self._tables_holding(db, needles) == set()
        assert db._conn.execute("SELECT COUNT(*) FROM reaped_messages").fetchone()[0] == 1

    def test_partial_reap_leaves_only_identifiers(self, tmp_path, monkeypatch):
        db, reconciler = self._index(tmp_path, monkeypatch, with_reply=True)
        reconciler.sweep()
        assert reconciler.reap()["threads_rebuilt"] == 1

        # The survivor's own subject repeats the subject marker; the
        # reaped body and sender name appear nowhere.
        assert self._tables_holding(db, ["zqxbodymarker", "zqxsendermarker"]) == set()
        assert db._conn.execute("SELECT COUNT(*) FROM reaped_messages").fetchone()[0] == 1


class TestPruneReapedRecords:
    def test_prunes_expired_records(self, tmp_path, caplog):
        db = Database(tmp_path / "mail.db")
        with db.transaction():
            db._conn.execute(
                "INSERT INTO reaped_messages VALUES ('a#1', 'a', 't', '2000-01-01T00:00:00+00:00')"
            )
        with caplog.at_level(logging.INFO):
            main._prune_reaped_records(db)
        assert db._conn.execute("SELECT COUNT(*) FROM reaped_messages").fetchone()[0] == 0
        assert "pruned 1 expired reaped-message record(s)" in caplog.text

    def test_failure_is_logged_by_type(self, tmp_path, caplog, monkeypatch):
        db = Database(tmp_path / "mail.db")

        def boom(**_kw):
            raise sqlite3.OperationalError("zqxmarker")

        monkeypatch.setattr(db, "prune_reaped_messages", boom)
        main._prune_reaped_records(db)
        assert "reaped-record prune failed: OperationalError" in caplog.text
        assert "zqxmarker" not in caplog.text

    def test_startup_prunes_before_the_embedder_wait(self, tmp_path, monkeypatch):
        """#576: an embedder that never answers holds ``main`` in
        ``wait_for_ready`` before the initial index, so the startup prune
        must run first or expired records outlive the retention window."""
        db = Database(tmp_path / "mail.db")
        with db.transaction():
            db._conn.execute(
                "INSERT INTO reaped_messages VALUES ('a#1', 'a', 't', '2000-01-01T00:00:00+00:00')"
            )

        class _Unreachable(Exception):
            pass

        def never_ready():
            raise _Unreachable

        embedder = make_mock_embedder()
        embedder.wait_for_ready = never_ready
        embedder.base_url = "http://host.docker.internal:8001/v1"
        monkeypatch.setattr(main, "_validate_embed_config", lambda: None)
        monkeypatch.setattr(main, "Database", lambda path: db)
        monkeypatch.setattr(main, "OpenAIEmbedder", lambda **kw: embedder)
        monkeypatch.setattr(main, "touch_health_file", lambda: None)
        monkeypatch.setattr(main, "StallGuard", MagicMock())
        monkeypatch.setattr(main, "initial_index", lambda *a, **kw: pytest.fail("indexed"))
        with pytest.raises(_Unreachable):
            main.main()
        assert db._conn.execute("SELECT COUNT(*) FROM reaped_messages").fetchone()[0] == 0

    def test_startup_scrubs_before_the_embedder_wait(self, tmp_path, monkeypatch):
        """#670: a reap whose scrub the last run did not reach is covered
        by the scrub every opened database owes. It needs only the
        database, so it runs before an embedder that never answers can
        hold ``main`` in ``wait_for_ready``."""
        db = Database(tmp_path / "mail.db")
        scrubbed: list[list[str]] = []
        real_scrub = db.scrub_reaped_fts

        def spy():
            scrubbed.append(real_scrub())
            return scrubbed[-1]

        monkeypatch.setattr(db, "scrub_reaped_fts", spy)

        class _Unreachable(Exception):
            pass

        def never_ready():
            raise _Unreachable

        embedder = make_mock_embedder()
        embedder.wait_for_ready = never_ready
        embedder.base_url = "http://host.docker.internal:8001/v1"
        monkeypatch.setattr(main, "_validate_embed_config", lambda: None)
        monkeypatch.setattr(main, "Database", lambda path: db)
        monkeypatch.setattr(main, "OpenAIEmbedder", lambda **kw: embedder)
        monkeypatch.setattr(main, "touch_health_file", lambda: None)
        monkeypatch.setattr(main, "StallGuard", MagicMock())
        monkeypatch.setattr(main, "initial_index", lambda *a, **kw: pytest.fail("indexed"))
        with pytest.raises(_Unreachable):
            main.main()
        assert scrubbed == [["threads_fts", "message_chunks_fts", "attachments_fts"]]


class TestChunkKinds:
    """The pipeline stores each chunk's kind (#646)."""

    def _index(self, tmp_path, files) -> Database:
        db = Database(tmp_path / "mail.db")
        queue = _make_queue(db)
        for path in files:
            queue.enqueue(str(path), REASON_INITIAL_SCAN)
        _drain(queue, db, make_mock_embedder(_UNIT_VECTOR), Threader(db))
        return db

    def _kinds(self, db, message_id: str) -> list[tuple[str, str | None]]:
        rows = db._conn.execute(
            "SELECT c.kind, c.attachment_id FROM message_chunks c "
            "JOIN message_thread_map m ON m.claimant_id = c.claimant_id "
            "WHERE m.message_id = ? ORDER BY c.attachment_id IS NOT NULL, c.chunk_index",
            (message_id,),
        ).fetchall()
        return [(r["kind"], r["attachment_id"]) for r in rows]

    def test_body_and_attachment_chunks_are_tagged(self, tmp_path):
        dest = tmp_path / "INBOX" / "cur" / "att.eml"
        _write_eml_with_text_attachment(dest, "att@example.com")
        db = self._index(tmp_path, [dest])
        kinds = self._kinds(db, "att@example.com")
        assert [k for k, _ in kinds] == ["body", "attachment"]
        assert kinds[0][1] is None and kinds[1][1] is not None

    def test_message_with_no_body_text_is_chunked_by_kind(self, tmp_path):
        dest = tmp_path / "INBOX" / "cur" / "fwd.eml"
        body = "-- \nBob Example\n\nOn Mon, Jan 1, 2024 Alice <alice@example.com> wrote:\n> Hi"
        _write_eml(dest, "fwd@example.com", body=body)
        db = self._index(tmp_path, [dest])
        assert self._kinds(db, "fwd@example.com") == [("signature", None), ("quote", None)]

    def test_quoted_history_is_still_left_out_of_a_reply_with_body_text(self, tmp_path):
        dest = tmp_path / "INBOX" / "cur" / "reply.eml"
        _write_eml(dest, "reply@example.com", body="Yes.\n\n> Can we meet?\n-- \nBob")
        db = self._index(tmp_path, [dest])
        assert self._kinds(db, "reply@example.com") == [("body", None)]
        text = db._conn.execute("SELECT text FROM message_chunks").fetchone()["text"]
        assert text == "Yes."


class TestWalMaintenance:
    """``_run_wal_maintenance`` scrubs the FTS5 tables a reap deleted
    from, then runs the truncate checkpoint, so the checkpoint clears
    the WAL copies of the rewritten segments (#641, #670)."""

    def test_scrubs_before_checkpoint_and_logs_fixed_line(self, caplog):
        calls: list[str] = []
        db = MagicMock()
        db.scrub_reaped_fts.side_effect = lambda: (
            calls.append("scrub")
            or [
                "threads_fts",
                "attachments_fts",
            ]
        )
        db.wal_checkpoint_truncate.side_effect = lambda: calls.append("checkpoint") or (0, 0, 0)
        with caplog.at_level(logging.INFO, logger=main.log.name):
            main._run_wal_maintenance(db)
        assert calls == ["scrub", "checkpoint"]
        lines = [r.getMessage() for r in caplog.records if "fts scrub" in r.getMessage()]
        assert len(lines) == 1
        assert lines[0].startswith("fts scrub tables=threads_fts,attachments_fts duration=")

    def test_no_deletes_logs_nothing(self, caplog):
        db = MagicMock()
        db.scrub_reaped_fts.return_value = []
        db.wal_checkpoint_truncate.return_value = (0, 0, 0)
        with caplog.at_level(logging.DEBUG, logger=main.log.name):
            main._run_wal_maintenance(db)
        assert not [r for r in caplog.records if "fts scrub" in r.getMessage()]
        db.wal_checkpoint_truncate.assert_called_once()

    def test_scrub_failure_logs_type_and_still_checkpoints(self, caplog):
        db = MagicMock()
        db.scrub_reaped_fts.side_effect = sqlite3.OperationalError("zq-marker-641")
        db.wal_checkpoint_truncate.return_value = (0, 0, 0)
        with caplog.at_level(logging.DEBUG, logger=main.log.name):
            main._run_wal_maintenance(db)
        db.wal_checkpoint_truncate.assert_called_once()
        assert "fts scrub failed: OperationalError" in caplog.text
        assert "zq-marker-641" not in caplog.text

    def test_real_database_reap_then_maintenance_clears_terms(self, tmp_path):
        """End to end on a real database: a reaped message's terms stay
        in the file after the checkpoint alone and are gone after one
        maintenance pass; keyword search still finds the survivor."""
        from tests.conftest import make_message, make_thread

        db = Database(tmp_path / "mail.db")
        try:
            embedding = [0.1] * EMBEDDING_DIM
            reaped = make_message(message_id="r@x", filepath="/m/r", body_text="zqmaintmark")
            keep = make_message(message_id="k@x", filepath="/m/k", body_text="survivor")
            thread = make_thread([reaped, keep])
            db.upsert_thread(thread, embedding)
            db.add_pending_deletion("/m/r", "r@x", thread.thread_id)
            rebuilt = make_thread([keep], thread_id=thread.thread_id, subject=thread.subject)
            assert db.reap_thread_messages(rebuilt, embedding, ["r@x"]) == ["/m/r"]
            assert db.wal_checkpoint_truncate()[0] == 0
            assert b"zqmaintmark" in db.path.read_bytes()

            main._run_wal_maintenance(db)

            assert b"zqmaintmark" not in db.path.read_bytes()
            assert (
                db._conn.execute(
                    "SELECT COUNT(*) FROM threads_fts WHERE threads_fts MATCH 'survivor'"
                ).fetchone()[0]
                == 1
            )
        finally:
            db.close()


class TestAttachmentSummaryCadence:
    """Review round 1 (#871): in steady state the attachments aggregate
    was flushed only every ``TIMING_LOG_EVERY`` drained messages, so a
    burst of 8 messages could sit unlogged until unrelated mail arrived.
    It is now also flushed when a drain empties the queue and after
    ``SUMMARY_MAX_INTERVAL_SECS``."""

    @pytest.mark.parametrize(
        "drained, since_log, seconds, due",
        [
            (8, 8, 1.0, False),  # a full batch: more may be queued
            (8, 24, 1.0, False),
            (8, 25, 1.0, True),  # the message cadence
            (3, 11, 1.0, True),  # a short batch emptied the queue
            (0, 8, 1.0, True),  # nothing ready after a burst
            (0, 0, 1.0, False),  # idle, nothing pending
            (8, 8, main.SUMMARY_MAX_INTERVAL_SECS, True),  # the interval
            (0, 0, main.SUMMARY_MAX_INTERVAL_SECS, True),
        ],
    )
    def test_summary_due(self, drained, since_log, seconds, due):
        assert (
            main._steady_state_summary_due(
                drained=drained,
                drained_since_log=since_log,
                batch_size=8,
                seconds_since_summary=seconds,
            )
            is due
        )

    def test_aggregate_without_degraded_counts_is_info(self, caplog):
        from src import attachment_indexing
        from src.extractors import STATUS_SUCCESS

        caplog.set_level("INFO")
        attachment_indexing.attachment_outcomes.drain()
        attachment_indexing.attachment_outcomes.record(STATUS_SUCCESS, None, cached=True)
        main._log_attachment_outcomes()
        [record] = [r for r in caplog.records if r.getMessage().startswith("attachments n=")]
        assert record.levelname == "INFO"

    def test_nothing_logged_without_attachments(self, caplog):
        from src import attachment_indexing

        caplog.set_level("INFO")
        attachment_indexing.attachment_outcomes.drain()
        main._log_attachment_outcomes()
        assert caplog.records == []


class TestAttachmentSummaryDebounce:
    """Review round 4 on #884 (security): the steady-state loop flushes
    the attachments line at the end of every short drain, so mail spaced
    to arrive one message per drain produced one line per message. At
    most one line is logged per ``OUTCOMES_LOG_MIN_INTERVAL_SECS``;
    counts held back carry over to the next line, so none are lost."""

    def test_short_drains_inside_the_interval_give_one_line(self, monkeypatch, caplog):
        from src import attachment_indexing
        from src.extractors import STATUS_UNSUPPORTED

        caplog.set_level("INFO")
        attachment_indexing.attachment_outcomes.drain()
        clock = {"now": 1000.0}
        monkeypatch.setattr(main, "_monotonic", lambda: clock["now"])

        def lines():
            return [
                r.getMessage()
                for r in caplog.records
                if r.getMessage().startswith("attachments n=")
            ]

        for _ in range(10):
            attachment_indexing.attachment_outcomes.record(STATUS_UNSUPPORTED, None, cached=False)
            main._log_attachment_outcomes()
            clock["now"] += 5.0
        assert len(lines()) == 1
        assert " unsupported=1 " in lines()[0]
        # The next line, once the interval has passed, carries the nine
        # counts held back.
        clock["now"] = 1000.0 + main.OUTCOMES_LOG_MIN_INTERVAL_SECS
        main._log_attachment_outcomes()
        assert len(lines()) == 2
        assert " unsupported=9 " in lines()[1]
        # Nothing left over.
        assert attachment_indexing.attachment_outcomes.drain()["unsupported"] == 0

    def test_force_logs_inside_the_interval(self, monkeypatch, caplog):
        """The initial index's final summary is logged whatever the
        interval, so the scan's counts are not held into steady state."""
        from src import attachment_indexing
        from src.extractors import STATUS_SUCCESS

        caplog.set_level("INFO")
        attachment_indexing.attachment_outcomes.drain()
        monkeypatch.setattr(main, "_monotonic", lambda: 1000.0)
        for _ in range(2):
            attachment_indexing.attachment_outcomes.record(STATUS_SUCCESS, None, cached=False)
            main._log_attachment_outcomes(force=True)
        lines = [r for r in caplog.records if r.getMessage().startswith("attachments n=")]
        assert len(lines) == 2
