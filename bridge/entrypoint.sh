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

readonly VAULT="$XDG_CONFIG_HOME/protonmail/bridge-v3/vault.enc"
readonly PASS_STORE_ID_FILE="$PASSWORD_STORE_DIR/.gpg-id"
readonly BOOTSTRAP_TIMEOUT_SECONDS=30

run_with_timeout() {
    local description="$1"
    shift

    if ! timeout "${BOOTSTRAP_TIMEOUT_SECONDS}s" "$@"; then
        echo "ERROR: ${description} failed or timed out after ${BOOTSTRAP_TIMEOUT_SECONDS}s." >&2
        exit 1
    fi
}

have_bridge_key() {
    timeout "${BOOTSTRAP_TIMEOUT_SECONDS}s" gpg --list-keys "ProtonBridge" >/dev/null 2>&1
}

bridge_fingerprint() {
    timeout "${BOOTSTRAP_TIMEOUT_SECONDS}s" gpg --list-keys --with-colons "ProtonBridge" \
        | awk -F: '/^fpr/{print $10; exit}'
}

main() {
    # make first-run sets BRIDGE_FORCE_CLI=true (docker-compose.first-run.yml).
    # Bridge writes vault.enc at startup, before any login, so a vault does
    # not show that an account is logged in: a retried, unfinished first run
    # must still get the CLI. Checked before anything touches the volume.
    local force_cli="${BRIDGE_FORCE_CLI:-false}"
    if [[ "$force_cli" != "true" && "$force_cli" != "false" ]]; then
        echo "ERROR: BRIDGE_FORCE_CLI must be 'true' or 'false'." >&2
        exit 1
    fi

    # =========================================================================
    # Bootstrap GPG and pass on first run
    # Only runs once and persists in the bridge-data volume.
    # The empty GPG passphrase is intentional: Bridge must restart unattended,
    # so the design relies on Docker volume isolation, restrictive
    # permissions, and host-level disk encryption rather than an interactive
    # key-unlock step.
    # =========================================================================
    if ! have_bridge_key; then
        echo ">>> First run: initializing GPG key and pass store..."

        run_with_timeout \
            "GPG key generation" \
            gpg --batch --passphrase '' --quick-gen-key \
            "ProtonBridge" default default never

        FPR="$(bridge_fingerprint)"
        [[ -n "$FPR" ]] \
            || { echo "ERROR: Failed to extract GPG fingerprint after key creation." >&2; exit 1; }

        run_with_timeout "pass store initialization" pass init "$FPR"

        echo ">>> GPG + pass initialized (fingerprint: $FPR)"
    fi

    if [[ ! -f "$PASS_STORE_ID_FILE" ]]; then
        echo ">>> Pass store metadata missing. Re-initializing pass store..."
        FPR="$(bridge_fingerprint)"
        [[ -n "$FPR" ]] \
            || { echo "ERROR: Failed to extract GPG fingerprint for pass store repair." >&2; exit 1; }
        run_with_timeout "pass store initialization" pass init "$FPR"
    fi

    # =========================================================================
    # Detect whether Bridge has a vault. A vault is not proof of a logged-in
    # account: Bridge creates it before any login.
    # =========================================================================
    local have_vault=false
    if [[ -f "$VAULT" ]]; then
        if have_bridge_key; then
            have_vault=true
        else
            echo "ERROR: vault.enc exists but GPG key 'ProtonBridge' is missing." >&2
            echo "       The vault cannot be decrypted. Remove the bridge-data volume and run: make first-run" >&2
            exit 1
        fi
    fi

    # =========================================================================
    # Launch
    # =========================================================================
    if [[ "$have_vault" == true && "$force_cli" == false ]]; then
        echo ">>> Vault found. Starting Bridge as user '$(whoami)'..."

        # exec replaces this shell with the bridge process — Docker tracks
        # bridge directly and SIGTERM from docker stop reaches it without a
        # wrapper.
        exec bridge --noninteractive
    fi

    cat <<'EOF'

┌──────────────────────────────────────────────────────────────┐
│  Bridge interactive CLI                                      │
│                                                              │
│  Steps:                                                      │
│    login    → enter your Proton email, password, and 2FA     │
│               (skip if `info` already lists your account)    │
│    info     → copy bridge username + password into secrets   │
│    exit                                                      │
│                                                              │
│  Then: make up                                               │
└──────────────────────────────────────────────────────────────┘

EOF

    exec bridge --cli
}

main
