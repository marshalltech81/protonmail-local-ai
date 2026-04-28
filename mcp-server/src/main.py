"""
MCP Server entry point.
Exposes local mailbox search, retrieval, intelligence, and system tools over
MCP transports. Mail-changing action tools are disabled by default until a safe
opt-in write backend exists.
"""

import logging
import os
from pathlib import Path
from typing import Literal, cast

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from .lib.ollama import OllamaClient
from .lib.sqlite import Database
from .tools.intelligence import register_intelligence_tools
from .tools.retrieval import register_retrieval_tools
from .tools.search import register_search_tools
from .tools.system import register_system_tools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("mcp-server")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _read_secret(secret_name: str, env_fallback: str = "") -> str:
    """Read a Docker secret file, falling back to an environment variable.

    Prefer the secret file so the value is never exposed via docker inspect.
    The env fallback preserves backward compatibility for local dev without
    Docker secrets configured.
    """
    path = Path(f"/run/secrets/{secret_name}")
    if path.exists():
        return path.read_text().strip()
    return os.environ.get(env_fallback, "")


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
SQLITE_PATH = os.environ.get("SQLITE_PATH", "/data/mail.db")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
LLM_MODEL = os.environ.get("OLLAMA_LLM_MODEL", "llama3.2")
LLM_MODE = os.environ.get("LLM_MODE", "local")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
ANTHROPIC_KEY = _read_secret("anthropic_api_key", "ANTHROPIC_API_KEY")
MCP_PORT = int(os.environ.get("MCP_PORT", "3000"))
MCP_READ_ONLY = _env_bool("MCP_READ_ONLY", True)
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "sse")


def _normalize_transport(raw: str) -> str:
    transport = raw.strip().lower()
    if transport in {"sse", "streamable-http", "dual"}:
        return transport
    raise ValueError("MCP_TRANSPORT must be one of: sse, streamable-http, dual")


async def _run_dual_transport_async(server: FastMCP) -> None:
    """Serve SSE and Streamable HTTP routes from one FastMCP instance."""
    sse_app = server.sse_app()
    streamable_http_app = server.streamable_http_app()

    app = Starlette(
        debug=server.settings.debug,
        routes=[*streamable_http_app.routes, *sse_app.routes],
        lifespan=lambda _: server.session_manager.run(),
    )
    config = uvicorn.Config(
        app,
        host=server.settings.host,
        port=server.settings.port,
        log_level=server.settings.log_level.lower(),
    )
    await uvicorn.Server(config).serve()


def _run_server(server: FastMCP, transport: str) -> None:
    normalized = _normalize_transport(transport)
    if normalized == "dual":
        import anyio

        anyio.run(lambda: _run_dual_transport_async(server))
        return
    server.run(transport=cast(Literal["sse", "streamable-http"], normalized))


def main():
    # Shared service clients
    db = Database(SQLITE_PATH)
    ollama = OllamaClient(OLLAMA_HOST, EMBED_MODEL, LLM_MODEL)

    # FastMCP server — supports @server.tool() decorator and SSE transport
    server = FastMCP("protonmail-local-ai", host="0.0.0.0", port=MCP_PORT)  # nosec B104

    # Plain HTTP health endpoint used by the container healthcheck. Sits
    # outside the MCP protocol so `docker healthcheck` and operator scripts
    # can probe liveness without speaking SSE. Returns 200 when the SQLite
    # index is reachable via the read-only connection — enough to catch a
    # missing volume mount or a corrupt DB without exercising any write
    # path. The error string is intentionally generic in the response so
    # the endpoint does not leak DB paths or schema details to anyone who
    # can reach localhost:MCP_PORT.
    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_: Request) -> JSONResponse:
        try:
            db.ping()
        except Exception:
            log.exception("health probe failed")
            return JSONResponse({"status": "unhealthy"}, status_code=503)
        return JSONResponse({"status": "ok"})

    # Register all tool groups
    register_search_tools(server, db, ollama)
    register_retrieval_tools(server, db)
    register_intelligence_tools(server, db, ollama, LLM_MODE, ANTHROPIC_KEY, CLAUDE_MODEL)
    if MCP_READ_ONLY:
        log.info("MCP read-only mode enabled; action tools are not registered.")
    else:
        log.warning(
            "MCP_READ_ONLY=false, but mail-changing tools are still not registered because "
            "the default deployment has no safe write backend for mcp-server."
        )
    register_system_tools(server, db, bridge_enabled=False)

    transport = _normalize_transport(MCP_TRANSPORT)

    log.info(f"MCP server starting on port {MCP_PORT}")
    log.info(f"  SQLite:   {SQLITE_PATH}")
    log.info(f"  Ollama:   {OLLAMA_HOST}")
    log.info(f"  LLM mode: {LLM_MODE}")
    log.info(f"  Transport: {transport}")
    if LLM_MODE == "cloud":
        log.info(f"  Claude model: {CLAUDE_MODEL}")
    log.info("  Retrieval: local SQLite index only")

    _run_server(server, transport)


if __name__ == "__main__":
    main()
