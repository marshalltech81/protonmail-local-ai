#!/bin/bash
set -Eeuo pipefail

# Bridge expects these runtime paths to be set explicitly. Unset before
# exporting so a caller-injected value cannot silently redirect Bridge state
# to an unexpected path. Exported here rather than baked into Dockerfile
# metadata so Trivy does not flag PASSWORD_STORE_DIR as a leaked secret.
unset HOME XDG_CONFIG_HOME XDG_DATA_HOME XDG_CACHE_HOME GNUPGHOME PASSWORD_STORE_DIR
export HOME="/home/bridge"
export XDG_CONFIG_HOME="/data/config"
export XDG_DATA_HOME="/data/local"
export XDG_CACHE_HOME="/data/cache"
export GNUPGHOME="/data/gnupg"
export PASSWORD_STORE_DIR="/data/pass"

VAULT="$XDG_CONFIG_HOME/protonmail/bridge-v3/vault.enc"

# =============================================================================
# Bootstrap GPG and pass on first run
# Only runs once and persists in the bridge-data volume.
# The empty GPG passphrase is intentional: Bridge must restart unattended, so
# the design relies on Docker volume isolation, restrictive permissions, and
# host-level disk encryption rather than an interactive key-unlock step.
# =============================================================================
if ! gpg --list-keys "ProtonBridge" >/dev/null 2>&1; then
    echo ">>> First run: initializing GPG key and pass store..."

    gpg --batch --passphrase '' --quick-gen-key \
        'ProtonBridge' default default never \
        || { echo "ERROR: GPG key generation failed." >&2; exit 1; }

    FPR="$(gpg --list-keys --with-colons 'ProtonBridge' | awk -F: '/^fpr/{print $10; exit}')"
    [[ -n "$FPR" ]] \
        || { echo "ERROR: Failed to extract GPG fingerprint after key creation." >&2; exit 1; }

    pass init "$FPR" \
        || { echo "ERROR: pass store initialization failed." >&2; exit 1; }

    echo ">>> GPG + pass initialized (fingerprint: $FPR)"
fi

# =============================================================================
# Detect whether a Proton account is already authenticated
# =============================================================================
LOGGED_IN=false
if [[ -f "$VAULT" ]]; then
    if gpg --list-keys "ProtonBridge" >/dev/null 2>&1; then
        LOGGED_IN=true
    else
        echo "ERROR: vault.enc exists but GPG key 'ProtonBridge' is missing." >&2
        echo "       The vault cannot be decrypted. Remove the bridge-data volume and run: make first-run" >&2
        exit 1
    fi
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
