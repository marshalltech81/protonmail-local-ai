"""Tests for ``src.lib.inference.InferenceClient``.

The client dispatches by mode to the official ``openai`` or
``anthropic`` SDK. Tests monkey-patch the SDK's create methods so
behavior is deterministic without hitting a live provider.
"""

import asyncio
from types import SimpleNamespace

import pytest
from src.lib.inference import InferenceClient, _AnthropicBackend, _OpenAIBackend


def _openai_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
    )


def _anthropic_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
    )


class TestFactory:
    def test_openai_mode_constructs_openai_backend(self):
        c = InferenceClient.create(
            mode="openai",
            base_url="http://x/v1",
            model="qwen",
            api_key="sk-test",  # pragma: allowlist secret
        )
        assert c.mode == "openai"
        assert isinstance(c._backend, _OpenAIBackend)

    def test_anthropic_mode_constructs_anthropic_backend(self):
        c = InferenceClient.create(
            mode="anthropic",
            base_url="https://api.anthropic.com",
            model="claude-x",
            api_key="sk-ant-test",  # pragma: allowlist secret
        )
        assert c.mode == "anthropic"
        assert isinstance(c._backend, _AnthropicBackend)

    def test_anthropic_with_empty_base_url_omits_kwarg_so_sdk_default_applies(self, monkeypatch):
        # The Anthropic SDK's default endpoint is the documented contract
        # for INFERENCE_MODE=anthropic when INFERENCE_BASE_URL is empty.
        # The backend must NOT substitute a hardcoded URL constant or
        # pass an empty ``base_url`` kwarg — either would override the
        # SDK default. Verify the kwarg is genuinely ABSENT from the
        # construction call by intercepting ``AsyncAnthropic`` itself.
        captured: dict = {}

        class FakeAsyncAnthropic:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs
                # Mimic the SDK's resolved base_url so the constructor
                # can read it back into ``self.base_url``.
                self.base_url = "https://api.anthropic.com"

        import anthropic

        monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAsyncAnthropic)

        c = InferenceClient.create(
            mode="anthropic",
            base_url="",
            model="claude-x",
            api_key="sk-ant-test",  # pragma: allowlist secret
        )
        assert isinstance(c._backend, _AnthropicBackend)
        # The contract: no ``base_url`` kwarg reaches AsyncAnthropic
        # when the operator left INFERENCE_BASE_URL empty. ``api_key``
        # and ``timeout`` still flow through.
        assert "base_url" not in captured["kwargs"]
        assert captured["kwargs"]["api_key"] == "sk-ant-test"  # pragma: allowlist secret
        # After construction, ``self.base_url`` reflects the SDK's
        # resolved endpoint — same pattern as ``_OpenAIBackend``,
        # ``EmbedClient``, and ``OpenAIEmbedder``.
        assert c._backend.base_url == "https://api.anthropic.com"

    def test_anthropic_with_explicit_base_url_passes_kwarg(self, monkeypatch):
        # Counterpart to the empty-base_url test: when the operator DOES
        # set INFERENCE_BASE_URL (compatible gateway, region override,
        # proxy), that exact value must reach AsyncAnthropic.
        captured: dict = {}

        class FakeAsyncAnthropic:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs
                # Mimic the SDK storing the operator-supplied base_url.
                self.base_url = kwargs.get("base_url", "")

        import anthropic

        monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAsyncAnthropic)

        InferenceClient.create(
            mode="anthropic",
            base_url="https://gateway.example.com/anthropic",
            model="claude-x",
            api_key="sk-ant-test",  # pragma: allowlist secret
        )
        assert captured["kwargs"]["base_url"] == "https://gateway.example.com/anthropic"

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="unsupported mode"):
            InferenceClient.create(
                mode="local",
                base_url="http://x",
                model="m",
                api_key="k",
            )

    def test_anthropic_rejects_base_url_ending_in_v1(self):
        # Operators carrying over the pre-collapse
        # INFERENCE_ANTHROPIC_BASE_URL=https://api.anthropic.com/v1 must
        # see a clear error rather than a runtime 404 on
        # '.../v1/v1/messages'. The Anthropic SDK appends '/v1/messages'
        # itself, so the base URL must not already include '/v1'.
        with pytest.raises(ValueError, match=r"must not end with '/v1'"):
            InferenceClient.create(
                mode="anthropic",
                base_url="https://api.anthropic.com/v1",
                model="claude-x",
                api_key="sk-ant-test",  # pragma: allowlist secret
            )

    def test_anthropic_rejects_base_url_ending_in_v1_with_trailing_slash(self):
        # The trailing slash is stripped before the suffix check so a
        # value like 'https://api.anthropic.com/v1/' is rejected the
        # same way as the no-slash form.
        with pytest.raises(ValueError, match=r"must not end with '/v1'"):
            InferenceClient.create(
                mode="anthropic",
                base_url="https://api.anthropic.com/v1/",
                model="claude-x",
                api_key="sk-ant-test",  # pragma: allowlist secret
            )

    def test_openai_with_empty_base_url_falls_back_to_sdk_default(self, monkeypatch):
        # ``INFERENCE_BASE_URL=""`` for ``INFERENCE_MODE=openai`` means
        # "use the SDK default" (OpenAI proper). The required non-empty
        # ``INFERENCE_API_KEY`` upstream is the explicit-intent signal:
        # an operator with a real ``sk-...`` has unambiguously chosen
        # their provider, so we trust the documented SDK fallback.
        # Symmetric with ``_AnthropicBackend``'s empty-URL path and
        # with ``EmbedClient`` / ``OpenAIEmbedder``.
        #
        # Intercept ``AsyncOpenAI`` to confirm ``base_url`` is NOT
        # passed as a kwarg when empty — passing an empty string would
        # defeat the SDK's fallback because the SDK only treats
        # ``None`` as "missing."
        captured: dict = {}

        class FakeAsyncOpenAI:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs
                # Mimic the SDK's resolved base_url so the constructor
                # can read it back into ``self.base_url``.
                self.base_url = "https://api.openai.com/v1/"

        import openai

        monkeypatch.setattr(openai, "AsyncOpenAI", FakeAsyncOpenAI)

        c = InferenceClient.create(
            mode="openai",
            base_url="",
            model="gpt-4",
            api_key="sk-real",  # pragma: allowlist secret
        )
        # ``base_url`` kwarg is genuinely absent — the SDK fallback
        # chain fires when the kwarg is missing, not when it is
        # passed as empty string.
        assert "base_url" not in captured["kwargs"]
        assert captured["kwargs"]["api_key"] == "sk-real"  # pragma: allowlist secret
        # After construction, ``self.base_url`` reflects the SDK's
        # resolved endpoint, not the empty string the operator typed.
        assert c._backend.base_url == "https://api.openai.com/v1"

    def test_openai_with_explicit_base_url_passes_kwarg(self, monkeypatch):
        # Counterpart: when the operator DOES set INFERENCE_BASE_URL
        # (host-side server, alternative provider, gateway), that
        # exact value must reach AsyncOpenAI.
        captured: dict = {}

        class FakeAsyncOpenAI:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs
                self.base_url = kwargs.get("base_url", "") + "/"

        import openai

        monkeypatch.setattr(openai, "AsyncOpenAI", FakeAsyncOpenAI)

        InferenceClient.create(
            mode="openai",
            base_url="http://host.docker.internal:8001/v1",
            model="qwen",
            api_key="sk-test",  # pragma: allowlist secret
        )
        assert captured["kwargs"]["base_url"] == "http://host.docker.internal:8001/v1"

    def test_client_re_exposes_backend_base_url(self, monkeypatch):
        # ``main.py`` logs ``inference_client.base_url`` at startup so
        # operators see the resolved wire endpoint (the SDK default
        # literal, not the empty string they typed). The class must
        # re-expose ``backend.base_url`` — without this attribute the
        # startup log would have to print "(SDK default)" again, which
        # is too vague in a privacy-sensitive deployment.
        class FakeAsyncAnthropic:
            def __init__(self, **kwargs):
                self.base_url = "https://api.anthropic.com"

        import anthropic

        monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAsyncAnthropic)

        c = InferenceClient.create(
            mode="anthropic",
            base_url="",
            model="claude-x",
            api_key="sk-ant-test",  # pragma: allowlist secret
        )
        assert c.base_url == "https://api.anthropic.com"
        assert c.base_url == c._backend.base_url


class TestComplete:
    def test_openai_backend_returns_message_content(self):
        c = InferenceClient.create(
            mode="openai",
            base_url="http://x/v1",
            model="qwen",
            api_key="sk-test",  # pragma: allowlist secret
        )

        captured: dict = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return _openai_response("hi from openai")

        c._backend.client.chat.completions.create = fake_create  # type: ignore[assignment]
        out = asyncio.run(c.complete("sys", "user"))
        assert out == "hi from openai"
        # Both system and user roles must reach the chat-completions
        # request — the prompt-injection defense in intelligence tools
        # depends on the user prompt being separable from the system
        # prompt.
        roles = [m["role"] for m in captured["messages"]]
        assert roles == ["system", "user"]

    def test_anthropic_backend_returns_first_text_block(self):
        c = InferenceClient.create(
            mode="anthropic",
            base_url="https://api.anthropic.com",
            model="claude-x",
            api_key="sk-ant-test",  # pragma: allowlist secret
        )

        captured: dict = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return _anthropic_response("hi from anthropic")

        c._backend.client.messages.create = fake_create  # type: ignore[assignment]
        out = asyncio.run(c.complete("sys", "user"))
        assert out == "hi from anthropic"
        # Anthropic Messages API takes the system prompt as a top-level
        # field, not a role-tagged message.
        assert captured["system"] == "sys"
        assert captured["messages"][0]["role"] == "user"

    def test_anthropic_backend_raises_when_no_text_block(self):
        # A Messages response with only non-text blocks (e.g. tool_use
        # only) is an actionable provider error, not a "successful empty
        # answer." Pre-fix the backend returned ``""``, which surfaced
        # to the caller as a blank tool response with no signal that
        # anything went wrong — structured callers got a confusing
        # JSONDecodeError two layers down, prose callers passed the
        # empty string straight to the agent. Raise here so all callers
        # see a sanitized RuntimeError naming the failure mode.
        c = InferenceClient.create(
            mode="anthropic",
            base_url="https://api.anthropic.com",
            model="claude-x",
            api_key="sk-ant-test",  # pragma: allowlist secret
        )

        async def fake_create(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(type="tool_use", text=None)],
            )

        c._backend.client.messages.create = fake_create  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="no text blocks"):
            asyncio.run(c.complete("sys", "user"))

    def test_anthropic_backend_raises_when_content_empty(self):
        # A Messages response with ``content=[]`` (provider abandoned the
        # generation, content-filter trip, etc.) also collapses to an
        # empty text result. Same RuntimeError surface.
        c = InferenceClient.create(
            mode="anthropic",
            base_url="https://api.anthropic.com",
            model="claude-x",
            api_key="sk-ant-test",  # pragma: allowlist secret
        )

        async def fake_create(**_kwargs):
            return SimpleNamespace(content=[])

        c._backend.client.messages.create = fake_create  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="no text blocks"):
            asyncio.run(c.complete("sys", "user"))

    def test_openai_backend_raises_when_choices_empty(self):
        # OpenAI-compatible servers occasionally return empty
        # ``choices`` (provider error states, content-filter trips).
        # Pre-fix this raised an opaque ``IndexError`` on
        # ``resp.choices[0]``; the new contract is a sanitized
        # RuntimeError naming the failure mode so the operator-facing
        # log line is actionable.
        c = InferenceClient.create(
            mode="openai",
            base_url="http://x/v1",
            model="qwen",
            api_key="sk-test",  # pragma: allowlist secret
        )

        async def fake_create(**_kwargs):
            return SimpleNamespace(choices=[])

        c._backend.client.chat.completions.create = fake_create  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="no choices"):
            asyncio.run(c.complete("sys", "user"))

    def test_openai_backend_raises_when_content_none(self):
        # A ``message.content=None`` case (tool-call-only delta,
        # length-truncated response) was previously coerced to ``""``
        # via ``or ""`` and returned as a successful empty answer. Now
        # it raises so the failure is visible.
        c = InferenceClient.create(
            mode="openai",
            base_url="http://x/v1",
            model="qwen",
            api_key="sk-test",  # pragma: allowlist secret
        )

        async def fake_create(**_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=None))],
            )

        c._backend.client.chat.completions.create = fake_create  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="empty content"):
            asyncio.run(c.complete("sys", "user"))

    def test_openai_backend_raises_when_content_empty_string(self):
        # The ``or ""`` coercion previously swallowed an explicit
        # empty string too. Reject the same way as None — both are
        # "no answer" signals from the provider.
        c = InferenceClient.create(
            mode="openai",
            base_url="http://x/v1",
            model="qwen",
            api_key="sk-test",  # pragma: allowlist secret
        )

        async def fake_create(**_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=""))],
            )

        c._backend.client.chat.completions.create = fake_create  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="empty content"):
            asyncio.run(c.complete("sys", "user"))

    def test_anthropic_backend_concatenates_multiple_text_blocks(self):
        # A future model might return multiple text blocks (or thinking
        # + text). The backend joins all text-typed blocks so the full
        # answer reaches the caller; non-text blocks are skipped.
        c = InferenceClient.create(
            mode="anthropic",
            base_url="https://api.anthropic.com",
            model="claude-x",
            api_key="sk-ant-test",  # pragma: allowlist secret
        )

        async def fake_create(**_kwargs):
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="thinking", text="reasoning trace"),
                    SimpleNamespace(type="text", text="part one. "),
                    SimpleNamespace(type="text", text="part two."),
                    SimpleNamespace(type="tool_use", text=None),
                ],
            )

        c._backend.client.messages.create = fake_create  # type: ignore[assignment]
        out = asyncio.run(c.complete("sys", "user"))
        assert out == "part one. part two."

    def test_anthropic_backend_passes_max_tokens_through(self):
        # Operator override of INFERENCE_MAX_TOKENS must reach the
        # Messages API call so the model is allowed to produce longer
        # outputs (e.g. detailed summaries on long threads).
        c = InferenceClient.create(
            mode="anthropic",
            base_url="https://api.anthropic.com",
            model="claude-x",
            api_key="sk-ant-test",  # pragma: allowlist secret
            max_tokens=4096,
        )
        captured: dict = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])

        c._backend.client.messages.create = fake_create  # type: ignore[assignment]
        asyncio.run(c.complete("sys", "user"))
        assert captured["max_tokens"] == 4096

    def test_openai_backend_passes_max_tokens_through(self):
        # The Chat Completions API accepts ``max_tokens`` and most
        # OpenAI-compatible servers (vLLM, mlx_lm.server, LM Studio,
        # DeepInfra) honor it. INFERENCE_MAX_TOKENS must reach the
        # OpenAI path too — pre-fix the kwarg was silently dropped, so
        # an operator who set the env var saw no effect in OpenAI mode.
        c = InferenceClient.create(
            mode="openai",
            base_url="http://x/v1",
            model="qwen",
            api_key="sk-test",  # pragma: allowlist secret
            max_tokens=4096,
        )
        captured: dict = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return _openai_response("ok")

        c._backend.client.chat.completions.create = fake_create  # type: ignore[assignment]
        asyncio.run(c.complete("sys", "user"))
        assert captured["max_tokens"] == 4096
