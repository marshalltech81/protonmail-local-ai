# =============================================================================
# MCP Server — exposes email search and intelligence tools to Claude Desktop
# Uses HTTP/SSE transport (required for Docker — stdio only works on host)
# =============================================================================
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

EXPOSE 3000

CMD ["python", "-m", "src.main"]
