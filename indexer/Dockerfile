# =============================================================================
# Indexer — parses, threads, embeds, and indexes email into SQLite
# Two-stage: builder installs deps into a uv-managed virtualenv; runtime has none.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder — installs Python dependencies that require compilation
# -----------------------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.0@sha256:cdc6093146eb3ff6a40107b38f008b789e050e77ad87865e381d9917da55a168 /uv /uvx /bin/

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
 && rm -rf /var/lib/apt/lists/*

# Bytecode is compiled here, for the dependencies (--compile-bytecode)
# and for src/ (compileall), so the read-only runtime imports .pyc files
# instead of compiling every module from source in each process. That
# matters for the extractor children, which start a fresh interpreter
# per extraction (#1230). PYTHONDONTWRITEBYTECODE stays set in both
# stages, so nothing else writes bytecode. src/ uses checked-hash pycs,
# which stay valid whatever mtime the copy into the runtime gives them.
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --compile-bytecode
COPY src/ ./src/
RUN python -m compileall -q --invalidation-mode checked-hash src

# -----------------------------------------------------------------------------
# Stage 2: Legacy .ppt reader (#957) — Apache POI on a trimmed Java runtime
# The JDK and Maven stay in these stages. ppt-tools installs them;
# ppt-builder has Maven fetch the jars pinned in java/pom.xml, checking
# every artifact it resolves against java/checksums/checksums.sha256;
# javac compiles the one-class reader against them; jlink writes a Java
# runtime with only the modules POI needs. The result is /opt/ppt:
# jre/ and lib/ (the jars).
# -----------------------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS ppt-tools

RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-21-jdk-headless \
    maven \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY java/pom.xml ./

FROM ppt-tools AS ppt-builder
# Every artifact Maven resolves here (the jars, their POMs and parent
# POMs, and the dependency plugin with its own tree) must match its
# SHA-256 line in the committed summary file, and one with no line
# fails too: Maven Resolver's trusted checksums with failIfMissing
# (#1117). The check runs on every resolution, from the cache mount as
# well as from Maven Central; --strict-checksums still checks each
# download against Central's own checksum file. The summary file is
# read from the build context, never from the local repository (the
# resolver's default), so nothing in the mount can vouch for itself.
#
# Maven's local repository is a BuildKit cache mount (#1070): a rebuild
# that reruns this step (a pom.xml bump, a base-image refresh)
# downloads only what the mount lacks, so a Maven Central outage or
# rate limit stops it only on a cold mount. A --no-cache build starts
# from an empty mount and refills it. The mount has its own id, so no
# other project's build on the same builder fills it or reads it; any
# build the operator runs there can still write to it, as to every
# cache mount, and the check above fails the build on any artifact so
# written that differs from its committed checksum. `docker builder
# prune` empties it. After a pom.xml change (a Dependabot bump),
# `make ppt-checksums` rewrites the summary file from the
# ppt-checksums stage below.
COPY java/checksums/checksums.sha256 /build/checksums/
RUN --mount=type=cache,id=protonmail-local-ai-maven,target=/root/.m2,sharing=locked \
    mvn --batch-mode --no-transfer-progress --strict-checksums \
    -Daether.artifactResolver.postProcessor.trustedChecksums=true \
    -Daether.artifactResolver.postProcessor.trustedChecksums.checksumAlgorithms=SHA-256 \
    -Daether.artifactResolver.postProcessor.trustedChecksums.failIfMissing=true \
    -Daether.artifactResolver.postProcessor.trustedChecksums.record=false \
    -Daether.trustedChecksumsSource.summaryFile=true \
    -Daether.trustedChecksumsSource.summaryFile.basedir=/build/checksums \
    -Daether.trustedChecksumsSource.summaryFile.originAware=false \
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

# The summary file's source, built only by `make ppt-checksums` (#1117).
# The same Maven, with no cache mount and no summary file, downloads
# every artifact the ppt-builder run above and the Maven step of
# .github/workflows/security.yml resolve, checks each download against
# Maven Central's checksum file (--strict-checksums) and records its
# SHA-256, so an artifact the pom no longer needs drops out.
FROM ppt-tools AS ppt-checksums-record
RUN mvn --batch-mode --no-transfer-progress --strict-checksums \
    -Daether.artifactResolver.postProcessor.trustedChecksums=true \
    -Daether.artifactResolver.postProcessor.trustedChecksums.checksumAlgorithms=SHA-256 \
    -Daether.artifactResolver.postProcessor.trustedChecksums.record=true \
    -Daether.trustedChecksumsSource.summaryFile=true \
    -Daether.trustedChecksumsSource.summaryFile.basedir=/out \
    -Daether.trustedChecksumsSource.summaryFile.originAware=false \
    dependency:copy-dependencies -DoutputDirectory=/tmp/lib dependency:resolve

FROM scratch AS ppt-checksums
COPY --from=ppt-checksums-record /out/checksums.sha256 /

# -----------------------------------------------------------------------------
# Stage 3: Runtime — no build toolchain
# Runs as non-root user 'indexer' (UID 1002)
# -----------------------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

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
 && python -m pip uninstall --yes --no-cache-dir pip \
 # The base image ships bytecode for only part of the standard library
 # (the xml, decimal and pydoc modules the extractor children import
 # have none), so it is compiled here, as the builder compiles the
 # virtualenv and src/ (#1230).
 && python -m compileall -q /usr/local/lib/python3.14

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
