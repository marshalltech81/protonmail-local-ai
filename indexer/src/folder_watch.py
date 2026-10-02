"""
Re-watch Maildir folders that became readable after the watch started (#516).

mbsync creates every folder directory (and its ``cur``/``new``/``tmp``)
with mode 0700 and makes it readable to other users only after the sync,
in the entrypoint's permission repair. The indexer runs as another UID,
so watchdog's recursive inotify watch cannot be added to such a folder:
watchdog ignores ``EACCES``, both when the watch is scheduled and when
the folder's create event arrives, and the later ``chmod`` adds no watch.

``FolderWatchRefresher`` records which directories were readable when
the watch was last scheduled, and their inodes. After each completed
sync, ``refresh`` walks the folder directories again and, when any
directory is readable now that was not then, or was replaced by a new
directory at the same path (deleting a directory drops its watch),
re-schedules the watch, which walks the tree again and adds every watch
it can. A directory-create event from the watch itself also forces a
re-schedule, since a recreated directory may reuse its inode number.
"""

import logging
import os
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers.api import BaseObserver, ObservedWatch

log = logging.getLogger(__name__)

# Message directories of a Maildir folder. The walk checks that they are
# readable but does not list them: they hold the messages, the folders
# hold only a handful of entries.
MESSAGE_DIRS = frozenset({"cur", "new", "tmp"})


def readable_dirs(root: Path) -> dict[str, int]:
    """Every directory under ``root`` the indexer can list and enter,
    mapped to its inode number.

    Lists only folder directories, never a ``cur``/``new``/``tmp``
    directory, so the work is linear in the number of folders, not
    messages. Symlinks are skipped, as watchdog skips them. A directory
    that cannot be listed, or vanishes mid-walk, ends that branch. A child
    folder named ``cur``, ``new`` or ``tmp`` is the directory ``.cur``,
    ``.new`` or ``.tmp`` (mbsync's ``SubFolders Legacy``, #281), so it is
    walked like any other folder.
    """
    found: dict[str, int] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                children = [entry for entry in entries if entry.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for entry in children:
            if not os.access(entry.path, os.R_OK | os.X_OK):
                continue
            found[entry.path] = entry.inode()
            if entry.name not in MESSAGE_DIRS:
                pending.append(Path(entry.path))
    return found


class FolderWatchRefresher:
    """Owns the recursive Maildir watch and re-schedules it when a
    directory became readable.

    The readable set is taken before each schedule: permissions only
    widen, so every directory in it is watched. A directory missing from
    it (created later, or unreadable then), or found at its path with a
    different inode (deleted and recreated, which drops the watch), is
    re-checked on every ``refresh``. A watch survives a ``chmod``, so a
    watched directory closed and reopened needs nothing. Re-scheduling
    replaces the watch, so there is never more than one.

    ``directory_created`` is set by the event handler for every
    directory the watch reports created. watchdog cannot watch one that
    appears unreadable, and a directory deleted and recreated between
    two refreshes may get its old inode number back, so the walk alone
    cannot tell it apart; the event forces the next refresh to
    re-schedule.
    """

    def __init__(
        self,
        root: Path,
        observer: BaseObserver,
        handler: FileSystemEventHandler,
        *,
        directory_created: threading.Event | None = None,
    ):
        self.root = root
        self._observer = observer
        self._handler = handler
        self._directory_created = directory_created or threading.Event()
        self._watch: ObservedWatch | None = None
        self._watched: dict[str, int] = {}
        # Set when ``refresh`` re-schedules the watch; the caller clears
        # it once its recovery walk for the re-schedule gap succeeds, so
        # a failed walk is retried on the next refresh (#529).
        self.recovery_pending = False

    def _schedule(self, readable: dict[str, int]) -> None:
        if self._watch is not None:
            self._observer.unschedule(self._watch)
            self._watch = None
        self._watch = self._observer.schedule(self._handler, str(self.root), recursive=True)
        # Only once the watch exists: if scheduling raised, the next
        # refresh tries again.
        self._watched = readable

    def start(self) -> None:
        """Schedule the watch. Call before ``observer.start()``."""
        self._schedule(readable_dirs(self.root))

    def refresh(self) -> bool:
        """Re-schedule the watch if a directory became readable, or was
        created, since it was last scheduled, or if the last schedule
        failed. Returns whether it did."""
        # Cleared before the walk, so a directory created during this
        # refresh forces the next one. Cleared only when seen set: a
        # ``set`` landing between the check and an unconditional clear
        # would be erased unprocessed (#528). One landing after a true
        # check is covered, since this refresh then re-schedules after
        # that directory exists.
        created = self._directory_created.is_set()
        if created:
            self._directory_created.clear()
        current = readable_dirs(self.root)
        added = sum(1 for path, inode in current.items() if self._watched.get(path) != inode)
        if not added and not created and self._watch is not None:
            return False
        log.info("Maildir watch: %d new or newly readable director(ies); re-watching", added)
        self._schedule(current)
        self.recovery_pending = True
        return True
