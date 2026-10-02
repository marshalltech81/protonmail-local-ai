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

# --- Chunk budgets checked against each other (#507) ---------------------
# chunker.chunk_message needs target <= max and overlap < target; an
# omitted or empty side takes its Compose default (1000 / 1500 / 150).

chunk_case() {
    local expected="$1"
    shift
    setup "$@"
    if [[ "$expected" == ok ]]; then
        passes
    else
        fails_with "$expected"
    fi
}

readonly CHUNK_TARGET_OVER_MAX='INDEXER_CHUNK_TARGET_TOKENS (2000) must be <= INDEXER_CHUNK_MAX_TOKENS (1500)'

chunk_budget_table() {
    chunk_case ok
    chunk_case ok 'INDEXER_CHUNK_TARGET_TOKENS=1000' 'INDEXER_CHUNK_MAX_TOKENS=1500' \
        'INDEXER_CHUNK_OVERLAP_TOKENS=150'
    chunk_case "$CHUNK_TARGET_OVER_MAX" 'INDEXER_CHUNK_TARGET_TOKENS=2000'
    chunk_case ok 'INDEXER_CHUNK_TARGET_TOKENS=1500'
    chunk_case 'INDEXER_CHUNK_TARGET_TOKENS (1501) must be <=' 'INDEXER_CHUNK_TARGET_TOKENS=1501'
    chunk_case 'INDEXER_CHUNK_TARGET_TOKENS (1000) must be <= INDEXER_CHUNK_MAX_TOKENS (999)' \
        'INDEXER_CHUNK_MAX_TOKENS=999'
    chunk_case ok 'INDEXER_CHUNK_MAX_TOKENS=1000'
    chunk_case 'INDEXER_CHUNK_OVERLAP_TOKENS (1000) must be < INDEXER_CHUNK_TARGET_TOKENS (1000)' \
        'INDEXER_CHUNK_OVERLAP_TOKENS=1000'
    chunk_case ok 'INDEXER_CHUNK_OVERLAP_TOKENS=999'
    chunk_case ok 'INDEXER_CHUNK_OVERLAP_TOKENS=0'
    chunk_case 'INDEXER_CHUNK_OVERLAP_TOKENS (150) must be < INDEXER_CHUNK_TARGET_TOKENS (150)' \
        'INDEXER_CHUNK_TARGET_TOKENS=150'
    chunk_case ok 'INDEXER_CHUNK_TARGET_TOKENS=151'
    chunk_case ok 'INDEXER_CHUNK_TARGET_TOKENS=0100' 'INDEXER_CHUNK_MAX_TOKENS=0100' \
        'INDEXER_CHUNK_OVERLAP_TOKENS=099'
    chunk_case ok 'INDEXER_CHUNK_TARGET_TOKENS=08' 'INDEXER_CHUNK_MAX_TOKENS=09' \
        'INDEXER_CHUNK_OVERLAP_TOKENS=07'
    chunk_case 'INDEXER_CHUNK_TARGET_TOKENS (1000) must be <= INDEXER_CHUNK_MAX_TOKENS (500)' \
        'INDEXER_CHUNK_TARGET_TOKENS=' 'INDEXER_CHUNK_MAX_TOKENS=500'
    chunk_case 'INDEXER_CHUNK_TARGET_TOKENS must be an integer' 'INDEXER_CHUNK_TARGET_TOKENS=abc'
}

exported_chunk_budget_overrides_env() {
    setup 'INDEXER_CHUNK_TARGET_TOKENS=1000'
    fails_with "$CHUNK_TARGET_OVER_MAX" INDEXER_CHUNK_TARGET_TOKENS=2000
    setup 'INDEXER_CHUNK_TARGET_TOKENS=2000'
    passes INDEXER_CHUNK_TARGET_TOKENS=
}

padded_chunk_budgets_are_stripped() {
    setup 'INDEXER_CHUNK_TARGET_TOKENS=" 1500 "' 'INDEXER_CHUNK_MAX_TOKENS=" 1500 "' \
        'INDEXER_CHUNK_OVERLAP_TOKENS="  "'
    passes
    setup 'INDEXER_CHUNK_TARGET_TOKENS=" 2000 "'
    fails_with "$CHUNK_TARGET_OVER_MAX"
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

# The Compose default model is an Anthropic one, so openai mode needs its
# own (Codex review round 2).
openai_mode_requires_a_model() {
    setup 'INFERENCE_MODE=openai'
    fails_with 'INFERENCE_MODEL must be set when INFERENCE_MODE=openai'
    setup 'INFERENCE_MODE=" OpenAI "' 'INFERENCE_MODEL='
    fails_with 'INFERENCE_MODEL must be set when INFERENCE_MODE=openai'
    setup 'INFERENCE_MODE=openai' 'INFERENCE_MODEL=placeholder-model'
    passes
    setup 'INFERENCE_MODEL=placeholder-model'
    fails_with 'INFERENCE_MODEL must be set when INFERENCE_MODE=openai' \
        INFERENCE_MODE=openai INFERENCE_MODEL=
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

# Compose and the mcp-server loader both read a zero-padded port as
# decimal (Codex review round 1).
zero_padded_port_is_decimal() {
    setup 'MCP_PORT=08080'
    passes
    setup
    passes MCP_PORT=09999
    setup
    fails_with 'MCP_PORT must be between 1 and 65535' MCP_PORT=065536
}

out_of_range_port_fails() {
    setup 'MCP_PORT=70000'
    fails_with 'MCP_PORT must be between 1 and 65535'
}

unknown_inference_mode_fails() {
    setup 'INFERENCE_MODE=bogus'
    fails_with 'INFERENCE_MODE must be one of'
}

# #498: Streamable HTTP is the only transport. The removed values fail
# with migration steps; anything else fails closed.
removed_transport_fails_with_migration_steps() {
    local value expected
    for value in sse dual ' Dual '; do
        expected="$(printf '%s' "$value" | tr -d ' ' | tr '[:upper:]' '[:lower:]')"
        setup "MCP_TRANSPORT=\"$value\""
        fails_with "MCP_TRANSPORT=$expected was removed"
        grep -F '/sse to' "$WORK/output" >/dev/null
        grep -F '/mcp' "$WORK/output" >/dev/null
    done
}

# An exported value wins over .env, so the steps must say to unset it.
exported_removed_transport_names_the_shell_export() {
    setup
    fails_with 'MCP_TRANSPORT=sse was removed' MCP_TRANSPORT=sse
    grep -F 'unset MCP_TRANSPORT' "$WORK/output" >/dev/null
}

unknown_transport_fails() {
    setup 'MCP_TRANSPORT=websocket'
    fails_with "MCP_TRANSPORT must be 'streamable-http' or unset"
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

# --- Source-authority rules file (#468) -----------------------------------
# config/authority.toml is optional and holds real addresses, so like the
# secret files it must be 600. A symlink is rejected: Compose mounts the
# directory, so a link the container cannot follow would pass here and
# stop the indexer.

write_authority() {
    mkdir -p "$ROOT/config"
    printf '[counsel]\ndomains = ["lawfirm.example"]\n' >"$ROOT/config/authority.toml"
    chmod "$1" "$ROOT/config/authority.toml"
}

absent_authority_file_passes() {
    setup
    passes
}

private_authority_file_passes() {
    setup
    write_authority 600
    passes
}

loose_authority_file_fails() {
    setup
    local mode
    for mode in 644 604 640 660 620 700; do
        write_authority "$mode"
        fails_with "config/authority.toml must have mode 600, found $mode"
    done
}

symlinked_authority_file_fails() {
    setup
    write_authority 600
    mv "$ROOT/config/authority.toml" "$ROOT/config/rules.toml"
    ln -s rules.toml "$ROOT/config/authority.toml"
    fails_with 'config/authority.toml must be a regular file, not a symlink'
    rm "$ROOT/config/authority.toml"
    ln -s missing.toml "$ROOT/config/authority.toml"
    fails_with 'must be a regular file, not a symlink'
}

# The indexer opens only a regular file, so a 600 directory or FIFO
# must fail here rather than at indexer startup.
non_regular_authority_path_fails() {
    setup
    mkdir -p "$ROOT/config/authority.toml"
    chmod 600 "$ROOT/config/authority.toml"
    fails_with 'config/authority.toml must be a regular file'
    rmdir "$ROOT/config/authority.toml"
    mkfifo -m 600 "$ROOT/config/authority.toml"
    fails_with 'config/authority.toml must be a regular file'
}

# The documented authority edit flow restarts the indexer through
# `make restart-indexer`, which must run this validator first (#530).
# A dry run prints the recipes in order without running them.
restart_indexer_validates_first() {
    local repo plan validate_line restart_line
    repo="$(cd "$(dirname "$SCRIPT")/.." && pwd)"
    plan="$(make -n -C "$repo" restart-indexer)"
    printf '%s\n' "$plan"
    validate_line="$(grep -n -F './scripts/validate-env.sh' <<<"$plan" | cut -d: -f1)"
    restart_line="$(grep -n -x 'docker compose restart indexer' <<<"$plan" | cut -d: -f1)"
    [[ -n "$validate_line" && -n "$restart_line" ]] || return 1
    ((validate_line < restart_line)) || return 1
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
    setup 'INFERENCE_MODE=" OpenAI "' 'INFERENCE_MODEL=placeholder-model' \
        'EMBED_MODE=" openai "' 'RERANK_MODE=" None "' 'MCP_TRANSPORT=" Streamable-HTTP "'
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
check "openai mode requires an explicit model" openai_mode_requires_a_model
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
check "a zero-padded MCP_PORT is decimal" zero_padded_port_is_decimal
check "an unknown INFERENCE_MODE fails" unknown_inference_mode_fails
check "a removed MCP_TRANSPORT fails with migration steps" removed_transport_fails_with_migration_steps
check "an exported removed MCP_TRANSPORT names the shell export" exported_removed_transport_names_the_shell_export
check "an unknown MCP_TRANSPORT fails" unknown_transport_fails
check "a missing EMBED_MODEL fails" missing_embed_model_fails
check "an empty embed key fails" empty_embed_key_fails
check "the placeholder BRIDGE_USER fails" placeholder_bridge_user_fails
check "a one-sided INFERENCE_MAX_TOKENS fails" one_sided_max_tokens_fails
check "a zero-padded INFERENCE_MAX_TOKENS is decimal" zero_padded_max_tokens_is_decimal
check "an API key in .env fails" api_key_in_env_fails
check "a secret file not 600 fails" loose_secret_mode_fails
check "an absent authority file passes" absent_authority_file_passes
check "a private authority file passes" private_authority_file_passes
check "an authority file not 600 fails" loose_authority_file_fails
check "a symlinked authority file fails" symlinked_authority_file_fails
check "a non-regular authority path fails" non_regular_authority_path_fails
check "make restart-indexer validates before restarting" restart_indexer_validates_first
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
check "chunk budgets follow the chunker's rules" chunk_budget_table
check "an exported chunk budget overrides .env" exported_chunk_budget_overrides_env
check "padded chunk budgets are stripped" padded_chunk_budgets_are_stripped

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
