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
#             and asserts they are gone. `all` runs it on any failure too.
#
# acquire writes a random tag into the work dir; the image is named by it
# and labelled with the SHA-256 of provenance.txt, and execute refuses an
# image that does not match the work dir. clean deletes only a work dir
# that carries that tag.
#
# Usage: run.sh all <work-dir> <out-dir>
#        run.sh acquire|assemble <work-dir>
#        run.sh execute <work-dir> <out-dir> [debug]
#        run.sh clean <work-dir>
# <work-dir> and <out-dir> must not exist yet (run.sh creates them with
# mode 700); <work-dir> lives outside the repository (it
# holds upstream trees, downloads and builds). Needs docker (Linux
# arm64 or amd64 engine), git, curl, jq, gpgv, xz, python3 and shasum or
# sha256sum on the host. Not part of CI or `make test`.
# =============================================================================
set -Eeuo pipefail

readonly GLUON_COMMIT=7e800978ab4a9942829eb6116e33ea0278c03e91
readonly BRIDGE_COMMIT=b9c5dac1651437100c40896dacd778a0518a26f2
readonly GO_VERSION=go1.26.7
readonly DEBS_WANTED=(gcc libc6-dev pkgconf libsecret-1-dev isync)
# The docker-library/repo-info commit whose debian record lists the base
# digest mbsync/Dockerfile pins (an immutable publisher record).
readonly REPO_INFO_COMMIT=631ff15a31ac5d63781079d695ade706e65708c5
# Marks a work dir run.sh created; holds that dir's random image tag.
readonly MARKER=.i1583-work
readonly IMAGE_REPO=i1583-upstream
# Container names carry the work dir's tag (set_names), so runs in other
# work dirs never share or remove each other's containers.
ACQ=""
ASM=""
RUNC=""

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly HERE
REPO="$(cd "$HERE/../../.." && pwd)"
readonly REPO

die() {
    printf 'run.sh: %s\n' "$*" >&2
    exit 1
}

if command -v sha256sum >/dev/null 2>&1; then
    SHA256_CMD=(sha256sum)
else
    SHA256_CMD=(shasum -a 256)
fi
readonly SHA256_CMD

sha256() {
    "${SHA256_CMD[@]}" "$1" | cut -d' ' -f1
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
    local ref digest tag md want_id got_id platform found="$REPO_INFO_COMMIT"
    ref="$(base_ref)"
    digest="${ref##*@}"
    tag="${ref%@*}"
    tag="${tag#debian:}"
    mkdir -p "$WORK/base"
    md="$WORK/base/repo-info-$tag.md"
    curl -fsSL -o "$md" "https://raw.githubusercontent.com/docker-library/repo-info/$REPO_INFO_COMMIT/repos/debian/remote/$tag.md"
    grep -qF "debian@$digest" "$md" ||
        die "the pinned base digest is not in repo-info $REPO_INFO_COMMIT; update REPO_INFO_COMMIT to a publisher commit that records it"
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
    local ref snap keyring suite_spec archive suite dir base want got line
    ref="$(base_ref)"
    # Named by the work dir's tag, so a failed copy is removed by
    # phase_cleanup or clean.
    docker create --pull=never --name "$ACQ" --network none "$ref" >/dev/null
    docker cp -q "$ACQ:/usr/share/keyrings/debian-archive-keyring.pgp" "$WORK/base/keyring.pgp"
    docker cp -q "$ACQ:/etc/apt/sources.list.d/debian.sources" "$WORK/base/debian.sources"
    docker cp -q "$ACQ:/var/lib/dpkg/status" "$WORK/base/dpkg-status"
    docker rm "$ACQ" >/dev/null
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


# The work dir's random tag: names its image and containers, so an
# execution can only use the image assembled from this work dir's inputs.
work_tag() {
    local tag
    [[ -f "$WORK/$MARKER" ]] || die "not a run.sh work dir (no $MARKER)"
    tag="$(cat "$WORK/$MARKER")"
    [[ "$tag" =~ ^[0-9a-f]{16}$ ]] || die "malformed $MARKER"
    printf '%s' "$tag"
}

set_names() {
    local tag
    tag="$(work_tag)"
    ASM="i1583-assemble-$tag"
    RUNC="i1583-execute-$tag"
    ACQ="i1583-acquire-$tag"
}

# One SHA-256 over everything that decides what assemble builds, apart
# from the module downloads (which go mod verify checks against go.sum
# inside the container) and the .debs (which apt checks against
# repo/Packages): the staged source trees with the harness files,
# repo/Packages, the Go archive, the scripts here (run.sh included) and
# mbsync/Dockerfile, which pins the base. acquire records it; assemble
# refuses inputs that changed since.
inputs_digest() {
    (
        cd "$WORK"
        find src -type f ! -path '*/.git/*' -print0 | LC_ALL=C sort -z | xargs -0 "${SHA256_CMD[@]}"
        "${SHA256_CMD[@]}" repo/Packages "go-dl/$GO_VERSION.linux-$ARCH.tar.gz"
        cd "$HERE"
        "${SHA256_CMD[@]}" run_inside.sh assemble.sh run.sh resolve_debs.py
        cd "$REPO"
        "${SHA256_CMD[@]}" mbsync/Dockerfile
    ) | "${SHA256_CMD[@]}" | cut -d' ' -f1
}

cmd_acquire() {
    # A new directory only this account can write: no other account can
    # swap or plant files in it between the checks and the copies.
    [[ ! -e "$WORK" && ! -L "$WORK" ]] || die "work dir must not exist yet"
    mkdir -m 700 "$WORK" || die "cannot create the work dir"
    od -An -N8 -tx1 /dev/urandom | tr -d ' \n' >"$WORK/$MARKER"
    set_names
    : >"$WORK/provenance.txt"
    acquire_src
    acquire_go
    acquire_mods
    verify_base
    acquire_debs
    stage_harness
    prov "inputs sha256=$(inputs_digest) (staged trees with harness files, repo/Packages, Go archive, run.sh and its scripts, mbsync/Dockerfile)"
    printf 'acquire: ok (%s)\n' "$WORK/provenance.txt"
}

# --------------------------------------------------------------------------
cmd_assemble() {
    local stage="$WORK/stage" image
    set_names
    image="$IMAGE_REPO:$(work_tag)"
    [[ "$(sed -n 's/^inputs sha256=\([0-9a-f]\{64\}\) .*/\1/p' "$WORK/provenance.txt")" == "$(inputs_digest)" ]] ||
        die "inputs changed since acquire; run acquire again into a new work dir"
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
    # --pull=never: the verified base must already be local; no registry
    # request happens in this phase.
    docker create --pull=never --name "$ASM" --network none --security-opt no-new-privileges:true \
        "$(base_ref)" sleep infinity >/dev/null
    docker cp -q "$stage" "$ASM:/tmp/i1583-in"
    docker cp -q "$HERE/assemble.sh" "$ASM:/tmp/i1583-assemble.sh"
    docker start "$ASM" >/dev/null
    docker exec "$ASM" bash /tmp/i1583-assemble.sh
    docker exec "$ASM" rm -f /tmp/i1583-assemble.sh
    docker stop -t 1 "$ASM" >/dev/null
    docker commit \
        --change 'USER 1583:1583' --change 'WORKDIR /home/h' --change 'CMD ["sleep", "infinity"]' \
        --change "LABEL i1583.provenance=$(sha256 "$WORK/provenance.txt")" \
        "$ASM" "$image" >/dev/null
    docker rm "$ASM" >/dev/null
    rm -rf "$stage"
    [[ "$(docker image inspect --format '{{json .Config.Volumes}}' "$image")" == "null" ]] || die "image declares volumes"
    printf 'assemble: ok\n'
}

# Copies a source tree without its .git directory.
rsync_tree() {
    mkdir -p "$2"
    (cd "$1" && tar --exclude ./.git -cf - .) | (cd "$2" && tar -xf -)
}

# --------------------------------------------------------------------------
# Returns the harness's status; every other failure exits (errexit stays
# active: callers must not run this on the left of || or &&).
cmd_execute() {
    local out="$1" debug="${2:-}" rc=0 image
    set_names
    image="$IMAGE_REPO:$(work_tag)"
    [[ "$(docker image inspect --format '{{index .Config.Labels "i1583.provenance"}}' "$image")" == "$(sha256 "$WORK/provenance.txt")" ]] ||
        die "image $image was not assembled from this work dir's provenance"
    # A new directory only this account can write, so nothing planted in
    # it (a symlink named like an output file) is followed.
    mkdir -m 700 "$out" || die "out dir must not exist yet"
    docker rm -f "$RUNC" >/dev/null 2>&1 || true
    docker run -d --pull=never --name "$RUNC" \
        --network none \
        --user 1583:1583 \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --memory 8g --memory-swap 8g \
        --pids-limit 2048 \
        "$image" >/dev/null
    [[ "$(docker inspect --format '{{json .Mounts}} {{json .NetworkSettings.Ports}} {{.HostConfig.NetworkMode}}' "$RUNC")" == '[] {} none' ]] ||
        die "execution container has mounts, ports or a network"
    docker exec "$RUNC" /home/h/run_inside.sh "$debug" | tee "$out/run-status.txt" || rc=$?
    docker cp -q "$RUNC:/home/h/out/." "$out/"
    docker rm -f "$RUNC" >/dev/null
    cp "$WORK/provenance.txt" "$out/provenance.txt"
    printf 'execute: exit=%s\n' "$rc"
    return "$rc"
}

# Removes this work dir's containers (and the assembly stage) when a
# standalone acquire, assemble or execute fails, keeping the failure
# status.
phase_cleanup() {
    local rc=$?
    trap - EXIT
    if [[ -n "$ASM" ]]; then
        docker rm -f "$ACQ" "$ASM" "$RUNC" >/dev/null 2>&1 || true
    fi
    rm -rf "$WORK/stage"
    exit "$rc"
}

# --------------------------------------------------------------------------
# Deletes only a work dir run.sh created (it holds the marker), with its
# own containers and image.
cmd_clean() {
    local tag=""
    if [[ -e "$WORK" ]]; then
        set_names
        tag="$(work_tag)"
        docker rm -f "$ACQ" "$ASM" "$RUNC" >/dev/null 2>&1 || true
        docker rmi "$IMAGE_REPO:$tag" >/dev/null 2>&1 || true
        chmod -R u+w "$WORK"
        rm -rf "$WORK"
        local left
        # A failed query is a failure, not an empty answer.
        left="$(docker ps -a --filter "name=^i1583-[a-z]+-$tag\$" --format '{{.Names}}')" || die "cannot list containers"
        [[ -z "$left" ]] || die "containers remain"
        left="$(docker image ls -q "$IMAGE_REPO:$tag")" || die "cannot list images"
        [[ -z "$left" ]] || die "image remains"
    fi
    [[ ! -e "$WORK" ]] || die "work dir remains"
    printf 'clean: containers, image and work dir removed\n'
}

# Canonical absolute path of the work dir (its parent must exist), kept
# away from the repository, its ancestors, / and $HOME.
resolve_work() {
    local parent base path
    parent="$(cd -P "$(dirname "$1")" 2>/dev/null && pwd)" || die "work dir parent does not exist"
    base="$(basename "$1")"
    [[ "$base" != "." && "$base" != ".." && "$base" != "/" ]] || die "work dir must name a directory"
    path="${parent%/}/$base"
    if [[ -e "$path" ]]; then
        path="$(cd -P "$path" && pwd)"
    fi
    case "$path" in
        / | "$HOME" | "$REPO" | "$REPO"/*) die "work dir must be outside the repository, / and \$HOME" ;;
    esac
    case "$REPO/" in
        "$path"/*) die "work dir must not contain the repository" ;;
    esac
    printf '%s' "$path"
}

# The out dir, canonical and outside the work dir that clean deletes.
resolve_out() {
    local out
    out="$(resolve_work "$1")"
    case "$out" in
        "$WORK" | "$WORK"/*) die "out dir must be outside the work dir" ;;
    esac
    [[ ! -e "$out" && ! -L "$out" ]] || die "out dir must not exist yet"
    printf '%s' "$out"
}

# --------------------------------------------------------------------------
main() {
    local verb="${1:-}"
    [[ -n "$verb" && -n "${2:-}" ]] || die "usage: run.sh all|acquire|assemble|execute|clean <work-dir> [out-dir]"
    WORK="$(resolve_work "$2")"
    need_tools
    ARCH="$(engine_arch)"
    readonly WORK ARCH
    case "$verb" in
        acquire)
            trap phase_cleanup EXIT
            cmd_acquire
            trap - EXIT
            ;;
        assemble)
            trap phase_cleanup EXIT
            cmd_assemble
            trap - EXIT
            ;;
        execute)
            [[ -n "${3:-}" ]] || die "execute needs an out dir"
            trap phase_cleanup EXIT
            cmd_execute "$(resolve_out "$3")" "${4:-}"
            ;;
        clean) cmd_clean ;;
        all)
            [[ -n "${3:-}" ]] || die "all needs an out dir"
            local out
            out="$(resolve_out "$3")"
            # Clean up after any failure from here on, keeping its status.
            trap 'rc=$?; trap - EXIT; cmd_clean || true; exit "$rc"' EXIT
            cmd_acquire
            cmd_assemble
            cmd_execute "$out"
            ;;
        *) die "unknown command" ;;
    esac
}

main "$@"
