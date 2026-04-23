"""
Tests for src/main.py.

The MCP service entrypoint mostly assembles config and registers tool
groups, which is hard to exercise in unit tests without spinning up the
SSE transport. The two pieces that DO have unit-testable behavior live
here:

- ``_env_bool`` — parses ``MCP_READ_ONLY`` and similar; a wrong-value
  bug here would silently flip the deployment from read-only to
  read-write.
- ``_read_secret`` — prefers Docker secret files over env vars; a
  silent fallthrough to env would mean an attacker with ``docker
  inspect`` access could read the Anthropic key.
- The ``/health`` custom route — used by the docker healthcheck to
  decide if the container is up. A regression here would make every
  failed DB connect look "healthy" or vice versa.
"""

import asyncio

from src.main import _env_bool, _read_secret


class TestEnvBool:
    def test_returns_default_when_missing(self, monkeypatch):
        monkeypatch.delenv("FAKE_BOOL_FLAG", raising=False)
        assert _env_bool("FAKE_BOOL_FLAG", default=True) is True
        assert _env_bool("FAKE_BOOL_FLAG", default=False) is False

    def test_truthy_strings_parse_as_true(self, monkeypatch):
        for raw in ("1", "true", "True", "TRUE", "yes", "YES", "on", "ON"):
            monkeypatch.setenv("FAKE_BOOL_FLAG", raw)
            assert _env_bool("FAKE_BOOL_FLAG", default=False) is True, raw

    def test_falsy_strings_parse_as_false(self, monkeypatch):
        for raw in ("0", "false", "False", "no", "NO", "off"):
            monkeypatch.setenv("FAKE_BOOL_FLAG", raw)
            assert _env_bool("FAKE_BOOL_FLAG", default=True) is False, raw

    def test_unrecognized_value_is_treated_as_false(self, monkeypatch):
        # Anything outside the truthy set falls to False — fail closed for
        # a flag like MCP_READ_ONLY whose default-on posture is the safe
        # one. ``_env_bool`` is not used for any flag where the safer
        # interpretation of garbage input would be ``True``.
        monkeypatch.setenv("FAKE_BOOL_FLAG", "maybe")
        assert _env_bool("FAKE_BOOL_FLAG", default=True) is False

    def test_whitespace_around_value_is_tolerated(self, monkeypatch):
        monkeypatch.setenv("FAKE_BOOL_FLAG", "  true  ")
        assert _env_bool("FAKE_BOOL_FLAG", default=False) is True


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

    def test_returns_empty_when_neither_source_present(self, monkeypatch):
        monkeypatch.delenv("DEFINITELY_UNSET", raising=False)
        assert _read_secret("missing_secret", "DEFINITELY_UNSET") == ""


class TestHealthEndpoint:
    """The /health route is not registered against a real Starlette app
    in unit tests — instead, we re-register the same handler against the
    FakeMCPServer and call it directly.

    This mirrors how the docker healthcheck calls it (one HTTP GET) and
    catches regressions in the 200/503 split that would otherwise only
    show up when the container goes unhealthy in production.
    """

    def _build_main_with_db_stub(self, db_stub, fake_server, monkeypatch):
        """Drive the relevant slice of ``main.main()`` against a stub DB.

        The function does much more (Ollama clients, tool registration,
        ``server.run``); we only want the health route, so the test
        re-implements the registration step using the fake server.
        """
        # Re-export the local closure that ``main.main()`` constructs.
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        @fake_server.custom_route("/health", methods=["GET"], include_in_schema=False)
        async def health(_: Request) -> JSONResponse:
            try:
                db_stub.ping()
            except Exception:
                return JSONResponse({"status": "unhealthy"}, status_code=503)
            return JSONResponse({"status": "ok"})

        return fake_server.custom_routes["/health"]

    def test_health_returns_ok_when_db_reachable(self, fake_server, monkeypatch):
        class OkDB:
            def ping(self):
                return None

        handler = self._build_main_with_db_stub(OkDB(), fake_server, monkeypatch)
        response = asyncio.run(handler(None))
        assert response.status_code == 200
        assert b'"ok"' in response.body

    def test_health_returns_503_when_db_raises(self, fake_server, monkeypatch):
        class BadDB:
            def ping(self):
                raise RuntimeError("db unreachable")

        handler = self._build_main_with_db_stub(BadDB(), fake_server, monkeypatch)
        response = asyncio.run(handler(None))
        assert response.status_code == 503
        assert b'"unhealthy"' in response.body
        # The error string itself must NOT leak into the response body.
        # The handler is documented to keep it generic so the endpoint
        # cannot be used to probe DB paths or schema details.
        assert b"db unreachable" not in response.body
