"""
The HTTP apps ``src.main._build_app`` serves, driven over ASGI.

These go through a real ``FastMCP`` instance and the same app the
container runs, so they pin what a client on the network sees: the
Host/Origin allowlist on every route, the static bearer token on the
Streamable HTTP route at ``/mcp`` (the only transport, #498), the
unauthenticated ``/health`` route, and that a tool failure stays out of
the logs.
"""

import asyncio
import hmac
import json
import logging

import anyio
import httpx2
import pytest
import src.main as main_mod
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from src.main import _build_app
from src.tools.retrieval import register_retrieval_tools
from src.tools.system import register_system_tools
from starlette.requests import Request
from starlette.responses import JSONResponse

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
# A synthetic bearer token; every authenticated request in this file
# sends it.
_TOKEN = "synthetic-bearer-token-5e0b"
_UNAUTHENTICATED_POST_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}
_POST_HEADERS = dict(_UNAUTHENTICATED_POST_HEADERS, authorization=f"Bearer {_TOKEN}")
# The address uvicorn reports as the local end of the socket when a
# request arrives through the Docker port forward.
_CONTAINER_ADDR = ("172.18.0.5", 3000)
_MARKER = "synthetic-mail-marker-c41d"


def _app(server: FastMCP | None = None, session_idle_timeout: float = 1800.0):
    return _build_app(
        server or _server(), session_idle_timeout=session_idle_timeout, auth_token=_TOKEN
    )


def _server() -> FastMCP:
    server = FastMCP("http-transport-test")

    @server.tool()
    async def ping() -> str:
        return "pong"

    @server.tool()
    async def fails_cleanly(q: str) -> str:
        raise ToolError(f"tool error {_MARKER}")

    @server.tool()
    async def fails_unexpectedly(q: str) -> str:
        raise RuntimeError(f"unexpected {_MARKER}")

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return server


async def _status(
    app,
    method: str,
    path: str,
    host: str | None = "localhost:3000",
    origin: str | None = None,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> int | None:
    """Send one request to ``app`` and return the response status.

    Raw ASGI rather than an HTTP client, so a request can omit the Host
    header; the request is cancelled once the status line is out.
    """
    raw = [] if host is None else [(b"host", host.encode())]
    if origin is not None:
        raw.append((b"origin", origin.encode()))
    raw.extend((k.encode(), v.encode()) for k, v in (headers or {}).items())
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": raw,
        "client": ("127.0.0.1", 50000),
        "server": _CONTAINER_ADDR,
    }
    status: int | None = None
    started = anyio.Event()
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await anyio.sleep_forever()

    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            started.set()

    async with anyio.create_task_group() as tg:

        async def run():
            await app(scope, receive, send)
            started.set()

        tg.start_soon(run)
        with anyio.move_on_after(5):
            await started.wait()
        tg.cancel_scope.cancel()
    return status


def _with_lifespan(app, fn):
    async def run():
        async with app.router.lifespan_context(app):
            return await fn()

    return asyncio.run(run())


def _mcp_status(app, headers: dict[str, str] | None = None, **kw):
    return _with_lifespan(
        app,
        lambda: _status(
            app,
            "POST",
            "/mcp",
            body=json.dumps(_INIT).encode(),
            headers=_POST_HEADERS if headers is None else headers,
            **kw,
        ),
    )


# Host values the allowlist accepts: the loopback names and the compose
# service name, with or without a port.
_ALLOWED_HOSTS = [
    "localhost",
    "localhost:3000",
    "127.0.0.1",
    "127.0.0.1:3000",
    "[::1]",
    "[::1]:3000",
    "mcp-server",
    "mcp-server:3000",
]
# Host values it rejects. The container's own address is reported by
# uvicorn as the socket's local end; it is not in the allowlist and must
# not be added to it implicitly.
_REJECTED_HOSTS = [
    "evil.example",
    "evil.example:3000",
    "localhost.evil.example",
    "mcp-server.evil.example:3000",
    "172.18.0.5:3000",
    "",
]
_ALLOWED_ORIGINS = [
    "http://localhost",
    "http://localhost:3000",
    "http://127.0.0.1:8080",
    "http://[::1]:3000",
    "http://mcp-server:3000",
]
# A loopback origin on another scheme, and a host the ``[::1]`` pattern
# would match if it were read as a glob character class.
_REJECTED_ORIGINS = [
    "http://evil.example",
    "http://evil.example:3000",
    "https://localhost:3000",
    "https://127.0.0.1",
    "http://1:3000",
    "null",
]


class TestHostAllowlist:
    @pytest.mark.parametrize("host", _ALLOWED_HOSTS)
    def test_streamable_http_accepts_allowed_host(self, host):
        assert _mcp_status(_app(), host=host) == 200

    @pytest.mark.parametrize("host", _REJECTED_HOSTS)
    def test_streamable_http_rejects_other_host(self, host):
        assert _mcp_status(_app(), host=host) == 421

    def test_streamable_http_rejects_missing_host(self):
        assert _mcp_status(_app(), host=None) == 421

    def test_health_rejects_other_host(self):
        app = _app()
        assert _with_lifespan(app, lambda: _status(app, "GET", "/health")) == 200
        assert (
            _with_lifespan(app, lambda: _status(app, "GET", "/health", host="evil.example")) == 421
        )
        assert _with_lifespan(app, lambda: _status(app, "GET", "/health", host=None)) == 421


class TestLegacySseEndpointsAreGone:
    """#498: the legacy HTTP+SSE transport and its ``/sse`` and
    ``/messages/`` endpoints are removed; ``/mcp`` is the only MCP
    endpoint. The Host check still runs first on any path."""

    @pytest.mark.parametrize(
        ("method", "path"), [("GET", "/sse"), ("POST", "/messages/"), ("POST", "/sse")]
    )
    def test_legacy_endpoint_is_not_found(self, method, path):
        app = _app()
        status = _with_lifespan(
            app, lambda: _status(app, method, path, body=b"{}", headers=_POST_HEADERS)
        )
        assert status == 404

    @pytest.mark.parametrize(("method", "path"), [("GET", "/sse"), ("POST", "/messages/")])
    def test_legacy_endpoint_with_other_host_is_rejected_first(self, method, path):
        app = _app()
        status = _with_lifespan(
            app,
            lambda: _status(
                app, method, path, host="evil.example", body=b"{}", headers=_POST_HEADERS
            ),
        )
        assert status == 421


class TestOriginAllowlist:
    @pytest.mark.parametrize("origin", _ALLOWED_ORIGINS)
    def test_streamable_http_accepts_allowed_origin(self, origin):
        assert _mcp_status(_app(), origin=origin) == 200

    @pytest.mark.parametrize("origin", _REJECTED_ORIGINS)
    def test_streamable_http_rejects_other_origin(self, origin):
        assert _mcp_status(_app(), origin=origin) == 403


def _http_session_calls(app, calls: list[tuple[str, dict]]) -> list[str]:
    """Initialize a Streamable HTTP session on ``app`` and send each
    ``(method, params)`` request, returning the raw response bodies. A
    method without a ``/`` is a tool name, sent as ``tools/call``."""

    async def run():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://localhost") as c:
            r = await c.post("/mcp", json=_INIT, headers=_POST_HEADERS)
            assert r.status_code == 200
            headers = dict(
                _POST_HEADERS,
                **{
                    "mcp-protocol-version": "2025-06-18",
                    "mcp-session-id": r.headers["mcp-session-id"],
                },
            )
            await c.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers,
            )
            bodies = []
            for i, (name, args) in enumerate(calls):
                if "/" in name:
                    method, params = name, args
                else:
                    method, params = "tools/call", {"name": name, "arguments": args}
                r = await c.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 10 + i, "method": method, "params": params},
                    headers=headers,
                )
                bodies.append(r.text)
            return bodies

    return _with_lifespan(app, run)


class TestReadOnlyClientOverStreamableHttp:
    """#498: an MCP client initializes, lists the tools and makes a
    read-only call over ``/mcp`` against the real tool registrations and
    a synthetic index."""

    def test_initialize_list_tools_and_read_a_thread(self, seeded_db):
        server = FastMCP("protonmail-local-ai")
        register_retrieval_tools(server, seeded_db)
        register_system_tools(server, seeded_db)
        listed, thread = _http_session_calls(
            _app(server=server),
            [("tools/list", {}), ("get_thread", {"thread_id": "t-alpha"})],
        )
        names = {t["name"] for t in _sse_json(listed)["result"]["tools"]}
        assert {"get_thread", "list_threads", "get_mailbox_status"} <= names
        result = _sse_json(thread)["result"]
        assert result["isError"] is False
        assert "invoice for march" in json.dumps(result)


def _sse_json(body: str) -> dict:
    """The JSON-RPC message in a Streamable HTTP response, which arrives
    as a single ``data:`` line of an SSE stream or as a plain JSON body."""
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:") :])
    return json.loads(body)


class TestToolFailures:
    def test_tool_error_is_an_error_result_over_http(self):
        ok, failed = _http_session_calls(
            _app(),
            [("ping", {}), ("fails_cleanly", {"q": "x"})],
        )
        assert '"isError":false' in ok
        assert '"isError":true' in failed

    @pytest.mark.parametrize("tool", ["fails_cleanly", "fails_unexpectedly"])
    def test_tool_failure_text_and_arguments_stay_out_of_the_log(self, caplog, tool):
        """fastmcp logs every tool failure at ERROR, and an unexpected
        exception with its traceback, whose message can quote mail. The
        record may name the tool but must carry neither the exception
        text nor the arguments."""

        async def call():
            async with Client(_server()) as client:
                return await client.call_tool_mcp(tool, {"q": f"argument {_MARKER}"})

        with caplog.at_level(logging.INFO):
            result = asyncio.run(call())
        assert result.is_error
        records = [r for r in caplog.records if r.name.startswith("fastmcp")]
        assert any(tool in r.getMessage() for r in records)
        assert _MARKER not in caplog.text
        assert not any(r.exc_info for r in records)


def _session_manager(app):
    """The Streamable HTTP session manager behind ``app``'s ``/mcp`` route,
    which fastmcp wraps in its ``RequireAuthMiddleware``."""
    route = next(r for r in app.routes if getattr(r, "path", "") == "/mcp")
    return route.endpoint.app.session_manager


class TestSessionBound:
    """#317: Streamable HTTP sessions a client abandons without a DELETE
    end after the idle timeout, and a request the Host allowlist rejects
    leaves no session behind."""

    def test_abandoned_sessions_expire_after_the_idle_timeout(self):
        app = _app(session_idle_timeout=0.5)

        async def run():
            manager = _session_manager(app)
            assert manager.session_idle_timeout == 0.5
            for _ in range(5):
                transport = httpx2.ASGITransport(app=app)
                async with httpx2.AsyncClient(
                    transport=transport, base_url="http://localhost"
                ) as c:
                    r = await c.post("/mcp", json=_INIT, headers=_POST_HEADERS)
                    assert r.status_code == 200
            retained = len(manager._server_instances)
            await anyio.sleep(1.5)
            return retained, len(manager._server_instances)

        assert _with_lifespan(app, run) == (5, 0)

    def test_rejected_host_leaves_no_session(self):
        app = _app()

        async def run():
            statuses = []
            for _ in range(3):
                transport = httpx2.ASGITransport(app=app)
                async with httpx2.AsyncClient(
                    transport=transport, base_url="http://evil.example"
                ) as c:
                    r = await c.post("/mcp", json=_INIT, headers=_POST_HEADERS)
                    statuses.append(r.status_code)
            return statuses, len(_session_manager(app)._server_instances)

        assert _with_lifespan(app, run) == ([421, 421, 421], 0)

    @pytest.mark.parametrize(
        "headers",
        [_UNAUTHENTICATED_POST_HEADERS, dict(_POST_HEADERS, authorization="Bearer wrong-token")],
        ids=["no-token", "wrong-token"],
    )
    def test_rejected_token_leaves_no_session(self, headers):
        app = _app()

        async def run():
            statuses = []
            for _ in range(3):
                transport = httpx2.ASGITransport(app=app)
                async with httpx2.AsyncClient(
                    transport=transport, base_url="http://localhost"
                ) as c:
                    r = await c.post("/mcp", json=_INIT, headers=headers)
                    statuses.append(r.status_code)
            return statuses, len(_session_manager(app)._server_instances)

        assert _with_lifespan(app, run) == ([401, 401, 401], 0)

    def test_idle_timeout_reaches_the_streamable_http_app(self):
        server = _server()
        calls = []
        http_app = server.http_app

        def recording_http_app(**kwargs):
            calls.append(kwargs)
            return http_app(**kwargs)

        server.http_app = recording_http_app  # type: ignore[method-assign]
        _app(server=server, session_idle_timeout=42.0)
        assert len(calls) == 1
        assert calls[0]["transport"] == "streamable-http"
        assert calls[0]["path"] == "/mcp"
        assert calls[0]["session_idle_timeout"] == 42.0


class TestBearerAuth:
    """PLAN.md Resolved decisions 13: ``/mcp`` requires the static bearer
    token, checked in constant time before a session is created; the
    Host/Origin allowlist still runs and ``/health`` stays open."""

    def test_missing_token_is_unauthorized(self):
        assert _mcp_status(_app(), headers=_UNAUTHENTICATED_POST_HEADERS) == 401

    @pytest.mark.parametrize(
        "value",
        [
            "Bearer wrong-token",
            f"Bearer {_TOKEN}x",
            f"Bearer {_TOKEN[:-1]}",
            f"Bearer  {_TOKEN}",
            f"Basic {_TOKEN}",
            _TOKEN,
            "Bearer ",
            "Bearer t\u00f6ken",
        ],
    )
    def test_wrong_token_is_unauthorized(self, value):
        headers = dict(_POST_HEADERS, authorization=value)
        assert _mcp_status(_app(), headers=headers) == 401

    def test_right_token_is_accepted(self):
        assert _mcp_status(_app()) == 200

    @pytest.mark.parametrize("method", ["GET", "DELETE"])
    def test_other_methods_need_the_token(self, method):
        app = _app()
        status = _with_lifespan(
            app,
            lambda: _status(app, method, "/mcp", headers={"accept": "text/event-stream"}),
        )
        assert status == 401

    def test_session_id_without_the_token_is_unauthorized(self):
        """A session id alone does not authenticate a later request."""
        app = _app()

        async def run():
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url="http://localhost") as c:
                r = await c.post("/mcp", json=_INIT, headers=_POST_HEADERS)
                assert r.status_code == 200
                headers = dict(
                    _UNAUTHENTICATED_POST_HEADERS,
                    **{
                        "mcp-protocol-version": "2025-06-18",
                        "mcp-session-id": r.headers["mcp-session-id"],
                    },
                )
                r = await c.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    headers=headers,
                )
                return r.status_code

        assert _with_lifespan(app, run) == 401

    def test_health_needs_no_token(self):
        app = _app()
        assert _with_lifespan(app, lambda: _status(app, "GET", "/health")) == 200
        wrong = {"authorization": "Bearer wrong-token"}
        assert _with_lifespan(app, lambda: _status(app, "GET", "/health", headers=wrong)) == 200

    def test_host_and_origin_checks_still_run_with_a_valid_token(self):
        assert _mcp_status(_app(), host="evil.example") == 421
        assert _mcp_status(_app(), origin="http://evil.example") == 403

    def test_compare_is_constant_time(self, monkeypatch):
        calls = []
        compare_digest = hmac.compare_digest

        def recording_compare_digest(a, b):
            calls.append((a, b))
            return compare_digest(a, b)

        monkeypatch.setattr(main_mod.hmac, "compare_digest", recording_compare_digest)
        wrong = dict(_POST_HEADERS, authorization="Bearer wrong-token")
        assert _mcp_status(_app(), headers=wrong) == 401
        assert _mcp_status(_app()) == 200
        assert (b"wrong-token", _TOKEN.encode()) in calls
        assert (_TOKEN.encode(), _TOKEN.encode()) in calls

    @pytest.mark.parametrize("token", ["", "   "])
    def test_empty_token_fails_closed(self, token):
        with pytest.raises(ValueError, match="bearer token"):
            _build_app(_server(), session_idle_timeout=1800.0, auth_token=token)

    def test_tokens_stay_out_of_the_log(self, caplog):
        """Neither the configured token nor a presented wrong one is
        logged, at any level."""
        marker = "synthetic-wrong-token-marker-91c2"
        with caplog.at_level(logging.DEBUG):
            wrong = dict(_POST_HEADERS, authorization=f"Bearer {marker}")
            assert _mcp_status(_app(), headers=wrong) == 401
            assert _mcp_status(_app(), headers=_UNAUTHENTICATED_POST_HEADERS) == 401
            _http_session_calls(_app(), [("ping", {})])
        assert marker not in caplog.text
        assert _TOKEN not in caplog.text
