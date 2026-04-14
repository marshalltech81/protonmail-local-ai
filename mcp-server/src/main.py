"""
MCP Server entry point.
Exposes local mailbox search, retrieval, intelligence, and system tools to
Claude Desktop via HTTP/SSE transport. Mail-changing action tools are disabled
by default until a safe opt-in write backend exists.
"""

import logging
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

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
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
ANTHROPIC_KEY = _read_secret("anthropic_api_key", "ANTHROPIC_API_KEY")
MCP_PORT = int(os.environ.get("MCP_PORT", "3000"))
MCP_READ_ONLY = _env_bool("MCP_READ_ONLY", True)


def main():
    # Shared service clients
    db = Database(SQLITE_PATH)
    ollama = OllamaClient(OLLAMA_HOST, EMBED_MODEL, LLM_MODEL)

    # FastMCP server — supports @server.tool() decorator and SSE transport
    server = FastMCP("protonmail-local-ai", port=MCP_PORT)

    # Register all tool groups
    register_search_tools(server, db, ollama)
    register_retrieval_tools(server, db)
    register_intelligence_tools(server, db, ollama, LLM_MODE, ANTHROPIC_KEY)
    if MCP_READ_ONLY:
        log.info("MCP read-only mode enabled; action tools are not registered.")
    else:
        log.warning(
            "MCP_READ_ONLY=false, but mail-changing tools are still not registered because "
            "the default deployment has no safe write backend for mcp-server."
        )
    register_system_tools(server, db, bridge_enabled=False)

    log.info(f"MCP server starting on port {MCP_PORT}")
    log.info(f"  SQLite:   {SQLITE_PATH}")
    log.info(f"  Ollama:   {OLLAMA_HOST}")
    log.info(f"  LLM mode: {LLM_MODE}")
    log.info("  Retrieval: local SQLite index only")

    server.run(transport="sse")


if __name__ == "__main__":
    main()
