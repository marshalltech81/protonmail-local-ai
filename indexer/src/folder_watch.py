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
it can.
"""

import logging
import os
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
    that cannot be listed, or vanishes mid-walk, ends that branch.
    Limitation: a subfolder nested under a folder itself named ``cur``,
    ``new`` or ``tmp`` is not seen; the periodic Maildir rescan still
    indexes its mail.
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
    replaces the watch, so there is never more than one. Limitation: a
    directory recreated with a reused inode number looks unchanged; the
    periodic Maildir rescan still indexes its mail.
    """

    def __init__(self, root: Path, observer: BaseObserver, handler: FileSystemEventHandler):
        self.root = root
        self._observer = observer
        self._handler = handler
        self._watch: ObservedWatch | None = None
        self._watched: dict[str, int] = {}

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
        recreated, since it was last scheduled, or if the last schedule
        failed. Returns whether it did."""
        current = readable_dirs(self.root)
        added = sum(1 for path, inode in current.items() if self._watched.get(path) != inode)
        if not added and self._watch is not None:
            return False
        log.info("Maildir watch: %d new or newly readable director(ies); re-watching", added)
        self._schedule(current)
        return True
