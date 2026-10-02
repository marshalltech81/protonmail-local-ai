#!/bin/bash
set -Eeuo pipefail

# End-to-end TLS check for the macOS Bridge mode (#497), run against the
# shipped mbsync image and a synthetic STARTTLS IMAP server (imap_stub.py)
# whose certificate is shaped like the macOS Bridge app's: self-signed,
# CA:TRUE, issued for 127.0.0.1 only. The real entrypoint runs with the
# overlay's BRIDGE_CERT_HOST=127.0.0.1, reaching the server by another
# name, as it reaches the app through host.docker.internal.
#
# Checks:
#   0. Without BRIDGE_CERT_FINGERPRINT the first certificate is not
#      trusted: mbsync exits before LOGIN and pins nothing.
#   1. mbsync waits while the server is down, then syncs once it is up
#      (STARTTLS, LOGIN over TLS, first-boot pin, success stamp).
#   2. A restart against the same certificate is accepted.
#   3. A different certificate at that address is refused, by the
#      expected fingerprint and, with that set to the new certificate,
#      by the pin.
#   4. isync itself, without the entrypoint's pin, refuses a certificate
#      other than the one in CertificateFile, and refuses the right
#      certificate under a name it is not issued for (so the tunnel, not a
#      disabled check, is what lets 127.0.0.1 through).
#
# Needs Docker and openssl. Builds the mbsync image unless MBSYNC_IMAGE
# names one. Run: bash mbsync/tests/tls_check.sh  (or make test-mbsync-tls)

MBSYNC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly MBSYNC_DIR
# The digest the indexer and mcp-server images are built from.
readonly PYTHON_IMAGE="python:3.14-slim-trixie@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d"
readonly RUN_ID="mbsync-tls-check-$$"
readonly NETWORK="$RUN_ID"
readonly STUB="${RUN_ID}-bridge"
readonly MBSYNC="${RUN_ID}-mbsync"
readonly WAIT_SECONDS=90
IMAGE="${MBSYNC_IMAGE:-}"
WORK="$(mktemp -d)"
readonly WORK
FAILURES=0

cleanup() {
    docker rm -f "$STUB" "$MBSYNC" >/dev/null 2>&1 || true
    docker network rm "$NETWORK" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

pass() { printf 'ok   %s\n' "$1"; }
fail() {
    printf 'FAIL %s\n' "$1"
    FAILURES=$((FAILURES + 1))
}

# Bridge-shaped certificate (upstream internal/certs/tls.go NewTLSTemplate).
make_cert() {
    openssl req -x509 -newkey rsa:2048 -nodes -days 2 \
        -subj "/C=CH/O=Synthetic/OU=Synthetic/CN=127.0.0.1" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=keyEncipherment,digitalSignature,keyCertSign" \
        -addext "extendedKeyUsage=serverAuth,clientAuth" \
        -addext "subjectAltName=IP:127.0.0.1" \
        -keyout "$WORK/$1.key" -out "$WORK/$1.pem" >"$WORK/openssl-$1.log" 2>&1
    # The containers run as other users.
    chmod 644 "$WORK/$1.key" "$WORK/$1.pem"
}

start_stub() {
    docker rm -f "$STUB" >/dev/null 2>&1 || true
    docker run -d --name "$STUB" --network "$NETWORK" -v "$WORK:/work:ro" \
        "$PYTHON_IMAGE" python /work/imap_stub.py "/work/$1.pem" "/work/$1.key" 1143 >/dev/null
}

# start_mbsync [EXPECTED_FINGERPRINT]
start_mbsync() {
    docker rm -f "$MBSYNC" >/dev/null 2>&1 || true
    # Run as the invoking user so the bind-mounted Maildir and state
    # directories can stay private (700) instead of world-writable.
    docker run -d --name "$MBSYNC" --network "$NETWORK" --init --read-only \
        --user "$(id -u):$(id -g)" \
        --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
        -e BRIDGE_HOST="$STUB" -e BRIDGE_IMAP_PORT=1143 -e BRIDGE_CERT_HOST=127.0.0.1 \
        -e BRIDGE_CERT_FINGERPRINT="${1:-}" \
        -e BRIDGE_USER=synthetic@example.com -e SYNC_INTERVAL=3600 \
        -v "$WORK/bridge_pass:/run/secrets/bridge_pass:ro" \
        -v "$WORK/maildir:/maildir" -v "$WORK/state:/state" \
        "$IMAGE" >/dev/null
}

fingerprint() {
    openssl x509 -in "$WORK/$1.pem" -outform DER | openssl dgst -sha256 | awk '{print $NF}'
}

# Waits, boundedly, for a line in a container's log.
wait_for_log() {
    local container="$1" pattern="$2" i
    for ((i = 0; i < WAIT_SECONDS; i++)); do
        if docker logs "$container" 2>&1 | grep -q -- "$pattern"; then
            return 0
        fi
        sleep 1
    done
    printf '     no "%s" in %s within %ss; log follows\n' "$pattern" "$container" "$WAIT_SECONDS"
    docker logs "$container" 2>&1 | sed 's/^/     /'
    return 1
}

# Waits, boundedly, for a container to exit; prints its status.
wait_for_exit() {
    local i
    for ((i = 0; i < WAIT_SECONDS; i++)); do
        if [[ "$(docker inspect -f '{{.State.Running}}' "$1")" == "false" ]]; then
            docker inspect -f '{{.State.ExitCode}}' "$1"
            return 0
        fi
        sleep 1
    done
    return 1
}

# Runs isync directly with the given connection lines and CertificateFile.
run_isync() {
    local connection="$1" certfile="$2"
    cat >"$WORK/isyncrc" <<EOF
IMAPAccount check
${connection}
User synthetic@example.com
PassCmd "echo synthetic"
SSLType STARTTLS
CertificateFile /work/${certfile}

IMAPStore check-remote
Account check

MaildirStore check-local
Path /tmp/
Inbox /tmp/INBOX

Channel check
Far :check-remote:
Near :check-local:
Patterns *
Create Near
SyncState /tmp/.mbsyncstate
Sync Pull
Expunge None
EOF
    chmod 644 "$WORK/isyncrc"
    docker run --rm --network "$NETWORK" --read-only --tmpfs /tmp --cap-drop ALL \
        --security-opt no-new-privileges:true -v "$WORK:/work:ro" \
        --entrypoint timeout "$IMAGE" 60 mbsync -c /work/isyncrc -a
}

if [[ -z "$IMAGE" ]]; then
    IMAGE="$RUN_ID"
    docker build -q -t "$IMAGE" "$MBSYNC_DIR" >/dev/null
fi
docker pull -q "$PYTHON_IMAGE" >/dev/null
cp "$MBSYNC_DIR/tests/imap_stub.py" "$WORK/imap_stub.py"
make_cert cert-a
make_cert cert-b
printf 'synthetic-password\n' >"$WORK/bridge_pass"
mkdir -p "$WORK/maildir" "$WORK/state"
chmod 644 "$WORK/bridge_pass" "$WORK/imap_stub.py"
chmod 700 "$WORK/maildir" "$WORK/state"
docker network create "$NETWORK" >/dev/null
FP_A="$(fingerprint cert-a)"
FP_B="$(fingerprint cert-b)"

# 0. No expected fingerprint: nothing is trusted on first use.
start_stub cert-a
start_mbsync
if [[ "$(wait_for_exit "$MBSYNC")" == "1" ]] \
    && docker logs "$MBSYNC" 2>&1 | grep -q "BRIDGE_CERT_FINGERPRINT is not set" \
    && docker logs "$MBSYNC" 2>&1 | grep -q "presented: sha256:${FP_A}" \
    && ! docker logs "$STUB" 2>&1 | grep -q "command=LOGIN" \
    && [[ ! -e "$WORK/state/bridge-cert.fingerprint" ]]; then
    pass "without an expected fingerprint, nothing is pinned or sent"
else
    fail "without an expected fingerprint, nothing is pinned or sent"
    docker logs "$MBSYNC" 2>&1 | tail -n 5 | sed 's/^/     /'
fi
docker rm -f "$STUB" >/dev/null

# 1. Bridge down at start, then up.
start_mbsync "$FP_A"
if wait_for_log "$MBSYNC" "Waiting for ProtonBridge IMAP" && sleep 4 \
    && [[ "$(docker inspect -f '{{.State.Running}}' "$MBSYNC")" == "true" ]]; then
    start_stub cert-a
    if wait_for_log "$MBSYNC" "First boot — pinned" \
        && wait_for_log "$MBSYNC" "Starting sync loop" \
        && [[ -s "$WORK/maildir/.mbsync-last-sync.json" ]] \
        && wait_for_log "$STUB" "stub: login over TLS" \
        && ! docker logs "$MBSYNC" 2>&1 | grep -q "Initial sync returned a non-zero status"; then
        pass "waits for an unavailable Bridge, then pins and syncs over STARTTLS"
    else
        fail "waits for an unavailable Bridge, then pins and syncs over STARTTLS"
    fi
else
    fail "waits for an unavailable Bridge, then pins and syncs over STARTTLS"
fi

# 2. Restart, same certificate.
rm -f "$WORK/maildir/.mbsync-last-sync.json"
docker restart "$MBSYNC" >/dev/null
if wait_for_log "$MBSYNC" "fingerprint matches the pinned value" \
    && wait_for_log "$MBSYNC" "Starting sync loop" \
    && [[ -s "$WORK/maildir/.mbsync-last-sync.json" ]]; then
    pass "a restart against the same certificate syncs"
else
    fail "a restart against the same certificate syncs"
fi

# 3. A different certificate at the same address: refused by the expected
# fingerprint, and by the pin when the expected fingerprint names it.
docker stop "$MBSYNC" >/dev/null
start_stub cert-b
docker start "$MBSYNC" >/dev/null
if [[ "$(wait_for_exit "$MBSYNC")" == "1" ]] \
    && docker logs "$MBSYNC" 2>&1 | grep -q "does not match BRIDGE_CERT_FINGERPRINT — refusing to sync"; then
    pass "a different certificate is refused by the expected fingerprint"
else
    fail "a different certificate is refused by the expected fingerprint"
    docker logs "$MBSYNC" 2>&1 | tail -n 5 | sed 's/^/     /'
fi
start_mbsync "$FP_B"
if [[ "$(wait_for_exit "$MBSYNC")" == "1" ]] \
    && docker logs "$MBSYNC" 2>&1 | grep -q "does not match pinned value — refusing to sync"; then
    pass "a different certificate is refused by the pin"
else
    fail "a different certificate is refused by the pin"
    docker logs "$MBSYNC" 2>&1 | tail -n 5 | sed 's/^/     /'
fi
docker rm -f "$MBSYNC" >/dev/null

# 4. isync's own checks (the server still presents cert-b).
tunnel="Host 127.0.0.1
Tunnel \"exec socat - TCP:${STUB}:1143\""
if run_isync "$tunnel" cert-b.pem >"$WORK/isync-ok.log" 2>&1; then
    pass "isync accepts the trusted certificate through the tunnel"
else
    fail "isync accepts the trusted certificate through the tunnel"
    sed 's/^/     /' "$WORK/isync-ok.log"
fi
if ! run_isync "$tunnel" cert-a.pem >"$WORK/isync-other.log" 2>&1 \
    && grep -q "SSL error connecting" "$WORK/isync-other.log"; then
    pass "isync refuses a certificate other than the trusted one"
else
    fail "isync refuses a certificate other than the trusted one"
    sed 's/^/     /' "$WORK/isync-other.log"
fi
if ! run_isync "Host ${STUB}
Port 1143" cert-b.pem >"$WORK/isync-name.log" 2>&1 \
    && grep -q "certificate owner does not match hostname ${STUB}" "$WORK/isync-name.log"; then
    pass "isync refuses the trusted certificate under another name"
else
    fail "isync refuses the trusted certificate under another name"
    sed 's/^/     /' "$WORK/isync-name.log"
fi

if ((FAILURES > 0)); then
    printf '%d check(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all checks passed\n'
