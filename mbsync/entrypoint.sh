#!/bin/bash
set -Eeuo pipefail

readonly BRIDGE_HOST="${BRIDGE_HOST:-protonmail-bridge}"
readonly BRIDGE_IMAP_PORT="${BRIDGE_IMAP_PORT:-1143}"
readonly SYNC_INTERVAL="${SYNC_INTERVAL:-60}"
readonly RUNTIME_DIR="/tmp/mbsync"
readonly CONFIG_FILE="${RUNTIME_DIR}/mbsyncrc"
readonly CERT_FILE="${RUNTIME_DIR}/bridge-cert.pem"
readonly HEALTH_FILE="${RUNTIME_DIR}/last-successful-sync"
readonly BRIDGE_PASS_FILE="/run/secrets/bridge_pass"
# State directory persists the pinned Bridge cert fingerprint across
# container restarts. The directory is backed by a named volume so it
# survives `docker compose down` / rebuilds but is cleared by `make
# clean`, giving the operator a clean way to start over if needed.
readonly STATE_DIR="/state"
readonly PIN_FILE="${STATE_DIR}/bridge-cert.fingerprint"
readonly BRIDGE_WAIT_INTERVAL_SECONDS=2
readonly BRIDGE_WAIT_MAX_ATTEMPTS=300
readonly CERT_EXTRACT_TIMEOUT_SECONDS=20
readonly MAX_CONSECUTIVE_SYNC_FAILURES=5
readonly BRIDGE_CERT_PIN_ROTATE="${BRIDGE_CERT_PIN_ROTATE:-false}"
readonly MAILDIR_PATH="/maildir"

# Owner-only umask for the runtime tmp dir (config + cert material).
# mbsync itself ignores umask for Maildir writes — it explicitly passes
# mode 0600 to ``open()`` and can create subdirectories under this umask,
# so the post-sync chmod hook in ``relax_new_maildir_perms`` is what makes
# new Maildir paths traversable/readable to the indexer (a different UID).
umask 077
mkdir -p "$RUNTIME_DIR"

require_prerequisites() {
    if [[ -z "${BRIDGE_USER:-}" ]]; then
        echo ">>> ERROR: BRIDGE_USER is empty. Populate it from 'bridge --cli info' before starting mbsync." >&2
        exit 1
    fi

    if [[ ! -s "$BRIDGE_PASS_FILE" ]]; then
        echo ">>> ERROR: ${BRIDGE_PASS_FILE} is missing or empty. Refusing to start without the Bridge password secret." >&2
        exit 1
    fi
}

wait_for_bridge_imap() {
    local attempt
    local nc_err_file

    nc_err_file="$(mktemp "${RUNTIME_DIR}/nc-check.XXXXXX")"
    echo ">>> Waiting for ProtonBridge IMAP on ${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}..."
    for ((attempt = 1; attempt <= BRIDGE_WAIT_MAX_ATTEMPTS; attempt++)); do
        if nc -z "$BRIDGE_HOST" "$BRIDGE_IMAP_PORT" 2>"$nc_err_file"; then
            rm -f "$nc_err_file"
            echo ">>> Bridge IMAP port is reachable."
            return 0
        fi

        sleep "$BRIDGE_WAIT_INTERVAL_SECONDS"
    done

    echo ">>> ERROR: Bridge IMAP did not become reachable after $((BRIDGE_WAIT_MAX_ATTEMPTS * BRIDGE_WAIT_INTERVAL_SECONDS)) seconds." >&2
    if [[ -s "$nc_err_file" ]]; then
        echo ">>> Last nc stderr follows:" >&2
        cat "$nc_err_file" >&2
    fi
    rm -f "$nc_err_file"
    return 1
}

cert_fingerprint() {
    local cert_path="$1"
    # SHA-256 over the DER-encoded cert — matches `openssl x509 -fingerprint
    # -sha256 -noout`. Emitted as a bare lowercase hex string without
    # colons so it's easy to compare and store.
    openssl x509 -in "$cert_path" -outform DER \
        | openssl dgst -sha256 \
        | awk '{print $NF}' \
        | tr '[:upper:]' '[:lower:]'
}

verify_cert_pin() {
    # First boot: no pin on disk yet → TOFU, save fingerprint.
    # Subsequent boots: fingerprint must match, or the operator must opt
    # in to rotation via BRIDGE_CERT_PIN_ROTATE=true (used when Bridge is
    # upgraded and its TLS cert is deliberately replaced).
    local current_fp="$1"
    local pinned_fp

    if [[ ! -s "$PIN_FILE" ]]; then
        printf '%s\n' "$current_fp" > "$PIN_FILE"
        chmod 600 "$PIN_FILE"
        echo ">>> First boot — pinned Bridge cert fingerprint sha256:${current_fp}."
        return 0
    fi

    pinned_fp="$(tr -d '[:space:]' < "$PIN_FILE")"
    if [[ "$pinned_fp" == "$current_fp" ]]; then
        echo ">>> Bridge cert fingerprint matches the pinned value."
        return 0
    fi

    if [[ "$BRIDGE_CERT_PIN_ROTATE" == "true" ]]; then
        echo ">>> WARNING: Bridge cert fingerprint changed and BRIDGE_CERT_PIN_ROTATE=true — rotating pin." >&2
        echo ">>>   pinned:  sha256:${pinned_fp}" >&2
        echo ">>>   current: sha256:${current_fp}" >&2
        printf '%s\n' "$current_fp" > "$PIN_FILE"
        chmod 600 "$PIN_FILE"
        return 0
    fi

    echo ">>> ERROR: Bridge cert fingerprint does not match pinned value — refusing to sync." >&2
    echo ">>>   pinned:  sha256:${pinned_fp}" >&2
    echo ">>>   current: sha256:${current_fp}" >&2
    echo ">>> If this rotation is expected (e.g. Bridge upgrade), restart mbsync with BRIDGE_CERT_PIN_ROTATE=true." >&2
    echo ">>> Otherwise this is a security event — investigate before proceeding." >&2
    return 1
}

extract_bridge_cert() {
    local cert_tmp
    local openssl_err_file
    local current_fp

    cert_tmp="$(mktemp "${RUNTIME_DIR}/bridge-cert.XXXXXX")"
    openssl_err_file="$(mktemp "${RUNTIME_DIR}/openssl-s_client.XXXXXX")"

    echo ">>> Extracting Bridge TLS cert from ${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}..."
    if ! timeout "${CERT_EXTRACT_TIMEOUT_SECONDS}s" \
        openssl s_client \
            -connect "${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}" \
            -starttls imap \
            < /dev/null \
            2>"$openssl_err_file" \
        | openssl x509 > "$cert_tmp"; then
        echo ">>> ERROR: cert extraction failed — refusing to sync without cert pinning." >&2
        if [[ -s "$openssl_err_file" ]]; then
            echo ">>> openssl s_client stderr follows:" >&2
            cat "$openssl_err_file" >&2
        fi
        rm -f "$cert_tmp" "$openssl_err_file"
        return 1
    fi

    if ! current_fp="$(cert_fingerprint "$cert_tmp")" || [[ -z "$current_fp" ]]; then
        echo ">>> ERROR: failed to compute fingerprint for extracted cert." >&2
        rm -f "$cert_tmp" "$openssl_err_file"
        return 1
    fi

    if ! verify_cert_pin "$current_fp"; then
        rm -f "$cert_tmp" "$openssl_err_file"
        return 1
    fi

    mv "$cert_tmp" "$CERT_FILE"
    chmod 600 "$CERT_FILE"
    rm -f "$openssl_err_file"
    echo ">>> Bridge cert extracted successfully."
    return 0
}

relax_new_maildir_perms() {
    # mbsync calls ``open(O_CREAT, 0600)`` for every new message and
    # ignores umask, so newly delivered files are owner-only by default
    # and unreadable to the indexer (a different UID). Subdirectories
    # created while the service umask is 077 are also not traversable by
    # the indexer. Re-apply directory execute/read and file read bits after
    # each sync so the indexer reads via "other" permission.
    find "$MAILDIR_PATH" -type d \! -perm -005 -exec chmod go+rx {} +
    find "$MAILDIR_PATH" -type f \! -perm -044 -exec chmod go+r {} +
}

run_sync() {
    local rc=0
    mbsync -c "$CONFIG_FILE" -a 2>&1 || rc=$?
    relax_new_maildir_perms
    return "$rc"
}

# =============================================================================
# Generate mbsync config from template
# envsubst substitutes ${BRIDGE_HOST}, ${BRIDGE_IMAP_PORT}, ${BRIDGE_USER}
# from environment variables set in docker-compose.yml / .env.
# BRIDGE_PASS is NOT passed as an env var — mbsyncrc uses PassCmd to read
# it directly from the Docker secret at /run/secrets/bridge_pass.
# =============================================================================
require_prerequisites

# BRIDGE_CERT_PIN_ROTATE is a single-restart opt-in for accepting a
# legitimate Bridge cert rotation. Leaving it set to true across
# restarts silently disables pin enforcement — every new cert will be
# accepted without comparison. Surface that drift on every boot so the
# operator notices if they forgot to flip it back to false.
if [[ "$BRIDGE_CERT_PIN_ROTATE" == "true" ]]; then
    echo ">>> WARNING: BRIDGE_CERT_PIN_ROTATE=true — any Bridge cert fingerprint change this boot will be accepted without comparison." >&2
    echo ">>> This is intended only for a single restart after a deliberate Bridge cert rotation (e.g. Bridge upgrade)." >&2
    echo ">>> Set BRIDGE_CERT_PIN_ROTATE=false (or remove it from .env) and restart mbsync to re-enable pin enforcement." >&2
fi

envsubst < /etc/mbsyncrc.template > "$CONFIG_FILE"
chmod 600 "$CONFIG_FILE" # protect the file because it contains credentials

# =============================================================================
# Wait for ProtonBridge IMAP to be available
# Bridge takes time to start and complete its internal Gluon sync before
# it will accept IMAP connections. Retry every 2 seconds, then fail so
# Docker restart policy makes the problem visible instead of hanging forever.
# =============================================================================
wait_for_bridge_imap

# =============================================================================
# Extract Bridge TLS certificate and verify the cert pin
# openssl s_client fetches the cert from the live IMAP connection without
# needing to verify it first. On first boot the SHA-256 fingerprint is
# saved to the persistent state volume ($PIN_FILE). On subsequent boots
# the fingerprint must match the pinned value — otherwise mbsync refuses
# to sync. A legitimate rotation (e.g. Bridge upgrade) is accepted by
# restarting mbsync with BRIDGE_CERT_PIN_ROTATE=true. `make clean`
# deletes the state volume and resets the pin.
# =============================================================================
extract_bridge_cert

# =============================================================================
# Initial sync
# Runs once on startup to catch up on any messages that arrived while
# the container was down. A failed attempt is logged and counted so repeated
# failures eventually exit and let Docker restart the container.
# =============================================================================
consecutive_sync_failures=0
echo ">>> Running initial sync..."
if run_sync; then
    touch "$HEALTH_FILE"
else
    consecutive_sync_failures=1
    echo ">>> Initial sync returned a non-zero status (${consecutive_sync_failures}/${MAX_CONSECUTIVE_SYNC_FAILURES})." >&2
fi

# =============================================================================
# Continuous sync loop
# Polls Bridge IMAP every SYNC_INTERVAL seconds for new messages.
# Default interval is 60 seconds — set SYNC_INTERVAL in .env to change.
# =============================================================================
echo ">>> Starting sync loop (interval: ${SYNC_INTERVAL}s)..."
while true; do
    sleep "$SYNC_INTERVAL"
    echo ">>> Syncing..."
    if run_sync; then
        consecutive_sync_failures=0
        touch "$HEALTH_FILE"
        continue
    fi

    ((consecutive_sync_failures += 1))
    echo ">>> Sync failed (${consecutive_sync_failures}/${MAX_CONSECUTIVE_SYNC_FAILURES} consecutive failures)." >&2
    if ((consecutive_sync_failures >= MAX_CONSECUTIVE_SYNC_FAILURES)); then
        echo ">>> ERROR: mbsync exceeded ${MAX_CONSECUTIVE_SYNC_FAILURES} consecutive failures — exiting for container restart." >&2
        exit 1
    fi

    echo ">>> Bridge may still be busy — will retry next interval." >&2
done
