# =============================================================================
# MCP Server — exposes email search and intelligence tools to Claude Desktop
# Uses HTTP/SSE transport (required for Docker — stdio only works on host)
# Two-stage: builder installs deps into a uv-managed virtualenv; runtime has none.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder — installs Python dependencies that require compilation
# -----------------------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:bc389f7dfcb21413e72a28f491985326994795e34d2b86c8ae2f417b4e7818aa AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.6@sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754 /uv /uvx /bin/

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src/ ./src/

# -----------------------------------------------------------------------------
# Stage 2: Runtime — no build toolchain
# Runs as non-root user 'mcp' (UID 1003)
# -----------------------------------------------------------------------------
FROM python:3.14-slim-trixie@sha256:bc389f7dfcb21413e72a28f491985326994795e34d2b86c8ae2f417b4e7818aa

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1003 mcp && \
    useradd --uid 1003 \
            --gid 1003 \
            --home-dir /home/mcp \
            --create-home \
            --shell /usr/sbin/nologin \
            --no-log-init \
            mcp

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

# Copy the uv-managed virtualenv from the builder; runtime keeps no toolchain.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src/ ./src/

USER mcp

# Probes the FastMCP custom /health route — returns 200 when the
# read-only SQLite connection answers a trivial query, 503 otherwise.
# urllib raises on non-2xx, giving docker a non-zero exit without
# adding curl/wget to the runtime image. Resolves the port from
# MCP_PORT so the check tracks whatever the server actually bound.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"MCP_PORT\",\"3000\")}/health', timeout=4).read()"]

EXPOSE 3000

CMD ["python", "-m", "src.main"]
