#!/bin/bash
set -Eeuo pipefail

# Tests for scripts/backup-index.sh and scripts/restore-index.sh (#1005),
# behind `make backup-index` and `make restore-index`. A fake `docker`
# first on PATH never contacts a daemon: `exec indexer python -c` and
# `run ... python -c` run the scripts' real Python on the host against a
# synthetic SQLite index in a temporary directory standing in for the
# index volume (SQLITE_PATH), with a stub `src.database` for the restore.
# `ps`, `inspect`, `stop`, `start` and `logs` answer from FAKE_* values.
#
# Run: bash scripts/tests/index_backup_test.sh

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
WORK="$(mktemp -d)"
WORK="$(cd "$WORK" && pwd -P)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0
MARKER=synthetic-mail-marker-1005

mkdir -p "$WORK/bin" "$WORK/stub/src"
touch "$WORK/stub/src/__init__.py"
# The values restore-index.sh imports from the indexer image; a case
# below checks they match indexer/src/database.py.
cat >"$WORK/stub/src/database.py" <<'EOF'
SCHEMA_VERSION = 1
SCHEMA_APPLICATION_ID = 0x504D4149
EOF

cat >"$WORK/bin/docker" <<'EOF'
#!/bin/bash
set -Eeuo pipefail
printf '%s\n' "$*" >>"$FAKE_DOCKER_LOG"
case "$1" in
ps)
    if [[ "$FAKE_RUNNING" == 1 ]]; then
        printf '0123456789ab\n'
    fi
    ;;
exec)
    # docker exec indexer python -c CODE ARGS...
    [[ "$2" == indexer && "$3" == python && "$4" == -c ]]
    SQLITE_PATH="$FAKE_DATA/mail.db" exec python3 -c "$5" "${@:6}"
    ;;
inspect)
    if [[ -n "${FAKE_NO_CONTAINER:-}" ]]; then
        printf 'Error: No such object: indexer\n' >&2
        exit 1
    fi
    if [[ "$3" == *Mounts* ]]; then
        printf 'fake_sqlite-volume\n'
    else
        printf 'sha256:fakeindexerimage\n'
    fi
    ;;
run)
    # docker run FLAGS... sha256:fakeindexerimage python -c CODE
    [[ "$*" == *" --network none "* && "$*" == *" --read-only "* && "$*" == *" --cap-drop ALL "* ]]
    [[ "$*" == *" --volume fake_sqlite-volume:/data "* && "$*" == *" sha256:fakeindexerimage python -c "* ]]
    cd "$FAKE_STUB"
    SQLITE_PATH="$FAKE_DATA/mail.db" PYTHONPATH="$FAKE_STUB" exec python3 -c "${*: -1}"
    ;;
stop | start) ;;
logs)
    printf '%s\n' "$FAKE_LOGS" >&2
    ;;
*)
    printf 'fake docker: unexpected command %s\n' "$1" >&2
    exit 99
    ;;
esac
EOF
chmod +x "$WORK/bin/docker"

# make_db PATH [VERSION] [APPLICATION_ID] [corrupt]: a synthetic WAL-mode
# index carrying the marker, with uncheckpointed rows left in the WAL.
make_db() {
    rm -f "$1" "$1-wal" "$1-shm"
    MARKER="$MARKER" python3 - "$1" "${2:-1}" "${3:-$((0x504D4149))}" "${4:-}" <<'EOF'
import os, sqlite3, sys
path, version, app_id, mode = sys.argv[1:]
marker = os.environ["MARKER"]
conn = sqlite3.connect(path)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute(f"PRAGMA application_id = {int(app_id)}")
conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
conn.execute("INSERT INTO schema_version VALUES (?)", (int(version),))
conn.execute("CREATE TABLE t (a INTEGER, body TEXT)")
conn.execute("CREATE INDEX ti ON t(a)")
conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, marker) for i in range(200)])
conn.commit()
if mode == "corrupt":
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute("UPDATE sqlite_master SET sql='CREATE INDEX ti ON t(a DESC)' WHERE name='ti'")
    conn.commit()
conn.close()
EOF
}

reset() {
    rm -rf "$WORK/data" "$WORK/backups" "$WORK/out" "$WORK/docker.log"
    mkdir -p "$WORK/data"
    : >"$WORK/docker.log"
}

run_backup() {
    PATH="$WORK/bin:$PATH" FAKE_DOCKER_LOG="$WORK/docker.log" FAKE_DATA="$WORK/data" \
        FAKE_RUNNING="${FAKE_RUNNING:-1}" BACKUP_DIR="$1" \
        bash "$REPO/scripts/backup-index.sh" >"$WORK/out" 2>&1 && STATUS=0 || STATUS=$?
}

run_restore() {
    printf '%s\n' "$2" | PATH="$WORK/bin:$PATH" FAKE_DOCKER_LOG="$WORK/docker.log" \
        FAKE_DATA="$WORK/data" FAKE_STUB="$WORK/stub" FAKE_LOGS="${FAKE_LOGS:-}" \
        RESTORE_WAIT_SECONDS="${RESTORE_WAIT_SECONDS:-5}" BACKUP="$1" \
        bash "$REPO/scripts/restore-index.sh" >"$WORK/out" 2>&1 && STATUS=0 || STATUS=$?
}

mode_of() {
    python3 -c 'import os, stat, sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))' "$1"
}

backups() {
    find "$WORK/backups" -type f
}

no_temp_copy_left() {
    [[ -z "$(find "$WORK/data" -name '.backup-index-*')" ]]
}

marker_not_printed() {
    if grep -F "$MARKER" "$WORK/out" >/dev/null; then
        return 1
    fi
}

query() {
    python3 -c 'import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute(sys.argv[2]).fetchone()[0])' "$1" "$2"
}

backup_writes_a_checked_private_copy() {
    reset
    make_db "$WORK/data/mail.db"
    run_backup "$WORK/backups/index"
    [[ "$STATUS" -eq 0 ]]
    local copy
    copy=$(backups)
    [[ "$copy" =~ /backups/index/mail-[0-9]{8}T[0-9]{6}Z\.db$ ]]
    [[ "$(mode_of "$WORK/backups/index")" == 0o700 ]]
    [[ "$(mode_of "$copy")" == 0o600 ]]
    grep -F "Index backup: $copy" "$WORK/out" >/dev/null
    grep -E "^Size: $(wc -c <"$copy" | tr -d ' ') bytes, schema version 1, " "$WORK/out" >/dev/null
    grep -Fx 'integrity_check: ok' "$WORK/out" >/dev/null
    [[ "$(query "$copy" 'SELECT count(*) FROM t')" == 200 ]]
    # One self-contained file: rollback-journal mode, not WAL.
    [[ "$(query "$copy" 'PRAGMA journal_mode')" == delete ]]
    no_temp_copy_left
    marker_not_printed
}

backup_is_consistent_while_writing() {
    reset
    make_db "$WORK/data/mail.db"
    # A writer adds rows in pairs, one transaction per pair, until stopped.
    python3 - "$WORK/data/mail.db" "$WORK/stop" <<'EOF' &
import os, sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1], timeout=10)
deadline = time.monotonic() + 30
while not os.path.exists(sys.argv[2]) and time.monotonic() < deadline:
    with conn:
        conn.execute("INSERT INTO t VALUES (-1, 'x')")
        conn.execute("INSERT INTO t VALUES (-1, 'x')")
EOF
    local writer=$! tries=0
    # Start the backup only once the writer is committing.
    until [[ "$(query "$WORK/data/mail.db" 'SELECT count(*) FROM t WHERE a = -1')" -gt 0 ]]; do
        ((++tries < 100))
        sleep 0.1
    done
    run_backup "$WORK/backups"
    touch "$WORK/stop"
    wait "$writer"
    rm -f "$WORK/stop"
    [[ "$STATUS" -eq 0 ]]
    grep -Fx 'integrity_check: ok' "$WORK/out" >/dev/null
    local copy count
    copy=$(backups)
    count=$(query "$copy" 'SELECT count(*) FROM t WHERE a = -1')
    ((count > 0 && count % 2 == 0))
    [[ "$(query "$copy" 'PRAGMA integrity_check')" == ok ]]
    # The writer kept going after the copy was taken.
    (("$(query "$WORK/data/mail.db" 'SELECT count(*) FROM t WHERE a = -1')" > count))
}

backup_expands_an_unexpanded_tilde() {
    reset
    make_db "$WORK/data/mail.db"
    mkdir -m 700 "$WORK/home"
    # shellcheck disable=SC2088 # the literal ~ zsh passes through make
    HOME="$WORK/home" run_backup '~/backups'
    [[ "$STATUS" -eq 0 ]]
    [[ -n "$(find "$WORK/home/backups" -name 'mail-*.db')" ]]
    rm -rf "${WORK:?}/home"
}

backup_refuses_a_path_inside_the_checkout() {
    local dir
    for dir in "$REPO" "$REPO/backups" "$REPO/scripts/../nested/x"; do
        reset
        make_db "$WORK/data/mail.db"
        run_backup "$dir"
        [[ "$STATUS" -ne 0 ]]
        grep -F 'inside the checkout' "$WORK/out" >/dev/null
        [[ ! -e "$REPO/backups" && ! -e "$REPO/nested" ]]
    done
    # A symlink outside the checkout that points into it.
    reset
    ln -s "$REPO" "$WORK/link"
    run_backup "$WORK/link/backups"
    rm "$WORK/link"
    [[ "$STATUS" -ne 0 ]]
    grep -F 'inside the checkout' "$WORK/out" >/dev/null
    if grep '^exec ' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
}

backup_requires_backup_dir() {
    reset
    run_backup ""
    [[ "$STATUS" -ne 0 ]]
    grep -F 'set BACKUP_DIR' "$WORK/out" >/dev/null
    [[ ! -s "$WORK/docker.log" ]]
}

backup_refuses_a_shared_directory() {
    reset
    make_db "$WORK/data/mail.db"
    mkdir -m 750 "$WORK/backups"
    run_backup "$WORK/backups"
    [[ "$STATUS" -ne 0 ]]
    grep -F 'accessible to other users' "$WORK/out" >/dev/null
    [[ -z "$(backups)" ]]
}

backup_needs_a_running_indexer() {
    reset
    make_db "$WORK/data/mail.db"
    FAKE_RUNNING=0 run_backup "$WORK/backups"
    [[ "$STATUS" -ne 0 ]]
    grep -F 'indexer is not running' "$WORK/out" >/dev/null
    [[ ! -e "$WORK/backups" ]]
}

backup_writes_nothing_when_the_check_fails() {
    reset
    make_db "$WORK/data/mail.db" 1 "$((0x504D4149))" corrupt
    run_backup "$WORK/backups"
    [[ "$STATUS" -ne 0 ]]
    grep -E '^integrity_check: row [0-9]+ missing from index ti' "$WORK/out" >/dev/null
    grep -F 'failed PRAGMA integrity_check' "$WORK/out" >/dev/null
    [[ -z "$(backups)" ]]
    no_temp_copy_left
    marker_not_printed
}

READY_LOGS='indexer | Startup identity: service=indexer commit=abc boot=1 schema_code=1 schema_stored=1 config=x
indexer | Database ready at /data/mail.db (schema v1)
indexer | Embedder identity verified against the index (model=synthetic)'

restore_replaces_the_index() {
    reset
    make_db "$WORK/backup.db"
    python3 -c 'import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute("DELETE FROM t WHERE a < 100"); c.commit()' "$WORK/backup.db"
    make_db "$WORK/data/mail.db"
    printf 'stale wal\n' >"$WORK/data/mail.db-wal"
    printf 'stale shm\n' >"$WORK/data/mail.db-shm"
    FAKE_LOGS="$READY_LOGS" run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -eq 0 ]]
    # Checked before opening the index: SQLite discards an invalid WAL.
    [[ ! -e "$WORK/data/mail.db-wal" && ! -e "$WORK/data/mail.db-shm" && ! -e "$WORK/data/.restore-index.db" ]]
    [[ "$(query "$WORK/data/mail.db" 'SELECT count(*) FROM t')" == 100 ]]
    grep -Fx 'stop mcp-server indexer' "$WORK/docker.log" >/dev/null
    grep -Fx 'start indexer mcp-server' "$WORK/docker.log" >/dev/null
    grep -F 'backup schema version: 1 (code: 1)' "$WORK/out" >/dev/null
    grep -F 'Embedder identity verified' "$WORK/out" >/dev/null
    grep -F 'schema_stored=1' "$WORK/out" >/dev/null
    grep -F "Index restored from $WORK/backup.db" "$WORK/out" >/dev/null
    marker_not_printed
}

restore_needs_a_yes() {
    reset
    make_db "$WORK/backup.db"
    make_db "$WORK/data/mail.db"
    run_restore "$WORK/backup.db" no
    [[ "$STATUS" -ne 0 ]]
    grep -F 'not restored' "$WORK/out" >/dev/null
    if grep -E '^(stop|run|start)' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
}

restore_needs_an_existing_backup_and_container() {
    reset
    run_restore "$WORK/missing.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'is not a file' "$WORK/out" >/dev/null
    make_db "$WORK/backup.db"
    FAKE_NO_CONTAINER=1 run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'no indexer container' "$WORK/out" >/dev/null
    if grep -E '^(stop|run|start)' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
}

# refused_restore DESCRIPTION-PATTERN MAKE_DB-ARGS...: the backup is
# refused, the index is untouched and the services start again.
refused_restore() {
    local expected="$1"
    shift
    reset
    make_db "$WORK/backup.db" "$@"
    make_db "$WORK/data/mail.db"
    printf 'live wal\n' >"$WORK/data/mail.db-wal"
    run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F "$expected" "$WORK/out" >/dev/null
    grep -F 'previous index is unchanged' "$WORK/out" >/dev/null
    [[ -e "$WORK/data/mail.db-wal" && ! -e "$WORK/data/.restore-index.db" ]]
    grep -Fx 'start indexer mcp-server' "$WORK/docker.log" >/dev/null
    marker_not_printed
}

restore_refuses_a_corrupt_backup() {
    refused_restore 'failed PRAGMA integrity_check' 1 "$((0x504D4149))" corrupt
}

restore_refuses_a_newer_schema() {
    refused_restore 'backup schema is newer than this code' 2
}

restore_refuses_a_foreign_file() {
    refused_restore 'not a protonmail-local-ai index' 1 7
}

restore_reports_a_refused_index() {
    reset
    make_db "$WORK/backup.db"
    make_db "$WORK/data/mail.db"
    FAKE_LOGS='indexer | The configured embedder is not the one that built this index (EMBED_MODEL)' \
        run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'indexer refused the restored index' "$WORK/out" >/dev/null
}

restore_wait_is_bounded() {
    reset
    make_db "$WORK/backup.db"
    make_db "$WORK/data/mail.db"
    FAKE_LOGS='indexer | Startup identity: service=indexer' RESTORE_WAIT_SECONDS=0 \
        run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'did not report its embedder identity within 0s' "$WORK/out" >/dev/null
    grep -F 'Startup identity' "$WORK/out" >/dev/null
}

stub_matches_the_indexer() {
    local name
    for name in SCHEMA_VERSION SCHEMA_APPLICATION_ID; do
        [[ "$(grep -E "^$name = " "$REPO/indexer/src/database.py" | cut -d'#' -f1 | tr -d ' ')" == \
            "$(grep -E "^$name = " "$WORK/stub/src/database.py" | tr -d ' ')" ]]
    done
}

# Runs each case in a subshell with errexit on, outside any condition.
check() {
    local description="$1" status
    shift
    set +e
    (
        set -e
        "$@"
    ) >"$WORK/case-output" 2>&1
    status=$?
    set -e
    if ((status == 0)); then
        printf 'ok   %s\n' "$description"
    else
        printf 'FAIL %s\n' "$description"
        sed 's/^/     /' "$WORK/case-output"
        if [[ -f "$WORK/out" ]]; then
            sed 's/^/     /' "$WORK/out"
        fi
        FAILURES=$((FAILURES + 1))
    fi
}

check "backup writes a checked, private, timestamped copy" backup_writes_a_checked_private_copy
check "backup is consistent while the index is written" backup_is_consistent_while_writing
check "backup expands a ~ that zsh passed through unexpanded" backup_expands_an_unexpanded_tilde
check "backup refuses a path inside the checkout" backup_refuses_a_path_inside_the_checkout
check "backup requires BACKUP_DIR" backup_requires_backup_dir
check "backup refuses a directory other users can open" backup_refuses_a_shared_directory
check "backup needs a running indexer" backup_needs_a_running_indexer
check "backup writes nothing when integrity_check fails" backup_writes_nothing_when_the_check_fails
check "restore replaces the index and drops the old WAL" restore_replaces_the_index
check "restore needs a yes" restore_needs_a_yes
check "restore needs a backup file and an indexer container" restore_needs_an_existing_backup_and_container
check "restore refuses a corrupt backup" restore_refuses_a_corrupt_backup
check "restore refuses a newer schema" restore_refuses_a_newer_schema
check "restore refuses a file that is not an index" restore_refuses_a_foreign_file
check "restore reports an index the indexer refuses" restore_reports_a_refused_index
check "restore waits a bounded time" restore_wait_is_bounded
check "the restore stub matches indexer/src/database.py" stub_matches_the_indexer

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
