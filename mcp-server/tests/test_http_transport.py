"""
The HTTP apps ``src.main._build_app`` serves, driven over ASGI.

These go through a real ``FastMCP`` instance and the same app the
container runs, so they pin what a client on the network sees: the
Host/Origin allowlist on every transport, the transport routes, the
``/health`` route, and that a tool failure stays out of the logs.
"""

import asyncio
import logging

import anyio
import httpx2
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from src.main import _build_app
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
_POST_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}
# The address uvicorn reports as the local end of the socket when a
# request arrives through the Docker port forward.
_CONTAINER_ADDR = ("172.18.0.5", 3000)
_MARKER = "synthetic-mail-marker-c41d"


def _app(transport, server: FastMCP | None = None, session_idle_timeout: float = 1800.0):
    return _build_app(server or _server(), transport, session_idle_timeout=session_idle_timeout)


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

    Raw ASGI rather than an HTTP client: an accepted ``GET /sse`` opens a
    stream that never ends, so the request is cancelled once the status
    line is out.
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


def _dual_with_lifespan(app, fn):
    """The dual app is a bare ASGI callable; drive its lifespan by hand."""

    async def run():
        sent = []
        startup = anyio.Event()
        shutdown = anyio.Event()
        queue = [{"type": "lifespan.startup"}]

        async def receive():
            if queue:
                return queue.pop()
            await shutdown.wait()
            return {"type": "lifespan.shutdown"}

        async def send(message):
            sent.append(message["type"])
            if message["type"] == "lifespan.startup.complete":
                startup.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(app, {"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
            await startup.wait()
            try:
                return await fn()
            finally:
                shutdown.set()

    return asyncio.run(run())


def _sse_status(app, **kw):
    return _with_lifespan(app, lambda: _status(app, "GET", "/sse", **kw))


def _mcp_status(app, **kw):
    import json

    return _with_lifespan(
        app,
        lambda: _status(
            app, "POST", "/mcp", body=json.dumps(_INIT).encode(), headers=_POST_HEADERS, **kw
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
    def test_sse_accepts_allowed_host(self, host):
        assert _sse_status(_app("sse"), host=host) == 200

    @pytest.mark.parametrize("host", _REJECTED_HOSTS)
    def test_sse_rejects_other_host(self, host):
        assert _sse_status(_app("sse"), host=host) == 421

    def test_sse_rejects_missing_host(self):
        assert _sse_status(_app("sse"), host=None) == 421

    @pytest.mark.parametrize("host", _ALLOWED_HOSTS)
    def test_streamable_http_accepts_allowed_host(self, host):
        assert _mcp_status(_app("streamable-http"), host=host) == 200

    @pytest.mark.parametrize("host", _REJECTED_HOSTS)
    def test_streamable_http_rejects_other_host(self, host):
        assert _mcp_status(_app("streamable-http"), host=host) == 421

    def test_sse_message_post_rejects_other_host(self):
        app = _app("sse")
        status = _with_lifespan(
            app,
            lambda: _status(
                app,
                "POST",
                "/messages/",
                host="evil.example",
                body=b"{}",
                headers=_POST_HEADERS,
            ),
        )
        assert status == 421

    def test_health_rejects_other_host(self):
        app = _app("sse")
        assert _with_lifespan(app, lambda: _status(app, "GET", "/health")) == 200
        assert (
            _with_lifespan(app, lambda: _status(app, "GET", "/health", host="evil.example")) == 421
        )


class TestOriginAllowlist:
    @pytest.mark.parametrize("origin", _ALLOWED_ORIGINS)
    def test_sse_accepts_allowed_origin(self, origin):
        assert _sse_status(_app("sse"), origin=origin) == 200

    @pytest.mark.parametrize("origin", _REJECTED_ORIGINS)
    def test_sse_rejects_other_origin(self, origin):
        assert _sse_status(_app("sse"), origin=origin) == 403

    @pytest.mark.parametrize("origin", _ALLOWED_ORIGINS)
    def test_streamable_http_accepts_allowed_origin(self, origin):
        assert _mcp_status(_app("streamable-http"), origin=origin) == 200

    @pytest.mark.parametrize("origin", _REJECTED_ORIGINS)
    def test_streamable_http_rejects_other_origin(self, origin):
        assert _mcp_status(_app("streamable-http"), origin=origin) == 403


class TestDualTransport:
    def test_both_transports_and_health_are_served(self):
        app = _app("dual")

        async def probe():
            import json

            return (
                await _status(app, "GET", "/sse"),
                await _status(
                    app, "POST", "/mcp", body=json.dumps(_INIT).encode(), headers=_POST_HEADERS
                ),
                await _status(app, "GET", "/health"),
            )

        assert _dual_with_lifespan(app, probe) == (200, 200, 200)

    def test_bad_host_is_rejected_on_both_transports(self):
        app = _app("dual")

        async def probe():
            import json

            return (
                await _status(app, "GET", "/sse", host="evil.example"),
                await _status(
                    app,
                    "POST",
                    "/mcp",
                    host="evil.example",
                    body=json.dumps(_INIT).encode(),
                    headers=_POST_HEADERS,
                ),
            )

        assert _dual_with_lifespan(app, probe) == (421, 421)


def _http_session_calls(app, calls: list[tuple[str, dict]]) -> list[str]:
    """Initialize a Streamable HTTP session on ``app`` and call each tool,
    returning the raw response bodies."""

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
                r = await c.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 10 + i,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": args},
                    },
                    headers=headers,
                )
                bodies.append(r.text)
            return bodies

    return _with_lifespan(app, run)


class TestToolFailures:
    def test_tool_error_is_an_error_result_over_http(self):
        ok, failed = _http_session_calls(
            _app("streamable-http"),
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
    """The Streamable HTTP session manager behind ``app``'s ``/mcp`` route."""
    route = next(r for r in app.routes if getattr(r, "path", "") == "/mcp")
    return route.endpoint.session_manager


class TestSessionBound:
    """#317: Streamable HTTP sessions a client abandons without a DELETE
    end after the idle timeout, and a request the Host allowlist rejects
    leaves no session behind."""

    def test_abandoned_sessions_expire_after_the_idle_timeout(self):
        app = _app("streamable-http", session_idle_timeout=0.5)

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
        app = _app("streamable-http")

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

    @pytest.mark.parametrize("transport", ["streamable-http", "dual"])
    def test_idle_timeout_reaches_every_streamable_http_app(self, transport):
        server = _server()
        calls = []
        http_app = server.http_app

        def recording_http_app(**kwargs):
            calls.append(kwargs)
            return http_app(**kwargs)

        server.http_app = recording_http_app  # type: ignore[method-assign]
        _app(transport, server=server, session_idle_timeout=42.0)
        streamable = [c for c in calls if c["transport"] == "streamable-http"]
        assert len(streamable) == 1
        assert streamable[0]["session_idle_timeout"] == 42.0
