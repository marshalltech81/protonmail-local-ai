# =============================================================================
# Indexer — parses, threads, embeds, and indexes email into SQLite
# Two-stage: builder installs deps into a uv-managed virtualenv; runtime has none.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder — installs Python dependencies that require compilation
# -----------------------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:5b3879b6f3cb77e712644d50262d05a7c146b7312d784a18eff7ff5462e77033 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.6@sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754 /uv /uvx /bin/

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src/ ./src/

# -----------------------------------------------------------------------------
# Stage 2: Runtime — no build toolchain
# Runs as non-root user 'indexer' (UID 1002)
# -----------------------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:5b3879b6f3cb77e712644d50262d05a7c146b7312d784a18eff7ff5462e77033

RUN apt-get update && apt-get install -y --no-install-recommends \
    inotify-tools \
    # Attachment text extraction (schema v10). Tesseract supplies the OCR
    # path used by image attachments and the scanned-PDF fallback in
    # src/extractors/{image,pdf}.py; Poppler's pdftoppm is what pdf2image
    # shells out to when rendering each PDF page to a PIL image. English
    # is the only Tesseract language pack installed by default — extend
    # via tesseract-ocr-* packages here if non-English mailboxes appear.
    tesseract-ocr \
    poppler-utils \
 && rm -rf /var/lib/apt/lists/*

# UID 1002 — distinct from mbsync (UID 1001) so a parser/extractor
# exploit triggered by a malicious .eml or attachment doesn't end up
# with mbsync's UID. mbsync's entrypoint relaxes Maildir file perms
# to 0644 after each sync (see relax_new_maildir_perms) so the
# indexer reads via "other" permission rather than sharing mbsync's
# UID. The Maildir mount is `:ro` from the indexer side as further
# defense-in-depth.
RUN groupadd --gid 1002 indexer && \
    useradd --uid 1002 \
            --gid 1002 \
            --home-dir /home/indexer \
            --create-home \
            --shell /usr/sbin/nologin \
            --no-log-init \
            indexer

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1

# Copy the uv-managed virtualenv from the builder; runtime keeps no toolchain.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src/ ./src/
COPY --chmod=755 --chown=indexer:indexer healthcheck.sh /healthcheck.sh

# Pre-create volume mount points with correct ownership BEFORE VOLUME declaration.
# Docker copies these into the named volume on first mount (copy-on-empty),
# ensuring the indexer user can write even though the volume starts root-owned.
RUN mkdir -p /maildir /data && chown -R indexer:indexer /maildir /data

USER indexer

VOLUME ["/maildir", "/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 CMD ["/healthcheck.sh"]

CMD ["python", "-m", "src.main"]
