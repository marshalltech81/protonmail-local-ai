#!/bin/bash
set -e

# All directories created as bridge user since USER bridge is set in Dockerfile
mkdir -p "$GNUPGHOME" \
         "$PASSWORD_STORE_DIR" \
         "$XDG_CONFIG_HOME" \
         "$XDG_CACHE_HOME"
chmod 700 "$GNUPGHOME"

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
    echo "│                                                               │"
    echo "│  Steps:                                                       │"
    echo "│    login    → enter your Proton email, password, and 2FA     │"
    echo "│    info     → copy bridge username + password into .env      │"
    echo "│    exit                                                       │"
    echo "│                                                               │"
    echo "│  Then: docker compose up -d                                  │"
    echo "└──────────────────────────────────────────────────────────────┘"
    echo ""
    exec bridge --cli
else
    echo ">>> Account found. Starting Bridge as user '$(whoami)' on 0.0.0.0..."
    exec bridge --noninteractive
fi
