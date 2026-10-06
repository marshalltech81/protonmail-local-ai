"""
MCP Server entry point.
Exposes local mailbox search, retrieval, intelligence, and system tools over
MCP's Streamable HTTP transport at ``/mcp``, behind a static bearer token.
The server is read-only: it has no mail-changing tools and no
connection to Bridge.
"""

import asyncio
import hmac
import logging
import math
import os
import re
import urllib.parse
from pathlib import Path

import fastmcp
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .lib.embed import DEFAULT_EMBED_TIMEOUT_SECS, EmbedClient
from .lib.embed_identity import run_startup_identity_check
from .lib.inference import (
    DEFAULT_COMPLETE_TIMEOUT_SECS,
    InferenceClient,
    PromptBudget,
    default_token_budget,
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

# fastmcp's default ``telemetry_mode`` is ``native``: it creates
# OpenTelemetry spans and propagates trace context, a no-op only while no
# OTel SDK and exporter are configured. ``off`` creates no spans and leaves
# the OTel context untouched. Set here rather than as an image ``ENV`` so it
# holds wherever ``src.main`` runs, and over any ``FASTMCP_TELEMETRY_MODE``.
fastmcp.settings.telemetry_mode = "off"


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


class _DropRawHostOriginWarning(logging.Filter):
    """Drop the MCP SDK's Host/Origin rejection warnings (#878).

    ``mcp.server.transport_security`` logs a rejected request's raw Host
    or Origin header, text any client chose. ``_HostOriginGuard`` logs
    the rejection itself with a fixed reason instead. Other records from
    the logger pass unchanged.
    """

    _PREFIXES = ("Invalid Host header", "Invalid Origin header", "Missing Host header")

    def filter(self, record: logging.LogRecord) -> bool:
        return not str(record.msg).startswith(self._PREFIXES)


logging.getLogger("mcp.server.transport_security").addFilter(_DropRawHostOriginWarning())


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
    a silent reroute to a different provider. Values are non-empty after
    trimming (#750), and the trimmed value is returned.
    """
    value = value.strip()
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
#   {LAYER}_BASE_URL  = endpoint URL, or ``default`` for the SDK's own
#                       endpoint (required, non-empty; #750)
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
# silently as a reroute to the OpenAI-shaped client. ``*_BASE_URL``
# must name the endpoint: a URL, or ``default`` for the SDK's
# documented default (OpenAI proper for openai/embed modes, Anthropic
# API for anthropic mode, Cohere API for cohere mode). An empty value
# is a startup error (``_resolve_base_url``): an API key is not consent
# to the SDK's default endpoint, because the request body (mail text)
# is sent before the provider checks the key (owner decision
# 2026-10-05, #750).
def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    """Read a finite float of at least ``minimum`` from the environment.

    Used for per-operation HTTP timeouts (not total-call deadlines). An unset or empty variable returns
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


def _flag_env(name: str, default: bool = False) -> bool:
    """Read an on/off flag: ``true`` is on and ``false`` off
    (case-insensitive), unset or empty is ``default``. Anything else raises
    ``ValueError`` so a typo fails startup instead of silently leaving the
    flag off or on."""
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    if raw == "false":
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


# The literal ``*_BASE_URL`` value that selects the provider's official
# endpoint, compared trimmed and case-insensitively; that endpoint's
# host per mode, named in the startup error and the privacy warning; and
# the URL ``default`` resolves to. The URL is passed to the SDK
# explicitly: left out, each SDK would read its own variable first
# (``OPENAI_BASE_URL``, ``ANTHROPIC_BASE_URL``, ``CO_API_URL``), which
# could send mail somewhere the operator did not choose (Codex round 1
# on #773).
_SDK_DEFAULT_BASE_URL = "default"
_SDK_DEFAULT_HOSTS = {
    "anthropic": "api.anthropic.com",
    "openai": "api.openai.com",
    "cohere": "api.cohere.com",
}
_SDK_DEFAULT_URLS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "cohere": "https://api.cohere.com",
}


def _resolve_base_url(name: str, raw: str, mode: str) -> str:
    """Return an enabled layer's base URL; ``default`` returns the
    provider's official URL (``_SDK_DEFAULT_URLS``).

    The official URL is passed to the SDK explicitly, so the SDK's own
    endpoint variable cannot redirect it (Codex round 1 on #773). An empty
    (or blank) value fails startup with fixed text naming the variable,
    both fixes and the host ``default`` would send mail to; it never
    echoes a value (#750).
    """
    value = raw.strip()
    if not value:
        raise ValueError(
            f"{name} is empty: set it to the provider's URL, or to `default` to use "
            f"the SDK's default endpoint (sends mail to {_SDK_DEFAULT_HOSTS[mode]})."
        )
    if value.lower() == _SDK_DEFAULT_BASE_URL:
        return _SDK_DEFAULT_URLS[mode]
    return value


# Endpoint hosts that keep a provider call on this machine: the host's
# loopback, or OrbStack's route from a container to it.
_HOST_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "host.docker.internal"})


def _warn_if_remote_endpoint(mode_setting: str, mode: str, url: str, sends: str) -> None:
    """Log one WARNING when an enabled layer's endpoint is not host-local.

    ``url`` is the resolved endpoint, or empty for an SDK default the
    client does not expose (always a remote provider). Only the host is
    named: never the path, query, port or the API key.
    """
    host = urllib.parse.urlsplit(url).hostname if url else None
    if host in _HOST_LOCAL_HOSTS:
        return
    log.warning(
        "Privacy: %s=%s sends %s off this host, to %s.",
        mode_setting,
        mode,
        sends,
        host or "the SDK's default endpoint",
    )


INFERENCE_MODE = _normalize_mode(
    "INFERENCE_MODE", os.environ.get("INFERENCE_MODE", "none"), _INFERENCE_MODES
)
INFERENCE_BASE_URL = _reject_url_userinfo(
    "INFERENCE_BASE_URL", os.environ.get("INFERENCE_BASE_URL", "")
)
INFERENCE_MODEL = os.environ.get("INFERENCE_MODEL", "").strip()
INFERENCE_API_KEY = _read_secret("inference_api_key", "INFERENCE_API_KEY")
INFERENCE_TIMEOUT_SECS = _float_env(
    "INFERENCE_TIMEOUT_SECS", DEFAULT_COMPLETE_TIMEOUT_SECS, minimum=1.0
)
# Unset values take the mode's defaults: larger in anthropic mode, where
# thinking counts against max_tokens (#764).
_DEFAULT_MAX_TOKENS, _DEFAULT_CONTEXT_TOKENS = default_token_budget(INFERENCE_MODE)
INFERENCE_MAX_TOKENS = _int_env("INFERENCE_MAX_TOKENS", _DEFAULT_MAX_TOKENS, minimum=1)
# The model's context window in tokens; every intelligence prompt plus
# INFERENCE_MAX_TOKENS of reply is fitted into it (#285). Set it to a
# small local model's window; ``PromptBudget`` rejects a window with too
# little room left for a prompt at startup.
INFERENCE_CONTEXT_TOKENS = _int_env("INFERENCE_CONTEXT_TOKENS", _DEFAULT_CONTEXT_TOKENS, minimum=1)
# In anthropic mode the JSON tools (extract_from_emails, brief_issue,
# check_conclusion) send their reply schema as a structured-output format
# (#808). Set false for a model or gateway without structured outputs.
INFERENCE_STRUCTURED_OUTPUT = _flag_env("INFERENCE_STRUCTURED_OUTPUT", default=True)

EMBED_MODE = _normalize_mode("EMBED_MODE", os.environ.get("EMBED_MODE", "openai"), _EMBED_MODES)
EMBED_BASE_URL = _reject_url_userinfo("EMBED_BASE_URL", os.environ.get("EMBED_BASE_URL", ""))
EMBED_MODEL = os.environ.get("EMBED_MODEL", "").strip()
EMBED_API_KEY = _read_secret("embed_api_key", "EMBED_API_KEY")
EMBED_TIMEOUT_SECS = _float_env("EMBED_TIMEOUT_SECS", DEFAULT_EMBED_TIMEOUT_SECS, minimum=1.0)

RERANK_MODE = _normalize_mode("RERANK_MODE", os.environ.get("RERANK_MODE", "none"), _RERANK_MODES)
RERANK_BASE_URL = _reject_url_userinfo("RERANK_BASE_URL", os.environ.get("RERANK_BASE_URL", ""))
RERANK_MODEL = os.environ.get("RERANK_MODEL", "").strip()
RERANK_API_KEY = _read_secret("rerank_api_key", "RERANK_API_KEY")
RERANK_CANDIDATES = _int_env("RERANK_CANDIDATES", 20, minimum=1)
RERANK_TIMEOUT_SECS = _float_env("RERANK_TIMEOUT_SECS", DEFAULT_RERANK_TIMEOUT_SECS, minimum=1.0)

MCP_PORT = int(os.environ.get("MCP_PORT", "3000"))

# Static bearer token every ``/mcp`` request must present (PLAN.md
# Resolved decisions 13). Compose mounts it as the ``mcp_auth_token``
# Docker secret; the ``MCP_AUTH_TOKEN`` fallback is only for running the
# server outside a container. ``main`` fails startup when it is empty.
MCP_AUTH_TOKEN = _read_secret("mcp_auth_token", "MCP_AUTH_TOKEN")


def _check_transport(raw: str) -> None:
    """Fail startup unless ``MCP_TRANSPORT`` is unset, empty or
    ``streamable-http``.

    Streamable HTTP at ``/mcp`` is the only transport (#498). The variable
    is still read so a ``.env`` left over from a release that served the
    legacy SSE transport fails with migration steps instead of being
    silently ignored.
    """
    transport = raw.strip().lower()
    if transport in {"", "streamable-http"}:
        return
    if transport in {"sse", "dual"}:
        raise ValueError(
            f"MCP_TRANSPORT={transport} was removed: Streamable HTTP is the only "
            "MCP transport. Remove MCP_TRANSPORT from .env and run "
            "'unset MCP_TRANSPORT' in any shell that exports it (or set it to "
            "streamable-http), and change MCP client URLs from "
            "http://localhost:<MCP_PORT>/sse to http://127.0.0.1:<MCP_PORT>/mcp."
        )
    raise ValueError("MCP_TRANSPORT must be 'streamable-http' or unset")


_check_transport(os.environ.get("MCP_TRANSPORT", ""))
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

# Path the Streamable HTTP transport is served on; fastmcp's default,
# pinned here so the docs cannot drift from it.
_STREAMABLE_HTTP_PATH = "/mcp"

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


class _HostOriginGuard:
    """ASGI middleware: reject a request whose Host (421) or Origin (403)
    is not in ``_TRANSPORT_SECURITY`` before it reaches any route.

    The check is the MCP SDK's own validator, so the allowlist means what
    it meant before the move to fastmcp. fastmcp's
    ``HostOriginGuardMiddleware`` is not used: it also accepts the
    server's own socket address as a Host and any loopback Origin on any
    scheme, which this allowlist does not. It runs ahead of the Streamable
    HTTP session manager, so a rejected request creates no session.

    It also logs every rejected request once at WARNING with a fixed
    reason (#878): ``bad_host`` or ``bad_origin`` for its own 421 or 403,
    and ``missing_token`` or ``invalid_token`` for the 401 the bearer
    check answers further in, told apart by whether an
    ``Authorization`` header was sent. Only the response status is
    read; the token and the header values are never logged, and the
    responses are unchanged.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._validator = TransportSecurityMiddleware(_TRANSPORT_SECURITY)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # ``is_post=False``: the transports check a POST's
        # Content-Type themselves.
        request = Request(scope)
        error = await self._validator.validate_request(request, is_post=False)
        if error is not None:
            # The validator answers 421 for the Host and 403 for the Origin.
            _log_rejection("bad_host" if error.status_code == 421 else "bad_origin")
            await error(scope, receive, send)
            return
        has_auth = "authorization" in request.headers

        async def send_and_log(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] == 401:
                _log_rejection("invalid_token" if has_auth else "missing_token")
            await send(message)

        await self.app(scope, receive, send_and_log)


def _log_rejection(reason: str) -> None:
    """One WARNING per rejected request; ``reason`` is a fixed literal."""
    log.warning("rejected request: reason=%s", reason)


_MISSING_AUTH_TOKEN = (
    "The MCP bearer token is missing or empty. Create it with "
    "'(umask 077; openssl rand -hex 32 > .secrets/mcp_auth_token.txt)' "
    "(the mcp_auth_token Docker secret; MCP_AUTH_TOKEN only when running "
    "outside a container), then configure MCP clients to send "
    "'Authorization: Bearer <token>'. See docs/setup.md."
)


# RFC 6750 ``b64token``: the characters a bearer token may carry in an
# ``Authorization`` header, and the set ``scripts/mcp-auth-headers.sh``
# sends (#589). The class holds ASCII ranges only, so ``fullmatch``
# rejects spaces, control characters and non-ASCII alike.
_AUTH_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/-]+=*")
# ``make init-secrets`` writes 64 hex characters (256 bits). 32 is the
# floor: 128 bits even for a hex token, and it admits
# ``openssl rand -base64 32`` (44 characters) while refusing a short
# hand-typed password.
_AUTH_TOKEN_MIN_LENGTH = 32
_UNUSABLE_AUTH_TOKEN = (
    "The MCP bearer token must be at least 32 characters from "
    "A-Z a-z 0-9 - . _ ~ + / with optional trailing '=' (RFC 6750), the "
    "set MCP clients can send. Regenerate it with "
    "'(umask 077; openssl rand -hex 32 > .secrets/mcp_auth_token.txt)'. "
    "See docs/setup.md."
)


def _require_auth_token(token: str) -> str:
    """Fail closed when the bearer token is empty, shorter than 32
    characters or outside the RFC 6750 character set. The messages never
    include the value."""
    if not token.strip():
        raise ValueError(_MISSING_AUTH_TOKEN)
    if len(token) < _AUTH_TOKEN_MIN_LENGTH or not _AUTH_TOKEN_PATTERN.fullmatch(token):
        raise ValueError(_UNUSABLE_AUTH_TOKEN)
    return token


class _StaticBearerTokenVerifier(TokenVerifier):
    """fastmcp token verifier accepting exactly one static bearer token.

    fastmcp's ``StaticTokenVerifier`` looks tokens up in a dict, which
    is not a constant-time comparison; this compares with
    ``hmac.compare_digest``. No base URL is set, so fastmcp adds no
    OAuth metadata routes.
    """

    def __init__(self, token: str) -> None:
        super().__init__()
        self._expected = _require_auth_token(token).encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token.encode(), self._expected):
            return None
        return AccessToken(token=token, client_id="local-operator", scopes=[])


def _build_app(server: FastMCP, *, session_idle_timeout: float, auth_token: str) -> ASGIApp:
    """The Streamable HTTP ASGI app serving ``server`` at ``/mcp``.

    The app carries ``_HostOriginGuard``; fastmcp's own Streamable HTTP
    Host/Origin check is off by default. Custom routes (``/health``) are
    served by the same app. Sessions end after ``session_idle_timeout``
    seconds without a request; it is required because fastmcp's default
    never ends them.

    ``/mcp`` requires ``Authorization: Bearer <auth_token>``. Setting
    ``server.auth`` makes ``http_app`` wrap the ``/mcp`` route in
    fastmcp's ``RequireAuthMiddleware``, which answers 401 before the
    request reaches the session manager, so a rejected request creates
    no session. Custom routes are not wrapped, so ``/health`` stays
    open for the healthcheck. ``_HostOriginGuard`` still rejects a bad
    Host or Origin first: the auth middleware fastmcp adds ahead of it
    only resolves the token, and the 401 comes from the route.
    """
    server.auth = _StaticBearerTokenVerifier(auth_token)
    return server.http_app(
        path=_STREAMABLE_HTTP_PATH,
        transport="streamable-http",
        middleware=[Middleware(_HostOriginGuard)],
        session_idle_timeout=session_idle_timeout,
    )


async def _health_response(db: Database) -> JSONResponse:
    """Body of the ``/health`` route. The probe opens SQLite, so it runs
    in a worker thread rather than on the shared event loop."""
    try:
        await asyncio.to_thread(db.ping)
    except Exception:
        log.exception("health probe failed")
        return JSONResponse({"status": "unhealthy"}, status_code=503)
    return JSONResponse({"status": "ok"})


def _run_server(server: FastMCP) -> None:
    """Serve ``server`` over Streamable HTTP with uvicorn.

    The app is built here and handed to uvicorn directly rather than
    through ``FastMCP.run``, which would print fastmcp's banner and check
    PyPI for a newer release. ``host="0.0.0.0"`` is required so the
    in-container bind is reachable through the Docker port-forward; the
    host-side mapping in ``docker-compose.yml`` keeps the port
    loopback-only (``127.0.0.1:${MCP_PORT}:${MCP_PORT}``).
    """
    config = uvicorn.Config(
        _build_app(
            server,
            session_idle_timeout=MCP_SESSION_IDLE_TIMEOUT_SECS,
            auth_token=MCP_AUTH_TOKEN,
        ),
        host="0.0.0.0",  # nosec B104 — see docstring
        port=MCP_PORT,
        log_level="info",
    )
    uvicorn.Server(config).run()


def main():
    # No MCP endpoint is served without its bearer token.
    _require_auth_token(MCP_AUTH_TOKEN)

    # Validate per-mode required vars BEFORE constructing service clients
    # or opening the SQLite database. A missing volume mount or bad DB
    # path is a much less common operator error than a missing env var,
    # so let env validation fire first — otherwise a bad SQLITE_PATH
    # would mask the real "you forgot INFERENCE_API_KEY" failure. A
    # chosen mode with missing config raises here so the operator sees a
    # precise error rather than a runtime fallback to a different
    # provider.
    # Every enabled layer must name its endpoint before any SDK client
    # is built: an empty ``*_BASE_URL`` fails here, ``default`` resolves
    # to the provider's official URL. Checked for all layers first
    # so no client exists, and no request is possible, when any of them
    # is ambiguous (#750). Disabled layers need no URL.
    embed_base_url = _resolve_base_url("EMBED_BASE_URL", EMBED_BASE_URL, EMBED_MODE)
    inference_base_url = ""
    if INFERENCE_MODE != "none":
        inference_base_url = _resolve_base_url(
            "INFERENCE_BASE_URL", INFERENCE_BASE_URL, INFERENCE_MODE
        )
    rerank_base_url = ""
    if RERANK_MODE != "none":
        rerank_base_url = _resolve_base_url("RERANK_BASE_URL", RERANK_BASE_URL, RERANK_MODE)
    # Every enabled layer also requires its API key (non-empty) at
    # startup; for an unauthenticated host-side server any placeholder
    # works. ``*_MODEL`` is always required because no SDK has a default
    # model — empty ``model`` always fails at request time.
    _require_env("EMBED_MODE", EMBED_MODE, "EMBED_MODEL", EMBED_MODEL)
    _require_env("EMBED_MODE", EMBED_MODE, "EMBED_API_KEY", EMBED_API_KEY)
    embed_client = EmbedClient(
        base_url=embed_base_url,
        model=EMBED_MODEL,
        api_key=EMBED_API_KEY,
        timeout_secs=EMBED_TIMEOUT_SECS,
    )
    # Re-check the endpoint the client resolved before it reaches the
    # startup log or an error.
    _reject_url_userinfo("EMBED_BASE_URL", embed_client.base_url)

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
            base_url=inference_base_url,
            model=INFERENCE_MODEL,
            api_key=INFERENCE_API_KEY,
            max_tokens=INFERENCE_MAX_TOKENS,
            timeout_secs=INFERENCE_TIMEOUT_SECS,
            structured_output=INFERENCE_STRUCTURED_OUTPUT,
        )
        _reject_url_userinfo("INFERENCE_BASE_URL", inference_client.base_url)

    reranker: CohereReranker | None = None
    if RERANK_MODE == "cohere":
        _require_env("RERANK_MODE", RERANK_MODE, "RERANK_MODEL", RERANK_MODEL)
        _require_env("RERANK_MODE", RERANK_MODE, "RERANK_API_KEY", RERANK_API_KEY)
        # Always set: ``default`` resolves to Cohere's official URL.
        rerank_endpoint = rerank_base_url
        _reject_url_userinfo("RERANK_BASE_URL", rerank_endpoint)
        reranker = CohereReranker(
            RerankConfig(
                base_url=rerank_base_url,
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
    # "no results"). The tool layer treats ``None`` as skip-validation,
    # but a fresh install the indexer has not built never gets that far:
    # the identity check below exits until the indexer has recorded its
    # embedder.
    expected_embed_dim = db.get_embedding_dim()

    # Refuse to serve query vectors from an embedder other than the one
    # that built the index (#645). A throwaway client: the check runs in
    # its own event loop, before the server's.
    run_startup_identity_check(
        db,
        lambda: EmbedClient(
            base_url=embed_base_url,
            model=EMBED_MODEL,
            api_key=EMBED_API_KEY,
            timeout_secs=EMBED_TIMEOUT_SECS,
        ),
        provider=EMBED_MODE,
        secrets=[k for k in (INFERENCE_API_KEY, EMBED_API_KEY, RERANK_API_KEY) if k],
        deadline_secs=EMBED_TIMEOUT_SECS,
    )

    # FastMCP server — provides the @server.tool() decorator and the
    # Streamable HTTP app ``_run_server`` serves, behind the
    # ``_TRANSPORT_SECURITY`` Host/Origin allowlist.
    server = FastMCP("protonmail-local-ai")

    # Plain HTTP health endpoint used by the container healthcheck. Sits
    # outside the MCP protocol so `docker healthcheck` and operator scripts
    # can probe liveness without speaking MCP. Returns 200 when the SQLite
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

    log.info(f"MCP server starting on port {MCP_PORT}")
    log.info(f"  SQLite:   {SQLITE_PATH}")
    log.info(f"  Embed mode:     {EMBED_MODE}")
    # Surface the resolved wire endpoint, not the raw env var.
    # ``EMBED_BASE_URL=default`` means OpenAI proper — printing the raw
    # value would hide that the request is going to api.openai.com.
    # ``EmbedClient.base_url`` reads the URL back from the SDK,
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
            f"  Rerank:         {rerank_endpoint} "
            f"(model={RERANK_MODEL}, candidates={RERANK_CANDIDATES})"
        )
    log.info(f"  Transport: streamable-http at {_STREAMABLE_HTTP_PATH} (bearer token required)")
    log.info(f"  Session idle timeout: {MCP_SESSION_IDLE_TIMEOUT_SECS:g}s")
    log.info("  Retrieval: local SQLite index only")
    # One loud line per enabled layer that sends mail-derived text off
    # the host, so it stands out from the INFO block above (#622).
    _warn_if_remote_endpoint("EMBED_MODE", EMBED_MODE, embed_client.base_url, "search query text")
    if inference_client is not None:
        _warn_if_remote_endpoint(
            "INFERENCE_MODE", INFERENCE_MODE, inference_client.base_url, "retrieved email excerpts"
        )
    if reranker is not None:
        _warn_if_remote_endpoint(
            "RERANK_MODE",
            RERANK_MODE,
            rerank_endpoint,
            "search queries and retrieved email excerpts",
        )

    _run_server(server)


if __name__ == "__main__":
    main()
