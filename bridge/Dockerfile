# =============================================================================
# ProtonBridge — built from official Proton source, no GUI
# Runs as non-root user 'bridge' for security
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder
# -----------------------------------------------------------------------------
FROM golang:1.26.2-bookworm AS builder

# Use bash with pipefail for all RUN steps so pipe failures are not silently ignored.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG BRIDGE_VERSION=v3.24.1

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    make \
    gcc \
    pkg-config \
    libsecret-1-dev \
    libfido2-dev \
    libcbor-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Harden the Go toolchain and CGO compiler/linker flags for all build steps.
# GOTOOLCHAIN=local prevents the builder from auto-downloading a different Go
# version if go.mod requests a newer toolchain — the pinned base image is the
# source of truth and must not be silently overridden at build time.
# CGO_CFLAGS:
#   -D_FORTIFY_SOURCE=2       runtime buffer-overflow detection in glibc functions
#   -fstack-protector-strong  stack canaries on functions with buffers or
#                             address-taken locals
#   -fPIE                     position-independent code required for -buildmode=pie
# CGO_LDFLAGS:
#   -Wl,-z,relro              GOT is remapped read-only after startup (partial RELRO)
#   -Wl,-z,now                all symbols resolved at load time (BIND_NOW),
#                             removing the late-binding attack surface
ENV GOTOOLCHAIN=local \
    CGO_CFLAGS="-D_FORTIFY_SOURCE=2 -fstack-protector-strong -fPIE" \
    CGO_LDFLAGS="-Wl,-z,relro,-z,now"

COPY --chmod=755 patch-source.sh /usr/local/bin/patch-source.sh

WORKDIR /build

RUN git clone --depth 1 --branch ${BRIDGE_VERSION} \
    https://github.com/ProtonMail/proton-bridge.git .

# Download and verify all Go modules against go.sum before building.
# go mod download pre-populates the module cache; go mod verify then confirms
# every cached module matches its recorded checksum, failing the build if any
# module has been tampered with or corrupted.
RUN go mod download && go mod verify

# Patch Bridge to bind on the container network and to include the Docker
# hostname in the generated TLS certificate. The helper fails closed if the
# upstream source moves and the expected patch points drift.
RUN /usr/local/bin/patch-source.sh /build

# Build without Qt/GUI.
# All flags are injected via GOFLAGS so they apply to every go build invocation
# in the upstream Makefile without patching it.
#
# -trimpath          strips host filesystem paths embedded in the binary.
# -ldflags=-s        omits the symbol table (function names, variable names).
# -ldflags=-w        omits DWARF debug information.
# -buildmode=pie     produces a Position Independent Executable so ASLR is
#                    effective at runtime; requires -fPIE in CGO_CFLAGS above.
#
# WARNING: if make build-nogui passes its own -ldflags or -buildmode on the
# command line, Go uses the command-line value and silently ignores GOFLAGS
# (last flag wins). The verification step below will catch that regression.
RUN GOFLAGS='-trimpath "-ldflags=-s -w" -buildmode=pie' make build-nogui

# Verify the binary was built with all required hardening properties.
# Fails the build immediately if any check is not met so regressions are caught
# in CI rather than discovered post-ship.
#
# PIE:       -buildmode=pie was honoured — binary loads at random addresses (ASLR).
# BIND_NOW:  -Wl,-z,now was applied — no lazy symbol resolution attack surface.
# GNU_RELRO: -Wl,-z,relro was applied — GOT is read-only after startup.
RUN if ! file /usr/local/bin/bridge | grep -q 'pie executable'; then \
        printf 'ERROR: bridge binary is not PIE — -buildmode=pie may have been overridden\n' >&2; exit 1; \
    fi && \
    if ! readelf -d /usr/local/bin/bridge | grep -q 'BIND_NOW'; then \
        printf 'ERROR: bridge binary missing BIND_NOW — check CGO_LDFLAGS\n' >&2; exit 1; \
    fi && \
    if ! readelf -l /usr/local/bin/bridge | grep -q 'GNU_RELRO'; then \
        printf 'ERROR: bridge binary missing GNU_RELRO — check CGO_LDFLAGS\n' >&2; exit 1; \
    fi

# -----------------------------------------------------------------------------
# Stage 2: Runtime
# Runs as non-root user 'bridge' (UID 1000)
# -----------------------------------------------------------------------------
FROM debian:bookworm-slim@sha256:4724b8cc51e33e398f0e2e15e18d5ec2851ff0c2280647e1310bc1642182655d AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    pass \
    gnupg2 \
    libfido2-1 \
    libsecret-1-0 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/share/doc /usr/share/man

# Create dedicated non-root user and group
RUN groupadd --gid 1000 bridge && \
    useradd --uid 1000 \
            --gid 1000 \
            --home-dir /home/bridge \
            --create-home \
            --shell /usr/sbin/nologin \
            --no-log-init \
            bridge

# Copy compiled binary from builder
COPY --from=builder /build/bridge /usr/local/bin/bridge

# Copy entrypoint — owned by bridge user, executable bit set at copy time
COPY --chmod=755 --chown=bridge:bridge entrypoint.sh /entrypoint.sh

# Pre-create all subdirectories under /data with correct ownership.
# Docker copies these into the named volume on first mount (copy-on-empty),
# ensuring the bridge user can write even though the volume starts root-owned.
# Keep every Bridge state directory private to the bridge user because these
# paths hold cached auth, cert, GPG, pass-store, and runtime state.
RUN mkdir -p /data/config /data/local /data/cache /data/gnupg /data/pass \
    && chmod 700 /data/config /data/local /data/cache /data/gnupg /data/pass \
    && chown -R bridge:bridge /data

# Drop to non-root — everything from here runs as bridge
USER bridge

EXPOSE 1143 1025

ENTRYPOINT ["/entrypoint.sh"]
