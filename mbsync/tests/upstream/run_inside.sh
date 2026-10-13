#!/bin/bash
set -Eeuo pipefail
# Runs as the non-root harness user inside the execution container
# (--network none, all capabilities dropped, no-new-privileges). Builds
# and runs the issue #1583 harness tests with the module cache only
# (GOPROXY=off, -mod=readonly, GOTOOLCHAIN=local). Test output, including
# any upstream log text, stays in /home/h/logs inside the container; only
# fixed tokens are printed and only /home/h/out (allowlisted JSON) is
# copied out by run.sh.

export HOME=/home/h
export PATH=/usr/local/go/bin:/usr/bin:/bin
export GOTOOLCHAIN=local GOPROXY=off GOFLAGS=-mod=readonly GOSUMDB=off GOTELEMETRY=off
export GOMODCACHE=/home/h/gomodcache GOCACHE=/home/h/gocache CGO_ENABLED=1
export ISSUE1583_OUT=/home/h/out ISSUE1583_LOGS=/home/h/logs
readonly DEBUG="${1:-}"

if [[ "$(go env GOVERSION)" != "go1.26.7" ]]; then
    printf 'inside: unexpected Go version\n' >&2
    exit 1
fi

status=0

verify() {
    local mod="$1"
    if (cd "/home/h/src/$mod" && go mod download && go mod verify) >"/home/h/logs/$mod-verify.log" 2>&1; then
        printf '%s go_mod_verify=ok\n' "$mod"
    else
        printf '%s go_mod_verify=failed\n' "$mod"
        status=1
    fi
}

# Ends every process the tests left running as this user (kill -1 spares
# only this shell and PID 1). It fails with "no such process" when none is
# left, which is the usual case.
stop_leftovers() {
    kill -s KILL -1 2>/dev/null || true
}

# The test code owns /home/h/logs, so the log is never reopened by its
# path: it is written through a descriptor opened on a new file while no
# test process runs, and read back through /dev/fd, which reopens that
# same inode, after every test process has ended. A FIFO or symlink the
# tests put in its place is never opened. Each suite also builds with its
# own new, empty GOCACHE and TMPDIR, created while no test process runs and
# removed after it, so an earlier suite cannot plant build-cache entries
# or temp files that a later one uses.
run() {
    local name="$1" mod="$2" pkg="$3" timeout="$4" rc=0 log="/home/h/logs/$1.log" w r output cache tmp
    stop_leftovers
    cache="$(mktemp -d /home/h/gocache/suite.XXXXXXXX)"
    tmp="$(mktemp -d /tmp/suite.XXXXXXXX)"
    rm -f "$log"
    exec {w}>"$log"
    (cd "/home/h/src/$mod" && GOCACHE="$cache" TMPDIR="$tmp" \
        go test -v -count=1 -p 1 -timeout "$timeout" -run '^TestIssue1583' "$pkg") \
        >&"$w" 2>&1 || rc=$?
    stop_leftovers
    rm -rf "$cache" "$tmp"
    exec {r}<"/dev/fd/$w" {w}>&-
    output="$(head -c 8388608 <&"$r")"
    exec {r}<&-
    printf '%s exit=%s\n' "$name" "$rc"
    # Our test names and verdicts only.
    grep -oE -- '^--- (PASS|FAIL|SKIP): TestIssue1583[A-Za-z0-9]+' <<<"$output" || true
    if [[ "$DEBUG" == "debug" ]]; then
        # Diagnostics that point at the harness's own source lines.
        grep -E 'zz_issue1583_([a-z]+_)?test\.go:[0-9]+' <<<"$output" | head -40 || true
    fi
    if [[ "$rc" -ne 0 ]]; then
        status=1
    fi
}

verify gluon
verify proton-bridge
run gluon gluon ./tests 30m
run bridge-builder proton-bridge ./internal/services/imapservice 20m
run bridge proton-bridge ./internal/bridge 30m
exit "$status"
