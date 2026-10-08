"""Rate-limited logging of tool arguments the argument model refuses (#1131).

FastMCP validates a call against the argument model it builds from the
handler signature (types, ``Field`` ranges, enums) before the handler
runs, so such a rejection never reaches the handlers' own
``ArgumentRejections``. FastMCP logs it instead as ``Invalid arguments
for tool ...`` at WARNING once per call, with no rate limit.
``ArgumentValidationLog`` records it under the same rate-limited
``rejected invalid argument: <tool>.<field>`` line and
``DropArgumentModelWarning`` drops FastMCP's line for it.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import ValidationError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from pydantic import ValidationError as PydanticValidationError

from .rate_limited_log import ArgumentRejections

log = logging.getLogger("mcp.tools.arguments")


class ArgumentValidationLog(Middleware):
    """Records every argument-model rejection through ``ArgumentRejections``
    and re-raises it unchanged, so the client receives the same error.

    FastMCP raises its ``ValidationError`` only for a call whose
    arguments fail the model; a pydantic error from the tool's own body
    reaches the chain as a plain pydantic ``ValidationError`` and is not
    counted. Each rejected field of the call is recorded once under
    ``<tool>.<field>``: the tool and field names come from the
    registered tools, never from the request, so a location naming
    anything else (an unexpected argument) is recorded as ``other``. The
    input value and pydantic's message text are never logged.

    Handler-checked arguments are counted by the handlers' own limiters:
    a call that fails the argument model never reaches the handler, so
    no rejection is counted twice. Read-only: the middleware changes no
    request, result or error.
    """

    def __init__(
        self,
        server: FastMCP,
        *,
        interval: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._server = server
        self._interval = interval
        self._clock = clock
        # Built on the first rejection, when every tool is registered.
        self._limiter: tuple[dict[str, frozenset[str]], ArgumentRejections] | None = None

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except ValidationError as e:
            await self._record(context.message.name, e)
            raise

    async def _state(self) -> tuple[dict[str, frozenset[str]], ArgumentRejections]:
        """Each registered tool's parameter names, and the limiter keyed
        by them."""
        if self._limiter is None:
            tools = await self._server.list_tools(run_middleware=False)
            params = {t.name: frozenset(t.parameters.get("properties", {})) for t in tools}
            limiter = ArgumentRejections(
                log,
                (*params, "other"),
                self._interval,
                self._clock,
                fields=tuple(sorted(set().union(*params.values()))),
            )
            # No await between the check and the assignment: a call that
            # raced this one keeps the limiter it built first.
            if self._limiter is None:
                self._limiter = (params, limiter)
        return self._limiter

    async def _record(self, name: str, error: ValidationError) -> None:
        params, limiter = await self._state()
        known = params.get(name)
        tool = name if known is not None else "other"
        fields: set[str] = set()
        cause = error.__cause__
        if isinstance(cause, PydanticValidationError):
            for detail in cause.errors(
                include_url=False, include_context=False, include_input=False
            ):
                loc = detail["loc"]
                field = loc[0] if loc else None
                fields.add(
                    field if known and isinstance(field, str) and field in known else "other"
                )
        else:
            fields.add("other")
        for field in sorted(fields):
            limiter.reject(tool, field)


class DropArgumentModelWarning(logging.Filter):
    """Drop fastmcp's ``Invalid arguments for tool`` WARNING for an
    argument-model rejection, which ``ArgumentValidationLog`` records
    rate-limited instead.

    fastmcp logs the line inside the ``except`` block handling its own
    ``ValidationError``, so the exception being handled tells the two
    uses of the line apart: the same text is logged for a pydantic error
    raised by a tool's body, which is kept. Other records pass.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            str(record.msg).startswith("Invalid arguments for tool")
            and isinstance(sys.exc_info()[1], ValidationError)
        )
