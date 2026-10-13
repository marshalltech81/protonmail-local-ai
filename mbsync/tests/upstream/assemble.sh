#!/bin/bash
set -Eeuo pipefail
# Runs as root inside the assembly container, which run.sh creates from
# the verified Debian base with --network none. Installs the .debs and Go
# toolchain that run.sh verified on the host, extracts the module
# downloads and creates the non-root harness user. Nothing here reaches
# the network.

readonly IN=/tmp/i1583-in
readonly HOME_DIR=/home/h

# Install from a flat local repository whose Packages file holds the
# index stanzas run.sh authenticated on the host, verbatim: apt checks
# each .deb against them and orders the (Pre-)Depends. The base image's
# own sources are not read, and the container has no network.
printf 'deb [trusted=yes] file:%s/repo ./\n' "$IN" >"$IN/local.list"
APT_OPTS=(-o "Dir::Etc::SourceList=$IN/local.list" -o "Dir::Etc::SourceParts=/nonexistent")
mapfile -t WANTED <"$IN/wanted.txt"
if ! { apt-get "${APT_OPTS[@]}" update &&
    DEBIAN_FRONTEND=noninteractive apt-get "${APT_OPTS[@]}" install -y --no-install-recommends "${WANTED[@]}"; } \
    >/tmp/i1583-dpkg.log 2>&1; then
    printf 'assemble: apt-get install from the local repository failed\n' >&2
    grep -E '^(E:|W:|dpkg: error)|Depends:' /tmp/i1583-dpkg.log >&2 || true
    exit 1
fi
if grep -qE '^(Get|Err|Ign|Hit):[0-9]+ https?:' /tmp/i1583-dpkg.log; then
    printf 'assemble: apt reached for a network source\n' >&2
    exit 1
fi
rm -rf /var/lib/apt/lists/*
if [[ -n "$(dpkg --audit 2>&1)" ]]; then
    printf 'assemble: dpkg --audit reports unconfigured packages\n' >&2
    exit 1
fi

tar -xzf "$IN/go.tar.gz" -C /usr/local

groupadd --gid 1583 h
useradd --uid 1583 --gid 1583 --home-dir "$HOME_DIR" --no-create-home --shell /usr/sbin/nologin h
# Root owns the home directory and every authenticated input in it (the
# trees with the harness files, the module cache and run_inside.sh), so
# code under test, which runs as h, cannot change them before or between
# the test runs. h owns only its build cache, logs and results.
install -d -m 0755 -o 0 -g 0 "$HOME_DIR" "$HOME_DIR/gomodcache" "$HOME_DIR/gomodcache/cache"
install -d -m 0700 -o 1583 -g 1583 "$HOME_DIR/out" "$HOME_DIR/logs" "$HOME_DIR/gocache"
mv "$IN/src" "$HOME_DIR/src"
mv "$IN/download" "$HOME_DIR/gomodcache/cache/download"
install -m 0755 -o 0 -g 0 "$IN/run_inside.sh" "$HOME_DIR/run_inside.sh"
chown -R 0:0 "$HOME_DIR/src" "$HOME_DIR/gomodcache"
chmod -R go-w "$HOME_DIR/src" "$HOME_DIR/gomodcache"
# Extract every module from the verified downloads now, offline, so the
# cache is complete before any code runs (go checks each zip against
# go.sum; extracted modules are read-only).
for mod in gluon proton-bridge; do
    if ! (cd "$HOME_DIR/src/$mod" &&
        env HOME=/tmp GOTOOLCHAIN=local GOPROXY=off GOFLAGS=-mod=readonly GOSUMDB=off GOTELEMETRY=off \
            GOMODCACHE="$HOME_DIR/gomodcache" GOCACHE=/tmp/i1583-gocache \
            /usr/local/go/bin/go mod download) >/tmp/i1583-mod.log 2>&1; then
        printf 'assemble: %s: module extraction failed\n' "$mod" >&2
        exit 1
    fi
done
rm -rf "$IN" /tmp/i1583-dpkg.log /tmp/i1583-mod.log /tmp/i1583-gocache
printf 'assemble: ok\n'
