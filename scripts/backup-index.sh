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

# Runs inside the indexer container: python -c CODE MODE STAMP.
# make: back up, check and describe the copy; stream: write it to
# stdout; remove: delete it. Paths derive from SQLITE_PATH.
BACKUP_PY='
import hashlib, os, sqlite3, sys
from contextlib import closing
from pathlib import Path

mode, stamp = sys.argv[1], sys.argv[2]
db = Path(os.environ["SQLITE_PATH"])
copy = db.with_name(f".backup-index-{stamp}.db")
if mode == "make":
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
    digest = hashlib.sha256()
    with copy.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    print(copy.stat().st_size, digest.hexdigest(), version, result)
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

if [[ -d "$dir" ]]; then
    if [[ -n "$(find "$dir" -maxdepth 0 \( -perm -g=r -o -perm -g=w -o -perm -g=x -o -perm -o=r -o -perm -o=w -o -perm -o=x \) -print)" ]]; then
        die "$dir is accessible to other users; run chmod 700 on it or choose a new directory"
    fi
else
    (umask 077 && mkdir -p "$dir")
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
dest="$dir/mail-$stamp.db"
[[ ! -e "$dest" ]] || die "$dest already exists"

cleanup() {
    docker exec indexer python -c "$BACKUP_PY" remove "$stamp" ||
        printf 'backup-index: could not remove the temporary copy .backup-index-%s.db from the index volume\n' "$stamp" >&2
    if [[ "${complete:-0}" != 1 ]]; then
        rm -f "$dest"
    fi
}
trap cleanup EXIT

printf 'Copying the index inside the indexer container...\n'
report=$(docker exec indexer python -c "$BACKUP_PY" make "$stamp") || die "the indexer could not copy the index (its error is above)"
integrity=""
read -r size digest version integrity <<<"$report"
[[ -n "$integrity" ]] || die "the indexer did not report on the copy"
printf 'integrity_check: %s\n' "$integrity"
[[ "$integrity" == ok ]] || die "the copy failed PRAGMA integrity_check; nothing was written to $dir"

(
    umask 077
    set -o noclobber
    docker exec indexer python -c "$BACKUP_PY" stream "$stamp" >"$dest"
)
host_digest=$(shasum -a 256 "$dest")
host_digest="${host_digest%% *}"
[[ "$host_digest" == "$digest" ]] || die "the file written to the host does not match the copy in the container"
chmod 600 "$dest"
complete=1

printf 'Index backup: %s\n' "$dest"
printf 'Size: %s bytes, schema version %s, SHA-256 %s\n' "$size" "$version" "$digest"
