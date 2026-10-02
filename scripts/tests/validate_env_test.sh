#!/bin/bash
set -Eeuo pipefail

# Tests for scripts/validate-env.sh, the preflight `make up` runs.
#
# Each case copies the real script into a fresh temporary root, writes a
# synthetic .env and .secrets/ there, and runs the script with a clean
# environment (plus any variables the case exports), so the developer's
# own shell and .env never leak in. Every value is a placeholder.
#
# Run: bash scripts/tests/validate_env_test.sh

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/validate-env.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

# A .env that passes, with the keys Compose defaults left out.
readonly BASE_ENV='BRIDGE_USER=placeholder@example.invalid
EMBED_MODEL=placeholder-embed-model'

# Create a fresh root holding the script, BASE_ENV plus any extra lines
# (later assignments win), and secret files with placeholder contents.
setup() {
    ROOT="$(mktemp -d "$WORK/root.XXXXXX")"
    mkdir -p "$ROOT/scripts" "$ROOT/.secrets"
    cp "$SCRIPT" "$ROOT/scripts/validate-env.sh"
    printf '%s\n' "$BASE_ENV" "$@" >"$ROOT/.env"
    printf 'placeholder-bridge-pass\n' >"$ROOT/.secrets/bridge_pass.txt"
    printf 'placeholder\n' >"$ROOT/.secrets/inference_api_key.txt"
    printf 'placeholder\n' >"$ROOT/.secrets/embed_api_key.txt"
    : >"$ROOT/.secrets/rerank_api_key.txt"
    chmod 600 "$ROOT"/.secrets/*.txt
}

# Run the validator with a clean environment plus the given NAME=VALUE
# exports; keep its output and status.
run_validator() {
    env -i PATH="$PATH" "$@" "$BASH" "$ROOT/scripts/validate-env.sh" \
        >"$WORK/output" 2>&1 && STATUS=0 || STATUS=$?
    cat "$WORK/output"
}

passes() {
    run_validator "$@"
    [[ "$STATUS" -eq 0 ]] || return 1
    grep -F 'Environment validation passed.' "$WORK/output" >/dev/null || return 1
}

# fails_with MESSAGE [NAME=VALUE...]
fails_with() {
    local message="$1"
    shift
    run_validator "$@"
    [[ "$STATUS" -eq 1 ]] || return 1
    grep -F -- "$message" "$WORK/output" >/dev/null || return 1
}

# Runs each case in a subshell with errexit on, outside any condition.
check() {
    local description="$1" status
    shift
    set +e
    (
        set -e
        "$@"
    ) >"$WORK/case-output" 2>&1
    status=$?
    set -e
    if ((status == 0)); then
        printf 'ok   %s\n' "$description"
    else
        printf 'FAIL %s\n' "$description"
        sed 's/^/     /' "$WORK/case-output"
        FAILURES=$((FAILURES + 1))
    fi
}

# --- Compose defaults (#482) ----------------------------------------------

keys_with_compose_defaults_may_be_omitted() {
    setup
    passes
}

empty_keys_with_compose_defaults_use_the_default() {
    setup 'BRIDGE_VERSION=' 'SYNC_INTERVAL=' 'MCP_PORT=' 'INFERENCE_MODEL='
    passes
}

explicit_values_still_pass() {
    setup 'BRIDGE_VERSION=v3.27.0' 'SYNC_INTERVAL=60' 'MCP_PORT=3000' \
        'INFERENCE_MODEL=placeholder-model'
    passes
}

# --- Shell exports win over .env, as in Compose (#482) --------------------

exported_invalid_value_overrides_valid_env() {
    setup 'SYNC_INTERVAL=60'
    fails_with 'SYNC_INTERVAL' SYNC_INTERVAL=0
}

exported_valid_value_overrides_invalid_env() {
    setup 'MCP_PORT=70000'
    passes MCP_PORT=3000
}

exported_empty_value_takes_the_compose_default() {
    setup 'SYNC_INTERVAL=0'
    passes SYNC_INTERVAL=
}

exported_mode_overrides_env() {
    setup 'INFERENCE_MODE=none'
    : >"$ROOT/.secrets/inference_api_key.txt"
    fails_with 'Inference API key' INFERENCE_MODE=anthropic
}

# --- Secret files (#482) ---------------------------------------------------

whitespace_only_embed_key_fails() {
    setup
    printf ' \t\n\n' >"$ROOT/.secrets/embed_api_key.txt"
    fails_with 'Embed API key'
}

newline_only_inference_key_fails() {
    setup
    printf '\n' >"$ROOT/.secrets/inference_api_key.txt"
    fails_with 'Inference API key'
}

whitespace_only_bridge_pass_fails() {
    setup
    printf '  \n' >"$ROOT/.secrets/bridge_pass.txt"
    fails_with 'Bridge password secret'
}

padded_key_passes() {
    setup
    printf '  placeholder  \n' >"$ROOT/.secrets/embed_api_key.txt"
    passes
}

# --- Fractional timeouts match the float loaders (#482) -------------------

fractional_timeouts_pass() {
    setup 'INFERENCE_TIMEOUT_SECS=1.5' 'EMBED_TIMEOUT_SECS=2.25' \
        'RERANK_TIMEOUT_SECS=1.0' 'MCP_SESSION_IDLE_TIMEOUT_SECS=90.5' \
        'EMBED_WARMUP_TIMEOUT_SECS=600.'
    passes
}

timeout_below_one_fails() {
    setup 'INFERENCE_TIMEOUT_SECS=0.5'
    fails_with 'INFERENCE_TIMEOUT_SECS must be >= 1'
}

non_numeric_timeout_fails() {
    setup 'RERANK_TIMEOUT_SECS=soon'
    fails_with 'RERANK_TIMEOUT_SECS must be a number'
}

# --- Existing failures still fail -----------------------------------------

zero_sync_interval_fails() {
    setup 'SYNC_INTERVAL=0'
    fails_with 'SYNC_INTERVAL'
}

zero_padded_sync_interval_fails() {
    # mbsync accepts only ^[1-9][0-9]*$.
    setup 'SYNC_INTERVAL=08'
    fails_with 'SYNC_INTERVAL'
}

out_of_range_port_fails() {
    setup 'MCP_PORT=70000'
    fails_with 'MCP_PORT must be between 1 and 65535'
}

unknown_inference_mode_fails() {
    setup 'INFERENCE_MODE=bogus'
    fails_with 'INFERENCE_MODE must be one of'
}

missing_embed_model_fails() {
    setup 'EMBED_MODEL='
    fails_with 'EMBED_MODEL must be set'
}

empty_embed_key_fails() {
    setup
    : >"$ROOT/.secrets/embed_api_key.txt"
    fails_with 'Embed API key'
}

placeholder_bridge_user_fails() {
    setup 'BRIDGE_USER=your@proton.me'
    fails_with 'BRIDGE_USER'
}

one_sided_max_tokens_fails() {
    setup 'INFERENCE_MAX_TOKENS=40000'
    fails_with 'INFERENCE_CONTEXT_TOKENS (32768 when unset)'
}

zero_padded_max_tokens_is_decimal() {
    setup 'INFERENCE_MAX_TOKENS=08'
    passes
}

api_key_in_env_fails() {
    setup 'EMBED_API_KEY=placeholder'
    fails_with 'EMBED_API_KEY must be stored in'
}

loose_secret_mode_fails() {
    setup
    chmod 644 "$ROOT/.secrets/embed_api_key.txt"
    fails_with 'must have mode 600'
}

# --- Whitespace the readers strip (#506) ----------------------------------
# The Python loaders read these values with .strip() (and modes with
# .lower()), so a quoted value padded with spaces is valid; Compose
# itself trims whitespace around an unquoted .env value.

padded_quoted_values_pass() {
    setup 'INDEXER_OCR_ENABLED=" On "' "INDEXER_DELETION_FORCE=' no '" \
        'INDEXER_OCR_MAX_PAGES=" 8 "' 'INFERENCE_MAX_TOKENS=" 2048 "' \
        'INFERENCE_CONTEXT_TOKENS=" 32768 "' 'RERANK_CANDIDATES=" 5 "' \
        'INDEXER_MAX_ATTEMPTS=" 3 "' 'INDEXER_PARSE_MAX_BYTES=" 0 "' \
        'INFERENCE_TIMEOUT_SECS=" 1.5 "' 'EMBED_WARMUP_TIMEOUT_SECS=" 600 "' \
        'INDEXER_DELETION_MAX_BATCH_PCT=" 0.1 "' 'MCP_EXPERIMENTAL_TOOLS=" TRUE "' \
        'MCP_SESSION_IDLE_TIMEOUT_SECS=" 60 "'
    passes
}

padded_and_cased_modes_pass() {
    setup 'INFERENCE_MODE=" OpenAI "' 'EMBED_MODE=" openai "' \
        'RERANK_MODE=" None "' 'MCP_TRANSPORT=" Dual "'
    passes
}

padded_disabled_mode_disables_the_layer() {
    setup 'INFERENCE_MODE=" none "'
    : >"$ROOT/.secrets/inference_api_key.txt"
    passes
}

exported_padded_value_passes() {
    setup
    passes 'INDEXER_OCR_ENABLED= on ' 'RERANK_TIMEOUT_SECS= 30 '
}

whitespace_only_number_takes_the_default() {
    setup 'INDEXER_OCR_MAX_PAGES="   "' 'INFERENCE_MAX_TOKENS="  "'
    passes
}

whitespace_only_mode_fails() {
    # Compose passes the spaces through and the loader strips them to an
    # empty, unknown mode.
    setup 'INFERENCE_MODE="   "'
    fails_with 'INFERENCE_MODE must be one of'
}

padded_invalid_bool_fails() {
    setup 'INDEXER_OCR_ENABLED=" tru "'
    fails_with 'INDEXER_OCR_ENABLED must be true or false'
}

padded_value_below_minimum_fails() {
    setup 'INDEXER_OCR_MAX_PAGES=" 0 "'
    fails_with 'INDEXER_OCR_MAX_PAGES must be >= 1'
}

padded_quoted_sync_interval_fails() {
    # mbsync reads SYNC_INTERVAL without stripping it.
    setup 'SYNC_INTERVAL=" 60 "'
    fails_with 'SYNC_INTERVAL'
}

unquoted_values_are_trimmed_like_compose() {
    setup 'SYNC_INTERVAL=60   ' 'MCP_PORT=  3000' 'INFERENCE_MODE=  ' \
        'BRIDGE_COMMIT= 04e46eb4fbc1c7ef4425a920bfc352050da0606b '
    passes
}

check "keys with Compose defaults may be omitted" keys_with_compose_defaults_may_be_omitted
check "empty keys with Compose defaults use the default" \
    empty_keys_with_compose_defaults_use_the_default
check "explicit values still pass" explicit_values_still_pass
check "an exported invalid value overrides a valid .env" exported_invalid_value_overrides_valid_env
check "an exported valid value overrides an invalid .env" exported_valid_value_overrides_invalid_env
check "an exported empty value takes the Compose default" \
    exported_empty_value_takes_the_compose_default
check "an exported mode overrides .env" exported_mode_overrides_env
check "a whitespace-only embed key fails" whitespace_only_embed_key_fails
check "a newline-only inference key fails" newline_only_inference_key_fails
check "a whitespace-only Bridge password fails" whitespace_only_bridge_pass_fails
check "a key padded with whitespace passes" padded_key_passes
check "fractional timeouts pass" fractional_timeouts_pass
check "a timeout below 1 fails" timeout_below_one_fails
check "a non-numeric timeout fails" non_numeric_timeout_fails
check "SYNC_INTERVAL=0 fails" zero_sync_interval_fails
check "a zero-padded SYNC_INTERVAL fails" zero_padded_sync_interval_fails
check "an out-of-range MCP_PORT fails" out_of_range_port_fails
check "an unknown INFERENCE_MODE fails" unknown_inference_mode_fails
check "a missing EMBED_MODEL fails" missing_embed_model_fails
check "an empty embed key fails" empty_embed_key_fails
check "the placeholder BRIDGE_USER fails" placeholder_bridge_user_fails
check "a one-sided INFERENCE_MAX_TOKENS fails" one_sided_max_tokens_fails
check "a zero-padded INFERENCE_MAX_TOKENS is decimal" zero_padded_max_tokens_is_decimal
check "an API key in .env fails" api_key_in_env_fails
check "a secret file not 600 fails" loose_secret_mode_fails
check "padded quoted values pass" padded_quoted_values_pass
check "padded and mixed-case modes pass" padded_and_cased_modes_pass
check "a padded none disables the layer" padded_disabled_mode_disables_the_layer
check "an exported padded value passes" exported_padded_value_passes
check "a whitespace-only number takes the default" whitespace_only_number_takes_the_default
check "a whitespace-only mode fails" whitespace_only_mode_fails
check "a padded invalid boolean fails" padded_invalid_bool_fails
check "a padded value below its minimum fails" padded_value_below_minimum_fails
check "a padded quoted SYNC_INTERVAL fails" padded_quoted_sync_interval_fails
check "unquoted values are trimmed like Compose" unquoted_values_are_trimmed_like_compose

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
