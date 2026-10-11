#!/bin/bash
set -Eeuo pipefail

# End-to-end TLS check for the Bridge app on the host (#497), run against
# the shipped mbsync image and a synthetic implicit-TLS IMAP server
# (imap_stub.py, #638) whose certificate is shaped like the Bridge app's: self-signed,
# CA:TRUE, issued for 127.0.0.1 only. The real entrypoint runs with
# Compose's BRIDGE_CERT_HOST=127.0.0.1 and BRIDGE_HOST=host.docker.internal,
# which resolves to the server here (--add-host), as it reaches the app
# on the host.
#
# Checks:
#   0. Without BRIDGE_CERT_FINGERPRINT the first certificate is not
#      trusted: mbsync exits at startup, before the wait for Bridge, so
#      it neither connects nor pins.
#   1. mbsync waits while the server is down, then syncs once it is up
#      (implicit TLS, LOGIN over TLS, first-boot pin, success stamp).
#  1a. That pull of the stub's one message asks for its INTERNALDATE
#      (CopyArrivalDate) and gives the file it writes that date, whose
#      offset is not UTC, as its mtime in UTC; it sends no command that
#      would change the far side (#1132).
#   2. A restart against the same certificate is accepted.
#   3. A different certificate at that address is refused, by the
#      expected fingerprint and, with that set to the new certificate,
#      by the pin.
#   4. isync itself, without the entrypoint's pin, refuses a certificate
#      other than the one in CertificateFile, and refuses the right
#      certificate under a name it is not issued for (so the tunnel, not a
#      disabled check, is what lets 127.0.0.1 through).
#   5. A server that greets in plaintext and offers STARTTLS (a Bridge
#      left in its STARTTLS mode) is refused, with no fallback: the
#      entrypoint stops at certificate extraction with the fix, and isync
#      itself fails the handshake; neither sends credentials.
#   6. Each other certificate name BRIDGE_CERT_HOST accepts (localhost,
#      127.0.0.2 and 127.255.255.254 for the rest of 127.0.0.0/8, ::1;
#      PLAN.md decision 46) syncs with a certificate issued for it, and a
#      certificate for 127.0.0.1 is refused under it before LOGIN.
#   7. A BRIDGE_HOST outside the accepted names is refused at startup,
#      before any connection.
#
# Needs Docker and openssl. Builds the mbsync image unless MBSYNC_IMAGE
# names one. Run: bash mbsync/tests/tls_check.sh  (or make test-mbsync-tls)

MBSYNC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly MBSYNC_DIR
# The image the indexer and mcp-server Dockerfiles build from. Dependabot
# bumps only the Dockerfiles; scripts/tests/image_pin_test.sh fails when
# this pin differs from them (#1023), so bump it with them.
readonly PYTHON_IMAGE="python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170"
readonly RUN_ID="mbsync-tls-check-$$"
readonly NETWORK="$RUN_ID"
readonly STUB="${RUN_ID}-bridge"
readonly MBSYNC="${RUN_ID}-mbsync"
readonly WAIT_SECONDS=90
# imap_stub.py's INTERNALDATE, 02-Jan-2020 05:04:05 +0200, in UTC:
# 2020-01-02T03:04:05Z.
readonly ARRIVAL_EPOCH=1577934245
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

# Bridge-shaped certificate (upstream internal/certs/tls.go NewTLSTemplate),
# issued for 127.0.0.1 like the app's own, or for the name $2: a custom
# certificate an operator imports into the app for another loopback name.
make_cert() {
    local name="${2:-127.0.0.1}" san="DNS:${2:-}"
    if [[ "$name" == *:* || "$name" =~ ^[0-9.]+$ ]]; then
        san="IP:${name}"
    fi
    openssl req -x509 -newkey rsa:2048 -nodes -days 2 \
        -subj "/C=CH/O=Synthetic/OU=Synthetic/CN=${name}" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=keyEncipherment,digitalSignature,keyCertSign" \
        -addext "extendedKeyUsage=serverAuth,clientAuth" \
        -addext "subjectAltName=${san}" \
        -keyout "$WORK/$1.key" -out "$WORK/$1.pem" >"$WORK/openssl-$1.log" 2>&1
    # The containers run as other users.
    chmod 644 "$WORK/$1.key" "$WORK/$1.pem"
}

# start_stub CERT [MODE]: MODE is implicit (the default) or starttls. The
# stub always has STUB_IP, which mbsync resolves as host.docker.internal.
start_stub() {
    docker rm -f "$STUB" >/dev/null 2>&1 || true
    docker run -d --name "$STUB" --network "$NETWORK" --ip "$STUB_IP" -v "$WORK:/work:ro" \
        "$PYTHON_IMAGE" python /work/imap_stub.py "/work/$1.pem" "/work/$1.key" 1143 \
        "${2:-implicit}" >/dev/null
}

# start_mbsync [EXPECTED_FINGERPRINT [CERT_HOST [LABEL]]]: Compose's
# BRIDGE_HOST, host.docker.internal, resolved to the stub (BRIDGE_HOST
# accepts only the names mbsync/proton-settings.sh lists, PLAN.md
# decision 46). CERT_HOST defaults to Compose's 127.0.0.1. LABEL names a
# Maildir and state directory of its own (a first boot).
start_mbsync() {
    local maildir="$WORK/maildir" state="$WORK/state"
    if [[ -n "${3:-}" ]]; then
        maildir="$WORK/maildir-$3" state="$WORK/state-$3"
        mkdir -p "$maildir" "$state"
        chmod 700 "$maildir" "$state"
    fi
    docker rm -f "$MBSYNC" >/dev/null 2>&1 || true
    # Run as the invoking user so the bind-mounted Maildir and state
    # directories can stay private (700) instead of world-writable. This
    # assumes container UIDs match host UIDs, as on OrbStack, Docker
    # Desktop and standard Docker Engine (CI). Rootless Docker and
    # userns-remap map UIDs differently, so the test is not supported
    # there.
    docker run -d --name "$MBSYNC" --network "$NETWORK" --init --read-only \
        --user "$(id -u):$(id -g)" --add-host "host.docker.internal:${STUB_IP}" \
        --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
        -e BRIDGE_HOST="${MBSYNC_BRIDGE_HOST:-host.docker.internal}" -e BRIDGE_IMAP_PORT=1143 \
        -e BRIDGE_CERT_HOST="${2:-127.0.0.1}" \
        -e BRIDGE_CERT_FINGERPRINT="${1:-}" \
        -e BRIDGE_USER=synthetic@example.com -e SYNC_INTERVAL=3600 \
        -v "$WORK/bridge_pass:/run/secrets/bridge_pass:ro" \
        -v "$maildir:/maildir" -v "$state:/state" \
        "$IMAGE" >/dev/null
}

fingerprint() {
    openssl x509 -in "$WORK/$1.pem" -outform DER | openssl dgst -sha256 | awk '{print $NF}'
}

# Succeeds if a container's log has a line matching a pattern. The log is
# read whole before it is searched: under pipefail, `docker logs | grep -q`
# fails when grep stops at a match before docker logs has written the
# rest, which then dies of SIGPIPE, so a line that is there could read as
# missing, and a check that a line is absent could pass (#588).
log_has() {
    local logs
    logs="$(docker logs "$1" 2>&1)" || return 1
    grep -q -- "$2" <<<"$logs"
}

# Waits, boundedly, for a line in a container's log.
wait_for_log() {
    local container="$1" pattern="$2" i
    for ((i = 0; i < WAIT_SECONDS; i++)); do
        if log_has "$container" "$pattern"; then
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

# Waits, boundedly, for a non-empty file.
wait_for_file() {
    local i
    for ((i = 0; i < WAIT_SECONDS; i++)); do
        if [[ -s "$1" ]]; then
            return 0
        fi
        sleep 1
    done
    printf '     no %s within %ss\n' "$1" "$WAIT_SECONDS"
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
TLSType IMAPS
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
# A subnet of its own, so the stub keeps one address (STUB_IP) whether or
# not it is running, and mbsync can resolve host.docker.internal to it
# from its start (--add-host). Docker refuses a subnet another network
# holds, so a few are tried.
STUB_IP=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    octet=$((RANDOM % 250 + 1))
    if docker network create --subnet "10.231.${octet}.0/24" "$NETWORK" >/dev/null 2>"$WORK/network.err"; then
        STUB_IP="10.231.${octet}.10"
        break
    fi
done
if [[ -z "$STUB_IP" ]]; then
    printf 'could not create a test network:\n' >&2
    cat "$WORK/network.err" >&2
    exit 1
fi
readonly STUB_IP
FP_A="$(fingerprint cert-a)"
FP_B="$(fingerprint cert-b)"

# 0. No expected fingerprint: nothing is trusted on first use.
start_stub cert-a
start_mbsync
if [[ "$(wait_for_exit "$MBSYNC")" == "1" ]] \
    && wait_for_log "$MBSYNC" "BRIDGE_CERT_FINGERPRINT is not set" \
    && ! log_has "$MBSYNC" "Waiting for ProtonBridge IMAP" \
    && ! log_has "$STUB" "command=" \
    && [[ ! -e "$WORK/state/bridge-cert.fingerprint" ]]; then
    pass "without an expected fingerprint, mbsync stops at startup: nothing is sent or pinned"
else
    fail "without an expected fingerprint, mbsync stops at startup: nothing is sent or pinned"
    docker logs "$MBSYNC" 2>&1 | tail -n 5 | sed 's/^/     /'
fi
docker rm -f "$STUB" >/dev/null

# 1. Bridge down at start, then up. The rendered template must load
# without a deprecation notice: isync writes it to stderr on every run,
# where it would count as another error and turn the tolerated far-side
# failure (#276) into a failed sync.
start_mbsync "$FP_A"
if wait_for_log "$MBSYNC" "Waiting for ProtonBridge IMAP" && sleep 4 \
    && [[ "$(docker inspect -f '{{.State.Running}}' "$MBSYNC")" == "true" ]]; then
    start_stub cert-a
    if wait_for_log "$MBSYNC" "First boot — pinned" \
        && wait_for_log "$MBSYNC" "Starting sync loop" \
        && [[ -s "$WORK/maildir/.mbsync-last-sync.json" ]] \
        && wait_for_log "$STUB" "stub: login over TLS" \
        && ! log_has "$MBSYNC" "Initial sync returned a non-zero status" \
        && ! log_has "$MBSYNC" "notice: .* is deprecated"; then
        pass "waits for an unavailable Bridge, then pins and syncs over implicit TLS"
    else
        fail "waits for an unavailable Bridge, then pins and syncs over implicit TLS"
    fi
else
    fail "waits for an unavailable Bridge, then pins and syncs over implicit TLS"
fi

# 1a. The pull asked for INTERNALDATE (CopyArrivalDate) and the file it
# wrote has the stub's INTERNALDATE, 02-Jan-2020 05:04:05 +0200, in UTC as
# its mtime; it sent nothing that would change the far side (#1132). The
# image's stat reads the time, so the host's stat dialect never enters.
near_file=""
near_mtime=""
if [[ -d "$WORK/maildir/INBOX" ]]; then
    near_file="$(find "$WORK/maildir/INBOX" -type f \( -path '*/new/*' -o -path '*/cur/*' \) -print -quit)"
fi
if [[ -n "$near_file" ]]; then
    near_mtime="$(docker run --rm --read-only --user "$(id -u):$(id -g)" --cap-drop ALL \
        --security-opt no-new-privileges:true -v "$WORK/maildir:/maildir:ro" \
        --entrypoint stat "$IMAGE" -c %Y "/maildir${near_file#"$WORK/maildir"}")"
fi
if [[ "$near_mtime" == "$ARRIVAL_EPOCH" ]] \
    && log_has "$STUB" "stub: fetch body internaldate=True" \
    && ! log_has "$STUB" "stub: write command refused"; then
    pass "the synced file's mtime is the IMAP INTERNALDATE in UTC, and the pull writes nothing back"
else
    fail "the synced file's mtime is the IMAP INTERNALDATE in UTC, and the pull writes nothing back"
    printf '     near file mtime "%s", INTERNALDATE in UTC %s\n' "$near_mtime" "$ARRIVAL_EPOCH"
    # sed, not grep: no matching line must not stop the script under
    # pipefail before the later checks run.
    docker logs "$STUB" 2>&1 | sed -n -E '/fetch body|write command/s/^/     /p'
fi

# 2. Restart, same certificate.
rm -f "$WORK/maildir/.mbsync-last-sync.json"
docker restart "$MBSYNC" >/dev/null
# The log still holds the first run's lines, "Starting sync loop" among
# them, so this run's sync is shown by its success stamp.
if wait_for_log "$MBSYNC" "fingerprint matches the pinned value" \
    && wait_for_file "$WORK/maildir/.mbsync-last-sync.json"; then
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
    && wait_for_log "$MBSYNC" "does not match BRIDGE_CERT_FINGERPRINT — refusing to sync"; then
    pass "a different certificate is refused by the expected fingerprint"
else
    fail "a different certificate is refused by the expected fingerprint"
    docker logs "$MBSYNC" 2>&1 | tail -n 5 | sed 's/^/     /'
fi
start_mbsync "$FP_B"
if [[ "$(wait_for_exit "$MBSYNC")" == "1" ]] \
    && wait_for_log "$MBSYNC" "does not match pinned value — refusing to sync"; then
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

# 5. A STARTTLS server with the trusted certificate: no fallback (#638).
# The stub logs "credentials" for a LOGIN or AUTHENTICATE before TLS and
# "login over TLS" for one after a STARTTLS upgrade.
start_stub cert-b starttls
start_mbsync "$FP_B"
if [[ "$(wait_for_exit "$MBSYNC")" == "1" ]] \
    && wait_for_log "$MBSYNC" "not serving implicit TLS" \
    && ! log_has "$MBSYNC" "Running initial sync" \
    && ! log_has "$STUB" "command=LOGIN" \
    && ! log_has "$STUB" "command=AUTHENTICATE" \
    && ! log_has "$STUB" "command=STARTTLS"; then
    pass "mbsync refuses a STARTTLS server at extraction and sends no credentials"
else
    fail "mbsync refuses a STARTTLS server at extraction and sends no credentials"
    docker logs "$MBSYNC" 2>&1 | tail -n 8 | sed 's/^/     /'
    docker logs "$STUB" 2>&1 | tail -n 8 | sed 's/^/     /'
fi
docker rm -f "$MBSYNC" >/dev/null
if ! run_isync "$tunnel" cert-b.pem >"$WORK/isync-starttls.log" 2>&1 \
    && grep -q "secure connect to tunnel" "$WORK/isync-starttls.log" \
    && ! log_has "$STUB" "command=LOGIN" \
    && ! log_has "$STUB" "command=AUTHENTICATE" \
    && ! log_has "$STUB" "command=STARTTLS"; then
    pass "isync fails the handshake with a STARTTLS server and sends no credentials"
else
    fail "isync fails the handshake with a STARTTLS server and sends no credentials"
    sed 's/^/     /' "$WORK/isync-starttls.log"
    docker logs "$STUB" 2>&1 | tail -n 8 | sed 's/^/     /'
fi

# 6. The other certificate names BRIDGE_CERT_HOST accepts (PLAN.md
# decision 46): localhost, another 127.0.0.0/8 address and ::1, none of
# which the app's own certificate holds, so each needs a certificate
# issued for it and imported into the app. Through the real entrypoint,
# isync syncs with a certificate issued for the name, and refuses the
# app's own certificate (issued for 127.0.0.1) under the name before any
# LOGIN, although the expected fingerprint and a first-boot pin accept it:
# the name check is isync's.
for name in localhost 127.0.0.2 127.255.255.254 ::1; do
    label="name-${name//[:.]/-}"
    make_cert "$label" "$name"
    start_stub "$label"
    start_mbsync "$(fingerprint "$label")" "$name" "$label"
    if wait_for_log "$MBSYNC" "Starting sync loop" \
        && wait_for_log "$STUB" "stub: login over TLS" \
        && [[ -s "$WORK/maildir-${label}/.mbsync-last-sync.json" ]] \
        && ! log_has "$MBSYNC" "Initial sync returned a non-zero status"; then
        pass "BRIDGE_CERT_HOST=${name}: a certificate issued for it syncs"
    else
        fail "BRIDGE_CERT_HOST=${name}: a certificate issued for it syncs"
        docker logs "$MBSYNC" 2>&1 | tail -n 8 | sed 's/^/     /'
    fi
    start_stub cert-a
    start_mbsync "$FP_A" "$name" "${label}-other"
    if wait_for_log "$MBSYNC" "Initial sync returned a non-zero status" \
        && log_has "$MBSYNC" "certificate owner does not match hostname ${name}" \
        && log_has "$MBSYNC" "Bridge cert extracted successfully" \
        && ! log_has "$STUB" "command=LOGIN" \
        && ! log_has "$STUB" "command=AUTHENTICATE" \
        && [[ ! -e "$WORK/maildir-${label}-other/.mbsync-last-sync.json" ]]; then
        pass "BRIDGE_CERT_HOST=${name}: a certificate for another name is refused before LOGIN"
    else
        fail "BRIDGE_CERT_HOST=${name}: a certificate for another name is refused before LOGIN"
        docker logs "$MBSYNC" 2>&1 | tail -n 8 | sed 's/^/     /'
        docker logs "$STUB" 2>&1 | tail -n 8 | sed 's/^/     /'
    fi
    docker rm -f "$MBSYNC" >/dev/null
done

# 7. A BRIDGE_HOST outside the list (here the stub's own container name,
# which would reach it) is refused at startup, naming #1499, before any
# connection: the stub sees nothing.
start_stub cert-a
MBSYNC_BRIDGE_HOST="$STUB" start_mbsync "$FP_A" "" refused-host
if [[ "$(wait_for_exit "$MBSYNC")" == "1" ]] \
    && wait_for_log "$MBSYNC" "BRIDGE_HOST must be one of" \
    && log_has "$MBSYNC" "#1499" \
    && ! log_has "$MBSYNC" "$STUB" \
    && ! log_has "$MBSYNC" "Waiting for ProtonBridge IMAP" \
    && [[ "$(docker logs "$STUB" 2>&1)" == "stub: listening on 1143 (implicit)" ]]; then
    pass "a BRIDGE_HOST outside the list is refused at startup, before any connection"
else
    fail "a BRIDGE_HOST outside the list is refused at startup, before any connection"
    docker logs "$MBSYNC" 2>&1 | tail -n 5 | sed 's/^/     /'
    docker logs "$STUB" 2>&1 | tail -n 5 | sed 's/^/     /'
fi
docker rm -f "$MBSYNC" >/dev/null

if ((FAILURES > 0)); then
    printf '%d check(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all checks passed\n'
