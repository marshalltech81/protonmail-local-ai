"""
Maildir filename helpers.

Maildir files use the form ``<uniq>:2,<flags>`` where ``<flags>`` is a string
of single-letter flags (D=Draft, F=Flagged, P=Passed, R=Replied, S=Seen,
T=Trashed). mbsync signals a remote deletion under ``Expunge None`` by adding
the ``T`` flag to the local file — which on disk is a rename, not a delete.
"""

from pathlib import Path

FLAG_SEPARATOR = ":2,"
TRASHED_FLAG = "T"


def parse_flags(path: Path | str) -> set[str]:
    """Return the Maildir flag letters present on ``path``."""
    name = Path(path).name
    if FLAG_SEPARATOR not in name:
        return set()
    return set(name.rsplit(FLAG_SEPARATOR, 1)[1])


def is_trashed(path: Path | str) -> bool:
    """True iff the file is flagged ``T`` (IMAP \\Deleted / Maildir trashed)."""
    return TRASHED_FLAG in parse_flags(path)


def get_uniq(path: Path | str) -> str:
    """Return the Maildir uniq base name — the portion before ``:2,flags``.

    Used to locate the same logical message after mbsync has renamed the file
    due to flag changes.
    """
    name = Path(path).name
    if FLAG_SEPARATOR in name:
        return name.rsplit(FLAG_SEPARATOR, 1)[0]
    return name


def resolve_current_path(stored_path: Path) -> Path | None:
    """Find the current on-disk path for a message previously indexed at
    ``stored_path``. Returns ``None`` if the file is no longer present under
    its original uniq in the same Maildir folder.

    Looks first at the stored path itself (fast path — no rename has occurred),
    then scans the parent directory for any file sharing the same uniq base
    name. Maildir semantics guarantee the uniq is stable across flag-induced
    renames within the same folder.
    """
    if stored_path.exists():
        return stored_path
    parent = stored_path.parent
    if not parent.exists():
        return None
    uniq = get_uniq(stored_path)
    prefix = uniq + FLAG_SEPARATOR
    for child in parent.iterdir():
        if not child.is_file():
            continue
        if child.name == uniq or child.name.startswith(prefix):
            return child
    return None
