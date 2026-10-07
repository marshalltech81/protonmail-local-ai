#!/bin/bash
set -Eeuo pipefail

# Copy the live SQLite index to an operator-chosen host directory while
# the stack keeps running (#1005). Run through `make backup-index
# BACKUP_DIR=<dir>`.
#
# The indexer container takes the copy with SQLite's online backup API
# from a read-only connection: one backup step reads a single WAL
# snapshot, so the copy is consistent while the indexer writes. The copy
# is written next to the index in the index volume, switched to
# rollback-journal mode so it is one self-contained file, and checked
# with PRAGMA integrity_check there. It is then streamed to the host over
# the exec's stdout into a file created mode 600, its SHA-256 is compared
# with the container's, and the in-volume copy is removed. No mount is
# added and the hardened containers are not changed.
#
# The copy holds the whole mailbox (AGENTS.md "Do not export mail in
# bulk"): it is written only to BACKUP_DIR, never inside the checkout.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

# Runs inside the indexer container: python -c CODE MODE TAG.
# make: back up, check and describe the copy; stream: write it to
# stdout; remove: delete it. Paths derive from SQLITE_PATH.
BACKUP_PY='
import hashlib, os, sqlite3, sys, time
from contextlib import closing
from pathlib import Path

mode, tag = sys.argv[1], sys.argv[2]
db = Path(os.environ["SQLITE_PATH"])
copy = db.with_name(f".backup-index-{tag}.db")
if mode == "make":
    # Reclaim what an interrupted run left behind (killed before its
    # cleanup ran): copies of earlier backups, and the file a restore
    # stages before swapping it in (#1054; restore-index names it). Only
    # those untouched for 6 hours, so a backup still running in another
    # shell, or a restore still writing, keeps its file.
    cutoff = time.time() - 6 * 3600

    def stale(path):
        try:
            return path.stat().st_mtime < cutoff
        except FileNotFoundError:
            # Absent, or removed by another backup sweep between the
            # listing and this check.
            return False

    copies = [p for p in db.parent.glob(".backup-index-*") if stale(p)]
    stagings = [p for p in (db.with_name(".restore-index.db"),) if stale(p)]
    for path in copies + stagings:
        path.unlink(missing_ok=True)
    if copies or stagings:
        print(
            f"Removed {len(copies)} stale backup copy(s) and {len(stagings)} stale restore staging file(s) from the index volume",
            file=sys.stderr,
        )
    os.umask(0o077)
    copy.unlink(missing_ok=True)
    with (
        closing(sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(copy)) as dst,
    ):
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
    with closing(sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True)) as conn:
        result = "; ".join(row[0] for row in conn.execute("PRAGMA integrity_check(20)"))
        try:
            version = str(conn.execute("SELECT version FROM schema_version").fetchone()[0])
        except (sqlite3.Error, TypeError):
            version = "none"
        # The same identity restore-index requires, so a copy it would
        # refuse is never reported as a good backup.
        from src.database import SCHEMA_APPLICATION_ID
        ours = "yes" if conn.execute("PRAGMA application_id").fetchone()[0] == SCHEMA_APPLICATION_ID else "no"
    digest = hashlib.sha256()
    with copy.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    print(copy.stat().st_size, digest.hexdigest(), version, ours, result)
elif mode == "stream":
    with copy.open("rb") as handle:
        while block := handle.read(1 << 20):
            sys.stdout.buffer.write(block)
elif mode == "remove":
    for path in (copy, Path(f"{copy}-journal")):
        path.unlink(missing_ok=True)
'

die() {
    printf 'backup-index: %s\n' "$1" >&2
    exit 1
}

# Prints the physical absolute path of $1, which need not exist yet:
# the nearest existing ancestor is resolved with pwd -P (so symlinks
# cannot hide the checkout) and the rest is appended as given.
resolve_path() {
    local path="$1" rest="" part
    # zsh passes make BACKUP_DIR=~/dir with the ~ unexpanded.
    if [[ "$path" == \~ || "$path" == \~/* ]]; then
        path="$HOME${path#\~}"
    fi
    [[ "$path" == /* ]] || path="$PWD/$path"
    while [[ "$path" == */ && "$path" != / ]]; do
        path="${path%/}"
    done
    while [[ ! -d "$path" ]]; do
        [[ ! -e "$path" ]] || die "BACKUP_DIR exists and is not a directory"
        part="${path##*/}"
        [[ "$part" != ".." && "$part" != "." ]] || die "BACKUP_DIR must not contain . or .. after a missing directory"
        rest="/$part$rest"
        path="${path%/*}"
        [[ -n "$path" ]] || path=/
    done
    path="$(cd "$path" && pwd -P)"
    printf '%s%s\n' "${path%/}" "$rest"
}

[[ -n "${BACKUP_DIR:-}" ]] || die "set BACKUP_DIR to a directory outside the checkout, for example make backup-index BACKUP_DIR=~/protonmail-local-ai-backup"

dir="$(resolve_path "$BACKUP_DIR")"
if [[ "$dir" == "$REPO" || "$dir" == "$REPO"/* ]]; then
    die "BACKUP_DIR is inside the checkout ($REPO); the copy holds the whole mailbox, choose a directory outside it"
fi

running=$(docker ps --quiet --filter 'name=^indexer$' --filter status=running)
[[ -n "$running" ]] || die "the indexer is not running; start the stack with make up"

# Succeeds when $1 carries an ACL. ACL entries are checked before the
# mode bits and can grant other accounts access to a mode-700 directory,
# or be inherited by what is created in it. macOS lists them with ls -e
# (the mode's + is hidden behind @ when extended attributes exist);
# elsewhere ls marks them with a + after the mode.
has_acl() {
    local listing
    if [[ "$(uname)" == Darwin ]]; then
        listing=$(ls -lde -- "$1")
        grep -qE '^ *[0-9]+: ' <<<"$listing"
    else
        listing=$(ls -ld -- "$1")
        [[ "${listing:10:1}" == + ]]
    fi
}

# require_private PATH [NAME]: refuses PATH (reported as NAME) when group
# or other mode bits, or an ACL, give access to it.
require_private() {
    if [[ -n "$(find "$1" -maxdepth 0 \( -perm -g=r -o -perm -g=w -o -perm -g=x -o -perm -o=r -o -perm -o=w -o -perm -o=x \) -print)" ]] ||
        has_acl "$1"; then
        die "${2:-$1} is accessible to other users (mode bits or an ACL); run chmod 700 (and chmod -N on macOS) on it or choose a new directory"
    fi
}

if [[ ! -d "$dir" ]]; then
    (umask 077 && mkdir -p "$dir")
fi
# Work inside the directory itself from here on: every path below is
# relative to it, so renaming or replacing $dir (or an ancestor) while
# the copy runs cannot redirect the file somewhere unchecked. The check
# runs on the directory entered, and also after creating it, since a new
# directory can inherit ACL entries.
cd -- "$dir"
here=$(pwd -P)
[[ "$here" != "$REPO" && "$here" != "$REPO"/* ]] || die "BACKUP_DIR is inside the checkout ($REPO)"
require_private . "$dir"

# The UTC second, then a token unique to this run (the pid and four
# random bytes): two runs that start in the same second would otherwise
# share the in-volume copy's name and remove each other's copy (#1051).
# The host file carries the same name, so it cannot collide either.
stamp=$(date -u +%Y%m%dT%H%M%SZ)
tag="$stamp-$$-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
[[ "$tag" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+-[0-9a-f]{8}$ ]] || die "could not build the backup name (date or od gave an unexpected value)"
dest="mail-$tag.db"
[[ ! -e "$dest" ]] || die "$dir/$dest already exists"

cleanup() {
    docker exec indexer python -c "$BACKUP_PY" remove "$tag" ||
        printf 'backup-index: could not remove the temporary copy .backup-index-%s.db from the index volume\n' "$tag" >&2
    if [[ "${complete:-0}" != 1 ]]; then
        rm -f "$dest"
    fi
}
trap cleanup EXIT

printf 'Copying the index inside the indexer container...\n'
report=$(docker exec indexer python -c "$BACKUP_PY" make "$tag") || die "the indexer could not copy the index (its error is above)"
integrity=""
read -r size digest version ours integrity <<<"$report"
[[ -n "$integrity" ]] || die "the indexer did not report on the copy"
printf 'integrity_check: %s\n' "$integrity"
[[ "$integrity" == ok ]] || die "the copy failed PRAGMA integrity_check; nothing was written to $dir"
# Refuse what restore-index would refuse.
[[ "$ours" == yes ]] || die "the copy is not a protonmail-local-ai index (application ID); nothing was written to $dir"
[[ "$version" =~ ^[0-9]+$ ]] || die "the copy has no schema version (the indexer may still be creating the index); nothing was written to $dir"

(
    umask 077
    set -o noclobber
    docker exec indexer python -c "$BACKUP_PY" stream "$tag" >"$dest"
)
host_digest=$(shasum -a 256 "$dest")
host_digest="${host_digest%% *}"
[[ "$host_digest" == "$digest" ]] || die "the file written to the host does not match the copy in the container"
chmod 600 "$dest"
require_private "$dest" "$dir/$dest"
complete=1

printf 'Index backup: %s\n' "$dir/$dest"
printf 'Size: %s bytes, schema version %s, SHA-256 %s\n' "$size" "$version" "$digest"
