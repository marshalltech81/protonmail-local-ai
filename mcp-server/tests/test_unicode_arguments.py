"""Wire serialization must remain safe when client strings contain surrogates."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from src.lib import argument_validation
from src.lib.argument_validation import ArgumentValidationLog
from src.tools.retrieval import register_retrieval_tools


@pytest.mark.parametrize(
    "tool,args",
    [
        (
            "query_messages",
            {"where": {"all": [{"leaf": "subject_contains", "value": "x", "id": "\ud800"}]}},
        ),
        ("query_messages", {"date_from": "\udfff"}),
        ("get_message", {"message_id": "missing", "fields": ["\ud800"]}),
    ],
)
def test_invalid_unicode_is_a_serializable_fixed_error(seeded_db, caplog, tool, args):
    server = FastMCP("unicode-test")
    server.add_middleware(ArgumentValidationLog(server))
    register_retrieval_tools(server, seeded_db)

    async def run():
        async with Client(server) as client:
            return await client.call_tool_mcp(tool, args)

    with caplog.at_level(logging.INFO):
        result = asyncio.run(run())
    assert result.is_error
    assert "Tool arguments must contain valid Unicode" in result.content[0].text
    result.model_dump_json()  # in-memory clients otherwise miss this boundary
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in caplog.text)


def test_valid_unicode_is_unchanged_and_rejection_is_rate_limited(caplog):
    server = FastMCP("unicode-test")
    server.add_middleware(ArgumentValidationLog(server))
    calls = []

    @server.tool
    def echo(value: str) -> str:
        calls.append(value)
        return value

    async def run():
        async with Client(server) as client:
            good = await client.call_tool_mcp("echo", {"value": "café 😀"})
            for _ in range(3):
                bad = await client.call_tool_mcp("echo", {"value": "PRIVATE_MARKER\ud800"})
                assert bad.is_error
                bad.model_dump_json()
            return good

    with caplog.at_level(logging.INFO):
        result = asyncio.run(run())
    assert result.content[0].text == "café 😀"
    assert calls == ["café 😀"]
    assert "PRIVATE_MARKER" not in caplog.text
    assert caplog.text.count("rejected invalid argument: echo.other") == 1


@pytest.mark.parametrize(
    "name,args",
    [
        ("echo", {"nested": [{"\ud800": "x"}]}),
        ("bad\ud800", {}),
    ],
)
def test_invalid_keys_and_tool_names_are_refused_before_dispatch(name, args, caplog):
    server = FastMCP("unicode-test")
    middleware = ArgumentValidationLog(server)
    context = SimpleNamespace(message=SimpleNamespace(name=name, arguments=args))
    next_call = AsyncMock()
    with caplog.at_level(logging.INFO), pytest.raises(ToolError) as error:
        asyncio.run(middleware.on_call_tool(context, next_call))
    assert str(error.value) == "Tool arguments must contain valid Unicode"
    str(error.value).encode("utf-8")
    next_call.assert_not_called()
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in caplog.text)


def test_scan_work_is_linear_and_deep_lists_do_not_recurse(monkeypatch):
    original = argument_validation._SURROGATE
    lengths = []

    class CountedPattern:
        def search(self, value):
            lengths.append(len(value))
            return original.search(value)

    monkeypatch.setattr(argument_validation, "_SURROGATE", CountedPattern())
    values = ["synthetic text " * 20 for _ in range(10000)]
    assert not argument_validation._invalid_unicode({"items": values})
    assert len(lengths) == 10001
    assert sum(lengths) == len("items") + sum(map(len, values))
    deep = "\ud800"
    for _ in range(5000):
        deep = [deep]
    assert argument_validation._invalid_unicode(deep)
