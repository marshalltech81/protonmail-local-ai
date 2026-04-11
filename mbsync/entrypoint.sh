#!/bin/bash
set -e

BRIDGE_HOST="${BRIDGE_HOST:-protonmail-bridge}"
BRIDGE_IMAP_PORT="${BRIDGE_IMAP_PORT:-1143}"
SYNC_INTERVAL="${SYNC_INTERVAL:-60}"

# =============================================================================
# Generate mbsync config from template
# envsubst substitutes ${BRIDGE_HOST}, ${BRIDGE_IMAP_PORT}, ${BRIDGE_USER},
# ${BRIDGE_PASS} from environment variables set in docker-compose.yml / .env
# =============================================================================
envsubst < /etc/mbsyncrc.template > /home/mbsync/.mbsyncrc
chmod 600 /home/mbsync/.mbsyncrc # protect the file because it contains credentials

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
# Initial sync
# Runs once on startup to catch up on any messages that arrived while
# the container was down. The || true prevents the container from exiting
# on warnings — Bridge sometimes returns warnings on first sync that are
# not fatal.
# =============================================================================
echo ">>> Running initial sync..."
mbsync -c /home/mbsync/.mbsyncrc -a 2>&1 || \
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
    mbsync -c /home/mbsync/.mbsyncrc -a 2>&1 || \
        echo ">>> Sync warning (bridge may be busy — will retry next interval)"
done
