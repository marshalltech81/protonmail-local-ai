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

COPY mbsyncrc.template /etc/mbsyncrc.template
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/maildir"]

ENTRYPOINT ["/entrypoint.sh"]
