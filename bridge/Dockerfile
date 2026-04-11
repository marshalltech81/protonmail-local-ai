# =============================================================================
# ProtonBridge — built from official Proton source, no GUI
# https://github.com/ProtonMail/proton-bridge
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
        ca-certificates \
     && rm -rf /var/lib/apt/lists/*
    
    WORKDIR /build
    
    RUN git clone --depth 1 --branch ${BRIDGE_VERSION} \
        https://github.com/ProtonMail/proton-bridge.git .
    
    # Critical fix: Bridge binds to 127.0.0.1 by default.
    # Inside Docker, 127.0.0.1 is the container's own loopback —
    # unreachable from mbsync or any other container on the network.
    # Patch to 0.0.0.0 so Bridge listens on all interfaces.
    RUN sed -i 's/Host = "127.0.0.1"/Host = "0.0.0.0"/' \
        internal/constants/constants.go
    
    # Verify the patch applied
    RUN grep 'Host = ' internal/constants/constants.go
    
    # Build without Qt/GUI
    RUN make build-nogui
    
    # -----------------------------------------------------------------------------
    # Stage 2: Runtime
    # -----------------------------------------------------------------------------
    FROM debian:bookworm-slim AS runtime
    
    RUN apt-get update && apt-get install -y --no-install-recommends \
        pass \
        gnupg2 \
        libfido2-1 \
        libsecret-1-0 \
        ca-certificates \
        netcat-openbsd \
     && rm -rf /var/lib/apt/lists/* \
     && rm -rf /usr/share/doc /usr/share/man
    
    COPY --from=builder /build/bridge /usr/local/bin/bridge
    
    ENV XDG_CONFIG_HOME=/data/config \
        XDG_CACHE_HOME=/data/cache \
        GNUPGHOME=/data/gnupg \
        PASSWORD_STORE_DIR=/data/pass
    
    COPY entrypoint.sh /entrypoint.sh
    RUN chmod +x /entrypoint.sh
    
    EXPOSE 1143 1025
    
    ENTRYPOINT ["/entrypoint.sh"]