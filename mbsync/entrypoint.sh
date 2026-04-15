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
readonly BRIDGE_WAIT_INTERVAL_SECONDS=2
readonly BRIDGE_WAIT_MAX_ATTEMPTS=300
readonly CERT_EXTRACT_TIMEOUT_SECONDS=20
readonly MAX_CONSECUTIVE_SYNC_FAILURES=5

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

extract_bridge_cert() {
    local cert_tmp
    local openssl_err_file

    cert_tmp="$(mktemp "${RUNTIME_DIR}/bridge-cert.XXXXXX")"
    openssl_err_file="$(mktemp "${RUNTIME_DIR}/openssl-s_client.XXXXXX")"

    echo ">>> Extracting Bridge TLS cert from ${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}..."
    if timeout "${CERT_EXTRACT_TIMEOUT_SECONDS}s" \
        openssl s_client \
            -connect "${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}" \
            -starttls imap \
            < /dev/null \
            2>"$openssl_err_file" \
        | openssl x509 > "$cert_tmp"; then
        mv "$cert_tmp" "$CERT_FILE"
        chmod 600 "$CERT_FILE"
        rm -f "$openssl_err_file"
        echo ">>> Bridge cert extracted successfully."
        return 0
    fi

    echo ">>> ERROR: cert extraction failed — refusing to sync without cert pinning." >&2
    if [[ -s "$openssl_err_file" ]]; then
        echo ">>> openssl s_client stderr follows:" >&2
        cat "$openssl_err_file" >&2
    fi

    rm -f "$cert_tmp" "$openssl_err_file"
    return 1
}

run_sync() {
    mbsync -c "$CONFIG_FILE" -a 2>&1
}

# =============================================================================
# Generate mbsync config from template
# envsubst substitutes ${BRIDGE_HOST}, ${BRIDGE_IMAP_PORT}, ${BRIDGE_USER}
# from environment variables set in docker-compose.yml / .env.
# BRIDGE_PASS is NOT passed as an env var — mbsyncrc uses PassCmd to read
# it directly from the Docker secret at /run/secrets/bridge_pass.
# =============================================================================
require_prerequisites
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
# Extract Bridge TLS certificate for cert pinning
# openssl s_client fetches the cert from the live IMAP connection without
# needing to verify it first. The cert is written to a container-local path
# and re-extracted fresh on every container start so it survives make clean
# or a Bridge update that rotates the cert.
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
