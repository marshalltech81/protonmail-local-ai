"""Tests for ``src.lib.inference.InferenceClient``.

The client dispatches by mode to the official ``openai`` or
``anthropic`` SDK. Tests monkey-patch the SDK's create methods so
behavior is deterministic without hitting a live provider.
"""

import asyncio
from types import SimpleNamespace

import pytest
from src.lib.inference import InferenceClient


def _openai_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
    )


def _anthropic_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
    )


class TestFactory:
    def test_openai_mode_dispatches_to_openai_backend(self):
        c = InferenceClient.create(
            mode="openai",
            base_url="http://x/v1",
            model="qwen",
            api_key="sk-test",  # pragma: allowlist secret
        )
        assert c.mode == "openai"

    def test_anthropic_mode_dispatches_to_anthropic_backend(self):
        c = InferenceClient.create(
            mode="anthropic",
            base_url="https://api.anthropic.com",
            model="claude-x",
            api_key="sk-ant-test",  # pragma: allowlist secret
        )
        assert c.mode == "anthropic"

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="unsupported mode"):
            InferenceClient.create(
                mode="local",
                base_url="http://x",
                model="m",
                api_key="k",
            )


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

    def test_anthropic_backend_returns_empty_when_no_text_block(self):
        # Defensive path: a Messages response with only non-text blocks
        # (e.g. tool_use only) should not raise — return empty so the
        # caller's downstream JSON parse fails cleanly with a clear
        # "No structured data" message rather than a TypeError.
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
        out = asyncio.run(c.complete("sys", "user"))
        assert out == ""
