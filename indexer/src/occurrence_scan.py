"""Reconcile mailbox copies using bounded, identity-bound byte evidence.

One complete traversal supplies all same-uniq candidates and directory
inventories. Proofs are prepared before occurrence writes. Every scan
hash shares a 500-file/100MiB admission budget; failed attempts spend it,
reads cannot exceed the captured size, and a rotating admission point
prevents unreadable early paths from starving later ones. Individually
oversized files remain visibly unverified under the existing cap.

Stable digests, including mismatches, are cached by path, size, mtime,
inode, ctime and device. A changed identity is never a cache hit; stale
locators are pruned after a complete traversal. Metadata alone never
proves equality. A different authoritative message size proves inequality.

A missed rename follows a verified matching file. A mismatch retires its
old occurrence only when every current same-uniq candidate is proved
not to hold that claimant, including held copies. Unknown reads, changed
proofs and mutated directory inventories defer the decision. The write
transaction rechecks file identities and the inventory; each directory
is listed once per pass. A partial traversal establishes no absence.

Held paths receive the same byte backstop. Retirement and parse admission
are atomic, preserving existing queued retries and dead letters. Only
mirror retention tombstones messages; this scan never removes a message.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .database import Database
from .file_evidence import MAX_HASH_BYTES, MAX_HASH_FILES, FolderInventory, read_digest
from .file_evidence import file_identity as file_identity
from .maildir import is_trashed
from .occurrences import OccurrenceLocatorError, locate
from .queue import REASON_STALE_CONTENT

log = logging.getLogger("indexer.occurrence_scan")

# Per-pass budget for the content backstop's hashing of held paths whose
# identity has not been verified before (cache misses only — a path
# already verified at its current size/mtime/inode/ctime/device costs one dict lookup,
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
_BACKSTOP_MAX_FILES = MAX_HASH_FILES
_BACKSTOP_MAX_BYTES = MAX_HASH_BYTES
# One configured account per process; retain the next admission point across passes.
_scan_next: dict[tuple[str, str], str] = {}
_scan_first: dict[str, str] = {}


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
    hash_deferred: int = 0
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


def _sha256(path: Path, identity=None) -> str:
    identity = identity if identity is not None else file_identity(path)
    if identity is None:
        raise FileNotFoundError("file unavailable")
    return read_digest(path, identity)


class _ScanHashes:
    """One budget for every scan read, including failed attempts."""

    def __init__(self, cache, counts):
        self.cache = cache if cache is not None else {}
        self.counts = counts
        self.files = 0
        self.bytes = 0
        self.preparing = True
        self.attempted = set()
        self.attempted_paths = set()
        self.deferred_paths = set()

    def digest(self, path, identity):
        key = (str(path), *identity)
        if key in self.cache:
            return self.cache[key]
        if key in self.attempted:
            return None
        if not self.preparing:
            if str(path) not in self.attempted_paths:
                self.counts.hash_deferred += 1
            return None
        if self.files >= _BACKSTOP_MAX_FILES or self.bytes + identity[0] > _BACKSTOP_MAX_BYTES:
            self.deferred_paths.add(str(path))
            return None
        self.attempted.add(key)
        self.attempted_paths.add(str(path))
        self.files += 1
        self.bytes += identity[0]
        self.counts.hashed += 1
        try:
            digest = _sha256(path, identity)
        except OSError:
            return None
        self.cache[key] = digest
        return digest


def _walk(
    root: Path, account_root: Path, counts: ScanCounts, inventory: FolderInventory
) -> dict[str, Path]:
    """One traversal also captures the inventories used at write boundaries."""
    files: dict[str, Path] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            before = file_identity(directory)
            with os.scandir(directory) as entries:
                children = list(entries)
            if directory.name in {"cur", "new"}:
                inventory.seed(directory, before, [Path(e.path) for e in children])
            for entry in children:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif directory.name in {"cur", "new"}:
                    path = Path(entry.path)
                    files[locate(path, account_root).relative_path] = path
        except (OSError, OccurrenceLocatorError) as exc:
            if not isinstance(exc, FileNotFoundError) or directory == root:
                counts.complete = False
                counts.unreadable += 1
    return files


@dataclass(frozen=True)
class RenameVerification:
    """The result of checking whether ``new_path`` still holds a
    claimant's bytes after a watcher-observed rename (#1612 round 14).
    ``identity`` is ``new_path``'s size, mtime, inode, ctime and device, captured at
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

    identity: tuple[int, int, int, int, int] | None
    mismatch: bool = False
    mismatch_identity: tuple[int, int, int, int, int] | None = None


def verify_rename_bytes(
    db: Database, old_path: str, new_path: str, claimant_id: str
) -> RenameVerification:
    """Verify against the claimant's full hash and size, never a reused path's hash.

    A different size proves a mismatch without reading; otherwise the
    descriptor-bound hash reads at most that size. Unknown or changed
    files provide no proof. Both outcomes carry identity evidence for
    the caller's transaction guard.
    """
    identity = file_identity(Path(new_path))
    if identity is None:
        return RenameVerification(None)
    expected = db.message_content(claimant_id)
    if expected is None or expected["content_hash"] is None or expected["size_bytes"] is None:
        return RenameVerification(None)
    if expected["size_bytes"] is not None and identity[0] != expected["size_bytes"]:
        return RenameVerification(None, True, identity)
    try:
        digest = _sha256(Path(new_path), identity)
    except OSError:
        return RenameVerification(None)
    if digest == expected["content_hash"]:
        return RenameVerification(identity)
    return RenameVerification(None, True, identity)


def scan_occurrences(
    db: Database,
    root: Path,
    *,
    hash_cache: dict[tuple[str, int, int, int, int, int], str] | None = None,
) -> ScanCounts:
    """Reconcile the account's active occurrences under ``root`` with
    the files there (see the module docstring) and return the counts.

    ``hash_cache`` enables hashing of unindexed ``T``-flagged or
    dead-lettered files, and the content backstop of held paths whose
    file did not move this pass, keyed by path, size, mtime, inode, ctime and device so
    each distinct file identity is read once per process.
    """
    counts = ScanCounts()
    account_root = db.account.maildir_root
    inventory = FolderInventory()
    files = _walk(root, account_root, counts, inventory)
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
    hashes = _ScanHashes(hash_cache, counts)
    needed = set()
    backstop = set()
    for o in occurrences:
        relative = o["relative_path"]
        if relative in files:
            identity = file_identity(files[relative])
            if (
                hash_cache is not None
                and identity is not None
                and (o["size_bytes"] is None or identity[0] == o["size_bytes"])
            ):
                backstop.add(relative)
        else:
            for candidate in by_uniq.get((o["folder"], o["uniq"]), []):
                identity = file_identity(files[candidate])
                if identity is not None and (
                    o["size_bytes"] is None or identity[0] == o["size_bytes"]
                ):
                    needed.add(candidate)
    for relative, path in files.items():
        if relative not in held and (
            db.is_indexed(str(path))
            or (
                hash_cache is not None
                and (is_trashed(path) or db.queue_get_status(str(path)) == "dead")
            )
        ):
            needed.add(relative)
    # Startup follows renames before the initial walk can reparse them;
    # routine held-path verification spends only the remaining budget.
    groups = [("copies", needed), ("backstop", backstop - needed)]
    if _scan_first.get(str(root)) == "backstop":
        groups.reverse()
    pending_groups = set()
    for group, candidates in groups:
        ordered = sorted(candidates)
        if not ordered:
            continue
        if hashes.files >= _BACKSTOP_MAX_FILES or hashes.bytes >= _BACKSTOP_MAX_BYTES:
            pending_groups.add(group)
            continue
        cursor = str(root), group
        start = _scan_next.get(cursor, "")
        pivot = next((i for i, name in enumerate(ordered) if name >= start), 0)
        ordered = ordered[pivot:] + ordered[:pivot]
        next_start = None
        for i, relative in enumerate(ordered):
            path = files[relative]
            identity = file_identity(path)
            if identity is not None:
                if hashes.digest(path, identity) is None:
                    pending_groups.add(group)
                if (
                    str(path) in hashes.deferred_paths
                    and identity[0] <= _BACKSTOP_MAX_BYTES
                    and next_start is None
                ):
                    next_start = relative
            if hashes.files >= _BACKSTOP_MAX_FILES or hashes.bytes >= _BACKSTOP_MAX_BYTES:
                if i + 1 < len(ordered):
                    pending_groups.add(group)
                if next_start is None:
                    next_start = ordered[(i + 1) % len(ordered)]
                break
        if next_start is not None:
            _scan_next[cursor] = next_start
    if len(pending_groups) == 2:
        _scan_first[str(root)] = "backstop" if groups[0][0] == "copies" else "copies"
    elif pending_groups:
        _scan_first[str(root)] = next(iter(pending_groups))
    hashes.preparing = False
    # Warm inventories before byte verification, so mutation during a
    # proof cannot become a newly accepted absence at the write boundary.
    for o in occurrences:
        inventory.candidates(account_root / o["relative_path"])
    for o in occurrences:
        relative = o["relative_path"]
        if scope.parts and PurePosixPath(relative).parts[: len(scope.parts)] != scope.parts:
            continue
        old = account_root / relative
        if relative in files:
            if hash_cache is not None and o["content_hash"] is not None:
                identity = file_identity(old)
                if identity is None:
                    counts.unverified += 1
                    continue
                before = counts.hash_deferred
                size_mismatch = o["size_bytes"] is not None and identity[0] != o["size_bytes"]
                digest = None if size_mismatch else hashes.digest(old, identity)
                counts.backstop_deferred += counts.hash_deferred - before
                if digest is None and not size_mismatch and counts.hash_deferred == before:
                    counts.unverified += 1
                if size_mismatch or (digest is not None and digest != o["content_hash"]):
                    with db.transaction():
                        retired = db.retire_mismatched_occurrence(
                            o["id"], relative, identity=identity
                        )
                        if retired:
                            db.queue_enqueue_if_absent(str(old), REASON_STALE_CONTENT)
                    if retired:
                        counts.stale += 1
                    else:
                        counts.unreachable += 1
            continue
        same_uniq = by_uniq.get((o["folder"], o["uniq"]), [])
        proven = {}
        moved = False
        for candidate in sorted(same_uniq):
            path = files[candidate]
            identity = file_identity(path)
            if identity is None or o["content_hash"] is None:
                continue
            if o["size_bytes"] is not None and identity[0] != o["size_bytes"]:
                proven[str(path)] = identity
                continue
            digest = hashes.digest(path, identity)
            if digest is None:
                continue
            if digest != o["content_hash"]:
                proven[str(path)] = identity
            elif candidate not in held and db.move_verified_occurrence(
                o["id"], relative, str(path), identity=identity
            ):
                held.add(candidate)
                counts.renamed += 1
                moved = True
                break
        if moved:
            continue
        if proven and len(proven) == len(same_uniq):
            if db.retire_mismatched_rename(o["id"], str(old), proven=proven, inventory=inventory):
                counts.retired += 1
            else:
                counts.unreachable += 1
        elif same_uniq:
            counts.unverified += 1
        elif counts.complete:
            if db.retire_vanished_occurrence(o["id"], relative, inventory=inventory):
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
        recorded = db.file_identity(str(path))
        # Ordinary new mail belongs to the parser. The scan also binds
        # copies parked by retention or dead-lettered before parsing.
        if recorded is None and (
            hash_cache is None
            or (not is_trashed(path) and db.queue_get_status(str(path)) != "dead")
        ):
            continue
        digest = hashes.digest(path, identity)
        if digest is None:
            continue
        claimant_id = db.claimant_for_content(digest)
        if claimant_id is not None and db.bind_found_copy(
            claimant_id, str(path), identity=identity
        ):
            held.add(relative)
            counts.bound += 1
    if hash_cache is not None and counts.complete:
        # A changed identity replaces its previous cache entry; vanished
        # locators do not accumulate indefinitely across flag renames.
        current = {str(path): file_identity(path) for path in files.values()}
        for key in list(hash_cache):
            if current.get(key[0]) != key[1:]:
                del hash_cache[key]
    return counts
