"""
MCP Server entry point.
Exposes local mailbox search, retrieval, intelligence, and system tools over
MCP transports. Mail-changing action tools are disabled by default until a safe
opt-in write backend exists.
"""

import contextlib
import logging
import os
from pathlib import Path
from typing import Literal, cast

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
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


class _SilenceClientDisconnect(logging.Filter):
    """Drop benign ``ClientDisconnect`` noise from the MCP SDK's logs.

    Open WebUI opens an MCP transport session via ``POST /mcp`` and sometimes
    abandons it before sending the body — typically when the chat
    coordinator decided to retry a different request shape, or the previous
    call already returned what it needed. The MCP SDK catches the resulting
    ``starlette.requests.ClientDisconnect`` cleanly and the connection ends
    without harm, but the SDK logs it at ERROR level on two loggers
    (``mcp.server.streamable_http`` and ``mcp.server.lowlevel.server``)
    which makes routine traffic look alarming in operator logs.

    Drop those specific records here. Any record without ``exc_info`` or
    with a different exception class still propagates unchanged, so a real
    bug surfaces normally.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            exc_type = record.exc_info[0]
            if exc_type is not None and exc_type.__name__ == "ClientDisconnect":
                return False
        return True


# Attach the filter to the two MCP SDK loggers known to surface
# ``ClientDisconnect`` tracebacks. Limited scope on purpose: filtering at
# the root logger would risk swallowing a future, genuinely-different
# ``ClientDisconnect`` somewhere in the stack.
_disconnect_filter = _SilenceClientDisconnect()
for _logger_name in ("mcp.server.streamable_http", "mcp.server.lowlevel.server"):
    logging.getLogger(_logger_name).addFilter(_disconnect_filter)


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
LLM_MODEL = os.environ.get("OLLAMA_LLM_MODEL", "qwen2.5:14b-instruct")
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
    """Serve SSE and Streamable HTTP routes from one FastMCP instance.

    Each transport app is invoked as a complete ASGI app rather than
    having its routes flattened into a fresh Starlette — that
    preserves whatever middleware and per-request context plumbing
    the SDK attaches to ``streamable_http_app()`` / ``sse_app()`` (the
    Streamable HTTP transport in particular relies on session-manager
    context that lives on the inner app, not on individual routes).

    Lifespan is run on a tiny outer Starlette whose only job is to
    enter both inner apps' ``lifespan_context`` — ``session_manager``
    starts here for Streamable HTTP, and SSE gets to register its
    startup/shutdown hooks too even though the current SDK sse_app
    doesn't ship any. HTTP/WebSocket scopes go straight to the right
    transport app via prefix dispatch.
    """
    sse_app = server.sse_app()
    streamable_http_app = server.streamable_http_app()

    streamable_path = server.settings.streamable_http_path
    # Pre-compute the prefix used to recognize trailing-slash and
    # sub-path requests (``/mcp/`` or ``/mcp/foo``) without also
    # matching unrelated paths like ``/mcpfoo`` or ``/mcp-debug``.
    streamable_prefix = streamable_path.rstrip("/") + "/"

    @contextlib.asynccontextmanager
    async def combined_lifespan(scope_app):
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(streamable_http_app.router.lifespan_context(scope_app))
            await stack.enter_async_context(sse_app.router.lifespan_context(scope_app))
            yield

    # Outer Starlette owns lifespan only — it has no routes of its own.
    lifespan_owner = Starlette(debug=server.settings.debug, lifespan=combined_lifespan)

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            await lifespan_owner(scope, receive, send)
            return
        path = scope.get("path", "/")
        # Streamable HTTP claims exactly the configured streamable path
        # (``/mcp`` by default) and any sub-path under it. Everything
        # else — ``/sse``, ``/messages/``, the ``/health`` custom route
        # registered on the FastMCP server, and any future ``/mcp-*``
        # custom route — is served by the SSE app (which inherits the
        # FastMCP custom routes). Using a startswith check on a
        # trailing-slash prefix avoids ``/mcp`` over-matching paths
        # like ``/mcpfoo``.
        if path == streamable_path or path.startswith(streamable_prefix):
            target = streamable_http_app
        else:
            target = sse_app
        await target(scope, receive, send)

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

    # FastMCP server — supports @server.tool() decorator and SSE transport.
    # ``host="0.0.0.0"`` is required so the in-container bind is reachable
    # through the Docker port-forward; the host-side mapping in
    # ``docker-compose.yml`` keeps the port loopback-only
    # (``127.0.0.1:${MCP_PORT}:3000``). nosec B104.
    #
    # FastMCP only auto-enables DNS-rebinding protection when ``host`` is
    # one of ``127.0.0.1``/``localhost``/``::1``; binding to ``0.0.0.0``
    # silently disables it. We re-enable Host/Origin allow-listing
    # explicitly so a malicious local browser page cannot DNS-rebind to
    # this listener — relevant once the optional Open WebUI overlay
    # shares the ``app-net`` network with the MCP server.
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "localhost",
            "localhost:*",
            "127.0.0.1",
            "127.0.0.1:*",
            "[::1]",
            "[::1]:*",
            "mcp-server",
            "mcp-server:*",
        ],
        allowed_origins=[
            "http://localhost",
            "http://localhost:*",
            "http://127.0.0.1",
            "http://127.0.0.1:*",
            "http://[::1]",
            "http://[::1]:*",
            "http://mcp-server",
            "http://mcp-server:*",
        ],
    )
    server = FastMCP(
        "protonmail-local-ai",
        host="0.0.0.0",  # nosec B104 — see comment above
        port=MCP_PORT,
        transport_security=transport_security,
    )

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
