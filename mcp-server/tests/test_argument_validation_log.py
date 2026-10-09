"""Arguments the tool's argument model refuses are logged through the
per-field rate limiter (#1131).

FastMCP validates a call against the argument model it builds from the
handler signature before the handler runs, so a wrong type or an
out-of-range ``Field`` never reaches the handlers' own
``ArgumentRejections``. ``ArgumentValidationLog`` records those
rejections under the same ``rejected invalid argument: <tool>.<field>``
line, rate limited, and ``DropArgumentModelWarning`` drops FastMCP's
own unlimited ``Invalid arguments for tool`` line for them.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastmcp import Client, FastMCP
from mcp.shared.exceptions import MCPError as McpError
from mcp.types import CallToolResult
from pydantic import BaseModel
from src.lib.argument_validation import ArgumentValidationLog, DropArgumentModelWarning
from src.tools.brief import register_experimental_tools
from src.tools.intelligence import register_intelligence_tools
from src.tools.retrieval import register_retrieval_tools
from src.tools.search import register_search_tools
from src.tools.system import register_system_tools

from tests.conftest import FakeEmbedClient, FakeInferenceClient

MARKER = "SYNTHETIC_SCHEMA_REJECTION_MARKER"
_FASTMCP_LINE = "Invalid arguments for tool"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fastmcp_filter():
    """The filter ``src.main`` attaches to fastmcp's server logger."""
    logger = logging.getLogger("fastmcp.server.server")
    drop = DropArgumentModelWarning()
    logger.addFilter(drop)
    yield
    logger.removeFilter(drop)


def _server(db, clock=None, *, middleware: bool = True) -> FastMCP:
    """A real server with every tool group, as ``main`` registers them
    with inference configured and ``MCP_EXPERIMENTAL_TOOLS=true``."""
    server = FastMCP("argument-validation-test")
    embed, inference = FakeEmbedClient(), FakeInferenceClient()
    register_search_tools(server, db, embed)
    register_retrieval_tools(server, db)
    register_intelligence_tools(server, db, embed, inference)
    register_experimental_tools(server, db, embed, inference)
    register_system_tools(server, db)
    if middleware:
        server.add_middleware(ArgumentValidationLog(server, clock=clock or _Clock()))
    return server


def _types(prop: dict) -> set[str]:
    """The JSON types a property's published schema allows."""
    return {v["type"] for v in prop.get("anyOf", [prop]) if "type" in v}


def _wrong_value(prop: dict) -> object:
    """A value outside the property's published type, carrying the
    marker: a string, or an object where a string is allowed."""
    types = _types(prop)
    if "string" in types and "object" not in types:
        return {"key": f"{MARKER}-value"}
    return f"{MARKER}-value"


def _required_values(schema: dict) -> dict:
    """A well-typed value for each required parameter, so the only
    rejected field in a call is the one under test."""
    values = {}
    for name in schema.get("required", []):
        prop = schema["properties"][name]
        if "enum" in prop:
            # A closed set (aggregate_messages' group_by): one member.
            values[name] = prop["enum"][0]
            continue
        values[name] = {"vendor": "string"} if prop.get("type") == "object" else "synthetic"
    return values


def _typed_calls(server: FastMCP) -> list[tuple[str, str, dict]]:
    """(tool, parameter, arguments) for every parameter with a published
    type of every tool a client lists, the parameter given a value of
    the wrong type carrying the marker. Derived from ``tools/list`` so a
    new tool or parameter is covered without editing this test."""

    async def run():
        async with Client(server) as client:
            return await client.list_tools()

    calls = []
    for tool in asyncio.run(run()):
        schema = tool.input_schema
        for name, prop in schema["properties"].items():
            if _types(prop):
                args = {**_required_values(schema), name: _wrong_value(prop)}
                calls.append((tool.name, name, args))
    return calls


def _call_all(server: FastMCP, calls) -> list[CallToolResult]:
    async def run():
        async with Client(server) as client:
            return [await client.call_tool_mcp(tool, args) for tool, _, args in calls]

    return asyncio.run(run())


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_every_typed_parameter_of_every_tool_is_rejected_through_the_limiter(
    seeded_db, caplog, fastmcp_filter
):
    clock = _Clock()
    server = _server(seeded_db, clock)
    calls = _typed_calls(server)
    # The sweep spans tools and kinds: page sizes, flags, ranges, text.
    keys = {f"{tool}.{name}" for tool, name, _ in calls}
    assert {
        "query_messages.limit",
        "query_messages.seen",
        "get_message.offset",
        "get_thread.include_attachments_metadata",
        "check_conclusion.max_threads",
        "search_emails.query",
        "summarize_thread.style",
    } <= keys
    with caplog.at_level(logging.INFO):
        results = _call_all(server, calls)
    assert all(r.is_error for r in results)
    warnings = _warnings(caplog)
    for key in keys:
        assert warnings.count(f"rejected invalid argument: {key}") == 1, key
    assert not any(_FASTMCP_LINE in w for w in warnings)
    assert MARKER not in caplog.text

    # Two more sweeps in the same minute are counted, not logged; the
    # next rejection after the minute reports every key with its count.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        _call_all(server, calls)
        _call_all(server, calls)
    assert not [w for w in _warnings(caplog) if "rejected invalid argument" in w]
    caplog.clear()
    clock.now += 61.0
    with caplog.at_level(logging.INFO):
        _call_all(server, calls[:1])
    [summary] = [w for w in _warnings(caplog) if w.startswith("rejected invalid arguments")]
    assert summary.startswith("rejected invalid arguments in the last 61s: ")
    counts = dict(item.split("=") for item in summary.split(": ", 1)[1].split())
    # Handler-checked parameters (``size_min`` / ``size_max``) are
    # counted by the handler's own limiter, not this one.
    handler_checked = {
        f"{tool}.{name}"
        for tool in ("query_messages", "aggregate_messages")
        for name in ("size_min", "size_max")
    }
    assert counts == {key: "3" for key in keys - handler_checked}
    assert MARKER not in caplog.text


def test_the_client_error_is_unchanged(seeded_db, fastmcp_filter):
    plain = _server(seeded_db, middleware=False)
    calls = _typed_calls(plain)
    expected = [r.model_dump() for r in _call_all(plain, calls)]
    assert [r.model_dump() for r in _call_all(_server(seeded_db), calls)] == expected


def test_a_pattern_rejection_is_logged_per_field_without_the_value(
    seeded_db, caplog, fastmcp_filter
):
    """query_attachments checks ``extraction_status`` against a pattern
    in the schema (#796): a value it refuses is logged like a wrong type,
    by tool and field, and the value stays out of the log."""
    with caplog.at_level(logging.INFO):
        [result] = _call_all(
            _server(seeded_db), [("query_attachments", "", {"extraction_status": MARKER})]
        )
    assert result.is_error
    assert "rejected invalid argument: query_attachments.extraction_status" in _warnings(caplog)
    assert MARKER not in caplog.text


def test_handler_and_schema_rejections_each_count_once(seeded_db, caplog, fastmcp_filter):
    """``size_min`` is checked in the handler (its own limiter);
    ``limit`` by the argument model (the middleware's). Neither is
    logged or counted by both."""
    clock = _Clock()
    server = _server(seeded_db, clock)
    calls = [
        ("query_messages", "size_min", {"size_min": "100"}),
        ("query_messages", "limit", {"limit": "abc"}),
    ]
    with caplog.at_level(logging.INFO):
        _call_all(server, calls * 2)
        clock.now += 61.0
        _call_all(server, calls[1:])
    warnings = _warnings(caplog)
    assert warnings.count("rejected invalid argument: query_messages.size_min") == 1
    assert warnings.count("rejected invalid argument: query_messages.limit") == 2
    assert "rejected invalid arguments in the last 61s: query_messages.limit=2" in warnings


def test_each_rejected_field_of_one_call_is_counted(seeded_db, caplog, fastmcp_filter):
    server = _server(seeded_db)
    with caplog.at_level(logging.INFO):
        _call_all(server, [("query_messages", "", {"limit": "abc", "seen": "maybe"})])
    warnings = _warnings(caplog)
    assert "rejected invalid argument: query_messages.limit" in warnings
    assert "rejected invalid argument: query_messages.seen" in warnings


def test_an_unexpected_argument_name_is_counted_as_other(seeded_db, caplog, fastmcp_filter):
    """A client-chosen argument name is never logged: an unexpected
    keyword maps to ``other``."""
    server = _server(seeded_db)
    with caplog.at_level(logging.INFO):
        [result] = _call_all(server, [("query_messages", "", {f"{MARKER}_name": 1})])
    assert result.is_error
    assert "rejected invalid argument: query_messages.other" in _warnings(caplog)
    assert MARKER not in caplog.text


class _Body(BaseModel):
    count: int


def test_a_body_validation_error_keeps_fastmcp_line(caplog, fastmcp_filter):
    """A pydantic error raised by the tool's own body is a server-side
    failure, not a rejected argument: FastMCP's line stays and nothing is
    counted."""
    server = FastMCP("body-error-test")

    @server.tool()
    def broken() -> str:
        return str(_Body.model_validate({"count": "not a number"}))

    server.add_middleware(ArgumentValidationLog(server))
    # fastmcp answers a body's pydantic error as a protocol error.
    with caplog.at_level(logging.INFO), pytest.raises(McpError):
        _call_all(server, [("broken", "", {})])
    warnings = _warnings(caplog)
    assert any(w.startswith(_FASTMCP_LINE) for w in warnings)
    assert not any("rejected invalid argument" in w for w in warnings)


def test_the_filter_drops_only_the_argument_model_line():
    from fastmcp.exceptions import ValidationError
    from pydantic import ValidationError as PydanticValidationError

    drop = DropArgumentModelWarning()

    def record(msg: str) -> logging.LogRecord:
        return logging.LogRecord(
            "fastmcp.server.server", logging.WARNING, __file__, 1, msg, ("t", {}), None
        )

    assert drop.filter(record("Invalid arguments for tool %r: %s"))
    try:
        raise ValidationError("synthetic")
    except ValidationError:
        assert not drop.filter(record("Invalid arguments for tool %r: %s"))
        assert not drop.filter(record("Invalid arguments for tool %r"))
        assert drop.filter(record("Error calling tool %r"))
    try:
        _Body.model_validate({"count": "x"})
    except PydanticValidationError:
        assert drop.filter(record("Invalid arguments for tool %r: %s"))


def test_main_attaches_the_filter():
    import src.main  # noqa: F401  (attaches its logging filters on import)

    filters = logging.getLogger("fastmcp.server.server").filters
    assert any(isinstance(f, DropArgumentModelWarning) for f in filters)


def test_an_unregistered_tool_or_a_non_pydantic_cause_is_counted_as_other(caplog):
    """Neither happens on the pinned fastmcp (its ``ValidationError``
    always wraps the pydantic error of a resolved tool), but such a
    rejection is still counted under fixed keys, not dropped or raised."""
    from fastmcp.exceptions import ValidationError
    from mcp.types import CallToolRequestParams

    server = FastMCP("fallback-test")

    @server.tool()
    def known(limit: int = 1) -> str:
        return "ok"

    middleware = ArgumentValidationLog(server)

    class _Context:
        message = CallToolRequestParams(name=f"{MARKER}_tool", arguments={})

    async def fail(_context):
        raise ValidationError("synthetic")

    with caplog.at_level(logging.INFO), pytest.raises(ValidationError):
        asyncio.run(middleware.on_call_tool(_Context(), fail))  # type: ignore[arg-type]
    assert "rejected invalid argument: other.other" in _warnings(caplog)
    assert MARKER not in caplog.text
