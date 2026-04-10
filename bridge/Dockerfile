# =============================================================================
# ProtonBridge — built from official Proton source, no GUI
# https://github.com/ProtonMail/proton-bridge
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder
# Compiles the bridge binary using the no-GUI Makefile target.
# The full Go toolchain and build dependencies are discarded after this stage.
# -----------------------------------------------------------------------------
FROM golang:1.24-bookworm AS builder

ARG BRIDGE_VERSION=v3.21.2

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    make \
    gcc \
    pkg-config \
    libsecret-1-dev \
    libfido2-dev \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

RUN git clone --depth 1 --branch ${BRIDGE_VERSION} \
    https://github.com/ProtonMail/proton-bridge.git .

# Build without Qt/GUI — produces a single lean `bridge` binary
RUN make build-nogui

# -----------------------------------------------------------------------------
# Stage 2: Runtime
# Minimal Debian image with only what Bridge needs to run.
# No Go toolchain, no build tools, no Qt.
# -----------------------------------------------------------------------------
FROM debian:bookworm-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Keychain — required by Bridge on Linux
    pass \
    gnupg2 \
    # CGO runtime dependencies
    libfido2-1 \
    libsecret-1-0 \
    # TLS
    ca-certificates \
    # Health check
    netcat-openbsd \
 && rm -rf /var/lib/apt/lists/* \
 && rm -rf /usr/share/doc /usr/share/man

# Copy only the compiled binary from the builder stage
COPY --from=builder /build/proton-bridge /usr/local/bin/bridge

# Redirect all Bridge state to /data so a single named volume
# captures credentials, config, cache, and keychain data.
ENV XDG_CONFIG_HOME=/data/config \
    XDG_CACHE_HOME=/data/cache \
    GNUPGHOME=/data/gnupg \
    PASSWORD_STORE_DIR=/data/pass

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# IMAP and SMTP ports (internal network only — not exposed to host)
EXPOSE 1143 1025

ENTRYPOINT ["/entrypoint.sh"]
