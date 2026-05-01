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
        # Best-effort terminate; bridge may have already exited from EOF.
        kill -TERM "$ENTRY_PID" 2>/dev/null || true
        sleep 1
        kill -KILL "$ENTRY_PID" 2>/dev/null || true
        # Dump the most recent log so the host can grep for the marker.
        # On the no-log path, also dump entrypoint output so a pre-Bridge
        # failure (GPG init, missing binary, exec error) is visible
        # instead of silently swallowed.
        LOG="$(find /data/local/protonmail/bridge-v3/logs -name "*.log" 2>/dev/null | sort | tail -1)"
        if [ -n "$LOG" ]; then
            cat "$LOG"
        else
            echo "NO_BRIDGE_LOG_WRITTEN"
            echo "--- entrypoint output ---"
            cat "$ENTRY_OUT" 2>/dev/null || true
        fi
    ' > "$SMOKE_OUT" 2>&1 || true

if grep -F 'autoUpdate="false"' "$SMOKE_OUT" >/dev/null; then
    printf 'AutoUpdate runtime default verified off in built image.\n'
elif grep -F 'autoUpdate="true"' "$SMOKE_OUT" >/dev/null; then
    printf 'ERROR: AutoUpdate runtime default is true in built image; patch did not take effect.\n' >&2
    exit 1
else
    printf 'ERROR: AutoUpdate marker not found in Bridge log output.\n' >&2
    printf '--- captured output (first 60 lines) ---\n' >&2
    head -60 "$SMOKE_OUT" >&2
    exit 1
fi

printf 'Proton Bridge smoke checks passed.\n'
