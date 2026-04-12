# =============================================================================
# MCP Server — exposes email search and intelligence tools to Claude Desktop
# Uses HTTP/SSE transport (required for Docker — stdio only works on host)
# Two-stage: builder installs gcc to compile any native deps; runtime has none.
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
# Runs as non-root user 'mcp' (UID 1003)
# -----------------------------------------------------------------------------
FROM python:3.14-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1003 mcp && \
    useradd --uid 1003 \
            --gid 1003 \
            --home-dir /home/mcp \
            --create-home \
            --shell /bin/bash \
            --no-log-init \
            mcp

WORKDIR /app

# Copy installed packages from builder — gcc and build artifacts not included
COPY --from=builder /usr/local/lib/python3.14/site-packages/ \
                    /usr/local/lib/python3.14/site-packages/
COPY --from=builder /usr/local/bin/ /usr/local/bin/

COPY src/ ./src/

# HEALTHCHECK deferred — requires a /health endpoint in src/main.py first.
# When implemented: HEALTHCHECK CMD python -c \
#   "import urllib.request; urllib.request.urlopen('http://localhost:3000/health')"

USER mcp

EXPOSE 3000

CMD ["python", "-m", "src.main"]
