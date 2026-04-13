#!/bin/bash
set -Eeuo pipefail

BRIDGE_HOST="${BRIDGE_HOST:-protonmail-bridge}"
BRIDGE_IMAP_PORT="${BRIDGE_IMAP_PORT:-1143}"
SYNC_INTERVAL="${SYNC_INTERVAL:-60}"
RUNTIME_DIR="/tmp/mbsync"
CONFIG_FILE="${RUNTIME_DIR}/mbsyncrc"
CERT_FILE="${RUNTIME_DIR}/bridge-cert.pem"

mkdir -p "$RUNTIME_DIR"

# =============================================================================
# Generate mbsync config from template
# envsubst substitutes ${BRIDGE_HOST}, ${BRIDGE_IMAP_PORT}, ${BRIDGE_USER}
# from environment variables set in docker-compose.yml / .env.
# BRIDGE_PASS is NOT passed as an env var — mbsyncrc uses PassCmd to read
# it directly from the Docker secret at /run/secrets/bridge_pass.
# =============================================================================
envsubst < /etc/mbsyncrc.template > "$CONFIG_FILE"
chmod 600 "$CONFIG_FILE" # protect the file because it contains credentials

# =============================================================================
# Wait for ProtonBridge IMAP to be available
# Bridge takes time to start and complete its internal Gluon sync before
# it will accept IMAP connections. Retry every 2 seconds indefinitely.
# =============================================================================
echo ">>> Waiting for ProtonBridge IMAP on ${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}..."
until nc -z "$BRIDGE_HOST" "$BRIDGE_IMAP_PORT" 2>/dev/null; do
    sleep 2
done
echo ">>> Bridge IMAP is ready."

# =============================================================================
# Extract Bridge TLS certificate for cert pinning
# openssl s_client fetches the cert from the live IMAP connection without
# needing to verify it first. The cert is written to a container-local path
# and re-extracted fresh on every container start so it survives make clean
# or a Bridge update that rotates the cert.
# =============================================================================
echo ">>> Extracting Bridge TLS cert from ${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}..."
echo | openssl s_client \
    -connect "${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}" \
    -starttls imap \
    2>/dev/null \
    | openssl x509 > "$CERT_FILE"
if [ -s "$CERT_FILE" ]; then
    echo ">>> Bridge cert extracted successfully."
else
    # Fail closed so mbsync never silently downgrades into an unpinned TLS path
    # if Bridge is not ready yet or a cert refresh/rotation goes wrong.
    echo ">>> ERROR: cert extraction failed — refusing to sync without cert pinning." >&2
    exit 1
fi

# =============================================================================
# Initial sync
# Runs once on startup to catch up on any messages that arrived while
# the container was down. The || true prevents the container from exiting
# on warnings — Bridge sometimes returns warnings on first sync that are
# not fatal.
# =============================================================================
echo ">>> Running initial sync..."
mbsync -c "$CONFIG_FILE" -a 2>&1 || \
    echo ">>> Initial sync completed with warnings (normal on first run)"

# =============================================================================
# Continuous sync loop
# Polls Bridge IMAP every SYNC_INTERVAL seconds for new messages.
# Default interval is 60 seconds — set SYNC_INTERVAL in .env to change.
# =============================================================================
echo ">>> Starting sync loop (interval: ${SYNC_INTERVAL}s)..."
while true; do
    sleep "$SYNC_INTERVAL"
    echo ">>> Syncing..."
    mbsync -c "$CONFIG_FILE" -a 2>&1 || \
        echo ">>> Sync warning (bridge may be busy — will retry next interval)"
done
