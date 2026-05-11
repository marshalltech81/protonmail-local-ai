"""
MCP Server entry point.
Exposes local mailbox search, retrieval, intelligence, and system tools over
MCP transports. Mail-changing action tools are disabled by default until a safe
opt-in write backend exists.
"""

import contextlib
import logging
import math
import os
from pathlib import Path
from typing import Literal

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from .lib.embed import DEFAULT_EMBED_TIMEOUT_SECS, EmbedClient
from .lib.inference import (
    DEFAULT_COMPLETE_TIMEOUT_SECS,
    DEFAULT_MAX_TOKENS,
    InferenceClient,
)
from .lib.reranker import DEFAULT_RERANK_TIMEOUT_SECS, CohereReranker, RerankConfig
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

    Streamable HTTP clients can open an MCP transport session via ``POST /mcp``
    and sometimes abandon it before sending the body — typically when a client
    retries a different request shape, or the previous call already returned
    what it needed. The MCP SDK catches the resulting
    ``starlette.requests.ClientDisconnect`` cleanly and the connection ends
    without harm, but the SDK logs it at ERROR level on two loggers in two
    different shapes:

    - ``mcp.server.streamable_http`` emits ``"Error handling POST request"``
      with ``exc_info`` set to the ``ClientDisconnect`` traceback. Filter
      by exception class.
    - ``mcp.server.lowlevel.server`` emits ``"Received exception from
      stream:"`` (with NO ``exc_info`` — the SDK catches the exception
      upstream and writes the formatted repr into the message). The
      same prefix is also used for genuinely-different exceptions
      caught off the stream, so we cannot suppress the prefix
      unconditionally — that would hide real failures like
      ``RuntimeError("boom")``. Drop only the two recognizable
      disconnect forms: an empty trailing message (the bare
      ``ClientDisconnect`` signature) or a trailing message that
      explicitly names the class.

    Records that don't match either shape still propagate unchanged so a
    real bug surfaces normally.
    """

    _STREAM_PREFIX = "Received exception from stream:"

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            exc_type = record.exc_info[0]
            if exc_type is not None and exc_type.__name__ == "ClientDisconnect":
                return False
        # ``getMessage`` resolves the format string + args the same way
        # the formatter would; checking ``record.msg`` alone would miss
        # any record built with logging-format args.
        message = record.getMessage()
        if message.startswith(self._STREAM_PREFIX):
            trailing = message[len(self._STREAM_PREFIX) :].strip()
            # Empty trailing == the ClientDisconnect bare signature
            # observed during eval ("Received exception from stream: ").
            # ClientDisconnect-bearing trailing == any wording that
            # explicitly names the class. Anything else (real exceptions
            # the SDK chose to surface) falls through and propagates.
            if not trailing or "ClientDisconnect" in trailing:
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


# Each layer (inference / embed / rerank) is selected by its ``*_MODE``
# variable. The same shape applies across all three:
#
#   {LAYER}_MODE      = anthropic|openai|none / openai / cohere|none
#   {LAYER}_BASE_URL  = endpoint URL (when the chosen mode needs one)
#   {LAYER}_MODEL     = model id served at that endpoint
#   {LAYER}_API_KEY   = bearer credential (Docker secret preferred)
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
# silently as a reroute to the OpenAI-shaped client.
def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    """Read a positive float from the environment with a fallback.

    Used for per-call HTTP deadlines so a typo or empty string falls back
    to the library default rather than raising at startup.
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


INFERENCE_MODE = _normalize_mode(
    "INFERENCE_MODE", os.environ.get("INFERENCE_MODE", "anthropic"), _INFERENCE_MODES
)
INFERENCE_BASE_URL = os.environ.get("INFERENCE_BASE_URL", "")
INFERENCE_MODEL = os.environ.get("INFERENCE_MODEL", "")
INFERENCE_API_KEY = _read_secret("inference_api_key", "INFERENCE_API_KEY")
INFERENCE_TIMEOUT_SECS = _float_env(
    "INFERENCE_TIMEOUT_SECS", DEFAULT_COMPLETE_TIMEOUT_SECS, minimum=1.0
)
INFERENCE_MAX_TOKENS = _int_env("INFERENCE_MAX_TOKENS", DEFAULT_MAX_TOKENS, minimum=1)

EMBED_MODE = _normalize_mode("EMBED_MODE", os.environ.get("EMBED_MODE", "openai"), _EMBED_MODES)
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "")
EMBED_API_KEY = _read_secret("embed_api_key", "EMBED_API_KEY")
EMBED_TIMEOUT_SECS = _float_env("EMBED_TIMEOUT_SECS", DEFAULT_EMBED_TIMEOUT_SECS, minimum=1.0)

RERANK_MODE = _normalize_mode("RERANK_MODE", os.environ.get("RERANK_MODE", "none"), _RERANK_MODES)
RERANK_BASE_URL = os.environ.get("RERANK_BASE_URL", "")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "")
RERANK_API_KEY = _read_secret("rerank_api_key", "RERANK_API_KEY")
RERANK_CANDIDATES = _int_env("RERANK_CANDIDATES", 20, minimum=1)
RERANK_TOP_N = _int_env("RERANK_TOP_N", 10, minimum=1)
RERANK_TIMEOUT_SECS = _float_env("RERANK_TIMEOUT_SECS", DEFAULT_RERANK_TIMEOUT_SECS, minimum=1.0)

MCP_PORT = int(os.environ.get("MCP_PORT", "3000"))
MCP_READ_ONLY = _env_bool("MCP_READ_ONLY", True)
MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "sse")


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


def _run_server(server: FastMCP, transport: _Transport) -> None:
    """Run ``server`` on ``transport``.

    The ``_Transport`` Literal type forces every caller — production
    or test — to pass a value already returned by ``_normalize_transport``.
    mypy catches a raw-string call site, so the runtime never re-does
    work the caller already did.
    """
    if transport == "dual":
        import anyio

        anyio.run(lambda: _run_dual_transport_async(server))
        return
    # Type narrowing on the Literal handles the dispatch; transport is
    # provably "sse" | "streamable-http" here, no cast needed.
    server.run(transport=transport)


def main():
    # Validate per-mode required vars BEFORE constructing service clients
    # or opening the SQLite database. A missing volume mount or bad DB
    # path is a much less common operator error than a missing env var,
    # so let env validation fire first — otherwise a bad SQLITE_PATH
    # would mask the real "you forgot INFERENCE_API_KEY" failure. A
    # chosen mode with missing config raises here so the operator sees a
    # precise error rather than a runtime fallback to a different
    # provider.
    _require_env("EMBED_MODE", EMBED_MODE, "EMBED_BASE_URL", EMBED_BASE_URL)
    _require_env("EMBED_MODE", EMBED_MODE, "EMBED_MODEL", EMBED_MODEL)
    embed_client = EmbedClient(
        base_url=EMBED_BASE_URL,
        model=EMBED_MODEL,
        api_key=EMBED_API_KEY,
        timeout_secs=EMBED_TIMEOUT_SECS,
    )

    inference_client: InferenceClient | None = None
    if INFERENCE_MODE in {"openai", "anthropic"}:
        _require_env("INFERENCE_MODE", INFERENCE_MODE, "INFERENCE_MODEL", INFERENCE_MODEL)
        if INFERENCE_MODE == "openai":
            # OpenAI-compatible mode points at an operator-supplied endpoint
            # (remote provider or host-side server), so a base URL is
            # required. Anthropic mode passes an empty base_url through
            # to the SDK so its real default applies — we never substitute
            # a hardcoded constant the SDK might later drift from.
            #
            # INFERENCE_API_KEY is only required for anthropic. openai
            # mode accepts an empty key so the local-only path (LM Studio,
            # vLLM, mlx_lm.server, etc.) works without a placeholder
            # value the operator has to invent. _OpenAIBackend substitutes
            # ``"unauthenticated"`` to satisfy the SDK constructor.
            _require_env("INFERENCE_MODE", INFERENCE_MODE, "INFERENCE_BASE_URL", INFERENCE_BASE_URL)
        else:
            _require_env("INFERENCE_MODE", INFERENCE_MODE, "INFERENCE_API_KEY", INFERENCE_API_KEY)
        inference_client = InferenceClient.create(
            mode=INFERENCE_MODE,
            base_url=INFERENCE_BASE_URL,
            model=INFERENCE_MODEL,
            api_key=INFERENCE_API_KEY,
            max_tokens=INFERENCE_MAX_TOKENS,
            timeout_secs=INFERENCE_TIMEOUT_SECS,
        )

    reranker: CohereReranker | None = None
    if RERANK_MODE == "cohere":
        _require_env("RERANK_MODE", RERANK_MODE, "RERANK_MODEL", RERANK_MODEL)
        _require_env("RERANK_MODE", RERANK_MODE, "RERANK_API_KEY", RERANK_API_KEY)
        reranker = CohereReranker(
            RerankConfig(
                base_url=RERANK_BASE_URL,
                model=RERANK_MODEL,
                api_key=RERANK_API_KEY,
                candidates=RERANK_CANDIDATES,
                top_n=RERANK_TOP_N,
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
    # this listener, even though the Docker port mapping keeps the
    # host-facing endpoint loopback-only.
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
        )
    else:
        log.info("Intelligence tools not registered (INFERENCE_MODE=none).")
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
    log.info(f"  Embed mode:     {EMBED_MODE}")
    if embed_client is not None:
        log.info(f"  Embed:          {EMBED_BASE_URL} (model={EMBED_MODEL})")
    log.info(f"  Inference mode: {INFERENCE_MODE}")
    if inference_client is not None:
        # Anthropic mode may use the SDK default URL when INFERENCE_BASE_URL
        # is empty; surface what was actually wired so the log doesn't
        # imply a configured value when none was set.
        log.info(
            f"  Inference:      {INFERENCE_BASE_URL or '(SDK default)'} (model={INFERENCE_MODEL})"
        )
    log.info(f"  Rerank mode:    {RERANK_MODE}")
    if reranker is not None:
        log.info(
            f"  Rerank:         {RERANK_BASE_URL or '(SDK default)'} "
            f"(model={RERANK_MODEL}, candidates={RERANK_CANDIDATES}, top_n={RERANK_TOP_N})"
        )
    log.info(f"  Transport: {transport}")
    log.info("  Retrieval: local SQLite index only")

    _run_server(server, transport)


if __name__ == "__main__":
    main()
