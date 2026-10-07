#!/bin/bash
set -Eeuo pipefail

# Replace the index with a copy made by `make backup-index` (#1005). Run
# through `make restore-index BACKUP=<file>`.
#
# Asks for confirmation, then stops the indexer and mcp-server (the only
# services that open the index) and streams the copy over stdin into a
# one-off container of the indexer's image with its index volume, the
# indexer's user, a read-only root, no capabilities and no network (no
# host mount, so a stopped stack's overlays and project name do not
# matter). That container writes it next to the index,
# refuses it unless PRAGMA integrity_check is ok, the application ID is
# this project's and its schema version is not above the code's, then
# checkpoints the old index's WAL into it, removes its -wal and -shm
# files and renames the copy over the index. Removing the WAL first
# matters: an old WAL left beside the restored file would be replayed
# into it. The indexer is started and its startup lines (schema version,
# embedder identity) are printed; mcp-server starts once the indexer has
# verified the restored index. A refused file leaves the index unchanged
# and both services are started again.

# Runs inside a one-off indexer container: python -c CODE, copy on stdin.
RESTORE_PY='
import os, sqlite3, sys
from contextlib import closing
from pathlib import Path

from src.database import SCHEMA_APPLICATION_ID, SCHEMA_VERSION

db = Path(os.environ["SQLITE_PATH"])
staged = db.with_name(".restore-index.db")
os.umask(0o077)
try:
    with staged.open("wb") as handle:
        while block := sys.stdin.buffer.read(1 << 20):
            handle.write(block)
        handle.flush()
        os.fsync(handle.fileno())
    with closing(sqlite3.connect(f"{staged.as_uri()}?mode=ro", uri=True)) as conn:
        result = "; ".join(row[0] for row in conn.execute("PRAGMA integrity_check(20)"))
        print(f"integrity_check: {result}")
        if result != "ok":
            sys.exit("refused: the backup failed PRAGMA integrity_check")
        if conn.execute("PRAGMA application_id").fetchone()[0] != SCHEMA_APPLICATION_ID:
            sys.exit("refused: the file is not a protonmail-local-ai index")
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    print(f"backup schema version: {version} (code: {SCHEMA_VERSION})")
    if version > SCHEMA_VERSION:
        sys.exit("refused: the backup schema is newer than this code; run the release that wrote it")
    # Fold the current index WAL into its main file first, so that file
    # alone holds every committed transaction if the swap below fails or
    # is interrupted after the sidecars are gone.
    if db.exists():
        with closing(sqlite3.connect(db)) as live:
            busy = live.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        if busy:
            sys.exit("refused: the current index is still in use")
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    os.replace(staged, db)
except BaseException as exc:
    # Any failure, a full volume included, leaves no staged copy behind.
    staged.unlink(missing_ok=True)
    if isinstance(exc, sqlite3.Error):
        sys.exit(f"refused: SQLite error ({type(exc).__name__})")
    if isinstance(exc, OSError):
        sys.exit(f"refused: {type(exc).__name__} (errno {exc.errno}) in the index volume")
    raise
print(f"restored {db}")
'

die() {
    printf 'restore-index: %s\n' "$1" >&2
    exit 1
}

[[ -n "${BACKUP:-}" ]] || die "set BACKUP to a file written by make backup-index, for example make restore-index BACKUP=~/protonmail-local-ai-backup/mail-20261007T120000Z.db"
# zsh passes make BACKUP=~/file with the ~ unexpanded.
if [[ "$BACKUP" == \~/* ]]; then
    BACKUP="$HOME${BACKUP#\~}"
fi
[[ -f "$BACKUP" ]] || die "$BACKUP is not a file"

# The indexer container's own image and index volume, whatever project
# name or overlays created it. The container must exist (make up first).
image=$(docker inspect --format '{{.Image}}' indexer) || die "no indexer container; start the stack with make up first"
volume=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' indexer)
[[ -n "$image" && -n "$volume" ]] || die "cannot find the indexer's image or index volume"

printf 'This stops the indexer and mcp-server and replaces the search index with\n'
printf '  %s\n' "$BACKUP"
printf 'The current index is deleted; take a copy first with make backup-index if you may want it.\n'
printf 'Mail that arrived after the backup is indexed again from Maildir when the indexer starts.\n'
read -r -p "Restore? (yes/no): " confirm || confirm=""
[[ "$confirm" == yes ]] || die "not restored"

docker stop mcp-server indexer

# Until the copy is in place, any exit starts both services again on the
# unchanged index. After it, mcp-server starts only once the indexer has
# migrated and verified the restored index: docker start does not apply
# Compose's depends_on, and mcp-server must not serve a schema the
# indexer is still migrating.
restored=0
on_exit() {
    if [[ "$restored" == 0 ]]; then
        docker start indexer mcp-server ||
            printf 'restore-index: could not start indexer and mcp-server; run make up\n' >&2
    elif [[ "$restored" == 1 ]]; then
        printf 'restore-index: mcp-server was left stopped; once the indexer is healthy, run make up\n' >&2
    fi
}
trap on_exit EXIT

since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
docker run --rm -i --network none --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges:true --user 1002:1002 \
    --env SQLITE_PATH=/data/mail.db --volume "$volume:/data" \
    "$image" python -c "$RESTORE_PY" <"$BACKUP" ||
    die "the index was not replaced (the reason is above); the previous index is unchanged"
restored=1
docker start indexer

# Wait (bounded) for the indexer to verify or refuse the embedder
# identity, printing its schema and embedder startup lines.
wait_secs="${RESTORE_WAIT_SECONDS:-900}"
[[ "$wait_secs" =~ ^[0-9]+$ ]] || die "RESTORE_WAIT_SECONDS must be a whole number of seconds"
deadline=$((SECONDS + wait_secs))
pattern='Startup identity|Migrating database|Database ready|Embedder identity|embedder is not the one|Schema version mismatch'
while :; do
    logs=$(docker logs --since "$since" indexer 2>&1)
    if grep -qE 'Embedder identity (verified|recorded)|Recorded embedder identity|embedder is not the one|Schema version mismatch' <<<"$logs"; then
        break
    fi
    if ((SECONDS >= deadline)); then
        printf '%s\n' "$logs" | grep -E "$pattern" || true
        die "the indexer did not report its embedder identity within ${wait_secs}s; check docker compose logs indexer"
    fi
    sleep 5
done
lines=$(grep -E "$pattern" <<<"$logs")
printf '%s\n' "$lines"
if grep -qE 'embedder is not the one|Schema version mismatch' <<<"$lines"; then
    die "the indexer refused the restored index; see docs/troubleshooting.md"
fi
docker start mcp-server
restored=2
printf 'Index restored from %s\n' "$BACKUP"
