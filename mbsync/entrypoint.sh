#!/bin/bash
# =============================================================================
# mbsync entrypoint
# Waits for Bridge to be ready, then syncs on an interval loop.
# =============================================================================
set -e

BRIDGE_HOST="${BRIDGE_HOST:-protonmail-bridge}"
BRIDGE_IMAP_PORT="${BRIDGE_IMAP_PORT:-1143}"
SYNC_INTERVAL="${SYNC_INTERVAL:-60}"

# Substitute environment variables into the mbsync config
envsubst < /etc/mbsyncrc.template > /etc/mbsyncrc

# Wait for Bridge IMAP to be available
echo ">>> Waiting for ProtonBridge IMAP on ${BRIDGE_HOST}:${BRIDGE_IMAP_PORT}..."
until nc -z "$BRIDGE_HOST" "$BRIDGE_IMAP_PORT" 2>/dev/null; do
    sleep 2
done
echo ">>> Bridge IMAP is ready."

# Create Maildir structure if it doesn't exist
mkdir -p /maildir/INBOX/{cur,new,tmp}

# Initial sync
echo ">>> Running initial sync..."
mbsync -c /etc/mbsyncrc -a 2>&1 || echo ">>> Initial sync completed with warnings (may be normal on first run)"

# Continuous sync loop
echo ">>> Starting sync loop (interval: ${SYNC_INTERVAL}s)..."
while true; do
    sleep "$SYNC_INTERVAL"
    echo ">>> Syncing..."
    mbsync -c /etc/mbsyncrc -a 2>&1 || echo ">>> Sync warning (bridge may be busy)"
done
