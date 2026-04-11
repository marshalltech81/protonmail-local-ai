#!/bin/bash
set -Eeuo pipefail

VAULT="$XDG_CONFIG_HOME/protonmail/bridge-v3/vault.enc"
LOG_DIR="$XDG_DATA_HOME/protonmail/bridge-v3/logs"

BRIDGE_PID=""
WATCHER_PID=""

cleanup() {
    for pid in "$BRIDGE_PID" "$WATCHER_PID"; do
        if [ -n "${pid:-}" ]; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
}

trap cleanup EXIT TERM INT

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
# Follow Bridge log files and switch when rotation creates a new one
# This watches only Bridge logs: *_bri_*.log
# Change to *.log if you want every Proton log in the directory.
# =============================================================================
follow_bridge_logs() {
    mkdir -p "$LOG_DIR"

    local current=""
    local tail_pid=""

    stop_tail() {
        if [ -n "${tail_pid:-}" ]; then
            kill "$tail_pid" 2>/dev/null || true
            wait "$tail_pid" 2>/dev/null || true
        fi
    }

    get_newest_bridge_log() {
        shopt -s nullglob
        local files=( "$LOG_DIR"/*_bri_*.log )
        shopt -u nullglob

        if [ "${#files[@]}" -eq 0 ]; then
            return 0
        fi

        ls -1t "${files[@]}" 2>/dev/null | head -n1
    }

    attach_newest() {
        local newest
        newest="$(get_newest_bridge_log || true)"

        if [ -n "$newest" ] && [ "$newest" != "$current" ]; then
            stop_tail
            echo ">>> Forwarding Bridge log: $newest"
            tail -n +1 -F "$newest" &
            tail_pid=$!
            current="$newest"
        fi
    }

    trap stop_tail EXIT TERM INT

    # Attach immediately if a log file already exists
    attach_newest

    # Then react to new files created by rotation/startup
    inotifywait -m -q -e create -e moved_to --format '%f' "$LOG_DIR" | \
    while IFS= read -r filename; do
        case "$filename" in
            *_bri_*.log)
                attach_newest
                ;;
        esac
    done
}

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

    follow_bridge_logs &
    WATCHER_PID=$!

    bridge --noninteractive &
    BRIDGE_PID=$!

    wait "$BRIDGE_PID"
fi