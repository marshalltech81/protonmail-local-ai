# =============================================================================
# Indexer — parses, threads, embeds, and indexes email into SQLite
# Two-stage: builder installs gcc to compile sqlite-vec; runtime has none.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder — installs Python dependencies that require compilation
# -----------------------------------------------------------------------------
FROM python:3.14-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# -----------------------------------------------------------------------------
# Stage 2: Runtime — no build toolchain
# Runs as non-root user 'indexer' (UID 1002)
# -----------------------------------------------------------------------------
FROM python:3.14-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    inotify-tools \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1002 indexer && \
    useradd --uid 1002 \
            --gid 1002 \
            --home-dir /home/indexer \
            --create-home \
            --shell /bin/bash \
            --no-log-init \
            indexer

WORKDIR /app

# Copy installed packages from builder — gcc and build artifacts not included
COPY --from=builder /usr/local/lib/python3.14/site-packages/ \
                    /usr/local/lib/python3.14/site-packages/
COPY --from=builder /usr/local/bin/ /usr/local/bin/

COPY src/ ./src/

# Pre-create volume mount points with correct ownership BEFORE VOLUME declaration.
# Docker copies these into the named volume on first mount (copy-on-empty),
# ensuring the indexer user can write even though the volume starts root-owned.
RUN mkdir -p /maildir /data && chown -R indexer:indexer /maildir /data

USER indexer

VOLUME ["/maildir", "/data"]

CMD ["python", "-m", "src.main"]
