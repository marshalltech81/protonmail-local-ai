# =============================================================================
# mbsync — real-time incremental email sync from ProtonBridge to Maildir
# =============================================================================
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    isync \
    ca-certificates \
    netcat-openbsd \
    gettext-base \
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
            --shell /bin/bash \
            --no-log-init \
            mbsync

# -----------------------------------------------------------------------------
# Bridge TLS cert for verified IMAP connection
# Extract via: make recert
# Until cert is extracted, replace with /dev/null in mbsyncrc.template
# -----------------------------------------------------------------------------
#COPY bridge-cert.pem /etc/bridge-cert.pem
#RUN chmod 644 /etc/bridge-cert.pem

# -----------------------------------------------------------------------------
# Config and entrypoint
# -----------------------------------------------------------------------------
COPY mbsyncrc.template /etc/mbsyncrc.template
COPY --chown=mbsync:mbsync entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

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

ENTRYPOINT ["/entrypoint.sh"]
