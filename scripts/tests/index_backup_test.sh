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
# FAKE_FAIL_REPLACE makes os.replace fail, as an I/O error at the swap
# would.
# FAKE_VANISH_STALE: another run's sweep removes the stale files this
# run found, between its listing them and its reading their mtime.
cat >"$WORK/stub/sitecustomize.py" <<'EOF'
import os

if os.environ.get("FAKE_FAIL_REPLACE"):
    def _fail(*args, **kwargs):
        raise OSError(5, "synthetic replace failure")

    os.replace = _fail

if os.environ.get("FAKE_VANISH_STALE"):
    _stat = os.stat

    def _vanish(path, *args, **kwargs):
        name = "" if isinstance(path, int) else os.path.basename(str(path))
        if name in (".restore-index.db", ".backup-index-20260101T000000Z.db"):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        return _stat(path, *args, **kwargs)

    os.stat = _vanish
EOF

cat >"$WORK/bin/docker" <<'EOF'
#!/bin/bash
set -Eeuo pipefail
printf '%s\n' "$*" >>"$FAKE_DOCKER_LOG"
case "$1" in
ps)
    # docker ps -q --filter name=^restore-index-PID$: the one-off restore
    # container is still running for the first FAKE_RUN_LINGER polls.
    if [[ "$*" == *"--filter name=^restore-index-"* ]]; then
        [[ "$*" == "ps -q --filter name=^$(cat "$FAKE_RUN_NAME_FILE")\$" ]]
        if (($(grep -c '^ps -q --filter name=^restore-index-' "$FAKE_DOCKER_LOG") <= ${FAKE_RUN_LINGER:-0})); then
            printf 'fedcba987654\n'
        fi
    elif [[ "$FAKE_RUNNING" == 1 ]]; then
        printf '0123456789ab\n'
    fi
    ;;
exec)
    # docker exec indexer python -c CODE ARGS...
    [[ "$2" == indexer && "$3" == python && "$4" == -c ]]
    # FAKE_SWAP_DIR: during the copy, another account moves the checked
    # directory away and puts an open one at its path.
    if [[ -n "${FAKE_SWAP_DIR:-}" && "$6" == make ]]; then
        mv "$FAKE_SWAP_DIR" "$FAKE_SWAP_DIR.moved"
        mkdir -m 755 "$FAKE_SWAP_DIR"
    fi
    SQLITE_PATH="$FAKE_DATA/mail.db" PYTHONPATH="$FAKE_STUB" python3 -c "$5" "${@:6}"
    # FAKE_SECOND_BACKUP: a second backup, into this directory, starts
    # in the same second and runs to completion between this run's copy
    # and its export (its output goes to <directory>.out).
    if [[ -n "${FAKE_SECOND_BACKUP:-}" && "$6" == make ]]; then
        # Bash 5 applies the assignments before a command one by one, so
        # the trigger is cleared from a copy of the directory.
        second="$FAKE_SECOND_BACKUP"
        FAKE_SECOND_BACKUP= BACKUP_DIR="$second" \
            bash "$FAKE_REPO/scripts/backup-index.sh" >"$second.out" 2>&1
    fi
    ;;
inspect)
    if [[ -n "${FAKE_NO_CONTAINER:-}" ]]; then
        printf 'Error: No such object: indexer\n' >&2
        exit 1
    fi
    if [[ "$3" == *Mounts* ]]; then
        printf 'fake_sqlite-volume\n'
    elif [[ "$3" == *StartedAt* ]]; then
        printf '%s\n' "$FAKE_STARTED_AT"
    else
        printf 'sha256:fakeindexerimage\n'
    fi
    ;;
run)
    # docker run FLAGS... sha256:fakeindexerimage python -c CODE
    [[ "$*" == *" --network none "* && "$*" == *" --read-only "* && "$*" == *" --cap-drop ALL "* ]]
    [[ "$*" == *" --volume fake_sqlite-volume:/data "* && "$*" == *" sha256:fakeindexerimage python -c "* ]]
    # The container is named, so the script can tell whether it is gone.
    [[ "$*" =~ \ --name\ (restore-index-[0-9]+)\  ]]
    printf '%s\n' "${BASH_REMATCH[1]}" >"$FAKE_RUN_NAME_FILE"
    # FAKE_RUN_EXIT: the container is killed (or docker fails) at a point
    # the script cannot know.
    if [[ -n "${FAKE_RUN_EXIT:-}" ]]; then
        exit "$FAKE_RUN_EXIT"
    fi
    cd "$FAKE_STUB"
    # FAKE_FSIZE caps file size (512-byte blocks), as a full volume would.
    if [[ -n "${FAKE_FSIZE:-}" ]]; then
        ulimit -f "$FAKE_FSIZE"
    fi
    SQLITE_PATH="$FAKE_DATA/mail.db" PYTHONPATH="$FAKE_STUB" exec python3 -c "${*: -1}"
    ;;
stop)
    # FAKE_SWAP_BACKUP: while the services stop, another account puts a
    # different, valid index file at the backup path.
    if [[ -n "${FAKE_SWAP_BACKUP:-}" ]]; then
        mv "$FAKE_SWAP_BACKUP" "$FAKE_SWAP_BACKUP.moved"
        cp "$FAKE_SWAP_BACKUP.other" "$FAKE_SWAP_BACKUP"
    fi
    # FAKE_STOP_FAIL: the daemon acted but the command reports an error.
    if [[ -n "${FAKE_STOP_FAIL:-}" ]]; then
        printf 'Error response from daemon: synthetic stop failure\n' >&2
        exit 1
    fi
    ;;
start) ;;
logs)
    if [[ -n "${FAKE_LOGS_FAIL:-}" ]]; then
        printf 'Error response from daemon: synthetic-logs-failure-1005\n' >&2
        exit 1
    fi
    # docker logs --since CURSOR indexer: a cursor other than the new
    # process's start time also returns the previous process's lines.
    if [[ "$3" != "$FAKE_STARTED_AT" ]]; then
        printf '%s\n' "${FAKE_OLD_LOGS:-}" >&2
    fi
    printf '%s\n' "$FAKE_LOGS" >&2
    ;;
*)
    printf 'fake docker: unexpected command %s\n' "$1" >&2
    exit 99
    ;;
esac
EOF
chmod +x "$WORK/bin/docker"

# FAKE_STAMP: two runs that start in the same second read the same
# clock; unset, the real date answers.
cat >"$WORK/bin/date" <<'EOF'
#!/bin/bash
if [[ -n "${FAKE_STAMP:-}" ]]; then
    printf '%s\n' "$FAKE_STAMP"
    exit 0
fi
exec /bin/date "$@"
EOF
chmod +x "$WORK/bin/date"

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
        FAKE_RUNNING="${FAKE_RUNNING:-1}" FAKE_STUB="$WORK/stub" FAKE_REPO="$REPO" BACKUP_DIR="$1" \
        bash "$REPO/scripts/backup-index.sh" >"$WORK/out" 2>&1 && STATUS=0 || STATUS=$?
}

run_restore() {
    printf '%s\n' "$2" | PATH="$WORK/bin:$PATH" FAKE_DOCKER_LOG="$WORK/docker.log" \
        FAKE_DATA="$WORK/data" FAKE_STUB="$WORK/stub" FAKE_LOGS="${FAKE_LOGS:-}" \
        FAKE_RUN_NAME_FILE="$WORK/run-name" \
        FAKE_OLD_LOGS="${FAKE_OLD_LOGS:-}" FAKE_STARTED_AT=2026-10-07T12:00:01.5Z \
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
    # UTC second, then a per-run token (pid and 4 random bytes), so two
    # runs that start in the same second never share a name (#1051).
    [[ "$copy" =~ /backups/index/mail-[0-9]{8}T[0-9]{6}Z-[0-9]+-[0-9a-f]{8}\.db$ ]]
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

# grant_acl DIR: gives another account read access through an ACL entry;
# fails when the platform cannot set one.
grant_acl() {
    if [[ "$(uname)" == Darwin ]]; then
        chmod +a "everyone allow list,search,read,file_inherit,directory_inherit" "$1"
    else
        # -d: a default entry, which new directories inherit.
        setfacl -m u:nobody:rx "$1" && setfacl -d -m u:nobody:rx "$1"
    fi
}

# grant_write_acl PATH: gives another account write access to a file or
# directory through an ACL entry.
grant_write_acl() {
    if [[ "$(uname)" == Darwin ]]; then
        chmod +a "everyone allow write" "$1"
    else
        setfacl -m u:nobody:rw "$1"
    fi
}

backup_refuses_a_directory_shared_through_an_acl() {
    reset
    make_db "$WORK/data/mail.db"
    mkdir -m 700 "$WORK/backups"
    if ! grant_acl "$WORK/backups"; then
        printf 'skipped: this file system takes no ACL\n'
        return 0
    fi
    run_backup "$WORK/backups"
    [[ "$STATUS" -ne 0 ]]
    grep -F 'accessible to other users' "$WORK/out" >/dev/null
    [[ -z "$(backups)" ]]
    # A new directory inheriting the entry from its parent is refused too.
    reset
    make_db "$WORK/data/mail.db"
    mkdir -m 700 "$WORK/backups"
    grant_acl "$WORK/backups"
    run_backup "$WORK/backups/new"
    [[ "$STATUS" -ne 0 ]]
    grep -F 'accessible to other users' "$WORK/out" >/dev/null
    [[ -z "$(backups)" ]]
}

backup_writes_into_the_checked_directory() {
    reset
    rm -rf "$WORK/backups.moved"
    make_db "$WORK/data/mail.db"
    FAKE_SWAP_DIR="$WORK/backups" run_backup "$WORK/backups"
    [[ "$STATUS" -eq 0 ]]
    # The copy is in the directory that was checked, now moved away;
    # nothing reached the open directory put at its path.
    [[ -n "$(find "$WORK/backups.moved" -name 'mail-*.db')" ]]
    [[ -z "$(find "$WORK/backups" -type f)" ]]
    rm -rf "$WORK/backups.moved"
}

backup_reclaims_stale_temporary_copies() {
    reset
    make_db "$WORK/data/mail.db"
    printf 'stale\n' >"$WORK/data/.backup-index-20260101T000000Z.db"
    touch -t 202601010000 "$WORK/data/.backup-index-20260101T000000Z.db"
    # A copy another run is still writing is recent and must stay.
    printf 'running\n' >"$WORK/data/.backup-index-29990101T000000Z.db"
    run_backup "$WORK/backups"
    [[ "$STATUS" -eq 0 ]]
    [[ ! -e "$WORK/data/.backup-index-20260101T000000Z.db" ]]
    [[ -e "$WORK/data/.backup-index-29990101T000000Z.db" ]]
    grep -F 'Removed 1 stale backup copy(s) and 0 stale restore staging file(s) from the index volume' "$WORK/out" >/dev/null
}

backup_reclaims_a_stale_restore_staging_file() {
    reset
    make_db "$WORK/data/mail.db"
    # Left by a restore container killed before its cleanup ran (#1054).
    printf 'stale\n' >"$WORK/data/.restore-index.db"
    touch -t 202601010000 "$WORK/data/.restore-index.db"
    run_backup "$WORK/backups"
    [[ "$STATUS" -eq 0 ]]
    [[ ! -e "$WORK/data/.restore-index.db" ]]
    grep -F 'Removed 0 stale backup copy(s) and 1 stale restore staging file(s) from the index volume' "$WORK/out" >/dev/null
    no_temp_copy_left
    # A recent one is a restore still writing and must stay.
    reset
    make_db "$WORK/data/mail.db"
    printf 'running\n' >"$WORK/data/.restore-index.db"
    run_backup "$WORK/backups"
    [[ "$STATUS" -eq 0 ]]
    [[ -e "$WORK/data/.restore-index.db" ]]
    if grep -F 'Removed' "$WORK/out" >/dev/null; then
        return 1
    fi
}

backup_tolerates_stale_files_another_run_removes() {
    reset
    make_db "$WORK/data/mail.db"
    printf 'stale\n' >"$WORK/data/.backup-index-20260101T000000Z.db"
    touch -t 202601010000 "$WORK/data/.backup-index-20260101T000000Z.db"
    printf 'stale\n' >"$WORK/data/.restore-index.db"
    touch -t 202601010000 "$WORK/data/.restore-index.db"
    FAKE_VANISH_STALE=1 run_backup "$WORK/backups"
    [[ "$STATUS" -eq 0 ]]
    [[ ! -e "$WORK/data/.backup-index-20260101T000000Z.db" && ! -e "$WORK/data/.restore-index.db" ]]
    # The other run removed them, so this run reports nothing.
    if grep -F 'Removed' "$WORK/out" >/dev/null; then
        return 1
    fi
    [[ -n "$(backups)" ]]
    no_temp_copy_left
}

backup_runs_in_the_same_second_do_not_collide() {
    reset
    rm -rf "$WORK/backups2" "$WORK/backups2.out"
    make_db "$WORK/data/mail.db"
    # Both runs read the same clock; the second runs to completion while
    # the first is between its copy and its export (#1051).
    FAKE_STAMP=20260101T000000Z FAKE_SECOND_BACKUP="$WORK/backups2" run_backup "$WORK/backups"
    [[ "$STATUS" -eq 0 ]]
    grep -F 'Index backup: ' "$WORK/backups2.out" >/dev/null
    local first second
    first=$(find "$WORK/backups" -name 'mail-20260101T000000Z-*.db')
    second=$(find "$WORK/backups2" -name 'mail-20260101T000000Z-*.db')
    [[ -n "$first" && -n "$second" && "${first##*/}" != "${second##*/}" ]]
    [[ "$(query "$first" 'SELECT count(*) FROM t')" == 200 ]]
    [[ "$(query "$second" 'SELECT count(*) FROM t')" == 200 ]]
    # The in-volume temporary names differed too (the tag after "make"
    # in the exec calls), and the second run's sweep kept the first
    # run's copy, which was in use rather than stale.
    [[ "$(grep -E '^ make 20260101T000000Z-' "$WORK/docker.log" | awk '{print $NF}' | sort -u | wc -l | tr -d ' ')" == 2 ]]
    if grep -F 'Removed' "$WORK/out" "$WORK/backups2.out" >/dev/null; then
        return 1
    fi
    no_temp_copy_left
    marker_not_printed
    rm -rf "$WORK/backups2" "$WORK/backups2.out"
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
    # The live index's mode, which lets mcp-server (another UID) read it.
    chmod 644 "$WORK/data/mail.db"
    printf 'stale wal\n' >"$WORK/data/mail.db-wal"
    printf 'stale shm\n' >"$WORK/data/mail.db-shm"
    FAKE_LOGS="$READY_LOGS" run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -eq 0 ]]
    [[ "$(mode_of "$WORK/data/mail.db")" == 0o644 ]]
    # Checked before opening the index: SQLite discards an invalid WAL.
    [[ ! -e "$WORK/data/mail.db-wal" && ! -e "$WORK/data/mail.db-shm" && ! -e "$WORK/data/.restore-index.db" ]]
    [[ "$(query "$WORK/data/mail.db" 'SELECT count(*) FROM t')" == 100 ]]
    grep -Fx 'stop mcp-server indexer' "$WORK/docker.log" >/dev/null
    # mcp-server starts only after the indexer has verified the index.
    local order
    order=$(grep -E '^(start|logs)' "$WORK/docker.log" | cut -d' ' -f1-2 | uniq | tr '\n' ',')
    [[ "$order" == "start indexer,logs --since,start mcp-server," ]]
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

# wal_only_rows DB: commits 50 rows that stay in the WAL (no checkpoint).
wal_only_rows() {
    python3 - "$1" <<'PY'
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA wal_autocheckpoint=0")
with conn:
    conn.executemany("INSERT INTO t VALUES (?, 'wal')", [(1000 + i,) for i in range(50)])
os._exit(0)
PY
    [[ -s "$1-wal" ]]
}

restore_keeps_committed_wal_rows_when_the_swap_fails() {
    reset
    make_db "$WORK/backup.db"
    make_db "$WORK/data/mail.db"
    wal_only_rows "$WORK/data/mail.db"
    FAKE_FAIL_REPLACE=1 run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'previous index is unchanged' "$WORK/out" >/dev/null
    [[ ! -e "$WORK/data/.restore-index.db" ]]
    # The old database file alone, without any sidecar, holds every
    # committed row.
    cp "$WORK/data/mail.db" "$WORK/solo.db"
    [[ "$(query "$WORK/solo.db" 'SELECT count(*) FROM t')" == 250 ]]
    grep -Fx 'start indexer mcp-server' "$WORK/docker.log" >/dev/null
}

restore_removes_a_partial_staged_file() {
    reset
    make_db "$WORK/backup.db"
    make_db "$WORK/data/mail.db"
    [[ "$(wc -c <"$WORK/backup.db")" -gt 4096 ]]
    # Writes past 4 KiB fail, as on a full volume.
    FAKE_FSIZE=8 run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'previous index is unchanged' "$WORK/out" >/dev/null
    [[ ! -e "$WORK/data/.restore-index.db" ]]
    [[ "$(query "$WORK/data/mail.db" 'SELECT count(*) FROM t')" == 200 ]]
    grep -Fx 'start indexer mcp-server' "$WORK/docker.log" >/dev/null
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
    grep -F 'mcp-server was left stopped' "$WORK/out" >/dev/null
    if grep -E '^start .*mcp-server' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
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

restore_ignores_the_previous_indexer_lines() {
    reset
    make_db "$WORK/backup.db"
    make_db "$WORK/data/mail.db"
    # The stopped indexer verified the old index; the new one has not
    # reported yet, so the restore must time out, not start mcp-server.
    FAKE_OLD_LOGS="$READY_LOGS" FAKE_LOGS='indexer | Startup identity: service=indexer' \
        RESTORE_WAIT_SECONDS=0 run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'did not report its embedder identity' "$WORK/out" >/dev/null
    grep -Fx 'logs --since 2026-10-07T12:00:01.5Z indexer' "$WORK/docker.log" >/dev/null
    if grep -E '^start .*mcp-server' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
}

restore_checks_the_wait_before_anything() {
    reset
    make_db "$WORK/backup.db"
    make_db "$WORK/data/mail.db"
    RESTORE_WAIT_SECONDS=900s run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'RESTORE_WAIT_SECONDS must be a whole number' "$WORK/out" >/dev/null
    if grep -E '^(stop|run|start)' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
}

restore_reads_the_wait_as_decimal() {
    reset
    make_db "$WORK/backup.db"
    make_db "$WORK/data/mail.db"
    # 08 is not valid octal; it must be read as eight seconds.
    FAKE_LOGS="$READY_LOGS" RESTORE_WAIT_SECONDS=08 run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -eq 0 ]]
    grep -Fx 'start mcp-server' "$WORK/docker.log" >/dev/null
    # Bash reports the octal error and leaves the deadline unset, which
    # only the timeout path would trip over.
    if grep -F 'value too great for base' "$WORK/out" >/dev/null; then
        return 1
    fi
}

restore_streams_the_file_it_checked() {
    reset
    make_db "$WORK/backup.db"
    make_db "$WORK/backup.db.other"
    python3 -c 'import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute("DELETE FROM t WHERE a >= 7"); c.commit()' "$WORK/backup.db.other"
    make_db "$WORK/data/mail.db"
    FAKE_SWAP_BACKUP="$WORK/backup.db" FAKE_LOGS="$READY_LOGS" run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -eq 0 ]]
    # The file opened before the prompt (200 rows), not the one put at
    # its path afterwards (7 rows).
    [[ "$(query "$WORK/data/mail.db" 'SELECT count(*) FROM t')" == 200 ]]
    rm -f "$WORK/backup.db.moved" "$WORK/backup.db.other"
}

restore_reports_a_failed_log_read() {
    reset
    make_db "$WORK/backup.db"
    make_db "$WORK/data/mail.db"
    FAKE_LOGS_FAIL=1 run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'synthetic-logs-failure-1005' "$WORK/out" >/dev/null
    grep -F 'could not read the indexer log' "$WORK/out" >/dev/null
}

restore_restarts_the_services_when_stop_fails() {
    reset
    make_db "$WORK/backup.db"
    make_db "$WORK/data/mail.db"
    FAKE_STOP_FAIL=1 run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -Fx 'start indexer mcp-server' "$WORK/docker.log" >/dev/null
    if grep -E '^run ' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
}

restore_refuses_a_replaceable_backup_path() {
    reset
    make_db "$WORK/data/mail.db"
    # A directory another account can write (group-writable, no sticky
    # bit) on the way to the backup lets it swap the file.
    mkdir -m 700 "$WORK/shared"
    chmod 775 "$WORK/shared"
    mkdir -m 700 "$WORK/shared/backups"
    make_db "$WORK/shared/backups/backup.db"
    run_restore "$WORK/shared/backups/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'another account can replace' "$WORK/out" >/dev/null
    if grep -E '^(stop|run|start)' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
    # A sticky directory (like /tmp) is fine.
    reset
    make_db "$WORK/data/mail.db"
    chmod 1775 "$WORK/shared"
    FAKE_LOGS="$READY_LOGS" run_restore "$WORK/shared/backups/backup.db" yes
    [[ "$STATUS" -eq 0 ]]
    rm -rf "${WORK:?}/shared"
}

restore_refuses_a_symlinked_directory_another_account_can_replace() {
    reset
    rm -rf "${WORK:?}/open" "${WORK:?}/real"
    make_db "$WORK/data/mail.db"
    mkdir -m 700 "$WORK/real"
    make_db "$WORK/real/backup.db"
    # The link is yours, but it sits in a directory another account can
    # write (group-writable, no sticky bit) that is not on the resolved
    # path, so that account can replace the link before the open.
    mkdir -m 700 "$WORK/open"
    chmod 775 "$WORK/open"
    ln -s "$WORK/real" "$WORK/open/link"
    run_restore "$WORK/open/link/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F "another account can replace $WORK/open/link/backup.db through $WORK/open;" "$WORK/out" >/dev/null
    if grep -E '^(stop|run|start)' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
    # A directory above the link that another account can write is
    # refused too, even when the link's own directory is private.
    reset
    make_db "$WORK/data/mail.db"
    chmod 700 "$WORK/open"
    mkdir -m 700 "$WORK/open/private"
    mv "$WORK/open/link" "$WORK/open/private/link"
    chmod 775 "$WORK/open"
    run_restore "$WORK/open/private/link/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F "another account can replace $WORK/open/private/link/backup.db through $WORK/open" "$WORK/out" >/dev/null
    if grep -E '^(stop|run|start)' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
    # Your link in a sticky directory (like /tmp) is fine.
    reset
    make_db "$WORK/data/mail.db"
    chmod 1775 "$WORK/open"
    FAKE_LOGS="$READY_LOGS" run_restore "$WORK/open/private/link/backup.db" yes
    [[ "$STATUS" -eq 0 ]]
    # A newline in a component above the link must not end the walk
    # early (a line-oriented split would check only the first line).
    reset
    make_db "$WORK/data/mail.db"
    chmod 700 "$WORK/open"
    mkdir -m 700 "$WORK/a"$'\n'"b"
    mv "$WORK/open" "$WORK/a"$'\n'"b/open"
    chmod 775 "$WORK/a"$'\n'"b/open"
    run_restore "$WORK/a"$'\n'"b/open/private/link/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'another account can replace' "$WORK/out" >/dev/null
    grep -F 'b/open; keep backups in a directory only you can write' "$WORK/out" >/dev/null
    if grep -E '^(stop|run|start)' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
    rm -rf "${WORK:?}/a"$'\n'"b"
}

restore_refuses_a_symlinked_directory_owned_by_another_account() {
    reset
    rm -rf "${WORK:?}/holder" "${WORK:?}/real"
    make_db "$WORK/data/mail.db"
    mkdir -m 700 "$WORK/real"
    make_db "$WORK/real/backup.db"
    # Refused for the link's owner alone, whatever its directory allows.
    mkdir -m 700 "$WORK/holder"
    ln -s "$WORK/real" "$WORK/holder/link"
    # Only root can give the link to another account; CI runners allow
    # sudo without a password, a developer machine usually does not.
    if ! sudo -n chown -h nobody "$WORK/holder/link" 2>/dev/null; then
        printf 'skipped: cannot create a symbolic link owned by another account without sudo\n'
        return 0
    fi
    run_restore "$WORK/holder/link/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F "another account can replace $WORK/holder/link/backup.db through $WORK/holder/link, a symbolic link it owns" "$WORK/out" >/dev/null
    if grep -E '^(stop|run|start)' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
}

restore_accepts_a_root_owned_symlinked_directory() {
    reset
    make_db "$WORK/data/mail.db"
    # On macOS /tmp is a root-owned symbolic link to /private/tmp, and
    # /private/tmp is sticky; on Linux it is a plain sticky directory.
    local dir
    dir=$(mktemp -d /tmp/index-backup-test.XXXXXX)
    chmod 700 "$dir"
    make_db "$dir/backup.db"
    FAKE_LOGS="$READY_LOGS" run_restore "$dir/backup.db" yes
    rm -rf "$dir"
    [[ "$STATUS" -eq 0 ]]
    grep -Fx 'start mcp-server' "$WORK/docker.log" >/dev/null
}

restore_refuses_a_symlinked_backup() {
    reset
    make_db "$WORK/data/mail.db"
    make_db "$WORK/backup.db"
    ln -s "$WORK/backup.db" "$WORK/link.db"
    run_restore "$WORK/link.db" yes
    rm -f "$WORK/link.db"
    [[ "$STATUS" -ne 0 ]]
    grep -F 'is a symbolic link' "$WORK/out" >/dev/null
    if grep -E '^(stop|run|start)' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
}

backup_refuses_a_copy_restore_would_reject() {
    reset
    make_db "$WORK/data/mail.db" 1 7
    run_backup "$WORK/backups"
    [[ "$STATUS" -ne 0 ]]
    grep -F 'not a protonmail-local-ai index' "$WORK/out" >/dev/null
    [[ -z "$(backups)" ]]
    no_temp_copy_left
    # No schema_version row yet (the indexer is still creating it).
    reset
    make_db "$WORK/data/mail.db"
    python3 -c 'import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute("DELETE FROM schema_version"); c.commit()' "$WORK/data/mail.db"
    run_backup "$WORK/backups"
    [[ "$STATUS" -ne 0 ]]
    grep -F 'no schema version' "$WORK/out" >/dev/null
    [[ -z "$(backups)" ]]
    no_temp_copy_left
}

restore_refuses_acl_writable_paths() {
    reset
    make_db "$WORK/data/mail.db"
    mkdir -m 700 "$WORK/acl"
    make_db "$WORK/acl/backup.db"
    if ! grant_write_acl "$WORK/acl/backup.db"; then
        printf 'skipped: this file system takes no ACL\n'
        return 0
    fi
    run_restore "$WORK/acl/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'another account can replace' "$WORK/out" >/dev/null
    if grep -E '^(stop|run|start)' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
    # An ACL on a directory above it that grants access.
    reset
    rm -rf "${WORK:?}/acl"
    make_db "$WORK/data/mail.db"
    mkdir -m 700 "$WORK/acl"
    make_db "$WORK/acl/backup.db"
    grant_write_acl "$WORK/acl"
    run_restore "$WORK/acl/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'another account can replace' "$WORK/out" >/dev/null
    rm -rf "${WORK:?}/acl"
    # A deny-only entry (as on macOS home directories) is fine.
    if [[ "$(uname)" == Darwin ]]; then
        reset
        make_db "$WORK/data/mail.db"
        mkdir -m 700 "$WORK/acl"
        make_db "$WORK/acl/backup.db"
        chmod +a "group:everyone deny delete" "$WORK/acl"
        FAKE_LOGS="$READY_LOGS" run_restore "$WORK/acl/backup.db" yes
        chmod -N "$WORK/acl"
        [[ "$STATUS" -eq 0 ]]
        rm -rf "${WORK:?}/acl"
    fi
}

restore_treats_a_killed_container_as_unknown() {
    reset
    make_db "$WORK/backup.db"
    make_db "$WORK/data/mail.db"
    FAKE_RUN_EXIT=137 run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'may or may not have been replaced' "$WORK/out" >/dev/null
    grep -Fx 'start indexer' "$WORK/docker.log" >/dev/null
    if grep -E '^start .*mcp-server' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
}

# restore_polls: how often the script asked docker ps whether the one-off
# restore container was still running.
restore_polls() {
    grep -c '^ps -q --filter name=^restore-index-[0-9]*\$$' "$WORK/docker.log" || true
}

restore_waits_for_a_lingering_container() {
    reset
    make_db "$WORK/backup.db"
    make_db "$WORK/data/mail.db"
    # docker run reports a failure while the container keeps running for
    # two polls; the indexer starts only once it is gone.
    FAKE_RUN_EXIT=137 FAKE_RUN_LINGER=2 run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F 'may or may not have been replaced' "$WORK/out" >/dev/null
    [[ "$(restore_polls)" == 3 ]]
    local order
    order=$(grep -E '^(ps|start)' "$WORK/docker.log" | cut -d' ' -f1-2 | tr '\n' ',')
    [[ "$order" == "ps -q,ps -q,ps -q,start indexer," ]]
    if grep -E '^start .*mcp-server' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
}

restore_starts_nothing_while_the_container_runs() {
    reset
    make_db "$WORK/backup.db"
    make_db "$WORK/data/mail.db"
    FAKE_RUN_EXIT=137 FAKE_RUN_LINGER=99 RESTORE_WAIT_SECONDS=0 run_restore "$WORK/backup.db" yes
    [[ "$STATUS" -ne 0 ]]
    grep -F "restore container $(cat "$WORK/run-name") is still running after 0s" "$WORK/out" >/dev/null
    grep -F 'nothing was started' "$WORK/out" >/dev/null
    grep -F 'mcp-server was left stopped' "$WORK/out" >/dev/null
    [[ "$(restore_polls)" -ge 1 ]]
    if grep -E '^start ' "$WORK/docker.log" >/dev/null; then
        return 1
    fi
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
check "backup refuses a directory shared through an ACL" backup_refuses_a_directory_shared_through_an_acl
check "backup writes into the directory it checked" backup_writes_into_the_checked_directory
check "backup reclaims stale temporary copies" backup_reclaims_stale_temporary_copies
check "backup reclaims a stale restore staging file" backup_reclaims_a_stale_restore_staging_file
check "backup tolerates stale files another run removes first" backup_tolerates_stale_files_another_run_removes
check "backup runs starting in the same second do not collide" backup_runs_in_the_same_second_do_not_collide
check "backup needs a running indexer" backup_needs_a_running_indexer
check "backup writes nothing when integrity_check fails" backup_writes_nothing_when_the_check_fails
check "restore replaces the index and drops the old WAL" restore_replaces_the_index
check "restore needs a yes" restore_needs_a_yes
check "restore needs a backup file and an indexer container" restore_needs_an_existing_backup_and_container
check "restore refuses a corrupt backup" restore_refuses_a_corrupt_backup
check "restore refuses a newer schema" restore_refuses_a_newer_schema
check "restore refuses a file that is not an index" restore_refuses_a_foreign_file
check "restore keeps committed WAL rows when the swap fails" restore_keeps_committed_wal_rows_when_the_swap_fails
check "restore removes a partial staged file" restore_removes_a_partial_staged_file
check "restore reports an index the indexer refuses" restore_reports_a_refused_index
check "restore waits a bounded time" restore_wait_is_bounded
check "restore ignores the previous indexer's log lines" restore_ignores_the_previous_indexer_lines
check "restore checks RESTORE_WAIT_SECONDS before anything" restore_checks_the_wait_before_anything
check "restore reads RESTORE_WAIT_SECONDS as decimal" restore_reads_the_wait_as_decimal
check "restore streams the file it opened before the prompt" restore_streams_the_file_it_checked
check "restore reports a failed log read" restore_reports_a_failed_log_read
check "restore restarts the services when stop fails" restore_restarts_the_services_when_stop_fails
check "restore refuses a backup path another account can replace" restore_refuses_a_replaceable_backup_path
check "restore refuses a symlinked backup" restore_refuses_a_symlinked_backup
check "restore refuses a symlinked directory another account can replace" restore_refuses_a_symlinked_directory_another_account_can_replace
check "restore refuses a symlinked directory owned by another account" restore_refuses_a_symlinked_directory_owned_by_another_account
check "restore accepts a root-owned symlinked directory" restore_accepts_a_root_owned_symlinked_directory
check "backup refuses a copy restore would reject" backup_refuses_a_copy_restore_would_reject
check "restore refuses ACL-writable paths" restore_refuses_acl_writable_paths
check "restore treats a killed container as an unknown swap state" restore_treats_a_killed_container_as_unknown
check "restore waits for a lingering restore container before starting the indexer" restore_waits_for_a_lingering_container
check "restore starts nothing while the restore container still runs" restore_starts_nothing_while_the_container_runs
check "the restore stub matches indexer/src/database.py" stub_matches_the_indexer

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
