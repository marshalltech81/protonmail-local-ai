#!/bin/bash
set -Eeuo pipefail

# Tests for scripts/config-mode.sh (config/mode, sourced by
# scripts/validate-env.sh) and scripts/init-config.sh (`make
# init-config`, #1603, PLAN.md decision 50). Each case runs against a
# fresh temporary root. Every value is a placeholder.
#
# Run: bash scripts/tests/config_mode_test.sh

ROOT_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_MODE_SCRIPT="$ROOT_SCRIPTS_DIR/config-mode.sh"
INIT_CONFIG_SCRIPT="$ROOT_SCRIPTS_DIR/init-config.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

# shellcheck source=scripts/config-mode.sh disable=SC1091 # linted on its own
source "$CONFIG_MODE_SCRIPT"

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

# --- config_mode_problem / read_config_mode / write_config_mode ------------

problem_includes() {
    local expected="$1" mode_file="$2" toml_file="$3"
    local problem
    problem="$(config_mode_problem "$mode_file" "$toml_file")"
    [[ "$problem" == *"$expected"* ]] || {
        printf 'expected a problem containing: %s\ngot: %s\n' "$expected" "$problem"
        return 1
    }
}

no_problem() {
    local mode_file="$1" toml_file="$2"
    local problem
    problem="$(config_mode_problem "$mode_file" "$toml_file")"
    [[ -z "$problem" ]] || {
        printf 'expected no problem, got: %s\n' "$problem"
        return 1
    }
}

a_missing_mode_file_is_a_problem() {
    local dir
    dir="$(mktemp -d "$WORK/d.XXXXXX")"
    problem_includes "config/mode is missing" "$dir/mode" "$dir/config.toml"
}

a_legacy_mode_with_no_toml_file_has_no_problem() {
    local dir
    dir="$(mktemp -d "$WORK/d.XXXXXX")"
    write_config_mode "$dir/mode" "$CONFIG_MODE_LEGACY"
    no_problem "$dir/mode" "$dir/config.toml"
}

a_toml_mode_with_a_toml_file_has_no_problem() {
    local dir
    dir="$(mktemp -d "$WORK/d.XXXXXX")"
    write_config_mode "$dir/mode" "$CONFIG_MODE_TOML"
    printf 'placeholder\n' >"$dir/config.toml"
    no_problem "$dir/mode" "$dir/config.toml"
}

a_legacy_mode_with_a_stray_toml_file_is_a_problem() {
    local dir
    dir="$(mktemp -d "$WORK/d.XXXXXX")"
    write_config_mode "$dir/mode" "$CONFIG_MODE_LEGACY"
    printf 'placeholder\n' >"$dir/config.toml"
    problem_includes "a mixed configuration is refused" "$dir/mode" "$dir/config.toml"
}

a_toml_mode_with_no_toml_file_is_a_problem() {
    local dir
    dir="$(mktemp -d "$WORK/d.XXXXXX")"
    write_config_mode "$dir/mode" "$CONFIG_MODE_TOML"
    problem_includes "config/config.toml does not exist" "$dir/mode" "$dir/config.toml"
}

an_unrecognized_mode_value_is_a_problem() {
    local dir
    dir="$(mktemp -d "$WORK/d.XXXXXX")"
    write_config_mode "$dir/mode" "bogus"
    problem_includes "must be exactly 'legacy' or 'toml'" "$dir/mode" "$dir/config.toml"
}

a_mode_file_not_600_is_a_problem() {
    local dir
    dir="$(mktemp -d "$WORK/d.XXXXXX")"
    write_config_mode "$dir/mode" "$CONFIG_MODE_LEGACY"
    chmod 644 "$dir/mode"
    problem_includes "must have mode 600" "$dir/mode" "$dir/config.toml"
}

write_config_mode_is_600_and_trimmed() {
    local dir
    dir="$(mktemp -d "$WORK/d.XXXXXX")"
    write_config_mode "$dir/mode" "$CONFIG_MODE_TOML"
    [[ "$(read_config_mode "$dir/mode")" == "toml" ]] || return 1
    [[ "$(stat -f '%Lp' "$dir/mode" 2>/dev/null || stat -c '%a' "$dir/mode")" == "600" ]]
}

check "a missing config/mode file is a problem" a_missing_mode_file_is_a_problem
check "legacy mode with no config.toml has no problem" a_legacy_mode_with_no_toml_file_has_no_problem
check "toml mode with a config.toml has no problem" a_toml_mode_with_a_toml_file_has_no_problem
check "legacy mode with a stray config.toml is a problem" \
    a_legacy_mode_with_a_stray_toml_file_is_a_problem
check "toml mode with no config.toml is a problem" a_toml_mode_with_no_toml_file_is_a_problem
check "an unrecognized mode value is a problem" an_unrecognized_mode_value_is_a_problem
check "a config/mode file not 600 is a problem" a_mode_file_not_600_is_a_problem
check "write_config_mode writes mode 600, trimmed on read" write_config_mode_is_600_and_trimmed

# --- scripts/init-config.sh (make init-config) -----------------------------

setup_init_config() {
    ROOT="$(mktemp -d "$WORK/root.XXXXXX")"
    mkdir -p "$ROOT/scripts" "$ROOT/config"
    cp "$INIT_CONFIG_SCRIPT" "$ROOT/scripts/init-config.sh"
    cp "$CONFIG_MODE_SCRIPT" "$ROOT/scripts/config-mode.sh"
}

run_init_config() {
    env -i PATH="$PATH" "$BASH" "$ROOT/scripts/init-config.sh" >"$WORK/output" 2>&1 && STATUS=0 || STATUS=$?
    cat "$WORK/output"
}

a_fresh_deployment_gets_legacy_mode() {
    setup_init_config
    run_init_config
    [[ "$STATUS" -eq 0 ]] || return 1
    [[ "$(read_config_mode "$ROOT/config/mode")" == "legacy" ]] || return 1
    [[ "$(stat -f '%Lp' "$ROOT/config/mode" 2>/dev/null || stat -c '%a' "$ROOT/config/mode")" == "600" ]]
}

an_existing_mode_file_is_never_replaced() {
    setup_init_config
    write_config_mode "$ROOT/config/mode" "$CONFIG_MODE_TOML"
    run_init_config
    [[ "$STATUS" -eq 0 ]] || return 1
    [[ "$(read_config_mode "$ROOT/config/mode")" == "toml" ]]
}

a_stray_config_toml_without_mode_is_refused() {
    setup_init_config
    printf 'placeholder\n' >"$ROOT/config/config.toml"
    run_init_config
    [[ "$STATUS" -eq 1 ]] || return 1
    grep -F 'make migrate-config' "$WORK/output" >/dev/null || return 1
    [[ ! -f "$ROOT/config/mode" ]]
}

check "a fresh deployment gets config/mode=legacy" a_fresh_deployment_gets_legacy_mode
check "an existing config/mode is never replaced" an_existing_mode_file_is_never_replaced
check "a stray config.toml without config/mode is refused" a_stray_config_toml_without_mode_is_refused

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
