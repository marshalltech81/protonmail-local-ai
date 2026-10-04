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
BRIDGE_CERT_FINGERPRINT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
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
    printf 'placeholder-mcp-token-xxxxxxxxxxxxxxxx\n' >"$ROOT/.secrets/mcp_auth_token.txt"
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
    setup 'SYNC_INTERVAL=' 'MCP_PORT=' 'INFERENCE_MODEL='
    passes
}

explicit_values_still_pass() {
    setup 'SYNC_INTERVAL=60' 'MCP_PORT=3000' \
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

# mbsync's per-run deadline (#282): the entrypoint and healthcheck accept
# only ^[1-9][0-9]{0,8}$, unstripped; empty or omitted takes the default.
sync_deadline_values() {
    local value
    for value in 1 86400 999999999; do
        setup "SYNC_DEADLINE_SECONDS=${value}"
        passes || return 1
    done
    setup 'SYNC_DEADLINE_SECONDS='
    passes || return 1
    for value in 0 08 -60 1.5 abc '" 60 "' 1000000000; do
        setup "SYNC_DEADLINE_SECONDS=${value}"
        fails_with 'SYNC_DEADLINE_SECONDS' || {
            printf 'accepted %s\n' "$value"
            return 1
        }
    done
    setup 'SYNC_DEADLINE_SECONDS=86400'
    fails_with 'SYNC_DEADLINE_SECONDS' SYNC_DEADLINE_SECONDS=0
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
        grep -F 'http://127.0.0.1:<MCP_PORT>/mcp' "$WORK/output" >/dev/null
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

# mbsync never trusts the Bridge app's certificate on first use, so the
# expected fingerprint is required and must be a SHA-256 (#497).
missing_bridge_cert_fingerprint_fails() {
    setup 'BRIDGE_CERT_FINGERPRINT='
    fails_with 'BRIDGE_CERT_FINGERPRINT'
}

malformed_bridge_cert_fingerprint_fails() {
    local value
    for value in abc 'aa:bb' "$(printf 'g%.0s' {1..64})" "$(printf 'a%.0s' {1..63})"; do
        setup "BRIDGE_CERT_FINGERPRINT=$value"
        fails_with 'BRIDGE_CERT_FINGERPRINT'
    done
}

openssl_form_bridge_cert_fingerprint_passes() {
    local colon_form
    colon_form="$(printf 'AB:%.0s' {1..31})AB"
    setup "BRIDGE_CERT_FINGERPRINT=\"sha256 Fingerprint=${colon_form}\""
    passes
    setup "BRIDGE_CERT_FINGERPRINT=${colon_form}"
    passes
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

# --- MCP bearer token (PLAN.md Resolved decisions 13) ---------------------

missing_mcp_token_file_fails() {
    setup
    rm "$ROOT/.secrets/mcp_auth_token.txt"
    fails_with 'MCP bearer token is missing or empty'
}

empty_mcp_token_fails() {
    setup
    : >"$ROOT/.secrets/mcp_auth_token.txt"
    fails_with 'MCP bearer token is missing or empty'
    printf ' \n' >"$ROOT/.secrets/mcp_auth_token.txt"
    fails_with 'MCP bearer token is missing or empty'
}

# #589: the token must be at least 32 RFC 6750 b64token characters, the
# set scripts/mcp-auth-headers.sh sends, and the failure never echoes it.
# write_mcp_token CONTENTS: replace the token file, keeping mode 600.
write_mcp_token() {
    printf '%s' "$1" >"$ROOT/.secrets/mcp_auth_token.txt"
}

unusable_mcp_token_fails_without_echoing_it() {
    local token
    setup
    for token in \
        'synthetic marker 5d1a xxxxxxxxxxxxxxxx' \
        'synthetic-marker-5d1a-xxxxxxxxxxxxxxxx:x' \
        'synthetic-marker-5d1a-xxxxxxxxxxxxxxxx?x' \
        'synthetic-marker-5d1a-xxxxxxxxxxxxxxxx"x' \
        $'synthetic-marker-5d1a-xxxxxxxxxxxxxxxx\tx' \
        $'synthetic-marker-5d1a-xxxxxxxxxxxxxxxx\x7fx' \
        $'synthetic-marker-5d1a-xxxxxxxxxxxxxxxx\nx' \
        $'synthetic-marker-5d1a-xxxxxxxxxxxxxxxx\xc3\xa9' \
        'synthetic-marker-5d1a=xxxxxxxxxxxxxxxx' \
        'synthetic-marker-5d1a' \
        'synthetic-marker-xxxxxxxxxxxxxx'; do
        write_mcp_token "$token"
        fails_with 'MCP bearer token in'
        if grep -F 'marker' "$WORK/output" >/dev/null; then
            return 1
        fi
    done
}

# Review round 1: command substitution drops NUL bytes, so a token with
# one inside must be refused before the file is read into a variable.
mcp_token_with_nul_byte_fails() {
    setup
    printf 'synthetic-marker-xxxxxxx\0xxxxxxxxxxxxxxxx' >"$ROOT/.secrets/mcp_auth_token.txt"
    fails_with 'MCP bearer token in'
    if grep -F 'marker' "$WORK/output" >/dev/null; then
        return 1
    fi
}

usable_mcp_token_passes() {
    local token
    setup
    for token in \
        "$(printf '0123456789abcdef%.0s' 1 2 3 4)" \
        'AZaz09-._~+/AZaz09-._~+/AZaz09-._~+/' \
        'SyntheticBase64TokenSyntheticBase64TokenAAA=' \
        'SyntheticBase64TokenSyntheticBase64TokenA==' \
        $'  xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \t\n\n'; do
        write_mcp_token "$token"
        passes
    done
}

loose_mcp_token_mode_fails() {
    setup
    chmod 644 "$ROOT/.secrets/mcp_auth_token.txt"
    fails_with 'mcp_auth_token.txt must have mode 600'
}

mcp_token_in_env_fails() {
    setup 'MCP_AUTH_TOKEN=placeholder'
    fails_with 'MCP_AUTH_TOKEN must be stored in'
}

# INDEXER_UNLINK_ON_REAP was removed (#721): the indexer never deletes
# Maildir files. Any value left in .env, false included, stops the run.
removed_unlink_on_reap_fails() {
    local value
    for value in true false; do
        setup "INDEXER_UNLINK_ON_REAP=$value"
        fails_with 'INDEXER_UNLINK_ON_REAP was removed: the indexer never deletes Maildir files (see #728). Delete the line from .env.'
    done
}

mcp_token_failure_does_not_echo_the_value() {
    setup
    chmod 644 "$ROOT/.secrets/mcp_auth_token.txt"
    fails_with 'must have mode 600'
    ! grep -F 'placeholder-mcp-token' "$WORK/output" >/dev/null
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

# The authority checks depend on the host: a Linux bind mount keeps host
# ownership, so the file must grant the indexer (UID 1002) read (#526).
# stub_host OS [OWNER] puts stub commands first on PATH so every case
# sees the same host whatever machine runs the tests: `uname -s` prints
# OS, `stat` reports OWNER as the owner of files and directories when
# given ($STUBS/file-owner and $STUBS/dir-owner, which a case may
# rewrite) and group 4242 ($STUBS/file-group, $STUBS/dir-group),
# `getfacl` prints $STUBS/acl (written by write_acl) for a
# file and $STUBS/diracl, or else the mode's base entries, for a
# directory, and `getent passwd 1002`
# finds an account only when $STUBS/uid1002 exists; `id -u` (the
# operator, `id -un` "operator") is 4242, or $STUBS/operator-uid when a
# case writes it ($STUBS/operator-name sets the name); `getent passwd`
# lists the operator, then `other`, and a lookup by UID fails with
# status 1 when $STUBS/lookup-error exists.
stub_host() {
    local os="$1" owner="${2:-}"
    STUBS="$(mktemp -d "$WORK/stubs.XXXXXX")"
    printf '#!/bin/bash\nprintf "%%s\\n" %q\n' "$os" >"$STUBS/uname"
    if [[ -n "$owner" ]]; then
        printf '%s\n' "$owner" >"$STUBS/file-owner"
        printf '%s\n' "$owner" >"$STUBS/dir-owner"
        printf '4242\n' >"$STUBS/file-group"
        printf '4242\n' >"$STUBS/dir-group"
        cat >"$STUBS/stat" <<'STUB'
#!/bin/bash
kind=file
[[ -d "${!#}" ]] && kind=dir
for arg in "$@"; do
    [[ "$arg" == '%u' ]] && exec cat "$(dirname "$0")/$kind-owner"
    [[ "$arg" == '%g' ]] && exec cat "$(dirname "$0")/$kind-group"
done
exec /usr/bin/stat "$@"
STUB
    fi
    cat >"$STUBS/getfacl" <<'STUB'
#!/bin/bash
target="${!#}"
if [[ ! -d "$target" ]]; then
    cat "$(dirname "$0")/acl"
elif [[ -e "$(dirname "$0")/diracl" ]]; then
    cat "$(dirname "$0")/diracl"
else
    # No extended ACL: getfacl prints the mode bits as the base entries.
    mode="$(/usr/bin/stat -c '%a' "$target" 2>/dev/null || /usr/bin/stat -f '%Lp' "$target")"
    for entry in user:: group:: other::; do
        case "$entry" in
            user::) digit=$(((8#$mode >> 6) & 7)) ;;
            group::) digit=$(((8#$mode >> 3) & 7)) ;;
            other::) digit=$((8#$mode & 7)) ;;
        esac
        perms=""
        ((digit & 4)) && perms+=r || perms+=-
        ((digit & 2)) && perms+=w || perms+=-
        ((digit & 1)) && perms+=x || perms+=-
        printf '%s%s\n' "$entry" "$perms"
    done
fi
STUB
    cat >"$STUBS/id" <<'STUB'
#!/bin/bash
if [[ "$*" == '-u' ]]; then
    [[ -e "$(dirname "$0")/operator-uid" ]] && exec cat "$(dirname "$0")/operator-uid"
    printf '4242\n'
    exit 0
fi
if [[ "$*" == '-un' ]]; then
    [[ -e "$(dirname "$0")/operator-name" ]] && exec cat "$(dirname "$0")/operator-name"
    printf 'operator\n'
    exit 0
fi
exec /usr/bin/id "$@"
STUB
    # The operator's entry comes first, so a direct lookup of a UID the
    # operator shares with `other` finds only the operator.
    cat >"$STUBS/getent" <<'STUB'
#!/bin/bash
stubs="$(dirname "$0")"
[[ "$1" == passwd ]] || exit 2
operator_uid=4242 operator_name=operator
[[ -e "$stubs/operator-uid" ]] && operator_uid="$(cat "$stubs/operator-uid")"
[[ -e "$stubs/operator-name" ]] && operator_name="$(cat "$stubs/operator-name")"
entries="$operator_name:x:$operator_uid:$operator_uid::/home/operator:/bin/bash"
[[ -e "$stubs/uid1002" ]] && entries+=$'\nother:x:1002:1002::/home/other:/bin/bash'
if [[ $# -eq 1 ]]; then
    printf '%s\n' "$entries"
    exit 0
fi
[[ -e "$stubs/lookup-error" ]] && exit 1
grep -m 1 "^[^:]*:x:$2:" <<<"$entries" || exit 2
STUB
    chmod 755 "$STUBS"/*
    PATH="$STUBS:$PATH"
}

# Take NAME off PATH: drop its stub and link every other command into
# one directory that leaves it out.
hide_command() {
    local name="$1" bin="$STUBS/bin" dir cmd
    local -a dirs
    rm "$STUBS/$name"
    mkdir "$bin"
    IFS=: read -ra dirs <<<"${PATH#"$STUBS:"}"
    for dir in "${dirs[@]}"; do
        for cmd in "$dir"/*; do
            [[ -x "$cmd" && ! -d "$cmd" && "${cmd##*/}" != "$name" && ! -e "$bin/${cmd##*/}" ]] ||
                continue
            ln -s "$cmd" "$bin/${cmd##*/}"
        done
    done
    PATH="$STUBS:$bin"
}

write_acl() {
    printf '%s\n' "$@" >"$STUBS/acl"
}

write_authority() {
    mkdir -p "$ROOT/config"
    printf '[counsel]\ndomains = ["lawfirm.example"]\n' >"$ROOT/config/authority.toml"
    chmod "$1" "$ROOT/config/authority.toml"
}

absent_authority_file_passes() {
    stub_host Darwin
    setup
    passes
}

private_authority_file_passes() {
    stub_host Darwin
    setup
    write_authority 600
    passes
}

loose_authority_file_fails() {
    stub_host Darwin
    setup
    local mode
    for mode in 644 604 640 660 620 700; do
        write_authority "$mode"
        fails_with "config/authority.toml must have mode 600, found $mode"
    done
}

symlinked_authority_file_fails() {
    stub_host Darwin
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
    stub_host Darwin
    setup
    mkdir -p "$ROOT/config/authority.toml"
    chmod 600 "$ROOT/config/authority.toml"
    fails_with 'config/authority.toml must be a regular file'
    rmdir "$ROOT/config/authority.toml"
    mkfifo -m 600 "$ROOT/config/authority.toml"
    fails_with 'config/authority.toml must be a regular file'
}

# On Linux the operator's own 600 file is unreadable to UID 1002, so it
# fails with the exact command that grants only that UID read.
linux_operator_owned_authority_file_fails_with_command() {
    stub_host Linux 4242
    setup
    write_authority 600
    fails_with 'not readable by the indexer (UID 1002)'
    grep -F "setfacl -b $ROOT/config/authority.toml && chmod 600 $ROOT/config/authority.toml && setfacl -m u:1002:r $ROOT/config/authority.toml" \
        "$WORK/output"
}

# A file UID 1002 owns at 600 is readable in the container as it is.
linux_authority_file_owned_by_indexer_passes() {
    stub_host Linux 1002
    setup
    write_authority 600
    passes
}

# The ACL that command leaves: `stat` shows its mask as group bits (640).
readonly INDEXER_ONLY_ACL=('user::rw-' 'user:1002:r--' 'group::---' 'mask::r--' 'other::---')

linux_indexer_only_acl_passes() {
    stub_host Linux 4242
    setup
    write_authority 640
    write_acl "${INDEXER_ONLY_ACL[@]}"
    passes
    ! grep -F WARNING "$WORK/output"
}

# Any other ACL behind a 640 mode lets someone else read the file.
linux_broader_acl_fails() {
    stub_host Linux 4242
    setup
    write_authority 640
    write_acl 'user::rw-' 'group::r--' 'other::---'
    fails_with 'ACL must grant read to UID 1002 and no one else'
    write_acl 'user::rw-' 'user:1002:r--' 'user:1005:r--' 'group::---' 'mask::r--' 'other::---'
    fails_with 'ACL must grant read to UID 1002 and no one else'
    write_acl 'user::rw-' 'user:1002:r--' 'group::r--' 'mask::r--' 'other::---'
    fails_with 'ACL must grant read to UID 1002 and no one else'
    grep -F 'setfacl -b' "$WORK/output"
}

# The ACL never makes a world- or group-writable mode acceptable.
linux_loose_authority_file_fails() {
    stub_host Linux 4242
    setup
    local mode
    for mode in 644 604 660 620 700; do
        write_authority "$mode"
        fails_with "config/authority.toml must have mode 600"
    done
}

# #662: host UID 1002 reads the file too, so a host account holding it
# that is not the operator fails the check, with the fix and without
# the file's content.
readonly HOST_ACCOUNT_1002_ERROR='ERROR: host account other has UID 1002, the indexer'"'"'s UID'

linux_host_account_1002_fails() {
    stub_host Linux 4242
    setup
    write_authority 640
    write_acl "${INDEXER_ONLY_ACL[@]}"
    : >"$STUBS/uid1002"
    fails_with "$HOST_ACCOUNT_1002_ERROR"
    grep -F 'usermod -u' "$WORK/output"
    ! grep -F 'lawfirm.example' "$WORK/output"
}

# A file UID 1002 owns is readable by that host account too.
linux_owner_1002_with_host_account_fails() {
    stub_host Linux 1002
    setup
    write_authority 600
    : >"$STUBS/uid1002"
    fails_with "$HOST_ACCOUNT_1002_ERROR"
}

# The operator's own account holding UID 1002 is no one else.
linux_operator_holding_uid_1002_passes() {
    stub_host Linux 1002
    setup
    write_authority 600
    printf '1002\n' >"$STUBS/operator-uid"
    passes
    ! grep -F 'host account' "$WORK/output"
}

# Review round 1: another account sharing UID 1002 with the operator
# reads the file just the same, though a lookup by UID names only the
# operator.
linux_account_sharing_operator_uid_1002_fails() {
    stub_host Linux 1002
    setup
    write_authority 600
    printf '1002\n' >"$STUBS/operator-uid"
    : >"$STUBS/uid1002"
    fails_with "$HOST_ACCOUNT_1002_ERROR"
}

# Review round 2: a failed lookup by UID stops validation even though
# the listing that follows it succeeds.
linux_failed_uid_lookup_fails() {
    stub_host Linux 4242
    setup
    write_authority 640
    write_acl "${INDEXER_ONLY_ACL[@]}"
    : >"$STUBS/lookup-error"
    fails_with 'getent passwd failed with status 1'
}

# Review round 2: the operator's name is compared as is, so a name
# with a backslash (winbind's DOMAIN\user) still matches its own entry.
linux_operator_name_with_backslash_passes() {
    stub_host Linux 1002
    setup
    write_authority 600
    printf '1002\n' >"$STUBS/operator-uid"
    printf '%s\n' 'DOMAIN\user' >"$STUBS/operator-name"
    passes
}

# Review round 1: without getent the account check cannot run.
linux_missing_getent_fails() {
    stub_host Linux 4242
    setup
    write_authority 640
    write_acl "${INDEXER_ONLY_ACL[@]}"
    hide_command getent
    fails_with 'getent is needed to check for another host account with UID 1002'
}

# Review round 1: the indexer must also be able to search config/, the
# mount root; a 700 directory needs a search ACL for UID 1002.
linux_unsearchable_config_dir_fails() {
    stub_host Linux 4242
    setup
    write_authority 640
    write_acl "${INDEXER_ONLY_ACL[@]}"
    chmod 700 "$ROOT/config"
    printf '%s\n' 'user::rwx' 'group::---' 'other::---' >"$STUBS/diracl"
    fails_with 'is not searchable by the indexer (UID 1002)'
    grep -F "setfacl -m u:1002:x $ROOT/config" "$WORK/output"
    printf '%s\n' 'user::rwx' 'user:1002:--x' 'group::---' 'mask::--x' 'other::---' \
        >"$STUBS/diracl"
    passes
}

# Review round 1: the printed command quotes the path for the shell.
linux_fix_command_quotes_the_path() {
    stub_host Linux 4242
    setup
    write_authority 600
    mv "$ROOT" "$WORK/with space"
    ROOT="$WORK/with space"
    fails_with 'not readable by the indexer (UID 1002)'
    grep -F "setfacl -b $(printf '%q' "$ROOT/config/authority.toml") &&" "$WORK/output"
}

# Review round 2: a named-user ACL entry for UID 1002 overrides the
# other bits, so one without search denies the indexer.
linux_config_dir_acl_denying_indexer_fails() {
    stub_host Linux 4242
    setup
    write_authority 640
    write_acl "${INDEXER_ONLY_ACL[@]}"
    chmod 701 "$ROOT/config"
    printf '%s\n' 'user::rwx' 'user:1002:---' 'group::---' 'mask::---' 'other::--x' \
        >"$STUBS/diracl"
    fails_with 'is not searchable by the indexer (UID 1002)'
}

# #668: without getfacl the ACL on config/ cannot be read, and a
# named entry for UID 1002 there could deny what the other bits allow.
# A config/ UID 1002 owns is decided by its owner bits alone.
linux_config_dir_check_without_getfacl_fails() {
    stub_host Linux 1002
    setup
    write_authority 600
    chmod 701 "$ROOT/config"
    printf '4242\n' >"$STUBS/dir-owner"
    hide_command getfacl
    fails_with "getfacl is needed to check the ACL on $ROOT/config; install the acl package."
    printf '1002\n' >"$STUBS/dir-owner"
    passes
}

# #663: the indexer's primary GID is 1002, so a group entry matching it
# (the owning group, or a named group:1002 entry, each limited by the
# mask) decides search before the other bits do.
linux_config_dir_group_entries_decide() {
    stub_host Linux 4242
    setup
    write_authority 640
    write_acl "${INDEXER_ONLY_ACL[@]}"
    chmod 701 "$ROOT/config"
    printf '1002\n' >"$STUBS/dir-group"
    fails_with 'is not searchable by the indexer (UID 1002)'
    chmod 711 "$ROOT/config"
    passes
    printf '%s\n' 'user::rwx' 'group::--x' 'mask::---' 'other::--x' >"$STUBS/diracl"
    fails_with 'is not searchable by the indexer (UID 1002)'
    printf '4242\n' >"$STUBS/dir-group"
    printf '%s\n' 'user::rwx' 'group::---' 'group:1002:---' 'mask::---' 'other::--x' \
        >"$STUBS/diracl"
    fails_with 'is not searchable by the indexer (UID 1002)'
    printf '%s\n' 'user::rwx' 'group::---' 'group:1002:--x' 'mask::r--' 'other::--x' \
        >"$STUBS/diracl"
    fails_with 'is not searchable by the indexer (UID 1002)'
    printf '%s\n' 'user::rwx' 'group::---' 'group:1002:--x' 'mask::--x' 'other::---' \
        >"$STUBS/diracl"
    passes
    # One matching group entry that grants search is enough.
    printf '1002\n' >"$STUBS/dir-group"
    passes
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
        'BRIDGE_CERT_FINGERPRINT= bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb '
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
check "SYNC_DEADLINE_SECONDS accepts only a bounded positive integer" sync_deadline_values
check "an out-of-range MCP_PORT fails" out_of_range_port_fails
check "a zero-padded MCP_PORT is decimal" zero_padded_port_is_decimal
check "an unknown INFERENCE_MODE fails" unknown_inference_mode_fails
check "a removed MCP_TRANSPORT fails with migration steps" removed_transport_fails_with_migration_steps
check "an exported removed MCP_TRANSPORT names the shell export" exported_removed_transport_names_the_shell_export
check "an unknown MCP_TRANSPORT fails" unknown_transport_fails
check "a missing EMBED_MODEL fails" missing_embed_model_fails
check "an empty embed key fails" empty_embed_key_fails
check "the placeholder BRIDGE_USER fails" placeholder_bridge_user_fails
check "a missing BRIDGE_CERT_FINGERPRINT fails" missing_bridge_cert_fingerprint_fails
check "a malformed BRIDGE_CERT_FINGERPRINT fails" malformed_bridge_cert_fingerprint_fails
check "a BRIDGE_CERT_FINGERPRINT in openssl's form passes" \
    openssl_form_bridge_cert_fingerprint_passes
check "a one-sided INFERENCE_MAX_TOKENS fails" one_sided_max_tokens_fails
check "a zero-padded INFERENCE_MAX_TOKENS is decimal" zero_padded_max_tokens_is_decimal
check "an API key in .env fails" api_key_in_env_fails
check "a secret file not 600 fails" loose_secret_mode_fails
check "a missing MCP token file fails" missing_mcp_token_file_fails
check "an empty MCP token fails" empty_mcp_token_fails
check "an unusable MCP token fails without echoing it" unusable_mcp_token_fails_without_echoing_it
check "an MCP token with a NUL byte fails" mcp_token_with_nul_byte_fails
check "a usable MCP token passes" usable_mcp_token_passes
check "an MCP token file not 600 fails" loose_mcp_token_mode_fails
check "an MCP token in .env fails" mcp_token_in_env_fails
check "a removed INDEXER_UNLINK_ON_REAP fails" removed_unlink_on_reap_fails
check "an MCP token failure does not echo the token" mcp_token_failure_does_not_echo_the_value
check "an absent authority file passes" absent_authority_file_passes
check "a private authority file passes" private_authority_file_passes
check "an authority file not 600 fails" loose_authority_file_fails
check "a symlinked authority file fails" symlinked_authority_file_fails
check "a non-regular authority path fails" non_regular_authority_path_fails
check "Linux: an operator-owned 600 authority file fails with the command" \
    linux_operator_owned_authority_file_fails_with_command
check "Linux: an authority file owned by UID 1002 passes" linux_authority_file_owned_by_indexer_passes
check "Linux: an ACL granting only UID 1002 read passes" linux_indexer_only_acl_passes
check "Linux: a broader ACL fails" linux_broader_acl_fails
check "Linux: an authority file with a loose mode fails" linux_loose_authority_file_fails
check "Linux: a host account with UID 1002 fails" linux_host_account_1002_fails
check "Linux: a UID 1002 owner with a host account fails" \
    linux_owner_1002_with_host_account_fails
check "Linux: an operator holding UID 1002 passes" linux_operator_holding_uid_1002_passes
check "Linux: an account sharing the operator's UID 1002 fails" \
    linux_account_sharing_operator_uid_1002_fails
check "Linux: a missing getent fails" linux_missing_getent_fails
check "Linux: a failed lookup by UID fails" linux_failed_uid_lookup_fails
check "Linux: an operator name with a backslash passes" \
    linux_operator_name_with_backslash_passes
check "Linux: an unsearchable config directory fails" linux_unsearchable_config_dir_fails
check "Linux: the fix command quotes the path" linux_fix_command_quotes_the_path
check "Linux: a config directory ACL denying UID 1002 fails" \
    linux_config_dir_acl_denying_indexer_fails
check "Linux: the config directory check fails without getfacl" \
    linux_config_dir_check_without_getfacl_fails
check "Linux: group entries for GID 1002 on config/ decide search" \
    linux_config_dir_group_entries_decide
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
