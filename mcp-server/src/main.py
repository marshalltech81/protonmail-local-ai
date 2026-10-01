"""
MCP Server entry point.
Exposes local mailbox search, retrieval, intelligence, and system tools over
MCP transports. The server is read-only: it has no mail-changing tools and no
connection to Bridge.
"""

import asyncio
import contextlib
import logging
import math
import os
import urllib.parse
from pathlib import Path
from typing import Literal

import uvicorn
from fastmcp import FastMCP
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .lib.embed import DEFAULT_EMBED_TIMEOUT_SECS, EmbedClient
from .lib.inference import (
    DEFAULT_COMPLETE_TIMEOUT_SECS,
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_MAX_TOKENS,
    InferenceClient,
    PromptBudget,
)
from .lib.reranker import DEFAULT_RERANK_TIMEOUT_SECS, CohereReranker, RerankConfig
from .lib.sqlite import Database
from .tools.brief import register_experimental_tools
from .tools.intelligence import register_intelligence_tools
from .tools.retrieval import register_retrieval_tools
from .tools.search import register_search_tools
from .tools.system import register_system_tools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("mcp-server")

# ``import fastmcp`` gives its ``fastmcp`` logger a Rich handler of its own
# and stops it propagating. Route it through the root handler instead, so
# its records share this service's format and pass the filters below.
_fastmcp_logger = logging.getLogger("fastmcp")
for _handler in list(_fastmcp_logger.handlers):
    _fastmcp_logger.removeHandler(_handler)
_fastmcp_logger.propagate = True


class _SilenceClientDisconnect(logging.Filter):
    """Drop benign ``ClientDisconnect`` noise from the MCP SDK's logs.

    Streamable HTTP clients can open an MCP transport session via ``POST /mcp``
    and sometimes abandon it before sending the body — typically when a client
    retries a different request shape, or the previous call already returned
    what it needed. The MCP SDK catches the resulting
    ``starlette.requests.ClientDisconnect`` cleanly and the connection ends
    without harm, but ``mcp.server.streamable_http`` logs it at ERROR as
    ``"Error handling POST request"`` with the ``ClientDisconnect``
    traceback in ``exc_info``. Filter by exception class.

    Records with any other exception still propagate unchanged so a real
    bug surfaces normally.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            exc_type = record.exc_info[0]
            if exc_type is not None and exc_type.__name__ == "ClientDisconnect":
                return False
        return True


# Attach the filter to the MCP SDK logger known to surface
# ``ClientDisconnect`` tracebacks. Limited scope on purpose: filtering at
# the root logger would risk swallowing a future, genuinely-different
# ``ClientDisconnect`` somewhere in the stack.
logging.getLogger("mcp.server.streamable_http").addFilter(_SilenceClientDisconnect())


class _DropToolErrorDetail(logging.Filter):
    """Keep tool failure text out of fastmcp's ``Error calling tool`` log.

    fastmcp logs every failed tool call at ERROR, and an exception other
    than ``ToolError`` with its traceback, whose message can quote mail
    content or a provider response. The record keeps the tool name; the
    traceback (and the exception text in it) is dropped. The caller still
    receives the error result.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage().startswith("Error calling tool"):
            record.exc_info = None
            record.exc_text = None
        return True


logging.getLogger("fastmcp.server.server").addFilter(_DropToolErrorDetail())


_INFERENCE_MODES = frozenset({"anthropic", "openai", "none"})
_EMBED_MODES = frozenset({"openai"})
_RERANK_MODES = frozenset({"cohere", "none"})


def _normalize_mode(name: str, raw: str, allowed: frozenset[str]) -> str:
    mode = raw.strip().lower()
    if mode in allowed:
        return mode
    allowed_repr = ", ".join(sorted(allowed))
    raise ValueError(f"{name} must be one of: {allowed_repr}")


def _require_env(mode_name: str, mode: str, var_name: str, value: str) -> str:
    """Fail fast at startup when a layer is active but its config is missing.

    This is the no-fallback rule: choosing a mode is intentional. A mode
    selected without its required vars surfaces as a startup error, never
    a silent reroute to a different provider.
    """
    if not value:
        raise ValueError(f"{var_name} must be set when {mode_name}={mode!r}")
    return value


def _read_secret(secret_name: str, env_fallback: str = "") -> str:
    """Read a Docker secret file, falling back to an environment variable.

    Prefer the secret file so the value is never exposed via docker inspect.
    The env fallback serves local dev runs without Docker secrets
    configured.

    Both paths strip surrounding whitespace. Operators using ``echo`` or
    a heredoc to write a secret file commonly leave a trailing newline,
    and a value pasted into an env var can pick up stray spaces — sending
    whitespace as part of the bearer credential fails in a non-obvious
    way at first call, so normalize both sources at the boundary.
    """
    path = Path(f"/run/secrets/{secret_name}")
    if path.exists():
        return path.read_text().strip()
    return os.environ.get(env_fallback, "").strip()


# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
SQLITE_PATH = os.environ.get("SQLITE_PATH", "/data/mail.db")


# Each layer (inference / embed / rerank) is selected by its ``*_MODE``
# variable. The same shape applies across all three:
#
#   {LAYER}_MODE      = anthropic|openai|none / openai / cohere|none
#   {LAYER}_BASE_URL  = endpoint URL — OPTIONAL (empty = SDK default)
#   {LAYER}_MODEL     = model id (required, no SDK default exists)
#   {LAYER}_API_KEY   = bearer credential (required, non-empty)
#
# Embed has no disabled mode because semantic / hybrid search is the
# headline retrieval feature and the indexer cannot run without an
# embedder either; ``EMBED_MODE=openai`` is the only valid value and
# is kept as a config knob purely for symmetry with the other layers.
# ``INFERENCE_MODE=none`` skips registration of the intelligence tools;
# ``RERANK_MODE=none`` disables the rerank stage in hybrid search.
#
# Validation is strict and fail-closed: a chosen mode without its
# required vars raises at startup. There is no inter-mode fallback —
# choosing ``anthropic`` and forgetting the API key surfaces here, not
# silently as a reroute to the OpenAI-shaped client. ``*_BASE_URL`` is
# the one var that may be empty: empty means "use the SDK's documented
# default" (OpenAI proper for openai/embed modes, Anthropic API for
# anthropic mode, Cohere API for cohere mode). The required
# ``*_API_KEY`` is the explicit-intent signal — an operator with a
# real ``sk-...`` has unambiguously chosen their provider, so we
# trust an empty base URL as "I want the SDK default" rather than
# "I forgot to configure."
def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    """Read a finite float of at least ``minimum`` from the environment.

    Used for per-call HTTP deadlines. An unset or empty variable returns
    ``default``; a value that does not parse as a number, is non-finite,
    or is below ``minimum`` raises ``ValueError`` so the misconfiguration
    fails startup instead of being silently replaced.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    # ``float("nan")`` / ``float("inf")`` parse cleanly and ``nan <
    # minimum`` is always False, so a non-finite value would otherwise
    # slip past the bounds check and reach the SDK client as a
    # per-call deadline.
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {raw!r}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _flag_env(name: str) -> bool:
    """Read an on/off flag: unset, empty or ``false`` is off, ``true`` is
    on (case-insensitive). Anything else raises ``ValueError`` so a typo
    fails startup instead of silently leaving the flag off or on."""
    raw = os.environ.get(name, "").strip().lower()
    if raw in {"", "false"}:
        return False
    if raw == "true":
        return True
    raise ValueError(f"{name} must be 'true' or 'false'")


def _reject_url_userinfo(name: str, value: str) -> str:
    """Reject URLs that embed a ``user:pass@host`` userinfo authority.

    Resolved base URLs flow into startup log lines naming the wire
    endpoint, so embedded credentials would leak to container logs /
    journald. The credential model puts every secret in a Docker-secrets
    file (``.secrets/<layer>_api_key.txt``); a URL with userinfo means
    the operator is trying to authenticate out-of-band and exposing the
    credential. Mirrors the same guard in ``scripts/validate-env.sh`` so
    a deployment that skipped that script (CI, tests, manual ``docker
    compose up``) still fails closed instead of leaking.
    """
    if not value:
        return value
    if "@" in urllib.parse.urlsplit(value).netloc:
        raise ValueError(
            f"{name} must not embed credentials (user:pass@host). Put the "
            "API key in the matching .secrets/<layer>_api_key.txt file instead."
        )
    return value


INFERENCE_MODE = _normalize_mode(
    "INFERENCE_MODE", os.environ.get("INFERENCE_MODE", "anthropic"), _INFERENCE_MODES
)
INFERENCE_BASE_URL = _reject_url_userinfo(
    "INFERENCE_BASE_URL", os.environ.get("INFERENCE_BASE_URL", "")
)
INFERENCE_MODEL = os.environ.get("INFERENCE_MODEL", "")
INFERENCE_API_KEY = _read_secret("inference_api_key", "INFERENCE_API_KEY")
INFERENCE_TIMEOUT_SECS = _float_env(
    "INFERENCE_TIMEOUT_SECS", DEFAULT_COMPLETE_TIMEOUT_SECS, minimum=1.0
)
INFERENCE_MAX_TOKENS = _int_env("INFERENCE_MAX_TOKENS", DEFAULT_MAX_TOKENS, minimum=1)
# The model's context window in tokens; every intelligence prompt plus
# INFERENCE_MAX_TOKENS of reply is fitted into it (#285). Set it to a
# small local model's window; ``PromptBudget`` rejects a window with too
# little room left for a prompt at startup.
INFERENCE_CONTEXT_TOKENS = _int_env("INFERENCE_CONTEXT_TOKENS", DEFAULT_CONTEXT_TOKENS, minimum=1)

EMBED_MODE = _normalize_mode("EMBED_MODE", os.environ.get("EMBED_MODE", "openai"), _EMBED_MODES)
EMBED_BASE_URL = _reject_url_userinfo("EMBED_BASE_URL", os.environ.get("EMBED_BASE_URL", ""))
EMBED_MODEL = os.environ.get("EMBED_MODEL", "")
EMBED_API_KEY = _read_secret("embed_api_key", "EMBED_API_KEY")
EMBED_TIMEOUT_SECS = _float_env("EMBED_TIMEOUT_SECS", DEFAULT_EMBED_TIMEOUT_SECS, minimum=1.0)

RERANK_MODE = _normalize_mode("RERANK_MODE", os.environ.get("RERANK_MODE", "none"), _RERANK_MODES)
RERANK_BASE_URL = _reject_url_userinfo("RERANK_BASE_URL", os.environ.get("RERANK_BASE_URL", ""))
RERANK_MODEL = os.environ.get("RERANK_MODEL", "")
RERANK_API_KEY = _read_secret("rerank_api_key", "RERANK_API_KEY")
RERANK_CANDIDATES = _int_env("RERANK_CANDIDATES", 20, minimum=1)
RERANK_TIMEOUT_SECS = _float_env("RERANK_TIMEOUT_SECS", DEFAULT_RERANK_TIMEOUT_SECS, minimum=1.0)

MCP_PORT = int(os.environ.get("MCP_PORT", "3000"))
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "sse")
# Seconds a Streamable HTTP session may sit idle before the server ends
# it (#317). fastmcp's default is no limit, so a session a client
# abandons without a DELETE would hold its server task until shutdown.
# A client whose session expires gets 404 on its next request and starts
# a new one.
MCP_SESSION_IDLE_TIMEOUT_SECS = _float_env("MCP_SESSION_IDLE_TIMEOUT_SECS", 1800.0, minimum=1.0)
# Experimental tools (brief_issue, check_conclusion) are registered only
# when this is ``true``; their output format may change (PLAN.md Resolved
# decisions 12).
MCP_EXPERIMENTAL_TOOLS = _flag_env("MCP_EXPERIMENTAL_TOOLS")

# Paths the transports are served on; fastmcp's defaults, pinned here so
# the dual-transport dispatch and the docs cannot drift from them.
_STREAMABLE_HTTP_PATH = "/mcp"
_SSE_PATH = "/sse"

# Host/Origin allowlist applied to every HTTP request before it reaches a
# transport, so a malicious local browser page cannot DNS-rebind to this
# listener even though the Docker port mapping keeps the host-facing
# endpoint loopback-only. An entry ending ``:*`` matches that host with
# any port; any other entry matches exactly. A request without an Origin
# header passes the Origin check.
_TRANSPORT_SECURITY = TransportSecuritySettings(
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


_Transport = Literal["sse", "streamable-http", "dual"]


def _normalize_transport(raw: str) -> _Transport:
    transport = raw.strip().lower()
    if transport == "sse":
        return "sse"
    if transport == "streamable-http":
        return "streamable-http"
    if transport == "dual":
        return "dual"
    raise ValueError("MCP_TRANSPORT must be one of: sse, streamable-http, dual")


class _HostOriginGuard:
    """ASGI middleware: reject a request whose Host (421) or Origin (403)
    is not in ``_TRANSPORT_SECURITY`` before it reaches any route.

    The check is the MCP SDK's own validator, so the allowlist means what
    it meant before the move to fastmcp. fastmcp's
    ``HostOriginGuardMiddleware`` is not used: it also accepts the
    server's own socket address as a Host and any loopback Origin on any
    scheme, which this allowlist does not. It runs ahead of the Streamable
    HTTP session manager, so a rejected request creates no session.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._validator = TransportSecurityMiddleware(_TRANSPORT_SECURITY)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            # ``is_post=False``: the transports check a POST's
            # Content-Type themselves.
            error = await self._validator.validate_request(Request(scope), is_post=False)
            if error is not None:
                await error(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _build_app(server: FastMCP, transport: _Transport, *, session_idle_timeout: float) -> ASGIApp:
    """The ASGI app serving ``server`` on ``transport``.

    Every transport app carries ``_HostOriginGuard``; fastmcp's SSE app
    has no Host/Origin check of its own and its Streamable HTTP check is
    off by default. Custom routes (``/health``) are served by each app.
    Streamable HTTP sessions end after ``session_idle_timeout`` seconds
    without a request; it is required because fastmcp's default never
    ends them.
    """
    guard = [Middleware(_HostOriginGuard)]
    if transport == "sse":
        return server.http_app(path=_SSE_PATH, transport="sse", middleware=guard)
    if transport == "streamable-http":
        return server.http_app(
            path=_STREAMABLE_HTTP_PATH,
            transport="streamable-http",
            middleware=guard,
            session_idle_timeout=session_idle_timeout,
        )
    return _build_dual_app(server, guard, session_idle_timeout)


def _build_dual_app(
    server: FastMCP, guard: list[Middleware], session_idle_timeout: float
) -> ASGIApp:
    """Serve SSE and Streamable HTTP routes from one FastMCP instance.

    Each transport app is invoked as a complete ASGI app rather than
    having its routes flattened into a fresh Starlette — that
    preserves whatever middleware and per-request context plumbing
    fastmcp attaches to each ``http_app()`` (the Streamable HTTP
    transport in particular relies on session-manager context that
    lives on the inner app, not on individual routes).

    Lifespan is run on a tiny outer Starlette whose only job is to
    enter both inner apps' ``lifespan_context`` — ``session_manager``
    starts here for Streamable HTTP, and both enter the server's own
    lifespan, which fastmcp reference-counts. HTTP/WebSocket scopes go
    straight to the right transport app via prefix dispatch.
    """
    sse_app = server.http_app(path=_SSE_PATH, transport="sse", middleware=guard)
    streamable_http_app = server.http_app(
        path=_STREAMABLE_HTTP_PATH,
        transport="streamable-http",
        middleware=guard,
        session_idle_timeout=session_idle_timeout,
    )

    streamable_path = _STREAMABLE_HTTP_PATH
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
    lifespan_owner = Starlette(lifespan=combined_lifespan)

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            await lifespan_owner(scope, receive, send)
            return
        path = scope.get("path", "/")
        # Streamable HTTP claims exactly the configured streamable path
        # (``/mcp``) and any sub-path under it. Everything else —
        # ``/sse``, ``/messages/``, the ``/health`` custom route
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

    return app


async def _health_response(db: Database) -> JSONResponse:
    """Body of the ``/health`` route. The probe opens SQLite, so it runs
    in a worker thread rather than on the shared event loop."""
    try:
        await asyncio.to_thread(db.ping)
    except Exception:
        log.exception("health probe failed")
        return JSONResponse({"status": "unhealthy"}, status_code=503)
    return JSONResponse({"status": "ok"})


def _run_server(server: FastMCP, transport: _Transport) -> None:
    """Serve ``server`` on ``transport`` with uvicorn.

    The app is built here and handed to uvicorn directly rather than
    through ``FastMCP.run``, which would print fastmcp's banner and check
    PyPI for a newer release. ``host="0.0.0.0"`` is required so the
    in-container bind is reachable through the Docker port-forward; the
    host-side mapping in ``docker-compose.yml`` keeps the port
    loopback-only (``127.0.0.1:${MCP_PORT}:${MCP_PORT}``).

    The ``_Transport`` Literal type forces every caller — production
    or test — to pass a value already returned by ``_normalize_transport``.
    """
    config = uvicorn.Config(
        _build_app(server, transport, session_idle_timeout=MCP_SESSION_IDLE_TIMEOUT_SECS),
        host="0.0.0.0",  # nosec B104 — see docstring
        port=MCP_PORT,
        log_level="info",
    )
    uvicorn.Server(config).run()


def main():
    # Validate per-mode required vars BEFORE constructing service clients
    # or opening the SQLite database. A missing volume mount or bad DB
    # path is a much less common operator error than a missing env var,
    # so let env validation fire first — otherwise a bad SQLITE_PATH
    # would mask the real "you forgot INFERENCE_API_KEY" failure. A
    # chosen mode with missing config raises here so the operator sees a
    # precise error rather than a runtime fallback to a different
    # provider.
    # Every enabled layer requires its API key (non-empty) at startup —
    # this is the explicit-intent signal. An operator with a real
    # ``sk-...`` has unambiguously chosen their provider, so an empty
    # ``*_BASE_URL`` is interpreted as "I want the SDK default" rather
    # than "I forgot to configure"; the required ``*_API_KEY`` is what
    # guards against an accidental ship-to-OpenAI/Anthropic from a
    # forgotten env var (a typo can't produce a real bearer credential).
    # ``*_MODEL`` is always required because no SDK has a default model
    # — empty ``model`` always fails at request time.
    _require_env("EMBED_MODE", EMBED_MODE, "EMBED_MODEL", EMBED_MODEL)
    _require_env("EMBED_MODE", EMBED_MODE, "EMBED_API_KEY", EMBED_API_KEY)
    embed_client = EmbedClient(
        base_url=EMBED_BASE_URL,
        model=EMBED_MODEL,
        api_key=EMBED_API_KEY,
        timeout_secs=EMBED_TIMEOUT_SECS,
    )
    # An empty ``*_BASE_URL`` lets the SDK read its own env var
    # (``OPENAI_BASE_URL``, ``ANTHROPIC_BASE_URL``, ``CO_API_URL``),
    # which bypasses the config-load userinfo guard above. Re-check the
    # resolved endpoint before it reaches the startup log or an error.
    _reject_url_userinfo("EMBED_BASE_URL (or the SDK's OPENAI_BASE_URL)", embed_client.base_url)

    inference_client: InferenceClient | None = None
    prompt_budget: PromptBudget | None = None
    if INFERENCE_MODE in {"openai", "anthropic"}:
        _require_env("INFERENCE_MODE", INFERENCE_MODE, "INFERENCE_MODEL", INFERENCE_MODEL)
        _require_env("INFERENCE_MODE", INFERENCE_MODE, "INFERENCE_API_KEY", INFERENCE_API_KEY)
        prompt_budget = PromptBudget(
            context_tokens=INFERENCE_CONTEXT_TOKENS, max_output_tokens=INFERENCE_MAX_TOKENS
        )
        inference_client = InferenceClient.create(
            mode=INFERENCE_MODE,
            base_url=INFERENCE_BASE_URL,
            model=INFERENCE_MODEL,
            api_key=INFERENCE_API_KEY,
            max_tokens=INFERENCE_MAX_TOKENS,
            timeout_secs=INFERENCE_TIMEOUT_SECS,
        )
        _reject_url_userinfo(
            "INFERENCE_BASE_URL (or the SDK's OPENAI_BASE_URL / ANTHROPIC_BASE_URL)",
            inference_client.base_url,
        )

    reranker: CohereReranker | None = None
    if RERANK_MODE == "cohere":
        _require_env("RERANK_MODE", RERANK_MODE, "RERANK_MODEL", RERANK_MODEL)
        _require_env("RERANK_MODE", RERANK_MODE, "RERANK_API_KEY", RERANK_API_KEY)
        # The Cohere SDK exposes its resolved URL only through private
        # API, so check the env var it falls back to directly.
        _reject_url_userinfo(
            "RERANK_BASE_URL (or the SDK's CO_API_URL)",
            RERANK_BASE_URL or os.environ.get("CO_API_URL", ""),
        )
        reranker = CohereReranker(
            RerankConfig(
                base_url=RERANK_BASE_URL,
                model=RERANK_MODEL,
                api_key=RERANK_API_KEY,
                candidates=RERANK_CANDIDATES,
                timeout_secs=RERANK_TIMEOUT_SECS,
            )
        )

    # Env validated — now open the SQLite index.
    db = Database(SQLITE_PATH)

    # Read the declared embedding dim from ``message_chunks_vec`` so the
    # tool layer can reject wrong-shaped query vectors before they reach
    # sqlite-vec MATCH (where the broad ``except`` in
    # ``_chunk_vector_search`` would otherwise swallow them as a silent
    # "no results"). ``None`` is expected on a fresh install where the
    # indexer has not yet run its schema migrations; the tool layer
    # treats that as skip-validation.
    expected_embed_dim = db.get_embedding_dim()

    # FastMCP server — provides the @server.tool() decorator and the
    # SSE / Streamable HTTP apps ``_run_server`` serves per MCP_TRANSPORT,
    # each behind the ``_TRANSPORT_SECURITY`` Host/Origin allowlist.
    server = FastMCP("protonmail-local-ai")

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
        return await _health_response(db)

    # All operator-configured API keys, scrubbed from any exception
    # text echoed back to the caller or written to logs. The empty
    # filter strips disabled-layer placeholders so ``redact_sensitive_text``
    # doesn't waste a no-op replace pass on them.
    secret_values = [k for k in (INFERENCE_API_KEY, EMBED_API_KEY, RERANK_API_KEY) if k]

    # Register all tool groups. Intelligence tools require inference;
    # the group is skipped when ``INFERENCE_MODE=none`` so a mailbox
    # without an inference provider still serves keyword / semantic /
    # hybrid retrieval cleanly.
    register_search_tools(
        server,
        db,
        embed_client,
        reranker=reranker,
        secret_values=secret_values,
        expected_embed_dim=expected_embed_dim,
    )
    register_retrieval_tools(server, db)
    if inference_client is not None:
        register_intelligence_tools(
            server,
            db,
            embed_client,
            inference_client,
            reranker=reranker,
            secret_values=secret_values,
            expected_embed_dim=expected_embed_dim,
            prompt_budget=prompt_budget,
        )
    else:
        log.info("Intelligence tools not registered (INFERENCE_MODE=none).")
    # Experimental tools are opt-in, and the current ones need inference.
    if MCP_EXPERIMENTAL_TOOLS and inference_client is not None:
        register_experimental_tools(
            server,
            db,
            embed_client,
            inference_client,
            reranker=reranker,
            secret_values=secret_values,
            expected_embed_dim=expected_embed_dim,
            prompt_budget=prompt_budget,
        )
        log.info(
            "Experimental tools registered (MCP_EXPERIMENTAL_TOOLS=true): "
            "brief_issue, check_conclusion."
        )
    elif MCP_EXPERIMENTAL_TOOLS:
        log.info("Experimental tools not registered: they need inference (INFERENCE_MODE=none).")
    register_system_tools(server, db)

    transport = _normalize_transport(MCP_TRANSPORT)

    log.info(f"MCP server starting on port {MCP_PORT}")
    log.info(f"  SQLite:   {SQLITE_PATH}")
    log.info(f"  Embed mode:     {EMBED_MODE}")
    # Surface the resolved wire endpoint, not the raw env var.
    # ``EMBED_BASE_URL=""`` intentionally means "use the SDK default"
    # (OpenAI proper) — printing the empty string hides that an
    # unauthenticated host-side server isn't actually being used and
    # the request is going to api.openai.com. ``EmbedClient.base_url``
    # reads the URL back from the SDK after fallback resolution,
    # matching the inference / rerank log lines below.
    log.info(f"  Embed:          {embed_client.base_url} (model={EMBED_MODEL})")
    log.info(f"  Inference mode: {INFERENCE_MODE}")
    if inference_client is not None:
        # Surface the resolved wire endpoint, not "(SDK default)". In a
        # privacy-sensitive deployment retrieved email excerpts go to
        # whatever URL the SDK resolves to, so the operator-facing log
        # must name it explicitly. ``InferenceClient.base_url`` reads
        # the URL back from the SDK after fallback resolution — same
        # pattern as ``EmbedClient.base_url`` above.
        log.info(f"  Inference:      {inference_client.base_url} (model={INFERENCE_MODEL})")
    log.info(f"  Rerank mode:    {RERANK_MODE}")
    if reranker is not None:
        log.info(
            f"  Rerank:         {RERANK_BASE_URL or '(SDK default)'} "
            f"(model={RERANK_MODEL}, candidates={RERANK_CANDIDATES})"
        )
    log.info(f"  Transport: {transport}")
    if transport != "sse":
        log.info(f"  Session idle timeout: {MCP_SESSION_IDLE_TIMEOUT_SECS:g}s")
    log.info("  Retrieval: local SQLite index only")

    _run_server(server, transport)


if __name__ == "__main__":
    main()
