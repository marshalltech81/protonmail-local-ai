#!/bin/bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR
readonly IMAGE="${BRIDGE_IMAGE:-protonmail-local-ai/bridge}"

cd "$ROOT_DIR"

printf 'Building Proton Bridge image...\n'
docker compose build protonmail-bridge

# Keep the smoke test intentionally small and local: verify the runtime image
# has the expected binary, supporting tools, service account shape, and locked
# down filesystem bits without requiring a live Proton login.
printf 'Running Proton Bridge runtime smoke checks...\n'
docker run --rm --entrypoint /bin/sh "$IMAGE" -ceu '
# Core runtime binaries the entrypoint depends on.
bridge --help >/dev/null
gpg --version >/dev/null
pass --version >/dev/null

# The service user should remain non-root and non-login.
getent passwd bridge | grep -F "bridge" | grep -F "/usr/sbin/nologin" >/dev/null
test "$(id -u bridge)" = "1000"

# Pre-created state directories and the entrypoint should keep their expected
# permissions after image build changes.
test "$(stat -c "%a" /data/config)" = "700"
test "$(stat -c "%a" /data/local)" = "700"
test "$(stat -c "%a" /data/cache)" = "700"
test "$(stat -c "%a" /data/gnupg)" = "700"
test "$(stat -c "%a" /data/pass)" = "700"
test -x /entrypoint.sh
'

printf 'Running Proton Bridge compose runtime checks...\n'
docker compose run --rm --no-deps --entrypoint /bin/sh protonmail-bridge -ceu '
# The service user should still be able to write to the declared scratch and
# state paths even when the Compose service uses a read-only root filesystem.
touch /tmp/bridge-smoke
touch /home/bridge/bridge-smoke
touch /data/bridge-smoke
rm -f /data/bridge-smoke
'

docker compose run --rm --no-deps --entrypoint /bin/sh --user root protonmail-bridge -ceu '
# Root is used here only to distinguish a read-only rootfs from normal
# non-root permission failures elsewhere in the image.
if touch /bridge-smoke-rootfs 2>/dev/null; then
    echo "Bridge root filesystem is unexpectedly writable." >&2
    exit 1
fi
'

# End-to-end check that the AutoUpdate source patch reached the built
# binary's runtime default. We start the real entrypoint with an ephemeral
# tmpfs /data, let GPG / pass / vault initialize, capture the structured
# log line "Vault loaded ... autoUpdate=...", and assert the value is
# false. The earlier require_count + go-test gates in patch-source.sh
# verify the source layer; this verifies the image actually loads it.
printf 'Verifying AutoUpdate runtime default in built Bridge image...\n'
SMOKE_OUT="$(mktemp)"
trap 'rm -f "$SMOKE_OUT"' EXIT

SMOKE_STATUS=0
docker run --rm \
    --init \
    --tmpfs /data:uid=1000,gid=1000,mode=700 \
    --tmpfs /home/bridge:uid=1000,gid=1000,mode=700 \
    --user 1000:1000 \
    --entrypoint /bin/sh \
    "$IMAGE" -c '
        set -e
        # The image pre-creates these dirs, but tmpfs masks the image
        # contents. Recreate so the entrypoint finds the layout it expects.
        mkdir -p /data/config /data/local /data/cache /data/gnupg /data/pass
        chmod 700 /data/config /data/local /data/cache /data/gnupg /data/pass
        # Run the real entrypoint with stdin closed. The first-run path
        # initializes GPG + pass, creates the vault, and exec()s into
        # bridge --cli. Bridge logs "Vault loaded" early (before any
        # network call), then sees EOF on stdin and exits cleanly.
        # Capture entrypoint output to a tmpfile rather than discarding
        # it: failures that happen before Bridge writes its structured
        # log (GPG init, exec error) would otherwise be invisible.
        ENTRY_OUT=/tmp/bridge-entrypoint.out
        /entrypoint.sh </dev/null >"$ENTRY_OUT" 2>&1 &
        ENTRY_PID=$!
        # Poll the structured log for the "Vault loaded" marker, with a
        # bounded deadline so a stuck startup fails the smoke test
        # rather than hanging the suite.
        DEADLINE=$(( $(date +%s) + 30 ))
        while [ "$(date +%s)" -lt "$DEADLINE" ]; do
            LOG="$(find /data/local/protonmail/bridge-v3/logs -name "*.log" 2>/dev/null | sort | tail -1)"
            if [ -n "$LOG" ] && grep -q "Vault loaded" "$LOG"; then
                break
            fi
            sleep 0.5
        done
        # Bridge usually exits on its own from EOF by now. One still
        # running is stopped here, which is the intended end of the check.
        # kill succeeds only while the process is alive, so it records
        # whether this harness sent a signal: the host accepts a TERM/KILL
        # status (143/137) only then, not from an OOM or external kill.
        HARNESS_SIGNAL=none
        if kill -TERM "$ENTRY_PID" 2>/dev/null; then
            HARNESS_SIGNAL=sent
            sleep 1
            kill -KILL "$ENTRY_PID" 2>/dev/null || true
        fi
        ENTRY_EXIT=0
        wait "$ENTRY_PID" || ENTRY_EXIT=$?
        # Dump the most recent log so the host can grep for the marker,
        # then the status lines, then the entrypoint output so a failure
        # outside the log (GPG init, exec error, a Go panic on stderr)
        # is visible instead of silently swallowed.
        LOG="$(find /data/local/protonmail/bridge-v3/logs -name "*.log" 2>/dev/null | sort | tail -1)"
        if [ -n "$LOG" ]; then
            cat "$LOG"
        else
            echo "NO_BRIDGE_LOG_WRITTEN"
        fi
        echo "SMOKE_ENTRYPOINT_EXIT=$ENTRY_EXIT"
        echo "SMOKE_HARNESS_SIGNAL=$HARNESS_SIGNAL"
        echo "--- entrypoint output ---"
        cat "$ENTRY_OUT" 2>/dev/null || true
    ' > "$SMOKE_OUT" 2>&1 || SMOKE_STATUS=$?

# Print the first 60 lines of the log and of the entrypoint output, so a
# long log cannot hide an error that only reached the entrypoint stream.
smoke_fail() {
    printf 'ERROR: %s\n' "$1" >&2
    printf '%s\n' '--- captured output (first 60 lines) ---' >&2
    awk '
        !entry && $0 == "--- entrypoint output ---" {
            entry = 1
            print "--- entrypoint output (first 60 lines) ---"
            next
        }
        !entry && ++log_lines <= 60 { print }
        entry && ++entry_lines <= 60 { print }
    ' "$SMOKE_OUT" >&2
    exit 1
}

# Bridge ended as intended: it exited 0, or this check stopped it.
entrypoint_ended_cleanly() {
    grep -Fx 'SMOKE_ENTRYPOINT_EXIT=0' "$SMOKE_OUT" >/dev/null && return 0
    grep -Ex 'SMOKE_ENTRYPOINT_EXIT=(137|143)' "$SMOKE_OUT" >/dev/null &&
        grep -Fx 'SMOKE_HARNESS_SIGNAL=sent' "$SMOKE_OUT" >/dev/null
}

# The marker alone is not enough: the run must also have ended the way
# the check intends, with Bridge exiting 0 or stopped after the marker.
if [[ "$SMOKE_STATUS" -ne 0 ]]; then
    smoke_fail "AutoUpdate check container exited with status $SMOKE_STATUS."
elif grep -F 'autoUpdate="true"' "$SMOKE_OUT" >/dev/null; then
    smoke_fail 'AutoUpdate runtime default is true in built image; patch did not take effect.'
elif ! grep -F 'autoUpdate="false"' "$SMOKE_OUT" >/dev/null; then
    smoke_fail 'AutoUpdate marker not found in Bridge log output.'
elif grep -E 'level="?(fatal|panic)' "$SMOKE_OUT" >/dev/null; then
    smoke_fail 'Bridge logged a fatal or panic error during the AutoUpdate check.'
elif ! entrypoint_ended_cleanly; then
    smoke_fail 'Bridge did not exit cleanly during the AutoUpdate check.'
fi
printf 'AutoUpdate runtime default verified off in built image.\n'

printf 'Proton Bridge smoke checks passed.\n'
