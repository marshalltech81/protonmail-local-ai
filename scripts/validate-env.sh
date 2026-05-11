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

# API keys are wired as Docker secrets (see ``secrets:`` in
# docker-compose.yml). Putting them in ``.env`` would surface them in
# ``docker inspect`` and is therefore disallowed — both for old names
# that have been renamed and for the current names themselves.
reject_secret_in_env() {
    local key_name="$1"
    local secret_path="$2"
    local value

    value="$(get_env_value "$key_name")"
    [[ -z "$value" ]] || {
        printf 'ERROR: %s must be stored in %s (Docker secret), not .env. Move the value and remove it from .env before starting.\n' \
            "$key_name" "$secret_path" >&2
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

# Validate as integer (>= minimum). Accept int form only — the python
# *_env helpers will tolerate floats for timeout-style vars, but the
# operator-facing default in .env.example is always an integer and
# rejecting decimal values here keeps the validation contract simple.
require_integer_min() {
    local name="$1"
    local value="$2"
    local minimum="$3"

    require_integer "$name" "$value"
    [[ "$value" -ge "$minimum" ]] || {
        printf 'ERROR: %s must be >= %s, found %s.\n' "$name" "$minimum" "$value" >&2
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

# Detect pre-collapse secret filenames before requiring the new ones,
# so an operator upgrading from the prior layout sees explicit
# migration guidance instead of a generic "file not found" followed by
# ``init-secrets`` creating an empty new file (which would surface as
# "API key empty" much later, never pointing back at the old file).
# ``init-secrets`` does not detect this case either — it short-circuits
# on the presence of the new filename — so the check has to land here
# before ``require_file`` fires below.
require_renamed_secret_migrated() {
    local old_path="$1"
    local new_path="$2"

    [[ -f "$old_path" ]] || return 0
    printf 'ERROR: legacy secret file %s exists.\n' "$old_path" >&2
    printf '       The *_MODE collapse refactor renamed the secret files.\n' >&2
    if [[ ! -s "$new_path" ]]; then
        printf '       Move the key value before starting:\n' >&2
        printf '         mv %s %s\n' "$old_path" "$new_path" >&2
        printf '         chmod 600 %s\n' "$new_path" >&2
    else
        printf '       %s already holds a value; confirm it was migrated,\n' "$new_path" >&2
        printf '       then remove the legacy file:\n' >&2
        printf '         rm %s\n' "$old_path" >&2
    fi
    exit 1
}
require_renamed_secret_migrated \
    "${ROOT_DIR}/.secrets/inference_anthropic_api_key.txt" "$INFERENCE_KEY_FILE"
require_renamed_secret_migrated \
    "${ROOT_DIR}/.secrets/inference_openai_api_key.txt" "$INFERENCE_KEY_FILE"
require_renamed_secret_migrated \
    "${ROOT_DIR}/.secrets/embed_openai_api_key.txt" "$EMBED_KEY_FILE"
require_renamed_secret_migrated \
    "${ROOT_DIR}/.secrets/anthropic_api_key.txt" "$INFERENCE_KEY_FILE"

require_file "$INFERENCE_KEY_FILE" "Inference API key secret file"
require_file "$EMBED_KEY_FILE" "Embed API key secret file"
require_file "$RERANK_KEY_FILE" "Rerank API key secret file"

# Inference / embed / rerank env vars collapsed into one *_MODE-based shape
# (no provider-namespaced vars). Mode selects the wire protocol; the
# remaining vars (BASE_URL, MODEL, API_KEY) configure that mode. ``none``
# disables a layer entirely. There is no inter-mode fallback — choosing
# a mode without its required vars is a startup error here, not a silent
# reroute to a different provider.
#
# API keys move via Docker secrets (``.secrets/*_api_key.txt``), not
# ``.env``. The reject helpers below split into two flavors: renamed
# non-secret vars get the standard "renamed → use new name in .env"
# message; renamed secret vars get the "moved to Docker secret file"
# message so the operator does not paste a key into .env where it would
# surface in ``docker inspect``.
reject_deprecated_env "LLM_BASE_URL" "INFERENCE_BASE_URL"
reject_deprecated_env "LLM_MODEL" "INFERENCE_MODEL"
reject_deprecated_env "LLM_MODE" "INFERENCE_MODE"
reject_deprecated_env "CLAUDE_MODEL" "INFERENCE_MODEL"
reject_secret_in_env "ANTHROPIC_API_KEY" "$INFERENCE_KEY_FILE"
reject_deprecated_env "INFERENCE_OPENAI_BASE_URL" "INFERENCE_BASE_URL"
reject_deprecated_env "INFERENCE_OPENAI_MODEL" "INFERENCE_MODEL"
reject_secret_in_env "INFERENCE_OPENAI_API_KEY" "$INFERENCE_KEY_FILE"
reject_deprecated_env "INFERENCE_ANTHROPIC_BASE_URL" "INFERENCE_BASE_URL"
reject_deprecated_env "INFERENCE_ANTHROPIC_MODEL" "INFERENCE_MODEL"
reject_secret_in_env "INFERENCE_ANTHROPIC_API_KEY" "$INFERENCE_KEY_FILE"
reject_deprecated_env "EMBED_OPENAI_BASE_URL" "EMBED_BASE_URL"
reject_deprecated_env "EMBED_OPENAI_MODEL" "EMBED_MODEL"
reject_secret_in_env "EMBED_OPENAI_API_KEY" "$EMBED_KEY_FILE"
reject_deprecated_env "RERANK_ENABLED" "RERANK_MODE"
# Current *_API_KEY names must never appear in .env either — they are
# wired as Docker secrets in docker-compose.yml. An operator who pastes
# them into .env would (a) leak the value into ``docker inspect``
# output and (b) silently mask the secret-file value, since the
# container ignores the env when ``_FILE`` indirection is used.
reject_secret_in_env "INFERENCE_API_KEY" "$INFERENCE_KEY_FILE"
reject_secret_in_env "EMBED_API_KEY" "$EMBED_KEY_FILE"
reject_secret_in_env "RERANK_API_KEY" "$RERANK_KEY_FILE"
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
INFERENCE_TIMEOUT_SECS="$(get_env_value INFERENCE_TIMEOUT_SECS)"
INFERENCE_MAX_TOKENS="$(get_env_value INFERENCE_MAX_TOKENS)"
EMBED_MODE="$(get_env_value EMBED_MODE)"
EMBED_BASE_URL="$(get_env_value EMBED_BASE_URL)"
EMBED_MODEL="$(get_env_value EMBED_MODEL)"
EMBED_TIMEOUT_SECS="$(get_env_value EMBED_TIMEOUT_SECS)"
EMBED_WARMUP_TIMEOUT_SECS="$(get_env_value EMBED_WARMUP_TIMEOUT_SECS)"
RERANK_MODE="$(get_env_value RERANK_MODE)"
RERANK_BASE_URL="$(get_env_value RERANK_BASE_URL)"
RERANK_MODEL="$(get_env_value RERANK_MODEL)"
RERANK_CANDIDATES="$(get_env_value RERANK_CANDIDATES)"
RERANK_TOP_N="$(get_env_value RERANK_TOP_N)"
RERANK_TIMEOUT_SECS="$(get_env_value RERANK_TIMEOUT_SECS)"
INDEXER_PARSE_MAX_BYTES="$(get_env_value INDEXER_PARSE_MAX_BYTES)"
INDEXER_MAX_ATTEMPTS="$(get_env_value INDEXER_MAX_ATTEMPTS)"
INDEXER_RETRY_BASE_SECONDS="$(get_env_value INDEXER_RETRY_BASE_SECONDS)"
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
    # Optional inference tuning knobs. Validate only when set so the
    # defaults in mcp-server/src/main.py remain authoritative when the
    # operator leaves the value blank. ``INFERENCE_TIMEOUT_SECS`` must
    # be >= 1 to bound a stalled inference call without rejecting
    # routine sub-second failures. ``INFERENCE_MAX_TOKENS`` must be
    # >= 1 — zero or negative would request an empty completion.
    if [[ -n "$INFERENCE_TIMEOUT_SECS" ]]; then
        require_integer_min "INFERENCE_TIMEOUT_SECS" "$INFERENCE_TIMEOUT_SECS" 1
    fi
    if [[ -n "$INFERENCE_MAX_TOKENS" ]]; then
        require_integer_min "INFERENCE_MAX_TOKENS" "$INFERENCE_MAX_TOKENS" 1
    fi
    if [[ -n "$INFERENCE_BASE_URL" ]]; then
        [[ "$INFERENCE_BASE_URL" =~ ^https?:// ]] || {
            echo "ERROR: INFERENCE_BASE_URL must start with http:// or https://." >&2
            exit 1
        }
        # The Anthropic SDK appends '/v1/messages' to the base URL itself.
        # Operators carrying over the pre-collapse
        # INFERENCE_ANTHROPIC_BASE_URL=https://api.anthropic.com/v1 would
        # produce '.../v1/v1/messages' and 404 every intelligence call.
        # OpenAI-compatible base URLs do end in '/v1' (the SDK appends
        # 'chat/completions' to that), so this guard only fires for
        # INFERENCE_MODE=anthropic.
        if [[ "$INFERENCE_MODE" == "anthropic" && "${INFERENCE_BASE_URL%/}" == */v1 ]]; then
            echo "ERROR: INFERENCE_BASE_URL must not end with '/v1' when INFERENCE_MODE=anthropic." >&2
            echo "       The Anthropic SDK appends '/v1/messages' itself. Drop the trailing '/v1'" >&2
            echo "       (e.g. 'https://api.anthropic.com'), or leave the var empty for the SDK default." >&2
            exit 1
        fi
    fi
fi

# ----- EMBED -----
# Embed has no disabled mode: semantic / hybrid search is the headline
# retrieval feature and the indexer cannot run without an embedder
# either. ``EMBED_MODE=openai`` is the only valid value and is kept as
# a config knob for symmetry with the other layers.
EMBED_MODE="${EMBED_MODE:-openai}"
[[ "$EMBED_MODE" == "openai" ]] || {
    echo "ERROR: EMBED_MODE must be 'openai' (the only supported embed mode)." >&2
    exit 1
}

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

# Optional warmup deadline. ``EMBED_WARMUP_TIMEOUT_SECS`` bounds one
# successful first-call response (covering a host-side server's
# first-time model load). Must be >= 1 — the indexer's _float_env
# helper already falls back on smaller values, but rejecting them
# here keeps the .env contract aligned with the in-container check
# (no value silently overridden by the loader).
if [[ -n "$EMBED_WARMUP_TIMEOUT_SECS" ]]; then
    require_integer_min "EMBED_WARMUP_TIMEOUT_SECS" "$EMBED_WARMUP_TIMEOUT_SECS" 1
fi

# Optional per-call embed deadline used by the mcp-server query path.
# Must be >= 1 for the same reason as ``RERANK_TIMEOUT_SECS`` — bound
# a stalled call without rejecting routine sub-second failures.
if [[ -n "$EMBED_TIMEOUT_SECS" ]]; then
    require_integer_min "EMBED_TIMEOUT_SECS" "$EMBED_TIMEOUT_SECS" 1
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

# Optional rerank tuning knobs. Validate only when set so the
# defaults in mcp-server/src/main.py remain authoritative when the
# operator leaves the value blank. ``RERANK_CANDIDATES`` and
# ``RERANK_TOP_N`` must be >= 1 — zero or negative values would feed
# the rerank stage an empty candidate set or ask for an empty top-K.
# ``RERANK_TIMEOUT_SECS`` must be >= 1 to bound a stalled rerank
# call without rejecting routine sub-second failures.
if [[ -n "$RERANK_CANDIDATES" ]]; then
    require_integer_min "RERANK_CANDIDATES" "$RERANK_CANDIDATES" 1
fi
if [[ -n "$RERANK_TOP_N" ]]; then
    require_integer_min "RERANK_TOP_N" "$RERANK_TOP_N" 1
fi
if [[ -n "$RERANK_TIMEOUT_SECS" ]]; then
    require_integer_min "RERANK_TIMEOUT_SECS" "$RERANK_TIMEOUT_SECS" 1
fi

# Per-message parse byte cap. ``0`` disables the cap, so the
# minimum is 0 rather than 1. Validated only when the operator
# overrides the indexer/src/parser.py default.
if [[ -n "$INDEXER_PARSE_MAX_BYTES" ]]; then
    require_integer_min "INDEXER_PARSE_MAX_BYTES" "$INDEXER_PARSE_MAX_BYTES" 0
fi

# Indexing queue retry knobs. The Python loader (``queue.load_config_from_env``)
# clamps both to the documented defaults on out-of-range values, but the
# operator-facing contract is that .env never contains values the indexer
# would silently override. ``max_attempts <= 0`` dead-letters on first
# failure; ``base_backoff_seconds <= 0`` schedules immediate retry
# churn that burns the attempt budget in a tight loop. Reject both
# here so the operator sees the actual problem instead of a runtime
# warning buried in indexer logs.
if [[ -n "$INDEXER_MAX_ATTEMPTS" ]]; then
    require_integer_min "INDEXER_MAX_ATTEMPTS" "$INDEXER_MAX_ATTEMPTS" 1
fi
if [[ -n "$INDEXER_RETRY_BASE_SECONDS" ]]; then
    require_integer_min "INDEXER_RETRY_BASE_SECONDS" "$INDEXER_RETRY_BASE_SECONDS" 1
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

# Each active mode's secret file requirement depends on whether the
# mode targets an authenticated provider or can also point at an
# unauthenticated host-side server (LM Studio, vLLM, ``mlx_lm.server``).
#
# - ``INFERENCE_MODE=anthropic``: Anthropic-only, always authenticated.
#   Requires a non-empty key.
# - ``INFERENCE_MODE=openai``: may target a remote provider OR an
#   unauthenticated host server. The openai SDK requires a non-empty
#   ``api_key`` to construct, but ``InferenceClient`` (mcp-server) and
#   ``OpenAIEmbedder`` (indexer) supply ``"unauthenticated"`` as a
#   placeholder when the configured key is empty, so the operator can
#   leave the secret file empty for unauthenticated host servers.
# - ``EMBED_MODE=openai``: same dual case as ``INFERENCE_MODE=openai``.
#   ``EmbedClient`` supplies the SDK placeholder when empty.
# - ``RERANK_MODE=cohere``: Cohere-only, always authenticated.
#   Requires a non-empty key.
#
# Disabled inference / rerank layers (``*_MODE=none``) can leave the
# file empty; the file must still exist with mode 600 so the
# docker-compose ``secrets:`` reference resolves cleanly. Embed has
# no disabled mode, but its file may be empty for unauthenticated
# host-side servers (LM Studio, vLLM, etc.).
if [[ "$INFERENCE_MODE" == "anthropic" ]]; then
    require_nonempty_file "$INFERENCE_KEY_FILE" "Inference API key"
fi
if [[ "$RERANK_MODE" == "cohere" ]]; then
    require_nonempty_file "$RERANK_KEY_FILE" "Rerank API key"
fi

printf 'Environment validation passed.\n'
