"""Per-call usage capture, using SDK response stubs only (#1255)."""

import asyncio
import json
import logging
from types import SimpleNamespace as NS

import pytest
from src.lib.inference import InferenceClient, InferenceTruncatedError, capture_usage

from tests.answer_eval.runner import RecordingInference

MARKER = "SYNTHETIC_USAGE_PRIVATE_MARKER"


def client_for(monkeypatch, mode, respond):
    client = InferenceClient.create(
        mode=mode,
        base_url="https://provider.example",
        model="synthetic",
        api_key="synthetic",  # pragma: allowlist secret
    )
    api = client._backend.client
    target = api.chat.completions if mode == "openai" else api.messages
    monkeypatch.setattr(target, "create", respond)
    return client


def response(mode, usage, *, truncated=False, text="ok"):
    if mode == "openai":
        return NS(
            usage=usage,
            choices=[NS(message=NS(content=text), finish_reason="length" if truncated else "stop")],
        )
    return NS(
        usage=usage,
        content=[NS(type="text", text=text)],
        stop_reason="max_tokens" if truncated else "end_turn",
    )


@pytest.mark.parametrize("mode", ["openai", "anthropic"])
@pytest.mark.parametrize("truncated", [False, True])
def test_provider_usage_survives_normal_and_truncated_replies(monkeypatch, mode, truncated, caplog):
    usage = (
        NS(
            prompt_tokens=19,
            completion_tokens=7,
            prompt_tokens_details=NS(cached_tokens=11, cache_write_tokens=3),
        )
        if mode == "openai"
        else NS(
            input_tokens=19,
            output_tokens=7,
            cache_read_input_tokens=11,
            cache_creation_input_tokens=3,
        )
    )

    async def respond(**kwargs):
        return response(mode, usage, truncated=truncated, text=MARKER)

    recorder = RecordingInference(client_for(monkeypatch, mode, respond))
    with caplog.at_level(logging.INFO):
        if truncated:
            with pytest.raises(InferenceTruncatedError):
                asyncio.run(recorder.complete(MARKER, MARKER))
        else:
            assert asyncio.run(recorder.complete(MARKER, MARKER)) == MARKER
    assert recorder.calls[0].usage == {
        "input_tokens": 19,
        "output_tokens": 7,
        "cache_read_input_tokens": 11,
        "cache_creation_input_tokens": 3,
    }
    assert recorder.calls[0].outcome == ("truncated" if truncated else "ok")
    assert MARKER not in caplog.text
    assert MARKER not in json.dumps(recorder.calls[0].usage)


@pytest.mark.parametrize("mode", ["openai", "anthropic"])
@pytest.mark.parametrize(
    "usage",
    [
        None,
        NS(input_tokens=MARKER, output_tokens=-1),
        NS(
            prompt_tokens=True,
            completion_tokens=2.5,
            prompt_tokens_details=NS(cached_tokens=MARKER),
        ),
    ],
)
def test_missing_or_invalid_counts_are_unknown_not_zero(monkeypatch, mode, usage):
    async def respond(**kwargs):
        return response(mode, usage)

    recorder = RecordingInference(client_for(monkeypatch, mode, respond))
    assert asyncio.run(recorder.complete("s", "u")) == "ok"
    assert recorder.calls[0].usage == dict.fromkeys(
        ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )


def test_concurrent_cases_and_repair_calls_keep_their_own_usage(monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def respond(**kwargs):
            label = kwargs["messages"][1]["content"]
            if label == "first":
                entered.set()
                await release.wait()
            else:
                release.set()
            return response("openai", NS(prompt_tokens=len(label), completion_tokens=0))

        client = client_for(monkeypatch, "openai", respond)
        first, second = RecordingInference(client), RecordingInference(client)
        pending = asyncio.create_task(first.complete("s", "first"))
        await entered.wait()
        await second.complete("s", "second")
        await pending
        await first.complete("s", "repair")
        assert [c.usage["input_tokens"] for c in first.calls] == [5, 6]
        assert [c.usage["input_tokens"] for c in second.calls] == [6]
        assert all(c.usage["output_tokens"] == 0 for c in first.calls + second.calls)
        # A completed capture does not leak into a later independent one.
        with capture_usage() as calls:
            await client.complete("s", "later")
        assert len(calls) == 1
        assert calls[0]["input_tokens"] == 5

    asyncio.run(scenario())
