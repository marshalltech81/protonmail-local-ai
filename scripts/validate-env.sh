#!/bin/bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR
readonly ENV_FILE="${ROOT_DIR}/.env"
readonly BRIDGE_PASS_FILE="${ROOT_DIR}/.secrets/bridge_pass.txt"
readonly ANTHROPIC_KEY_FILE="${ROOT_DIR}/.secrets/anthropic_api_key.txt"

require_file() {
    local path="$1"
    local description="$2"

    [[ -f "$path" ]] || {
        printf 'ERROR: %s not found at %s.\n' "$description" "$path" >&2
        exit 1
    }
}

require_nonempty_file() {
    local path="$1"
    local description="$2"

    [[ -s "$path" ]] || {
        printf 'ERROR: %s is missing or empty at %s.\n' "$description" "$path" >&2
        exit 1
    }
}

file_mode() {
    local path="$1"
    local mode

    # GNU coreutils (Linux containers) uses -c; BSD (macOS host) uses -f.
    # validate-env runs on the operator's host via `make up`, so both must
    # work — stderr is suppressed on each attempt to avoid surfacing the
    # format-flag mismatch as a spurious error.
    if mode=$(stat -c '%a' "$path" 2>/dev/null); then
        printf '%s\n' "$mode"
        return 0
    fi
    if mode=$(stat -f '%Lp' "$path" 2>/dev/null); then
        printf '%s\n' "$mode"
        return 0
    fi
    printf 'ERROR: unable to read file mode for %s on this platform.\n' "$path" >&2
    return 1
}

require_mode_600() {
    local path="$1"
    local actual_mode

    actual_mode="$(file_mode "$path")"
    [[ "$actual_mode" == "600" ]] || {
        printf 'ERROR: %s must have mode 600, found %s.\n' "$path" "$actual_mode" >&2
        exit 1
    }
}

require_integer() {
    local name="$1"
    local value="$2"

    [[ "$value" =~ ^[0-9]+$ ]] || {
        printf 'ERROR: %s must be an integer, found %s.\n' "$name" "$value" >&2
        exit 1
    }
}

# Read a single KEY=VALUE from .env without shell-sourcing.
# Shell-sourcing would evaluate command substitutions in values, so a
# malformed or hostile .env line could execute arbitrary commands from the
# operator's host. Parsing known keys avoids that entire class of issue.
#
# Semantics:
#   - last assignment wins (matches `source` behavior)
#   - comment (#) and blank lines are ignored
#   - optional surrounding single or double quotes are stripped
#   - no variable expansion, no command substitution, no escape processing
get_env_value() {
    local key="$1"
    local line raw value

    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || {
        printf 'ERROR: invalid environment key: %s\n' "$key" >&2
        return 2
    }

    raw=""
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^[[:space:]]*${key}= ]]; then
            raw="$line"
        fi
    done < "$ENV_FILE"
    [[ -n "$raw" ]] || { printf '\n'; return 0; }

    value="${raw#*=}"
    # strip a single pair of matching surrounding quotes, if present
    if [[ "$value" =~ ^\"(.*)\"$ ]]; then
        value="${BASH_REMATCH[1]}"
    elif [[ "$value" =~ ^\'(.*)\'$ ]]; then
        value="${BASH_REMATCH[1]}"
    fi
    printf '%s\n' "$value"
}

if [[ "${1:-}" == "--get" ]]; then
    [[ $# -eq 2 ]] || {
        echo "ERROR: usage: validate-env.sh --get KEY" >&2
        exit 1
    }
    require_file "$ENV_FILE" ".env"
    get_env_value "$2"
    exit 0
fi

require_file "$ENV_FILE" ".env"
require_file "$BRIDGE_PASS_FILE" "Bridge password secret"
require_file "$ANTHROPIC_KEY_FILE" "Anthropic API key secret file"

BRIDGE_USER="$(get_env_value BRIDGE_USER)"
BRIDGE_VERSION="$(get_env_value BRIDGE_VERSION)"
OLLAMA_EMBED_MODEL="$(get_env_value OLLAMA_EMBED_MODEL)"
OLLAMA_LLM_MODEL="$(get_env_value OLLAMA_LLM_MODEL)"
SYNC_INTERVAL="$(get_env_value SYNC_INTERVAL)"
MCP_PORT="$(get_env_value MCP_PORT)"
MCP_TRANSPORT="$(get_env_value MCP_TRANSPORT)"
MCP_READ_ONLY="$(get_env_value MCP_READ_ONLY)"
LLM_MODE="$(get_env_value LLM_MODE)"
OPEN_WEBUI_PORT="$(get_env_value OPEN_WEBUI_PORT)"

[[ -n "$BRIDGE_USER" && "$BRIDGE_USER" != "your@proton.me" ]] || {
    echo "ERROR: BRIDGE_USER in .env must be set to the Bridge username from 'bridge --cli info'." >&2
    exit 1
}

[[ -n "$BRIDGE_VERSION" ]] || {
    echo "ERROR: BRIDGE_VERSION must be set in .env." >&2
    exit 1
}

[[ -n "$OLLAMA_EMBED_MODEL" ]] || {
    echo "ERROR: OLLAMA_EMBED_MODEL must be set in .env." >&2
    exit 1
}

[[ -n "$OLLAMA_LLM_MODEL" ]] || {
    echo "ERROR: OLLAMA_LLM_MODEL must be set in .env." >&2
    exit 1
}

require_integer "SYNC_INTERVAL" "$SYNC_INTERVAL"
[[ "$SYNC_INTERVAL" -gt 0 ]] || {
    echo "ERROR: SYNC_INTERVAL must be greater than zero." >&2
    exit 1
}

require_integer "MCP_PORT" "$MCP_PORT"
[[ "$MCP_PORT" -ge 1 && "$MCP_PORT" -le 65535 ]] || {
    echo "ERROR: MCP_PORT must be between 1 and 65535." >&2
    exit 1
}

MCP_TRANSPORT="${MCP_TRANSPORT:-sse}"
[[ "$MCP_TRANSPORT" =~ ^(sse|streamable-http|dual)$ ]] || {
    echo "ERROR: MCP_TRANSPORT must be 'sse', 'streamable-http', or 'dual'." >&2
    exit 1
}

[[ "$MCP_READ_ONLY" =~ ^(true|false)$ ]] || {
    echo "ERROR: MCP_READ_ONLY must be 'true' or 'false'." >&2
    exit 1
}

[[ "$LLM_MODE" =~ ^(local|cloud)$ ]] || {
    echo "ERROR: LLM_MODE must be 'local' or 'cloud'." >&2
    exit 1
}

require_nonempty_file "$BRIDGE_PASS_FILE" "Bridge password secret"
require_mode_600 "$BRIDGE_PASS_FILE"

if [[ "$LLM_MODE" == "cloud" ]]; then
    require_nonempty_file "$ANTHROPIC_KEY_FILE" "Anthropic API key secret"
    require_mode_600 "$ANTHROPIC_KEY_FILE"
fi

if [[ -n "$OPEN_WEBUI_PORT" ]]; then
    require_integer "OPEN_WEBUI_PORT" "$OPEN_WEBUI_PORT"
    [[ "$OPEN_WEBUI_PORT" -ge 1 && "$OPEN_WEBUI_PORT" -le 65535 ]] || {
        echo "ERROR: OPEN_WEBUI_PORT must be between 1 and 65535." >&2
        exit 1
    }
fi

printf 'Environment validation passed.\n'
