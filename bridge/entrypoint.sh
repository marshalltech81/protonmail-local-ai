#!/bin/bash
set -e

# =============================================================================
# Bootstrap GPG and pass on first run
# Only runs once — persists in the bridge-data volume
# =============================================================================
if ! gpg --list-keys "ProtonBridge" &>/dev/null 2>&1; then
    echo ">>> First run: initializing GPG key and pass store..."

    gpg --batch --passphrase '' --quick-gen-key \
        'ProtonBridge' default default never 2>/dev/null

    FPR=$(gpg --list-keys --with-colons 'ProtonBridge' \
          | awk -F: '/^fpr/{print $10; exit}')

    pass init "$FPR"
    echo ">>> GPG + pass initialized (fingerprint: $FPR)"
fi

# =============================================================================
# Detect whether a Proton account is already authenticated
# Bridge writes vault.enc after a successful login — its presence means
# credentials are stored and Bridge can start noninteractively.
# =============================================================================
VAULT="$XDG_CONFIG_HOME/protonmail/bridge-v3/vault.enc"
LOGGED_IN=false
if [ -f "$VAULT" ]; then
    LOGGED_IN=true
fi

# =============================================================================
# Launch
# =============================================================================
if [ "$LOGGED_IN" = false ]; then
    echo ""
    echo "┌──────────────────────────────────────────────────────────────┐"
    echo "│  No Proton account found. Dropping to Bridge interactive CLI │"
    echo "│                                                              │"
    echo "│  Steps:                                                      │"
    echo "│    login    → enter your Proton email, password, and 2FA     │"
    echo "│    info     → copy bridge username + password into .env      │"
    echo "│    exit                                                      │"
    echo "│                                                              │"
    echo "│  Then: docker compose up -d                                  │"
    echo "└──────────────────────────────────────────────────────────────┘"
    echo ""
    exec bridge --cli
else
    echo ">>> Account found. Starting Bridge as user '$(whoami)' on 0.0.0.0..."

    bridge --noninteractive &
    BRIDGE_PID=$!

    # Forward SIGTERM/SIGINT to Bridge so Docker stop works cleanly
    trap 'kill "$BRIDGE_PID"' TERM INT

    # Tail Bridge log file to stdout once it appears
    LOG_DIR="$XDG_DATA_HOME/protonmail/bridge-v3/logs"
    echo ">>> Waiting for Bridge log file..."
    LOG_FILE=""
    while [ -z "$LOG_FILE" ]; do
        sleep 1
        LOG_FILE=$(ls -t "$LOG_DIR"/*.log 2>/dev/null | head -1)
    done
    echo ">>> Streaming logs from $LOG_FILE"
    tail -F "$LOG_FILE" &
    TAIL_PID=$!

    wait "$BRIDGE_PID"
    kill "$TAIL_PID" 2>/dev/null
fi
