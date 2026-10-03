"""
Maildir filename helpers.

Maildir files use the form ``<uniq>:2,<flags>`` where ``<flags>`` is a string
of single-letter flags (D=Draft, F=Flagged, P=Passed, R=Replied, S=Seen,
T=Trashed). mbsync signals a remote deletion under ``Expunge None`` by adding
the ``T`` flag to the local file — which on disk is a rename, not a delete.

mbsync also writes a last-sync stamp at the Maildir root after every
successful sync; see ``read_sync_stamp``.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

FLAG_SEPARATOR = ":2,"
TRASHED_FLAG = "T"

# Written by mbsync/entrypoint.sh (``record_successful_sync``) at the
# Maildir root, outside any ``cur``/``new`` folder, so no Maildir walk or
# watchdog handler treats it as a message. mbsync writes each stamp to a
# temporary file named after that sync (``_SYNC_STAMP_TMP_RE``) and
# renames it into place.
SYNC_STAMP_NAME = ".mbsync-last-sync.json"
_SYNC_STAMP_TMP_RE = re.compile(r"^\.mbsync-last-sync\.([0-9T:Z-]+)\.(\d+)\.tmp$")

# Renamed into place at the Maildir root by mbsync/entrypoint.sh
# (``signal_perms_repaired``) after every permission repair, a failed
# sync's included, so the indexer re-watches the folders it opened
# (#524). It marks no successful sync and carries no content.
PERMS_REPAIRED_NAME = ".mbsync-perms-repaired"


@dataclass(frozen=True)
class SyncStamp:
    completed_at: str  # ISO 8601, UTC
    sync_interval_secs: int


def _sync_stamp(completed_at: object, interval: object) -> SyncStamp:
    completed = datetime.fromisoformat(str(completed_at))
    if completed.tzinfo is None:
        raise ValueError("sync stamp completion time has no timezone")
    if type(interval) is not int or interval < 1:
        raise ValueError("sync stamp interval is not a positive integer")
    return SyncStamp(completed.astimezone(UTC).isoformat(), interval)


def read_sync_stamp(root: Path) -> SyncStamp | None:
    """Return mbsync's last successful sync, or ``None`` before the first.

    Raises ``ValueError`` when the stamp exists but cannot be read as a
    timezone-aware completion time plus a positive integer interval.
    """
    try:
        raw = (root / SYNC_STAMP_NAME).read_text()
    except FileNotFoundError:
        return None
    data = json.loads(raw)  # JSONDecodeError is a ValueError
    if not isinstance(data, dict):
        raise ValueError("sync stamp is not a JSON object")
    return _sync_stamp(data.get("completed_at"), data.get("sync_interval_secs"))


def parse_sync_stamp_rename(src_path: Path | str) -> SyncStamp | None:
    """The sync named by the temporary file mbsync renamed onto the stamp,
    or ``None`` when ``src_path`` is not one."""
    match = _SYNC_STAMP_TMP_RE.match(Path(src_path).name)
    if match is None:
        return None
    try:
        return _sync_stamp(match.group(1), int(match.group(2)))
    except ValueError:
        return None


def parse_flags(path: Path | str) -> set[str]:
    """Return the Maildir flag letters present on ``path``."""
    name = Path(path).name
    if FLAG_SEPARATOR not in name:
        return set()
    return set(name.rsplit(FLAG_SEPARATOR, 1)[1])


def is_trashed(path: Path | str) -> bool:
    """True iff the file is flagged ``T`` (IMAP \\Deleted / Maildir trashed)."""
    return TRASHED_FLAG in parse_flags(path)


@dataclass(frozen=True)
class MessageState:
    """A message's read / flagged / replied state as mbsync mirrors it
    from Proton into the filename (``S``, ``F``, ``R``)."""

    seen: bool
    flagged: bool
    replied: bool


def message_state(path: Path | str) -> MessageState:
    """The state the ``:2,<flags>`` suffix of ``path`` records. A file
    without the suffix (mbsync's ``new/`` deliveries) is unread; other
    letters, including ``T``, are ignored."""
    flags = parse_flags(path)
    return MessageState(seen="S" in flags, flagged="F" in flags, replied="R" in flags)


def get_uniq(path: Path | str) -> str:
    """Return the Maildir uniq base name — the portion before ``:2,flags``.

    Used to locate the same logical message after mbsync has renamed the file
    due to flag changes.
    """
    name = Path(path).name
    if FLAG_SEPARATOR in name:
        return name.rsplit(FLAG_SEPARATOR, 1)[0]
    return name


def resolve_current_path(
    stored_path: Path, listings: dict[Path, dict[str, Path]] | None = None
) -> Path | None:
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

    ``listings`` is a per-sweep cache of each scanned directory's
    uniq -> file map. A sweep resolving many stale paths in one folder
    passes the same dict, so the folder is listed once rather than once
    per path (quadratic in the number of renamed files).
    """
    if stored_path.exists():
        return stored_path

    uniq = get_uniq(stored_path)
    prefix = uniq + FLAG_SEPARATOR

    def _scan(directory: Path) -> Path | None:
        if not directory.exists():
            return None
        if listings is not None:
            if directory not in listings:
                listing: dict[str, Path] = {}
                for child in directory.iterdir():
                    if child.is_file():
                        listing.setdefault(get_uniq(child), child)
                listings[directory] = listing
            return listings[directory].get(uniq)
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
