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

    Scans, in order:

    1. the stored path itself (fast path — no rename has occurred),
    2. the stored file's parent directory (flag-only rename, e.g. ``S → SR``),
    3. the ``new`` / ``cur`` siblings under the Maildir folder root when the
       stored file lived in one of them (mbsync promotion from ``new`` to
       ``cur`` while the indexer was offline would otherwise look like a
       deletion).

    Maildir semantics guarantee the uniq is stable across flag-induced renames
    and ``new``/``cur`` moves within the same folder.
    """
    if stored_path.exists():
        return stored_path

    uniq = get_uniq(stored_path)
    prefix = uniq + FLAG_SEPARATOR

    def _scan(directory: Path) -> Path | None:
        if not directory.exists():
            return None
        for child in directory.iterdir():
            if not child.is_file():
                continue
            if child.name == uniq or child.name.startswith(prefix):
                return child
        return None

    parent = stored_path.parent
    match = _scan(parent)
    if match is not None:
        return match

    # If the stored path lives in a Maildir folder's ``new`` or ``cur``
    # subdir, also scan its sibling. This catches the case where mbsync
    # promoted the file from ``new`` to ``cur`` (or vice versa) while the
    # indexer was offline, which would otherwise trip reconciliation into
    # treating a live message as missing.
    if parent.name in {"new", "cur"}:
        sibling_name = "cur" if parent.name == "new" else "new"
        match = _scan(parent.parent / sibling_name)
        if match is not None:
            return match

    return None
