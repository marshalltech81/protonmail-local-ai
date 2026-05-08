#!/bin/bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR
readonly ENV_FILE="${ROOT_DIR}/.env"
readonly BRIDGE_PASS_FILE="${ROOT_DIR}/.secrets/bridge_pass.txt"
readonly INFERENCE_OPENAI_KEY_FILE="${ROOT_DIR}/.secrets/inference_openai_api_key.txt"
readonly INFERENCE_ANTHROPIC_KEY_FILE="${ROOT_DIR}/.secrets/inference_anthropic_api_key.txt"
readonly EMBED_OPENAI_KEY_FILE="${ROOT_DIR}/.secrets/embed_openai_api_key.txt"

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

reject_deprecated_env() {
    local old_name="$1"
    local new_name="$2"
    local value

    value="$(get_env_value "$old_name")"
    [[ -z "$value" ]] || {
        printf 'ERROR: %s has been renamed to %s. Update .env before starting.\n' \
            "$old_name" "$new_name" >&2
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
require_file "$INFERENCE_OPENAI_KEY_FILE" "OpenAI-compatible inference API key secret file"
require_file "$INFERENCE_ANTHROPIC_KEY_FILE" "Anthropic-compatible inference API key secret file"
require_file "$EMBED_OPENAI_KEY_FILE" "OpenAI-compatible embedder API key secret file"

reject_deprecated_env "LLM_BASE_URL" "INFERENCE_OPENAI_BASE_URL"
reject_deprecated_env "LLM_MODEL" "INFERENCE_OPENAI_MODEL"
reject_deprecated_env "LLM_MODE" "INFERENCE_MODE"
reject_deprecated_env "CLAUDE_MODEL" "INFERENCE_ANTHROPIC_MODEL"
reject_deprecated_env "ANTHROPIC_API_KEY" "INFERENCE_ANTHROPIC_API_KEY"
reject_deprecated_env "EMBED_BASE_URL" "EMBED_OPENAI_BASE_URL"
reject_deprecated_env "EMBED_MODEL" "EMBED_OPENAI_MODEL"
reject_deprecated_env "EMBED_API_KEY" "EMBED_OPENAI_API_KEY"

BRIDGE_USER="$(get_env_value BRIDGE_USER)"
BRIDGE_VERSION="$(get_env_value BRIDGE_VERSION)"
INFERENCE_MODE="$(get_env_value INFERENCE_MODE)"
INFERENCE_OPENAI_BASE_URL="$(get_env_value INFERENCE_OPENAI_BASE_URL)"
INFERENCE_OPENAI_MODEL="$(get_env_value INFERENCE_OPENAI_MODEL)"
INFERENCE_ANTHROPIC_BASE_URL="$(get_env_value INFERENCE_ANTHROPIC_BASE_URL)"
INFERENCE_ANTHROPIC_MODEL="$(get_env_value INFERENCE_ANTHROPIC_MODEL)"
EMBED_OPENAI_BASE_URL="$(get_env_value EMBED_OPENAI_BASE_URL)"
EMBED_OPENAI_MODEL="$(get_env_value EMBED_OPENAI_MODEL)"
RERANK_ENABLED="$(get_env_value RERANK_ENABLED)"
RERANK_BASE_URL="$(get_env_value RERANK_BASE_URL)"
SYNC_INTERVAL="$(get_env_value SYNC_INTERVAL)"
MCP_PORT="$(get_env_value MCP_PORT)"
MCP_TRANSPORT="$(get_env_value MCP_TRANSPORT)"
MCP_READ_ONLY="$(get_env_value MCP_READ_ONLY)"

[[ -n "$BRIDGE_USER" && "$BRIDGE_USER" != "your@proton.me" ]] || {
    echo "ERROR: BRIDGE_USER in .env must be set to the Bridge username from 'bridge --cli info'." >&2
    exit 1
}

[[ -n "$BRIDGE_VERSION" ]] || {
    echo "ERROR: BRIDGE_VERSION must be set in .env." >&2
    exit 1
}

INFERENCE_MODE="${INFERENCE_MODE:-anthropic}"
[[ "$INFERENCE_MODE" =~ ^(openai|anthropic)$ ]] || {
    echo "ERROR: INFERENCE_MODE must be 'openai' or 'anthropic'." >&2
    exit 1
}

# Embedder is always required — the indexer cannot run without it,
# regardless of which inference mode is in use.
[[ -n "$EMBED_OPENAI_BASE_URL" ]] || {
    echo "ERROR: EMBED_OPENAI_BASE_URL must be set in .env (OpenAI-compatible /v1 base URL of your embedder)." >&2
    exit 1
}

[[ "$EMBED_OPENAI_BASE_URL" =~ ^https?:// ]] || {
    echo "ERROR: EMBED_OPENAI_BASE_URL must start with http:// or https://." >&2
    exit 1
}

[[ -n "$EMBED_OPENAI_MODEL" ]] || {
    echo "ERROR: EMBED_OPENAI_MODEL must be set in .env (model id served at EMBED_OPENAI_BASE_URL)." >&2
    exit 1
}

# OpenAI-compatible inference vars are required only when that client
# is selected.
if [[ "$INFERENCE_MODE" == "openai" ]]; then
    [[ -n "$INFERENCE_OPENAI_BASE_URL" ]] || {
        echo "ERROR: INFERENCE_OPENAI_BASE_URL must be set in .env when INFERENCE_MODE=openai." >&2
        exit 1
    }

    [[ "$INFERENCE_OPENAI_BASE_URL" =~ ^https?:// ]] || {
        echo "ERROR: INFERENCE_OPENAI_BASE_URL must start with http:// or https://." >&2
        exit 1
    }

    [[ -n "$INFERENCE_OPENAI_MODEL" ]] || {
        echo "ERROR: INFERENCE_OPENAI_MODEL must be set in .env (model id served at INFERENCE_OPENAI_BASE_URL)." >&2
        exit 1
    }
fi

# Anthropic-compatible inference vars must be syntactically valid even
# in openai mode (the values are still wired into the container env so
# a flip to anthropic mode doesn't require a stack rebuild). The
# nonempty-key check below only fires when the mode is actually anthropic.
[[ -n "$INFERENCE_ANTHROPIC_BASE_URL" ]] || {
    echo "ERROR: INFERENCE_ANTHROPIC_BASE_URL must be set in .env (Anthropic-compatible base URL, e.g. https://api.anthropic.com/v1)." >&2
    exit 1
}

[[ "$INFERENCE_ANTHROPIC_BASE_URL" =~ ^https?:// ]] || {
    echo "ERROR: INFERENCE_ANTHROPIC_BASE_URL must start with http:// or https://." >&2
    exit 1
}

[[ -n "$INFERENCE_ANTHROPIC_MODEL" ]] || {
    echo "ERROR: INFERENCE_ANTHROPIC_MODEL must be set in .env (model id served at INFERENCE_ANTHROPIC_BASE_URL, e.g. claude-sonnet-4-6)." >&2
    exit 1
}

# Reranker is opt-in. Default to false when unset, validate the URL
# only when the flag is true.
RERANK_ENABLED="${RERANK_ENABLED:-false}"
[[ "$RERANK_ENABLED" =~ ^(true|false)$ ]] || {
    echo "ERROR: RERANK_ENABLED must be 'true' or 'false'." >&2
    exit 1
}

if [[ "$RERANK_ENABLED" == "true" ]]; then
    [[ -n "$RERANK_BASE_URL" ]] || {
        echo "ERROR: RERANK_BASE_URL must be set in .env when RERANK_ENABLED=true." >&2
        exit 1
    }

    [[ "$RERANK_BASE_URL" =~ ^https?:// ]] || {
        echo "ERROR: RERANK_BASE_URL must start with http:// or https://." >&2
        exit 1
    }
fi

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

require_nonempty_file "$BRIDGE_PASS_FILE" "Bridge password secret"
require_mode_600 "$BRIDGE_PASS_FILE"
require_mode_600 "$INFERENCE_OPENAI_KEY_FILE"
require_mode_600 "$INFERENCE_ANTHROPIC_KEY_FILE"
require_mode_600 "$EMBED_OPENAI_KEY_FILE"

if [[ "$INFERENCE_MODE" == "anthropic" ]]; then
    require_nonempty_file \
        "$INFERENCE_ANTHROPIC_KEY_FILE" \
        "Anthropic-compatible inference API key secret"
fi

printf 'Environment validation passed.\n'
