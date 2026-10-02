"""Tests for re-watching Maildir folders that became readable late (#516)."""

import logging
import os
import sys
from pathlib import Path

import pytest
from src import folder_watch
from src.folder_watch import FolderWatchRefresher, readable_dirs

FOLDER_MARKER = "SYNTHETIC-FOLDER-516"

# chmod 000 does not stop root from listing a directory.
needs_unprivileged = pytest.mark.skipif(
    os.geteuid() == 0, reason="root can list a mode-000 directory"
)


def _folder(root: Path, name: str, *, messages: int = 0) -> Path:
    folder = root / name
    for sub in ("cur", "new", "tmp"):
        (folder / sub).mkdir(parents=True)
    for i in range(messages):
        (folder / "cur" / f"{i}.eml:2,S").write_text("x")
    return folder


class _FakeObserver:
    """Records schedule / unschedule calls; watches are plain tokens."""

    def __init__(self):
        self.calls: list[str] = []
        self.live: set[int] = set()
        self._next = 0
        self.fail_next_schedule = False

    def schedule(self, handler, path, *, recursive=False):
        assert recursive
        self.calls.append("schedule")
        if self.fail_next_schedule:
            self.fail_next_schedule = False
            raise OSError(28, "inotify watch limit reached")
        self._next += 1
        self.live.add(self._next)
        return self._next

    def unschedule(self, watch):
        self.calls.append("unschedule")
        self.live.remove(watch)


@pytest.fixture
def unreadable():
    """chmod a directory to 000, restoring it at teardown so tmp_path
    cleanup can remove it."""
    locked: list[Path] = []

    def lock(path: Path) -> Path:
        path.chmod(0o000)
        locked.append(path)
        return path

    yield lock
    for path in locked:
        if path.exists():
            path.chmod(0o755)


class TestReadableDirs:
    def test_lists_folders_and_message_dirs_at_any_depth(self, tmp_path):
        _folder(tmp_path, "INBOX")
        _folder(tmp_path / "Folders", "Clients")

        found = {str(Path(p).relative_to(tmp_path)) for p in readable_dirs(tmp_path)}

        assert found == {
            "INBOX",
            "INBOX/cur",
            "INBOX/new",
            "INBOX/tmp",
            "Folders",
            "Folders/Clients",
            "Folders/Clients/cur",
            "Folders/Clients/new",
            "Folders/Clients/tmp",
        }

    @needs_unprivileged
    def test_skips_a_directory_it_cannot_enter(self, tmp_path, unreadable):
        _folder(tmp_path, "INBOX")
        unreadable(_folder(tmp_path, "Late"))

        found = readable_dirs(tmp_path)

        assert str(tmp_path / "INBOX") in found
        assert not any(p.startswith(str(tmp_path / "Late")) for p in found)

    def test_skips_symlinks(self, tmp_path):
        _folder(tmp_path, "INBOX")
        (tmp_path / "link").symlink_to(tmp_path / "INBOX")

        assert str(tmp_path / "link") not in readable_dirs(tmp_path)

    def test_lists_folders_only_not_message_directories(self, tmp_path, monkeypatch):
        """Linear in folders, not messages: a ``cur`` holding many
        messages is checked but never listed."""
        _folder(tmp_path, "INBOX", messages=500)
        _folder(tmp_path / "Folders", "Clients", messages=500)
        listed: list[str] = []
        real_scandir = os.scandir

        def counting_scandir(path):
            listed.append(str(path))
            return real_scandir(path)

        monkeypatch.setattr(folder_watch.os, "scandir", counting_scandir)
        found = readable_dirs(tmp_path)

        assert sorted(listed) == sorted(
            [
                str(tmp_path),
                str(tmp_path / "INBOX"),
                str(tmp_path / "Folders"),
                str(tmp_path / "Folders" / "Clients"),
            ]
        )
        assert str(tmp_path / "INBOX" / "cur") in found

    def test_a_directory_that_vanishes_ends_its_branch(self, tmp_path, monkeypatch):
        _folder(tmp_path, "INBOX")
        real_scandir = os.scandir

        def flaky_scandir(path):
            if Path(path).name == "INBOX":
                raise FileNotFoundError(2, "gone")
            return real_scandir(path)

        monkeypatch.setattr(folder_watch.os, "scandir", flaky_scandir)

        assert set(readable_dirs(tmp_path)) == {str(tmp_path / "INBOX")}


class TestFolderWatchRefresher:
    def test_start_schedules_one_recursive_watch(self, tmp_path):
        _folder(tmp_path, "INBOX")
        observer = _FakeObserver()

        FolderWatchRefresher(tmp_path, observer, handler=None).start()  # type: ignore[arg-type]

        assert observer.calls == ["schedule"]

    def test_refresh_without_changes_keeps_the_watch(self, tmp_path):
        _folder(tmp_path, "INBOX")
        observer = _FakeObserver()
        refresher = FolderWatchRefresher(tmp_path, observer, handler=None)  # type: ignore[arg-type]
        refresher.start()

        assert refresher.refresh() is False
        assert refresher.refresh() is False
        assert observer.calls == ["schedule"]

    @needs_unprivileged
    def test_folder_made_readable_is_rewatched_once(self, tmp_path, unreadable, caplog):
        caplog.set_level(logging.INFO, logger=folder_watch.__name__)
        _folder(tmp_path, "INBOX")
        late = unreadable(_folder(tmp_path, FOLDER_MARKER))
        observer = _FakeObserver()
        refresher = FolderWatchRefresher(tmp_path, observer, handler=None)  # type: ignore[arg-type]
        refresher.start()

        # Still unreadable: nothing to add.
        assert refresher.refresh() is False
        late.chmod(0o755)
        assert refresher.refresh() is True
        # Repeated syncs do not stack watches.
        assert refresher.refresh() is False
        assert refresher.refresh() is False

        assert observer.calls == ["schedule", "unschedule", "schedule"]
        assert len(observer.live) == 1
        # Counts only: folder names are mailbox content.
        assert "4 new or newly readable director(ies)" in caplog.text
        assert FOLDER_MARKER not in caplog.text

    def test_folder_created_after_start_is_rewatched(self, tmp_path):
        """mbsync creates a new folder 0700, so the watch's own create
        handling could not add it; treat it as unwatched."""
        observer = _FakeObserver()
        refresher = FolderWatchRefresher(tmp_path, observer, handler=None)  # type: ignore[arg-type]
        refresher.start()
        _folder(tmp_path, "New")

        assert refresher.refresh() is True
        assert refresher.refresh() is False
        assert observer.calls == ["schedule", "unschedule", "schedule"]

    @needs_unprivileged
    def test_folder_briefly_unreadable_keeps_its_watch(self, tmp_path, unreadable):
        """An inotify watch survives a chmod, so a watched directory that
        is closed and reopened needs no new watch."""
        folder = _folder(tmp_path, "Box")
        observer = _FakeObserver()
        refresher = FolderWatchRefresher(tmp_path, observer, handler=None)  # type: ignore[arg-type]
        refresher.start()

        unreadable(folder)
        assert refresher.refresh() is False
        folder.chmod(0o755)

        assert refresher.refresh() is False
        assert observer.calls == ["schedule"]

    @pytest.mark.parametrize("victim", ["Box", "Box/new"])
    def test_directory_recreated_at_the_same_path_is_rewatched(self, tmp_path, victim):
        """Review round 1: deleting a directory drops its watch, and
        mbsync recreates it 0700, so watchdog cannot add the new one. Same
        path, new inode: re-watch."""
        root = tmp_path / "maildir"
        _folder(root, "Box")
        observer = _FakeObserver()
        refresher = FolderWatchRefresher(root, observer, handler=None)  # type: ignore[arg-type]
        refresher.start()
        # Moved out of the Maildir rather than deleted, so the filesystem
        # cannot hand its inode number to the replacement.
        (root / victim).rename(tmp_path / "gone")
        if victim == "Box":
            _folder(root, "Box")
        else:
            (root / victim).mkdir()

        assert refresher.refresh() is True
        assert refresher.refresh() is False
        assert observer.calls == ["schedule", "unschedule", "schedule"]

    def test_a_created_directory_forces_a_rewatch(self, tmp_path):
        """Review round 2: a directory deleted and recreated between two
        refreshes may get its old inode number back, so the walk alone
        cannot see it. The watch's own directory-create event marks the
        watch stale; the next refresh re-schedules once."""
        import threading

        _folder(tmp_path, "Box")
        created = threading.Event()
        observer = _FakeObserver()
        refresher = FolderWatchRefresher(
            tmp_path,
            observer,
            handler=None,  # type: ignore[arg-type]
            directory_created=created,
        )
        refresher.start()
        assert refresher.refresh() is False

        created.set()
        assert refresher.refresh() is True
        assert not created.is_set()
        assert refresher.refresh() is False
        assert observer.calls == ["schedule", "unschedule", "schedule"]

    def test_a_directory_created_while_checking_is_not_lost(self, tmp_path):
        """#528: a directory-create event that sets the flag just after
        ``refresh`` found it unset must survive to the next refresh, not
        be erased by that refresh's clear."""
        import threading

        class _SetDuringCheck(threading.Event):
            """The handler's ``set`` lands right after the first check."""

            def __init__(self):
                super().__init__()
                self.armed = False

            def is_set(self) -> bool:
                was_set = super().is_set()
                if self.armed:
                    self.armed = False
                    self.set()
                return was_set

        _folder(tmp_path, "Box")
        created = _SetDuringCheck()
        observer = _FakeObserver()
        refresher = FolderWatchRefresher(
            tmp_path,
            observer,
            handler=None,  # type: ignore[arg-type]
            directory_created=created,
        )
        refresher.start()

        created.armed = True
        # The signal arrived after the check: this refresh has nothing
        # to do, but the next one must re-schedule.
        assert refresher.refresh() is False
        assert refresher.refresh() is True
        assert refresher.refresh() is False
        assert observer.calls == ["schedule", "unschedule", "schedule"]

    def test_failed_schedule_is_retried_on_the_next_refresh(self, tmp_path):
        observer = _FakeObserver()
        refresher = FolderWatchRefresher(tmp_path, observer, handler=None)  # type: ignore[arg-type]
        refresher.start()
        _folder(tmp_path, "New")
        observer.fail_next_schedule = True

        with pytest.raises(OSError):
            refresher.refresh()
        assert not observer.live

        assert refresher.refresh() is True
        assert len(observer.live) == 1
        assert refresher.refresh() is False


@pytest.mark.skipif(sys.platform != "linux", reason="the EACCES gap is inotify-specific")
@needs_unprivileged
class TestRealInotifyWatch:
    """The bug itself, against watchdog's real inotify observer."""

    def test_watch_misses_a_late_folder_until_refreshed(self, tmp_path, unreadable):
        import threading

        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        created: list[str] = []
        seen = threading.Event()

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    created.append(Path(event.src_path).name)
                    seen.set()

        late = unreadable(_folder(tmp_path, "Late"))
        observer = Observer()
        refresher = FolderWatchRefresher(tmp_path, observer, Handler())
        refresher.start()
        observer.start()
        try:
            late.chmod(0o755)
            (late / "new" / "before").write_text("x")
            assert not seen.wait(1.0), "watch already covered the late folder"

            assert refresher.refresh() is True
            (late / "new" / "after").write_text("x")
            assert seen.wait(5.0)
            assert created == ["after"]
        finally:
            observer.stop()
            observer.join()
