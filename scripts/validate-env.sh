#!/bin/bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly ROOT_DIR
readonly ENV_FILE="${ROOT_DIR}/.env"
readonly BRIDGE_PASS_FILE="${ROOT_DIR}/.secrets/bridge_pass.txt"
readonly INFERENCE_KEY_FILE="${ROOT_DIR}/.secrets/inference_api_key.txt"
readonly EMBED_KEY_FILE="${ROOT_DIR}/.secrets/embed_api_key.txt"
readonly RERANK_KEY_FILE="${ROOT_DIR}/.secrets/rerank_api_key.txt"
readonly MCP_TOKEN_FILE="${ROOT_DIR}/.secrets/mcp_auth_token.txt"
readonly AUTHORITY_FILE="${ROOT_DIR}/config/authority.toml"

require_file() {
    local path="$1"
    local description="$2"

    [[ -f "$path" ]] || {
        printf 'ERROR: %s not found at %s.\n' "$description" "$path" >&2
        exit 1
    }
}

# The services strip surrounding whitespace from every secret they read,
# so a file holding only whitespace (a stray newline) counts as empty.
require_nonempty_file() {
    local path="$1"
    local description="$2"

    if [[ ! -f "$path" ]] || ! grep -q '[^[:space:]]' "$path"; then
        printf 'ERROR: %s is missing or empty at %s.\n' "$description" "$path" >&2
        exit 1
    fi
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

file_owner() {
    local path="$1"
    local owner

    if owner=$(stat -c '%u' "$path" 2>/dev/null); then
        printf '%s\n' "$owner"
        return 0
    fi
    if owner=$(stat -f '%u' "$path" 2>/dev/null); then
        printf '%s\n' "$owner"
        return 0
    fi
    printf 'ERROR: unable to read the owner of %s on this platform.\n' "$path" >&2
    return 1
}

readonly INDEXER_UID=1002

# On Linux with Docker Engine a bind mount keeps host ownership and the
# kernel checks the indexer's own UID, so a 600 file the operator owns
# cannot be read in the container (#526). Compose cannot change that:
# file-based secrets and configs are plain bind mounts that ignore
# `uid`/`gid`/`mode`. Access is granted on the host instead, with an
# ACL naming UID 1002 alone (a group would also admit whoever holds
# that GID on the host). `stat` then shows the ACL mask as group bits,
# so the mode reads 640 and the ACL itself is checked to be exactly
# that grant. macOS file sharing serves the file to the container's
# user, so there the plain 600 stands.
readonly INDEXER_ONLY_ACL=$'user::rw-\nuser:1002:r--\ngroup::---\nmask::r--\nother::---'

# Print the ACL on PATH, or fail: without it the grant cannot be checked.
acl_of() {
    local path="$1"

    command -v getfacl >/dev/null || {
        printf 'ERROR: getfacl is needed to check the ACL on %s; install the acl package.\n' "$path" >&2
        exit 1
    }
    getfacl --omit-header --numeric --absolute-names "$path" || {
        printf 'ERROR: unable to read the ACL on %s.\n' "$path" >&2
        exit 1
    }
}

# The indexer must also search config/, the mount root: a directory
# without the other-search bit (say, from a 077 umask) needs a search
# ACL for UID 1002 too.
require_indexer_can_search_on_linux() {
    local dir="$1"
    local mode acl

    # POSIX ACL order: the owner entry, then a named-user entry for the
    # UID (limited by the mask), and only then the other bits.
    mode="$(file_mode "$dir")"
    if [[ "$(file_owner "$dir")" == "$INDEXER_UID" ]]; then
        (((8#$mode & 8#100) != 0)) && return 0
    else
        acl="$(acl_of "$dir")"
        if grep -Eq "^user:${INDEXER_UID}:" <<<"$acl"; then
            grep -Eq "^user:${INDEXER_UID}:..x" <<<"$acl" && grep -Eq '^mask::..x' <<<"$acl" &&
                return 0
        elif (((8#$mode & 8#001) != 0)); then
            return 0
        fi
    fi
    printf 'ERROR: %s is not searchable by the indexer (UID %s). Run:\n  setfacl -m u:%s:x %s\n' \
        "$dir" "$INDEXER_UID" "$INDEXER_UID" "$(printf '%q' "$dir")" >&2
    exit 1
}

require_indexer_readable_on_linux() {
    local path="$1"
    local mode acl account quoted fix

    quoted="$(printf '%q' "$path")"
    fix="setfacl -b $quoted && chmod 600 $quoted && setfacl -m u:${INDEXER_UID}:r $quoted"
    mode="$(file_mode "$path")"
    if [[ "$mode" == "600" ]]; then
        [[ "$(file_owner "$path")" == "$INDEXER_UID" ]] || {
            printf 'ERROR: %s is not readable by the indexer (UID %s): on Linux the container sees the host owner and mode. Grant that UID alone read access (needs the acl package):\n  %s\n' \
                "$path" "$INDEXER_UID" "$fix" >&2
            exit 1
        }
    elif [[ "$mode" == "640" ]]; then
        acl="$(acl_of "$path")"
        [[ "$acl" == "$INDEXER_ONLY_ACL" ]] || {
            printf 'ERROR: %s has mode 640; its ACL must grant read to UID %s and no one else. Run:\n  %s\n' \
                "$path" "$INDEXER_UID" "$fix" >&2
            exit 1
        }
    else
        printf 'ERROR: %s must have mode 600, with an ACL granting UID %s read, found %s. Run:\n  %s\n' \
            "$path" "$INDEXER_UID" "$mode" "$fix" >&2
        exit 1
    fi
    require_indexer_can_search_on_linux "$(dirname "$path")"
    # Either grant also covers host UID 1002, so name any account holding
    # it, unless that account is the operator's own.
    if [[ "$(id -u)" != "$INDEXER_UID" ]] && command -v getent >/dev/null &&
        account="$(getent passwd "$INDEXER_UID")"; then
        printf 'WARNING: host account %s has UID %s and can read %s if it can reach the directory; keep the checkout under a directory that account cannot enter.\n' \
            "${account%%:*}" "$INDEXER_UID" "$path" >&2
    fi
}

# The optional source-authority rules file holds real addresses and
# domains, so it is held to the secret files' 600 (on Linux, 600 plus
# an ACL for the indexer alone; see above). A symlink is rejected:
# Compose mounts config/ as a directory, so a link whose target the
# container cannot reach would pass here and stop the indexer at
# startup; so is anything else that is not a regular file, which the
# indexer refuses to open.
require_private_optional_file() {
    local path="$1"

    if [[ -L "$path" ]]; then
        printf 'ERROR: %s must be a regular file, not a symlink.\n' "$path" >&2
        exit 1
    fi
    [[ -e "$path" ]] || return 0
    [[ -f "$path" ]] || {
        printf 'ERROR: %s must be a regular file.\n' "$path" >&2
        exit 1
    }
    if [[ "$(uname -s)" == "Linux" ]]; then
        require_indexer_readable_on_linux "$path"
    else
        require_mode_600 "$path"
    fi
}

# API keys are wired as Docker secrets (see ``secrets:`` in
# docker-compose.yml). Putting them in ``.env`` would surface them in
# ``docker inspect`` and is therefore disallowed.
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

# Validate as integer (>= minimum), for the knobs the services read
# with ``int()``.
require_integer_min() {
    local name="$1"
    local value="$2"
    local minimum="$3"

    require_integer "$name" "$value"
    # Force base 10: a zero-padded value such as 08 would otherwise be
    # read as octal, while the Python loaders parse it as decimal.
    (( 10#$value >= minimum )) || {
        printf 'ERROR: %s must be >= %s, found %s.\n' "$name" "$minimum" "$value" >&2
        exit 1
    }
}

# Validate a decimal number (>= an integer minimum), for the timeouts
# the services read with ``float()`` (``_float_env`` in
# mcp-server/src/main.py and indexer/src/embedder.py). Plain decimals
# only; exponent forms such as 1e3 are rejected.
require_number_min() {
    local name="$1"
    local value="$2"
    local minimum="$3"
    local whole

    [[ "$value" =~ ^([0-9]+(\.[0-9]*)?|\.[0-9]+)$ ]] || {
        printf 'ERROR: %s must be a number, found %s.\n' "$name" "$value" >&2
        exit 1
    }
    # With an integer minimum, comparing the whole part is exact.
    whole="${value%%.*}"
    (( 10#${whole:-0} >= minimum )) || {
        printf 'ERROR: %s must be >= %s, found %s.\n' "$name" "$minimum" "$value" >&2
        exit 1
    }
}

# Boolean vocabulary shared with the indexer's ``_bool_env`` and the
# reconciler loader (case-insensitive); anything else stops the indexer
# at startup, so reject it here first.
require_bool() {
    local name="$1"
    local value="$2"
    local lowered

    lowered="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
    [[ "$lowered" =~ ^(1|true|yes|on|0|false|no|off)$ ]] || {
        printf 'ERROR: %s must be true or false (also 1/0, yes/no, on/off), found %s.\n' "$name" "$value" >&2
        exit 1
    }
}

# Reject URLs that embed a ``user:pass@host`` userinfo authority.
# ``*_BASE_URL`` values flow into startup log lines naming the resolved
# wire endpoint, so embedded credentials would leak to container logs
# / journald. The project's credential model puts every secret in a
# Docker-secrets file (``.secrets/<layer>_api_key.txt``); a URL with
# userinfo means the operator is trying to authenticate out-of-band,
# which both bypasses the contract and exposes the credential.
reject_url_userinfo() {
    local name="$1"
    local value="$2"

    # Use an explicit ``if`` rather than ``[[ ]] && { ... }`` so the
    # not-matched path returns 0. Under ``set -e`` a function whose
    # last command is ``[[ no-match ]] && {...}`` propagates the test
    # failure (exit 1) and the caller aborts on every clean URL.
    if [[ "$value" =~ ^https?://[^/]*@ ]]; then
        printf 'ERROR: %s must not embed credentials (user:pass@host). Put the API key in the matching .secrets/<layer>_api_key.txt file instead.\n' "$name" >&2
        exit 1
    fi
}

# Print VALUE without leading or trailing whitespace.
trim() {
    local value="$1"

    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s\n' "$value"
}

# Read a single KEY=VALUE from .env without shell-sourcing.
# Shell-sourcing would evaluate command substitutions in values, so a
# malformed or hostile .env line could execute arbitrary commands from the
# operator's host. Parsing known keys avoids that entire class of issue.
#
# Semantics:
#   - last assignment wins (matches `source` behavior)
#   - comment (#) and blank lines are ignored
#   - whitespace around the value is dropped, as Compose does
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

    value="$(trim "${raw#*=}")"
    # strip a single pair of matching surrounding quotes, if present
    if [[ "$value" =~ ^\"(.*)\"$ ]]; then
        value="${BASH_REMATCH[1]}"
    elif [[ "$value" =~ ^\'(.*)\'$ ]]; then
        value="${BASH_REMATCH[1]}"
    fi
    printf '%s\n' "$value"
}

# The value Compose interpolates for KEY: a variable exported in the
# calling shell wins over .env, even when it is empty, as in Compose.
# Callers apply the ``${KEY:-default}`` fallback from docker-compose.yml
# themselves, which Compose uses for an empty value as well as an unset
# one.
env_value() {
    local key="$1"

    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || {
        printf 'ERROR: invalid environment key: %s\n' "$key" >&2
        return 2
    }
    if [[ -n "${!key+set}" ]]; then
        printf '%s\n' "${!key}"
    else
        get_env_value "$key"
    fi
}

# env_value with surrounding whitespace removed, for the values the
# Python services read with ``.strip()``: every number, boolean and mode
# below. SYNC_INTERVAL and SYNC_DEADLINE_SECONDS (mbsync's shell),
# MCP_PORT (also Compose's port mapping), URLs and model names are
# passed on unstripped, so they are read with env_value.
env_value_stripped() {
    local value

    value="$(env_value "$1")" || return 2
    trim "$value"
}

# The loaders read a mode as ``.strip().lower()`` of the value Compose
# passes, after Compose has applied the default to an empty one.
normalize_mode() {
    trim "$1" | tr '[:upper:]' '[:lower:]'
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
# Every /mcp request must carry this bearer token, and mcp-server fails
# startup without it (PLAN.md Resolved decisions 13). The file is
# required non-empty whatever the other settings are.
if [[ ! -f "$MCP_TOKEN_FILE" ]] || ! grep -q '[^[:space:]]' "$MCP_TOKEN_FILE"; then
    printf 'ERROR: MCP bearer token is missing or empty at %s. Create it with: (umask 077; openssl rand -hex 32 > .secrets/mcp_auth_token.txt)\n' \
        "$MCP_TOKEN_FILE" >&2
    exit 1
fi
# The token must be one scripts/mcp-auth-headers.sh can send and
# mcp-server accepts at startup (#589): at least 32 RFC 6750 b64token
# characters once surrounding whitespace is stripped, as both of them
# strip it. ``make init-secrets`` writes 64 hex characters. LC_ALL=C
# keeps the ranges ASCII. Command substitution drops NUL bytes, which
# mcp-server would keep and reject, so they are counted first. The
# message never includes the token.
check_mcp_token() {
    local LC_ALL=C token="" nul_bytes
    nul_bytes="$(tr -cd '\000' <"$MCP_TOKEN_FILE" | wc -c)"
    if ((nul_bytes == 0)); then
        token="$(<"$MCP_TOKEN_FILE")"
        token="${token#"${token%%[![:space:]]*}"}"
        token="${token%"${token##*[![:space:]]}"}"
    fi
    if ((${#token} < 32)) || [[ ! "$token" =~ ^[A-Za-z0-9._~+/-]+=*$ ]]; then
        printf 'ERROR: MCP bearer token in %s must be at least 32 characters from A-Z a-z 0-9 - . _ ~ + / with optional trailing =. Regenerate it with: (umask 077; openssl rand -hex 32 > .secrets/mcp_auth_token.txt)\n' \
            "$MCP_TOKEN_FILE" >&2
        exit 1
    fi
}
check_mcp_token

# Mode selects the wire protocol; BASE_URL, MODEL and API_KEY configure
# that mode, and ``none`` disables a layer. There is no inter-mode
# fallback: choosing a mode without its required vars is a startup error.
#
# *_API_KEY values must never appear in .env: they are wired as Docker
# secrets in docker-compose.yml. A key pasted into .env would leak into
# ``docker inspect`` output and silently mask the secret-file value,
# since both services read the secret file first and only fall back to
# the env var.
reject_secret_in_env "INFERENCE_API_KEY" "$INFERENCE_KEY_FILE"
reject_secret_in_env "EMBED_API_KEY" "$EMBED_KEY_FILE"
reject_secret_in_env "RERANK_API_KEY" "$RERANK_KEY_FILE"
# MCP_AUTH_TOKEN is mcp-server's fallback for runs outside a container
# only; under Compose the token is always the mcp_auth_token secret.
reject_secret_in_env "MCP_AUTH_TOKEN" "$MCP_TOKEN_FILE"

# Values resolve as Compose interpolates them: a variable exported in the
# calling shell, else .env, else the ``${VAR:-default}`` fallback that
# docker-compose.yml gives it (applied below, after each read). So a
# key Compose defaults (BRIDGE_VERSION, SYNC_INTERVAL, MCP_PORT,
# INFERENCE_MODEL) may be left out of .env, and ``SYNC_INTERVAL=0 make
# up`` is checked as 0. BRIDGE_VERSION has nothing to check beyond its
# default. INFERENCE_MODEL's default is an Anthropic model, so it only
# satisfies the required-model contract in anthropic mode (see below).
BRIDGE_USER="$(env_value BRIDGE_USER)"
INFERENCE_MODE="$(env_value INFERENCE_MODE)"
INFERENCE_BASE_URL="$(env_value INFERENCE_BASE_URL)"
INFERENCE_MODEL="$(env_value INFERENCE_MODEL)"
INFERENCE_TIMEOUT_SECS="$(env_value_stripped INFERENCE_TIMEOUT_SECS)"
INFERENCE_MAX_TOKENS="$(env_value_stripped INFERENCE_MAX_TOKENS)"
INFERENCE_CONTEXT_TOKENS="$(env_value_stripped INFERENCE_CONTEXT_TOKENS)"
EMBED_MODE="$(env_value EMBED_MODE)"
EMBED_BASE_URL="$(env_value EMBED_BASE_URL)"
EMBED_MODEL="$(env_value EMBED_MODEL)"
EMBED_TIMEOUT_SECS="$(env_value_stripped EMBED_TIMEOUT_SECS)"
EMBED_WARMUP_TIMEOUT_SECS="$(env_value_stripped EMBED_WARMUP_TIMEOUT_SECS)"
RERANK_MODE="$(env_value RERANK_MODE)"
RERANK_BASE_URL="$(env_value RERANK_BASE_URL)"
RERANK_MODEL="$(env_value RERANK_MODEL)"
RERANK_CANDIDATES="$(env_value_stripped RERANK_CANDIDATES)"
RERANK_TIMEOUT_SECS="$(env_value_stripped RERANK_TIMEOUT_SECS)"
INDEXER_PARSE_MAX_BYTES="$(env_value_stripped INDEXER_PARSE_MAX_BYTES)"
INDEXER_MAX_ATTEMPTS="$(env_value_stripped INDEXER_MAX_ATTEMPTS)"
INDEXER_RETRY_BASE_SECONDS="$(env_value_stripped INDEXER_RETRY_BASE_SECONDS)"
INDEXER_MESSAGE_TIMEOUT_SECONDS="$(env_value_stripped INDEXER_MESSAGE_TIMEOUT_SECONDS)"
SYNC_INTERVAL="$(env_value SYNC_INTERVAL)"
SYNC_INTERVAL="${SYNC_INTERVAL:-60}"
SYNC_DEADLINE_SECONDS="$(env_value SYNC_DEADLINE_SECONDS)"
SYNC_DEADLINE_SECONDS="${SYNC_DEADLINE_SECONDS:-86400}"
MCP_PORT="$(env_value MCP_PORT)"
MCP_PORT="${MCP_PORT:-3000}"
MCP_TRANSPORT="$(env_value MCP_TRANSPORT)"
MCP_SESSION_IDLE_TIMEOUT_SECS="$(env_value_stripped MCP_SESSION_IDLE_TIMEOUT_SECS)"
MCP_EXPERIMENTAL_TOOLS="$(env_value_stripped MCP_EXPERIMENTAL_TOOLS)"

[[ -n "$BRIDGE_USER" && "$BRIDGE_USER" != "your@proton.me" ]] || {
    echo "ERROR: BRIDGE_USER in .env must be set to the Bridge username from 'bridge --cli info'." >&2
    exit 1
}

# Optional (docker-compose.yml carries the default pin). When set, it must
# be a full commit SHA: the Bridge build refuses a tag that resolves to
# anything else, so a typo here would only surface deep inside the build.
BRIDGE_COMMIT="$(env_value BRIDGE_COMMIT)"
[[ -z "$BRIDGE_COMMIT" || "$BRIDGE_COMMIT" =~ ^[0-9a-f]{40}$ ]] || {
    echo "ERROR: BRIDGE_COMMIT in .env must be a full 40-character lowercase commit SHA." >&2
    exit 1
}

# ----- INFERENCE -----
INFERENCE_MODE="$(normalize_mode "${INFERENCE_MODE:-anthropic}")"
[[ "$INFERENCE_MODE" =~ ^(openai|anthropic|none)$ ]] || {
    echo "ERROR: INFERENCE_MODE must be one of: anthropic, openai, none." >&2
    exit 1
}

if [[ "$INFERENCE_MODE" != "none" ]]; then
    # An empty INFERENCE_MODEL becomes Compose's ``claude-sonnet-4-6``,
    # which an OpenAI-compatible endpoint does not serve, so openai mode
    # must name its model.
    [[ -n "$INFERENCE_MODEL" || "$INFERENCE_MODE" == "anthropic" ]] || {
        echo "ERROR: INFERENCE_MODEL must be set when INFERENCE_MODE=$INFERENCE_MODE." >&2
        exit 1
    }
    # ``INFERENCE_BASE_URL`` may be empty for any enabled mode. Empty
    # means "use the SDK default" — Anthropic Messages API for
    # ``anthropic`` mode (``api.anthropic.com``) and OpenAI proper for
    # ``openai`` mode (``api.openai.com/v1``). The required
    # ``INFERENCE_API_KEY`` (checked below) is the explicit-intent
    # signal that makes empty-URL unambiguous — a typo can't produce a
    # real bearer credential.
    # Optional inference tuning knobs. Validate only when set so the
    # defaults in mcp-server/src/main.py remain authoritative when the
    # operator leaves the value blank. ``INFERENCE_TIMEOUT_SECS`` must
    # be >= 1 to bound a stalled inference call without rejecting
    # routine sub-second failures. ``INFERENCE_MAX_TOKENS`` must be
    # >= 1 — zero or negative would request an empty completion.
    if [[ -n "$INFERENCE_TIMEOUT_SECS" ]]; then
        require_number_min "INFERENCE_TIMEOUT_SECS" "$INFERENCE_TIMEOUT_SECS" 1
    fi
    if [[ -n "$INFERENCE_MAX_TOKENS" ]]; then
        require_integer_min "INFERENCE_MAX_TOKENS" "$INFERENCE_MAX_TOKENS" 1
    fi
    # ``INFERENCE_CONTEXT_TOKENS`` must leave at least 1088 tokens (a
    # 1024-token prompt plus the 64-token chat-template reserve) after
    # the reply; mirrors ``PromptBudget`` in mcp-server/src/lib/inference.py.
    # Both sides are resolved to the Compose defaults first, so a
    # one-sided override (a large INFERENCE_MAX_TOKENS alone) is caught.
    if [[ -n "$INFERENCE_CONTEXT_TOKENS" ]]; then
        require_integer_min "INFERENCE_CONTEXT_TOKENS" "$INFERENCE_CONTEXT_TOKENS" 1
    fi
    require_integer_min "INFERENCE_CONTEXT_TOKENS (32768 when unset)" \
        "${INFERENCE_CONTEXT_TOKENS:-32768}" "$(( 10#${INFERENCE_MAX_TOKENS:-1024} + 1088 ))"
    if [[ -n "$INFERENCE_BASE_URL" ]]; then
        [[ "$INFERENCE_BASE_URL" =~ ^https?:// ]] || {
            echo "ERROR: INFERENCE_BASE_URL must start with http:// or https://." >&2
            exit 1
        }
        reject_url_userinfo "INFERENCE_BASE_URL" "$INFERENCE_BASE_URL"
        # The Anthropic SDK appends '/v1/messages' to the base URL itself,
        # so a base URL ending in '/v1' would produce '.../v1/v1/messages'
        # and 404 every intelligence call.
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
EMBED_MODE="$(normalize_mode "${EMBED_MODE:-openai}")"
[[ "$EMBED_MODE" == "openai" ]] || {
    echo "ERROR: EMBED_MODE must be 'openai' (the only supported embed mode)." >&2
    exit 1
}

# ``EMBED_BASE_URL`` may be empty: an empty value means "use the SDK
# default" (OpenAI proper, via the openai SDK's documented fallback).
# Symmetric with the inference layer. The required ``EMBED_API_KEY``
# (checked below) is the explicit-intent signal that makes empty-URL
# unambiguous. ``EMBED_MODEL`` is always required because no SDK has a
# default model — empty model always fails at request time.
if [[ -n "$EMBED_BASE_URL" ]]; then
    [[ "$EMBED_BASE_URL" =~ ^https?:// ]] || {
        echo "ERROR: EMBED_BASE_URL must start with http:// or https://." >&2
        exit 1
    }
    reject_url_userinfo "EMBED_BASE_URL" "$EMBED_BASE_URL"
fi
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
    require_number_min "EMBED_WARMUP_TIMEOUT_SECS" "$EMBED_WARMUP_TIMEOUT_SECS" 1
fi

# Optional per-call embed deadline used by the mcp-server query path.
# Must be >= 1 for the same reason as ``RERANK_TIMEOUT_SECS`` — bound
# a stalled call without rejecting routine sub-second failures.
if [[ -n "$EMBED_TIMEOUT_SECS" ]]; then
    require_number_min "EMBED_TIMEOUT_SECS" "$EMBED_TIMEOUT_SECS" 1
fi

# ----- RERANK -----
RERANK_MODE="$(normalize_mode "${RERANK_MODE:-none}")"
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
        reject_url_userinfo "RERANK_BASE_URL" "$RERANK_BASE_URL"
    fi
fi

# Optional rerank tuning knobs. Validate only when set so the
# defaults in mcp-server/src/main.py remain authoritative when the
# operator leaves the value blank. ``RERANK_CANDIDATES`` must be >= 1
# — zero or negative values would feed the rerank stage an empty
# candidate set.
# ``RERANK_TIMEOUT_SECS`` must be >= 1 to bound a stalled rerank
# call without rejecting routine sub-second failures.
if [[ -n "$RERANK_CANDIDATES" ]]; then
    require_integer_min "RERANK_CANDIDATES" "$RERANK_CANDIDATES" 1
fi
if [[ -n "$RERANK_TIMEOUT_SECS" ]]; then
    require_number_min "RERANK_TIMEOUT_SECS" "$RERANK_TIMEOUT_SECS" 1
fi

# Per-message parse byte cap. ``0`` disables the cap, so the
# minimum is 0 rather than 1. Validated only when the operator
# overrides the indexer/src/parser.py default.
if [[ -n "$INDEXER_PARSE_MAX_BYTES" ]]; then
    require_integer_min "INDEXER_PARSE_MAX_BYTES" "$INDEXER_PARSE_MAX_BYTES" 0
fi

# Indexing queue retry knobs. The Python loader (``queue.load_config_from_env``)
# stops the indexer at startup on out-of-range values; rejecting them
# here surfaces the problem before any container starts.
# ``max_attempts <= 0`` dead-letters on first failure;
# ``base_backoff_seconds <= 0`` schedules immediate retry churn that
# burns the attempt budget in a tight loop.
if [[ -n "$INDEXER_MAX_ATTEMPTS" ]]; then
    require_integer_min "INDEXER_MAX_ATTEMPTS" "$INDEXER_MAX_ATTEMPTS" 1
fi
if [[ -n "$INDEXER_RETRY_BASE_SECONDS" ]]; then
    require_integer_min "INDEXER_RETRY_BASE_SECONDS" "$INDEXER_RETRY_BASE_SECONDS" 1
fi
if [[ -n "$INDEXER_MESSAGE_TIMEOUT_SECONDS" ]]; then
    require_integer_min "INDEXER_MESSAGE_TIMEOUT_SECONDS" "$INDEXER_MESSAGE_TIMEOUT_SECONDS" 0
fi

# Remaining indexer integer knobs, as NAME:MINIMUM. The minimums match
# the ``_int_env`` / reconciler ``_int`` calls in indexer/src/main.py
# and indexer/src/reconciler.py, which stop the indexer at startup on a
# non-integer or a value below them (#481). Validated only when set so
# the code defaults stay authoritative otherwise.
for spec in \
    EMBED_BATCH_SIZE:1 \
    INITIAL_INDEX_BATCH_SIZE:1 \
    INDEXER_STEADY_STATE_BATCH_SIZE:1 \
    INDEXER_WAL_CHECKPOINT_INTERVAL_SECS:60 \
    INDEXER_RECOVERY_SWEEP_INTERVAL_SECS:60 \
    INDEXER_CHUNK_TARGET_TOKENS:1 \
    INDEXER_CHUNK_MAX_TOKENS:1 \
    INDEXER_CHUNK_OVERLAP_TOKENS:0 \
    INDEXER_ATTACHMENT_MAX_BYTES:1 \
    INDEXER_OCR_MAX_PAGES:1 \
    INDEXER_OCR_TIMEOUT_SECONDS:0 \
    INDEXER_PDF_MAX_DIGITAL_PAGES:0 \
    INDEXER_ATTACHMENT_MAX_EXTRACTED_CHARS:0 \
    INDEXER_DELETION_GRACE_DAYS:0 \
    INDEXER_DELETION_SWEEP_INTERVAL_SECS:60; do
    name="${spec%%:*}"
    value="$(env_value_stripped "$name")"
    if [[ -n "$value" ]]; then
        require_integer_min "$name" "$value" "${spec##*:}"
    fi
done

# The chunk budgets must also fit together: chunker.chunk_message
# requires target <= max and overlap < target, and the indexer checks
# this at startup (#507). An omitted side takes its docker-compose.yml
# default. Each value already passed require_integer_min above.
chunk_target="$(env_value_stripped INDEXER_CHUNK_TARGET_TOKENS)"
chunk_target="${chunk_target:-1000}"
chunk_max="$(env_value_stripped INDEXER_CHUNK_MAX_TOKENS)"
chunk_max="${chunk_max:-1500}"
chunk_overlap="$(env_value_stripped INDEXER_CHUNK_OVERLAP_TOKENS)"
chunk_overlap="${chunk_overlap:-150}"
(( 10#$chunk_target <= 10#$chunk_max )) || {
    printf 'ERROR: INDEXER_CHUNK_TARGET_TOKENS (%s) must be <= INDEXER_CHUNK_MAX_TOKENS (%s).\n' "$chunk_target" "$chunk_max" >&2
    exit 1
}
(( 10#$chunk_overlap < 10#$chunk_target )) || {
    printf 'ERROR: INDEXER_CHUNK_OVERLAP_TOKENS (%s) must be < INDEXER_CHUNK_TARGET_TOKENS (%s).\n' "$chunk_overlap" "$chunk_target" >&2
    exit 1
}

# Indexer booleans: an unrecognised value stops the indexer at startup
# rather than reading as false (#481). INDEXER_DELETION_ENABLED (the
# retention mode) uses the same vocabulary.
for name in \
    INDEXER_DELETION_ENABLED \
    INDEXER_ATTACHMENT_EXTRACTION_ENABLED \
    INDEXER_OCR_ENABLED \
    INDEXER_DELETION_FORCE \
    INDEXER_UNLINK_ON_REAP; do
    value="$(env_value_stripped "$name")"
    if [[ -n "$value" ]]; then
        require_bool "$name" "$value"
    fi
done

# Mass-delete brake: a decimal fraction in [0, 1]. The reconciler
# rejects anything else, including NaN and infinity, at startup.
INDEXER_DELETION_MAX_BATCH_PCT="$(env_value_stripped INDEXER_DELETION_MAX_BATCH_PCT)"
if [[ -n "$INDEXER_DELETION_MAX_BATCH_PCT" ]]; then
    [[ "$INDEXER_DELETION_MAX_BATCH_PCT" =~ ^(0*\.[0-9]+|0+(\.[0-9]*)?|0*1(\.0*)?)$ ]] || {
        printf 'ERROR: INDEXER_DELETION_MAX_BATCH_PCT must be a decimal between 0 and 1, found %s.\n' "$INDEXER_DELETION_MAX_BATCH_PCT" >&2
        exit 1
    }
fi

# Same pattern as mbsync/entrypoint.sh, which reads the value unstripped.
[[ "$SYNC_INTERVAL" =~ ^[1-9][0-9]*$ ]] || {
    echo "ERROR: SYNC_INTERVAL must be a positive integer without leading zeros." >&2
    exit 1
}

# Same pattern as mbsync/entrypoint.sh and healthcheck.sh, which read the
# value unstripped; nine digits at most so their arithmetic cannot
# overflow.
[[ "$SYNC_DEADLINE_SECONDS" =~ ^[1-9][0-9]{0,8}$ ]] || {
    echo "ERROR: SYNC_DEADLINE_SECONDS must be a positive integer of seconds without leading zeros, at most 999999999." >&2
    exit 1
}

require_integer "MCP_PORT" "$MCP_PORT"
# Base 10, as Compose and the mcp-server loader read a zero-padded port.
(( 10#$MCP_PORT >= 1 && 10#$MCP_PORT <= 65535 )) || {
    echo "ERROR: MCP_PORT must be between 1 and 65535." >&2
    exit 1
}

# Streamable HTTP at /mcp is the only transport (#498). MCP_TRANSPORT is
# still read so a value left from a release that served the legacy SSE
# transport fails with migration steps, as mcp-server/src/main.py does.
MCP_TRANSPORT="$(normalize_mode "$MCP_TRANSPORT")"
if [[ "$MCP_TRANSPORT" == "sse" || "$MCP_TRANSPORT" == "dual" ]]; then
    echo "ERROR: MCP_TRANSPORT=$MCP_TRANSPORT was removed: Streamable HTTP is the only MCP transport." >&2
    echo "       Remove MCP_TRANSPORT from .env and run 'unset MCP_TRANSPORT' in any shell that" >&2
    echo "       exports it (an exported value wins over .env), or set it to streamable-http." >&2
    echo "       Change MCP client URLs from http://localhost:<MCP_PORT>/sse to" >&2
    echo "       http://127.0.0.1:<MCP_PORT>/mcp." >&2
    exit 1
fi
[[ -z "$MCP_TRANSPORT" || "$MCP_TRANSPORT" == "streamable-http" ]] || {
    echo "ERROR: MCP_TRANSPORT must be 'streamable-http' or unset." >&2
    exit 1
}

# Optional Streamable HTTP session idle timeout. Must be >= 1 so a
# session a client abandons is ended; validated only when set so the
# mcp-server/src/main.py default (1800) stays authoritative otherwise.
if [[ -n "$MCP_SESSION_IDLE_TIMEOUT_SECS" ]]; then
    require_number_min "MCP_SESSION_IDLE_TIMEOUT_SECS" "$MCP_SESSION_IDLE_TIMEOUT_SECS" 1
fi

# Experimental tools flag; mcp-server/src/main.py accepts the same
# values case-insensitively and fails startup on anything else.
MCP_EXPERIMENTAL_TOOLS_LC="$(printf '%s' "$MCP_EXPERIMENTAL_TOOLS" | tr '[:upper:]' '[:lower:]')"
[[ "$MCP_EXPERIMENTAL_TOOLS_LC" =~ ^(true|false)?$ ]] || {
    echo "ERROR: MCP_EXPERIMENTAL_TOOLS must be 'true' or 'false'." >&2
    exit 1
}

require_nonempty_file "$BRIDGE_PASS_FILE" "Bridge password secret"
require_mode_600 "$BRIDGE_PASS_FILE"
require_mode_600 "$INFERENCE_KEY_FILE"
require_mode_600 "$EMBED_KEY_FILE"
require_mode_600 "$RERANK_KEY_FILE"
require_mode_600 "$MCP_TOKEN_FILE"
require_private_optional_file "$AUTHORITY_FILE"

# Every enabled layer requires a non-empty API key — uniform rule
# across the three operator-supplied layers. Operators pointing at an
# unauthenticated host-side server (LM Studio, vLLM, ``mlx_lm.server``,
# TEI) supply any placeholder string (e.g. ``unauthenticated``); the
# compat server ignores the bearer header, but the no-fallback startup
# contract requires the value to be non-empty so a missing key
# surfaces here rather than at first tool call.
#
# - ``INFERENCE_MODE=anthropic|openai``: requires a non-empty
#   inference key.
# - ``EMBED_MODE=openai``: always enabled (embed has no ``none`` mode);
#   requires a non-empty embed key.
# - ``RERANK_MODE=cohere``: requires a non-empty rerank key.
#
# Disabled inference / rerank layers (``*_MODE=none``) can leave the
# file empty; the file must still exist with mode 600 so the
# docker-compose ``secrets:`` reference resolves cleanly.
if [[ "$INFERENCE_MODE" != "none" ]]; then
    require_nonempty_file "$INFERENCE_KEY_FILE" "Inference API key"
fi
require_nonempty_file "$EMBED_KEY_FILE" "Embed API key"
if [[ "$RERANK_MODE" == "cohere" ]]; then
    require_nonempty_file "$RERANK_KEY_FILE" "Rerank API key"
fi

printf 'Environment validation passed.\n'
