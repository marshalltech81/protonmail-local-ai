"""
MCP Server entry point.
Exposes email search, retrieval, intelligence, action, and system tools
to Claude Desktop via HTTP/SSE transport.
"""
import os
import logging

from mcp.server.fastmcp import FastMCP

from .tools.search import register_search_tools
from .tools.retrieval import register_retrieval_tools
from .tools.intelligence import register_intelligence_tools
from .tools.actions import register_action_tools
from .tools.system import register_system_tools
from .lib.sqlite import Database
from .lib.imap import IMAPClient
from .lib.ollama import OllamaClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("mcp-server")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
SQLITE_PATH   = os.environ.get("SQLITE_PATH", "/data/mail.db")
BRIDGE_HOST   = os.environ.get("BRIDGE_HOST", "protonmail-bridge")
BRIDGE_IMAP   = int(os.environ.get("BRIDGE_IMAP_PORT", "1143"))
BRIDGE_SMTP   = int(os.environ.get("BRIDGE_SMTP_PORT", "1025"))
BRIDGE_USER   = os.environ.get("BRIDGE_USER", "")
BRIDGE_PASS   = os.environ.get("BRIDGE_PASS", "")
OLLAMA_HOST   = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
EMBED_MODEL   = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
LLM_MODEL     = os.environ.get("OLLAMA_LLM_MODEL", "llama3.2")
LLM_MODE      = os.environ.get("LLM_MODE", "local")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MCP_PORT      = int(os.environ.get("MCP_PORT", "3000"))


def main():
    # Shared service clients
    db     = Database(SQLITE_PATH)
    imap   = IMAPClient(BRIDGE_HOST, BRIDGE_IMAP, BRIDGE_USER, BRIDGE_PASS)
    ollama = OllamaClient(OLLAMA_HOST, EMBED_MODEL, LLM_MODEL)

    # FastMCP server — supports @server.tool() decorator and SSE transport
    server = FastMCP("protonmail-local-ai", port=MCP_PORT)

    # Register all tool groups
    register_search_tools(server, db, ollama)
    register_retrieval_tools(server, db, imap)
    register_intelligence_tools(server, db, ollama, LLM_MODE, ANTHROPIC_KEY)
    register_action_tools(server, imap)
    register_system_tools(server, db)

    log.info(f"MCP server starting on port {MCP_PORT}")
    log.info(f"  SQLite:   {SQLITE_PATH}")
    log.info(f"  Bridge:   {BRIDGE_HOST}:{BRIDGE_IMAP}")
    log.info(f"  Ollama:   {OLLAMA_HOST}")
    log.info(f"  LLM mode: {LLM_MODE}")

    server.run(transport="sse")


if __name__ == "__main__":
    main()
