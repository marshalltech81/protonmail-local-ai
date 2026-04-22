"""Tests for src/main.py.

Covers the two watchdog-facing behaviors that cannot be verified by
``database`` or ``threader`` tests alone: that ``on_moved`` indexes the
destination of a Maildir rename (standard delivery path), and that
``initial_index`` refreshes the health file periodically so long scans
do not exceed ``HEALTH_MAX_AGE_SECONDS``.

``main`` orchestrates watchdog, Ollama, and filesystem I/O; these tests
exercise it with stub collaborators rather than booting a live indexer.
"""

from pathlib import Path
from unittest.mock import MagicMock

from src import main
from src.database import Database
from src.threader import Threader


class _FakeEvent:
    def __init__(self, src_path: str, dest_path: str, is_directory: bool = False):
        self.src_path = src_path
        self.dest_path = dest_path
        self.is_directory = is_directory


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


class TestOnMovedIndexesDestination:
    def test_rename_into_new_indexes_destination(self, tmp_path, monkeypatch):
        """Regression: Maildir delivery writes a file under ``tmp/`` then
        renames it into ``new/``. Prior behavior only fired ``on_created``
        for the source rename event, leaving the message unindexed until
        restart. ``on_moved`` must now index the destination."""
        db_path = tmp_path / "db" / "mail.db"
        db = Database(db_path)
        threader = Threader(db)

        # Populate a real Maildir destination file so ``_index_file``
        # succeeds end-to-end through the parser.
        dest = tmp_path / "INBOX" / "new" / "msg.eml"
        _write_eml(dest, "moved@example.com")

        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * 768

        handler = main.MaildirHandler(db, embedder, threader)
        handler.on_moved(
            _FakeEvent(
                src_path=str(tmp_path / "tmp" / "msg.eml"),
                dest_path=str(dest),
            )
        )

        assert db.is_indexed(str(dest))

    def test_directory_moves_are_ignored(self, tmp_path):
        db = Database(tmp_path / "db" / "mail.db")
        handler = main.MaildirHandler(db, MagicMock(), Threader(db))
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

        dest = tmp_path / "INBOX" / "cur" / "msg.eml"
        _write_eml(dest, "flag_change@example.com")

        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * 768

        handler = main.MaildirHandler(db, embedder, threader)

        # First delivery indexes the message
        handler.on_moved(_FakeEvent(src_path=str(tmp_path / "tmp" / "m"), dest_path=str(dest)))
        first_call_count = embedder.embed.call_count

        # Second move event on the same path (e.g., flag rename) must not
        # trigger another embed.
        handler.on_moved(_FakeEvent(src_path=str(dest), dest_path=str(dest)))
        assert embedder.embed.call_count == first_call_count


class TestInitialIndexHeartbeat:
    def test_health_file_refreshed_during_long_scan(self, tmp_path, monkeypatch):
        """``initial_index`` must refresh the heartbeat every
        ``HEALTH_REFRESH_EVERY`` messages so that embedding a large mailbox
        does not exceed ``HEALTH_MAX_AGE_SECONDS`` mid-scan."""
        maildir = tmp_path / "maildir"
        inbox = maildir / "INBOX" / "cur"
        inbox.mkdir(parents=True)

        # Write one more than the refresh threshold so we expect at least
        # one refresh to fire during the scan.
        for i in range(main.HEALTH_REFRESH_EVERY + 1):
            _write_eml(inbox / f"m{i}.eml", f"m{i}@example.com")

        db = Database(tmp_path / "mail.db")
        threader = Threader(db)

        embedder = MagicMock()
        embedder.embed.return_value = [0.0] * 768

        touches: list[None] = []
        monkeypatch.setattr(main, "touch_health_file", lambda: touches.append(None))
        monkeypatch.setattr(main, "MAILDIR_PATH", maildir)

        main.initial_index(db, embedder, threader)

        assert len(touches) >= 1
