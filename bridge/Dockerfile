# =============================================================================
# ProtonBridge — built from official Proton source, no GUI
# Runs as non-root user 'bridge' for security
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder
# -----------------------------------------------------------------------------
FROM golang:1.26.2-bookworm AS builder

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

WORKDIR /build

RUN git clone --depth 1 --branch ${BRIDGE_VERSION} \
    https://github.com/ProtonMail/proton-bridge.git .

# Critical fix: patch Bridge to listen on 0.0.0.0 instead of 127.0.0.1
# Without this mbsync cannot reach Bridge from another container
RUN sed -i 's/Host = "127.0.0.1"/Host = "0.0.0.0"/' \
    internal/constants/constants.go

# Verify the patch applied
RUN grep 'Host = ' internal/constants/constants.go

# Patch Bridge TLS cert generation to include the Docker container hostname as a
# DNS SAN. Without this mbsync rejects the cert due to hostname mismatch —
# Bridge issues the cert for 127.0.0.1 but mbsync connects via protonmail-bridge.
# CertificateFile in mbsyncrc handles the self-signed trust; this fixes the SAN.
RUN sed -i \
    's|IPAddresses:           \[\]net\.IP{net\.ParseIP("127\.0\.0\.1")},|IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},\n\t\tDNSNames:    []string{"protonmail-bridge", "localhost"},|' \
    internal/certs/tls.go

# Verify the patch applied
RUN grep -A1 'IPAddresses' internal/certs/tls.go

# Build without Qt/GUI
RUN make build-nogui

# -----------------------------------------------------------------------------
# Stage 2: Runtime
# Runs as non-root user 'bridge' (UID 1000)
# -----------------------------------------------------------------------------
FROM debian:bookworm-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    pass \
    gnupg2 \
    libfido2-1 \
    libsecret-1-0 \
    ca-certificates \
    netcat-openbsd \
    inotify-tools \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /usr/share/doc /usr/share/man

# Create dedicated non-root user and group
RUN groupadd --gid 1000 bridge && \
    useradd --uid 1000 \
            --gid 1000 \
            --home-dir /home/bridge \
            --create-home \
            --shell /bin/bash \
            --no-log-init \
            bridge

# Copy compiled binary from builder
COPY --from=builder /build/bridge /usr/local/bin/bridge

# Copy entrypoint — owned by bridge user, executable bit set at copy time
COPY --chmod=755 --chown=bridge:bridge entrypoint.sh /entrypoint.sh

# Pre-create all subdirectories under /data with correct ownership.
# Docker copies these into the named volume on first mount (copy-on-empty),
# ensuring the bridge user can write even though the volume starts root-owned.
RUN mkdir -p /data/config /data/local /data/cache /data/gnupg /data/pass \
    && chmod 700 /data/gnupg \
    && chown -R bridge:bridge /data

# Drop to non-root — everything from here runs as bridge
USER bridge

EXPOSE 1143 1025

ENTRYPOINT ["/entrypoint.sh"]
