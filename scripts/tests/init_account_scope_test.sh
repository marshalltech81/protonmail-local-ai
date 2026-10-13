#!/bin/bash
set -Eeuo pipefail

# Tests for scripts/init-account-scope.sh, which `make init-secrets` runs
# to write ACCOUNT_SCOPE to .env (#1596). Each case copies the script into
# a fresh temporary root with a synthetic .env and runs it with a clean
# environment. Every value is a placeholder.
#
# Run: bash scripts/tests/init_account_scope_test.sh

SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/init-account-scope.sh"
VALIDATOR="$(dirname "$SCRIPT")/validate-env.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0

readonly UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
readonly KEPT='11111111-2222-4333-8444-555555555555'

# setup [ENV_CONTENT]: a fresh root holding the script and, unless the
# content is the word "none", a .env with that content (mode 640).
setup() {
    ROOT="$(mktemp -d "$WORK/root.XXXXXX")"
    mkdir -p "$ROOT/scripts"
    cp "$SCRIPT" "$ROOT/scripts/init-account-scope.sh"
    if [[ "${1-}" != none ]]; then
        printf '%s' "${1-}" >"$ROOT/.env"
        chmod 640 "$ROOT/.env"
    fi
}

run_script() {
    env -i PATH="$PATH" "$@" "$BASH" "$ROOT/scripts/init-account-scope.sh" \
        >"$WORK/output" 2>&1 && STATUS=0 || STATUS=$?
    cat "$WORK/output"
}

# The ACCOUNT_SCOPE values in .env, one per line.
scopes() {
    grep -E '^ACCOUNT_SCOPE=' "$ROOT/.env" | sed 's/^ACCOUNT_SCOPE=//'
}

file_mode() {
    stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

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

appends_a_uuid_when_absent() {
    setup $'BRIDGE_USER=placeholder@example.invalid\nEMBED_MODEL=placeholder'
    run_script
    [[ "$STATUS" -eq 0 ]]
    [[ "$(scopes | wc -l | tr -d ' ')" == 1 ]]
    [[ "$(scopes)" =~ $UUID_RE ]]
    # The other lines are kept, the last one now ending in a newline.
    grep -qx 'EMBED_MODEL=placeholder' "$ROOT/.env"
    grep -qx 'BRIDGE_USER=placeholder@example.invalid' "$ROOT/.env"
}

fills_the_empty_line_from_env_example_in_place() {
    setup $'SOURCE_MODE=proton-bridge\nACCOUNT_SCOPE=\nBRIDGE_USER=placeholder@example.invalid\n'
    run_script
    [[ "$STATUS" -eq 0 ]]
    [[ "$(scopes | wc -l | tr -d ' ')" == 1 ]]
    [[ "$(scopes)" =~ $UUID_RE ]]
    [[ "$(sed -n 2p "$ROOT/.env")" == "ACCOUNT_SCOPE=$(scopes)" ]]
    [[ "$(file_mode "$ROOT/.env")" == 640 ]]
    [[ -z "$(find "$ROOT" -name '.env.account-scope.*')" ]]
}

never_replaces_a_value() {
    local value
    for value in "$KEPT" "\"$KEPT\"" " $KEPT " 'not-a-uuid' 'AAAAAAAA-2222-4333-8444-555555555555'; do
        setup "ACCOUNT_SCOPE=$value"$'\n'
        run_script
        [[ "$STATUS" -eq 0 ]]
        [[ "$(cat "$ROOT/.env")" == "ACCOUNT_SCOPE=$value" ]]
        grep -qF 'already set' "$WORK/output"
    done
}

is_idempotent() {
    local first
    setup $'ACCOUNT_SCOPE=\n'
    run_script
    first="$(scopes)"
    run_script
    [[ "$STATUS" -eq 0 ]]
    [[ "$(scopes)" == "$first" ]]
}

two_runs_write_different_uuids() {
    local one two
    setup ''
    run_script
    one="$(scopes)"
    setup ''
    run_script
    two="$(scopes)"
    [[ "$one" =~ $UUID_RE && "$two" =~ $UUID_RE && "$one" != "$two" ]]
}

refuses_an_earlier_value_hidden_by_an_empty_last_line() {
    setup "ACCOUNT_SCOPE=$KEPT"$'\nACCOUNT_SCOPE=\n'
    run_script
    [[ "$STATUS" -eq 1 ]]
    grep -qF 'more than one ACCOUNT_SCOPE line' "$WORK/output"
    [[ "$(cat "$ROOT/.env")" == "ACCOUNT_SCOPE=$KEPT"$'\nACCOUNT_SCOPE=' ]]
    ! grep -qF "$KEPT" "$WORK/output"
}

writes_nothing_when_exported() {
    setup $'ACCOUNT_SCOPE=\n'
    run_script ACCOUNT_SCOPE="$KEPT"
    [[ "$STATUS" -eq 0 ]]
    [[ "$(cat "$ROOT/.env")" == 'ACCOUNT_SCOPE=' ]]
    ! grep -qF "$KEPT" "$WORK/output"
}

fails_without_env_file() {
    setup none
    run_script
    [[ "$STATUS" -eq 1 ]]
    grep -qF 'cp .env.example .env' "$WORK/output"
    [[ ! -e "$ROOT/.env" ]]
}

# An interrupted run can leave the temporary copy of .env in the
# checkout; it must be ignored as .env is (Codex round 3 on #1601).
the_temporary_copy_is_ignored() {
    local repo
    repo="$(dirname "$(dirname "$SCRIPT")")"
    grep -qF '.env.account-scope.' "$SCRIPT"
    git -C "$repo" check-ignore -q .env.account-scope.AbC123
}

generated_value_passes_the_validator_rule() {
    local scope
    setup $'ACCOUNT_SCOPE=\n'
    run_script
    scope="$(scopes)"
    # The rule validate-env.sh applies, read from the script itself.
    grep -qF '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' "$VALIDATOR"
    [[ "$scope" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]
}

check "appends a UUID when .env has no ACCOUNT_SCOPE line" appends_a_uuid_when_absent
check "the temporary copy of .env is ignored" the_temporary_copy_is_ignored
check "fills the empty line .env.example ships, keeping the file mode" \
    fills_the_empty_line_from_env_example_in_place
check "never replaces a value, valid or not" never_replaces_a_value
check "a second run changes nothing" is_idempotent
check "each generation is a new random UUID" two_runs_write_different_uuids
check "refuses an earlier value hidden by an empty last line" \
    refuses_an_earlier_value_hidden_by_an_empty_last_line
check "writes nothing when the shell exports ACCOUNT_SCOPE" writes_nothing_when_exported
check "fails without .env" fails_without_env_file
check "the generated value passes validate-env's rule" generated_value_passes_the_validator_rule

if ((FAILURES)); then
    printf '%d init-account-scope.sh case(s) failed\n' "$FAILURES"
    exit 1
fi
printf 'All init-account-scope.sh cases passed\n'
