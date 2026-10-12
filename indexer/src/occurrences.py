"""Mailbox occurrence locators (#1566, PLAN.md decision 47).

Each Maildir copy of a message is its own occurrence in
``message_occurrences``, keyed by the account, its folder, the file's
Maildir uniq and the claimant ID. Its locator is relative to the
account's Maildir root, so relocating the root alone keeps every
occurrence. The relative path comes from a structural, component-wise
relativization, never a string prefix: a path outside the root (a
sibling that shares the root's spelling as a prefix, or one climbing out
through ``..``) is refused with fixed text, never assigned the leaf
folder name ``parser._derive_folder`` falls back to.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .maildir import folder_name, get_uniq, is_trashed

OUTSIDE_ROOT = (
    "A message path lies outside the configured Maildir root (MAILDIR_PATH), so it "
    "cannot be stored relative to the account. The index was built under another "
    "root: restore the original MAILDIR_PATH, or rebuild the index from Maildir; see "
    'docs/troubleshooting.md "Indexer stops during a schema migration".'
)


class OccurrenceLocatorError(ValueError):
    """A message path cannot be made relative to the account's Maildir
    root. Fixed text only: never the path."""

    def __init__(self) -> None:
        super().__init__(OUTSIDE_ROOT)


@dataclass(frozen=True)
class Locator:
    """Where an occurrence lives, relative to its account: the folder
    (``maildir.folder_name`` of the folder directory), the opaque Maildir
    uniq and the file's path below the root."""

    folder: str
    uniq: str
    relative_path: str


def locate(filepath: str | Path, root: Path) -> Locator:
    """The locator of the message file ``filepath`` under ``root``, or
    ``OccurrenceLocatorError``. Both must be absolute; ``filepath`` must
    lie strictly inside ``root``, component by component, with no ``..``
    component."""
    path = PurePosixPath(filepath)
    base = PurePosixPath(root)
    if not path.is_absolute() or not base.is_absolute() or ".." in path.parts:
        raise OccurrenceLocatorError
    try:
        relative = path.relative_to(base)
    except ValueError:
        raise OccurrenceLocatorError from None
    if not relative.parts:
        raise OccurrenceLocatorError
    return Locator(
        folder=folder_name(relative.parent.parent),
        uniq=get_uniq(relative.name),
        relative_path=relative.as_posix(),
    )


class SeedFunctions:
    """The v14 migration's seed functions, bound to the configured root
    and registered on the migrating connection only. ``refused`` counts
    the paths outside the root, so the caller can replace SQLite's
    generic "user-defined function raised exception" with the fixed
    ``OUTSIDE_ROOT`` text."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.refused = 0

    def _locate(self, filepath: str) -> Locator:
        try:
            return locate(filepath, self.root)
        except OccurrenceLocatorError:
            self.refused += 1
            raise

    def register(self, conn: sqlite3.Connection) -> None:
        conn.create_function(
            "pmai_occurrence_folder", 1, lambda p: self._locate(p).folder, deterministic=True
        )
        conn.create_function(
            "pmai_occurrence_uniq", 1, lambda p: self._locate(p).uniq, deterministic=True
        )
        conn.create_function(
            "pmai_occurrence_path", 1, lambda p: self._locate(p).relative_path, deterministic=True
        )
        conn.create_function(
            "pmai_occurrence_trashed", 1, lambda p: int(is_trashed(p)), deterministic=True
        )
