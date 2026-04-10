#!/bin/bash
# =============================================================================
# ProtonBridge entrypoint
# Handles GPG/pass bootstrap on first run, then launches Bridge.
# =============================================================================
set -e

# Ensure all data directories exist with correct permissions
mkdir -p "$GNUPGHOME" \
         "$PASSWORD_STORE_DIR" \
         "$XDG_CONFIG_HOME" \
         "$XDG_CACHE_HOME"
chmod 700 "$GNUPGHOME"

# =============================================================================
# Bootstrap GPG and pass on first run
# Bridge requires a keychain on Linux. We use pass backed by GPG.
# This only runs once — credentials persist in the bridge-data volume.
# =============================================================================
if ! gpg --list-keys "ProtonBridge" &>/dev/null; then
    echo ">>> First run: initializing GPG key and pass store..."

    # Generate a no-passphrase GPG key for the pass store
    # The key protects the pass store; the volume protects the key.
    gpg --batch --passphrase '' --quick-gen-key \
        'ProtonBridge' default default never 2>/dev/null

    # Get the fingerprint of the key we just created
    FPR=$(gpg --list-keys --with-colons 'ProtonBridge' \
          | awk -F: '/^fpr/{print $10; exit}')

    # Initialize the pass store with this key
    pass init "$FPR"

    echo ">>> GPG + pass initialized (fingerprint: $FPR)"
fi

# =============================================================================
# Detect whether a Proton account is already authenticated
# Bridge writes account cache files after first successful login.
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
    echo "│                                                               │"
    echo "│  Steps:                                                       │"
    echo "│    login    → enter your Proton email, password, and 2FA     │"
    echo "│    info     → copy bridge username + password into .env      │"
    echo "│    exit                                                       │"
    echo "│                                                               │"
    echo "│  Then restart: docker compose up -d protonmail-bridge        │"
    echo "└──────────────────────────────────────────────────────────────┘"
    echo ""
    exec bridge --cli
else
    echo ">>> Proton account found. Starting Bridge in noninteractive mode..."
    exec bridge --noninteractive
fi
