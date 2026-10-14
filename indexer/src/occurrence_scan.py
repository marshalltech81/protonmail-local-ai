"""Completeness-gated occurrence reconciliation (#1567, PLAN.md decision 47).

The watcher carries an occurrence through every rename it observes
(``Database.update_filepath``). What it misses, while the indexer is
down or when an event is lost, this scan finds by walking the account's
Maildir root and comparing it with the account's active occurrences:

- **Missed renames.** An active occurrence whose file is gone but whose
  folder holds a file with its Maildir uniq (a flag change, a
  ``new``/``cur`` move) moves to that file, once its bytes are verified
  to be the message's (``_verify``): a size/mtime match with the
  recorded file identity is checked, then hashed before it is trusted
  (``_recorded_hash``, #1612 round 14) — a metadata match alone is
  never proof — else the candidate is hashed directly. A candidate
  whose bytes are read and proven not to be the occurrence's (not
  merely unreadable or unresolved) retires it directly (#1612 round
  15): the uniq still resolving to some file would otherwise make
  ``retire_vanished_occurrence`` refuse, leaving the vanished claimant
  active and searchable indefinitely.
- **Found copies.** A file no active occurrence holds whose bytes are an
  indexed message's is bound to it as an occurrence with provenance
  ``scan``: a byte-identical copy the migration seed or the parse never
  bound (a ``T``-flagged file the drain parks, a dead-lettered job, an
  unmapped copy moved across folders, #1141). Its bytes are confirmed
  against its own ``indexed_files`` record (metadata match, then
  hashed) when it has one, else, only for a ``T``-flagged or dead-
  lettered file and with a ``hash_cache``, from hashing it; any other
  unindexed file is left to the Maildir walk, whose parse binds it.
- **Vanished files.** An active occurrence with no such file is retired
  only by a complete scan: the walk read every directory under the root.
  A partial scan (an unreadable directory, a missing root) establishes
  no absence, so it retires nothing; the occurrence stays active and is
  counted as unreachable. Retiring removes a locator, never a message:
  whether the message is reaped is mirror retention's reaper's call
  (``reconciler``), and archive retention never reaps.
- **Content backstop.** A metadata match (size and mtime unchanged) is a
  fast admission shortcut, not proof: mbsync sets a message's mtime from
  IMAP's INTERNALDATE, so two different messages can legitimately share
  one. For a held path whose file did not move this pass (so nothing
  above read its bytes), a bounded number of still-unverified ones per
  pass (``_BACKSTOP_MAX_FILES`` / ``_BACKSTOP_MAX_BYTES``, sized from a
  measured hash throughput, docs/architecture.md "File Identity") are
  hashed and checked against the occurrence's claimant. Once a path's
  exact identity (size, mtime, inode) has been hash-verified, it is
  cached (``hash_cache``) and skipped on every later pass until that
  identity changes again — a genuinely different file landing at the
  same path gets a new inode in the overwhelming case, so this is not
  merely a one-time check. A verified mismatch retires only that
  occurrence (its file is still there, so this is distinct from a
  vanished file) and requests a parse of the new content; an unreadable
  or unstable file is left unverified, not retired. A path the
  per-pass budget leaves unhashed (``_BACKSTOP_MAX_FILES`` /
  ``_BACKSTOP_MAX_BYTES`` already spent, or the file's own size always
  exceeds the byte budget) counts as ``backstop_deferred`` and is
  surfaced in the pass's log line, so an operator can tell this
  secondary check is incomplete rather than silently never running for
  that path.

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
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .database import Database
from .maildir import is_trashed
from .occurrences import OccurrenceLocatorError, locate
from .parser import CLAIMANT_HASH_CHARS
from .queue import REASON_STALE_CONTENT

log = logging.getLogger("indexer.occurrence_scan")

# Bytes read per call when hashing a file.
_HASH_CHUNK_BYTES = 1 << 20

# Per-pass budget for the content backstop's hashing of held paths whose
# identity has not been verified before (cache misses only — a path
# already verified at its current size/mtime/inode costs one dict lookup,
# not a read). Sized from a plain wall-clock measurement on this
# project's own hardware (2026-10-13, Apple Silicon, warm page cache):
# SHA-256 over a 20 MB file took ~8.7 ms (~2.2 GB/s); a typical mail
# body/attachment is far smaller (a 200 KB file took ~0.1 ms). Cold reads
# over real disk or a mounted volume are slower than a warm page-cache
# read, so these budgets assume a much more conservative ~20 MB/s: 100 MB
# at that rate is ~5 seconds, acceptable for a pass that already walks
# the whole Maildir tree. The file cap guards the per-file open/stat
# overhead a byte cap alone would not (many tiny files). At the default
# hourly sweep interval this backstop-verifies a multi-gigabyte mailbox
# within a few days of first running — an eventual backstop for a narrow
# metadata-collision case, not the primary defense (the watcher's own
# rename verification and the Maildir walk's freshness check are).
_BACKSTOP_MAX_FILES = 500
_BACKSTOP_MAX_BYTES = 100 * 1024 * 1024

# A claimant ID is "<Message-ID>#<hash prefix>" (parser.claimant_id); the
# hash prefix is CLAIMANT_HASH_CHARS lowercase hex digits of the file's
# SHA-256, at the end of the string. A Message-ID is attacker-controlled
# and may itself contain "#", so only a trailing token of the right shape
# is trusted as the embedded hash.
_CLAIMANT_HASH_RE = re.compile(rf"#([0-9a-f]{{{CLAIMANT_HASH_CHARS}}})$")


def _claimant_hash_prefix(claimant_id: str) -> str | None:
    """The hash prefix ``claimant_id`` embeds, or ``None`` when it has
    none (a ``Message`` built without file identity, only in tests, or a
    Message-ID that happens to end the same way)."""
    match = _CLAIMANT_HASH_RE.search(claimant_id)
    return match.group(1) if match is not None else None


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
    # A held path's content backstop found its bytes no longer match its
    # occurrence's claimant (#1612): that occurrence was retired and a
    # reparse of the path requested.
    stale: int = 0
    # A held path the content backstop left unhashed this pass because
    # the per-pass budget (``_BACKSTOP_MAX_FILES`` / ``_BACKSTOP_MAX_BYTES``)
    # was already spent, including a single file whose own size always
    # exceeds the byte budget (#1612 round 10 review): stale-content
    # detection is incomplete for these paths until a later pass picks
    # them up.
    backstop_deferred: int = 0
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


def _recorded_hash(db: Database, recorded_at: Path, path: Path, counts: ScanCounts) -> str | None:
    """The content hash ``indexed_files`` records for ``recorded_at``,
    once ``path``'s current bytes are confirmed to hold it: a size and
    mtime match with the identity recorded with it (the file identity
    the Maildir walk trusts, docs/architecture.md "File Identity on
    ``indexed_files``") is a fast admission shortcut, not proof — the
    same reasoning the content backstop and ``verify_rename_bytes``
    rest on, extended here (#1612 round 14) to this scan's own rename-
    matching and found-copy binding, which previously trusted the
    metadata match alone and never read ``path`` at all. Once the
    metadata agrees, ``path`` is hashed and checked against the
    recorded hash before it is trusted. Returns ``None`` on a metadata
    mismatch, no recorded hash, an unreadable file, or a hash mismatch;
    the caller's own fallback covers a genuine mismatch."""
    identity = db.file_identity(str(recorded_at))
    if identity is None or identity["content_hash"] is None:
        return None
    if _stat_identity(path) != (identity["size"], identity["mtime_ns"]):
        return None
    try:
        counts.hashed += 1
        if _sha256(path) != identity["content_hash"]:
            return None
    except OSError:
        return None
    return str(identity["content_hash"])


def _verify(
    db: Database, old: Path, candidate: Path, content_hash: str, counts: ScanCounts
) -> bool | None:
    """Whether ``candidate`` holds the bytes whose hash is
    ``content_hash``: by the identity recorded for ``old`` (the file
    before the rename) or ``candidate``, else by hashing it directly.
    Returns ``None``, not ``False``, when ``candidate`` could not be
    read for the direct hash: inconclusive, not a proven mismatch
    (#1612 round 15) — a caller must not retire an occurrence on this
    result the way it would on a hash-proven ``False``."""
    for recorded_at in (old, candidate):
        recorded = _recorded_hash(db, recorded_at, candidate, counts)
        if recorded is not None:
            return recorded == content_hash
    try:
        counts.hashed += 1
        return _sha256(candidate) == content_hash
    except OSError:
        return None


@dataclass(frozen=True)
class RenameVerification:
    """The result of checking whether ``new_path`` still holds a
    claimant's bytes after a watcher-observed rename (#1612 round 14).
    ``identity`` is ``new_path``'s size, mtime and inode, captured at
    verification time, present only when the bytes are proven to
    match — the caller passes it into ``Database.update_filepath`` as
    ``verified_identity`` for the in-transaction recheck
    (``move_verified_occurrence`` uses the same pattern). ``mismatch``
    is true only when the destination's bytes were actually read and
    found not to be the claimant's: never for a destination this check
    could not read, or whose metadata alone ruled out trusting it
    without reading anything — those leave the destination's status
    unproven either way, and a caller must not treat an unproven result
    as a confirmed mismatch (conflating the two let a trashed-rename
    retirement fire on a merely inconclusive check)."""

    identity: tuple[int, int, int] | None
    mismatch: bool = False


def verify_rename_bytes(
    db: Database, old_path: str, new_path: str, claimant_id: str
) -> RenameVerification:
    """Whether ``new_path`` still holds ``claimant_id``'s bytes after a
    watcher-observed rename from ``old_path`` (#1612): by ``old_path``'s
    ``indexed_files`` identity (a rename keeps size and mtime) when there
    is one, else by hashing ``new_path`` and checking it against the
    claimant ID's embedded hash prefix — the case for a scan-bound
    occurrence, since ``bind_found_copy`` never writes ``indexed_files``.

    Size and mtime agreement with the recorded identity is a fast
    admission shortcut, not proof (mbsync sets mtime from IMAP's
    INTERNALDATE, so two different messages can legitimately share one —
    the same reasoning ``occurrence_scan``'s content backstop and
    ``Database._move_occurrence``'s sibling check both rest on): once
    size and mtime agree, ``new_path`` is also hashed and checked against
    the recorded ``content_hash`` before it is trusted. A record with no
    recorded hash (none expected for current rows, but the column is
    nullable) falls through to the claimant-hash-prefix check below, the
    same as having no prior record at all.

    Returns a ``RenameVerification``: a caller that observed only a
    pre-check, not this return value, must not trust the rename.
    """
    identity = file_identity(Path(new_path))
    if identity is None:
        return RenameVerification(None)
    prior = db.file_identity(old_path)
    if prior is not None:
        if (identity[0], identity[1]) != (prior["size"], prior["mtime_ns"]):
            return RenameVerification(None)
        if prior["content_hash"] is not None:
            try:
                matches = _sha256(Path(new_path)) == prior["content_hash"]
            except OSError:
                return RenameVerification(None)
            return RenameVerification(identity if matches else None, mismatch=not matches)
    expected = _claimant_hash_prefix(claimant_id)
    if expected is None:
        return RenameVerification(None)
    try:
        digest = _sha256(Path(new_path))
    except OSError:
        return RenameVerification(None)
    matches = digest[: len(expected)] == expected
    return RenameVerification(identity if matches else None, mismatch=not matches)


def scan_occurrences(
    db: Database,
    root: Path,
    *,
    hash_cache: dict[tuple[str, int, int, int], str] | None = None,
) -> ScanCounts:
    """Reconcile the account's active occurrences under ``root`` with
    the files there (see the module docstring) and return the counts.

    ``hash_cache`` enables hashing of unindexed ``T``-flagged or
    dead-lettered files, and the content backstop of held paths whose
    file did not move this pass, keyed by path, size, mtime and inode so
    each distinct file identity is read once per process.
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
    # Occurrences whose file is still exactly where it was recorded: the
    # only ones the content backstop (second loop) needs to check, since
    # a moved-to or freshly-bound path is verified by the logic around
    # it, in this same pass.
    unmoved_at: dict[str, sqlite3.Row] = {}
    for o in occurrences:
        relative = o["relative_path"]
        if relative in files:
            unmoved_at[relative] = o
            continue
        if scope.parts and PurePosixPath(relative).parts[: len(scope.parts)] != scope.parts:
            continue
        old = account_root / relative
        candidates = [c for c in by_uniq.get((o["folder"], o["uniq"]), []) if c not in held]
        moved = False
        mismatched = False
        for candidate in sorted(candidates):
            identity = file_identity(files[candidate])
            if identity is None or o["content_hash"] is None:
                continue
            verified = _verify(db, old, files[candidate], o["content_hash"], counts)
            if verified is None:
                continue
            if not verified:
                # A candidate's bytes were read and proven not to be
                # this occurrence's (#1612 round 15), not merely
                # unreadable or unresolved: distinguish this from the
                # inconclusive case below, which retire_vanished_
                # occurrence would refuse anyway (the uniq still
                # resolves to a file).
                mismatched = True
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
        if mismatched:
            # retire_vanished_occurrence would refuse (the uniq still
            # resolves to some file), leaving the vanished claimant
            # active and searchable indefinitely; retire it directly,
            # the sibling of on_moved's trashed-destination retire
            # (round 13) for a watcher-missed rename instead of an
            # observed one.
            if db.retire_mismatched_rename(o["id"], str(old)):
                counts.retired += 1
            else:
                counts.unreachable += 1
        elif candidates:
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

    backstop_budget = {"files": 0, "bytes": 0}
    for relative, path in sorted(files.items()):
        if relative in held:
            occurrence = unmoved_at.get(relative)
            if occurrence is not None:
                _backstop_check(db, occurrence, path, hash_cache, counts, backstop_budget)
            continue
        identity = file_identity(path)
        if identity is None:
            continue
        content_hash = _recorded_hash(db, path, path, counts)
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


def _backstop_check(
    db: Database,
    occurrence: sqlite3.Row,
    path: Path,
    hash_cache: dict[tuple[str, int, int, int], str] | None,
    counts: ScanCounts,
    budget: dict[str, int],
) -> None:
    """The content backstop for a held path whose file did not move this
    pass (module docstring, "Content backstop"). A metadata match alone
    is never trusted here — that is exactly the blind spot this guards.
    Hashes ``path``, within the pass's ``budget``, only when its current
    identity (size, mtime, inode) has not been hash-verified before;
    retires the occurrence on a verified mismatch and requests a parse
    of the new content. ``hash_cache`` is required: with none, the
    backstop is skipped entirely (archive mode's startup-recovery call,
    before #1612, passed none; ``reconciler.sweep_paths`` now always
    does, #1612 point 5)."""
    if hash_cache is None or occurrence["content_hash"] is None:
        return
    identity = file_identity(path)
    if identity is None:
        # Unreadable or gone between the walk and here: unstable, not a
        # removal the scan can establish on its own.
        counts.unverified += 1
        return
    key = (str(path), *identity)
    if key in hash_cache:
        return
    if (
        budget["files"] >= _BACKSTOP_MAX_FILES
        or budget["bytes"] + identity[0] > _BACKSTOP_MAX_BYTES
    ):
        # Checked against the prospective total, not only bytes already
        # consumed (#1612 round 8 review): the latter let the very first
        # held path through untouched whenever it alone exceeded the
        # whole per-pass budget (0 is never >= a positive cap), hashing
        # it in full regardless of size. Left for a later pass, which
        # will see the same cache miss (nothing here has changed its
        # identity) — including a file whose own size always exceeds the
        # budget, an accepted gap in this secondary backstop (module
        # docstring: the watcher's rename verification and the walk's
        # freshness check are the primary defense). Counted rather than
        # left silent (#1612 round 10 review), so an operator can tell
        # stale-content detection is incomplete this pass.
        counts.backstop_deferred += 1
        return
    try:
        digest = _sha256(path)
    except OSError:
        counts.unverified += 1
        return
    counts.hashed += 1
    budget["files"] += 1
    budget["bytes"] += identity[0]
    if digest == occurrence["content_hash"]:
        hash_cache[key] = digest
        return
    # One transaction (#1612 round 15): a crash or SQLite error between
    # the retire and the admission must not leave the retirement
    # committed with no runnable job for the replacement -- both happen
    # or neither does.
    with db.transaction():
        retired = db.retire_mismatched_occurrence(
            occurrence["id"], occurrence["relative_path"], identity=identity
        )
        if retired:
            db.queue_enqueue_if_absent(str(path), REASON_STALE_CONTENT)
    if retired:
        counts.stale += 1
    else:
        # The occurrence moved or retired since active_occurrences() read
        # it, or the file changed again before the retire's transaction
        # (the identity captured above no longer matches): not this
        # scan's call to make any more.
        counts.unreachable += 1


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
