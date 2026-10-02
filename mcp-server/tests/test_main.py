"""
Tests for src/main.py.

The MCP service entrypoint mostly assembles config and registers tool
groups, which is hard to exercise in unit tests without spinning up the
Streamable HTTP transport. The two pieces that DO have unit-testable behavior live
here:

- ``_read_secret`` — prefers Docker secret files over env vars; a
  silent fallthrough to env would mean an attacker with ``docker
  inspect`` access could read the Anthropic key.
- The ``/health`` custom route — used by the docker healthcheck to
  decide if the container is up. A regression here would make every
  failed DB connect look "healthy" or vice versa.
"""

import asyncio
import logging
import threading

import pytest
from src.main import (
    _INFERENCE_MODES,
    _float_env,
    _normalize_mode,
    _read_secret,
    _reject_url_userinfo,
    _require_env,
    _run_server,
    _warn_if_remote_endpoint,
)


class TestReadSecret:
    def test_secret_file_is_preferred_over_env_fallback(self, monkeypatch, tmp_path):
        # Point the secret reader at a tmp directory by patching the path
        # construction. The real reader hardcodes ``/run/secrets/<name>``;
        # we simulate by writing into a controlled location and patching
        # ``Path`` lookup via monkeypatch on the module.
        secret_dir = tmp_path / "secrets"
        secret_dir.mkdir()
        secret_file = secret_dir / "fake_secret"
        secret_file.write_text("from-secret-file\n")
        monkeypatch.setenv("FAKE_SECRET_ENV", "from-env-fallback")

        # Patch the Path construction in the module under test so the test
        # does not depend on writing to /run/secrets.
        import src.main as main_mod

        original_path = main_mod.Path

        def patched_path(arg):
            if arg == "/run/secrets/fake_secret":
                return secret_file
            return original_path(arg)

        monkeypatch.setattr(main_mod, "Path", patched_path)

        # Trailing whitespace on the secret file (common when an operator
        # uses ``echo`` to write the secret) must be stripped.
        assert _read_secret("fake_secret", "FAKE_SECRET_ENV") == "from-secret-file"

    def test_falls_back_to_env_when_secret_file_missing(self, monkeypatch):
        monkeypatch.setenv("FAKE_SECRET_ENV", "from-env-fallback")
        # The default ``/run/secrets/missing_secret`` will not exist on
        # the test machine, so the env fallback must fire.
        assert _read_secret("missing_secret", "FAKE_SECRET_ENV") == "from-env-fallback"

    def test_env_fallback_value_is_whitespace_stripped(self, monkeypatch):
        # Operators pasting a key into ``INFERENCE_API_KEY=" key "`` (or
        # any shell-quoted form that picks up stray spaces) would
        # otherwise send whitespace as part of the bearer credential and
        # fail in a non-obvious way at first call. The secret-file path
        # already strips; the env fallback must match so both paths are
        # interchangeable. Indexer's equivalent already strips both
        # paths — keep mcp-server symmetric.
        monkeypatch.setenv("FAKE_SECRET_ENV", "  env-key  ")
        assert _read_secret("missing_secret", "FAKE_SECRET_ENV") == "env-key"

    def test_returns_empty_when_neither_source_present(self, monkeypatch):
        monkeypatch.delenv("DEFINITELY_UNSET", raising=False)
        assert _read_secret("missing_secret", "DEFINITELY_UNSET") == ""


class TestMcpTransport:
    """#498: Streamable HTTP at ``/mcp`` is the only transport.
    ``MCP_TRANSPORT`` unset, empty or ``streamable-http`` starts; the
    removed ``sse`` and ``dual`` values fail startup with migration
    steps; anything else fails closed."""

    def _load(self, monkeypatch, value):
        import importlib

        import src.main as main_mod

        if value is None:
            monkeypatch.delenv("MCP_TRANSPORT", raising=False)
        else:
            monkeypatch.setenv("MCP_TRANSPORT", value)
        try:
            return importlib.reload(main_mod)
        finally:
            monkeypatch.delenv("MCP_TRANSPORT", raising=False)
            importlib.reload(main_mod)

    @pytest.mark.parametrize("value", [None, "", "streamable-http", " Streamable-HTTP "])
    def test_streamable_http_or_unset_starts(self, monkeypatch, value):
        self._load(monkeypatch, value)

    @pytest.mark.parametrize("value", ["sse", "dual", " SSE ", "Dual"])
    def test_removed_transport_fails_with_migration_steps(self, monkeypatch, value):
        with pytest.raises(ValueError) as exc:
            self._load(monkeypatch, value)
        message = str(exc.value)
        assert f"MCP_TRANSPORT={value.strip().lower()}" in message
        assert "removed" in message
        assert "/sse" in message and "/mcp" in message
        # Compose and validate-env read an exported value ahead of .env,
        # so the steps must cover the shell environment too.
        assert "unset MCP_TRANSPORT" in message

    @pytest.mark.parametrize("value", ["websocket", "stdio", "http"])
    def test_unknown_transport_fails_closed(self, monkeypatch, value):
        with pytest.raises(ValueError, match="MCP_TRANSPORT must be 'streamable-http'"):
            self._load(monkeypatch, value)

    def test_run_server_serves_the_built_app_with_uvicorn(self, monkeypatch):
        """``FastMCP.run`` is bypassed (it prints a banner and checks PyPI
        for updates); uvicorn serves ``_build_app``'s app on 0.0.0.0 at
        ``MCP_PORT``."""
        import src.main as main_mod

        captured: dict = {}

        class _StubUvicorn:
            class Config:
                def __init__(self, app, **kwargs):
                    captured["app"] = app
                    captured.update(kwargs)

            class Server:
                def __init__(self, config):
                    pass

                def run(self):
                    captured["ran"] = True

        class _Server:
            def run(self, *_args, **_kwargs):  # pragma: no cover — must not be called
                raise AssertionError("FastMCP.run must not be used")

        app = object()
        monkeypatch.setattr(main_mod, "uvicorn", _StubUvicorn)
        monkeypatch.setattr(
            main_mod,
            "_build_app",
            lambda server, session_idle_timeout, auth_token: (
                app
                if (session_idle_timeout, auth_token) == (900.0, "synthetic-mcp-token")
                else None
            ),
        )
        monkeypatch.setattr(main_mod, "MCP_PORT", 3000)
        monkeypatch.setattr(main_mod, "MCP_SESSION_IDLE_TIMEOUT_SECS", 900.0)
        monkeypatch.setattr(main_mod, "MCP_AUTH_TOKEN", "synthetic-mcp-token")
        _run_server(_Server())  # type: ignore[arg-type]
        assert captured == {
            "app": app,
            "host": "0.0.0.0",  # nosec B104
            "port": 3000,
            "log_level": "info",
            "ran": True,
        }


class TestMcpAuthToken:
    """PLAN.md Resolved decisions 13: the MCP bearer token is the
    ``mcp_auth_token`` Docker secret (``MCP_AUTH_TOKEN`` only outside
    a container), and startup fails closed without it."""

    class _FakeDatabase:
        def __init__(self, _path):  # pragma: no cover — must not be opened
            raise AssertionError("the index must not be opened without a token")

    def test_token_is_read_from_the_secret_with_env_fallback(self, monkeypatch):
        import importlib

        import src.main as main_mod

        monkeypatch.setenv("MCP_AUTH_TOKEN", "  synthetic-env-token  ")
        try:
            assert importlib.reload(main_mod).MCP_AUTH_TOKEN == "synthetic-env-token"
        finally:
            monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
            importlib.reload(main_mod)

    @pytest.mark.parametrize("token", ["", "   "])
    def test_missing_or_empty_token_fails_startup(self, monkeypatch, token):
        import src.main as main_mod

        monkeypatch.setattr(main_mod, "MCP_AUTH_TOKEN", token)
        monkeypatch.setattr(main_mod, "Database", self._FakeDatabase)
        monkeypatch.setattr(main_mod, "_run_server", lambda *_: pytest.fail("server started"))
        with pytest.raises(ValueError) as excinfo:
            main_mod.main()
        message = str(excinfo.value)
        assert ".secrets/mcp_auth_token.txt" in message
        assert "openssl rand -hex 32" in message


class TestSessionIdleTimeout:
    """#317: ``MCP_SESSION_IDLE_TIMEOUT_SECS`` bounds Streamable HTTP
    sessions; it defaults to 1800 s and a value that is not a finite
    number of at least one second fails startup."""

    def _load(self, monkeypatch, value):
        import importlib

        import src.main as main_mod

        if value is None:
            monkeypatch.delenv("MCP_SESSION_IDLE_TIMEOUT_SECS", raising=False)
        else:
            monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT_SECS", value)
        try:
            return importlib.reload(main_mod).MCP_SESSION_IDLE_TIMEOUT_SECS
        finally:
            monkeypatch.delenv("MCP_SESSION_IDLE_TIMEOUT_SECS", raising=False)
            importlib.reload(main_mod)

    def test_defaults_to_thirty_minutes(self, monkeypatch):
        assert self._load(monkeypatch, None) == 1800.0

    def test_operator_value_is_used(self, monkeypatch):
        assert self._load(monkeypatch, "600") == 600.0

    @pytest.mark.parametrize("value", ["0", "0.5", "-1", "nan", "inf", "soon"])
    def test_invalid_value_fails_startup(self, monkeypatch, value):
        with pytest.raises(ValueError, match="MCP_SESSION_IDLE_TIMEOUT_SECS"):
            self._load(monkeypatch, value)


class TestContextTokens:
    """#285: ``INFERENCE_CONTEXT_TOKENS`` is the model window prompts are
    fitted into; it defaults to 32,768, and a window that leaves too
    little room after ``INFERENCE_MAX_TOKENS`` fails startup."""

    class _FakeDatabase:
        def __init__(self, _path):
            pass

        def get_embedding_dim(self):
            return 4

    def _load(self, monkeypatch, value):
        import importlib

        import src.main as main_mod

        if value is None:
            monkeypatch.delenv("INFERENCE_CONTEXT_TOKENS", raising=False)
        else:
            monkeypatch.setenv("INFERENCE_CONTEXT_TOKENS", value)
        try:
            return importlib.reload(main_mod).INFERENCE_CONTEXT_TOKENS
        finally:
            monkeypatch.delenv("INFERENCE_CONTEXT_TOKENS", raising=False)
            importlib.reload(main_mod)

    def test_defaults_to_32k(self, monkeypatch):
        assert self._load(monkeypatch, None) == 32768

    def test_operator_value_is_used(self, monkeypatch):
        assert self._load(monkeypatch, "8192") == 8192

    @pytest.mark.parametrize("value", ["0", "-1", "big"])
    def test_invalid_value_fails_startup(self, monkeypatch, value):
        with pytest.raises(ValueError, match="INFERENCE_CONTEXT_TOKENS"):
            self._load(monkeypatch, value)

    def _run_main(self, monkeypatch, *, context, max_tokens, mode="anthropic"):
        import src.main as main_mod

        for name, value in {
            "EMBED_BASE_URL": "http://host.docker.internal:8001/v1",
            "EMBED_MODEL": "synthetic",
            "MCP_AUTH_TOKEN": _PLACEHOLDER_TOKEN,
            "EMBED_API_KEY": _PLACEHOLDER_KEY,
            "INFERENCE_MODE": mode,
            "INFERENCE_BASE_URL": "http://host.docker.internal:8002",
            "INFERENCE_MODEL": "synthetic",
            "INFERENCE_API_KEY": _PLACEHOLDER_KEY,
            "INFERENCE_CONTEXT_TOKENS": context,
            "INFERENCE_MAX_TOKENS": max_tokens,
            "RERANK_MODE": "none",
        }.items():
            monkeypatch.setattr(main_mod, name, value)
        monkeypatch.setattr(main_mod, "Database", self._FakeDatabase)
        ran = []
        monkeypatch.setattr(main_mod, "_run_server", lambda *args: ran.append(args))
        main_mod.main()
        return ran

    def test_window_without_room_for_a_prompt_fails_startup(self, monkeypatch):
        with pytest.raises(ValueError, match="INFERENCE_CONTEXT_TOKENS"):
            self._run_main(monkeypatch, context=4096, max_tokens=4000)

    def test_small_window_starts(self, monkeypatch):
        assert self._run_main(monkeypatch, context=4096, max_tokens=1024)

    def test_window_is_not_checked_without_inference(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        assert self._run_main(monkeypatch, context=1, max_tokens=1024, mode="none")


class TestFloatEnv:
    """``_float_env`` rejects non-finite values.

    ``float("nan")`` and ``float("inf")`` parse without raising, and
    ``nan < minimum`` is always False — so without an explicit
    ``math.isfinite`` check, a typo'd timeout like ``EMBED_TIMEOUT_SECS=inf``
    or ``=nan`` reaches the SDK client and breaks its HTTP timeouts in
    surprising ways. Reject non-finite values at parse time.
    """

    def test_returns_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("FAKE_FLOAT_VAR", raising=False)
        assert _float_env("FAKE_FLOAT_VAR", default=12.5) == 12.5

    def test_valid_finite_value_parses(self, monkeypatch):
        monkeypatch.setenv("FAKE_FLOAT_VAR", "30.0")
        assert _float_env("FAKE_FLOAT_VAR", default=12.5, minimum=1.0) == 30.0

    def test_nan_is_rejected(self, monkeypatch):
        monkeypatch.setenv("FAKE_FLOAT_VAR", "nan")
        with pytest.raises(ValueError, match="FAKE_FLOAT_VAR"):
            _float_env("FAKE_FLOAT_VAR", default=12.5, minimum=1.0)

    def test_positive_infinity_is_rejected(self, monkeypatch):
        monkeypatch.setenv("FAKE_FLOAT_VAR", "inf")
        with pytest.raises(ValueError, match="FAKE_FLOAT_VAR"):
            _float_env("FAKE_FLOAT_VAR", default=12.5, minimum=1.0)

    def test_negative_infinity_is_rejected(self, monkeypatch):
        monkeypatch.setenv("FAKE_FLOAT_VAR", "-inf")
        with pytest.raises(ValueError, match="FAKE_FLOAT_VAR"):
            _float_env("FAKE_FLOAT_VAR", default=12.5, minimum=1.0)


class TestNormalizeMode:
    def test_supported_modes_are_normalized(self):
        assert _normalize_mode("INFERENCE_MODE", "openai", _INFERENCE_MODES) == "openai"
        assert _normalize_mode("INFERENCE_MODE", " ANTHROPIC ", _INFERENCE_MODES) == "anthropic"
        assert _normalize_mode("INFERENCE_MODE", "none", _INFERENCE_MODES) == "none"

    def test_unknown_mode_fails_closed(self):
        with pytest.raises(ValueError, match="INFERENCE_MODE"):
            _normalize_mode("INFERENCE_MODE", "local", _INFERENCE_MODES)


class TestRejectUrlUserinfo:
    """``_reject_url_userinfo`` blocks base URLs that embed a
    ``user:pass@host`` userinfo authority at config load. Resolved base
    URLs flow into the startup log naming the wire endpoint, so
    embedded credentials would leak to container logs / journald.
    Mirrors the same guard in ``scripts/validate-env.sh``.
    """

    def test_empty_value_passes_through(self):
        # Empty ``*_BASE_URL`` is contract-supported (use SDK default).
        assert _reject_url_userinfo("INFERENCE_BASE_URL", "") == ""

    def test_clean_url_passes_through(self):
        assert (
            _reject_url_userinfo("EMBED_BASE_URL", "https://api.example.com/v1")
            == "https://api.example.com/v1"
        )

    def test_host_with_port_no_userinfo_passes(self):
        # ``host.docker.internal:8001`` has a colon in netloc but no '@'.
        url = "http://host.docker.internal:8001/v1"
        assert _reject_url_userinfo("EMBED_BASE_URL", url) == url

    def test_userinfo_in_url_raises(self):
        with pytest.raises(ValueError, match="INFERENCE_BASE_URL.*credentials"):
            _reject_url_userinfo(
                "INFERENCE_BASE_URL",
                "https://user:token@gateway.example/v1",  # pragma: allowlist secret
            )

    def test_userinfo_username_only_raises(self):
        with pytest.raises(ValueError, match="RERANK_BASE_URL.*credentials"):
            _reject_url_userinfo("RERANK_BASE_URL", "https://user@gateway.example/v1")


_PLACEHOLDER_KEY = "sk-test-marker"  # pragma: allowlist secret
_PLACEHOLDER_TOKEN = "synthetic-mcp-token"  # pragma: allowlist secret
_URL_CREDENTIAL_MARKER = "SYNTHETIC_URL_CREDENTIAL"
_INHERITED_URL = (
    f"https://user:{_URL_CREDENTIAL_MARKER}@provider.invalid/v1"  # pragma: allowlist secret
)


class TestInheritedEndpointUserinfo:
    """An empty ``*_BASE_URL`` lets the SDK fall back to its own env var
    (``OPENAI_BASE_URL``, ``ANTHROPIC_BASE_URL``, ``CO_API_URL``). The
    userinfo guard must cover that inherited URL too, so an embedded
    credential never reaches the startup log or an error (#326)."""

    class _FakeDatabase:
        def __init__(self, _path):
            pass

        def get_embedding_dim(self):
            return 4

    def _run_main(self, monkeypatch, caplog, env_var, **config):
        import src.main as main_mod

        defaults = {
            "EMBED_BASE_URL": "",
            "EMBED_MODEL": "synthetic",
            "MCP_AUTH_TOKEN": _PLACEHOLDER_TOKEN,
            "EMBED_API_KEY": _PLACEHOLDER_KEY,
            "INFERENCE_MODE": "none",
            "RERANK_MODE": "none",
        }
        for name, value in {**defaults, **config}.items():
            monkeypatch.setattr(main_mod, name, value)
        for name in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "CO_API_URL"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(env_var, _INHERITED_URL)
        monkeypatch.setattr(main_mod, "Database", self._FakeDatabase)
        monkeypatch.setattr(main_mod, "_run_server", lambda *_: None)
        caplog.set_level(logging.DEBUG)
        with pytest.raises(ValueError, match="credentials") as excinfo:
            main_mod.main()
        assert _URL_CREDENTIAL_MARKER not in str(excinfo.value)
        assert _URL_CREDENTIAL_MARKER not in caplog.text
        return str(excinfo.value)

    def test_inherited_embed_url_with_userinfo_is_rejected(self, monkeypatch, caplog):
        message = self._run_main(monkeypatch, caplog, "OPENAI_BASE_URL")
        assert "EMBED_BASE_URL" in message

    @pytest.mark.parametrize(
        ("mode", "env_var"),
        [("openai", "OPENAI_BASE_URL"), ("anthropic", "ANTHROPIC_BASE_URL")],
    )
    def test_inherited_inference_url_with_userinfo_is_rejected(
        self, monkeypatch, caplog, mode, env_var
    ):
        message = self._run_main(
            monkeypatch,
            caplog,
            env_var,
            EMBED_BASE_URL="http://host.docker.internal:8001/v1",
            INFERENCE_MODE=mode,
            INFERENCE_BASE_URL="",
            INFERENCE_MODEL="synthetic",
            INFERENCE_API_KEY=_PLACEHOLDER_KEY,
        )
        assert "INFERENCE_BASE_URL" in message

    def test_inherited_rerank_url_with_userinfo_is_rejected(self, monkeypatch, caplog):
        message = self._run_main(
            monkeypatch,
            caplog,
            "CO_API_URL",
            EMBED_BASE_URL="http://host.docker.internal:8001/v1",
            RERANK_MODE="cohere",
            RERANK_BASE_URL="",
            RERANK_MODEL="synthetic",
            RERANK_API_KEY=_PLACEHOLDER_KEY,
        )
        assert "RERANK_BASE_URL" in message

    def test_empty_base_urls_without_inherited_urls_still_start(self, monkeypatch, caplog):
        """The contract that an empty base URL selects the SDK default
        is preserved when no inherited URL carries userinfo."""
        import src.main as main_mod

        for name, value in {
            "EMBED_BASE_URL": "",
            "EMBED_MODEL": "synthetic",
            "MCP_AUTH_TOKEN": _PLACEHOLDER_TOKEN,
            "EMBED_API_KEY": _PLACEHOLDER_KEY,
            "INFERENCE_MODE": "anthropic",
            "INFERENCE_BASE_URL": "",
            "INFERENCE_MODEL": "synthetic",
            "INFERENCE_API_KEY": _PLACEHOLDER_KEY,
            "RERANK_MODE": "cohere",
            "RERANK_BASE_URL": "",
            "RERANK_MODEL": "synthetic",
            "RERANK_API_KEY": _PLACEHOLDER_KEY,
        }.items():
            monkeypatch.setattr(main_mod, name, value)
        for name in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "CO_API_URL"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(main_mod, "Database", self._FakeDatabase)
        ran = []
        monkeypatch.setattr(main_mod, "_run_server", lambda *args: ran.append(args))
        caplog.set_level(logging.INFO)
        main_mod.main()
        assert ran
        assert "https://api.openai.com/v1" in caplog.text


class TestRemoteEndpointWarning:
    """An enabled provider layer whose endpoint is not host-local sends
    mail-derived text off the machine. Startup logs one WARNING per such
    layer naming the mode and the endpoint host only (#622)."""

    @staticmethod
    def _warnings(caplog):
        return [r for r in caplog.records if r.levelno == logging.WARNING]

    @pytest.mark.parametrize(
        ("url", "host"),
        [
            ("https://api.anthropic.com", "api.anthropic.com"),
            ("https://gateway.example:8443/v1?tenant=SYNTHETIC_QUERY#frag", "gateway.example"),
            ("http://192.0.2.10:1234/v1", "192.0.2.10"),
        ],
    )
    def test_remote_url_warns_once_with_host_only(self, caplog, url, host):
        caplog.set_level(logging.DEBUG)
        _warn_if_remote_endpoint("INFERENCE_MODE", "openai", url, "retrieved email excerpts")
        [record] = self._warnings(caplog)
        # Exact text, so no scheme, port, path, query or fragment survives.
        assert record.getMessage() == (
            "Privacy: INFERENCE_MODE=openai sends retrieved email excerpts off this "
            f"host, to {host}."
        )

    def test_empty_url_warns_about_sdk_default(self, caplog):
        caplog.set_level(logging.DEBUG)
        _warn_if_remote_endpoint("RERANK_MODE", "cohere", "", "search queries")
        [record] = self._warnings(caplog)
        assert "RERANK_MODE=cohere" in record.getMessage()
        assert "default endpoint" in record.getMessage()

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:1234/v1",
            "http://[::1]:8000/v1",
            "http://localhost:8001/v1",
            "http://LOCALHOST/v1",
            "http://host.docker.internal:8001/v1",
        ],
    )
    def test_host_local_url_is_silent(self, caplog, url):
        caplog.set_level(logging.DEBUG)
        _warn_if_remote_endpoint("EMBED_MODE", "openai", url, "search query text")
        assert self._warnings(caplog) == []

    def _run_main(self, monkeypatch, caplog, **config):
        import src.main as main_mod

        defaults = {
            "MCP_AUTH_TOKEN": _PLACEHOLDER_TOKEN,
            "EMBED_MODEL": "synthetic",
            "EMBED_API_KEY": _PLACEHOLDER_KEY,
            "INFERENCE_MODE": "anthropic",
            "INFERENCE_MODEL": "synthetic",
            "INFERENCE_API_KEY": _PLACEHOLDER_KEY,
            "RERANK_MODE": "cohere",
            "RERANK_MODEL": "synthetic",
            "RERANK_API_KEY": _PLACEHOLDER_KEY,
        }
        for name, value in {**defaults, **config}.items():
            monkeypatch.setattr(main_mod, name, value)
        for name in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "CO_API_URL"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(main_mod, "Database", TestInheritedEndpointUserinfo._FakeDatabase)
        monkeypatch.setattr(main_mod, "_run_server", lambda *_: None)
        caplog.set_level(logging.DEBUG)
        main_mod.main()
        assert _PLACEHOLDER_KEY not in caplog.text
        return [r.getMessage() for r in self._warnings(caplog)]

    def test_main_warns_once_per_remote_layer(self, monkeypatch, caplog):
        warnings = self._run_main(
            monkeypatch,
            caplog,
            EMBED_BASE_URL="",
            INFERENCE_BASE_URL="",
            RERANK_BASE_URL="https://rerank.example/v2",
        )
        # Exact lines: one per layer, each ending at the bare host.
        assert warnings == [
            "Privacy: EMBED_MODE=openai sends search query text off this host, to api.openai.com.",
            "Privacy: INFERENCE_MODE=anthropic sends retrieved email excerpts off this "
            "host, to api.anthropic.com.",
            "Privacy: RERANK_MODE=cohere sends search queries and retrieved email "
            "excerpts off this host, to rerank.example.",
        ]

    def test_main_is_silent_for_host_local_layers(self, monkeypatch, caplog):
        local = "http://host.docker.internal:8001/v1"
        warnings = self._run_main(
            monkeypatch,
            caplog,
            EMBED_BASE_URL=local,
            INFERENCE_MODE="openai",
            INFERENCE_BASE_URL=local,
            RERANK_BASE_URL="http://127.0.0.1:8002",
        )
        assert warnings == []

    def test_main_skips_disabled_layers(self, monkeypatch, caplog):
        warnings = self._run_main(
            monkeypatch,
            caplog,
            EMBED_BASE_URL="http://localhost:8001/v1",
            INFERENCE_MODE="none",
            INFERENCE_BASE_URL="",
            RERANK_MODE="none",
            RERANK_BASE_URL="",
        )
        assert warnings == []


class TestRequireEnv:
    def test_passes_through_present_value(self):
        assert _require_env("INFERENCE_MODE", "openai", "INFERENCE_BASE_URL", "https://x") == (
            "https://x"
        )

    def test_empty_value_raises_with_actionable_message(self):
        # The no-fallback rule: a chosen mode without its required vars
        # must surface here, not silently route to a different provider.
        with pytest.raises(ValueError) as excinfo:
            _require_env("INFERENCE_MODE", "anthropic", "INFERENCE_API_KEY", "")
        msg = str(excinfo.value)
        assert "INFERENCE_API_KEY" in msg
        assert "INFERENCE_MODE" in msg
        assert "anthropic" in msg

    def test_inference_openai_mode_requires_api_key(self):
        # Tightened contract: every enabled inference layer requires a
        # non-empty key, including ``openai`` mode. Pre-fix an operator
        # pointing at a remote OpenAI-compatible provider could start
        # cleanly and only fail at first intelligence call with a 401.
        # Operators pointing at an unauthenticated host-side server
        # supply any placeholder string (``unauthenticated``) so the
        # startup contract holds uniformly.
        with pytest.raises(ValueError) as excinfo:
            _require_env("INFERENCE_MODE", "openai", "INFERENCE_API_KEY", "")
        msg = str(excinfo.value)
        assert "INFERENCE_API_KEY" in msg
        assert "openai" in msg

    def test_embed_mode_requires_api_key(self):
        # ``EMBED_MODE=openai`` is the only valid embed mode (no ``none``
        # mode exists), and the contract is the same across layers:
        # non-empty key required. Pre-fix the indexer + mcp-server would
        # accept an empty embed key and silently send an empty bearer
        # token on first call.
        with pytest.raises(ValueError) as excinfo:
            _require_env("EMBED_MODE", "openai", "EMBED_API_KEY", "")
        msg = str(excinfo.value)
        assert "EMBED_API_KEY" in msg
        assert "openai" in msg


class TestExperimentalToolsFlag:
    """``MCP_EXPERIMENTAL_TOOLS`` gates experimental tools (``brief_issue``,
    ``check_conclusion``; PLAN.md Resolved decisions 12): off unless exactly ``true``, an
    unrecognized value fails startup, and the tools need inference."""

    class _FakeDatabase:
        def __init__(self, _path):
            pass

        def get_embedding_dim(self):
            return 4

    def _load(self, monkeypatch, value):
        import importlib

        import src.main as main_mod

        if value is None:
            monkeypatch.delenv("MCP_EXPERIMENTAL_TOOLS", raising=False)
        else:
            monkeypatch.setenv("MCP_EXPERIMENTAL_TOOLS", value)
        try:
            return importlib.reload(main_mod).MCP_EXPERIMENTAL_TOOLS
        finally:
            monkeypatch.delenv("MCP_EXPERIMENTAL_TOOLS", raising=False)
            importlib.reload(main_mod)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, False), ("", False), ("false", False), ("FALSE", False), ("true", True)],
    )
    def test_flag_parses_strictly(self, monkeypatch, value, expected):
        assert self._load(monkeypatch, value) is expected

    @pytest.mark.parametrize("value", ["1", "yes", "on", "ture", "true!"])
    def test_unrecognized_value_fails_startup(self, monkeypatch, value):
        with pytest.raises(ValueError, match="MCP_EXPERIMENTAL_TOOLS"):
            self._load(monkeypatch, value)

    def _tool_names(self, monkeypatch, *, experimental, inference_mode="anthropic"):
        import src.main as main_mod
        from fastmcp import Client

        for name, value in {
            "EMBED_BASE_URL": "http://host.docker.internal:8001/v1",
            "EMBED_MODEL": "synthetic",
            "MCP_AUTH_TOKEN": _PLACEHOLDER_TOKEN,
            "EMBED_API_KEY": _PLACEHOLDER_KEY,
            "INFERENCE_MODE": inference_mode,
            "INFERENCE_BASE_URL": "http://host.docker.internal:8002",
            "INFERENCE_MODEL": "synthetic",
            "INFERENCE_API_KEY": _PLACEHOLDER_KEY,
            "RERANK_MODE": "none",
            "MCP_EXPERIMENTAL_TOOLS": experimental,
        }.items():
            monkeypatch.setattr(main_mod, name, value)
        monkeypatch.setattr(main_mod, "Database", self._FakeDatabase)
        servers = []
        monkeypatch.setattr(main_mod, "_run_server", lambda server: servers.append(server))
        main_mod.main()

        async def names():
            async with Client(servers[0]) as client:
                return {t.name for t in await client.list_tools()}

        return asyncio.run(names())

    _EXPERIMENTAL = {"brief_issue", "check_conclusion"}

    def test_experimental_tools_are_not_registered_by_default(self, monkeypatch):
        names = self._tool_names(monkeypatch, experimental=False)
        assert "ask_mailbox" in names
        assert not names & self._EXPERIMENTAL

    def test_experimental_tools_are_registered_when_enabled(self, monkeypatch):
        assert self._EXPERIMENTAL <= self._tool_names(monkeypatch, experimental=True)

    def test_experimental_tools_need_inference(self, monkeypatch, caplog):
        caplog.set_level(logging.INFO)
        names = self._tool_names(monkeypatch, experimental=True, inference_mode="none")
        assert not names & self._EXPERIMENTAL
        assert "Experimental tools not registered" in caplog.text


class TestHealthEndpoint:
    """The /health route delegates to ``_health_response``, which the
    tests call directly with a stub DB.

    This mirrors how the docker healthcheck calls it (one HTTP GET) and
    catches regressions in the 200/503 split that would otherwise only
    show up when the container goes unhealthy in production.
    """

    def test_health_returns_ok_when_db_reachable(self):
        from src.main import _health_response

        class OkDB:
            def ping(self):
                return None

        response = asyncio.run(_health_response(OkDB()))
        assert response.status_code == 200
        assert b'"ok"' in response.body

    def test_health_returns_503_when_db_raises(self):
        from src.main import _health_response

        class BadDB:
            def ping(self):
                raise RuntimeError("db unreachable")

        response = asyncio.run(_health_response(BadDB()))
        assert response.status_code == 503
        assert b'"unhealthy"' in response.body
        # The error string itself must NOT leak into the response body.
        # The handler is documented to keep it generic so the endpoint
        # cannot be used to probe DB paths or schema details.
        assert b"db unreachable" not in response.body

    def test_ping_does_not_block_the_event_loop(self):
        """#320: the probe runs in a worker thread, so a slow SQLite open
        cannot stall MCP requests sharing the loop."""
        from src.main import _health_response

        entered = threading.Event()
        release = threading.Event()
        released_by_loop: list[bool] = []

        class SlowDB:
            def ping(self):
                entered.set()
                # Only the event loop sets ``release``; run on the loop,
                # this wait cannot be answered and times out.
                released_by_loop.append(release.wait(timeout=2))

        async def scenario():
            task = asyncio.create_task(_health_response(SlowDB()))
            while not entered.is_set() and not task.done():
                await asyncio.sleep(0.001)
            release.set()
            return await task

        response = asyncio.run(scenario())
        assert released_by_loop == [True]
        assert response.status_code == 200


class TestSilenceClientDisconnect:
    """The log filter that drops ``ClientDisconnect`` traceback noise.

    ``mcp.server.streamable_http`` logs ``"Error handling POST
    request"`` with the ``ClientDisconnect`` traceback in ``exc_info``.
    Filter by exception class; records with any other exception, or
    none, must propagate unchanged so a real bug still surfaces
    normally.
    """

    @staticmethod
    def _record(
        msg: str = "Error handling POST request",
        exc_info: object = None,
        name: str = "mcp.server.streamable_http",
    ) -> logging.LogRecord:
        return logging.LogRecord(
            name=name,
            level=logging.ERROR,
            pathname="x",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=exc_info,
        )

    def test_drops_record_with_clientdisconnect_exc_info(self):
        from src.main import _SilenceClientDisconnect

        # Stand in a synthetic exception that mirrors the *type name* the
        # filter checks for. Avoids importing starlette in the test file
        # (which would change the dependency surface for tests).
        class ClientDisconnect(Exception):  # noqa: N818 — mirrors starlette name
            pass

        exc = ClientDisconnect()
        record = self._record(exc_info=(type(exc), exc, exc.__traceback__))
        assert _SilenceClientDisconnect().filter(record) is False

    def test_lets_through_record_with_other_exception(self):
        from src.main import _SilenceClientDisconnect

        exc = RuntimeError("real bug")
        record = self._record(exc_info=(type(exc), exc, exc.__traceback__))
        assert _SilenceClientDisconnect().filter(record) is True

    def test_lets_through_record_with_no_exc_info_and_other_message(self):
        # No exc_info — ordinary log record on the same logger, must
        # propagate.
        from src.main import _SilenceClientDisconnect

        record = self._record(msg="Some other event the SDK might log")
        assert _SilenceClientDisconnect().filter(record) is True


class TestTelemetryOff:
    """FastMCP's OpenTelemetry instrumentation is switched off (#492).

    fastmcp defaults ``telemetry_mode`` to ``native``: it creates spans and
    propagates trace context, which stays a no-op only while no OTel SDK
    and exporter are configured. ``src.main`` sets ``off`` at import so the
    privacy posture does not depend on that absence.
    """

    def test_import_sets_telemetry_off_over_environment(self):
        # A fresh interpreter, so the import-time setting is what the test
        # observes, with the environment asking for the fastmcp default.
        import os
        import subprocess
        import sys
        from pathlib import Path

        env = {**os.environ, "FASTMCP_TELEMETRY_MODE": "native"}
        script = (
            "import fastmcp, fastmcp.telemetry\n"
            "assert fastmcp.settings.telemetry_mode == 'native'\n"
            "import src.main\n"
            "print(fastmcp.settings.telemetry_mode, fastmcp.telemetry.telemetry_mode())\n"
        )
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        assert result.stdout.split() == ["off", "off"]
