#!/bin/bash
set -Eeuo pipefail

# Tests for bridge/patch-source.sh text patching under non-GNU sed (#617).
#
# The Docker build runs the helper with GNU sed, but `make bridge-patch-check`
# runs it on the host, which is BSD sed on macOS. GNU sed turns `\t` in a
# replacement into a tab; POSIX leaves it undefined and older BSD sed emits a
# literal `t`. Each case runs the real helper against a synthetic source tree
# with a `sed` wrapper on PATH that applies those strict semantics, and with
# `go` and `pkg-config` mocked so the compile and `go test` layers are no-ops.
# No Docker, network or Bridge source is used.
#
# Run: bash bridge/tests/patch_source_test.sh

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_SOURCE="$TESTS_DIR/../patch-source.sh"
REAL_SED="$(command -v sed)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILURES=0
TAB=$'\t'
readonly TESTS_DIR PATCH_SOURCE REAL_SED TAB

# Same harness shape as entrypoint_test.sh: each case runs in a subshell
# with errexit on, and every [[ ]] assertion ends in `|| return 1` for
# bash 3.2 (macOS /bin/bash).
check() {
    local description="$1" rc=0
    shift
    set +e
    (set -e; "$@") >"$WORK/output" 2>&1
    rc=$?
    set -e
    if ((rc == 0)); then
        printf 'ok   %s\n' "$description"
    else
        printf 'FAIL %s\n' "$description"
        "$REAL_SED" 's/^/     /' "$WORK/output"
        FAILURES=$((FAILURES + 1))
    fi
}

# Writes the five upstream patch points, indented with tabs as gofmt does,
# and mock go / pkg-config / sed executables. $2 picks the sed wrapper:
#   escape  - `\t` in an argument becomes `t` (strict POSIX / older BSD)
#   tabs    - additionally, a literal tab becomes `t`, so any tab the
#             helper inserts is lost and the post-patch guard must notice
setup() {
    SRC="$WORK/src-$1"
    BIN="$WORK/bin-$1"
    mkdir -p "$SRC/internal/constants" "$SRC/internal/certs" \
        "$SRC/internal/vault" "$SRC/internal/bridge" "$BIN"
    printf '%sHost = "127.0.0.1"\n' "$TAB" > "$SRC/internal/constants/constants.go"
    printf '%s%sIPAddresses:           []net.IP{net.ParseIP("127.0.0.1")},\n' \
        "$TAB" "$TAB" > "$SRC/internal/certs/tls.go"
    printf '%s%sAutoUpdate:        true,\n' "$TAB" "$TAB" > "$SRC/internal/vault/types_settings.go"
    printf '%sautoUpdateEnabled := bridge.vault.GetAutoUpdate()\n' "$TAB" \
        > "$SRC/internal/bridge/updates.go"
    printf 'func (vault *Vault) GetIMAPSSL() bool {\n%sreturn vault.getSafe().Settings.IMAPSSL\n}\n' \
        "$TAB" > "$SRC/internal/vault/settings.go"

    cat > "$BIN/go" <<'EOF'
#!/bin/bash
if [[ "$*" == "env GOVERSION" ]]; then
    printf 'go1.99.0\n'
fi
EOF
    printf '#!/bin/bash\nexit 0\n' > "$BIN/pkg-config"

    local tab_rule=""
    if [[ "$2" == "tabs" ]]; then
        tab_rule="arg=\"\${arg//\$'\\t'/t}\""
    fi
    cat > "$BIN/sed" <<EOF
#!/bin/bash
args=()
for arg in "\$@"; do
    arg="\${arg//\\\\t/t}"
    $tab_rule
    args+=("\$arg")
done
exec "$REAL_SED" "\${args[@]}"
EOF
    chmod +x "$BIN/go" "$BIN/pkg-config" "$BIN/sed"
}

run_patch() {
    PATH="$BIN:$PATH" bash "$PATCH_SOURCE" "$SRC"
}

strict_sed_inserts_tab_indented_san_line() {
    setup strict escape
    run_patch
    grep -F -x -q -- "${TAB}${TAB}DNSNames:    []string{\"protonmail-bridge\", \"localhost\"}," \
        "$SRC/internal/certs/tls.go" || return 1
    ! grep -q 'tDNSNames' "$SRC/internal/certs/tls.go" || return 1
}

lost_indentation_fails_the_san_guard() {
    setup mangled tabs
    local rc=0
    run_patch >"$WORK/mangled.out" 2>&1 || rc=$?
    cat "$WORK/mangled.out"
    ((rc != 0)) || return 1
    grep -q 'Patch drift detected: expected 1 match(es) for tab-indented patched TLS SAN line' \
        "$WORK/mangled.out" || return 1
}

# #638: the IMAP getter returns true whatever the vault stores, so the
# Bridge container serves IMAP with implicit TLS for old and new vaults.
imap_ssl_getter_is_forced_true() {
    setup imapssl escape
    run_patch
    grep -F -x -q -- "${TAB}return true // protonmail-local-ai: IMAP always uses implicit TLS (#638)" \
        "$SRC/internal/vault/settings.go" || return 1
    ! grep -q 'Settings.IMAPSSL' "$SRC/internal/vault/settings.go" || return 1
}

# Upstream changed the getter: the guard fails instead of skipping the hunk.
changed_imap_ssl_getter_fails_the_guard() {
    setup imapdrift escape
    printf 'func (vault *Vault) GetIMAPSSL() bool {\n%sreturn vault.get().Settings.IMAPSSL\n}\n' \
        "$TAB" > "$SRC/internal/vault/settings.go"
    local rc=0
    run_patch >"$WORK/imapdrift.out" 2>&1 || rc=$?
    cat "$WORK/imapdrift.out"
    ((rc != 0)) || return 1
    grep -q 'Patch drift detected: expected 1 match(es) for upstream IMAP SSL getter' \
        "$WORK/imapdrift.out" || return 1
}

check "strict sed escapes still insert a tab-indented SAN line (#617)" \
    strict_sed_inserts_tab_indented_san_line
check "a SAN line without its tab indentation fails the guard (#617)" \
    lost_indentation_fails_the_san_guard
check "the IMAP SSL getter is forced to implicit TLS (#638)" imap_ssl_getter_is_forced_true
check "a changed IMAP SSL getter fails the guard (#638)" changed_imap_ssl_getter_fails_the_guard

if ((FAILURES > 0)); then
    printf '%d test(s) failed\n' "$FAILURES" >&2
    exit 1
fi
printf 'all tests passed\n'
