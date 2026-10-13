#!/bin/bash
set -Eeuo pipefail
# =============================================================================
# Issue #1583: run pinned Gluon and Bridge code in an isolated container
# =============================================================================
# Reproduces the executed rows of docs/architecture.md "Proton identity
# evidence" (U1). Owner decision 2026-10-12 on #1583 sets the rules this
# script follows:
#
#   acquire   On the host, with network: fetch the pinned commits, the Go
#             toolchain go.mod names (checked against the SHA-256 pinned
#             here and go.dev's published one), the Go modules against
#             the unchanged go.sum (then `go mod verify`), the Debian base
#             image by the digest
#             mbsync/Dockerfile pins (checked against the publisher's
#             record in docker-library/repo-info), and the exact .debs the
#             harness needs from the snapshot the base names (InRelease
#             checked against the SHA-256 pinned here for that snapshot
#             and verified with gpgv against the base's own archive
#             keyring, Packages and each .deb against the signed SHA-256).
#             Every download is size- and time-bounded; each repo's git
#             fetch additionally writes into a GIT_IMAGE_SIZE-capped
#             sparse APFS image mounted at its source tree, so it fails
#             closed with ENOSPC rather than filling the host disk
#             (owner decision 2026-10-13, round 23 finding 4). Writes
#             provenance.txt. No container runs in this step: the keyring,
#             apt sources and dpkg status are copied out of a created,
#             never-started container.
#   assemble  With --network none: install the verified inputs into a
#             container from the verified base (root, dpkg -i), extract
#             the modules, create the non-root user (who owns only its
#             build cache, logs and results; root owns every input) and
#             commit a local image.
#   execute   With --network none, as the non-root user, all capabilities
#             dropped, no-new-privileges, memory, pids, CPU and file-size
#             limits, no container log, a wall-clock deadline, a
#             read-only root filesystem with a size-capped tmpfs for
#             every writable path (owner decision 2026-10-13, round 21
#             and 27 finding: amends the earlier no-user-mounts rule so
#             fallocate cannot reserve disk the host-side write-byte
#             sampling misses) and a cap on bytes written that the host
#             also enforces, no ports or user-added bind mounts or
#             volumes:
#             build and run the harness tests (Go test files under gluon/
#             and bridge/ here) with GOTOOLCHAIN=local,
#             GOPROXY=off and -mod=readonly. Copies out only the allowlisted
#             JSON results, checked against a committed manifest (an
#             in-process observation of what ran, not an attestation
#             against code that could tamper with its own process; round
#             23 finding 3, owner decision 2026-10-13); upstream logs
#             stay in the container.
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
# acquire and all create <work-dir>, which must not exist yet; assemble,
# execute and clean take a <work-dir> acquire created. execute and all
# create <out-dir>, which must not exist yet, and keep it only when the
# harness passes. Both are created with mode 700, under a parent only this
# account can write. <work-dir> lives outside the repository (it holds
# upstream trees, downloads and builds). Needs docker (Linux
# arm64 or amd64 engine), git, curl, jq, gpgv, xz, python3, hdiutil and
# diskutil (macOS, to bound git fetch's disk use with a sparse APFS
# image) and shasum or sha256sum on the host (clean needs docker,
# hdiutil and diskutil, to detach a mounted git image). Not part of CI
# or `make test`.
# =============================================================================

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
# Wall-clock deadline for everything that runs in the execution container
# (the three go test runs take at most 80 minutes by their own -timeout,
# which the code under test can defeat). Enforced from the host.
readonly EXEC_DEADLINE_SECS=7200
# Resource caps for the execution container, measured on full runs
# (2026-10-13: at most 3.2 cores, 1.5 GB in its writable layer and 1.23 GB
# written to disk). The container's root filesystem is read-only
# (--read-only); its only writable paths are the four tmpfs mounts
# below, each capped by EXEC_TMPFS_*_BYTES. That cap is enforced by the
# kernel at every write or allocation, fallocate included, so it cannot
# be defeated the way an overlay2 writable layer could: a keep-size
# fallocate on tmpfs is rejected with ENOSPC the instant it would pass
# the mount's size= option (owner decision 2026-10-13, round 21 and 27
# finding; measured before and after in the round's own throwaway-
# container test). The host-side watchdog's layer-size and
# bytes-written sampling (below) stays in place as a second, independent
# control; with nothing writing to the overlay layer or a block device
# any more, it now typically reads at or near zero, which is expected
# and not a sign the tmpfs cap is doing nothing.
readonly EXEC_MAX_CPUS=4
# Largest file any process in the execution container may write or
# allocate (RLIMIT_FSIZE, which also limits fallocate that grows a file).
readonly EXEC_MAX_FILE_BYTES=2147483648
readonly EXEC_MAX_WRITE_BYTES=8589934592
# tmpfs sizes for the execution container's writable paths, charged
# against its 8 GiB memory limit (tmpfs pages are anonymous memory).
# /home/h/out holds only each suite's small JSON result; /home/h/logs
# the go test -v output (read back capped at 8 MiB); /home/h/gocache
# the Go build cache, the largest of the four; /tmp the per-suite
# GOCACHE/TMPDIR scratch dirs run_inside.sh creates and removes. Sized
# generously above the measured peaks above, well under the memory
# limit so real process RSS still has headroom.
readonly EXEC_TMPFS_OUT_BYTES=67108864
readonly EXEC_TMPFS_LOGS_BYTES=536870912
readonly EXEC_TMPFS_GOCACHE_BYTES=2147483648
readonly EXEC_TMPFS_TMP_BYTES=1073741824
# Mount option strings used both for `docker run --tmpfs` and for the
# post-start assertion that the container got exactly these, so the two
# can never drift apart. /tmp must stay executable: go test builds the
# test binary under TMPDIR and execs it directly from there.
readonly EXEC_TMPFS_OUT_OPTS="rw,noexec,nosuid,nodev,size=$EXEC_TMPFS_OUT_BYTES,uid=1583,gid=1583,mode=0700"
readonly EXEC_TMPFS_LOGS_OPTS="rw,noexec,nosuid,nodev,size=$EXEC_TMPFS_LOGS_BYTES,uid=1583,gid=1583,mode=0700"
readonly EXEC_TMPFS_GOCACHE_OPTS="rw,noexec,nosuid,nodev,size=$EXEC_TMPFS_GOCACHE_BYTES,uid=1583,gid=1583,mode=0700"
# Docker adds noexec to every --tmpfs mount unless the options say
# otherwise; exec is explicit here because go test builds the test
# binary under TMPDIR and execs it directly from there (measured
# 2026-10-13: without it, that exec is refused).
readonly EXEC_TMPFS_TMP_OPTS="rw,exec,nosuid,nodev,size=$EXEC_TMPFS_TMP_BYTES,mode=1777"
readonly WATCH_INTERVAL_SECS=5
# Each Docker call the watchdog makes is cut off after this long, and a
# measurement that times out counts as unreadable.
readonly PROBE_TIMEOUT_SECS=30
# Each network step of acquire (each download through fetch, git fetch,
# the host-side go mod download, docker pull) is cut off after this long,
# with everything it started. git fetch's disk use is additionally
# bounded by GIT_IMAGE_SIZE (below); the host-side go mod download and
# docker pull have no byte bound yet (#1583 round 24, open).
readonly ACQUIRE_TIMEOUT_SECS=1800
# Size of the sparse APFS image each repo's git fetch writes into
# (owner decision 2026-10-13, round 23 finding 4): a fetch that would
# grow past it fails closed with ENOSPC, on top of ACQUIRE_TIMEOUT_SECS,
# instead of writing an unbounded amount to the host filesystem. Real
# shallow clones of both repos measured under 10 MB combined
# (2026-10-13); this leaves large headroom for upstream growth while
# staying a small, finite cap. hdiutil's -size takes a unit suffix
# (here, gigabytes), not a byte count.
readonly GIT_IMAGE_SIZE=2g
# Fixed text after the digest on provenance.txt's inputs line, which must
# match exactly.
readonly INPUTS_NOTE='(provenance lines above, staged trees with harness files, repo/Packages, Go archive, run.sh, its scripts and manifest.json, mbsync/Dockerfile)'
# Container names carry the work dir's tag (set_names), so runs in other
# work dirs never share or remove each other's containers.
ACQ=""
ASM=""
RUNC=""
WATCHDOG=""
PROBE=""
# The out dir while execute is still filling it; the cleanup paths remove
# it, so a failed run leaves no partial results behind.
OUT_PENDING=""
# The status lines of the last run_inside.sh step (run_step).
STEP_LINES=""
# Set once this invocation created the work dir (acquire) or verified its
# marker (assemble, execute); the cleanup paths touch it only then.
WORK_CLAIMED=""

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

# Downloads <url> to <dest> through a temp file, refusing more than <max>
# bytes (curl stops the transfer at the limit; the size is checked again)
# and taking at most ACQUIRE_TIMEOUT_SECS in all, retries included (curl's
# own --max-time restarts on each retry), so no response can fill the disk
# or hold acquire first.
fetch() {
    local max="$1" dest="$2" url="$3"
    [[ "$max" =~ ^[1-9][0-9]*$ ]] || die "no size limit for $url"
    if ! bounded "$ACQUIRE_TIMEOUT_SECS" curl -fsSL --retry 3 --max-filesize "$max" --max-time "$ACQUIRE_TIMEOUT_SECS" -o "$dest.part" "$url" ||
        [[ "$(wc -c <"$dest.part" | tr -d ' ')" -gt "$max" ]]; then
        rm -f "$dest.part"
        die "download failed or larger than $max bytes: $url"
    fi
    mv "$dest.part" "$dest"
}

need_tools() {
    local t
    for t in docker git curl jq gpgv xz python3 hdiutil diskutil; do
        command -v "$t" >/dev/null 2>&1 || die "missing tool: $t"
    done
}

# Path of the per-repo sparse image file (hdiutil appends the extension
# itself; this is where it lands).
git_image_file() {
    printf '%s/git-images/%s.sparseimage' "$WORK" "$1"
}

# Creates a GIT_IMAGE_SIZE-capped APFS image and mounts it at
# $WORK/src/<name>, which must be a fresh, empty directory (acquire_src
# creates it immediately before calling this, once per repo). Every
# byte git writes for that repo is then bounded by the filesystem
# itself, not only by the time bound bounded already gives git fetch.
# The image stays attached after this process exits (acquire, assemble
# and execute are separate invocations that all read $WORK/src/<name>);
# only detach_git_image, called from clean, unmounts it.
attach_git_image() {
    local name="$1" img base
    local dir="$WORK/src/$name"
    img="$(git_image_file "$name")"
    base="${img%.sparseimage}"
    mkdir -p "$WORK/git-images" "$dir"
    hdiutil create -size "$GIT_IMAGE_SIZE" -fs APFS -volname "i1583-git-$name" -type SPARSE -quiet "$base" ||
        die "$name: cannot create the bounded git image"
    hdiutil attach "$img" -mountpoint "$dir" -nobrowse -quiet ||
        die "$name: cannot attach the bounded git image"
}

# Detaches the image at $WORK/src/<name> if one is mounted there
# (a no-op otherwise, so clean works whether or not acquire reached
# this repo), and fails if anything is still mounted there afterward:
# clean must not rm -rf a live mountpoint.
detach_git_image() {
    local name="$1"
    local dir="$WORK/src/$name"
    diskutil info "$dir" >/dev/null 2>&1 || return 0
    hdiutil detach "$dir" -quiet 2>/dev/null || hdiutil detach "$dir" -force -quiet 2>/dev/null || true
    diskutil info "$dir" >/dev/null 2>&1 && die "$name: the bounded git image did not detach"
    return 0
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
        attach_git_image "$name"
        git -C "$dir" init -q
        bounded "$ACQUIRE_TIMEOUT_SECS" git -C "$dir" fetch -q --depth 1 "https://github.com/ProtonMail/$name.git" "$commit" ||
            die "$name: git fetch failed, passed $ACQUIRE_TIMEOUT_SECS s, or passed the $GIT_IMAGE_SIZE image cap"
        git -C "$dir" -c advice.detachedHead=false checkout -q FETCH_HEAD
        [[ "$(git -C "$dir" rev-parse HEAD)" == "$commit" ]] || die "$name: fetched commit differs"
        # Before anything on the host reads or writes inside the tree.
        only_regular_files
        [[ "$(sed -n 's/^toolchain //p' "$dir/go.mod")" == "$GO_VERSION" ]] || die "$name: go.mod toolchain is not $GO_VERSION"
        prov "source $name commit=$commit tree=$(git -C "$dir" rev-parse 'HEAD^{tree}') go.sum-sha256=$(sha256 "$dir/go.sum")"
    done
}

# SHA-256 of each GO_VERSION archive, as go.dev/dl published them when
# GO_VERSION was set (reviewed; the arm64 values matched across separate
# sessions). A download must match both this pin and go.dev's record, so a
# record and archive forged together in one session are refused before
# the host runs the toolchain.
go_archive_sha256() {
    case "$1" in
        go1.26.7.darwin-arm64.tar.gz) printf '020a1e8224811be75163e920bc77e0926a1390a6aeea19bdcf23f74b9d749f6d' ;; # pragma: allowlist secret
        go1.26.7.darwin-amd64.tar.gz) printf '92e8b34bff3c89ab16404c595669ac8cb004cc2f676dcbd1f5b87a6b8def3b47' ;; # pragma: allowlist secret
        go1.26.7.linux-arm64.tar.gz) printf '5a4ec883379d51ee9ce1040d5e87f8d35e20387574dd8c947feb01eabc3c1b37' ;; # pragma: allowlist secret
        go1.26.7.linux-amd64.tar.gz) printf 'ffb5f8de10c62550dfddab66b36b57030721e0a44a3218e9e1181d7b59f121ca' ;; # pragma: allowlist secret
        *) die "no pinned checksum for $1" ;;
    esac
}

acquire_go() {
    local plat file want pinned
    mkdir -p "$WORK/go-dl"
    # Limits: about 7x the 2.2 MB index and 2x the 65 MB archives.
    fetch 16777216 "$WORK/go-dl/go-dl.json" 'https://go.dev/dl/?mode=json&include=all'
    for plat in "$(host_go_platform)" "linux-$ARCH"; do
        file="$GO_VERSION.$plat.tar.gz"
        pinned="$(go_archive_sha256 "$file")"
        want="$(jq -r --arg f "$file" '.[].files[] | select(.filename == $f and .kind == "archive") | .sha256' "$WORK/go-dl/go-dl.json")"
        [[ "$want" =~ ^[0-9a-f]{64}$ ]] || die "no published checksum for $file"
        [[ "$want" == "$pinned" ]] || die "$file: go.dev's checksum differs from the one pinned in run.sh"
        fetch 134217728 "$WORK/go-dl/$file" "https://dl.google.com/go/$file"
        [[ "$(sha256 "$WORK/go-dl/$file")" == "$pinned" ]] || die "$file: checksum mismatch"
        prov "go $file sha256=$pinned (pinned in run.sh, matches go.dev/dl published)"
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
        # In this shell, not a subshell, so the cleanup paths see bounded's
        # process group (PROBE); go -C replaces cd.
        bounded "$ACQUIRE_TIMEOUT_SECS" env GOTOOLCHAIN=local GOFLAGS=-mod=readonly \
            GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org \
            GONOSUMDB='' GOPRIVATE='' GONOPROXY='' GOINSECURE='' \
            GOMODCACHE="$WORK/gomodcache" GOCACHE="$WORK/hostgocache" GOPATH="$WORK/hostgopath" \
            "$go" -C "$WORK/src/$mod" mod download || die "$mod: module download failed or passed $ACQUIRE_TIMEOUT_SECS s"
        env GOTOOLCHAIN=local GOFLAGS=-mod=readonly GOPROXY=off \
            GOMODCACHE="$WORK/gomodcache" GOCACHE="$WORK/hostgocache" GOPATH="$WORK/hostgopath" \
            "$go" -C "$WORK/src/$mod" mod verify >/dev/null || die "$mod: go mod verify failed"
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
    fetch 1048576 "$md" "https://raw.githubusercontent.com/docker-library/repo-info/$REPO_INFO_COMMIT/repos/debian/remote/$tag.md"
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
    bounded "$ACQUIRE_TIMEOUT_SECS" docker pull -q "$ref" >/dev/null || die "base image pull failed or passed $ACQUIRE_TIMEOUT_SECS s"
    got_id="$(docker image inspect --format '{{.Id}}' "$ref")"
    [[ "$got_id" == "$want_id" ]] || die "pulled base image ID differs from the publisher's"
    prov "base $ref repo-info-commit=$found platform=$platform image-id=$got_id (matches publisher)"
}

# SHA-256 of each suite's InRelease at the snapshot the pinned base names
# (reviewed; matched across separate sessions). gpgv accepts any release
# the archive keys signed, so this binds the metadata to the snapshot; a
# new base digest names a new snapshot, whose hashes are added here.
inrelease_sha256() {
    case "$1" in
        20261005T000000Z:debian/trixie) printf '0584fba32e13e0ab8285fb16c27adea1ec03a73669c18702821094fd6ca86675' ;; # pragma: allowlist secret
        20261005T000000Z:debian/trixie-updates) printf 'ef1bab53128b164c03c968625d0f2e8841e3e754ff358ab010ccc2c7dbf6124a' ;; # pragma: allowlist secret
        20261005T000000Z:debian-security/trixie-security) printf '1a25f10a8e266ba293823ac2c52325b7333e49dddbc2ef74de71f8d7c6d6b7f8' ;; # pragma: allowlist secret
        *) die "no pinned InRelease for $1; add the new snapshot's hashes to inrelease_sha256" ;;
    esac
}

# Debian packages: keyring, sources and dpkg status come out of a
# created, never-started container; metadata is authenticated with gpgv,
# then every .deb is checked against the signed index.
acquire_debs() {
    local ref snap keyring suite_spec archive suite dir base want got line size
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
        fetch 1048576 "$dir/InRelease" "$base/InRelease"
        [[ "$(sha256 "$dir/InRelease")" == "$(inrelease_sha256 "$snap:$archive/$suite")" ]] ||
            die "$archive $suite: InRelease differs from the one pinned for snapshot $snap"
        gpgv --keyring "$keyring" --output - "$dir/InRelease" >"$dir/Release" 2>"$dir/gpgv.txt" ||
            die "$archive $suite: InRelease signature not verified"
        read -r want size <<<"$(awk -v f="main/binary-$ARCH/Packages.xz" '/^SHA256:/ { s = 1; next } /^[^ ]/ { s = 0 } s && $3 == f { print $1, $2 }' "$dir/Release")"
        fetch "$size" "$dir/Packages.xz" "$base/main/binary-$ARCH/Packages.xz"
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
        fetch "$size" "$WORK/repo/$file" "https://snapshot.debian.org/archive/$archive/$snap/$file"
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
# repo/Packages, the Go archive, the scripts and manifest.json here
# (run.sh included), mbsync/Dockerfile, which pins the base, and every
# line acquire wrote to
# provenance.txt before this digest. acquire records it as the last line;
# assemble and execute refuse inputs or a provenance record that changed
# since.
inputs_digest() {
    (
        cd "$WORK"
        { grep -v '^inputs sha256=' provenance.txt || true; } | "${SHA256_CMD[@]}"
        # Every file rsync_tree copies: all but each tree's top-level .git.
        find src -type f ! -path 'src/gluon/.git/*' ! -path 'src/proton-bridge/.git/*' -print0 |
            LC_ALL=C sort -z | xargs -0 "${SHA256_CMD[@]}"
        "${SHA256_CMD[@]}" repo/Packages "go-dl/$GO_VERSION.linux-$ARCH.tar.gz"
        cd "$HERE"
        "${SHA256_CMD[@]}" run_inside.sh assemble.sh run.sh resolve_debs.py manifest.json
        cd "$REPO"
        "${SHA256_CMD[@]}" mbsync/Dockerfile
    ) | "${SHA256_CMD[@]}" | cut -d' ' -f1
}

# The digest hashes regular files only, so the staged trees may hold
# nothing else (no symlink, which tar would copy and Go would follow).
# The pinned trees have none.
only_regular_files() {
    local other
    other="$(cd "$WORK" && find src ! -type f ! -type d ! -path 'src/gluon/.git/*' ! -path 'src/proton-bridge/.git/*' -print -quit)" ||
        die "cannot list the staged trees"
    [[ -z "$other" ]] || die "the staged trees contain a symlink or special file"
}

# assemble and execute both refuse inputs (run.sh included) or acquired
# provenance lines that changed since acquire recorded them. The inputs
# line must be the record's only one and its last, exactly as acquire
# wrote it.
check_inputs() {
    only_regular_files
    [[ "$(grep -c '^inputs sha256=' "$WORK/provenance.txt")" == 1 ]] ||
        die "provenance.txt must hold exactly one inputs line; run acquire again into a new work dir"
    [[ "$(tail -n 1 "$WORK/provenance.txt")" == "inputs sha256=$(inputs_digest) $INPUTS_NOTE" ]] ||
        die "inputs changed since acquire; run acquire again into a new work dir"
}

cmd_acquire() {
    # A new directory only this account can write: no other account can
    # swap or plant files in it between the checks and the copies.
    [[ ! -e "$WORK" && ! -L "$WORK" ]] || die "work dir must not exist yet"
    local tag
    tag="$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')" || die "cannot generate the work dir tag"
    [[ "$tag" =~ ^[0-9a-f]{16}$ ]] || die "cannot generate the work dir tag"
    mkdir -m 700 "$WORK" || die "cannot create the work dir"
    WORK_CLAIMED=1
    # The marker appears whole (rename) or not at all; a work dir without
    # one is removed here, since neither a retry nor clean would take it.
    if ! { printf '%s' "$tag" >"$WORK/$MARKER.tmp" && mv "$WORK/$MARKER.tmp" "$WORK/$MARKER"; }; then
        rm -rf "$WORK"
        die "cannot write the work dir marker"
    fi
    set_names
    : >"$WORK/provenance.txt"
    acquire_src
    acquire_go
    acquire_mods
    verify_base
    acquire_debs
    stage_harness
    only_regular_files
    prov "inputs sha256=$(inputs_digest) $INPUTS_NOTE"
    printf 'acquire: ok (%s)\n' "$WORK/provenance.txt"
}

# --------------------------------------------------------------------------
cmd_assemble() {
    local stage="$WORK/stage" image prior
    set_names
    WORK_CLAIMED=1
    image="$IMAGE_REPO:$(work_tag)"
    check_inputs
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
    # Readable by every user and writable by none but the owner, whatever
    # the operator's umask: apt reads the repository as its _apt user, and
    # the harness user reads the trees and modules.
    chmod -R u+rwX,go+rX,go-w "$stage"
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
    # A re-assembly would move the tag and leave the old image dangling,
    # out of clean's reach: remove it first. A failed query is a failure,
    # not an empty answer.
    prior="$(docker image ls -q "$image")" || die "cannot list images"
    if [[ -n "$prior" ]]; then
        docker rmi "$image" >/dev/null || die "cannot remove the earlier image for this work dir"
    fi
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
    WORK_CLAIMED=1
    check_inputs
    image="$IMAGE_REPO:$(work_tag)"
    [[ "$(docker image inspect --format '{{index .Config.Labels "i1583.provenance"}}' "$image")" == "$(sha256 "$WORK/provenance.txt")" ]] ||
        die "image $image was not assembled from this work dir's provenance"
    # A new directory only this account can write, so nothing planted in
    # it (a symlink named like an output file) is followed.
    mkdir -m 700 "$out" || die "out dir must not exist yet"
    OUT_PENDING="$out"
    local cpus
    cpus="$(docker info --format '{{.NCPU}}')" || die "cannot read the engine's CPU count"
    [[ "$cpus" =~ ^[1-9][0-9]*$ ]] || die "unexpected engine CPU count"
    # Equal --memory and --memory-swap keep the container out of swap only
    # where the engine enforces swap limits.
    [[ "$(docker info --format '{{.SwapLimit}}')" == true ]] || die "the engine does not enforce swap limits"
    ((cpus <= EXEC_MAX_CPUS)) || cpus="$EXEC_MAX_CPUS"
    docker rm -f "$RUNC" >/dev/null 2>&1 || true
    docker run -d --pull=never --name "$RUNC" \
        --network none \
        --user 1583:1583 \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --memory 8g --memory-swap 8g \
        --pids-limit 2048 \
        --cpus "$cpus" \
        --ulimit "fsize=$EXEC_MAX_FILE_BYTES" \
        --log-driver none \
        --read-only \
        --tmpfs "/home/h/out:$EXEC_TMPFS_OUT_OPTS" \
        --tmpfs "/home/h/logs:$EXEC_TMPFS_LOGS_OPTS" \
        --tmpfs "/home/h/gocache:$EXEC_TMPFS_GOCACHE_OPTS" \
        --tmpfs "/tmp:$EXEC_TMPFS_TMP_OPTS" \
        "$image" >/dev/null
    [[ "$(docker inspect --format '{{json .Mounts}} {{json .NetworkSettings.Ports}} {{.HostConfig.NetworkMode}} {{.HostConfig.ReadonlyRootfs}}' "$RUNC")" == '[] {} none true' ]] ||
        die "execution container has mounts, ports or a network, or its root filesystem is not read-only"
    # The container's only writable paths: exactly these four tmpfs
    # mounts, each with the size cap that stops a fallocate-style
    # reservation (round 21 and 27). jq, not a string match, because
    # Go's JSON encoder is free to order the map's keys however it
    # likes (it happens to sort them, but nothing here depends on that).
    docker inspect --format '{{json .HostConfig.Tmpfs}}' "$RUNC" |
        jq -e --arg out "$EXEC_TMPFS_OUT_OPTS" --arg logs "$EXEC_TMPFS_LOGS_OPTS" \
            --arg gocache "$EXEC_TMPFS_GOCACHE_OPTS" --arg tmp "$EXEC_TMPFS_TMP_OPTS" \
            '. == {"/home/h/out": $out, "/home/h/logs": $logs, "/home/h/gocache": $gocache, "/tmp": $tmp}' \
            >/dev/null || die "execution container's tmpfs mounts differ from the expected set"
    # No container log: its code runs as PID 1's user and could write to
    # /proc/1/fd/1, which neither of the watchdog's measures counts.
    [[ "$(docker inspect --format '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.PidsLimit}} {{.HostConfig.LogConfig.Type}}{{range .HostConfig.Ulimits}} {{.Name}}={{.Soft}}/{{.Hard}}{{end}}' "$RUNC")" == "${cpus}000000000 8589934592 8589934592 2048 none fsize=$EXEC_MAX_FILE_BYTES/$EXEC_MAX_FILE_BYTES" ]] ||
        die "execution container lacks its CPU, memory, swap, pids or file-size limit, or has a log"
    local suite start=$SECONDS
    start_watchdog
    : >"$out/run-status.txt"
    # One run_inside.sh step at a time: each suite's result is copied out
    # and checked before the next suite starts, so no suite can plant or
    # rewrite another's result.
    # No suite runs on a module cache that failed verification.
    if run_step verify "$out" "$debug"; then
        for suite in gluon bridge-builder bridge; do
            if run_step "$suite" "$out" "$debug"; then
                accept_result "$suite" "$out" || rc=1
            else
                rc=1
            fi
        done
    else
        rc=1
    fi
    final_measure "$start"
    stop_watchdog
    docker rm -f "$RUNC" >/dev/null
    cp "$WORK/provenance.txt" "$out/provenance.txt"
    # Only a passing run keeps its out dir: a failed test's results may
    # stop part way.
    [[ "$rc" -ne 0 ]] || OUT_PENDING=""
    printf 'execute: exit=%s\n' "$rc"
    return "$rc"
}

# Kills the execution container from the host once EXEC_DEADLINE_SECS
# have passed, so a hang inside it (go test's own -timeout runs in the
# code under test) ends every docker exec into it. Every
# WATCH_INTERVAL_SECS it also measures the container's writable layer and
# its bytes written to disk, and kills it when either passes
# EXEC_MAX_WRITE_BYTES or cannot be read. Once it fires it ignores TERM
# and exits 3, whether or not the kill worked, so stop_watchdog can tell.
start_watchdog() {
    watchdog_loop &
    WATCHDOG=$!
}

# The watchdog itself, run in the background by start_watchdog. Its
# locals (s, why) are read by watchdog_term and watchdog_fire.
watchdog_loop() {
    local s="" start=$SECONDS size written why="" left
    # The probes below run in command substitutions, whose bounded groups
    # end before a TERM handler can run (bash runs it once the probe
    # returns); PROBE here only tracks watchdog_fire's kill.
    PROBE=""
    trap watchdog_term TERM
    while [[ -z "$why" ]]; do
        # The deadline is checked before each sleep and probe, and each is
        # bounded by the time left, so none can carry the run past it.
        left=$((EXEC_DEADLINE_SECS - (SECONDS - start)))
        ((left > 0)) || { why="passed its $EXEC_DEADLINE_SECS s deadline"; break; }
        sleep "$((left < WATCH_INTERVAL_SECS ? left : WATCH_INTERVAL_SECS))" &
        s=$!
        wait "$s"
        # Reaped: its PID may be reused, so the TERM handler must not kill it.
        s=""
        left=$((EXEC_DEADLINE_SECS - (SECONDS - start)))
        ((left > 0)) || { why="passed its $EXEC_DEADLINE_SECS s deadline"; break; }
        size="$(bounded "$((left < PROBE_TIMEOUT_SECS ? left : PROBE_TIMEOUT_SECS))" docker container inspect --size --format '{{.SizeRw}}' "$RUNC")" || size=""
        left=$((EXEC_DEADLINE_SECS - (SECONDS - start)))
        ((left > 0)) || { why="passed its $EXEC_DEADLINE_SECS s deadline"; break; }
        written="$(container_written_bytes "$((left < PROBE_TIMEOUT_SECS ? left : PROBE_TIMEOUT_SECS))")" || written=""
        if ((SECONDS - start >= EXEC_DEADLINE_SECS)); then
            why="passed its $EXEC_DEADLINE_SECS s deadline"
        elif [[ ! "$size" =~ ^[0-9]+$ || ! "$written" =~ ^[0-9]+$ ]]; then
            why="has a layer size or bytes-written count that cannot be read"
        elif ((size > EXEC_MAX_WRITE_BYTES || written > EXEC_MAX_WRITE_BYTES)); then
            why="wrote more than $EXEC_MAX_WRITE_BYTES bytes"
        fi
    done
    watchdog_fire
}

# Runs a command for at most <secs> seconds, then kills it with every
# process it started and fails (the host may have no timeout(1)). The
# command runs in its own process group (job control), whose id is in
# PROBE while it runs: git fetch, for one, starts helpers that would
# outlive a kill of its own pid.
bounded() {
    local secs="$1" rc=0 end
    shift
    # An absolute end time, so a slow or suspended host cannot stretch the
    # limit (SECONDS counts whole seconds since the shell started).
    end=$((SECONDS + secs))
    set -m
    "$@" &
    PROBE=$!
    set +m
    while kill -0 "$PROBE" 2>/dev/null; do
        if ((SECONDS >= end)); then
            kill -s KILL -- "-$PROBE" 2>/dev/null || true
            break
        fi
        sleep 0.1
    done
    wait "$PROBE" || rc=$?
    PROBE=""
    return "$rc"
}

# Bytes the execution container's cgroup wrote to block devices, from
# docker stats' BlockIO ("<read> / <written>" in decimal units such as
# 315MB or 1.23GB). Unlike the layer size, it counts data written to a
# file that was unlinked while open.
container_written_bytes() {
    local io limit="${1:-$PROBE_TIMEOUT_SECS}"
    io="$(bounded "$limit" docker stats --no-stream --format '{{.BlockIO}}' "$RUNC")" || return 1
    awk -v w="${io##*/ }" 'BEGIN {
        if (!match(w, /^[0-9]+(\.[0-9]+)?/)) exit 1
        n = substr(w, 1, RLENGTH)
        u = substr(w, RLENGTH + 1)
        split("B kB MB GB TB PB", units, " ")
        for (i = 1; i <= 6; i++) if (u == units[i]) { printf "%.0f\n", n * 1000 ^ (i - 1); exit 0 }
        exit 1
    }'
}

# In the watchdog (s and why are watchdog_loop's locals): reports why,
# kills the container, ignoring TERM from here on, and exits 3 whether or
# not the kill worked.
watchdog_fire() {
    trap '' TERM
    printf 'run.sh: the execution container %s; killing it\n' "$why" >&2
    bounded "$PROBE_TIMEOUT_SECS" docker kill "$RUNC" >/dev/null || true
    exit 3
}

# The watchdog's TERM handler. It ends the sleep or probe in progress
# (either may already have ended); a stop before any violation was
# observed exits 0, and one that arrives after a violation was observed
# still fires.
watchdog_term() {
    [[ -z "$s" ]] || kill "$s" 2>/dev/null || true
    [[ -z "$PROBE" ]] || kill -s KILL -- "-$PROBE" 2>/dev/null || true
    [[ -z "$why" ]] || watchdog_fire
    exit 0
}

# One last check after the suites, before the watchdog stops, of the time
# since <start> and of the bytes written: a run that ends just past the
# deadline, or a burst written just before the last suite exited, would
# otherwise fall between two samples. Past the deadline, over the cap or
# unreadable fails the run.
final_measure() {
    local start="$1" size written left
    left=$((EXEC_DEADLINE_SECS - (SECONDS - start)))
    ((left > 0)) || die "the execution passed its $EXEC_DEADLINE_SECS s deadline"
    size="$(bounded "$((left < PROBE_TIMEOUT_SECS ? left : PROBE_TIMEOUT_SECS))" docker container inspect --size --format '{{.SizeRw}}' "$RUNC")" || size=""
    left=$((EXEC_DEADLINE_SECS - (SECONDS - start)))
    ((left > 0)) || die "the execution passed its $EXEC_DEADLINE_SECS s deadline"
    written="$(container_written_bytes "$((left < PROBE_TIMEOUT_SECS ? left : PROBE_TIMEOUT_SECS))")" || written=""
    ((SECONDS - start < EXEC_DEADLINE_SECS)) || die "the execution passed its $EXEC_DEADLINE_SECS s deadline"
    [[ "$size" =~ ^[0-9]+$ && "$written" =~ ^[0-9]+$ ]] ||
        die "cannot measure the execution container's layer size or bytes written"
    ((size <= EXEC_MAX_WRITE_BYTES && written <= EXEC_MAX_WRITE_BYTES)) ||
        die "the execution container wrote more than $EXEC_MAX_WRITE_BYTES bytes"
}

# Fails when the execution container has stopped (the watchdog fired).
require_running() {
    [[ "$(docker inspect --format '{{.State.Running}}' "$RUNC")" == true ]] ||
        die "the execution container stopped (killed by the watchdog?)"
}

# Stops the watchdog (and its sleep), then fails if it had fired (any
# nonzero status, even with the container still running) or if the
# container is no longer running.
stop_watchdog() {
    local st=0
    [[ -n "$WATCHDOG" ]] || return 0
    kill "$WATCHDOG" 2>/dev/null || true
    wait "$WATCHDOG" || st=$?
    WATCHDOG=""
    ((st == 0)) || die "the watchdog fired or failed (status $st); the run is not valid"
    require_running
}

# Runs one run_inside.sh step (verify or a suite). Both streams are read,
# capped: the tests run as the same user as run_inside.sh and can write to
# either. Everything from the container is untrusted, so only fixed-shape
# status lines are shown and appended to run-status.txt (with debug, also
# the harness's own diagnostic lines, printable characters only). The
# step's status lines are left in STEP_LINES; returns the step's status.
run_step() {
    local step="$1" out="$2" debug="$3" text src=0
    text="$(docker exec "$RUNC" /home/h/run_inside.sh "$step" "$debug" 2>&1 | head -c 65536)" || src=$?
    require_running
    STEP_LINES="$(printf '%s\n' "$text" |
        grep -E '^(gluon|proton-bridge) go_mod_verify=(ok|failed)$|^[a-z-]+ exit=[0-9]+$|^--- (PASS|FAIL|SKIP): TestIssue1583[A-Za-z0-9]+$|^inside: [a-z ]+$' || true)"
    [[ -z "$STEP_LINES" ]] || printf '%s\n' "$STEP_LINES" | tee -a "$out/run-status.txt"
    if [[ "$debug" == debug ]]; then
        printf '%s\n' "$text" | grep -E 'zz_issue1583_([a-z]+_)?test\.go:[0-9]+' | LC_ALL=C tr -cd '[:print:]\n' || true
    fi
    return "$src"
}

# Copies a suite's result out and keeps it only if the suite reported
# exactly the tests manifest.json lists, all passing, and the result holds
# exactly the scenarios, keys and value types listed there, in the
# allowlisted shape
# (the one the harness's i1583Rec enforces). An empty or partial result,
# or a test that never ran, fails. Uses the suite's STEP_LINES.
# This is an in-process observation, not an attestation: it cross-checks
# what the test process reported against a fixed, committed shape, but
# code running inside that same process could in principle forge or
# empty its own results, and no check running in that process can rule
# that out by construction. Accepted as a residual, undischarged risk
# rather than further in-process mitigation (owner decision 2026-10-13,
# round 23 finding 3).
accept_result() {
    local suite="$1" out="$2" manifest="$HERE/manifest.json" tmp="$2/$1.json.tmp"
    if ! { [[ "$(printf '%s\n' "$STEP_LINES" | sed -n 's/^--- PASS: //p' | LC_ALL=C sort)" == "$(jq -r --arg s "$suite" '.[$s].tests[]' "$manifest" | LC_ALL=C sort)" ]] &&
        printf '%s\n' "$STEP_LINES" | grep -qx "$suite exit=0" &&
        ! printf '%s\n' "$STEP_LINES" | grep -qE '^--- (FAIL|SKIP): '; }; then
        printf 'run.sh: %s: the tests that passed differ from manifest.json\n' "$suite" >&2
        return 1
    fi
    fetch_result "$suite/$suite.json" "$tmp"
    jq -e 'type == "object" and all(.[];
            type == "object" and all(to_entries[];
                (.key | test("^[a-z0-9_]{1,64}$")) and
                (.value | type == "boolean" or type == "number"
                    or (type == "string" and test("^[A-Za-z_]{0,16}$")))))' \
        "$tmp" >/dev/null || die "$suite.json: not in the allowlisted result shape"
    # Every scenario, key and JSON type exactly as listed; a repeated key
    # (which the parser would collapse) fails too.
    jq -e --slurpfile m "$manifest" --arg s "$suite" '$m[0][$s].scenarios == map_values(map_values(type))' \
        "$tmp" >/dev/null || die "$suite.json: scenarios, keys or types differ from manifest.json"
    [[ "$(jq -n --stream '[inputs | select(length == 2)] | length' "$tmp")" == "$(jq '[tostream | select(length == 2)] | length' "$tmp")" ]] ||
        die "$suite.json: repeats a key"
    mv "$tmp" "$out/$suite.json"
}

# Reads one result file out of the execution container into <dest>. The
# container's code owns /home/h/out, so the entry must be a regular file,
# is opened without following a symlink and without blocking (a FIFO reads
# as empty), within 60 seconds, and may hold at most 1 MiB.
fetch_result() {
    local name="$1" dest="$2"
    docker exec "$RUNC" timeout -s KILL 60 sh -c \
        'f="/home/h/out/$1"; [ -f "$f" ] && [ ! -L "$f" ] &&
            exec dd if="$f" iflag=nofollow,nonblock,count_bytes bs=65536 count=1048577 status=none' \
        sh "$name" >"$dest" || die "$name: missing, not a regular file, or not read in time"
    [[ "$(wc -c <"$dest" | tr -d ' ')" -le 1048576 ]] || die "$name: larger than 1 MiB"
}

# Kills a running bounded job and removes a partial out dir and, when this
# invocation claimed the work dir (WORK_CLAIMED), its containers and the
# assembly stage, when a standalone acquire, assemble or execute fails or
# is interrupted, keeping the failure status.
phase_cleanup() {
    local rc=$?
    trap - EXIT
    [[ -z "$PROBE" ]] || kill -s KILL -- "-$PROBE" 2>/dev/null || true
    [[ -z "$WATCHDOG" ]] || kill "$WATCHDOG" 2>/dev/null || true
    [[ -z "$OUT_PENDING" ]] || rm -rf "$OUT_PENDING"
    if [[ -n "$WORK_CLAIMED" ]]; then
        docker rm -f "$ACQ" "$ASM" "$RUNC" >/dev/null 2>&1 || true
        rm -rf "$WORK/stage"
    fi
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
        local left
        # A failed query is a failure, not an empty answer. The work dir,
        # which holds the only copy of the tag, goes only after Docker is
        # verified clean, so a retry can still find what remains.
        left="$(docker ps -a --filter "name=^i1583-[a-z]+-$tag\$" --format '{{.Names}}')" || die "cannot list containers"
        [[ -z "$left" ]] || die "containers remain"
        left="$(docker image ls -q "$IMAGE_REPO:$tag")" || die "cannot list images"
        [[ -z "$left" ]] || die "image remains"
        detach_git_image gluon
        detach_git_image proton-bridge
        chmod -R u+w "$WORK"
        rm -rf "$WORK"
    fi
    [[ ! -e "$WORK" ]] || die "work dir remains"
    printf 'clean: containers, image and work dir removed\n'
}

# Canonical absolute path of the work dir (its parent must exist), kept
# away from the repository, its ancestors, / and $HOME.
resolve_work() {
    local parent base path
    parent="$(cd -P "$(dirname "$1")" 2>/dev/null && pwd)" || die "work dir parent does not exist"
    # The parent must be this account's and writable only by it, and every
    # ancestor up to / owned by this account or root and not writable by
    # others unless sticky: otherwise another account could rename a
    # directory on the path and put its own (with symlinks) in place.
    [[ -n "$(find "$parent" -maxdepth 0 -user "$(id -u)" ! -perm -g+w ! -perm -o+w)" ]] ||
        die "the parent of the work and out dirs must be owned by this account and writable only by it"
    local dir="$parent"
    while [[ "$dir" != "/" ]]; do
        dir="$(dirname "$dir")"
        [[ -n "$(find "$dir" -maxdepth 0 \( -user "$(id -u)" -o -user 0 \) \( \( ! -perm -g+w ! -perm -o+w \) -o -perm -1000 \))" ]] ||
            die "an ancestor of the work and out dirs is writable by another account"
    done
    base="$(basename "$1")"
    [[ "$base" != "." && "$base" != ".." && "$base" != "/" ]] || die "work dir must name a directory"
    path="${parent%/}/$base"
    # Only the parent's chain is checked above, so a symlink (to anywhere)
    # is refused rather than followed.
    [[ ! -L "$path" ]] || die "work and out dirs must not be symlinks"
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
    readonly WORK
    # A signal ends the run through exit, so the EXIT cleanup runs. It also
    # kills a bounded job, which job control keeps out of the terminal's
    # process group (Ctrl-C does not reach it).
    trap 'exit 130' INT
    trap 'exit 143' TERM
    # clean needs docker and the image tools (to detach a mounted git
    # image), but none of the other acquire-only tools, and works when
    # the engine's architecture is unsupported.
    if [[ "$verb" == clean ]]; then
        local t
        for t in docker hdiutil diskutil; do
            command -v "$t" >/dev/null 2>&1 || die "missing tool: $t"
        done
        cmd_clean
        return
    fi
    need_tools
    ARCH="$(engine_arch)"
    readonly ARCH
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
        all)
            [[ -n "${3:-}" ]] || die "all needs an out dir"
            local out
            out="$(resolve_out "$3")"
            # Clean up at exit. An earlier failure keeps its status; a
            # failed cleanup after a success exits 1. cmd_clean runs in a
            # subshell so its die cannot replace the earlier status.
            trap 'rc=$?; trap - EXIT; [[ -z "$PROBE" ]] || kill -s KILL -- "-$PROBE" 2>/dev/null || true; [[ -z "$WATCHDOG" ]] || kill "$WATCHDOG" 2>/dev/null || true; [[ -z "$OUT_PENDING" ]] || rm -rf "$OUT_PENDING"; if [[ -n "$WORK_CLAIMED" ]]; then (cmd_clean) || { [[ "$rc" -ne 0 ]] || rc=1; }; fi; exit "$rc"' EXIT
            cmd_acquire
            cmd_assemble
            cmd_execute "$out"
            ;;
        *) die "unknown command" ;;
    esac
}

main "$@"
