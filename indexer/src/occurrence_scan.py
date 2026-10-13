"""Completeness-gated occurrence reconciliation (#1567, PLAN.md decision 47).

The watcher carries an occurrence through every rename it observes
(``Database.update_filepath``). What it misses, while the indexer is
down or when an event is lost, this scan finds by walking the account's
Maildir root and comparing it with the account's active occurrences:

- **Missed renames.** An active occurrence whose file is gone but whose
  folder holds a file with its Maildir uniq (a flag change, a
  ``new``/``cur`` move) moves to that file, once its bytes are verified
  to be the message's (``_verify``): by the recorded file identity when
  size and mtime match, else by hashing the file.
- **Found copies.** A file no active occurrence holds whose bytes are an
  indexed message's is bound to it as an occurrence with provenance
  ``scan``: a byte-identical copy the migration seed or the parse never
  bound (a ``T``-flagged file the drain parks, a dead-lettered job, an
  unmapped copy moved across folders, #1141). Its bytes come from its
  ``indexed_files`` record when size and mtime match, else, only for a
  ``T``-flagged or dead-lettered file and with a ``hash_cache``, from
  hashing it; any other unindexed file is left to the Maildir walk,
  whose parse binds it.
- **Vanished files.** An active occurrence with no such file is retired
  only by a complete scan: the walk read every directory under the root.
  A partial scan (an unreadable directory, a missing root) establishes
  no absence, so it retires nothing; the occurrence stays active and is
  counted as unreachable. Retiring removes a locator, never a message:
  whether the message is reaped is mirror retention's reaper's call
  (``reconciler``), and archive retention never reaps.

Every write re-checks, in its own transaction, that the occurrence or
path is as the scan saw it, so a rename the watcher records meanwhile
wins, and that the file is still the one whose bytes were verified (its
size, mtime and inode taken before the verification), so a file
replaced in between is not bound to the message. The scan covers one account: occurrences of other accounts, and
of this one outside the walked root, are left alone.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .database import Database
from .maildir import is_trashed
from .occurrences import OccurrenceLocatorError, locate

log = logging.getLogger("indexer.occurrence_scan")

# Bytes read per call when hashing a file.
_HASH_CHUNK_BYTES = 1 << 20


@dataclass
class ScanCounts:
    """What one scan did. ``unreadable`` counts directories the walk
    could not read; ``complete`` says it read them all."""

    renamed: int = 0
    retired: int = 0
    bound: int = 0
    unreachable: int = 0
    unverified: int = 0
    hashed: int = 0
    unreadable: int = 0
    complete: bool = True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _walk(root: Path, account_root: Path, counts: ScanCounts) -> dict[str, Path]:
    """Every message file under ``root`` (a file in a ``cur`` or ``new``
    directory, at any depth), keyed by its path relative to
    ``account_root``. Marks ``counts`` partial when ``root`` is not a
    readable directory or a directory under it cannot be read; a
    directory that vanished mid-walk is gone, not unreadable."""
    files: dict[str, Path] = {}
    if not root.is_dir():
        counts.complete = False
        counts.unreadable += 1
        return files

    def _on_error(exc: OSError) -> None:
        if not isinstance(exc, FileNotFoundError) or Path(str(exc.filename)) == root:
            counts.unreadable += 1
            counts.complete = False

    for dirpath, _dirnames, filenames in os.walk(root, onerror=_on_error):
        if os.path.basename(dirpath) not in ("cur", "new"):
            continue
        for name in filenames:
            path = Path(dirpath, name)
            try:
                files[locate(path, account_root).relative_path] = path
            except OccurrenceLocatorError:
                # Outside the account's root (a root that is not the
                # account's): nothing can be said about it.
                counts.complete = False
    return files


def file_identity(path: Path) -> tuple[int, int, int] | None:
    """``path``'s size, mtime and inode, or ``None`` when it cannot be
    read: taken before its bytes are verified and checked again inside
    the write (``Database.move_verified_occurrence``,
    ``bind_found_copy``), so a file replaced in between is refused."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns, st.st_ino


def _stat_identity(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def _recorded_hash(db: Database, recorded_at: Path, path: Path) -> str | None:
    """The content hash ``indexed_files`` records for ``recorded_at``,
    when ``path`` has the size and mtime recorded with it (the file
    identity the Maildir walk trusts, docs/architecture.md "File
    Identity on ``indexed_files``"): a rename keeps both."""
    identity = db.file_identity(str(recorded_at))
    if identity is None or identity["content_hash"] is None:
        return None
    if _stat_identity(path) != (identity["size"], identity["mtime_ns"]):
        return None
    return str(identity["content_hash"])


def _verify(
    db: Database, old: Path, candidate: Path, content_hash: str, counts: ScanCounts
) -> bool:
    """Whether ``candidate`` holds the bytes whose hash is
    ``content_hash``: by the identity recorded for ``old`` (the file
    before the rename) or ``candidate``, else by hashing it."""
    for recorded_at in (old, candidate):
        recorded = _recorded_hash(db, recorded_at, candidate)
        if recorded is not None:
            return recorded == content_hash
    try:
        counts.hashed += 1
        return _sha256(candidate) == content_hash
    except OSError:
        return False


def scan_occurrences(
    db: Database,
    root: Path,
    *,
    hash_cache: dict[tuple[str, int, int, int], str] | None = None,
) -> ScanCounts:
    """Reconcile the account's active occurrences under ``root`` with
    the files there (see the module docstring) and return the counts.

    ``hash_cache`` enables
    hashing of unindexed ``T``-flagged or dead-lettered files, keyed by
    path, size, mtime and inode so each file is read once per process.
    """
    counts = ScanCounts()
    account_root = db.account.maildir_root
    files = _walk(root, account_root, counts)
    try:
        scope = PurePosixPath(root).relative_to(PurePosixPath(account_root))
    except ValueError:
        # ``root`` is not inside the account's root: nothing it holds
        # can be an occurrence of the account.
        counts.complete = False
        return counts
    by_uniq: dict[tuple[str, str], list[str]] = {}
    for relative in files:
        where = locate(files[relative], account_root)
        by_uniq.setdefault((where.folder, where.uniq), []).append(relative)

    occurrences = db.active_occurrences()
    held = {o["relative_path"] for o in occurrences}
    for o in occurrences:
        relative = o["relative_path"]
        if relative in files:
            continue
        if scope.parts and PurePosixPath(relative).parts[: len(scope.parts)] != scope.parts:
            continue
        old = account_root / relative
        candidates = [c for c in by_uniq.get((o["folder"], o["uniq"]), []) if c not in held]
        moved = False
        for candidate in sorted(candidates):
            identity = file_identity(files[candidate])
            if (
                identity is None
                or o["content_hash"] is None
                or not _verify(db, old, files[candidate], o["content_hash"], counts)
            ):
                continue
            if db.move_verified_occurrence(
                o["id"], relative, str(files[candidate]), identity=identity
            ):
                held.add(candidate)
                counts.renamed += 1
                moved = True
                break
        if moved:
            continue
        if candidates:
            # A file with its uniq is there, but not with its bytes (or
            # the move was refused): not a removal the scan can establish.
            counts.unverified += 1
        elif counts.complete:
            if db.retire_vanished_occurrence(o["id"], relative):
                counts.retired += 1
            else:
                counts.unreachable += 1
        else:
            counts.unreachable += 1

    for relative, path in sorted(files.items()):
        if relative in held:
            continue
        identity = file_identity(path)
        if identity is None:
            continue
        content_hash = _recorded_hash(db, path, path)
        if content_hash is None and hash_cache is not None:
            content_hash = _hash_unindexed(db, path, identity, hash_cache, counts)
        if content_hash is None:
            continue
        claimant_id = db.claimant_for_content(content_hash)
        if claimant_id is not None and db.bind_found_copy(
            claimant_id, str(path), identity=identity
        ):
            held.add(relative)
            counts.bound += 1
    return counts


def _hash_unindexed(
    db: Database,
    path: Path,
    identity: tuple[int, int, int],
    hash_cache: dict[tuple[str, int, int, int], str],
    counts: ScanCounts,
) -> str | None:
    """The content hash of a file the walk never indexes on its own: a
    ``T``-flagged one (mirror retention parks it) or one whose job is
    dead-lettered. Any other unindexed file is new mail the walk queues,
    and its parse binds it. Cached by path and ``identity`` (size, mtime
    and inode, as the bind re-checks), so a replacement with the same
    size and mtime is hashed again (review round 2 on #1612)."""
    if not is_trashed(path) and db.queue_get_status(str(path)) != "dead":
        return None
    key = (str(path), *identity)
    if key not in hash_cache:
        try:
            counts.hashed += 1
            hash_cache[key] = _sha256(path)
        except OSError:
            return None
    return hash_cache[key]
