"""Provider errors are classified before they are logged or returned (#257).

The five tools that call an embed or inference provider log and return
the failure through ``safe_provider_exception_text``. A response parse,
validation or conversion error can quote the provider's response, which
can echo the prompt and so the mail, so only its type may leave the
handler. SDK status errors keep their status; connection and timeout
errors and our own fixed-message errors keep their text.
"""

import asyncio
import json
import logging

import httpx2
import openai
import pydantic
import pytest
from fastmcp.exceptions import ToolError
from src.lib.security import ProviderResponseError
from src.tools.intelligence import register_intelligence_tools
from src.tools.search import register_search_tools

from tests.conftest import FakeEmbedClient, FakeInferenceClient

_MARKER = "synthetic-mail-marker-4c1e"


def _validation_error() -> pydantic.ValidationError:
    try:
        pydantic.TypeAdapter(int).validate_python(_MARKER)
    except pydantic.ValidationError as e:
        return e
    raise AssertionError("validation unexpectedly passed")


class _StatusError(Exception):
    """SDK-shaped status error whose body echoes the request."""

    status_code = 502


def _content_errors() -> list[tuple[str, Exception]]:
    """Errors whose message quotes provider (and so mail) content."""
    return [
        ("ValidationError", _validation_error()),
        ("JSONDecodeError", json.JSONDecodeError(f"Unexpected {_MARKER}", _MARKER, 0)),
        ("TypeError", TypeError(f"cannot convert {_MARKER!r}")),
        ("RuntimeError", RuntimeError(f"bad field {_MARKER}")),
    ]


class _RaisingEmbed(FakeEmbedClient):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def embed(self, text: str) -> list[float]:
        raise self._error


def _call(tool: str, error: Exception, seeded_db, fake_server) -> str:
    """Run ``tool`` with its provider raising ``error``; return the
    ``ToolError`` text the caller receives."""
    if tool in ("search_emails", "get_evidence"):
        register_search_tools(fake_server, seeded_db, _RaisingEmbed(error))
        args: dict = {"query": "invoice"}
    else:
        register_intelligence_tools(
            fake_server,
            seeded_db,
            FakeEmbedClient(),
            FakeInferenceClient(complete_responses=[error]),
        )
        args = {
            "ask_mailbox": {"question": "What was the budget?"},
            "summarize_thread": {"thread_id": "t-alpha"},
            "extract_from_emails": {"query": "invoice", "schema": {"vendor": "string"}},
        }[tool]
    with pytest.raises(ToolError) as excinfo:
        asyncio.run(fake_server.tools[tool](**args))
    return str(excinfo.value)


_TOOLS = ["search_emails", "get_evidence", "ask_mailbox", "summarize_thread", "extract_from_emails"]


@pytest.mark.parametrize("tool", _TOOLS)
@pytest.mark.parametrize(("type_name", "error"), _content_errors(), ids=lambda v: str(v)[:20])
def test_content_error_leaves_only_its_type(tool, type_name, error, seeded_db, fake_server, caplog):
    assert _MARKER in str(error)  # the stub really quotes the marker
    with caplog.at_level(logging.DEBUG):
        text = _call(tool, error, seeded_db, fake_server)
    assert _MARKER not in text
    assert _MARKER not in caplog.text
    assert type_name in text
    assert type_name in caplog.text


@pytest.mark.parametrize("tool", _TOOLS)
def test_status_error_keeps_only_type_and_status(tool, seeded_db, fake_server, caplog):
    with caplog.at_level(logging.DEBUG):
        text = _call(tool, _StatusError(f"upstream echoed {_MARKER}"), seeded_db, fake_server)
    assert "_StatusError: status=502" in text
    assert _MARKER not in text
    assert _MARKER not in caplog.text


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(
        message="Connection error: kept-detail",
        request=httpx2.Request("POST", "http://host.docker.internal:1234/v1/embeddings"),
    )


@pytest.mark.parametrize("tool", _TOOLS)
@pytest.mark.parametrize(
    "error",
    [
        _connection_error(),
        TimeoutError("read timeout kept-detail"),
        ProviderResponseError("provider returned kept-detail"),
    ],
    ids=["APIConnectionError", "TimeoutError", "ProviderResponseError"],
)
def test_connection_and_fixed_message_errors_keep_their_text(
    tool, error, seeded_db, fake_server, caplog
):
    with caplog.at_level(logging.DEBUG):
        text = _call(tool, error, seeded_db, fake_server)
    assert "kept-detail" in text
    assert "kept-detail" in caplog.text
