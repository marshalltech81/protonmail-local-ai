#!/bin/bash
set -Eeuo pipefail

# Bridge expects these runtime paths to be set explicitly. Export them here
# instead of baking PASSWORD_STORE_DIR into Dockerfile metadata so Trivy does
# not flag it as a leaked secret purely because of the variable name.
export HOME="${HOME:-/home/bridge}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-/data/config}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-/data/local}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/data/cache}"
export GNUPGHOME="${GNUPGHOME:-/data/gnupg}"
export PASSWORD_STORE_DIR="${PASSWORD_STORE_DIR:-/data/pass}"

VAULT="$XDG_CONFIG_HOME/protonmail/bridge-v3/vault.enc"

# =============================================================================
# Bootstrap GPG and pass on first run
# Only runs once and persists in the bridge-data volume
# =============================================================================
if ! gpg --list-keys "ProtonBridge" >/dev/null 2>&1; then
    echo ">>> First run: initializing GPG key and pass store..."

    gpg --batch --passphrase '' --quick-gen-key \
        'ProtonBridge' default default never >/dev/null 2>&1

    FPR="$(gpg --list-keys --with-colons 'ProtonBridge' | awk -F: '/^fpr/{print $10; exit}')"
    pass init "$FPR"

    echo ">>> GPG + pass initialized (fingerprint: $FPR)"
fi

# =============================================================================
# Detect whether a Proton account is already authenticated
# =============================================================================
LOGGED_IN=false
if [ -f "$VAULT" ]; then
    LOGGED_IN=true
fi

# =============================================================================
# Launch
# =============================================================================
if [ "$LOGGED_IN" = false ]; then
    cat <<'EOF'

┌──────────────────────────────────────────────────────────────┐
│  No Proton account found. Dropping to Bridge interactive CLI │
│                                                              │
│  Steps:                                                      │
│    login    → enter your Proton email, password, and 2FA     │
│    info     → copy bridge username + password into secrets   │
│    exit                                                      │
│                                                              │
│  Then: docker compose up -d                                  │
└──────────────────────────────────────────────────────────────┘

EOF

    exec bridge --cli
else
    echo ">>> Account found. Starting Bridge as user '$(whoami)'..."

    # exec replaces this shell with the bridge process — Docker tracks bridge
    # directly and SIGTERM from docker stop reaches it without a wrapper.
    exec bridge --noninteractive
fi
