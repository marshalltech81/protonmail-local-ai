#!/bin/bash
set -Eeuo pipefail

# Host alias forwarding check (PLAN.md decision 46, #1497). BRIDGE_HOST
# accepts only the container names for this Mac listed in
# mbsync/proton-settings.sh (PROTON_BRIDGE_HOST_ALIASES), and a name is
# listed only with this check passing for it: from a container, through
# the shipped image's socat as mbsync's Tunnel runs it, the name reaches
# a listener on the host bound to 127.0.0.1 alone, as the Proton Mail
# Bridge app is. Every listed name is checked, so a name added to the list
# without this evidence fails here.
#
# Runs on the Mac with the container engine mbsync uses (OrbStack, Docker
# Desktop); it prints the engine's name with the result, which is the
# record. Not run in CI: Linux's Docker Engine does not forward to the
# host's loopback, which is why Linux is not supported (docs/setup.md).
#
# Needs Docker and python3 on the host. Builds the mbsync image unless
# MBSYNC_IMAGE names one.
# Run: bash mbsync/tests/host_alias_check.sh  (or make test-mbsync-host-aliases)

MBSYNC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly MBSYNC_DIR
# shellcheck source=mbsync/proton-settings.sh disable=SC1091 # linted on its own
source "$MBSYNC_DIR/proton-settings.sh"
readonly RUN_ID="mbsync-host-alias-check-$$"
IMAGE="${MBSYNC_IMAGE:-}"
WORK="$(mktemp -d)"
readonly WORK
LISTENER_PID=""
FAILURES=0

cleanup() {
    if [[ -n "$LISTENER_PID" ]]; then
        kill "$LISTENER_PID" 2>/dev/null || true
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

pass() { printf 'ok   %s\n' "$1"; }
fail() {
    printf 'FAIL %s\n' "$1"
    FAILURES=$((FAILURES + 1))
}

if [[ -z "$IMAGE" ]]; then
    IMAGE="$RUN_ID"
    docker build -q -t "$IMAGE" "$MBSYNC_DIR" >/dev/null
fi

# A listener on 127.0.0.1 alone, on a port the kernel picks, that sends a
# random token to each connection. It writes its port to a file once
# bound.
TOKEN="loopback-only-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
readonly TOKEN
python3 -c '
import socket, sys
s = socket.socket()
s.bind(("127.0.0.1", 0))
s.listen(8)
with open(sys.argv[1], "w") as f:
    f.write(str(s.getsockname()[1]))
while True:
    c, _ = s.accept()
    c.sendall(sys.argv[2].encode() + b"\n")
    c.close()
' "$WORK/port" "$TOKEN" &
LISTENER_PID=$!
for _ in $(seq 1 50); do
    [[ -s "$WORK/port" ]] && break
    sleep 0.1
done
PORT="$(cat "$WORK/port")"
readonly PORT

# The listener is bound to the loopback address alone: the host's other
# addresses refuse it.
listening="$(lsof -nP -a -p "$LISTENER_PID" -iTCP -sTCP:LISTEN)" || true
if grep -q "127.0.0.1:${PORT} (LISTEN)" <<<"$listening" && [[ "$(grep -c LISTEN <<<"$listening")" == 1 ]]; then
    pass "the listener is bound to 127.0.0.1 only"
else
    fail "the listener is bound to 127.0.0.1 only"
fi

engine="$(docker info --format '{{.OperatingSystem}} {{.ServerVersion}}')"
printf 'engine: %s\n' "$engine"
for alias in "${PROTON_BRIDGE_HOST_ALIASES[@]}"; do
    got="$(docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges:true \
        --entrypoint timeout "$IMAGE" 10 socat -T5 - "TCP:${alias}:${PORT}" </dev/null 2>&1 || true)"
    if [[ "$got" == "$TOKEN" ]]; then
        pass "${alias} reaches the host's 127.0.0.1 (${engine})"
    else
        fail "${alias} reaches the host's 127.0.0.1 (${engine})"
        printf '     got: %s\n' "$got"
    fi
done

if ((FAILURES > 0)); then
    printf '%d check(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all checks passed\n'
