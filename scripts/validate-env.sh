#!/bin/bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR
readonly ENV_FILE="${ROOT_DIR}/.env"
readonly BRIDGE_PASS_FILE="${ROOT_DIR}/.secrets/bridge_pass.txt"
readonly INFERENCE_KEY_FILE="${ROOT_DIR}/.secrets/inference_api_key.txt"
readonly EMBED_KEY_FILE="${ROOT_DIR}/.secrets/embed_api_key.txt"
readonly RERANK_KEY_FILE="${ROOT_DIR}/.secrets/rerank_api_key.txt"

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
require_file "$INFERENCE_KEY_FILE" "Inference API key secret file"
require_file "$EMBED_KEY_FILE" "Embed API key secret file"
require_file "$RERANK_KEY_FILE" "Rerank API key secret file"

# Inference / embed / rerank env vars collapsed into one *_MODE-based shape
# (no provider-namespaced vars). Mode selects the wire protocol; the
# remaining vars (BASE_URL, MODEL, API_KEY) configure that mode. ``none``
# disables a layer entirely. There is no inter-mode fallback — choosing
# a mode without its required vars is a startup error here, not a silent
# reroute to a different provider.
reject_deprecated_env "LLM_BASE_URL" "INFERENCE_BASE_URL"
reject_deprecated_env "LLM_MODEL" "INFERENCE_MODEL"
reject_deprecated_env "LLM_MODE" "INFERENCE_MODE"
reject_deprecated_env "CLAUDE_MODEL" "INFERENCE_MODEL"
reject_deprecated_env "ANTHROPIC_API_KEY" "INFERENCE_API_KEY"
reject_deprecated_env "INFERENCE_OPENAI_BASE_URL" "INFERENCE_BASE_URL"
reject_deprecated_env "INFERENCE_OPENAI_MODEL" "INFERENCE_MODEL"
reject_deprecated_env "INFERENCE_OPENAI_API_KEY" "INFERENCE_API_KEY"
reject_deprecated_env "INFERENCE_ANTHROPIC_BASE_URL" "INFERENCE_BASE_URL"
reject_deprecated_env "INFERENCE_ANTHROPIC_MODEL" "INFERENCE_MODEL"
reject_deprecated_env "INFERENCE_ANTHROPIC_API_KEY" "INFERENCE_API_KEY"
reject_deprecated_env "EMBED_OPENAI_BASE_URL" "EMBED_BASE_URL"
reject_deprecated_env "EMBED_OPENAI_MODEL" "EMBED_MODEL"
reject_deprecated_env "EMBED_OPENAI_API_KEY" "EMBED_API_KEY"
reject_deprecated_env "RERANK_ENABLED" "RERANK_MODE"
# Earlier-era names that predate the *_MODE shape entirely. Carrying
# any of these in .env would silently get the *_MODE default applied;
# rejecting them keeps the migration message unambiguous.
reject_deprecated_env "EMBED_SERVICE_URL" "EMBED_BASE_URL"
reject_deprecated_env "MLX_SERVICE_URL" "EMBED_BASE_URL"
reject_deprecated_env "OLLAMA_EMBED_MODEL" "EMBED_MODEL"
reject_deprecated_env "OLLAMA_LLM_MODEL" "INFERENCE_MODEL"
reject_deprecated_env "USE_MLX_EMBEDDER" "EMBED_MODE"
reject_deprecated_env "USE_MLX_RERANKER" "RERANK_MODE"

BRIDGE_USER="$(get_env_value BRIDGE_USER)"
BRIDGE_VERSION="$(get_env_value BRIDGE_VERSION)"
INFERENCE_MODE="$(get_env_value INFERENCE_MODE)"
INFERENCE_BASE_URL="$(get_env_value INFERENCE_BASE_URL)"
INFERENCE_MODEL="$(get_env_value INFERENCE_MODEL)"
EMBED_MODE="$(get_env_value EMBED_MODE)"
EMBED_BASE_URL="$(get_env_value EMBED_BASE_URL)"
EMBED_MODEL="$(get_env_value EMBED_MODEL)"
RERANK_MODE="$(get_env_value RERANK_MODE)"
RERANK_BASE_URL="$(get_env_value RERANK_BASE_URL)"
RERANK_MODEL="$(get_env_value RERANK_MODEL)"
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

# ----- INFERENCE -----
INFERENCE_MODE="${INFERENCE_MODE:-anthropic}"
[[ "$INFERENCE_MODE" =~ ^(openai|anthropic|none)$ ]] || {
    echo "ERROR: INFERENCE_MODE must be one of: anthropic, openai, none." >&2
    exit 1
}

if [[ "$INFERENCE_MODE" != "none" ]]; then
    [[ -n "$INFERENCE_MODEL" ]] || {
        echo "ERROR: INFERENCE_MODEL must be set when INFERENCE_MODE=$INFERENCE_MODE." >&2
        exit 1
    }
    if [[ "$INFERENCE_MODE" == "openai" ]]; then
        # OpenAI-compatible mode requires an explicit endpoint; anthropic
        # mode falls through to the SDK default when INFERENCE_BASE_URL
        # is empty, so URL validation only fires when a value was set.
        [[ -n "$INFERENCE_BASE_URL" ]] || {
            echo "ERROR: INFERENCE_BASE_URL must be set when INFERENCE_MODE=openai." >&2
            exit 1
        }
    fi
    if [[ -n "$INFERENCE_BASE_URL" ]]; then
        [[ "$INFERENCE_BASE_URL" =~ ^https?:// ]] || {
            echo "ERROR: INFERENCE_BASE_URL must start with http:// or https://." >&2
            exit 1
        }
    fi
fi

# ----- EMBED -----
# Indexer + mcp-server share these vars. mcp-server accepts ``none`` for a
# keyword-only retrieval surface; the indexer rejects ``none`` at startup
# because it cannot ingest mail without an embedder. Validate the shape
# here — the indexer / mcp-server containers each enforce their own
# tighter rule.
EMBED_MODE="${EMBED_MODE:-openai}"
[[ "$EMBED_MODE" =~ ^(openai|none)$ ]] || {
    echo "ERROR: EMBED_MODE must be one of: openai, none." >&2
    exit 1
}

if [[ "$EMBED_MODE" != "none" ]]; then
    [[ -n "$EMBED_BASE_URL" ]] || {
        echo "ERROR: EMBED_BASE_URL must be set when EMBED_MODE=$EMBED_MODE." >&2
        exit 1
    }
    [[ "$EMBED_BASE_URL" =~ ^https?:// ]] || {
        echo "ERROR: EMBED_BASE_URL must start with http:// or https://." >&2
        exit 1
    }
    [[ -n "$EMBED_MODEL" ]] || {
        echo "ERROR: EMBED_MODEL must be set when EMBED_MODE=$EMBED_MODE." >&2
        exit 1
    }
fi

# ----- RERANK -----
RERANK_MODE="${RERANK_MODE:-none}"
[[ "$RERANK_MODE" =~ ^(cohere|none)$ ]] || {
    echo "ERROR: RERANK_MODE must be one of: cohere, none." >&2
    exit 1
}

if [[ "$RERANK_MODE" != "none" ]]; then
    [[ -n "$RERANK_MODEL" ]] || {
        echo "ERROR: RERANK_MODEL must be set when RERANK_MODE=$RERANK_MODE." >&2
        exit 1
    }
    if [[ -n "$RERANK_BASE_URL" ]]; then
        [[ "$RERANK_BASE_URL" =~ ^https?:// ]] || {
            echo "ERROR: RERANK_BASE_URL must start with http:// or https://." >&2
            exit 1
        }
    fi
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
require_mode_600 "$INFERENCE_KEY_FILE"
require_mode_600 "$EMBED_KEY_FILE"
require_mode_600 "$RERANK_KEY_FILE"

# Each active mode's secret file must contain a real value. Disabled
# layers can leave the file empty (it must still exist with mode 600 so
# the docker-compose ``secrets:`` reference resolves cleanly).
if [[ "$INFERENCE_MODE" != "none" ]]; then
    require_nonempty_file "$INFERENCE_KEY_FILE" "Inference API key"
fi
if [[ "$RERANK_MODE" != "none" ]]; then
    require_nonempty_file "$RERANK_KEY_FILE" "Rerank API key"
fi

printf 'Environment validation passed.\n'
