# =============================================================================
# ProtonBridge — built from official Proton source, no GUI
# Runs as non-root user 'bridge' for security
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder
# -----------------------------------------------------------------------------
FROM golang:1.26.2-bookworm@sha256:47ce5636e9936b2c5cbf708925578ef386b4f8872aec74a67bd13a627d242b19 AS builder

# Use bash with pipefail for all RUN steps so pipe failures are not silently ignored.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG BRIDGE_VERSION=v3.24.2

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

# GOTOOLCHAIN=local prevents the builder from auto-downloading a different Go
# version if go.mod requests a newer toolchain — the pinned base image is the
# source of truth and must not be silently overridden at build time.
ENV GOTOOLCHAIN=local

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

# Build without Qt/GUI using the upstream Makefile target.
# Proton's own build configuration is used without injecting custom GOFLAGS
# or CGO flags — Proton is a security company and their build reflects their
# security requirements.
RUN make build-nogui

# -----------------------------------------------------------------------------
# Stage 2: Runtime
# Runs as non-root user 'bridge' (UID 1000)
# -----------------------------------------------------------------------------
FROM debian:bookworm-slim@sha256:f9c6a2fd2ddbc23e336b6257a5245e31f996953ef06cd13a59fa0a1df2d5c252 AS runtime

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
COPY --chmod=555 --from=builder /build/bridge /usr/local/bin/bridge

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
