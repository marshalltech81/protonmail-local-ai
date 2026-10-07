# =============================================================================
# Indexer — parses, threads, embeds, and indexes email into SQLite
# Two-stage: builder installs deps into a uv-managed virtualenv; runtime has none.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder — installs Python dependencies that require compilation
# -----------------------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151 AS builder

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
# Stage 2: Legacy .ppt reader (#957) — Apache POI on a trimmed Java runtime
# The JDK and Maven stay in this stage. Maven fetches the jars pinned in
# java/pom.xml and fails on a checksum mismatch (--strict-checksums);
# javac compiles the one-class reader against them; jlink writes a Java
# runtime with only the modules POI needs. The result is /opt/ppt:
# jre/ and lib/ (the jars).
# -----------------------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151 AS ppt-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-21-jdk-headless \
    maven \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY java/pom.xml ./
RUN mvn --batch-mode --no-transfer-progress --strict-checksums \
    dependency:copy-dependencies -DoutputDirectory=/opt/ppt/lib
COPY java/PptText.java ./
RUN javac --release 21 -d classes -cp '/opt/ppt/lib/*' PptText.java \
 && jar --create --file /opt/ppt/lib/ppt-text.jar -C classes . \
 && jlink --add-modules java.base,java.desktop,java.xml,java.logging,jdk.unsupported \
    --strip-java-debug-attributes --no-header-files --no-man-pages --compress=zip-6 \
    --output /opt/ppt/jre

# Only /opt/ppt, so CI can export it for the indexer tests
# (.github/workflows/tests.yml) and the runtime copies the same files.
FROM scratch AS ppt-runtime
COPY --from=ppt-builder /opt/ppt /opt/ppt

# -----------------------------------------------------------------------------
# Stage 3: Runtime — no build toolchain
# Runs as non-root user 'indexer' (UID 1002)
# -----------------------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Attachment text extraction. Tesseract supplies the OCR
    # path used by image attachments and the scanned-PDF fallback in
    # src/extractors/{image,pdf}.py; Poppler's pdftoppm is what pdf2image
    # shells out to when rendering each PDF page to a PIL image. English
    # is the only Tesseract language pack installed, and the extractors
    # pass no language, so adding a tesseract-ocr-* pack here would not
    # select it by itself (issue #490). catdoc supplies catdoc, which
    # src/extractors/doc.py runs on legacy binary .doc attachments (#935).
    tesseract-ocr \
    poppler-utils \
    catdoc \
 && rm -rf /var/lib/apt/lists/* \
 # The service runs from the uv-built virtualenv, which has no pip, so
 # the base image's pip (and the packages it vendors) is removed (#1007).
 && python -m pip uninstall --yes --no-cache-dir pip

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
# The legacy .ppt reader: a trimmed Java runtime and the POI jars, owned
# by root and read-only to the indexer (src/extractors/ppt.py).
COPY --from=ppt-runtime /opt/ppt /opt/ppt
COPY --chmod=755 --chown=indexer:indexer healthcheck.sh /healthcheck.sh

# Pre-create volume mount points with correct ownership BEFORE VOLUME declaration.
# Docker copies these into the named volume on first mount (copy-on-empty).
#
# /maildir is owned by UID 1001 (mbsync) here, NOT the indexer user, to make
# the named-volume seeding race harmless: the maildir-volume is shared with
# the mbsync container, and whichever image's mount is hit first by docker
# compose seeds the volume's ownership. mbsync writes; the indexer reads via
# "other" permission bits (mbsync's entrypoint chmods go+r after each sync)
# and mounts /maildir :ro for defense-in-depth, so indexer does not need to
# own this directory. Keeping both images' /maildir at 1001:1001 means either
# seeding order produces a working volume.
RUN mkdir -p /maildir /data \
 && chown 1001:1001 /maildir \
 && chown -R indexer:indexer /data

# Source commit the image was built from, logged at startup (#887).
# Declared late so a new commit does not invalidate the layers above.
ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.revision="${GIT_COMMIT}"
ENV GIT_COMMIT="${GIT_COMMIT}"

USER indexer

VOLUME ["/maildir", "/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 CMD ["/healthcheck.sh"]

CMD ["python", "-m", "src.main"]
