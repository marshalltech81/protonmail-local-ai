#!/bin/bash
# =============================================================================
# Issue #1583: run pinned Gluon and Bridge code in an isolated container
# =============================================================================
# Reproduces the executed rows of docs/architecture.md "Proton identity
# evidence" (U1). Owner decision 2026-10-12 on #1583 sets the rules this
# script follows:
#
#   acquire   On the host, with network: fetch the pinned commits, the Go
#             toolchain go.mod names (checked against go.dev's published
#             SHA-256), the Go modules against the unchanged go.sum (then
#             `go mod verify`), the Debian base image by the digest
#             mbsync/Dockerfile pins (checked against the publisher's
#             record in docker-library/repo-info), and the exact .debs the
#             harness needs from the snapshot the base names (InRelease
#             verified with gpgv against the base's own archive keyring,
#             Packages and each .deb against the signed SHA-256). Writes
#             provenance.txt. No container runs in this step: the keyring,
#             apt sources and dpkg status are copied out of a created,
#             never-started container.
#   assemble  With --network none: install the verified inputs into a
#             container from the verified base (root, dpkg -i), create the
#             non-root user and commit a local image.
#   execute   With --network none, as the non-root user, all capabilities
#             dropped, no-new-privileges, memory and pids limits, no ports,
#             mounts or volumes: build and run the harness tests (Go test
#             files under gluon/ and bridge/ here) with GOTOOLCHAIN=local,
#             GOPROXY=off and -mod=readonly. Copies out only the allowlisted
#             JSON results; upstream logs stay in the container.
#   clean     Removes the containers, the image and the work directory,
#             and asserts they are gone.
#
# Usage: run.sh all <work-dir> <out-dir>
#        run.sh acquire|assemble <work-dir>
#        run.sh execute <work-dir> <out-dir> [debug]
#        run.sh clean <work-dir>
# <work-dir> must be new or empty and lives outside the repository (it
# holds upstream trees, downloads and builds). Needs docker (Linux
# arm64 or amd64 engine), git, curl, jq, gpgv, xz, python3 and shasum or
# sha256sum on the host. Not part of CI or `make test`.
# =============================================================================
set -Eeuo pipefail

readonly GLUON_COMMIT=7e800978ab4a9942829eb6116e33ea0278c03e91
readonly BRIDGE_COMMIT=b9c5dac1651437100c40896dacd778a0518a26f2
readonly GO_VERSION=go1.26.7
readonly DEBS_WANTED=(gcc libc6-dev pkgconf libsecret-1-dev isync)
readonly IMAGE=i1583-upstream:local
readonly ASM=i1583-assemble
readonly RUNC=i1583-execute

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly HERE
REPO="$(cd "$HERE/../../.." && pwd)"
readonly REPO

die() {
    printf 'run.sh: %s\n' "$*" >&2
    exit 1
}

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

prov() {
    printf '%s\n' "$*" >>"$WORK/provenance.txt"
}

need_tools() {
    local t
    for t in docker git curl jq gpgv xz python3; do
        command -v "$t" >/dev/null 2>&1 || die "missing tool: $t"
    done
}

engine_arch() {
    case "$(docker info --format '{{.Architecture}}')" in
        aarch64 | arm64) printf 'arm64' ;;
        x86_64 | amd64) printf 'amd64' ;;
        *) die "unsupported engine architecture" ;;
    esac
}

host_go_platform() {
    local os arch
    os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    case "$(uname -m)" in
        arm64 | aarch64) arch=arm64 ;;
        x86_64 | amd64) arch=amd64 ;;
        *) die "unsupported host architecture" ;;
    esac
    printf '%s-%s' "$os" "$arch"
}

base_ref() {
    local ref
    ref="$(sed -n 's/^FROM \(debian:[^ ]*@sha256:[0-9a-f]\{64\}\).*/\1/p' "$REPO/mbsync/Dockerfile" | head -1)"
    [[ -n "$ref" ]] || die "no pinned debian base in mbsync/Dockerfile"
    printf '%s' "$ref"
}

# --------------------------------------------------------------------------
acquire_src() {
    local name commit dir spec
    mkdir -p "$WORK/src"
    for spec in "gluon:$GLUON_COMMIT" "proton-bridge:$BRIDGE_COMMIT"; do
        name="${spec%%:*}"
        commit="${spec#*:}"
        dir="$WORK/src/$name"
        mkdir -p "$dir"
        git -C "$dir" init -q
        git -C "$dir" fetch -q --depth 1 "https://github.com/ProtonMail/$name.git" "$commit"
        git -C "$dir" -c advice.detachedHead=false checkout -q FETCH_HEAD
        [[ "$(git -C "$dir" rev-parse HEAD)" == "$commit" ]] || die "$name: fetched commit differs"
        [[ "$(sed -n 's/^toolchain //p' "$dir/go.mod")" == "$GO_VERSION" ]] || die "$name: go.mod toolchain is not $GO_VERSION"
        prov "source $name commit=$commit tree=$(git -C "$dir" rev-parse 'HEAD^{tree}') go.sum-sha256=$(sha256 "$dir/go.sum")"
    done
}

acquire_go() {
    local plat file want
    mkdir -p "$WORK/go-dl"
    curl -fsSL -o "$WORK/go-dl/go-dl.json" 'https://go.dev/dl/?mode=json&include=all'
    for plat in "$(host_go_platform)" "linux-$ARCH"; do
        file="$GO_VERSION.$plat.tar.gz"
        want="$(jq -r --arg f "$file" '.[].files[] | select(.filename == $f and .kind == "archive") | .sha256' "$WORK/go-dl/go-dl.json")"
        [[ "$want" =~ ^[0-9a-f]{64}$ ]] || die "no published checksum for $file"
        curl -fsSL -o "$WORK/go-dl/$file" "https://dl.google.com/go/$file"
        [[ "$(sha256 "$WORK/go-dl/$file")" == "$want" ]] || die "$file: checksum mismatch"
        prov "go $file sha256=$want (go.dev/dl published)"
    done
    mkdir -p "$WORK/hostgo"
    tar -xzf "$WORK/go-dl/$GO_VERSION.$(host_go_platform).tar.gz" -C "$WORK/hostgo"
}

# Modules: downloaded by the verified host toolchain only (no build, no
# code run), against each module's unchanged go.sum, then verified.
acquire_mods() {
    local go="$WORK/hostgo/go/bin/go" mod before
    for mod in gluon proton-bridge; do
        before="$(sha256 "$WORK/src/$mod/go.sum")$(sha256 "$WORK/src/$mod/go.mod")"
        (cd "$WORK/src/$mod" &&
            env GOTOOLCHAIN=local GOFLAGS=-mod=readonly GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org \
                GONOSUMDB= GOPRIVATE= GONOPROXY= GOINSECURE= \
                GOMODCACHE="$WORK/gomodcache" GOCACHE="$WORK/hostgocache" GOPATH="$WORK/hostgopath" \
                "$go" mod download &&
            env GOTOOLCHAIN=local GOFLAGS=-mod=readonly GOPROXY=off \
                GOMODCACHE="$WORK/gomodcache" GOCACHE="$WORK/hostgocache" GOPATH="$WORK/hostgopath" \
                "$go" mod verify >/dev/null) || die "$mod: module download or verify failed"
        [[ "$before" == "$(sha256 "$WORK/src/$mod/go.sum")$(sha256 "$WORK/src/$mod/go.mod")" ]] ||
            die "$mod: go.sum or go.mod changed"
        prov "modules $mod go-mod-download+verify=ok go.sum unchanged (sum.golang.org)"
    done
}

# The pinned base digest must be one the publisher (Docker Official
# Images) recorded for the tag, and the pulled image's ID must be the one
# the publisher lists for this architecture.
verify_base() {
    local ref digest tag commits c md want_id got_id platform found=""
    ref="$(base_ref)"
    digest="${ref##*@}"
    tag="${ref%@*}"
    tag="${tag#debian:}"
    mkdir -p "$WORK/base"
    commits="$(curl -fsSL "https://api.github.com/repos/docker-library/repo-info/commits?path=repos/debian/remote/$tag.md&per_page=30" | jq -r '.[].sha')"
    for c in $commits; do
        md="$WORK/base/repo-info-$tag.md"
        curl -fsSL -o "$md" "https://raw.githubusercontent.com/docker-library/repo-info/$c/repos/debian/remote/$tag.md"
        if grep -qF "debian@$digest" "$md"; then
            found="$c"
            break
        fi
    done
    [[ -n "$found" ]] || die "pinned base digest not in the publisher's recent records"
    case "$ARCH" in
        arm64) platform='linux; arm64 variant v8' ;;
        amd64) platform='linux; amd64' ;;
    esac
    want_id="$(awk -v p="$platform" '
        index($0, "### `debian:") == 1 && index($0, "` - " p) > 0 { f = 1; next }
        index($0, "### ") == 1 { f = 0 }
        f && /Image ID:/ && match($0, /sha256:[0-9a-f]+/) { print substr($0, RSTART, RLENGTH); exit }
    ' "$md")"
    [[ "$want_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "no publisher image ID for $platform"
    docker pull -q "$ref" >/dev/null
    got_id="$(docker image inspect --format '{{.Id}}' "$ref")"
    [[ "$got_id" == "$want_id" ]] || die "pulled base image ID differs from the publisher's"
    prov "base $ref repo-info-commit=$found platform=$platform image-id=$got_id (matches publisher)"
}

# Debian packages: keyring, sources and dpkg status come out of a
# created, never-started container; metadata is authenticated with gpgv,
# then every .deb is checked against the signed index.
acquire_debs() {
    local ref cid snap keyring suite_spec archive suite dir base want got line
    ref="$(base_ref)"
    cid="$(docker create --network none "$ref")"
    docker cp -q "$cid:/usr/share/keyrings/debian-archive-keyring.pgp" "$WORK/base/keyring.pgp"
    docker cp -q "$cid:/etc/apt/sources.list.d/debian.sources" "$WORK/base/debian.sources"
    docker cp -q "$cid:/var/lib/dpkg/status" "$WORK/base/dpkg-status"
    docker rm "$cid" >/dev/null
    snap="$(sed -n 's|^# http://snapshot.debian.org/archive/debian/\([0-9T]*Z\)$|\1|p' "$WORK/base/debian.sources" | head -1)"
    [[ "$snap" =~ ^[0-9]{8}T[0-9]{6}Z$ ]] || die "base names no snapshot"
    keyring="$WORK/base/keyring.pgp"
    prov "apt snapshot=$snap keyring-sha256=$(sha256 "$keyring") (from the base image)"
    mkdir -p "$WORK/apt" "$WORK/repo"
    local suite_dirs=()
    for suite_spec in debian:trixie debian:trixie-updates debian-security:trixie-security; do
        archive="${suite_spec%%:*}"
        suite="${suite_spec#*:}"
        dir="$WORK/apt/$archive-$suite"
        base="https://snapshot.debian.org/archive/$archive/$snap/dists/$suite"
        mkdir -p "$dir"
        curl -fsSL --retry 3 -o "$dir/InRelease" "$base/InRelease"
        gpgv --keyring "$keyring" --output - "$dir/InRelease" >"$dir/Release" 2>"$dir/gpgv.txt" ||
            die "$archive $suite: InRelease signature not verified"
        curl -fsSL --retry 3 -o "$dir/Packages.xz" "$base/main/binary-$ARCH/Packages.xz"
        want="$(awk -v f="main/binary-$ARCH/Packages.xz" '/^SHA256:/ { s = 1; next } /^[^ ]/ { s = 0 } s && $3 == f { print $1 }' "$dir/Release")"
        got="$(sha256 "$dir/Packages.xz")"
        [[ -n "$want" && "$want" == "$got" ]] || die "$archive $suite: Packages.xz not in the signed index"
        xz -dc "$dir/Packages.xz" >"$dir/Packages"
        prov "apt $archive/$suite InRelease-sha256=$(sha256 "$dir/InRelease") signed-by=$(grep -oE 'using [A-Z]+ key [0-9A-F]+' "$dir/gpgv.txt" | awk '{print $4}' | paste -sd, -) Packages.xz-sha256=$got"
        suite_dirs+=("$dir")
    done
    # The chosen stanzas, verbatim, become the Packages file of a flat
    # local repository the assembly container installs from; each .deb
    # sits at its index Filename.
    python3 -I "$HERE/resolve_debs.py" "$WORK/base/dpkg-status" "$ARCH" "$WORK/repo/Packages" \
        "${suite_dirs[@]}" -- "${DEBS_WANTED[@]}" >"$WORK/debs.txt"
    while read -r line; do
        read -r _ archive file want size name version <<<"$line"
        [[ "$file" =~ ^pool/[A-Za-z0-9._+~/-]+\.deb$ ]] || die "$name: unexpected index Filename"
        mkdir -p "$WORK/repo/$(dirname "$file")"
        curl -fsSL --retry 3 -o "$WORK/repo/$file" "https://snapshot.debian.org/archive/$archive/$snap/$file"
        [[ "$(sha256 "$WORK/repo/$file")" == "$want" ]] || die "$name: .deb checksum mismatch"
        [[ "$(wc -c <"$WORK/repo/$file" | tr -d ' ')" == "$size" ]] || die "$name: .deb size mismatch"
        prov "deb $name=$version sha256=$want"
    done <"$WORK/debs.txt"
}

# Offline check that every package the harness builds resolves from the
# module cache, with the harness files in place.
stage_harness() {
    local go="$WORK/hostgo/go/bin/go"
    cp "$HERE/gluon/zz_issue1583_test.go" "$WORK/src/gluon/tests/"
    cp "$HERE/bridge/zz_issue1583_bridge_test.go" "$WORK/src/proton-bridge/internal/bridge/"
    cp "$HERE/bridge/zz_issue1583_build_test.go" "$WORK/src/proton-bridge/internal/services/imapservice/"
    for spec in gluon:./tests proton-bridge:./internal/bridge proton-bridge:./internal/services/imapservice; do
        (cd "$WORK/src/${spec%%:*}" &&
            env GOTOOLCHAIN=local GOFLAGS=-mod=readonly GOPROXY=off GOOS=linux GOARCH="$ARCH" CGO_ENABLED=1 \
                GOMODCACHE="$WORK/gomodcache" GOCACHE="$WORK/hostgocache" GOPATH="$WORK/hostgopath" \
                "$go" list -deps -test "${spec#*:}" >/dev/null) || die "${spec%%:*}: packages do not resolve offline"
    done
}

cmd_acquire() {
    [[ ! -e "$WORK" || -z "$(find "$WORK" -mindepth 1 -maxdepth 1 -print -quit)" ]] || die "work dir not empty"
    mkdir -p "$WORK"
    : >"$WORK/provenance.txt"
    acquire_src
    acquire_go
    acquire_mods
    verify_base
    acquire_debs
    stage_harness
    printf 'acquire: ok (%s)\n' "$WORK/provenance.txt"
}

# --------------------------------------------------------------------------
cmd_assemble() {
    local stage="$WORK/stage"
    rm -rf "$stage"
    mkdir -p "$stage"
    cp -R "$WORK/repo" "$stage/repo"
    printf '%s\n' "${DEBS_WANTED[@]}" >"$stage/wanted.txt"
    cp "$WORK/go-dl/$GO_VERSION.linux-$ARCH.tar.gz" "$stage/go.tar.gz"
    mkdir -p "$stage/src"
    rsync_tree "$WORK/src/gluon" "$stage/src/gluon"
    rsync_tree "$WORK/src/proton-bridge" "$stage/src/proton-bridge"
    cp -R "$WORK/gomodcache/cache/download" "$stage/download"
    cp "$HERE/run_inside.sh" "$stage/run_inside.sh"
    docker rm -f "$ASM" >/dev/null 2>&1 || true
    docker create --name "$ASM" --network none --security-opt no-new-privileges:true "$(base_ref)" sleep infinity >/dev/null
    docker cp -q "$stage" "$ASM:/tmp/i1583-in"
    docker cp -q "$HERE/assemble.sh" "$ASM:/tmp/i1583-assemble.sh"
    docker start "$ASM" >/dev/null
    docker exec "$ASM" bash /tmp/i1583-assemble.sh
    docker exec "$ASM" rm -f /tmp/i1583-assemble.sh
    docker stop -t 1 "$ASM" >/dev/null
    docker commit --change 'USER 1583:1583' --change 'WORKDIR /home/h' --change 'CMD ["sleep", "infinity"]' "$ASM" "$IMAGE" >/dev/null
    docker rm "$ASM" >/dev/null
    rm -rf "$stage"
    [[ "$(docker image inspect --format '{{json .Config.Volumes}}' "$IMAGE")" == "null" ]] || die "image declares volumes"
    printf 'assemble: ok\n'
}

# Copies a source tree without its .git directory.
rsync_tree() {
    mkdir -p "$2"
    (cd "$1" && tar --exclude ./.git -cf - .) | (cd "$2" && tar -xf -)
}

# --------------------------------------------------------------------------
cmd_execute() {
    local out="$1" debug="${2:-}" rc=0
    mkdir -p "$out"
    docker rm -f "$RUNC" >/dev/null 2>&1 || true
    docker run -d --name "$RUNC" \
        --network none \
        --user 1583:1583 \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --memory 8g --memory-swap 8g \
        --pids-limit 2048 \
        "$IMAGE" >/dev/null
    [[ "$(docker inspect --format '{{json .Mounts}} {{json .NetworkSettings.Ports}} {{.HostConfig.NetworkMode}}' "$RUNC")" == '[] {} none' ]] ||
        die "execution container has mounts, ports or a network"
    docker exec "$RUNC" /home/h/run_inside.sh "$debug" | tee "$out/run-status.txt" || rc=$?
    docker cp -q "$RUNC:/home/h/out/." "$out/"
    docker rm -f "$RUNC" >/dev/null
    cp "$WORK/provenance.txt" "$out/provenance.txt"
    printf 'execute: exit=%s\n' "$rc"
    return "$rc"
}

# --------------------------------------------------------------------------
cmd_clean() {
    docker rm -f "$ASM" "$RUNC" >/dev/null 2>&1 || true
    docker rmi "$IMAGE" >/dev/null 2>&1 || true
    if [[ -d "$WORK" ]]; then
        chmod -R u+w "$WORK"
        rm -rf "$WORK"
    fi
    [[ -z "$(docker ps -a --filter "name=^i1583-" --format '{{.Names}}')" ]] || die "containers remain"
    [[ -z "$(docker image ls -q "$IMAGE")" ]] || die "image remains"
    [[ ! -e "$WORK" ]] || die "work dir remains"
    printf 'clean: containers, image and work dir removed\n'
}

# --------------------------------------------------------------------------
main() {
    local verb="${1:-}"
    WORK="${2:-}"
    [[ -n "$verb" && -n "$WORK" ]] || die "usage: run.sh all|acquire|assemble|execute|clean <work-dir> [out-dir]"
    case "$WORK" in
        "$REPO" | "$REPO"/*) die "work dir must be outside the repository" ;;
    esac
    need_tools
    ARCH="$(engine_arch)"
    readonly WORK ARCH
    case "$verb" in
        acquire) cmd_acquire ;;
        assemble) cmd_assemble ;;
        execute) cmd_execute "${3:?out dir}" "${4:-}" ;;
        clean) cmd_clean ;;
        all)
            local rc=0
            cmd_acquire
            cmd_assemble
            cmd_execute "${3:?out dir}" || rc=$?
            cmd_clean
            return "$rc"
            ;;
        *) die "unknown command" ;;
    esac
}

main "$@"
