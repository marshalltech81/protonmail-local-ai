# =============================================================================
# mbsync — real-time incremental email sync from ProtonBridge to Maildir
# =============================================================================
FROM debian:bookworm-slim@sha256:4724b8cc51e33e398f0e2e15e18d5ec2851ff0c2280647e1310bc1642182655d

RUN apt-get update && apt-get install -y --no-install-recommends \
    isync \
    ca-certificates \
    netcat-openbsd \
    gettext-base \
    openssl \
 && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# Non-root user
# UID 1001 — distinct from bridge user (UID 1000)
# -----------------------------------------------------------------------------
RUN groupadd --gid 1001 mbsync && \
    useradd --uid 1001 \
            --gid 1001 \
            --home-dir /home/mbsync \
            --create-home \
            --shell /usr/sbin/nologin \
            --no-log-init \
            mbsync

# -----------------------------------------------------------------------------
# Config and entrypoint
# -----------------------------------------------------------------------------
COPY mbsyncrc.template /etc/mbsyncrc.template
COPY --chmod=755 --chown=mbsync:mbsync entrypoint.sh /entrypoint.sh
COPY --chmod=755 --chown=mbsync:mbsync healthcheck.sh /healthcheck.sh

# -----------------------------------------------------------------------------
# Pre-create Maildir structure with correct ownership BEFORE dropping to
# non-root user. This ensures the mbsync user can write to the volume on
# first mount even if Docker initialises the volume from this directory.
#
# The VOLUME declaration must come AFTER the mkdir+chown — Docker copies
# the directory contents into the named volume on first mount, preserving
# ownership. If VOLUME comes before, the directory is empty and ownership
# is lost.
# -----------------------------------------------------------------------------
RUN mkdir -p /maildir \
  && chown -R mbsync:mbsync /maildir

# Drop to non-root user
USER mbsync

# Declare volume AFTER chown so Docker preserves ownership on first mount
VOLUME ["/maildir"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 CMD ["/healthcheck.sh"]

ENTRYPOINT ["/entrypoint.sh"]
