"""Identity-bound byte evidence and once-per-pass directory inventories."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from pathlib import Path

from .maildir import get_uniq

FileIdentity = tuple[int, int, int, int, int]
MAX_HASH_FILES = 500
MAX_HASH_BYTES = 100 * 1024 * 1024


def identity_of(st: os.stat_result) -> FileIdentity:
    return st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns, st.st_dev


def file_identity(path: Path) -> FileIdentity | None:
    try:
        return identity_of(path.stat())
    except OSError:
        return None


def read_digest(path: Path, identity: FileIdentity) -> str:
    """Read at most the captured size, rejecting changed/non-regular files.

    The caller admits this size against its budget before opening. Fstat
    binds the proof to the descriptor; the final path check rejects a
    replacement while the descriptor still names the original inode.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or identity_of(st) != identity:
            raise OSError(errno.EAGAIN, "file identity changed")
        digest = hashlib.sha256()
        remaining = identity[0]
        while remaining:
            chunk = os.read(fd, min(1 << 20, remaining))
            if not chunk:
                raise OSError(errno.EAGAIN, "file identity changed")
            digest.update(chunk)
            remaining -= len(chunk)
        if identity_of(os.fstat(fd)) != identity or file_identity(path) != identity:
            raise OSError(errno.EAGAIN, "file identity changed")
        return digest.hexdigest()
    finally:
        os.close(fd)


class FolderInventory:
    """List each directory once; changed or unreadable inventories prove nothing."""

    def __init__(self, hash_cache=None) -> None:
        self._entries: dict[Path, tuple[FileIdentity | None, dict[str, list[Path]]] | None] = {}
        self._hashes = hash_cache if hash_cache is not None else {}
        self._files = 0
        self._bytes = 0
        self.hash_deferred = 0
        self._attempted: set[tuple[str, int, int, int, int, int]] = set()
        self._deferred: set[tuple[str, int, int, int, int, int]] = set()

    def digest(self, path: Path, identity: FileIdentity) -> str | None:
        key = (str(path), *identity)
        if key in self._hashes:
            return self._hashes[key]
        if key in self._attempted:
            return None
        if self._files >= MAX_HASH_FILES or self._bytes + identity[0] > MAX_HASH_BYTES:
            if key not in self._deferred:
                self._deferred.add(key)
                self.hash_deferred += 1
            return None
        self._attempted.add(key)
        self._files += 1
        self._bytes += identity[0]
        try:
            digest = read_digest(path, identity)
        except OSError:
            return None
        self._hashes[key] = digest
        return digest

    def seed(self, directory: Path, before: FileIdentity | None, children: list[Path]) -> None:
        if file_identity(directory) != before:
            self._entries[directory] = None
            return
        grouped: dict[str, list[Path]] = {}
        for child in children:
            grouped.setdefault(get_uniq(child), []).append(child)
        self._entries[directory] = before, grouped

    @staticmethod
    def _signature(directory: Path) -> FileIdentity | None:
        try:
            return identity_of(directory.stat())
        except FileNotFoundError:
            return None

    def _directory(self, directory: Path) -> dict[str, list[Path]] | None:
        if directory not in self._entries:
            try:
                before = self._signature(directory)
                children = list(directory.iterdir()) if before is not None else []
                if self._signature(directory) != before:
                    self._entries[directory] = None
                else:
                    grouped: dict[str, list[Path]] = {}
                    for child in children:
                        grouped.setdefault(get_uniq(child), []).append(child)
                    self._entries[directory] = before, grouped
            except OSError:
                self._entries[directory] = None
        entry = self._entries[directory]
        try:
            unchanged = entry is not None and self._signature(directory) == entry[0]
        except OSError:
            unchanged = False
        if not unchanged:
            return None
        assert entry is not None
        return entry[1]

    def candidates(self, path: Path) -> list[Path] | None:
        directories = [path.parent]
        if path.parent.name in {"new", "cur"}:
            directories.append(path.parent.parent / ("new" if path.parent.name == "cur" else "cur"))
        paths: list[Path] = []
        for directory in directories:
            listing = self._directory(directory)
            if listing is None:
                return None
            paths.extend(listing.get(get_uniq(path), []))
        return sorted(paths)
