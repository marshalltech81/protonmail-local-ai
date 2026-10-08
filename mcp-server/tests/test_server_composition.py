"""
The server ``main()`` serves, pinned end to end (#1267).

``main()`` is driven with a fake index and no identity check, uvicorn is
stubbed, and the app it would serve is captured. The tests pin what a
client and the HTTP layer see: the registered tools with their full
wire listing and annotations for every registration condition, the
server's own middleware and identity, and the app's middleware, auth
verifier, session idle timeout and routes. They were written against
the inline composition before it moved into a factory and must pass
unchanged after.
"""

import asyncio
import logging

import pytest
from fastmcp import Client, FastMCP
from starlette.routing import Route

from tests.test_tool_annotations import EXPECTED_TITLES, _server, _wire_tools

_TOKEN = "synthetic-mcp-token-xxxxxxxxxxxxxxxx"  # pragma: allowlist secret
_KEY = "sk-test-marker"  # pragma: allowlist secret
_EXPERIMENTAL = {"brief_issue", "check_conclusion"}
_INTELLIGENCE = {"ask_mailbox", "summarize_thread", "extract_from_emails"}


class _FakeDatabase:
    def __init__(self, _path):
        pass

    def get_embedding_dim(self):
        return 4


def _skip_identity_check(*_args, **_kwargs):
    return None


def _serve(monkeypatch, *, inference_mode: str, experimental: bool, rerank_mode: str = "none"):
    """Run ``main()`` and return the server and the app uvicorn was given."""
    import src.main as main_mod

    for name, value in {
        "MCP_AUTH_TOKEN": _TOKEN,
        "EMBED_BASE_URL": "http://host.docker.internal:8001/v1",
        "EMBED_MODEL": "synthetic",
        "EMBED_API_KEY": _KEY,
        "INFERENCE_MODE": inference_mode,
        "INFERENCE_BASE_URL": "http://host.docker.internal:8002",
        "INFERENCE_MODEL": "synthetic",
        "INFERENCE_API_KEY": _KEY,
        "RERANK_MODE": rerank_mode,
        "RERANK_BASE_URL": "http://host.docker.internal:8003",
        "RERANK_MODEL": "synthetic",
        "RERANK_API_KEY": _KEY,
        "MCP_EXPERIMENTAL_TOOLS": experimental,
        "MCP_SESSION_IDLE_TIMEOUT_SECS": 900.0,
    }.items():
        monkeypatch.setattr(main_mod, name, value)
    monkeypatch.setattr(main_mod, "Database", _FakeDatabase)
    monkeypatch.setattr(main_mod, "run_startup_identity_check", _skip_identity_check)

    captured: dict = {}
    original_build_app = main_mod._build_app

    def recording_build_app(server, **kwargs):
        captured["server"] = server
        captured["build_app_kwargs"] = kwargs
        return original_build_app(server, **kwargs)

    class _StubUvicorn:
        class Config:
            def __init__(self, app, **kwargs):
                captured["app"] = app

        class Server:
            def __init__(self, config):
                pass

            def run(self):
                pass

    monkeypatch.setattr(main_mod, "_build_app", recording_build_app)
    monkeypatch.setattr(main_mod, "uvicorn", _StubUvicorn)
    main_mod.main()
    assert captured["build_app_kwargs"] == {"session_idle_timeout": 900.0, "auth_token": _TOKEN}
    return captured["server"], captured["app"]


def _tool_names(server: FastMCP) -> set[str]:
    async def run():
        async with Client(server) as client:
            return {t.name for t in await client.list_tools()}

    return asyncio.run(run())


@pytest.mark.parametrize(
    ("inference_mode", "experimental", "expected"),
    [
        ("anthropic", True, set(EXPECTED_TITLES)),
        ("openai", True, set(EXPECTED_TITLES)),
        ("anthropic", False, set(EXPECTED_TITLES) - _EXPERIMENTAL),
        ("none", True, set(EXPECTED_TITLES) - _EXPERIMENTAL - _INTELLIGENCE),
        ("none", False, set(EXPECTED_TITLES) - _EXPERIMENTAL - _INTELLIGENCE),
    ],
)
def test_registered_tools_follow_inference_and_the_experimental_flag(
    monkeypatch, inference_mode, experimental, expected
):
    server, _ = _serve(monkeypatch, inference_mode=inference_mode, experimental=experimental)
    assert _tool_names(server) == expected


def test_served_listing_matches_every_group_registered_directly(monkeypatch, empty_db):
    """Each tool's full wire entry (description, schemas, annotations) is
    the one the tool groups produce when registered directly."""
    server, _ = _serve(monkeypatch, inference_mode="anthropic", experimental=True)
    assert _wire_tools(server) == _wire_tools(_server(empty_db))


@pytest.mark.parametrize(
    ("inference_mode", "experimental", "lines", "absent"),
    [
        (
            "anthropic",
            True,
            [
                "Experimental tools registered (MCP_EXPERIMENTAL_TOOLS=true): "
                "brief_issue, check_conclusion."
            ],
            ["Intelligence tools not registered", "Experimental tools not registered"],
        ),
        (
            "none",
            True,
            [
                "Intelligence tools not registered (INFERENCE_MODE=none).",
                "Experimental tools not registered: they need inference (INFERENCE_MODE=none).",
            ],
            ["Experimental tools registered"],
        ),
        (
            "none",
            False,
            ["Intelligence tools not registered (INFERENCE_MODE=none)."],
            ["Experimental tools"],
        ),
    ],
)
def test_registration_decisions_are_logged(
    monkeypatch, caplog, inference_mode, experimental, lines, absent
):
    caplog.set_level(logging.INFO)
    _serve(monkeypatch, inference_mode=inference_mode, experimental=experimental)
    messages = [r.getMessage() for r in caplog.records if r.name == "mcp-server"]
    for line in lines:
        assert messages.count(line) == 1, line
    for fragment in absent:
        assert not any(fragment in m for m in messages), fragment


@pytest.mark.parametrize("rerank_mode", ["none", "cohere"])
def test_server_identity_and_middleware(monkeypatch, rerank_mode):
    import src.main as main_mod
    from src.lib.argument_validation import ArgumentValidationLog

    server, _ = _serve(
        monkeypatch, inference_mode="anthropic", experimental=False, rerank_mode=rerank_mode
    )
    assert server.name == "protonmail-local-ai"
    assert server.version == main_mod._git_commit()
    assert [type(m).__name__ for m in server.middleware] == [
        "DereferenceRefsMiddleware",
        ArgumentValidationLog.__name__,
    ]


def test_app_middleware_auth_and_routes(monkeypatch):
    from fastmcp.server.auth.middleware import RequireAuthMiddleware
    from src.main import _HostOriginGuard, _StaticBearerTokenVerifier

    server, app = _serve(monkeypatch, inference_mode="anthropic", experimental=True)

    assert type(server.auth) is _StaticBearerTokenVerifier
    assert [m.cls.__name__ for m in app.user_middleware] == [
        "RequestContextMiddleware",
        "AuthenticationMiddleware",
        "AuthContextMiddleware",
        _HostOriginGuard.__name__,
    ]
    assert app.user_middleware[-1].cls is _HostOriginGuard

    routes = {r.path: r for r in app.routes if isinstance(r, Route)}
    assert set(routes) == {"/mcp", "/health"}
    assert set(routes["/health"].methods or ()) >= {"GET"}
    assert isinstance(routes["/mcp"].endpoint, RequireAuthMiddleware)


_GROUPS = (
    "register_search_tools",
    "register_retrieval_tools",
    "register_intelligence_tools",
    "register_experimental_tools",
    "register_system_tools",
)


def test_each_group_gets_the_clients_and_settings_main_built(monkeypatch):
    """The reranker, the secrets to scrub, the index's embedding
    dimension and the prompt budget reach every group that takes them."""
    import src.main as main_mod

    calls: dict[str, tuple] = {}
    for group in _GROUPS:
        original = getattr(main_mod, group)

        def recording(*args, _group=group, _original=original, **kwargs):
            calls[_group] = (args, kwargs)
            return _original(*args, **kwargs)

        monkeypatch.setattr(main_mod, group, recording)

    server, _ = _serve(
        monkeypatch, inference_mode="anthropic", experimental=True, rerank_mode="cohere"
    )
    assert set(calls) == set(_GROUPS)
    search_args, search_kwargs = calls["register_search_tools"]
    assert search_args[0] is server
    db, embed = search_args[1], search_args[2]
    assert isinstance(db, _FakeDatabase)
    assert isinstance(embed, main_mod.EmbedClient)
    assert isinstance(search_kwargs["reranker"], main_mod.CohereReranker)
    assert search_kwargs["secret_values"] == [_KEY, _KEY, _KEY]
    assert search_kwargs["expected_embed_dim"] == 4
    assert calls["register_retrieval_tools"] == ((server, db), {})
    assert calls["register_system_tools"] == ((server, db), {})
    inference = calls["register_intelligence_tools"][0][3]
    assert isinstance(inference, main_mod.InferenceClient)
    for group in ("register_intelligence_tools", "register_experimental_tools"):
        args, kwargs = calls[group]
        assert args == (server, db, embed, inference)
        assert kwargs["reranker"] is search_kwargs["reranker"]
        assert kwargs["secret_values"] == [_KEY, _KEY, _KEY]
        assert kwargs["expected_embed_dim"] == 4
        assert isinstance(kwargs["prompt_budget"], main_mod.PromptBudget)
